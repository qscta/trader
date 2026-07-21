"""OKX 适配层交易安全单测（桩 ccxt/pandas，本机可运行）。

覆盖 Codex 第四轮审查的三条红线：
1. contractSize 不可得必须 fail closed（抛异常拒绝换算/交易，不许默认 1.0）。
2. 撤算法止损必须可验证：OrderNotFound 不等于已撤干净，以「算法单列表查无此 id」为准。
3. 止损创建超时确认必须 方向+触发价+张数 全匹配，不能只按方向误认残留旧单。
"""
import sys
import types
import unittest
from unittest.mock import Mock, patch

# 桩 ccxt / pandas 后导入 okx_api，导入完立即恢复（同 _test_stubs 思路）
_CCXT_EXC = ('OrderNotFound', 'RequestTimeout', 'NetworkError', 'ExchangeNotAvailable',
             'DDoSProtection', 'RateLimitExceeded', 'InsufficientFunds', 'InvalidOrder',
             'BadRequest', 'AuthenticationError', 'PermissionDenied', 'BadSymbol', 'NotSupported')
_saved = {}
for _name in ('ccxt', 'pandas'):
    _saved[_name] = sys.modules.get(_name)
_ccxt = types.ModuleType('ccxt')
for _e in _CCXT_EXC:
    setattr(_ccxt, _e, type(_e, (Exception,), {}))
_ccxt.okx = Mock()
sys.modules['ccxt'] = _ccxt
sys.modules['pandas'] = types.ModuleType('pandas')
sys.modules.pop('exchange_base', None)
sys.modules.pop('okx_api', None)
import okx_api
from okx_api import OkxApi, ContractSizeUnavailable
for _name, _orig in _saved.items():
    if _orig is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _orig
sys.modules.pop('exchange_base', None)
sys.modules.pop('okx_api', None)

OrderNotFound = _ccxt.OrderNotFound
RequestTimeout = _ccxt.RequestTimeout


def _bare_api():
    """不经 __init__ 构造最小 OkxApi（不连交易所）。"""
    api = object.__new__(OkxApi)
    api.exchange = Mock()
    api._contract_size_cache = {}
    api._amount_precision_cache = {}
    api.CANCEL_VERIFY_RECHECK_DELAY = 0  # 测试不等待复查间隔
    return api


def _native_stop(algo_id='stop-1', side='sell', sz='10', px='55000'):
    """OKX orders-algo-pending 原生响应里的一条 conditional 止损单。"""
    return {'algoId': algo_id, 'side': side, 'sz': sz, 'slTriggerPx': px,
            'ordType': 'conditional', 'reduceOnly': 'true'}


def _algo_stub(items_by_type):
    """按 ordType 返回原生响应的桩（缺省该类型无挂单）。"""
    def call(params):
        return {'code': '0', 'msg': '',
                'data': list(items_by_type.get(params.get('ordType'), []))}
    return call


