"""RiskManager 自身输入守卫回归（防御纵深）。

calculate_position_size 曾完全信任调用方：喂负权益/负风险度会算出负仓位、
NaN 会算出 NaN、极小止损距会算出数量级失控的仓位。生产链路靠上游三层兜底
（权益>0、价格有限>0、风险度∈(0,50%] + 成交后 risk_ratio 回滚 + 交易所保证金）
挡着，但函数本身必须 fail-closed，绝不把危险数量抛给下游。
"""
import math
import unittest

from risk_manager import RiskManager


class PositionSizeGuardTest(unittest.TestCase):
    def test_risk_is_required_at_each_calculation(self):
        with self.assertRaises(TypeError):
            RiskManager(10000).calculate_position_size(100.0, 95.0)

    def test_valid_input_unchanged(self):
        # 权益 1 万、1% 风险、5% 止损距 → 仓位价值 = 100/0.05 = 2000，数量 = 20
        size = RiskManager(10000).calculate_position_size(100.0, 95.0, 0.01)
        self.assertAlmostEqual(size, 20.0)

    def test_negative_equity_returns_zero(self):
        self.assertEqual(RiskManager(-5000).calculate_position_size(100.0, 95.0, 0.01), 0)

    def test_nonfinite_equity_returns_zero(self):
        self.assertEqual(RiskManager(float('nan')).calculate_position_size(100.0, 95.0, 0.01), 0)
        self.assertEqual(RiskManager(float('inf')).calculate_position_size(100.0, 95.0, 0.01), 0)

    def test_negative_risk_returns_zero(self):
        self.assertEqual(RiskManager(10000).calculate_position_size(100.0, 95.0, -0.5), 0)

    def test_risk_over_one_returns_zero(self):
        self.assertEqual(RiskManager(10000).calculate_position_size(100.0, 95.0, 5.0), 0)

    def test_guard_uses_same_fifty_percent_limit_as_config(self):
        manager = RiskManager(10000)
        self.assertGreater(manager.calculate_position_size(100.0, 95.0, 0.5), 0)
        self.assertEqual(manager.calculate_position_size(100.0, 95.0, 0.500001), 0)

    def test_nonfinite_price_returns_zero(self):
        self.assertEqual(RiskManager(10000).calculate_position_size(float('nan'), 95.0, 0.01), 0)
        self.assertEqual(RiskManager(10000).calculate_position_size(100.0, float('inf'), 0.01), 0)

    def test_nonpositive_price_returns_zero(self):
        self.assertEqual(RiskManager(10000).calculate_position_size(0.0, -1.0, 0.01), 0)
        self.assertEqual(RiskManager(10000).calculate_position_size(-100.0, -95.0, 0.01), 0)

    def test_stop_equals_entry_returns_zero(self):
        self.assertEqual(RiskManager(10000).calculate_position_size(100.0, 100.0, 0.01), 0)

    def test_bool_is_never_treated_as_one(self):
        cases = (
            (True, 100.0, 95.0, 0.01),
            (10000, True, 95.0, 0.01),
            (10000, 100.0, True, 0.01),
            (10000, 100.0, 95.0, True),
        )
        for equity, entry, stop, risk in cases:
            with self.subTest(
                    equity=equity, entry=entry, stop=stop, risk=risk):
                self.assertEqual(
                    0,
                    RiskManager(equity).calculate_position_size(
                        entry, stop, risk))

    def test_huge_integer_in_any_numeric_slot_returns_zero(self):
        huge = 10 ** 10000
        cases = (
            (huge, 100.0, 95.0, 0.01),
            (10000, huge, 95.0, 0.01),
            (10000, 100.0, huge, 0.01),
            (10000, 100.0, 95.0, huge),
        )
        for equity, entry, stop, risk in cases:
            with self.subTest(slot=(equity, entry, stop, risk)):
                self.assertEqual(
                    0,
                    RiskManager(equity).calculate_position_size(
                        entry, stop, risk))

    def test_result_is_always_finite_nonnegative(self):
        # 无论输入怎么畸形，输出要么是有限正数，要么是 0——绝不 NaN/负。
        import itertools
        vals = [float('nan'), float('inf'), -1.0, 0.0, 1e-30, 1e30, 100.0]
        for eq, entry, stop, risk in itertools.product(vals, repeat=4):
            size = RiskManager(eq).calculate_position_size(entry, stop, risk)
            self.assertTrue(
                size == 0 or (isinstance(size, float) and math.isfinite(size) and size > 0),
                f'危险输出 size={size!r} for eq={eq} entry={entry} stop={stop} risk={risk}')


if __name__ == '__main__':
    unittest.main()
