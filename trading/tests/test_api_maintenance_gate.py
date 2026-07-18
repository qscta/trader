"""Flask integration checks for the deployment maintenance gate."""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


class ApiMaintenanceGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = mock.patch.dict(os.environ, {
            'FLASK_SECRET_KEY': 'm' * 48,
            'TRADING_API_TOKEN': 'test-token-not-production',
        })
        cls.env.start()
        cls.api = importlib.import_module('api_server')

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def test_path_helper_fails_closed_and_absence_is_open(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / '.maintenance_no_open'
            env = {'TRADING_MAINTENANCE_SENTINEL': str(sentinel)}
            self.assertFalse(
                self.api._maintenance_no_open_active(env, directory))
            sentinel.write_text('{}\n', encoding='utf-8')
            self.assertTrue(
                self.api._maintenance_no_open_active(env, directory))
            sentinel.unlink()
            sentinel.symlink_to(Path(directory) / 'missing')
            self.assertTrue(
                self.api._maintenance_no_open_active(env, directory))
            self.assertTrue(self.api._maintenance_no_open_active(
                {'TRADING_MAINTENANCE_SENTINEL': 'relative'}, directory))

    def test_maintenance_blocks_all_writes_but_keeps_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / '.maintenance_no_open'
            sentinel.write_text('{}\n', encoding='utf-8')
            with mock.patch.dict(os.environ, {
                    'TRADING_MAINTENANCE_SENTINEL': str(sentinel)}):
                client = self.api.app.test_client()
                blocked = client.post('/api/instant_open', json={})
                self.assertEqual(503, blocked.status_code)
                self.assertTrue(
                    blocked.get_json()['maintenance_no_open'])
                self.assertNotEqual(
                    503, client.get('/api/status').status_code)
                self.assertEqual(
                    503,
                    client.post('/api/close_position', json={}).status_code,
                )


if __name__ == '__main__':
    unittest.main()
