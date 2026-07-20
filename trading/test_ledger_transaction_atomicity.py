"""账本事务原子性与只读补偿恢复回归测试（终审缺陷反例，纯标准库可运行）。

对应终审判决确认的缺陷：
1. 部分平仓/完整平仓/止损保护记录更新在「改完内存后校验失败」时必须整体回滚——
   否则会出现磁盘仓位 10、内存仓位 8、close intent 仍计划关闭 10 的三方
   不一致，且 intent 计划量与内存仓位失配会让后续所有保存持续失败；
2. add_open_position 不得静默覆盖同品种持仓，且拒绝 bool/NaN/无穷/非正数；
3. 「已确认空仓后的只读补偿证据找回」绝不允许调用可真实下单的
   close_position——极端竞态下那会把用户人工开出的同向仓平掉。
"""
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import trade_executor
import migrate_single_strategy as migration
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

    def test_duplicate_open_positions_cannot_hide_a_real_position(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'trade_state.json'
            path.write_text(
                '{"open_positions":{"BTCUSDT":{"symbol":"BTCUSDT"}},'
                '"open_positions":{},"closed_trades":[]}',
                encoding='utf-8')
            with self.assertRaisesRegex(
                    TradeStatePersistenceError, '重复字段'):
                TradeState(str(path))

    def test_nonstandard_save_failure_rolls_back_pending_close_intent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state(temp_dir)

            with patch.object(
                    state, 'save_state', side_effect=MemoryError('fault')):
                with self.assertRaises(MemoryError):
                    state.prepare_close_intent(
                        'BTCUSDT', 'C' + 'z' * 31, '日检平仓')

            self.assertIsNone(state.get_close_intent('BTCUSDT'))
            self.assertIsNone(
                TradeState(state.state_file).get_close_intent('BTCUSDT'))

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
            with self.assertRaises(ValueError):
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

    def test_force_runtime_partial_close_cannot_bypass_full_schema(self):
        """force-runtime 也必须拒绝 writer 未局部检查的非法止损 ID。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state(temp_dir)
            with self.assertRaises(ValueError):
                state.force_runtime_apply_partial_close(
                    'BTCUSDT', 2.0, 105.0, new_stop_order_id=123)
            position = state.get_open_position('BTCUSDT')
            self.assertEqual(10.0, position['position_size'])
            self.assertEqual('stop-1', position['stop_order_id'])
            TradeState.validate_state(state.state)

    def test_force_runtime_untracked_open_cannot_bypass_full_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            with self.assertRaises(TradeStatePersistenceError):
                state.force_runtime_add_untracked_open_position(
                    'ETHUSDT', 'long', 100.0, 1.0, 90.0,
                    stop_order_id=123, strategy='ma_cross')
            self.assertEqual({}, state.get_all_open_positions())
            self.assertEqual({}, state.get_position_quarantines())
            TradeState.validate_state(state.state)

    def test_unresolved_adoption_rolls_back_all_fields_on_save_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IAtomicAdopt1',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=10.0)
            before = copy.deepcopy(state.state)
            position_kwargs = {
                'side': 'long', 'entry_price': 100.0,
                'position_size': 5.0, 'stop_loss_price': 90.0,
                'stop_order_id': None, 'stop_order_size': 5.0,
                'strategy': 'ma_cross', 'stop_resize_pending': True,
                'quarantine_reason': 'atomic recovery test',
                'stop_residue_possible': True,
                'requested_position_size': 10.0,
            }

            with patch.object(
                    state, 'save_state', side_effect=MemoryError('fault')):
                with self.assertRaises(MemoryError):
                    state.adopt_unresolved_open_position(
                        'BTCUSDT', 'IAtomicAdopt1',
                        'open_compensation', 5.0,
                        compensation_client_order_id='RAtomicAdopt1',
                        position_kwargs=position_kwargs)

            self.assertEqual(before, state.state)
            self.assertEqual(before, TradeState(state.state_file).state)

            position = state.adopt_unresolved_open_position(
                'BTCUSDT', 'IAtomicAdopt1', 'open_compensation', 5.0,
                compensation_client_order_id='RAtomicAdopt1',
                position_kwargs=position_kwargs)
            self.assertEqual(5.0, position['position_size'])
            self.assertEqual(
                5.0, state.get_open_intent(
                    'BTCUSDT')['unresolved_execution'][
                        'expected_position_size'])
            self.assertTrue(state.is_position_quarantined('BTCUSDT'))
            self.assertTrue(state.has_stop_residue('BTCUSDT'))
            TradeState.validate_state(state.state)

    def test_existing_open_marker_cannot_expand_during_adoption(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IOpenBound1',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=10.0)
            state.mark_open_intent_unresolved_execution(
                'BTCUSDT', 'IOpenBound1', 'open', 5.0)

            with self.assertRaisesRegex(
                    TradeStatePersistenceError, '上界不得扩大'):
                state.adopt_unresolved_open_position(
                    'BTCUSDT', 'IOpenBound1', 'open', 10.0,
                    position_kwargs={
                        'side': 'long', 'entry_price': 100.0,
                        'position_size': 10.0, 'stop_loss_price': 90.0,
                        'stop_order_id': None, 'stop_order_size': 10.0,
                        'strategy': 'ma_cross',
                        'stop_resize_pending': True,
                        'requested_position_size': 10.0,
                    })
            self.assertIsNone(state.get_open_position('BTCUSDT'))

    def test_repeated_compensation_marker_keeps_original_request_size(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'ICompImmutable1',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=10.0)
            state.mark_open_intent_unresolved_execution(
                'BTCUSDT', 'ICompImmutable1', 'open_compensation', 5.0,
                compensation_client_order_id='RCompImmutable1',
                compensation_requested_size=5.0)

            repeated = state.mark_open_intent_unresolved_execution(
                'BTCUSDT', 'ICompImmutable1', 'open_compensation', 10.0,
                compensation_client_order_id='RCompImmutable1')

            self.assertEqual(5.0, repeated['compensation_requested_size'])
            with self.assertRaisesRegex(
                    TradeStatePersistenceError, '不得改写既有补偿请求量'):
                state.mark_open_intent_unresolved_execution(
                    'BTCUSDT', 'ICompImmutable1', 'open_compensation', 10.0,
                    compensation_client_order_id='RCompImmutable1',
                    compensation_requested_size=10.0)

    def test_attribution_marker_can_record_real_overfill(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IOverfillBlock1',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=1.0)

            marker = state.mark_open_intent_unresolved_execution(
                'BTCUSDT', 'IOverfillBlock1', 'open_attribution', 1.5)

            self.assertEqual(1.5, marker['expected_position_size'])
            TradeState.validate_state(state.state)

    def test_force_runtime_close_books_entry_price_instead_of_nan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state(temp_dir)
            trade = state.force_runtime_close_position('BTCUSDT', float('nan'))
            self.assertEqual(100.0, trade['exit_price'])
            self.assertTrue(math.isfinite(trade['pnl']))
            self.assertEqual(
                'estimated_entry_fallback', trade['exit_price_source'])
            self.assertIs(True, trade['exit_price_estimated'])
            TradeState.validate_state(state.state)

    def test_estimated_stop_source_must_match_stop_anchor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state(temp_dir)
            with self.assertRaises(ValueError):
                state.close_position(
                    'BTCUSDT', 91.0,
                    exit_price_source='estimated_stop')
            self.assertIsNotNone(state.get_open_position('BTCUSDT'))
            self.assertEqual([], state.get_closed_trades())

            trade = state.close_position(
                'BTCUSDT', 90.0,
                exit_price_source='estimated_stop')
            self.assertEqual('estimated_stop', trade['exit_price_source'])
            self.assertIs(True, trade['exit_price_estimated'])
            TradeState.validate_state(state.state)


class OpenToClosedSchemaClosureTest(unittest.TestCase):
    """在途仓共享字段必须在真钱平仓前满足 closed-trade schema。"""

    @staticmethod
    def _ledger():
        return {
            'exchange': 'okx',
            'open_positions': {
                'BTCUSDT': {
                    'symbol': 'BTCUSDT', 'side': 'long',
                    'entry_price': 100.0, 'position_size': 10.0,
                    'original_position_size': 10.0,
                    'stop_loss_price': 90.0, 'stop_order_id': 'stop-1',
                    'stop_order_size': 10.0, 'strategy': 'ma_cross',
                    'open_time': '2026-07-18T08:00:00',
                    'last_stop_update': '2026-07-18T08:01:00',
                    'last_partial_close': '2026-07-18T08:02:00',
                    'recovered_partial_rollback': True,
                    'recovered_unresolved_open': False,
                },
            },
            'closed_trades': [], 'open_intents': {},
            'signal_states': {}, 'stop_residues': {},
            'stop_loss_dates': {}, 'position_quarantines': {},
        }

    def test_runtime_rejects_open_fields_that_closed_trade_would_reject(self):
        """反例曾在交易所平仓完成后才爆；现在必须在启动阶段阻断。"""
        corruptions = {
            'original_position_size': '10',
            'stop_order_size': '10',
            'open_time': 'not-an-iso-time',
            'last_stop_update': 123,
            'last_partial_close': '',
            'recovered_partial_rollback': 'true',
            'recovered_unresolved_open': 1,
        }
        for field, bad_value in corruptions.items():
            ledger = self._ledger()
            ledger['open_positions']['BTCUSDT'][field] = bad_value
            with self.subTest(field=field), self.assertRaises(ValueError):
                TradeState.validate_state(ledger)

    def test_migration_gate_rejects_same_inflight_corruptions(self):
        """部署预检不得让坏在途字段穿过，等真实平仓后才失败。"""
        corruptions = {
            'original_position_size': '10',
            'stop_order_size': '10',
            'open_time': 'not-an-iso-time',
            'last_stop_update': 123,
            'last_partial_close': '',
            'recovered_partial_rollback': 'true',
            'recovered_unresolved_open': 1,
        }
        for field, bad_value in corruptions.items():
            ledger = self._ledger()
            ledger['open_positions']['BTCUSDT'][field] = bad_value
            cleaned, _report, blockers = migration.normalize_ledger(
                copy.deepcopy(ledger))
            with self.subTest(field=field):
                self.assertTrue(blockers)
                with self.assertRaises(ValueError):
                    TradeState.validate_state(cleaned)

    def test_runtime_requires_explicit_ma_owner_for_every_open_position(self):
        for label in (None, 'legacy_strategy'):
            ledger = self._ledger()
            if label is None:
                ledger['open_positions']['BTCUSDT'].pop('strategy')
            else:
                ledger['open_positions']['BTCUSDT']['strategy'] = label
            with self.subTest(label=label), self.assertRaises(ValueError):
                TradeState.validate_state(ledger)
            self.assertTrue(
                migration.normalize_ledger(copy.deepcopy(ledger))[2])


class QuarantineSchemaSafetyTest(unittest.TestCase):
    def test_nonfinite_diagnostics_are_sanitized_without_poisoning_next_save(self):
        """交易所 NaN 诊断必须仍能持久化隔离，且不拖垮整个后续账本。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / 'trade_state.json')
            state = TradeState(path)
            quarantine = state.mark_position_quarantine(
                'BTCUSDT', '仓位数量非法', {
                    'exchange_contracts': float('nan'),
                    'nested': [float('inf'), float('-inf')],
                    'oversized_integer': 10 ** 10000,
                })
            self.assertEqual('nan', quarantine['details']['exchange_contracts'])
            self.assertEqual(
                ['inf', '-inf'], quarantine['details']['nested'])
            self.assertRegex(
                quarantine['details']['oversized_integer'],
                r'^<integer-too-large:[0-9]+-bits>$')

            # 修复前 force-runtime 会把 NaN 留在内存，此后的任何无关保存都失败。
            state.mark_stop_residue('ETHUSDT')
            TradeState.validate_state(state.state)
            reloaded = TradeState(path)
            self.assertTrue(reloaded.is_position_quarantined('BTCUSDT'))
            self.assertTrue(reloaded.has_stop_residue('ETHUSDT'))


