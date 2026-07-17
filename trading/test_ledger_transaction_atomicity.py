"""账本事务原子性与只读补偿恢复回归测试（终审缺陷反例，纯标准库可运行）。

对应终审判决确认的缺陷：
1. 部分平仓/完整平仓/止损更新在「改完内存后校验失败」时必须整体回滚——
   否则会出现磁盘仓位 10、内存仓位 8、close intent 仍计划关闭 10 的三方
   不一致，且 intent 计划量与内存仓位失配会让后续所有保存持续失败；
2. add_open_position 不得静默覆盖同品种持仓，且拒绝 bool/NaN/无穷/非正数；
3. 「已确认空仓后的只读补偿证据找回」绝不允许调用可真实下单的
   close_position——极端竞态下那会把用户人工开出的同向仓平掉。
"""
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import trade_executor
from trade_state import TradeState, TradeStatePersistenceError


def _read_disk(state):
    with open(state.state_file) as f:
        return json.load(f)


class LedgerTransactionAtomicityTest(unittest.TestCase):
    def _state(self, temp_dir):
        state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
        state.add_open_position(
            'BTCUSDT', 'long', 100.0, 10.0, 90.0, 'stop-1', strategy='ma_cross')
        return state

    def test_partial_close_rolls_back_memory_when_intent_check_fails(self):
        """反例：改完余仓后 close intent 校验失败，内存必须回到 10 币。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state(temp_dir)
            state.prepare_close_intent('BTCUSDT', 'C' + 'a' * 31, '日检平仓')
            with self.assertRaises(TradeStatePersistenceError):
                # 有 pending intent 时不带句柄修改账本必须被拒——修复前
                # 该异常发生在 position_size 已缩到 8 之后且不回滚。
                state.apply_partial_close(
                    'BTCUSDT', 2.0, 105.0, close_intent_client_id=None)
            position = state.get_open_position('BTCUSDT')
            self.assertEqual(10.0, position['position_size'])
            self.assertFalse(position.get('partial_closes'))
            self.assertEqual('pending', position['close_intent']['status'])
            self.assertEqual(
                10.0, position['close_intent']['planned_position_size'])
            disk = _read_disk(state)
            self.assertEqual(
                10.0, disk['open_positions']['BTCUSDT']['position_size'])
            # 回滚后账本仍满足 schema，后续保存不再持续失败。
            state.update_stop_loss('BTCUSDT', 95.0, 'stop-2')

    def test_force_runtime_partial_close_rolls_back_memory_too(self):
        """磁盘失效路径同样不允许留下半截内存账本。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state(temp_dir)
            state.prepare_close_intent('BTCUSDT', 'C' + 'a' * 31, '日检平仓')
            with self.assertRaises(TradeStatePersistenceError):
                state.force_runtime_apply_partial_close(
                    'BTCUSDT', 2.0, 105.0,
                    close_intent_client_id='C' + 'b' * 31)
            position = state.get_open_position('BTCUSDT')
            self.assertEqual(10.0, position['position_size'])
            self.assertEqual('pending', position['close_intent']['status'])

    def test_close_position_rolls_back_consumed_intent_on_corrupt_partial(self):
        """反例：intent 已在内存被消费后遇到损坏分段抛出，必须整体回滚。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state(temp_dir)
            intent = state.prepare_close_intent(
                'BTCUSDT', 'C' + 'c' * 31, '日检平仓')
            with state.lock:
                # 模拟被外部破坏的历史分段：float() 在 intent 消费之后才失败。
                state.state['open_positions']['BTCUSDT']['partial_closes'] = [
                    {'position_size': '损坏', 'exit_price': 105.0}]
            with self.assertRaises((TypeError, ValueError)):
                state.close_position(
                    'BTCUSDT', 106.0,
                    close_intent_client_id=intent['client_order_id'])
            position = state.get_open_position('BTCUSDT')
            self.assertIsNotNone(position)
            self.assertEqual('pending', position['close_intent']['status'])
            self.assertEqual(
                intent['client_order_id'],
                position['close_intent']['client_order_id'])
            self.assertNotIn('last_close_client_order_id', position)

    def test_contract_violation_mutate_none_after_change_is_rolled_back(self):
        """加固反例：mutate 宣称未修改（返回 None）却改了账本 → 回滚并抛出。

        没有这道守卫时，违约的 mutate 会静默跳过落盘，留下「内存已改、
        磁盘未存」的观测盲区——正是本轮修复要消灭的中间态。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state(temp_dir)

            def rogue_mutate():
                state.state['open_positions']['BTCUSDT']['position_size'] = 1.0
                return None

            with state.lock:
                with self.assertRaises(TradeStatePersistenceError):
                    state._transact_locked(rogue_mutate)
            self.assertEqual(
                10.0, state.get_open_position('BTCUSDT')['position_size'])

    def test_true_noop_mutate_returning_none_skips_save_quietly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state(temp_dir)
            with state.lock:
                self.assertIsNone(state._transact_locked(lambda: None))
            self.assertEqual(
                10.0, state.get_open_position('BTCUSDT')['position_size'])

    def test_update_stop_loss_rolls_back_on_bad_extra_ids(self):
        """反例：extra_stop_order_ids 非法时，止损价/ID 不得留下半截修改。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state(temp_dir)
            with self.assertRaises(TypeError):
                state.update_stop_loss(
                    'BTCUSDT', 95.0, 'stop-2', extra_stop_order_ids=42)
            position = state.get_open_position('BTCUSDT')
            self.assertEqual(90.0, position['stop_loss_price'])
            self.assertEqual('stop-1', position['stop_order_id'])
            self.assertNotIn('extra_stop_order_ids', position)


class AddOpenPositionGuardTest(unittest.TestCase):
    def test_rejects_silent_overwrite_of_same_symbol(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-1', strategy='ma_cross')
            with self.assertRaises(TradeStatePersistenceError):
                state.add_open_position(
                    'BTCUSDT', 'short', 200.0, 2.0, 210.0, 'stop-2',
                    strategy='ma_cross')
            position = state.get_open_position('BTCUSDT')
            self.assertEqual('long', position['side'])
            self.assertEqual(100.0, position['entry_price'])
            self.assertEqual('stop-1', position['stop_order_id'])

    def test_rejects_bool_nan_inf_nonpositive_and_garbage_numbers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            bad_values = (True, float('nan'), float('inf'), 0, -1.5, '价格', None)
            for field_index, field in enumerate(
                    ('entry_price', 'position_size', 'stop_loss_price')):
                for bad in bad_values:
                    args = [100.0, 1.0, 90.0]
                    args[field_index] = bad
                    with self.subTest(field=field, bad=bad), \
                            self.assertRaises(ValueError):
                        state.add_open_position('ETHUSDT', 'long', *args)
            self.assertIsNone(state.get_open_position('ETHUSDT'))
            self.assertEqual({}, state.get_all_open_positions())
            # 全部写入被拒于修改之前：从未发生任何落盘。
            self.assertFalse(Path(state.state_file).exists())

    def test_rejects_invalid_side(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            with self.assertRaises(ValueError):
                state.add_open_position('ETHUSDT', 'buy', 100.0, 1.0, 90.0)
            self.assertIsNone(state.get_open_position('ETHUSDT'))


class ForceRuntimeInputBoundaryTest(unittest.TestCase):
    """对抗复审反例 D：仅内存（force_runtime）路径同样拒绝 NaN/错误类型。

    落盘路径有 validate_state 兜底；不落盘路径此前没有任何校验，
    NaN 止损价/bool 数量会直接住进账本并污染后续风控计算。
    """

    def _state(self, temp_dir):
        state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
        state.add_open_position(
            'BTCUSDT', 'long', 100.0, 10.0, 90.0, 'stop-1', strategy='ma_cross')
        return state

    def test_force_runtime_stop_update_rejects_nan_and_bad_types(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state(temp_dir)
            for bad in (float('nan'), float('inf'), True, 0, -1, '价格', None):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    state.force_runtime_update_stop_loss('BTCUSDT', bad, 'stop-2')
            with self.assertRaises(ValueError):
                state.force_runtime_update_stop_loss('BTCUSDT', 95.0, 123)
            with self.assertRaises(ValueError):
                state.force_runtime_update_stop_loss(
                    'BTCUSDT', 95.0, 'stop-2', stop_order_size=float('nan'))
            position = state.get_open_position('BTCUSDT')
            self.assertEqual(90.0, position['stop_loss_price'])
            self.assertEqual('stop-1', position['stop_order_id'])

    def test_force_runtime_partial_close_rejects_bool_stop_size(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state(temp_dir)
            with self.assertRaises(ValueError):
                state.force_runtime_apply_partial_close(
                    'BTCUSDT', 2.0, 105.0, stop_order_size=True)
            self.assertEqual(
                10.0, state.get_open_position('BTCUSDT')['position_size'])

    def test_force_runtime_close_books_entry_price_instead_of_nan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state(temp_dir)
            trade = state.force_runtime_close_position('BTCUSDT', float('nan'))
            self.assertEqual(100.0, trade['exit_price'])
            self.assertTrue(math.isfinite(trade['pnl']))


class SafeFillPriceTest(unittest.TestCase):
    """成交均价解析反例：垃圾字符串/NaN/bool 一律回退兜底价，绝不裸抛。

    开仓路径若在「已成交、未挂止损」之间因 float('垃圾') 崩溃，会留下
    最长一个巡检周期的无止损裸仓；NaN 会静默滑过止损失效比较。
    """

    def test_garbage_average_falls_back_instead_of_raising(self):
        safe = trade_executor.TradeExecutorMixin._safe_fill_price
        for bad in ('垃圾', 'nan', float('nan'), float('inf'), -1, 0,
                    True, None, [50000.0]):
            with self.subTest(bad=bad):
                self.assertEqual(100.0, safe({'average': bad}, 100.0))
        self.assertEqual(100.0, safe({}, 100.0))
        self.assertEqual(100.0, safe(None, 100.0))

    def test_valid_average_is_used(self):
        safe = trade_executor.TradeExecutorMixin._safe_fill_price
        self.assertEqual(50000.5, safe({'average': 50000.5}, 100.0))
        self.assertEqual(50000.5, safe({'average': '50000.5'}, 100.0))


class _EvidenceHost(trade_executor.TradeExecutorMixin):
    def __init__(self, exchange_api):
        self.exchange_api = exchange_api


class ReadOnlyCompensationEvidenceTest(unittest.TestCase):
    """只读恢复路径的资金安全边界：任何分支都不得发出真实平仓 POST。"""

    def test_adapter_without_readonly_finder_never_falls_back_to_posting(self):
        api = Mock(spec=['close_position'])
        host = _EvidenceHost(api)
        result = host._recover_flat_compensation_evidence(
            'BTC/USDT:USDT', 'long', 0.5, 'OPENID1')
        self.assertIsNone(result)
        api.close_position.assert_not_called()

    def test_readonly_finder_result_is_returned_without_posting(self):
        evidence = {'fully_closed': True, 'average': 50000.5, 'ids': ['1']}
        api = Mock(
            spec=['close_position', 'find_compensation_close_evidence'])
        api.find_compensation_close_evidence.return_value = evidence
        host = _EvidenceHost(api)
        result = host._recover_flat_compensation_evidence(
            'BTC/USDT:USDT', 'long', 0.5, 'OPENID1')
        self.assertEqual(evidence, result)
        api.close_position.assert_not_called()
        api.find_compensation_close_evidence.assert_called_once_with(
            'BTC/USDT:USDT', 'long', 0.5, 'OPENID1')

    def test_incomplete_or_ambiguous_evidence_is_discarded(self):
        for bad in (None, {'fully_closed': False},
                    {'fully_closed': True, 'execution_ambiguous': True},
                    'not-a-dict'):
            api = Mock(
                spec=['close_position', 'find_compensation_close_evidence'])
            api.find_compensation_close_evidence.return_value = bad
            host = _EvidenceHost(api)
            with self.subTest(bad=bad):
                self.assertIsNone(host._recover_flat_compensation_evidence(
                    'BTC/USDT:USDT', 'long', 0.5, 'OPENID1'))
                api.close_position.assert_not_called()

    def test_missing_open_client_id_short_circuits_without_queries(self):
        api = Mock(
            spec=['close_position', 'find_compensation_close_evidence'])
        host = _EvidenceHost(api)
        self.assertIsNone(host._recover_flat_compensation_evidence(
            'BTC/USDT:USDT', 'long', 0.5, None))
        api.find_compensation_close_evidence.assert_not_called()
        api.close_position.assert_not_called()


if __name__ == '__main__':
    unittest.main()
