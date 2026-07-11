"""部分成交后账本、手续费与保护性止损必须同步到交易所现实。"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trade_executor import TradeExecutorMixin
from stop_guardian import StopGuardianMixin
from trade_state import TradeState, TradeStatePersistenceError


class _System(TradeExecutorMixin):
    pass


class _GuardianSystem(StopGuardianMixin, TradeExecutorMixin):
    pass


class PartialCloseReconciliationTest(unittest.TestCase):
    def _system(self, temp_dir, *, new_stop=True, old_cancel=True):
        system = _System()
        system.trade_state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
        system.trade_state.add_open_position(
            'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-old', strategy='turtle')
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT',
            get_last_price=lambda _symbol: 105.0,
            create_stop_loss_order=Mock(
                return_value={'id': 'stop-new'} if new_stop else None),
            cancel_stop_order_only=Mock(return_value=old_cancel),
            cancel_order=Mock(return_value=old_cancel),
        )
        system.notifier = SimpleNamespace(notify_error=Mock())
        system._stop_anomalies = {}
        system._notify_trade_state_persistence_issue = Mock()
        return system

    def test_partial_close_replaces_stop_and_atomically_shrinks_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = self._system(temp_dir)
            order = {
                'id': 'close-1', 'ids': ['close-1'], 'average': 110.0,
                'amount': 0.4, 'remaining_amount': 0.6,
                'fully_closed': False,
                'fee': {'cost': 0.01, 'currency': 'USDT'},
            }

            self.assertTrue(system._handle_partial_close(
                'BTCUSDT', order, system.trade_state.get_open_position('BTCUSDT'),
                '测试平仓'))

            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertAlmostEqual(position['position_size'], 0.6)
            self.assertEqual(position['stop_order_id'], 'stop-new')
            self.assertAlmostEqual(position['stop_order_size'], 0.6)
            self.assertFalse(position['stop_resize_pending'])
            self.assertAlmostEqual(position['partial_closes'][0]['exit_fee'], 0.01)
            system.exchange_api.cancel_stop_order_only.assert_called_once_with(
                'BTC/USDT:USDT', 'stop-old')
            system.exchange_api.cancel_order.assert_not_called()

    def test_partial_close_marks_unknown_stop_before_new_stop_post(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = self._system(temp_dir)

            def create_after_marker(*_args):
                self.assertTrue(
                    system.trade_state.has_stop_residue('BTCUSDT'))
                return {'id': 'stop-new'}

            system.exchange_api.create_stop_loss_order.side_effect = (
                create_after_marker)
            order = {
                'id': 'close-1', 'average': 110.0, 'amount': 0.4,
                'remaining_amount': 0.6, 'fully_closed': False,
            }

            self.assertTrue(system._handle_partial_close(
                'BTCUSDT', order,
                system.trade_state.get_open_position('BTCUSDT'),
                '测试平仓'))
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))

    def test_submit_close_persists_intent_before_exchange_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = self._system(temp_dir)

            def close_after_persist(
                    _symbol, _side, _amount, client_order_id=None):
                persisted = TradeState(
                    system.trade_state.state_file).get_close_intent('BTCUSDT')
                self.assertIsNotNone(persisted)
                self.assertEqual(
                    persisted['client_order_id'], client_order_id)
                return {
                    'id': 'close-1', 'average': 105.0,
                    'fully_closed': True, 'amount': 1.0,
                    'remaining_amount': 0.0,
                }

            system.exchange_api.close_position = Mock(
                side_effect=close_after_persist)
            order = system._submit_persisted_close(
                'BTCUSDT', 'BTC/USDT:USDT',
                system.trade_state.get_open_position('BTCUSDT'),
                '测试主动平仓')

            intent = system.trade_state.get_close_intent('BTCUSDT')
            self.assertEqual(
                intent['client_order_id'],
                order['close_intent_client_id'])

    def test_guardian_recovers_full_close_intent_with_real_execution_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _GuardianSystem()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.trade_state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-old',
                strategy='turtle')
            intent = system.trade_state.prepare_close_intent(
                'BTCUSDT', 'CloseRecover123', '信号平仓')
            system.exchange_api = SimpleNamespace(
                to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT',
                close_position=Mock(return_value={
                    'id': 'close-real', 'ids': ['close-real'],
                    'average': 107.0, 'fully_closed': True,
                    'amount': 1.0, 'remaining_amount': 0.0,
                    'fee': {'cost': 0.07, 'currency': 'USDT'},
                }),
                get_last_price=Mock(return_value=106.0),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._stop_anomalies = {}
            system._cancel_stop_order_confirmed = Mock(return_value=True)
            system._notify_trade_state_persistence_issue = Mock()

            status = system._resume_persisted_close_intent(
                'BTCUSDT',
                system.trade_state.get_open_position('BTCUSDT'),
                '启动对账')

            self.assertEqual('closed', status)
            self.assertIsNone(system.trade_state.get_open_position('BTCUSDT'))
            closed = system.trade_state.get_closed_trades()[-1]
            self.assertEqual(107.0, closed['exit_price'])
            self.assertEqual(0.07, closed['exit_fee'])
            self.assertEqual(['close-real'], closed['exit_order_ids'])
            system.exchange_api.close_position.assert_called_once_with(
                'BTC/USDT:USDT', 'long', 1.0,
                client_order_id=intent['client_order_id'])

    def test_failed_stop_resize_keeps_old_reduce_only_stop_and_marks_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = self._system(temp_dir, new_stop=False)
            order = {
                'id': 'close-1', 'average': 110.0, 'amount': 0.4,
                'remaining_amount': 0.6, 'fully_closed': False,
            }

            self.assertTrue(system._handle_partial_close(
                'BTCUSDT', order, system.trade_state.get_open_position('BTCUSDT'),
                '测试平仓'))

            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertAlmostEqual(position['position_size'], 0.6)
            self.assertEqual(position['stop_order_id'], 'stop-old')
            self.assertAlmostEqual(position['stop_order_size'], 1.0)
            self.assertTrue(position['stop_resize_pending'])
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))
            system.exchange_api.cancel_order.assert_not_called()

    def test_resize_retry_keeps_residue_until_full_list_verifies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _GuardianSystem()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.trade_state.add_open_position(
                'BTCUSDT', 'long', 100.0, 0.6, 90.0, 'stop-old',
                strategy='turtle')
            system.trade_state.update_stop_loss(
                'BTCUSDT', 90.0, 'stop-old', stop_order_size=1.0,
                stop_resize_pending=True)
            system.trade_state.mark_stop_residue('BTCUSDT')
            system._stop_anomalies = {}
            system.notifier = SimpleNamespace(
                notify_error=Mock(), send_message=Mock())
            system._notify_trade_state_persistence_issue = Mock()
            system.exchange_api = SimpleNamespace(
                create_stop_loss_order=Mock(return_value={'id': 'stop-new'}),
                cancel_stop_order_only=Mock(return_value=True),
                find_stop_order_state=Mock(
                    side_effect=RuntimeError('完整算法单清单暂不可用')),
            )

            protected = system._ensure_stop_order_alive(
                'BTCUSDT', 'BTC/USDT:USDT',
                system.trade_state.get_open_position('BTCUSDT'), '海龟通道')

            self.assertFalse(protected)
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))

    def test_resize_retry_clears_old_ids_then_full_list_releases_residue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _GuardianSystem()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.trade_state.add_open_position(
                'BTCUSDT', 'long', 100.0, 0.6, 90.0, 'stop-new',
                strategy='turtle')
            system.trade_state.update_stop_loss(
                'BTCUSDT', 90.0, 'stop-new', stop_order_size=0.6,
                extra_stop_order_ids=['stop-old'], stop_resize_pending=True)
            system.trade_state.mark_stop_residue('BTCUSDT')
            system._stop_anomalies = {}
            system.notifier = SimpleNamespace(
                notify_error=Mock(), send_message=Mock())
            system._notify_trade_state_persistence_issue = Mock()
            system.exchange_api = SimpleNamespace(
                create_stop_loss_order=Mock(return_value={'id': 'stop-new'}),
                cancel_stop_order_only=Mock(return_value=True),
                find_stop_order_state=Mock(return_value='intact'),
            )

            protected = system._ensure_stop_order_alive(
                'BTCUSDT', 'BTC/USDT:USDT',
                system.trade_state.get_open_position('BTCUSDT'), '海龟通道')

            self.assertTrue(protected)
            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertFalse(position['stop_resize_pending'])
            self.assertEqual([], position['extra_stop_order_ids'])
            self.assertFalse(system.trade_state.has_stop_residue('BTCUSDT'))

    def test_uncancelled_old_stop_is_tracked_as_extra_residue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = self._system(temp_dir, old_cancel=False)
            order = {
                'id': 'close-1', 'average': 110.0, 'amount': 0.4,
                'remaining_amount': 0.6, 'fully_closed': False,
            }

            self.assertTrue(system._handle_partial_close(
                'BTCUSDT', order, system.trade_state.get_open_position('BTCUSDT'),
                '测试平仓'))

            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual(position['extra_stop_order_ids'], ['stop-old'])
            self.assertTrue(position['stop_resize_pending'])
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))

    def test_exchange_remaining_amount_is_persisted_without_double_float_subtraction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = self._system(temp_dir)
            # 三张、余一张是曾经会被两次 float 相减写成 0.9999999999999999 张的反例。
            system.trade_state.state['open_positions']['BTCUSDT']['position_size'] = 0.0003
            system.trade_state.state['open_positions']['BTCUSDT']['original_position_size'] = 0.0003
            system.trade_state.state['open_positions']['BTCUSDT']['stop_order_size'] = 0.0003
            order = {
                'id': 'close-tiny', 'average': 110.0, 'amount': 0.0002,
                'remaining_amount': 0.0001, 'fully_closed': False,
            }

            self.assertTrue(system._handle_partial_close(
                'BTCUSDT', order, system.trade_state.get_open_position('BTCUSDT'),
                '微量测试平仓'))

            remaining = system.trade_state.get_open_position('BTCUSDT')['position_size']
            self.assertEqual(Decimal('1'), Decimal(str(remaining)) / Decimal('0.0001'))

    def test_second_partial_close_preserves_all_older_stop_residue_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = self._system(temp_dir, old_cancel=False)
            first = {
                'id': 'close-1', 'average': 105.0, 'amount': 0.2,
                'remaining_amount': 0.8, 'fully_closed': False,
            }
            self.assertTrue(system._handle_partial_close(
                'BTCUSDT', first, system.trade_state.get_open_position('BTCUSDT'),
                '第一次部分平仓'))

            system.exchange_api.create_stop_loss_order.return_value = {'id': 'stop-newer'}
            system.exchange_api.cancel_stop_order_only.return_value = True
            second = {
                'id': 'close-2', 'average': 106.0, 'amount': 0.2,
                'remaining_amount': 0.6, 'fully_closed': False,
            }
            self.assertTrue(system._handle_partial_close(
                'BTCUSDT', second, system.trade_state.get_open_position('BTCUSDT'),
                '第二次部分平仓'))

            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual('stop-newer', position['stop_order_id'])
            self.assertEqual([], position['extra_stop_order_ids'])
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))

    def test_runtime_only_partial_reconciliation_is_not_reported_as_durable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = self._system(temp_dir)
            order = {
                'id': 'close-1', 'average': 110.0, 'amount': 0.4,
                'remaining_amount': 0.6, 'fully_closed': False,
            }
            with patch.object(
                    system.trade_state, 'apply_partial_close',
                    side_effect=TradeStatePersistenceError('disk full')):
                self.assertFalse(system._handle_partial_close(
                    'BTCUSDT', order,
                    system.trade_state.get_open_position('BTCUSDT'), '测试平仓'))
            self.assertAlmostEqual(
                0.6, system.trade_state.get_open_position('BTCUSDT')['position_size'])
            system._notify_trade_state_persistence_issue.assert_called_once()

    def test_partial_emergency_rollback_builds_protected_ledger_with_real_fees(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _System()
            path = str(Path(temp_dir) / 'trade_state.json')
            system.trade_state = TradeState(path)
            system.trade_state.replace_stop_loss_dates({'BTCUSDT': '2026-07-10'})
            system.stop_loss_dates = {'BTCUSDT': '2026-07-10'}
            system.exchange_api = SimpleNamespace(
                get_last_price=Mock(return_value=102.0),
                create_stop_loss_order=Mock(return_value={'id': 'stop-emergency'}),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()
            open_order = {
                'id': 'open-1', 'average': 100.0,
                'fee': {'cost': 0.05, 'currency': 'USDT'},
            }
            rollback = {
                'id': 'close-1', 'average': 101.0, 'fully_closed': False,
                'amount': 0.6, 'remaining_amount': 0.4,
                'fee': {'cost': 0.03, 'currency': 'USDT'},
            }

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 90.0,
                'turtle', open_order, rollback, '测试紧急回滚')

            self.assertEqual('rollback_incomplete', outcome['status'])
            self.assertTrue(outcome['residual_ledger_reconciled'])
            self.assertTrue(outcome['state_saved'])
            self.assertTrue(outcome['residual_stop_protected'])
            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual(0.4, position['position_size'])
            self.assertEqual('stop-emergency', position['stop_order_id'])
            self.assertEqual(0.4, position['stop_order_size'])
            self.assertEqual(0.05, position['entry_fee'])
            self.assertEqual(['open-1'], position['entry_order_ids'])
            self.assertEqual(0.03, position['partial_closes'][0]['exit_fee'])
            self.assertEqual(['close-1'], position['partial_closes'][0]['exit_order_ids'])
            self.assertTrue(system.trade_state.is_position_quarantined('BTCUSDT'))
            self.assertNotIn('BTCUSDT', system.trade_state.get_stop_loss_dates())
            self.assertNotIn('BTCUSDT', system.stop_loss_dates)
            reloaded = TradeState(path)
            self.assertEqual(
                0.4, reloaded.get_open_position('BTCUSDT')['position_size'])
            self.assertTrue(reloaded.is_position_quarantined('BTCUSDT'))

    def test_partial_emergency_rollback_without_stop_is_ledgered_and_quarantined(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _System()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.exchange_api = SimpleNamespace(
                get_last_price=Mock(return_value=79.0),
                create_stop_loss_order=Mock(return_value=None),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 80.0,
                'turtle', {'id': 'open-1'},
                {'id': 'close-1', 'average': 79.0, 'fully_closed': False,
                 'amount': 0.5, 'remaining_amount': 0.5},
                '止损失效回滚', allow_stop_rebuild=False)

            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual('rollback_incomplete', outcome['status'])
            self.assertTrue(outcome['residual_ledger_reconciled'])
            self.assertFalse(outcome['residual_stop_protected'])
            self.assertIsNone(position['stop_order_id'])
            self.assertTrue(position['stop_resize_pending'])
            self.assertTrue(system.trade_state.is_position_quarantined('BTCUSDT'))
            system.exchange_api.create_stop_loss_order.assert_not_called()

    def test_uncertain_initial_stop_survives_partial_rollback_as_residue_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _System()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.exchange_api = SimpleNamespace(
                get_last_price=Mock(return_value=99.0),
                create_stop_loss_order=Mock(return_value={'id': 'stop-residual'}),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 90.0,
                'turtle', {'id': 'open-1'},
                {'id': 'close-1', 'average': 99.0, 'fully_closed': False,
                 'amount': 0.6, 'remaining_amount': 0.4},
                '止损确认不确定后的回滚', stop_residue_possible=True)

            self.assertEqual('rollback_incomplete', outcome['status'])
            self.assertTrue(outcome['residual_stop_protected'])
            self.assertEqual(
                'stop-residual',
                system.trade_state.get_open_position('BTCUSDT')['stop_order_id'])
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))

    def test_zero_fill_compensation_builds_full_residual_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _System()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.exchange_api = SimpleNamespace(
                get_last_price=Mock(return_value=100.0),
                create_stop_loss_order=Mock(return_value={'id': 'stop-emergency'}),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 90.0,
                'turtle', {'id': 'open-1'},
                {'id': 'close-zero', 'fully_closed': False,
                 'amount': 0.0, 'remaining_amount': 1.0},
                '补偿零成交')

            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual('rollback_incomplete', outcome['status'])
            self.assertTrue(outcome['residual_ledger_reconciled'])
            self.assertEqual(1.0, position['position_size'])
            self.assertEqual('stop-emergency', position['stop_order_id'])
            self.assertTrue(position['recovered_unresolved_open'])
            self.assertEqual([], position.get('partial_closes', []))

    def test_partial_residual_atomically_consumes_generic_open_intent(self):
        """余仓先被平掉、恢复器后运行时也不能再补一笔重复往返。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / 'trade_state.json')
            system = _System()
            system.trade_state = TradeState(path)
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IRESIDUAL1',
                {'entry_price': 100.0, 'stop_loss_price': 90.0},
                planned_position_size=1.0)
            system.exchange_api = SimpleNamespace(
                get_last_price=Mock(return_value=101.0),
                create_stop_loss_order=Mock(return_value={'id': 'stop-residual'}),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 90.0,
                'ma_cross', {'id': 'open-1'},
                {'id': 'close-partial', 'average': 101.0,
                 'fully_closed': False, 'amount': 0.6,
                 'remaining_amount': 0.4},
                'generic open intent 部分回滚',
                open_intent_client_id='IRESIDUAL1')

            self.assertEqual('rollback_incomplete', outcome['status'])
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual('IRESIDUAL1', position['client_order_id'])

            closed = system.trade_state.close_position(
                'BTCUSDT', 102.0, exit_order_ids=['close-final'])
            self.assertEqual('IRESIDUAL1', closed['client_order_id'])
            self.assertEqual(1, len(system.trade_state.get_closed_trades()))
            with self.assertRaises(TradeStatePersistenceError):
                system.trade_state.finalize_open_intent_round_trip(
                    'BTCUSDT', 'IRESIDUAL1', 100.0, 102.0, 1.0)
            self.assertEqual(
                1, len(TradeState(path).get_closed_trades()))

    def test_full_residual_atomically_consumes_generic_open_intent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _System()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IFULLRESIDUAL',
                {'entry_price': 100.0, 'stop_loss_price': 90.0},
                planned_position_size=1.0)
            system.exchange_api = SimpleNamespace(
                get_last_price=Mock(return_value=100.0),
                create_stop_loss_order=Mock(return_value={'id': 'stop-emergency'}),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 90.0,
                'ma_cross', {'id': 'open-1'},
                {'id': 'close-zero', 'fully_closed': False,
                 'amount': 0.0, 'remaining_amount': 1.0},
                'generic open intent 零成交回滚',
                open_intent_client_id='IFULLRESIDUAL')

            self.assertEqual('rollback_incomplete', outcome['status'])
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual('IFULLRESIDUAL', position['client_order_id'])
            self.assertTrue(position['recovered_unresolved_open'])

    def test_missing_rollback_result_requeries_exchange_and_builds_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _System()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.exchange_api = SimpleNamespace(
                get_position=Mock(return_value={
                    'side': 'long', 'contracts': 10.0}),
                _contracts_to_coins=lambda _symbol, contracts: contracts * 0.1,
                get_last_price=Mock(return_value=100.0),
                create_stop_loss_order=Mock(return_value={'id': 'stop-emergency'}),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 90.0,
                'turtle', {'id': 'open-1'}, None, '回滚结果丢失')

            self.assertEqual('rollback_incomplete', outcome['status'])
            self.assertEqual(
                1.0, system.trade_state.get_open_position('BTCUSDT')['position_size'])

    def test_persistence_failure_partial_rollback_keeps_old_stop_and_runtime_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _System()
            state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            state.replace_stop_loss_dates({'BTCUSDT': '2026-07-10'})
            system.trade_state = state
            system.stop_loss_dates = {'BTCUSDT': '2026-07-10'}
            rollback = {
                'id': 'close-partial', 'average': 99.0,
                'fully_closed': False, 'amount': 0.6,
                'remaining_amount': 0.4,
                'fee': {'cost': 0.02, 'currency': 'USDT'},
            }
            system.exchange_api = SimpleNamespace(
                close_position=Mock(return_value=rollback),
                create_stop_loss_order=Mock(),
                get_last_price=Mock(return_value=99.0),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._cancel_stop_order_confirmed = Mock()
            system._notify_trade_state_persistence_issue = Mock()
            open_order = {
                'id': 'open-1', 'fee': {'cost': 0.04, 'currency': 'USDT'}}

            with patch.object(
                    state, 'add_open_position',
                    side_effect=TradeStatePersistenceError('disk full')), \
                 patch.object(
                    state, 'add_open_after_partial_rollback',
                    side_effect=TradeStatePersistenceError('disk still full')):
                saved = system._persist_open_position_or_rollback(
                    'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0,
                    90.0, 'stop-original', strategy='turtle',
                    open_order=open_order)

            self.assertEqual('rollback_incomplete', saved['status'])
            position = state.get_open_position('BTCUSDT')
            self.assertEqual(0.4, position['position_size'])
            self.assertEqual('stop-original', position['stop_order_id'])
            self.assertEqual(1.0, position['stop_order_size'])
            self.assertTrue(position['stop_resize_pending'])
            self.assertEqual(0.02, position['partial_closes'][0]['exit_fee'])
            self.assertTrue(state.is_position_quarantined('BTCUSDT'))
            self.assertNotIn('BTCUSDT', state.get_stop_loss_dates())
            self.assertNotIn('BTCUSDT', system.stop_loss_dates)
            system.exchange_api.create_stop_loss_order.assert_not_called()
            system._cancel_stop_order_confirmed.assert_not_called()
            system._notify_trade_state_persistence_issue.assert_called_once()

    def test_normal_open_atomically_clears_stale_t1_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            state.replace_stop_loss_dates({'BTCUSDT': '2026-07-10'})

            state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0,
                'stop-1', strategy='ma_cross')

            self.assertNotIn('BTCUSDT', state.get_stop_loss_dates())
            self.assertNotIn(
                'BTCUSDT', TradeState(state.state_file).get_stop_loss_dates())

    def test_successful_open_syncs_in_memory_t1_mirror_without_second_save(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _System()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.trade_state.replace_stop_loss_dates(
                {'BTCUSDT': '2026-07-10'})
            system.stop_loss_dates = {'BTCUSDT': '2026-07-10'}
            system.notifier = SimpleNamespace(notify_error=Mock())

            self.assertTrue(system._persist_open_position_or_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0,
                90.0, 'stop-1', strategy='ma_cross',
                open_order={'id': 'open-1'}))

            self.assertNotIn('BTCUSDT', system.stop_loss_dates)
            self.assertNotIn(
                'BTCUSDT', system.trade_state.get_stop_loss_dates())


if __name__ == '__main__':
    unittest.main()