def _phased_algo_stub(phases):
    """按「轮次」返回 conditional 挂单的桩：一轮 = 一次 _fetch_algo_orders =
    len(ALGO_ORDER_TYPES) 次原生调用；轮数超出 phases 后沿用最后一轮
    （模拟交易所列表滞后于撤单生效的时序）。"""
    state = {'calls': 0}
    n_types = len(OkxApi.ALGO_ORDER_TYPES)

    def call(params):
        round_i = min(state['calls'] // n_types, len(phases) - 1)
        state['calls'] += 1
        data = phases[round_i] if params.get('ordType') == 'conditional' else []
        return {'code': '0', 'msg': '', 'data': list(data)}
    return call


class ContractSizeFailClosedTest(unittest.TestCase):
    def test_missing_contract_size_raises(self):
        """市场数据缺 contractSize：必须抛异常，不允许默认 1.0。"""
        api = _bare_api()
        api.exchange.market.return_value = {'contractSize': None}
        with self.assertRaises(ContractSizeUnavailable):
            api._get_contract_size('BTC/USDT:USDT')

    def test_market_query_failure_raises(self):
        api = _bare_api()
        api.exchange.market.side_effect = RuntimeError('网络错误')
        with self.assertRaises(ContractSizeUnavailable):
            api._get_contract_size('BTC/USDT:USDT')

    def test_nonfinite_contract_size_raises(self):
        api = _bare_api()
        api.exchange.market.return_value = {'contractSize': float('nan')}
        with self.assertRaises(ContractSizeUnavailable):
            api._get_contract_size('BTC/USDT:USDT')

    def test_coin_to_contracts_propagates(self):
        """换算入口同样拒绝交易（异常向上传播给调用方放弃本次开/平/止损）。"""
        api = _bare_api()
        api.exchange.market.side_effect = RuntimeError('网络错误')
        with self.assertRaises(ContractSizeUnavailable):
            api._coin_to_contracts('BTC/USDT:USDT', 0.5)

    def test_valid_contract_size_cached(self):
        api = _bare_api()
        api.exchange.market.return_value = {'contractSize': 0.01}
        self.assertEqual(api._get_contract_size('BTC/USDT:USDT'), 0.01)
        self.assertEqual(api._contract_size_cache['BTC/USDT:USDT'], 0.01)

    def test_coin_to_contracts_does_not_lose_one_contract_to_float_error(self):
        """10.1 币 / 0.1 面值必须还原 101 张，不能先变成 100.999…再被截断。"""
        api = _bare_api()
        api._contract_size_cache['HYPE/USDT:USDT'] = 0.1
        api.exchange.amount_to_precision.side_effect = (
            lambda _symbol, amount: str(int(float(amount))))

        self.assertEqual(api._coin_to_contracts('HYPE/USDT:USDT', 10.1), 101.0)
        self.assertTrue(api.managed_position_matches(
            'HYPE/USDT:USDT', {'side': 'short', 'contracts': 101}, 'short', 10.1))

    def test_coin_to_contracts_still_truncates_real_sub_contract_amount(self):
        """十进制修复只能消除浮点误差，真实不足 101 张的数量仍必须截成 100 张。"""
        api = _bare_api()
        api._contract_size_cache['HYPE/USDT:USDT'] = 0.1
        api.exchange.amount_to_precision.side_effect = (
            lambda _symbol, amount: str(int(float(amount))))

        self.assertEqual(api._coin_to_contracts('HYPE/USDT:USDT', 10.099), 100.0)

    def test_coin_to_contracts_repairs_upstream_multiplication_residue(self):
        """账本币数由张数×面值形成时即使略低一个 ULP，也必须还原原张数。"""
        api = _bare_api()
        api._contract_size_cache['MICRO/USDT:USDT'] = 0.000001
        api.exchange.amount_to_precision.side_effect = (
            lambda _symbol, amount: str(int(float(amount))))
        coin_amount = 10 * 0.000001

        self.assertLess(coin_amount, 0.00001)
        self.assertEqual(
            api._coin_to_contracts('MICRO/USDT:USDT', coin_amount), 10.0)


class ManagedPositionMatchTest(unittest.TestCase):
    def _api(self):
        api = _bare_api()
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api.exchange.amount_to_precision.return_value = '10'
        return api

    def test_direction_and_quantity_must_both_match(self):
        api = self._api()
        self.assertTrue(api.managed_position_matches(
            'BTC/USDT:USDT', {'side': 'long', 'contracts': 10}, 'long', 0.1))
        self.assertFalse(api.managed_position_matches(
            'BTC/USDT:USDT', {'side': 'short', 'contracts': 10}, 'long', 0.1))
        self.assertFalse(api.managed_position_matches(
            'BTC/USDT:USDT', {'side': 'long', 'contracts': 12}, 'long', 0.1))

    def test_open_refuses_when_position_preflight_fails(self):
        api = self._api()
        api.margin_mode = 'cross'
        api._leverage_done = {'BTC/USDT:USDT'}
        api.exchange.fetch_positions.side_effect = RuntimeError('查询失败')

        self.assertIsNone(api.open_position('BTC/USDT:USDT', 'long', 0.1))
        api.exchange.create_order.assert_not_called()

    def test_nonfinite_position_is_not_mistaken_for_flat(self):
        api = self._api()
        api.exchange.fetch_positions.return_value = [
            {'contracts': float('nan'), 'side': 'long'}]
        with self.assertRaises(RuntimeError):
            api.get_position('BTC/USDT:USDT')

    def test_nonfinite_last_price_is_rejected(self):
        api = self._api()
        api.exchange.fetch_ticker.return_value = {'last': float('inf')}
        with self.assertRaises(ValueError):
            api.get_last_price('BTC/USDT:USDT')

    def test_open_refuses_when_position_appears_at_final_boundary(self):
        api = self._api()
        api.margin_mode = 'cross'
        api._leverage_done = {'BTC/USDT:USDT'}
        api.exchange.fetch_positions.return_value = [
            {'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT'}]

        self.assertIsNone(api.open_position('BTC/USDT:USDT', 'long', 0.1))
        api.exchange.create_order.assert_not_called()

    def test_deployment_gate_blocks_adapter_boundary(self):
        api = self._api()
        api.margin_mode = 'cross'
        api._leverage_done = {'BTC/USDT:USDT'}
        with patch.dict(okx_api.os.environ, {'TRADING_DISABLE_NEW_OPENS': '1'}):
            self.assertIsNone(api.open_position('BTC/USDT:USDT', 'long', 0.1))
        api.exchange.fetch_positions.assert_not_called()
        api.exchange.create_order.assert_not_called()


class OpenFillConfirmationTest(unittest.TestCase):
    """OKX 下单回执只代表受理；完整实仓才是开仓成功证据。"""

    def _api(self):
        api = _bare_api()
        api.margin_mode = 'cross'
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api._leverage_done = {'BTC/USDT:USDT'}
        api.exchange.amount_to_precision.return_value = '10'
        api.exchange.create_order.return_value = {'id': 'open-1', 'average': None}
        return api

    def test_normal_ack_requires_full_position_confirmation(self):
        api = self._api()
        api.exchange.fetch_positions.side_effect = [
            [],
            [{'contracts': 10, 'side': 'long', 'entryPrice': 60000,
              'symbol': 'BTC/USDT:USDT'}],
        ]

        order = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertEqual(order['id'], 'open-1')
        self.assertEqual(order['confirmed_coin_amount'], 0.1)
        self.assertEqual(order['average'], 60000)

    def test_malformed_ack_cannot_orphan_a_fully_confirmed_position(self):
        """回执畸形但实仓完整：继续托管且不伪造订单号，不能异常退出遗忘真实仓位。"""
        api = self._api()
        api.exchange.create_order.return_value = None
        api.exchange.fetch_positions.side_effect = [
            [],
            [{'contracts': 10, 'side': 'long', 'entryPrice': 60000,
              'symbol': 'BTC/USDT:USDT'}],
        ]
        api.cancel_all_orders = Mock(return_value=True)
        api.close_position = Mock(return_value={'id': 'rollback'})

        order = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertIsInstance(order, dict)
        self.assertNotIn('id', order)
        self.assertEqual(order['confirmed_coin_amount'], 0.1)
        self.assertEqual(order['average'], 60000)
        api.cancel_all_orders.assert_not_called()
        api.close_position.assert_not_called()

    def test_ack_with_only_partial_fill_is_cancelled_then_flattened(self):
        api = self._api()
        partial = [{'contracts': 4, 'side': 'long', 'entryPrice': 60000,
                    'symbol': 'BTC/USDT:USDT'}]
        api.exchange.fetch_positions.side_effect = [[], partial, partial, partial]
        calls = []
        api.cancel_all_orders = Mock(side_effect=lambda _s: calls.append('cancel') or True)
        api.close_position = Mock(side_effect=lambda *_a: calls.append('close') or {'id': 'close-1'})

        with patch.object(okx_api, 'time', Mock(sleep=lambda _s: None)):
            order = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertIsNone(order)
        self.assertEqual(calls, ['cancel', 'close'])

    def test_timeout_partial_fill_is_not_mistaken_for_complete_fill(self):
        api = self._api()
        api.exchange.create_order.side_effect = RequestTimeout('超时')
        partial = [{'contracts': 4, 'side': 'long', 'entryPrice': 60000,
                    'symbol': 'BTC/USDT:USDT'}]
        api.exchange.fetch_positions.side_effect = [[], partial, partial, partial]
        api.cancel_all_orders = Mock(return_value=True)
        api.close_position = Mock(return_value={'id': 'close-1'})

        with patch.object(okx_api, 'time', Mock(sleep=lambda _s: None)):
            order = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertIsNone(order)
        api.cancel_all_orders.assert_called_once_with('BTC/USDT:USDT')
        api.close_position.assert_called_once_with('BTC/USDT:USDT', 'long', 0.1)

    def test_unclassified_submit_exception_is_treated_as_ambiguous_write(self):
        """未知异常可能发生在订单已到达后：必须清理，不能按确定失败直接遗忘。"""
        api = self._api()
        api.exchange.create_order.side_effect = RuntimeError('回执解析失败')
        api.exchange.fetch_positions.return_value = []
        api.cancel_all_orders = Mock(return_value=True)
        api.close_position = Mock(return_value={'id': 'already_closed'})

        order = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertIsNone(order)
        api.cancel_all_orders.assert_called_once_with('BTC/USDT:USDT')
        api.close_position.assert_called_once_with('BTC/USDT:USDT', 'long', 0.1)


class CloseConfirmationTest(unittest.TestCase):
    def _api(self):
        api = _bare_api()
        api.margin_mode = 'cross'
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api.exchange.amount_to_precision.return_value = '10'
        api.exchange.create_order.return_value = {'id': 'close-1'}
        return api

    def test_success_response_returns_only_after_position_is_flat(self):
        api = self._api()
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 10, 'side': 'long'}], [], []]

        with patch.object(okx_api, 'time', Mock(sleep=lambda _s: None)):
            order = api.close_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertEqual(order['id'], 'close-1')

    def test_success_response_with_residual_position_is_not_confirmed(self):
        api = self._api()
        residual = [{'contracts': 4, 'side': 'long'}]
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 10, 'side': 'long'}], residual, residual, residual]

        with patch.object(okx_api, 'time', Mock(sleep=lambda _s: None)):
            order = api.close_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertIsNone(order)

    def test_single_empty_response_does_not_confirm_flat(self):
        api = self._api()
        api.exchange.fetch_positions.side_effect = [
            [], [{'contracts': 4, 'side': 'long'}], []]

        with patch.object(okx_api, 'time', Mock(sleep=lambda _s: None)):
            self.assertFalse(api.confirm_position_flat('BTC/USDT:USDT'))


