import json
import time
import logging
import logging.handlers
import os
from datetime import datetime, date, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import threading
from okx_api import OkxApi
from equity_tracker import EquityTracker
from turtle_strategy import TurtleStrategy
from ma_cross_strategy import MaCrossStrategy
from risk_manager import RiskManager
from dingtalk_notifier import DingTalkNotifier
from trade_state import TradeState, TradeStatePersistenceError, atomic_write_json
from stop_guardian import StopGuardianMixin
from reporting import ReportingMixin
from signal_handlers import SignalHandlersMixin
from trade_executor import TradeExecutorMixin
import config_validation as cfgv

# 日志轮转（10MB自动切割，保留5个备份）。路径锚定项目目录，避免 systemd/cron 等不同 cwd 下日志写错位置
_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading.log')
log_handler = logging.handlers.RotatingFileHandler(
    _LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8'
)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

logging.basicConfig(
    level=logging.INFO,
    handlers=[log_handler, console_handler]
)
logger = logging.getLogger(__name__)
logging.getLogger("apscheduler").setLevel(logging.WARNING)


class TradingSystem(StopGuardianMixin, ReportingMixin, SignalHandlersMixin, TradeExecutorMixin):
    """欧易单所交易系统的装配与调度核心。

    物理分层（mixin，方法仍全部绑定在本类实例上，self 语义与调用链不变）：
      - StopGuardianMixin（stop_guardian.py）：止损防线——验证式撤销确认、残留阻断、
        止损自愈巡检、「交易所已平」统一收尾、账本落盘失败的运行时补偿；
      - ReportingMixin（reporting.py）：通知缓冲/汇总、每日持仓汇总、周报、权益采样告警；
      - SignalHandlersMixin（signal_handlers.py）：两策略的信号分派——无仓开仓判定、
        有仓时的止损确认/平仓/翻转分派、海龟止损推进检查、双均线 T+1 重入；
      - TradeExecutorMixin（trade_executor.py）：下单执行——通用开仓（校验/回滚）、
        止损单更新、平仓执行、双均线翻转、开仓落盘失败的交易所侧回滚；
      - 本文件保留：装配与配置、状态迁移与归属护栏、启动同步、日检总指挥
        check_and_execute_trades 与调度注册（真钱编排核心，刻意留在 main 便于审查）。
    """

    def __init__(self, config_file='config.json'):
        """初始化欧易单交易所交易系统。状态文件落项目根目录（与原单所版一致）。"""
        self.config_file = config_file
        self.config = self.load_config(config_file)
        self.exchange_id = 'okx'
        self.label = self.config.get('okx', {}).get('label') or '欧易'
        self.base_dir = os.path.dirname(os.path.abspath(config_file))
        self.data_dir = self.base_dir
        self._config_lock = threading.RLock()

        # 旧多所版若把欧易状态存在 data/okx/，收敛单所时迁回根目录（仅当根目录尚无状态）
        self._migrate_okx_legacy_state()

        # 交易所适配层：只换成欧易，策略层输入输出语义不变
        self.exchange_api = OkxApi(self.config['okx'])

        self.turtle_strategy = TurtleStrategy(self.config['strategy']['channel_period'])
        self.ma_cross_strategy = MaCrossStrategy(
            self.config['strategy'].get('ma_short_period', 7),
            self.config['strategy'].get('ma_long_period', 28),
            self.config['strategy'].get('ma_stop_period', 28)
        )

        webhook = os.environ.get('DINGTALK_WEBHOOK') or self.config.get('dingtalk', {}).get('webhook_url')
        self.notifier = DingTalkNotifier(webhook)
        # 时区守卫：日检时点、T+1 记录、求索指数切日全用系统本地时间，部署要求
        # Asia/Shanghai（UTC+8）。不符只在启动时告警一次、不阻断——不给调度器单独
        # 钉时区（那会造出「调度上海时、业务本地时」的双时钟系统）。
        _tz_offset = datetime.now().astimezone().utcoffset()
        if _tz_offset != timedelta(hours=8):
            _tz_msg = (f'[{self.label}] 服务器时区异常：当前 UTC 偏移 {_tz_offset}，部署要求 UTC+8'
                       f'（Asia/Shanghai）。日检时点/T+1 记录/求索指数切日均依赖本地时间，'
                       f'请尽快修正服务器时区！')
            logger.critical(_tz_msg)
            try:
                self.notifier.notify_error(_tz_msg)
            except Exception:
                pass
        try:
            self.trade_state = TradeState(os.path.join(self.data_dir, 'trade_state.json'))
        except TradeStatePersistenceError as e:
            # 账本损坏且备份不可恢复（fail-closed）：广播后拒绝启动，绝不失忆运行——
            # 失忆不仅漏管旧仓，日检还会把有真实仓位的品种当空仓重复开仓
            logger.critical(f'交易状态账本不可恢复，拒绝启动: {e}')
            try:
                self.notifier.notify_error(
                    f'[{self.label}] 交易状态账本损坏且备份不可恢复，系统已拒绝启动，'
                    f'请立即人工修复 trade_state.json！\n{e}')
            except Exception:
                pass
            raise
        self._guard_state_owner()  # 校验状态归属，防止把其它交易所(如旧币安)的持仓当成欧易状态读入
        self.scheduler = BackgroundScheduler()
        self._last_check_date = None  # 防重复执行
        self._last_summary_date = None  # 每日持仓汇总去重
        self._pending_trade_open_notifications = []
        self._pending_trade_close_notifications = []
        self._pending_stop_loss_updates = []
        self._trade_lock = threading.Lock()  # 防并发执行锁
        self._summary_lock = threading.Lock()  # 每日汇总「查重→推送→标记」的原子化（兜底调度与日检可能并发）
        self._stop_anomalies = {}  # 止损异常状态（mismatch/补挂失败），供前端警示与告警节流
        self._last_failure_notify_ts = 0
        self._equity_tick_fail_streak = 0
        self._equity_tick_alert_sent = False

        self.stop_loss_file = os.path.join(self.data_dir, 'stop_loss_dates.json')
        self.stop_loss_dates = self._load_stop_loss_dates()

        self.equity_tracker = EquityTracker(
            self.data_dir, self,
            notify_failure=self._notify_persistence_failure,
            retention_days=self.config.get('equity_tick_retention_days'),
        )

        # 启动时必须成功获取权益，重试3次
        account_equity = None
        for _retry_i in range(3):
            balance = self.exchange_api.get_balance()
            if balance and 'USDT' in balance.get('total', {}):
                account_equity = balance['total']['USDT']
                break
            logger.warning(f'[{self.label}] 启动时获取账户权益失败，10秒后重试... (第{_retry_i+1}/3次)')
            time.sleep(10)

        if account_equity is None:
            logger.critical('系统启动失败：3次尝试后仍无法获取初始账户权益！请检查API密钥和网络连接。')
            # 退出前尽力补发钉钉：系统静默死掉是最贵的故障模式（与账本损坏路径同标准）
            try:
                self.notifier.notify_error(
                    f'[{self.label}] 系统启动失败：3次尝试后仍无法获取初始账户权益，'
                    f'进程即将退出，请检查API密钥和网络连接！')
            except Exception:
                pass
            import sys
            sys.exit(1)

        self.risk_manager = RiskManager(account_equity, self.config['strategy']['default_risk_per_trade'])
        logger.info(f"[{self.label}] 交易系统初始化完成，账户权益: {account_equity} USDT")

        self.sync_positions_on_startup()

    def load_config(self, config_file):
        """加载欧易单所配置：{okx:{凭据...}, strategy, trading, scheduler, dingtalk}。
        兼容旧多所格式（exchanges.okx）自动展平；环境变量可覆盖凭据。"""
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        # 兼容旧多所格式：从 exchanges.okx 展平到顶层
        if 'okx' not in config and isinstance(config.get('exchanges'), dict):
            okx_block = dict(config['exchanges'].get('okx') or {})
            config['okx'] = okx_block
            config.setdefault('strategy', okx_block.get('strategy', {}))
            config.setdefault('trading', okx_block.get('trading', {'symbols': []}))
        okx = config.setdefault('okx', {})
        if os.environ.get('OKX_API_KEY'):
            okx['apiKey'] = os.environ['OKX_API_KEY']
        if os.environ.get('OKX_API_SECRET'):
            okx['secret'] = os.environ['OKX_API_SECRET']
        _pass = os.environ.get('OKX_API_PASSPHRASE') or os.environ.get('OKX_PASSWORD')
        if _pass:
            okx['password'] = _pass
        if not okx.get('apiKey') or not okx.get('secret') or not okx.get('password'):
            raise ValueError('未配置 OKX API 凭据（apiKey/secret/passphrase），请在 config.json 或环境变量中提供')
        config.setdefault('strategy', {})
        self._validate_strategy_config(config['strategy'])
        config.setdefault('trading', {'symbols': []})
        config['trading'].setdefault('symbols', [])
        self._validate_symbol_configs(config['trading']['symbols'])
        config.setdefault('scheduler', {})
        self._validate_scheduler_config(config['scheduler'])
        config.setdefault('dingtalk', {})
        return config

    def _validate_scheduler_config(self, scheduler):
        """启动前校验并规范化调度参数（就地写回 config，消除类型漂移）。

        这些整数直接流入 register_jobs 的算术与格式化（check_minute + 1、
        {check_hour:02d}）和 APScheduler 的 hour/minute 字段。此前无校验：手写
        config.json 把 check_minute 写成字符串 "0" 会让 `check_minute + 1` 抛
        TypeError、把 check_hour 写成 25 会让 APScheduler 抛内部错——都是难懂的
        启动崩溃。与 strategy/symbols 同源、同标准 fail-loud：给清晰 ValueError。
        全部为可选键（缺省由 register_jobs 的 .get 默认值兜底），仅当显式提供时校验。
        """
        hour_keys = ('check_hour', 'summary_hour', 'weekly_hour')
        minute_keys = ('check_minute', 'summary_minute', 'weekly_minute')
        for key in hour_keys:
            if scheduler.get(key) is None:
                continue
            v = cfgv.strict_int(scheduler[key], f'config.scheduler.{key}')
            if not (0 <= v <= 23):
                raise ValueError(f"config.scheduler.{key} 超出允许范围 [0, 23]: {v}")
            scheduler[key] = v
        for key in minute_keys:
            if scheduler.get(key) is None:
                continue
            v = cfgv.strict_int(scheduler[key], f'config.scheduler.{key}')
            if not (0 <= v <= 59):
                raise ValueError(f"config.scheduler.{key} 超出允许范围 [0, 59]: {v}")
            scheduler[key] = v
        if scheduler.get('stop_loss_scan_interval_minutes') is not None:
            v = cfgv.strict_int(scheduler['stop_loss_scan_interval_minutes'],
                                'config.scheduler.stop_loss_scan_interval_minutes')
            if not (1 <= v <= 1440):
                raise ValueError(
                    f"config.scheduler.stop_loss_scan_interval_minutes 超出允许范围 [1, 1440]: {v}")
            scheduler['stop_loss_scan_interval_minutes'] = v

    def _validate_strategy_config(self, strategy):
        """启动前校验并**规范化**策略参数（就地写回 config，消除类型漂移）。

        口径全部取自 config_validation（与前端/API 同一事实源）。channel_period /
        default_risk_per_trade 在装配层是直接下标访问（非 .get 兜底），缺失或非法会在
        运行中抛裸异常或产出危险仓位（channel_period=0 让通道计算崩溃、风险度<0 算出负仓位）。
        校验后必须写回规范类型——否则 "28"/"0.01" 字符串能通过校验却仍是字符串，构造
        TurtleStrategy("28")→盘中 `int < str` TypeError、RiskManager 权益×"0.01"→TypeError。
        与凭据缺失同标准 fail-loud，绝不静默塞默认值（真钱系统默认策略参数比拒启更危险）。
        """
        missing = [k for k in ('channel_period', 'default_risk_per_trade') if strategy.get(k) is None]
        if missing:
            raise ValueError(
                f"config.strategy 缺少必需参数 {missing}，请对照 config.example.json 补全后再启动")

        # 周期类：channel_period 必校；ma_* 三键有 .get 默认值，仅当显式提供时校验。规范化写回 int
        for key in ('channel_period', 'ma_short_period', 'ma_long_period', 'ma_stop_period'):
            if strategy.get(key) is None:
                continue
            v = cfgv.strict_int(strategy[key], f'config.strategy.{key}')
            if not (cfgv.PERIOD_MIN <= v <= cfgv.PERIOD_MAX):
                raise ValueError(
                    f"config.strategy.{key} 超出允许范围 [{cfgv.PERIOD_MIN}, {cfgv.PERIOD_MAX}]: {v}")
            strategy[key] = v

        strategy['default_risk_per_trade'] = cfgv.strict_risk_per_trade(
            strategy['default_risk_per_trade'], 'config.strategy.default_risk_per_trade')

        # EMA 短期必须小于长期（用生效值判定：缺省短 7 / 长 28，与构造处默认一致）
        eff_short = strategy.get('ma_short_period', 7)
        eff_long = strategy.get('ma_long_period', 28)
        if eff_short >= eff_long:
            raise ValueError(f"config.strategy EMA 短期({eff_short})必须小于长期({eff_long})")

    def _validate_symbol_configs(self, symbols):
        """启动前校验并规范化交易对池——与 api_server._validate_symbol_input 同口径（同源常量）。

        手写 config.json 的品种 risk_per_trade / strategy / name 此前无启动校验：
        risk_per_trade=1.0（100%）会直接进 _execute_open 的仓位计算放大到全仓风险；
        非法策略名会在 get_strategy_for_symbol 静默落到海龟（错误托管）。补齐三校验，
        与增删品种的 API 入口一致，堵住"手写配置"这条绕过风控的入口。
        """
        seen = set()
        for i, s in enumerate(symbols):
            if not isinstance(s, dict):
                raise ValueError(f"config.trading.symbols[{i}] 不是对象: {s!r}")
            name = cfgv.normalize_symbol_name(s.get('name'), f"config.trading.symbols[{i}] 交易对名")
            if name in seen:
                raise ValueError(f"config.trading.symbols 存在重复交易对: {name}")
            seen.add(name)
            s['name'] = name  # 规范化写回（去空格/转大写）

            if s.get('risk_per_trade') is not None:  # 缺省时由 default_risk_per_trade 兜底（既有行为）
                s['risk_per_trade'] = cfgv.strict_risk_per_trade(s['risk_per_trade'], f"{name} risk_per_trade")
            if s.get('enabled') is not None:  # 缺省时 .get('enabled', True) 兜底（既有行为）
                s['enabled'] = cfgv.strict_bool(s['enabled'], f"{name} enabled")  # 挡 "false" 被当真
            if s.get('strategy') is not None and s['strategy'] not in cfgv.STRATEGY_WHITELIST:
                raise ValueError(f"{name} 未知策略: {s['strategy']!r}（只支持 turtle / ma_cross）")

    def _migrate_okx_legacy_state(self):
        """旧多所版把欧易状态存在 data/okx/，收敛单所时迁回根目录。

        边界保护（本地持仓状态是命脉，不能被空文件覆盖或绕过）：
        - 根目录无 trade_state.json：正常迁移；
        - 根目录有但**空仓**、而 data/okx/ 旧文件**有持仓**：备份空文件后迁移旧持仓；
        - 两边都有持仓：无法自动裁决，拒绝启动等人工；
        - 任一文件读取失败：拒绝启动（不能在持仓不明的情况下继续）。
        """
        import shutil
        legacy_dir = os.path.join(self.base_dir, 'data', 'okx')
        if not os.path.isdir(legacy_dir):
            return
        root_ts = os.path.join(self.base_dir, 'trade_state.json')
        legacy_ts = os.path.join(legacy_dir, 'trade_state.json')

        if os.path.exists(root_ts):
            if not os.path.exists(legacy_ts):
                return
            try:
                with open(legacy_ts, encoding='utf-8') as f:
                    legacy_positions = json.load(f).get('open_positions') or {}
                with open(root_ts, encoding='utf-8') as f:
                    root_positions = json.load(f).get('open_positions') or {}
            except Exception as e:
                raise RuntimeError(
                    f"状态迁移前读取 trade_state.json 失败({e})，持仓不明拒绝启动。"
                    f"请人工检查 {root_ts} 与 {legacy_ts}")
            if not legacy_positions:
                return  # 旧文件无持仓，根目录维持现状
            if root_positions:
                raise RuntimeError(
                    "根目录与 data/okx/ 的 trade_state.json 都含持仓，无法自动选择，已拒绝启动。"
                    "请人工核对欧易实际持仓，保留正确的一份（把另一份移走）后再启动。")
            backup = f"{root_ts}.bak.empty.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(root_ts, backup)
            os.remove(root_ts)
            logger.warning(f"根目录 trade_state.json 为空仓而 data/okx/ 有持仓：空文件已备份到 {backup}，开始迁移旧持仓状态")
        moved = []
        for fn in ['trade_state.json', 'stop_loss_dates.json', 'peak_equity.json',
                   'equity_history.json', 'daily_equity.json', 'equity_ticks.json', 'qiusuo_index.json']:
            src = os.path.join(legacy_dir, fn)
            dst = os.path.join(self.base_dir, fn)
            if os.path.exists(src) and not os.path.exists(dst):
                try:
                    shutil.copy2(src, dst)
                    moved.append(fn)
                except Exception as e:
                    logger.error(f"迁移欧易状态 {fn} 失败: {e}")
        if moved:
            logger.warning(f"已把 data/okx/ 的欧易状态迁回根目录: {moved}（老文件保留作备份）")
        if 'trade_state.json' in moved:
            # 来源 data/okx 明确属欧易，固化归属标记，避免之后启动校验把它当来路不明状态拦下
            ts_path = os.path.join(self.base_dir, 'trade_state.json')
            try:
                with open(ts_path, encoding='utf-8') as f:
                    ts = json.load(f)
                ts['exchange'] = 'okx'
                atomic_write_json(ts_path, ts)
            except Exception as e:
                logger.error(f"标记迁移状态归属失败: {e}")

    def _guard_state_owner(self):
        """启动前校验本地状态归属，防止把其它交易所(如旧币安)的持仓当成欧易状态读入。

        - 已标记为本所(okx)：放行。
        - 标记为其它交易所：拒绝启动。
        - 未标记且无持仓：视为全新状态，安全地打上本所标记。
        - 未标记但已有持仓：来路不明(可能是旧币安状态)，拒绝启动并要求人工确认。
        """
        owner = self.trade_state.get_owner_exchange()
        has_positions = bool(self.trade_state.get_all_open_positions())
        if owner == self.exchange_id:
            return
        if owner and owner != self.exchange_id:
            raise RuntimeError(
                f"状态文件 trade_state.json 归属交易所为「{owner}」，与当前「{self.exchange_id}」不一致，"
                "已拒绝启动以避免串仓。请改用独立目录部署，或人工确认后修正归属标记。"
            )
        # owner 为空：未标记
        if not has_positions:
            self.trade_state.claim_owner_exchange(self.exchange_id)
            logger.info(f"[{self.label}] 全新本地状态，已标记归属交易所为 {self.exchange_id}")
            return
        raise RuntimeError(
            "检测到根目录 trade_state.json 含持仓但无交易所归属标记，可能是旧币安单所版遗留状态。"
            "为避免把币安持仓当作欧易仓位管理(错误止损/平仓)，已拒绝启动。请人工确认后二选一：\n"
            "  1) 若该状态确属欧易：在 trade_state.json 顶层加 \"exchange\": \"okx\" 后重启；\n"
            "  2) 若不确定或属币安：把欧易系统部署到独立目录(避免与旧状态混用)。"
        )

    def persist_config(self):
        """把当前 config 原子写回磁盘（增删品种/改参数后调用）。"""
        with self._config_lock:
            return atomic_write_json(self.config_file, self.config)

    def reload_strategies(self):
        """重新加载策略参数"""
        self.turtle_strategy = TurtleStrategy(self.config['strategy']['channel_period'])
        self.ma_cross_strategy = MaCrossStrategy(
            self.config['strategy'].get('ma_short_period', 7),
            self.config['strategy'].get('ma_long_period', 28),
            self.config['strategy'].get('ma_stop_period', 28)
        )
        logger.info(f"策略参数已重新加载: 海龟周期={self.config['strategy']['channel_period']}, "
                    f"EMA短期={self.config['strategy'].get('ma_short_period', 7)}, "
                    f"EMA长期={self.config['strategy'].get('ma_long_period', 28)}, "
                    f"EMA止损周期={self.config['strategy'].get('ma_stop_period', 28)}")

    def sync_positions_on_startup(self):
        """启动时与交易所同步持仓状态"""
        logger.info("开始同步持仓状态...")
        open_positions = self.trade_state.get_all_open_positions()

        for symbol in list(open_positions.keys()):
            ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
            position = self.exchange_api.get_position(ccxt_symbol)

            if position is None or position.get('contracts', 0) == 0:
                logger.warning(f"{symbol} 在交易所中没有持仓，但本地记录有，更新状态...")
                try:
                    exit_price = self.exchange_api.get_last_price(ccxt_symbol) or open_positions[symbol]['entry_price']
                except Exception:
                    exit_price = open_positions[symbol]['entry_price']
                closed_position, _state_saved, _stop_cleared = self._handle_exchange_flat_close(
                    symbol, ccxt_symbol, open_positions[symbol], exit_price, "启动同步平仓")
                if not closed_position:
                    logger.warning(f"{symbol} 启动同步时本地状态补偿失败，保留原状态等待人工处理")
            else:
                logger.info(f"{symbol} 持仓同步成功")

        # 反向核对：交易所有仓但本地无记录（本地状态损坏丢失/人工开仓）——
        # 该仓不会被系统托管（不推止损、不检查平仓），静默存在比报错更危险，必须告警
        try:
            exchange_symbols = set(self.exchange_api.list_position_symbols())
            local_symbols = set(self.trade_state.get_all_open_positions().keys())
            orphans = sorted(exchange_symbols - local_symbols)
            if orphans:
                msg = (f"发现交易所端存在、但本地无记录的持仓: {', '.join(orphans)}。"
                       f"系统不会自动接管（可能是人工仓位或本地状态丢失），也不会为其推进止损/平仓，"
                       f"请立即人工确认处理！")
                logger.critical(msg)
                self.notifier.notify_error(msg)
        except Exception as e:
            logger.warning(f"启动孤儿仓核对失败（不阻断启动）: {e}")

        logger.info("持仓状态同步完成")

    def get_strategy_for_symbol(self, symbol_config):
        """根据交易对配置获取对应策略"""
        strategy_type = symbol_config.get('strategy', 'turtle')
        if strategy_type == 'ma_cross':
            return self.ma_cross_strategy, 'ma_cross'
        else:
            return self.turtle_strategy, 'turtle'

    def _load_stop_loss_dates(self):
        """从文件加载止损日期记录"""
        if os.path.exists(self.stop_loss_file):
            try:
                with open(self.stop_loss_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                logger.info(f"已加载止损日期记录: {data}")
                return data
            except Exception as e:
                logger.warning(f"加载止损日期文件失败: {e}")
        return {}

    def _save_stop_loss_dates(self):
        """保存止损日期记录到文件（原子写入）"""
        if not atomic_write_json(self.stop_loss_file, self.stop_loss_dates):
            logger.error(f"保存止损日期文件失败: {self.stop_loss_file}")

    def is_stop_loss_today(self, symbol):
        """检查该交易对今天是否已经止损过（T+1限制）"""
        if symbol in self.stop_loss_dates:
            today_str = date.today().strftime('%Y-%m-%d')
            if self.stop_loss_dates[symbol] == today_str:
                return True
        return False

    def record_stop_loss(self, symbol):
        """记录止损日期（持久化到文件）"""
        self.stop_loss_dates[symbol] = date.today().strftime('%Y-%m-%d')
        self._save_stop_loss_dates()

    def check_and_execute_trades(self, manual_run=False):
        """检查并执行交易"""
        # 三重防护：线程锁 + 日期检查 + APScheduler max_instances
        if not self._trade_lock.acquire(blocking=False):
            logger.warning("交易检查正在执行中(锁冲突)，跳过本次触发")
            return
        try:
            self.equity_tracker.record_daily_equity_snapshot()  # 记录每日权益快照
            # (文件顶部已导入date)
            today = date.today().isoformat()
            if self._last_check_date == today and not manual_run:
                logger.warning(f"今日({today})已执行过交易检查，跳过重复执行")
                return
            logger.info("开始检查交易信号...")
            self._pending_trade_open_notifications = []
            self._pending_trade_close_notifications = []
            self._pending_stop_loss_updates = []

        # 获取所有需要监控的交易对
            all_open_positions = self.trade_state.get_all_open_positions()
            symbols_to_check = set()

            # 添加手动池交易对
            for s in self.config['trading']['symbols']:
                if s.get('enabled', True):
                    symbols_to_check.add(s['name'])

            # 添加有开仓的交易对
            symbols_to_check.update(all_open_positions.keys())

            logger.info(f"本轮检查交易对数: {len(symbols_to_check)}")

            # 先重试清理止损残留（清理确认后解除对应品种的开仓阻断）
            self._retry_clear_stop_residues()

            # 第三步：逐个检查交易对（排序保证遍历与日志顺序确定，跨轮可对比）
            failed_symbols = []
            for symbol in sorted(symbols_to_check):
                # 单品种异常只跳过该品种，不得中断其余品种的止损推进/平仓检查（真钱红线）
                try:
                    # 检查是否应该监控该交易对（必须在手动池中或有持仓）
                    if symbol not in all_open_positions:
                        manual_names = [s['name'] for s in self.config['trading']['symbols'] if s.get('enabled', True)]
                        if symbol not in manual_names:
                            logger.debug(f"{symbol} 不在手动品种池中，跳过")
                            continue

                    # 获取策略配置
                    strategy_type = 'turtle'
                    symbol_config = None

                    for s in self.config['trading']['symbols']:
                        if s['name'] == symbol:
                            symbol_config = s
                            strategy_type = s.get('strategy', 'turtle')
                            break

                    if symbol_config is None:
                        # 品种已从手动池删除但仍有持仓：优先用「持仓记录的策略」，避免错按 turtle 退出
                        held = all_open_positions.get(symbol) or {}
                        held_strategy = held.get('strategy') or 'turtle'
                        symbol_config = {
                            'name': symbol,
                            'enabled': True,
                            'risk_per_trade': self.config['strategy']['default_risk_per_trade'],
                            'strategy': held_strategy
                        }
                        strategy_type = held_strategy

                    strategy, strategy_type = self.get_strategy_for_symbol(symbol_config)
                    logger.info(f"检查 {symbol} (策略: {strategy_type})...")

                    ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
                    required_closed_candles = cfgv.required_closed_candles_for_strategy(
                        strategy_type, self.config.get('strategy', {}))
                    fetch_limit = cfgv.ohlcv_fetch_limit_for_strategy(
                        strategy_type, self.config.get('strategy', {}))

                    ohlcv = self.exchange_api.fetch_ohlcv(ccxt_symbol, '1d', limit=fetch_limit)
                    if not ohlcv:
                        logger.warning(f"{symbol} 获取K线数据失败")
                        continue

                    df = self.exchange_api.ohlcv_to_dataframe(ohlcv)
                    df = self.exchange_api.filter_closed_candles(df, timeframe='1d')
                    if len(df) == 0:
                        logger.warning(f"{symbol} 无已收盘K线，跳过本轮检查")
                        continue
                    if len(df) < required_closed_candles:
                        logger.warning(
                            f"{symbol} K线数据不足：{strategy_type} 策略配置至少需要 "
                            f"{required_closed_candles} 根已收盘K线，本轮仅取得 {len(df)} 根"
                            f"（请求 {fetch_limit} 根），请检查周期配置或交易所历史K线供应")
                        continue

                    if strategy_type == 'turtle':
                        mid_line_crossed = self.trade_state.get_signal_state(symbol)
                        try:
                            # 这里只回填“截至上一根K线”的可开仓状态，不能把今天刚发生的
                            # 首次突破提前算成已消耗，否则会吞掉本轮本该执行的开仓信号。
                            armed = self.turtle_strategy.is_first_breakout_armed(df, include_latest_bar=False)
                            if armed != mid_line_crossed:
                                self.trade_state.set_signal_state(symbol, armed)
                                mid_line_crossed = armed
                                if armed:
                                    logger.info(f"{symbol} [海龟] 历史回溯判定为可开仓状态（中轨后尚未突破），已激活")
                                else:
                                    logger.info(f"{symbol} [海龟] 历史回溯判定为未激活状态（首次突破资格已消耗或尚未有效穿越中轨），已重置")
                        except Exception as e:
                            logger.warning(f"{symbol} [海龟] 历史回溯开仓状态失败: {e}")

                        signal = strategy.check_signal(df, mid_line_crossed=mid_line_crossed)
                        if signal and signal.get('mid_line_crossed'):
                            self.trade_state.set_signal_state(symbol, True)
                    else:
                        signal = strategy.check_signal(df)

                    if not signal:
                        logger.warning(f"{symbol} 策略未返回信号，跳过本轮检查")
                        continue

                    current_close = float(df['close'].iloc[-1])
                    if strategy_type == 'turtle':
                        upper = signal.get('upper_line')
                        lower = signal.get('lower_line')
                        mid = signal.get('mid_line')
                        if upper and lower:
                            dist_upper = (upper - current_close) / current_close * 100
                            dist_lower = (current_close - lower) / current_close * 100
                            signal_label = self._get_turtle_signal_label(signal)
                            logger.info(f"{symbol} [海龟指标] 收盘价={current_close:.2f}, "
                                       f"上轨={upper:.2f}(距{dist_upper:+.2f}%), "
                                       f"下轨={lower:.2f}(距{dist_lower:+.2f}%), "
                                       f"中轨={mid:.2f}, 信号={signal_label}")
                    elif strategy_type == 'ma_cross':
                        ema_s = signal.get('ema_short')
                        ema_l = signal.get('ema_long')
                        upper_stop = signal.get('upper_stop')
                        lower_stop = signal.get('lower_stop')
                        logger.info(f"{symbol} [双均线指标] 收盘价={current_close:.2f}, "
                                   f"EMA短={ema_s:.2f}, EMA长={ema_l:.2f}, "
                                   f"N日高={upper_stop:.2f}, N日低={lower_stop:.2f}, "
                                   f"信号={signal.get('action', '无')}")

                    position = self.trade_state.get_open_position(symbol)

                    if position:
                        if strategy_type == 'turtle':
                            self.handle_open_position_turtle(symbol, signal, position, symbol_config)
                        elif strategy_type == 'ma_cross':
                            self.handle_open_position_ma_cross(symbol, signal, position, symbol_config, df)
                    else:
                        if strategy_type == 'turtle':
                            self.handle_no_position_turtle(symbol, signal, symbol_config, df)
                        elif strategy_type == 'ma_cross':
                            self.handle_no_position_ma_cross(symbol, signal, symbol_config, df)

                except Exception as sym_e:
                    logger.exception(f"{symbol} 本轮检查异常，跳过该品种继续: {sym_e}")
                    failed_symbols.append(symbol)

            # 信号检查完成后按汇总顺序推送，避免 08:00 单条消息过多触发限流
            self._flush_pending_trade_notifications()
            if self._pending_stop_loss_updates:
                logger.info(f"信号检查完毕，推送止损更新汇总({len(self._pending_stop_loss_updates)}条)...")
                self.notifier.notify_stop_loss_updates_summary(self._pending_stop_loss_updates)
            logger.info("信号检查完毕，刷新账户统计状态...")
            self.equity_tracker.refresh_account_stats_state()
            logger.info("信号检查完毕，推送每日持仓汇总...")
            self.send_daily_position_summary_if_due(mark_sent=not manual_run)
            if failed_symbols:
                # 不标记当日完成：让 +1 分钟的重试调度整轮重跑（开仓/止损/平仓均有幂等防护）
                logger.error(f"本轮 {len(failed_symbols)} 个品种检查异常: {', '.join(sorted(failed_symbols))}，"
                             f"今日暂不标记完成，等待重试调度整轮重跑")
                now_ts = int(time.time())
                if now_ts - self._last_failure_notify_ts >= 600:
                    self._last_failure_notify_ts = now_ts
                    self.notifier.send_message(
                        "交易检查部分品种失败",
                        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"失败品种: {', '.join(sorted(failed_symbols))}\n其余品种已正常检查")
            elif not manual_run:
                # 手动检查不标记当日完成：00:00–08:00 间手动触发跑的是昨日已收盘数据，
                # 若标记会让当天 08:00 的正式日检被跳过，整日的新信号与止损推进丢失
                self._last_check_date = today
        except Exception as e:
            logger.exception(f"交易检查异常: {e}")
            try:
                now_ts = int(time.time())
                if now_ts - self._last_failure_notify_ts >= 600:
                    self._last_failure_notify_ts = now_ts
                    self.notifier.send_message("交易检查失败",
                        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n错误: {e}")
            except Exception as ne:
                logger.warning(f"发送失败告警失败: {ne}")
        finally:
            self._pending_trade_open_notifications = []
            self._pending_trade_close_notifications = []
            self._pending_stop_loss_updates = []
            self._trade_lock.release()
            logger.info("交易检查锁已释放")


    def _run_startup_catchup_check(self, now=None):
        """兜底补跑：已过今日检查时间而今日未执行过日检时，立即补跑一轮
        （启动时调用一次 + 每 30 分钟周期兜底，守卫幂等，已跑则空转）。

        场景：服务器恰在 08:00 前后宕机/重启，错过当天全部调度点——不补跑则当天的
        新信号与止损推进整日缺席。信号基于已收盘日线，补跑与 08:00 正点执行等价；
        重启后 _last_check_date 必然为空，午后重启也会补跑一轮——幂等防护
        （持仓检查/同价跳过/T+1/张数上限）保证重复执行无副作用，且顺带自校验一遍状态。
        缓冲 2 分钟：恰在调度窗口内启动时，让正常 cron（:05/:20/:40 与 +1 分钟重试）先走。
        """
        now = now or datetime.now()
        sched = self.config.get('scheduler', {})
        check_hour = sched.get('check_hour', 8)
        check_minute = sched.get('check_minute', 0)
        # 按当日绝对分钟数比较，避免 check_minute 接近 59 时 check_minute+2 溢出
        # 让缓冲窗口错误地跨过整点（如 08:59 的缓冲应到 09:01，而非落在 (8,61) 的元组里）
        if now.hour * 60 + now.minute < check_hour * 60 + check_minute + 2:
            return
        if self._last_check_date == date.today().isoformat():
            return
        logger.warning(f"[{self.label}] 已过今日 {check_hour:02d}:{check_minute:02d} 检查时间且今日未执行，兜底补跑一轮日检")
        self.check_and_execute_trades()

    def _apply_deploy_restart_skip_catchup(self):
        """部署重启专用护栏：显式要求时只跳过今天的启动兜底日检。

        实盘晚间滚动代码时，重启会让内存级 _last_check_date 丢失；若已过 08:00，
        启动兜底会立刻按上一根已收盘日线再跑一轮，可能管理当前实盘仓位。部署方可在
        本次重启的进程环境里设 TRADING_SKIP_STARTUP_CATCHUP_ONCE=1，把今日标记为已
        日检，避免启动/30分钟兜底补跑；次日自然恢复正常 08:00 日检。
        """
        if os.environ.get('TRADING_SKIP_STARTUP_CATCHUP_ONCE') != '1':
            return False
        today = date.today().isoformat()
        self._last_check_date = today
        logger.warning(
            f"[{self.label}] 已按 TRADING_SKIP_STARTUP_CATCHUP_ONCE=1 标记今日({today})已日检，"
            "本次部署重启跳过启动兜底补跑；次日正常恢复"
        )
        return True

    def start(self):
        """启动交易系统：注册定时任务、启动调度、阻塞主循环。"""
        logger.info("启动交易系统...")
        skip_startup_catchup = self._apply_deploy_restart_skip_catchup()
        self.register_jobs(self.config.get('scheduler', {}))
        self.scheduler.start()
        logger.info(f"[{self.label}] 调度已启动，等待定时任务...")
        if not skip_startup_catchup:
            self._run_startup_catchup_check()
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("收到中断信号，关闭交易系统...")
            self.scheduler.shutdown()

    def register_jobs(self, scheduler_config=None):
        """把定时任务注册到本系统的调度器。"""
        scheduler_config = scheduler_config or {}
        ex = self.exchange_id
        check_hour = scheduler_config.get('check_hour', 8)
        check_minute = scheduler_config.get('check_minute', 0)
        summary_hour = scheduler_config.get('summary_hour', 8)
        summary_minute = scheduler_config.get('summary_minute', 0)
        weekly_hour = scheduler_config.get('weekly_hour', 8)
        weekly_minute = scheduler_config.get('weekly_minute', 1)

        # 日内权益采样（默认5分钟）用于前端求索指数
        self.scheduler.add_job(self._record_equity_tick_with_alert, 'cron',
                              id=f'{ex}_equity_tick', max_instances=1, coalesce=True, misfire_grace_time=120,
                              minute='*/5', second=15)

        stop_loss_scan_interval = max(1, int(scheduler_config.get('stop_loss_scan_interval_minutes', 5)))
        if stop_loss_scan_interval <= 59:
            # cron 分钟步长仅支持 [1,59]：对齐到每 N 分钟的 :45 秒，错开权益采样(:15)与日检(:05/20/40)
            self.scheduler.add_job(self.reconcile_intraday_stop_losses, 'cron',
                                  id=f'{ex}_stoploss_scan', max_instances=1, coalesce=True, misfire_grace_time=120,
                                  minute=f'*/{stop_loss_scan_interval}', second=45)
        else:
            # 间隔 ≥ 60 分钟：cron 的 minute='*/N' 会被 APScheduler 拒绝（步长 > 59，抛
            # "step value higher than the total range"）——那会在守护线程里让整个调度注册崩溃，
            # 交易线程静默死亡（Web 面板照常、日检/巡检/采样全部不跑）。改用 interval 触发器，
            # 覆盖 _validate_scheduler_config 放行的 [60,1440] 全区间，与校验口径一致。
            self.scheduler.add_job(self.reconcile_intraday_stop_losses, 'interval',
                                  id=f'{ex}_stoploss_scan', max_instances=1, coalesce=True, misfire_grace_time=120,
                                  minutes=stop_loss_scan_interval)

        # 主执行 + 短窗口重试（成功一次后由 _last_check_date 拦截重复）
        self.scheduler.add_job(self.check_and_execute_trades, 'cron',
                              id=f'{ex}_daily_check', max_instances=1, coalesce=True, misfire_grace_time=60,
                              hour=check_hour, minute=check_minute, second='5,20,40')
        if check_minute < 59:
            self.scheduler.add_job(self.check_and_execute_trades, 'cron',
                                  id=f'{ex}_daily_check_retry', max_instances=1, coalesce=True, misfire_grace_time=60,
                                  hour=check_hour, minute=check_minute + 1, second=0)
        else:
            logger.warning(f"[{self.label}] check_minute=59，跳过 +1 分钟重试任务")
        # 日检兜底：主执行与 +1 分钟重试整窗失败（如恰逢网络故障）后当日再无触发点——
        # 每 30 分钟由幂等守卫补跑（时间窗 + _last_check_date + 交易锁，已跑则空转）
        self.scheduler.add_job(self._run_startup_catchup_check, 'cron',
                              id=f'{ex}_daily_check_fallback', max_instances=1, coalesce=True, misfire_grace_time=120,
                              minute='*/30', second=0)
        # 每日持仓汇总保持独立兜底调度，避免交易检查提前返回/异常时漏推
        self.scheduler.add_job(self.send_daily_position_summary_if_due, 'cron',
                              id=f'{ex}_daily_summary', max_instances=1, coalesce=True, misfire_grace_time=120,
                              hour=summary_hour, minute=summary_minute, second=50)
        if summary_minute < 59:
            self.scheduler.add_job(self.send_daily_position_summary_if_due, 'cron',
                                  id=f'{ex}_daily_summary_retry', max_instances=1, coalesce=True, misfire_grace_time=120,
                                  hour=summary_hour, minute=summary_minute + 1, second=20)
        else:
            logger.warning(f"[{self.label}] summary_minute=59，跳过 +1 分钟持仓汇总重试任务")
        # 与其余任务同一防护口径：APScheduler 默认 misfire_grace_time=1 秒，周一恰逢
        # 日检占线/重启窗口会静默跳过整周报告，宽限 2 分钟内补发
        self.scheduler.add_job(self.send_weekly_report, 'cron',
                              id=f'{ex}_weekly', max_instances=1, coalesce=True, misfire_grace_time=120,
                              day_of_week='mon', hour=weekly_hour, minute=weekly_minute, second=0)

        # 启动即采一次权益
        self._record_equity_tick_with_alert()
        logger.info(
            f"[{self.label}] 定时任务已注册，每日检查 {check_hour:02d}:{check_minute:02d}，"
            f"盘中止损巡检每 {stop_loss_scan_interval} 分钟"
        )


if __name__ == '__main__':
    TradingSystem().start()
