import time
import math
from datetime import datetime, timezone


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

    def validate_live_history(self, df, now_epoch=None):
        """真钱计算前验证所需日线连续且最新；返回 ``(ok, reason)``。

        CCXT 日线可能按 UTC 或 UTC+8 锚定，因此不写死收盘时钟；用数据自身
        的 24 小时锚点推导“此刻最新一根应已收盘的 K 线”。陈旧、缺口、重复、
        倒序或坏时间戳一律拒绝进入策略，避免拿旧行情生成新订单。
        """
        required = max(self.long_period * 2, self.stop_loss_period + 1)
        try:
            values = list(df['timestamp'].iloc[-required:])
            if len(values) < required:
                return False, f'日线数量不足: {len(values)} < {required}'

            def epoch_ms(value):
                if hasattr(value, 'to_pydatetime'):
                    value = value.to_pydatetime()
                if not isinstance(value, datetime):
                    value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return int(value.timestamp() * 1000)

            stamps = [epoch_ms(v) for v in values]
            closes = [float(v) for v in df['close'].iloc[-required:]]
        except Exception as e:
            return False, f'日线时间戳或收盘价不可解析: {e}'

        if any(not math.isfinite(value) or value <= 0 for value in closes):
            return False, '日线收盘价包含非正数、NaN 或无穷值'

        day_ms = 86_400_000
        if any(b - a != day_ms for a, b in zip(stamps, stamps[1:])):
            return False, '日线存在缺口、重复或倒序'

        cutoff_ms = int((time.time() if now_epoch is None else now_epoch) * 1000) - 2_000
        anchor_ms = stamps[-1] % day_ms
        current_open_ms = ((cutoff_ms - anchor_ms) // day_ms) * day_ms + anchor_ms
        expected_last_closed_open = current_open_ms - day_ms
        if stamps[-1] != expected_last_closed_open:
            return False, '最新已收盘日线陈旧或时间锚点异常'
        return True, None

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
        检查止损后重入条件
        重入前提：EMA短期仍高于EMA长期（多头环境）

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