class StopOrderTimeoutSafetyTest(unittest.TestCase):
    def _api(self):
        api = _bare_api()
        api.margin_mode = 'cross'
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api.exchange.amount_to_precision.return_value = '10'
        api.exchange.price_to_precision.return_value = '55000'
        api.exchange.create_order.side_effect = RequestTimeout('超时')
        return api

    def test_ambiguous_timeout_never_resends_non_idempotent_stop(self):
        api = self._api()
        with patch.object(api, '_fetch_algo_orders', return_value=[]), \
                patch.object(okx_api, 'time', Mock(sleep=lambda _s: None)):
            order = api.create_stop_loss_order(
                'BTC/USDT:USDT', 'long', 0.1, 55000)

        self.assertIsNone(order)
        self.assertEqual(api.exchange.create_order.call_count, 1)

    def test_timeout_confirmation_finds_order_without_resending(self):
        api = self._api()
        confirmed = {
            'id': 'stop-1', 'side': 'sell', 'amount': 10,
            'stopLossPrice': 55000, 'reduceOnly': True, 'info': {},
        }
        with patch.object(api, '_fetch_algo_orders', side_effect=[[], [confirmed]]), \
                patch.object(okx_api, 'time', Mock(sleep=lambda _s: None)):
            order = api.create_stop_loss_order(
                'BTC/USDT:USDT', 'long', 0.1, 55000)

        self.assertEqual(order['id'], 'stop-1')
        self.assertEqual(api.exchange.create_order.call_count, 1)

    def test_success_response_without_pending_order_is_not_trusted(self):
        api = self._api()
        api.exchange.create_order.side_effect = None
        api.exchange.create_order.return_value = {'id': 'ack-only'}
        with patch.object(api, '_fetch_algo_orders', return_value=[]), \
                patch.object(okx_api, 'time', Mock(sleep=lambda _s: None)):
            order = api.create_stop_loss_order(
                'BTC/USDT:USDT', 'long', 0.1, 55000)

        self.assertIsNone(order)
        self.assertEqual(api.exchange.create_order.call_count, 1)


