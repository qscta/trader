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
SINGLE_STRATEGY_MIGRATION_JOURNAL = '.single_strategy_migration_journal.json'


def _strategy_period(strategy_config, key, default):
    """读取策略周期：缺省键走默认值，显式值仍用 strict_int 保持三入口同口径。"""
    strategy_config = strategy_config or {}
    value = strategy_config.get(key)
    if value is None:
        value = default
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
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 不是有效数字: {value!r}")
    if not math.isfinite(f):
        raise ValueError(f"{field} 不是有限数字: {value!r}")
    return f


def strict_int(value, field):
    """严格整数：接受 28 / 28.0 / "28"，拒绝 28.9 / "28.9" / nan / inf（不静默截断，fail-loud）。

    抛 ValueError（附字段名）供调用方转成清晰错误。周期是窗口长度，语义上必须是
    整数——静默把 28.9 截成 28 会让"我设的参数"与"实际生效"悄悄不一致。
    inf/-inf/nan 经 strict_float_finite 先挡下：否则 "inf" 的 int() 抛 OverflowError（非
    ValueError），会绕过 API/启动的 (TypeError, ValueError) 捕获，畸形输入变成 500/崩溃。
    """
    f = strict_float_finite(value, field)
    if f != int(f):
        raise ValueError(f"{field} 必须是整数，不接受小数: {value!r}")
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
    raise ValueError(f"{field} 不是有效布尔值: {value!r}（请用 true/false）")


def normalize_symbol_name(value, field='交易对名'):
    """规范化交易对名：必须是字符串，去空格后转大写，须匹配 SYMBOL_RE。抛 ValueError。

    非字符串（None/int 等）显式拒绝——否则 .upper() 会抛 AttributeError 变成 500。
    """
    if not isinstance(value, str):
        raise ValueError(f"{field}必须是字符串: {value!r}")
    name = value.strip().upper()
    if not SYMBOL_RE.match(name):
        raise ValueError(f"{field}不合法: {value!r}（须为大写字母/数字且以 USDT 结尾，如 BTCUSDT）")
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
        if strategy.get(key) is None:
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
                f'config.trading.symbols[{index}] 不是对象: {symbol!r}')
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
        if symbol.get('risk_per_trade') is not None:
            symbol['risk_per_trade'] = strict_risk_per_trade(
                symbol['risk_per_trade'], f'{name} risk_per_trade')
        if symbol.get('enabled') is not None:
            symbol['enabled'] = strict_bool(
                symbol['enabled'], f'{name} enabled')
    return symbols
