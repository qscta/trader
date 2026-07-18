"""双均线策略的真实 pandas 行为测试。"""

import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import pandas as pd  # noqa: E402

from ma_cross_strategy import MaCrossStrategy  # noqa: E402


class MaCrossStrategyTest(unittest.TestCase):
    def setUp(self):
        self.strategy = MaCrossStrategy(
            short_period=2, long_period=4, stop_loss_period=3)

    @staticmethod
    def frame(closes):
        return pd.DataFrame({'close': [float(value) for value in closes]})

    def test_golden_cross_from_equal_boundary_opens_long(self):
        signal = self.strategy.check_signal(
            self.frame([10, 10, 10, 10, 10, 10, 10, 11]))

        self.assertEqual('long', signal['action'])
        self.assertGreater(signal['ema_short'], signal['ema_long'])

    def test_death_cross_from_equal_boundary_opens_short(self):
        signal = self.strategy.check_signal(
            self.frame([10, 10, 10, 10, 10, 10, 10, 9]))

        self.assertEqual('short', signal['action'])
        self.assertLess(signal['ema_short'], signal['ema_long'])

    def test_persistent_trend_does_not_reemit_cross(self):
        signal = self.strategy.check_signal(
            self.frame([1, 2, 3, 4, 5, 6, 7, 8]))

        self.assertIsNone(signal['action'])
        self.assertTrue(signal['ema_bullish'])

    def test_stop_window_excludes_current_candle(self):
        upper, lower = self.strategy.calculate_stop_levels(
            self.frame([1, 2, 3, 4, 5, 6, 7, 100]))

        self.assertEqual(7.0, upper)
        self.assertEqual(5.0, lower)

    def test_minimum_history_is_enforced_consistently(self):
        short = self.frame([1, 2, 3, 4, 5, 6, 7])

        self.assertIsNone(self.strategy.check_signal(short))
        self.assertIsNone(self.strategy.check_current_state(short))
        self.assertEqual(
            (False, None, None),
            self.strategy.check_reentry_condition(short))

    def test_nonfinite_or_nonpositive_closes_fail_closed(self):
        for bad in (float('nan'), float('inf'), 0, -1, True, 'bad'):
            with self.subTest(bad=bad):
                frame = pd.DataFrame(
                    {'close': [1, 2, 3, 4, 5, 6, 7, bad]},
                    dtype=object)

                self.assertIsNone(self.strategy.check_signal(frame))
                self.assertIsNone(self.strategy.check_current_state(frame))
                self.assertEqual(
                    (False, None, None),
                    self.strategy.check_reentry_condition(frame))

    def test_invalid_close_outside_minimum_window_also_fails_closed(self):
        frame = pd.DataFrame({
            'close': ['bad', 1, 2, 3, 4, 5, 6, 7, 8]}, dtype=object)

        self.assertIsNone(self.strategy.check_signal(frame))
        self.assertIsNone(self.strategy.check_current_state(frame))
        self.assertEqual(
            (False, None, None),
            self.strategy.check_reentry_condition(frame))

    def test_numeric_strings_are_normalized_before_indicators(self):
        frame = pd.DataFrame({
            'close': ['1', '2', '3', '4', '5', '6', '7', '8']})

        signal = self.strategy.check_current_state(frame)

        self.assertEqual('long', signal['action'])
        self.assertEqual(7.0, signal['upper_stop'])

    def test_current_state_maps_trend_to_side(self):
        bullish = self.strategy.check_current_state(
            self.frame([1, 2, 3, 4, 5, 6, 7, 8]))
        bearish = self.strategy.check_current_state(
            self.frame([8, 7, 6, 5, 4, 3, 2, 1]))

        self.assertEqual('long', bullish['action'])
        self.assertEqual('short', bearish['action'])

    def test_equal_emas_have_no_current_direction_or_reentry(self):
        frame = self.frame([10] * 8)

        self.assertIsNone(self.strategy.check_current_state(frame)['action'])
        should_reenter, side, signal = (
            self.strategy.check_reentry_condition(frame))
        self.assertFalse(should_reenter)
        self.assertIsNone(side)
        self.assertIsNotNone(signal)

    def test_reentry_follows_current_ema_direction(self):
        long_result = self.strategy.check_reentry_condition(
            self.frame([1, 2, 3, 4, 5, 6, 7, 8]))
        short_result = self.strategy.check_reentry_condition(
            self.frame([8, 7, 6, 5, 4, 3, 2, 1]))

        self.assertEqual((True, 'long'), long_result[:2])
        self.assertEqual((True, 'short'), short_result[:2])


if __name__ == '__main__':
    unittest.main()
