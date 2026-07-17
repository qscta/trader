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
。
"""

import logging
import math
import uuid
from decimal import Decimal, InvalidOperation

from trade_state import TradeStatePersistenceError

logger = logging.getLogger(__name__)


def safe_fill_price(order, fallback):
    """从交易所结果读成交均价：读不出正有限数一律用调用方兜底价。

    average 偶发为垃圾字符串/NaN 时绝不能裸抛——开仓路径崩在“已成交、
    未挂止损”之间会留下裸仓窗口；NaN 滑过止损失效比较（NaN 比较恒 False）
    会跳过本该立即执行的回滚。模块级函数：api_server 手动平仓与本 mixin
    共用同一实现，不再各写一份。
    """
    value = order.get('average') if isinstance(order, dict) else None
    if isinstance(value, bool):
        value = None
    try:
        value = float(value) if value is not None else None
    except (TypeError, ValueError):
        value = None
    if value is None or not math.isfinite(value) or value <= 0:
        return fallback
    return value


class TradeExecutorMixin:

    _safe_fill_price = staticmethod(safe_fill_price)

    @staticmethod
    def _order_ids(order):
        if not isinstance(order, dict):
            return []
        values = order.get('ids') or ([order.get('id')] if order.get('id') else [])
        return [str(value) for value in values if value]

    @staticmethod
    def _extract_usdt_fee(order):
        """仅返回可直接计入 USDT 盈亏的交易所真实手续费。"""
        if not isinstance(order, dict) or order.get('execution_ambiguous'):
            return None, None
        fees = order.get('fees')
        if not isinstance(fees, list) or not fees:
            fee = order.get('fee')
            fees = [fee] if isinstance(fee, dict) else []
        if not fees:
            return None, None
        total = 0.0
        for fee in fees:
            if not isinstance(fee, dict) or str(fee.get('currency') or '').upper() != 'USDT':
                return None, None
            try:
                cost = float(fee.get('cost'))
            except (TypeError, ValueError):
                return None, None
            if not math.isfinite(cost) or cost < 0:
                return None, None
            total += cost
        return total, 'USDT'

    @staticmethod
    def _order_actual_amount(order, fallback):
        """读取适配层确认的实际成交币数；兼容未升级的测试桩/其他适配器。"""
        value = order.get('amount') if isinstance(order, dict) else None
        if value is None:
            return fallback
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _submit_persisted_close(self, symbol, ccxt_symbol, position, context):
        """先落盘 close intent，再用固定 clOrdId 执行/恢复主动平仓。"""
        prepare = getattr(self.trade_state, 'prepare_close_intent', None)
        if not callable(prepare):
            # 只为尚未升级的测试桩保留兼容；真实 TradeState 必有该原语。
            logger.warning(
                f'{symbol} 状态实现缺少 close intent（兼容路径）；请勿用于生产')
            return self.exchange_api.close_position(
                ccxt_symbol, position['side'], position['position_size'])
        candidate_id = f'C{uuid.uuid4().hex[:31]}'
        try:
            intent = prepare(symbol, candidate_id, context)
        except Exception as exc:
            logger.critical(
                f'{symbol} 无法在平仓 POST 前持久化 close intent: {exc}')
            self.notifier.notify_error(
                f'{symbol} 平仓意图无法落盘，已拒绝发单: {exc}')
            return None
        client_order_id = intent.get('client_order_id')
        planned = intent.get('planned_position_size')
        try:
            planned = float(planned)
            if not math.isfinite(planned) or planned <= 0:
                raise ValueError(planned)
        except (TypeError, ValueError):
            logger.critical(f'{symbol} close intent 计划量非法: {planned!r}')
            return None
        close_order = self.exchange_api.close_position(
            ccxt_symbol, intent.get('side') or position['side'], planned,
            client_order_id=client_order_id)
        if isinstance(close_order, dict):
            close_order['close_intent_client_id'] = client_order_id
            close_order['close_intent_context'] = intent.get('context')
        return close_order

    def _resume_persisted_close_intent(self, symbol, position, context):
        """在常规仓位对账前恢复主动平仓，返回 none/partial/closed/unresolved。"""
        getter = getattr(self.trade_state, 'get_close_intent', None)
        intent = getter(symbol) if callable(getter) else None
        if not intent:
            return 'none'
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        intent_context = intent.get('context') or context
        logger.warning(
            f'{symbol} {context}发现未收口 close intent '
            f"{intent.get('client_order_id')}，先恢复全部确定性平仓腿")
        try:
            close_order = self._submit_persisted_close(
                symbol, ccxt_symbol, position, intent_context)
        except Exception as exc:
            close_order = None
            logger.exception(f'{symbol} close intent 恢复异常: {exc}')
        if not close_order:
            quarantine = getattr(self, '_quarantine_position_mismatch', None)
            if callable(quarantine):
                quarantine(
                    symbol, f'{context} close intent 无法确认/继续',
                    {'client_order_id': intent.get('client_order_id'),
                     'intent_context': intent_context})
            return 'unresolved'
        if close_order.get('fully_closed') is False:
            saved = self._handle_partial_close(
                symbol, close_order, position,
                f'{context} close intent 恢复')
            return 'partial' if saved else 'unresolved'

        self._warn_ambiguous_close_execution(
            symbol, close_order, f'{context} close intent 恢复')
        stop_cleared = self._cancel_stop_order_confirmed(
            symbol, ccxt_symbol, position.get('stop_order_id'),
            position.get('extra_stop_order_ids'))
        actual_exit = (
            None if close_order.get('execution_ambiguous')
            else close_order.get('average'))
        try:
            actual_exit = float(actual_exit) if actual_exit is not None else None
        except (TypeError, ValueError):
            actual_exit = None
        if not actual_exit or not math.isfinite(actual_exit) or actual_exit <= 0:
            try:
                actual_exit = float(self.exchange_api.get_last_price(ccxt_symbol))
            except Exception:
                actual_exit = float(position['entry_price'])
        exit_fee, exit_fee_currency = self._extract_usdt_fee(close_order)
        closed, state_saved = self._close_trade_state_with_runtime_fallback(
            symbol, actual_exit, f'{context} close intent 恢复',
            exit_fee=exit_fee, exit_fee_currency=exit_fee_currency,
            exit_order_ids=self._order_ids(close_order),
            close_intent_client_id=intent.get('client_order_id'))
        if not closed or not state_saved:
            return 'unresolved'
        logger.critical(
            f'{symbol} {context}已按全部平仓腿恢复真实退出并原子消费 close intent')
        if not stop_cleared:
            logger.critical(
                f'{symbol} close intent 已收口，但旧止损撤销未确认；残留标记继续阻断开仓')
        return 'closed'

    def _compensation_close_client_id(self, open_order=None, fallback=None):
        open_client_id = None
        if isinstance(open_order, dict):
            open_client_id = (
                open_order.get('clientOrderId') or
                open_order.get('client_order_id'))
        open_client_id = open_client_id or fallback
        builder = getattr(
            self.exchange_api, 'compensation_client_order_id', None)
        if not open_client_id or not callable(builder):
            return None
        return builder(open_client_id)

    def _submit_compensation_close(
            self, ccxt_symbol, side, amount, open_order=None,
            open_client_order_id=None):
        """开仓回滚使用由持久化开仓 clOrdId 派生的固定平仓句柄。"""
        close_id = self._compensation_close_client_id(
            open_order, open_client_order_id)
        if close_id is None:
            if not callable(getattr(
                    self.exchange_api, 'compensation_client_order_id', None)):
                # 尚未升级的测试桩/非 OKX 兼容路径；真实 OKX 适配器不得走这里。
                logger.warning(
                    f'{ccxt_symbol} 交易所桩缺少补偿 clOrdId 派生能力（兼容路径）')
                return self.exchange_api.close_position(
                    ccxt_symbol, side, amount)
            logger.critical(
                f'{ccxt_symbol} 开仓结果缺少持久化 clOrdId，拒绝无句柄补偿 POST')
            return None
        return self.exchange_api.close_position(
            ccxt_symbol, side, amount, client_order_id=close_id)

    def _recover_flat_compensation_evidence(
            self, ccxt_symbol, side, amount, open_client_order_id):
        """已确认空仓时只读找回确定性补偿腿；绝不发送任何下单请求。

        历史实现复用可下单的 close_position() 当查询：确认空仓与查询之间
        若有人工开出同方向同数量仓位，reduce-only「查询」会真把人工仓平掉。
        """
        if not open_client_order_id:
            return None
        finder = getattr(
            self.exchange_api, 'find_compensation_close_evidence', None)
        if not callable(finder):
            # 未升级的适配器/测试桩没有只读找回能力；宁可让调用方用保守
            # 退出价兜底，也不允许退回可能真实下单的路径。
            logger.warning(
                f'{ccxt_symbol} 交易所适配器缺少只读补偿证据查找，按无证据处理')
            return None
        result = finder(ccxt_symbol, side, amount, open_client_order_id)
        if (not isinstance(result, dict) or
                result.get('execution_ambiguous') or
                result.get('fully_closed') is not True):
            return None
        return result

    def _reject_partial_close(self, symbol, close_order, context):
        """部分平仓不能删除完整账本，也不能撤掉仍在保护余仓的止损。"""
        if close_order and close_order.get('fully_closed') is False:
            actual = close_order.get('amount')
            msg = (f"{symbol} {context}仅部分成交（实际={actual}币），交易所仍有余仓；"
                   f"已保留本地仓位和止损，禁止反手，请立即复核")
            logger.critical(msg)
            mark_quarantine = getattr(self.trade_state, 'mark_position_quarantine', None)
            if callable(mark_quarantine):
                try:
                    mark_quarantine(symbol, f'{context}部分成交，等待原子缩减账本', {
                        'actual_closed_amount': actual,
                        'remaining_amount': close_order.get('remaining_amount'),
                        'order_ids': close_order.get('ids'),
                    })
                except Exception as e:
                    logger.critical(f"{symbol} 部分成交后的仓位隔离落盘失败: {e}")
            self.notifier.notify_error(msg)
            return True
        return False

    def _handle_partial_close(self, symbol, close_order, position, context):
        """把交易所部分平仓现实原子映射到余仓账本，并维持止损保护。"""
        if not close_order or close_order.get('fully_closed') is not False:
            return False
        try:
            local_size = float(position['position_size'])
            remaining = float(close_order.get('remaining_amount'))
        except (TypeError, ValueError, KeyError) as e:
            logger.critical(f'{symbol} {context}部分成交返回缺少可信余仓数量: {e}')
            self._reject_partial_close(symbol, close_order, context)
            return False
        if (not math.isfinite(remaining) or remaining <= 0 or
                remaining >= local_size):
            logger.critical(
                f'{symbol} {context}部分成交余仓异常: local={local_size}, remaining={remaining}')
            self._reject_partial_close(symbol, close_order, context)
            return False

        # 交易所余仓是权威值。用十进制字符串推导已平量，避免二进制 float
        # 连续做 current-remaining、再 current-closed 后把精确步长压低 1 ULP。
        try:
            closed_size = float(Decimal(str(local_size)) - Decimal(str(remaining)))
        except (InvalidOperation, ValueError) as e:
            logger.critical(f'{symbol} {context}部分成交数量无法做十进制收口: {e}')
            self._reject_partial_close(symbol, close_order, context)
            return False
        reported_closed = self._order_actual_amount(close_order, closed_size)
        tolerance = max(1e-12, math.ulp(local_size) * 8)
        if reported_closed is None or abs(reported_closed - closed_size) > tolerance:
            logger.critical(
                f'{symbol} {context}成交量与余仓不一致，按交易所余仓收口: '
                f'reported={reported_closed}, derived={closed_size}')

        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        exit_price = None if close_order.get('execution_ambiguous') else close_order.get('average')
        try:
            exit_price = float(exit_price) if exit_price is not None else None
        except (TypeError, ValueError):
            exit_price = None
        if not exit_price or not math.isfinite(exit_price) or exit_price <= 0:
            try:
                exit_price = float(self.exchange_api.get_last_price(ccxt_symbol))
            except Exception:
                exit_price = float(position['entry_price'])

        old_stop_id = position.get('stop_order_id')
        old_stop_size = float(position.get('stop_order_size') or local_size)
        # 新止损 POST 成功后、ID 落盘前也可能崩溃。先持久化未知残留标记；
        # 标记失败时保留旧 oversized reduce-only 止损，不冒险创建不可追踪新单。
        residue_marked = self._mark_possible_unknown_stop_residue(symbol)
        new_stop = None
        if residue_marked:
            new_stop = self.exchange_api.create_stop_loss_order(
                ccxt_symbol, position['side'], remaining,
                position['stop_loss_price'])
        # 上一轮撤销失败的旧止损仍可能活着；新保护确认后逐张验证式撤销所有
        # 已知旧 ID，任一失败都继续落账，绝不调用 cancel-all。
        extra_stop_ids = list(dict.fromkeys(
            str(value) for value in (position.get('extra_stop_order_ids') or []) if value))
        stop_resize_pending = False
        if new_stop and new_stop.get('id'):
            new_stop_id = new_stop['id']
            previous_ids = [old_stop_id] + extra_stop_ids
            previous_ids = [
                value for value in previous_ids
                if value and str(value) != str(new_stop_id)]
            extra_stop_ids = self._cancel_active_stop_ids_only(
                symbol, ccxt_symbol, previous_ids)
            stop_order_id = new_stop_id
            stop_order_size = remaining
            stop_resize_pending = bool(extra_stop_ids)
        else:
            # 旧单仍是 reduce-only，数量偏大但可完整保护余仓；账本明确记录其真实
            # 委托量，并让巡检继续“先挂新、后撤旧”重试缩量。
            # None 也可能表示 POST 已送达但确认查询不可见，必须留下未知单句柄。
            self._mark_possible_unknown_stop_residue(symbol)
            stop_order_id = old_stop_id
            stop_order_size = old_stop_size
            stop_resize_pending = True
            logger.critical(
                f'{symbol} {context}后余仓止损缩量失败；保留旧 reduce-only 止损并标记重试')

        exit_fee, exit_fee_currency = self._extract_usdt_fee(close_order)
        order_ids = self._order_ids(close_order)
        try:
            updated = self.trade_state.apply_partial_close(
                symbol, closed_size, exit_price,
                exit_fee=exit_fee, exit_fee_currency=exit_fee_currency,
                exit_order_ids=order_ids, new_stop_order_id=stop_order_id,
                remaining_size=remaining,
                stop_order_size=stop_order_size,
                extra_stop_order_ids=extra_stop_ids,
                stop_resize_pending=stop_resize_pending,
                close_intent_client_id=close_order.get(
                    'close_intent_client_id'))
            state_saved = True
        except TradeStatePersistenceError as e:
            try:
                updated = self.trade_state.force_runtime_apply_partial_close(
                    symbol, closed_size, exit_price,
                    exit_fee=exit_fee, exit_fee_currency=exit_fee_currency,
                    exit_order_ids=order_ids, new_stop_order_id=stop_order_id,
                    remaining_size=remaining,
                    stop_order_size=stop_order_size,
                    extra_stop_order_ids=extra_stop_ids,
                    stop_resize_pending=stop_resize_pending,
                    close_intent_client_id=close_order.get(
                        'close_intent_client_id'))
            except Exception as runtime_error:
                logger.critical(
                    f'{symbol} {context}部分成交连运行时账本也无法收口: {runtime_error}')
                self._reject_partial_close(symbol, close_order, context)
                return False
            state_saved = False
            self._notify_trade_state_persistence_issue(symbol, context, e)
        except Exception as e:
            logger.critical(f'{symbol} {context}部分成交账本收口失败: {e}')
            self._reject_partial_close(symbol, close_order, context)
            return False
        if not updated:
            self._reject_partial_close(symbol, close_order, context)
            return False

        msg = (f'{symbol} {context}仅部分成交：已平 {closed_size}币，余仓 {remaining}币；'
               f'账本已按现实缩减，余仓止损'
               f'{"等待缩量重试" if stop_resize_pending else "已重新挂妥"}')
        logger.critical(msg)
        self.notifier.notify_error(msg)
        # 磁盘提交失败时，force_runtime_* 只保证本进程内不再误删/反手；重启后仍需
        # 对账，因此不能向 API 谎报 safely_reconciled=True。
        return state_saved

    def _warn_ambiguous_close_execution(self, symbol, close_order, context):
        if close_order and close_order.get('execution_ambiguous'):
            msg = (f"{symbol} {context}时订单成交量与仓位变化不一致，疑似止损/人工成交并发；"
                   f"仓位已归零，但成交价和手续费无法完整归因，账本将使用保守回退价")
            logger.critical(msg)
            self.notifier.notify_error(msg)

    def _flip_position(self, symbol, signal, old_position, new_side, symbol_config):
        """双均线策略：翻转仓位（平旧开新）"""
        retired_from_pool = bool(symbol_config.get('_retired_from_pool'))
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)

        # 获取实时市价用于记录平仓价
        exit_price = signal['current_close']
        try:
            exit_price = self.exchange_api.get_last_price(ccxt_symbol)
            logger.info(f"{symbol} [双均线] 翻转使用实时市价: {exit_price}")
        except Exception as e:
            logger.warning(f"{symbol} [双均线] 获取实时市价失败({e})，使用收盘价: {exit_price}")

        close_order = self._submit_persisted_close(
            symbol, ccxt_symbol, old_position, '双均线翻转平仓')
        if not close_order:
            logger.error(f"{symbol} [双均线] 翻转平仓失败")
            self.notifier.notify_error(f"{symbol} [双均线] 翻转平仓失败")
            return
        if close_order.get('fully_closed') is False:
            self._handle_partial_close(
                symbol, close_order, old_position, "[双均线] 翻转平仓")
            return
        self._warn_ambiguous_close_execution(symbol, close_order, "[双均线] 翻转平仓")

        stop_cleared = self._cancel_stop_order_confirmed(symbol, ccxt_symbol, old_position.get('stop_order_id'))

        # 用平仓订单的实际成交价记录；读不出正有限数回退信号价
        actual_exit = self._safe_fill_price(close_order, exit_price)

        exit_fee, exit_fee_currency = self._extract_usdt_fee(close_order)
        closed_position, state_saved = self._close_trade_state_with_runtime_fallback(
            symbol, actual_exit, "双均线翻转平仓",
            exit_fee=exit_fee, exit_fee_currency=exit_fee_currency,
            exit_order_ids=self._order_ids(close_order),
            close_intent_client_id=close_order.get(
                'close_intent_client_id'))
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
            if not retired_from_pool:
                self.record_stop_loss(symbol)
                logger.error(f"{symbol} [双均线] 旧止损撤销不可确认，本轮不反手开仓；已记录 T+1，残留清理确认后次日按 EMA 方向重入")
            else:
                logger.error(
                    f"{symbol} [双均线] 退池仓已平，但旧止损撤销不可确认；"
                    "保留残留阻断，不记录 T+1、不再开仓")
            return

        if retired_from_pool:
            logger.info(
                f"{symbol} [双均线] 退池仓已按反向交叉平仓；"
                "按退池只平不开规则结束托管，不开反向新腿")
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
            # 平旧仓成功但反手开新腿失败（价格已穿止损/超时未确认/保证金不足等）：
            # 不把执行故障伪装成止损 T+1；保留本根 candle marker，交给当日
            # 08:01/30 分钟兜底按同一个最新交叉重试。跨日后不追补旧信号。
            self._mark_ma_cross_reentry_pending(
                symbol, new_side, signal,
                '双均线翻转反手开仓未成功；保留本根交叉等待日内重试，请复核交易所与日志')
            logger.error(f"{symbol} [双均线] 翻转反手开仓未成功，保留交叉等待日内重试")

    def _mark_open_rollback_quarantine(self, symbol, reason, details):
        """持久化隔离；磁盘故障时至少在本进程内保持 fail-closed。"""
        marker = getattr(self.trade_state, 'mark_position_quarantine', None)
        if callable(marker):
            try:
                marker(symbol, reason, details)
                return True
            except Exception as exc:
                logger.critical(f'{symbol} 紧急回滚隔离落盘失败: {exc}')
        force_marker = getattr(
            self.trade_state, 'force_runtime_mark_position_quarantine', None)
        if callable(force_marker):
            try:
                force_marker(symbol, reason, details)
            except Exception as exc:
                logger.critical(f'{symbol} 紧急回滚连运行时隔离也失败: {exc}')
        return False

    def _mark_possible_unknown_stop_residue(self, symbol):
        """创建结果不确定时阻断未来开仓；磁盘失败也保留运行时标记。"""
        try:
            self.trade_state.mark_stop_residue(symbol)
            return True
        except Exception as exc:
            logger.critical(f'{symbol} 未知止损残留标记落盘失败: {exc}')
        force = getattr(self.trade_state, 'force_runtime_mark_stop_residue', None)
        if callable(force):
            try:
                force(symbol)
            except Exception as exc:
                logger.critical(f'{symbol} 未知止损残留运行时标记也失败: {exc}')
        return False

    def _cancel_active_stop_ids_only(self, symbol, ccxt_symbol, order_ids):
        """持仓仍在时仅验证式撤指定旧止损，返回未能确认撤销的 ID。

        这里故意不调用通用 ``cancel_order``：其兼容 fallback 可能 cancel-all，
        会把 make-before-break 刚挂好的新保护一起撤掉。
        """
        ids = []
        for value in order_ids or []:
            if value and str(value) not in ids:
                ids.append(str(value))
        cancel_one = getattr(self.exchange_api, 'cancel_stop_order_only', None)
        if not callable(cancel_one):
            logger.critical(
                f'{symbol} 交易所适配器缺少“仅撤指定止损”能力，拒绝在持仓中清单')
            self._mark_possible_unknown_stop_residue(symbol)
            return ids
        uncleared = []
        for order_id in ids:
            try:
                cleared = bool(cancel_one(ccxt_symbol, order_id))
            except Exception as exc:
                logger.warning(f'{symbol} 指定止损 {order_id} 撤销异常: {exc}')
                cleared = False
            if not cleared:
                uncleared.append(order_id)
        if uncleared:
            self._mark_possible_unknown_stop_residue(symbol)
        return uncleared

    def _finalize_generic_rolled_back_outcome(
            self, symbol, open_intent, outcome):
        """普通 open_intent 已完整回滚时，立即用真实成交收口账目。"""
        if (not open_intent or not isinstance(outcome, dict) or
                outcome.get('status') != 'rolled_back'):
            return outcome
        finalizer = getattr(self, '_finalize_open_intent_rollback', None)
        if not callable(finalizer):
            return outcome
        try:
            if finalizer(symbol, open_intent, outcome):
                outcome['open_intent_finalized'] = True
            else:
                logger.critical(
                    f'{symbol} generic open intent 回滚结果未满足原子收口条件；'
                    '保留句柄等待恢复')
        except Exception as exc:
            logger.critical(
                f'{symbol} generic open intent 回滚账目即时收口失败: {exc}')
            self.notifier.notify_error(
                f'{symbol} 开仓已回滚但本地往返账目未落盘，已保留恢复句柄: {exc}')
        return outcome

    def _partial_rollback_exit_price(self, ccxt_symbol, rollback, entry_price):
        value = None if rollback.get('execution_ambiguous') else rollback.get('average')
        try:
            value = float(value) if value is not None else None
        except (TypeError, ValueError):
            value = None
        if value is not None and math.isfinite(value) and value > 0:
            return value
        try:
            value = float(self.exchange_api.get_last_price(ccxt_symbol))
        except Exception:
            value = float(entry_price)
        return value if math.isfinite(value) and value > 0 else float(entry_price)

    def _reconcile_partial_open_rollback(
            self, symbol, ccxt_symbol, side, entry_price, original_size,
            stop_loss_price, strategy, open_order, rollback, context,
            existing_stop_order_id=None, existing_stop_order_size=None,
            allow_stop_rebuild=True, stop_residue_possible=False,
            open_intent_client_id=None):
        """把“开仓后仅部分回滚”的真钱余仓建账、保护并隔离。

        交易所 ``remaining_amount`` 是余仓事实源。已有止损时保留其 oversized
        reduce-only 保护并让 guardian 稍后缩量；没有止损时立即尝试为余仓补挂。
        建账和 quarantine 同属一次 TradeState 事务，磁盘失败再退化为运行时账本。
        """
        try:
            original_size = float(original_size)
            remaining = float(rollback.get('remaining_amount'))
        except (TypeError, ValueError, AttributeError):
            return None, False, False
        if (not math.isfinite(original_size) or original_size <= 0 or
                not math.isfinite(remaining) or remaining <= 0 or
                remaining >= original_size):
            return None, False, False

        stop_order_id = existing_stop_order_id
        stop_order_size = (
            float(existing_stop_order_size)
            if existing_stop_order_size is not None else original_size)
        protected = bool(stop_order_id)
        if not protected and allow_stop_rebuild:
            try:
                emergency_stop = self.exchange_api.create_stop_loss_order(
                    ccxt_symbol, side, remaining, stop_loss_price)
            except Exception as exc:
                emergency_stop = None
                logger.critical(f'{symbol} 部分回滚余仓应急止损创建异常: {exc}')
            if emergency_stop and emergency_stop.get('id'):
                stop_order_id = str(emergency_stop['id'])
                stop_order_size = remaining
                protected = True

        entry_fee, entry_fee_currency = self._extract_usdt_fee(open_order)
        exit_fee, exit_fee_currency = self._extract_usdt_fee(rollback)
        entry_ids = self._order_ids(open_order)
        exit_ids = self._order_ids(rollback)
        exit_price = self._partial_rollback_exit_price(
            ccxt_symbol, rollback, entry_price)
        stop_resize_pending = (
            not protected or
            abs(stop_order_size - remaining) > max(1e-15, math.ulp(original_size) * 8))
        protection_label = (
            'existing_reduce_only' if existing_stop_order_id
            else 'rebuilt_reduce_only' if protected else 'missing')
        quarantine_reason = (
            f'{context}仅部分成交，余仓已建账；'
            f'{"止损待自动补挂" if not protected else "等待持仓/止损复核"}')
        quarantine_details = {
            'original_size': original_size,
            'remaining_amount': remaining,
            'open_order_ids': entry_ids,
            'rollback_order_ids': exit_ids,
            'stop_order_id': stop_order_id,
            'protection': protection_label,
        }
        kwargs = dict(
            symbol=symbol, side=side, entry_price=entry_price,
            original_size=original_size, remaining_size=remaining,
            stop_loss_price=stop_loss_price, partial_exit_price=exit_price,
            stop_order_id=stop_order_id, stop_order_size=stop_order_size,
            strategy=strategy,
            entry_fee=entry_fee, entry_fee_currency=entry_fee_currency,
            entry_order_ids=entry_ids,
            exit_fee=exit_fee, exit_fee_currency=exit_fee_currency,
            exit_order_ids=exit_ids,
            stop_resize_pending=stop_resize_pending,
            quarantine_reason=quarantine_reason,
            quarantine_details=quarantine_details,
            stop_residue_possible=stop_residue_possible,
            open_intent_client_id=open_intent_client_id,
        )
        try:
            updated = self.trade_state.add_open_after_partial_rollback(**kwargs)
            state_saved = True
        except TradeStatePersistenceError as exc:
            try:
                updated = self.trade_state.force_runtime_add_open_after_partial_rollback(
                    **kwargs)
            except Exception as runtime_exc:
                logger.critical(
                    f'{symbol} 部分回滚余仓连运行时账本也无法建立: {runtime_exc}')
                return None, False, protected
            state_saved = False
            notifier = getattr(self, '_notify_trade_state_persistence_issue', None)
            if callable(notifier):
                notifier(symbol, f'{context}部分回滚余仓建账', exc)
        except Exception as exc:
            logger.critical(f'{symbol} 部分回滚余仓账本建立失败: {exc}')
            return None, False, protected

        in_memory_t1 = getattr(self, 'stop_loss_dates', None)
        if isinstance(in_memory_t1, dict):
            in_memory_t1.pop(symbol, None)
        msg = (
            f'{symbol} {context}仅部分成交：交易所余仓={remaining}币，'
            f'账本已按实际余仓建立；'
            f'{"reduce-only 止损仍在/已重建" if protected else "应急止损无法建立"}，'
            '品种已隔离等待复核')
        logger.critical(msg)
        self.notifier.notify_error(msg)
        return updated, state_saved, protected

    def _reconcile_unclosed_open_rollback(
            self, symbol, ccxt_symbol, side, entry_price, remaining_size,
            stop_loss_price, strategy, open_order, context,
            existing_stop_order_id=None, existing_stop_order_size=None,
            allow_stop_rebuild=True, stop_residue_possible=False,
            open_intent_client_id=None):
        """补偿零成交时把完整真实仓位建账；不能硬套“部分平仓”结构。"""
        try:
            remaining = float(remaining_size)
        except (TypeError, ValueError):
            return None, False, False
        if not math.isfinite(remaining) or remaining <= 0:
            return None, False, False
        stop_order_id = existing_stop_order_id
        stop_order_size = (
            float(existing_stop_order_size)
            if existing_stop_order_size is not None else remaining)
        protected = bool(stop_order_id)
        if not protected and allow_stop_rebuild:
            try:
                emergency_stop = self.exchange_api.create_stop_loss_order(
                    ccxt_symbol, side, remaining, stop_loss_price)
            except Exception as exc:
                emergency_stop = None
                logger.critical(f'{symbol} 未决完整余仓应急止损创建异常: {exc}')
            if emergency_stop and emergency_stop.get('id'):
                stop_order_id = str(emergency_stop['id'])
                stop_order_size = remaining
                protected = True
        entry_fee, entry_fee_currency = self._extract_usdt_fee(open_order)
        details = {
            'remaining_amount': remaining,
            'open_order_ids': self._order_ids(open_order),
            'stop_order_id': stop_order_id,
            'protection': ('existing_reduce_only' if existing_stop_order_id
                           else 'rebuilt_reduce_only' if protected else 'missing'),
        }
        kwargs = dict(
            symbol=symbol, side=side, entry_price=entry_price,
            position_size=remaining, stop_loss_price=stop_loss_price,
            stop_order_id=stop_order_id, stop_order_size=stop_order_size,
            strategy=strategy, entry_fee=entry_fee,
            entry_fee_currency=entry_fee_currency,
            entry_order_ids=self._order_ids(open_order),
            stop_resize_pending=(
                not protected or abs(stop_order_size - remaining) >
                max(1e-15, math.ulp(remaining) * 8)),
            quarantine_reason=f'{context}未能成交，完整余仓已建账等待复核',
            quarantine_details=details,
            stop_residue_possible=stop_residue_possible,
            open_intent_client_id=open_intent_client_id,
        )
        try:
            updated = self.trade_state.add_untracked_open_position(**kwargs)
            state_saved = True
        except TradeStatePersistenceError as exc:
            try:
                updated = self.trade_state.force_runtime_add_untracked_open_position(
                    **kwargs)
            except Exception as runtime_exc:
                logger.critical(
                    f'{symbol} 未决完整余仓连运行时账本也无法建立: {runtime_exc}')
                return None, False, protected
            state_saved = False
            notifier = getattr(self, '_notify_trade_state_persistence_issue', None)
            if callable(notifier):
                notifier(symbol, f'{context}完整余仓建账', exc)
        except Exception as exc:
            logger.critical(f'{symbol} 未决完整余仓账本建立失败: {exc}')
            return None, False, protected
        in_memory_t1 = getattr(self, 'stop_loss_dates', None)
        if isinstance(in_memory_t1, dict):
            in_memory_t1.pop(symbol, None)
        msg = (
            f'{symbol} {context}未成交/不可确认：完整余仓 {remaining}币已建账；'
            f'{"reduce-only 止损仍在/已重建" if protected else "应急止损无法建立"}，'
            '品种已隔离等待复核')
        logger.critical(msg)
        self.notifier.notify_error(msg)
        return updated, state_saved, protected

    def _observe_exchange_position_amount(self, ccxt_symbol, side):
        """回滚结果丢失时直接读取当前净仓，并换回上层币数。"""
        position = self.exchange_api.get_position(ccxt_symbol)
        if not position:
            return 0.0
        observed_side = position.get('side')
        if observed_side and observed_side != side:
            raise RuntimeError(
                f'回滚复核发现方向反转: expected={side}, actual={observed_side}')
        contracts = position.get('contracts')
        if contracts is None:
            raise RuntimeError('回滚复核持仓缺少 contracts')
        converter = getattr(self.exchange_api, '_contracts_to_coins', None)
        if not callable(converter):
            raise RuntimeError('交易所适配层缺少 contracts→coins 换算')
        return float(converter(ccxt_symbol, abs(float(contracts))))

    def _finalize_open_rollback(
            self, symbol, ccxt_symbol, side, entry_price, original_size,
            stop_loss_price, strategy, open_order, rollback, context,
            existing_stop_order_id=None, existing_stop_order_size=None,
            allow_stop_rebuild=True, stop_residue_possible=False,
            open_intent_client_id=None):
        """统一裁决开仓后的补偿平仓，truthy 从不等于“已全平”。"""
        try:
            normalized_entry = float(entry_price)
        except (TypeError, ValueError):
            normalized_entry = None
        if (normalized_entry is None or not math.isfinite(normalized_entry)
                or normalized_entry <= 0):
            try:
                normalized_entry = float(self.exchange_api.get_last_price(ccxt_symbol))
            except Exception:
                normalized_entry = 0.0
        entry_price = normalized_entry
        if not rollback:
            try:
                observed_remaining = self._observe_exchange_position_amount(
                    ccxt_symbol, side)
            except Exception as exc:
                logger.critical(f'{symbol} 回滚结果丢失且持仓复核失败: {exc}')
            else:
                if observed_remaining <= 0:
                    try:
                        current_price = float(
                            self.exchange_api.get_last_price(ccxt_symbol))
                    except Exception:
                        current_price = float(stop_loss_price)
                    candidates = [float(stop_loss_price)]
                    if math.isfinite(current_price) and current_price > 0:
                        candidates.append(current_price)
                    conservative_exit = (
                        min(candidates) if side == 'long' else max(candidates))
                    rollback = {
                        'id': 'position-confirmed-flat', 'fully_closed': True,
                        'remaining_amount': 0.0, 'average': conservative_exit,
                        'execution_ambiguous': True,
                    }
                else:
                    rollback = {
                        'id': 'position-confirmed-open', 'fully_closed': False,
                        'remaining_amount': observed_remaining,
                        'amount': 0.0, 'execution_ambiguous': True,
                    }
        if rollback and rollback.get('fully_closed') is True:
            return {
                'status': 'rolled_back', 'open_order': open_order,
                'close_order': rollback, 'entry_price': entry_price,
                'position_size': original_size,
            }
        if rollback and rollback.get('fully_closed') is False:
            remaining = rollback.get('remaining_amount')
            try:
                remaining_value = float(remaining)
                original_value = float(original_size)
            except (TypeError, ValueError):
                remaining_value = original_value = None
            tolerance = (
                max(1e-15, math.ulp(max(abs(original_value), 1.0)) * 8)
                if original_value is not None and math.isfinite(original_value) else 1e-15)
            if (remaining_value is not None and original_value is not None and
                    math.isfinite(remaining_value) and remaining_value > 0 and
                    remaining_value >= original_value - tolerance):
                updated, state_saved, protected = self._reconcile_unclosed_open_rollback(
                    symbol, ccxt_symbol, side, entry_price, remaining_value,
                    stop_loss_price, strategy, open_order, context,
                    existing_stop_order_id=existing_stop_order_id,
                    existing_stop_order_size=existing_stop_order_size,
                    allow_stop_rebuild=allow_stop_rebuild,
                    stop_residue_possible=stop_residue_possible,
                    open_intent_client_id=open_intent_client_id)
            else:
                updated, state_saved, protected = self._reconcile_partial_open_rollback(
                    symbol, ccxt_symbol, side, entry_price, original_size,
                    stop_loss_price, strategy, open_order, rollback, context,
                    existing_stop_order_id=existing_stop_order_id,
                    existing_stop_order_size=existing_stop_order_size,
                    allow_stop_rebuild=allow_stop_rebuild,
                    stop_residue_possible=stop_residue_possible,
                    open_intent_client_id=open_intent_client_id)
            if updated:
                remaining = updated.get('position_size', remaining)
            else:
                details = {
                    'original_size': original_size,
                    'remaining_amount': remaining,
                    'open_order_ids': self._order_ids(open_order),
                    'rollback_order_ids': self._order_ids(rollback),
                    'existing_stop_order_id': existing_stop_order_id,
                }
                reason = f'{context}部分结果无法建立可信余仓账本'
                self._mark_open_rollback_quarantine(symbol, reason, details)
                msg = (f'{symbol} {reason}；交易结果保持未决并已隔离，'
                       '请立即核对交易所持仓与保护单')
                logger.critical(msg)
                self.notifier.notify_error(msg)
            return {
                'status': 'rollback_incomplete', 'open_order': open_order,
                'close_order': rollback, 'entry_price': entry_price,
                'position_size': remaining,
                'original_position_size': original_size,
                'residual_ledger_reconciled': bool(updated),
                'state_saved': state_saved,
                'residual_stop_protected': protected,
            }

        details = {
            'original_size': original_size,
            'open_order_ids': self._order_ids(open_order),
            'rollback_order_ids': self._order_ids(rollback),
            'existing_stop_order_id': existing_stop_order_id,
        }
        reason = f'{context}结果不可确认，无法证明交易所已归零'
        if stop_residue_possible:
            self._mark_possible_unknown_stop_residue(symbol)
        self._mark_open_rollback_quarantine(symbol, reason, details)
        msg = (f'{symbol} {reason}；'
               f'{"原 reduce-only 止损仍保留" if existing_stop_order_id else "当前无可证明的止损保护"}，'
               '已隔离并要求立即人工核对')
        logger.critical(msg)
        self.notifier.notify_error(msg)
        return {
            'status': 'rollback_incomplete', 'open_order': open_order,
            'close_order': rollback, 'entry_price': entry_price,
            'position_size': None,
            'original_position_size': original_size,
            'residual_ledger_reconciled': False,
            'state_saved': False,
            'residual_stop_protected': bool(existing_stop_order_id),
        }

    def _persist_open_position_or_rollback(self, symbol, ccxt_symbol, side,
                                           actual_price, position_size,
                                           stop_loss_price, stop_order_id,
                                           strategy=None, open_order=None,
                                           open_intent_client_id=None):
        try:
            entry_fee, entry_fee_currency = self._extract_usdt_fee(open_order)
            add_kwargs = {}
            if open_intent_client_id is not None:
                add_kwargs['open_intent_client_id'] = open_intent_client_id
            self.trade_state.add_open_position(
                symbol, side, actual_price, position_size, stop_loss_price,
                stop_order_id, strategy=strategy,
                entry_fee=entry_fee, entry_fee_currency=entry_fee_currency,
                entry_order_ids=self._order_ids(open_order), **add_kwargs)
            in_memory_t1 = getattr(self, 'stop_loss_dates', None)
            if isinstance(in_memory_t1, dict):
                in_memory_t1.pop(symbol, None)
            return True
        except (TradeStatePersistenceError, ValueError) as e:
            # ValueError＝账本入口拒绝了非法开仓数据（覆盖/NaN/非正数），
            # 与保存失败同责：成交已发生，必须交易所侧回滚而不是裸抛。
            logger.critical(
                f"{symbol} 开仓后本地状态落账被拒或保存失败；"
                f"保留现有止损保护并执行交易所侧回滚: {e}")
            # 先平、确认归零后再撤止损。旧顺序“先撤保护再平仓”在三腿仍部分
            # 成交时会留下既无账本又无止损的真钱裸仓。
            self._mark_possible_unknown_stop_residue(symbol)
            rollback = self._submit_compensation_close(
                ccxt_symbol, side, position_size, open_order,
                open_intent_client_id)
            outcome = self._finalize_open_rollback(
                symbol, ccxt_symbol, side, actual_price, position_size,
                stop_loss_price, strategy, open_order, rollback,
                '账本保存失败后的紧急回滚',
                existing_stop_order_id=stop_order_id,
                existing_stop_order_size=position_size,
                allow_stop_rebuild=False, stop_residue_possible=True,
                open_intent_client_id=open_intent_client_id)
            if outcome.get('status') == 'rolled_back':
                logger.warning(f"{symbol} 已执行状态失败后的回滚平仓，避免本地无记录的裸仓")
                if not self._cancel_stop_order_confirmed(
                        symbol, ccxt_symbol, stop_order_id):
                    logger.critical(f"{symbol} 全平后新止损撤销不可确认，已标记残留")
            elif outcome.get('residual_ledger_reconciled'):
                logger.critical(
                    f"{symbol} 状态失败后的回滚仅部分成交，余仓="
                    f"{outcome.get('position_size')}；原 reduce-only 止损与运行时账本均保留，"
                    "禁止把 truthy 订单误报为已回滚")
            else:
                logger.critical(
                    f"{symbol} 状态失败后的回滚平仓失败；原止损仍保留，请立即人工处理！")

            self.notifier.notify_error(
                f"{symbol} 开仓后本地状态保存失败，已在保留止损保护的前提下尝试平仓，"
                "请立即核对交易所持仓和本地状态"
            )
            return outcome

    def _execute_open(self, symbol, side, entry_price, stop_loss_price, symbol_config,
                      buffer_notification=True, client_order_id=None,
                      recover_pending_position=False):
        """通用开仓执行逻辑（使用实时市场价计算仓位，成交后校验风险）。

        buffer_notification=False 供即时开仓路由使用：该路由自己发专属钉钉，
        不走日检的汇总缓冲——否则消息滞留缓冲区，直到下次日检开头被静默清空。

        recover_pending_position=True 只能用于调用方已经确认“交易所有仓、本地无仓”
        且存在匹配 pending clOrdId 的崩溃恢复。它允许先查询旧订单，再按当前价
        决定补止损或立即 reduce-only 回滚；绝不表示可用过期信号新开仓。
        """
        retired = bool(
            symbol_config.get('_retired_from_pool') or
            symbol_config.get('enabled') is False)
        if retired and not recover_pending_position:
            logger.warning(
                f'{symbol} 已删除或禁用，通用开仓执行器按只平不开阻断新开仓')
            return {'status': 'retired_blocked'}

        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)

        pending_getter = getattr(self.trade_state, 'get_pending_signal_execution', None)
        pending_execution = pending_getter(symbol) if callable(pending_getter) else None
        if pending_execution:
            pending_client_id = pending_execution.get('client_order_id')
            if not client_order_id or str(client_order_id) != str(pending_client_id):
                msg = (f"{symbol} 存在未收口的幂等订单 {pending_client_id}，"
                       "阻断另一笔开仓；须先按原订单完成对账")
                logger.error(msg)
                self.notifier.notify_error(msg)
                return

        intent_getter = getattr(self.trade_state, 'get_open_intent', None)
        open_intent = intent_getter(symbol) if callable(intent_getter) else None
        if open_intent:
            intent_client_id = open_intent.get('client_order_id')
            if (client_order_id is None or
                    str(client_order_id) != str(intent_client_id)):
                msg = (f'{symbol} 存在未收口 open intent {intent_client_id}，'
                       '阻断另一笔开仓')
                logger.error(msg)
                self.notifier.notify_error(msg)
                return
        recovery_execution = pending_execution or open_intent

        quarantine_check = getattr(self.trade_state, 'is_position_quarantined', None)
        if callable(quarantine_check):
            try:
                quarantined = quarantine_check(symbol)
            except Exception as e:
                msg = f"{symbol} 无法读取仓位隔离状态，按 fail-closed 阻断开仓: {e}"
                logger.critical(msg)
                self.notifier.notify_error(msg)
                return
            if quarantined:
                pending_getter = getattr(self.trade_state, 'get_pending_signal_execution', None)
                pending = (
                    pending_getter(symbol, client_order_id)
                    if client_order_id and callable(pending_getter) else None)
                intent = (
                    intent_getter(symbol, client_order_id)
                    if client_order_id and callable(intent_getter) else None)
                if not pending and not intent:
                    msg = f"{symbol} 处于交易所/本地仓位不一致隔离状态，阻断本次开仓；请先完成对账并解除隔离"
                    logger.error(msg)
                    self.notifier.notify_error(msg)
                    return
                logger.warning(
                    f"{symbol} 仅允许用 pending clOrdId={client_order_id} 恢复中断交易；"
                    "适配层必须先查旧订单，不得新建另一单")

        # 止损残留阻断：该品种可能有撤销未确认的旧止损单，开新仓可能被残留单错杀
        if self.trade_state.has_stop_residue(symbol):
            msg = f"{symbol} 存在撤销未确认的止损单残留，阻断本次开仓；待自动清理确认或人工核对欧易委托后恢复"
            logger.error(msg)
            self.notifier.notify_error(msg)
            return

        # 普通新开仓在任何下单查询前即拒绝坏止损。崩溃恢复则必须先找回旧单/旧仓，
        # 不能因旧信号已经过期而把真实孤儿仓留在交易所；该标记会在成交确认后强平。
        recovery_requires_close = False
        signal_stop_invalid = (
            (side == 'long' and stop_loss_price >= entry_price) or
            (side == 'short' and stop_loss_price <= entry_price))
        if signal_stop_invalid:
            if not recover_pending_position:
                logger.error(
                    f"{symbol} 开仓中止: {side} 止损价({stop_loss_price})与"
                    f"入场价({entry_price})方向不合法")
                return
            recovery_requires_close = True
            logger.critical(
                f'{symbol} pending 恢复发现原始止损方向非法；先按旧 clOrdId '
                '确认真实成交，再立即回滚')

        # ====== 核心修复：用实时市场价替代信号收盘价计算仓位 ======
        calc_price = entry_price
        try:
            market_price = float(self.exchange_api.get_last_price(ccxt_symbol))
            if not math.isfinite(market_price) or market_price <= 0:
                raise ValueError(f'非法实时价 {market_price!r}')
            price_diff_pct = abs(market_price - entry_price) / entry_price * 100
            logger.info(f"{symbol} 实时市价={market_price}, 信号价={entry_price}, 偏差={price_diff_pct:.2f}%")
            calc_price = market_price  # 用实时价格计算仓位
        except Exception as e:
            if not recover_pending_position:
                logger.error(f"{symbol} 获取有限正实时市价失败({e})，拒绝新开仓")
                return
            # 恢复旧 clOrdId 不能因行情接口故障遗忘真实成交，但也不能在无法
            # 证明止损仍有效时继续持仓：查回旧单后强制 reduce-only 收口。
            recovery_requires_close = True
            logger.critical(
                f"{symbol} pending 恢复无法取得实时价({e})；将确认旧单后立即回滚")

        # 使用实际计算价再次校验止损方向，避免信号价过时导致危险开仓
        current_stop_invalid = (
            (side == 'long' and stop_loss_price >= calc_price) or
            (side == 'short' and stop_loss_price <= calc_price))
        if current_stop_invalid:
            if not recover_pending_position:
                logger.error(
                    f"{symbol} 开仓中止: {side} 止损价({stop_loss_price})与"
                    f"实时计算价({calc_price})方向不合法")
                return
            recovery_requires_close = True
            logger.critical(
                f'{symbol} pending 恢复时行情已越过原止损；先确认旧成交，再立即回滚')

        risk_per_trade = symbol_config.get('risk_per_trade', self.config['strategy']['default_risk_per_trade'])
        self.risk_manager.risk_per_trade = risk_per_trade
        logger.info(f"{symbol} 使用风险度: {risk_per_trade*100:.1f}%")

        if recover_pending_position:
            # 发单前 set_pending_signal_order_amount 已经与 pending 原子落盘。恢复真实
            # 孤儿仓时不能再依赖余额、风险公式或当前止损距离；这些外部/过期输入会
            # 阻断旧 clOrdId 查询。缺计划量意味着没有证据证明请求参数，按 fail-closed。
            planned = (recovery_execution or {}).get('planned_position_size')
            try:
                position_size = float(planned)
            except (TypeError, ValueError):
                logger.critical(
                    f'{symbol} pending 恢复缺少已固化 planned_position_size，拒绝猜测旧订单参数')
                return
            if not math.isfinite(position_size) or position_size <= 0:
                logger.critical(f'{symbol} pending 恢复的计划仓位非法: {planned!r}')
                return
            account_equity = float(getattr(self.risk_manager, 'account_equity', 0) or 0)
            if not math.isfinite(account_equity) or account_equity < 0:
                # 权益不可信时风险基准归零：成交后风险校验按“无基准”显式跳过，
                # 而不是让 NaN 在比较中静默吞掉这道防线。
                account_equity = 0.0
            raw_position_size = position_size
            price_risk_pct = (
                abs(calc_price - stop_loss_price) / calc_price if calc_price else 0)
            risk_amount = account_equity * risk_per_trade
            position_value = position_size * calc_price
            precision = 'pending-fixed'
        else:
            try:
                balance = self.exchange_api.get_balance()
                account_equity = float(
                    (balance.get('total') or {}).get('USDT'))
                if not math.isfinite(account_equity) or account_equity <= 0:
                    raise ValueError(f'非法 USDT 权益 {account_equity!r}')
            except Exception as e:
                logger.error(
                    f"{symbol} 本轮无法取得有限正 total.USDT({e})，"
                    "拒绝用启动时旧权益开仓")
                return
            self.risk_manager.account_equity = account_equity
            logger.info(f"已更新账户权益: {account_equity} USDT")

            raw_position_size = self.risk_manager.calculate_position_size(
                calc_price, stop_loss_price, risk_per_trade)
            if raw_position_size <= 0:
                logger.error(f"{symbol} 止损距离为0或无效，无法计算头寸")
                return

            price_risk_pct = abs(calc_price - stop_loss_price) / calc_price
            risk_amount = account_equity * risk_per_trade
            position_value = risk_amount / price_risk_pct

            # P3-1修复：使用动态精度舍入，不再硬编码round(..., 3)
            position_size = self.exchange_api.round_quantity(ccxt_symbol, raw_position_size)
            precision = self.exchange_api.get_quantity_precision(ccxt_symbol)

            # 确定性 clOrdId 首次发单前固化数量；普通重试也必须复用，不能随行情改变。
            if client_order_id and pending_execution:
                if pending_execution.get('planned_position_size') is not None:
                    position_size = float(pending_execution['planned_position_size'])
                else:
                    position_size = self.trade_state.set_pending_signal_order_amount(
                        symbol, client_order_id, position_size)
            elif client_order_id and open_intent:
                if open_intent.get('planned_position_size') is not None:
                    position_size = float(open_intent['planned_position_size'])
                else:
                    msg = (f'{symbol} open intent 缺少原子固化的计划量；该中间态'
                           '只能由启动/日检按“从未发单”收口，拒绝猜量下单')
                    logger.critical(msg)
                    self.notifier.notify_error(msg)
                    return

        logger.info(f"{symbol} 仓位计算: 权益={account_equity:.2f}, 风险度={risk_per_trade*100:.1f}%, "
                    f"风险金额={risk_amount:.2f}, 计算价={calc_price}, 信号价={entry_price}, 止损价={stop_loss_price}, "
                    f"价格风险%={price_risk_pct*100:.2f}%, 仓位价值={position_value:.2f}, "
                    f"原始数量={raw_position_size}, 精度={precision}, 最终数量={position_size}")

        if position_size <= 0:
            logger.error(f"{symbol} 头寸大小无效: {position_size}")
            return

        # 海龟已有 signal_execution；其余入口在任何 POST 前统一持久化
        # open intent + clOrdId + 固化数量，封住成交后记账前崩溃的孤儿仓窗口。
        if client_order_id is None:
            prepare_intent = getattr(self.trade_state, 'prepare_open_intent', None)
            if callable(prepare_intent):
                generated_client_id = f'I{uuid.uuid4().hex[:31]}'
                try:
                    open_intent = prepare_intent(
                        symbol, symbol_config.get('strategy', 'turtle'), side,
                        generated_client_id,
                        {'side': side, 'entry_price': float(entry_price),
                         'stop_loss_price': float(stop_loss_price)},
                        planned_position_size=position_size)
                    position_size = float(open_intent['planned_position_size'])
                    client_order_id = generated_client_id
                except Exception as exc:
                    logger.critical(
                        f'{symbol} 无法在发单前持久化 open intent: {exc}')
                    self.notifier.notify_error(
                        f'{symbol} 开仓意图无法落盘，已拒绝发单: {exc}')
                    return
        open_intent_client_id = (
            client_order_id if open_intent is not None else None)

        if client_order_id is None:
            open_order = self.exchange_api.open_position(ccxt_symbol, side, position_size)
        else:
            open_order = self.exchange_api.open_position(
                ccxt_symbol, side, position_size, client_order_id=client_order_id)
        if not open_order:
            logger.error(f"{symbol} 开仓失败")
            self.notifier.notify_error(f"{symbol} 开仓失败")
            return

        if open_order.get('open_execution_compensated'):
            compensation = open_order.get('compensation') or {}
            logger.warning(
                f"{symbol} 开仓未决，但内部 reduce-only 补偿已确认归零；"
                f"返回 rolled_back 供信号两阶段状态原子收口")
            outcome = {
                'status': 'rolled_back',
                'open_order': open_order,
                'close_order': compensation,
                'entry_price': open_order.get('average') or calc_price,
                'position_size': open_order.get('amount') or position_size,
            }
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, outcome)

        if open_order.get('open_order_may_remain_live'):
            details = {
                'client_order_id': open_order.get('clientOrderId'),
                'order_id': open_order.get('id'),
                'side': side, 'planned_position_size': position_size,
            }
            self._mark_open_rollback_quarantine(
                symbol, '开仓订单未证明终态，当前零仓仍可能迟到成交', details)
            msg = (
                f"{symbol} 开仓订单未证明终态，当前虽无仓仍可能迟到成交；"
                "已隔离，禁止再次开仓并等待按原 clOrdId 对账")
            logger.critical(msg)
            self.notifier.notify_error(msg)
            return {
                'status': 'order_unresolved', 'open_order': open_order,
                'position_size': 0.0, 'planned_position_size': position_size,
            }

        if open_order.get('open_execution_attribution_ambiguous'):
            details = {
                'client_order_id': open_order.get('clientOrderId'),
                'order_id': open_order.get('id'), 'side': side,
                'order_amount': open_order.get('amount'),
                'observed_position_amount': open_order.get(
                    'observed_position_amount', open_order.get('remaining_amount')),
            }
            self._mark_open_rollback_quarantine(
                symbol, '开仓订单成交与净仓变化无法唯一归因，拒绝自动平人工仓',
                details)
            msg = (
                f'{symbol} 开仓订单成交与交易所净仓变化不一致；可能混入人工同向仓。'
                '已隔离并保留原 clOrdId，未自动平掉整仓，请立即人工对账')
            logger.critical(msg)
            self.notifier.notify_error(msg)
            return {
                'status': 'attribution_unresolved', 'open_order': open_order,
                'position_size': open_order.get('observed_position_amount'),
                'planned_position_size': position_size,
            }

        if open_order.get('open_execution_unresolved'):
            remaining = open_order.get('remaining_amount')
            compensation = open_order.get('compensation')
            # 适配层即使无法确认补偿订单，也会返回最后一次成功观测的权威余仓。
            # 合成明确的 partial 契约，让统一收口器建账；绝不能只写 quarantine
            # 而继续把真钱余仓遗忘在 open_positions 之外。
            if isinstance(compensation, dict):
                rollback_contract = dict(compensation)
                # top-level unresolved 是最终契约，不能让 nested 曾经 fully_closed
                # 的补偿结果穿透并消费仍可能迟到成交的 open intent。
                rollback_contract['fully_closed'] = False
                rollback_contract['remaining_amount'] = remaining
            else:
                rollback_contract = {
                    'fully_closed': False,
                    'remaining_amount': remaining,
                    'execution_ambiguous': True,
                }
            original_size = self._order_actual_amount(open_order, position_size)
            if original_size is None:
                original_size = position_size
            outcome = self._finalize_open_rollback(
                symbol, ccxt_symbol, side,
                open_order.get('average') or calc_price,
                original_size, stop_loss_price,
                symbol_config.get('strategy', 'turtle'),
                open_order, rollback_contract, '未决开仓内部补偿',
                allow_stop_rebuild=not recovery_requires_close,
                open_intent_client_id=open_intent_client_id)
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, outcome)

        if open_order.get('confirmed') is False:
            logger.critical(f"{symbol} 开仓订单未获成交确认，拒绝挂止损/记账")
            self.notifier.notify_error(f"{symbol} 开仓订单未获成交确认，请立即核对交易所")
            return

        actual_position_size = self._order_actual_amount(open_order, position_size)
        if actual_position_size is None:
            logger.critical(f"{symbol} 开仓返回的实际成交数量无效，拒绝记账")
            self.notifier.notify_error(f"{symbol} 开仓实际成交数量无效，请立即核对交易所")
            return
        if open_order.get('execution_ambiguous'):
            logger.critical(f"{symbol} 开仓成交与仓位变化无法归因，按实际仓位执行回滚")
            rollback = self._submit_compensation_close(
                ccxt_symbol, side, actual_position_size, open_order,
                client_order_id)
            outcome = self._finalize_open_rollback(
                symbol, ccxt_symbol, side,
                open_order.get('average') or calc_price,
                actual_position_size, stop_loss_price,
                symbol_config.get('strategy', 'turtle'),
                open_order, rollback, '歧义开仓紧急回滚',
                open_intent_client_id=open_intent_client_id)
            self.notifier.notify_error(f"{symbol} 开仓成交存在外部并发歧义，已尝试回滚，请核对交易所")
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, outcome)
        if actual_position_size > position_size * (1 + 1e-9):
            logger.critical(
                f"{symbol} 实际成交量({actual_position_size})超过计划量({position_size})，"
                f"按实际量执行回滚")
            rollback = self._submit_compensation_close(
                ccxt_symbol, side, actual_position_size, open_order,
                client_order_id)
            outcome = self._finalize_open_rollback(
                symbol, ccxt_symbol, side,
                open_order.get('average') or calc_price,
                actual_position_size, stop_loss_price,
                symbol_config.get('strategy', 'turtle'),
                open_order, rollback, '超量开仓紧急回滚',
                open_intent_client_id=open_intent_client_id)
            self.notifier.notify_error(f"{symbol} 开仓超量成交，已尝试回滚，请核对交易所")
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, outcome)
        if actual_position_size < position_size * (1 - 1e-9):
            logger.warning(
                f"{symbol} 开仓部分成交: 计划={position_size}币, 实际={actual_position_size}币；"
                f"后续止损、风险与账本全部按实际量")
        position_size = actual_position_size

        actual_price = self._safe_fill_price(open_order, calc_price)
        if open_order.get('fee') is not None or open_order.get('fees'):
            logger.info(
                f"{symbol} 开仓真实手续费: fee={open_order.get('fee')}, fees={open_order.get('fees')}")

        # 成交后再次校验。恢复模式还要看“当前行情是否已越过旧止损”；此时不能
        # 尝试挂一张已失效的条件单，必须立即 reduce-only 收口旧仓。
        fill_stop_invalid = (
            (side == 'long' and stop_loss_price >= actual_price) or
            (side == 'short' and stop_loss_price <= actual_price))
        if recovery_requires_close or fill_stop_invalid:
            logger.error(
                f"{symbol} 开仓回滚: 止损价({stop_loss_price})已不再保护"
                f"成交/当前价(actual={actual_price}, current={calc_price})")
            self.notifier.notify_error(f"{symbol} 开仓恢复时止损已失效，正在立即回滚")
            rollback = self._submit_compensation_close(
                ccxt_symbol, side, position_size, open_order,
                client_order_id)
            outcome = self._finalize_open_rollback(
                symbol, ccxt_symbol, side, actual_price, position_size,
                stop_loss_price, symbol_config.get('strategy', 'turtle'),
                open_order, rollback, '止损失效后的紧急回滚',
                allow_stop_rebuild=False,
                open_intent_client_id=open_intent_client_id)
            if outcome.get('status') == 'rolled_back':
                logger.warning(f"{symbol} 已执行开仓后回滚平仓，避免无效止损")
            else:
                logger.critical(f"{symbol} 开仓后回滚未确认全平，请立即人工处理！")
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, outcome)

        stop_order = self.exchange_api.create_stop_loss_order(ccxt_symbol, side, position_size, stop_loss_price)
        if not stop_order:
            logger.error(f"{symbol} 创建止损单失败")
            self.notifier.notify_error(f"{symbol} 创建止损单失败，请手动设置止损！")
            # 返回 None 可能是“POST 已成功但确认查询不确定”。此时绝不能先
            # cancel_all 再平仓：三腿仍部分成交会留下裸余仓。保留一切可能的
            # reduce-only 保护，先回滚；仅明确全平后才安全清扫全部算法单。
            self._mark_possible_unknown_stop_residue(symbol)
            rollback = self._submit_compensation_close(
                ccxt_symbol, side, position_size, open_order,
                client_order_id)
            outcome = self._finalize_open_rollback(
                symbol, ccxt_symbol, side, actual_price, position_size,
                stop_loss_price, symbol_config.get('strategy', 'turtle'),
                open_order, rollback, '止损创建失败后的紧急回滚',
                stop_residue_possible=True,
                open_intent_client_id=open_intent_client_id)
            if outcome.get('status') == 'rolled_back':
                logger.warning(f"{symbol} 已执行紧急回滚平仓，避免裸仓风险")
                if not self._cancel_stop_order_confirmed(
                        symbol, ccxt_symbol, None):
                    logger.critical(
                        f"{symbol} 全平后未知挂单仍在可见性复验期，已阻断未来开仓")
            else:
                logger.critical(f"{symbol} 紧急回滚未确认全平，请立即人工处理！")
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, outcome)

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
                       f"已触发自动 reduce-only 回滚")
            logger.critical(warn_msg)
            self.notifier.notify_error(warn_msg)
            # 已知止损在回滚期间继续保护仓位；先落盘 cleanup intent，消除
            # “全平后、撤止损前”进程崩溃留下无 marker 条件单的窗口。
            self._mark_possible_unknown_stop_residue(symbol)
            rollback = self._submit_compensation_close(
                ccxt_symbol, side, position_size, open_order,
                client_order_id)
            outcome = self._finalize_open_rollback(
                symbol, ccxt_symbol, side, actual_price, position_size,
                stop_loss_price, symbol_config.get('strategy', 'turtle'),
                open_order, rollback, '成交后风险超标紧急回滚',
                existing_stop_order_id=stop_order_id,
                existing_stop_order_size=position_size,
                allow_stop_rebuild=False, stop_residue_possible=True,
                open_intent_client_id=open_intent_client_id)
            if outcome.get('status') == 'rolled_back':
                if not self._cancel_stop_order_confirmed(
                        symbol, ccxt_symbol, stop_order_id):
                    logger.critical(
                        f'{symbol} 风险超标回滚已全平，但止损清理未确认')
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, outcome)

        persist_result = self._persist_open_position_or_rollback(
            symbol, ccxt_symbol, side, actual_price, position_size, stop_loss_price, stop_order_id,
            strategy=symbol_config.get('strategy', 'turtle'), open_order=open_order,
            open_intent_client_id=open_intent_client_id
        )
        if persist_result is not True:
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, persist_result)
        if buffer_notification:
            self._buffer_trade_open_notification(symbol, side, actual_price, position_size, stop_loss_price)
        return {
            'status': 'opened', 'open_order': open_order,
            'entry_price': actual_price, 'position_size': position_size,
            'stop_order_id': stop_order_id,
        }


    def _update_stop_order(self, symbol, position, new_stop_loss_price):
        """以 make-before-break 顺序替换止损，任何中间态都至少有旧保护。"""
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        old_stop_loss_price = position['stop_loss_price']

        # 未知残留要求 cancel_all 才能验净；若此时先挂新止损，后续全量清扫会
        # 把刚挂的新保护一起撤掉，再把其 ID 误记为有效。残留只能先由 guardian
        # 通过完整算法单清单裁决，不能在止损推进路径里边挂边清。
        try:
            if self.trade_state.has_stop_residue(symbol):
                logger.warning(
                    f"{symbol} 仍有未知止损残留，保留现有保护并跳过本轮止损推进")
                return
        except Exception as e:
            msg = f"{symbol} 无法确认止损残留状态({e})，按 fail-closed 跳过止损推进"
            logger.critical(msg)
            self.notifier.notify_error(msg)
            return

        # 旧保护仍在，因此查询不确定时可以安全地跳过本次推进；不能为了更新
        # 止损而在未知仓位现实下再造一张可能成为孤儿的条件单。
        try:
            if self.exchange_api.get_position(ccxt_symbol) is None:
                logger.warning(f"{symbol} 止损推进前交易所已无持仓（可能已触发/人工平仓），"
                               f"不再挂新止损，交由盘中巡检/日检确认记平")
                return
        except Exception as e:
            logger.warning(f"{symbol} 止损推进前持仓复核失败({e})，保留旧止损并跳过本轮")
            return

        # 先确认新 reduce-only 止损已存在，再撤旧。若新单失败，旧单原封不动。
        stop_order = self.exchange_api.create_stop_loss_order(
            ccxt_symbol, position['side'], position['position_size'],
            new_stop_loss_price)
        if not stop_order:
            logger.error(f"{symbol} 创建新止损单失败，旧止损仍保留")
            # None 仍可能是“交易所已创建、确认暂不可见”；完整清单复核前
            # 不能假定绝未创建，否则未知新单会从后续清扫链路消失。
            self._mark_possible_unknown_stop_residue(symbol)
            self.notifier.notify_error(
                f"{symbol} 更新止损单失败，旧止损仍在；请检查！")
            return

        stop_order_id = stop_order.get('id')
        if not stop_order_id:
            self._mark_possible_unknown_stop_residue(symbol)
            self.notifier.notify_error(
                f'{symbol} 新止损已返回但缺少可追踪 ID，已隔离并保留旧止损')
            return

        previous_ids = []
        for value in ([position.get('stop_order_id')] +
                      list(position.get('extra_stop_order_ids') or [])):
            if value and str(value) != str(stop_order_id) and str(value) not in {
                    str(item) for item in previous_ids}:
                previous_ids.append(value)

        # 新单和所有旧 ID 先一起落盘。此后哪怕崩溃，guardian 也会看到多单
        # 歧义并保持 fail-closed，而不会遗忘其中任何一张。
        updated_position, first_saved = self._update_trade_state_stop_with_runtime_fallback(
            symbol, new_stop_loss_price, stop_order_id, "止损先挂后撤切换",
            stop_order_size=position['position_size'],
            extra_stop_order_ids=previous_ids,
            stop_resize_pending=bool(previous_ids))
        if not updated_position:
            return

        uncleared_ids = self._cancel_active_stop_ids_only(
            symbol, ccxt_symbol, previous_ids)
        if uncleared_ids:
            logger.error(
                f"{symbol} 新止损已生效，但旧止损撤销不可确认；已保留所有 ID 与残留阻断")
            self.notifier.notify_error(
                f'{symbol} 新止损已挂，但旧止损 {uncleared_ids} 撤销不可确认；'
                '已保留双重保护记录并隔离，未执行全量撤单')
            return

        # 旧单全部验净后再清理 extra IDs。落盘失败会保留运行时新保护并隔离，
        # 磁盘上的保守旧记录在重启时仍会由 guardian 重新裁决。
        updated_position, final_saved = self._update_trade_state_stop_with_runtime_fallback(
            symbol, new_stop_loss_price, stop_order_id, "止损切换收口",
            stop_order_size=position['position_size'], extra_stop_order_ids=[],
            notify_on_failure=first_saved)
        if not updated_position:
            return

        logger.info(f"{symbol} 止损单已更新: 新止损价={new_stop_loss_price}, 新止损单ID={stop_order_id}")
        if first_saved and final_saved:
            self._pending_stop_loss_updates.append({
                'symbol': symbol,
                'old_stop_loss_price': old_stop_loss_price,
                'new_stop_loss_price': new_stop_loss_price,
            })