class CancelAlgoVerifiedTest(unittest.TestCase):
    def test_cancel_command_ok_but_still_listed_is_failure(self):
        """关键回归：撤销指令自称成功但清单仍有该 id → 必须判失败（指令返回不构成结论）。"""
        api = _bare_api()
        api.exchange.privatePostTradeCancelAlgos.return_value = {'code': '0', 'data': []}
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop()]})
        self.assertFalse(api._cancel_algo_order('BTC/USDT:USDT', 'stop-1'))

    def test_cancelled_and_absent_is_success(self):
        api = _bare_api()
        api.exchange.privatePostTradeCancelAlgos.return_value = {'code': '0', 'data': []}
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({})
        self.assertTrue(api._cancel_algo_order('BTC/USDT:USDT', 'stop-1'))
        # 撤销指令必须走原生 cancel-algos，携带 algoId + instId 数组体
        api.exchange.privatePostTradeCancelAlgos.assert_called_once_with(
            [{'algoId': 'stop-1', 'instId': 'BTC-USDT-SWAP'}])

    def test_cancel_failed_but_absent_is_success(self):
        """撤销指令失败但清单确认目标不存在（已触发/已撤）→ 成功。"""
        api = _bare_api()
        api.exchange.privatePostTradeCancelAlgos.side_effect = OrderNotFound('不存在')
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop(algo_id='other')]})
        self.assertTrue(api._cancel_algo_order('BTC/USDT:USDT', 'stop-1'))

    def test_unverifiable_is_failure(self):
        """撤销与确认查询都失败：不可确认 ≠ 已撤干净 → 失败。"""
        api = _bare_api()
        api.exchange.privatePostTradeCancelAlgos.side_effect = RuntimeError('超时')
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = RuntimeError('超时')
        self.assertFalse(api._cancel_algo_order('BTC/USDT:USDT', 'stop-1'))


