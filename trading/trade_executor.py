"""下单执行子系统（TradingSystem 的 mixin）。

真钱下单的统一执行边界：通用开仓（止损残留阻断、实时市价计算仓位、双重
方向校验、成交后风险校验与回滚、挂止损失败回滚平仓）、双均线翻转
（平旧开新）、开仓落盘失败的交易所侧回滚。

以 mixin 形式承载：方法仍绑定在 TradingSystem 实例上——self 语义、
测试对实例方法的桩打法、调用链与日志行为全部不变，只做物理分层。
宿主须提供：exchange_api / trade_state / notifier / risk_manager / config /
    stop_loss_dates / clear_stop_loss /
record_stop_loss / _cancel_stop_order_confirmed / _mark_ma_cross_reentry_pending /
_close_trade_state_with_runtime_fallback /
_buffer_trade_open_notification / _buffer_trade_close_notification。
"""

import logging
import math

from trade_state import TradeStatePersistenceError

logger = logging.getLogger(__name__)


class TradeExecutorMixin:

    def _retain_position_after_failed_rollback(
            self, symbol, side, actual_price, position_size, stop_loss_price,
            stop_order_id, strategy, force_runtime=False):
        """回滚未确认时沿用普通持仓模型继续托管，不把真实残仓遗忘成空仓。"""
        if force_runtime:
            self.trade_state.force_runtime_add_open_position(
                symbol, side, actual_price, position_size, stop_loss_price,
                stop_order_id, strategy=strategy)
            logger.critical(f"{symbol} 回滚失败且持仓只能保留在当前进程内存")
        else:
            try:
                self.trade_state.add_open_position(
                    symbol, side, actual_price, position_size, stop_loss_price,
                    stop_order_id, strategy=strategy)
            except TradeStatePersistenceError as e:
                self.trade_state.force_runtime_add_open_position(
                    symbol, side, actual_price, position_size, stop_loss_price,
                    stop_order_id, strategy=strategy)
                logger.critical(f"{symbol} 回滚失败且持仓只能保留在当前进程内存: {e}")
        self.notifier.notify_error(
            f"{symbol} 开仓回滚未确认，已保留为托管持仓；请立即核对实仓与止损，修复前勿重启")

    def _flip_position(self, symbol, signal, old_position, new_side, symbol_config, exit_only=False):
        """双均线策略：翻转仓位；已删除品种在 exit_only 模式下只平旧仓。"""
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

        # 先把“不可反手/T+1”意图落盘，再记平本地仓位；否则两步之间崩溃，
        # 重启补跑可能在旧止损仍不可确认时同日重新开仓。
        if exit_only:
            self._persist_exchange_flat_policy(symbol, True)
        elif not stop_cleared:
            self.record_stop_loss(symbol)

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

        if exit_only:
            if not stop_cleared:
                logger.error(f"{symbol} [退出模式] 平仓后旧止损撤销不可确认，已标记残留；不反手、不记 T+1")
            else:
                logger.info(f"{symbol} [退出模式] 当前仓已平，不反手、不记 T+1")
            return

        if not stop_cleared:
            # 记入 T+1：次日日检开头会自动重试清理残留，清理确认后由 T+1 重入按当时
            # EMA 方向重新入场，恢复「永远在市」；清理仍失败则维持开仓阻断（已有告警）。
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
            # 先平仓、后清止损：若平仓失败，原止损仍可能保护残仓。
            rollback = self.exchange_api.close_position(ccxt_symbol, side, position_size)
            if rollback:
                logger.warning(f"{symbol} 已执行状态失败后的回滚平仓，避免本地无记录的裸仓")
                if not self._cancel_stop_order_confirmed(symbol, ccxt_symbol, stop_order_id):
                    logger.critical(f"{symbol} 回滚平仓后新止损撤销不可确认，已标记残留")
            else:
                logger.critical(f"{symbol} 状态失败后的回滚平仓未确认，保留止损并在内存继续托管")
                self._retain_position_after_failed_rollback(
                    symbol, side, actual_price, position_size, stop_loss_price,
                    stop_order_id, strategy, force_runtime=True)

            self.notifier.notify_error(
                f"{symbol} 开仓后本地状态保存失败，已尝试撤单并平仓，请立即核对交易所持仓和本地状态"
            )
            return False

    def _execute_open(self, symbol, side, entry_price, stop_loss_price, symbol_config, buffer_notification=True):
        """通用开仓执行逻辑（使用实时市场价计算仓位，成交后校验风险）。

        buffer_notification=False 供即时开仓路由使用：该路由自己发专属钉钉，
        不走日检的汇总缓冲——否则消息滞留缓冲区，直到下次日检开头被静默清空。
        """
        # 所有自动/即时/T+1/翻转开仓最终都经过本方法；部署总闸必须放在
        # 这个最内层边界，避免只拦调度入口却漏掉 Web 即时开仓或反手腿。
        if getattr(self, 'new_entries_disabled', False):
            logger.warning(f"{symbol} 新开仓被 TRADING_DISABLE_NEW_OPENS 总闸阻断")
            return
        if symbol_config.get('enabled') is False or symbol_config.get('exit_only'):
            logger.warning(f"{symbol} 已禁用或处于退出模式，本次新开仓被阻断")
            return

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
        # 本阻断负责在人工处理前不让敞口继续叠加。查询失败时无法证明空仓，
        # 按同一 fail-closed 标准拒绝开仓。
        try:
            unmanaged = self.exchange_api.get_position(ccxt_symbol)
        except Exception as e:
            msg = f"{symbol} 开仓前持仓核对失败({e})，无法证明交易所空仓，已阻断本次开仓"
            logger.error(msg)
            self.notifier.notify_error(msg)
            return
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
        try:
            market_price = self.exchange_api.get_last_price(ccxt_symbol)
            price_diff_pct = abs(market_price - entry_price) / entry_price * 100
            logger.info(f"{symbol} 实时市价={market_price}, 信号价={entry_price}, 偏差={price_diff_pct:.2f}%")
            calc_price = market_price
        except Exception as e:
            logger.error(f"{symbol} 获取实时市价失败({e})，无法安全计算仓位，阻断本次开仓")
            return

        # 使用实际计算价再次校验止损方向，避免信号价过时导致危险开仓
        if side == 'long' and stop_loss_price >= calc_price:
            logger.error(f"{symbol} 开仓中止: 多单止损价({stop_loss_price})必须低于实时计算价({calc_price})")
            return
        if side == 'short' and stop_loss_price <= calc_price:
            logger.error(f"{symbol} 开仓中止: 空单止损价({stop_loss_price})必须高于实时计算价({calc_price})")
            return

        balance = self.exchange_api.get_balance()
        account_equity = balance.get('total', {}).get('USDT') if balance else None
        try:
            account_equity = float(account_equity)
        except (TypeError, ValueError):
            account_equity = None
        # 新开仓必须基于刚取得的真实权益；余额缺失时沿用启动快照会在入出金、
        # 账户变动或异常响应后按错误资金规模下单。无法证明时直接拒绝。
        if account_equity is None or not math.isfinite(account_equity) or account_equity <= 0:
            logger.error(f"{symbol} 无法取得有效的当前账户权益，阻断本次开仓")
            return
        self.risk_manager.account_equity = account_equity
        logger.info(f"已更新账户权益: {account_equity} USDT")

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

        # 在最终发单边界前确认该品种没有历史止损或待成交普通单。进程可在
        # 「止损请求已到交易所→本地记残留标记」之间崩溃，仅查本地标记会放过孤儿单；
        # 下一仓可被它误平。复用验证式清扫：不可确认即持久阻断，绝不带单开仓。
        # 连续空仓确认必须紧贴清扫，避免前面行情/余额查询形成过大的检查后竞态窗口。
        try:
            exchange_flat = self.exchange_api.confirm_position_flat(ccxt_symbol)
        except Exception as e:
            exchange_flat = False
            logger.warning(f"{symbol} 开仓前连续空仓确认异常: {e}")
        if not exchange_flat:
            msg = (f"{symbol} 开仓前无法连续证明交易所空仓，已阻断挂单清扫和开仓；"
                   "避免瞬时空响应导致撤掉真实持仓的保护单")
            logger.error(msg)
            self.notifier.notify_error(msg)
            return
        if not self._cancel_stop_order_confirmed(symbol, ccxt_symbol, None):
            logger.error(f"{symbol} 开仓前挂单清扫不可确认，已阻断本次开仓")
            return

        open_order = self.exchange_api.open_position(ccxt_symbol, side, position_size)
        if not open_order:
            msg = (f"{symbol} 开仓失败或完整成交无法确认；适配层已尝试撤销未完成订单并回滚，"
                   "请立即核对 OKX 持仓与委托，确认前系统不会认领该仓位")
            logger.error(msg)
            self.notifier.notify_error(msg)
            return

        confirmed_size = open_order.get('confirmed_coin_amount')
        if confirmed_size is not None:
            try:
                confirmed_size = float(confirmed_size)
            except (TypeError, ValueError):
                confirmed_size = 0
            if not math.isfinite(confirmed_size) or confirmed_size <= 0:
                logger.critical(f"{symbol} 开仓确认返回非法实际数量，执行紧急回滚且不继续自动归因")
                rollback = self.exchange_api.close_position(
                    ccxt_symbol, side, position_size)
                if not rollback:
                    self._retain_position_after_failed_rollback(
                        symbol, side, calc_price, position_size, stop_loss_price,
                        None, symbol_config.get('strategy', 'ma_cross'))
                return
            position_size = confirmed_size

        actual_price = open_order.get('average', calc_price)
        if actual_price is None:
            actual_price = calc_price
        try:
            actual_price = float(actual_price)
        except (TypeError, ValueError):
            actual_price = float('nan')
        if not math.isfinite(actual_price) or actual_price <= 0:
            logger.critical(f"{symbol} 开仓返回非法成交价，执行紧急回滚且不写入账本")
            rollback = self.exchange_api.close_position(
                ccxt_symbol, side, position_size)
            if not rollback:
                self._retain_position_after_failed_rollback(
                    symbol, side, calc_price, position_size, stop_loss_price,
                    None, symbol_config.get('strategy', 'ma_cross'))
            return

        # 成交后再次校验，若价格已穿越止损位，立即回滚避免创建无效止损单
        if side == 'long' and stop_loss_price >= actual_price:
            logger.error(f"{symbol} 开仓回滚: 多单止损价({stop_loss_price})已不低于成交价({actual_price})")
            self.notifier.notify_error(f"{symbol} 开仓后价格已接近/跌破止损位，已取消建仓")
            rollback = self.exchange_api.close_position(ccxt_symbol, side, position_size)
            if rollback:
                logger.warning(f"{symbol} 已执行开仓后回滚平仓，避免无效止损")
            else:
                logger.critical(f"{symbol} 开仓后回滚未确认，保留为托管持仓等待人工/巡检")
                self._retain_position_after_failed_rollback(
                    symbol, side, actual_price, position_size, stop_loss_price,
                    None, symbol_config.get('strategy', 'ma_cross'))
            return
        if side == 'short' and stop_loss_price <= actual_price:
            logger.error(f"{symbol} 开仓回滚: 空单止损价({stop_loss_price})已不高于成交价({actual_price})")
            self.notifier.notify_error(f"{symbol} 开仓后价格已接近/突破止损位，已取消建仓")
            rollback = self.exchange_api.close_position(ccxt_symbol, side, position_size)
            if rollback:
                logger.warning(f"{symbol} 已执行开仓后回滚平仓，避免无效止损")
            else:
                logger.critical(f"{symbol} 开仓后回滚未确认，保留为托管持仓等待人工/巡检")
                self._retain_position_after_failed_rollback(
                    symbol, side, actual_price, position_size, stop_loss_price,
                    None, symbol_config.get('strategy', 'ma_cross'))
            return

        stop_order = self.exchange_api.create_stop_loss_order(ccxt_symbol, side, position_size, stop_loss_price)
        if not stop_order:
            logger.error(f"{symbol} 创建止损单失败")
            self.notifier.notify_error(f"{symbol} 创建止损单失败，请手动设置止损！")
            # 先平仓、确认成功后再清扫疑似止损；若平仓失败，保留可能已经到达
            # 交易所的 reduce-only 止损，并把残仓写回普通持仓模型继续托管。
            rollback = self.exchange_api.close_position(ccxt_symbol, side, position_size)
            if rollback:
                logger.warning(f"{symbol} 已执行紧急回滚平仓，避免裸仓风险")
                if not self._cancel_stop_order_confirmed(symbol, ccxt_symbol, None):
                    logger.warning(f"{symbol} 回滚平仓后挂单清扫未确认，已标记残留")
            else:
                logger.critical(f"{symbol} 紧急回滚平仓未确认，保留疑似止损并继续托管残仓")
                self._retain_position_after_failed_rollback(
                    symbol, side, actual_price, position_size, stop_loss_price,
                    None, symbol_config.get('strategy', 'ma_cross'))
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
            strategy=symbol_config.get('strategy', 'ma_cross')
        ):
            return
        # 开仓成功即已重新入市：统一清除该品种的 T+1 重入待定标记（若有）。标记的唯一使命
        # 是「空仓时次日补回持仓」，入市即寿终——否则「止损→次日全新交叉直接开仓」等不经
        # 重入路径的成功入市会留下过期标记，几周后人工平仓的下一个日检会因这枚陈旧标记
        # 触发意外自动重入，把用户明确退出的仓位悄悄补回来。此处是所有开仓成功路径的
        # 单一收口（fresh-cross/T+1 重入/翻转反手/即时开仓），重入路径的显式清除改为 pop 兜底。
        self.clear_stop_loss(symbol)
        if buffer_notification:
            self._buffer_trade_open_notification(symbol, side, actual_price, position_size, stop_loss_price)
