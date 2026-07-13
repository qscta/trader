"""策略信号分派子系统（TradingSystem 的 mixin）。

海龟通道与双均线 EMA 两策略的信号处理：无仓时的开仓信号判定、有仓时的
止损确认/平仓/翻转分派、海龟止损推进检查、双均线 T+1 止损重入。

以 mixin 形式承载：方法仍绑定在 TradingSystem 实例上——self 语义、
测试对实例方法的桩打法、调用链与日志行为全部不变，只做物理分层。
宿主须提供：exchange_api / trade_state / notifier / ma_cross_strategy /
stop_loss_dates / is_stop_loss_today / record_stop_loss / clear_stop_loss /
_handle_exchange_flat_close / _get_strategy_display_name /
_notify_missing_position_after_signal / _execute_open / _update_stop_order /
handle_close_signal / _flip_position。
"""

import logging
from datetime import date

logger = logging.getLogger(__name__)


class SignalHandlersMixin:

    def _mark_ma_cross_reentry_pending(self, symbol, side, signal, reason):
        """双均线开仓腿失败后统一告警；K 线不推进，由日内调度幂等重试。

        T+1 只属于真实止损事件。把网络/下单失败伪装成“今天已止损”会阻断
        08:01/30 分钟重试，并在主调度错误推进 candle 后永久吞掉交叉。
        """
        self._notify_missing_position_after_signal(symbol, 'ma_cross', side, signal, reason)

    def handle_no_position_turtle(self, symbol, signal, symbol_config, df):
        """海龟策略：处理没有开仓头寸的情况（多空对称，只差方向字面量）"""
        logger.info(f"{symbol} [海龟] 没有开仓头寸，检查是否有开仓信号...")

        side = signal['action']
        if side not in ('long', 'short'):
            return

        mid_line_crossed = self.trade_state.get_signal_state(symbol)
        bootstrap_direct_entry = bool(signal.get('bootstrap_direct_entry'))
        if not (mid_line_crossed or bootstrap_direct_entry):
            logger.info(f"{symbol} [海龟] 检测到非标准信号 {side}，忽略")
            return

        label = '新币启动期直通' if bootstrap_direct_entry else '标准'
        logger.info(f"{symbol} [海龟] 检测到{label}信号 {side.upper()}，执行开仓")
        self.handle_open_signal_turtle(symbol, side, signal, symbol_config)
        if not self.trade_state.get_open_position(symbol):
            self._notify_missing_position_after_signal(
                symbol,
                'turtle',
                side,
                signal,
                f'{label} {side.upper()} 信号已出现，但本轮检查结束后仍无持仓，请复核交易所与日志'
            )

    def handle_open_position_turtle(self, symbol, signal, position, symbol_config):
        """海龟策略：处理已开仓头寸"""
        logger.info(f"{symbol} [海龟] 有开仓头寸，检查是否需要平仓或更新止损...")

        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        exchange_position = self.exchange_api.get_position(ccxt_symbol)
        if exchange_position is None or exchange_position.get('contracts', 0) == 0:
            if self.trade_state.get_open_position(symbol):
                logger.warning(f"{symbol} [海龟] 检测到交易所端已无持仓，止损单可能已触发")
                exit_price = position.get('stop_loss_price', signal['current_close'])
                closed_position, state_saved, stop_cleared = self._handle_exchange_flat_close(
                    symbol, ccxt_symbol, position, exit_price, "海龟止损确认",
                    strategy_type='turtle')
                if not closed_position:
                    return
                if not state_saved:
                    logger.warning(f"{symbol} [海龟] 止损确认已执行，但本地状态落盘失败，本轮不再反手开仓")
                    return
                # 止损平仓后判断 mid_line_crossed 状态
                if signal.get('mid_line_crossed'):
                    # 当前K线穿越了中轨（同时可能突破轨道）
                    self.trade_state.set_signal_state(symbol, True)
                    logger.info(f"{symbol} [海龟] 止损平仓，当前K线已穿越中轨，允许开仓")
                else:
                    # 检查价格是否仍在原开仓方向那一侧
                    current_close = signal.get('current_close', 0)
                    mid_line = signal.get('mid_line', 0)
                    side = position.get('side', '')
                    if side == 'short' and current_close < mid_line:
                        # 开空被止损，价格回落到中轨下方，保持允许开仓
                        self.trade_state.set_signal_state(symbol, True)
                        logger.info(
                            f"{symbol} [海龟] 空单止损，价格仍在中轨下方"
                            f"({self._format_indicator_price(current_close)}<"
                            f"{self._format_indicator_price(mid_line)})，保持允许开仓")
                    elif side == 'long' and current_close > mid_line:
                        # 开多被止损，价格回落到中轨上方，保持允许开仓
                        self.trade_state.set_signal_state(symbol, True)
                        logger.info(
                            f"{symbol} [海龟] 多单止损，价格仍在中轨上方"
                            f"({self._format_indicator_price(current_close)}>"
                            f"{self._format_indicator_price(mid_line)})，保持允许开仓")
                    else:
                        self.trade_state.set_signal_state(symbol, False)
                        logger.info(f"{symbol} [海龟] 止损平仓，价格已穿越到另一侧，等待重新穿越中轨")
                # 出场价传原始数值（与盘中巡检同口径），不做 .4f 假精度格式化
                self.notifier.notify_stop_loss_triggered(
                    symbol,
                    self._get_strategy_display_name('turtle'),
                    position.get('side', ''),
                    closed_position.get('final_exit_price', exit_price),
                    source='日检确认'
                )
                if not stop_cleared:
                    logger.error(f"{symbol} [海龟] 旧止损撤销不可确认，本轮不进行止损后反手开仓")
                    return
                # 止损确认后，检查当前信号是否需要开新仓
                logger.info(f"{symbol} [海龟] 止损已确认，继续检查当前信号...")
                if symbol_config.get('_retired_from_pool'):
                    logger.info(
                        f"{symbol} [海龟] 退池仓止损已确认；结束托管，不再开仓")
                    return
                mid_line_crossed = self.trade_state.get_signal_state(symbol)
                if signal['action'] in ('long', 'short') and mid_line_crossed:
                    logger.info(f"{symbol} [海龟] 止损后检测到标准开仓信号 {signal['action']}，执行反手开仓")
                    self.handle_open_signal_turtle(symbol, signal['action'], signal, symbol_config)
                elif signal['action'] in ('long', 'short'):
                    logger.info(f"{symbol} [海龟] 止损后检测到非标准信号 {signal['action']}（未穿越中轨），忽略")
                else:
                    logger.info(f"{symbol} [海龟] 当前无开仓信号({signal.get('action')}), 等待后续信号")
                return

        if signal['action'] == 'close_long' and position['side'] == 'long':
            self.handle_close_signal(symbol, signal, position, symbol_config)
        elif signal['action'] == 'close_short' and position['side'] == 'short':
            self.handle_close_signal(symbol, signal, position, symbol_config)
        elif signal['action'] in ('long', 'short') and signal.get('mid_line_crossed'):
            # 同一天穿越中轨+突破轨道：先平反向仓位，再开新仓
            if (signal['action'] == 'long' and position['side'] == 'short') or \
               (signal['action'] == 'short' and position['side'] == 'long'):
                logger.info(f"{symbol} [海龟] 穿越中轨+突破轨道，先平{position['side']}仓再开{signal['action']}仓")
                close_ok = self.handle_close_signal(symbol, signal, position, symbol_config, skip_reopen=True)
                if close_ok and not symbol_config.get('_retired_from_pool'):
                    self.handle_open_signal_turtle(symbol, signal['action'], signal, symbol_config)
                elif close_ok:
                    logger.info(
                        f"{symbol} [海龟] 退池仓已平；禁止突破反手新腿，结束托管")
                else:
                    logger.warning(f"{symbol} [海龟] 平仓未成功，取消开仓")
            else:
                self.check_and_update_stop_loss_turtle(symbol, signal, position)
        else:
            if signal.get('_history_discontinuity'):
                # 断档只允许最新一根明确产生的事件进入上方分支；不能仅凭
                # “当前价格已经在中轨另一侧”推断历史期间漏过平仓并补做旧交易。
                logger.warning(
                    f"{symbol} [海龟] 历史断档且最新一根无明确退出/反手信号；"
                    "不执行历史状态补偿平仓，仅按当前通道维护保护止损")
                self.check_and_update_stop_loss_turtle(symbol, signal, position)
                return
            # 补偿平仓：如果错过了穿越中轨的平仓信号（如之前因bug崩溃），
            # 检查价格是否已在中轨的反向一侧，若是则补偿平仓
            current_close = signal.get('current_close', 0)
            mid_line = signal.get('mid_line', 0)
            if position['side'] == 'short' and current_close > mid_line:
                logger.info(
                    f"{symbol} [海龟] 补偿平仓：空单价格"
                    f"({self._format_indicator_price(current_close)})已在中轨"
                    f"({self._format_indicator_price(mid_line)})上方，执行平仓")
                signal['action'] = 'close_short'
                self.handle_close_signal(symbol, signal, position, symbol_config)
            elif position['side'] == 'long' and current_close < mid_line:
                logger.info(
                    f"{symbol} [海龟] 补偿平仓：多单价格"
                    f"({self._format_indicator_price(current_close)})已在中轨"
                    f"({self._format_indicator_price(mid_line)})下方，执行平仓")
                signal['action'] = 'close_long'
                self.handle_close_signal(symbol, signal, position, symbol_config)
            else:
                self.check_and_update_stop_loss_turtle(symbol, signal, position)

    def handle_open_signal_turtle(self, symbol, side, signal, symbol_config):
        """海龟策略：处理开仓信号"""
        if symbol_config.get('_retired_from_pool'):
            logger.info(f"{symbol} [海龟] 品种已退池，禁止开新腿")
            return {'status': 'retired_no_reopen'}
        if signal.get('bootstrap_direct_entry'):
            logger.info(f"{symbol} [海龟] 触发新币启动期直通 {side} 信号，准备开仓...")
        else:
            logger.info(f"{symbol} [海龟] 触发 {side} 信号，准备开仓...")

        entry_price = signal['current_close']
        stop_loss_price = signal['lower_line'] if side == 'long' else signal['upper_line']

        outcome = self._execute_open(
            symbol, side, entry_price, stop_loss_price, symbol_config,
            client_order_id=signal.get('_client_order_id'))
        # 主调度在统一出口据此决定 confirmed/重试；嵌套的“平旧再开新”路径也
        # 不会丢失成功回滚或部分回滚终态。
        signal['_execution_outcome'] = outcome
        return outcome

    def check_and_update_stop_loss_turtle(self, symbol, signal, position):
        """海龟策略：检查并更新止损单（含方向保护）"""
        new_stop_loss_price = signal['lower_line'] if position['side'] == 'long' else signal['upper_line']
        old_stop_loss_price = position['stop_loss_price']

        if new_stop_loss_price == old_stop_loss_price:
            logger.info(f"{symbol} 止损价未变动")
            return

        if position['side'] == 'long' and new_stop_loss_price < old_stop_loss_price:
            logger.info(f"{symbol} 多单止损不允许下移: {old_stop_loss_price} -> {new_stop_loss_price}，保持不变")
            return
        if position['side'] == 'short' and new_stop_loss_price > old_stop_loss_price:
            logger.info(f"{symbol} 空单止损不允许上移: {old_stop_loss_price} -> {new_stop_loss_price}，保持不变")
            return

        logger.info(f"{symbol} 止损价有变动: {old_stop_loss_price} -> {new_stop_loss_price}")
        self._update_stop_order(symbol, position, new_stop_loss_price)


    # ========== 双均线策略处理 ==========

    def handle_no_position_ma_cross(self, symbol, signal, symbol_config, df):
        """双均线策略：处理没有开仓头寸的情况"""
        logger.info(f"{symbol} [双均线] 没有开仓头寸，检查信号...")

        if self.is_stop_loss_today(symbol):
            logger.info(f"{symbol} [双均线] 今天已止损，检查T+1重入条件...")
            logger.info(f"{symbol} [双均线] T+1限制：今天止损过，等待次日重入")
            return

        if signal['action'] == 'long':
            logger.info(f"{symbol} [双均线] 金叉信号，准备做多...")
            entry_price = signal['current_close']
            stop_loss_price = signal['lower_stop']
            self._execute_open(symbol, 'long', entry_price, stop_loss_price, symbol_config)
            if not self.trade_state.get_open_position(symbol):
                self._mark_ma_cross_reentry_pending(
                    symbol,
                    'long',
                    signal,
                    '双均线做多信号已出现，但本轮检查结束后仍无持仓；保留本根 K 线等待日内重试'
                )
        elif signal['action'] == 'short':
            logger.info(f"{symbol} [双均线] 死叉信号，准备做空...")
            entry_price = signal['current_close']
            stop_loss_price = signal['upper_stop']
            self._execute_open(symbol, 'short', entry_price, stop_loss_price, symbol_config)
            if not self.trade_state.get_open_position(symbol):
                self._mark_ma_cross_reentry_pending(
                    symbol,
                    'short',
                    signal,
                    '双均线做空信号已出现，但本轮检查结束后仍无持仓；保留本根 K 线等待日内重试'
                )
        else:
            yesterday_str = None
            if symbol in self.stop_loss_dates:
                yesterday_str = self.stop_loss_dates[symbol]

            if yesterday_str and yesterday_str != date.today().strftime('%Y-%m-%d'):
                logger.info(f"{symbol} [双均线] 检查止损后重入条件...")
                should_reenter, side, reentry_signal = self.ma_cross_strategy.check_reentry_condition(df)
                if should_reenter and side:
                    entry_price = reentry_signal['current_close']
                    if side == 'long':
                        stop_loss_price = reentry_signal['lower_stop']
                    else:
                        stop_loss_price = reentry_signal['upper_stop']
                    logger.info(f"{symbol} [双均线] T+1重入: 方向={side}, EMA仍然{'看多' if side == 'long' else '看空'}")
                    self._execute_open(symbol, side, entry_price, stop_loss_price, symbol_config)
                    if self.trade_state.get_open_position(symbol):
                        # 重入成功：解除 T+1 标记，回归常规「永远在市」
                        self.clear_stop_loss(symbol)
                    else:
                        # 重入开仓未成功（价格已穿止损/超时未确认/残留阻断等）：**保留** T+1 标记，
                        # 次日 EMA 方向仍成立则再重试重入，不放弃「永远在市」（此前无条件删除会永久放弃）
                        self._notify_missing_position_after_signal(
                            symbol,
                            'ma_cross',
                            side,
                            reentry_signal,
                            '双均线 T+1 重入开仓未成功，已保留标记次日按 EMA 方向重试，请复核交易所与日志'
                        )
                        logger.warning(f"{symbol} [双均线] T+1 重入开仓未成功，保留标记次日重试")
                else:
                    # 只有两条 EMA 精确相等时才会进入此分支；方向并未“改变”。
                    # 保留 T+1 标记，下一根已收盘 K 线再按明确 EMA 方向重试。
                    logger.info(f"{symbol} [双均线] EMA 短长线暂时相等，无明确方向，保留 T+1 标记")

    def handle_open_position_ma_cross(self, symbol, signal, position, symbol_config, df):
        """双均线策略：处理已开仓头寸"""
        logger.info(f"{symbol} [双均线] 有开仓头寸({position['side']})，检查信号...")

        # 优先检查交易所端持仓状态（止损可能已触发）
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        exchange_position = self.exchange_api.get_position(ccxt_symbol)
        if exchange_position is None or exchange_position.get('contracts', 0) == 0:
            if self.trade_state.get_open_position(symbol):
                logger.warning(f"{symbol} [双均线] 检测到交易所端已无持仓，止损单可能已触发")
                exit_price = position.get('stop_loss_price', signal['current_close'])
                closed_position, state_saved, _stop_cleared = self._handle_exchange_flat_close(
                    symbol, ccxt_symbol, position, exit_price, "双均线止损确认",
                    strategy_type='ma_cross')
                if not closed_position:
                    return
                if not state_saved:
                    logger.warning(f"{symbol} [双均线] 止损确认已执行，但本地状态落盘失败，本轮不记录 T+1")
                    return
                # 出场价传原始数值（与盘中巡检同口径），不做 .4f 假精度格式化
                self.notifier.notify_stop_loss_triggered(
                    symbol,
                    self._get_strategy_display_name('ma_cross'),
                    position.get('side', ''),
                    closed_position.get('final_exit_price', exit_price),
                    source='日检确认（T+1 已记录）'
                )
                logger.info(f"{symbol} [双均线] 止损已记录，T+1将检查重入")
                return

        # 检查反向交叉信号
        if signal['action'] == 'long' and position['side'] == 'short':
            logger.info(f"{symbol} [双均线] 金叉信号，空翻多！先平空仓...")
            self._flip_position(symbol, signal, position, 'long', symbol_config)
            return
        elif signal['action'] == 'short' and position['side'] == 'long':
            logger.info(f"{symbol} [双均线] 死叉信号，多翻空！先平多仓...")
            self._flip_position(symbol, signal, position, 'short', symbol_config)
            return

        logger.info(f"{symbol} [双均线] 无反向交叉信号，止损保持不变: {position['stop_loss_price']}")