class TimeoutConfirmCoinUnitsTest(unittest.TestCase):
    """下单超时后经持仓查询确认的返回单，amount 必须换算回币数——张数不外泄的分层契约。"""

    def _api(self):
        api = _bare_api()
        api.margin_mode = 'cross'
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api._leverage_done = {'BTC/USDT:USDT'}
        api.exchange.amount_to_precision.return_value = '10'  # 0.1 币 / 0.01 面值 = 10 张
        return api

    def test_open_timeout_confirm_returns_coins(self):
        api = self._api()
        api.exchange.create_order.side_effect = RequestTimeout('超时')
        api.exchange.fetch_positions.side_effect = [
            [],                                                # 开仓前：无仓
            [{'contracts': 10, 'side': 'long',
              'symbol': 'BTC/USDT:USDT'}],                     # 超时后确认：已成交 10 张
        ]
        with patch.object(okx_api, 'time', Mock(sleep=lambda s: None)):
            order = api.open_position('BTC/USDT:USDT', 'long', 0.1)
        self.assertEqual(order['id'], 'timeout_confirmed')
        self.assertAlmostEqual(order['amount'], 0.1)  # 10 张 × 0.01 = 0.1 币
        self.assertAlmostEqual(order['confirmed_coin_amount'], 0.1)

    def test_open_timeout_does_not_claim_opposite_position(self):
        api = self._api()
        api.exchange.create_order.side_effect = RequestTimeout('超时')
        api.exchange.fetch_positions.side_effect = [
            [],
            [{'contracts': 10, 'side': 'short', 'symbol': 'BTC/USDT:USDT'}],
        ]
        with patch.object(okx_api, 'time', Mock(sleep=lambda s: None)):
            self.assertIsNone(api.open_position('BTC/USDT:USDT', 'long', 0.1))

    def test_close_timeout_confirm_returns_coins(self):
        api = self._api()
        api.exchange.create_order.side_effect = RequestTimeout('超时')
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 10, 'symbol': 'BTC/USDT:USDT'}],    # 平仓前：持仓 10 张
            [], [],                                            # 超时后连续确认：已平
        ]
        with patch.object(okx_api, 'time', Mock(sleep=lambda s: None)):
            order = api.close_position('BTC/USDT:USDT', 'long', 0.1)
        self.assertEqual(order['id'], 'timeout_confirmed')
        self.assertAlmostEqual(order['amount'], 0.1)


class CancelVerifyRecheckTest(unittest.TestCase):
    """验证复查：交易所列表可能滞后于撤单生效，首查仍在须复查一次再裁决。"""

    def test_laggy_list_recheck_confirms_success(self):
        """撤单已生效但首次复核列表仍显示该单：复查确认消失 → 成功，不误报残留。"""
        api = _bare_api()
        api.exchange.cancel_order.return_value = {'id': 'stop-1'}
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _phased_algo_stub(
            [[_native_stop()], []])  # 首轮仍在列表，复查轮已消失
        self.assertTrue(api._cancel_algo_order('BTC/USDT:USDT', 'stop-1'))

    def test_still_listed_after_recheck_is_failure(self):
        """复查后仍在列表：确实没撤掉 → 失败（残留标记生效）。"""
        api = _bare_api()
        api.exchange.cancel_order.return_value = {'id': 'stop-1'}
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop()]})
        self.assertFalse(api._cancel_algo_order('BTC/USDT:USDT', 'stop-1'))


