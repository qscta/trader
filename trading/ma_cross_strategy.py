class MaCrossStrategy:
    def __init__(self, short_period=7, long_period=28, stop_loss_period=28):
        """
        初始化双均线交叉策略
        short_period: EMA短期周期（默认7）
        long_period: EMA长期周期（默认28）
        stop_loss_period: 止损用N天收盘价高低点周期（默认28）
        """
        self.short_period = short_period
        self.long_period = long_period
        self.stop_loss_period = stop_loss_period

    def calculate_ema(self, df):
        """计算EMA短期和长期均线"""
        df = df.copy()
        df['ema_short'] = df['close'].ewm(span=self.short_period, adjust=False).mean()
        df['ema_long'] = df['close'].ewm(span=self.long_period, adjust=False).mean()
        return df

    def calculate_stop_levels(self, df):
        """
        计算N天收盘价高低点（用于止损）
        做多止损 = 前N天收盘价最低值
        做空止损 = 前N天收盘价最高值
        """
        if len(df) < self.stop_loss_period + 1:
            return None, None

        # 前stop_loss_period根K线的收盘价（不含当前K线）
        close_prices = df.iloc[-(self.stop_loss_period + 1):-1]['close']

        upper_stop = close_prices.max()
        lower_stop = close_prices.min()

        return upper_stop, lower_stop

    def check_signal(self, df):
        """
        检查交易信号

        双均线交叉逻辑（永远在市）：
        - EMA短期上穿EMA长期 → 做多信号
        - EMA短期下穿EMA长期 → 做空信号
        - 反向交叉时，先平仓再反向开仓

        返回信号字典，action 可能的值：
        - 'long': 做多（含从空翻多）
        - 'short': 做空（含从多翻空）
        - 'close_long': 平多（EMA死叉）
        - 'close_short': 平空（EMA金叉）
        - None: 无信号
        """
        min_required = max(self.long_period * 2, self.stop_loss_period + 1)
        if len(df) < min_required:
            return None

        df = self.calculate_ema(df)

        # 获取最后两根K线的EMA值
        current_ema_short = df['ema_short'].iloc[-1]
        current_ema_long = df['ema_long'].iloc[-1]
        previous_ema_short = df['ema_short'].iloc[-2]
        previous_ema_long = df['ema_long'].iloc[-2]

        current_close = df['close'].iloc[-1]
        previous_close = df['close'].iloc[-2]

        # 计算止损高低点
        upper_stop, lower_stop = self.calculate_stop_levels(df)
        if upper_stop is None:
            return None

        signal = {
            'ema_short': current_ema_short,
            'ema_long': current_ema_long,
            'upper_stop': upper_stop,    # N天最高收盘价（空单止损）
            'lower_stop': lower_stop,    # N天最低收盘价（多单止损）
            'current_close': current_close,
            'previous_close': previous_close,
            'action': None
        }

        # 金叉：EMA短期上穿EMA长期
        golden_cross = (previous_ema_short <= previous_ema_long and
                       current_ema_short > current_ema_long)

        # 死叉：EMA短期下穿EMA长期
        death_cross = (previous_ema_short >= previous_ema_long and
                      current_ema_short < current_ema_long)

        if golden_cross:
            # 金叉：做多信号（如果有空仓会先平空再开多）
            signal['action'] = 'long'
        elif death_cross:
            # 死叉：做空信号（如果有多仓会先平多再开空）
            signal['action'] = 'short'

        # 当前EMA相对位置（用于止损后重入判断）
        signal['ema_bullish'] = current_ema_short > current_ema_long

        return signal

    def check_current_state(self, df):
        """
        回溯历史判断当前多空状态（用于即时开仓）

        逻辑：
        1. 计算当前EMA短期和长期的关系
        2. 如果EMA短期 > EMA长期（金叉状态）→ 应该做多
        3. 如果EMA短期 < EMA长期（死叉状态）→ 应该做空
        4. 止损价使用最新的N天收盘价高低点
        """
        min_required = max(self.long_period * 2, self.stop_loss_period + 1)
        if len(df) < min_required:
            return None

        df = self.calculate_ema(df)

        current_ema_short = df['ema_short'].iloc[-1]
        current_ema_long = df['ema_long'].iloc[-1]
        current_close = df['close'].iloc[-1]

        upper_stop, lower_stop = self.calculate_stop_levels(df)
        if upper_stop is None:
            return None

        # 判断当前状态
        if current_ema_short > current_ema_long:
            action = 'long'
        elif current_ema_short < current_ema_long:
            action = 'short'
        else:
            action = None

        return {
            'action': action,
            'ema_short': current_ema_short,
            'ema_long': current_ema_long,
            'upper_stop': upper_stop,
            'lower_stop': lower_stop,
            'current_close': current_close,
            'ema_bullish': current_ema_short > current_ema_long
        }

    def check_reentry_condition(self, df):
        """
        检查止损后重入方向（永远在市）：按当前 EMA 相对关系重入，
        方向可能与被止损的仓位相反（短>长重入做多、短<长重入做空）。
        仅当两条 EMA 精确相等（方向不明，实务上几乎不发生）时不重入。

        返回: (should_reenter, side, signal)
        """
        min_required = max(self.long_period * 2, self.stop_loss_period + 1)
        if len(df) < min_required:
            return False, None, None

        df = self.calculate_ema(df)

        current_ema_short = df['ema_short'].iloc[-1]
        current_ema_long = df['ema_long'].iloc[-1]
        current_close = df['close'].iloc[-1]

        upper_stop, lower_stop = self.calculate_stop_levels(df)
        if upper_stop is None:
            return False, None, None

        signal = {
            'ema_short': current_ema_short,
            'ema_long': current_ema_long,
            'upper_stop': upper_stop,
            'lower_stop': lower_stop,
            'current_close': current_close,
            'ema_bullish': current_ema_short > current_ema_long
        }

        if current_ema_short > current_ema_long:
            return True, 'long', signal
        elif current_ema_short < current_ema_long:
            return True, 'short', signal

        return False, None, signal
