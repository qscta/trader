"""Tests for the tracked, hash-bound one-key deployment cleanup."""

import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
TOOL = ROOT / 'remove-one-confirmed-config-key.py'
SHA = 'a' * 40


def load_tool():
    spec = importlib.util.spec_from_file_location('config_cleanup_tool', TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigCleanupToolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def _directory(self):
        temporary = tempfile.TemporaryDirectory()
        return temporary, Path(os.path.realpath(temporary.name))

    @staticmethod
    def _config(path):
        path.write_text(json.dumps({
            'strategy': {
                'ma_short_period': 7,
                'confirmed_obsolete_key': {'nested': [1, 2, 3]},
            },
            'trading': {'symbols': []},
        }, ensure_ascii=False, indent=4), encoding='utf-8')
        os.chmod(path, 0o600)

    def test_generate_check_apply_verify_is_exact_and_preserves_owner(self):
        temporary, directory = self._directory()
        with temporary:
            config = directory / 'config.json'
            spec = directory / 'cleanup.spec.json'
            audit = directory / 'cleanup.audit.json'
            self._config(config)
            before = config.stat()

            generated = self.tool.generate_spec(
                str(config), str(spec), SHA,
                ('strategy', 'confirmed_obsolete_key'),
                'confirmed obsolete configuration field')
            self.assertEqual('generated', generated['status'])
            self.assertEqual(0o600, stat.S_IMODE(spec.stat().st_mode))
            self.assertEqual(
                'ready',
                self.tool.check(str(config), str(spec), SHA)['status'])

            result = self.tool.apply(
                str(config), str(spec), str(audit), SHA)
            self.assertEqual('applied', result['status'])
            after = config.stat()
            self.assertEqual(before.st_uid, after.st_uid)
            self.assertEqual(before.st_gid, after.st_gid)
            self.assertEqual(0o600, stat.S_IMODE(after.st_mode))
            self.assertNotIn(
                'confirmed_obsolete_key',
                json.loads(config.read_text(encoding='utf-8'))['strategy'])
            self.assertEqual(
                'verified',
                self.tool.verify_applied(
                    str(config), str(spec), str(audit), SHA)['status'])

    def test_any_preimage_or_spec_change_fails_closed(self):
        temporary, directory = self._directory()
        with temporary:
            config = directory / 'config.json'
            spec = directory / 'cleanup.spec.json'
            self._config(config)
            self.tool.generate_spec(
                str(config), str(spec), SHA,
                ('strategy', 'confirmed_obsolete_key'), 'reviewed removal')
            config.write_text(
                config.read_text(encoding='utf-8') + '\n', encoding='utf-8')
            os.chmod(config, 0o600)
            with self.assertRaisesRegex(
                    self.tool.CleanupError, 'before_sha256'):
                self.tool.check(str(config), str(spec), SHA)

            config.unlink()
            self._config(config)
            payload = json.loads(spec.read_text(encoding='utf-8'))
            payload['path'] = ['strategy', 'different_key']
            spec.write_text(json.dumps(payload), encoding='utf-8')
            os.chmod(spec, 0o600)
            with self.assertRaises(self.tool.CleanupError):
                self.tool.check(str(config), str(spec), SHA)

    def test_spec_schema_version_requires_an_exact_json_integer(self):
        temporary, directory = self._directory()
        with temporary:
            config = directory / 'config.json'
            spec = directory / 'cleanup.spec.json'
            self._config(config)
            self.tool.generate_spec(
                str(config), str(spec), SHA,
                ('strategy', 'confirmed_obsolete_key'), 'reviewed removal')
            original = json.loads(spec.read_text(encoding='utf-8'))
            for version in (True, 1.0):
                with self.subTest(version=version):
                    payload = dict(original, schema_version=version)
                    spec.write_text(json.dumps(payload), encoding='utf-8')
                    os.chmod(spec, 0o600)
                    with self.assertRaisesRegex(
                            self.tool.CleanupError, '版本不兼容'):
                        self.tool.check(str(config), str(spec), SHA)

    def test_retry_repairs_exact_after_state_when_audit_write_was_interrupted(self):
        temporary, directory = self._directory()
        with temporary:
            config = directory / 'config.json'
            spec = directory / 'cleanup.spec.json'
            audit = directory / 'cleanup.audit.json'
            self._config(config)
            self.tool.generate_spec(
                str(config), str(spec), SHA,
                ('strategy', 'confirmed_obsolete_key'), 'reviewed removal')

            with patch.object(
                    self.tool, '_write_exclusive',
                    side_effect=self.tool.CleanupError('interrupted')):
                with self.assertRaisesRegex(
                        self.tool.CleanupError, 'interrupted'):
                    self.tool.apply(
                        str(config), str(spec), str(audit), SHA)

            self.assertFalse(audit.exists())
            self.assertNotIn(
                'confirmed_obsolete_key',
                json.loads(config.read_text(encoding='utf-8'))['strategy'])
            recovered = self.tool.apply(
                str(config), str(spec), str(audit), SHA)
            self.assertEqual('recovered_applied', recovered['status'])
            self.assertEqual(
                'already_applied',
                self.tool.check(str(config), str(spec), SHA)['status'])
            preview = self.tool.preview_config(str(config), str(spec), SHA)
            self.assertNotIn('confirmed_obsolete_key', preview['strategy'])
            already = self.tool.apply(
                str(config), str(spec), str(audit), SHA)
            self.assertEqual('already_applied', already['status'])
            self.assertEqual(
                'verified', self.tool.verify_applied(
                    str(config), str(spec), str(audit), SHA)['status'])

    def test_duplicate_json_and_non_private_files_are_rejected(self):
        temporary, directory = self._directory()
        with temporary:
            config = directory / 'config.json'
            spec = directory / 'cleanup.spec.json'
            config.write_text('{"strategy":{},"strategy":{}}', encoding='utf-8')
            os.chmod(config, 0o600)
            with self.assertRaisesRegex(self.tool.CleanupError, '重复字段'):
                self.tool.generate_spec(
                    str(config), str(spec), SHA,
                    ('strategy', 'confirmed_obsolete_key'), 'reviewed removal')

            config.write_text('{}', encoding='utf-8')
            os.chmod(config, 0o644)
            with self.assertRaisesRegex(self.tool.CleanupError, '权限'):
                self.tool.generate_spec(
                    str(config), str(spec), SHA,
                    ('strategy', 'confirmed_obsolete_key'), 'reviewed removal')


if __name__ == '__main__':
    unittest.main()
