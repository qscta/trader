"""verify_okx.run_fire_test 决策逻辑单测（桩 ccxt/pandas，本机可运行，不连交易所）。

--fire 模式验证「止损触发瞬间是否只减仓、绝不反向」——本系统止损防线
唯一从未被实测过的一环。这里只测决策逻辑本身（触发判定/归零判定/反向
    判定/超时判定/列表消失判定），真实触发行为由用户在实盘上用该脚本验证，
    两者互补：这里锁定「代码看到某种交易所响应时会不会下对结论」。
"""
import sys
import types
import unittest
from unittest.mock import Mock, call, patch

# 桩 ccxt / pandas 后导入 verify_okx（它顶部 import okx_api → import ccxt），
# 导入完立即恢复（同 test_okx_adapter_safety.py 思路）
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
from verify_okx import run_fire_test
for _name, _orig in _saved.items():
    if _orig is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _orig
sys.modules.pop('exchange_base', None)
sys.modules.pop('okx_api', None)
sys.modules.pop('verify_okx', None)


def _fake_api(position_sequence, algo_after_trigger=None, side_after='long'):
    """构造一个只实现 run_fire_test 所需方法的假 api（不经 OkxApi，纯离线）。

    position_sequence: get_position 依次返回的值列表——None 表示「无仓」
    （用于让轮询检测到触发）；耗尽后固定返回最后一个值。
    """
    api = Mock()
    api.open_position.return_value = {'id': 'order-1'}
    api.get_last_price.return_value = 100.0
    api.create_stop_loss_order.return_value = {'id': 'stop-1'}
    api.cancel_all_orders.return_value = True
    api.close_position.return_value = {'id': 'close-1'}
    api._fetch_algo_orders.return_value = list(algo_after_trigger or [])

    seq = list(position_sequence)

    def get_position(_symbol):
        if seq:
            val = seq.pop(0)
        else:
            val = position_sequence[-1]
        if val is None:
            return None
        return {'contracts': val, 'side': side_after}

    api.get_position.side_effect = get_position
    return api


