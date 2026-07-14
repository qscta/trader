"""交易所已空仓收尾的真实止损成交回查与估算降级回归。"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import _test_stubs
from trade_state import TradeState, TradeStatePersistenceError

TradingSystem = _test_stubs.import_main().TradingSystem


class StopFillAccountingIntegrationTest(unittest.TestCase):
    EXCHANGE_TIME = '2026-07-09T13:31:18.720000+00:00'

    @staticmethod
    def _evidence():
        return {
            'source': 'okx_stop_fill',
            'average': 89.5,
            'fee': 0.2,
            'fee_currency': 'USDT',
            'order_ids': ['child-1'],
            'algo_order_ids': ['algo-1'],
            'fill_time': StopFillAccountingIntegrationTest.EXCHANGE_TIME,
        }

    def _system(self, tmp, recovery_result=None, recovery_error=None):
        system = TradingSystem.__new__(TradingSystem)
        system.config = {
            'strategy': {'default_risk_per_trade': 0.01},
            'trading': {'symbols': [
                {'name': 'BTCUSDT', 'enabled': True,
                 'strategy': 'turtle'}]},
        }
        system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
        system.trade_state.add_open_position(
            'BTCUSDT', 'long', 100.0, 1.0, 90.0,
            'algo-1', strategy='turtle')
        system.stop_loss_dates = {}
        system._stop_anomalies = {}
        recovery = Mock(return_value=recovery_result)
        if recovery_error is not None:
            recovery.side_effect = recovery_error
        system.exchange_api = SimpleNamespace(
            recover_stop_fill_evidence=recovery,
            to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT',
            get_position=Mock(return_value=None),
        )
        system._cancel_stop_order_confirmed = Mock(return_value=True)
        return system, recovery

    def test_proven_stop_fill_overrides_estimate_and_persists_audit_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, recovery = self._system(
                tmp, recovery_result=self._evidence())
            position = system.trade_state.get_open_position('BTCUSDT')

            closed, saved, cleared = system._handle_exchange_flat_close(
                'BTCUSDT', 'BTC/USDT:USDT', position, 90.0,
                '启动同步平仓', strategy_type='turtle')

            self.assertTrue(saved)
            self.assertTrue(cleared)
            self.assertEqual(closed['final_exit_price'], 89.5)
            self.assertEqual(closed['exit_price_source'], 'okx_stop_fill')
            self.assertFalse(closed['exit_price_estimated'])
            self.assertEqual(closed['exit_fee'], 0.2)
            self.assertEqual(closed['exit_fee_currency'], 'USDT')
            self.assertEqual(closed['exit_order_ids'], ['child-1'])
            self.assertEqual(closed['exit_algo_order_ids'], ['algo-1'])
            self.assertEqual(closed['exchange_exit_time'], self.EXCHANGE_TIME)
            recovery.assert_called_once_with(
                'BTC/USDT:USDT', 'long', 1.0, ['algo-1'])

            reloaded = TradeState(os.path.join(tmp, 'trade_state.json'))
            persisted = reloaded.get_closed_trades()[-1]
            self.assertEqual(persisted['final_exit_price'], 89.5)
            self.assertEqual(persisted['exit_price_source'], 'okx_stop_fill')
            self.assertFalse(persisted['exit_price_estimated'])
            self.assertEqual(persisted['exit_order_ids'], ['child-1'])

    def test_no_effective_stop_uses_explicit_estimated_stop_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _recovery = self._system(tmp, recovery_result=None)
            position = system.trade_state.get_open_position('BTCUSDT')

            closed, saved, cleared = system._handle_exchange_flat_close(
                'BTCUSDT', 'BTC/USDT:USDT', position, 90.0,
                '启动同步平仓', strategy_type='turtle')

            self.assertTrue(saved)
            self.assertTrue(cleared)
            self.assertEqual(closed['final_exit_price'], 90.0)
            self.assertEqual(closed['exit_price_source'], 'estimated_stop')
            self.assertTrue(closed['exit_price_estimated'])
            self.assertNotIn('exchange_exit_time', closed)
            self.assertNotIn('exit_algo_order_ids', closed)

    def test_recovery_exception_or_malformed_evidence_never_blocks_flat_close(self):
        cases = (
            RuntimeError('OKX query unavailable'),
            None,
        )
        for recovery_error in cases:
            with self.subTest(recovery_error=recovery_error), \
                    tempfile.TemporaryDirectory() as tmp:
                malformed = None if recovery_error else {
                    **self._evidence(), 'fee': -1.0}
                system, _recovery = self._system(
                    tmp, recovery_result=malformed,
                    recovery_error=recovery_error)
                position = system.trade_state.get_open_position('BTCUSDT')

                closed, saved, cleared = system._handle_exchange_flat_close(
                    'BTCUSDT', 'BTC/USDT:USDT', position, 90.0,
                    '盘中巡检', strategy_type='turtle')

                self.assertTrue(saved)
                self.assertTrue(cleared)
                self.assertEqual(closed['final_exit_price'], 90.0)
                self.assertEqual(
                    closed['exit_price_source'], 'estimated_stop')
                self.assertTrue(closed['exit_price_estimated'])

    def test_intraday_notification_uses_recovered_fill_not_old_stop_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _recovery = self._system(
                tmp, recovery_result=self._evidence())
            system._resume_persisted_close_intent = Mock(return_value=None)
            system.get_strategy_for_symbol = Mock(
                return_value=(None, 'turtle'))
            system._get_strategy_display_name = Mock(return_value='海龟策略')
            system.notifier = SimpleNamespace(
                notify_stop_loss_triggered=Mock())
            position = system.trade_state.get_open_position('BTCUSDT')

            system._reconcile_symbol_intraday(
                'BTCUSDT', position,
                {'BTCUSDT': system.config['trading']['symbols'][0]})

            system.notifier.notify_stop_loss_triggered.assert_called_once_with(
                'BTCUSDT', '海龟策略', 'long', 89.5,
                source='盘中5分钟巡检确认')

    def test_persistence_failure_runtime_fallback_keeps_same_fill_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _recovery = self._system(
                tmp, recovery_result=self._evidence())
            original_force = system.trade_state.force_runtime_close_position
            system.trade_state.close_position = Mock(side_effect=
                TradeStatePersistenceError('disk full'))
            system.trade_state.force_runtime_close_position = Mock(
                wraps=original_force)
            system._notify_trade_state_persistence_issue = Mock()
            position = system.trade_state.get_open_position('BTCUSDT')

            closed, saved, cleared = system._handle_exchange_flat_close(
                'BTCUSDT', 'BTC/USDT:USDT', position, 90.0,
                '启动同步平仓', strategy_type='turtle')

            self.assertFalse(saved)
            self.assertTrue(cleared)
            self.assertEqual(closed['final_exit_price'], 89.5)
            self.assertEqual(closed['exit_price_source'], 'okx_stop_fill')
            kwargs = system.trade_state.force_runtime_close_position.call_args.kwargs
            self.assertEqual(kwargs['exit_fee'], 0.2)
            self.assertEqual(kwargs['exit_price_source'], 'okx_stop_fill')
            self.assertFalse(kwargs['exit_price_estimated'])
            self.assertEqual(kwargs['exit_order_ids'], ['child-1'])
            self.assertEqual(kwargs['exit_algo_order_ids'], ['algo-1'])
            self.assertEqual(kwargs['exchange_exit_time'], self.EXCHANGE_TIME)

    def test_trade_state_rejects_provenance_contradiction_without_losing_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'algo-1')

            with self.assertRaisesRegex(ValueError, '来源与估算标记'):
                state.close_position(
                    'BTCUSDT', 89.5,
                    exit_price_source='okx_stop_fill',
                    exit_price_estimated=True)

            self.assertIsNotNone(state.get_open_position('BTCUSDT'))
            self.assertEqual(state.get_closed_trades(), [])


if __name__ == '__main__':
    unittest.main()
