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
    def test_default_periods_always_request_one_full_okx_page(self):
        config = {'ma_long_period': 28, 'ma_stop_period': 28}

        self.assertEqual(cv.ohlcv_fetch_limit_for_strategy('ma_cross', config), 300)

    def test_capacity_boundary_leaves_one_open_candle_buffer(self):
        config = {'ma_long_period': 149, 'ma_stop_period': 298}

        self.assertTrue(cv.validate_strategy_ohlcv_capacity(config))
        self.assertEqual(cv.required_closed_candles_for_strategy('ma_cross', config), 299)

    def test_retired_turtle_is_rejected_as_unknown_strategy(self):
        # 海龟已彻底下线：既不在白名单，也不再有任何周期/容量计算分支。
        self.assertNotIn('turtle', cv.STRATEGY_WHITELIST)
        self.assertIn('turtle', cv.RETIRED_STRATEGIES)
        with self.assertRaisesRegex(ValueError, '未知策略'):
            cv.required_closed_candles_for_strategy('turtle', {})

    def test_ma_period_over_single_page_capacity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '超过单次 300 根上限'):
            cv.validate_strategy_ohlcv_capacity({
                'ma_long_period': 150,
                'ma_stop_period': 28})


if __name__ == '__main__':
    unittest.main()
