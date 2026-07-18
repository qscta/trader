"""盘中反向仓位核对：本地零仓时也必须发现交易所迟到/人工孤儿仓。"""

import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import _test_stubs

TradingSystem = _test_stubs.import_main().TradingSystem
from trade_state import TradeState  # noqa: E402


class IntradayOrphanReconciliationTest(unittest.TestCase):
    def _system(self, temp_dir, listing):
        system = TradingSystem.__new__(TradingSystem)
        system.trade_state = TradeState(os.path.join(temp_dir, 'trade_state.json'))
        system._trade_lock = threading.Lock()
        system.config = {
            'strategy': {'default_risk_per_trade': 0.01},
            'trading': {'symbols': [
                {'name': 'BTCUSDT', 'enabled': True},
            ]},
        }
        system.exchange_api = SimpleNamespace(
            list_position_symbols=listing,
            to_ccxt_symbol=lambda symbol: symbol,
        )
        system.notifier = SimpleNamespace(notify_error=Mock())
        return system

    def test_zero_local_positions_still_quarantines_exchange_orphan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            system = self._system(temp_dir, lambda: ['GHOSTUSDT'])

            system.reconcile_intraday_stop_losses()

            self.assertTrue(system.trade_state.is_position_quarantined('GHOSTUSDT'))
            system.notifier.notify_error.assert_called_once()

    def test_failed_reverse_query_blocks_configured_empty_symbol(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def fail():
                raise RuntimeError('position list unavailable')

            system = self._system(temp_dir, fail)
            system.reconcile_intraday_stop_losses()

            self.assertTrue(system.trade_state.is_position_quarantined('BTCUSDT'))

    def test_flat_recheck_clears_old_orphan_quarantine(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            exchange_symbols = ['GHOSTUSDT']
            system = self._system(temp_dir, lambda: list(exchange_symbols))
            system.reconcile_intraday_stop_losses()
            self.assertTrue(system.trade_state.is_position_quarantined('GHOSTUSDT'))

            exchange_symbols.clear()
            system.reconcile_intraday_stop_losses()

            self.assertFalse(system.trade_state.is_position_quarantined('GHOSTUSDT'))


if __name__ == '__main__':
    unittest.main()
