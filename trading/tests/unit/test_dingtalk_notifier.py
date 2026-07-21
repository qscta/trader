import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import Mock, patch


# 单元套件承诺只依赖标准库；用最小 requests 桩从源文件单独加载通知器。
_requests_stub = types.ModuleType('requests')
_requests_stub.post = Mock()
_module_path = pathlib.Path(__file__).resolve().parents[2] / 'dingtalk_notifier.py'
_spec = importlib.util.spec_from_file_location('_dingtalk_notifier_unit_under_test', _module_path)
_module = importlib.util.module_from_spec(_spec)
with patch.dict(sys.modules, {'requests': _requests_stub}):
    _spec.loader.exec_module(_module)
DingTalkNotifier = _module.DingTalkNotifier


class DingTalkDeliveryEvidenceTest(unittest.TestCase):
    def _send_with_response(self, response):
        notifier = DingTalkNotifier('https://example.invalid/robot/send?access_token=secret')
        notifier.SEND_RETRY_DELAY_SECONDS = 0
        with patch.object(_module.requests, 'post', return_value=response) as post:
            result = notifier.send_message('系统警告', 'test')
        return result, post.call_count

    def test_explicit_zero_errcode_is_success(self):
        response = Mock(status_code=200, text='{"errcode":0}')
        response.json.return_value = {'errcode': 0, 'errmsg': 'ok'}

        result, calls = self._send_with_response(response)

        self.assertTrue(result)
        self.assertEqual(1, calls)

    def test_http_200_without_errcode_is_not_success(self):
        response = Mock(status_code=200, text='{}')
        response.json.return_value = {}

        result, calls = self._send_with_response(response)

        self.assertFalse(result)
        self.assertEqual(2, calls)

    def test_http_200_non_json_body_is_not_success(self):
        response = Mock(status_code=200, text='<html>proxy error</html>')
        response.json.side_effect = ValueError('not json')

        result, calls = self._send_with_response(response)

        self.assertFalse(result)
        self.assertEqual(2, calls)


if __name__ == '__main__':
    unittest.main()
