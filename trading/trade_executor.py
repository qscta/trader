"""下单执行子系统（TradingSystem 的 mixin）。

真钱下单的统一执行边界：通用开仓（止损残留阻断、实时市价计算仓位、双重
方向校验、成交后风险校验与回滚、挂止损失败回滚平仓）、止损单更新（验证式
撤旧→持仓复核→挂新）、平仓信号执行、双均线翻转（平旧开新）、开仓落盘
失败的交易所侧回滚。

以 mixin 形式承载：方法仍绑定在 TradingSystem 实例上——self 语义、
测试对实例方法的桩打法、调用链与日志行为全部不变，只做物理分层。
宿主须提供：exchange_api / trade_state / notifier / risk_manager / config /
_pending_stop_loss_updates / record_stop_loss / _cancel_stop_order_confirmed /
_close_trade_state_with_runtime_fallback / _update_trade_state_stop_with_runtime_fallback /
_buffer_trade_open_notification / _buffer_trade_close_notification /
handle_open_signal_turtle。
"""

import logging

from trade_state import TradeStatePersistenceError

logger = logging.getLogger(__name__)


class TradeExecutorMixin:

    def _flip_position(self, symbol, signal, old_position, new_side, symbol_config):
        """双均线策略：翻转仓位（平旧开新）"""
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)

        # 获取实时市价用于记录平仓价
        exit_price = signal['current_close']
        try:
            exit_price = self.exchange_api.get_last_price(ccxt_symbol)
            logger.info(f"{symbol} [双均线] 翻转使用实时市价: {exit_price}")
        except Exception as e:
            logger.warning(f"{symbol} [双均线] 获取实时市价失败({e})，使用收盘价: {exit_price}")

        close_order = self.exchange_api.close_position(ccxt_symbol, old_position['side'], old_position['position_size'])
        if not close_order:
            logger.error(f"{symbol} [双均线] 翻转平仓失败")
            self.notifier.notify_error(f"{symbol} [双均线] 翻转平仓失败")
            return

        stop_cleared = self._cancel_stop_order_confirmed(symbol, ccxt_symbol, old_position.get('stop_order_id'))

        # 用平仓订单的实际成交价记录
        actual_exit = close_order.get('average', exit_price)
        if isinstance(actual_exit, str):
            actual_exit = float(actual_exit)
        if actual_exit is None:
            actual_exit = exit_price

        closed_position, state_saved = self._close_trade_state_with_runtime_fallback(
            symbol, actual_exit, "双均线翻转平仓"
        )
        if not closed_position:
            return
        pnl = round(closed_position['pnl'], 2)
        pnl_pct = round(closed_position['pnl_percent'], 2)
        logger.info(f"{symbol} [双均线] 翻转平仓成功: 出场价={actual_exit}, 盈亏={pnl}, 盈亏率={pnl_pct}%")
        self._buffer_trade_close_notification(symbol, old_position['side'], actual_exit, pnl, pnl_pct)

        if not state_saved:
            return

        if not stop_cleared:
            # 记入 T+1：次日日检开头会自动重试清理残留，清理确认后由 T+1 重入按当时
            # EMA 方向重新入场，恢复「永远在市」；清理仍失败则维持开仓阻断（已有告警）。
            self.record_stop_loss(symbol)
            logger.error(f"{symbol} [双均线] 旧止损撤销不可确认，本轮不反手开仓；已记录 T+1，残留清理确认后次日按 EMA 方向重入")
            return

        # 开仓价由 _execute_open 内部获取实时价格，这里传信号价作为参考
        entry_price = signal['current_close']
        if new_side == 'long':
            stop_loss_price = signal['lower_stop']
        else:
            stop_loss_price = signal['upper_stop']

        logger.info(f"{symbol} [双均线] 翻转开仓: 方向={new_side}, 信号价={entry_price}, 止损={stop_loss_price}")
        self._execute_open(symbol, new_side, entry_price, stop_loss_price, symbol_config)
        if not self.trade_state.get_open_position(symbol):
            # 平旧仓成功但反手开新腿失败（价格已穿止损/超时未确认/保证金不足等）：记 T+1，
            # 次日 handle_no_position_ma_cross 按当时 EMA 方向自动重入，恢复「永远在市」——
            # 与 stop_cleared=False 分支同一恢复机制（_execute_open 内部已发失败告警）
            self._mark_ma_cross_reentry_pending(
                symbol, new_side, signal,
                '双均线翻转反手开仓未成功，已记 T+1 次日按 EMA 方向重入，请复核交易所与日志')
            logger.error(f"{symbol} [双均线] 翻转反手开仓未成功，已记 T+1 次日按 EMA 方向重入恢复在市")

    def _persist_open_position_or_rollback(self, symbol, ccxt_symbol, side, actual_price, position_size, stop_loss_price, stop_order_id, strategy=None):
        try:
            self.trade_state.add_open_position(symbol, side, actual_price, position_size, stop_loss_price, stop_order_id, strategy=strategy)
            return True
        except TradeStatePersistenceError as e:
            logger.critical(f"{symbol} 开仓后本地状态保存失败，执行交易所侧回滚: {e}")
            if not self._cancel_stop_order_confirmed(symbol, ccxt_symbol, stop_order_id):
                logger.critical(f"{symbol} 回滚时新止损撤销不可确认，已标记残留")

            rollback = self.exchange_api.close_position(ccxt_symbol, side, position_size)
            if rollback:
                logger.warning(f"{symbol} 已执行状态失败后的回滚平仓，避免本地无记录的裸仓")
            else:
                logger.critical(f"{symbol} 状态失败后的回滚平仓失败，请立即人工处理！")

            self.notifier.notify_error(
                f"{symbol} 开仓后本地状态保存失败，已尝试撤单并平仓，请立即核对交易所持仓和本地状态"
            )
            return False

    def _execute_open(self, symbol, side, entry_price, stop_loss_price, symbol_config, buffer_notification=True):
        """通用开仓执行逻辑（使用实时市场价计算仓位，成交后校验风险）。

        buffer_notification=False 供即时开仓路由使用：该路由自己发专属钉钉，
        不走日检的汇总缓冲——否则消息滞留缓冲区，直到下次日检开头被静默清空。
        """
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)

        # 止损残留阻断：该品种可能有撤销未确认的旧止损单，开新仓可能被残留单错杀
        if self.trade_state.has_stop_residue(symbol):
            msg = f"{symbol} 存在撤销未确认的止损单残留，阻断本次开仓；待自动清理确认或人工核对欧易委托后恢复"
            logger.error(msg)
            self.notifier.notify_error(msg)
            return

        # 孤儿仓阻断：本方法所有调用点都以「本地无持仓」为前提，此刻交易所端若有持仓，
        # 必为本地无记录的孤儿仓（人工开仓/开仓超时后迟到成交）。单向(净)模式下新单会与
        # 孤儿仓合并成一笔更大的净持仓——本地只记本次数量、止损也只覆盖本次数量，孤儿部分
        # 继续裸奔且账目/风控全部错位。与止损残留同标准 fail-closed；巡检告警负责发现孤儿，
        # 本阻断负责在人工处理前不让敞口继续叠加。查询失败按无孤儿继续（与适配层开仓前
        # 查询失败的既有取向一致：不让读故障吞掉入场，孤儿检测主责在巡检告警）。
        try:
            unmanaged = self.exchange_api.get_position(ccxt_symbol)
        except Exception as e:
            unmanaged = None
            logger.warning(f"{symbol} 开仓前孤儿仓核对查询失败({e})，按无孤儿继续")
        if unmanaged is not None:
            msg = (f"{symbol} 交易所端已存在本地无记录的持仓（孤儿仓），已阻断本次开仓："
                   f"单向模式下新单会与孤儿仓合并，止损只覆盖新增部分。请人工处理该持仓后再开仓")
            logger.error(msg)
            self.notifier.notify_error(msg)
            return

        # 基本方向校验：防止数据异常时开出危险仓位
        if side == 'long' and stop_loss_price >= entry_price:
            logger.error(f"{symbol} 开仓中止: 多单止损价({stop_loss_price})必须低于入场价({entry_price})")
            return
        if side == 'short' and stop_loss_price <= entry_price:
            logger.error(f"{symbol} 开仓中止: 空单止损价({stop_loss_price})必须高于入场价({entry_price})")
            return

        # ====== 核心修复：用实时市场价替代信号收盘价计算仓位 ======
        calc_price = entry_price  # 默认回退值
        try:
            market_price = self.exchange_api.get_last_price(ccxt_symbol)
            price_diff_pct = abs(market_price - entry_price) / entry_price * 100
            logger.info(f"{symbol} 实时市价={market_price}, 信号价={entry_price}, 偏差={price_diff_pct:.2f}%")
            calc_price = market_price  # 用实时价格计算仓位
        except Exception as e:
            logger.warning(f"{symbol} 获取实时市价失败({e})，回退使用信号价: {entry_price}")

        # 使用实际计算价再次校验止损方向，避免信号价过时导致危险开仓
        if side == 'long' and stop_loss_price >= calc_price:
            logger.error(f"{symbol} 开仓中止: 多单止损价({stop_loss_price})必须低于实时计算价({calc_price})")
            return
        if side == 'short' and stop_loss_price <= calc_price:
            logger.error(f"{symbol} 开仓中止: 空单止损价({stop_loss_price})必须高于实时计算价({calc_price})")
            return

        balance = self.exchange_api.get_balance()
        # 防御式取值：balance 为空、缺 total 段、或该段无 USDT 键，统一回退上次记录的权益
        # （与启动/统计路径的 .get 口径一致；直接 balance['total']['USDT'] 遇缺键会抛 KeyError）
        account_equity = balance.get('total', {}).get('USDT') if balance else None
        if account_equity is not None:
            self.risk_manager.account_equity = account_equity
            logger.info(f"已更新账户权益: {account_equity} USDT")
        else:
            logger.warning(f"获取账户余额失败，使用上次记录的权益: {self.risk_manager.account_equity} USDT")
            account_equity = self.risk_manager.account_equity

        risk_per_trade = symbol_config.get('risk_per_trade', self.config['strategy']['default_risk_per_trade'])
        self.risk_manager.risk_per_trade = risk_per_trade
        logger.info(f"{symbol} 使用风险度: {risk_per_trade*100:.1f}%")

        raw_position_size = self.risk_manager.calculate_position_size(calc_price, stop_loss_price, risk_per_trade)
        if raw_position_size <= 0:
            logger.error(f"{symbol} 止损距离为0或无效，无法计算头寸")
            return

        price_risk_pct = abs(calc_price - stop_loss_price) / calc_price
        risk_amount = account_equity * risk_per_trade
        position_value = risk_amount / price_risk_pct

        # P3-1修复：使用动态精度舍入，不再硬编码round(..., 3)
        position_size = self.exchange_api.round_quantity(ccxt_symbol, raw_position_size)
        precision = self.exchange_api.get_quantity_precision(ccxt_symbol)

        logger.info(f"{symbol} 仓位计算: 权益={account_equity:.2f}, 风险度={risk_per_trade*100:.1f}%, "
                    f"风险金额={risk_amount:.2f}, 计算价={calc_price}, 信号价={entry_price}, 止损价={stop_loss_price}, "
                    f"价格风险%={price_risk_pct*100:.2f}%, 仓位价值={position_value:.2f}, "
                    f"原始数量={raw_position_size}, 精度={precision}, 最终数量={position_size}")

        if position_size <= 0:
            logger.error(f"{symbol} 头寸大小无效: {position_size}")
            return

        open_order = self.exchange_api.open_position(ccxt_symbol, side, position_size)
        if not open_order:
            logger.error(f"{symbol} 开仓失败")
            self.notifier.notify_error(f"{symbol} 开仓失败")
            return

        actual_price = open_order.get('average', calc_price)
        if isinstance(actual_price, str):
            actual_price = float(actual_price)
        if actual_price is None:
            actual_price = calc_price

        # 成交后再次校验，若价格已穿越止损位，立即回滚避免创建无效止损单
        if side == 'long' and stop_loss_price >= actual_price:
            logger.error(f"{symbol} 开仓回滚: 多单止损价({stop_loss_price})已不低于成交价({actual_price})")
            self.notifier.notify_error(f"{symbol} 开仓后价格已接近/跌破止损位，已取消建仓")
            rollback = self.exchange_api.close_position(ccxt_symbol, side, position_size)
            if rollback:
                logger.warning(f"{symbol} 已执行开仓后回滚平仓，避免无效止损")
            else:
                logger.critical(f"{symbol} 开仓后回滚失败，请立即人工处理！")
            return
        if side == 'short' and stop_loss_price <= actual_price:
            logger.error(f"{symbol} 开仓回滚: 空单止损价({stop_loss_price})已不高于成交价({actual_price})")
            self.notifier.notify_error(f"{symbol} 开仓后价格已接近/突破止损位，已取消建仓")
            rollback = self.exchange_api.close_position(ccxt_symbol, side, position_size)
            if rollback:
                logger.warning(f"{symbol} 已执行开仓后回滚平仓，避免无效止损")
            else:
                logger.critical(f"{symbol} 开仓后回滚失败，请立即人工处理！")
            return

        stop_order = self.exchange_api.create_stop_loss_order(ccxt_symbol, side, position_size, stop_loss_price)
        if not stop_order:
            logger.error(f"{symbol} 创建止损单失败")
            self.notifier.notify_error(f"{symbol} 创建止损单失败，请手动设置止损！")
            # 先清扫可能的孤儿止损单再回滚平仓：create_stop_loss_order 返回 None 的一种情形是
            # 下单超时→实际已在交易所创建→复查因触发价被交易所按 tick 取整、与我方原始价差超
            # 1ppm 未匹配上。这张 reduce-only 孤儿单本地无记录、止损自愈只查「记录的单是否在」
            # 检测不到它，会在该品种下次开仓时错价触发误平新仓。回滚要平的就是本次仓位，撤光该
            # 品种挂单是安全的（幂等）。
            if not self.exchange_api.cancel_all_orders(ccxt_symbol):
                logger.warning(f"{symbol} 回滚前清扫挂单未确认，可能残留孤儿止损单，请人工核对欧易委托")
            rollback = self.exchange_api.close_position(ccxt_symbol, side, position_size)
            if rollback:
                logger.warning(f"{symbol} 已执行紧急回滚平仓，避免裸仓风险")
            else:
                logger.critical(f"{symbol} 紧急回滚平仓失败，请立即人工处理！")
            return

        stop_order_id = stop_order.get('id') if stop_order else None

        order_value = position_size * actual_price
        max_loss = account_equity * risk_per_trade
        logger.info(f"{symbol} 开仓成功: 方向={side}, 实际入场价={actual_price}, "
                    f"头寸={position_size}, 仓位价值={order_value:.2f} USDT, "
                    f"止损={stop_loss_price}, 最大亏损={max_loss:.2f} USDT, 止损单ID={stop_order_id}")

        # ====== 安全网：成交后风险校验 ======
        if side == 'long':
            actual_risk = (actual_price - stop_loss_price) * position_size
        else:
            actual_risk = (stop_loss_price - actual_price) * position_size
        risk_ratio = actual_risk / risk_amount if risk_amount > 0 else 0
        logger.info(f"{symbol} 风险校验: 预期风险={risk_amount:.2f}U, 实际风险={actual_risk:.2f}U, 比率={risk_ratio:.2f}")
        if risk_ratio > 1.2:
            warn_msg = (f"⚠️ {symbol} 风险超标警告！\n"
                       f"实际风险={actual_risk:.2f}U，预期={risk_amount:.2f}U（超出{(risk_ratio-1)*100:.0f}%）\n"
                       f"计算价={calc_price}, 成交价={actual_price}, 止损={stop_loss_price}\n"
                       f"请检查是否需要手动减仓")
            logger.warning(warn_msg)
            self.notifier.notify_error(warn_msg)

        if not self._persist_open_position_or_rollback(
            symbol, ccxt_symbol, side, actual_price, position_size, stop_loss_price, stop_order_id,
            strategy=symbol_config.get('strategy', 'turtle')
        ):
            return
        if buffer_notification:
            self._buffer_trade_open_notification(symbol, side, actual_price, position_size, stop_loss_price)


    def _update_stop_order(self, symbol, position, new_stop_loss_price):
        """通用止损单更新逻辑"""
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        old_stop_loss_price = position['stop_loss_price']

        if not self._cancel_stop_order_confirmed(symbol, ccxt_symbol, position.get('stop_order_id')):
            logger.error(f"{symbol} 旧止损撤销不可确认，本次不更新止损（本地保持旧止损记录，与交易所现存挂单对应）")
            return
        logger.info(f"{symbol} 旧止损单已撤销(已验证)")

        # 撤旧确认与挂新之间的缝隙防护：若旧止损恰好在撤销瞬间已触发（或已被人工平仓），
        # 交易所此刻已无持仓，再挂新止损会留下一张无仓可对应的孤儿 reduce-only 条件单。
        # 仅在「查询成功且确认无仓」时放弃挂新，本地仓位交由盘中巡检/日检确认记平；
        # 查询失败按持仓仍在处理继续挂新（保护现有仓位优先于避免孤儿挂单）。
        try:
            if self.exchange_api.get_position(ccxt_symbol) is None:
                logger.warning(f"{symbol} 旧止损撤销确认后交易所已无持仓（可能撤销瞬间已触发/人工平仓），"
                               f"不再挂新止损，交由盘中巡检/日检确认记平")
                return
        except Exception as e:
            logger.warning(f"{symbol} 挂新止损前持仓复核失败({e})，按持仓仍在继续挂新止损")

        stop_order = self.exchange_api.create_stop_loss_order(ccxt_symbol, position['side'], position['position_size'], new_stop_loss_price)
        if not stop_order:
            logger.error(f"{symbol} 创建新止损单失败")
            self.notifier.notify_error(f"{symbol} 更新止损单失败，请手动检查！")
            return

        stop_order_id = stop_order.get('id')
        # BUG-2 修复: 调用标准方法更新止损，以记录时间戳
        updated_position, state_saved = self._update_trade_state_stop_with_runtime_fallback(
            symbol, new_stop_loss_price, stop_order_id, "止损更新"
        )
        if not updated_position:
            return

        logger.info(f"{symbol} 止损单已更新: 新止损价={new_stop_loss_price}, 新止损单ID={stop_order_id}")
        if state_saved:
            self._pending_stop_loss_updates.append({
                'symbol': symbol,
                'old_stop_loss_price': old_stop_loss_price,
                'new_stop_loss_price': new_stop_loss_price,
            })

    def handle_close_signal(self, symbol, signal, position, symbol_config, skip_reopen=False):
        """通用平仓信号处理（海龟策略使用）"""
        logger.info(f"{symbol} 触发平仓信号，准备平仓...")

        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)

        close_order = self.exchange_api.close_position(ccxt_symbol, position['side'], position['position_size'])
        if not close_order:
            logger.error(f"{symbol} 平仓失败")
            self.notifier.notify_error(f"{symbol} 平仓失败")
            return False

        stop_cleared = self._cancel_stop_order_confirmed(symbol, ccxt_symbol, position.get('stop_order_id'))

        # BUG-3 修复: 使用实际成交价记录盈亏，而非K线收盘价
        exit_price = close_order.get('average', signal['current_close'])
        if isinstance(exit_price, str):
            exit_price = float(exit_price)
        if exit_price is None:
            exit_price = signal['current_close']

        closed_position, state_saved = self._close_trade_state_with_runtime_fallback(
            symbol, exit_price, "信号平仓"
        )
        if not closed_position:
            return False

        pnl = round(closed_position['pnl'], 2)
        pnl_pct = round(closed_position['pnl_percent'], 2)
        logger.info(f"{symbol} 平仓成功: 出场价={exit_price}, 盈亏={pnl}, 盈亏率={pnl_pct}%")
        self._buffer_trade_close_notification(symbol, position['side'], exit_price, pnl, pnl_pct)

        if not state_saved:
            return False

        if not stop_cleared:
            # 平仓已记账完成；返回 False 让本函数与调用方都不进入任何再开仓流程，
            # 不依赖 _execute_open 的残留检查做下游兜底
            logger.error(f"{symbol} 旧止损撤销不可确认，平仓已记账，阻断后续一切再开仓（残留清理确认后恢复）")
            return False

        # 检查是否有新的开仓机会
        if skip_reopen:
            logger.info(f"{symbol} 平仓完成（调用方将负责开仓）")
            return True
        logger.info(f"{symbol} 仍在品种池内，检查当前信号是否需要开新仓...")
        if signal['action'] in ('long', 'short'):
            logger.info(f"{symbol} 检测到开仓信号 {signal['action']}，立即执行开仓")
            self.handle_open_signal_turtle(symbol, signal['action'], signal, symbol_config)
        else:
            logger.info(f"{symbol} 平仓后继续监控")
        return True
