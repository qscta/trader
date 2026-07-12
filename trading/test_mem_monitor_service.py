"""资源监控启动失败语义回归。"""

import sys
import types
import unittest
from unittest.mock import patch

# stdlib-only CI job intentionally does not install requests.  These tests never
# perform HTTP; provide only the import-time shape needed by mem_monitor.
try:
    import requests  # noqa: F401
except ImportError:
    requests_stub = types.ModuleType('requests')
    requests_stub.post = None
    sys.modules['requests'] = requests_stub

import mem_monitor


class ResourceMonitorStartupTest(unittest.TestCase):
    def test_log_override_requires_absolute_path(self):
        self.assertTrue(mem_monitor.os.path.isabs(mem_monitor.LOG_FILE))

    def test_service_mode_missing_webhook_fails_loudly(self):
        with patch.object(mem_monitor.sys, 'argv', ['mem_monitor.py']), \
                patch.object(mem_monitor, 'load_webhook', return_value=None):
            self.assertEqual(mem_monitor.main(), 1)

    def test_test_mode_missing_webhook_is_nonzero(self):
        with patch.object(mem_monitor.sys, 'argv', ['mem_monitor.py', '--test']), \
                patch.object(mem_monitor, 'load_webhook', return_value=None):
            self.assertEqual(mem_monitor.main(), 1)

    def test_test_mode_propagates_delivery_failure(self):
        with patch.object(mem_monitor.sys, 'argv', ['mem_monitor.py', '--test']), \
                patch.object(mem_monitor, 'load_webhook', return_value='https://example.invalid'), \
                patch.object(mem_monitor, 'send_test_message', return_value=False):
            self.assertEqual(mem_monitor.main(), 1)

    def test_dingtalk_requires_explicit_success(self):
        accepted = type('Response', (), {
            'status_code': 200,
            'json': lambda self: {'errcode': 0},
        })()
        rejected = type('Response', (), {
            'status_code': 200,
            'json': lambda self: {'errcode': 310000, 'errmsg': 'rejected'},
        })()
        with patch.object(mem_monitor.requests, 'post', return_value=accepted):
            self.assertTrue(mem_monitor.send_dingtalk('https://example.invalid', 'ok'))
        with patch.object(mem_monitor.requests, 'post', return_value=rejected):
            self.assertFalse(mem_monitor.send_dingtalk('https://example.invalid', 'no'))

    def test_config_webhook_must_be_a_string(self):
        with patch.dict(mem_monitor.os.environ, {}, clear=True), \
                patch.object(mem_monitor, 'CONFIG_FILE', 'unused'), \
                patch('builtins.open'), \
                patch.object(mem_monitor.json, 'load', return_value={
                    'dingtalk': {'webhook_url': 12345},
                }):
            self.assertIsNone(mem_monitor.load_webhook())

    def test_top_processes_never_reads_or_returns_command_arguments(self):
        fake_result = type('Result', (), {
            'stdout': '/usr/bin/cloudflared 1.9 0.1\n/usr/bin/gunicorn 8.2 3.4\n',
        })()
        with patch.object(mem_monitor.subprocess, 'run', return_value=fake_result) as run:
            processes = mem_monitor.get_top_processes(2)
        self.assertEqual([p['cmd'] for p in processes], ['cloudflared', 'gunicorn'])
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ['/usr/bin/ps', '-eo', 'comm=,%mem=,%cpu='])
        self.assertNotIn('aux', argv)


if __name__ == '__main__':
    unittest.main()
