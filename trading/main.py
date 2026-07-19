import copy
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
from exchange_base import ExchangeApi
from okx_api import OkxApi
from equity_tracker import EquityTracker
from ma_cross_strategy import MaCrossStrategy
from risk_manager import RiskManager
from dingtalk_notifier import DingTalkNotifier
from trade_state import (
    AtomicWriteCommitDurabilityError, TradeState,
    TradeStateCommitDurabilityError, TradeStatePersistenceError,
    atomic_write_json,
    load_strict_json, open_private_text_file, owner_auxiliary_state_paths,
    private_file_exists, state_has_lifecycle_data,
    validate_okx_owner_manifest,
)
from stop_guardian import StopGuardianMixin
from reporting import ReportingMixin
from signal_handlers import (
    NO_POSITION_T1_BLOCKED, NO_POSITION_T1_REENTRY_FAILED,
    SignalHandlersMixin,
)
from trade_executor import TradeExecutorMixin
from runtime_guard import (
    acquire_runner_lock, assess_runtime_health, catchup_schedule_slot,
    runtime_data_path,
)
import config_validation as cfgv

# 日志轮转（10MB自动切割，保留5个备份）。路径锚定项目目录，避免 systemd/cron 等不同 cwd 下日志写错位置。
# delay=True 懒打开：首条日志时才创建文件——测试进程导入 main 时（根 logger 已被测试挂
# NullHandler，basicConfig 空转）不再凭空建文件/占句柄；生产启动毫秒内即写日志，行为等价
_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_FILE = runtime_data_path('trading.log', default_dir=_CODE_DIR)
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

# OKX 的已撤未成交订单官方只承诺约 2 小时留存。不在边界上
# 赌交易所/本机时钟和索引延迟：只在严格小于 90 分钟时消费 absent。
# 只使用首次外部写之前固化且永不刷新的 created_at，updated_at 不能延寿。
_PENDING_ORDER_ABSENCE_PROOF_WINDOW = timedelta(minutes=90)

# 单一具体系统的真钱编排能力闭包。这不是“兼容接口”：任何
# 方法在重构中漂移，都必须在获取权益、启动对账或任何 POST 前拒绝启动。
_REQUIRED_TRADE_STATE_RUNTIME_METHODS = (
    'get_open_position', 'get_all_open_positions',
    'get_open_intent', 'get_open_intents',
    'prepare_open_intent', 'resolve_open_intent',
    'mark_open_intent_unresolved_execution',
    'force_runtime_mark_open_intent_unresolved_execution',
    'finalize_unresolved_open_execution',
    'finalize_open_intent_round_trip',
    'add_open_position', 'add_open_after_partial_rollback',
    'force_runtime_add_open_after_partial_rollback',
    'add_untracked_open_position',
    'force_runtime_add_untracked_open_position',
    'prepare_close_intent', 'get_close_intent',
    'resolve_zero_fill_close_intent',
    'get_safety_blocker_counts',
    'apply_partial_close', 'force_runtime_apply_partial_close',
    'close_position', 'force_runtime_close_position',
    'update_stop_loss', 'force_runtime_update_stop_loss',
    'is_position_quarantined', 'get_position_quarantines',
    'mark_position_quarantine',
    'force_runtime_mark_position_quarantine',
    'clear_position_quarantine',
    'mark_stop_residue', 'force_runtime_mark_stop_residue',
    'has_stop_residue', 'get_stop_residues', 'clear_stop_residue',
    'get_runtime_persistence_status',
    'mark_runtime_persistence_degraded',
    'replace_stop_loss_dates', 'get_stop_loss_dates',
    'stop_loss_dates_migrated',
    'remove_symbol_metadata', 'prune_inactive_symbol_metadata',
)
_REQUIRED_OKX_DIRECT_RUNTIME_HELPERS = (
    '_get_contract_size', '_coin_to_contracts', '_contracts_to_coins',
)
_REQUIRED_EXCHANGE_API_OVERRIDES = (
    'to_ccxt_symbol', 'get_position', 'list_position_symbols',
    'verify_one_way_mode',
    'setup_symbol',
    'open_position', 'close_position', 'compensation_client_order_id',
    'create_stop_loss_order', 'cancel_stop_order_only',
    'cancel_order', 'cancel_all_orders',
    'round_quantity', 'get_quantity_precision', 'find_stop_order_state',
    'find_existing_open_order', 'find_compensation_close_evidence',
    'find_compensation_close_progress', 'confirm_stop_execution',
)
_REQUIRED_SYSTEM_RUNTIME_METHODS = (
    '_execute_open', '_maintenance_open_gate_status',
    '_submit_persisted_close', '_handle_partial_close',
    '_cancel_stop_order_confirmed',
    '_close_trade_state_with_runtime_fallback',
    '_extract_usdt_fee', '_order_ids',
    '_normalize_compensation_close_progress',
    '_quarantine_position_mismatch',
    '_verify_existing_position_or_quarantine',
    '_clear_position_quarantine_after_reconcile',
    '_handle_exchange_flat_close',
    '_mark_possible_unknown_stop_residue',
    '_update_trade_state_stop_with_runtime_fallback',
    '_notify_trade_state_persistence_issue',
    '_finalize_open_intent_rollback',
    '_classify_close_execution', '_handle_unproven_close_execution',
    '_protect_exact_position_during_unresolved_close',
    '_stop_trigger_close_date', '_sync_stop_trigger_date',
)


def _parse_startup_equity(balance):
    """启动权益解析：读不出有限非负数一律返回 None（触发重试/退出）。

    只查 'USDT' in total 会放行 None/字符串垃圾/NaN——NaN 一旦住进
    RiskManager，pending 恢复分支的成交后风险校验（risk_amount > 0 对
    NaN 恒 False）会被静默禁用。启动权益必须与开仓前重取同一校验口径。
    允许 0（空账户可以启动，只是算不出任何仓位）。
    """
    if not isinstance(balance, dict):
        return None
    total = balance.get('total')
    if not isinstance(total, dict):
        # total 为字符串/数字等真值垃圾时，(x or {}).get 会 AttributeError；
        # 本函数调用点在启动重试 try 之外，裸抛=进程裸 traceback 死亡。
        return None
    value = total.get('USDT')
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


