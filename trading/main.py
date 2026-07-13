import copy
import hashlib
import json
import math
import sys
import time
import logging
import logging.handlers
import os
import stat
from datetime import datetime, date, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import threading
from okx_api import OkxApi
from equity_tracker import EquityTracker
from turtle_strategy import TurtleStrategy
from ma_cross_strategy import MaCrossStrategy
from risk_manager import RiskManager
from dingtalk_notifier import DingTalkNotifier
from trade_state import (
    TradeState, TradeStatePersistenceError, atomic_write_json,
    open_private_text_file, private_file_exists,
)
from stop_guardian import StopGuardianMixin
from reporting import ReportingMixin
from signal_handlers import SignalHandlersMixin
from trade_executor import TradeExecutorMixin
from runtime_guard import acquire_runner_lock
import config_validation as cfgv

# 日志轮转（10MB自动切割，保留5个备份）。路径锚定项目目录，避免 systemd/cron 等不同 cwd 下日志写错位置。
# delay=True 懒打开：首条日志时才创建文件——测试进程导入 main 时（根 logger 已被测试挂
# NullHandler，basicConfig 空转）不再凭空建文件/占句柄；生产启动毫秒内即写日志，行为等价
_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading.log')
log_handler = logging.handlers.RotatingFileHandler(
    _LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8', delay=True
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

# OKX 的已撤未成交订单只保留 2 小时。pending 已进入“计划量已落盘”阶段后，
# 超过这个窗口再得到 OrderNotFound，已不能证明当年的请求从未到达交易所。
_PENDING_ORDER_ABSENCE_PROOF_WINDOW = timedelta(hours=2)
_PENDING_TIMESTAMP_FUTURE_TOLERANCE = timedelta(minutes=5)


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
            except Exception as exc:
                # 时区问题已 critical 记录；告警发送再失败不能掩盖主问题。
                logger.debug('发送时区异常告警失败（不影响启动）: %s', exc)
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
            except Exception as exc:
                # 账本损坏是主错误（下方 raise 拒绝启动）；二次告警失败仅留痕。
                logger.debug('账本损坏告警发送失败: %s', exc)
            raise
        self._guard_state_owner()  # 校验状态归属，防止把其它交易所(如旧币安)的持仓当成欧易状态读入

        # 双均线 T+1 是交易状态而非展示数据，必须在启动仓位同步之前就可用，
        # 并且与主账本共享事务性落盘。旧的独立 JSON 只用于一次性迁移。
        self.stop_loss_file = os.path.join(self.data_dir, 'stop_loss_dates.json')
        self.stop_loss_dates = self._load_stop_loss_dates()

        self.scheduler = BackgroundScheduler()
        self._last_check_date = self.trade_state.get_last_daily_check_date()  # 跨重启防重复执行
        self._last_summary_date = self.trade_state.get_last_daily_summary_date()
        self._pending_trade_open_notifications = []
        self._pending_trade_close_notifications = []
        self._pending_stop_loss_updates = []
        self._trade_lock = threading.Lock()  # 防并发执行锁
        self._summary_lock = threading.Lock()  # 每日汇总「查重→推送→标记」的原子化（兜底调度与日检可能并发）
        self._stop_anomalies = {}  # 止损异常状态（mismatch/补挂失败），供前端警示与告警节流
        self._last_failure_notify_ts = 0
        self._equity_tick_fail_streak = 0
        self._equity_tick_alert_sent = False
        self._stop_event = threading.Event()
        self._heartbeat_lock = threading.Lock()
        self._runner_heartbeat_ts = None

        self.equity_tracker = EquityTracker(
            self.data_dir, self,
            notify_failure=self._notify_persistence_failure,
            retention_days=self.config.get('equity_tick_retention_days'),
        )

        # 启动时必须成功获取权益，重试3次。get_balance 的网络异常（适配层重试耗尽后
        # re-raise）与认证异常（立抛）都必须在此捕获——否则会绕过下方「钉钉告警+退出」
        # 路径，进程裸 traceback 静默死亡，恰是本函数要消灭的最贵故障模式
        account_equity = None
        for _retry_i in range(3):
            try:
                balance = self.exchange_api.get_balance()
            except Exception as e:
                balance = None
                logger.warning(f'[{self.label}] 启动时获取账户权益异常: {e}')
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
            except Exception as exc:
                # 启动失败是主错误（下方 sys.exit）；二次告警失败不能阻挠退出。
                logger.debug('启动失败告警发送失败: %s', exc)
            sys.exit(1)

        self.risk_manager = RiskManager(account_equity, self.config['strategy']['default_risk_per_trade'])
        logger.info(f"[{self.label}] 交易系统初始化完成，账户权益: {account_equity} USDT")

        self.sync_positions_on_startup()

    def load_config(self, config_file):
        """加载欧易单所配置：{okx:{凭据...}, strategy, trading, scheduler, dingtalk}。
        兼容旧多所格式（exchanges.okx）自动展平；环境变量可覆盖凭据。"""
        # 配置可能直接含 API 密钥；与命脉账本使用同一套 O_NOFOLLOW +
        # fstat/inode/owner 校验，消除 lstat/chmod/open 之间的替换窗口。
        with open_private_text_file(config_file) as f:
            config = json.load(f)
        # 兼容旧多所格式：从 exchanges.okx 展平到顶层
        if 'okx' not in config and isinstance(config.get('exchanges'), dict):
            okx_block = dict(config['exchanges'].get('okx') or {})
            config['okx'] = okx_block
            config.setdefault('strategy', okx_block.get('strategy', {}))
            config.setdefault('trading', okx_block.get('trading', {'symbols': []}))
        okx = config.setdefault('okx', {})
        # 记住磁盘上原始凭据。环境变量只是运行时覆盖，之后 API 持久化
        # 策略配置时必须恢复这些原值，不能把 env-only 密钥扩散到文件/备份。
        self._disk_okx_credentials = {
            key: okx[key] for key in ('apiKey', 'secret', 'password') if key in okx
        }
        self._env_okx_credential_keys = set()
        if os.environ.get('OKX_API_KEY'):
            okx['apiKey'] = os.environ['OKX_API_KEY']
            self._env_okx_credential_keys.add('apiKey')
        if os.environ.get('OKX_API_SECRET'):
            okx['secret'] = os.environ['OKX_API_SECRET']
            self._env_okx_credential_keys.add('secret')
        _pass = os.environ.get('OKX_API_PASSPHRASE') or os.environ.get('OKX_PASSWORD')
        if _pass:
            okx['password'] = _pass
            self._env_okx_credential_keys.add('password')
        if not okx.get('apiKey') or not okx.get('secret') or not okx.get('password'):
            raise ValueError('未配置 OKX API 凭据（apiKey/secret/passphrase），请在 config.json 或环境变量中提供')
        config.setdefault('strategy', {})
        self._validate_strategy_config(config['strategy'])
        config.setdefault('trading', {'symbols': []})
        config['trading'].setdefault('symbols', [])
        self._validate_symbol_configs(config['trading']['symbols'])
        config.setdefault('scheduler', {})
        self._validate_scheduler_config(config['scheduler'])
        if config.get('equity_tick_retention_days') is not None:
            # 与 strategy/scheduler 同标准 fail-loud：EquityTracker 虽有 try/except 防御，
            # 但静默吞掉非法值会让「我配的保留天数」与实际生效值悄悄不一致
            v = cfgv.strict_int(config['equity_tick_retention_days'], 'config.equity_tick_retention_days')
            if not (7 <= v <= 3650):
                raise ValueError(f"config.equity_tick_retention_days 超出允许范围 [7, 3650]: {v}")
            config['equity_tick_retention_days'] = v
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
        cfgv.validate_strategy_ohlcv_capacity(strategy)

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

    @staticmethod
    def _state_has_lifecycle_data(state):
        """空仓不等于空状态：pending/历史/阻断标记同样带交易所归属。"""
        if not isinstance(state, dict):
            return True
        for key in (
                'open_positions', 'closed_trades', 'signal_states',
                'open_intents', 'stop_residues', 'stop_loss_dates',
                'position_quarantines'):
            if state.get(key):
                return True
        return bool(state.get('last_daily_check_date'))

    @staticmethod
    def _states_equal_ignoring_owner(left, right):
        left_copy = copy.deepcopy(left)
        right_copy = copy.deepcopy(right)
        left_copy.pop('exchange', None)
        right_copy.pop('exchange', None)
        return left_copy == right_copy

    @staticmethod
    def _validate_migrated_auxiliary(filename, payload):
        expected = {
            'stop_loss_dates.json': dict,
            'peak_equity.json': dict,
            'equity_history.json': dict,
            'daily_equity.json': list,
            'equity_ticks.json': list,
            'qiusuo_index.json': dict,
            'closed_trades_archive.json': list,
        }[filename]
        if not isinstance(payload, expected):
            raise ValueError(
                f'{filename} 顶层必须是 {expected.__name__}')
        if filename == 'stop_loss_dates.json':
            for symbol, day in payload.items():
                if not isinstance(symbol, str) or not isinstance(day, str):
                    raise ValueError('T+1 必须是 品种→YYYY-MM-DD')
                datetime.strptime(day, '%Y-%m-%d')
        if filename == 'closed_trades_archive.json' and any(
                not isinstance(item, dict) for item in payload):
            raise ValueError('平仓史书每项必须是对象')

    def _read_private_json_for_startup(self, path, context):
        try:
            with open_private_text_file(path) as handle:
                return json.load(
                    handle,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f'不允许的 JSON 数值常量: {value}')))
        except Exception as exc:
            raise RuntimeError(
                f'{context}读取 {path} 失败({exc})，状态不明拒绝启动。') from exc

    def _migrate_okx_legacy_state(self):
        """只执行一次的旧 data/okx 状态迁移；任一歧义都 fail-closed。"""
        legacy_dir = os.path.join(self.base_dir, 'data', 'okx')
        if not os.path.lexists(legacy_dir):
            return
        try:
            info = os.lstat(legacy_dir)
        except OSError as exc:
            raise RuntimeError(f'无法检查旧状态目录 {legacy_dir}: {exc}') from exc
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f'旧状态目录不是实际目录（拒绝符号链接）: {legacy_dir}')
        current_uid = os.geteuid() if hasattr(os, 'geteuid') else os.getuid()
        if info.st_uid != current_uid:
            raise RuntimeError(f'旧状态目录不属于当前用户: {legacy_dir}')

        marker = os.path.join(self.base_dir, '.okx_legacy_migration_complete.json')
        if private_file_exists(marker):
            payload = self._read_private_json_for_startup(marker, '迁移标记')
            if not isinstance(payload, dict) or payload.get('exchange') != 'okx':
                raise RuntimeError(f'旧状态迁移标记非法: {marker}')
            return

        root_ts = os.path.join(self.base_dir, 'trade_state.json')
        legacy_ts = os.path.join(legacy_dir, 'trade_state.json')
        aux_names = [
            'stop_loss_dates.json', 'peak_equity.json', 'equity_history.json',
            'daily_equity.json', 'equity_ticks.json', 'qiusuo_index.json',
            'closed_trades_archive.json',
        ]
        legacy_has_any = private_file_exists(legacy_ts) or any(
            private_file_exists(os.path.join(legacy_dir, name))
            for name in aux_names)
        if not legacy_has_any:
            return

        if (not private_file_exists(root_ts) and
                private_file_exists(root_ts + '.bak')):
            raise RuntimeError(
                '根主账本缺失但 .bak 尚在，拒绝用永久旧 legacy 快照覆盖；'
                '请先按主账本恢复指引人工裁决')

        legacy_state = TradeState.get_default_state()
        if private_file_exists(legacy_ts):
            legacy_state = self._read_private_json_for_startup(
                legacy_ts, '旧账本迁移前')
            try:
                TradeState.validate_state(legacy_state)
            except Exception as exc:
                raise RuntimeError(
                    f'旧账本 {legacy_ts} schema 非法({exc})，拒绝迁移/启动。') from exc
            if legacy_state.get('exchange') not in (None, 'okx'):
                raise RuntimeError(
                    f'旧 data/okx 账本归属异常: {legacy_state.get("exchange")!r}')

        root_exists = private_file_exists(root_ts)
        root_state = None
        if root_exists:
            root_state = self._read_private_json_for_startup(
                root_ts, '根账本迁移前')
            try:
                TradeState.validate_state(root_state)
            except Exception as exc:
                raise RuntimeError(
                    f'根账本 {root_ts} schema 非法({exc})，拒绝迁移/启动。') from exc

        root_nonempty = self._state_has_lifecycle_data(root_state or {})
        legacy_nonempty = self._state_has_lifecycle_data(legacy_state)
        lineage_confirmed = (
            root_state is not None and
            self._states_equal_ignoring_owner(root_state, legacy_state))
        # 即便旧目录只有 T+1/权益/史书，也要同时建立带 okx owner 的根账本，
        # 否则目录归属护栏会正确地把这些非空辅助状态判为来路不明。
        replace_root = not root_exists
        if root_exists and legacy_nonempty and not lineage_confirmed:
            if root_nonempty:
                raise RuntimeError(
                    '根目录与 data/okx 均含生命周期状态，无法自动选择；'
                    '请人工核对持仓、pending、残留和历史后再启动')
            backup = (
                f'{root_ts}.bak.empty.'
                f'{datetime.now().strftime("%Y%m%d_%H%M%S_%f")}')
            if not atomic_write_json(backup, root_state):
                raise RuntimeError(f'备份根目录空账本失败: {backup}')
            replace_root = True
            logger.warning(f'根空状态已备份到 {backup}，开始迁入旧 OKX 生命周期状态')

        moved = []
        if replace_root:
            migrated = copy.deepcopy(legacy_state)
            migrated['exchange'] = 'okx'
            if not atomic_write_json(root_ts, migrated):
                raise RuntimeError(
                    f'迁移命脉账本 {legacy_ts} -> {root_ts} 原子写入失败')
            written = self._read_private_json_for_startup(root_ts, '迁移后账本')
            TradeState.validate_state(written)
            moved.append('trade_state.json')
            lineage_confirmed = True

        source_aux = [
            name for name in aux_names
            if private_file_exists(os.path.join(legacy_dir, name))]
        if root_nonempty and source_aux and not lineage_confirmed:
            raise RuntimeError(
                '根账本已有独立生命周期状态，无法安全合并旧 data/okx 辅助状态；'
                '请人工确认数据归属')

        for filename in source_aux:
            src = os.path.join(legacy_dir, filename)
            dst = os.path.join(self.base_dir, filename)
            payload = self._read_private_json_for_startup(src, '旧辅助状态迁移前')
            try:
                self._validate_migrated_auxiliary(filename, payload)
            except Exception as exc:
                raise RuntimeError(
                    f'旧辅助状态 {src} schema 非法({exc})') from exc
            if private_file_exists(dst):
                existing = self._read_private_json_for_startup(dst, '根辅助状态')
                if existing != payload:
                    raise RuntimeError(
                        f'根目录与旧目录的 {filename} 内容冲突，拒绝自动覆盖')
                continue
            if private_file_exists(dst + '.bak'):
                raise RuntimeError(
                    f'{dst} 缺失但备份仍在，拒绝用 legacy 绕过恢复裁决')
            if not atomic_write_json(dst, payload):
                raise RuntimeError(f'迁移欧易状态 {filename} 失败')
            moved.append(filename)

        if not atomic_write_json(marker, {
                'exchange': 'okx', 'completed_at': datetime.now().isoformat(),
                'moved': moved}):
            raise RuntimeError('状态文件已迁移但无法写完成标记，拒绝启动')
        logger.warning(
            f'旧 data/okx 状态迁移已一次性收口: {moved}；原路径此后不再参与候选')

    def _directory_has_unowned_state(self):
        names = [
            'closed_trades_archive.json', 'stop_loss_dates.json',
            'peak_equity.json', 'equity_history.json', 'daily_equity.json',
            'equity_ticks.json', 'qiusuo_index.json',
            '.equity_sync_journal.json',
        ]
        try:
            annual_names = set()
            for name in os.listdir(self.base_dir):
                if not name.startswith('closed_trades_archive_'):
                    continue
                if name.endswith('.json'):
                    annual_names.add(name)
                elif name.endswith('.json.bak'):
                    annual_names.add(name[:-4])
            names.extend(sorted(annual_names))
        except OSError as exc:
            raise RuntimeError(
                f'无法枚举数据目录中的年度平仓史书: {exc}') from exc
        for name in names:
            for path in (
                    os.path.join(self.base_dir, name),
                    os.path.join(self.base_dir, name + '.bak')):
                if not private_file_exists(path):
                    continue
                payload = self._read_private_json_for_startup(
                    path, '目录归属校验')
                if payload not in ({}, [], None):
                    return True
        backup = os.path.join(self.base_dir, 'trade_state.json.bak')
        if private_file_exists(backup):
            backup_state = self._read_private_json_for_startup(
                backup, '账本备份归属校验')
            TradeState.validate_state(backup_state)
            if self._state_has_lifecycle_data(backup_state):
                return True
        return False

    def _guard_state_owner(self):
        """用目录级 owner manifest 在加载任何辅助状态前阻止跨所污染。"""
        owner = self.trade_state.get_owner_exchange()
        manifest_path = os.path.join(self.base_dir, '.trading_data_owner.json')
        manifest_owner = None
        if private_file_exists(manifest_path):
            manifest = self._read_private_json_for_startup(
                manifest_path, '数据目录归属')
            if not isinstance(manifest, dict) or not isinstance(
                    manifest.get('exchange'), str):
                raise RuntimeError(f'数据目录归属标记非法: {manifest_path}')
            manifest_owner = manifest['exchange']

        claimed_owners = {value for value in (owner, manifest_owner) if value}
        if len(claimed_owners) > 1 or any(
                value != self.exchange_id for value in claimed_owners):
            raise RuntimeError(
                f'状态归属冲突：账本={owner!r}, 目录={manifest_owner!r}, '
                f'当前={self.exchange_id!r}；拒绝启动')

        if manifest_owner == self.exchange_id and owner is None:
            self.trade_state.claim_owner_exchange(self.exchange_id)
            owner = self.exchange_id
        elif owner is None:
            with self.trade_state.lock:
                state = self.trade_state._snapshot_locked()
            if (self._state_has_lifecycle_data(state) or
                    self._directory_has_unowned_state()):
                raise RuntimeError(
                    '检测到无交易所归属的生命周期/历史/权益状态；空仓不足以证明安全。'
                    '请人工确认全部文件确属 OKX 后写入 exchange="okx"，或使用独立目录')
            self.trade_state.claim_owner_exchange(self.exchange_id)
            owner = self.exchange_id

        if manifest_owner is None:
            if not atomic_write_json(manifest_path, {
                    'exchange': self.exchange_id,
                    'claimed_at': datetime.now().isoformat()}):
                raise RuntimeError('无法持久化数据目录归属标记，拒绝启动')
        logger.info(f'[{self.label}] 账本与数据目录归属均已确认: {owner}')

    def persist_config(self):
        """把当前 config 原子写回磁盘（增删品种/改参数后调用）。

        OKX_* 环境变量是运行时 secret overlay；持久化时恢复文件原值，
        如原文件无该键则继续保持无键，绝不把环境密钥写入磁盘。
        """
        with self._config_lock:
            disk_config = copy.deepcopy(self.config)
            disk_okx = disk_config.setdefault('okx', {})
            for key in getattr(self, '_env_okx_credential_keys', set()):
                originals = getattr(self, '_disk_okx_credentials', {})
                if key in originals:
                    disk_okx[key] = originals[key]
                else:
                    disk_okx.pop(key, None)
            return atomic_write_json(self.config_file, disk_config)

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

    @staticmethod
    def _normalise_position_side(position):
        if not position:
            return None
        side = str(position.get('side') or (position.get('info') or {}).get('posSide') or '').lower()
        if side in ('long', 'short'):
            return side
        return None

    def _position_reconciliation_details(self, symbol, local_position, exchange_position):
        """生成本地/交易所仓位方向与张数对账结果。"""
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        local_side = self._normalise_position_side(local_position)
        exchange_side = self._normalise_position_side(exchange_position)
        exchange_contracts = abs(float((exchange_position or {}).get('contracts') or 0))
        local_coin = float((local_position or {}).get('position_size') or 0)
        local_contracts = 0.0
        if local_position:
            local_contracts = abs(float(
                self.exchange_api._coin_to_contracts(ccxt_symbol, local_coin)))
        side_match = local_side == exchange_side
        quantity_match = math.isclose(
            local_contracts, exchange_contracts, rel_tol=1e-10, abs_tol=1e-10)
        return {
            'local_side': local_side,
            'exchange_side': exchange_side,
            'local_contracts': local_contracts,
            'exchange_contracts': exchange_contracts,
            'side_match': side_match,
            'quantity_match': quantity_match,
            'matched': bool(side_match and quantity_match),
        }

    def _quarantine_position_mismatch(
            self, symbol, reason, details=None, *, notify=True,
            stop_residue_possible=False):
        """隔离仓位不一致品种；磁盘故障时至少保住本进程阻断。"""
        check = getattr(self.trade_state, 'is_position_quarantined', None)
        mark = getattr(self.trade_state, 'mark_position_quarantine', None)
        previous = bool(check(symbol)) if callable(check) else False
        persist_error = None
        if callable(mark):
            try:
                mark_kwargs = (
                    {'stop_residue_possible': True}
                    if stop_residue_possible else {})
                mark(symbol, reason, details, **mark_kwargs)
            except Exception as exc:
                persist_error = exc
                force_mark = getattr(
                    self.trade_state, 'force_runtime_mark_position_quarantine', None)
                if callable(force_mark):
                    try:
                        force_mark(symbol, reason, details, **mark_kwargs)
                    except Exception:
                        logger.exception(f'{symbol} 运行时隔离也失败')
                logger.critical(
                    f'{symbol} 隔离状态落盘失败，已尽力启用运行时隔离: {exc}')
        if notify and not previous:
            msg = f"{symbol} 已进入仓位对账隔离: {reason}"
            if persist_error is not None:
                msg += f'（隔离落盘失败，仅本进程生效: {persist_error}）'
            logger.critical(msg)
            try:
                self.notifier.notify_error(msg)
            except Exception:
                logger.exception(f"{symbol} 发送仓位隔离告警失败")
        return persist_error is None

    def _clear_position_quarantine_after_reconcile(self, symbol):
        # 方向/数量一致还不等于“可解除隔离”：应急余仓可能仍无止损，
        # 或有未知算法单残留。等 guardian 验证/补挂保护后再清。
        get_position = getattr(self.trade_state, 'get_open_position', None)
        position = get_position(symbol) if callable(get_position) else None
        if position and (
                not position.get('stop_order_id') or
                position.get('stop_resize_pending')):
            return False
        has_residue = getattr(self.trade_state, 'has_stop_residue', None)
        if callable(has_residue) and has_residue(symbol):
            return False
        clear = getattr(self.trade_state, 'clear_position_quarantine', None)
        if callable(clear) and clear(symbol):
            logger.warning(f"{symbol} 本地/交易所仓位已重新一致，自动解除隔离")
            return True
        return False

    def is_symbol_quarantined(self, symbol):
        """API/executor 可调用的统一隔离查询入口。"""
        check = getattr(self.trade_state, 'is_position_quarantined', None)
        return bool(check(symbol)) if callable(check) else False

    def _verify_existing_position_or_quarantine(
            self, symbol, local_position, exchange_position, clear_on_match=True):
        """两边都有仓时必须方向+张数完整一致。"""
        try:
            details = self._position_reconciliation_details(
                symbol, local_position, exchange_position)
        except Exception as exc:
            self._quarantine_position_mismatch(
                symbol, f'仓位数量换算失败: {exc}')
            return False
        if not details['matched']:
            reason = (
                f"本地 {details['local_side']} {details['local_contracts']} 张，"
                f"交易所 {details['exchange_side']} {details['exchange_contracts']} 张")
            self._quarantine_position_mismatch(symbol, reason, details)
            return False
        if clear_on_match:
            self._clear_position_quarantine_after_reconcile(symbol)
        return True

    def sync_positions_on_startup(self):
        """启动时对账持仓现实：存在性、方向、张数三者必须同时一致。"""
        logger.info("开始同步持仓状态...")
        open_positions = self.trade_state.get_all_open_positions()

        for symbol in list(open_positions.keys()):
            close_recovery = self._resume_persisted_close_intent(
                symbol, open_positions[symbol], '启动对账')
            if close_recovery in ('closed', 'unresolved'):
                continue
            if close_recovery == 'partial':
                refreshed = self.trade_state.get_open_position(symbol)
                if not refreshed:
                    continue
                open_positions[symbol] = refreshed
            ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
            try:
                position = self.exchange_api.get_position(ccxt_symbol)
            except Exception as exc:
                self._quarantine_position_mismatch(
                    symbol, f'启动对账查询交易所持仓失败: {exc}')
                continue

            if position is None or position.get('contracts', 0) == 0:
                logger.warning(f"{symbol} 在交易所中没有持仓，但本地记录有，更新状态...")
                # 进程离线期间仓位消失时，重启后的当前市价与真实退出时刻没有
                # 因果关系：止损后若已反弹/回落，用裸市价会把亏损记成盈利。
                # 没有可归因 close intent 时，与盘中 guardian / 日检统一采用
                # 账本保护止损价作为保守估值；仅兼容无止损字段的旧账本回退入场价。
                exit_price = (
                    open_positions[symbol].get('stop_loss_price') or
                    open_positions[symbol]['entry_price'])
                closed_position, _state_saved, _stop_cleared = self._handle_exchange_flat_close(
                    symbol, ccxt_symbol, open_positions[symbol], exit_price,
                    "启动同步平仓",
                    strategy_type=(open_positions[symbol].get('strategy') or 'turtle'))
                if not closed_position:
                    logger.warning(f"{symbol} 启动同步时本地状态补偿失败，保留原状态等待人工处理")
                    self._quarantine_position_mismatch(
                        symbol, '交易所已空仓但本地平仓补偿失败')
                    continue
                if not _state_saved:
                    # 持久化故障处理器已用同一条告警建立运行时隔离；这里不再
                    # 对同一磁盘故障重复通知。
                    continue
                # 启动发现双均线仓位已被止损/人工平掉，与盘中守护、日检
                # 用同一原子状态迁移：平仓与 T+1 已经同事务落盘。
                if (open_positions[symbol].get('strategy') or 'turtle') == 'ma_cross':
                    pass
                else:
                    pending = self.trade_state.get_pending_signal_execution(symbol)
                    if (pending and (pending.get('payload') or {}).get('side') ==
                            open_positions[symbol].get('side')):
                        self.trade_state.confirm_signal_execution(
                            symbol, 'turtle', pending.get('signal_id'))
                        candle = (pending.get('payload') or {}).get('candle_id')
                        if candle:
                            self.trade_state.mark_candle_processed(
                                symbol, 'turtle', candle)
                self._clear_position_quarantine_after_reconcile(symbol)
            else:
                local_position = open_positions[symbol]
                if self._verify_existing_position_or_quarantine(
                        symbol, local_position, position, clear_on_match=False):
                    strategy_type = local_position.get('strategy') or 'turtle'
                    strategy_name = self._get_strategy_display_name(strategy_type)
                    if not self._ensure_stop_order_alive(
                            symbol, ccxt_symbol, local_position, strategy_name):
                        self._quarantine_position_mismatch(
                            symbol, '启动时仓位一致，但交易所止损保护未能严格确认')
                        continue
                    self._clear_position_quarantine_after_reconcile(symbol)
                    intent_getter = getattr(
                        self.trade_state, 'get_open_intent', None)
                    intent = (
                        intent_getter(symbol) if callable(intent_getter) else None)
                    if intent and intent.get('side') == local_position.get('side'):
                        self.trade_state.resolve_open_intent(
                            symbol, intent.get('client_order_id'))
                    pending = self.trade_state.get_pending_signal_execution(symbol)
                    if (pending and pending.get('strategy') == 'turtle' and
                            (pending.get('payload') or {}).get('side') == open_positions[symbol].get('side')):
                        # 崩溃可能发生在“成交+止损+账本已落盘”之后、confirmed
                        # 标记之前。既然此刻本地/交易所方向张数已完整匹配，可安全补确认。
                        self.trade_state.confirm_signal_execution(
                            symbol, 'turtle', pending.get('signal_id'))
                        candle = (pending.get('payload') or {}).get('candle_id')
                        if candle:
                            self.trade_state.mark_candle_processed(symbol, 'turtle', candle)
                    logger.info(f"{symbol} 持仓方向与张数同步成功")

        # 本地/交易所都空仓的 pending 也必须主动裁决：不能只在
        # “交易所有孤儿仓”时才触发恢复。
        self._reconcile_all_pending_turtle_executions('启动')
        self._reconcile_all_open_intents('启动')

        # 反向核对：交易所有仓但本地无记录（本地状态损坏丢失/人工开仓）——
        # 该仓不会被系统托管，必须持久化隔离，不能仅发一条告警后继续开仓。
        try:
            exchange_symbols = set(self.exchange_api.list_position_symbols())
            local_symbols = set(self.trade_state.get_all_open_positions().keys())
            orphans = sorted(exchange_symbols - local_symbols)
            if orphans:
                for orphan in orphans:
                    pending_getter = getattr(
                        self.trade_state, 'get_pending_signal_execution', None)
                    pending = pending_getter(orphan) if callable(pending_getter) else None
                    if (pending and pending.get('strategy') == 'turtle' and
                            self._resume_pending_turtle_execution(orphan, pending)):
                        continue
                    self._quarantine_position_mismatch(
                        orphan, '交易所有仓但本地无账本记录（孤儿仓）')
            # 只有本轮完整反向查询成功才可解除已经实际双边空仓的旧隔离。
            get_quarantines = getattr(self.trade_state, 'get_position_quarantines', None)
            quarantined_symbols = (
                list(get_quarantines().keys()) if callable(get_quarantines) else [])
            for quarantined in quarantined_symbols:
                if quarantined not in exchange_symbols and quarantined not in local_symbols:
                    self._clear_position_quarantine_after_reconcile(quarantined)
        except Exception as e:
            # 无法列出交易所全部持仓时，无法证明任一本地空仓品种真的安全。
            # 将当前配置池的空仓品种全部隔离；后续成功对账时自动解除。
            logger.critical(f"启动孤儿仓核对失败，对本地空仓品种 fail-closed: {e}")
            for cfg in getattr(self, 'config', {}).get('trading', {}).get('symbols', []):
                symbol = cfg.get('name')
                if symbol and symbol not in open_positions:
                    self._quarantine_position_mismatch(
                        symbol, f'无法完成启动孤儿仓核对: {e}')

        logger.info("持仓状态同步完成")

    def get_strategy_for_symbol(self, symbol_config):
        """根据交易对配置获取对应策略"""
        strategy_type = symbol_config.get('strategy', 'turtle')
        if strategy_type == 'ma_cross':
            return self.ma_cross_strategy, 'ma_cross'
        else:
            return self.turtle_strategy, 'turtle'

    def _load_stop_loss_dates(self):
        """从主账本加载 T+1；首次升级时严格迁移旧独立 JSON。"""
        if self.trade_state.stop_loss_dates_migrated():
            dates = self.trade_state.get_stop_loss_dates()
            logger.info(f"已从主账本加载止损日期记录: {dates}")
            return dates

        legacy = {}
        if private_file_exists(self.stop_loss_file):
            try:
                with open_private_text_file(self.stop_loss_file) as f:
                    legacy = json.load(
                        f,
                        parse_constant=lambda value: (_ for _ in ()).throw(
                            ValueError(f'不允许的 JSON 数值常量: {value}')))
                if not isinstance(legacy, dict):
                    raise ValueError('顶层必须是对象')
                for symbol, value in legacy.items():
                    if not isinstance(symbol, str) or not isinstance(value, str):
                        raise ValueError('键值必须都是字符串')
                    datetime.strptime(value, '%Y-%m-%d')
            except Exception as exc:
                raise TradeStatePersistenceError(
                    f'旧 T+1 文件 {self.stop_loss_file} 损坏({exc})，拒绝以空标记启动'
                ) from exc
        self.trade_state.replace_stop_loss_dates(legacy)
        if legacy:
            logger.warning(f"已把旧 stop_loss_dates.json 迁入主账本: {legacy}")
        return self.trade_state.get_stop_loss_dates()

    def _save_stop_loss_dates(self):
        """保存 T+1：主账本落盘失败向上抛出，由交易调用链 fail-closed。"""
        if hasattr(self, 'trade_state') and hasattr(self.trade_state, 'replace_stop_loss_dates'):
            self.stop_loss_dates = self.trade_state.replace_stop_loss_dates(self.stop_loss_dates)
            return True
        # 仅为老单元测试的 __new__ 最小桩保留兼容；生产必走主账本。
        if not atomic_write_json(self.stop_loss_file, self.stop_loss_dates):
            raise TradeStatePersistenceError(f'保存 T+1 状态失败: {self.stop_loss_file}')
        return True

    def is_stop_loss_today(self, symbol):
        """检查该交易对今天是否已经止损过（T+1限制）"""
        if symbol in self.stop_loss_dates:
            today_str = date.today().strftime('%Y-%m-%d')
            if self.stop_loss_dates[symbol] == today_str:
                return True
        return False

    def record_stop_loss(self, symbol):
        """记录止损日期（与主账本事务性持久化）。"""
        previous = dict(self.stop_loss_dates)
        self.stop_loss_dates[symbol] = date.today().strftime('%Y-%m-%d')
        try:
            self._save_stop_loss_dates()
        except Exception:
            self.stop_loss_dates = previous
            raise
        return True

    def clear_stop_loss(self, symbol):
        """事务性清除某品种 T+1 标记。"""
        if symbol not in self.stop_loss_dates:
            return False
        previous = dict(self.stop_loss_dates)
        del self.stop_loss_dates[symbol]
        try:
            self._save_stop_loss_dates()
        except Exception:
            self.stop_loss_dates = previous
            raise
        return True

    @staticmethod
    def _format_indicator_price(value):
        """按价格量级保留足够小数，避免低价品种在日志中显示为 0.00。"""
        number = float(value)
        magnitude = abs(number)
        if magnitude >= 100:
            decimals = 2
        elif magnitude >= 1:
            decimals = 4
        elif magnitude >= 0.01:
            decimals = 6
        elif magnitude >= 0.0001:
            decimals = 8
        else:
            decimals = 10
        return f'{number:.{decimals}f}'

    @staticmethod
    def _closed_candle_id(df):
        """返回最新已收盘 K 线的稳定 ID（带时区无关的 ISO 字符串）。"""
        value = df.iloc[-1].get('timestamp') if len(df) else None
        if value is None:
            raise ValueError('K 线缺少 timestamp，无法建立幂等信号 ID')
        try:
            return value.isoformat()
        except AttributeError:
            return str(value)

    @staticmethod
    def _turtle_client_order_id(symbol, candle_id, side):
        digest = hashlib.sha256(
            f'turtle|{symbol}|{candle_id}|{side}'.encode('utf-8')).hexdigest()
        # OKX clOrdId 上限 32，仅接受 ASCII 字母数字。
        return f'T{digest[:31]}'

    def _prepare_turtle_signal_execution(self, symbol, signal, candle_id):
        """为海龟突破建立 pending + 确定性 clOrdId；已确认信号直接拒绝重放。"""
        side = signal.get('action')
        if side not in ('long', 'short'):
            return None
        signal_id = f'{candle_id}|{side}'
        client_order_id = self._turtle_client_order_id(symbol, candle_id, side)
        payload = {
            'side': side,
            'candle_id': candle_id,
            'entry_price': float(signal['current_close']),
            'stop_loss_price': float(
                signal['lower_line'] if side == 'long' else signal['upper_line']),
        }
        execution = self.trade_state.prepare_signal_execution(
            symbol, 'turtle', signal_id, client_order_id, payload=payload)
        if execution.get('status') == 'confirmed':
            logger.warning(
                f"{symbol} [海龟] 忽略已消费的同 K 线突破 {signal_id}，防止止损后重放")
            signal['action'] = None
            return None
        # pending 重试必须复用账本中原 clOrdId，不可重新生成。
        signal['_client_order_id'] = execution['client_order_id']
        signal['_signal_id'] = signal_id
        return signal_id

    def _confirm_turtle_signal_if_reconciled(self, symbol, signal_id, side):
        """只有主账本已经反映目标持仓方向时才确认消费。"""
        if not signal_id:
            return False
        position = self.trade_state.get_open_position(symbol)
        if not position or position.get('side') != side:
            return False
        self.trade_state.confirm_signal_execution(symbol, 'turtle', signal_id)
        return True

    def _finalize_rolled_back_turtle_signal(self, symbol, execution, outcome):
        """把“旧开仓已确认、补偿平仓也已全平”原子收口为成交史+已消费信号。"""
        if not isinstance(outcome, dict) or outcome.get('status') != 'rolled_back':
            return False
        close_order = outcome.get('close_order') or {}
        if close_order.get('fully_closed') is not True:
            return False
        payload = execution.get('payload') or {}
        side = payload.get('side')
        if side not in ('long', 'short'):
            return False
        recovered_entry = outcome.get('entry_price') or payload.get('entry_price')
        recovered_exit = close_order.get('average')
        if recovered_exit is None:
            try:
                recovered_exit = self.exchange_api.get_last_price(
                    self.exchange_api.to_ccxt_symbol(symbol))
            except Exception:
                recovered_exit = recovered_entry
        entry_fee, entry_fee_currency = self._extract_usdt_fee(
            outcome.get('open_order'))
        exit_fee, exit_fee_currency = self._extract_usdt_fee(close_order)
        try:
            self.trade_state.finalize_recovered_signal_round_trip(
                symbol, 'turtle', execution.get('signal_id'), side,
                recovered_entry, recovered_exit, outcome.get('position_size'),
                payload.get('stop_loss_price'),
                candle_id=payload.get('candle_id'),
                entry_fee=entry_fee,
                entry_fee_currency=entry_fee_currency,
                entry_order_ids=self._order_ids(outcome.get('open_order')),
                exit_fee=exit_fee,
                exit_fee_currency=exit_fee_currency,
                exit_order_ids=self._order_ids(close_order))
        except Exception as exc:
            logger.critical(
                f'{symbol} 仓位已全平，但往返成交史/幂等状态无法原子落盘: {exc}')
            return False
        self._clear_position_quarantine_after_reconcile(symbol)
        logger.critical(
            f'{symbol} [海龟] 开仓后已补偿全平，往返成交与信号状态已原子收口')
        return True

    def _recovery_symbol_config(self, symbol, strategy):
        """返回恢复事务使用的配置，并明确标记是否已经退池/禁用。

        恢复已有交易所仓位时，即使品种已经退池也必须补账、补止损；但在
        “本地与交易所都空仓、确认旧请求从未送达”时，退池状态必须阻断任何
        新 POST。统一在这里构造标记，避免各恢复分支把缺失配置兜成 enabled=True。
        """
        configured = next(
            (cfg for cfg in self.config.get('trading', {}).get('symbols', [])
             if cfg.get('name') == symbol), None)
        if configured is None:
            return {
                'name': symbol,
                'enabled': False,
                'risk_per_trade': self.config['strategy']['default_risk_per_trade'],
                'strategy': strategy,
                '_retired_from_pool': True,
            }, True
        symbol_config = dict(configured)
        retired = not symbol_config.get('enabled', True)
        if retired:
            symbol_config['_retired_from_pool'] = True
        return symbol_config, retired

    def _resume_pending_turtle_execution(self, symbol, execution):
        """启动对账专用：用原 clOrdId 恢复“已成交、未挂止损/未记账”的中断交易。"""
        payload = execution.get('payload') or {}
        side = payload.get('side')
        if side not in ('long', 'short'):
            return False
        try:
            entry_price = float(payload['entry_price'])
            stop_loss_price = float(payload['stop_loss_price'])
        except (KeyError, TypeError, ValueError):
            return False
        symbol_config, _retired = self._recovery_symbol_config(
            symbol, 'turtle')
        outcome = self._execute_open(
            symbol, side, entry_price, stop_loss_price, symbol_config,
            buffer_notification=False,
            client_order_id=execution.get('client_order_id'),
            recover_pending_position=True)
        if isinstance(outcome, dict) and outcome.get('status') == 'rolled_back':
            return self._finalize_rolled_back_turtle_signal(
                symbol, execution, outcome)
        if isinstance(outcome, dict) and outcome.get('status') == 'rollback_incomplete':
            logger.critical(
                f'{symbol} [海龟] pending 恢复回滚后仍有余仓，保留 pending 并隔离人工处理')
            return False
        if not self._confirm_turtle_signal_if_reconciled(
                symbol, execution.get('signal_id'), side):
            return False
        self._clear_position_quarantine_after_reconcile(symbol)
        logger.critical(
            f"{symbol} [海龟] 已用原 clOrdId 恢复中断开仓，止损与账本已补齐")
        return True

    @staticmethod
    def _finite_nonnegative(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value >= 0 else None

    def _pending_order_resolution(self, order):
        """将只读查到的旧订单归一为 (terminal, filled_contracts)。"""
        info = order.get('info') if isinstance(order.get('info'), dict) else {}
        states = {
            str(value).lower() for value in (
                order.get('status'), info.get('state'), info.get('ordState'))
            if value is not None
        }
        terminal_states = {
            'closed', 'filled', 'canceled', 'cancelled', 'rejected',
            'expired', 'mmp_canceled', 'mmp_cancelled',
        }
        terminal = bool(states & terminal_states)
        filled = self._finite_nonnegative(order.get('filled'))
        amount = self._finite_nonnegative(order.get('amount'))
        remaining = self._finite_nonnegative(order.get('remaining'))
        if filled is None and amount is not None and remaining is not None:
            filled = max(0.0, amount - remaining)
        if (filled is None and amount is not None and
                states & {'closed', 'filled'} and (remaining is None or remaining == 0)):
            filled = amount
        return terminal, filled

    def _consume_pending_without_trade(
            self, symbol, execution, resolution, reason, order=None):
        payload = execution.get('payload') or {}
        self.trade_state.finalize_signal_without_trade(
            symbol, 'turtle', execution.get('signal_id'),
            candle_id=payload.get('candle_id'), resolution=resolution,
            reason=reason, order=order)
        self._clear_position_quarantine_after_reconcile(symbol)
        logger.warning(f'{symbol} [海龟] pending 已无成交原子收口: {reason}')
        return True

    def _finalize_flat_filled_pending(self, symbol, execution, order, filled_contracts):
        """旧单有成交但当前已空仓：按保守退出价补记完整往返。"""
        payload = execution.get('payload') or {}
        side = payload.get('side')
        try:
            stop_price = float(payload['stop_loss_price'])
            entry_price = float(
                order.get('average') or order.get('price') or payload['entry_price'])
            ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
            contract_size = float(self.exchange_api._get_contract_size(ccxt_symbol))
            position_size = float(filled_contracts) * contract_size
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f'pending 旧成交的价格/数量无法安全还原: {exc}') from exc
        if (side not in ('long', 'short') or
                any(not math.isfinite(value) or value <= 0 for value in (
                    stop_price, entry_price, position_size, contract_size))):
            raise RuntimeError('pending 旧成交缺少有效方向/价格/数量')

        close_evidence = None
        try:
            close_evidence = self._recover_flat_compensation_evidence(
                ccxt_symbol, side, position_size,
                execution.get('client_order_id'))
        except Exception as exc:
            logger.warning(
                f'{symbol} pending 补偿平仓腿查询失败，退回保守退出价: {exc}')
        if close_evidence and close_evidence.get('average') is not None:
            exit_price = float(close_evidence['average'])
            recovery_reason = 'pending 旧订单与确定性补偿腿均已找回'
        else:
            current_price = None
            try:
                current_price = float(self.exchange_api.get_last_price(ccxt_symbol))
                if not math.isfinite(current_price) or current_price <= 0:
                    current_price = None
            except Exception:
                current_price = None
            # 真实退出价不可证明时：多单取止损/现价较低者，空单取较高者，
            # 不用入场价伪造“无损退出”。
            exit_candidates = [stop_price]
            if current_price is not None:
                exit_candidates.append(current_price)
            exit_price = (
                min(exit_candidates) if side == 'long'
                else max(exit_candidates))
            recovery_reason = 'pending 旧订单已成交但当前已空仓，按保守退出价补记'
        entry_fee, entry_fee_currency = self._extract_usdt_fee(order)
        exit_fee, exit_fee_currency = self._extract_usdt_fee(close_evidence)
        trade = self.trade_state.finalize_recovered_signal_round_trip(
            symbol, 'turtle', execution.get('signal_id'), side,
            entry_price, exit_price, position_size, stop_price,
            candle_id=payload.get('candle_id'), entry_fee=entry_fee,
            entry_fee_currency=entry_fee_currency,
            entry_order_ids=self._order_ids(order),
            exit_fee=exit_fee, exit_fee_currency=exit_fee_currency,
            exit_order_ids=self._order_ids(close_evidence),
            reason=recovery_reason)
        self._clear_position_quarantine_after_reconcile(symbol)
        msg = (
            f'{symbol} [海龟] 发现 pending 旧订单已成交 {position_size}币，'
            f'但交易所当前已空仓；已用'
            f'{"确定性补偿腿真实" if close_evidence else "保守"}退出价 {exit_price} '
            f'原子补记往返成交（PnL={trade.get("pnl")}）')
        logger.critical(msg)
        self.notifier.notify_error(msg)
        return True

    def _continue_unsubmitted_pending_turtle(self, symbol, execution):
        """确认从未发单后，在原止损仍有效时复用原 clOrdId 开仓。"""
        payload = execution.get('payload') or {}
        side = payload.get('side')
        symbol_config, retired = self._recovery_symbol_config(
            symbol, 'turtle')
        if retired:
            return self._consume_pending_without_trade(
                symbol, execution, 'unsubmitted_retired',
                '确认从未发单，且品种已删除或禁用；只平不开，取消恢复开仓')
        try:
            entry_price = float(payload['entry_price'])
            stop_loss_price = float(payload['stop_loss_price'])
        except (KeyError, TypeError, ValueError):
            return False
        outcome = self._execute_open(
            symbol, side, entry_price, stop_loss_price, symbol_config,
            buffer_notification=False,
            client_order_id=execution.get('client_order_id'))
        if isinstance(outcome, dict) and outcome.get('status') == 'rolled_back':
            return self._finalize_rolled_back_turtle_signal(symbol, execution, outcome)
        if isinstance(outcome, dict) and outcome.get('status') == 'opened':
            if not self._confirm_turtle_signal_if_reconciled(
                    symbol, execution.get('signal_id'), side):
                return False
            candle = payload.get('candle_id')
            if candle:
                self.trade_state.mark_candle_processed(symbol, 'turtle', candle)
            self._clear_position_quarantine_after_reconcile(symbol)
            return True
        return False

    @staticmethod
    def _pending_order_absence_is_conclusive(execution):
        """判断 OrderNotFound 是否仍足以证明 pending 从未发单。"""
        raw_updated_at = execution.get('updated_at')
        if not isinstance(raw_updated_at, str) or not raw_updated_at.strip():
            return False, 'pending 缺少下单阶段时间戳'
        try:
            updated_at = datetime.fromisoformat(raw_updated_at)
            now = datetime.now(updated_at.tzinfo) if updated_at.tzinfo else datetime.now()
            age = now - updated_at
        except (TypeError, ValueError, OverflowError) as exc:
            return False, f'pending 下单阶段时间戳非法: {exc}'
        if age < -_PENDING_TIMESTAMP_FUTURE_TOLERANCE:
            return False, f'pending 下单阶段时间戳位于未来（偏差 {-age}）'
        if age > _PENDING_ORDER_ABSENCE_PROOF_WINDOW:
            return False, f'pending 已超过交易所可证明未发单的 2 小时窗口（{age}）'
        return True, None

    def _adjudicate_flat_pending_turtle(self, symbol, execution):
        """裁决“本地空仓 + 交易所空仓”的 pending。"""
        payload = execution.get('payload') or {}
        side = payload.get('side')
        if side not in ('long', 'short'):
            self._quarantine_position_mismatch(
                symbol, f'pending payload 方向非法: {side!r}')
            return False
        planned = execution.get('planned_position_size')
        order = None
        if planned is not None:
            try:
                planned_value = float(planned)
                if not math.isfinite(planned_value) or planned_value <= 0:
                    raise ValueError(f'非法 planned_position_size={planned!r}')
                finder = getattr(self.exchange_api, 'find_existing_open_order', None)
                if not callable(finder):
                    raise RuntimeError('交易所适配层缺少旧 clOrdId 只读查询')
                order = finder(
                    self.exchange_api.to_ccxt_symbol(symbol), side,
                    planned_value, execution.get('client_order_id'))
            except Exception as exc:
                self._quarantine_position_mismatch(
                    symbol, f'pending 旧 clOrdId 查询不确定: {exc}',
                    {'client_order_id': execution.get('client_order_id')})
                return False

        if order is not None:
            terminal, filled_contracts = self._pending_order_resolution(order)
            if not terminal or filled_contracts is None:
                self._quarantine_position_mismatch(
                    symbol, 'pending 旧订单尚未终态或成交量不确定',
                    {'client_order_id': execution.get('client_order_id'),
                     'order_id': order.get('id'), 'status': order.get('status')})
                return False
            if filled_contracts <= 1e-12:
                return self._consume_pending_without_trade(
                    symbol, execution, 'terminal_zero_fill',
                    '旧 clOrdId 订单已终态且零成交', order=order)
            try:
                return self._finalize_flat_filled_pending(
                    symbol, execution, order, filled_contracts)
            except Exception as exc:
                self._quarantine_position_mismatch(
                    symbol, f'pending 旧成交无法补记: {exc}',
                    {'client_order_id': execution.get('client_order_id'),
                     'order_id': order.get('id')})
                return False

        if planned is not None:
            conclusive, reason = self._pending_order_absence_is_conclusive(execution)
            if not conclusive:
                self._quarantine_position_mismatch(
                    symbol,
                    f'{reason}；旧 clOrdId 查无订单不能再证明请求从未送达，禁止自动重开',
                    {'client_order_id': execution.get('client_order_id'),
                     'updated_at': execution.get('updated_at')})
                return False

        _symbol_config, retired = self._recovery_symbol_config(
            symbol, 'turtle')
        if retired:
            return self._consume_pending_without_trade(
                symbol, execution, 'unsubmitted_retired',
                '确认从未发单，且品种已删除或禁用；只平不开，取消恢复开仓')

        # planned 缺失，或按 clOrdId 明确 OrderNotFound：根据发单前事务顺序，
        # 且仍在交易所可查询窗口内，才可确认这笔从未发出。只有当前止损仍能
        # 保护新仓时才可继续。
        try:
            stop_price = float(payload['stop_loss_price'])
            current_price = float(self.exchange_api.get_last_price(
                self.exchange_api.to_ccxt_symbol(symbol)))
            if any(not math.isfinite(value) or value <= 0 for value in (
                    stop_price, current_price)):
                raise ValueError('当前价/止损价非法')
        except Exception as exc:
            self._quarantine_position_mismatch(
                symbol, f'pending 未发单的当前止损有效性无法确认: {exc}')
            return False
        stop_valid = (
            (side == 'long' and stop_price < current_price) or
            (side == 'short' and stop_price > current_price))
        if not stop_valid:
            return self._consume_pending_without_trade(
                symbol, execution, 'unsubmitted_expired',
                f'从未发单，且当前价 {current_price} 已越过原止损 {stop_price}')
        if self._continue_unsubmitted_pending_turtle(symbol, execution):
            return True
        self._quarantine_position_mismatch(
            symbol, 'pending 确认未发单，但复用原 clOrdId 开仓未能收口')
        return False

    def _reconcile_all_pending_turtle_executions(self, context):
        """主动枚举所有 pending，返回仍未收口的品种集合。"""
        getter = getattr(self.trade_state, 'get_pending_signal_executions', None)
        pending_by_symbol = getter() if callable(getter) else {}
        unresolved = set()
        for symbol, execution in sorted(pending_by_symbol.items()):
            if execution.get('strategy') != 'turtle':
                self._quarantine_position_mismatch(
                    symbol, f'{context} pending 策略非 turtle，无法自动裁决')
                unresolved.add(symbol)
                continue
            local_position = self.trade_state.get_open_position(symbol)
            try:
                exchange_position = self.exchange_api.get_position(
                    self.exchange_api.to_ccxt_symbol(symbol))
            except Exception as exc:
                self._quarantine_position_mismatch(
                    symbol, f'{context} pending 仓位查询不确定: {exc}')
                unresolved.add(symbol)
                continue
            if local_position:
                # 本地仓的常规对账/平仓链路负责收口，此处不抢跑外部动作。
                continue
            if exchange_position:
                if not self._resume_pending_turtle_execution(symbol, execution):
                    self._quarantine_position_mismatch(
                        symbol, f'{context} pending 孤儿仓恢复未收口')
                    unresolved.add(symbol)
                continue
            if not self._adjudicate_flat_pending_turtle(symbol, execution):
                unresolved.add(symbol)
        return unresolved

    def _finalize_open_intent_rollback(self, symbol, intent, outcome):
        close_order = (outcome or {}).get('close_order') or {}
        if close_order.get('fully_closed') is not True:
            return False
        payload = intent.get('payload') or {}
        entry_price = (outcome or {}).get('entry_price') or payload.get('entry_price')
        exit_price = close_order.get('average')
        if exit_price is None:
            try:
                exit_price = self.exchange_api.get_last_price(
                    self.exchange_api.to_ccxt_symbol(symbol))
            except Exception:
                exit_price = payload.get('stop_loss_price') or entry_price
        position_size = (outcome or {}).get('position_size') or intent.get(
            'planned_position_size')
        entry_fee, _entry_currency = self._extract_usdt_fee(
            (outcome or {}).get('open_order'))
        exit_fee, _exit_currency = self._extract_usdt_fee(close_order)
        self.trade_state.finalize_open_intent_round_trip(
            symbol, intent.get('client_order_id'), entry_price, exit_price,
            position_size,
            entry_order_ids=self._order_ids((outcome or {}).get('open_order')),
            exit_order_ids=self._order_ids(close_order),
            entry_fee=entry_fee, exit_fee=exit_fee,
            reason='开仓意图执行后已完整回滚')
        self._clear_position_quarantine_after_reconcile(symbol)
        return True

    def _resume_open_intent_position(self, symbol, intent):
        payload = intent.get('payload') or {}
        side = intent.get('side')
        try:
            entry_price = float(payload['entry_price'])
            stop_price = float(payload['stop_loss_price'])
        except (KeyError, TypeError, ValueError):
            return False
        symbol_config, _retired = self._recovery_symbol_config(
            symbol, intent.get('strategy') or 'turtle')
        outcome = self._execute_open(
            symbol, side, entry_price, stop_price, symbol_config,
            buffer_notification=False,
            client_order_id=intent.get('client_order_id'),
            recover_pending_position=True)
        if isinstance(outcome, dict) and outcome.get('status') == 'opened':
            return True
        if isinstance(outcome, dict) and outcome.get('status') == 'rolled_back':
            if outcome.get('open_intent_finalized'):
                return True
            return self._finalize_open_intent_rollback(symbol, intent, outcome)
        return False

    def _finalize_flat_filled_open_intent(
            self, symbol, intent, order, filled_contracts):
        payload = intent.get('payload') or {}
        side = intent.get('side')
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        contract_size = float(self.exchange_api._get_contract_size(ccxt_symbol))
        position_size = float(filled_contracts) * contract_size
        entry_price = float(
            order.get('average') or order.get('price') or payload['entry_price'])
        stop_price = float(payload['stop_loss_price'])
        close_evidence = None
        try:
            close_evidence = self._recover_flat_compensation_evidence(
                ccxt_symbol, side, position_size,
                intent.get('client_order_id'))
        except Exception as exc:
            logger.warning(
                f'{symbol} open intent 补偿平仓腿查询失败，退回保守退出价: {exc}')
        if close_evidence and close_evidence.get('average') is not None:
            exit_price = float(close_evidence['average'])
            recovery_reason = '旧开仓意图与确定性补偿腿均已找回'
        else:
            try:
                current_price = float(self.exchange_api.get_last_price(ccxt_symbol))
            except Exception:
                current_price = stop_price
            exit_price = (
                min(stop_price, current_price) if side == 'long'
                else max(stop_price, current_price))
            recovery_reason = (
                '旧开仓意图订单有成交但交易所当前已空仓，保守补记往返')
        entry_fee, _currency = self._extract_usdt_fee(order)
        exit_fee, _exit_currency = self._extract_usdt_fee(close_evidence)
        self.trade_state.finalize_open_intent_round_trip(
            symbol, intent.get('client_order_id'), entry_price, exit_price,
            position_size, entry_order_ids=self._order_ids(order),
            exit_order_ids=self._order_ids(close_evidence),
            entry_fee=entry_fee, exit_fee=exit_fee,
            reason=recovery_reason)
        self._clear_position_quarantine_after_reconcile(symbol)
        self.notifier.notify_error(
            f'{symbol} 旧 open intent 有成交但当前已空仓，已按'
            f'{"确定性补偿腿真实" if close_evidence else "保守"}退出价补记往返')
        return True

    def _adjudicate_flat_open_intent(self, symbol, intent):
        side = intent.get('side')
        planned = intent.get('planned_position_size')
        if planned is None:
            # 旧两阶段实现可能崩在 prepare 与 set_amount 之间；POST 位于两次
            # 成功落盘之后，因此缺计划量可严格证明从未发单，不得用当前风险重算。
            self.trade_state.resolve_open_intent(
                symbol, intent.get('client_order_id'))
            self._clear_position_quarantine_after_reconcile(symbol)
            logger.warning(
                f'{symbol} 收口无计划量 open intent：确认属于发单前中间态，'
                '已删除句柄并允许策略重新计算')
            return True
        try:
            planned_value = float(planned)
            if not math.isfinite(planned_value) or planned_value <= 0:
                raise ValueError(f'非法计划量 {planned!r}')
            order = self.exchange_api.find_existing_open_order(
                self.exchange_api.to_ccxt_symbol(symbol), side,
                planned_value, intent.get('client_order_id'))
        except Exception as exc:
            self._quarantine_position_mismatch(
                symbol, f'open intent 旧订单查询不确定: {exc}')
            return False
        if order is not None:
            terminal, filled = self._pending_order_resolution(order)
            if not terminal or filled is None:
                self._quarantine_position_mismatch(
                    symbol, 'open intent 旧订单尚未终态或成交量不确定')
                return False
            if filled <= 1e-12:
                self.trade_state.resolve_open_intent(
                    symbol, intent.get('client_order_id'))
                self._clear_position_quarantine_after_reconcile(symbol)
                return True
            try:
                return self._finalize_flat_filled_open_intent(
                    symbol, intent, order, filled)
            except Exception as exc:
                self._quarantine_position_mismatch(
                    symbol, f'open intent 旧成交无法补记: {exc}')
                return False

        conclusive, reason = self._pending_order_absence_is_conclusive(intent)
        if not conclusive:
            self._quarantine_position_mismatch(
                symbol, f'{reason}；open intent 查无订单不能证明从未送达')
            return False
        symbol_config, retired = self._recovery_symbol_config(
            symbol, intent.get('strategy') or 'turtle')
        if retired:
            self.trade_state.resolve_open_intent(
                symbol, intent.get('client_order_id'))
            self._clear_position_quarantine_after_reconcile(symbol)
            logger.warning(
                f'{symbol} open intent 确认从未发单，但品种已删除或禁用；'
                '已收口句柄，严格执行只平不开')
            return True
        payload = intent.get('payload') or {}
        try:
            stop_price = float(payload['stop_loss_price'])
            current_price = float(self.exchange_api.get_last_price(
                self.exchange_api.to_ccxt_symbol(symbol)))
            if any(not math.isfinite(value) or value <= 0 for value in (
                    stop_price, current_price)):
                raise ValueError('当前价/止损价非法')
        except Exception as exc:
            self._quarantine_position_mismatch(
                symbol, f'open intent 止损有效性无法确认: {exc}')
            return False
        stop_valid = (
            side == 'long' and stop_price < current_price or
            side == 'short' and stop_price > current_price)
        if not stop_valid:
            self.trade_state.resolve_open_intent(
                symbol, intent.get('client_order_id'))
            self._clear_position_quarantine_after_reconcile(symbol)
            return True
        outcome = self._execute_open(
            symbol, side, float(payload['entry_price']), stop_price,
            symbol_config,
            buffer_notification=False,
            client_order_id=intent.get('client_order_id'))
        if isinstance(outcome, dict) and outcome.get('status') == 'opened':
            return True
        if isinstance(outcome, dict) and outcome.get('status') == 'rolled_back':
            if outcome.get('open_intent_finalized'):
                return True
            return self._finalize_open_intent_rollback(symbol, intent, outcome)
        return False

    def _reconcile_all_open_intents(self, context):
        getter = getattr(self.trade_state, 'get_open_intents', None)
        intents = getter() if callable(getter) else {}
        unresolved = set()
        for symbol, intent in sorted(intents.items()):
            if self.trade_state.get_open_position(symbol):
                continue
            try:
                exchange_position = self.exchange_api.get_position(
                    self.exchange_api.to_ccxt_symbol(symbol))
            except Exception as exc:
                self._quarantine_position_mismatch(
                    symbol, f'{context} open intent 持仓查询不确定: {exc}')
                unresolved.add(symbol)
                continue
            if exchange_position:
                resolved = self._resume_open_intent_position(symbol, intent)
            else:
                resolved = self._adjudicate_flat_open_intent(symbol, intent)
            if not resolved:
                unresolved.add(symbol)
        return unresolved

    def _ma_signal_with_catchup(self, symbol, strategy, df):
        """只检查最新已收盘 K 线是否刚发生 EMA 交叉。

        last_processed_candle 仅用于同一根 K 线的消费幂等。即使停机或
        行情故障造成历史间隔，也不回放旧交叉、不按当前 EMA 位置补仓；
        只比较最新两根已收盘 K 线，避免恢复后补做历史交易。
        """
        metadata = self.trade_state.get_signal_metadata(symbol)
        last_id = metadata.get('last_processed_candle') if metadata.get('strategy') in (None, 'ma_cross') else None
        candle_ids = [
            (v.isoformat() if hasattr(v, 'isoformat') else str(v))
            for v in df['timestamp'].tolist()
        ]
        current_id = candle_ids[-1]
        current_state = strategy.check_current_state(df)
        if current_state is None:
            return None, current_id, 0

        if last_id == current_id:
            # 已处理过这根 K 线：仍返回当前指标供 T+1/持仓检查，但不重放交叉。
            current_state['action'] = None
            return current_state, current_id, 0

        rebaseline, _, _, gap = self._history_requires_rebaseline(
            symbol, 'ma_cross', df)
        latest_signal = strategy.check_signal(df)
        latest_action = (
            latest_signal.get('action') if isinstance(latest_signal, dict) else None)
        current_state['action'] = (
            latest_action if latest_action in ('long', 'short') else None)

        if rebaseline:
            current_state['_history_discontinuity'] = True
            current_state['_history_gap_candles'] = gap
            logger.warning(
                f"{symbol} [双均线] 信号历史不可安全连续：上次处理={last_id}，"
                f"当前={current_id}，间隔={gap}；忽略间隔内的历史交叉，"
                f"仅检查最新一根，信号={current_state['action']}")

        return current_state, current_id, int(current_state['action'] is not None)

    @staticmethod
    def _turtle_exit_still_pending(
            signal, position_before, position_after, retired_from_pool=False):
        """退出/退池反手信号只有在旧方向仓位确实归零后才能消费。"""
        if not position_before or not position_after:
            return False
        action = signal.get('action') if isinstance(signal, dict) else None
        old_side = position_before.get('side')
        explicit_close = (
            (action == 'close_long' and old_side == 'long') or
            (action == 'close_short' and old_side == 'short'))
        retired_reverse_exit = bool(
            retired_from_pool and action in ('long', 'short') and
            old_side != action)
        return bool(explicit_close or retired_reverse_exit)

    def _history_requires_rebaseline(self, symbol, strategy_type, df,
                                     max_gap_candles=3):
        """判断信号 marker 与当前可见历史是否存在不可安全回放的断层。"""
        metadata = self.trade_state.get_signal_metadata(symbol)
        last_id = (
            metadata.get('last_processed_candle')
            if metadata.get('strategy') in (None, strategy_type) else None)
        candle_ids = [
            (value.isoformat() if hasattr(value, 'isoformat') else str(value))
            for value in df['timestamp'].tolist()
        ]
        current_id = candle_ids[-1]
        if last_id == current_id:
            return False, last_id, current_id, 0
        if not last_id:
            return True, None, current_id, None
        if last_id not in candle_ids:
            return True, last_id, current_id, None
        unseen = len(candle_ids) - candle_ids.index(last_id) - 1
        # 行数连续不等于时间连续：若行情源中间缺月但只返回两根稀疏 K 线，
        # 仅按 unseen 会误判为安全。生产 candle ID 是 ISO 时间，同时比较日历跨度。
        gap = unseen
        try:
            calendar_days = (
                date.fromisoformat(current_id[:10]) -
                date.fromisoformat(str(last_id)[:10])).days
            gap = max(gap, calendar_days)
        except (TypeError, ValueError):
            # 测试桩/兼容旧 marker 可能不是 ISO；仍保留行数防线。
            pass
        return gap > max_gap_candles, last_id, current_id, gap

    @staticmethod
    def _daily_candle_is_fresh(df, scheduled_date, max_lag_days=1):
        """验证最新日 K 足以代表本次调度日，拒绝陈旧行情进入真钱策略。

        北京时间 08:00 对应 UTC 日线刚收盘；调度日 D 正常应至少拿到时间戳
        为 D-1 的日 K。允许额外落后 1 天，兼容周末/节假日不连续的传统资产
        映射合约；超过该窗口一律 fail-closed。
        """
        try:
            values = df['timestamp'].tolist()
            latest = values[-1]
            if hasattr(latest, 'to_pydatetime'):
                latest = latest.to_pydatetime()
            elif not isinstance(latest, (datetime, date)):
                latest = datetime.fromisoformat(str(latest).replace('Z', '+00:00'))
            latest_date = latest.date() if isinstance(latest, datetime) else latest
            check_date = (
                scheduled_date.date() if isinstance(scheduled_date, datetime)
                else scheduled_date if isinstance(scheduled_date, date)
                else date.fromisoformat(str(scheduled_date)))
            minimum_date = check_date - timedelta(days=1 + int(max_lag_days))
            return latest_date >= minimum_date, latest_date, minimum_date
        except Exception as exc:
            logger.error(f'无法验证最新日 K 时间戳，按陈旧数据拒绝交易: {exc}')
            return False, None, None

    def _mark_daily_check_complete(self, check_date):
        """先持久化调度日，成功后再更新内存守卫。"""
        setter = getattr(self.trade_state, 'set_last_daily_check_date', None)
        if callable(setter):
            setter(check_date)
        self._last_check_date = check_date

    def check_and_execute_trades(self, manual_run=False, scheduled_date=None):
        """检查并执行交易"""
        # 三重防护：线程锁 + 日期检查 + APScheduler max_instances
        if not self._trade_lock.acquire(blocking=False):
            logger.warning("交易检查正在执行中(锁冲突)，跳过本次触发")
            return
        try:
            today = scheduled_date or date.today().isoformat()
            if self._last_check_date == today and not manual_run:
                logger.warning(f"今日({today})已执行过交易检查，跳过重复执行")
                return
            # 只有正式调度可建立日收盘快照；手动「立即检查」只跑交易逻辑，
            # 绝不能把下午含浮盈的权益改写成 08:00 收盘高水位。
            if not manual_run:
                self.equity_tracker.record_daily_equity_snapshot()
            logger.info("开始检查交易信号...")
            self._pending_trade_open_notifications = []
            self._pending_trade_close_notifications = []
            self._pending_stop_loss_updates = []

            unresolved_pending = self._reconcile_all_pending_turtle_executions('日检')
            unresolved_pending.update(self._reconcile_all_open_intents('日检'))

            # 本轮监控集合 = 手动池启用品种 ∪ 有持仓品种 ∪ pending 品种。
            # 品种即使已从配置删除，未收口 clOrdId 也不得被遗忘/剪枝。
            # 快照视图（与盘中巡检同一模式）：循环中途 API 增删品种不影响本轮的
            # 一致性，也免去逐品种重扫池子
            all_open_positions = self.trade_state.get_all_open_positions()
            pending_getter = getattr(
                self.trade_state, 'get_pending_signal_executions', None)
            pending_after_reconcile = (
                pending_getter() if callable(pending_getter) else {})
            intent_getter = getattr(self.trade_state, 'get_open_intents', None)
            intents_after_reconcile = (
                intent_getter() if callable(intent_getter) else {})
            symbol_config_map = {s['name']: s for s in self.config['trading']['symbols']}
            symbols_to_check = {name for name, s in symbol_config_map.items() if s.get('enabled', True)}
            symbols_to_check.update(all_open_positions.keys())
            symbols_to_check.update(pending_after_reconcile.keys())
            symbols_to_check.update(intents_after_reconcile.keys())

            logger.info(f"本轮检查交易对数: {len(symbols_to_check)}")

            # 先重试清理止损残留（清理确认后解除对应品种的开仓阻断）
            self._retry_clear_stop_residues()

            # 账本瘦身：超出保留窗口的平仓历史搬进只追加的史书文件（失败不影响交易）
            try:
                self.trade_state.compact_closed_trades()
            except Exception as e:
                logger.warning(f"平仓历史归档失败（不影响交易，账本保留全部记录）: {e}")

            # 逐个检查交易对（排序保证遍历与日志顺序确定，跨轮可对比）
            failed_symbols = sorted(unresolved_pending)
            data_unready_symbols = []
            for symbol in sorted(symbols_to_check):
                # 单品种异常只跳过该品种，不得中断其余品种的止损推进/平仓检查（真钱红线）
                try:
                    symbol_config = symbol_config_map.get(symbol)
                    if symbol_config is None:
                        # 品种已从手动池删除但仍有持仓：优先用「持仓记录的策略」，避免错按 turtle 退出
                        held = all_open_positions.get(symbol) or {}
                        held_strategy = held.get('strategy') or 'turtle'
                        symbol_config = {
                            'name': symbol,
                            'enabled': True,
                            'risk_per_trade': self.config['strategy']['default_risk_per_trade'],
                            'strategy': held_strategy,
                            # 删除品种只托管当前仓位到下一次平仓；禁止反手或再开新腿。
                            '_retired_from_pool': True,
                        }
                    elif not symbol_config.get('enabled', True):
                        # 在池但已禁用且仍有持仓：与删除品种同规则——只托管现有仓位平仓
                        # 退出，禁止反手/再开新腿。复制一份再打标记，避免污染共享配置。
                        symbol_config = dict(symbol_config)
                        symbol_config['_retired_from_pool'] = True

                    strategy, strategy_type = self.get_strategy_for_symbol(symbol_config)
                    logger.info(f"检查 {symbol} (策略: {strategy_type})...")

                    if symbol in unresolved_pending:
                        logger.error(
                            f'{symbol} 仍有未裁决 pending，本轮阻断新策略信号')
                        continue

                    ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
                    # 每一轮正常调度都重新核对仓位现实，不把安全性只寄托在启动时。
                    # 查询失败也必须持久化隔离：未知不等于空仓。
                    local_position = self.trade_state.get_open_position(symbol)
                    if local_position:
                        close_recovery = self._resume_persisted_close_intent(
                            symbol, local_position, '日检对账')
                        if close_recovery == 'unresolved':
                            failed_symbols.append(symbol)
                            continue
                        if close_recovery == 'closed':
                            local_position = None
                        elif close_recovery == 'partial':
                            local_position = self.trade_state.get_open_position(symbol)
                    try:
                        exchange_position = self.exchange_api.get_position(ccxt_symbol)
                    except Exception as exc:
                        self._quarantine_position_mismatch(
                            symbol, f'日检持仓对账查询失败: {exc}')
                        failed_symbols.append(symbol)
                        continue
                    if local_position and exchange_position:
                        if not self._verify_existing_position_or_quarantine(
                                symbol, local_position, exchange_position,
                                clear_on_match=False):
                            failed_symbols.append(symbol)
                            continue
                        if not self._ensure_stop_order_alive(
                                symbol, ccxt_symbol, local_position,
                                self._get_strategy_display_name(strategy_type)):
                            self._quarantine_position_mismatch(
                                symbol, '日检仓位一致但止损保护未能严格确认')
                            failed_symbols.append(symbol)
                            continue
                        self._clear_position_quarantine_after_reconcile(symbol)
                        intent_getter = getattr(
                            self.trade_state, 'get_open_intent', None)
                        intent = (
                            intent_getter(symbol)
                            if callable(intent_getter) else None)
                        if intent and intent.get('side') == local_position.get('side'):
                            self.trade_state.resolve_open_intent(
                                symbol, intent.get('client_order_id'))
                        pending = self.trade_state.get_pending_signal_execution(symbol)
                        if (pending and pending.get('strategy') == 'turtle' and
                                (pending.get('payload') or {}).get('side') ==
                                local_position.get('side')):
                            self.trade_state.confirm_signal_execution(
                                symbol, 'turtle', pending.get('signal_id'))
                            pending_candle = (pending.get('payload') or {}).get('candle_id')
                            if pending_candle:
                                self.trade_state.mark_candle_processed(
                                    symbol, 'turtle', pending_candle)
                    elif local_position and not exchange_position:
                        exit_price = (
                            local_position.get('stop_loss_price') or
                            local_position.get('entry_price'))
                        closed, state_saved, _stop_cleared = self._handle_exchange_flat_close(
                            symbol, ccxt_symbol, local_position, exit_price,
                            f'{strategy_type} 日检仓位对账',
                            strategy_type=strategy_type)
                        if not closed:
                            self._quarantine_position_mismatch(
                                symbol, '交易所已空仓但本地日检平仓补偿失败')
                            failed_symbols.append(symbol)
                            continue
                        if not state_saved:
                            # _notify_trade_state_persistence_issue 已通知并建立
                            # 运行时隔离，不重复轰炸。
                            failed_symbols.append(symbol)
                            continue
                        if strategy_type == 'ma_cross':
                            logger.info(
                                f'{symbol} [双均线] 日检记平与 T+1 已同事务落盘')
                        else:
                            # 仅重置 armed 布尔，set_signal_state 会保留已处理 candle /
                            # confirmed signal ID，同一根突破不可重放。
                            self.trade_state.set_signal_state(symbol, False)
                            pending = self.trade_state.get_pending_signal_execution(symbol)
                            if (pending and (pending.get('payload') or {}).get('side') ==
                                    local_position.get('side')):
                                self.trade_state.confirm_signal_execution(
                                    symbol, 'turtle', pending.get('signal_id'))
                                pending_candle = (pending.get('payload') or {}).get('candle_id')
                                if pending_candle:
                                    self.trade_state.mark_candle_processed(
                                        symbol, 'turtle', pending_candle)
                        self._clear_position_quarantine_after_reconcile(symbol)
                        local_position = None
                    elif not local_position and exchange_position:
                        pending = self.trade_state.get_pending_signal_execution(symbol)
                        if (pending and pending.get('strategy') == 'turtle' and
                                self._resume_pending_turtle_execution(symbol, pending)):
                            local_position = self.trade_state.get_open_position(symbol)
                        else:
                            self._quarantine_position_mismatch(
                                symbol, '交易所有仓但本地无记录（日检发现孤儿仓）')
                            failed_symbols.append(symbol)
                            continue
                    elif not local_position and not exchange_position:
                        self._clear_position_quarantine_after_reconcile(symbol)

                    required_closed_candles = cfgv.required_closed_candles_for_strategy(
                        strategy_type, self.config.get('strategy', {}))
                    fetch_limit = cfgv.ohlcv_fetch_limit_for_strategy(
                        strategy_type, self.config.get('strategy', {}))

                    ohlcv = self.exchange_api.fetch_ohlcv(ccxt_symbol, '1d', limit=fetch_limit)
                    if not ohlcv:
                        logger.warning(f"{symbol} 获取K线数据失败")
                        failed_symbols.append(symbol)
                        continue

                    df = self.exchange_api.ohlcv_to_dataframe(ohlcv)
                    df = self.exchange_api.filter_closed_candles(df, timeframe='1d')
                    if len(df) == 0:
                        logger.warning(f"{symbol} 无已收盘K线，跳过本轮检查")
                        failed_symbols.append(symbol)
                        continue
                    if len(df) < required_closed_candles:
                        logger.warning(
                            f"{symbol} K线数据不足：{strategy_type} 策略配置至少需要 "
                            f"{required_closed_candles} 根已收盘K线，本轮仅取得 {len(df)} 根"
                            f"（请求 {fetch_limit} 根），请检查周期配置或交易所历史K线供应")
                        if local_position:
                            # 有钱仓位不得以“新币历史不足”降级：退出/止损推进未完成。
                            failed_symbols.append(symbol)
                        else:
                            # 双边已确认空仓且历史确实不足，属结构性 data-unready。
                            # 保持日报可见，但不要拖累全品种每 30 分钟重跑一整天。
                            data_unready_symbols.append(symbol)
                        continue

                    fresh, latest_candle_date, minimum_candle_date = (
                        self._daily_candle_is_fresh(df, today))
                    if not fresh:
                        logger.critical(
                            f"{symbol} 最新已收盘日 K 陈旧：latest={latest_candle_date}，"
                            f"本次调度日={today}，最低允许={minimum_candle_date}；"
                            "禁止本品种开仓、平仓、反手及策略止损推进")
                        failed_symbols.append(symbol)
                        continue

                    turtle_signal_id = None
                    turtle_signal_side = None
                    candle_id = self._closed_candle_id(df)
                    if strategy_type == 'turtle':
                        turtle_metadata = self.trade_state.get_signal_metadata(symbol)
                        mid_line_crossed = self.trade_state.get_signal_state(symbol)
                        execution = turtle_metadata.get('signal_execution') or {}
                        execution_same_candle = str(execution.get('signal_id') or '').startswith(
                            f'{candle_id}|')
                        already_processed = (
                            turtle_metadata.get('strategy') == 'turtle' and
                            turtle_metadata.get('last_processed_candle') == candle_id)
                        rebaseline, previous_candle, _, gap_candles = (
                            self._history_requires_rebaseline(
                                symbol, 'turtle', df))
                        armed_state_ready = True
                        try:
                            # 这里只回填“截至上一根K线”的可开仓状态，不能把今天刚发生的
                            # 首次突破提前算成已消耗，否则会吞掉本轮本该执行的开仓信号。
                            # 断档时同样只重算资格状态、绝不回放旧交易；随后仍应检查
                            # 最新两根 K 线本身是否刚产生突破/中轨穿越。
                            if not already_processed and not execution_same_candle:
                                armed = self.turtle_strategy.is_first_breakout_armed(
                                    df, include_latest_bar=False)
                                if armed != mid_line_crossed:
                                    self.trade_state.set_signal_state(symbol, armed)
                                    mid_line_crossed = armed
                                    if armed:
                                        logger.info(f"{symbol} [海龟] 历史回溯判定为可开仓状态（中轨后尚未突破），已激活")
                                    else:
                                        logger.info(f"{symbol} [海龟] 历史回溯判定为未激活状态（首次突破资格已消耗或尚未有效穿越中轨），已重置")
                        except Exception as e:
                            armed_state_ready = False
                            logger.warning(f"{symbol} [海龟] 历史回溯开仓状态失败: {e}")

                        signal = strategy.check_signal(
                            df, mid_line_crossed=mid_line_crossed)
                        if rebaseline and not execution_same_candle and signal:
                            signal['_history_discontinuity'] = True
                            signal['_history_gap_candles'] = gap_candles
                            if armed_state_ready:
                                logger.warning(
                                    f"{symbol} [海龟] 信号历史不可安全连续："
                                    f"上次处理={previous_candle}，当前={candle_id}，"
                                    f"间隔={gap_candles}；忽略间隔内历史事件，"
                                    f"仅检查最新一根，信号={signal.get('action')}")
                            else:
                                # 无法证明截至上一根的“首次突破资格”，开仓信号不能执行；
                                # close_long/close_short 只依赖最新中轨穿越，仍可安全保留。
                                if signal.get('action') in ('long', 'short'):
                                    signal['action'] = None
                                logger.critical(
                                    f"{symbol} [海龟] 断档且开仓资格重建失败；"
                                    "禁止最新突破开仓，最新中轨平仓信号仍按事实处理")
                        if signal and signal.get('mid_line_crossed'):
                            self.trade_state.set_signal_state(symbol, True)
                        if (signal and signal.get('action') in ('long', 'short') and
                                not symbol_config.get('_retired_from_pool')):
                            turtle_signal_side = signal['action']
                            turtle_signal_id = self._prepare_turtle_signal_execution(
                                symbol, signal, candle_id)
                    else:
                        signal, candle_id, _missed_crosses = self._ma_signal_with_catchup(
                            symbol, strategy, df)

                    if not signal:
                        logger.warning(f"{symbol} 策略未返回信号，跳过本轮检查")
                        continue

                    current_close = float(df['close'].iloc[-1])
                    fmt_price = self._format_indicator_price
                    if strategy_type == 'turtle':
                        upper = signal.get('upper_line')
                        lower = signal.get('lower_line')
                        mid = signal.get('mid_line')
                        if upper and lower:
                            dist_upper = (upper - current_close) / current_close * 100
                            dist_lower = (current_close - lower) / current_close * 100
                            signal_label = self._get_turtle_signal_label(signal)
                            logger.info(
                                f"{symbol} [海龟指标] 收盘价={fmt_price(current_close)}, "
                                f"上轨={fmt_price(upper)}(距{dist_upper:+.2f}%), "
                                f"下轨={fmt_price(lower)}(距{dist_lower:+.2f}%), "
                                f"中轨={fmt_price(mid)}, 信号={signal_label}")
                    elif strategy_type == 'ma_cross':
                        ema_s = signal.get('ema_short')
                        ema_l = signal.get('ema_long')
                        upper_stop = signal.get('upper_stop')
                        lower_stop = signal.get('lower_stop')
                        logger.info(
                            f"{symbol} [双均线指标] 收盘价={fmt_price(current_close)}, "
                            f"EMA短={fmt_price(ema_s)}, EMA长={fmt_price(ema_l)}, "
                            f"N日高={fmt_price(upper_stop)}, "
                            f"N日低={fmt_price(lower_stop)}, "
                            f"信号={signal.get('action', '无')}")

                    position = self.trade_state.get_open_position(symbol)

                    if position:
                        if strategy_type == 'turtle':
                            self.handle_open_position_turtle(symbol, signal, position, symbol_config)
                        elif strategy_type == 'ma_cross':
                            self.handle_open_position_ma_cross(symbol, signal, position, symbol_config, df)
                    else:
                        if symbol_config.get('_retired_from_pool'):
                            logger.info(
                                f"{symbol} 已退池且当前仓位已结束；禁止新开仓并完成生命周期清理")
                        elif strategy_type == 'turtle':
                            self.handle_no_position_turtle(symbol, signal, symbol_config, df)
                        elif strategy_type == 'ma_cross':
                            self.handle_no_position_ma_cross(symbol, signal, symbol_config, df)

                    if strategy_type == 'turtle':
                        if turtle_signal_id:
                            outcome = signal.get('_execution_outcome')
                            reconciled = False
                            candle_already_marked = False
                            if (isinstance(outcome, dict) and
                                    outcome.get('status') == 'rolled_back'):
                                pending = self.trade_state.get_pending_signal_execution(
                                    symbol, signal.get('_client_order_id'))
                                reconciled = bool(pending) and self._finalize_rolled_back_turtle_signal(
                                    symbol, pending, outcome)
                                candle_already_marked = reconciled
                            elif (isinstance(outcome, dict) and
                                  outcome.get('status') == 'rollback_incomplete'):
                                reconciled = False
                            else:
                                reconciled = self._confirm_turtle_signal_if_reconciled(
                                    symbol, turtle_signal_id, turtle_signal_side)
                            if not reconciled:
                                logger.error(
                                    f'{symbol} [海龟] 信号交易尚未与账本/交易所收口；'
                                    '不推进 K 线幂等标记，交由日内重试')
                                failed_symbols.append(symbol)
                                continue
                            if not candle_already_marked:
                                self.trade_state.mark_candle_processed(
                                    symbol, 'turtle', candle_id)
                        else:
                            post_position = self.trade_state.get_open_position(symbol)
                            if self._turtle_exit_still_pending(
                                    signal, position, post_position,
                                    retired_from_pool=bool(
                                        symbol_config.get('_retired_from_pool'))):
                                logger.error(
                                    f'{symbol} [海龟] 退出目标尚未归零；'
                                    '不推进 K 线幂等标记，等待日内重试')
                                failed_symbols.append(symbol)
                                continue
                            self.trade_state.mark_candle_processed(
                                symbol, 'turtle', candle_id)
                    else:
                        # EMA 交叉只有在目标方向已经真实落到账本后才可消费。
                        # 平仓失败/部分成交/反手开仓失败时保留旧 marker，让 08:01
                        # 及 30 分钟兜底按同一根交叉再次收敛，而不是永久吞信号。
                        target_side = signal.get('action')
                        if target_side in ('long', 'short'):
                            post_position = self.trade_state.get_open_position(symbol)
                            retired_exit_complete = bool(
                                symbol_config.get('_retired_from_pool') and
                                position and position.get('side') != target_side and
                                not post_position)
                            if (not retired_exit_complete and
                                    (not post_position or
                                     post_position.get('side') != target_side)):
                                logger.error(
                                    f'{symbol} [双均线] 目标仓位 {target_side} 尚未对齐；'
                                    '不推进 K 线幂等标记，等待日内重试')
                                failed_symbols.append(symbol)
                                continue
                        self.trade_state.mark_candle_processed(
                            symbol, 'ma_cross', candle_id)

                except Exception as sym_e:
                    logger.exception(f"{symbol} 本轮检查异常，跳过该品种继续: {sym_e}")
                    failed_symbols.append(symbol)

            try:
                pruner = getattr(self.trade_state, 'prune_inactive_symbol_metadata', None)
                removed_metadata = (
                    pruner(symbol_config_map.keys()) if callable(pruner) else [])
                if removed_metadata:
                    logger.info(
                        f"已清理 {len(removed_metadata)} 个退池且无仓品种的信号元数据: "
                        f"{', '.join(removed_metadata)}")
            except Exception as e:
                logger.warning(f"清理退池品种信号元数据失败（不影响本轮交易）: {e}")

            # 信号检查完成后按汇总顺序推送，避免 08:00 单条消息过多触发限流
            self._flush_pending_trade_notifications()
            if self._pending_stop_loss_updates:
                logger.info(f"信号检查完毕，推送止损更新汇总({len(self._pending_stop_loss_updates)}条)...")
                self.notifier.notify_stop_loss_updates_summary(self._pending_stop_loss_updates)
            logger.info("信号检查完毕，刷新账户统计状态...")
            self.equity_tracker.refresh_account_stats_state()
            logger.info("信号检查完毕，推送每日持仓汇总...")
            self.send_daily_position_summary_if_due(
                mark_sent=not manual_run, summary_date=today)
            if data_unready_symbols:
                logger.warning(
                    f"本轮 {len(data_unready_symbols)} 个空仓品种历史 K 线尚不足（结构性未就绪）: "
                    f"{', '.join(sorted(data_unready_symbols))}；不触发日内全局重试")
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
                self._mark_daily_check_complete(today)
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


    def _catchup_schedule_slot(self, now):
        """计算当前时刻应归属的最近一次日检调度日。

        常规配置在今日正点前不补昨日（保持原语义）；唯一例外是
        23:58/23:59 的 2 分钟正常调度窗口跨过零点，此时必须仍归属前一调度日。
        """
        sched = self.config.get('scheduler', {})
        check_hour = sched.get('check_hour', 8)
        check_minute = sched.get('check_minute', 0)
        scheduled_today = now.replace(
            hour=check_hour, minute=check_minute, second=0, microsecond=0)
        if now >= scheduled_today:
            slot = scheduled_today
        else:
            previous = scheduled_today - timedelta(days=1)
            if (previous + timedelta(minutes=2)).date() == previous.date():
                return None
            slot = previous
        if now < slot + timedelta(minutes=2):
            return None
        return slot

    def _run_startup_catchup_check(self, now=None):
        """兜底补跑：已过今日检查时间而今日未执行过日检时，立即补跑一轮
        （启动时调用一次 + 每 30 分钟周期兜底，守卫幂等，已跑则空转）。

        场景：服务器恰在 08:00 前后宕机/重启，错过当天全部调度点——不补跑则当天的
        新信号与止损推进整日缺席。信号基于已收盘日线，补跑与 08:00 正点执行等价；
        _last_check_date 已持久化到主账本：成功调度日跨重启仍去重，
        未完成调度日才会补跑。持仓对账/信号 ID/T+1 继续作为业务幂等防护。
        缓冲 2 分钟：恰在调度窗口内启动时，让正常 cron（:05/:20/:40 与 +1 分钟重试）先走。
        """
        now = now or datetime.now()
        slot = self._catchup_schedule_slot(now)
        if slot is None:
            return
        schedule_date = slot.date().isoformat()
        if self._last_check_date == schedule_date:
            return
        logger.warning(
            f"[{self.label}] 调度日 {schedule_date} 日检未完成，兜底补跑一轮")
        self.check_and_execute_trades(scheduled_date=schedule_date)

    def _run_daily_check_retry(self, now=None):
        """+1 分钟重试；23:59→00:00 时仍使用前一调度日的去重键。"""
        now = now or datetime.now()
        sched = self.config.get('scheduler', {})
        total = sched.get('check_hour', 8) * 60 + sched.get('check_minute', 0)
        rolled = total + 1 >= 24 * 60
        schedule_date = (now.date() - timedelta(days=1) if rolled else now.date()).isoformat()
        self.check_and_execute_trades(scheduled_date=schedule_date)

    def _run_daily_summary_retry(self, now=None):
        """持仓汇总 +1 分钟重试；跨零点时不把前一调度日错记成新日期。"""
        now = now or datetime.now()
        sched = self.config.get('scheduler', {})
        total = sched.get('summary_hour', 8) * 60 + sched.get('summary_minute', 0)
        rolled = total + 1 >= 24 * 60
        summary_date = (now.date() - timedelta(days=1) if rolled else now.date()).isoformat()
        self.send_daily_position_summary_if_due(summary_date=summary_date)

    def _apply_deploy_restart_skip_catchup(self, now=None):
        """部署重启专用护栏：显式要求时只跳过今天的启动兜底日检。

        当调度日尚未成功、但部署方明确要放弃本日补跑时，可在
        本次重启的进程环境里设 TRADING_SKIP_STARTUP_CATCHUP_ONCE=1，把今日标记为已
        日检，避免启动/30分钟兜底补跑；次日自然恢复正常 08:00 日检。

        只在已过今日检查窗口（与 _run_startup_catchup_check 同一阈值）时生效：
        未到检查时间本就没有兜底补跑可跳，若此时也标记，当天正点日检会被
        _last_check_date 拦截，整日的新信号与止损推进丢失——标志按无效处理并告警。
        """
        if os.environ.get('TRADING_SKIP_STARTUP_CATCHUP_ONCE') != '1':
            return False
        now = now or datetime.now()
        slot = self._catchup_schedule_slot(now)
        sched = self.config.get('scheduler', {})
        check_hour = sched.get('check_hour', 8)
        check_minute = sched.get('check_minute', 0)
        if slot is None:
            logger.warning(
                f"[{self.label}] TRADING_SKIP_STARTUP_CATCHUP_ONCE=1 已忽略：尚未到今日 "
                f"{check_hour:02d}:{check_minute:02d} 日检时间，无兜底补跑可跳过；"
                "若此时标记会吞掉今天的正点日检")
            return False
        today = slot.date().isoformat()
        self._mark_daily_check_complete(today)
        logger.warning(
            f"[{self.label}] 已按 TRADING_SKIP_STARTUP_CATCHUP_ONCE=1 标记今日({today})已日检，"
            "本次部署重启跳过启动兜底补跑；次日正常恢复"
        )
        return True

    def start(self):
        """启动交易系统：注册定时任务、启动调度、阻塞主循环。"""
        logger.info("启动交易系统...")
        if self._stop_event.is_set():
            logger.warning("启动前已收到停止请求，runner 不再启动")
            return
        try:
            # 注册、启动与启动补跑也必须在 finally 保护内：任一阶段抛异常时都要
            # 关闭已部分启动的 scheduler 并清空心跳，不能留下 Web 正常但后台残活。
            skip_startup_catchup = self._apply_deploy_restart_skip_catchup()
            self.register_jobs(self.config.get('scheduler', {}))
            self.scheduler.start()
            self._update_runner_heartbeat()
            logger.info(f"[{self.label}] 调度已启动，等待定时任务...")
            if not skip_startup_catchup and not self._stop_event.is_set():
                self._run_startup_catchup_check()
            elif self._stop_event.is_set():
                logger.info(f"[{self.label}] runner 已收到停止请求，跳过启动补跑")
            while not self._stop_event.wait(60):
                self._update_runner_heartbeat()
        except KeyboardInterrupt:
            logger.info("收到中断信号，关闭交易系统...")
        finally:
            try:
                if getattr(self.scheduler, 'running', False):
                    self.scheduler.shutdown(wait=True)
            finally:
                self._update_runner_heartbeat(stopped=True)

    def stop(self):
        """请求 runner 停止。不拿交易锁，避免 worker-exit 与长交易互锁。"""
        self._stop_event.set()

    def _update_runner_heartbeat(self, stopped=False):
        with self._heartbeat_lock:
            self._runner_heartbeat_ts = None if stopped else time.time()

    def health_snapshot(self):
        """Web 层可用的真实 runner/scheduler/心跳快照。"""
        with self._heartbeat_lock:
            heartbeat = self._runner_heartbeat_ts
        scheduler_running = bool(getattr(self.scheduler, 'running', False))
        scheduler_thread = getattr(self.scheduler, '_thread', None)
        scheduler_thread_alive = (
            bool(scheduler_thread.is_alive()) if scheduler_thread is not None else scheduler_running)
        age = (time.time() - heartbeat) if heartbeat is not None else None
        healthy = bool(
            scheduler_running and scheduler_thread_alive and
            heartbeat is not None and age <= 150 and
            not self._stop_event.is_set())
        return {
            'healthy': healthy,
            'scheduler_running': scheduler_running,
            'scheduler_thread_alive': scheduler_thread_alive,
            'runner_heartbeat_ts': heartbeat,
            'heartbeat_age_seconds': age,
            'stopping': self._stop_event.is_set(),
        }

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

        # 日内权益采样（间隔与 EquityTracker 分桶常量同源）用于前端求索指数
        self.scheduler.add_job(self._update_runner_heartbeat, 'interval',
                              id=f'{ex}_runner_heartbeat', max_instances=1, coalesce=True,
                              misfire_grace_time=120, minutes=1)
        equity_tick_interval = EquityTracker.EQUITY_TICK_INTERVAL_MINUTES
        self.scheduler.add_job(self._record_equity_tick_with_alert, 'cron',
                              id=f'{ex}_equity_tick', max_instances=1, coalesce=True, misfire_grace_time=120,
                              minute=f'*/{equity_tick_interval}', second=15)

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
        retry_hour, retry_minute = divmod(
            (check_hour * 60 + check_minute + 1) % (24 * 60), 60)
        self.scheduler.add_job(self._run_daily_check_retry, 'cron',
                              id=f'{ex}_daily_check_retry', max_instances=1, coalesce=True, misfire_grace_time=60,
                              hour=retry_hour, minute=retry_minute, second=0)
        # 日检兜底：主执行与 +1 分钟重试整窗失败（如恰逢网络故障）后当日再无触发点——
        # 每 30 分钟由幂等守卫补跑（时间窗 + _last_check_date + 交易锁，已跑则空转）
        self.scheduler.add_job(self._run_startup_catchup_check, 'cron',
                              id=f'{ex}_daily_check_fallback', max_instances=1, coalesce=True, misfire_grace_time=120,
                              minute='*/30', second=0)
        # 每日持仓汇总保持独立兜底调度，避免交易检查提前返回/异常时漏推
        self.scheduler.add_job(self.send_daily_position_summary_if_due, 'cron',
                              id=f'{ex}_daily_summary', max_instances=1, coalesce=True, misfire_grace_time=120,
                              hour=summary_hour, minute=summary_minute, second=50)
        summary_retry_hour, summary_retry_minute = divmod(
            (summary_hour * 60 + summary_minute + 1) % (24 * 60), 60)
        self.scheduler.add_job(self._run_daily_summary_retry, 'cron',
                              id=f'{ex}_daily_summary_retry', max_instances=1, coalesce=True, misfire_grace_time=120,
                              hour=summary_retry_hour, minute=summary_retry_minute, second=20)
        self.scheduler.add_job(self.send_weekly_report, 'cron',
                              id=f'{ex}_weekly', day_of_week='mon', hour=weekly_hour, minute=weekly_minute, second=0)

        # 启动即采一次权益
        self._record_equity_tick_with_alert()
        logger.info(
            f"[{self.label}] 定时任务已注册，每日检查 {check_hour:02d}:{check_minute:02d}，"
            f"盘中止损巡检每 {stop_loss_scan_interval} 分钟"
        )


if __name__ == '__main__':
    acquire_runner_lock()
    TradingSystem().start()
