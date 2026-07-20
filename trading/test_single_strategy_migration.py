"""单策略部署预检的失败原子性、阻断与幂等回归。"""

import copy
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _test_stubs
import migrate_single_strategy as migration

_CLEANUP_SPEC = importlib.util.spec_from_file_location(
    'confirmed_config_cleanup',
    Path(__file__).resolve().parent / 'remove-one-confirmed-config-key.py',
)
cleanup = importlib.util.module_from_spec(_CLEANUP_SPEC)
_CLEANUP_SPEC.loader.exec_module(cleanup)

TradingSystem = _test_stubs.import_main().TradingSystem


def _config():
    return {
        'okx': {},
        'strategy': {'default_risk_per_trade': 0.01},
        'trading': {'symbols': [{
            'name': 'BTCUSDT', 'enabled': True,
            'risk_per_trade': 0.01, 'strategy': 'ma_cross'}]},
    }


def _ledger():
    return {
        'exchange': 'okx',
        'open_positions': {}, 'closed_trades': [], 'open_intents': {},
        'signal_states': {'BTCUSDT': {
            'strategy': 'ma_cross',
            'last_processed_candle': '2026-07-17T00:00:00',
            'last_update': '2026-07-18T08:00:00',
            'obsolete_flag': True,
            'obsolete_execution': {'status': 'pending'},
        }},
    }


