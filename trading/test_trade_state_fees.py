import tempfile
import unittest
from pathlib import Path

from trade_state import (
    TRADING_FEE_RATE,
    calculate_closed_trade_metrics,
    enrich_closed_trade_with_fees,
    TradeState,
)


class TradeStateFeeTest(unittest.TestCase):
    def test_long_trade_pnl_includes_entry_and_exit_fees(self):
        metrics = calculate_closed_trade_metrics(
            side='long',
            entry_price=100,
            exit_price=110,
            position_size=2,
        )

        expected_entry_fee = 100 * 2 * TRADING_FEE_RATE
        expected_exit_fee = 110 * 2 * TRADING_FEE_RATE
        expected_gross = (110 - 100) * 2
        expected_net = expected_gross - expected_entry_fee - expected_exit_fee

        self.assertAlmostEqual(expected_entry_fee, metrics['entry_fee'])
        self.assertAlmostEqual(expected_exit_fee, metrics['exit_fee'])
        self.assertAlmostEqual(expected_gross, metrics['gross_pnl'])
        self.assertAlmostEqual(expected_net, metrics['pnl'])
        self.assertAlmostEqual(expected_net / 200 * 100, metrics['pnl_percent'])

    def test_short_trade_pnl_includes_entry_and_exit_fees(self):
        metrics = calculate_closed_trade_metrics(
            side='short',
            entry_price=100,
            exit_price=90,
            position_size=2,
        )

        expected_entry_fee = 100 * 2 * TRADING_FEE_RATE
        expected_exit_fee = 90 * 2 * TRADING_FEE_RATE
        expected_gross = (100 - 90) * 2
        expected_net = expected_gross - expected_entry_fee - expected_exit_fee

        self.assertAlmostEqual(expected_entry_fee, metrics['entry_fee'])
        self.assertAlmostEqual(expected_exit_fee, metrics['exit_fee'])
        self.assertAlmostEqual(expected_gross, metrics['gross_pnl'])
        self.assertAlmostEqual(expected_net, metrics['pnl'])
        self.assertAlmostEqual(expected_net / 200 * 100, metrics['pnl_percent'])

    def test_historical_trade_is_normalized_to_fee_adjusted_pnl(self):
        historical = {
            'symbol': 'TESTUSDT',
            'side': 'long',
            'entry_price': 100,
            'exit_price': 110,
            'position_size': 2,
            'pnl': 20,
            'pnl_percent': 10,
        }

        enriched = enrich_closed_trade_with_fees(historical)

        self.assertLess(enriched['pnl'], historical['pnl'])
        self.assertAlmostEqual(0.09 + 0.099, enriched['total_fee'])
        self.assertAlmostEqual(20 - 0.189, enriched['pnl'])

    def test_exchange_fees_override_estimate_and_survive_enrichment(self):
        metrics = calculate_closed_trade_metrics(
            'long', 100, 110, 2, entry_fee=0.03, exit_fee=0.04)
        self.assertEqual(metrics['fee_source'], 'actual')
        self.assertAlmostEqual(metrics['total_fee'], 0.07)
        self.assertEqual(enrich_closed_trade_with_fees(metrics), metrics)

    def test_partial_close_shrinks_ledger_and_final_close_aggregates_vwap_and_fees(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            state.add_open_position(
                'BTCUSDT', 'long', 100, 1.0, 90, 'stop-old', strategy='ma_cross',
                entry_fee=0.02, entry_fee_currency='USDT', entry_order_ids=['open-1'])

            remaining = state.apply_partial_close(
                'BTCUSDT', 0.4, 110, exit_fee=0.01,
                exit_fee_currency='USDT', exit_order_ids=['close-1'],
                new_stop_order_id='stop-new', stop_order_size=0.6)
            self.assertAlmostEqual(remaining['position_size'], 0.6)
            self.assertAlmostEqual(remaining['stop_order_size'], 0.6)
            self.assertEqual(remaining['stop_order_id'], 'stop-new')

            closed = state.close_position(
                'BTCUSDT', 120, exit_fee=0.015,
                exit_fee_currency='USDT', exit_order_ids=['close-2'])
            self.assertAlmostEqual(closed['position_size'], 1.0)
            self.assertAlmostEqual(closed['exit_price'], 116.0)
            self.assertAlmostEqual(closed['gross_pnl'], 16.0)
            self.assertAlmostEqual(closed['total_fee'], 0.045)
            self.assertAlmostEqual(closed['pnl'], 15.955)
            self.assertEqual(closed['fee_source'], 'actual')
            self.assertEqual(closed['exit_order_ids'], ['close-1', 'close-2'])

            reloaded = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            persisted = reloaded.get_closed_trades()[-1]
            self.assertAlmostEqual(persisted['pnl'], 15.955)

            corrupted = dict(persisted)
            corrupted['final_exit_price'] = 121.0
            with self.assertRaises(ValueError):
                TradeState.validate_state({
                    'open_positions': {}, 'closed_trades': [corrupted],
                })

    def test_partial_close_rejects_full_quantity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = TradeState(str(Path(temp_dir) / 'trade_state.json'))
            state.add_open_position(
                'ETHUSDT', 'short', 100, 2, 110, 'stop',
                strategy='ma_cross')
            with self.assertRaises(ValueError):
                state.apply_partial_close('ETHUSDT', 2, 90)


if __name__ == '__main__':
    unittest.main()