class FetchAlgoNativeTest(unittest.TestCase):
    """原生端点查询：单一权威来源，信封校验 + 全 ordType 覆盖 + 任一失败即抛。"""

    def test_queries_all_ord_types_and_merges(self):
        """conditional 与 trigger 各有一单：全类型都要问到、结果合并返回。"""
        api = _bare_api()
        seen_types = []

        def record(params):
            seen_types.append(params.get('ordType'))
            return _algo_stub({
                'conditional': [_native_stop()],
                'trigger': [{'algoId': 'manual-1', 'side': 'buy', 'sz': '2',
                             'triggerPx': '60000', 'ordType': 'trigger'}],
            })(params)

        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = record
        orders = api._fetch_algo_orders('BTC/USDT:USDT')
        self.assertEqual(sorted(o['id'] for o in orders), ['manual-1', 'stop-1'])
        self.assertEqual(tuple(seen_types), OkxApi.ALGO_ORDER_TYPES)

    def test_bad_envelope_raises(self):
        """交易所返回非成功信封（code!='0' / data 非数组）：必须抛出，绝不当空清单。"""
        api = _bare_api()
        for bad in ({'code': '1', 'msg': 'err', 'data': []},
                    {'code': '0', 'data': None},
                    'not-a-dict'):
            api.exchange.privateGetTradeOrdersAlgoPending.side_effect = None
            api.exchange.privateGetTradeOrdersAlgoPending.return_value = bad
            with self.assertRaises(RuntimeError, msg=f"信封 {bad!r} 应抛出"):
                api._fetch_algo_orders('BTC/USDT:USDT')

    def test_any_ord_type_failure_raises(self):
        """任一 ordType 查询失败即整体抛出：不完整清单不得用于「不存在」裁决——
        这正是被本实现消除的旧盲区（某组合成功但空 → 误判已撤干净）。"""
        api = _bare_api()

        def fail_on_trigger(params):
            if params.get('ordType') == 'trigger':
                raise RuntimeError('该类型查询失败')
            return {'code': '0', 'msg': '', 'data': []}

        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = fail_on_trigger
        with self.assertRaises(RuntimeError):
            api._fetch_algo_orders('BTC/USDT:USDT')

    def test_native_fields_feed_matcher(self):
        """原生响应字段（algoId/side/sz/slTriggerPx）解析后可直接喂给严格匹配器。"""
        api = _bare_api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop(sz='25', px='98.5')]})
        orders = api._fetch_algo_orders('BTC/USDT:USDT')
        self.assertEqual(len(orders), 1)
        self.assertTrue(OkxApi._algo_order_matches(orders[0], 'sell', 98.5, 25.0))
        self.assertTrue(orders[0]['reduceOnly'])

    def test_inst_id_derivation(self):
        """内部符号与 ccxt 符号都归一到 OKX instId 命名规则。"""
        self.assertEqual(OkxApi._to_inst_id('BTC/USDT:USDT'), 'BTC-USDT-SWAP')
        api = _bare_api()
        seen = []

        def record(params):
            seen.append(params.get('instId'))
            return {'code': '0', 'msg': '', 'data': []}

        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = record
        api._fetch_algo_orders('DOGEUSDT')  # 内部符号也须先归一再变换
        self.assertEqual(set(seen), {'DOGE-USDT-SWAP'})


class CancelOrderFallbackReverifyTest(unittest.TestCase):
    def test_normal_cancel_success_but_algo_still_listed_is_failure(self):
        """普通撤单 fallback 返回成功，但算法单列表仍有该 id：必须返回 False。"""
        api = _bare_api()
        api.exchange.privatePostTradeCancelAlgos.side_effect = RuntimeError('算法单撤销失败')
        api.exchange.cancel_order.return_value = {'id': 'stop-1'}  # 普通撤单"成功"
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop()]})

        self.assertFalse(api.cancel_order('BTC/USDT:USDT', 'stop-1'))

    def test_normal_cancel_success_and_absent_is_success(self):
        """算法单路径确认失败（单仍在列表）→ 普通撤单成功 → 复验已消失 → True。"""
        api = _bare_api()
        api.exchange.privatePostTradeCancelAlgos.side_effect = RuntimeError('算法单撤销失败')
        api.exchange.cancel_order.return_value = {'id': 'stop-1'}
        # 轮次：算法单路径首查在 + 复查在（不可确认）→ 普通撤单成功 → 复验已消失
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _phased_algo_stub(
            [[_native_stop()], [_native_stop()], []])

        self.assertTrue(api.cancel_order('BTC/USDT:USDT', 'stop-1'))


class TickAlignmentTest(unittest.TestCase):
    """触发价 tick 对齐：实盘验证实证 OKX 会把非对齐触发价取整存储（39.384→39.38），
    发单前与比对前都必须用交易所元数据对齐，否则严格匹配误判 mismatch/重复挂单。"""

    def _api(self):
        api = _bare_api()
        api.margin_mode = 'cross'
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api._leverage_done = {'BTC/USDT:USDT'}
        api.exchange.amount_to_precision.return_value = '10'
        api.exchange.price_to_precision = lambda s, p: f'{float(p):.2f}'  # tick=0.01
        return api

    def test_create_stop_sends_aligned_price(self):
        """发单前对齐：发送值与交易所存储值必然一致，超时确认匹配不再受取整干扰。"""
        api = self._api()
        api.exchange.create_order.return_value = {'id': 'stop-1'}
        with patch.object(api, '_fetch_algo_orders', return_value=[{
                'id': 'stop-1', 'side': 'sell', 'reduceOnly': True,
                'info': {'slTriggerPx': '55000.38', 'sz': '10'}}]):
            api.create_stop_loss_order('BTC/USDT:USDT', 'long', 0.1, 55000.384)
        _args, _kwargs = api.exchange.create_order.call_args
        params = _args[5]
        self.assertEqual(params['stopLossPrice'], 55000.38)

    def test_find_stop_intact_with_unaligned_local_price(self):
        """本地记录 39.384、交易所存储 39.38：比对前同一函数对齐 → intact，不误判 mismatch。"""
        api = self._api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop(sz='10', px='55000.38')]})
        self.assertEqual(
            api.find_stop_order_state('BTCUSDT', 'long', 0.1, 55000.384, 'stop-1'), 'intact')

    def test_align_falls_back_to_raw_when_precision_unavailable(self):
        """精度元数据不可得：按原价返回（fail-safe，不阻断创建/比对）。"""
        api = _bare_api()
        api.exchange.price_to_precision = Mock(side_effect=RuntimeError('markets 未加载'))
        self.assertEqual(api._align_stop_price('BTC/USDT:USDT', 39.384), 39.384)