class TradingSystem(StopGuardianMixin, ReportingMixin, SignalHandlersMixin, TradeExecutorMixin):
    """欧易单所交易系统的装配与调度核心。

    物理分层（mixin，方法仍全部绑定在本类实例上，self 语义与调用链不变）：
      - StopGuardianMixin（stop_guardian.py）：止损防线——验证式撤销确认、残留阻断、
        止损自愈巡检、「交易所已平」统一收尾、账本落盘失败的运行时补偿；
      - ReportingMixin（reporting.py）：通知缓冲/汇总、每日持仓汇总、周报、权益采样告警；
      - SignalHandlersMixin（signal_handlers.py）：双均线信号分派——无仓开仓判定、
        有仓时的止损确认/翻转分派、T+1 重入；
      - TradeExecutorMixin（trade_executor.py）：下单执行——通用开仓（校验/回滚）、
        止损单更新、平仓执行、双均线翻转、开仓落盘失败的交易所侧回滚；
      - 本文件保留：装配与配置、状态迁移与归属护栏、启动同步、日检总指挥
        check_and_execute_trades 与调度注册（真钱编排核心，刻意留在 main 便于审查）。
    """

    def __init__(self, config_file='config.json'):
        """初始化欧易单交易所交易系统。状态文件落项目根目录（与原单所版一致）。"""
        self.config_file = config_file
        self.base_dir = os.path.dirname(os.path.abspath(config_file))
        self.data_dir = self.base_dir
        migration_journal = os.path.join(
            self.data_dir, cfgv.SINGLE_STRATEGY_MIGRATION_JOURNAL)
        if private_file_exists(migration_journal):
            raise RuntimeError(
                f'检测到未完成的单策略迁移事务，拒绝启动: {migration_journal}；'
                '请先停机运行 migrate_single_strategy.py '
                f'--data-dir {self.data_dir} --apply 完成自动恢复')
        self.config = self.load_config(config_file)
        self.exchange_id = 'okx'
        self.label = self.config.get('okx', {}).get('label') or '欧易'
        self._config_lock = threading.RLock()

        webhook = os.environ.get('DINGTALK_WEBHOOK') or self.config.get('dingtalk', {}).get('webhook_url')
        self.notifier = DingTalkNotifier(webhook)
        # 时区守卫：日检时点、T+1 记录、求索指数切日全用系统本地时间，部署要求
        # Asia/Shanghai（UTC+8）。不符必须拒绝启动；不给调度器单独钉时区，
        # 否则会造出「调度上海时、业务本地时」的双时钟系统。
        _tz_offset = datetime.now().astimezone().utcoffset()
        if _tz_offset != timedelta(hours=8):
            _tz_msg = (f'[{self.label}] 服务器时区异常：当前 UTC 偏移 {_tz_offset}，部署要求 UTC+8'
                       f'（Asia/Shanghai）。日检时点/T+1 记录/求索指数切日均依赖本地时间，'
                       f'已拒绝启动，请先修正服务器时区！')
            logger.critical(_tz_msg)
            try:
                self.notifier.notify_error(_tz_msg)
            except Exception as exc:
                logger.debug('发送时区异常告警失败（仍拒绝启动）: %s', exc)
            raise RuntimeError(_tz_msg)

        # 先以类级能力闭包裁决全部真钱调用链。此处不能构造 OkxApi：其构造
        # 会建立 ccxt 客户端并读取市场；接口漂移必须在任何交易所 I/O 前拒启。
        self._assert_runtime_class_contract()

        # 旧 data/okx 若未由离线迁移明确收口则拒绝启动；runtime 绝不自动导入。
        self._migrate_okx_legacy_state()

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

        self.ma_cross_strategy = MaCrossStrategy(
            self.config['strategy'].get('ma_short_period', 7),
            self.config['strategy'].get('ma_long_period', 28),
            self.config['strategy'].get('ma_stop_period', 28)
        )

        # 只有全部本地门禁通过后才允许接触交易所。构造器只建立客户端并读取
        # 市场；账户模式仅只读回验，绝不在启动时隐式修改真实账户设置。
        self.exchange_api = OkxApi(self.config['okx'])
        self._assert_runtime_contract()
        self.exchange_api.verify_one_way_mode()

        self.scheduler = BackgroundScheduler()
        self._last_check_date = self.trade_state.get_last_daily_check_date()  # 跨重启防重复执行
        self._last_summary_date = self.trade_state.get_last_daily_summary_date()
        self._pending_trade_open_notifications = []
        self._pending_trade_close_notifications = []
        self._trade_lock = threading.Lock()  # 防并发执行锁
        self._summary_lock = threading.Lock()  # 每日汇总「查重→推送→标记」的原子化（兜底调度与日检可能并发）
        self._stop_anomalies = {}  # 止损异常状态（mismatch/补挂失败），供前端警示与告警节流
        self._last_failure_notify_ts = 0
        self._equity_tick_fail_streak = 0
        self._equity_tick_alert_sent = False
        self._last_trade_check_failure = None
        self._last_successful_trade_check_ts = None
        self._last_guardian_failure = None
        self._last_successful_guardian_ts = None
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
            account_equity = _parse_startup_equity(balance)
            if account_equity is not None:
                break
            logger.warning(f'[{self.label}] 启动时获取账户权益失败或非有限非负数，'
                           f'10秒后重试... (第{_retry_i+1}/3次)')
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

        self.risk_manager = RiskManager(account_equity)
        logger.info(f"[{self.label}] 交易系统初始化完成，账户权益: {account_equity} USDT")

        try:
            self.sync_positions_on_startup()
        except Exception as exc:
            # 与启动权益失败同标准：进程拒绝带着未对账的账本运行，但退出前
            # 必须大声告警——裸 traceback 静默死亡是最贵的故障模式。
            logger.critical(f'[{self.label}] 启动持仓对账失败，拒绝启动: {exc}')
            try:
                self.notifier.notify_error(
                    f'[{self.label}] 启动持仓对账失败，进程拒绝启动，'
                    f'请立即人工检查: {exc}')
            except Exception as notify_exc:
                logger.debug('启动对账失败告警发送失败: %s', notify_exc)
            raise

    def _assert_runtime_class_contract(self):
        """在构造交易所适配器前，以具体类锁死全部真钱能力。"""
        required = {
            'TradingSystem': (type(self), _REQUIRED_SYSTEM_RUNTIME_METHODS),
            'TradeState': (TradeState, _REQUIRED_TRADE_STATE_RUNTIME_METHODS),
            'OkxApi': (OkxApi, _REQUIRED_OKX_DIRECT_RUNTIME_HELPERS),
        }
        missing = []
        for label, (owner_type, names) in required.items():
            missing.extend(
                f'{label}.{name}' for name in names
                if not callable(getattr(owner_type, name, None)))
        for name in _REQUIRED_EXCHANGE_API_OVERRIDES:
            implementation = getattr(OkxApi, name, None)
            if not callable(implementation):
                missing.append(f'OkxApi.{name}')
            elif implementation is getattr(ExchangeApi, name, None):
                missing.append(f'OkxApi.{name}(base_stub)')
        if missing:
            raise RuntimeError(
                f'真钱运行接口不完整，拒绝启动: {", ".join(missing)}')

    def _assert_runtime_contract(self):
        """实例装配只复核具体类型；能力已在任何交易所 I/O 前按类裁决。"""
        missing = []
        if type(self.trade_state) is not TradeState:
            missing.append('TradeState.concrete_type')
        if type(self.exchange_api) is not OkxApi:
            missing.append('OkxApi.concrete_type')
        if missing:
            raise RuntimeError(
                f'真钱运行接口不完整，拒绝启动: {", ".join(missing)}')

    def load_config(self, config_file):
        """加载欧易单所配置：{okx:{凭据...}, strategy, trading, scheduler, dingtalk}。
        兼容旧多所格式（exchanges.okx）自动展平；环境变量可覆盖凭据。"""
        # 配置可能直接含 API 密钥；与命脉账本使用同一套 O_NOFOLLOW +
        # fstat/inode/owner 校验，消除 lstat/chmod/open 之间的替换窗口。
        with open_private_text_file(config_file) as f:
            config = load_strict_json(f)
        if not isinstance(config, dict):
            raise ValueError('config 顶层必须是对象')
        # 旧多所格式只允许存在一份 exchanges.okx；收敛后立即删除旧容器，
        # 避免 runner、验证脚本与持久化路径各自读取不同账户配置。
        cfgv.canonicalize_single_okx_config(config)
        okx = config.setdefault('okx', {})
        if not isinstance(okx, dict):
            raise ValueError('config.okx 必须是对象')
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
        _pass = cfgv.resolve_optional_alias(
            os.environ.get('OKX_API_PASSPHRASE'),
            os.environ.get('OKX_PASSWORD'),
            'OKX passphrase 环境变量')
        if _pass:
            okx['password'] = _pass
            self._env_okx_credential_keys.add('password')
        if any(
                not isinstance(okx.get(key), str) or not okx[key].strip()
                for key in ('apiKey', 'secret', 'password')):
            raise ValueError('未配置 OKX API 凭据（apiKey/secret/passphrase），请在 config.json 或环境变量中提供')
        config.setdefault('strategy', {})
        config.setdefault('trading', {'symbols': []})
        if isinstance(config['trading'], dict):
            config['trading'].setdefault('symbols', [])
        config.setdefault('scheduler', {})
        cfgv.validate_and_normalize_execution_config(config)
        config.setdefault('dingtalk', {})
        return config

    def _read_private_json_for_startup(self, path, context):
        try:
            with open_private_text_file(path) as handle:
                return load_strict_json(handle)
        except Exception as exc:
            raise RuntimeError(
                f'{context}读取 {path} 失败({exc})，状态不明拒绝启动。') from exc

    def _migrate_okx_legacy_state(self):
        """拒绝在 MA-only 运行时自动导入未经单策略门禁审查的旧状态。"""
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
            try:
                cfgv.validate_okx_legacy_migration_marker(payload)
            except ValueError as exc:
                raise RuntimeError(
                    f'旧状态迁移标记非法: {marker}: {exc}') from exc
            return
        try:
            entries = os.listdir(legacy_dir)
        except OSError as exc:
            raise RuntimeError(f'无法枚举旧状态目录 {legacy_dir}: {exc}') from exc
        if entries:
            raise RuntimeError(
                '检测到未完成标记下的旧 data/okx 状态。MA-only 版本禁止在'
                '启动时自动导入未经过单策略预检的生命周期；请保持停机，'
                '人工核对并收口旧目录后再部署。')

    def _directory_has_unowned_state(self):
        try:
            paths = owner_auxiliary_state_paths(self.base_dir)
        except OSError as exc:
            raise RuntimeError(
                f'无法枚举数据目录中的年度平仓史书: {exc}') from exc
        for path in paths:
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
            if state_has_lifecycle_data(backup_state):
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
            try:
                validate_okx_owner_manifest(manifest)
            except ValueError as exc:
                raise RuntimeError(
                    f'数据目录归属标记非法: {manifest_path}: {exc}') from exc
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
            if (state_has_lifecycle_data(state) or
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
            try:
                return atomic_write_json(self.config_file, disk_config)
            except AtomicWriteCommitDurabilityError:
                # config 已替换，调用方不得回滚内存；同时复用交易账本的永久
                # 降级门闩，使所有中央开仓入口立即 fail closed。
                self.trade_state.mark_runtime_persistence_degraded(
                    'config_directory_fsync_failed_after_replace')
                raise

    def reload_strategies(self):
        """重新加载策略参数"""
        self.ma_cross_strategy = MaCrossStrategy(
            self.config['strategy'].get('ma_short_period', 7),
            self.config['strategy'].get('ma_long_period', 28),
            self.config['strategy'].get('ma_stop_period', 28)
        )
        logger.info(f"策略参数已重新加载: "
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

    @staticmethod
    def _exchange_position_has_contracts(position):
        """严格裁决 get_position 响应；畸形零仓证明必须抛出而非猜 flat。"""
        if position is None:
            return False
        if not isinstance(position, dict):
            raise RuntimeError('持仓查询返回非对象')
        contracts = position.get('contracts')
        if contracts is None or isinstance(contracts, bool):
            raise RuntimeError('持仓查询缺少有效 contracts')
        try:
            contracts = abs(float(contracts))
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError('持仓 contracts 非数值') from exc
        if not math.isfinite(contracts):
            raise RuntimeError('持仓 contracts 非有限数')
        return contracts > 0

    def _position_reconciliation_details(self, symbol, local_position, exchange_position):
        """生成本地/交易所仓位方向与张数对账结果。"""
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        local_side = self._normalise_position_side(local_position)
        exchange_side = self._normalise_position_side(exchange_position)
        raw_exchange_contracts = (exchange_position or {}).get('contracts')
        raw_local_coin = (local_position or {}).get('position_size')
        if isinstance(raw_exchange_contracts, bool) or isinstance(raw_local_coin, bool):
            raise ValueError('仓位数量不能是 bool')
        exchange_contracts = abs(float(raw_exchange_contracts or 0))
        local_coin = float(raw_local_coin or 0)
        if (not math.isfinite(exchange_contracts) or
                not math.isfinite(local_coin) or local_coin <= 0):
            raise ValueError('仓位数量必须是正有限数')
        local_contracts = 0.0
        local_round_trip_coin = 0.0
        local_amount_aligned = False
        if local_position:
            local_contracts = abs(float(
                self.exchange_api._coin_to_contracts(ccxt_symbol, local_coin)))
            local_round_trip_coin = abs(float(
                self.exchange_api._contracts_to_coins(
                    ccxt_symbol, local_contracts)))
            if not all(math.isfinite(value) and value > 0 for value in (
                    local_contracts, local_round_trip_coin)):
                raise ValueError('本地仓位无法换算为正有限张数')
            # _coin_to_contracts 是下单边界，会向下对齐到交易所 step；它不能
            # 单独充当相等比较，否则 10.9→10 的截断会把账本超额静默洗掉。
            # 只有币数→张数→币数往返保持原量，才允许继续比较真实张数。
            round_trip_tolerance = max(
                1e-15,
                math.ulp(max(abs(local_coin), 1.0)) * 8,
                math.ulp(max(abs(local_round_trip_coin), 1.0)) * 8,
            )
            local_amount_aligned = math.isclose(
                local_coin, local_round_trip_coin,
                rel_tol=0.0, abs_tol=round_trip_tolerance)
        side_match = local_side == exchange_side
        contract_tolerance = max(
            1e-12,
            math.ulp(max(abs(local_contracts),
                         abs(exchange_contracts), 1.0)) * 8,
        )
        quantity_match = bool(
            local_amount_aligned and
            abs(local_contracts - exchange_contracts) <= contract_tolerance)
        return {
            'local_side': local_side,
            'exchange_side': exchange_side,
            'local_coin': local_coin,
            'local_round_trip_coin': local_round_trip_coin,
            'local_amount_aligned': local_amount_aligned,
            'local_contracts': local_contracts,
            'exchange_contracts': exchange_contracts,
            'contract_tolerance': contract_tolerance,
            'side_match': side_match,
            'quantity_match': quantity_match,
            'matched': bool(side_match and quantity_match),
        }

    def _quarantine_position_mismatch(
            self, symbol, reason, details=None, *, notify=True,
            stop_residue_possible=False):
        """隔离仓位不一致品种；磁盘故障时至少保住本进程阻断。"""
        try:
            previous = bool(self.trade_state.is_position_quarantined(symbol))
        except Exception:
            previous = False
        persist_error = None
        mark_kwargs = (
            {'stop_residue_possible': True}
            if stop_residue_possible else {})
        try:
            self.trade_state.mark_position_quarantine(
                symbol, reason, details, **mark_kwargs)
        except Exception as exc:
            persist_error = exc
            try:
                self.trade_state.force_runtime_mark_position_quarantine(
                    symbol, reason, details, **mark_kwargs)
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
        # 或有 pending open intent / 未知算法单残留。所有调用方统一经过
        # 这道中央门；任一状态读取不确定都失败关闭，绝不各自猜测可清。
        try:
            if self.trade_state.get_open_intent(symbol):
                return False
            if self.trade_state.get_close_intent(symbol):
                return False
            position = self.trade_state.get_open_position(symbol)
            stop_residue = self.trade_state.has_stop_residue(symbol)
        except Exception as exc:
            logger.error(f'{symbol} 无法证明隔离解除条件: {exc}')
            return False
        if position and (
                not position.get('stop_order_id') or
                position.get('stop_resize_pending')):
            return False
        if stop_residue:
            return False
        if self.trade_state.clear_position_quarantine(symbol):
            logger.warning(f"{symbol} 本地/交易所仓位已重新一致，自动解除隔离")
            return True
        return False

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
            try:
                lifecycle_intent = self.trade_state.get_open_intent(symbol)
                if lifecycle_intent:
                    if not self._reconcile_position_open_intent(
                            symbol, lifecycle_intent,
                            open_positions[symbol]):
                        # 未终态执行优先于 generic flat/position 对账；否则会先
                        # 记平余仓再丢掉迟到开仓/单笔补偿订单的恢复责任人。
                        self._protect_unresolved_lifecycle_position(
                            symbol, lifecycle_intent,
                            open_positions[symbol])
                        continue
                    refreshed_local = self.trade_state.get_open_position(symbol)
                    if not refreshed_local:
                        continue
                    open_positions[symbol] = refreshed_local
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
                        "启动同步平仓")
                    if not closed_position:
                        logger.warning(f"{symbol} 启动同步时本地状态补偿失败，保留原状态等待人工处理")
                        self._quarantine_position_mismatch(
                            symbol, '交易所已空仓但本地平仓补偿失败')
                        continue
                    if not _state_saved:
                        # 持久化故障处理器已用同一条告警建立运行时隔离；这里不再
                        # 对同一磁盘故障重复通知。
                        continue
                    # 启动发现仓位已被止损/人工平掉，与盘中守护、日检
                    # 用同一原子状态迁移：平仓与 T+1 已经同事务落盘。
                    self._clear_position_quarantine_after_reconcile(symbol)
                else:
                    local_position = open_positions[symbol]
                    if self._verify_existing_position_or_quarantine(
                            symbol, local_position, position, clear_on_match=False):
                        strategy_name = self._get_strategy_display_name()
                        if not self._ensure_stop_order_alive(
                                symbol, ccxt_symbol, local_position, strategy_name):
                            self._quarantine_position_mismatch(
                                symbol, '启动时仓位一致，但交易所止损保护未能严格确认')
                            continue
                        self._clear_position_quarantine_after_reconcile(symbol)
                        logger.info(f"{symbol} 持仓方向与张数同步成功")
            except Exception as exc:
                # 单品种启动对账异常绝不连累其余品种，也绝不让构造
                # 函数裸崩；隔离本身会持久化并告警，该品种交易被阻断。
                logger.exception(
                    f'{symbol} 启动对账异常，隔离后继续其余品种: {exc}')
                self._quarantine_position_mismatch(
                    symbol, f'启动对账异常: {exc}')

        # 本地/交易所都空仓的 open intent 也必须主动裁决：不能只在
        # “交易所有孤儿仓”时才触发恢复。
        self._reconcile_all_open_intents('启动')

        # 反向核对：交易所有仓但本地无记录（本地状态损坏丢失/人工开仓）——
        # 该仓不会被系统托管，必须持久化隔离，不能仅发一条告警后继续开仓。
        try:
            exchange_symbols = set(self.exchange_api.list_position_symbols())
            local_symbols = set(self.trade_state.get_all_open_positions().keys())
            orphans = sorted(exchange_symbols - local_symbols)
            if orphans:
                for orphan in orphans:
                    self._quarantine_position_mismatch(
                        orphan, '交易所有仓但本地无账本记录（孤儿仓）')
            # 只有本轮完整反向查询成功才可解除已经实际双边空仓的旧隔离。
            quarantined_symbols = list(
                self.trade_state.get_position_quarantines().keys())
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
                    legacy = load_strict_json(f)
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
        self.stop_loss_dates = self.trade_state.replace_stop_loss_dates(
            self.stop_loss_dates)
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

    def _recovery_symbol_config(self, symbol):
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
                '_retired_from_pool': True,
            }, True
        symbol_config = dict(configured)
        retired = not symbol_config.get('enabled', True)
        if retired:
            symbol_config['_retired_from_pool'] = True
        return symbol_config, retired


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
        if not isinstance(order, dict):
            return False, None
        raw_info = order.get('info')
        if raw_info is not None and not isinstance(raw_info, dict):
            return False, None
        info = raw_info or {}

        status_aliases = {
            'closed': 'filled', 'filled': 'filled',
            'canceled': 'canceled', 'cancelled': 'canceled',
            'mmp_canceled': 'canceled', 'mmp_cancelled': 'canceled',
            'rejected': 'rejected', 'expired': 'expired',
            'open': 'open', 'live': 'open', 'partially_filled': 'open',
            'pending': 'open',
        }
        raw_states = [
            value for value in (
                order.get('status'), info.get('state'), info.get('ordState'))
            if value not in (None, '')]
        states = [
            status_aliases.get(str(value).lower()) for value in raw_states]
        if (not states or any(value is None for value in states) or
                len(set(states)) != 1):
            return False, None
        status = states[0]
        terminal = status in {'filled', 'canceled', 'rejected', 'expired'}

        def evidence_values(*values):
            parsed = []
            for value in values:
                if value in (None, ''):
                    continue
                number = self._finite_nonnegative(value)
                if number is None:
                    return None
                parsed.append(number)
            return parsed

        amounts = evidence_values(order.get('amount'), info.get('sz'))
        filled_values = evidence_values(
            order.get('filled'), info.get('accFillSz'))
        remaining_values = evidence_values(order.get('remaining'))
        if amounts is None or filled_values is None or remaining_values is None:
            return False, None

        amount = amounts[0] if amounts else None
        tolerance = max(
            1e-12,
            math.ulp(max(abs(amount or 0.0), 1.0)) * 8,
        )
        if ((amounts and max(amounts) - min(amounts) > tolerance) or
                (filled_values and
                 max(filled_values) - min(filled_values) > tolerance) or
                (remaining_values and
                 max(remaining_values) - min(remaining_values) > tolerance)):
            return False, None
        if amount is None or amount <= tolerance:
            # find_existing_open_order 的公共契约本应已证明委托量；恢复层
            # 若仍拿不到，就不能判断成交量守恒。
            return False, None

        filled = filled_values[0] if filled_values else None
        remaining = remaining_values[0] if remaining_values else None
        if amount is not None:
            if ((filled is not None and filled > amount + tolerance) or
                    (remaining is not None and
                     remaining > amount + tolerance) or
                    (filled is not None and remaining is not None and
                     abs((filled + remaining) - amount) > tolerance)):
                return False, None
        if filled is None and amount is not None and remaining is not None:
            filled = max(0.0, amount - remaining)
        # closed/filled 只是终态，不是数量证据。filled/remaining 同时缺失时
        # 必须保持 None；否则会把“未知成交”捏造成满成并写入虚假往返账本。
        if (status == 'filled' and filled is not None and
                abs(filled - amount) > tolerance):
            return False, None
        return terminal, filled




    @staticmethod
    def _pending_order_absence_is_conclusive(execution):
        """校验连续 OrderNotFound 证据是否仍在交易所历史留存上界内。

        即时不存在证明由适配层统一可见性宽限产生；这里的 90 分钟只限制
        历史查询仍可用于裁决的最大年龄，绝不把单次 OrderNotFound 升格。
        """
        raw_created_at = execution.get('created_at')
        if not isinstance(raw_created_at, str) or not raw_created_at.strip():
            return False, 'pending 缺少首次外部写时间戳'
        try:
            created_at = datetime.fromisoformat(raw_created_at)
            now = datetime.now(created_at.tzinfo) if created_at.tzinfo else datetime.now()
            age = now - created_at
        except (TypeError, ValueError, OverflowError) as exc:
            return False, f'pending 下单阶段时间戳非法: {exc}'
        if age.total_seconds() < 0:
            return False, f'pending 首次外部写时间戳位于未来（偏差 {-age}）'
        if age >= _PENDING_ORDER_ABSENCE_PROOF_WINDOW:
            return False, f'pending 已超过交易所可证明未发单的安全窗口（{age}）'
        return True, None



    def _finalize_open_intent_rollback(self, symbol, intent, outcome):
        close_order = (outcome or {}).get('close_order') or {}
        if self._classify_close_execution(close_order) != 'closed':
            return False
        open_order = (outcome or {}).get('open_order') or {}
        open_evidence = self._authoritative_single_order_evidence(open_order)
        close_evidence = self._authoritative_single_order_evidence(close_order)
        position_size = self._order_actual_amount(
            {'amount': (outcome or {}).get('position_size')}, None)
        open_amount = self._order_actual_amount(open_order, None)
        close_amount = self._order_actual_amount(close_order, None)
        conserved = False
        if None not in (position_size, open_amount, close_amount):
            tolerance = max(
                1e-12,
                math.ulp(max(position_size, open_amount, close_amount, 1.0)) * 8)
            conserved = (
                abs(position_size - open_amount) <= tolerance and
                abs(position_size - close_amount) <= tolerance and
                abs(open_amount - close_amount) <= tolerance)
        if not conserved:
            expected = self._order_actual_amount(
                {'amount': intent.get('planned_position_size')}, None)
            try:
                compensation_id = (
                    self.exchange_api.compensation_client_order_id(
                        intent.get('client_order_id')))
                if expected is not None:
                    self._mark_unresolved_open_execution(
                        symbol, intent.get('client_order_id'),
                        'open_compensation', expected,
                        '完整回滚数量不守恒，拒绝消费 open intent',
                        {'open_amount': open_amount,
                         'close_amount': close_amount,
                         'outcome_position_size': position_size},
                        compensation_client_order_id=compensation_id)
            except Exception as exc:
                logger.critical(
                    f'{symbol} 完整回滚数量不守恒，且 lifecycle blocker '
                    f'建立失败: {exc}')
            self._quarantine_position_mismatch(
                symbol, '完整回滚原开仓/补偿/结果数量不守恒')
            return False
        if open_evidence is None or close_evidence is None:
            try:
                compensation_id = (
                    self.exchange_api.compensation_client_order_id(
                        intent.get('client_order_id')))
                self._mark_unresolved_open_execution(
                    symbol, intent.get('client_order_id'),
                    'open_compensation', float(position_size),
                    '完整回滚已观测，但双边订单缺少唯一 ID/'
                    '权威成交均价，拒绝伪造往返财务',
                    {'open_order_ids': self._order_ids(open_order),
                     'close_order_ids': self._order_ids(close_order)},
                    compensation_client_order_id=compensation_id)
            except Exception as exc:
                logger.critical(
                    f'{symbol} 完整回滚财务证据不足，且 lifecycle '
                    f'blocker 建立失败: {exc}')
            self._quarantine_position_mismatch(
                symbol, '完整回滚缺少权威双边财务证据')
            return False
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        try:
            refreshed_position = self.exchange_api.get_position(ccxt_symbol)
            position_present = self._exchange_position_has_contracts(
                refreshed_position)
        except Exception as exc:
            self._quarantine_position_mismatch(
                symbol, f'完整回滚证据后持仓复查不确定: {exc}')
            return False
        if position_present:
            # 这里可能正位于 _resume_open_intent_position 调用栈中；立即递归
            # resume 会重放恢复。保留 intent/quarantine，交下一轮顶层对账接管。
            self._quarantine_position_mismatch(
                symbol, '完整回滚证据后持仓仍/重新出现，拒绝消费 open intent')
            return False
        entry_fee, _entry_currency = self._extract_usdt_fee(
            open_order)
        exit_fee, _exit_currency = self._extract_usdt_fee(close_order)
        self.trade_state.finalize_open_intent_round_trip(
            symbol, intent.get('client_order_id'),
            open_evidence['price'], close_evidence['price'],
            position_size,
            entry_order_ids=open_evidence['order_ids'],
            exit_order_ids=close_evidence['order_ids'],
            entry_fee=entry_fee, exit_fee=exit_fee,
            reason='开仓意图执行后已完整回滚')
        self._clear_position_quarantine_after_reconcile(symbol)
        return True

    def _resume_open_intent_position(self, symbol, intent):
        """只读裁决「有 open intent、无本地仓、交易所有仓」。

        这是最危险的崩溃窗：单笔补偿订单可能已发送但尚未可见。
        严禁再调通用 ``_execute_open``；这里只查原开仓与唯一确定性
        补偿订单，建立无财务 partial 的 provisional 账本后交给一等
        lifecycle 收口。
        """
        payload = intent.get('payload') or {}
        side = intent.get('side')
        try:
            entry_price = float(payload['entry_price'])
            stop_price = float(payload['stop_loss_price'])
            planned = float(intent['planned_position_size'])
        except (KeyError, TypeError, ValueError):
            return False
        if (side not in ('long', 'short') or not math.isfinite(planned) or
                planned <= 0):
            return False
        if isinstance(intent.get('unresolved_execution'), dict):
            # 已有责任人却丢了 local provisional，不得猜测重建。
            self._quarantine_position_mismatch(
                symbol, '未决 lifecycle 存在但 provisional 余仓缺失')
            return False
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        open_client_id = intent.get('client_order_id')
        try:
            order = self.exchange_api.find_existing_open_order(
                ccxt_symbol, side, planned, open_client_id,
                wait_for_visibility=True)
            if order is None:
                raise RuntimeError('原开仓 clOrdId 连续查无')
            terminal, open_filled = self._pending_order_resolution(order)
            if not terminal or open_filled is None:
                raise RuntimeError('原开仓未终态/成交量不可证明')
            planned_contracts = float(self.exchange_api._coin_to_contracts(
                ccxt_symbol, planned))
            if (not math.isfinite(planned_contracts) or
                    planned_contracts <= 0):
                raise RuntimeError('持久化计划量换算张数必须是有限正数')
            tolerance = max(
                1e-12,
                math.ulp(max(abs(planned_contracts), 1.0)) * 8)
            open_filled = float(open_filled)
            if (not math.isfinite(open_filled) or
                    open_filled <= tolerance or
                    open_filled > planned_contracts + tolerance):
                raise RuntimeError('原开仓成交量不在 (0, 持久化计划量]')
            actual_open_amount = float(
                self.exchange_api._contracts_to_coins(
                    ccxt_symbol, open_filled))
            if (not math.isfinite(actual_open_amount) or
                    actual_open_amount <= 0 or
                    actual_open_amount > planned + max(
                        1e-12, math.ulp(max(abs(planned), 1.0)) * 8)):
                raise RuntimeError('原开仓实际成交币数非法')
            compensation_id = self.exchange_api.compensation_client_order_id(
                open_client_id)
            raw_progress = (
                self.exchange_api.find_compensation_close_progress(
                    ccxt_symbol, side, actual_open_amount, open_client_id))
            progress = self._normalize_compensation_close_progress(
                raw_progress,
                expected_client_order_id=compensation_id,
                expected_contracts=open_filled,
                expected_amount=actual_open_amount)
            if progress['presence'] == 'absent':
                conclusive, reason = self._pending_order_absence_is_conclusive(
                    intent)
                if not conclusive:
                    raise RuntimeError(
                        f'补偿订单历史 absent 已不可证明零成交: '
                        f'{reason}')
            expected_remaining_contracts = progress['remaining_contracts']
            if (not math.isfinite(expected_remaining_contracts) or
                    expected_remaining_contracts <= tolerance or
                    expected_remaining_contracts >
                    open_filled + tolerance):
                raise RuntimeError('单笔补偿订单与当前有仓现实不守恒')
            fresh = self.exchange_api.get_position(ccxt_symbol)
            if (not isinstance(fresh, dict) or fresh.get('side') != side or
                    isinstance(fresh.get('contracts'), bool) or
                    fresh.get('contracts') is None or
                    abs(abs(float(fresh['contracts'])) -
                        expected_remaining_contracts) > tolerance):
                raise RuntimeError('终态订单与 fresh 净仓无法唯一归因')
            remaining_amount = float(self.exchange_api._contracts_to_coins(
                ccxt_symbol, expected_remaining_contracts))
            if not self._mark_unresolved_open_execution(
                    symbol, open_client_id, 'open_compensation',
                    actual_open_amount,
                    '崩溃后已只读找回原开仓/单笔补偿订单，建立受托余仓',
                    {'planned_position_size': planned,
                     'actual_open_amount': actual_open_amount,
                     'remaining_amount': remaining_amount},
                    compensation_client_order_id=compensation_id):
                raise RuntimeError('未决执行 blocker 无法原子落盘')
            provisional_entry = self._safe_fill_price(order, entry_price)
            kwargs = dict(
                symbol=symbol, side=side, entry_price=provisional_entry,
                position_size=remaining_amount,
                stop_loss_price=stop_price, stop_order_id=None,
                stop_order_size=remaining_amount, strategy='ma_cross',
                entry_order_ids=self._order_ids(order),
                stop_resize_pending=True,
                quarantine_reason='崩溃恢复余仓等待权威执行收口',
                quarantine_details={
                    'open_client_order_id': open_client_id,
                    'compensation_client_order_id': compensation_id,
                    'remaining_amount': remaining_amount,
                },
                # 崩溃点可能在保护单 POST 之后；未查完整算法单
                # 清单前严禁自动补挂第二张。
                stop_residue_possible=True,
                # 不能把重启时间当成未知保护单的新可见性起点；open intent
                # 在首次外部写之前已落盘，是安全且更早的保守锚点。
                stop_residue_marked_at=intent.get('created_at'),
                open_intent_client_id=open_client_id,
                requested_position_size=planned,
                preserve_open_intent=True)
            try:
                recovered_position = self.trade_state.add_untracked_open_position(
                    **kwargs)
            except TradeStateCommitDurabilityError as exc:
                # rename 已提交，新内存不能再 force 重放；进程门闩已禁开仓。
                recovered_position = exc.committed_result
                logger.critical(
                    f'{symbol} provisional 已提交但账本目录耐久性不可证明；'
                    '保留新内存并继续同栈建立风险保护')
            except TradeStatePersistenceError:
                recovered_position = (
                    self.trade_state.force_runtime_add_untracked_open_position(
                        **kwargs))
            if not recovered_position:
                raise RuntimeError('provisional 余仓未能进入运行时账本')
            logger.critical(
                f'{symbol} 已用只读订单证据建立 provisional '
                '余仓；未调用通用开仓/补偿 POST')
            # 不能等下一轮 guardian：刚恢复出的真实余仓必须在同一交易锁调用栈
            # 立刻按完整算法单清单裁决，并在旧 residue 宽限已过时 make-before-write。
            refreshed_local = self.trade_state.get_open_position(symbol)
            if not refreshed_local:
                raise RuntimeError('provisional 落账后无法重新读取本地余仓')
            try:
                protected = self._protect_unresolved_lifecycle_position(
                    symbol, self.trade_state.get_open_intent(symbol),
                    refreshed_local)
            except Exception as exc:
                protected = False
                logger.exception(
                    f'{symbol} 崩溃恢复余仓本栈保护器异常；保留全部阻断: {exc}')
            if not protected:
                logger.critical(
                    f'{symbol} 崩溃恢复余仓本栈未能严格确认 reduce-only 保护；'
                    '保留 lifecycle blocker/quarantine 并等待下一轮巡检')
            return False
        except Exception as exc:
            self._quarantine_position_mismatch(
                symbol, f'open intent 孤儿仓只读恢复失败: {exc}')
        return False

    def _reconcile_position_open_intent(self, symbol, intent, position):
        """恢复 position+open_intent 共存的一等未决执行，绝不发送新订单。"""
        unresolved = (intent or {}).get('unresolved_execution')
        if not isinstance(unresolved, dict):
            self._quarantine_position_mismatch(
                symbol, '持仓与 open intent 共存但缺少未决执行句柄')
            return False
        kind = unresolved.get('kind')
        if kind == 'open_attribution':
            # 人工同向仓与系统成交已经混合，订单终态本身也不能唯一拆分仓位。
            self._quarantine_position_mismatch(
                symbol, '开仓归因歧义仍需人工裁决，保留 unresolved_execution')
            return False
        side = intent.get('side')
        open_client_id = unresolved.get('open_client_order_id')
        planned = intent.get('planned_position_size')
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        try:
            order = self.exchange_api.find_existing_open_order(
                ccxt_symbol, side, float(planned), open_client_id,
                wait_for_visibility=True)
        except Exception as exc:
            self._quarantine_position_mismatch(
                symbol, f'未决执行原开仓终态查询不确定: {exc}')
            return False
        if order is None:
            self._quarantine_position_mismatch(
                symbol, '已有余仓但原开仓 clOrdId 连续查无，归因不成立')
            return False
        terminal, open_filled_contracts = self._pending_order_resolution(order)
        if not terminal or open_filled_contracts is None:
            self._quarantine_position_mismatch(
                symbol, '原开仓订单仍未终态或成交量证据不完整')
            return False
        try:
            raw_expected_open_amount = unresolved['expected_position_size']
            if isinstance(raw_expected_open_amount, bool):
                raise ValueError('未决执行预期币数不能是 bool')
            expected_open_amount = float(raw_expected_open_amount)
            expected_open_contracts = float(
                self.exchange_api._coin_to_contracts(
                    ccxt_symbol, expected_open_amount))
            if (not math.isfinite(expected_open_amount) or
                    expected_open_amount <= 0 or
                    not math.isfinite(expected_open_contracts) or
                    expected_open_contracts <= 0):
                raise ValueError('未决执行预期币数/张数必须是有限正数')
        except Exception as exc:
            self._quarantine_position_mismatch(
                symbol, f'未决执行数量换算失败: {exc}')
            return False
        tolerance = max(
            1e-12,
            math.ulp(max(abs(expected_open_contracts), 1.0)) * 8)
        if abs(open_filled_contracts - expected_open_contracts) > tolerance:
            self._quarantine_position_mismatch(
                symbol, '原开仓终态成交量与未决执行预期不一致')
            return False

        normalized_progress = None
        if kind == 'open_compensation':
            try:
                derived_compensation_id = (
                    self.exchange_api.compensation_client_order_id(
                        open_client_id))
                if (derived_compensation_id !=
                        unresolved.get('compensation_client_order_id')):
                    raise RuntimeError('补偿句柄不是由原开仓句柄确定性派生')
                raw_progress = (
                    self.exchange_api.find_compensation_close_progress(
                        ccxt_symbol, side, expected_open_amount,
                        open_client_id))
                normalized_progress = (
                    self._normalize_compensation_close_progress(
                        raw_progress,
                        expected_client_order_id=derived_compensation_id,
                        expected_contracts=expected_open_contracts,
                        expected_amount=expected_open_amount))
            except Exception as exc:
                self._quarantine_position_mismatch(
                    symbol, f'未决单笔补偿订单终态查询不确定: {exc}')
                return False
            if normalized_progress['presence'] == 'absent':
                absence_conclusive, absence_reason = (
                    self._pending_order_absence_is_conclusive(unresolved))
                if not absence_conclusive:
                    self._quarantine_position_mismatch(
                        symbol,
                        f'补偿订单 absent，且历史留存不足以证明零成交: '
                        f'{absence_reason}')
                    return False
        expected_remaining = (
            normalized_progress['remaining_contracts']
            if normalized_progress is not None else
            max(0.0, open_filled_contracts))
        # 上述只读查询可能等待多个可见性窗口；裁决紧前重读净仓。
        try:
            exchange_position = self.exchange_api.get_position(ccxt_symbol)
        except Exception as exc:
            self._quarantine_position_mismatch(
                symbol, f'未决执行终态后持仓复查不确定: {exc}')
            return False
        if exchange_position is None:
            exchange_contracts = 0.0
            exchange_side = None
        elif isinstance(exchange_position, dict):
            raw_contracts = exchange_position.get('contracts')
            if isinstance(raw_contracts, bool) or raw_contracts is None:
                self._quarantine_position_mismatch(
                    symbol, '未决执行终态后持仓数量缺失/非法')
                return False
            try:
                exchange_contracts = abs(float(raw_contracts))
            except (TypeError, ValueError, OverflowError):
                self._quarantine_position_mismatch(
                    symbol, '未决执行终态后持仓数量不可解析')
                return False
            exchange_side = exchange_position.get('side')
        else:
            self._quarantine_position_mismatch(
                symbol, '未决执行终态后持仓响应非对象')
            return False
        try:
            local_contracts = float(self.exchange_api._coin_to_contracts(
                ccxt_symbol, float(position['position_size'])))
        except Exception as exc:
            self._quarantine_position_mismatch(
                symbol, f'未决执行本地余仓换算失败: {exc}')
            return False
        if (not math.isfinite(exchange_contracts) or
                (exchange_contracts > tolerance and exchange_side != side) or
                abs(exchange_contracts - expected_remaining) > tolerance or
                local_contracts + tolerance < expected_remaining):
            self._quarantine_position_mismatch(
                symbol,
                '未决执行终态与 fresh 交易所/本地余仓无法完整归因',
                {'expected_contracts': expected_remaining,
                 'exchange_contracts': exchange_contracts,
                 'local_contracts': local_contracts,
                 'exchange_side': exchange_side})
            return False

        open_evidence = self._authoritative_single_order_evidence(order)
        if open_evidence is None:
            self._quarantine_position_mismatch(
                symbol, '原开仓终态缺少唯一订单 ID/'
                '权威成交均价，拒绝消费 lifecycle blocker')
            return False
        entry_price = open_evidence['price']
        authoritative_close = None
        compensation_order = (
            normalized_progress['order']
            if normalized_progress is not None else None)
        if isinstance(compensation_order, dict):
            filled_amount = normalized_progress['filled_amount']
            if filled_amount > 1e-15:
                close_evidence = self._authoritative_single_order_evidence(
                    compensation_order)
                if close_evidence is None:
                    self._quarantine_position_mismatch(
                        symbol, '补偿订单缺少唯一 ID/权威成交均价')
                    return False
                close_fee, _close_currency = self._extract_usdt_fee(
                    compensation_order)
                authoritative_close = {
                    'id': close_evidence['order_ids'][0],
                    'amount': filled_amount,
                    'price': close_evidence['price'], 'fee': close_fee,
                }
        entry_fee, entry_fee_currency = self._extract_usdt_fee(order)
        expected_remaining_amount = (
            normalized_progress['remaining_amount']
            if normalized_progress is not None else
            expected_open_amount)
        try:
            finalized = self.trade_state.finalize_unresolved_open_execution(
                symbol, open_client_id, entry_price,
                expected_remaining_amount, authoritative_close,
                entry_fee=entry_fee,
                entry_fee_currency=entry_fee_currency,
                entry_order_id=open_evidence['order_ids'][0])
        except Exception as exc:
            self._quarantine_position_mismatch(
                symbol, f'未决执行权威账本原子收口失败: {exc}')
            return False
        logger.warning(
            f'{symbol} 原开仓与补偿订单均已终态，fresh 余仓归因一致；'
            f'已原子{(finalized or {}).get("action")}并消费 '
            'unresolved_execution，隔离仍由止损/仓位中央门裁决')
        return bool(finalized)

    def _resolve_open_intent_only_if_still_flat(
            self, symbol, intent, reason):
        """在最终订单证据之后复读持仓，再消费确定性 open intent。"""
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        try:
            refreshed_position = self.exchange_api.get_position(ccxt_symbol)
        except Exception as exc:
            self._quarantine_position_mismatch(
                symbol, f'{reason}后持仓复查不确定: {exc}')
            return False
        try:
            position_present = self._exchange_position_has_contracts(
                refreshed_position)
        except Exception as exc:
            self._quarantine_position_mismatch(
                symbol, f'{reason}后持仓复查响应非法: {exc}')
            return False
        if position_present:
            logger.warning(
                f'{symbol} {reason}后持仓已出现，转入孤儿仓恢复而不消费 intent')
            return self._resume_open_intent_position(symbol, intent)
        self.trade_state.resolve_open_intent(
            symbol, intent.get('client_order_id'))
        self._clear_position_quarantine_after_reconcile(symbol)
        return True

    def _finalize_flat_filled_open_intent(
            self, symbol, intent, order, filled_contracts):
        payload = intent.get('payload') or {}
        side = intent.get('side')
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        contract_size = float(self.exchange_api._get_contract_size(ccxt_symbol))
        position_size = float(filled_contracts) * contract_size
        stop_price = float(payload['stop_loss_price'])
        # 先复查风险事实，再验证只在 flat 财务收口才需要的均价。
        # 若旧订单终态后仓位已重现，缺少 average 不得阻断孤儿仓
        # 恢复/保护；否则最长会留下一轮未托管的真实持仓。
        try:
            refreshed_position = self.exchange_api.get_position(ccxt_symbol)
        except Exception as exc:
            raise RuntimeError(
                f'旧开仓订单终态后无法复查当前持仓: {exc}') from exc
        if self._exchange_position_has_contracts(refreshed_position):
            logger.warning(
                f'{symbol} 旧开仓订单终态确认后持仓已出现，转入孤儿仓恢复')
            return self._resume_open_intent_position(symbol, intent)

        # find_existing_open_order 已在 OKX 公共边界净化 average/fee。
        # 恢复层只消费这份已验证 average；不能在其被剥离后回退到未经同一
        # 财务契约验证的 raw price（market order 的 price 也不是成交均价证据）。
        open_evidence = self._authoritative_single_order_evidence(order)
        if open_evidence is None:
            raise RuntimeError(
                '原开仓终态仍缺少唯一订单 ID/权威成交均价，'
                '不用信号/行情价伪造往返财务')
        entry_price = open_evidence['price']

        unresolved = (intent.get('unresolved_execution') or {})
        if unresolved.get('kind') == 'open_attribution':
            stop_order_id = unresolved.get('protective_stop_order_id')
            stop_order_size = unresolved.get('protective_stop_order_size')
            if not stop_order_id or stop_order_size is None:
                raise RuntimeError(
                    'open_attribution 未携带已知保护止损句柄，'
                    '可能混入人工仓，拒绝自动收口')
            size_tolerance = max(
                1e-12, math.ulp(max(abs(position_size), 1.0)) * 8)
            contract_tolerance = max(
                1e-12,
                math.ulp(max(abs(float(filled_contracts)), 1.0)) * 8)
            if (isinstance(stop_order_size, bool) or
                    abs(float(stop_order_size) - position_size) >
                    size_tolerance):
                raise RuntimeError('open_attribution 保护止损数量与开仓成交不一致')
            # 先裁决唯一确定性补偿订单。存在 live/部分成交订单时，
            # 不能把 flat 唯一归因给止损。
            open_client_id = intent.get('client_order_id')
            compensation_id = self.exchange_api.compensation_client_order_id(
                open_client_id)
            raw_progress = (
                self.exchange_api.find_compensation_close_progress(
                    ccxt_symbol, side, position_size, open_client_id))
            progress = self._normalize_compensation_close_progress(
                raw_progress,
                expected_client_order_id=compensation_id,
                expected_contracts=float(filled_contracts),
                expected_amount=position_size)
            if progress['presence'] == 'absent':
                conclusive, reason = self._pending_order_absence_is_conclusive(
                    unresolved)
                if not conclusive:
                    raise RuntimeError(
                        'open_attribution 补偿订单历史 absent '
                        f'已不可证明零成交: {reason}')
            if progress['filled_contracts'] > contract_tolerance:
                raise RuntimeError(
                    'open_attribution 单笔补偿订单非零成交/终态不可证明')
            if self.exchange_api.confirm_stop_execution(
                    ccxt_symbol, side, position_size, stop_price,
                    stop_order_id) is not True:
                raise RuntimeError('已知保护止损未能证明全量有效成交')
            final_position = self.exchange_api.get_position(ccxt_symbol)
            if self._exchange_position_has_contracts(final_position):
                raise RuntimeError('止损成交证据后 fresh 持仓已重新出现')
            # confirm_stop_execution 当前只返回严格布尔执行事实，
            # 不暴露 child 的权威 avgPx/fee/ordId。可以确认风险已归零，
            # 但不能用 trigger price/algoId 伪造真实往返财务。
            self._quarantine_position_mismatch(
                symbol,
                '已知保护止损已全量执行，但 child 成交财务'
                '证据未进入公共契约；保留 lifecycle blocker')
            return False

        try:
            close_evidence = self._recover_flat_compensation_evidence(
                ccxt_symbol, side, position_size,
                intent.get('client_order_id'))
        except Exception as exc:
            raise RuntimeError(
                f'确定性单笔补偿平仓订单查询失败: {exc}') from exc
        if not close_evidence:
            raise RuntimeError(
                '当前 flat 但缺少覆盖全部成交量的确定性补偿终态证据')

        # 单笔补偿订单的统一可见性宽限可能经历数秒；消费 intent 前再读一次，
        # 防止等待期间出现迟到/人工仓而被误记成完整往返。
        try:
            final_position = self.exchange_api.get_position(ccxt_symbol)
        except Exception as exc:
            raise RuntimeError(
                f'补偿订单终态找到后无法再次证明 flat: {exc}') from exc
        if self._exchange_position_has_contracts(final_position):
            logger.warning(
                f'{symbol} 补偿订单终态找到后持仓已重新出现，转入孤儿仓恢复')
            return self._resume_open_intent_position(symbol, intent)

        # 崩溃点可能位于“保护单 POST 成功”与 stop ID 落账之间。即使仓位
        # 与确定性单笔补偿订单都已闭环，也必须先原子持久化未知止损残留+隔离，
        # 交 guardian 用完整清单证明清净后再释放该品种。
        if not self._quarantine_position_mismatch(
                symbol,
                '旧 open intent 往返已确认，但成交后保护单是否遗留未知',
                {'open_client_order_id': intent.get('client_order_id'),
                 'compensation_order_ids': self._order_ids(close_evidence)},
                stop_residue_possible=True):
            raise RuntimeError('未知止损残留/隔离无法持久化，拒绝消费 open intent')

        close_authoritative = self._authoritative_single_order_evidence(
            close_evidence)
        if close_authoritative is None:
            raise RuntimeError(
                '确定性补偿单缺少唯一订单 ID/权威成交均价，'
                '不用 last/stop 估值伪造 recovered 往返')
        exit_price = close_authoritative['price']
        recovery_reason = '旧开仓意图与确定性补偿单权威证据均已找回'
        entry_fee, _currency = self._extract_usdt_fee(order)
        exit_fee, _exit_currency = self._extract_usdt_fee(close_evidence)
        self.trade_state.finalize_open_intent_round_trip(
            symbol, intent.get('client_order_id'), entry_price, exit_price,
            position_size, entry_order_ids=open_evidence['order_ids'],
            exit_order_ids=close_authoritative['order_ids'],
            entry_fee=entry_fee, exit_fee=exit_fee,
            reason=recovery_reason)
        self._clear_position_quarantine_after_reconcile(symbol)
        logger.warning(
            f'{symbol} 旧 open intent 与确定性单笔补偿订单已补记往返；'
            '未知止损残留标记继续隔离，待 guardian 完整清单复验')
        return True

    def _adjudicate_flat_open_intent(self, symbol, intent):
        side = intent.get('side')
        planned = intent.get('planned_position_size')
        if planned is None:
            # 旧两阶段实现可能崩在 prepare 与 set_amount 之间；POST 位于两次
            # 成功落盘之后，因此缺计划量可严格证明从未发单，不得用当前风险重算。
            resolved = self._resolve_open_intent_only_if_still_flat(
                symbol, intent, '确认无计划量 intent 属于发单前中间态')
            if resolved:
                logger.warning(
                    f'{symbol} 收口无计划量 open intent：最终仍为空仓，'
                    '已删除句柄并允许策略重新计算')
            return resolved
        try:
            planned_value = float(planned)
            if not math.isfinite(planned_value) or planned_value <= 0:
                raise ValueError(f'非法计划量 {planned!r}')
            order = self.exchange_api.find_existing_open_order(
                self.exchange_api.to_ccxt_symbol(symbol), side,
                planned_value, intent.get('client_order_id'),
                wait_for_visibility=True)
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
                return self._resolve_open_intent_only_if_still_flat(
                    symbol, intent, 'open intent 旧订单已终态且零成交')
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
        # 已经过适配层统一可见性宽限、当前仍 flat 且确定性 clOrdId 连续查无：
        # 只能证明旧请求未送达，不能证明旧信号此刻仍值得执行。直接收口句柄，
        # 等未来新 K 线/新信号；恢复流程绝不重放历史信号或重新 POST 开仓。
        resolved = self._resolve_open_intent_only_if_still_flat(
            symbol, intent, 'open intent 经可见性宽限确认从未发单')
        if resolved:
            logger.warning(
                f'{symbol} open intent 经可见性宽限且最终仍为空仓；已收口句柄，'
                '不在恢复流程重放旧信号')
        return resolved

    def _reconcile_all_open_intents(self, context):
        intents = self.trade_state.get_open_intents()
        unresolved = set()
        for symbol, intent in sorted(intents.items()):
            local_position = self.trade_state.get_open_position(symbol)
            if local_position:
                if not self._reconcile_position_open_intent(
                        symbol, intent, local_position):
                    unresolved.add(symbol)
                    self._protect_unresolved_lifecycle_position(
                        symbol, intent, local_position)
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

    def _ma_signal_with_catchup(self, symbol, df):
        """只检查最新已收盘 K 线是否刚发生 EMA 交叉。

        last_processed_candle 仅用于同一根 K 线的消费幂等。即使停机或
        行情故障造成历史间隔，也不回放旧交叉、不按当前 EMA 位置补仓；
        只比较最新两根已收盘 K 线，避免恢复后补做历史交易。
        """
        metadata = self.trade_state.get_signal_metadata(symbol)
        last_id = metadata.get('last_processed_candle')
        candle_ids = [
            (v.isoformat() if hasattr(v, 'isoformat') else str(v))
            for v in df['timestamp'].tolist()
        ]
        current_id = candle_ids[-1]
        current_state = self.ma_cross_strategy.check_current_state(df)
        if current_state is None:
            return None, current_id

        if last_id == current_id:
            # 已处理过这根 K 线：仍返回当前指标供 T+1/持仓检查，但不重放交叉。
            current_state['action'] = None
            return current_state, current_id

        rebaseline, _, _, gap = self._history_requires_rebaseline(symbol, df)
        latest_signal = self.ma_cross_strategy.check_signal(df)
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

        return current_state, current_id


    def _history_requires_rebaseline(self, symbol, df, max_gap_candles=3):
        """判断信号 marker 与当前可见历史是否存在不可安全回放的断层。"""
        metadata = self.trade_state.get_signal_metadata(symbol)
        last_id = metadata.get('last_processed_candle')
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
    def _daily_candle_is_fresh(df, scheduled_date):
        """验证最新日 K 足以代表本次调度日，拒绝陈旧行情进入真钱策略。

        北京时间 08:00 对应 UTC 日线刚收盘；本系统只交易 24x7 的 OKX
        U 本位加密永续，因此调度日 D 必须恰好拿到 D-1。D-2 陈旧数据与
        D 当日/未来错标数据都 fail-closed，避免吞掉真正的下一根交叉。
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
            expected_date = check_date - timedelta(days=1)
            return latest_date == expected_date, latest_date, expected_date
        except Exception as exc:
            logger.error(f'无法验证最新日 K 时间戳，按陈旧数据拒绝交易: {exc}')
            return False, None, None

    def _mark_daily_check_complete(self, check_date):
        """先持久化调度日，成功后再更新内存守卫。"""
        self.trade_state.set_last_daily_check_date(check_date)
        self._last_check_date = check_date

    def check_and_execute_trades(self, manual_run=False, scheduled_date=None):
        """检查并执行交易"""
        if self._maintenance_open_gate_status() is not None:
            logger.warning('部署维护哨兵生效：本次日检完全跳过且不标记完成')
            return
        # 三重防护：线程锁 + 日期检查 + APScheduler max_instances
        if not self._trade_lock.acquire(blocking=False):
            self._last_trade_check_failure = {
                'at': time.time(),
                'kind': 'trade_lock_busy',
                'manual_run': bool(manual_run),
            }
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

            unresolved_pending = self._reconcile_all_open_intents('日检')

            # 本轮监控集合 = 手动池启用品种 ∪ 有持仓品种 ∪ pending 品种。
            # 品种即使已从配置删除，未收口 clOrdId 也不得被遗忘/剪枝。
            # 快照视图（与盘中巡检同一模式）：循环中途 API 增删品种不影响本轮的
            # 一致性，也免去逐品种重扫池子
            all_open_positions = self.trade_state.get_all_open_positions()
            intents_after_reconcile = self.trade_state.get_open_intents()
            symbol_config_map = {s['name']: s for s in self.config['trading']['symbols']}
            symbols_to_check = {name for name, s in symbol_config_map.items() if s.get('enabled', True)}
            symbols_to_check.update(all_open_positions.keys())
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
                # 单品种异常只跳过该品种，不得中断其余品种的保护复核/平仓检查（真钱红线）
                try:
                    symbol_config = symbol_config_map.get(symbol)
                    if symbol_config is None:
                        # 品种已从手动池删除但仍有持仓：继续按唯一策略托管退出。
                        symbol_config = {
                            'name': symbol,
                            'enabled': True,
                            'risk_per_trade': self.config['strategy']['default_risk_per_trade'],
                            # 删除品种只托管当前仓位到下一次平仓；禁止反手或再开新腿。
                            '_retired_from_pool': True,
                        }
                    elif not symbol_config.get('enabled', True):
                        # 在池但已禁用且仍有持仓：与删除品种同规则——只托管现有仓位平仓
                        # 退出，禁止反手/再开新腿。复制一份再打标记，避免污染共享配置。
                        symbol_config = dict(symbol_config)
                        symbol_config['_retired_from_pool'] = True

                    logger.info(f"检查 {symbol} (策略: ma_cross)...")

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
                        elif close_recovery in (
                                'partial', 'zero_fill_resolved'):
                            # 恢复订单本轮已发生一次外部执行收口。即使
                            # partial/zero 已原子落账，也不得在同一调度
                            # invocation 再因反向信号建新 intent/POST。
                            failed_symbols.append(symbol)
                            continue
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
                                self._get_strategy_display_name()):
                            self._quarantine_position_mismatch(
                                symbol, '日检仓位一致但止损保护未能严格确认')
                            failed_symbols.append(symbol)
                            continue
                        self._clear_position_quarantine_after_reconcile(symbol)
                    elif local_position and not exchange_position:
                        exit_price = (
                            local_position.get('stop_loss_price') or
                            local_position.get('entry_price'))
                        closed, state_saved, _stop_cleared = self._handle_exchange_flat_close(
                            symbol, ccxt_symbol, local_position, exit_price,
                            'ma_cross 日检仓位对账')
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
                        logger.info(
                            f'{symbol} [双均线] 日检记平与 T+1 已同事务落盘')
                        self._clear_position_quarantine_after_reconcile(symbol)
                        local_position = None
                    elif not local_position and exchange_position:
                        self._quarantine_position_mismatch(
                            symbol, '交易所有仓但本地无记录（日检发现孤儿仓）')
                        failed_symbols.append(symbol)
                        continue
                    elif not local_position and not exchange_position:
                        self._clear_position_quarantine_after_reconcile(symbol)

                    required_closed_candles = cfgv.required_closed_candles(
                        self.config.get('strategy', {}))
                    fetch_limit = cfgv.ohlcv_fetch_limit(
                        self.config.get('strategy', {}))

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
                            f"{symbol} K线数据不足：双均线策略配置至少需要 "
                            f"{required_closed_candles} 根已收盘K线，本轮仅取得 {len(df)} 根"
                            f"（请求 {fetch_limit} 根），请检查周期配置或交易所历史K线供应")
                        if local_position:
                            # 有钱仓位不得以“新币历史不足”降级：退出信号检查未完成。
                            failed_symbols.append(symbol)
                        else:
                            # 双边已确认空仓且历史确实不足，属结构性 data-unready。
                            # 保持日报可见，但不要拖累全品种每 30 分钟重跑一整天。
                            data_unready_symbols.append(symbol)
                        continue

                    fresh, latest_candle_date, expected_candle_date = (
                        self._daily_candle_is_fresh(df, today))
                    if not fresh:
                        logger.critical(
                            f"{symbol} 最新已收盘日 K 陈旧：latest={latest_candle_date}，"
                            f"本次调度日={today}，必须等于={expected_candle_date}；"
                            "禁止本品种开仓、平仓及反手")
                        failed_symbols.append(symbol)
                        continue

                    signal, candle_id = self._ma_signal_with_catchup(symbol, df)

                    if not signal:
                        # 根数与新鲜度均已通过后仍无法计算，只可能是指标输入/结果
                        # 不可用。必须让当日保持未完成并进入日内重试；若静默跳过，
                        # 其余品种成功会把整日标记完成，永久吞掉本品种这根 K 线。
                        logger.error(
                            f"{symbol} 双均线无法从已收盘 K 线计算有效状态；"
                            "不推进 K 线标记，等待日内重试")
                        failed_symbols.append(symbol)
                        continue

                    current_close = float(df['close'].iloc[-1])
                    fmt_price = self._format_indicator_price
                    logger.info(
                        f"{symbol} [双均线指标] 收盘价={fmt_price(current_close)}, "
                        f"EMA短={fmt_price(signal.get('ema_short'))}, "
                        f"EMA长={fmt_price(signal.get('ema_long'))}, "
                        f"N日高={fmt_price(signal.get('upper_stop'))}, "
                        f"N日低={fmt_price(signal.get('lower_stop'))}, "
                        f"信号={signal.get('action', '无')}")

                    position = self.trade_state.get_open_position(symbol)

                    no_position_outcome = None
                    if position:
                        self.handle_open_position_ma_cross(symbol, signal, position, symbol_config)
                    elif symbol_config.get('_retired_from_pool'):
                        logger.info(
                            f"{symbol} 已退池且当前仓位已结束；禁止新开仓并完成生命周期清理")
                    else:
                        no_position_outcome = self.handle_no_position_ma_cross(
                            symbol, signal, symbol_config, df)

                    if no_position_outcome == NO_POSITION_T1_REENTRY_FAILED:
                        logger.error(
                            f'{symbol} [双均线] T+1 重入尚未形成真实持仓；'
                            '不推进 K 线或当日完成标记，等待日内调度重试')
                        failed_symbols.append(symbol)
                        continue

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
                        intentionally_blocked = (
                            no_position_outcome == NO_POSITION_T1_BLOCKED)
                        if (not retired_exit_complete and not intentionally_blocked and
                                (not post_position or
                                 post_position.get('side') != target_side)):
                            logger.error(
                                f'{symbol} [双均线] 目标仓位 {target_side} 尚未对齐；'
                                '不推进 K 线幂等标记，等待日内重试')
                            failed_symbols.append(symbol)
                            continue
                    self.trade_state.mark_candle_processed(symbol, candle_id)

                except Exception as sym_e:
                    logger.exception(f"{symbol} 本轮检查异常，跳过该品种继续: {sym_e}")
                    failed_symbols.append(symbol)

            try:
                removed_metadata = self.trade_state.prune_inactive_symbol_metadata(
                    symbol_config_map.keys())
                if removed_metadata:
                    logger.info(
                        f"已清理 {len(removed_metadata)} 个退池且无仓品种的信号元数据: "
                        f"{', '.join(removed_metadata)}")
            except Exception as e:
                logger.warning(f"清理退池品种信号元数据失败（不影响本轮交易）: {e}")

            # 信号检查完成后按汇总顺序推送，避免 08:00 单条消息过多触发限流
            self._flush_pending_trade_notifications()
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
                self._last_trade_check_failure = {
                    'at': time.time(),
                    'kind': 'symbol_failures',
                    'count': len(set(failed_symbols)),
                }
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
                # 若标记会让当天 08:00 的正式日检被跳过，整日的新信号与仓位生命周期检查丢失
                self._mark_daily_check_complete(today)
                self._last_trade_check_failure = None
                self._last_successful_trade_check_ts = time.time()
            else:
                self._last_trade_check_failure = None
                self._last_successful_trade_check_ts = time.time()
        except Exception as e:
            self._last_trade_check_failure = {
                'at': time.time(),
                'kind': 'check_exception',
                'exception_type': type(e).__name__,
            }
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
            self._trade_lock.release()
            logger.info("交易检查锁已释放")


    def _catchup_schedule_slot(self, now):
        """计算当前时刻应归属的最近一次日检调度日。

        常规配置在今日正点前不补昨日（保持原语义）；唯一例外是
        23:58/23:59 的 2 分钟正常调度窗口跨过零点，此时必须仍归属前一调度日。
        """
        return catchup_schedule_slot(self.config, now)

    def _daily_check_readiness(self, now=None):
        """正式调度窗口结束后，未完成对应调度日即为运行健康红灯。"""
        now = now or datetime.now()
        sched = self.config.get('scheduler', {})
        scheduled_today = now.replace(
            hour=sched.get('check_hour', 8),
            minute=sched.get('check_minute', 0),
            second=0, microsecond=0)
        slot = (
            scheduled_today if now >= scheduled_today
            else scheduled_today - timedelta(days=1))
        # 当前最近槽仍在 2 分钟宽限期时，健康度继续绑定再前一槽；
        # 宽限一结束即原子切换，跨午夜 23:59 配置也同样成立。
        if now < slot + timedelta(minutes=2):
            slot -= timedelta(days=1)
        expected = slot.date().isoformat()
        return self._last_check_date != expected, expected

    def _run_startup_catchup_check(self, now=None):
        """兜底补跑：已过今日检查时间而今日未执行过日检时，立即补跑一轮
        （启动时调用一次 + 每 30 分钟周期兜底，守卫幂等，已跑则空转）。

        场景：服务器恰在 08:00 前后宕机/重启，错过当天全部调度点——不补跑则当天的
        新信号与仓位生命周期检查整日缺席。信号基于已收盘日线，补跑与 08:00 正点执行等价；
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

    def start(self):
        """启动交易系统：注册定时任务、启动调度、阻塞主循环。"""
        logger.info("启动交易系统...")
        if self._stop_event.is_set():
            logger.warning("启动前已收到停止请求，runner 不再启动")
            return
        try:
            # 注册、启动与启动补跑也必须在 finally 保护内：任一阶段抛异常时都要
            # 关闭已部分启动的 scheduler 并清空心跳，不能留下 Web 正常但后台残活。
            self.register_jobs(self.config.get('scheduler', {}))
            self.scheduler.start()
            self._update_runner_heartbeat()
            logger.info(f"[{self.label}] 调度已启动，等待定时任务...")
            if not self._stop_event.is_set():
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
        try:
            with self._heartbeat_lock:
                heartbeat = self._runner_heartbeat_ts
        except Exception:
            heartbeat = None
        try:
            # 保留探针的原始类型给共享裁决器；``bool('false')``
            # 会把明确的契约漂移洗成健康。
            scheduler_running = getattr(self.scheduler, 'running', None)
        except Exception:
            scheduler_running = None
        try:
            scheduler_thread = getattr(self.scheduler, '_thread', None)
            scheduler_thread_alive = (
                False if scheduler_thread is None else
                scheduler_thread.is_alive())
        except Exception:
            scheduler_thread_alive = None
        try:
            persistence = self.trade_state.get_runtime_persistence_status()
        except Exception:
            persistence = None
        if isinstance(persistence, dict):
            persistence_degraded = persistence.get('degraded')
            persistence_context = persistence.get('context')
        else:
            persistence_degraded = None
            persistence_context = None
        try:
            safety_blockers = self.trade_state.get_safety_blocker_counts()
        except Exception:
            safety_blockers = None
        try:
            daily_check_overdue, expected_daily_check_date = (
                self._daily_check_readiness())
        except Exception:
            daily_check_overdue = None
            expected_daily_check_date = None
        try:
            stopping = self._stop_event.is_set()
        except Exception:
            stopping = None
        raw_snapshot = {
            'scheduler_running': scheduler_running,
            'scheduler_thread_alive': scheduler_thread_alive,
            'runner_heartbeat_ts': heartbeat,
            'persistence_degraded': persistence_degraded,
            'persistence_degraded_context': persistence_context,
            'safety_blockers': safety_blockers,
            'last_successful_trade_check_ts': getattr(
                self, '_last_successful_trade_check_ts', None),
            'last_successful_guardian_ts': getattr(
                self, '_last_successful_guardian_ts', None),
            'daily_check_overdue': daily_check_overdue,
            'expected_daily_check_date': expected_daily_check_date,
            'stopping': stopping,
        }
        # ``None`` 是合法的“已观测且无故障”；属性根本不存在则必须
        # 让 assessor 看到 missing，不能用 getattr(..., None) 伪造绿灯。
        for field in ('trade_check_failure', 'guardian_failure'):
            try:
                value = getattr(self, f'_last_{field}')
            except Exception:
                continue
            raw_snapshot[field] = value
        return assess_runtime_health(raw_snapshot, time.time())

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
            # 覆盖共享 scheduler 校验放行的 [60,1440] 全区间。
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
