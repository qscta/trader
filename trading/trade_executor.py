"""下单执行子系统（TradingSystem 的 mixin）。

真钱下单的统一执行边界：通用开仓（止损残留阻断、实时市价计算仓位、双重
方向校验、成交后风险校验与回滚、挂止损失败回滚平仓）、止损单更新（验证式
撤旧→持仓复核→挂新）、平仓信号执行、双均线翻转（平旧开新）、开仓落盘
失败的交易所侧回滚。

以 mixin 形式承载：方法仍绑定在 TradingSystem 实例上——self 语义、
测试对实例方法的桩打法、调用链与日志行为全部不变，只做物理分层。
宿主须提供：exchange_api / trade_state / notifier / risk_manager / config /
record_stop_loss / _cancel_stop_order_confirmed /
_close_trade_state_with_runtime_fallback / _update_trade_state_stop_with_runtime_fallback /
_buffer_trade_open_notification / _buffer_trade_close_notification /
。
"""

import logging
import math
import time
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from config_validation import strict_float_finite
from runtime_guard import maintenance_sentinel_active, maintenance_sentinel_path
from trade_state import (
    TradeStateCommitDurabilityError,
    TradeStatePersistenceError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ConfirmedOpenRollbackContext:
    """``_execute_open`` 各确认成交分支共享的稳定回滚输入。"""

    symbol: str
    ccxt_symbol: str
    side: str
    stop_loss_price: float
    open_order: dict
    open_client_order_id: object
    open_intent_client_order_id: object
    requested_position_size: float


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

    def _maintenance_open_gate_status(self):
        """API 握手与真实开仓共用同一解析/裁决，不允许两套 sentinel 路径。"""
        path = maintenance_sentinel_path(
            base_dir=getattr(self, 'base_dir', None))
        return {
            'status': 'maintenance_blocked',
            'sentinel_path': path,
        } if maintenance_sentinel_active(path) else None

    @staticmethod
    def _order_ids(order):
        if not isinstance(order, dict):
            return []
        raw_ids = order.get('ids')
        values = [order.get('id')]
        if isinstance(raw_ids, list):
            values.extend(raw_ids)
        normalized = []
        for value in values:
            if isinstance(value, bool):
                continue
            if isinstance(value, str):
                value = value.strip()
            elif isinstance(value, int):
                value = str(value)
            else:
                continue
            if value and value not in normalized:
                normalized.append(value)
        return normalized

    def _authoritative_single_order_evidence(self, order):
        """资金账共用门：唯一订单 ID + 权威正均价 + 非歧义。"""
        if (not isinstance(order, dict) or
                order.get('execution_ambiguous') is True or
                order.get('financial_evidence_incomplete') is True):
            return None
        if order.get('ids') is not None and not isinstance(
                order.get('ids'), list):
            return None
        price = self._safe_fill_price(order, None)
        order_ids = self._order_ids(order)
        if price is None or len(order_ids) != 1:
            return None
        return {'price': float(price), 'order_ids': order_ids}

    def _normalize_compensation_close_progress(
            self, progress, *, expected_client_order_id,
            expected_contracts, expected_amount):
        """验证并归一只读补偿进度的完整二态契约。

        ``OkxApi.find_compensation_close_progress`` 只有两种合法返回：

        * ``absent=True``、``terminal=None``，且没有订单/成交；
        * ``absent=False``、``terminal=True``，且携带唯一终态订单。

        这里不查询交易所、不改写传入对象，只把经过交叉守恒验证的字段复制
        成编排层可消费的最小结构。任何缺字段、bool/NaN/负数、ID 冲突或
        presence/terminal/filled/order 自相矛盾都抛错，调用方必须保留 blocker。
        """
        def finite_number(value, field):
            if isinstance(value, bool):
                raise ValueError(f'{field} 不能是 bool')
            try:
                parsed = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f'{field} 不是有限非负数') from exc
            if not math.isfinite(parsed) or parsed < 0:
                raise ValueError(f'{field} 不是有限非负数')
            return parsed

        def close_enough(left, right):
            scale = max(abs(left), abs(right), 1.0)
            return abs(left - right) <= max(
                1e-12, math.ulp(scale) * 8)

        def public_order_id(value, field):
            if (not isinstance(value, str) or value != value.strip() or
                    not value or len(value) > 64 or not value.isascii() or
                    any(not (char.isalnum() or char in '_-')
                        for char in value)):
                raise ValueError(f'{field} 不是 canonical 单订单 ID')
            return value

        if (not isinstance(expected_client_order_id, str) or
                not 1 <= len(expected_client_order_id) <= 32 or
                not expected_client_order_id.isascii() or
                not expected_client_order_id.isalnum()):
            raise ValueError('确定性补偿 clOrdId 非法')
        expected_contracts = finite_number(
            expected_contracts, '预期补偿张数')
        expected_amount = finite_number(expected_amount, '预期补偿币数')
        if expected_contracts <= 0 or expected_amount <= 0:
            raise ValueError('预期补偿数量必须为正数')
        if not isinstance(progress, dict):
            raise ValueError('补偿订单进度响应不是对象')

        required = {
            'terminal', 'absent', 'confirmed', 'filled', 'amount',
            'requested_amount', 'remaining_amount', 'clientOrderId',
            'ids', 'read_only_evidence', 'order', 'order_state',
        }
        if not required.issubset(progress):
            raise ValueError('补偿订单进度缺少二态契约字段')
        if progress.get('read_only_evidence') is not True:
            raise ValueError('补偿订单进度缺少只读证据标记')
        if progress.get('clientOrderId') != expected_client_order_id:
            raise ValueError('补偿订单顶层 clOrdId 与确定性句柄冲突')
        if not isinstance(progress.get('ids'), list):
            raise ValueError('补偿订单顶层 ids 不是列表')

        order_state = progress.get('order_state')
        if not isinstance(order_state, dict):
            raise ValueError('补偿订单存在性证据不是对象')
        if order_state.get('client_order_id') != expected_client_order_id:
            raise ValueError('补偿订单状态 clOrdId 与确定性句柄冲突')
        presence = order_state.get('presence')
        if presence not in ('absent', 'present'):
            raise ValueError('补偿订单 presence 非法')

        absent = progress.get('absent')
        if absent is not (presence == 'absent'):
            raise ValueError('补偿订单 presence/absent 自相矛盾')
        terminal = progress.get('terminal')
        expected_terminal = None if presence == 'absent' else True
        if terminal is not expected_terminal:
            raise ValueError('补偿订单 presence/terminal 自相矛盾')
        if order_state.get('terminal') is not expected_terminal:
            raise ValueError('补偿订单状态 presence/terminal 自相矛盾')
        expected_confirmed = presence == 'present'
        if progress.get('confirmed') is not expected_confirmed:
            raise ValueError('补偿订单 presence/confirmed 自相矛盾')

        filled_contracts = finite_number(
            progress.get('filled'), '补偿订单顶层 filled')
        top_amount = finite_number(
            progress.get('amount'), '补偿订单顶层 amount')
        requested_amount = finite_number(
            progress.get('requested_amount'),
            '补偿订单顶层 requested_amount')
        remaining_amount = finite_number(
            progress.get('remaining_amount'),
            '补偿订单顶层 remaining_amount')
        if not close_enough(requested_amount, expected_amount):
            raise ValueError('补偿订单请求币数与调用参数冲突')
        if (filled_contracts > expected_contracts and
                not close_enough(filled_contracts, expected_contracts)):
            raise ValueError('补偿订单成交张数超过请求张数')
        if close_enough(filled_contracts, 0.0):
            filled_contracts = 0.0
        elif close_enough(filled_contracts, expected_contracts):
            filled_contracts = expected_contracts
        expected_filled_amount = (
            expected_amount * filled_contracts / expected_contracts)
        expected_remaining_amount = max(
            0.0, expected_amount - expected_filled_amount)
        if (not close_enough(top_amount, expected_filled_amount) or
                not close_enough(
                    remaining_amount, expected_remaining_amount)):
            raise ValueError('补偿订单顶层币数与成交张数不守恒')

        if presence == 'absent':
            if set(progress) != required:
                raise ValueError('absent 补偿订单携带契约外字段')
            if set(order_state) != {
                    'client_order_id', 'presence', 'terminal', 'filled'}:
                raise ValueError('absent 补偿订单状态字段非法')
            if order_state.get('filled') is not None:
                raise ValueError('absent 补偿订单不能携带成交量')
            if (filled_contracts != 0.0 or top_amount != 0.0 or
                    progress.get('order') is not None or
                    progress.get('ids') != []):
                raise ValueError('absent 补偿订单携带了订单/成交/终态状态')
            return {
                'presence': 'absent', 'absent': True, 'terminal': None,
                'client_order_id': expected_client_order_id,
                'filled_contracts': 0.0, 'filled_amount': 0.0,
                'remaining_contracts': expected_contracts,
                'remaining_amount': expected_amount,
                'order': None,
            }

        if set(order_state) != {
                'client_order_id', 'presence', 'terminal',
                'filled', 'remaining'}:
            raise ValueError('present 补偿订单状态字段非法')
        state_filled = finite_number(
            order_state.get('filled'), '补偿订单状态 filled')
        state_remaining = finite_number(
            order_state.get('remaining'), '补偿订单状态 remaining')
        expected_remaining_contracts = max(
            0.0, expected_contracts - filled_contracts)
        if (not close_enough(state_filled, filled_contracts) or
                not close_enough(
                    state_remaining, expected_remaining_contracts)):
            raise ValueError('补偿订单状态张数与顶层成交不守恒')

        order = progress.get('order')
        if not isinstance(order, dict):
            raise ValueError('present 补偿订单缺少 canonical 单订单')
        order_id = public_order_id(order.get('id'), '补偿订单 id')
        if order.get('clientOrderId') != expected_client_order_id:
            raise ValueError('补偿订单 clOrdId 与确定性句柄冲突')
        order_ids = order.get('ids')
        if order_ids is not None:
            if not isinstance(order_ids, list):
                raise ValueError('补偿订单 ids 不是列表')
            for value in order_ids:
                if public_order_id(value, '补偿订单 ids') != order_id:
                    raise ValueError('补偿订单包含多个/冲突订单 ID')
        info = order.get('info')
        if info is not None:
            if not isinstance(info, dict):
                raise ValueError('补偿订单 info 不是对象')
            if (info.get('clOrdId') not in (None, '') and
                    info.get('clOrdId') != expected_client_order_id):
                raise ValueError('补偿订单原生 clOrdId 与确定性句柄冲突')

        top_ids = [
            public_order_id(value, '补偿订单顶层 ids')
            for value in progress.get('ids')]
        expected_top_ids = [order_id] if filled_contracts > 0.0 else []
        if top_ids != expected_top_ids:
            raise ValueError('补偿订单顶层 ids 与 canonical 单订单冲突')
        if ('id' in progress and
                public_order_id(
                    progress.get('id'), '补偿订单顶层 id') != order_id):
            raise ValueError('补偿订单顶层 id 与 canonical 单订单冲突')
        if 'clientOrderIds' in progress:
            client_order_ids = progress.get('clientOrderIds')
            if (not isinstance(client_order_ids, list) or
                    client_order_ids != [expected_client_order_id]):
                raise ValueError('补偿订单顶层 clientOrderIds 与句柄冲突')
        order_filled_contracts = finite_number(
            order.get('filled_contracts'), '补偿订单 filled_contracts')
        order_filled_amount = finite_number(
            order.get('filled_amount'), '补偿订单 filled_amount')
        if (not close_enough(order_filled_contracts, filled_contracts) or
                not close_enough(
                    order_filled_amount, expected_filled_amount)):
            raise ValueError('补偿订单 order/filled_amount 与顶层成交冲突')

        for field, expected in (
                ('amount', expected_contracts),
                ('filled', filled_contracts),
                ('remaining', expected_remaining_contracts)):
            if field in order and order.get(field) not in (None, ''):
                value = finite_number(
                    order.get(field), f'补偿订单 order.{field}')
                if not close_enough(value, expected):
                    raise ValueError(
                        f'补偿订单 order.{field} 与进度契约冲突')

        raw_info = info or {}
        for field, expected in (
                ('sz', expected_contracts),
                ('accFillSz', filled_contracts)):
            if raw_info.get(field) not in (None, ''):
                value = finite_number(
                    raw_info.get(field), f'补偿订单 info.{field}')
                if not close_enough(value, expected):
                    raise ValueError(
                        f'补偿订单 info.{field} 与进度契约冲突')

        status_aliases = {
            'closed': 'filled', 'filled': 'filled',
            'canceled': 'canceled', 'cancelled': 'canceled',
            'mmp_canceled': 'canceled', 'mmp_cancelled': 'canceled',
            'rejected': 'rejected', 'expired': 'expired',
        }
        raw_statuses = [
            value for value in (
                order.get('status'), raw_info.get('state'),
                raw_info.get('ordState'))
            if value not in (None, '')]
        statuses = [
            status_aliases.get(str(value).lower())
            for value in raw_statuses]
        if (not statuses or any(value is None for value in statuses) or
                len(set(statuses)) != 1):
            raise ValueError('present 补偿订单缺少一致终态')
        if (statuses[0] == 'filled' and
                not close_enough(filled_contracts, expected_contracts)):
            raise ValueError('补偿订单 filled 状态与部分成交量冲突')
        expected_status = (
            'closed'
            if close_enough(filled_contracts, expected_contracts)
            else 'partial')
        if progress.get('status') != expected_status:
            raise ValueError('补偿订单顶层 status 与成交量冲突')
        fully_closed = expected_status == 'closed'
        if filled_contracts > 0.0:
            missing_flags = {
                'fully_filled', 'fully_closed'} - set(progress)
            if missing_flags:
                raise ValueError('正成交补偿订单缺少 fully_* 终态字段')
        for field in ('fully_filled', 'fully_closed'):
            if (field in progress and
                    progress.get(field) is not fully_closed):
                raise ValueError(f'补偿订单 {field} 与成交量冲突')

        return {
            'presence': 'present', 'absent': False, 'terminal': True,
            'client_order_id': expected_client_order_id,
            'filled_contracts': filled_contracts,
            'filled_amount': expected_filled_amount,
            'remaining_contracts': expected_remaining_contracts,
            'remaining_amount': expected_remaining_amount,
            'order': dict(order),
        }

    @staticmethod
    def _extract_usdt_fee(order):
        """仅返回可直接计入 USDT 盈亏的交易所真实手续费。"""
        if not isinstance(order, dict) or order.get('execution_ambiguous'):
            return None, None
        # 只有当前订单的手续费证据完整时才计为 exchange fee。
        fees_complete = order.get('fees_complete')
        if (fees_complete is not None and
                not isinstance(fees_complete, bool)):
            return None, None
        if fees_complete is False:
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
            if isinstance(fee.get('cost'), bool):
                return None, None
            try:
                cost = float(fee.get('cost'))
            except (TypeError, ValueError):
                return None, None
            if not math.isfinite(cost) or cost < 0:
                return None, None
            total += cost
            if not math.isfinite(total):
                return None, None
        return total, 'USDT'

    @staticmethod
    def _order_actual_amount(order, fallback):
        """读取正有限实际成交币数；可选 fallback 也走同一严格校验。"""
        value = order.get('amount') if isinstance(order, dict) else None
        if value is None:
            value = fallback
        if value is None or isinstance(value, bool):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    def _classify_close_execution(self, close_order):
        """只把适配层明确确认的两种终态交给资金账本消费者。"""
        if (not isinstance(close_order, dict) or
                close_order.get('confirmed') is not True or
                close_order.get('execution_ambiguous') is True):
            # 主动 close 与保护止损/人工操作并发时，净仓 delta
            # 不能被单一 close ID/average/fee 覆盖。不论 flat 还是
            # partial 都保留 intent+原止损+隔离，绝不制造伪财务。
            return 'unresolved'
        zero_fill_terminal = (
            close_order.get('zero_fill_terminal') is True and
            close_order.get('fully_closed') is False and
            not isinstance(close_order.get('amount'), bool) and
            close_order.get('amount') == 0.0)
        if close_order.get('exit_price_source') == 'estimated_stop':
            if (close_order.get('stop_triggered_before_post') is not True or
                    close_order.get('stop_execution_confirmed') is not True or
                    close_order.get('definitely_no_post') is not True or
                    self._safe_fill_price(close_order, None) is None or
                    self._order_ids(close_order)):
                return 'unresolved'
        elif (not zero_fill_terminal and
              self._authoritative_single_order_evidence(close_order) is None):
            return 'unresolved'
        remaining = close_order.get('remaining_amount')
        if remaining is None or isinstance(remaining, bool):
            return 'unresolved'
        try:
            remaining = float(remaining)
        except (TypeError, ValueError, OverflowError):
            return 'unresolved'
        if not math.isfinite(remaining) or remaining < 0:
            return 'unresolved'
        if close_order.get('fully_closed') is True:
            return 'closed' if remaining == 0 else 'unresolved'
        if close_order.get('fully_closed') is False:
            return 'partial' if remaining > 0 else 'unresolved'
        return 'unresolved'

    def _handle_unproven_close_execution(
            self, symbol, close_order, context):
        """保留仓位、止损和 close intent，并持久化隔离未证明的平仓回包。"""
        details = {
            'confirmed': (
                close_order.get('confirmed')
                if isinstance(close_order, dict) else None),
            'fully_closed': (
                close_order.get('fully_closed')
                if isinstance(close_order, dict) else None),
            'order_ids': (
                self._order_ids(close_order)
                if isinstance(close_order, dict) else []),
            'result_type': type(close_order).__name__,
        }
        self._quarantine_position_mismatch(
            symbol, f'{context}回包未严格证明成交终态', details)
        msg = (
            f'{symbol} {context}回包未同时满足 confirmed=true 与明确的 '
            'fully_closed 布尔终态；已保留仓位、止损和 close intent，禁止反手')
        logger.critical(msg)
        self.notifier.notify_error(msg)

    def _submit_persisted_close(self, symbol, ccxt_symbol, position, context):
        """先落盘 close intent，再用固定 clOrdId 执行/恢复主动平仓。"""
        candidate_id = f'C{uuid.uuid4().hex[:31]}'
        try:
            intent = self.trade_state.prepare_close_intent(
                symbol, candidate_id, context)
        except Exception as exc:
            logger.critical(
                f'{symbol} 无法在平仓 POST 前持久化 close intent: {exc}')
            self.notifier.notify_error(
                f'{symbol} 平仓意图无法落盘，已拒绝发单: {exc}')
            return None
        client_order_id = intent.get('client_order_id')
        created_in_this_call = client_order_id == candidate_id
        if intent.get('execution_model') != 'single_order_v1':
            logger.critical(
                f'{symbol} close intent 不是 single_order_v1，拒绝猜测'
                '未知执行语义；保留仓位/止损并要求人工收口')
            return None
        planned = intent.get('planned_position_size')
        try:
            planned = float(planned)
            if not math.isfinite(planned) or planned <= 0:
                raise ValueError(planned)
        except (TypeError, ValueError):
            logger.critical(f'{symbol} close intent 计划量非法: {planned!r}')
            return None
        if not created_in_this_call:
            logger.critical(
                f'{symbol} close intent 不是本调用刚刚原子创建；'
                '恢复阶段只读找回已有确定性订单，永不重放 POST')
        close_order = self.exchange_api.close_position(
            ccxt_symbol, intent.get('side') or position['side'], planned,
            client_order_id=client_order_id,
            require_existing=not created_in_this_call)
        if (isinstance(close_order, dict) and
                close_order.get('definitely_no_post') is True and
                close_order.get('close_order_absent') is True and
                close_order.get('position_unchanged') is True and
                close_order.get('amount') == 0.0 and
                close_order.get('remaining_amount') == planned):
            checker = getattr(
                self, '_pending_order_absence_is_conclusive', None)
            conclusive = False
            reason = '宿主缺少历史 absent 年龄门禁'
            if callable(checker):
                try:
                    conclusive, reason = checker(intent)
                except Exception as exc:
                    reason = f'历史 absent 年龄门禁异常: {exc}'
            if conclusive:
                try:
                    self.trade_state.resolve_zero_fill_close_intent(
                        symbol, client_order_id)
                except Exception as exc:
                    logger.critical(
                        f'{symbol} POST 前崩溃的 close intent 只读收口失败: '
                        f'{exc}')
                    return close_order
                logger.warning(
                    f'{symbol} close intent 在安全历史窗内经完整可见性查无，'
                    '且 fresh 仓位精确未变；只消费 intent，本调用结束')
                return {
                    'zero_fill_resolved': True,
                    'confirmed': True,
                    'definitely_no_post': True,
                    'clientOrderId': client_order_id,
                }
            logger.critical(
                f'{symbol} close intent 查无但 absent 不足以裁决（{reason}）；'
                '保留 intent，只允许继续维护精确匹配仓位的保护止损')
        if (isinstance(close_order, dict) and
                close_order.get('definitely_no_post') is True and
                close_order.get('position_flat_before_post') is True):
            stop_id = position.get('stop_order_id')
            stop_price = position.get('stop_loss_price')
            try:
                stop_size = float(
                    position.get('stop_order_size') or
                    position.get('position_size'))
                position_size = float(position.get('position_size'))
            except (TypeError, ValueError, OverflowError):
                stop_size = position_size = math.nan
            tolerance = max(
                1e-12,
                math.ulp(max(abs(planned), 1.0)) * 8)
            stop_matches = bool(
                stop_id and math.isfinite(stop_size) and
                math.isfinite(position_size) and
                abs(stop_size - planned) <= tolerance and
                abs(position_size - planned) <= tolerance)
            try:
                stop_executed = bool(
                    stop_matches and
                    self.exchange_api.confirm_stop_execution(
                        ccxt_symbol, intent.get('side') or position['side'],
                        planned, stop_price, stop_id))
                final_position = self.exchange_api.get_position(ccxt_symbol)
                final_flat = not self._exchange_position_has_contracts(
                    final_position)
            except Exception as exc:
                logger.critical(
                    f'{symbol} close POST 前 flat 的止损归因查询失败: '
                    f'{exc}')
                stop_executed = final_flat = False
            if stop_executed and final_flat:
                # 不把 algoId 伪装成普通 close order ID；原 position 的
                # stop_order_id 会保留在 closed trade，价格明确标 estimated_stop。
                close_order = {
                    'confirmed': True, 'fully_closed': True,
                    'fully_filled': True, 'remaining_amount': 0.0,
                    'amount': planned, 'requested_amount': planned,
                    'average': float(stop_price),
                    'exit_price_source': 'estimated_stop',
                    'stop_triggered_before_post': True,
                    'stop_execution_confirmed': True,
                    'definitely_no_post': True,
                }
            else:
                logger.critical(
                    f'{symbol} close POST 前已 flat，但无法用已知全量'
                    '保护止损唯一归因；保留 close intent/账本')
        if (isinstance(close_order, dict) and
                close_order.get('confirmed') is True and
                close_order.get('zero_fill_terminal') is True and
                close_order.get('amount') == 0.0 and
                close_order.get('remaining_amount') == planned):
            try:
                self.trade_state.resolve_zero_fill_close_intent(
                    symbol, client_order_id)
            except Exception as exc:
                logger.critical(
                    f'{symbol} 终态零成交 close intent 原子收口失败: '
                    f'{exc}')
                return close_order
            logger.warning(
                f'{symbol} 单笔平仓订单已终态零成交；只消费 intent，'
                '持仓/止损/财务未改，本次业务动作结束')
            return {
                'zero_fill_resolved': True,
                'confirmed': True,
                'clientOrderId': client_order_id,
            }
        if isinstance(close_order, dict):
            close_order['close_intent_client_id'] = client_order_id
            close_order['close_intent_context'] = intent.get('context')
        return close_order

    def _stop_trigger_close_date(self, symbol, *, retired_from_pool=False):
        """止损归因的平仓只给仍在启用池内的品种建立当日 T+1 门禁。"""
        if retired_from_pool:
            return None
        config = getattr(self, 'config', None)
        if isinstance(config, dict):
            pool = config.get('trading', {}).get('symbols', [])
            matched = next(
                (item for item in pool
                 if isinstance(item, dict) and item.get('name') == symbol),
                None)
            if matched is None or not matched.get('enabled', True):
                return None
        return date.today().strftime('%Y-%m-%d')

    def _sync_stop_trigger_date(self, symbol, stop_loss_date):
        """主账本事务完成后，仅同步旧的进程内 T+1 镜像。"""
        if stop_loss_date is None:
            return
        in_memory_dates = getattr(self, 'stop_loss_dates', None)
        if isinstance(in_memory_dates, dict):
            in_memory_dates[symbol] = stop_loss_date

    def _resume_persisted_close_intent(self, symbol, position, context):
        """在常规仓位对账前恢复主动平仓，返回 none/partial/closed/unresolved。"""
        intent = self.trade_state.get_close_intent(symbol)
        if not intent:
            return 'none'
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        intent_context = intent.get('context') or context
        logger.warning(
            f'{symbol} {context}发现未收口 close intent '
            f"{intent.get('client_order_id')}，先恢复该单笔确定性平仓订单")
        try:
            close_order = self._submit_persisted_close(
                symbol, ccxt_symbol, position, intent_context)
        except Exception as exc:
            close_order = None
            logger.exception(f'{symbol} close intent 恢复异常: {exc}')
        if not close_order:
            self._quarantine_position_mismatch(
                symbol, f'{context} close intent 无法确认/继续',
                {'client_order_id': intent.get('client_order_id'),
                 'intent_context': intent_context})
            self._protect_exact_position_during_unresolved_close(
                symbol, ccxt_symbol, position, context)
            return 'unresolved'
        if close_order.get('zero_fill_resolved') is True:
            return 'zero_fill_resolved'
        close_state = self._classify_close_execution(close_order)
        if close_state == 'partial':
            saved = self._handle_partial_close(
                symbol, close_order, position,
                f'{context} close intent 恢复')
            return 'partial' if saved else 'unresolved'
        if close_state != 'closed':
            self._handle_unproven_close_execution(
                symbol, close_order, f'{context} close intent 恢复')
            self._protect_exact_position_during_unresolved_close(
                symbol, ccxt_symbol, position, context)
            return 'unresolved'

        stop_cleared = self._cancel_stop_order_confirmed(
            symbol, ccxt_symbol, position.get('stop_order_id'),
            position.get('extra_stop_order_ids'))
        actual_exit = self._safe_fill_price(close_order, None)
        if actual_exit is None:
            self._handle_unproven_close_execution(
                symbol, close_order,
                f'{context} close intent 恢复缺少权威退出价')
            return 'unresolved'
        exit_fee, exit_fee_currency = self._extract_usdt_fee(close_order)
        stop_loss_date = (
            self._stop_trigger_close_date(symbol)
            if close_order.get('stop_triggered_before_post') is True else None)
        closed, state_saved = self._close_trade_state_with_runtime_fallback(
            symbol, actual_exit, f'{context} close intent 恢复',
            exit_fee=exit_fee, exit_fee_currency=exit_fee_currency,
            exit_order_ids=self._order_ids(close_order),
            stop_loss_date=stop_loss_date,
            close_intent_client_id=intent.get('client_order_id'),
            exit_price_source=close_order.get('exit_price_source'))
        if not closed or not state_saved:
            return 'unresolved'
        self._sync_stop_trigger_date(symbol, stop_loss_date)
        logger.critical(
            f'{symbol} {context}已按单笔平仓订单恢复真实退出并原子消费 close intent')
        if not stop_cleared:
            logger.critical(
                f'{symbol} close intent 已收口，但旧止损撤销未确认；残留标记继续阻断开仓')
        return 'closed'

    def _protect_exact_position_during_unresolved_close(
            self, symbol, ccxt_symbol, position, context):
        """close blocker 只能维护精确同一余仓的保护，绝不执行策略订单。"""
        ensure_stop = getattr(self, '_ensure_stop_order_alive', None)
        if not callable(ensure_stop):
            return False
        try:
            fresh = self.exchange_api.get_position(ccxt_symbol)
            if (not isinstance(fresh, dict) or
                    fresh.get('side') != position.get('side') or
                    isinstance(fresh.get('contracts'), bool) or
                    fresh.get('contracts') is None):
                return False
            fresh_amount = float(self.exchange_api._contracts_to_coins(
                ccxt_symbol, abs(float(fresh['contracts']))))
            expected = float(position.get('position_size'))
            tolerance = max(
                1e-12, math.ulp(max(abs(expected), 1.0)) * 8)
            if (not math.isfinite(fresh_amount) or
                    abs(fresh_amount - expected) > tolerance):
                return False
            return bool(ensure_stop(
                symbol, ccxt_symbol, position,
                f'{context} close intent 未决保护'))
        except Exception as exc:
            logger.critical(
                f'{symbol} close intent 未决期间保护维护失败: {exc}')
            return False

    def _compensation_close_client_id(self, open_order=None, fallback=None):
        open_client_id = None
        if isinstance(open_order, dict):
            open_client_id = (
                open_order.get('clientOrderId') or
                open_order.get('client_order_id'))
        open_client_id = open_client_id or fallback
        if not open_client_id:
            return None
        return self.exchange_api.compensation_client_order_id(open_client_id)

    def _submit_compensation_close(
            self, ccxt_symbol, side, amount, open_order=None,
            open_client_order_id=None):
        """开仓回滚使用由持久化开仓 clOrdId 派生的固定平仓句柄。"""
        close_id = self._compensation_close_client_id(
            open_order, open_client_order_id)
        if close_id is None:
            logger.critical(
                f'{ccxt_symbol} 开仓结果缺少持久化 clOrdId，拒绝无句柄补偿 POST')
            return None
        return self.exchange_api.close_position(
            ccxt_symbol, side, amount, client_order_id=close_id)

    def _recover_flat_compensation_evidence(
            self, ccxt_symbol, side, amount, open_client_order_id):
        """已确认空仓时只读找回确定性单笔补偿订单；绝不发送任何下单请求。

        历史实现复用可下单的 close_position() 当查询：确认空仓与查询之间
        若有人工开出同方向同数量仓位，reduce-only「查询」会真把人工仓平掉。
        """
        if not open_client_order_id:
            return None
        result = self.exchange_api.find_compensation_close_evidence(
            ccxt_symbol, side, amount, open_client_order_id)
        if (not isinstance(result, dict) or
                result.get('execution_ambiguous') or
                self._classify_close_execution(result) != 'closed'):
            return None
        return result

    def _reject_partial_close(self, symbol, close_order, context):
        """部分平仓不能删除完整账本，也不能撤掉仍在保护余仓的止损。"""
        if close_order and close_order.get('fully_closed') is False:
            actual = close_order.get('amount')
            msg = (f"{symbol} {context}仅部分成交（实际={actual}币），交易所仍有余仓；"
                   f"已保留本地仓位和止损，禁止反手，请立即复核")
            logger.critical(msg)
            self._mark_open_rollback_quarantine(
                symbol, f'{context}部分成交，等待原子缩减账本', {
                    'actual_closed_amount': actual,
                    'remaining_amount': close_order.get('remaining_amount'),
                    'order_ids': close_order.get('ids'),
                })
            self.notifier.notify_error(msg)
            return True
        return False

    def _handle_partial_close(self, symbol, close_order, position, context):
        """把交易所部分平仓现实原子映射到余仓账本，并维持止损保护。"""
        if not close_order or close_order.get('fully_closed') is not False:
            return False
        financial = self._authoritative_single_order_evidence(close_order)
        if financial is None:
            self._handle_unproven_close_execution(
                symbol, close_order, f'{context}部分成交财务证据不完整')
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
        exit_price = financial['price']

        old_stop_id = position.get('stop_order_id')
        old_stop_size = float(position.get('stop_order_size') or local_size)
        # 统一 make-before-write 边界会先持久化未知残留标记；标记失败或已有
        # 未裁决写入时保留旧 oversized reduce-only 止损，不创建不可追踪新单。
        new_stop = self._create_stop_with_write_intent(
            symbol, ccxt_symbol, position['side'], remaining,
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
            # None 也可能表示 POST 已送达但确认查询不可见；统一写边界已经
            # 留下未知单句柄，不能再次刷新其可见性起点。
            stop_order_id = old_stop_id
            stop_order_size = old_stop_size
            stop_resize_pending = True
            logger.critical(
                f'{symbol} {context}后余仓止损缩量失败；保留旧 reduce-only 止损并标记重试')

        exit_fee, exit_fee_currency = self._extract_usdt_fee(close_order)
        order_ids = financial['order_ids']
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
        except TradeStateCommitDurabilityError as e:
            # 主账本 rename 已提交；新内存已经包含本次缩仓。再次 force 会
            # 重复扣减，必须只读取专用异常携带的事务结果。
            updated = e.committed_result
            state_saved = False
            self._notify_trade_state_persistence_issue(symbol, context, e)
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
        if (state_saved and isinstance(new_stop, dict) and
                not stop_resize_pending and not extra_stop_ids):
            # 新 ID/余仓尺寸已耐久落账且全部旧单已验证撤销，方可消费写句柄。
            self._complete_stop_write(symbol, f'{context}部分平仓止损缩量')

        msg = (f'{symbol} {context}仅部分成交：已平 {closed_size}币，余仓 {remaining}币；'
               f'账本已按现实缩减，余仓止损'
               f'{"等待缩量重试" if stop_resize_pending else "已重新挂妥"}')
        logger.critical(msg)
        self.notifier.notify_error(msg)
        # 磁盘提交失败时，force_runtime_* 只保证本进程内不再误删/反手；重启后仍需
        # 对账，因此不能向 API 谎报 safely_reconciled=True。
        return state_saved

    def _flip_position(self, symbol, signal, old_position, new_side, symbol_config):
        """双均线策略：翻转仓位（平旧开新）"""
        retired_from_pool = bool(symbol_config.get('_retired_from_pool'))
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)

        close_order = self._submit_persisted_close(
            symbol, ccxt_symbol, old_position, '双均线翻转平仓')
        if not close_order:
            logger.error(f"{symbol} [双均线] 翻转平仓失败")
            self.notifier.notify_error(f"{symbol} [双均线] 翻转平仓失败")
            return
        if close_order.get('zero_fill_resolved') is True:
            logger.warning(
                f'{symbol} [双均线] 平仓单终态零成交；'
                '本调用不再建新 intent，交下一调度周期处理')
            return
        close_state = self._classify_close_execution(close_order)
        if close_state == 'partial':
            self._handle_partial_close(
                symbol, close_order, old_position, "[双均线] 翻转平仓")
            return
        if close_state != 'closed':
            self._handle_unproven_close_execution(
                symbol, close_order, '[双均线] 翻转平仓')
            return
        stop_cleared = self._cancel_stop_order_confirmed(symbol, ccxt_symbol, old_position.get('stop_order_id'))

        actual_exit = self._safe_fill_price(close_order, None)
        if actual_exit is None:
            self._handle_unproven_close_execution(
                symbol, close_order, '[双均线] 翻转平仓缺少权威退出价')
            return

        exit_fee, exit_fee_currency = self._extract_usdt_fee(close_order)
        stop_triggered_before_post = (
            close_order.get('stop_triggered_before_post') is True)
        stop_loss_date = (
            self._stop_trigger_close_date(
                symbol, retired_from_pool=retired_from_pool)
            if stop_triggered_before_post else None)
        closed_position, state_saved = self._close_trade_state_with_runtime_fallback(
            symbol, actual_exit, "双均线翻转平仓",
            exit_fee=exit_fee, exit_fee_currency=exit_fee_currency,
            exit_order_ids=self._order_ids(close_order),
            stop_loss_date=stop_loss_date,
            close_intent_client_id=close_order.get(
                'close_intent_client_id'),
            exit_price_source=close_order.get('exit_price_source'))
        if not closed_position:
            return
        pnl = round(closed_position['pnl'], 2)
        pnl_pct = round(closed_position['pnl_percent'], 2)
        logger.info(f"{symbol} [双均线] 翻转平仓成功: 出场价={actual_exit}, 盈亏={pnl}, 盈亏率={pnl_pct}%")
        self._buffer_trade_close_notification(symbol, old_position['side'], actual_exit, pnl, pnl_pct)

        if not state_saved:
            return
        self._sync_stop_trigger_date(symbol, stop_loss_date)

        if stop_triggered_before_post:
            logger.warning(
                f'{symbol} [双均线] 保护止损在主动平仓 POST 前已成交；'
                '已原子记录 T+1，本调用禁止反手开仓')
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
        try:
            self.trade_state.mark_position_quarantine(symbol, reason, details)
            return True
        except Exception as exc:
            logger.critical(f'{symbol} 紧急回滚隔离落盘失败: {exc}')
        try:
            self.trade_state.force_runtime_mark_position_quarantine(
                symbol, reason, details)
        except Exception as exc:
            logger.critical(f'{symbol} 紧急回滚连运行时隔离也失败: {exc}')
        return False

    def _mark_unresolved_open_execution(
            self, symbol, client_order_id, kind, expected_position_size,
            reason, details=None, compensation_client_order_id=None,
            protective_stop_order_id=None, protective_stop_order_size=None):
        """把未终态开仓/补偿句柄升级为有恢复责任人的一等生命周期 blocker。"""
        if compensation_client_order_id is None and kind == 'open_compensation':
            try:
                compensation_client_order_id = (
                    self.exchange_api.compensation_client_order_id(
                        client_order_id))
            except Exception as exc:
                logger.critical(
                    f'{symbol} 无法派生补偿平仓句柄，拒绝降级未决执行: {exc}')
                return False
        kwargs = dict(
            compensation_client_order_id=compensation_client_order_id,
            protective_stop_order_id=protective_stop_order_id,
            protective_stop_order_size=protective_stop_order_size,
            reason=reason, details=details)
        try:
            self.trade_state.mark_open_intent_unresolved_execution(
                symbol, client_order_id, kind, expected_position_size,
                **kwargs)
            return True
        except Exception as exc:
            logger.critical(f'{symbol} 未决执行 blocker 落盘失败: {exc}')
        try:
            self.trade_state.force_runtime_mark_open_intent_unresolved_execution(
                symbol, client_order_id, kind, expected_position_size,
                **kwargs)
        except Exception as exc:
            logger.critical(f'{symbol} 未决执行 blocker 运行时建立也失败: {exc}')
        return False

    def _mark_possible_unknown_stop_residue(self, symbol):
        """创建结果不确定时阻断未来开仓；磁盘失败也保留运行时标记。"""
        try:
            self.trade_state.mark_stop_residue(symbol)
            return True
        except Exception as exc:
            logger.critical(f'{symbol} 未知止损残留标记落盘失败: {exc}')
        try:
            self.trade_state.force_runtime_mark_stop_residue(symbol)
        except Exception as exc:
            logger.critical(f'{symbol} 未知止损残留运行时标记也失败: {exc}')
        return False

    def _begin_stop_write(self, symbol):
        """在任何止损 POST 前持久化唯一的 make-before-write 句柄。

        已有 marker 代表上一笔创建/撤销仍未完成权威裁决，绝不能刷新时间戳
        后继续发另一张单。读取或落盘不确定同样拒绝 POST；运行时 fallback
        只能保住当前进程，不能满足跨重启的真钱写入前提。
        """
        try:
            if self.trade_state.has_stop_residue(symbol):
                logger.warning(
                    f'{symbol} 已有未裁决止损写入句柄，拒绝再次创建算法单')
                return False
        except Exception as exc:
            logger.critical(
                f'{symbol} 无法读取止损写入句柄，按 fail-closed 拒绝 POST: {exc}')
            return False
        return self._mark_possible_unknown_stop_residue(symbol)

    def _create_stop_with_write_intent(
            self, symbol, ccxt_symbol, side, amount, stop_price, *,
            require_existing=False):
        """止损单的统一外部写边界：先落句柄，再查询/创建，结果严格归一。

        ``require_existing`` 只用于崩溃恢复：既有 marker 证明此前可能已 POST，
        因而只允许适配层跨可见性窗口找回同一确定性算法单，禁止再 POST。
        """
        if require_existing:
            try:
                if not self.trade_state.has_stop_residue(symbol):
                    logger.critical(
                        f'{symbol} 恢复止损缺少既有写入句柄，拒绝伪装成只读恢复')
                    return None
            except Exception as exc:
                logger.critical(
                    f'{symbol} 恢复止损无法读取写入句柄，拒绝查询/POST: {exc}')
                return None
        elif not self._begin_stop_write(symbol):
            return None
        try:
            stop_kwargs = (
                {'require_existing': True} if require_existing else {})
            order = self.exchange_api.create_stop_loss_order(
                ccxt_symbol, side, amount, stop_price, **stop_kwargs)
        except Exception as exc:
            logger.critical(
                f'{symbol} 止损创建/恢复结果不确定，保留写入句柄: {exc}')
            if not require_existing:
                self._record_unknown_stop_write_runtime_grace(symbol)
            return None
        if not isinstance(order, dict) or not order.get('id'):
            logger.critical(
                f'{symbol} 止损创建/恢复没有可信订单 ID，保留写入句柄')
            if not require_existing:
                self._record_unknown_stop_write_runtime_grace(symbol)
            return None
        return order

    def _record_unknown_stop_write_runtime_grace(self, symbol):
        """POST 返回不确定后，从「返回时刻」再等一整个巡检周期。"""
        grace = float(getattr(
            self, 'STOP_RESIDUE_VISIBILITY_GRACE_SECONDS', 5 * 60))
        deadlines = getattr(
            self, '_stop_residue_runtime_not_before', None)
        if not isinstance(deadlines, dict):
            deadlines = {}
            self._stop_residue_runtime_not_before = deadlines
        deadlines[symbol] = max(
            float(deadlines.get(symbol, 0.0)), time.monotonic() + grace)

    def _complete_stop_write(self, symbol, context):
        """仅由调用方在止损 ID/尺寸已持久化且旧单已验撤后消费句柄。"""
        try:
            self.trade_state.clear_stop_residue(symbol)
            deadlines = getattr(
                self, '_stop_residue_runtime_not_before', None)
            if isinstance(deadlines, dict):
                deadlines.pop(symbol, None)
            return True
        except Exception as exc:
            self._notify_trade_state_persistence_issue(
                symbol, f'{context}止损写入句柄收口', exc)
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
        uncleared = []
        for order_id in ids:
            try:
                cleared = bool(self.exchange_api.cancel_stop_order_only(
                    ccxt_symbol, order_id))
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
        try:
            if self._finalize_open_intent_rollback(
                    symbol, open_intent, outcome):
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
            open_intent_client_id=None, requested_position_size=None,
            preserve_open_intent=False):
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
        rebuilt_stop = False
        if not protected and allow_stop_rebuild:
            # 无论 POST 最终是否可确认，都要把未知算法单风险与余仓账本同事务
            # 保存；已有 marker 时统一边界会拒绝第二次 POST。
            stop_residue_possible = True
            emergency_stop = self._create_stop_with_write_intent(
                symbol, ccxt_symbol, side, remaining, stop_loss_price)
            if emergency_stop:
                stop_order_id = str(emergency_stop['id'])
                stop_order_size = remaining
                protected = True
                rebuilt_stop = True

        if not self._verify_fresh_position_for_open_ledger(
                symbol, ccxt_symbol, side, remaining,
                open_intent_client_id, context,
                unresolved_expected_amount=original_size,
                protective_stop_order_id=stop_order_id,
                protective_stop_order_size=(
                    stop_order_size if stop_order_id else None)):
            return None, False, protected

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
            'requested_position_size': requested_position_size,
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
            requested_position_size=requested_position_size,
            preserve_open_intent=preserve_open_intent,
        )
        try:
            updated = self.trade_state.add_open_after_partial_rollback(**kwargs)
            state_saved = True
        except TradeStateCommitDurabilityError as exc:
            updated = exc.committed_result
            state_saved = False
            self._notify_trade_state_persistence_issue(
                symbol, f'{context}部分回滚余仓建账', exc)
        except TradeStatePersistenceError as exc:
            try:
                updated = self.trade_state.force_runtime_add_open_after_partial_rollback(
                    **kwargs)
            except Exception as runtime_exc:
                logger.critical(
                    f'{symbol} 部分回滚余仓连运行时账本也无法建立: {runtime_exc}')
                return None, False, protected
            state_saved = False
            self._notify_trade_state_persistence_issue(
                symbol, f'{context}部分回滚余仓建账', exc)
        except Exception as exc:
            logger.critical(f'{symbol} 部分回滚余仓账本建立失败: {exc}')
            return None, False, protected

        if state_saved and rebuilt_stop:
            self._complete_stop_write(symbol, f'{context}部分回滚余仓保护')

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
            open_intent_client_id=None, requested_position_size=None,
            preserve_open_intent=False):
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
        rebuilt_stop = False
        if not protected and allow_stop_rebuild:
            stop_residue_possible = True
            emergency_stop = self._create_stop_with_write_intent(
                symbol, ccxt_symbol, side, remaining, stop_loss_price)
            if emergency_stop:
                stop_order_id = str(emergency_stop['id'])
                stop_order_size = remaining
                protected = True
                rebuilt_stop = True
        if not self._verify_fresh_position_for_open_ledger(
                symbol, ccxt_symbol, side, remaining,
                open_intent_client_id, context,
                unresolved_expected_amount=(
                    requested_position_size or remaining),
                protective_stop_order_id=stop_order_id,
                protective_stop_order_size=(
                    stop_order_size if stop_order_id else None)):
            return None, False, protected
        entry_fee, entry_fee_currency = self._extract_usdt_fee(open_order)
        details = {
            'requested_position_size': requested_position_size,
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
            requested_position_size=requested_position_size,
            preserve_open_intent=preserve_open_intent,
        )
        try:
            updated = self.trade_state.add_untracked_open_position(**kwargs)
            state_saved = True
        except TradeStateCommitDurabilityError as exc:
            updated = exc.committed_result
            state_saved = False
            self._notify_trade_state_persistence_issue(
                symbol, f'{context}完整余仓建账', exc)
        except TradeStatePersistenceError as exc:
            try:
                updated = self.trade_state.force_runtime_add_untracked_open_position(
                    **kwargs)
            except Exception as runtime_exc:
                logger.critical(
                    f'{symbol} 未决完整余仓连运行时账本也无法建立: {runtime_exc}')
                return None, False, protected
            state_saved = False
            self._notify_trade_state_persistence_issue(
                symbol, f'{context}完整余仓建账', exc)
        except Exception as exc:
            logger.critical(f'{symbol} 未决完整余仓账本建立失败: {exc}')
            return None, False, protected
        if state_saved and rebuilt_stop:
            self._complete_stop_write(symbol, f'{context}完整余仓保护')
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
        return float(self.exchange_api._contracts_to_coins(
            ccxt_symbol, abs(float(contracts))))

    def _verify_fresh_position_for_open_ledger(
            self, symbol, ccxt_symbol, side, expected_amount,
            open_intent_client_id, context, unresolved_expected_amount=None,
            protective_stop_order_id=None, protective_stop_order_size=None):
        """任何消费 open intent 的持仓落账紧前，严格复读 side+币数。"""
        try:
            fresh = self.exchange_api.get_position(ccxt_symbol)
            if not isinstance(fresh, dict):
                raise RuntimeError('fresh position 已为空或非对象')
            if fresh.get('side') != side:
                raise RuntimeError(
                    f'fresh side={fresh.get("side")!r}, expected={side!r}')
            raw_contracts = fresh.get('contracts')
            if isinstance(raw_contracts, bool) or raw_contracts is None:
                raise RuntimeError('fresh contracts 缺失/非法')
            contracts = abs(float(raw_contracts))
            if not math.isfinite(contracts) or contracts <= 0:
                raise RuntimeError(f'fresh contracts 非有限正数: {contracts!r}')
            fresh_amount = float(self.exchange_api._contracts_to_coins(
                ccxt_symbol, contracts))
            expected_amount = float(expected_amount)
            tolerance = max(
                1e-12, math.ulp(max(abs(expected_amount), 1.0)) * 8)
            if (not math.isfinite(fresh_amount) or
                    abs(fresh_amount - expected_amount) > tolerance):
                raise RuntimeError(
                    f'fresh amount={fresh_amount}, expected={expected_amount}')
            return True
        except Exception as exc:
            reason = f'{context}落账紧前持仓归因失败: {exc}'
            details = {
                'expected_side': side,
                'expected_amount': expected_amount,
                'open_client_order_id': open_intent_client_id,
            }
            marked = False
            if open_intent_client_id is not None:
                try:
                    intent = self.trade_state.get_open_intent(
                        symbol, open_intent_client_id) or {}
                    existing_unresolved = intent.get('unresolved_execution') or {}
                    marked = bool(
                        existing_unresolved.get('kind') == 'open_attribution' and
                        (protective_stop_order_id is None or
                         existing_unresolved.get('protective_stop_order_id') ==
                         str(protective_stop_order_id)))
                except Exception:
                    marked = False
                if not marked:
                    marked = self._mark_unresolved_open_execution(
                        symbol, open_intent_client_id, 'open_attribution',
                        (unresolved_expected_amount
                         if unresolved_expected_amount is not None
                         else expected_amount),
                        reason, details,
                        protective_stop_order_id=protective_stop_order_id,
                        protective_stop_order_size=protective_stop_order_size)
            if not marked:
                self._mark_open_rollback_quarantine(
                    symbol, reason, details)
            logger.critical(f'{symbol} {reason}；保留 lifecycle blocker，拒绝消费')
            return False

    def _finalize_open_rollback(
            self, symbol, ccxt_symbol, side, entry_price, original_size,
            stop_loss_price, strategy, open_order, rollback, context,
            existing_stop_order_id=None, existing_stop_order_size=None,
            allow_stop_rebuild=True, stop_residue_possible=False,
            open_intent_client_id=None, requested_position_size=None,
            preserve_open_intent=False,
            unresolved_execution_kind='open_compensation'):
        """统一裁决开仓后的补偿平仓，truthy 从不等于“已全平”。"""
        try:
            actual_open_size = float(original_size)
            planned_open_size = float(requested_position_size)
            overfill_tolerance = max(
                1e-15,
                math.ulp(max(abs(planned_open_size), 1.0)) * 8)
            attribution_ambiguous = bool(
                isinstance(open_order, dict) and
                open_order.get('execution_ambiguous') is True)
            overfilled = (
                math.isfinite(actual_open_size) and
                math.isfinite(planned_open_size) and
                actual_open_size > planned_open_size + overfill_tolerance)
        except (TypeError, ValueError, OverflowError):
            attribution_ambiguous = bool(
                isinstance(open_order, dict) and
                open_order.get('execution_ambiguous') is True)
            overfilled = False
        if attribution_ambiguous or overfilled:
            # 上层遗漏传 preserve 也不得把人工同向量/超计划量
            # 洗成系统财务仓。底层收口器自身强制升级归因 blocker。
            preserve_open_intent = True
            unresolved_execution_kind = 'open_attribution'
        rollback_evidence_supplied = bool(rollback)
        if (not rollback_evidence_supplied and open_intent_client_id is not None):
            preserve_open_intent = True
        unresolved_marked = False
        if preserve_open_intent and open_intent_client_id is not None:
            unresolved_marked = bool(self._mark_unresolved_open_execution(
                symbol, open_intent_client_id,
                unresolved_execution_kind, original_size,
                f'{context}的确定性执行终态尚未证明',
                {'open_client_order_id': open_intent_client_id,
                 'remaining_amount': (
                     rollback.get('remaining_amount')
                     if isinstance(rollback, dict) else None)}))
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
                    open_client_order_id = None
                    if isinstance(open_order, dict):
                        open_client_order_id = (
                            open_order.get('clientOrderId') or
                            open_order.get('client_order_id'))
                    open_client_order_id = (
                        open_client_order_id or open_intent_client_id)
                    try:
                        rollback = self._recover_flat_compensation_evidence(
                            ccxt_symbol, side, original_size,
                            open_client_order_id)
                    except Exception as exc:
                        logger.critical(
                            f'{symbol} 当前虽空仓，但确定性单笔补偿订单查询失败: {exc}')
                    if rollback:
                        try:
                            refreshed_remaining = (
                                self._observe_exchange_position_amount(
                                    ccxt_symbol, side))
                        except Exception as exc:
                            logger.critical(
                                f'{symbol} 补偿订单终态找到后无法再次证明 flat: {exc}')
                            rollback = None
                        else:
                            if refreshed_remaining > 0:
                                logger.critical(
                                    f'{symbol} 补偿订单终态找到后持仓已重新出现 '
                                    f'{refreshed_remaining} 币，拒绝消费 open intent')
                                rollback = None
                    if not rollback:
                        # 单次 flat 只证明此刻无仓，不能证明固定 clOrdId 已终态；
                        # 迟到 reduce-only 单仍可能在之后成交或与人工新仓交互。
                        rollback = {
                            'id': 'position-flat-order-unresolved',
                            'confirmed': False,
                            'remaining_amount': 0.0,
                            'execution_ambiguous': True,
                            'position_flat_observed': True,
                        }
                else:
                    rollback = {
                        'id': 'position-confirmed-open',
                        'confirmed': True, 'fully_closed': False,
                        'remaining_amount': observed_remaining,
                        'amount': 0.0, 'execution_ambiguous': True,
                    }
                    # 回滚 ACK 丢失时，fresh 同向余仓是风险事实，却不是
                    # 足以生成往返财务的证据。只有在持久 lifecycle 句柄、
                    # 原开仓实成交量与 fresh 数量可严格守恒时，才建立
                    # 无财务 provisional：不消费 intent，不产生
                    # partial_closes，也绝不再发第二笔补偿平仓。
                    intent = None
                    deterministic_provisional = False
                    if (unresolved_marked and
                            open_intent_client_id is not None):
                        try:
                            intent = self.trade_state.get_open_intent(
                                symbol, open_intent_client_id)
                            actual_value = float(original_size)
                            planned_value = float(
                                (intent or {}).get('planned_position_size'))
                            observed_value = float(observed_remaining)
                            requested_value = (
                                planned_value
                                if requested_position_size is None else
                                float(requested_position_size))
                            quantity_tolerance = max(
                                1e-12,
                                math.ulp(max(abs(planned_value), 1.0)) * 8)
                            unresolved = (intent or {}).get(
                                'unresolved_execution') or {}
                            deterministic_provisional = bool(
                                isinstance(intent, dict) and
                                intent.get('status') == 'pending' and
                                intent.get('client_order_id') ==
                                str(open_intent_client_id) and
                                intent.get('strategy') == strategy and
                                intent.get('side') == side and
                                unresolved.get('kind') ==
                                unresolved_execution_kind and
                                unresolved_execution_kind !=
                                'open_attribution' and
                                not attribution_ambiguous and
                                not overfilled and
                                all(
                                    math.isfinite(value) and value > 0
                                    for value in (
                                        actual_value, planned_value,
                                        observed_value, requested_value)) and
                                abs(requested_value - planned_value) <=
                                quantity_tolerance and
                                actual_value <= planned_value +
                                quantity_tolerance and
                                observed_value <= actual_value +
                                quantity_tolerance)
                        except Exception as exc:
                            logger.critical(
                                f'{symbol} 回滚响应丢失后无法验证 '
                                f'provisional 不变量: {exc}')
                    if deterministic_provisional and (
                            self._verify_fresh_position_for_open_ledger(
                                symbol, ccxt_symbol, side, observed_value,
                                open_intent_client_id,
                                f'{context}回滚响应丢失风险托管前',
                                unresolved_expected_amount=actual_value,
                                protective_stop_order_id=
                                existing_stop_order_id,
                                protective_stop_order_size=(
                                    existing_stop_order_size
                                    if existing_stop_order_id else None))):
                        updated, state_saved, protected = (
                            self._reconcile_unclosed_open_rollback(
                                symbol, ccxt_symbol, side, entry_price,
                                observed_value, stop_loss_price, strategy, {},
                                context,
                                existing_stop_order_id=existing_stop_order_id,
                                existing_stop_order_size=
                                existing_stop_order_size,
                                allow_stop_rebuild=allow_stop_rebuild,
                                stop_residue_possible=stop_residue_possible,
                                open_intent_client_id=
                                open_intent_client_id,
                                requested_position_size=planned_value,
                                preserve_open_intent=True))
                        if updated:
                            return {
                                'status': 'rollback_incomplete',
                                'open_order': open_order,
                                'close_order': None,
                                'entry_price': entry_price,
                                'position_size': observed_value,
                                'original_position_size': original_size,
                                'residual_ledger_reconciled': True,
                                'state_saved': state_saved,
                                'residual_stop_protected': protected,
                            }
        rollback_state = self._classify_close_execution(rollback)
        if rollback_state == 'closed':
            if (preserve_open_intent and
                    unresolved_execution_kind == 'open_attribution'):
                # 补偿平掉了当前净仓，也不能反向证明其中没有
                # 人工同向仓。保留 open_attribution 责任人，不伪造往返财务账。
                self._mark_open_rollback_quarantine(
                    symbol, f'{context}已平但开仓归因仍不可唯一拆分',
                    {'open_order_ids': self._order_ids(open_order),
                     'rollback_order_ids': self._order_ids(rollback),
                     'original_size': original_size})
                return {
                    'status': 'attribution_unresolved',
                    'open_order': open_order, 'close_order': rollback,
                    'entry_price': entry_price,
                    'position_size': 0.0,
                }
            return {
                'status': 'rolled_back', 'open_order': open_order,
                'close_order': rollback, 'entry_price': entry_price,
                'position_size': original_size,
            }
        if rollback_state == 'partial':
            remaining = rollback.get('remaining_amount')
            try:
                remaining_value = float(remaining)
                original_value = float(original_size)
            except (TypeError, ValueError):
                remaining_value = original_value = None
            tolerance = (
                max(1e-15, math.ulp(max(abs(original_value), 1.0)) * 8)
                if original_value is not None and math.isfinite(original_value) else 1e-15)
            if preserve_open_intent and remaining_value is not None:
                # 未终态执行期间只托管权威观测到的真钱余仓，不生成任何
                # partial_closes 财务事实；待原开仓/单笔补偿订单都终态后再用
                # VWAP/fee/order IDs 单事务重建真实账目。
                updated, state_saved, protected = self._reconcile_unclosed_open_rollback(
                    symbol, ccxt_symbol, side, entry_price, remaining_value,
                    stop_loss_price, strategy, {}, context,
                    existing_stop_order_id=existing_stop_order_id,
                    existing_stop_order_size=existing_stop_order_size,
                    allow_stop_rebuild=allow_stop_rebuild,
                    stop_residue_possible=stop_residue_possible,
                    open_intent_client_id=open_intent_client_id,
                    requested_position_size=requested_position_size,
                    preserve_open_intent=True)
            elif (remaining_value is not None and original_value is not None and
                    math.isfinite(remaining_value) and remaining_value > 0 and
                    remaining_value >= original_value - tolerance):
                updated, state_saved, protected = self._reconcile_unclosed_open_rollback(
                    symbol, ccxt_symbol, side, entry_price, remaining_value,
                    stop_loss_price, strategy, open_order, context,
                    existing_stop_order_id=existing_stop_order_id,
                    existing_stop_order_size=existing_stop_order_size,
                    allow_stop_rebuild=allow_stop_rebuild,
                    stop_residue_possible=stop_residue_possible,
                    open_intent_client_id=open_intent_client_id,
                    requested_position_size=requested_position_size,
                    preserve_open_intent=preserve_open_intent)
            else:
                updated, state_saved, protected = self._reconcile_partial_open_rollback(
                    symbol, ccxt_symbol, side, entry_price, original_size,
                    stop_loss_price, strategy, open_order, rollback, context,
                    existing_stop_order_id=existing_stop_order_id,
                    existing_stop_order_size=existing_stop_order_size,
                    allow_stop_rebuild=allow_stop_rebuild,
                    stop_residue_possible=stop_residue_possible,
                    open_intent_client_id=open_intent_client_id,
                    requested_position_size=requested_position_size,
                    preserve_open_intent=preserve_open_intent)
            if updated:
                remaining = updated.get('position_size', remaining)
            else:
                details = {
                    'requested_position_size': requested_position_size,
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
            'requested_position_size': requested_position_size,
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

    def _rollback_confirmed_open(
            self, rollback_context, entry_price, position_size, context,
            *, existing_stop_order_id=None, existing_stop_order_size=None,
            allow_stop_rebuild=True, stop_residue_possible=False,
            preserve_open_intent=False,
            unresolved_execution_kind='open_compensation'):
        """补偿确认成交并统一裁决；分支后置动作留在调用处以保持顺序。"""
        if preserve_open_intent:
            # 归因歧义是已知事实，必须在补偿 POST 前落盘；
            # 否则崩溃后只剩通用 open intent，会被错误恢复成纯系统仓。
            blocker_saved = self._mark_unresolved_open_execution(
                rollback_context.symbol,
                rollback_context.open_intent_client_order_id,
                unresolved_execution_kind, position_size,
                f'{context}存在无法唯一拆分的开仓归因',
                {'requested_position_size':
                 rollback_context.requested_position_size,
                 'observed_position_size': position_size})
            if not blocker_saved:
                logger.critical(
                    f'{rollback_context.symbol} 归因歧义 blocker 无法'
                    '建立，拒绝发送补偿平仓')
                return {
                    'status': 'attribution_unresolved',
                    'open_order': rollback_context.open_order,
                    'position_size': position_size,
                }
        rollback = self._submit_compensation_close(
            rollback_context.ccxt_symbol, rollback_context.side,
            position_size, rollback_context.open_order,
            rollback_context.open_client_order_id)
        return self._finalize_open_rollback(
            rollback_context.symbol, rollback_context.ccxt_symbol,
            rollback_context.side, entry_price, position_size,
            rollback_context.stop_loss_price, 'ma_cross',
            rollback_context.open_order, rollback, context,
            existing_stop_order_id=existing_stop_order_id,
            existing_stop_order_size=existing_stop_order_size,
            allow_stop_rebuild=allow_stop_rebuild,
            stop_residue_possible=stop_residue_possible,
            open_intent_client_id=rollback_context.open_intent_client_order_id,
            requested_position_size=rollback_context.requested_position_size,
            preserve_open_intent=preserve_open_intent,
            unresolved_execution_kind=unresolved_execution_kind)

    def _persist_open_position_or_rollback(self, symbol, ccxt_symbol, side,
                                           actual_price, position_size,
                                           stop_loss_price, stop_order_id,
                                           strategy='ma_cross', open_order=None,
                                           open_intent_client_id=None,
                                           requested_position_size=None):
        if not self._verify_fresh_position_for_open_ledger(
                symbol, ccxt_symbol, side, position_size,
                open_intent_client_id, '正常开仓保护确认后',
                unresolved_expected_amount=position_size,
                protective_stop_order_id=stop_order_id,
                protective_stop_order_size=(
                    position_size if stop_order_id else None)):
            return {
                'status': 'attribution_unresolved',
                'open_order': open_order,
                'entry_price': actual_price,
                'position_size': position_size,
            }
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
        except TradeStateCommitDurabilityError as exc:
            # 主账本 rename 已发生，且事务结果已经包含这笔真实持仓并消费
            # open intent。此时再发 reduce-only 补偿会把交易所仓位平掉，
            # 却把已提交的新仓留在内存/可见主文件中，制造双重现实。
            committed = exc.committed_result
            committed_matches = (
                isinstance(committed, dict) and
                committed.get('symbol') == symbol and
                committed.get('side') == side)
            if not committed_matches:
                logger.critical(
                    f'{symbol} 开仓账本报告已提交但缺少事务结果；'
                    '保持永久禁开仓且绝不重放补偿订单')
            in_memory_t1 = getattr(self, 'stop_loss_dates', None)
            if isinstance(in_memory_t1, dict):
                in_memory_t1.pop(symbol, None)
            self._notify_trade_state_persistence_issue(
                symbol, '开仓持仓落账', exc)
            if not committed_matches:
                # 专用异常契约本身若被破坏，也只能向上失败并等待人工核对；
                # 绝不能掉进下面会发送外部补偿单的普通失败分支。
                raise TradeStatePersistenceError(
                    f'{symbol} 已提交开仓缺少可信 committed_result') from exc
            # 交易所仓、已知 reduce-only 止损和当前进程账本三者一致；上层
            # 仍须完成止损写入句柄收口并报告 opened。永久 persistence latch
            # 会在中央边界阻断本进程余下生命期的任何新开仓。
            return True
        except Exception as e:
            # ValueError＝账本入口拒绝了非法开仓数据（覆盖/NaN/非正数），
            # 与保存失败同责：成交已发生，必须交易所侧回滚而不是裸抛。
            logger.critical(
                f"{symbol} 开仓后本地状态落账被拒或保存失败；"
                f"保留现有止损保护并执行交易所侧回滚: {e}")
            # 先平、确认归零后再撤止损。旧顺序“先撤保护再平仓”在订单部分
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
                open_intent_client_id=open_intent_client_id,
                requested_position_size=requested_position_size)
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
                      buffer_notification=True):
        """通用开仓执行逻辑（使用实时市场价计算仓位，成交后校验风险）。

        buffer_notification=False 供即时开仓路由使用：该路由自己发专属钉钉，
        不走日检的汇总缓冲——否则消息滞留缓冲区，直到下次日检开头被静默清空。

        本边界只处理本调用新建的 open intent。崩溃恢复由 main 的
        只读 lifecycle 裁决器处理，不得重入本方法回放旧信号。
        """
        maintenance_gate = self._maintenance_open_gate_status()
        if maintenance_gate is not None:
            no_open_path = maintenance_gate.get('sentinel_path')
            logger.warning(
                f'{symbol} 部署禁开仓哨兵存在、路径非法或状态不可确认 '
                f'({no_open_path!r})，按 fail-closed 阻断任何新开仓')
            return {'status': 'maintenance_blocked'}

        # 目录 fsync 失败后的主账本虽然当前可见，但掉电后可能回退；该门闩
        # 运行中不可清除。所有开仓入口必须在任何交易所 I/O 前统一拒绝。
        try:
            persistence = self.trade_state.get_runtime_persistence_status()
            if (not isinstance(persistence, dict) or
                    set(persistence) != {'degraded', 'context'} or
                    not isinstance(persistence.get('degraded'), bool) or
                    persistence.get('degraded')):
                logger.critical(
                    f'{symbol} 持久化健康状态降级或不可验证，中央边界禁开仓: '
                    f'{persistence!r}')
                return {'status': 'state_blocked'}
        except Exception as exc:
            logger.critical(
                f'{symbol} 无法读取持久化健康状态，中央边界禁开仓: {exc}')
            return {'status': 'state_blocked'}

        # T+1 是系统级风险约束，不属于某个信号入口。直接读命脉账本，防止
        # 即时开仓或未来新入口绕过当天止损标记；没有隐式人工 override。
        try:
            stop_loss_dates = self.trade_state.get_stop_loss_dates()
            if not isinstance(stop_loss_dates, dict):
                raise TypeError('stop_loss_dates 不是对象')
            stopped_on = stop_loss_dates.get(symbol)
        except Exception as exc:
            logger.critical(
                f'{symbol} 无法读取 T+1 状态，中央边界禁开仓: {exc}')
            return {'status': 'state_blocked'}
        if stopped_on == date.today().isoformat():
            logger.warning(f'{symbol} 当天已有止损记录，中央边界执行 T+1 禁开仓')
            return {'status': 't1_blocked'}

        retired = bool(
            symbol_config.get('_retired_from_pool') or
            symbol_config.get('enabled') is False)
        if retired:
            logger.warning(
                f'{symbol} 已删除或禁用，通用开仓执行器按只平不开阻断新开仓')
            return {'status': 'retired_blocked'}

        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)

        try:
            open_intent = self.trade_state.get_open_intent(symbol)
            quarantined = self.trade_state.is_position_quarantined(symbol)
        except Exception as exc:
            msg = f'{symbol} 无法读取开仓 intent/隔离状态，按 fail-closed 阻断: {exc}'
            logger.critical(msg)
            self.notifier.notify_error(msg)
            return {'status': 'state_blocked'}
        if open_intent:
            intent_client_id = open_intent.get('client_order_id')
            msg = (f'{symbol} 存在未收口 open intent {intent_client_id}，'
                   '新开仓边界绝不恢复/重放旧请求')
            logger.error(msg)
            self.notifier.notify_error(msg)
            return {'status': 'state_blocked'}

        if quarantined:
            msg = f"{symbol} 处于交易所/本地仓位不一致隔离状态，阻断本次开仓；请先完成对账并解除隔离"
            logger.error(msg)
            self.notifier.notify_error(msg)
            return {'status': 'state_blocked'}

        # 止损残留阻断：新开仓绝不能越过旧 marker。
        try:
            stop_residue_present = self.trade_state.has_stop_residue(symbol)
        except Exception as exc:
            logger.critical(
                f'{symbol} 无法读取止损残留状态，按 fail-closed 阻断: {exc}')
            return {'status': 'state_blocked'}
        if stop_residue_present:
            msg = f"{symbol} 存在撤销未确认的止损单残留，阻断本次开仓；待自动清理确认或人工核对欧易委托后恢复"
            logger.error(msg)
            self.notifier.notify_error(msg)
            return
        # 新开仓在任何下单查询前即拒绝坏止损。
        signal_stop_invalid = (
            (side == 'long' and stop_loss_price >= entry_price) or
            (side == 'short' and stop_loss_price <= entry_price))
        if signal_stop_invalid:
            logger.error(
                f"{symbol} 开仓中止: {side} 止损价({stop_loss_price})与"
                f"入场价({entry_price})方向不合法")
            return

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
            logger.error(f"{symbol} 获取有限正实时市价失败({e})，拒绝新开仓")
            return

        # 使用实际计算价再次校验止损方向，避免信号价过时导致危险开仓
        current_stop_invalid = (
            (side == 'long' and stop_loss_price >= calc_price) or
            (side == 'short' and stop_loss_price <= calc_price))
        if current_stop_invalid:
            logger.error(
                f"{symbol} 开仓中止: {side} 止损价({stop_loss_price})与"
                f"实时计算价({calc_price})方向不合法")
            return

        risk_per_trade = symbol_config.get('risk_per_trade', self.config['strategy']['default_risk_per_trade'])
        logger.info(f"{symbol} 使用风险度: {risk_per_trade*100:.1f}%")

        try:
            balance = self.exchange_api.get_balance()
            account_equity = strict_float_finite(
                (balance.get('total') or {}).get('USDT'),
                'total.USDT')
            if account_equity <= 0:
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

        position_size = self.exchange_api.round_quantity(
            ccxt_symbol, raw_position_size)
        precision = self.exchange_api.get_quantity_precision(ccxt_symbol)

        logger.info(f"{symbol} 仓位计算: 权益={account_equity:.2f}, 风险度={risk_per_trade*100:.1f}%, "
                    f"风险金额={risk_amount:.2f}, 计算价={calc_price}, 信号价={entry_price}, 止损价={stop_loss_price}, "
                    f"价格风险%={price_risk_pct*100:.2f}%, 仓位价值={position_value:.2f}, "
                    f"原始数量={raw_position_size}, 精度={precision}, 最终数量={position_size}")

        if position_size <= 0:
            logger.error(f"{symbol} 头寸大小无效: {position_size}")
            return

        # 所有开仓入口在任何 POST 前统一新建 open intent + clOrdId +
        # 固化数量。旧 intent 已在上方阻断，本方法不接受外部句柄。
        generated_client_id = f'I{uuid.uuid4().hex[:31]}'
        try:
            open_intent = self.trade_state.prepare_open_intent(
                symbol, 'ma_cross', side,
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
        open_intent_client_id = client_order_id
        requested_position_size = float(position_size)

        open_order = self.exchange_api.open_position(
            ccxt_symbol, side, position_size,
            client_order_id=client_order_id)
        if not open_order:
            logger.error(f"{symbol} 开仓失败")
            self.notifier.notify_error(f"{symbol} 开仓失败")
            return

        if not isinstance(open_order, dict):
            self._mark_open_rollback_quarantine(
                symbol, '开仓适配层返回非对象，成交状态无法证明',
                {'result_type': type(open_order).__name__})
            self.notifier.notify_error(
                f'{symbol} 开仓回包类型非法，已保留 open intent 并隔离')
            return

        execution_flags = (
            'open_execution_compensated', 'open_order_may_remain_live',
            'open_execution_attribution_ambiguous',
            'open_execution_unresolved',
        )
        malformed_flags = [
            name for name in execution_flags
            if name in open_order and
            not isinstance(open_order.get(name), bool)]
        if malformed_flags:
            self._mark_open_rollback_quarantine(
                symbol, '开仓回包的执行状态标志类型非法',
                {'invalid_flags': malformed_flags,
                 'client_order_id': open_order.get('clientOrderId'),
                 'order_id': open_order.get('id')})
            self.notifier.notify_error(
                f'{symbol} 开仓回包状态标志非法，已保留 open intent 并隔离')
            return

        active_execution_flags = [
            name for name in execution_flags if open_order.get(name) is True]
        if len(active_execution_flags) > 1:
            self._mark_open_rollback_quarantine(
                symbol, '开仓回包含互相冲突的执行状态',
                {'active_flags': active_execution_flags,
                 'client_order_id': open_order.get('clientOrderId'),
                 'order_id': open_order.get('id')})
            self.notifier.notify_error(
                f'{symbol} 开仓回包执行状态冲突，已保留 open intent 并隔离')
            return

        if open_order.get('open_execution_compensated') is True:
            compensation = open_order.get('compensation') or {}
            compensated_amount = self._order_actual_amount(open_order, None)
            if (open_order.get('confirmed') is not False or
                    compensated_amount is None or
                    self._classify_close_execution(compensation) != 'closed'):
                self._mark_open_rollback_quarantine(
                    symbol, '开仓补偿回包未严格证明原单未确认且补偿已全平',
                    {'client_order_id': open_order.get('clientOrderId'),
                     'order_id': open_order.get('id')})
                self.notifier.notify_error(
                    f'{symbol} 开仓补偿证明不完整，已保留 open intent 并隔离')
                return
            logger.warning(
                f"{symbol} 开仓未决，但内部 reduce-only 补偿已确认归零；"
                f"返回 rolled_back 供信号两阶段状态原子收口")
            outcome = {
                'status': 'rolled_back',
                'open_order': open_order,
                'close_order': compensation,
                'entry_price': open_order.get('average') or calc_price,
                'position_size': compensated_amount,
            }
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, outcome)

        if open_order.get('open_order_may_remain_live') is True:
            if open_order.get('confirmed') is not False:
                self._mark_open_rollback_quarantine(
                    symbol, '可能仍存活的开仓订单缺少 confirmed=false',
                    {'client_order_id': open_order.get('clientOrderId'),
                     'order_id': open_order.get('id')})
                return
            details = {
                'client_order_id': open_order.get('clientOrderId'),
                'order_id': open_order.get('id'),
                'side': side, 'planned_position_size': position_size,
            }
            if not self._mark_unresolved_open_execution(
                    symbol, client_order_id, 'open', position_size,
                    '开仓订单未证明终态，当前零仓仍可能迟到成交', details):
                self._mark_open_rollback_quarantine(
                    symbol, '开仓订单未证明终态，当前零仓仍可能迟到成交',
                    details)
            msg = (
                f"{symbol} 开仓订单未证明终态，当前虽无仓仍可能迟到成交；"
                "已隔离，禁止再次开仓并等待按原 clOrdId 对账")
            logger.critical(msg)
            self.notifier.notify_error(msg)
            return {
                'status': 'order_unresolved', 'open_order': open_order,
                'position_size': 0.0, 'planned_position_size': position_size,
            }

        if open_order.get('open_execution_attribution_ambiguous') is True:
            if not isinstance(open_order.get('confirmed'), bool):
                self._mark_open_rollback_quarantine(
                    symbol, '开仓归因不确定回包缺少严格 confirmed 布尔值',
                    {'client_order_id': open_order.get('clientOrderId'),
                     'order_id': open_order.get('id')})
                return
            details = {
                'client_order_id': open_order.get('clientOrderId'),
                'order_id': open_order.get('id'), 'side': side,
                'order_amount': open_order.get('amount'),
                'observed_position_amount': open_order.get(
                    'observed_position_amount', open_order.get('remaining_amount')),
            }
            if not self._mark_unresolved_open_execution(
                    symbol, client_order_id, 'open_attribution', position_size,
                    '开仓订单成交与净仓变化无法唯一归因，拒绝自动平人工仓',
                    details):
                self._mark_open_rollback_quarantine(
                    symbol,
                    '开仓订单成交与净仓变化无法唯一归因，拒绝自动平人工仓',
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

        if open_order.get('open_execution_unresolved') is True:
            if open_order.get('confirmed') is not False:
                self._mark_open_rollback_quarantine(
                    symbol, '未决开仓回包缺少 confirmed=false',
                    {'client_order_id': open_order.get('clientOrderId'),
                     'order_id': open_order.get('id')})
                return
            remaining = open_order.get('remaining_amount')
            compensation = open_order.get('compensation')
            # 适配层即使无法确认补偿订单，也会返回最后一次成功观测的权威余仓。
            # 合成明确的 partial 契约，让统一收口器建账；绝不能只写 quarantine
            # 而继续把真钱余仓遗忘在 open_positions 之外。
            if isinstance(compensation, dict):
                rollback_contract = dict(compensation)
                # top-level unresolved 是最终契约，不能让 nested 曾经 fully_closed
                # 的补偿结果穿透并消费仍可能迟到成交的 open intent。
                rollback_contract['confirmed'] = True
                rollback_contract['fully_closed'] = False
                rollback_contract['remaining_amount'] = remaining
            else:
                rollback_contract = {
                    'confirmed': True,
                    'fully_closed': False,
                    'remaining_amount': remaining,
                    'execution_ambiguous': True,
                }
            original_size = self._order_actual_amount(open_order, position_size)
            if original_size is None:
                original_size = position_size
            # 适配层已自行补偿；这里只裁决最终契约，严禁再次补偿 POST。
            outcome = self._finalize_open_rollback(
                symbol, ccxt_symbol, side,
                open_order.get('average') or calc_price,
                original_size, stop_loss_price,
                'ma_cross',
                open_order, rollback_contract, '未决开仓内部补偿',
                allow_stop_rebuild=True,
                open_intent_client_id=open_intent_client_id,
                requested_position_size=requested_position_size,
                preserve_open_intent=True,
                unresolved_execution_kind='open_compensation')
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, outcome)

        if open_order.get('confirmed') is not True:
            self._mark_open_rollback_quarantine(
                symbol, '开仓回包未严格证明成交',
                {'confirmed': open_order.get('confirmed'),
                 'client_order_id': open_order.get('clientOrderId'),
                 'order_id': open_order.get('id')})
            logger.critical(
                f"{symbol} 开仓订单未获严格成交确认，拒绝挂止损/记账")
            self.notifier.notify_error(
                f"{symbol} 开仓订单未获严格成交确认，已保留 intent 并隔离")
            return

        actual_position_size = self._order_actual_amount(open_order, None)
        if actual_position_size is None:
            self._mark_open_rollback_quarantine(
                symbol, '开仓回包的实际成交数量非法',
                {'amount': open_order.get('amount'),
                 'client_order_id': open_order.get('clientOrderId'),
                 'order_id': open_order.get('id')})
            logger.critical(f"{symbol} 开仓返回的实际成交数量无效，拒绝记账")
            self.notifier.notify_error(
                f"{symbol} 开仓实际成交数量无效，已保留 intent 并隔离")
            return
        rollback_context = _ConfirmedOpenRollbackContext(
            symbol=symbol,
            ccxt_symbol=ccxt_symbol,
            side=side,
            stop_loss_price=stop_loss_price,
            open_order=open_order,
            open_client_order_id=client_order_id,
            open_intent_client_order_id=open_intent_client_id,
            requested_position_size=requested_position_size,
        )
        if open_order.get('execution_ambiguous'):
            logger.critical(f"{symbol} 开仓成交与仓位变化无法归因，按实际仓位执行回滚")
            outcome = self._rollback_confirmed_open(
                rollback_context,
                open_order.get('average') or calc_price,
                actual_position_size, '歧义开仓紧急回滚',
                preserve_open_intent=True,
                unresolved_execution_kind='open_attribution')
            self.notifier.notify_error(f"{symbol} 开仓成交存在外部并发歧义，已尝试回滚，请核对交易所")
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, outcome)
        authoritative_open = self._authoritative_single_order_evidence(
            open_order)
        if authoritative_open is None:
            logger.critical(
                f'{symbol} 开仓数量已确认，但缺少唯一真实订单 ID 或权威'
                '成交均价；不用行情价污染入场账，转入可恢复补偿')
            outcome = self._rollback_confirmed_open(
                rollback_context, calc_price, actual_position_size,
                '开仓财务证据未决紧急回滚',
                preserve_open_intent=True,
                unresolved_execution_kind='open_compensation')
            self.notifier.notify_error(
                f'{symbol} 开仓已成交但财务证据未决；'
                '已尝试 reduce-only 回滚并保留 lifecycle blocker')
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, outcome)
        if actual_position_size > position_size * (1 + 1e-9):
            logger.critical(
                f"{symbol} 实际成交量({actual_position_size})超过计划量({position_size})，"
                f"按实际量执行回滚")
            outcome = self._rollback_confirmed_open(
                rollback_context,
                open_order.get('average') or calc_price,
                actual_position_size, '超量开仓紧急回滚',
                preserve_open_intent=True,
                unresolved_execution_kind='open_attribution')
            self.notifier.notify_error(f"{symbol} 开仓超量成交，已尝试回滚，请核对交易所")
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, outcome)
        if actual_position_size < position_size * (1 - 1e-9):
            logger.warning(
                f"{symbol} 开仓部分成交: 计划={position_size}币, 实际={actual_position_size}币；"
                f"后续止损、风险与账本全部按实际量")
        position_size = actual_position_size

        actual_price = authoritative_open['price']
        if open_order.get('fee') is not None or open_order.get('fees'):
            logger.info(
                f"{symbol} 开仓真实手续费: fee={open_order.get('fee')}, fees={open_order.get('fees')}")

        # 成交后再次校验；不得为已越过的止损价挂无效条件单。
        fill_stop_invalid = (
            (side == 'long' and stop_loss_price >= actual_price) or
            (side == 'short' and stop_loss_price <= actual_price))
        if fill_stop_invalid:
            logger.error(
                f"{symbol} 开仓回滚: 止损价({stop_loss_price})已不再保护"
                f"成交/当前价(actual={actual_price}, current={calc_price})")
            self.notifier.notify_error(f"{symbol} 开仓成交后止损已失效，正在立即回滚")
            outcome = self._rollback_confirmed_open(
                rollback_context, actual_price, position_size,
                '止损失效后的紧急回滚',
                allow_stop_rebuild=False)
            if outcome.get('status') == 'rolled_back':
                logger.warning(f"{symbol} 已执行开仓后回滚平仓，避免无效止损")
            else:
                logger.critical(f"{symbol} 开仓后回滚未确认全平，请立即人工处理！")
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, outcome)

        stop_order = self._create_stop_with_write_intent(
            symbol, ccxt_symbol, side, position_size, stop_loss_price)
        if not stop_order:
            logger.error(f"{symbol} 创建止损单失败")
            self.notifier.notify_error(f"{symbol} 创建止损单失败，请手动设置止损！")
            # 返回 None 可能是“POST 已成功但确认查询不确定”。此时绝不能先
            # cancel_all 再平仓：订单部分成交会留下裸余仓。保留一切可能的
            # reduce-only 保护，先回滚；仅明确全平后才安全清扫全部算法单。
            outcome = self._rollback_confirmed_open(
                rollback_context, actual_price, position_size,
                '止损创建失败后的紧急回滚',
                stop_residue_possible=True)
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
            outcome = self._rollback_confirmed_open(
                rollback_context, actual_price, position_size,
                '成交后风险超标紧急回滚',
                existing_stop_order_id=stop_order_id,
                existing_stop_order_size=position_size,
                allow_stop_rebuild=False, stop_residue_possible=True)
            if outcome.get('status') == 'rolled_back':
                if not self._cancel_stop_order_confirmed(
                        symbol, ccxt_symbol, stop_order_id):
                    logger.critical(
                        f'{symbol} 风险超标回滚已全平，但止损清理未确认')
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, outcome)

        persist_result = self._persist_open_position_or_rollback(
            symbol, ccxt_symbol, side, actual_price, position_size, stop_loss_price, stop_order_id,
            strategy='ma_cross', open_order=open_order,
            open_intent_client_id=open_intent_client_id,
            requested_position_size=requested_position_size,
        )
        if persist_result is not True:
            return self._finalize_generic_rolled_back_outcome(
                symbol, open_intent, persist_result)
        # 已确认止损 ID/尺寸与真实仓位一并耐久落账后才可消费 POST 前句柄。
        self._complete_stop_write(symbol, '开仓保护单')
        if buffer_notification:
            self._buffer_trade_open_notification(symbol, side, actual_price, position_size, stop_loss_price)
        return {
            'status': 'opened', 'open_order': open_order,
            'entry_price': actual_price, 'position_size': position_size,
            'stop_order_id': stop_order_id,
        }
