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

    def test_default_periods_use_exchange_supply_cap_as_floor(self):
        """默认请求根数 = 交易所单次供应上限 300（此前 365 的“约一年”从未真正拿到过）。"""
        config = {'channel_period': 28, 'ma_long_period': 28, 'ma_stop_period': 28}

        self.assertEqual(cv.ohlcv_fetch_limit_for_strategy('turtle', config), 300)
        self.assertEqual(cv.ohlcv_fetch_limit_for_strategy('ma_cross', config), 300)


class StrategySupplyValidationTest(unittest.TestCase):
    """周期需求 vs 交易所单次 K 线供应（OKX 硬上限 300，不分页）——超出必须 fail-loud，
    否则品种通过全部校验入池后会每天“K线不足”静默跳过、永不交易。"""

    def test_max_servable_periods_pass(self):
        # 海龟需求 period+2 ≤ 299 → period 上限 297；双均线 long*2 ≤ 299 → long 上限 149
        cv.validate_strategy_supply({'channel_period': 297,
                                     'ma_short_period': 7, 'ma_long_period': 149,
                                     'ma_stop_period': 298})  # stop+1 = 299 恰好可供

    def test_turtle_period_beyond_supply_rejected(self):
        with self.assertRaises(ValueError):
            cv.validate_strategy_supply({'channel_period': 298})

    def test_ma_long_period_beyond_supply_rejected(self):
        with self.assertRaises(ValueError):
            cv.validate_strategy_supply({'ma_long_period': 150})

    def test_ma_stop_period_beyond_supply_rejected(self):
        with self.assertRaises(ValueError):
            cv.validate_strategy_supply({'ma_stop_period': 299})

    def test_defaults_pass(self):
        cv.validate_strategy_supply({})   # 全默认周期必然可供


if __name__ == '__main__':
    unittest.main()
