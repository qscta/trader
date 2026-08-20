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

    def test_safe_defer_message_says_no_order_and_no_manual_action(self):
        notifier = DingTalkNotifier('https://example.invalid/robot/send?access_token=secret')
        signal = {
            'current_close': 205.5,
            'ema_short': 198.31500934090957,
            'ema_long': 198.44727891198724,
        }
        with patch.object(notifier, 'send_message', return_value=True) as send:
            result = notifier.notify_open_safely_deferred(
                'TAOUSDT', '双均线 EMA', 'short',
                '空单止损价(203.7)必须高于入场参考价(205.5)，当前风险结构不成立',
                205.5, 203.7, signal=signal,
            )

        self.assertTrue(result)
        title, content = send.call_args.args
        self.assertEqual('[交易系统] 开仓安全暂缓 - TAOUSDT', title)
        self.assertIn('### ⏸️ 开仓安全暂缓 - TAOUSDT', content)
        self.assertIn('当前方向: 做空', content)
        self.assertIn('EMA短/长: 198.31500934090957 / 198.44727891198724', content)
        self.assertIn('信号参考价: 205.5', content)
        self.assertIn('固定止损价: 203.7', content)
        self.assertIn('未向 OKX 发送开仓订单', content)
        self.assertIn('无新增仓位、无新增挂单', content)
        self.assertIn('无需人工操作', content)


if __name__ == '__main__':
    unittest.main()
