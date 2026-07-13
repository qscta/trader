"""峰值口径一次性迁移单测（纯标准库）。用审查给出的真实生产数据校验纠正结果。"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import migrate_peak_baseline as mig


# 审查现场数据：5 分钟采样把峰值污染到 11031.3579 @ 08:15；应有峰值是
# 资金同步基准 11001.3914 @ 07-12 20:29（07-13 日收盘 10991.0692 低于基准）。
POLLUTED_PEAK = {'peak_equity': 11031.3579, 'peak_time': '2026-07-13T08:15:15'}
HISTORY = {'initial_equity': 11001.3914, 'initial_time': '2026-07-12T20:29:51',
           'longest_drawdown_days': 0, 'max_drawdown': 0}
DAILY = [{'date': '2026-07-13', 'equity': 10991.0692, 'qiusuo_index': 1850.0}]


class RecomputeTest(unittest.TestCase):
    def test_baseline_wins_over_lower_daily_close(self):
        a = mig.recompute_daily_close_peak(POLLUTED_PEAK, HISTORY, DAILY)
        self.assertAlmostEqual(a['recomputed_peak_equity'], 11001.3914, places=4)
        self.assertEqual(a['recomputed_peak_time'], '2026-07-12T20:29:51')
        self.assertEqual(a['recomputed_peak_advanced_day'], '2026-07-12')
        self.assertEqual(a['source'], 'baseline')

    def test_higher_daily_close_after_baseline_wins(self):
        daily = [{'date': '2026-07-13', 'equity': 12000.0}]
        a = mig.recompute_daily_close_peak(POLLUTED_PEAK, HISTORY, daily)
        self.assertAlmostEqual(a['recomputed_peak_equity'], 12000.0, places=4)
        self.assertEqual(a['source'], 'daily:2026-07-13')

    def test_pre_baseline_daily_close_ignored(self):
        # 同步(07-12)之前的旧收盘即便很高也不计入基准之后的高水位。
        daily = [{'date': '2026-07-01', 'equity': 99999.0},
                 {'date': '2026-07-13', 'equity': 10991.0692}]
        a = mig.recompute_daily_close_peak(POLLUTED_PEAK, HISTORY, daily)
        self.assertAlmostEqual(a['recomputed_peak_equity'], 11001.3914, places=4)


class RunTest(unittest.TestCase):
    def _seed(self, tmp):
        for name, payload in (('peak_equity.json', POLLUTED_PEAK),
                              ('equity_history.json', HISTORY),
                              ('daily_equity.json', DAILY)):
            with open(os.path.join(tmp, name), 'w', encoding='utf-8') as f:
                json.dump(payload, f)

    def test_dry_run_does_not_modify(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            self.assertEqual(0, mig.run(tmp, apply=False))
            with open(os.path.join(tmp, 'peak_equity.json')) as f:
                self.assertEqual(json.load(f), POLLUTED_PEAK)  # 未改动

    def test_apply_corrects_downward_backs_up_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            self.assertEqual(0, mig.run(tmp, apply=True))
            with open(os.path.join(tmp, 'peak_equity.json')) as f:
                corrected = json.load(f)
            self.assertAlmostEqual(corrected['peak_equity'], 11001.3914, places=4)
            self.assertEqual(corrected['peak_time'], '2026-07-12T20:29:51')
            self.assertEqual(corrected['peak_advanced_day'], '2026-07-12')
            # 备份存在且是被污染的原值
            backups = [n for n in os.listdir(tmp) if '.premigrate.' in n]
            self.assertEqual(len(backups), 1)
            with open(os.path.join(tmp, backups[0])) as f:
                self.assertEqual(json.load(f), POLLUTED_PEAK)
            # 再跑一次：已纠正 → 无需再改（幂等）
            self.assertEqual(0, mig.run(tmp, apply=True))
            self.assertEqual(
                1, len([n for n in os.listdir(tmp) if '.premigrate.' in n]))

    def test_never_raises_a_legitimate_peak(self):
        # 落盘峰值已等于日收盘高水位（未被污染）→ 不纠正、不备份。
        with tempfile.TemporaryDirectory() as tmp:
            clean_peak = {'peak_equity': 11001.3914, 'peak_time': '2026-07-12T20:29:51'}
            with open(os.path.join(tmp, 'peak_equity.json'), 'w') as f:
                json.dump(clean_peak, f)
            with open(os.path.join(tmp, 'equity_history.json'), 'w') as f:
                json.dump(HISTORY, f)
            with open(os.path.join(tmp, 'daily_equity.json'), 'w') as f:
                json.dump(DAILY, f)
            self.assertEqual(0, mig.run(tmp, apply=True))
            self.assertEqual([], [n for n in os.listdir(tmp) if '.premigrate.' in n])


if __name__ == '__main__':
    unittest.main()
