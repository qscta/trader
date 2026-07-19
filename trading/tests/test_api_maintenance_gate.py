"""Flask integration checks for the deployment maintenance gate."""

import importlib
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
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
        cls.api_token = mock.patch.object(
            cls.api, 'API_TOKEN', 'test-token-not-production')
        cls.api_token.start()

    @classmethod
    def tearDownClass(cls):
        cls.api_token.stop()
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

    def test_old_runner_handshake_arms_under_trade_lock_then_blocks_http(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = os.path.realpath(directory)
            sentinel = Path(directory) / '.maintenance_no_open'
            lock = threading.Lock()
            lock_observations = []

            def engine_gate():
                if sentinel.exists():
                    lock_observations.append(lock.locked())
                    return {
                        'status': 'maintenance_blocked',
                        'sentinel_path': str(sentinel),
                    }
                return None

            system = SimpleNamespace(
                base_dir=directory,
                _trade_lock=lock,
                _maintenance_open_gate_status=engine_gate,
            )
            with mock.patch.object(self.api, 'trading_system', system), \
                    mock.patch.dict(os.environ, {
                        'TRADING_MAINTENANCE_SENTINEL': str(sentinel),
                    }):
                client = self.api.app.test_client()
                headers = {'X-API-Token': 'test-token-not-production'}
                capability = client.get(
                    '/api/deployment/no-open-capability', headers=headers)
                self.assertEqual(200, capability.status_code)
                self.assertEqual(
                    'trade-lock-no-open-v1',
                    capability.get_json()['protocol'])

                armed = client.post(
                    '/api/deployment/arm-no-open', headers=headers,
                    json={'release_sha': 'a' * 40, 'nonce': 'b' * 64})

                self.assertEqual(200, armed.status_code)
                self.assertEqual(
                    'maintenance_blocked', armed.get_json()['status'])
                self.assertTrue(
                    armed.get_json()['inflight_open_boundary_drained'])
                self.assertTrue(all(lock_observations))
                self.assertEqual(
                    0o600, stat.S_IMODE(sentinel.stat().st_mode))
                blocked = client.post('/api/instant_open', json={})
                self.assertEqual(503, blocked.status_code)

    def test_handshake_rejects_group_writable_sentinel_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = os.path.realpath(directory)
            os.chmod(directory, 0o770)
            sentinel = Path(directory) / '.maintenance_no_open'
            system = SimpleNamespace(
                base_dir=directory,
                _trade_lock=threading.Lock(),
                _maintenance_open_gate_status=lambda: None,
            )
            with mock.patch.object(self.api, 'trading_system', system), \
                    mock.patch.dict(os.environ, {
                        'TRADING_MAINTENANCE_SENTINEL': str(sentinel),
                    }):
                response = self.api.app.test_client().post(
                    '/api/deployment/arm-no-open',
                    headers={'X-API-Token': 'test-token-not-production'},
                    json={'release_sha': 'a' * 40, 'nonce': 'b' * 64})

            self.assertEqual(503, response.status_code)
            self.assertFalse(os.path.lexists(sentinel))


if __name__ == '__main__':
    unittest.main()
