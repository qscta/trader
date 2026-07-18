"""单策略部署预检的失败原子性、阻断与幂等回归。"""

import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import _test_stubs
import migrate_single_strategy as migration

TradingSystem = _test_stubs.import_main().TradingSystem


def _config():
    return {
        'strategy': {'default_risk_per_trade': 0.01},
        'trading': {'symbols': [{
            'name': 'BTCUSDT', 'enabled': True,
            'risk_per_trade': 0.01, 'strategy': 'ma_cross'}]},
    }


def _ledger():
    return {
        'open_positions': {}, 'closed_trades': [], 'open_intents': {},
        'signal_states': {'BTCUSDT': {
            'strategy': 'ma_cross',
            'last_processed_candle': '2026-07-17T00:00:00',
            'last_update': '2026-07-18T08:00:00',
            'obsolete_flag': True,
            'obsolete_execution': {'status': 'pending'},
        }},
    }


def _write(path, payload):
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle)
    os.chmod(path, 0o600)


def _read(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


class NormalizeTest(unittest.TestCase):
    def test_normalizers_do_not_mutate_inputs(self):
        config = _config()
        ledger = _ledger()
        originals = copy.deepcopy((config, ledger))

        cleaned_config, config_report, blockers = migration.normalize_config(config)
        cleaned_ledger, ledger_report, ledger_blockers = migration.normalize_ledger(ledger)

        self.assertEqual(originals, (config, ledger))
        self.assertFalse(blockers)
        self.assertFalse(ledger_blockers)
        self.assertNotIn('strategy', cleaned_config['trading']['symbols'][0])
        self.assertEqual(
            {'last_processed_candle', 'last_update'},
            set(cleaned_ledger['signal_states']['BTCUSDT']))
        self.assertTrue(config_report)
        self.assertTrue(ledger_report)

    def test_pending_intent_and_incompatible_position_block(self):
        ledger = _ledger()
        ledger['open_intents']['BTCUSDT'] = {'status': 'pending'}
        ledger['open_positions']['ETHUSDT'] = {'strategy': 'unsupported'}

        _cleaned, _report, blockers = migration.normalize_ledger(ledger)

        self.assertEqual(2, len(blockers))

    def test_pending_signal_execution_blocks_instead_of_being_deleted(self):
        ledger = _ledger()
        ledger['signal_states']['BTCUSDT']['signal_execution'] = {
            'strategy': 'ma_cross',
            'signal_id': '2026-07-17T00:00:00',
            'client_order_id': 'ma-open-BTC-123',
            'status': 'pending',
        }

        cleaned, _report, blockers = migration.normalize_ledger(ledger)

        self.assertTrue(blockers)
        self.assertIn(
            'signal_execution', cleaned['signal_states']['BTCUSDT'])

    def test_unknown_signal_execution_status_blocks_instead_of_being_deleted(self):
        ledger = _ledger()
        ledger['signal_states']['BTCUSDT']['signal_execution'] = {
            'status': 'submitted_unknown',
        }

        cleaned, _report, blockers = migration.normalize_ledger(ledger)

        self.assertTrue(blockers)
        self.assertIn(
            'signal_execution', cleaned['signal_states']['BTCUSDT'])

    def test_config_uses_production_risk_and_symbol_validators(self):
        for mutate in (
                lambda config: config['strategy'].__setitem__(
                    'default_risk_per_trade', 1.0),
                lambda config: config['trading']['symbols'][0].__setitem__(
                    'name', 'BTC-USDT')):
            with self.subTest(mutate=mutate):
                config = _config()
                mutate(config)
                _cleaned, _report, blockers = migration.normalize_config(config)
                self.assertTrue(blockers)

    def test_unlabelled_active_position_blocks_instead_of_being_guessed(self):
        ledger = _ledger()
        ledger['open_positions']['BTCUSDT'] = {
            'symbol': 'BTCUSDT', 'side': 'long'}

        _cleaned, _report, blockers = migration.normalize_ledger(ledger)

        self.assertEqual(1, len(blockers))
        self.assertIn('必须人工裁决', blockers[0])

    def test_archive_removes_only_incompatible_labels(self):
        records = [
            {'symbol': 'BTCUSDT', 'strategy': 'ma_cross'},
            {'symbol': 'ETHUSDT', 'strategy': 'unsupported'},
        ]

        cleaned, report, blockers = migration.normalize_archive(records)

        self.assertFalse(blockers)
        self.assertEqual('ma_cross', cleaned[0]['strategy'])
        self.assertNotIn('strategy', cleaned[1])
        self.assertEqual(1, len(report))

    def test_empty_wrong_container_types_are_not_silently_repaired(self):
        for field, bad in (
                ('open_positions', []),
                ('open_intents', []),
                ('signal_states', []),
                ('closed_trades', {})):
            with self.subTest(field=field):
                ledger = _ledger()
                ledger[field] = bad

                _cleaned, _report, blockers = migration.normalize_ledger(ledger)

                self.assertTrue(blockers)

    def test_unknown_config_fields_block_instead_of_surviving_migration(self):
        for section in ('strategy', 'symbol'):
            with self.subTest(section=section):
                config = _config()
                if section == 'strategy':
                    config['strategy']['obsolete_period'] = 20
                else:
                    config['trading']['symbols'][0]['obsolete_flag'] = True

                _cleaned, _report, blockers = migration.normalize_config(config)

                self.assertTrue(blockers)


class RunTest(unittest.TestCase):
    def _paths(self, tmp):
        config_path = os.path.join(tmp, 'config.json')
        state_path = os.path.join(tmp, 'trade_state.json')
        _write(config_path, _config())
        _write(state_path, _ledger())
        return config_path, state_path

    def test_missing_required_file_is_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(migration.EXIT_UNSAFE, migration.run(tmp))

    def test_final_data_dir_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, 'real')
            os.mkdir(real)
            self._paths(real)
            linked = os.path.join(tmp, 'linked')
            os.symlink(real, linked)

            self.assertEqual(migration.EXIT_UNSAFE, migration.run(linked))

    def test_dry_run_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, state_path = self._paths(tmp)
            originals = (_read(config_path), _read(state_path))

            self.assertEqual(migration.EXIT_OK, migration.run(tmp))

            self.assertEqual(originals, (_read(config_path), _read(state_path)))
            self.assertFalse(any('.premigrate.' in name for name in os.listdir(tmp)))

    def test_dry_run_rejects_unsafe_mode_without_chmod(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, _state_path = self._paths(tmp)
            os.chmod(config_path, 0o644)

            self.assertEqual(migration.EXIT_UNSAFE, migration.run(tmp))

            self.assertEqual(0o644, os.stat(config_path).st_mode & 0o777)

    def test_apply_backs_up_both_files_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, state_path = self._paths(tmp)

            self.assertEqual(
                migration.EXIT_OK, migration.run(tmp, apply=True))
            self.assertNotIn(
                'strategy', _read(config_path)['trading']['symbols'][0])
            self.assertEqual(
                {'last_processed_candle', 'last_update'},
                set(_read(state_path)['signal_states']['BTCUSDT']))
            backups = [name for name in os.listdir(tmp)
                       if '.premigrate.' in name]
            self.assertEqual(2, len(backups))

            self.assertEqual(
                migration.EXIT_OK, migration.run(tmp, apply=True))
            self.assertEqual(
                backups,
                [name for name in os.listdir(tmp) if '.premigrate.' in name])

    def test_apply_normalizes_year_archive_in_same_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._paths(tmp)
            archive = os.path.join(tmp, 'closed_trades_archive_2025.json')
            _write(archive, [{
                'symbol': 'BTCUSDT', 'strategy': 'unsupported'}])

            self.assertEqual(
                migration.EXIT_OK, migration.run(tmp, apply=True))

            self.assertNotIn('strategy', _read(archive)[0])
            self.assertEqual(
                3, len([name for name in os.listdir(tmp)
                        if '.premigrate.' in name]))

    def test_apply_includes_runtime_undated_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._paths(tmp)
            archive = os.path.join(
                tmp, 'closed_trades_archive_undated.json')
            _write(archive, [{
                'symbol': 'BTCUSDT', 'strategy': 'unsupported'}])

            self.assertEqual(
                migration.EXIT_OK, migration.run(tmp, apply=True))

            self.assertNotIn('strategy', _read(archive)[0])

    def test_backup_failure_is_nonzero_and_changes_no_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, state_path = self._paths(tmp)
            originals = (_read(config_path), _read(state_path))
            real_write = migration.atomic_write_json

            def fail_backup(path, payload):
                if '.premigrate.' in path:
                    return False
                return real_write(path, payload)

            with patch.object(migration, 'atomic_write_json', side_effect=fail_backup):
                result = migration.run(tmp, apply=True)

            self.assertEqual(migration.EXIT_BACKUP_FAILED, result)
            self.assertEqual(originals, (_read(config_path), _read(state_path)))

    def test_second_write_failure_restores_first_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, state_path = self._paths(tmp)
            originals = (_read(config_path), _read(state_path))
            real_write = migration.atomic_write_json
            writes = []

            def fail_second_original(path, payload):
                if path in (config_path, state_path):
                    writes.append(path)
                    if len(writes) == 2:
                        return False
                return real_write(path, payload)

            with patch.object(
                    migration, 'atomic_write_json', side_effect=fail_second_original):
                result = migration.run(tmp, apply=True)

            self.assertEqual(migration.EXIT_WRITE_FAILED, result)
            self.assertEqual(originals, (_read(config_path), _read(state_path)))

    def test_process_death_leaves_journal_and_next_run_recovers_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, state_path = self._paths(tmp)
            originals = (_read(config_path), _read(state_path))
            real_write = migration.atomic_write_json
            target_writes = []

            def die_on_second_target(path, payload):
                if path in (config_path, state_path):
                    target_writes.append(path)
                    if len(target_writes) == 2:
                        raise SystemExit(99)
                return real_write(path, payload)

            with patch.object(
                    migration, 'atomic_write_json', side_effect=die_on_second_target):
                with self.assertRaises(SystemExit):
                    migration.run(tmp, apply=True)

            journal = os.path.join(
                tmp, migration.cfgv.SINGLE_STRATEGY_MIGRATION_JOURNAL)
            self.assertTrue(os.path.exists(journal))
            self.assertNotEqual(originals[0], _read(config_path))

            mixed = (_read(config_path), _read(state_path))
            self.assertEqual(migration.EXIT_UNSAFE, migration.run(tmp))
            self.assertEqual(mixed, (_read(config_path), _read(state_path)))
            self.assertTrue(os.path.exists(journal))

            self.assertEqual(
                migration.EXIT_OK, migration.run(tmp, apply=True))
            self.assertNotIn(
                'strategy', _read(config_path)['trading']['symbols'][0])
            self.assertEqual(
                {'last_processed_candle', 'last_update'},
                set(_read(state_path)['signal_states']['BTCUSDT']))
            self.assertFalse(os.path.exists(journal))

    def test_runtime_refuses_unfinished_migration_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, 'config.json')
            journal = os.path.join(
                tmp, migration.cfgv.SINGLE_STRATEGY_MIGRATION_JOURNAL)
            _write(journal, {'version': 1, 'items': []})

            with self.assertRaisesRegex(RuntimeError, '未完成的单策略迁移'):
                TradingSystem(config_path)


if __name__ == '__main__':
    unittest.main()
