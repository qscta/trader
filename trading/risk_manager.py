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

        return round(position_size, 3)

    def calculate_stop_loss(self, entry_price, lower_channel, upper_channel, side):
        """
        计算止损价格
        做多：止损价 = 下轨
        做空：止损价 = 上轨
        """
        if side == 'long':
            return lower_channel
        elif side == 'short':
            return upper_channel
        else:
            return None

    def validate_trade(self, account_equity, entry_price, stop_loss_price, min_position_size=0.001):
        """验证交易是否有效"""
        if entry_price == stop_loss_price:
            return False, "入场价和止损价相同"

        if account_equity <= 0:
            return False, "账户权益不足"

        position_size = self.calculate_position_size(entry_price, stop_loss_price)

        if position_size < min_position_size:
            return False, f"头寸大小过小: {position_size}"

        return True, "验证通过"
