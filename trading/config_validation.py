"""配置校验的共享原语（零依赖，只用标准库 re）。

单一事实源：交易系统有三条配置入口——前端表单、HTTP API、手写 config.json，
它们必须用**同一套**规则把关（周期范围、风险度上限、交易对名格式、
严格整数）。此前这些常量与逻辑在 main.py 与 api_server.py 各存一份，靠人工同步——
本模块把原语收敛于一处，让三入口的一致性由构造保证，而非碰巧相等。

除叶子原语外，策略块与交易对池的全量校验也在这里编排，供生产启动与部署迁移
直接复用；HTTP API 的增量输入形态不同，仍复用同一组叶子原语。
"""

import math
import re
from datetime import datetime

# 策略周期允许范围（整数，含端点）
PERIOD_MIN = 2
PERIOD_MAX = 500
# OKX /market/candles 单次可靠上限为 300；策略日检固定只取最新一页。
STRATEGY_OHLCV_FETCH_LIMIT = 300
# fetch_ohlcv 可能包含当前未收盘 K 线，过滤后要仍满足策略最低已收盘根数。
OPEN_CANDLE_FETCH_BUFFER = 1
# 单笔风险度上限 50%：防止把 1 当 1% 输这类数量级笔误直接放大到全仓
MAX_RISK_PER_TRADE = 0.5
# 内部交易对名：大写字母/数字，以 USDT 结尾（U 本位永续）
SYMBOL_RE = re.compile(r'^[A-Z0-9]{1,20}USDT$')
MA_PARAMETER_FIELDS = frozenset({
    'ma_short_period', 'ma_long_period',
    'ma_stop_period', 'default_risk_per_trade',
})
SYMBOL_CONFIG_FIELDS = frozenset({'name', 'enabled', 'risk_per_trade'})
TRADING_CONFIG_FIELDS = frozenset({'symbols'})
EXECUTION_CONFIG_FIELDS = frozenset({
    '_comment', 'okx', 'strategy', 'trading', 'scheduler', 'dingtalk',
    'equity_tick_retention_days',
})
DINGTALK_CONFIG_FIELDS = frozenset({'webhook_url'})
OKX_CONFIG_FIELDS = frozenset({
    'label', 'apiKey', 'secret', 'password',
    'margin_mode', 'leverage', 'leverage_overrides', 'sandbox', 'demo',
})
SINGLE_STRATEGY_MIGRATION_JOURNAL = '.single_strategy_migration_journal.json'
SCHEDULER_HOUR_FIELDS = (
    'check_hour', 'summary_hour', 'weekly_hour',
)
SCHEDULER_MINUTE_FIELDS = (
    'check_minute', 'summary_minute', 'weekly_minute',
)
SCHEDULER_CONFIG_FIELDS = frozenset(
    SCHEDULER_HOUR_FIELDS + SCHEDULER_MINUTE_FIELDS +
    ('stop_loss_scan_interval_minutes',))


def resolve_optional_alias(primary, legacy, field):
    """解析同一配置的两个环境变量别名；冲突时拒绝猜测且不泄露值。"""
    if primary is not None and legacy is not None and primary != legacy:
        raise ValueError(f'{field} 的主变量与兼容变量同时存在且值不一致')
    return primary if primary is not None else legacy


def _safe_repr(value):
    try:
        return repr(value)
    except Exception:
        return f'<{type(value).__name__}:unrepresentable>'


def _strategy_period(strategy_config, key, default):
    """读取策略周期：缺省键走默认值，显式值仍用 strict_int 保持三入口同口径。"""
    strategy_config = strategy_config or {}
    value = strategy_config[key] if key in strategy_config else default
    return strict_int(value, f'config.strategy.{key}')


def required_closed_candles(strategy_config=None):
    """返回双均线计算所需的最低“已收盘”K 线根数。"""
    long_period = _strategy_period(strategy_config, 'ma_long_period', 28)
    stop_period = _strategy_period(strategy_config, 'ma_stop_period', 28)
    return max(long_period * 2, stop_period + 1)


