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
        """
        if entry_price == stop_loss_price or entry_price == 0:
            return 0

        risk_pct = risk_per_trade if risk_per_trade is not None else self.risk_per_trade
        risk_amount = self.account_equity * risk_pct
        price_risk_pct = abs(entry_price - stop_loss_price) / entry_price

        if price_risk_pct == 0:
            return 0

        position_value = risk_amount / price_risk_pct
        position_size = position_value / entry_price

        # 向下取整（floor），不用 round：round 是四舍五入会进位，把偏大的币数喂给下游
        # 张数截断后可能比严格 floor 多一个张步 = 轻微超仓，与「以损定量、绝不超风险」相悖。
        # 最终下单张数仍由 round_quantity 按整张截断，这里先按 3 位小数向下取整保持粒度一致。
        return math.floor(position_size * 1000) / 1000
