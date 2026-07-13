"""日内峰值污染的一次性迁移测试。"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import migrate_peak_baseline as migration


POLLUTED_PEAK = {
    'peak_equity': 11031.3579,
    'peak_time': '2026-07-13T08:15:15',
}
HISTORY = {
    'initial_equity': 11001.3914,
    'initial_time': '2026-07-12T20:29:51',
    'longest_drawdown_days': 0,
    'max_drawdown': 0,
}
DAILY = [
    {'date': '2026-07-13', 'equity': 10991.0692, 'qiusuo_index': 1850.0},
]


class RecomputeTest(unittest.TestCase):
    def test_baseline_wins_over_lower_daily_close(self):
        result = migration.recompute_daily_close_peak(
            POLLUTED_PEAK, HISTORY, DAILY)
        self.assertAlmostEqual(
            result['recomputed_peak_equity'], 11001.3914, places=4)
        self.assertEqual(
            result['recomputed_peak_time'], '2026-07-12T20:29:51')
        self.assertEqual(
            result['recomputed_peak_observed_day'], '2026-07-13')
        self.assertEqual(result['source'], 'baseline')

    def test_higher_daily_close_after_baseline_wins(self):
        result = migration.recompute_daily_close_peak(
            POLLUTED_PEAK,
            HISTORY,
            [{'date': '2026-07-13', 'equity': 12000.0}],
        )
        self.assertAlmostEqual(
            result['recomputed_peak_equity'], 12000.0, places=4)
        self.assertEqual(result['source'], 'daily:2026-07-13')

    def test_pre_baseline_daily_close_ignored(self):
        result = migration.recompute_daily_close_peak(
            POLLUTED_PEAK,
            HISTORY,
            [
                {'date': '2026-07-01', 'equity': 99999.0},
                {'date': '2026-07-13', 'equity': 10991.0692},
            ],
        )
        self.assertAlmostEqual(
            result['recomputed_peak_equity'], 11001.3914, places=4)

    def test_same_day_snapshot_before_equity_sync_is_ignored(self):
        """07-12 08:00 收盘发生在 20:29 资金同步前，不能混入新基线。"""
        result = migration.recompute_daily_close_peak(
            POLLUTED_PEAK,
            HISTORY,
            [
                {'date': '2026-07-12', 'equity': 15000.0},
                {'date': '2026-07-13', 'equity': 10991.0692},
            ],
        )
        self.assertAlmostEqual(
            result['recomputed_peak_equity'], 11001.3914, places=4)
        self.assertEqual(result['source'], 'baseline')

    def test_compacted_same_day_candle_closes_after_equity_sync(self):
        """带 samples 的 07-12 日线在 07-13 08:00 才收盘，发生在同步之后。"""
        compacted = {
            'date': '2026-07-12',
            'equity': 12000.0,
            'open': 1850.0,
            'high': 1900.0,
            'low': 1800.0,
            'close': 1890.0,
            'samples': 288,
        }
        result = migration.recompute_daily_close_peak(
            POLLUTED_PEAK, HISTORY, [compacted])
        self.assertAlmostEqual(
            result['recomputed_peak_equity'], 12000.0, places=4)
        self.assertEqual(
            result['recomputed_peak_time'], '2026-07-13T08:00:00')
        self.assertEqual(
            result['recomputed_peak_observed_day'], '2026-07-13')

    def test_missing_fund_flow_baseline_fails_closed(self):
        self.assertIsNone(migration.recompute_daily_close_peak(
            POLLUTED_PEAK,
            {},
            [{'date': '2026-07-13', 'equity': 9000.0}],
        ))


class RunTest(unittest.TestCase):
    @staticmethod
    def _seed(tmp):
        for name, payload in (
            ('peak_equity.json', POLLUTED_PEAK),
            ('equity_history.json', HISTORY),
            ('daily_equity.json', DAILY),
        ):
            with open(os.path.join(tmp, name), 'w', encoding='utf-8') as handle:
                json.dump(payload, handle)

    def test_dry_run_does_not_modify(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            self.assertEqual(0, migration.run(tmp, apply=False))
            with open(os.path.join(tmp, 'peak_equity.json')) as handle:
                self.assertEqual(json.load(handle), POLLUTED_PEAK)

    def test_apply_corrects_backs_up_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            self.assertEqual(0, migration.run(tmp, apply=True))
            with open(os.path.join(tmp, 'peak_equity.json')) as handle:
                corrected = json.load(handle)
            self.assertAlmostEqual(
                corrected['peak_equity'], 11001.3914, places=4)
            self.assertEqual(
                corrected['peak_time'], '2026-07-12T20:29:51')
            self.assertEqual(corrected['peak_observed_day'], '2026-07-13')
            backups = [name for name in os.listdir(tmp) if '.premigrate.' in name]
            self.assertEqual(len(backups), 1)
            with open(os.path.join(tmp, backups[0])) as handle:
                self.assertEqual(json.load(handle), POLLUTED_PEAK)
            self.assertEqual(0, migration.run(tmp, apply=True))
            self.assertEqual(
                1,
                len([name for name in os.listdir(tmp) if '.premigrate.' in name]),
            )

    def test_never_raises_a_legitimate_peak(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = {
                'peak_equity': 11001.3914,
                'peak_time': '2026-07-12T20:29:51',
            }
            for name, payload in (
                ('peak_equity.json', clean),
                ('equity_history.json', HISTORY),
                ('daily_equity.json', DAILY),
            ):
                with open(os.path.join(tmp, name), 'w') as handle:
                    json.dump(payload, handle)
            self.assertEqual(0, migration.run(tmp, apply=True))
            self.assertEqual(
                [], [name for name in os.listdir(tmp) if '.premigrate.' in name])


if __name__ == '__main__':
    unittest.main()
