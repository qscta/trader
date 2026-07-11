class TurtleStrategy:
    def __init__(self, channel_period=28):
        """初始化海龟通道策略"""
        self.channel_period = channel_period

    def _build_channel_lines(self, closes):
        """为每根K线构造对应的上下轨和中轨（均不含当前K线本身）"""
        upper_lines = []
        lower_lines = []
        mid_lines = []
        for i in range(len(closes)):
            if i < self.channel_period:
                upper_lines.append(None)
                lower_lines.append(None)
                mid_lines.append(None)
            else:
                window = closes[i-self.channel_period:i]
                upper_line = max(window)
                lower_line = min(window)
                upper_lines.append(upper_line)
                lower_lines.append(lower_line)
                mid_lines.append((upper_line + lower_line) / 2)
        return upper_lines, lower_lines, mid_lines

    def _detect_mid_cross(self, previous_close, previous_mid, current_close, current_mid):
        """
        严格按“各自当日收盘价相对各自当日中轨”的方式判定中轨穿越。
        只有前一日收盘明确在中轨一侧、当日收盘明确到达另一侧，才算有效穿越。
        """
        if previous_mid is None or current_mid is None:
            return False, None
        if previous_close < previous_mid and current_close > current_mid:
            return True, 'up'
        if previous_close > previous_mid and current_close < current_mid:
            return True, 'down'
        return False, None

    def _find_latest_mid_cross_index(self, closes, mid_lines, end_index=None):
        """找到截至某根K线为止最近一次有效中轨穿越的位置。"""
        if end_index is None:
            end_index = len(closes) - 1
        end_index = min(end_index, len(closes) - 1)

        for i in range(end_index, self.channel_period, -1):
            prev_close = closes[i - 1]
            curr_close = closes[i]
            crossed, _ = self._detect_mid_cross(
                prev_close,
                mid_lines[i - 1],
                curr_close,
                mid_lines[i],
            )
            if crossed:
                return i
        return None

    def _is_bootstrap_direct_entry_armed(self, closes, upper_lines, lower_lines, mid_lines, end_index):
        """
        新币启动期直通规则：
        - 之前长期因K线不足，无法形成海龟通道
        - 一旦进入“可计算上下轨”的阶段，在首次有效突破出现之前，不再强制要求先刷新中轨
        - 如果期间已经出现过中轨穿越，后续就回归常规海龟资格链
        - 如果期间已经出现过一次上下轨突破，启动期直通资格即视为已消耗
        """
        if end_index <= self.channel_period:
            return True

        if self._find_latest_mid_cross_index(closes, mid_lines, end_index=end_index) is not None:
            return False

        for i in range(self.channel_period + 1, end_index + 1):
            if upper_lines[i] is None or lower_lines[i] is None:
                continue
            prev_close = closes[i - 1]
            curr_close = closes[i]
            if prev_close <= upper_lines[i] and curr_close > upper_lines[i]:
                return False
            if prev_close >= lower_lines[i] and curr_close < lower_lines[i]:
                return False

        return True

    def check_signal(self, df, mid_line_crossed=True):
        """
        检查交易信号
        mid_line_crossed 参数表示当前是否允许开仓
        """
        # 严格判定中轨穿越需要同时拿到“前一日中轨”和“当日中轨”
        if len(df) < self.channel_period + 2:
            return None

        closes = df['close'].values
        upper_lines, lower_lines, mid_lines = self._build_channel_lines(closes)

        current_close = closes[-1]
        previous_close = closes[-2]
        upper_line = upper_lines[-1]
        lower_line = lower_lines[-1]
        mid_line = mid_lines[-1]
        previous_mid_line = mid_lines[-2]

        if upper_line is None or previous_mid_line is None or mid_line is None:
            return None

        signal = {
            'upper_line': upper_line,
            'lower_line': lower_line,
            'mid_line': mid_line,
            'previous_mid_line': previous_mid_line,
            'current_close': current_close,
            'previous_close': previous_close,
            'action': None
        }

        crossed_mid, cross_direction = self._detect_mid_cross(
            previous_close, previous_mid_line, current_close, mid_line
        )
        if crossed_mid:
            signal['mid_line_crossed'] = True
            signal['cross_direction'] = cross_direction

        # 新币启动期：如果截至上一根K线仍从未形成过有效中轨穿越，
        # 则允许第一批可计算的上下轨突破直接开仓。
        bootstrap_direct_open = (
            not mid_line_crossed
            and not crossed_mid
            and self._is_bootstrap_direct_entry_armed(
                closes,
                upper_lines,
                lower_lines,
                mid_lines,
                end_index=len(closes) - 2,
            )
        )

        # 判断是否允许检查开仓信号
        can_open = mid_line_crossed or crossed_mid or bootstrap_direct_open

        # 检查开仓信号（穿越中轨后首次突破）
        if can_open:
            if previous_close <= upper_line and current_close > upper_line:
                if bootstrap_direct_open:
                    signal['bootstrap_direct_entry'] = True
                signal['action'] = 'long'
                return signal
            if previous_close >= lower_line and current_close < lower_line:
                if bootstrap_direct_open:
                    signal['bootstrap_direct_entry'] = True
                signal['action'] = 'short'
                return signal

        # 如果没有开仓信号但穿越了中轨，返回平仓信号
        if crossed_mid:
            if signal.get('cross_direction') == 'up':
                signal['action'] = 'close_short'
            else:
                signal['action'] = 'close_long'
            return signal

        return signal

    def check_current_state(self, df):
        """回溯历史判断当前多空状态"""
        if len(df) < self.channel_period + 2:
            return None

        closes = df['close'].values
        upper_lines, lower_lines, mid_lines = self._build_channel_lines(closes)

        cross_mid_idx = self._find_latest_mid_cross_index(closes, mid_lines)

        if cross_mid_idx is None:
            return {'action': None, 'reason': '未找到中轨穿越',
                    'upper_line': upper_lines[-1], 'lower_line': lower_lines[-1], 'mid_line': mid_lines[-1]}

        signal_side = None
        for i in range(cross_mid_idx + 1, len(closes)):
            if upper_lines[i] is None:
                continue
            prev_close = closes[i-1]
            curr_close = closes[i]
            if prev_close <= upper_lines[i] and curr_close > upper_lines[i]:
                signal_side = 'long'
                break
            elif prev_close >= lower_lines[i] and curr_close < lower_lines[i]:
                signal_side = 'short'
                break

        return {
            'action': signal_side,
            'upper_line': upper_lines[-1],
            'lower_line': lower_lines[-1],
            'mid_line': mid_lines[-1]
        }

    def is_first_breakout_armed(self, df, include_latest_bar=True):
        """
        判断是否处于“中轨穿越后的首次突破待触发”状态。
        规则：
        - 找到最近一次中轨穿越
        - 若该穿越后至今从未发生上下轨突破，则返回 True（下一次突破有效）
        - 否则返回 False（已消耗，需等待下一次中轨穿越）

        include_latest_bar=False 时，只检查截至上一根已收盘K线是否仍处于待触发状态。
        这个模式用于主交易流程在“生成今天信号之前”回填历史状态，避免把今天
        首次发生的突破提前当作“资格已消耗”，从而吞掉本该开的单。
        """
        if len(df) < self.channel_period + 2:
            return False

        closes = df['close'].values
        upper_lines, lower_lines, mid_lines = self._build_channel_lines(closes)

        cross_mid_idx = self._find_latest_mid_cross_index(closes, mid_lines)

        if cross_mid_idx is None:
            return False

        latest_breakout_idx = len(closes) if include_latest_bar else len(closes) - 1

        # 最近一次中轨穿越后，只要发生过任意一次上下轨突破，就说明首次突破已被消耗
        for i in range(cross_mid_idx + 1, latest_breakout_idx):
            if upper_lines[i] is None or lower_lines[i] is None:
                continue
            prev_close = closes[i-1]
            curr_close = closes[i]
            if prev_close <= upper_lines[i] and curr_close > upper_lines[i]:
                return False
            if prev_close >= lower_lines[i] and curr_close < lower_lines[i]:
                return False

        return True
