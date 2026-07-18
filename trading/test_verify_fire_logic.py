"""verify_okx.run_fire_test 决策逻辑单测（桩 ccxt/pandas，本机可运行，不连交易所）。

--fire 模式验证「止损触发瞬间是否只减仓、绝不反向」——本系统止损防线
唯一需要外部实测的一环。这里只测决策逻辑本身（触发判定/归零判定/反向
    判定/超时判定/列表消失判定），真实触发行为只允许用明确模拟盘凭据验证，
    两者互补：这里锁定「代码看到某种交易所响应时会不会下对结论」。
"""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
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
import verify_okx  # noqa: E402
from verify_okx import run_fire_test  # noqa: E402
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
    api._coin_to_contracts.return_value = 10
    api.open_position.return_value = {
        'id': 'order-1', 'confirmed': True,
        'fully_filled': True, 'amount': 0.1,
    }
    api.get_last_price.return_value = 100.0
    api.create_stop_loss_order.return_value = {'id': 'stop-1'}
    api.find_stop_order_state.return_value = 'intact'
    api.confirm_stop_execution.return_value = True
    api.cancel_all_orders.return_value = True
    api.close_position.return_value = {'id': 'close-1'}
    api._fetch_algo_orders.return_value = list(algo_after_trigger or [])

    seq = list(position_sequence)
    first_read = {'pending': True}

    def get_position(_symbol):
        # wrapper 在任何写入前先做一次空仓门禁；真实
        # 开仓仓位只能在 open_position 已被调用后出现。
        if not api.open_position.called:
            return None
        if api.close_position.called:
            return None
        if first_read['pending']:
            first_read['pending'] = False
            opened_side = api.open_position.call_args.args[1]
            return {
                'contracts': 10.0, 'side': opened_side,
                'entryPrice': 100.0,
            }
        if seq:
            val = seq.pop(0)
        elif not position_sequence:
            return None
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
        """归零、无反向、算法单消失且精确成交归因成立 → True。"""
        api = _fake_api(position_sequence=[10.0, None], algo_after_trigger=[])
        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long',
                               distance_pct=0.15, timeout_seconds=10, poll_interval=0)
        self.assertTrue(result)
        api.confirm_stop_execution.assert_called_once()
        # finally 保证清理即使已触发（幂等：无仓时 close_position 由适配层自行处理）
        api.cancel_all_orders.assert_called()
        api.close_position.assert_called()

    def test_manual_flat_and_pending_absent_without_execution_proof_fails(self):
        """手工归零也会造成空仓+待触发清单消失；无精确成交证明不得误判通过。"""
        api = _fake_api(position_sequence=[10.0, None], algo_after_trigger=[])
        api.confirm_stop_execution.return_value = False

        result = run_fire_test(
            api, 'BTC/USDT:USDT', 0.1, 'long',
            distance_pct=0.15, timeout_seconds=10, poll_interval=0)

        self.assertFalse(result)
        self.assertEqual(5, api.confirm_stop_execution.call_count)

    def test_triggered_but_reversed_fails(self):
        """触发后仍有持仓且方向与开仓方向相反 → False，判定为 reduce-only 失守。"""
        api = _fake_api(position_sequence=[], algo_after_trigger=[])
        # 开的是 long：轮询先看到有仓 → 归零（触发）→ 复核时却报告一笔 short 持仓（反向）
        seq = [None, {'contracts': 10.0, 'side': 'long'}, None,
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
            None,
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
            None,
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

    def test_cleanup_rejects_non_true_final_cancel_confirmation(self):
        """最终撤净契约只接受字面 True；None/0/空对象都不是成功证明。"""
        for unconfirmed in (None, 0, {}):
            api = Mock()
            api.cancel_all_orders.side_effect = [True, unconfirmed]
            api.get_position.side_effect = [
                {'contracts': 10.0, 'side': 'long'}, None]
            api._contracts_to_coins.return_value = 0.1
            api.close_position.return_value = {'id': 'close'}

            with self.subTest(unconfirmed=unconfirmed):
                self.assertFalse(verify_okx.cleanup_live_position(
                    api, 'BTC/USDT:USDT', 'long', 0.1,
                    'non-true-final-cancel'))

    def test_cleanup_rejects_non_true_cancel_after_retry(self):
        """竞态补平后的最后一次撤净同样只接受字面 True。"""
        api = Mock()
        api.cancel_all_orders.side_effect = [True, True, None]
        api.get_position.side_effect = [
            {'contracts': 10.0, 'side': 'long'},
            {'contracts': 5.0, 'side': 'long'},
            None,
        ]
        api._contracts_to_coins.side_effect = (
            lambda _symbol, contracts: contracts / 100)
        api.close_position.side_effect = [
            {'id': 'first-close'}, {'id': 'retry-close'}]

        self.assertFalse(verify_okx.cleanup_live_position(
            api, 'BTC/USDT:USDT', 'long', 0.1,
            'non-true-retry-cancel'))

    def test_partial_stop_that_never_flattens_fails(self):
        """止损已部分成交不是“行情未触发”，超时后必须判失败。"""
        api = _fake_api(position_sequence=[])
        seq = [
            None,
            {'contracts': 10.0, 'side': 'long'},
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

    def test_open_none_or_exception_always_runs_cleanup(self):
        """POST 返回不明/抛异常都可能已成交；两者必须强制清理。"""
        for outcome in ('none', 'exception'):
            api = _fake_api(position_sequence=[])
            if outcome == 'none':
                api.open_position.return_value = None
            else:
                api.open_position.side_effect = RuntimeError(
                    'POST outcome unknown')

            result = run_fire_test(
                api, 'BTC/USDT:USDT', 0.1, 'long')

            with self.subTest(outcome=outcome):
                self.assertIsNone(result)
                api.cancel_all_orders.assert_called()
                api.close_position.assert_called_once_with(
                    'BTC/USDT:USDT', 'long', 0.1)

    def test_create_stop_failure_still_cleans_up(self):
        """止损创建失败：返回 None，但已开的仓必须在 finally 清理，不留裸仓。"""
        api = _fake_api(position_sequence=[10.0])
        api.create_stop_loss_order.return_value = None
        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long')
        self.assertIsNone(result)
        api.cancel_all_orders.assert_called()
        api.close_position.assert_called()

    def test_open_ack_without_real_position_is_inconclusive(self):
        api = _fake_api(position_sequence=[])
        api.get_position.side_effect = None
        api.get_position.return_value = None

        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long')

        self.assertIsNone(result)
        api.create_stop_loss_order.assert_not_called()
        api.close_position.assert_called()

    def test_unresolved_open_contract_with_real_position_is_inconclusive(self):
        api = _fake_api(position_sequence=[])
        api.open_position.return_value.update({
            'confirmed': False, 'open_execution_unresolved': True})

        result = run_fire_test(api, 'BTC/USDT:USDT', 0.1, 'long')

        self.assertIsNone(result)
        api.create_stop_loss_order.assert_not_called()
        api.close_position.assert_called()

    def test_successful_trigger_with_failed_cleanup_is_failure(self):
        for unconfirmed in (False, None, 0, {}):
            api = _fake_api(position_sequence=[10.0, None])
            api.cancel_all_orders.return_value = unconfirmed

            result = run_fire_test(
                api, 'BTC/USDT:USDT', 0.1, 'long',
                timeout_seconds=10, poll_interval=0)

            with self.subTest(unconfirmed=unconfirmed):
                self.assertFalse(result)


class BasicVerificationDecisionTest(unittest.TestCase):
    def test_open_ack_without_real_position_fails_instead_of_skipping_stop(self):
        api = Mock()
        api._coin_to_contracts.return_value = 10
        api.open_position.return_value = {'id': 'ack-only'}
        api.get_position.return_value = None
        api.cancel_all_orders.return_value = True
        api.close_position.return_value = {'id': 'cleanup'}
        with patch.object(verify_okx.time, 'sleep'):
            result = verify_okx.run_side(
                api, 'BTC/USDT:USDT', 0.1, 'long')

        self.assertFalse(result)
        api.create_stop_loss_order.assert_not_called()

    def test_unresolved_or_partial_open_contract_never_runs_stop_test(self):
        for order in (
                {'id': 'u', 'confirmed': False, 'fully_filled': True,
                 'amount': 0.1, 'open_execution_unresolved': True},
                {'id': 'p', 'confirmed': True, 'fully_filled': False,
                 'amount': 0.05}):
            api = Mock()
            api._coin_to_contracts.return_value = 10
            api.open_position.return_value = order
            api.get_position.side_effect = [
                None,
                {'contracts': 10, 'side': 'long', 'entryPrice': 100},
                None]
            api.cancel_all_orders.return_value = True
            api.close_position.return_value = {'id': 'cleanup'}

            with self.subTest(order=order), patch.object(
                    verify_okx.time, 'sleep'):
                self.assertFalse(verify_okx.run_side(
                    api, 'BTC/USDT:USDT', 0.1, 'long'))
            api.create_stop_loss_order.assert_not_called()

    def test_cancel_command_false_prevents_basic_false_green(self):
        api = Mock()
        api._coin_to_contracts.return_value = 10
        api.open_position.return_value = {
            'id': 'ok', 'confirmed': True,
            'fully_filled': True, 'amount': 0.1}
        api.get_position.side_effect = [
            None,
            {'contracts': 10, 'side': 'long', 'entryPrice': 100},
            None, None]
        api.create_stop_loss_order.return_value = {'id': 'stop-1'}
        api._fetch_algo_orders.side_effect = [
            [{'id': 'stop-1'}], []]
        api.find_stop_order_state.return_value = 'intact'
        api.cancel_order.return_value = False
        api.cancel_all_orders.return_value = True
        api.close_position.return_value = {'id': 'cleanup'}

        with patch.object(verify_okx.time, 'sleep'):
            result = verify_okx.run_side(
                api, 'BTC/USDT:USDT', 0.1, 'long')

        self.assertFalse(result)

    def test_open_none_or_exception_always_runs_cleanup(self):
        """基础验证 wrapper 也不得把未知 POST 结果当作“无仓”。"""
        for outcome in ('none', 'exception'):
            api = Mock()
            api._coin_to_contracts.return_value = 10
            if outcome == 'none':
                api.open_position.return_value = None
            else:
                api.open_position.side_effect = RuntimeError(
                    'POST outcome unknown')
            api.get_position.side_effect = [
                None, {'contracts': 10, 'side': 'long'}, None]
            api.cancel_all_orders.return_value = True
            api.close_position.return_value = {'id': 'cleanup'}

            with self.subTest(outcome=outcome), patch.object(
                    verify_okx.time, 'sleep'):
                self.assertFalse(verify_okx.run_side(
                    api, 'BTC/USDT:USDT', 0.1, 'long'))
            api.cancel_all_orders.assert_called()
            api.close_position.assert_called_once_with(
                'BTC/USDT:USDT', 'long', 0.1)


class FireModeCliGuardTest(unittest.TestCase):
    def test_module_entrypoint_has_sys_for_nonzero_exit(self):
        self.assertIs(verify_okx.sys, sys)

    def test_fire_requires_explicit_side(self):
        """--fire 与 --side both 组合必须返回非零。"""
        with patch.object(sys, 'argv', [
                'verify_okx.py', 'BTCUSDT', '0.1', '--fire']):
            self.assertEqual(2, verify_okx.main())

    def test_write_modes_require_finite_positive_coin_before_config_load(self):
        """写模式的数量无效不得退化为退出码 0 的只读假绿。"""
        invalid_coins = (None, '0', '-0.1', 'nan', 'inf')
        for mode in ('--fire', '--stop-id-reuse'):
            for coin in invalid_coins:
                argv = ['verify_okx.py', 'BTCUSDT']
                if coin is not None:
                    argv.append(coin)
                argv.extend(['--side', 'long', mode])
                with self.subTest(mode=mode, coin=coin), \
                        patch.object(sys, 'argv', argv), \
                        patch.object(
                            verify_okx, 'load_cfg',
                            side_effect=AssertionError(
                                '非法写数量必须在读取配置前拒绝')), \
                        patch.object(
                            verify_okx, 'OkxApi',
                            side_effect=AssertionError(
                                '非法写数量不得构造交易 API')):
                    self.assertEqual(2, verify_okx.main())

    @staticmethod
    def _cli_fixture():
        cfg = {
            'apiKey': 'k', 'secret': 's', 'password': 'p',
            'sandbox': True, 'margin_mode': 'cross', 'leverage': 1,
        }
        api = Mock()
        api.to_ccxt_symbol.return_value = 'BTC/USDT:USDT'
        api._get_contract_size.return_value = 0.01
        api.get_balance.return_value = {'total': {'USDT': 1}}
        return cfg, api

    def test_fire_failure_and_inconclusive_are_nonzero(self):
        for result in (False, None):
            cfg, api = self._cli_fixture()
            with self.subTest(result=result), \
                    patch.object(sys, 'argv', [
                        'verify_okx.py', 'BTCUSDT', '0.1',
                        '--fire', '--side', 'long']), \
                    patch.object(verify_okx, 'load_cfg', return_value=cfg), \
                    patch.object(verify_okx, 'OkxApi', return_value=api), \
                    patch.object(verify_okx, 'check_position_mode', return_value=True), \
                    patch.object(verify_okx, 'run_fire_test', return_value=result):
                self.assertEqual(1, verify_okx.main())

    def test_read_only_success_is_zero(self):
        cfg, api = self._cli_fixture()
        for argv in (
                ['verify_okx.py'],
                ['verify_okx.py', 'BTCUSDT', '0']):
            with self.subTest(argv=argv), patch.object(sys, 'argv', argv), \
                    patch.object(verify_okx, 'load_cfg', return_value=cfg), \
                    patch.object(verify_okx, 'OkxApi', return_value=api), \
                    patch.object(
                        verify_okx, 'check_position_mode', return_value=True):
                self.assertEqual(0, verify_okx.main())

    def test_live_credentials_hard_reject_every_write_mode_before_api_construction(self):
        cfg, _api = self._cli_fixture()
        cfg['sandbox'] = False
        argvs = (
            ['verify_okx.py', 'BTCUSDT', '0.1', '--side', 'long'],
            ['verify_okx.py', 'BTCUSDT', '0.1', '--side', 'long', '--fire'],
            ['verify_okx.py', 'BTCUSDT', '0.1', '--side', 'long',
             '--stop-id-reuse'],
        )
        for argv in argvs:
            with self.subTest(argv=argv), patch.object(sys, 'argv', argv), \
                    patch.object(verify_okx, 'load_cfg', return_value=cfg), \
                    patch.object(
                        verify_okx, 'OkxApi',
                        side_effect=AssertionError(
                            '实盘写验证不得构造交易 API')):
                self.assertEqual(2, verify_okx.main())

    def test_live_read_only_is_noninteractive_and_allowed(self):
        cfg, api = self._cli_fixture()
        cfg['sandbox'] = False
        with patch.object(sys, 'argv', ['verify_okx.py', 'BTCUSDT', '0']), \
                patch.object(verify_okx, 'load_cfg', return_value=cfg), \
                patch.object(verify_okx, 'OkxApi', return_value=api), \
                patch.object(verify_okx, 'check_position_mode', return_value=True), \
                patch('builtins.input', side_effect=AssertionError(
                    '实盘只读检查不应有交互放行旁路')):
            self.assertEqual(0, verify_okx.main())

    def test_missing_balance_is_nonzero(self):
        cfg, api = self._cli_fixture()
        api.get_balance.return_value = {'total': {}}
        with patch.object(sys, 'argv', ['verify_okx.py']), \
                patch.object(verify_okx, 'load_cfg', return_value=cfg), \
                patch.object(verify_okx, 'OkxApi', return_value=api), \
                patch.object(verify_okx, 'check_position_mode', return_value=True):
            self.assertEqual(1, verify_okx.main())

    def test_demo_environment_is_strictly_parsed(self):
        for value, expected in (('1', True), ('0', False),
                                ('true', True), ('false', False)):
            with self.subTest(value=value), patch.dict(
                    verify_okx.os.environ, {
                        'OKX_DEMO': value,
                        'OKX_API_KEY': 'k',
                        'OKX_API_SECRET': 's',
                        'OKX_API_PASSPHRASE': 'p',
                    }, clear=True), patch.object(
                        verify_okx.os.path, 'exists', return_value=False):
                self.assertIs(verify_okx.load_cfg()['sandbox'], expected)
        with patch.dict(verify_okx.os.environ, {
                'OKX_DEMO': 'maybe'}, clear=True), self.assertRaises(ValueError):
            verify_okx.load_cfg()

    def test_config_file_demo_switch_is_strictly_parsed(self):
        base_env = {
            'OKX_API_KEY': 'k',
            'OKX_API_SECRET': 's',
            'OKX_API_PASSPHRASE': 'p',
        }
        for value, expected in (('false', False), ('true', True)):
            payload = '{"okx": {"sandbox": "%s"}}' % value
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / 'config.json'
                config_path.write_text(payload, encoding='utf-8')
                os.chmod(config_path, 0o600)
                with patch.dict(
                        verify_okx.os.environ, base_env, clear=True), patch.object(
                            verify_okx, '__file__', str(Path(tmp) / 'verify_okx.py')):
                    self.assertIs(verify_okx.load_cfg()['sandbox'], expected)

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / 'config.json'
            config_path.write_text(
                '{"okx": {"sandbox": null}}', encoding='utf-8')
            os.chmod(config_path, 0o600)
            with patch.dict(
                    verify_okx.os.environ, base_env, clear=True), patch.object(
                        verify_okx, '__file__', str(Path(tmp) / 'verify_okx.py')):
                with self.assertRaisesRegex(ValueError, '无法安全读取验证配置'):
                    verify_okx.load_cfg()

    def test_config_file_execution_settings_match_production_precedence(self):
        base_env = {
            'OKX_API_KEY': 'k', 'OKX_API_SECRET': 's',
            'OKX_API_PASSPHRASE': 'p',
        }
        payload = (
            '{"okx": {"sandbox": false, "margin_mode": "isolated", '
            '"leverage": 9}}')
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / 'config.json'
            config_path.write_text(payload, encoding='utf-8')
            os.chmod(config_path, 0o600)
            with patch.dict(
                    verify_okx.os.environ, base_env, clear=True), patch.object(
                        verify_okx, '__file__', str(Path(tmp) / 'verify_okx.py')):
                cfg = verify_okx.load_cfg()
        self.assertEqual('isolated', cfg['margin_mode'])
        self.assertEqual(9, cfg['leverage'])

        dual = (
            '{"okx": {"sandbox": false}, '
            '"exchanges": {"okx": {"sandbox": true}}}')
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / 'config.json'
            config_path.write_text(dual, encoding='utf-8')
            os.chmod(config_path, 0o600)
            with patch.dict(
                    verify_okx.os.environ, base_env, clear=True), patch.object(
                        verify_okx, '__file__', str(Path(tmp) / 'verify_okx.py')):
                with self.assertRaisesRegex(ValueError, '双源'):
                    verify_okx.load_cfg()


if __name__ == '__main__':
    unittest.main()
