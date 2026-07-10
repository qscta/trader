"""config_validation 共享校验原语单测（纯标准库）。

三入口（前端/API/手写 config）的一致性由这一层保证，故直接锁定其边界行为。
"""
import unittest

import config_validation as cv


class StrictIntTest(unittest.TestCase):
    def test_accepts_integral(self):
        for v in (28, 28.0, "28", " 28 "):
            self.assertEqual(cv.strict_int(v, 'x'), 28)

    def test_rejects_fractional_and_nonfinite(self):
        for v in (28.9, "28.9", "inf", "-inf", "nan", "abc", None):
            with self.assertRaises(ValueError, msg=repr(v)):
                cv.strict_int(v, 'x')


class StrictFloatFiniteTest(unittest.TestCase):
    def test_accepts_finite(self):
        self.assertEqual(cv.strict_float_finite("0.01", 'x'), 0.01)
        self.assertEqual(cv.strict_float_finite(-5, 'x'), -5.0)

    def test_rejects_nonfinite(self):
        for v in ("inf", "-inf", "nan", float('inf'), float('nan'), "abc", None):
            with self.assertRaises(ValueError, msg=repr(v)):
                cv.strict_float_finite(v, 'x')


class StrictRiskTest(unittest.TestCase):
    def test_in_range(self):
        self.assertEqual(cv.strict_risk_per_trade("0.01"), 0.01)
        self.assertEqual(cv.strict_risk_per_trade(0.5), 0.5)  # 上限含端点

    def test_out_of_range(self):
        for v in (0, -0.1, 0.51, 1.0, "inf", "nan"):
            with self.assertRaises(ValueError, msg=repr(v)):
                cv.strict_risk_per_trade(v)


class StrictBoolTest(unittest.TestCase):
    def test_real_bool_passthrough(self):
        self.assertIs(cv.strict_bool(True), True)
        self.assertIs(cv.strict_bool(False), False)

    def test_string_parsing(self):
        self.assertIs(cv.strict_bool("true"), True)
        self.assertIs(cv.strict_bool("false"), False)
        self.assertIs(cv.strict_bool(" TRUE "), True)

    def test_rejects_ambiguous(self):
        # 关键回归：Python bool("false")==True，必须显式拒绝而非当真
        for v in ("maybe", "1", "0", 1, 0, None, "yes"):
            with self.assertRaises(ValueError, msg=repr(v)):
                cv.strict_bool(v)


class NormalizeSymbolTest(unittest.TestCase):
    def test_normalizes(self):
        self.assertEqual(cv.normalize_symbol_name("btcusdt"), "BTCUSDT")
        self.assertEqual(cv.normalize_symbol_name(" ethusdt "), "ETHUSDT")

    def test_rejects_bad(self):
        for v in ("BTC-USDT", "BTCUSD", "", 123, None, "USDT"):
            with self.assertRaises(ValueError, msg=repr(v)):
                cv.normalize_symbol_name(v)


class StrategyOhlcvLimitTest(unittest.TestCase):
    def test_turtle_limit_tracks_channel_period_and_open_candle_buffer(self):
        config = {'channel_period': 500}

        self.assertEqual(cv.required_closed_candles_for_strategy('turtle', config), 502)
        self.assertEqual(cv.ohlcv_fetch_limit_for_strategy('turtle', config), 503)

    def test_ma_cross_limit_tracks_longest_required_window(self):
        config = {'ma_long_period': 250, 'ma_stop_period': 400}

        self.assertEqual(cv.required_closed_candles_for_strategy('ma_cross', config), 500)
        self.assertEqual(cv.ohlcv_fetch_limit_for_strategy('ma_cross', config), 501)

    def test_default_periods_keep_existing_fetch_floor(self):
        config = {'channel_period': 28, 'ma_long_period': 28, 'ma_stop_period': 28}

        self.assertEqual(cv.ohlcv_fetch_limit_for_strategy('turtle', config), 365)
        self.assertEqual(cv.ohlcv_fetch_limit_for_strategy('ma_cross', config), 365)


class CandleSupplyTest(unittest.TestCase):
    """K 线供应能力校验：所需已收盘根数超过交易所单次上限（300−1 缓冲=299）的
    周期组合必须在配置入口拒绝，否则品种「每日警告、永不交易」。"""

    def test_defaults_pass(self):
        cv.ensure_candle_supply({'channel_period': 28, 'ma_long_period': 28, 'ma_stop_period': 28})

    def test_exact_boundaries_pass(self):
        cv.ensure_candle_supply({'channel_period': 297})   # 297+2 = 299 恰好可供
        cv.ensure_candle_supply({'ma_long_period': 149})   # 149×2 = 298
        cv.ensure_candle_supply({'ma_stop_period': 298})   # 298+1 = 299

    def test_over_capacity_rejected(self):
        for bad in ({'channel_period': 298},               # 298+2 = 300 > 299
                    {'ma_long_period': 150},               # 150×2 = 300 > 299
                    {'ma_stop_period': 299}):              # 299+1 = 300 > 299
            with self.assertRaises(ValueError, msg=repr(bad)):
                cv.ensure_candle_supply(bad)


if __name__ == '__main__':
    unittest.main()