def ohlcv_fetch_limit(strategy_config=None):
    """日检固定请求 OKX 单页上限；超容量配置由入口拒绝。"""
    required = required_closed_candles(strategy_config)
    if required + OPEN_CANDLE_FETCH_BUFFER > STRATEGY_OHLCV_FETCH_LIMIT:
        raise ValueError(
            f'双均线最低需要 {required} 根已收盘 K 线，'
            f'加未收盘缓冲后超过单次 {STRATEGY_OHLCV_FETCH_LIMIT} 根上限')
    return STRATEGY_OHLCV_FETCH_LIMIT


def validate_ohlcv_capacity(strategy_config=None):
    """确保当前参数可由一次 300 根请求完整计算。"""
    ohlcv_fetch_limit(strategy_config)
    return True


def strict_float_finite(value, field):
    """有限浮点：拒绝 nan / inf / -inf（否则会污染求索指数除数等下游状态）。抛 ValueError。"""
    if isinstance(value, bool):
        raise ValueError(
            f"{field} 不是有效数字（不能是布尔值）: {_safe_repr(value)}")
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field} 不是有效数字: {_safe_repr(value)}")
    if not math.isfinite(f):
        raise ValueError(f"{field} 不是有限数字: {_safe_repr(value)}")
    return f


def strict_int(value, field):
    """严格整数：接受 28 / 28.0 / "28"，拒绝 28.9 / "28.9" / nan / inf（不静默截断，fail-loud）。

    抛 ValueError（附字段名）供调用方转成清晰错误。周期是窗口长度，语义上必须是
    整数——静默把 28.9 截成 28 会让"我设的参数"与"实际生效"悄悄不一致。
    inf/-inf/nan 经 strict_float_finite 先挡下：否则 "inf" 的 int() 抛 OverflowError（非
    ValueError），会绕过 API/启动的 (TypeError, ValueError) 捕获，畸形输入变成 500/崩溃。
    """
    if isinstance(value, bool):
        raise ValueError(
            f"{field} 必须是整数，不能是布尔值: {_safe_repr(value)}")
    f = strict_float_finite(value, field)
    if f != int(f):
        raise ValueError(
            f"{field} 必须是整数，不接受小数: {_safe_repr(value)}")
    return int(f)


def strict_risk_per_trade(value, field='风险度'):
    """规范化单笔风险度为 (0, MAX_RISK_PER_TRADE] 内的有限 float。抛 ValueError。"""
    r = strict_float_finite(value, field)
    if not (0 < r <= MAX_RISK_PER_TRADE):
        raise ValueError(f"{field}超出允许范围 (0, {MAX_RISK_PER_TRADE*100:.0f}%]: {r}")
    return r


