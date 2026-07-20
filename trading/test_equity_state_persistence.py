"""权益辅助状态不得在损坏后静默清空或覆盖。"""

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from equity_tracker import (
    EquityStatePersistenceError,
    EquitySyncCommitDurabilityError,
    EquityTracker,
)
from trade_state import AtomicWriteCommitDurabilityError


class EquityStatePersistenceTest(unittest.TestCase):
    def _tracker(self, data_dir, notifications=None, system=None):
        notifications = notifications if notifications is not None else []
        return EquityTracker(
            data_dir,
            system or SimpleNamespace(),
            notify_failure=lambda label, path: notifications.append((label, path)),
        )

    def test_new_deployment_gets_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)

            self.assertEqual(tracker.load_daily_equity(), [])
            self.assertEqual(tracker.load_equity_ticks(), [])
            self.assertEqual(tracker.load_peak_equity()['peak_equity'], 0)

    def test_timezone_aware_tick_is_normalized_before_retention_comparison(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            aware = datetime.now(timezone(timedelta(hours=8))).isoformat()

            trimmed = tracker._trim_equity_ticks([{
                'timestamp': aware, 'recorded_at': aware,
                'equity': 1000, 'qiusuo_index': 1853,
            }])

            self.assertEqual(1, len(trimmed))
            self.assertNotIn('+', trimmed[0]['timestamp'])

    def test_valid_backup_recovers_corrupt_main(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            expected = [{'date': '2026-07-09', 'equity': 1000}]
            with open(tracker.DAILY_EQUITY_FILE, 'w', encoding='utf-8') as f:
                f.write('{broken')
            with open(tracker.DAILY_EQUITY_FILE + '.bak', 'w', encoding='utf-8') as f:
                json.dump(expected, f)

            self.assertEqual(tracker.load_daily_equity(), expected)
            with open(tracker.DAILY_EQUITY_FILE, 'r', encoding='utf-8') as f:
                self.assertEqual(json.load(f), expected)

    def test_missing_main_recovers_valid_backup_instead_of_resetting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            expected = {'peak_equity': 1234.5, 'peak_time': '2026-07-10T08:00:00'}
            with open(tracker.PEAK_EQUITY_FILE + '.bak', 'w', encoding='utf-8') as f:
                json.dump(expected, f)

            self.assertEqual(tracker.load_peak_equity(), expected)
            with open(tracker.PEAK_EQUITY_FILE, 'r', encoding='utf-8') as f:
                self.assertEqual(json.load(f), expected)
            self.assertEqual(
                stat.S_IMODE(os.stat(tracker.PEAK_EQUITY_FILE).st_mode), 0o600)

    def test_missing_main_with_invalid_backup_refuses_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            with open(tracker.EQUITY_HISTORY_FILE + '.bak', 'w', encoding='utf-8') as f:
                json.dump([], f)
            with self.assertRaises(EquityStatePersistenceError):
                tracker.load_equity_history()

    def test_corrupt_main_without_valid_backup_refuses_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            with open(tracker.EQUITY_TICKS_FILE, 'w', encoding='utf-8') as f:
                f.write('{broken')

            with self.assertRaises(EquityStatePersistenceError):
                tracker.load_equity_ticks()

    def test_duplicate_json_keys_are_never_last_wins(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            with open(tracker.PEAK_EQUITY_FILE, 'w', encoding='utf-8') as f:
                f.write(
                    '{"peak_equity": 1000, "peak_equity": 0, '
                    '"peak_time": null}')

            with self.assertRaisesRegex(
                    EquityStatePersistenceError, '重复字段'):
                tracker.load_peak_equity()

    def test_valid_json_with_wrong_shape_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            with open(tracker.QIUSUO_INDEX_FILE, 'w', encoding='utf-8') as f:
                json.dump([], f)

            with self.assertRaises(EquityStatePersistenceError):
                tracker.load_qiusuo_index_state()

    def test_semantically_bad_tick_main_recovers_valid_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            valid = [{
                'timestamp': '2026-07-10T08:00',
                'recorded_at': '2026-07-10T08:00:01',
                'equity': 1000, 'qiusuo_index': 1853,
            }]
            with open(tracker.EQUITY_TICKS_FILE, 'w', encoding='utf-8') as f:
                json.dump([1], f)
            with open(tracker.EQUITY_TICKS_FILE + '.bak', 'w', encoding='utf-8') as f:
                json.dump(valid, f)

            self.assertEqual(valid, tracker.load_equity_ticks())

    def test_semantically_bad_peak_and_qiusuo_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            with open(tracker.PEAK_EQUITY_FILE, 'w', encoding='utf-8') as f:
                json.dump({'peak_equity': 'not-a-number', 'peak_time': None}, f)
            with self.assertRaises(EquityStatePersistenceError):
                tracker.load_peak_equity()

        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            with open(tracker.QIUSUO_INDEX_FILE, 'w', encoding='utf-8') as f:
                json.dump({'base_index': 1853, 'history': {}}, f)
            with self.assertRaises(EquityStatePersistenceError):
                tracker.load_qiusuo_index_state()

    def test_interrupted_equity_sync_journal_rolls_forward_whole_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            old = {
                'peak': {'peak_equity': 1000, 'peak_time': '2026-07-10T08:00:00'},
                'history': {
                    'max_drawdown': 1, 'max_dd_time': None,
                    'initial_equity': 1000, 'initial_time': '2026-07-10T08:00:00',
                    'year_start_equity': 1000, 'year_start_time': '2026-07-10T08:00:00',
                    'longest_drawdown_days': 1,
                },
                'qiusuo': {
                    'base_index': 1853, 'current_divisor': 1000 / 1853,
                    'anchor_equity': 1000, 'anchor_index': 1853,
                    'anchor_time': '2026-07-10T08:00:00',
                    'history': [{
                        'effective_from': '2026-07-10T08:00:00',
                        'divisor': 1000 / 1853, 'anchor_equity': 1000,
                        'anchor_index': 1853, 'reason': 'initial',
                    }],
                },
            }
            new = {
                'peak': {'peak_equity': 1500, 'peak_time': '2026-07-11T08:00:00'},
                'history': {
                    'max_drawdown': 0, 'max_dd_time': None,
                    'initial_equity': 1500, 'initial_time': '2026-07-11T08:00:00',
                    'year_start_equity': 1500, 'year_start_time': '2026-07-11T08:00:00',
                    'longest_drawdown_days': 0,
                },
                'qiusuo': {
                    'base_index': 1853, 'current_divisor': 1500 / 1853,
                    'anchor_equity': 1500, 'anchor_index': 1853,
                    'anchor_time': '2026-07-11T08:00:00',
                    'history': [{
                        'effective_from': '2026-07-11T08:00:00',
                        'divisor': 1500 / 1853, 'anchor_equity': 1500,
                        'anchor_index': 1853, 'reason': 'equity_sync',
                    }],
                },
            }
            # 模拟崩溃：journal 已提交，但只写了第一个主文件。
            with open(tracker.EQUITY_SYNC_JOURNAL_FILE, 'w', encoding='utf-8') as f:
                json.dump({'version': 1, 'old': old, 'new': new}, f)
            with open(tracker.PEAK_EQUITY_FILE, 'w', encoding='utf-8') as f:
                json.dump(new['peak'], f)
            with open(tracker.EQUITY_HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(old['history'], f)
            with open(tracker.QIUSUO_INDEX_FILE, 'w', encoding='utf-8') as f:
                json.dump(old['qiusuo'], f)

            recovered = self._tracker(temp_dir)

            self.assertEqual(new['peak'], recovered.load_peak_equity())
            self.assertEqual(new['history'], recovered.load_equity_history())
            self.assertEqual(new['qiusuo'], recovered.load_qiusuo_index_state())
            self.assertFalse(os.path.lexists(recovered.EQUITY_SYNC_JOURNAL_FILE))

            for version in (True, 1.0):
                with self.subTest(version=version):
                    with open(
                            tracker.EQUITY_SYNC_JOURNAL_FILE,
                            'w', encoding='utf-8') as handle:
                        json.dump({
                            'version': version, 'old': old, 'new': new,
                        }, handle)
                    with self.assertRaisesRegex(
                            EquityStatePersistenceError, 'journal 版本非法'):
                        self._tracker(temp_dir)
                    os.unlink(tracker.EQUITY_SYNC_JOURNAL_FILE)

    def test_completed_sync_backups_remain_on_same_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            old = {
                'peak': {'peak_equity': 1000, 'peak_time': None},
                'history': {'initial_equity': 1000},
                'qiusuo': {
                    'base_index': 1853, 'current_divisor': 1000 / 1853,
                    'history': [],
                },
            }
            new = {
                'peak': {'peak_equity': 1500, 'peak_time': None},
                'history': {'initial_equity': 1500},
                'qiusuo': {
                    'base_index': 1853, 'current_divisor': 1500 / 1853,
                    'history': [],
                },
            }
            self.assertTrue(tracker._commit_equity_sync_generation(old, new))

            with open(tracker.QIUSUO_INDEX_FILE, 'w', encoding='utf-8') as f:
                f.write('{broken')

            self.assertEqual(new['qiusuo'], tracker.load_qiusuo_index_state())
            self.assertEqual(new['peak'], tracker.load_peak_equity())
            self.assertEqual(new['history'], tracker.load_equity_history())

    def test_journal_retirement_failure_never_rolls_back_committed_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            old = {
                'peak': {'peak_equity': 1000, 'peak_time': None},
                'history': {'initial_equity': 1000},
                'qiusuo': {
                    'base_index': 1853,
                    'current_divisor': 1000 / 1853,
                    'history': [],
                },
            }
            new = {
                'peak': {'peak_equity': 1500, 'peak_time': None},
                'history': {'initial_equity': 1500},
                'qiusuo': {
                    'base_index': 1853,
                    'current_divisor': 1500 / 1853,
                    'history': [],
                },
            }
            error = EquitySyncCommitDurabilityError('journal fsync failed')

            with patch.object(
                    tracker, '_remove_sync_journal', side_effect=error):
                with self.assertRaises(EquitySyncCommitDurabilityError):
                    tracker._commit_equity_sync_generation(old, new)

            for key, (path, _expected) in tracker._equity_sync_targets().items():
                with open(path, 'r', encoding='utf-8') as handle:
                    self.assertEqual(new[key], json.load(handle))
                with open(path + '.bak', 'r', encoding='utf-8') as handle:
                    self.assertEqual(new[key], json.load(handle))
            self.assertTrue(os.path.lexists(tracker.EQUITY_SYNC_JOURNAL_FILE))

    def test_precommit_directory_ambiguity_is_finished_in_same_lock_stack(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            old = {
                'peak': {'peak_equity': 1000, 'peak_time': None},
                'history': {'initial_equity': 1000},
                'qiusuo': {
                    'base_index': 1853,
                    'current_divisor': 1000 / 1853,
                    'history': [],
                },
            }
            new = {
                'peak': {'peak_equity': 1500, 'peak_time': None},
                'history': {'initial_equity': 1500},
                'qiusuo': {
                    'base_index': 1853,
                    'current_divisor': 1500 / 1853,
                    'history': [],
                },
            }
            real_write = tracker._atomic_write_json
            injected = False

            def ambiguous_journal_write(path, data):
                nonlocal injected
                written = real_write(path, data)
                if path == tracker.EQUITY_SYNC_JOURNAL_FILE and not injected:
                    injected = True
                    raise AtomicWriteCommitDurabilityError(
                        path, OSError('directory fsync'))
                return written

            with patch.object(
                    tracker, '_atomic_write_json',
                    side_effect=ambiguous_journal_write):
                with self.assertRaises(EquitySyncCommitDurabilityError):
                    tracker._commit_equity_sync_generation(old, new)

            self.assertFalse(os.path.lexists(tracker.EQUITY_SYNC_JOURNAL_FILE))
            for key, (path, _expected) in tracker._equity_sync_targets().items():
                with open(path, 'r', encoding='utf-8') as handle:
                    self.assertEqual(new[key], json.load(handle))
                with open(path + '.bak', 'r', encoding='utf-8') as handle:
                    self.assertEqual(new[key], json.load(handle))

    def test_unlink_directory_fsync_failure_latches_and_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            degraded = []
            system = SimpleNamespace(trade_state=SimpleNamespace(
                mark_runtime_persistence_degraded=degraded.append))
            tracker = self._tracker(temp_dir, system=system)
            with open(
                    tracker.EQUITY_SYNC_JOURNAL_FILE,
                    'w', encoding='utf-8') as handle:
                json.dump({'version': 1}, handle)

            with patch('equity_tracker.os.fsync', side_effect=OSError('disk')):
                with self.assertRaises(EquitySyncCommitDurabilityError):
                    tracker._remove_sync_journal()

            self.assertFalse(os.path.lexists(tracker.EQUITY_SYNC_JOURNAL_FILE))
            self.assertEqual(
                ['equity_journal_removal_not_durable'], degraded)

    def test_nonfinite_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tracker = self._tracker(temp_dir)
            with open(tracker.PEAK_EQUITY_FILE, 'w', encoding='utf-8') as f:
                f.write('{"peak_equity": NaN, "peak_time": null}')

            with self.assertRaises(EquityStatePersistenceError):
                tracker.load_peak_equity()

    def test_save_keeps_last_verified_version_and_refuses_corrupt_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            notifications = []
            tracker = self._tracker(temp_dir, notifications)
            first = {'peak_equity': 1000, 'peak_time': None}
            second = {'peak_equity': 1100, 'peak_time': '2026-07-10T08:00:00'}
            self.assertTrue(tracker.save_peak_equity(first))
            self.assertTrue(tracker.save_peak_equity(second))
            with open(tracker.PEAK_EQUITY_FILE + '.bak', 'r', encoding='utf-8') as f:
                self.assertEqual(json.load(f), first)

            with open(tracker.PEAK_EQUITY_FILE, 'w', encoding='utf-8') as f:
                f.write('{broken')
            self.assertFalse(tracker.save_peak_equity({'peak_equity': 1200}))
            self.assertTrue(notifications)
            with open(tracker.PEAK_EQUITY_FILE, 'r', encoding='utf-8') as f:
                self.assertEqual(f.read(), '{broken')


if __name__ == '__main__':
    unittest.main()
