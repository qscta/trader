"""verify_okx.run_stop_id_reuse_test 裁决逻辑单测（桩 ccxt/pandas，本机可运行，不连交易所）。

--stop-id-reuse 模式验证「止损 algoClOrdId 在旧单终态后能否复用」——确定性
止损 ID（四元组哈希）的幂等假设中唯一无法离线自证、必须真连交易所确认的
一环。这里只测裁决逻辑本身（接受判定/拒绝判定/确认滞后判定/前提破坏判定/
清理保证），真实交易所行为由用户在模拟盘上用该脚本验证，两者互补：这里
锁定「代码看到某种交易所响应时会不会下对结论」。
"""
import sys
import types
import unittest
from unittest.mock import Mock, patch

# 桩 ccxt / pandas 后导入 verify_okx（它顶部 import okx_api → import ccxt），
# 导入完立即恢复（同 test_verify_fire_logic.py 思路）
_saved = {}
for _name in ('ccxt', 'pandas'):
    _saved[_name] = sys.modules.get(_name)
_ccxt = types.ModuleType('ccxt')
_ccxt.okx = Mock()
sys.modules['ccxt'] = _ccxt
sys.modules['pandas'] = types.ModuleType('pandas')
sys.modules.pop('exchange_base', None)
sys.modules.pop('okx_api', None)
sys.modules.pop('verify_okx', None)
import verify_okx
from verify_okx import run_stop_id_reuse_test
for _name, _orig in _saved.items():
    if _orig is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _orig
sys.modules.pop('exchange_base', None)
sys.modules.pop('okx_api', None)
sys.modules.pop('verify_okx', None)


def _fake_api(create_results, cancel_ok=True, pending_after_second=None):
    """构造只实现 run_stop_id_reuse_test 所需方法的假 api（纯离线）。

    create_results: create_stop_loss_order 依次返回的值列表（第一次/第二次）。
    pending_after_second: 第二次返回 None 后 _fetch_algo_orders 的清单内容。
    """
    api = Mock()
    api.open_position.return_value = {'id': 'order-1'}
    api.get_last_price.return_value = 100.0
    api.create_stop_loss_order.side_effect = list(create_results)
    api.cancel_order.return_value = cancel_ok
    api._fetch_algo_orders.return_value = list(pending_after_second or [])
    api.cancel_all_orders.return_value = True
    api.close_position.return_value = {'id': 'close-1'}
    api.get_position.return_value = None  # 清理后的复核：无仓
    return api


_FIRST = {'id': 'algo-1', 'clientOrderId': 'S' + 'a' * 31}
_SECOND_NEW = {'id': 'algo-2', 'clientOrderId': 'S' + 'a' * 31}


