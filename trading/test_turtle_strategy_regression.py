import unittest

from turtle_strategy import TurtleStrategy


class _FakeCloseSeries:
    def __init__(self, values):
        self.values = values


class _FakeDataFrame:
    def __init__(self, closes):
        self._closes = closes

    def __len__(self):
        return len(self._closes)

    def __getitem__(self, key):
        if key != "close":
            raise KeyError(key)
        return _FakeCloseSeries(self._closes)


class TurtleStrategyRegressionTest(unittest.TestCase):
    def _assert_next_day_first_breakout_short(self, closes, label):
        strategy = TurtleStrategy(channel_period=2)
        df = _FakeDataFrame(closes)

        self.assertTrue(
            strategy.is_first_breakout_armed(df, include_latest_bar=False),
            f"{label}: 生成当日信号前，应仍保留中轨穿越后的首次突破资格",
        )
        self.assertFalse(
            strategy.is_first_breakout_armed(df, include_latest_bar=True),
            f"{label}: 若把最新K线也计入历史回溯，这根首次下破会被错误视为已消耗资格",
        )

        signal = strategy.check_signal(df, mid_line_crossed=True)
        self.assertIsNotNone(signal, f"{label}: 应当生成有效信号")
        self.assertEqual("short", signal["action"], f"{label}: 应当生成 short")

    def test_first_breakout_next_day_remains_armed_before_signal_generation(self):
        self._assert_next_day_first_breakout_short(
            [10.0, 14.0, 13.0, 13.2, 12.5],
            "base-case",
        )

    def test_new_listing_can_open_short_directly_on_first_computable_breakdown(self):
        strategy = TurtleStrategy(channel_period=2)
        df = _FakeDataFrame([10.0, 12.0, 11.0, 9.0])

        signal = strategy.check_signal(df, mid_line_crossed=False)

        self.assertIsNotNone(signal)
        self.assertEqual("short", signal["action"])
        self.assertTrue(signal.get("bootstrap_direct_entry"))

    def test_new_listing_can_open_long_directly_on_first_computable_breakout(self):
        strategy = TurtleStrategy(channel_period=2)
        df = _FakeDataFrame([12.0, 10.0, 11.0, 13.0])

        signal = strategy.check_signal(df, mid_line_crossed=False)

        self.assertIsNotNone(signal)
        self.assertEqual("long", signal["action"])
        self.assertTrue(signal.get("bootstrap_direct_entry"))

    def test_new_listing_later_breakdown_still_bypasses_mid_refresh(self):
        strategy = TurtleStrategy(channel_period=2)
        df = _FakeDataFrame([10.0, 12.0, 11.0, 11.5, 9.0])

        signal = strategy.check_signal(df, mid_line_crossed=False)

        self.assertIsNotNone(signal)
        self.assertEqual("short", signal["action"])
        self.assertTrue(signal.get("bootstrap_direct_entry"))

    def test_new_listing_later_breakout_still_bypasses_mid_refresh(self):
        strategy = TurtleStrategy(channel_period=2)
        df = _FakeDataFrame([10.0, 12.0, 12.1, 12.1, 12.5])

        signal = strategy.check_signal(df, mid_line_crossed=False)

        self.assertIsNotNone(signal)
        self.assertEqual("long", signal["action"])
        self.assertTrue(signal.get("bootstrap_direct_entry"))

    def test_new_listing_mid_cross_only_arms_without_opening(self):
        strategy = TurtleStrategy(channel_period=2)
        df = _FakeDataFrame([9.0, 10.0, 12.0, 10.4, 11.5])

        signal = strategy.check_signal(df, mid_line_crossed=False)

        self.assertIsNotNone(signal)
        self.assertEqual("close_short", signal["action"])
        self.assertTrue(signal.get("mid_line_crossed"))
        self.assertIsNone(signal.get("bootstrap_direct_entry"))

    def test_new_listing_same_day_mid_cross_and_upper_break_opens_long_normally(self):
        strategy = TurtleStrategy(channel_period=2)
        df = _FakeDataFrame([10.0, 12.0, 10.4, 10.5, 12.5])

        signal = strategy.check_signal(df, mid_line_crossed=False)

        self.assertIsNotNone(signal)
        self.assertEqual("long", signal["action"])
        self.assertTrue(signal.get("mid_line_crossed"))
        self.assertIsNone(signal.get("bootstrap_direct_entry"))

    def test_aster_hbar_wld_brev_family_of_missed_shorts(self):
        # 同一个结构按不同价格尺度缩放，覆盖 ASTER/HBAR/WLD/BREV 这类
        # “先下穿中轨、次日首次下破下轨”的漏单家族。
        cases = {
            "ASTER-family": [0.7000, 0.9800, 0.9100, 0.9240, 0.8750],
            "HBAR-family": [0.0700, 0.0980, 0.0910, 0.0924, 0.0875],
            "WLD-family": [0.2800, 0.3920, 0.3640, 0.3696, 0.3500],
            "BREV-family": [0.1000, 0.1400, 0.1300, 0.1320, 0.1250],
        }
        for label, closes in cases.items():
            with self.subTest(label=label):
                self._assert_next_day_first_breakout_short(closes, label)


if __name__ == "__main__":
    unittest.main()
