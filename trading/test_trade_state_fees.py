import unittest

from trade_state import (
    TRADING_FEE_RATE,
    calculate_closed_trade_metrics,
    enrich_closed_trade_with_fees,
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


if __name__ == '__main__':
    unittest.main()
