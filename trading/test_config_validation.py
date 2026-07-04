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


if __name__ == '__main__':
    unittest.main()