class FindStopOrderStateTest(unittest.TestCase):
    """止损存在性三态判定：intact 必须方向+触发价+张数严格一致；id 在但内容不符返回 mismatch。"""

    def _api(self):
        api = _bare_api()
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api.exchange.amount_to_precision.return_value = '10'  # 0.1 币 / 0.01 面值 = 10 张
        return api

    def test_strict_match_is_intact(self):
        api = self._api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop(sz='10', px='55000')]})
        self.assertEqual(api.find_stop_order_state('BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'), 'intact')

    def test_same_price_wrong_size_not_intact(self):
        """Codex 场景：同方向同触发价但张数只有一半（人工改挂）→ 不算 intact。"""
        api = self._api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop(algo_id='other', sz='5', px='55000')]})
        self.assertEqual(api.find_stop_order_state('BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'), 'mismatch')

    def test_exact_stop_plus_extra_order_is_mismatch(self):
        api = self._api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [
                _native_stop(sz='10', px='55000'),
                _native_stop(algo_id='extra', sz='10', px='50000'),
            ]})
        self.assertEqual(
            api.find_stop_order_state('BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'),
            'mismatch')

    def test_id_present_but_content_differs_is_mismatch(self):
        api = self._api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop(sz='5', px='50000')]})
        self.assertEqual(api.find_stop_order_state('BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'), 'mismatch')

    def test_empty_list_is_missing(self):
        api = self._api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({})
        self.assertEqual(api.find_stop_order_state('BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'), 'missing')

    def test_contract_size_failure_propagates(self):
        """面值不可得：fail-closed 异常向上传播，调用方按 fail-safe 跳过本轮。"""
        api = _bare_api()
        api.exchange.market.side_effect = RuntimeError('网络错误')
        with self.assertRaises(ContractSizeUnavailable):
            api.find_stop_order_state('BTCUSDT', 'long', 0.1, 55000.0)


class StopOrderMatchTest(unittest.TestCase):
    GOOD = {'side': 'sell', 'amount': 25.0, 'stopLossPrice': 98.5,
            'reduceOnly': True, 'info': {}}

    def test_full_match(self):
        self.assertTrue(OkxApi._algo_order_matches(self.GOOD, 'sell', 98.5, 25.0))

    def test_wrong_trigger_price_is_old_order(self):
        """触发价不同（残留旧止损）→ 不匹配，防止误认。"""
        old = dict(self.GOOD, stopLossPrice=95.0)
        self.assertFalse(OkxApi._algo_order_matches(old, 'sell', 98.5, 25.0))

    def test_wrong_contracts_rejected(self):
        self.assertFalse(OkxApi._algo_order_matches(dict(self.GOOD, amount=10.0), 'sell', 98.5, 25.0))

    def test_wrong_side_rejected(self):
        self.assertFalse(OkxApi._algo_order_matches(dict(self.GOOD, side='buy'), 'sell', 98.5, 25.0))

    def test_non_reduce_only_rejected(self):
        self.assertFalse(OkxApi._algo_order_matches(
            dict(self.GOOD, reduceOnly=False), 'sell', 98.5, 25.0))

    def test_unreadable_fields_rejected(self):
        """字段读不到一律视为不匹配（宁可重试创建，不误认）。"""
        self.assertFalse(OkxApi._algo_order_matches({'side': 'sell', 'info': {}}, 'sell', 98.5, 25.0))

    def test_nonfinite_fields_rejected(self):
        self.assertFalse(OkxApi._algo_order_matches(
            dict(self.GOOD, stopLossPrice=float('nan')), 'sell', 98.5, 25.0))
        self.assertFalse(OkxApi._algo_order_matches(
            dict(self.GOOD, amount=float('inf')), 'sell', 98.5, 25.0))
        self.assertFalse(OkxApi._algo_order_matches(
            self.GOOD, 'sell', 98.5, float('nan')))

    def test_trigger_from_okx_info_field(self):
        o = {'side': 'sell', 'reduceOnly': True,
             'info': {'slTriggerPx': '98.5', 'sz': '25'}}
        self.assertTrue(OkxApi._algo_order_matches(o, 'sell', 98.5, 25.0))


