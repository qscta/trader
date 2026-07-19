"""Web 进程锁与账本 schema/耐久性回归（纯标准库）。"""

import json
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import trade_state as trade_state_module
from runtime_guard import runner_lock_path
from trade_state import (
    AtomicWriteCommitDurabilityError,
    TradeState,
    TradeStateCommitDurabilityError,
    TradeStatePersistenceError,
    atomic_write_json,
)


def _closed_trade(symbol):
    """构造可被生产 schema 消费的最小历史成交。"""
    return {
        'symbol': symbol, 'side': 'long',
        'entry_price': 100.0, 'exit_price': 101.0,
        'position_size': 1.0,
    }


class TradeStateSchemaSafetyTest(unittest.TestCase):
    def test_valid_json_with_wrong_shape_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump([], f)
            with self.assertRaises(TradeStatePersistenceError):
                TradeState(path)

    def test_invalid_main_recovers_backup_and_repairs_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('{broken')
            expected = {
                'open_positions': {}, 'closed_trades': [], 'signal_states': {},
                'stop_loss_dates': {}, 'position_quarantines': {},
            }
            with open(path + '.bak', 'w', encoding='utf-8') as f:
                json.dump(expected, f)

            state = TradeState(path)

            self.assertEqual(state.get_all_open_positions(), {})
            with open(path, 'r', encoding='utf-8') as f:
                self.assertEqual(json.load(f), expected)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_nonfinite_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('{"open_positions": {}, "closed_trades": [], "x": NaN}')
            with self.assertRaises(TradeStatePersistenceError):
                TradeState(path)
            self.assertFalse(atomic_write_json(path, {'x': float('nan')}))

    def test_existing_state_is_private_and_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {'open_positions': {}, 'closed_trades': []}
            path = os.path.join(tmp, 'trade_state.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
            os.chmod(path, 0o644)

            TradeState(path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

            target = os.path.join(tmp, 'real-state.json')
            with open(target, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
            link = os.path.join(tmp, 'linked-state.json')
            os.symlink(target, link)
            with self.assertRaises(TradeStatePersistenceError):
                TradeState(link)

    def test_schema_rejects_unknown_signal_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            payload = state.get_default_state()
            payload['signal_states']['BTCUSDT'] = {
                'obsolete_number': True,
            }
            with self.assertRaises(ValueError):
                TradeState.validate_state(payload)

    def test_directory_fsync_failure_reports_committed_but_not_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'state.json')
            with patch.object(
                    trade_state_module.os, 'fsync',
                    side_effect=[None, OSError('directory fsync unavailable')]):
                with self.assertRaises(AtomicWriteCommitDurabilityError):
                    atomic_write_json(path, {'committed': True})
            with open(path, 'r', encoding='utf-8') as f:
                self.assertEqual(json.load(f), {'committed': True})

    def test_main_replace_durability_failure_preserves_memory_and_latches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            state = TradeState(path)
            with patch.object(
                    trade_state_module.os, 'fsync',
                    side_effect=[None, OSError('directory fsync unavailable')]):
                with self.assertRaises(TradeStateCommitDurabilityError):
                    state.mark_candle_processed('BTCUSDT', 'c1')

            self.assertEqual(
                'c1', state.get_signal_metadata('BTCUSDT')[
                    'last_processed_candle'])
            self.assertEqual(
                {
                    'degraded': True,
                    'context': 'state_directory_fsync_failed_after_replace',
                },
                state.get_runtime_persistence_status())
            with open(path, 'r', encoding='utf-8') as f:
                self.assertEqual(
                    'c1', json.load(f)['signal_states']['BTCUSDT'][
                        'last_processed_candle'])

    def test_backup_failure_aborts_commit_and_rolls_back_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.mark_candle_processed('BTCUSDT', 'c1')
            real_write = trade_state_module.atomic_write_json

            def fail_backup(path, data):
                return False if path.endswith('.bak') else real_write(path, data)

            with patch.object(trade_state_module, 'atomic_write_json', side_effect=fail_backup):
                with self.assertRaises(TradeStatePersistenceError):
                    state.mark_candle_processed('BTCUSDT', 'c2')
            self.assertEqual(
                'c1', state.get_signal_metadata('BTCUSDT')['last_processed_candle'])

    def test_symbol_metadata_cleanup_preserves_quarantine_until_confirmed_flat(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.mark_candle_processed('OLDUSDT', 'c1')
            state.replace_stop_loss_dates({'OLDUSDT': '2026-07-10'})
            state.mark_position_quarantine('OLDUSDT', 'exchange orphan')

            self.assertTrue(state.remove_symbol_metadata('OLDUSDT'))
            self.assertEqual(state.get_signal_metadata('OLDUSDT'), {})
            self.assertNotIn('OLDUSDT', state.get_stop_loss_dates())
            self.assertTrue(state.is_position_quarantined('OLDUSDT'))

            self.assertTrue(state.remove_symbol_metadata(
                'OLDUSDT', clear_quarantine=True))
            self.assertFalse(state.is_position_quarantined('OLDUSDT'))

    def test_inactive_metadata_pruning_keeps_active_and_quarantined_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.mark_candle_processed('ACTIVEUSDT', 'c1')
            state.mark_candle_processed('OLDUSDT', 'c1')
            state.mark_candle_processed('QUARANTINEDUSDT', 'c1')
            state.replace_stop_loss_dates({
                'ACTIVEUSDT': '2026-07-10', 'OLDUSDT': '2026-07-10'})
            state.mark_position_quarantine('QUARANTINEDUSDT', 'exchange orphan')

            removed = state.prune_inactive_symbol_metadata({'ACTIVEUSDT'})

            self.assertEqual(removed, ['OLDUSDT'])
            self.assertTrue(state.get_signal_metadata('ACTIVEUSDT'))
            self.assertEqual(state.get_signal_metadata('OLDUSDT'), {})
            self.assertTrue(state.get_signal_metadata('QUARANTINEDUSDT'))

    def test_archive_is_not_reparsed_on_every_page_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            with open(state.archive_file, 'w', encoding='utf-8') as f:
                json.dump([_closed_trade('OLDUSDT')], f)
            real_load = trade_state_module.json.load
            calls = []

            def counting_load(*args, **kwargs):
                calls.append(1)
                return real_load(*args, **kwargs)

            with patch.object(trade_state_module.json, 'load', side_effect=counting_load):
                state.get_closed_trades()
                state.get_closed_trades()
            self.assertEqual(len(calls), 1)

    def test_closed_trade_page_spans_recent_and_archive_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            with open(state.archive_file, 'w', encoding='utf-8') as f:
                json.dump([_closed_trade(f'T{i}') for i in range(5)], f)
            state.state['closed_trades'] = [
                _closed_trade('T5'), _closed_trade('T6')]

            page1, total = state.get_closed_trades_page(1, 3)
            page2, _ = state.get_closed_trades_page(2, 3)
            page3, _ = state.get_closed_trades_page(3, 3)

            self.assertEqual(total, 7)
            self.assertEqual([t['symbol'] for t in page1], ['T6', 'T5', 'T4'])
            self.assertEqual([t['symbol'] for t in page2], ['T3', 'T2', 'T1'])
            self.assertEqual([t['symbol'] for t in page3], ['T0'])


class RuntimeGuardTest(unittest.TestCase):
    def test_second_process_cannot_acquire_private_project_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, 'private', 'runner.lock')
            env = os.environ.copy()
            env['TRADING_RUNNER_LOCK_FILE'] = lock_path
            code = (
                'from runtime_guard import acquire_runner_lock; '
                'acquire_runner_lock(); print("ready", flush=True); input()'
            )
            first = subprocess.Popen(
                [sys.executable, '-c', code], cwd=os.path.dirname(__file__), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(first.stdout.readline().strip(), 'ready')
                second = subprocess.run(
                    [sys.executable, '-c',
                     'from runtime_guard import acquire_runner_lock; acquire_runner_lock()'],
                    cwd=os.path.dirname(__file__), env=env, capture_output=True, text=True,
                    timeout=5,
                )
                self.assertNotEqual(second.returncode, 0)
                self.assertEqual(stat.S_IMODE(os.stat(lock_path).st_mode), 0o600)
                self.assertEqual(
                    stat.S_IMODE(os.stat(os.path.dirname(lock_path)).st_mode), 0o700)
            finally:
                if first.stdin:
                    first.stdin.write('\n')
                    first.stdin.flush()
                first.wait(timeout=5)
                for stream in (first.stdin, first.stdout, first.stderr):
                    if stream:
                        stream.close()

    def test_default_lock_is_not_in_shared_tmp(self):
        self.assertIn(os.path.join('trading', '.runtime'), runner_lock_path())

    def test_existing_shared_parent_is_rejected_without_chmod(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = os.path.join(tmp, 'shared')
            os.mkdir(shared, 0o755)
            os.chmod(shared, 0o755)
            env = os.environ.copy()
            env['TRADING_RUNNER_LOCK_FILE'] = os.path.join(shared, 'runner.lock')
            result = subprocess.run(
                [sys.executable, '-c',
                 'from runtime_guard import acquire_runner_lock; acquire_runner_lock()'],
                cwd=os.path.dirname(__file__), env=env, capture_output=True, text=True,
                timeout=5,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(stat.S_IMODE(os.stat(shared).st_mode), 0o755)
            self.assertFalse(os.path.exists(os.path.join(shared, 'runner.lock')))

    def test_symlink_lock_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, 'private')
            os.mkdir(target, 0o700)
            linked = os.path.join(tmp, 'linked')
            os.symlink(target, linked)
            env = os.environ.copy()
            env['TRADING_RUNNER_LOCK_FILE'] = os.path.join(linked, 'runner.lock')
            result = subprocess.run(
                [sys.executable, '-c',
                 'from runtime_guard import acquire_runner_lock; acquire_runner_lock()'],
                cwd=os.path.dirname(__file__), env=env, capture_output=True, text=True,
                timeout=5,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(os.path.exists(os.path.join(target, 'runner.lock')))

    def test_gunicorn_baseline_uses_single_gthread_worker_and_long_timeout(self):
        path = os.path.join(os.path.dirname(__file__), 'gunicorn.conf.py')
        spec = importlib.util.spec_from_file_location('trading_gunicorn_config', path)
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
        self.assertEqual(config.workers, 1)
        self.assertEqual(config.worker_class, 'gthread')
        self.assertGreaterEqual(config.threads, 2)
        self.assertGreaterEqual(config.timeout, 60)
        self.assertGreaterEqual(config.graceful_timeout, 60)


if __name__ == '__main__':
    unittest.main()
