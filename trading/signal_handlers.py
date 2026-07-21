"""策略信号分派子系统（TradingSystem 的 mixin）。

双均线 EMA 策略的信号处理：无仓时的开仓信号判定、有仓时的止损确认/翻转
分派、双均线 T+1 止损重入。

以 mixin 形式承载：方法仍绑定在 TradingSystem 实例上——self 语义、
测试对实例方法的桩打法、调用链与日志行为全部不变，只做物理分层。
宿主须提供：exchange_api / trade_state / notifier / ma_cross_strategy /
stop_loss_dates / is_stop_loss_today / record_stop_loss / clear_stop_loss /
_handle_exchange_flat_close / _get_strategy_display_name /
_notify_missing_position_after_signal / _execute_open / _flip_position。
"""

import logging
from datetime import date

logger = logging.getLogger(__name__)


class SignalHandlersMixin:

    def _mark_ma_cross_reentry_pending(self, symbol, side, signal, reason):
        """双均线开仓腿失败后统一收口：记 T+1「重入待定」标记 + 告警。

        双均线「永远在市」：任一开仓腿失败（初始金叉/死叉开仓、翻转反手、T+1 重入）若不留
        恢复线索，该品种会一直空到下一次全新 EMA 交叉。记 T+1 让次日 handle_no_position_ma_cross
        的重入逻辑按当时 EMA 方向自动补回持仓——与止损后 T+1 重入同一套机制。
        （标记记为当天：当日不再抢开，交由次日重入；同日 08:01 整轮重试对孤立失败本不触发，
        故不抑制既有恢复。）
        """
        self.record_stop_loss(symbol)
        self._notify_missing_position_after_signal(symbol, 'ma_cross', side, signal, reason)

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
                    '双均线做多信号已出现，但本轮检查结束后仍无持仓，已记 T+1 次日按 EMA 方向重入'
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
                    '双均线做空信号已出现，但本轮检查结束后仍无持仓，已记 T+1 次日按 EMA 方向重入'
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
                        # 重入成功：解除 T+1 标记，回归常规「永远在市」。标记通常已在
                        # _execute_open 成功路径统一清除，此处 pop 幂等兜底（del 会 KeyError）
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
                    logger.info(f"{symbol} [双均线] 重入条件不满足（EMA方向已变），不重入")
                    self.clear_stop_loss(symbol)

    def handle_open_position_ma_cross(self, symbol, signal, position, symbol_config, df):
        """双均线策略：处理已开仓头寸"""
        logger.info(f"{symbol} [双均线] 有开仓头寸({position['side']})，检查信号...")
        exit_only = bool(symbol_config.get('exit_only'))

        # 优先检查交易所端持仓状态（止损可能已触发）
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        exchange_position = self.exchange_api.get_position(ccxt_symbol)
        if exchange_position is None or exchange_position.get('contracts', 0) == 0:
            if not self._confirm_exchange_flat(symbol, ccxt_symbol):
                return
            if self.trade_state.get_open_position(symbol):
                logger.warning(f"{symbol} [双均线] 检测到交易所端已无持仓，止损单可能已触发")
                exit_price = position.get('stop_loss_price', signal['current_close'])
                self._persist_exchange_flat_policy(symbol, exit_only)
                closed_position, state_saved, _stop_cleared = self._handle_exchange_flat_close(
                    symbol, ccxt_symbol, position, exit_price, "双均线止损确认")
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
                    exit_price,
                    source='日检确认（退出模式不重入）' if exit_only else '日检确认（T+1 已记录）'
                )
                if exit_only:
                    logger.info(f"{symbol} [退出模式] 当前仓已结束，不记录 T+1，后续不再开仓")
                else:
                    logger.info(f"{symbol} [双均线] 止损已记录，T+1将检查重入")
                return
        elif not self._managed_position_is_consistent(
                symbol, ccxt_symbol, position, exchange_position):
            return

        # 检查反向交叉信号
        if signal['action'] == 'long' and position['side'] == 'short':
            action_text = '退出模式只平空仓、不反手' if exit_only else '空翻多'
            logger.info(f"{symbol} [双均线] 金叉信号，{action_text}...")
            self._flip_position(
                symbol, signal, position, 'long', symbol_config,
                exit_only=exit_only)
            return
        elif signal['action'] == 'short' and position['side'] == 'long':
            action_text = '退出模式只平多仓、不反手' if exit_only else '多翻空'
            logger.info(f"{symbol} [双均线] 死叉信号，{action_text}...")
            self._flip_position(
                symbol, signal, position, 'short', symbol_config,
                exit_only=exit_only)
            return

        logger.info(f"{symbol} [双均线] 无反向交叉信号，止损保持不变: {position['stop_loss_price']}")