class ClosedTradeStrictSchemaTest(unittest.TestCase):
    def test_empty_or_incomplete_history_cannot_count_as_a_trade(self):
        for record in ({}, {'symbol': 'BTCUSDT'}):
            ledger = {
                'open_positions': {}, 'closed_trades': [record],
            }
            with self.subTest(record=record), self.assertRaises(ValueError):
                TradeState.validate_state(ledger)
            self.assertTrue(migration.normalize_ledger(ledger)[2])
            self.assertTrue(migration.normalize_archive([record])[2])

    def test_historical_okx_exit_evidence_keeps_relational_schema(self):
        base = {
            'symbol': 'BTCUSDT', 'side': 'long',
            'entry_price': 100.0, 'exit_price': 90.0,
            'position_size': 1.0,
        }
        corruptions = (
            {'exit_price_source': 'garbage', 'exit_price_estimated': True},
            {'exit_price_estimated': True},
            {'exit_price_source': 'estimated_stop'},
            {
                'exit_price_source': 'okx_stop_fill',
                'exit_price_estimated': False,
            },
            {
                'exit_price_source': 'estimated_stop',
                'exit_price_estimated': True,
                'exchange_exit_time': '2026-07-18T08:00:00',
            },
            {
                'exit_price_source': 'estimated_stop',
                'exit_price_estimated': True,
                'stop_loss_price': 91.0,
            },
            {
                'exit_price_source': 'estimated_entry_fallback',
                'exit_price_estimated': True,
            },
        )
        for corruption in corruptions:
            trade = dict(base, **corruption)
            ledger = {'open_positions': {}, 'closed_trades': [trade]}
            with self.subTest(corruption=corruption):
                with self.assertRaises(ValueError):
                    TradeState.validate_state(ledger)
                self.assertTrue(migration.normalize_ledger(ledger)[2])
                self.assertTrue(migration.normalize_archive([trade])[2])