class CancelAllOrdersVerificationTest(unittest.TestCase):
    def _api(self):
        api = _bare_api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({})
        return api

    def test_command_success_but_normal_order_still_listed_is_failure(self):
        api = self._api()
        pending = [{'id': 'limit-1'}]
        api.exchange.fetch_open_orders.side_effect = [pending, pending]

        self.assertFalse(api.cancel_all_orders('BTC/USDT:USDT'))
        api.exchange.cancel_order.assert_called_once_with(
            'limit-1', 'BTC/USDT:USDT')

    def test_normal_and_algo_lists_confirmed_empty_is_success(self):
        api = self._api()
        api.exchange.fetch_open_orders.return_value = []

        self.assertTrue(api.cancel_all_orders('BTC/USDT:USDT'))

    def test_remaining_normal_order_removed_and_rechecked_is_success(self):
        api = self._api()
        api.exchange.fetch_open_orders.side_effect = [
            [{'id': 'limit-1'}], [],
        ]

        self.assertTrue(api.cancel_all_orders('BTC/USDT:USDT'))
        api.exchange.cancel_order.assert_called_once_with(
            'limit-1', 'BTC/USDT:USDT')

    def test_late_algorithm_order_in_final_recheck_is_failure(self):
        api = self._api()
        api.exchange.fetch_open_orders.return_value = []
        late = {'id': 'late-stop'}
        with patch.object(api, '_fetch_algo_orders', side_effect=[[], [late]]):
            self.assertFalse(api.cancel_all_orders('BTC/USDT:USDT'))


class MarginModeValidationTest(unittest.TestCase):
    """margin_mode 启动即校验（fail-loud）：该值直接进入每一笔订单的 tdMode，
    写错不会在启动时暴露，而是让全部下单在盘中被交易所逐笔拒绝。"""

    @staticmethod
    def _cfg(mode):
        cfg = {'apiKey': 'k', 'secret': 's', 'password': 'p'}
        if mode is not None:
            cfg['margin_mode'] = mode
        return cfg

    def test_invalid_margin_mode_rejected_at_startup(self):
        for bad in ('corss', 'both', 'margin', 5, ['cross']):
            with self.assertRaises(ValueError, msg=repr(bad)):
                OkxApi(self._cfg(bad))

    def test_valid_and_default_modes_accepted(self):
        exchange = _ccxt.okx.return_value
        exchange.load_markets.return_value = {}
        exchange.privateGetAccountConfig.return_value = {
            'data': [{'posMode': 'net_mode'}]}
        for mode, expect in (('cross', 'cross'), ('isolated', 'isolated'),
                             (' CROSS ', 'cross'), (None, 'cross'), ('', 'cross')):
            api = OkxApi(self._cfg(mode))
            self.assertEqual(api.margin_mode, expect, msg=repr(mode))


class PositionModeStartupGateTest(unittest.TestCase):
    def test_setting_failure_is_allowed_only_when_query_proves_net_mode(self):
        api = _bare_api()
        api.exchange.set_position_mode.side_effect = RuntimeError('已有持仓')
        api.exchange.privateGetAccountConfig.return_value = {
            'data': [{'posMode': 'net_mode'}]}

        api._ensure_one_way_mode()

    def test_non_net_mode_refuses_startup(self):
        api = _bare_api()
        api.exchange.privateGetAccountConfig.return_value = {
            'data': [{'posMode': 'long_short_mode'}]}

        with self.assertRaisesRegex(RuntimeError, '只支持单向净持仓'):
            api._ensure_one_way_mode()

    def test_unverifiable_mode_refuses_startup(self):
        api = _bare_api()
        api.exchange.privateGetAccountConfig.side_effect = RuntimeError('网络失败')

        with self.assertRaisesRegex(RuntimeError, '无法验证 OKX 持仓模式'):
            api._ensure_one_way_mode()


class ListPositionSymbolsFilterTest(unittest.TestCase):
    """孤儿仓核对的数据源必须只含 U 本位永续：OKX 的 SWAP 持仓查询会带出币本位
    （BTC/USD:BTC），to_internal_symbol 会把它错映射成 BTCUSDT——人工币本位仓会被
    误报成孤儿、或恰有同名 U 本位托管仓时把币本位仓错当已接管。"""

    def test_only_usdt_settled_positions_are_listed(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [
            {'contracts': 10, 'symbol': 'BTC/USDT:USDT'},
            {'contracts': 5, 'symbol': 'BTC/USD:BTC'},     # 币本位：必须被过滤
            {'contracts': 0, 'symbol': 'ETH/USDT:USDT'},   # 空仓条目：不计
            {'contracts': 2, 'symbol': 'DOGE/USDT:USDT'},
            {'contracts': 3},                              # 缺 symbol 字段：不计
            None,                                          # 脏条目：不计
        ]
        self.assertEqual(sorted(api.list_position_symbols()), ['BTCUSDT', 'DOGEUSDT'])

    def test_empty_positions_returns_empty(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = None
        self.assertEqual(api.list_position_symbols(), [])


if __name__ == '__main__':
    unittest.main()
