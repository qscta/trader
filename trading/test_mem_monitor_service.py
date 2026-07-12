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


if __name__ == '__main__':
    unittest.main()