class FeeMetadataSchemaClosureTest(unittest.TestCase):
    """手续费元数据必须在启动门禁拦住，不能等真钱成交后才爆账本。"""

    @staticmethod
    def _open_ledger():
        return {
            'exchange': 'okx',
            'open_positions': {
                'BTCUSDT': {
                    'symbol': 'BTCUSDT', 'side': 'long',
                    'entry_price': 100.0, 'position_size': 1.0,
                    'stop_loss_price': 90.0, 'strategy': 'ma_cross',
                },
            },
            'closed_trades': [], 'open_intents': {},
            'signal_states': {}, 'stop_residues': {},
            'stop_loss_dates': {}, 'position_quarantines': {},
        }

    def test_open_entry_fee_tuple_is_rejected_by_runtime_and_migration(self):
        corruptions = (
            {'entry_fee': 0.01},
            {
                'entry_fee': 0.01, 'entry_fee_source': 'typo',
                'entry_fee_currency': 'USDT',
            },
            {
                'entry_fee': 0.01, 'entry_fee_source': 'exchange',
                'entry_fee_currency': 'BTC',
            },
            {
                'entry_fee': 0.01, 'entry_fee_source': 'exchange',
            },
            {
                'entry_fee_source': 'exchange',
                'entry_fee_currency': 'USDT',
            },
        )
        for corruption in corruptions:
            ledger = self._open_ledger()
            ledger['open_positions']['BTCUSDT'].update(corruption)
            with self.subTest(corruption=corruption):
                with self.assertRaises(ValueError):
                    TradeState.validate_state(ledger)
                self.assertTrue(
                    migration.normalize_ledger(copy.deepcopy(ledger))[2])

    def test_partial_and_closed_fee_sources_are_strict_in_all_history_gates(self):
        partial_base = {
            'position_size': 0.4, 'exit_price': 110.0,
            'exit_notional': 44.0, 'gross_pnl': 4.0,
            'exit_fee': 0.0198, 'fee_source': 'estimated',
            'close_time': '2026-07-18T08:00:00',
        }
        partial_corruptions = (
            {'fee_source': 'typo'},
            {'fee_source': 'exchange'},
            {'fee_source': 'estimated', 'exit_fee_currency': 'USDT'},
            {'fee_source': 'exchange', 'exit_fee_currency': 'BTC'},
        )
        for corruption in partial_corruptions:
            ledger = self._open_ledger()
            position = ledger['open_positions']['BTCUSDT']
            position.update({
                'position_size': 0.6, 'original_position_size': 1.0,
                'partial_closes': [dict(partial_base, **corruption)],
            })
            with self.subTest(layer='partial', corruption=corruption):
                with self.assertRaises(ValueError):
                    TradeState.validate_state(ledger)
                self.assertTrue(
                    migration.normalize_ledger(copy.deepcopy(ledger))[2])

        closed_base = {
            'symbol': 'BTCUSDT', 'side': 'long',
            'entry_price': 100.0, 'exit_price': 110.0,
            'position_size': 1.0,
        }
        for corruption in (
                {'fee_source': 'typo'},
                {'fee_source': 'actual', 'exit_fee_currency': 'BTC'},
                {
                    'fee_source': 'actual', 'entry_fee': 0.045,
                    'exit_fee': 0.0495, 'total_fee': 0.0945,
                    'gross_pnl': 10.0, 'pnl': 9.9055,
                    'pnl_percent': 9.9055,
                },
                {
                    'fee_source': 'estimated', 'entry_fee': 0.045,
                    'exit_fee': 0.0495, 'total_fee': 0.0945,
                    'gross_pnl': 10.0, 'pnl': 9.9055,
                    'pnl_percent': 9.9055,
                    'entry_fee_source': 'exchange',
                    'entry_fee_currency': 'USDT',
                }):
            trade = dict(closed_base, **corruption)
            ledger = {'open_positions': {}, 'closed_trades': [trade]}
            with self.subTest(layer='closed', corruption=corruption):
                with self.assertRaises(ValueError):
                    TradeState.validate_state(ledger)
                self.assertTrue(
                    migration.normalize_ledger(copy.deepcopy(ledger))[2])
                self.assertTrue(
                    migration.normalize_archive([copy.deepcopy(trade)])[2])


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


