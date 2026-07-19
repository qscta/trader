"""部分成交后账本、手续费与保护性止损必须同步到交易所现实。"""

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import trade_state as trade_state_module
from trade_executor import TradeExecutorMixin
from stop_guardian import StopGuardianMixin
from trade_state import TRADING_FEE_RATE, TradeState, TradeStatePersistenceError


class _System(TradeExecutorMixin):
    pass


class _GuardianSystem(StopGuardianMixin, TradeExecutorMixin):
    pass


class PartialCloseReconciliationTest(unittest.TestCase):
    @staticmethod
    def _observe_exact_position(system, amount, side='long'):
        system.exchange_api.get_position = Mock(return_value={
            'side': side, 'contracts': float(amount)})
        system.exchange_api._contracts_to_coins = (
            lambda _symbol, contracts: float(contracts))

    def _system(self, temp_dir, *, new_stop=True, old_cancel=True):
        system = _System()
        system.trade_state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
        system.trade_state.add_open_position(
            'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-old', strategy='ma_cross')
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

    def test_partial_fill_non_usdt_fee_falls_back_to_estimate_and_persists(self):
        """POST 已成交后收到 BTC 手续费，也必须安全收口为 estimated。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            system = self._system(temp_dir)
            order = {
                'id': 'close-btc-fee', 'average': 110.0,
                'amount': 0.4, 'remaining_amount': 0.6,
                'fully_closed': False,
                'fee': {'cost': 0.0001, 'currency': 'BTC'},
            }

            self.assertTrue(system._handle_partial_close(
                'BTCUSDT', order,
                system.trade_state.get_open_position('BTCUSDT'),
                '非 USDT 手续费测试'))

            partial = system.trade_state.get_open_position(
                'BTCUSDT')['partial_closes'][0]
            self.assertEqual('estimated', partial['fee_source'])
            self.assertNotIn('exit_fee_currency', partial)
            self.assertAlmostEqual(
                110.0 * 0.4 * TRADING_FEE_RATE, partial['exit_fee'])
            reloaded = TradeState(system.trade_state.state_file)
            TradeState.validate_state(reloaded.state)
            self.assertEqual(
                'estimated', reloaded.get_open_position(
                    'BTCUSDT')['partial_closes'][0]['fee_source'])

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
            self.assertFalse(system.trade_state.has_stop_residue('BTCUSDT'))

    def test_submit_close_persists_intent_before_exchange_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = self._system(temp_dir)

            def close_after_persist(
                    _symbol, _side, _amount, client_order_id=None,
                    require_existing=False):
                persisted = TradeState(
                    system.trade_state.state_file).get_close_intent('BTCUSDT')
                self.assertIsNotNone(persisted)
                self.assertEqual(
                    persisted['client_order_id'], client_order_id)
                self.assertFalse(require_existing)
                return {
                    'id': 'close-1', 'average': 105.0,
                    'confirmed': True, 'fully_closed': True, 'amount': 1.0,
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
                strategy='ma_cross')
            intent = system.trade_state.prepare_close_intent(
                'BTCUSDT', 'CloseRecover123', '信号平仓')
            system.exchange_api = SimpleNamespace(
                to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT',
                close_position=Mock(return_value={
                    'id': 'close-real', 'ids': ['close-real'],
                    'average': 107.0, 'confirmed': True,
                    'fully_closed': True,
                    'amount': 1.0, 'remaining_amount': 0.0,
                    'fee': {'cost': 0.07, 'currency': 'USDT'},
                }),
                get_last_price=Mock(return_value=106.0),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._stop_anomalies = {}
            system._exchange_position_has_contracts = (
                lambda position: bool(
                    position and float(position.get('contracts') or 0) != 0))
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
                client_order_id=intent['client_order_id'],
                require_existing=True)

    def test_flip_stop_triggered_before_close_post_records_t1_and_never_reopens(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _GuardianSystem()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.trade_state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-old',
                strategy='ma_cross')
            old_position = system.trade_state.get_open_position('BTCUSDT')
            system.config = {
                'trading': {'symbols': [
                    {'name': 'BTCUSDT', 'enabled': True}]}}
            system.stop_loss_dates = {}
            system.exchange_api = SimpleNamespace(
                to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT',
                close_position=Mock(return_value={
                    'definitely_no_post': True,
                    'position_flat_before_post': True,
                }),
                confirm_stop_execution=Mock(return_value=True),
                get_position=Mock(return_value=None),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._stop_anomalies = {}
            system._exchange_position_has_contracts = (
                lambda position: bool(
                    position and float(position.get('contracts') or 0) != 0))
            system._cancel_stop_order_confirmed = Mock(return_value=True)
            system._notify_trade_state_persistence_issue = Mock()
            system._buffer_trade_close_notification = Mock()
            system._execute_open = Mock()

            system._flip_position(
                'BTCUSDT',
                {'current_close': 89.0, 'lower_stop': 80.0,
                 'upper_stop': 110.0},
                old_position, 'short', {'name': 'BTCUSDT'})

            system.exchange_api.close_position.assert_called_once_with(
                'BTC/USDT:USDT', 'long', 1.0,
                client_order_id=unittest.mock.ANY,
                require_existing=False)
            system._execute_open.assert_not_called()
            self.assertIsNone(system.trade_state.get_open_position('BTCUSDT'))
            closed = system.trade_state.get_closed_trades()[-1]
            self.assertEqual('estimated_stop', closed['exit_price_source'])
            today = date.today().strftime('%Y-%m-%d')
            self.assertEqual(
                today, system.trade_state.get_stop_loss_dates()['BTCUSDT'])
            self.assertEqual(today, system.stop_loss_dates['BTCUSDT'])

    def test_resume_stop_triggered_before_close_post_atomically_records_t1(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _GuardianSystem()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.trade_state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-old',
                strategy='ma_cross')
            intent = system.trade_state.prepare_close_intent(
                'BTCUSDT', 'CloseStopRace123', '双均线翻转平仓')
            system.config = {
                'trading': {'symbols': [
                    {'name': 'BTCUSDT', 'enabled': True}]}}
            system.stop_loss_dates = {}
            system.exchange_api = SimpleNamespace(
                to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT',
                close_position=Mock(return_value={
                    'definitely_no_post': True,
                    'position_flat_before_post': True,
                }),
                confirm_stop_execution=Mock(return_value=True),
                get_position=Mock(return_value=None),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._stop_anomalies = {}
            system._exchange_position_has_contracts = (
                lambda position: bool(
                    position and float(position.get('contracts') or 0) != 0))
            system._cancel_stop_order_confirmed = Mock(return_value=True)
            system._notify_trade_state_persistence_issue = Mock()

            status = system._resume_persisted_close_intent(
                'BTCUSDT',
                system.trade_state.get_open_position('BTCUSDT'),
                '启动对账')

            self.assertEqual('closed', status)
            system.exchange_api.close_position.assert_called_once_with(
                'BTC/USDT:USDT', 'long', 1.0,
                client_order_id=intent['client_order_id'],
                require_existing=True)
            today = date.today().strftime('%Y-%m-%d')
            self.assertEqual(
                today, system.trade_state.get_stop_loss_dates()['BTCUSDT'])
            self.assertEqual(today, system.stop_loss_dates['BTCUSDT'])

    def test_recent_close_intent_absent_consumes_only_intent_and_ends_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _GuardianSystem()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.trade_state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-old',
                strategy='ma_cross')
            intent = system.trade_state.prepare_close_intent(
                'BTCUSDT', 'CloseCrashBeforeAdapter', '信号平仓')
            system.exchange_api = SimpleNamespace(
                to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT',
                close_position=Mock(return_value={
                    'confirmed': False, 'definitely_no_post': True,
                    'close_order_absent': True,
                    'position_unchanged': True,
                    'amount': 0.0, 'remaining_amount': 1.0,
                }),
            )
            system._pending_order_absence_is_conclusive = Mock(
                return_value=(True, None))
            system._ensure_stop_order_alive = Mock()
            system.notifier = SimpleNamespace(notify_error=Mock())

            status = system._resume_persisted_close_intent(
                'BTCUSDT',
                system.trade_state.get_open_position('BTCUSDT'),
                '启动对账')

            self.assertEqual('zero_fill_resolved', status)
            self.assertIsNone(system.trade_state.get_close_intent('BTCUSDT'))
            self.assertIsNotNone(system.trade_state.get_open_position('BTCUSDT'))
            system.exchange_api.close_position.assert_called_once_with(
                'BTC/USDT:USDT', 'long', 1.0,
                client_order_id=intent['client_order_id'],
                require_existing=True)
            system._ensure_stop_order_alive.assert_not_called()

    def test_historical_close_absent_keeps_blocker_but_maintains_exact_stop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _GuardianSystem()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.trade_state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, None,
                strategy='ma_cross')
            intent = system.trade_state.prepare_close_intent(
                'BTCUSDT', 'CloseHistoricalAbsent', '信号平仓')
            system.exchange_api = SimpleNamespace(
                to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT',
                close_position=Mock(return_value={
                    'confirmed': False, 'definitely_no_post': True,
                    'close_order_absent': True,
                    'position_unchanged': True,
                    'amount': 0.0, 'remaining_amount': 1.0,
                }),
                get_position=Mock(return_value={
                    'side': 'long', 'contracts': 1.0}),
                _contracts_to_coins=lambda _symbol, value: float(value),
            )
            system._pending_order_absence_is_conclusive = Mock(
                return_value=(False, '已超过安全窗口'))
            system._ensure_stop_order_alive = Mock(return_value=True)
            system._quarantine_position_mismatch = Mock()
            system.notifier = SimpleNamespace(notify_error=Mock())

            status = system._resume_persisted_close_intent(
                'BTCUSDT',
                system.trade_state.get_open_position('BTCUSDT'),
                '日检对账')

            self.assertEqual('unresolved', status)
            self.assertEqual(
                intent['client_order_id'],
                system.trade_state.get_close_intent(
                    'BTCUSDT')['client_order_id'])
            self.assertIsNotNone(system.trade_state.get_open_position('BTCUSDT'))
            system._ensure_stop_order_alive.assert_called_once()

    def test_persisted_close_none_and_flat_keeps_intent_and_quarantine(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _System()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.trade_state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-old',
                strategy='ma_cross')
            intent = system.trade_state.prepare_close_intent(
                'BTCUSDT', 'CloseFlatUnresolved', '信号平仓')
            system.exchange_api = SimpleNamespace(
                to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT',
                close_position=Mock(return_value=None),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._cancel_stop_order_confirmed = Mock()

            def quarantine(symbol, reason, details):
                system.trade_state.mark_position_quarantine(
                    symbol, reason, details)

            system._quarantine_position_mismatch = quarantine

            status = system._resume_persisted_close_intent(
                'BTCUSDT',
                system.trade_state.get_open_position('BTCUSDT'),
                '启动对账')

            self.assertEqual('unresolved', status)
            self.assertIsNotNone(
                system.trade_state.get_open_position('BTCUSDT'))
            self.assertEqual(
                intent['client_order_id'],
                system.trade_state.get_close_intent(
                    'BTCUSDT')['client_order_id'])
            self.assertTrue(
                system.trade_state.is_position_quarantined('BTCUSDT'))
            system._cancel_stop_order_confirmed.assert_not_called()

    def test_close_intent_recovery_preserves_ledger_on_unproven_result(self):
        malformed = (
            {'fully_closed': True},
            {'confirmed': None, 'fully_closed': True},
            {'confirmed': 'true', 'fully_closed': True},
            {'confirmed': False, 'fully_closed': True},
            {'confirmed': True},
            {'confirmed': True, 'fully_closed': 'true'},
        )
        for close_result in malformed:
            with self.subTest(close_result=close_result), \
                    tempfile.TemporaryDirectory() as temp_dir:
                system = _System()
                system.trade_state = TradeState(
                    str(Path(temp_dir) / 'trade_state.json'))
                system.trade_state.add_open_position(
                    'BTCUSDT', 'long', 100.0, 1.0, 90.0,
                    'stop-old', strategy='ma_cross')
                system.trade_state.prepare_close_intent(
                    'BTCUSDT', 'CloseMalformed123', '测试平仓')
                system.exchange_api = SimpleNamespace(
                    to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT')
                system._submit_persisted_close = Mock(
                    return_value=close_result)
                system._quarantine_position_mismatch = Mock()
                system._cancel_stop_order_confirmed = Mock()
                system.notifier = SimpleNamespace(notify_error=Mock())

                status = system._resume_persisted_close_intent(
                    'BTCUSDT',
                    system.trade_state.get_open_position('BTCUSDT'),
                    '启动对账')

                self.assertEqual('unresolved', status)
                self.assertIsNotNone(
                    system.trade_state.get_open_position('BTCUSDT'))
                self.assertIsNotNone(
                    system.trade_state.get_close_intent('BTCUSDT'))
                system._cancel_stop_order_confirmed.assert_not_called()
                system._quarantine_position_mismatch.assert_called_once()

    def test_full_close_non_usdt_fee_cannot_break_post_action_ledger_commit(self):
        """真实平仓调用已返回后，非 USDT fee 只能估算，不能让账本事务失败。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _GuardianSystem()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.trade_state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-old',
                strategy='ma_cross', entry_fee=0.0002,
                entry_fee_currency='BTC')
            intent = system.trade_state.prepare_close_intent(
                'BTCUSDT', 'CloseNonUsdt123', '信号平仓')
            system.exchange_api = SimpleNamespace(
                to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT',
                close_position=Mock(return_value={
                    'id': 'close-non-usdt', 'average': 107.0,
                    'confirmed': True, 'fully_closed': True,
                    'amount': 1.0,
                    'remaining_amount': 0.0,
                    'fee': {'cost': 0.0001, 'currency': 'BTC'},
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
            closed = system.trade_state.get_closed_trades()[-1]
            self.assertEqual('estimated', closed['fee_source'])
            self.assertNotIn('entry_fee_source', closed)
            self.assertNotIn('entry_fee_currency', closed)
            self.assertNotIn('exit_fee_currency', closed)
            self.assertAlmostEqual(
                (100.0 + 107.0) * TRADING_FEE_RATE,
                closed['total_fee'])
            reloaded = TradeState(system.trade_state.state_file)
            TradeState.validate_state(reloaded.state)
            self.assertEqual(
                'estimated', reloaded.get_closed_trades()[-1]['fee_source'])
            system.exchange_api.close_position.assert_called_once_with(
                'BTC/USDT:USDT', 'long', 1.0,
                client_order_id=intent['client_order_id'],
                require_existing=True)

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
                strategy='ma_cross')
            system.trade_state.update_stop_loss(
                'BTCUSDT', 90.0, 'stop-old', stop_order_size=1.0,
                stop_resize_pending=True)
            system.trade_state.mark_stop_residue('BTCUSDT')
            system.STOP_RESIDUE_VISIBILITY_GRACE_SECONDS = 0
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
                system.trade_state.get_open_position('BTCUSDT'), '双均线 EMA')

            self.assertFalse(protected)
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))

    def test_resize_retry_clears_old_ids_then_full_list_releases_residue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _GuardianSystem()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.trade_state.add_open_position(
                'BTCUSDT', 'long', 100.0, 0.6, 90.0, 'stop-new',
                strategy='ma_cross')
            system.trade_state.update_stop_loss(
                'BTCUSDT', 90.0, 'stop-new', stop_order_size=0.6,
                extra_stop_order_ids=['stop-old'], stop_resize_pending=True)
            system.trade_state.mark_stop_residue('BTCUSDT')
            system.STOP_RESIDUE_VISIBILITY_GRACE_SECONDS = 0
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
                system.trade_state.get_open_position('BTCUSDT'), '双均线 EMA')

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
            self.assertEqual('stop-new', position['stop_order_id'])
            self.assertIn('stop-old', position['extra_stop_order_ids'])
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))
            self.assertEqual(
                1, system.exchange_api.create_stop_loss_order.call_count)

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
            self._observe_exact_position(system, 0.4)
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()
            open_order = {
                'id': 'open-1', 'average': 100.0,
                'fee': {'cost': 0.05, 'currency': 'USDT'},
            }
            rollback = {
                'id': 'close-1', 'average': 101.0,
                'confirmed': True, 'fully_closed': False,
                'amount': 0.6, 'remaining_amount': 0.4,
                'fee': {'cost': 0.03, 'currency': 'USDT'},
            }

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 90.0,
                'ma_cross', open_order, rollback, '测试紧急回滚')

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

    def test_rollback_never_claims_flat_without_strict_confirmation(self):
        malformed = (
            {'fully_closed': True},
            {'confirmed': None, 'fully_closed': True},
            {'confirmed': 'true', 'fully_closed': True},
            {'confirmed': False, 'fully_closed': True},
            {'confirmed': True, 'fully_closed': 'true'},
        )
        for rollback in malformed:
            with self.subTest(rollback=rollback), \
                    tempfile.TemporaryDirectory() as temp_dir:
                system = _System()
                system.trade_state = TradeState(
                    str(Path(temp_dir) / 'trade_state.json'))
                system.notifier = SimpleNamespace(notify_error=Mock())
                system._mark_open_rollback_quarantine = Mock()

                outcome = system._finalize_open_rollback(
                    'BTCUSDT', 'BTC/USDT:USDT', 'long',
                    100.0, 1.0, 90.0, 'ma_cross',
                    {'id': 'open-1'}, rollback, '严格回滚测试')

                self.assertEqual('rollback_incomplete', outcome['status'])
                self.assertFalse(outcome['residual_ledger_reconciled'])
                self.assertIsNone(
                    system.trade_state.get_open_position('BTCUSDT'))
                system._mark_open_rollback_quarantine.assert_called_once()

    def test_partial_emergency_rollback_without_stop_is_ledgered_and_quarantined(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _System()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.exchange_api = SimpleNamespace(
                get_last_price=Mock(return_value=79.0),
                create_stop_loss_order=Mock(return_value=None),
            )
            self._observe_exact_position(system, 0.5)
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 80.0,
                'ma_cross', {'id': 'open-1', 'average': 100.0},
                {'id': 'close-1', 'average': 79.0,
                 'confirmed': True, 'fully_closed': False,
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
            self._observe_exact_position(system, 0.4)
            system.trade_state.mark_stop_residue('BTCUSDT')
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 90.0,
                'ma_cross', {'id': 'open-1', 'average': 100.0},
                {'id': 'close-1', 'average': 99.0,
                 'confirmed': True, 'fully_closed': False,
                 'amount': 0.6, 'remaining_amount': 0.4},
                '止损确认不确定后的回滚', stop_residue_possible=True)

            self.assertEqual('rollback_incomplete', outcome['status'])
            self.assertFalse(outcome['residual_stop_protected'])
            self.assertIsNone(
                system.trade_state.get_open_position('BTCUSDT')['stop_order_id'])
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))
            system.exchange_api.create_stop_loss_order.assert_not_called()

    def test_zero_fill_compensation_builds_full_residual_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _System()
            system.trade_state = TradeState(
                str(Path(temp_dir) / 'trade_state.json'))
            system.exchange_api = SimpleNamespace(
                get_last_price=Mock(return_value=100.0),
                create_stop_loss_order=Mock(return_value={'id': 'stop-emergency'}),
            )
            self._observe_exact_position(system, 1.0)
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 90.0,
                'ma_cross', {'id': 'open-1', 'average': 100.0},
                {'id': 'close-zero', 'confirmed': True,
                 'zero_fill_terminal': True,
                 'fully_closed': False,
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
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=1.0)
            system.exchange_api = SimpleNamespace(
                get_last_price=Mock(return_value=101.0),
                create_stop_loss_order=Mock(return_value={'id': 'stop-residual'}),
            )
            self._observe_exact_position(system, 0.4)
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 90.0,
                'ma_cross', {'id': 'open-1', 'average': 100.0},
                {'id': 'close-partial', 'average': 101.0,
                 'confirmed': True, 'fully_closed': False, 'amount': 0.6,
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
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=1.0)
            system.exchange_api = SimpleNamespace(
                get_last_price=Mock(return_value=100.0),
                create_stop_loss_order=Mock(return_value={'id': 'stop-emergency'}),
            )
            self._observe_exact_position(system, 1.0)
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 90.0,
                'ma_cross', {'id': 'open-1', 'average': 100.0},
                {'id': 'close-zero', 'confirmed': True,
                 'zero_fill_terminal': True,
                 'fully_closed': False,
                 'amount': 0.0, 'remaining_amount': 1.0},
                'generic open intent 零成交回滚',
                open_intent_client_id='IFULLRESIDUAL')

            self.assertEqual('rollback_incomplete', outcome['status'])
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual('IFULLRESIDUAL', position['client_order_id'])
            self.assertTrue(position['recovered_unresolved_open'])

    def test_overfill_partial_compensation_persists_real_residual(self):
        """计划 1.0 却实际成交 1.2 时，部分补偿后的真实余仓不得被 planned 上限丢弃。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / 'trade_state.json')
            system = _System()
            system.trade_state = TradeState(path)
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IOVERPARTIAL1',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=1.0)
            system.exchange_api = SimpleNamespace(
                get_last_price=Mock(return_value=101.0),
                create_stop_loss_order=Mock(
                    return_value={'id': 'stop-overfill-residual'}),
            )
            self._observe_exact_position(system, 0.4)
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.2, 90.0,
                'ma_cross', {'id': 'open-overfill', 'average': 100.0},
                {'id': 'close-partial', 'average': 101.0,
                 'confirmed': True, 'fully_closed': False, 'amount': 0.8,
                 'remaining_amount': 0.4},
                '超量开仓部分补偿',
                open_intent_client_id='IOVERPARTIAL1',
                requested_position_size=1.0)

            self.assertEqual('rollback_incomplete', outcome['status'])
            self.assertTrue(outcome['residual_ledger_reconciled'])
            self.assertTrue(outcome['state_saved'])
            intent = system.trade_state.get_open_intent('BTCUSDT')
            self.assertIsNotNone(intent)
            self.assertEqual(
                'open_attribution', intent['unresolved_execution']['kind'])
            self.assertEqual(
                1.2, intent['unresolved_execution']['expected_position_size'])
            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual(0.4, position['position_size'])
            # 归因未决期只托管 fresh 真实余仓，不把 1.2 伪造成
            # 已有权威财务明细的 original_position_size。
            self.assertEqual(0.4, position['original_position_size'])
            self.assertNotIn('planned_position_size', position)
            self.assertFalse(position.get('recovered_open_overfill', False))
            self.assertFalse(position['execution_recovery_finalized'])
            self.assertEqual([], position.get('partial_closes', []))
            self.assertEqual(
                'stop-overfill-residual', position['stop_order_id'])
            self.assertTrue(system.trade_state.is_position_quarantined('BTCUSDT'))
            self.assertEqual(
                position, TradeState(path).get_open_position('BTCUSDT'))

    def test_overfill_zero_compensation_persists_full_real_position(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / 'trade_state.json')
            system = _System()
            system.trade_state = TradeState(path)
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IOVERZERO1',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=1.0)
            system.exchange_api = SimpleNamespace(
                get_last_price=Mock(return_value=100.0),
                create_stop_loss_order=Mock(
                    return_value={'id': 'stop-overfill-full'}),
            )
            self._observe_exact_position(system, 1.2)
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.2, 90.0,
                'ma_cross', {'id': 'open-overfill', 'average': 100.0},
                {'id': 'close-zero', 'confirmed': True,
                 'zero_fill_terminal': True,
                 'fully_closed': False,
                 'amount': 0.0, 'remaining_amount': 1.2},
                '超量开仓零成交补偿',
                open_intent_client_id='IOVERZERO1',
                requested_position_size=1.0)

            self.assertTrue(outcome['residual_ledger_reconciled'])
            intent = system.trade_state.get_open_intent('BTCUSDT')
            self.assertIsNotNone(intent)
            self.assertEqual(
                'open_attribution', intent['unresolved_execution']['kind'])
            self.assertEqual(
                1.2, intent['unresolved_execution']['expected_position_size'])
            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual(1.2, position['position_size'])
            self.assertEqual(1.2, position['original_position_size'])
            self.assertEqual(1.0, position['planned_position_size'])
            self.assertTrue(position['recovered_open_overfill'])
            self.assertTrue(position['recovered_unresolved_open'])
            self.assertFalse(position['execution_recovery_finalized'])
            self.assertEqual(
                position, TradeState(path).get_open_position('BTCUSDT'))

    def test_missing_rollback_result_does_not_fabricate_financial_ledger(self):
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
                'ma_cross', {'id': 'open-1'}, None, '回滚结果丢失')

            self.assertEqual('rollback_incomplete', outcome['status'])
            self.assertFalse(outcome['residual_ledger_reconciled'])
            self.assertIsNone(system.trade_state.get_open_position('BTCUSDT'))
            self.assertTrue(system.trade_state.is_position_quarantined('BTCUSDT'))
            self.assertTrue(
                TradeState(system.trade_state.state_file)
                .is_position_quarantined('BTCUSDT'))

    def _missing_rollback_provisional_system(
            self, temp_dir, *, stop_confirmed=True):
        system = _System()
        system.trade_state = TradeState(
            str(Path(temp_dir) / 'trade_state.json'))
        system.trade_state.prepare_open_intent(
            'BTCUSDT', 'ma_cross', 'long', 'IMISSINGROLLBACK1',
            {'side': 'long', 'entry_price': 100.0,
             'stop_loss_price': 90.0},
            planned_position_size=1.0)
        system.exchange_api = SimpleNamespace(
            get_position=Mock(return_value={
                'side': 'long', 'contracts': 0.4}),
            _contracts_to_coins=lambda _symbol, contracts: float(contracts),
            compensation_client_order_id=Mock(
                return_value='RMISSINGROLLBACK1'),
            create_stop_loss_order=Mock(return_value=(
                {'id': 'stop-provisional'} if stop_confirmed else None)),
            open_position=Mock(), close_position=Mock(),
            get_last_price=Mock(return_value=100.0),
        )
        system.notifier = SimpleNamespace(notify_error=Mock())
        system._notify_trade_state_persistence_issue = Mock()
        return system

    def test_missing_rollback_with_attributable_fresh_position_builds_provisional(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = self._missing_rollback_provisional_system(temp_dir)

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 90.0,
                'ma_cross', {'id': 'open-1', 'average': 100.0}, None,
                '回滚 ACK 丢失',
                open_intent_client_id='IMISSINGROLLBACK1',
                requested_position_size=1.0)

            self.assertEqual('rollback_incomplete', outcome['status'])
            self.assertTrue(outcome['residual_ledger_reconciled'])
            self.assertTrue(outcome['residual_stop_protected'])
            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual(0.4, position['position_size'])
            self.assertEqual('stop-provisional', position['stop_order_id'])
            self.assertFalse(position['execution_recovery_finalized'])
            self.assertEqual([], position.get('partial_closes', []))
            intent = system.trade_state.get_open_intent('BTCUSDT')
            self.assertIsNotNone(intent)
            self.assertEqual(
                'open_compensation', intent['unresolved_execution']['kind'])
            self.assertEqual(
                'RMISSINGROLLBACK1',
                intent['unresolved_execution']['compensation_client_order_id'])
            system.exchange_api.create_stop_loss_order.assert_called_once()
            system.exchange_api.open_position.assert_not_called()
            system.exchange_api.close_position.assert_not_called()

    def test_missing_rollback_uncertain_stop_keeps_marker_and_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = self._missing_rollback_provisional_system(
                temp_dir, stop_confirmed=False)

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0, 90.0,
                'ma_cross', {'id': 'open-1', 'average': 100.0}, None,
                '回滚 ACK 丢失且止损 ACK 不确定',
                open_intent_client_id='IMISSINGROLLBACK1',
                requested_position_size=1.0)

            self.assertTrue(outcome['residual_ledger_reconciled'])
            self.assertFalse(outcome['residual_stop_protected'])
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))
            self.assertIsNotNone(
                system.trade_state.get_open_intent('BTCUSDT')
                ['unresolved_execution'])
            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertIsNone(position['stop_order_id'])
            self.assertFalse(position['execution_recovery_finalized'])
            system.exchange_api.close_position.assert_not_called()

    def test_missing_rollback_without_safe_stop_still_tracks_provisional(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = self._missing_rollback_provisional_system(temp_dir)

            outcome = system._finalize_open_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 80.0, 1.0, 90.0,
                'ma_cross', {'id': 'open-1', 'average': 80.0}, None,
                '止损价已失效且回滚 ACK 丢失',
                allow_stop_rebuild=False,
                open_intent_client_id='IMISSINGROLLBACK1',
                requested_position_size=1.0)

            self.assertTrue(outcome['residual_ledger_reconciled'])
            self.assertFalse(outcome['residual_stop_protected'])
            self.assertIsNotNone(system.trade_state.get_open_position('BTCUSDT'))
            self.assertIsNotNone(system.trade_state.get_open_intent('BTCUSDT'))
            system.exchange_api.create_stop_loss_order.assert_not_called()
            system.exchange_api.close_position.assert_not_called()

    def test_persistence_failure_partial_rollback_keeps_old_stop_and_runtime_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _System()
            state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            state.replace_stop_loss_dates({'BTCUSDT': '2026-07-10'})
            system.trade_state = state
            system.stop_loss_dates = {'BTCUSDT': '2026-07-10'}
            rollback = {
                'id': 'close-partial', 'average': 99.0,
                'confirmed': True, 'fully_closed': False, 'amount': 0.6,
                'remaining_amount': 0.4,
                'fee': {'cost': 0.02, 'currency': 'USDT'},
            }
            system.exchange_api = SimpleNamespace(
                close_position=Mock(return_value=rollback),
                compensation_client_order_id=lambda value: f'R{value[1:]}',
                create_stop_loss_order=Mock(),
                get_last_price=Mock(return_value=99.0),
            )
            system.exchange_api.get_position = Mock(side_effect=[
                {'side': 'long', 'contracts': 1.0},
                {'side': 'long', 'contracts': 0.4},
            ])
            system.exchange_api._contracts_to_coins = (
                lambda _symbol, contracts: float(contracts))
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._cancel_stop_order_confirmed = Mock()
            system._notify_trade_state_persistence_issue = Mock()
            open_order = {
                'id': 'open-1', 'clientOrderId': 'IopenPersistFail',
                'average': 100.0,
                'fee': {'cost': 0.04, 'currency': 'USDT'}}

            with patch.object(
                    state, 'add_open_position',
                    side_effect=TradeStatePersistenceError('disk full')), \
                 patch.object(
                    state, 'add_open_after_partial_rollback',
                    side_effect=TradeStatePersistenceError('disk still full')):
                saved = system._persist_open_position_or_rollback(
                    'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0,
                    90.0, 'stop-original', strategy='ma_cross',
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
            system.exchange_api = SimpleNamespace()
            self._observe_exact_position(system, 1.0)

            self.assertTrue(system._persist_open_position_or_rollback(
                'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0,
                90.0, 'stop-1', strategy='ma_cross',
                open_order={'id': 'open-1', 'average': 100.0}))

            self.assertNotIn('BTCUSDT', system.stop_loss_dates)
            self.assertNotIn(
                'BTCUSDT', system.trade_state.get_stop_loss_dates())

    def test_committed_open_dir_fsync_failure_never_posts_compensation(self):
        """主账本 rename 已提交时只能继续托管，绝不能误平真实仓位。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            system = _System()
            state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            state.replace_stop_loss_dates({'BTCUSDT': '2026-07-10'})
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'ICOMMITTEDOPEN1',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=1.0)
            system.trade_state = state
            system.stop_loss_dates = {'BTCUSDT': '2026-07-10'}
            system.exchange_api = SimpleNamespace(
                get_position=Mock(return_value={
                    'side': 'long', 'contracts': 1.0}),
                _contracts_to_coins=lambda _symbol, contracts: float(contracts),
                close_position=Mock(),
                compensation_client_order_id=Mock(
                    return_value='RCOMMITTEDOPEN1'),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system._notify_trade_state_persistence_issue = Mock()

            # add_open_position 的备份文件 fsync + 目录 fsync、主文件 fsync
            # 均成功；仅主文件 replace 后的父目录 fsync 失败。
            with patch.object(
                    trade_state_module.os, 'fsync',
                    side_effect=[None, None, None,
                                 OSError('main directory fsync unavailable')]):
                saved = system._persist_open_position_or_rollback(
                    'BTCUSDT', 'BTC/USDT:USDT', 'long', 100.0, 1.0,
                    90.0, 'stop-1', strategy='ma_cross',
                    open_order={'id': 'open-1', 'average': 100.0},
                    open_intent_client_id='ICOMMITTEDOPEN1',
                    requested_position_size=1.0)

            self.assertTrue(saved)
            system.exchange_api.close_position.assert_not_called()
            system.exchange_api.compensation_client_order_id.assert_not_called()
            self.assertEqual(
                'stop-1', state.get_open_position('BTCUSDT')['stop_order_id'])
            self.assertIsNone(state.get_open_intent('BTCUSDT'))
            self.assertNotIn('BTCUSDT', state.get_stop_loss_dates())
            self.assertNotIn('BTCUSDT', system.stop_loss_dates)
            reloaded = TradeState(state.state_file)
            self.assertEqual(
                'stop-1', reloaded.get_open_position('BTCUSDT')[
                    'stop_order_id'])
            self.assertIsNone(reloaded.get_open_intent('BTCUSDT'))
            self.assertTrue(
                state.get_runtime_persistence_status()['degraded'])
            persistence_call = (
                system._notify_trade_state_persistence_issue.call_args)
            self.assertEqual('BTCUSDT', persistence_call.args[0])
            self.assertEqual(
                state.get_open_position('BTCUSDT'),
                persistence_call.args[2].committed_result)


class SingleCompensationOrderFinalizationTest(unittest.TestCase):
    def _provisional_state(self, temp_dir, position_size=1.0):
        state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
        state.prepare_open_intent(
            'BTCUSDT', 'ma_cross', 'long', 'ISINGLECLOSE123',
            {'side': 'long', 'entry_price': 100.0,
             'stop_loss_price': 90.0},
            planned_position_size=position_size)
        state.mark_open_intent_unresolved_execution(
            'BTCUSDT', 'ISINGLECLOSE123', 'open_compensation', position_size,
            compensation_client_order_id='RSINGLECLOSE123')
        state.add_untracked_open_position(
            symbol='BTCUSDT', side='long', entry_price=100.0,
            position_size=position_size, stop_loss_price=90.0,
            stop_order_id='stop-provisional', stop_order_size=position_size,
            strategy='ma_cross',
            open_intent_client_id='ISINGLECLOSE123',
            requested_position_size=position_size,
            preserve_open_intent=True)
        return state

    def test_single_partial_compensation_creates_exactly_one_financial_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._provisional_state(temp_dir)

            result = state.finalize_unresolved_open_execution(
                'BTCUSDT', 'ISINGLECLOSE123', 100.0, 0.4,
                {'id': 'close-partial', 'amount': 0.6,
                 'price': 99.0, 'fee': 0.02},
                entry_fee=0.01, entry_fee_currency='USDT',
                entry_order_id='open-1')

            self.assertEqual('partial', result['action'])
            position = state.get_open_position('BTCUSDT')
            self.assertEqual(0.4, position['position_size'])
            self.assertEqual(1.0, position['original_position_size'])
            self.assertTrue(position['execution_recovery_finalized'])
            self.assertTrue(position['stop_resize_pending'])
            self.assertEqual(1, len(position['partial_closes']))
            self.assertEqual(
                ['close-partial'],
                position['partial_closes'][0]['exit_order_ids'])
            self.assertIsNone(state.get_open_intent('BTCUSDT'))
            TradeState.validate_state(TradeState(state.state_file).state)

    def test_single_full_compensation_closes_without_partial_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._provisional_state(temp_dir)

            result = state.finalize_unresolved_open_execution(
                'BTCUSDT', 'ISINGLECLOSE123', 100.0, 0.0,
                {'id': 'close-full', 'amount': 1.0,
                 'price': 98.0, 'fee': 0.03},
                entry_fee=0.01, entry_fee_currency='USDT',
                entry_order_id='open-1')

            self.assertEqual('closed', result['action'])
            self.assertIsNone(state.get_open_position('BTCUSDT'))
            self.assertIsNone(state.get_open_intent('BTCUSDT'))
            closed = state.get_closed_trades()[-1]
            self.assertEqual(['open-1'], closed['entry_order_ids'])
            self.assertEqual(['close-full'], closed['exit_order_ids'])
            self.assertNotIn('partial_closes', closed)
            TradeState.validate_state(TradeState(state.state_file).state)

    def test_zero_fill_uses_none_and_consumes_lifecycle_without_financial_close(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._provisional_state(temp_dir)

            result = state.finalize_unresolved_open_execution(
                'BTCUSDT', 'ISINGLECLOSE123', 100.0, 1.0, None,
                entry_order_id='open-1')

            self.assertEqual('unchanged', result['action'])
            position = state.get_open_position('BTCUSDT')
            self.assertEqual(1.0, position['position_size'])
            self.assertTrue(position['execution_recovery_finalized'])
            self.assertEqual([], position.get('partial_closes', []))
            self.assertIsNone(state.get_open_intent('BTCUSDT'))

    def test_legacy_compensation_list_is_rejected_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._provisional_state(temp_dir)

            with self.assertRaisesRegex(ValueError, '单笔补偿订单'):
                state.finalize_unresolved_open_execution(
                    'BTCUSDT', 'ISINGLECLOSE123', 100.0, 0.4,
                    [{'id': 'close-old-list', 'amount': 0.6,
                      'price': 99.0}], entry_order_id='open-1')

            position = state.get_open_position('BTCUSDT')
            self.assertFalse(position['execution_recovery_finalized'])
            self.assertIsNotNone(state.get_open_intent('BTCUSDT'))
            self.assertEqual([], state.get_closed_trades())

    def test_invalid_entry_order_id_is_rejected_atomically(self):
        for invalid in (None, [], '', '   ', True):
            with self.subTest(invalid=invalid), \
                    tempfile.TemporaryDirectory() as temp_dir:
                state = self._provisional_state(temp_dir)

                with self.assertRaisesRegex(ValueError, '原开仓订单 ID'):
                    state.finalize_unresolved_open_execution(
                        'BTCUSDT', 'ISINGLECLOSE123', 100.0, 1.0,
                        None, entry_order_id=invalid)

                self.assertFalse(
                    state.get_open_position(
                        'BTCUSDT')['execution_recovery_finalized'])
                self.assertIsNotNone(state.get_open_intent('BTCUSDT'))

    def test_noncanonical_compensation_object_is_rejected_atomically(self):
        invalid_orders = (
            {'id': 'close-1', 'ids': ['close-1'],
             'amount': 0.6, 'price': 99.0},
            {'id': 'close-1', 'amount': 0.6, 'price': 99.0,
             'execution_ambiguous': True},
            {'id': 'close-1', 'amount': 0.6, 'price': 99.0,
             'financial_evidence_incomplete': True},
        )
        for order in invalid_orders:
            with self.subTest(order=order), \
                    tempfile.TemporaryDirectory() as temp_dir:
                state = self._provisional_state(temp_dir)

                with self.assertRaisesRegex(ValueError, '非法字段'):
                    state.finalize_unresolved_open_execution(
                        'BTCUSDT', 'ISINGLECLOSE123', 100.0, 0.4,
                        order, entry_order_id='open-1')

                self.assertFalse(
                    state.get_open_position(
                        'BTCUSDT')['execution_recovery_finalized'])
                self.assertIsNotNone(state.get_open_intent('BTCUSDT'))

    def test_large_position_conservation_uses_ulp_not_relative_slack(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._provisional_state(
                temp_dir, position_size=1_000_000_000.0)

            with self.assertRaisesRegex(
                    TradeStatePersistenceError, '不守恒'):
                state.finalize_unresolved_open_execution(
                    'BTCUSDT', 'ISINGLECLOSE123', 100.0,
                    400_000_000.0,
                    {'id': 'close-large',
                     'amount': 599_999_999.95,
                     'price': 99.0},
                    entry_order_id='open-large')

            position = state.get_open_position('BTCUSDT')
            self.assertEqual(1_000_000_000.0, position['position_size'])
            self.assertFalse(position['execution_recovery_finalized'])
            self.assertIsNotNone(state.get_open_intent('BTCUSDT'))


if __name__ == '__main__':
    unittest.main()
