"""verify_okx.run_fire_test 决策逻辑单测（桩 ccxt/pandas，本机可运行，不连交易所）。

--fire 模式验证「止损触发瞬间是否只减仓、绝不反向」——本系统止损防线
唯一从未被实测过的一环。这里只测决策逻辑本身（触发判定/归零判定/反向
判定/超时判定/列表消失判定），真实触发行为由用户在实盘上用该脚本验证，
两者互补：这里锁定「代码看到某种交易所响应时会不会下对结论」。
"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

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
from verify_okx import run_fire_test, run_side
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
    api.confirm_position_flat.return_value = True
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
        clock = [0]

        def tick():
            clock[0] += 1
            return clock[0]

        patcher = patch.object(verify_okx, 'time', Mock(sleep=lambda s: None, time=tick))
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

    def test_timeout_without_trigger_is_inconclusive(self):
        """价格窗口内未走到止损位：既不算通过也不算失败，返回 None。"""
        api = _fake_api(position_sequence=[10.0, None])
        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long',
                               distance_pct=0.15, timeout_seconds=2, poll_interval=0)
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

    def test_cleanup_residual_overrides_trigger_pass(self):
        """触发证据本来通过，但最终清理又见残仓：最终门禁必须失败。"""
        api = _fake_api(position_sequence=[10.0, None])
        api.confirm_position_flat.side_effect = [True, False]
        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long',
                               distance_pct=0.15, timeout_seconds=10, poll_interval=0)
        self.assertFalse(result)

    def test_cleanup_order_failure_overrides_trigger_pass(self):
        """已确认空仓但最终撤单不可确认时，门禁仍必须失败。"""
        api = _fake_api(position_sequence=[10.0, None])
        api.cancel_all_orders.side_effect = [True, False]
        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long',
                               distance_pct=0.15, timeout_seconds=10, poll_interval=0)
        self.assertFalse(result)

    def test_open_position_failure_is_failure_and_still_cleans_uncertain_fill(self):
        """开仓返回失败也可能迟到成交，必须进入统一清理。"""
        api = _fake_api(position_sequence=[])
        api.open_position.return_value = None
        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long')
        self.assertFalse(result)
        api.close_position.assert_called()

    def test_unproven_initial_flat_refuses_without_touching_existing_position(self):
        """验证前无法证明空仓时不得开仓，也不得借清理误平人工仓。"""
        api = _fake_api(position_sequence=[])
        api.confirm_position_flat.return_value = False
        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long')
        self.assertFalse(result)
        api.open_position.assert_not_called()
        api.close_position.assert_not_called()
        api.cancel_all_orders.assert_not_called()

    def test_create_stop_failure_still_cleans_up(self):
        """止损创建失败：明确判失败，且已开的仓必须在 finally 清理，不留裸仓。"""
        api = _fake_api(position_sequence=[10.0])
        api.create_stop_loss_order.return_value = None
        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long')
        self.assertFalse(result)
        api.cancel_all_orders.assert_called()
        api.close_position.assert_called()


class StandardVerificationDecisionLogicTest(unittest.TestCase):
    def test_missing_real_entry_price_cannot_pass(self):
        """拿不到实仓入场价时止损链未被验证，普通验证必须明确失败。"""
        api = Mock()
        api._coin_to_contracts.return_value = 10
        api.open_position.return_value = {'id': 'open-1'}
        api.get_position.side_effect = [
            {'contracts': 10, 'side': 'long', 'entryPrice': None},
            None,
        ]
        api.cancel_all_orders.return_value = True
        api.close_position.return_value = {'id': 'close-1'}
        api.confirm_position_flat.return_value = True
        with patch.object(verify_okx.time, 'sleep', return_value=None):
            self.assertFalse(run_side(api, 'BTC/USDT:USDT', 0.1, 'long'))
        api.create_stop_loss_order.assert_not_called()
        api.close_position.assert_called_once()


class FireModeCliGuardTest(unittest.TestCase):
    def test_fire_requires_explicit_side(self):
        """--fire 与 --side both 组合须在下单前拒绝，并返回非零退出码。"""
        with patch.object(sys, 'argv', ['verify_okx.py', 'BTCUSDT', '0.1', '--fire']):
            self.assertEqual(verify_okx.main(), 2)

    def test_missing_credentials_returns_nonzero(self):
        with patch.object(sys, 'argv', ['verify_okx.py']), patch.object(
                verify_okx, 'load_cfg', return_value={
                    'apiKey': None, 'secret': None, 'password': None,
                    'sandbox': True, 'margin_mode': 'cross', 'leverage': 3,
                }):
            self.assertEqual(verify_okx.main(), 2)

    def test_unproven_net_position_mode_refuses_before_any_open(self):
        """双向模式或查询失败时不得把警告当通过，更不得进入实弹开仓。"""
        api = Mock()
        api.to_ccxt_symbol.return_value = 'BTC/USDT:USDT'
        api._get_contract_size.return_value = 0.01
        cfg = {
            'apiKey': 'key', 'secret': 'secret', 'password': 'pass',
            'sandbox': True, 'margin_mode': 'cross', 'leverage': 3,
        }
        with patch.object(sys, 'argv', ['verify_okx.py', 'BTCUSDT', '0.1']), patch.object(
                verify_okx, 'load_cfg', return_value=cfg), patch.object(
                verify_okx, 'acquire_verification_lock', return_value=Mock()), patch.object(
                verify_okx, 'OkxApi', return_value=api), patch.object(
                verify_okx, 'check_position_mode', return_value=None):
            self.assertEqual(verify_okx.main(), 2)
        api.open_position.assert_not_called()
        api.get_balance.assert_not_called()

    def test_missing_balance_cannot_pass_read_only_verification(self):
        api = Mock()
        api.to_ccxt_symbol.return_value = 'BTC/USDT:USDT'
        api._get_contract_size.return_value = 0.01
        api.get_balance.return_value = None
        cfg = {
            'apiKey': 'key', 'secret': 'secret', 'password': 'pass',
            'sandbox': True, 'margin_mode': 'cross', 'leverage': 3,
        }
        with patch.object(sys, 'argv', ['verify_okx.py', 'BTCUSDT']), patch.object(
                verify_okx, 'load_cfg', return_value=cfg), patch.object(
                verify_okx, 'acquire_verification_lock', return_value=Mock()), patch.object(
                verify_okx, 'OkxApi', return_value=api), patch.object(
                verify_okx, 'check_position_mode', return_value=True):
            self.assertEqual(verify_okx.main(), 2)
        api.open_position.assert_not_called()

    def test_verification_uses_same_global_runner_lock(self):
        lock = Mock()
        fake_main = types.SimpleNamespace(acquire_runner_lock=Mock(return_value=lock))
        with patch.dict(sys.modules, {'main': fake_main}):
            self.assertIs(verify_okx.acquire_verification_lock(), lock)
        fake_main.acquire_runner_lock.assert_called_once_with()


class VerificationConfigPrecedenceTest(unittest.TestCase):
    def test_config_margin_and_leverage_are_not_hidden_by_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_script = os.path.join(tmp, 'verify_okx.py')
            with open(os.path.join(tmp, 'config.json'), 'w', encoding='utf-8') as f:
                json.dump({
                    'okx': {
                        'apiKey': 'config-key', 'secret': 'config-secret', 'password': 'config-pass',
                        'margin_mode': 'isolated', 'leverage': 7,
                    },
                    'exchanges': {'okx': {'margin_mode': 'cross', 'leverage': 99}},
                }, f)
            with patch.object(verify_okx, '__file__', fake_script), patch.dict(
                    os.environ, {}, clear=True):
                cfg = verify_okx.load_cfg()
        self.assertEqual(cfg['margin_mode'], 'isolated')
        self.assertEqual(cfg['leverage'], 7)

    def test_environment_overrides_config_then_defaults_fill_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_script = os.path.join(tmp, 'verify_okx.py')
            with open(os.path.join(tmp, 'config.json'), 'w', encoding='utf-8') as f:
                json.dump({'okx': {'margin_mode': 'isolated', 'leverage': 7}}, f)
            env = {'OKX_MARGIN_MODE': 'cross', 'OKX_LEVERAGE': '5'}
            with patch.object(verify_okx, '__file__', fake_script), patch.dict(
                    os.environ, env, clear=True):
                cfg = verify_okx.load_cfg()
        self.assertEqual(cfg['margin_mode'], 'cross')
        self.assertEqual(cfg['leverage'], 5.0)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(verify_okx, '__file__', os.path.join(tmp, 'verify_okx.py')), patch.dict(
                    os.environ, {}, clear=True):
                defaults = verify_okx.load_cfg()
        self.assertEqual(defaults['margin_mode'], 'cross')
        self.assertEqual(defaults['leverage'], 3.0)


if __name__ == '__main__':
    unittest.main()