class ExecutionResultBoundaryTest(unittest.TestCase):
    def test_close_classifier_requires_exact_confirmed_and_terminal_bools(self):
        classify = (
            trade_executor.TradeExecutorMixin()._classify_close_execution)
        self.assertEqual(
            'closed', classify({
                'id': 'close-full', 'average': 99.0,
                'confirmed': True, 'fully_closed': True,
                'remaining_amount': 0.0}))
        self.assertEqual(
            'partial', classify({
                'id': 'close-partial', 'average': 99.0,
                'confirmed': True, 'fully_closed': False,
                'remaining_amount': 0.5}))
        invalid = (
            None, {}, {'fully_closed': True},
            {'confirmed': None, 'fully_closed': True},
            {'confirmed': 'true', 'fully_closed': True},
            {'confirmed': False, 'fully_closed': True},
            {'confirmed': True},
            {'confirmed': True, 'fully_closed': None},
            {'confirmed': True, 'fully_closed': 'true'},
            {'confirmed': True, 'fully_closed': True,
             'remaining_amount': 1.0},
            {'confirmed': True, 'fully_closed': False,
             'remaining_amount': 0.0},
            {'confirmed': True, 'fully_closed': True,
             'remaining_amount': True},
            {'confirmed': True, 'fully_closed': True,
             'remaining_amount': float('nan')},
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual('unresolved', classify(value))

    def test_actual_amount_rejects_bool_nonfinite_and_nonpositive(self):
        parse = trade_executor.TradeExecutorMixin._order_actual_amount
        for bad in (
                True, False, float('nan'), float('inf'), float('-inf'),
                'nan', 'inf', 'garbage', 0, -1, [], {}):
            with self.subTest(source='order', bad=bad):
                self.assertIsNone(parse({'amount': bad}, 1.0))
            with self.subTest(source='fallback', bad=bad):
                self.assertIsNone(parse({}, bad))
        self.assertEqual(1.25, parse({'amount': '1.25'}, None))

    def test_fee_and_order_id_metadata_are_strict(self):
        fee = trade_executor.TradeExecutorMixin._extract_usdt_fee
        for bad in (True, False, float('nan'), float('inf')):
            with self.subTest(cost=bad):
                self.assertEqual(
                    (None, None),
                    fee({'fee': {'cost': bad, 'currency': 'USDT'}}))
        self.assertEqual(
            (None, None),
            fee({'fees_complete': 'false',
                 'fee': {'cost': 1, 'currency': 'USDT'}}))
        self.assertEqual(
            (0.5, 'USDT'),
            fee({'fees_complete': True,
                 'fee': {'cost': 0.5, 'currency': 'USDT'}}))

        order_ids = trade_executor.TradeExecutorMixin._order_ids
        self.assertEqual(['123'], order_ids({'ids': '123', 'id': '123'}))
        self.assertEqual(
            ['A', '12'],
            order_ids({'ids': [' A ', True, {}, 12, 'A', '']}))


class _EvidenceHost(trade_executor.TradeExecutorMixin):
    def __init__(self, exchange_api):
        self.exchange_api = exchange_api


class ReadOnlyCompensationEvidenceTest(unittest.TestCase):
    """只读恢复路径的资金安全边界：任何分支都不得发出真实平仓 POST。"""

    def test_adapter_without_readonly_finder_fails_loudly_without_posting(self):
        api = Mock(spec=['close_position'])
        host = _EvidenceHost(api)
        with self.assertRaises(AttributeError):
            host._recover_flat_compensation_evidence(
                'BTC/USDT:USDT', 'long', 0.5, 'OPENID1')
        api.close_position.assert_not_called()

    def test_readonly_finder_result_is_returned_without_posting(self):
        evidence = {
            'confirmed': True, 'fully_closed': True,
            'remaining_amount': 0.0,
            'average': 50000.5, 'ids': ['1']}
        api = Mock(
            spec=['close_position', 'find_compensation_close_progress'])
        api.find_compensation_close_progress.return_value = evidence
        host = _EvidenceHost(api)
        result = host._recover_flat_compensation_evidence(
            'BTC/USDT:USDT', 'long', 0.5, 'OPENID1')
        self.assertEqual(evidence, result)
        api.close_position.assert_not_called()
        api.find_compensation_close_progress.assert_called_once_with(
            'BTC/USDT:USDT', 'long', 0.5, 'OPENID1')

    def test_incomplete_or_ambiguous_evidence_is_discarded(self):
        for bad in (None, {'confirmed': True, 'fully_closed': False},
                    {'confirmed': True, 'fully_closed': True,
                     'remaining_amount': 0.0,
                     'execution_ambiguous': True},
                    {'fully_closed': True},
                    {'confirmed': None, 'fully_closed': True},
                    {'confirmed': 'true', 'fully_closed': True},
                    {'confirmed': False, 'fully_closed': True},
                    'not-a-dict'):
            api = Mock(
                spec=['close_position', 'find_compensation_close_progress'])
            api.find_compensation_close_progress.return_value = bad
            host = _EvidenceHost(api)
            with self.subTest(bad=bad):
                self.assertIsNone(host._recover_flat_compensation_evidence(
                    'BTC/USDT:USDT', 'long', 0.5, 'OPENID1'))
                api.close_position.assert_not_called()

    def test_missing_open_client_id_short_circuits_without_queries(self):
        api = Mock(
            spec=['close_position', 'find_compensation_close_progress'])
        host = _EvidenceHost(api)
        self.assertIsNone(host._recover_flat_compensation_evidence(
            'BTC/USDT:USDT', 'long', 0.5, None))
        api.find_compensation_close_progress.assert_not_called()
        api.close_position.assert_not_called()


if __name__ == '__main__':
    unittest.main()