def strict_bool(value, field='enabled'):
    """规范化布尔：真布尔原样返回；字符串仅接受 true/false（不区分大小写）。

    关键：Python `bool("false") == True`——手写/直调传字符串 "false" 若不显式解析，
    会被当成"启用"继续开仓。其余值一律拒绝（不猜测），fail-loud。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low == 'true':
            return True
        if low == 'false':
            return False
    raise ValueError(
        f"{field} 不是有效布尔值: {_safe_repr(value)}（请用 true/false）")


def strict_leverage(value, field='okx.leverage'):
    """规范化 OKX 杠杆倍数；拒绝 bool、非有限数及越界值。"""
    if isinstance(value, bool):
        raise ValueError(f'{field} 不能是 bool')
    parsed = strict_float_finite(value, field)
    if not (0 < parsed <= 125):
        raise ValueError(f'{field} 必须在 (0, 125] 内')
    return int(parsed) if parsed.is_integer() else parsed


def normalize_symbol_name(value, field='交易对名'):
    """规范化交易对名：必须是 ASCII 字母/数字，去空格后转大写。

    非字符串（None/int 等）显式拒绝——否则 .upper() 会抛 AttributeError 变成 500。
    必须在 ``upper()`` 之前拒绝非 ASCII；否则 Unicode 大小写折叠
    可把不同原始输入变成另一个真实交易对。
    """
    if not isinstance(value, str):
        raise ValueError(f"{field}必须是字符串: {_safe_repr(value)}")
    raw_name = value.strip()
    if (not raw_name or not raw_name.isascii() or
            re.fullmatch(r'[A-Za-z0-9]+', raw_name) is None):
        raise ValueError(
            f"{field}不合法: {_safe_repr(value)}"
            "（只允许 ASCII 字母/数字且以 USDT 结尾）")
    name = raw_name.upper()
    if not SYMBOL_RE.fullmatch(name):
        raise ValueError(
            f"{field}不合法: {_safe_repr(value)}"
            "（须为大写字母/数字且以 USDT 结尾，如 BTCUSDT）")
    return name


def validate_and_normalize_strategy_config(strategy):
    """生产启动与部署迁移共用的完整策略配置校验（就地规范化）。"""
    if not isinstance(strategy, dict):
        raise ValueError('config.strategy 必须是对象')
    unknown = sorted(set(strategy) - MA_PARAMETER_FIELDS)
    if unknown:
        raise ValueError(f'config.strategy 含未知字段: {unknown}')
    if strategy.get('default_risk_per_trade') is None:
        raise ValueError(
            "config.strategy 缺少必需参数 ['default_risk_per_trade']，"
            "请对照 config.example.json 补全后再启动")

    for key in ('ma_short_period', 'ma_long_period', 'ma_stop_period'):
        if key not in strategy:
            continue
        value = strict_int(strategy[key], f'config.strategy.{key}')
        if not (PERIOD_MIN <= value <= PERIOD_MAX):
            raise ValueError(
                f'config.strategy.{key} 超出允许范围 '
                f'[{PERIOD_MIN}, {PERIOD_MAX}]: {value}')
        strategy[key] = value

    strategy['default_risk_per_trade'] = strict_risk_per_trade(
        strategy['default_risk_per_trade'],
        'config.strategy.default_risk_per_trade')
    effective_short = strategy.get('ma_short_period', 7)
    effective_long = strategy.get('ma_long_period', 28)
    if effective_short >= effective_long:
        raise ValueError(
            f'config.strategy EMA 短期({effective_short})必须小于长期({effective_long})')
    validate_ohlcv_capacity(strategy)
    return strategy


def validate_and_normalize_symbol_configs(symbols):
    """生产启动与部署迁移共用的完整交易对池校验（就地规范化）。"""
    if not isinstance(symbols, list):
        raise ValueError('config.trading.symbols 必须是数组')
    seen = set()
    for index, symbol in enumerate(symbols):
        if not isinstance(symbol, dict):
            raise ValueError(
                f'config.trading.symbols[{index}] 不是对象: {_safe_repr(symbol)}')
        unknown = sorted(set(symbol) - SYMBOL_CONFIG_FIELDS)
        if unknown:
            raise ValueError(
                f'config.trading.symbols[{index}] 含未知字段: {unknown}；'
                '请先执行部署预检')
        name = normalize_symbol_name(
            symbol.get('name'),
            f'config.trading.symbols[{index}] 交易对名')
        if name in seen:
            raise ValueError(f'config.trading.symbols 存在重复交易对: {name}')
        seen.add(name)
        symbol['name'] = name
        if 'risk_per_trade' in symbol:
            symbol['risk_per_trade'] = strict_risk_per_trade(
                symbol['risk_per_trade'], f'{name} risk_per_trade')
        if 'enabled' in symbol:
            symbol['enabled'] = strict_bool(
                symbol['enabled'], f'{name} enabled')
    return symbols


def validate_and_normalize_scheduler_config(scheduler):
    """校验调度参数；键缺失才走运行时默认，显式 ``null`` 一律拒绝。"""
    if not isinstance(scheduler, dict):
        raise ValueError('config.scheduler 必须是对象')
    unknown = sorted(set(scheduler) - SCHEDULER_CONFIG_FIELDS)
    if unknown:
        raise ValueError(f'config.scheduler 含未知字段: {unknown}')
    for key in SCHEDULER_HOUR_FIELDS:
        if key not in scheduler:
            continue
        value = strict_int(scheduler[key], f'config.scheduler.{key}')
        if not (0 <= value <= 23):
            raise ValueError(
                f'config.scheduler.{key} 超出允许范围 [0, 23]: {value}')
        scheduler[key] = value
    for key in SCHEDULER_MINUTE_FIELDS:
        if key not in scheduler:
            continue
        value = strict_int(scheduler[key], f'config.scheduler.{key}')
        if not (0 <= value <= 59):
            raise ValueError(
                f'config.scheduler.{key} 超出允许范围 [0, 59]: {value}')
        scheduler[key] = value
    interval_key = 'stop_loss_scan_interval_minutes'
    if interval_key in scheduler:
        value = strict_int(
            scheduler[interval_key], f'config.scheduler.{interval_key}')
        if not (1 <= value <= 1440):
            raise ValueError(
                f'config.scheduler.{interval_key} 超出允许范围 [1, 1440]: {value}')
        scheduler[interval_key] = value
    return scheduler


def validate_and_normalize_okx_environment(okx):
    """规范化 OKX 账户域开关，拒绝 ``"false"`` 被 Python 当真的歧义。"""
    if not isinstance(okx, dict):
        raise ValueError('config.okx 必须是对象')
    values = {}
    for key in ('sandbox', 'demo'):
        if key not in okx:
            continue
        values[key] = strict_bool(okx[key], f'config.okx.{key}')
        okx[key] = values[key]
    if len(set(values.values())) > 1:
        raise ValueError(
            'config.okx.sandbox 与 config.okx.demo 配置矛盾，拒绝猜测账户域')
    return okx


def validate_and_normalize_okx_config(okx):
    """校验并规范化所有会影响 OKX 下单语义的非凭据配置。"""
    if not isinstance(okx, dict):
        raise ValueError('config.okx 必须是对象')
    unknown = sorted(set(okx) - OKX_CONFIG_FIELDS)
    if unknown:
        raise ValueError(f'config.okx 含未知字段: {unknown}')
    for key in ('apiKey', 'secret', 'password', 'label'):
        if key in okx and not isinstance(okx[key], str):
            raise ValueError(f'config.okx.{key} 必须是字符串')
    for key in ('apiKey', 'secret', 'password'):
        if key in okx and okx[key] and not okx[key].strip():
            raise ValueError(f'config.okx.{key} 不能只包含空白')
    validate_and_normalize_okx_environment(okx)
    if 'margin_mode' in okx:
        value = okx['margin_mode']
        if not isinstance(value, str) or value.strip().lower() not in {
                'cross', 'isolated'}:
            raise ValueError(
                f'config.okx.margin_mode 非法: {_safe_repr(value)}'
                '（只支持 cross / isolated）')
        okx['margin_mode'] = value.strip().lower()
    if 'leverage' in okx:
        okx['leverage'] = strict_leverage(
            okx['leverage'], 'config.okx.leverage')
    if 'leverage_overrides' in okx:
        overrides = okx['leverage_overrides']
        if not isinstance(overrides, dict):
            raise ValueError('config.okx.leverage_overrides 必须是对象')
        normalized = {}
        for raw_symbol, raw_value in overrides.items():
            symbol = normalize_symbol_name(
                raw_symbol, 'config.okx.leverage_overrides 交易对名')
            if symbol in normalized:
                raise ValueError(
                    f'config.okx.leverage_overrides 存在重复交易对: {symbol}')
            normalized[symbol] = strict_leverage(
                raw_value, f'config.okx.leverage_overrides.{symbol}')
        okx['leverage_overrides'] = normalized
    return okx


def validate_and_normalize_dingtalk_config(dingtalk):
    """校验通知配置；显式 ``null`` 不能延迟成启动期 AttributeError。"""
    if not isinstance(dingtalk, dict):
        raise ValueError('config.dingtalk 必须是对象')
    unknown = sorted(set(dingtalk) - DINGTALK_CONFIG_FIELDS)
    if unknown:
        raise ValueError(f'config.dingtalk 含未知字段: {unknown}')
    if 'webhook_url' in dingtalk:
        value = dingtalk['webhook_url']
        if not isinstance(value, str):
            raise ValueError('config.dingtalk.webhook_url 必须是字符串')
        dingtalk['webhook_url'] = value.strip()
    return dingtalk


def validate_okx_legacy_migration_marker(payload):
    """验证旧 runtime 曾写出的完成标记，拒绝任意扩展字段冒充安全收口。"""
    if not isinstance(payload, dict):
        raise ValueError('旧状态迁移标记必须是对象')
    expected = {'exchange', 'completed_at', 'moved'}
    if set(payload) != expected:
        raise ValueError(
            '旧状态迁移标记字段必须精确为 '
            f'{sorted(expected)}，实际为 '
            f'{sorted(repr(key) for key in payload)}')
    if payload.get('exchange') != 'okx':
        raise ValueError('旧状态迁移标记 exchange 必须是 okx')
    value = payload['completed_at']
    if not isinstance(value, str) or not value:
        raise ValueError('旧状态迁移标记 completed_at 非法')
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('旧状态迁移标记 completed_at 非法') from exc
    moved = payload['moved']
    if (not isinstance(moved, list) or
            any(not isinstance(item, str) or not item for item in moved)):
        raise ValueError('旧状态迁移标记 moved 必须是字符串数组')
    return payload


def canonicalize_single_okx_config(config):
    """把唯一受支持的旧 ``exchanges.okx`` 布局收敛成顶层 ``okx``。

    同时存在新旧两份、旧容器含其它交易所，或策略/品种块重复时都拒绝猜测。
    返回 True 表示本次移除了旧布局。
    """
    if not isinstance(config, dict):
        raise ValueError('config 顶层必须是对象')
    if 'exchanges' not in config:
        return False
    exchanges = config['exchanges']
    if not isinstance(exchanges, dict):
        raise ValueError('config.exchanges 必须是对象')
    unknown = sorted(set(exchanges) - {'okx'})
    if unknown:
        raise ValueError(
            f'单交易所配置不接受 config.exchanges 中的字段: {unknown}')
    if 'okx' not in exchanges:
        raise ValueError('旧 config.exchanges 缺少唯一允许的 okx 配置块')
    if 'okx' in config:
        raise ValueError('config 同时包含 okx 与 exchanges.okx，拒绝双源配置')
    legacy = exchanges['okx']
    if not isinstance(legacy, dict):
        raise ValueError('config.exchanges.okx 必须是对象')
    canonical_okx = dict(legacy)
    for key in ('strategy', 'trading'):
        if key not in canonical_okx:
            continue
        if key in config:
            raise ValueError(
                f'config 同时包含顶层 {key} 与 exchanges.okx.{key}，拒绝双源配置')
        config[key] = canonical_okx.pop(key)
    config['okx'] = canonical_okx
    del config['exchanges']
    return True


def validate_and_normalize_execution_config(config):
    """校验唯一生产执行布局；旧布局须先由离线迁移显式收敛。"""
    if not isinstance(config, dict):
        raise ValueError('config 顶层必须是对象')
    unknown = sorted(set(config) - EXECUTION_CONFIG_FIELDS)
    if unknown:
        raise ValueError(f'config 顶层含未知字段: {unknown}')
    if '_comment' in config and not isinstance(config['_comment'], str):
        raise ValueError('config._comment 必须是字符串')
    validate_and_normalize_strategy_config(config.get('strategy'))
    trading = config.get('trading')
    if not isinstance(trading, dict):
        raise ValueError('config.trading 必须是对象')
    unknown = sorted(set(trading) - TRADING_CONFIG_FIELDS)
    if unknown:
        raise ValueError(f'config.trading 含未知字段: {unknown}')
    validate_and_normalize_symbol_configs(trading.get('symbols'))
    validate_and_normalize_scheduler_config(config.get('scheduler', {}))
    if 'okx' not in config:
        raise ValueError('config 缺少必需的 okx 配置块')
    validate_and_normalize_okx_config(config['okx'])
    validate_and_normalize_dingtalk_config(config.get('dingtalk', {}))
    if 'equity_tick_retention_days' in config:
        value = strict_int(
            config['equity_tick_retention_days'],
            'config.equity_tick_retention_days')
        if not (7 <= value <= 3650):
            raise ValueError(
                'config.equity_tick_retention_days 超出允许范围 '
                f'[7, 3650]: {value}')
        config['equity_tick_retention_days'] = value
    return config
