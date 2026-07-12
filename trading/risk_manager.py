import math


class RiskManager:
    def __init__(self, account_equity, risk_per_trade=0.01):
        """
        初始化风险管理器
        account_equity: 账户权益
        risk_per_trade: 每笔交易风险比例（默认1%）
        """
        self.account_equity = account_equity
        self.risk_per_trade = risk_per_trade

    def calculate_position_size(self, entry_price, stop_loss_price, risk_per_trade=None):
        """
        计算头寸大小（以损定量 - 百分比风险模型）

        公式：
        做多: 仓位价值 = (账户权益 * 风险比例) / [(入场价 - 止损价) / 入场价]
        做空: 仓位价值 = (账户权益 * 风险比例) / [(止损价 - 入场价) / 入场价]
        仓位数量 = 仓位价值 / 入场价

        自身输入守卫（防御纵深）：本方法不依赖调用方先行校验——权益/价格/风险度
        任一为非有限、非正或越界时一律返回 0（=不开仓），绝不产出负仓位、NaN 或
        数量级失控的仓位。生产链路上游已校验权益>0、价格有限>0、风险度∈(0,50%]，
        这里是那道防线万一被绕过时的最后兜底。
        """
        risk_pct = risk_per_trade if risk_per_trade is not None else self.risk_per_trade
        try:
            equity = float(self.account_equity)
            entry_price = float(entry_price)
            stop_loss_price = float(stop_loss_price)
            risk_pct = float(risk_pct)
        except (TypeError, ValueError):
            return 0

        if not all(math.isfinite(v) for v in
                   (equity, entry_price, stop_loss_price, risk_pct)):
            return 0
        # 权益/入场价须为正；风险度须在 (0, 1] 内（更严的 50% 上限由上游 config 校验把关）；
        # 止损价须为正且不等于入场价（否则风险距离为 0）。
        if equity <= 0 or entry_price <= 0 or stop_loss_price <= 0:
            return 0
        if not (0 < risk_pct <= 1):
            return 0
        if entry_price == stop_loss_price:
            return 0

        risk_amount = equity * risk_pct
        price_risk_pct = abs(entry_price - stop_loss_price) / entry_price
        if price_risk_pct <= 0:
            return 0

        position_size = (risk_amount / price_risk_pct) / entry_price
        if not math.isfinite(position_size) or position_size <= 0:
            return 0
        return position_size