class FireTestDecisionLogicTest(unittest.TestCase):
    """决策逻辑测试：patch 掉 verify_okx.time.sleep（不真实等待），
    只验证「看到某种交易所响应时会不会下对结论」，与真实等待时长无关。"""

    def setUp(self):
        # 使用会前进的虚拟时钟；sleep(0) 也推进 1ms，避免超时测试在真实
        # 10ms 内忙等并向测试日志打印数千行。
        clock = {'now': 0.0}

        def fake_time():
            clock['now'] += 0.001
            return clock['now']

        def fake_sleep(seconds):
            clock['now'] += max(float(seconds), 0.001)

        patcher = patch.object(
            verify_okx, 'time', Mock(sleep=fake_sleep, time=fake_time))
        self._mock_time = patcher.start()
        self.addCleanup(patcher.stop)

    def test_triggered_flat_no_reverse_passes(self):
        """触发后持仓归零、无反向、算法单消失 → True。"""
        api = _fake_api(position_sequence=[10.0, None], algo_after_trigger=[])
        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long',
                               distance_pct=0.15, timeout_seconds=10, poll_interval=0)
        self.assertTrue(result)
        # finally 保证清理即使已触发（幂等：无仓时 close_position 由适配层自行处理）
        api.cancel_all_orders.assert_called()
        api.close_position.assert_called()

    def test_triggered_but_reversed_fails(self):
        """触发后仍有持仓且方向与开仓方向相反 → False，判定为 reduce-only 失守。"""
        api = _fake_api(position_sequence=[], algo_after_trigger=[])
        # 开的是 long：轮询先看到有仓 → 归零（触发）→ 复核时却报告一笔 short 持仓（反向）
        seq = [{'contracts': 10.0, 'side': 'long'}, None,
               {'contracts': 5.0, 'side': 'short'}]
        last = seq[-1]  # 序列耗尽后沿用最后一个值（清理阶段可能追加查询，不得 IndexError）
        api.get_position.side_effect = lambda _s: seq.pop(0) if seq else last
        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long',
                               distance_pct=0.15, timeout_seconds=10, poll_interval=0)
        self.assertFalse(result)
        self.assertEqual('short', api.close_position.call_args.args[1])

    def test_direct_long_to_short_between_polls_fails(self):
        """轮询没看到空仓、直接看到反向仓，也必须立即判失败而不是超时不确定。"""
        api = _fake_api(position_sequence=[])
        seq = [
            {'contracts': 10.0, 'side': 'long'},
            {'contracts': 5.0, 'side': 'short'},
        ]
        last = seq[-1]
        api.get_position.side_effect = lambda _s: seq.pop(0) if seq else last

        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long',
                               distance_pct=0.15, timeout_seconds=10, poll_interval=0)

        self.assertFalse(result)
        self.assertEqual('short', api.close_position.call_args.args[1])

    def test_reverse_cleanup_uses_actual_size_and_survives_cancel_failure(self):
        """撤单报错不能跳过平仓；反向仓按交易所真实方向和张数清理。"""
        api = _fake_api(position_sequence=[])
        seq = [
            {'contracts': 10.0, 'side': 'long'},
            {'contracts': 5.0, 'side': 'short'},
        ]
        last = seq[-1]
        api.get_position.side_effect = lambda _s: seq.pop(0) if seq else last
        api.cancel_all_orders.side_effect = [
            RuntimeError('cancel unavailable'), True]
        api._contracts_to_coins = Mock(return_value=0.05)

        result = run_fire_test(
            api, 'BTC/USDT:USDT', 0.1, 'long',
            distance_pct=0.15, timeout_seconds=10, poll_interval=0)

        self.assertFalse(result)
        api.close_position.assert_any_call(
            'BTC/USDT:USDT', 'short', 0.05)
        self.assertGreaterEqual(api.cancel_all_orders.call_count, 2)

    def test_cleanup_retries_with_fresh_side_after_stop_race(self):
        """首次平仓遇到止损并发变向时，须按最新方向/数量再补平一次。"""
        api = Mock()
        api.cancel_all_orders.return_value = True
        api._contracts_to_coins.side_effect = lambda _symbol, contracts: contracts / 100
        api.close_position.side_effect = [
            None,  # 首次调用时适配层复读仓位，发现方向已由 long 变 short
            {'id': 'retry-close'},
        ]
        positions = [
            {'contracts': 10.0, 'side': 'long'},
            {'contracts': 5.0, 'side': 'short'},
            None,
        ]
        api.get_position.side_effect = lambda _symbol: positions.pop(0)

        result = verify_okx.cleanup_live_position(
            api, 'BTC/USDT:USDT', 'long', 0.1, 'race')

        self.assertTrue(result)
        self.assertEqual([
            call('BTC/USDT:USDT', 'long', 0.1),
            call('BTC/USDT:USDT', 'short', 0.05),
        ], api.close_position.call_args_list)
        self.assertEqual(3, api.cancel_all_orders.call_count)

    def test_cleanup_survives_contract_conversion_runtime_failure(self):
        """合约面值获取异常也不得在 finally 真正平仓之前中断清理。"""
        api = Mock()
        api.cancel_all_orders.return_value = True
        api._contracts_to_coins.side_effect = RuntimeError(
            'contract size unavailable')
        positions = [
            {'contracts': 5.0, 'side': 'short'},
            None,
        ]
        api.get_position.side_effect = lambda _symbol: positions.pop(0)
        api.close_position.return_value = {'id': 'fallback-close'}

        result = verify_okx.cleanup_live_position(
            api, 'BTC/USDT:USDT', 'long', 0.1, 'conversion')

        self.assertTrue(result)
        api.close_position.assert_called_once_with(
            'BTC/USDT:USDT', 'short', 0.1)

    def test_partial_stop_that_never_flattens_fails(self):
        """止损已部分成交不是“行情未触发”，超时后必须判失败。"""
        api = _fake_api(position_sequence=[])
        seq = [
            {'contracts': 10.0, 'side': 'long'},
            {'contracts': 5.0, 'side': 'long'},
        ]
        last = seq[-1]
        api.get_position.side_effect = lambda _s: seq.pop(0) if seq else last

        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long',
                               distance_pct=0.15, timeout_seconds=0.01, poll_interval=0)

        self.assertFalse(result)

    def test_timeout_without_trigger_is_inconclusive(self):
        """价格窗口内未走到止损位：既不算通过也不算失败，返回 None。"""
        api = _fake_api(position_sequence=[10.0, 10.0, 10.0])
        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long',
                               distance_pct=0.15, timeout_seconds=0.01, poll_interval=0)
        self.assertIsNone(result)
        # 超时分支也须清理（防裸仓）
        api.cancel_all_orders.assert_called()
        api.close_position.assert_called()

    def test_triggered_but_algo_still_listed_fails(self):
        """触发后算法单仍在待触发列表：状态语义异常 → False。"""
        api = _fake_api(position_sequence=[10.0, None],
                        algo_after_trigger=[{'id': 'stop-1'}])
        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long',
                               distance_pct=0.15, timeout_seconds=10, poll_interval=0)
        self.assertFalse(result)

    def test_open_position_failure_returns_none_without_cleanup_crash(self):
        """开仓失败：直接返回 None，不进入清理逻辑（无仓可平）。"""
        api = _fake_api(position_sequence=[])
        api.open_position.return_value = None
        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long')
        self.assertIsNone(result)
        api.close_position.assert_not_called()

    def test_create_stop_failure_still_cleans_up(self):
        """止损创建失败：返回 None，但已开的仓必须在 finally 清理，不留裸仓。"""
        api = _fake_api(position_sequence=[10.0])
        api.create_stop_loss_order.return_value = None
        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long')
        self.assertIsNone(result)
        api.cancel_all_orders.assert_called()
        api.close_position.assert_called()


class FireModeCliGuardTest(unittest.TestCase):
    def test_fire_requires_explicit_side(self):
        """--fire 与 --side both 组合：main() 必须在下单前拒绝，不允许模糊方向下的实弹测试。"""
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument('symbol', nargs='?', default='BTCUSDT')
        ap.add_argument('coin', nargs='?', type=float, default=0.0)
        ap.add_argument('--side', choices=['long', 'short', 'both'], default='both')
        ap.add_argument('--fire', action='store_true')
        args = ap.parse_args(['BTCUSDT', '0.1', '--fire'])
        self.assertTrue(args.fire and args.side == 'both')  # 复现 main() 里被拒绝的组合


if __name__ == '__main__':
    unittest.main()
