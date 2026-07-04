"""配置校验的共享原语（零依赖，只用标准库 re）。

单一事实源：交易系统有三条配置入口——前端表单、HTTP API、手写 config.json，
它们必须用**同一套**规则把关（周期范围、风险度上限、交易对名格式、策略白名单、
严格整数）。此前这些常量与逻辑在 main.py 与 api_server.py 各存一份，靠人工同步——
本模块把原语收敛于一处，让三入口的一致性由构造保证，而非碰巧相等。

只放"叶子原语"（常量 + strict_int）；各入口的编排（启动校验全量 config vs
API 校验增量 delta）形态不同，仍各自保留，不强行统一成一个更难读的函数。
"""

import re

# 策略周期允许范围（整数，含端点）
PERIOD_MIN = 2
PERIOD_MAX = 500
# 单笔风险度上限 50%：防止把 1 当 1% 输这类数量级笔误直接放大到全仓
MAX_RISK_PER_TRADE = 0.5
# 内部交易对名：大写字母/数字，以 USDT 结尾（U 本位永续）
SYMBOL_RE = re.compile(r'^[A-Z0-9]{1,20}USDT$')
# 支持的策略
STRATEGY_WHITELIST = ('turtle', 'ma_cross')


def strict_int(value, field):
    """严格整数：接受 28 / 28.0 / "28"，拒绝 28.9 / "28.9"（不静默截断，fail-loud）。

    抛 ValueError（附字段名）供调用方转成清晰错误。周期是窗口长度，语义上必须是
    整数——静默把 28.9 截成 28 会让"我设的参数"与"实际生效"悄悄不一致。
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 不是有效数字: {value!r}")
    if f != int(f):
        raise ValueError(f"{field} 必须是整数，不接受小数: {value!r}")
    return int(f)