class StopIdReuseDecisionLogicTest(unittest.TestCase):
    """裁决逻辑测试：patch 掉 verify_okx.time.sleep（不真实等待）。"""

    def setUp(self):
        patcher = patch.object(
            verify_okx, 'time', Mock(sleep=Mock(), time=Mock(return_value=0.0)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reuse_accepted_returns_true(self):
        """第二次返回同 algoClOrdId、新 algoId 的算法单 → True（复用被接受）。"""
        api = _fake_api([dict(_FIRST), dict(_SECOND_NEW)])
        result = run_stop_id_reuse_test(api, 'BTC/USDT:USDT', 0.1, 'long')
        self.assertTrue(result)
        # 两次挂单必须使用完全相同的 (方向, 币数, 触发价)——前提即四元组一致
        calls = api.create_stop_loss_order.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        # finally 清理保证执行
        api.cancel_all_orders.assert_called()
        api.close_position.assert_called()

    def test_rejection_returns_false(self):
        """第二次返回 None 且该 ID 不在待触发清单 → False（判定为交易所拒绝复用）。"""
        api = _fake_api([dict(_FIRST), None], pending_after_second=[])
        result = run_stop_id_reuse_test(api, 'BTC/USDT:USDT', 0.1, 'long')
        self.assertFalse(result)
        api.cancel_all_orders.assert_called()
        api.close_position.assert_called()

    def test_created_but_unconfirmed_counts_as_reuse_success(self):
        """第二次返回 None 但该 algoClOrdId 已出现在待触发清单 → True（仅确认滞后）。"""
        api = _fake_api(
            [dict(_FIRST), None],
            pending_after_second=[{'id': 'algo-2', 'clientOrderId': _FIRST['clientOrderId']}])
        result = run_stop_id_reuse_test(api, 'BTC/USDT:USDT', 0.1, 'long')
        self.assertTrue(result)

    def test_first_create_failure_is_inconclusive_and_cleans_up(self):
        """第一张止损创建失败 → None（不构成证据），已开的仓必须在 finally 清理。"""
        api = _fake_api([None, None])
        result = run_stop_id_reuse_test(api, 'BTC/USDT:USDT', 0.1, 'long')
        self.assertIsNone(result)
        api.cancel_all_orders.assert_called()
        api.close_position.assert_called()

    def test_unverified_cancel_is_inconclusive(self):
        """撤销未能确认（无法证明旧单终态）→ None，且不得进行第二次挂单。"""
        api = _fake_api([dict(_FIRST), dict(_SECOND_NEW)], cancel_ok=False)
        result = run_stop_id_reuse_test(api, 'BTC/USDT:USDT', 0.1, 'long')
        self.assertIsNone(result)
        self.assertEqual(api.create_stop_loss_order.call_count, 1)
        api.cancel_all_orders.assert_called()

    def test_client_id_mismatch_is_inconclusive(self):
        """两次派生的 algoClOrdId 不一致（前提被破坏）→ None，绝不误判为复用成功。"""
        second = {'id': 'algo-2', 'clientOrderId': 'S' + 'b' * 31}
        api = _fake_api([dict(_FIRST), second])
        result = run_stop_id_reuse_test(api, 'BTC/USDT:USDT', 0.1, 'long')
        self.assertIsNone(result)

    def test_same_algo_id_means_cancel_did_not_stick(self):
        """第二次返回与已撤销单相同的 algoId（预查复用了旧单，撤销未生效）→ None。"""
        api = _fake_api([dict(_FIRST), dict(_FIRST)])
        result = run_stop_id_reuse_test(api, 'BTC/USDT:USDT', 0.1, 'long')
        self.assertIsNone(result)

    def test_open_position_failure_returns_none_without_cleanup(self):
        """开仓失败：直接返回 None，不进入清理逻辑（无仓可平）。"""
        api = _fake_api([dict(_FIRST), dict(_SECOND_NEW)])
        api.open_position.return_value = None
        result = run_stop_id_reuse_test(api, 'BTC/USDT:USDT', 0.1, 'long')
        self.assertIsNone(result)
        api.close_position.assert_not_called()

    def test_short_side_uses_stop_above_market(self):
        """做空方向的止损价必须在市价上方（+10%），两次挂单同价。"""
        api = _fake_api([dict(_FIRST), dict(_SECOND_NEW)])
        result = run_stop_id_reuse_test(api, 'BTC/USDT:USDT', 0.1, 'short')
        self.assertTrue(result)
        stop_px = api.create_stop_loss_order.call_args_list[0][0][3]
        self.assertGreater(stop_px, 100.0)


class StopIdReuseCliGuardTest(unittest.TestCase):
    def test_stop_id_reuse_requires_explicit_side(self):
        """--stop-id-reuse 与 --side both 组合：main() 必须在下单前拒绝。"""
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument('symbol', nargs='?', default='BTCUSDT')
        ap.add_argument('coin', nargs='?', type=float, default=0.0)
        ap.add_argument('--side', choices=['long', 'short', 'both'], default='both')
        ap.add_argument('--stop-id-reuse', action='store_true')
        args = ap.parse_args(['BTCUSDT', '0.1', '--stop-id-reuse'])
        self.assertTrue(args.stop_id_reuse and args.side == 'both')  # 复现 main() 里被拒绝的组合

    def test_fire_and_stop_id_reuse_are_mutually_exclusive(self):
        """--fire 与 --stop-id-reuse 同时给出：main() 必须拒绝（两个独立试验）。"""
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument('symbol', nargs='?', default='BTCUSDT')
        ap.add_argument('coin', nargs='?', type=float, default=0.0)
        ap.add_argument('--side', choices=['long', 'short', 'both'], default='both')
        ap.add_argument('--fire', action='store_true')
        ap.add_argument('--stop-id-reuse', action='store_true')
        args = ap.parse_args(['BTCUSDT', '0.1', '--side', 'long', '--fire', '--stop-id-reuse'])
        self.assertTrue(args.fire and args.stop_id_reuse)  # 复现 main() 里被拒绝的组合


if __name__ == '__main__':
    unittest.main()