def _closed_trade(symbol='BTCUSDT', strategy=None):
    """构造 runtime/archive strict schema 接受的最小历史成交。"""
    trade = {
        'symbol': symbol, 'side': 'long',
        'entry_price': 100.0, 'exit_price': 101.0,
        'position_size': 1.0,
    }
    if strategy is not None:
        trade['strategy'] = strategy
    return trade


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

    def test_explicit_null_execution_fields_block_migration(self):
        mutations = (
            lambda c: c['strategy'].__setitem__('ma_long_period', None),
            lambda c: c['trading']['symbols'][0].__setitem__(
                'risk_per_trade', None),
            lambda c: c['trading']['symbols'][0].__setitem__('enabled', None),
            lambda c: c.setdefault('scheduler', {}).__setitem__(
                'check_minute', None),
            lambda c: c.setdefault('okx', {}).__setitem__(
                'margin_mode', None),
            lambda c: c.setdefault('okx', {}).__setitem__(
                'leverage_overrides', None),
            lambda c: c.__setitem__('equity_tick_retention_days', None),
        )
        for mutate in mutations:
            config = _config()
            mutate(config)
            _cleaned, _report, blockers = migration.normalize_config(config)
            with self.subTest(config=config):
                self.assertTrue(blockers)

    def test_sandbox_string_false_is_normalized_not_treated_as_demo(self):
        config = _config()
        config['okx'] = {'sandbox': 'false'}

        cleaned, _report, blockers = migration.normalize_config(config)

        self.assertFalse(blockers)
        self.assertIs(cleaned['okx']['sandbox'], False)

    def test_legacy_okx_layout_is_removed_and_dual_source_blocks(self):
        config = _config()
        legacy = {
            'exchanges': {'okx': {
                'sandbox': False, 'strategy': config['strategy'],
                'trading': config['trading'],
            }},
        }

        cleaned, report, blockers = migration.normalize_config(legacy)

        self.assertFalse(blockers)
        self.assertNotIn('exchanges', cleaned)
        self.assertTrue(any('配置布局' in item for item in report))

        dual = _config()
        dual['okx'] = {'sandbox': False}
        dual['exchanges'] = {'okx': {'sandbox': True}}
        _cleaned, _report, blockers = migration.normalize_config(dual)
        self.assertTrue(blockers)

    def test_unknown_execution_block_fields_are_rejected(self):
        for block, key in (
                ('trading', 'obsolete_strategy'),
                ('scheduler', 'check_huor'),
                ('okx', 'obsolete_strategy')):
            config = _config()
            config.setdefault(block, {})[key] = True
            with self.subTest(block=block, key=key):
                _cleaned, _report, blockers = migration.normalize_config(config)
                self.assertTrue(blockers)

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
            _closed_trade('BTCUSDT', 'ma_cross'),
            _closed_trade('ETHUSDT', 'unsupported'),
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
        for section in ('top', 'strategy', 'symbol', 'dingtalk'):
            with self.subTest(section=section):
                config = _config()
                if section == 'top':
                    config['obsolete_execution'] = {}
                elif section == 'strategy':
                    config['strategy']['obsolete_period'] = 20
                elif section == 'dingtalk':
                    config['dingtalk'] = {'obsolete': True}
                else:
                    config['trading']['symbols'][0]['obsolete_flag'] = True

                _cleaned, _report, blockers = migration.normalize_config(config)

                self.assertTrue(blockers)

    def test_last_check_time_is_the_only_supported_top_level_cleanup(self):
        ledger = _ledger()
        ledger['last_check_time'] = '2026-07-18T08:00:00'

        cleaned, report, blockers = migration.normalize_ledger(ledger)

        self.assertFalse(blockers)
        self.assertNotIn('last_check_time', cleaned)
        self.assertTrue(any('last_check_time' in item for item in report))

    def test_unknown_ledger_fields_block_at_every_execution_layer(self):
        valid_position = {
            'symbol': 'BTCUSDT', 'side': 'long', 'entry_price': 100.0,
            'position_size': 1.0, 'stop_loss_price': 90.0,
            'strategy': 'ma_cross',
        }
        valid_closed = {
            'symbol': 'BTCUSDT', 'side': 'long', 'entry_price': 100.0,
            'exit_price': 110.0, 'position_size': 1.0,
            'partial_closes': [{
                'position_size': 0.5, 'exit_price': 105.0,
                'exit_notional': 52.5, 'gross_pnl': 2.5,
                'exit_fee': 0.01,
            }],
        }
        cases = []

        ledger = _ledger()
        ledger['obsolete_execution'] = {}
        cases.append(ledger)

        ledger = _ledger()
        ledger['open_positions']['BTCUSDT'] = dict(
            valid_position, obsolete_field=True)
        cases.append(ledger)

        ledger = _ledger()
        closed = dict(valid_closed, obsolete_field=True)
        ledger['closed_trades'] = [closed]
        cases.append(ledger)

        ledger = _ledger()
        closed = copy.deepcopy(valid_closed)
        closed['partial_closes'][0]['obsolete_field'] = True
        ledger['closed_trades'] = [closed]
        cases.append(ledger)

        for ledger in cases:
            with self.subTest(ledger=ledger):
                _cleaned, _report, blockers = migration.normalize_ledger(ledger)
                self.assertTrue(blockers)

    def test_archive_numeric_fields_are_normalized_or_fail_closed(self):
        valid = [{
            'symbol': 'BTCUSDT', 'side': 'long',
            'entry_price': '100', 'exit_price': '110',
            'position_size': '1', 'pnl': '9.9',
        }]

        cleaned, report, blockers = migration.normalize_archive(valid)

        self.assertFalse(blockers)
        self.assertEqual(100.0, cleaned[0]['entry_price'])
        self.assertEqual(9.9, cleaned[0]['pnl'])
        self.assertTrue(report)

        for bad in (None, 'not-a-number', 10 ** 10000):
            records = [{'symbol': 'BTCUSDT', 'pnl': bad}]
            with self.subTest(bad=bad):
                _cleaned, _report, blockers = migration.normalize_archive(records)
                self.assertTrue(blockers)

    def test_partial_close_metadata_types_fail_closed(self):
        base_partial = {
            'position_size': 0.5, 'exit_price': 105.0,
            'exit_notional': 52.5, 'gross_pnl': 2.5,
            'exit_fee': 0.01,
        }
        mutations = (
            {'exit_order_ids': 5},
            {'exit_order_ids': ['order-1', 2]},
            {'close_time': 'not-an-iso-time'},
            {'fee_source': {}},
            {'exit_fee_currency': []},
        )
        for mutation in mutations:
            ledger = _ledger()
            ledger['signal_states'] = {}
            partial = dict(base_partial, **mutation)
            ledger['open_positions']['BTCUSDT'] = {
                'symbol': 'BTCUSDT', 'side': 'long',
                'entry_price': 100.0, 'position_size': 0.5,
                'original_position_size': 1.0,
                'stop_loss_price': 90.0, 'strategy': 'ma_cross',
                'partial_closes': [partial],
            }
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    migration.TradeState.validate_state(ledger)
                _cleaned, _report, blockers = migration.normalize_ledger(ledger)
                self.assertTrue(blockers)

    def test_active_position_must_be_safe_to_transition_into_history(self):
        base_position = {
            'symbol': 'BTCUSDT', 'side': 'long',
            'entry_price': 100.0, 'position_size': 1.0,
            'stop_loss_price': 90.0, 'strategy': 'ma_cross',
        }
        bad_fields = (
            {'open_time': 5},
            {'last_stop_update': {}},
            {'last_partial_close': 'not-an-iso-time'},
            {'recovered_partial_rollback': 'yes'},
        )
        for bad in bad_fields:
            ledger = _ledger()
            ledger['signal_states'] = {}
            ledger['open_positions']['BTCUSDT'] = dict(
                base_position, **bad)
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    migration.TradeState.validate_state(ledger)
                self.assertTrue(migration.normalize_ledger(ledger)[2])

    def test_migration_blocks_conflicting_stop_identity_set(self):
        ledger = _ledger()
        ledger['signal_states'] = {}
        ledger['open_positions']['BTCUSDT'] = {
            'symbol': 'BTCUSDT', 'side': 'long',
            'entry_price': 100.0, 'position_size': 1.0,
            'stop_loss_price': 90.0, 'stop_order_id': 'stop-new',
            'stop_order_size': 1.0, 'extra_stop_order_ids': ['stop-new'],
            'stop_resize_pending': True, 'strategy': 'ma_cross',
        }

        with self.assertRaises(ValueError):
            migration.TradeState.validate_state(ledger)
        self.assertTrue(migration.normalize_ledger(ledger)[2])

    def test_active_position_partial_financials_must_be_consistent(self):
        base_position = {
            'symbol': 'BTCUSDT', 'side': 'long',
            'entry_price': 100.0, 'position_size': 1.0,
            'stop_loss_price': 90.0, 'strategy': 'ma_cross',
        }
        bad_partials = (
            {
                'position_size': 0.4, 'exit_price': 105.0,
                'exit_notional': 999.0, 'gross_pnl': 999.0,
                'exit_fee': 0.01,
            },
            {
                'position_size': 0.8, 'exit_price': 105.0,
                'exit_notional': 84.0, 'gross_pnl': 4.0,
                'exit_fee': 0.01,
            },
        )
        for partial in bad_partials:
            ledger = _ledger()
            ledger['signal_states'] = {}
            ledger['open_positions']['BTCUSDT'] = dict(
                base_position,
                position_size=0.6,
                original_position_size=1.0,
                partial_closes=[partial])
            with self.subTest(partial=partial):
                with self.assertRaises(ValueError):
                    migration.TradeState.validate_state(ledger)
                self.assertTrue(migration.normalize_ledger(ledger)[2])


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

    def test_dry_run_can_preview_reviewed_cleanup_without_writing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            config_path, state_path = self._paths(tmp)
            config = _read(config_path)
            config['obsolete_execution'] = {'confirmed': True}
            _write(config_path, config)
            originals = (_read(config_path), _read(state_path))
            spec_path = os.path.join(tmp, 'cleanup.spec.json')
            release_sha = '1' * 40

            self.assertEqual(migration.EXIT_UNSAFE, migration.run(tmp))
            cleanup.generate_spec(
                config_path,
                spec_path,
                release_sha,
                ('obsolete_execution',),
                'remove confirmed obsolete execution key',
            )

            self.assertEqual(
                migration.EXIT_OK,
                migration.run(
                    tmp,
                    cleanup_spec=spec_path,
                    release_sha=release_sha,
                ),
            )
            self.assertEqual(originals, (_read(config_path), _read(state_path)))
            self.assertFalse(any(
                '.premigrate.' in name for name in os.listdir(tmp)))

    def test_cleanup_preview_is_dry_run_only_and_requires_sha_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            config_path, _state_path = self._paths(tmp)
            spec_path = os.path.join(tmp, 'cleanup.spec.json')
            release_sha = '2' * 40
            cleanup.generate_spec(
                config_path,
                spec_path,
                release_sha,
                ('strategy', 'default_risk_per_trade'),
                'valid spec used only to test rejected argument combinations',
            )

            self.assertEqual(
                migration.EXIT_UNSAFE,
                migration.run(tmp, cleanup_spec=spec_path),
            )
            self.assertEqual(
                migration.EXIT_UNSAFE,
                migration.run(
                    tmp,
                    apply=True,
                    cleanup_spec=spec_path,
                    release_sha=release_sha,
                ),
            )

    def test_dry_run_rejects_explicit_null_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, state_path = self._paths(tmp)
            config = _read(config_path)
            config['scheduler'] = {'check_hour': None}
            _write(config_path, config)
            originals = (_read(config_path), _read(state_path))

            self.assertEqual(migration.EXIT_UNSAFE, migration.run(tmp))

            self.assertEqual(originals, (_read(config_path), _read(state_path)))
            self.assertFalse(any(
                '.premigrate.' in name for name in os.listdir(tmp)))

    def test_dry_run_blocks_unmarked_legacy_state_before_root_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, state_path = self._paths(tmp)
            legacy = os.path.join(tmp, 'data', 'okx')
            os.makedirs(legacy)
            _write(os.path.join(legacy, 'trade_state.json'), _ledger())
            originals = (_read(config_path), _read(state_path))

            self.assertEqual(migration.EXIT_UNSAFE, migration.run(tmp))

            self.assertEqual(originals, (_read(config_path), _read(state_path)))
            self.assertFalse(any(
                '.premigrate.' in name for name in os.listdir(tmp)))

    def test_legacy_completion_marker_has_a_strict_shared_schema(self):
        bad_markers = (
            {'exchange': 'okx'},
            {'exchange': 'okx', 'completed_at': '2026-07-18T08:00:00'},
            {'exchange': 'okx', 'moved': []},
            {'exchange': 'okx', 'obsolete': True},
            {'exchange': 'okx', 'completed_at': 123},
            {'exchange': 'okx', 'moved': ['trade_state.json', 2]},
        )
        for marker in bad_markers:
            with self.subTest(marker=marker), \
                    tempfile.TemporaryDirectory() as tmp:
                self._paths(tmp)
                os.makedirs(os.path.join(tmp, 'data', 'okx'))
                _write(os.path.join(
                    tmp, 'data', 'okx', 'legacy_state.json'), {'x': 1})
                _write(os.path.join(
                    tmp, '.okx_legacy_migration_complete.json'), marker)
                self.assertEqual(migration.EXIT_UNSAFE, migration.run(tmp))

        with tempfile.TemporaryDirectory() as tmp:
            self._paths(tmp)
            os.makedirs(os.path.join(tmp, 'data', 'okx'))
            _write(os.path.join(
                tmp, 'data', 'okx', 'legacy_state.json'), {'x': 1})
            _write(os.path.join(
                tmp, '.okx_legacy_migration_complete.json'), {
                    'exchange': 'okx',
                    'completed_at': '2026-07-18T08:00:00',
                    'moved': ['trade_state.json'],
                })
            self.assertEqual(migration.EXIT_OK, migration.run(tmp))

    def test_dry_run_rejects_unsafe_mode_without_chmod(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, _state_path = self._paths(tmp)
            os.chmod(config_path, 0o644)

            self.assertEqual(migration.EXIT_UNSAFE, migration.run(tmp))

            self.assertEqual(0o644, os.stat(config_path).st_mode & 0o777)

    def test_dry_run_rejects_unsupported_archive_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._paths(tmp)
            unsupported = os.path.join(
                tmp, 'closed_trades_archive_manual.json')
            _write(unsupported, [])

            self.assertEqual(migration.EXIT_UNSAFE, migration.run(tmp))

    def test_dry_run_validates_owner_manifest(self):
        bad_manifests = (
            {'exchange': 'other'},
            {'exchange': 'okx', 'obsolete': True},
            {'exchange': 'okx', 'claimed_at': 123},
            {'exchange': 'okx', 'claimed_at': 'not-a-time'},
        )
        for manifest in bad_manifests:
            with self.subTest(manifest=manifest), \
                    tempfile.TemporaryDirectory() as tmp:
                self._paths(tmp)
                _write(os.path.join(tmp, '.trading_data_owner.json'), manifest)
                self.assertEqual(migration.EXIT_UNSAFE, migration.run(tmp))

        with tempfile.TemporaryDirectory() as tmp:
            self._paths(tmp)
            _write(os.path.join(tmp, '.trading_data_owner.json'), {
                'exchange': 'okx',
                'claimed_at': '2026-07-18T08:00:00',
            })
            self.assertEqual(migration.EXIT_OK, migration.run(tmp))

    def test_unowned_lifecycle_and_auxiliary_state_block_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            _config_path, state_path = self._paths(tmp)
            ledger = _ledger()
            ledger.pop('exchange')
            ledger['last_daily_summary_date'] = '2026-07-17'
            _write(state_path, ledger)

            self.assertEqual(migration.EXIT_UNSAFE, migration.run(tmp))

        with tempfile.TemporaryDirectory() as tmp:
            _config_path, state_path = self._paths(tmp)
            ledger = _ledger()
            ledger.pop('exchange')
            _write(state_path, ledger)
            _write(os.path.join(tmp, 'peak_equity.json'), {
                'peak_equity': 100.0})

            self.assertEqual(migration.EXIT_UNSAFE, migration.run(tmp))

    def test_okx_owner_proves_lifecycle_for_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            _config_path, state_path = self._paths(tmp)
            ledger = _ledger()
            ledger.update({
                'exchange': 'okx',
                'last_daily_summary_date': '2026-07-17',
            })
            _write(state_path, ledger)

            self.assertEqual(migration.EXIT_OK, migration.run(tmp))

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
            _write(archive, [_closed_trade(
                'BTCUSDT', 'unsupported')])

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
            _write(archive, [_closed_trade(
                'BTCUSDT', 'unsupported')])

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

    def test_recover_only_rolls_back_without_continuing_migration(self):
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
            self.assertNotEqual(originals, (_read(config_path), _read(state_path)))

            self.assertEqual(
                migration.EXIT_OK, migration.run(tmp, recover_only=True))
            self.assertEqual(originals, (_read(config_path), _read(state_path)))
            self.assertTrue(
                'strategy' in _read(config_path)['trading']['symbols'][0])
            self.assertFalse(os.path.exists(journal))

            # Crash retries are harmless: after the durable rollback this mode
            # remains a no-op and still never starts a new migration.
            self.assertEqual(
                migration.EXIT_OK, migration.run(tmp, recover_only=True))
            self.assertEqual(originals, (_read(config_path), _read(state_path)))

    def test_recover_only_rejects_migration_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._paths(tmp)
            self.assertEqual(
                migration.EXIT_UNSAFE,
                migration.run(tmp, apply=True, recover_only=True))

    def test_migration_journal_version_requires_exact_json_integer(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, 'config.json')
            item = {
                'target': target,
                'backup': target + '.premigrate.0001',
            }
            for version in (True, 1.0):
                with self.subTest(version=version), self.assertRaisesRegex(
                        ValueError, '版本或结构无效'):
                    migration._validate_journal_items(
                        tmp, {'version': version, 'items': [item]})

    def test_classify_state_distinguishes_complete_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, state_path = self._paths(tmp)
            originals = (_read(config_path), _read(state_path))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = migration.run(tmp, classify_state=True)
            self.assertEqual(migration.EXIT_OK, result)
            self.assertEqual('requires_migration\n', output.getvalue())
            self.assertEqual(originals, (_read(config_path), _read(state_path)))

            self.assertEqual(migration.EXIT_OK, migration.run(tmp, apply=True))
            migrated = (_read(config_path), _read(state_path))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = migration.run(tmp, classify_state=True)
            self.assertEqual(migration.EXIT_OK, result)
            self.assertEqual('migration_complete\n', output.getvalue())
            self.assertEqual(migrated, (_read(config_path), _read(state_path)))

    def test_classify_promotes_commit_before_phase_write_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, state_path = self._paths(tmp)
            real_remove = migration._remove_journal

            def die_after_transaction_commit(path):
                real_remove(path)
                raise SystemExit(99)

            with patch.object(
                    migration, '_remove_journal',
                    side_effect=die_after_transaction_commit):
                with self.assertRaises(SystemExit):
                    migration.run(tmp, apply=True)

            journal = os.path.join(
                tmp, migration.cfgv.SINGLE_STRATEGY_MIGRATION_JOURNAL)
            self.assertFalse(os.path.exists(journal))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = migration.run(tmp, classify_state=True)
            self.assertEqual(migration.EXIT_OK, result)
            self.assertEqual('migration_complete\n', output.getvalue())
            self.assertNotIn(
                'strategy', _read(config_path)['trading']['symbols'][0])
            self.assertEqual(
                {'last_processed_candle', 'last_update'},
                set(_read(state_path)['signal_states']['BTCUSDT']))

    def test_classify_state_never_relabels_blocked_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            _config_path, state_path = self._paths(tmp)
            state = _read(state_path)
            state['open_intents'] = {'BTCUSDT': {'status': 'pending'}}
            _write(state_path, state)
            self.assertEqual(
                migration.EXIT_UNSAFE,
                migration.run(tmp, classify_state=True))

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
