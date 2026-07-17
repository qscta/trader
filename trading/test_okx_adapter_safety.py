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
from okx_api import OkxApi, ContractSizeUnavailable, PositionModeError
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
    api.ORDER_CONFIRM_DELAY = 0
    api.ORDER_CONFIRM_ATTEMPTS = 3
    api.STOP_CONFIRM_DELAY = 0
    api.STOP_CONFIRM_ATTEMPTS = 3
    api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({})
    api.exchange.privateGetTradeOrdersPending.return_value = {
        'code': '0', 'msg': '', 'data': []}
    api.exchange.privateGetTradeOrder.return_value = {
        'code': '51603', 'msg': 'Order does not exist', 'data': []}
    api.exchange.fetch_positions.return_value = []
    return api


def _native_stop(algo_id='stop-1', side='sell', sz='10', px='55000',
                 client_id='', state='live', trigger_px_type='last',
                 pos_side='net'):
    """OKX orders-algo-pending 原生响应里的一条 conditional 止损单。"""
    return {'algoId': algo_id, 'algoClOrdId': client_id, 'side': side,
            'sz': sz, 'slTriggerPx': px, 'slOrdPx': '-1',
            'ordType': 'conditional', 'reduceOnly': 'true',
            'state': state, 'slTriggerPxType': trigger_px_type,
            'posSide': pos_side}


def _native_normal(order_id='order-1', side='buy', size='10'):
    return {
        'ordId': order_id, 'clOrdId': '', 'side': side, 'sz': size,
        'ordType': 'limit', 'reduceOnly': 'false', 'state': 'live',
    }


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


class MarginModeValidationTest(unittest.TestCase):
    @staticmethod
    def _config(mode):
        config = {'apiKey': 'k', 'secret': 's', 'password': 'p'}
        if mode is not None:
            config['margin_mode'] = mode
        return config

    def test_invalid_margin_mode_is_rejected_before_trading(self):
        for bad in ('corss', 'both', 5, ['cross']):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                OkxApi(self._config(bad))

    def test_valid_and_default_margin_modes_are_normalized(self):
        with patch.object(OkxApi, '_load_market_cache'), patch.object(
                OkxApi, '_ensure_one_way_mode'):
            for value, expected in (
                    ('cross', 'cross'), (' ISOLATED ', 'isolated'),
                    (None, 'cross'), ('', 'cross')):
                with self.subTest(value=value):
                    self.assertEqual(
                        expected, OkxApi(self._config(value)).margin_mode)


class ContractSizeFailClosedTest(unittest.TestCase):
    def test_missing_contract_size_raises(self):
        """市场数据缺 contractSize：必须抛异常，不允许默认 1.0。"""
        api = _bare_api()
        api.exchange.market.return_value = {'contractSize': None}
        with self.assertRaises(ContractSizeUnavailable):
            api._get_contract_size('BTC/USDT:USDT')

    def test_nonfinite_contract_size_raises(self):
        """畸形面值（'1e999'→inf / NaN）不得越过校验进入换算缓存。"""
        for bad in ('1e999', float('inf'), float('nan'), '-0.01'):
            api = _bare_api()
            api.exchange.market.return_value = {'contractSize': bad}
            with self.subTest(bad=bad), \
                    self.assertRaises(ContractSizeUnavailable):
                api._get_contract_size('BTC/USDT:USDT')
            self.assertNotIn('BTC/USDT:USDT', api._contract_size_cache)

    def test_market_query_failure_raises(self):
        api = _bare_api()
        api.exchange.market.side_effect = RuntimeError('网络错误')
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

    def test_coin_contract_round_trip_never_loses_exact_contract(self):
        """回归：49*0.0001 再除回面值不得被 ccxt TRUNCATE 成 48 张。"""
        api = _bare_api()
        api._contract_size_cache['BTC/USDT:USDT'] = 0.0001
        api._amount_precision_cache['BTC/USDT:USDT'] = 0
        api.exchange.amount_to_precision.side_effect = lambda _s, value: str(int(value))
        coins = 49 * 0.0001
        self.assertEqual(api._coin_to_contracts('BTC/USDT:USDT', coins), 49.0)

    def test_decimal_fix_does_not_round_genuine_fraction_up(self):
        api = _bare_api()
        api._contract_size_cache['BTC/USDT:USDT'] = 1.0
        api._amount_precision_cache['BTC/USDT:USDT'] = 0
        api.exchange.amount_to_precision.side_effect = lambda _s, value: str(int(value))
        self.assertEqual(api._coin_to_contracts('BTC/USDT:USDT', 0.9999995), 0.0)

    def test_position_symbol_listing_filters_coin_margined_contracts(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [
            {'contracts': 10, 'symbol': 'BTC/USDT:USDT'},
            {'contracts': 5, 'symbol': 'BTC/USD:BTC'},
            {'contracts': 0, 'symbol': 'ETH/USDT:USDT'},
            {'contracts': 2, 'symbol': 'DOGE/USDT:USDT'},
            None,
        ]
        self.assertEqual(
            ['BTCUSDT', 'DOGEUSDT'], sorted(api.list_position_symbols()))


class PositionModeFailClosedTest(unittest.TestCase):
    def test_set_failure_is_allowed_only_when_readback_proves_net_mode(self):
        api = _bare_api()
        api.exchange.set_position_mode.side_effect = RuntimeError('already configured')
        api.exchange.fetch_position_mode.return_value = {
            'hedged': False, 'info': {'posMode': 'net_mode'}}
        api._ensure_one_way_mode()  # 不抛出

    def test_hedged_or_unreadable_mode_rejects_startup(self):
        api = _bare_api()
        api.exchange.fetch_position_mode.return_value = {
            'hedged': True, 'info': {'posMode': 'long_short_mode'}}
        with self.assertRaises(PositionModeError):
            api._ensure_one_way_mode()

        api.exchange.fetch_position_mode.side_effect = RuntimeError('network')
        with self.assertRaises(PositionModeError):
            api._ensure_one_way_mode()

    def test_get_position_refuses_to_hide_two_legs(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [
            {'contracts': 2, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {}},
            {'contracts': 3, 'side': 'short', 'symbol': 'BTC/USDT:USDT', 'info': {}},
        ]
        with self.assertRaises(PositionModeError):
            api.get_position('BTC/USDT:USDT')

    def test_get_position_rejects_explicit_hedge_leg(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [
            {'contracts': 2, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'long'}},
        ]
        with self.assertRaises(PositionModeError):
            api.get_position('BTC/USDT:USDT')

    def test_get_position_rejects_nonzero_position_with_unknown_direction(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [
            {'contracts': 2, 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}},
        ]
        with self.assertRaises(PositionModeError):
            api.get_position('BTC/USDT:USDT')


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
        api.exchange.fetch_order.return_value = {
            'id': 'timeout-open', 'status': 'closed', 'filled': 10, 'average': None}
        api.exchange.fetch_positions.side_effect = [
            [],                                                # 开仓前：无仓
            [],                                                # 挂单预检后：仍无仓
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT'}],  # 已成交 10 张
        ]
        with patch.object(okx_api, 'time', Mock(sleep=lambda s: None)):
            order = api.open_position('BTC/USDT:USDT', 'long', 0.1)
        self.assertEqual(order['id'], 'timeout-open')
        self.assertAlmostEqual(order['amount'], 0.1)  # 10 张 × 0.01 = 0.1 币

    def test_close_timeout_confirm_returns_coins(self):
        api = self._api()
        api.exchange.create_order.side_effect = RequestTimeout('超时')
        api.exchange.fetch_order.return_value = {
            'id': 'timeout-close', 'status': 'closed', 'filled': 10, 'average': None}
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT'}],  # 平仓前 10 张
            [],                                                # 超时后确认：已平
        ]
        with patch.object(okx_api, 'time', Mock(sleep=lambda s: None)):
            order = api.close_position('BTC/USDT:USDT', 'long', 0.1)
        self.assertEqual(order['id'], 'timeout-close')
        self.assertAlmostEqual(order['amount'], 0.1)


class MarketOrderConfirmationTest(unittest.TestCase):
    def _api(self):
        api = _bare_api()
        api.margin_mode = 'cross'
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api._amount_precision_cache['BTC/USDT:USDT'] = 0
        api._leverage_done = {'BTC/USDT:USDT'}
        api.exchange.amount_to_precision.side_effect = lambda _s, value: str(int(value))
        return api

    def test_ack_is_polled_and_actual_fill_fields_are_preserved(self):
        api = self._api()
        api.exchange.create_order.return_value = {'id': 'ord-1'}  # 仅 ACK
        api.exchange.fetch_order.return_value = {
            'id': 'ord-1', 'status': 'closed', 'filled': 10,
            'average': 101.5, 'cost': 1015, 'fee': {'cost': 0.42, 'currency': 'USDT'},
        }
        api.exchange.fetch_positions.side_effect = [
            [],
            [],
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT',
              'info': {'posSide': 'net'}}],
        ]

        order = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertTrue(order['confirmed'])
        self.assertTrue(order['fully_filled'])
        self.assertAlmostEqual(order['amount'], 0.1)
        self.assertEqual(order['average'], 101.5)
        self.assertEqual(order['fee']['cost'], 0.42)
        api.exchange.fetch_order.assert_called_once_with('ord-1', 'BTC/USDT:USDT')

    def test_open_preflight_query_failure_or_existing_position_blocks_order(self):
        api = self._api()
        api.exchange.fetch_positions.side_effect = RuntimeError('unavailable')
        self.assertIsNone(api.open_position('BTC/USDT:USDT', 'long', 0.1))
        api.exchange.create_order.assert_not_called()

        api.exchange.fetch_positions.side_effect = None
        api.exchange.fetch_positions.return_value = [
            {'contracts': 2, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}]
        self.assertIsNone(api.open_position('BTC/USDT:USDT', 'long', 0.1))
        api.exchange.create_order.assert_not_called()

    def test_terminal_partial_open_returns_only_actual_coin_amount(self):
        api = self._api()
        api.exchange.create_order.return_value = {'id': 'ord-partial'}
        api.exchange.fetch_order.return_value = {
            'id': 'ord-partial', 'status': 'canceled', 'filled': 4, 'average': 99}
        api.exchange.fetch_positions.side_effect = [
            [],
            [],
            [{'contracts': 4, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
        ]

        order = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertFalse(order['fully_filled'])
        self.assertAlmostEqual(order['amount'], 0.04)

    def test_live_partial_open_is_cancelled_before_accepting_actual_amount(self):
        api = self._api()
        api.exchange.create_order.return_value = {'id': 'ord-live'}
        live = {'id': 'ord-live', 'status': 'open', 'filled': 4, 'average': 99}
        terminal = {'id': 'ord-live', 'status': 'canceled', 'filled': 4, 'average': 99}
        api.exchange.fetch_order.side_effect = [live, live, live, terminal]
        partial_position = [
            {'contracts': 4, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}]
        api.exchange.fetch_positions.side_effect = [
            [], [], partial_position, partial_position, partial_position, partial_position,
        ]

        order = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertFalse(order['fully_filled'])
        self.assertAlmostEqual(order['amount'], 0.04)
        api.exchange.cancel_order.assert_called_once_with('ord-live', 'BTC/USDT:USDT')

    def test_live_full_position_is_not_confirmed_until_order_becomes_terminal(self):
        api = self._api()
        api.exchange.create_order.return_value = {'id': 'ord-live-full'}
        live = {'id': 'ord-live-full', 'status': 'open', 'filled': 10, 'average': 100}
        terminal = {'id': 'ord-live-full', 'status': 'closed', 'filled': 10, 'average': 100}
        api.exchange.fetch_order.side_effect = [live, live, live, terminal]
        full_position = [
            {'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}]
        api.exchange.fetch_positions.side_effect = [
            [], [], full_position, full_position, full_position, full_position,
        ]

        order = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertTrue(order['confirmed'])
        self.assertFalse(order.get('execution_ambiguous', False))
        self.assertEqual(api.exchange.fetch_order.call_count, 4)
        api.exchange.cancel_order.assert_called_once_with(
            'ord-live-full', 'BTC/USDT:USDT')

    def test_partial_close_is_explicit_and_does_not_claim_flat(self):
        api = self._api()
        api.MAX_CLOSE_LEGS = 1
        api.exchange.create_order.return_value = {'id': 'close-partial'}
        api.exchange.fetch_order.return_value = {
            'id': 'close-partial', 'status': 'canceled', 'filled': 6, 'average': 102}
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
            [{'contracts': 4, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
        ]

        order = api.close_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertFalse(order['fully_closed'])
        self.assertAlmostEqual(order['amount'], 0.06)
        self.assertAlmostEqual(order['remaining_amount'], 0.04)

    def test_partial_close_is_supplemented_and_execution_is_aggregated(self):
        api = self._api()
        api.exchange.create_order.side_effect = [
            {'id': 'close-1'}, {'id': 'close-2'},
        ]
        api.exchange.fetch_order.side_effect = [
            {'id': 'close-1', 'status': 'canceled', 'filled': 6,
             'average': 100, 'cost': 600,
             'fee': {'cost': 0.1, 'currency': 'USDT'}},
            {'id': 'close-2', 'status': 'closed', 'filled': 4,
             'average': 110, 'cost': 440,
             'fee': {'cost': 0.2, 'currency': 'USDT'}},
        ]
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
            [{'contracts': 4, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
            [],
        ]

        order = api.close_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertTrue(order['fully_closed'])
        self.assertEqual(order['ids'], ['close-1', 'close-2'])
        self.assertAlmostEqual(order['amount'], 0.1)
        self.assertAlmostEqual(order['average'], 104.0)
        self.assertAlmostEqual(order['cost'], 1040.0)
        self.assertAlmostEqual(order['fee']['cost'], 0.3)
        self.assertEqual(api.exchange.create_order.call_count, 2)

    def test_concurrent_external_close_does_not_misattribute_order_price_or_fee(self):
        api = self._api()
        api.MAX_CLOSE_LEGS = 1
        api.exchange.create_order.return_value = {'id': 'close-race'}
        api.exchange.fetch_order.return_value = {
            'id': 'close-race', 'status': 'closed', 'filled': 6,
            'average': 100, 'fee': {'cost': 0.1, 'currency': 'USDT'},
        }
        # 订单仅报 6 张，但仓位从 10 直接归零：其余 4 张可能由止损/人工平掉。
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}], []]

        order = api.close_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertTrue(order['fully_closed'])
        self.assertTrue(order['execution_ambiguous'])
        self.assertAlmostEqual(order['amount'], 0.1)
        self.assertIsNone(order.get('average'))
        self.assertIsNone(order.get('fee'))

    def test_deterministic_client_id_reuses_existing_order_without_create(self):
        api = self._api()
        existing = {
            'id': 'ord-existing', 'symbol': 'BTC/USDT:USDT',
            'type': 'market', 'side': 'buy', 'amount': 10, 'reduceOnly': False,
            'status': 'closed', 'filled': 10,
            'average': 100, 'fee': {'cost': 0.1, 'currency': 'USDT'},
        }
        api.exchange.fetch_order.side_effect = [existing, existing]
        api.exchange.fetch_positions.return_value = [
            {'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}]

        order = api.open_position(
            'BTC/USDT:USDT', 'long', 0.1,
            client_order_id='btcOrder20260710')

        self.assertTrue(order['confirmed'])
        self.assertEqual(order['clientOrderId'], 'btcOrder20260710')
        api.exchange.create_order.assert_not_called()

    def test_recovered_zero_fill_order_does_not_claim_or_close_manual_position(self):
        api = self._api()
        existing = {
            'id': 'ord-zero', 'symbol': 'BTC/USDT:USDT',
            'type': 'market', 'side': 'buy', 'amount': 10,
            'reduceOnly': False, 'status': 'canceled', 'filled': 0,
        }
        api.exchange.fetch_order.return_value = existing
        api.exchange.fetch_positions.return_value = [
            {'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}]

        order = api.open_position(
            'BTC/USDT:USDT', 'long', 0.1,
            client_order_id='btcRecoveredZeroFill')

        self.assertTrue(order['open_execution_attribution_ambiguous'])
        self.assertEqual(order['amount'], 0.0)
        self.assertEqual(order['observed_position_amount'], 0.1)
        api.exchange.create_order.assert_not_called()

    def test_recovered_zero_fill_does_not_compensate_partial_manual_position(self):
        api = self._api()
        existing = {
            'id': 'ord-zero-partial', 'symbol': 'BTC/USDT:USDT',
            'type': 'market', 'side': 'buy', 'amount': 10,
            'reduceOnly': False, 'status': 'canceled', 'filled': 0,
        }
        api.exchange.fetch_order.return_value = existing
        api.exchange.fetch_positions.return_value = [
            {'contracts': 5, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}]

        order = api.open_position(
            'BTC/USDT:USDT', 'long', 0.1,
            client_order_id='btcRecoveredPartialManual')

        self.assertTrue(order['open_execution_attribution_ambiguous'])
        self.assertEqual(order['observed_position_amount'], 0.05)
        api.exchange.cancel_order.assert_not_called()

    def test_recovered_terminal_without_filled_never_claims_current_position(self):
        api = self._api()
        existing = {
            'id': 'ord-no-filled', 'symbol': 'BTC/USDT:USDT',
            'type': 'market', 'side': 'buy', 'amount': 10,
            'reduceOnly': False, 'status': 'closed', 'filled': None,
        }
        api.exchange.fetch_order.return_value = existing
        api.exchange.fetch_positions.return_value = [
            {'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}]

        order = api.open_position(
            'BTC/USDT:USDT', 'long', 0.1,
            client_order_id='btcRecoveredNoFilled')

        self.assertTrue(order['open_execution_attribution_ambiguous'])
        self.assertEqual(order['amount'], 0.0)

    def test_client_id_collision_mismatch_fails_closed(self):
        valid = {
            'id': 'collision', 'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'buy', 'amount': 10, 'reduceOnly': False,
            'status': 'closed', 'filled': 10,
        }
        for mutation in (
                {'symbol': 'ETH/USDT:USDT'}, {'type': 'limit'},
                {'side': 'sell'}, {'amount': 9}, {'reduceOnly': True}):
            api = self._api()
            api.exchange.fetch_order.return_value = dict(valid, **mutation)
            result = api.open_position(
                'BTC/USDT:USDT', 'long', 0.1,
                client_order_id='btcOrderCollision')
            self.assertIsNone(result, mutation)
            api.exchange.create_order.assert_not_called()
            api.exchange.fetch_positions.assert_not_called()

    def test_invalid_client_id_fails_closed(self):
        api = self._api()
        self.assertIsNone(api.open_position(
            'BTC/USDT:USDT', 'long', 0.1, client_order_id='bad-id-with-dashes'))
        api.exchange.create_order.assert_not_called()

    def test_deterministic_close_id_recovers_finished_order_without_recreate(self):
        api = self._api()
        existing = {
            'id': 'close-existing', 'symbol': 'BTC/USDT:USDT',
            'type': 'market', 'side': 'sell', 'amount': 10, 'reduceOnly': True,
            'status': 'closed', 'filled': 10, 'average': 105,
            'fee': {'cost': 0.15, 'currency': 'USDT'},
        }
        api.exchange.fetch_order.side_effect = [existing, existing]
        api.exchange.fetch_positions.side_effect = [[], []]

        order = api.close_position(
            'BTC/USDT:USDT', 'long', 0.1,
            client_order_id='btcClose20260710')

        self.assertTrue(order['fully_closed'])
        self.assertAlmostEqual(order['amount'], 0.1)
        self.assertEqual(order['average'], 105)
        self.assertAlmostEqual(order['fee']['cost'], 0.15)
        api.exchange.create_order.assert_not_called()

    def test_close_recovery_aggregates_base_and_retry_leg_without_recreate(self):
        api = self._api()
        base_id = 'CloseIntentABC'
        retry_id = f'{base_id}r1'
        api.exchange.fetch_order.side_effect = [
            {
                'id': 'close-base', 'clientOrderId': base_id,
                'symbol': 'BTC/USDT:USDT', 'type': 'market',
                'side': 'sell', 'amount': 10, 'reduceOnly': True,
                'status': 'canceled', 'filled': 6, 'average': 100,
                'fee': {'cost': 0.1, 'currency': 'USDT'},
            },
            {
                'id': 'close-r1', 'clientOrderId': retry_id,
                'symbol': 'BTC/USDT:USDT', 'type': 'market',
                'side': 'sell', 'amount': 4, 'reduceOnly': True,
                'status': 'closed', 'filled': 4, 'average': 110,
                'fee': {'cost': 0.2, 'currency': 'USDT'},
            },
        ]
        api.exchange.fetch_positions.return_value = []

        order = api.close_position(
            'BTC/USDT:USDT', 'long', 0.1,
            client_order_id=base_id)

        self.assertTrue(order['fully_closed'])
        self.assertEqual(['close-base', 'close-r1'], order['ids'])
        self.assertEqual([base_id, retry_id], order['clientOrderIds'])
        self.assertAlmostEqual(104.0, order['average'])
        self.assertAlmostEqual(0.3, order['fee']['cost'])
        api.exchange.create_order.assert_not_called()

    def test_close_recovery_continues_from_next_deterministic_leg(self):
        api = self._api()
        base_id = 'CloseIntentPartial'
        retry_id = f'{base_id}r1'
        base = {
            'id': 'close-base', 'clientOrderId': base_id,
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'sell', 'amount': 10, 'reduceOnly': True,
            'status': 'canceled', 'filled': 6, 'average': 100,
        }
        api.exchange.fetch_order.side_effect = [
            base, OrderNotFound('r1 not submitted'),
            {
                'id': 'close-r1', 'clientOrderId': retry_id,
                'symbol': 'BTC/USDT:USDT', 'type': 'market',
                'side': 'sell', 'amount': 4, 'reduceOnly': True,
                'status': 'closed', 'filled': 4, 'average': 110,
            },
        ]
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 4, 'side': 'long', 'symbol': 'BTC/USDT:USDT',
              'info': {'posSide': 'net'}}],
            [],
        ]
        api.exchange.create_order.return_value = {'id': 'close-r1'}

        order = api.close_position(
            'BTC/USDT:USDT', 'long', 0.1,
            client_order_id=base_id)

        self.assertTrue(order['fully_closed'])
        self.assertEqual(['close-base', 'close-r1'], order['ids'])
        self.assertAlmostEqual(104.0, order['average'])
        api.exchange.create_order.assert_called_once()
        self.assertEqual(
            retry_id,
            api.exchange.create_order.call_args.args[-1]['clOrdId'])

    def test_close_recovery_mismatch_with_live_residual_fails_closed(self):
        api = self._api()
        base_id = 'CloseIntentMismatch'
        api.exchange.fetch_order.side_effect = [
            {
                'id': 'close-base', 'clientOrderId': base_id,
                'symbol': 'BTC/USDT:USDT', 'type': 'market',
                'side': 'sell', 'amount': 10, 'reduceOnly': True,
                'status': 'canceled', 'filled': 5, 'average': 100,
            },
            OrderNotFound('r1 not submitted'),
        ]
        api.exchange.fetch_positions.return_value = [
            {'contracts': 4, 'side': 'long', 'symbol': 'BTC/USDT:USDT',
             'info': {'posSide': 'net'}}]

        self.assertIsNone(api.close_position(
            'BTC/USDT:USDT', 'long', 0.1,
            client_order_id=base_id))
        api.exchange.create_order.assert_not_called()

    def test_new_close_intent_refuses_ledger_exchange_size_mismatch(self):
        api = self._api()
        api.exchange.fetch_order.side_effect = OrderNotFound('not submitted')
        api.exchange.fetch_positions.return_value = [
            {'contracts': 8, 'side': 'long', 'symbol': 'BTC/USDT:USDT',
             'info': {'posSide': 'net'}}]

        self.assertIsNone(api.close_position(
            'BTC/USDT:USDT', 'long', 0.1,
            client_order_id='CloseIntentSizeMismatch'))
        api.exchange.create_order.assert_not_called()

    def test_incomplete_internal_compensation_returns_explicit_unresolved_contract(self):
        api = self._api()
        api.get_position = Mock(return_value=None)
        api.exchange.create_order.return_value = {'id': 'open-uncertain'}
        api._confirm_market_order = Mock(side_effect=[
            (None, 10.0), (None, 10.0),
        ])
        api.close_position = Mock(return_value={
            'id': 'close-partial', 'confirmed': True, 'fully_closed': False,
            'amount': 0.06, 'remaining_amount': 0.04,
        })

        result = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertTrue(result['open_execution_unresolved'])
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['status'], 'compensation_incomplete')
        self.assertAlmostEqual(result['remaining_amount'], 0.04)
        self.assertEqual(result['compensation']['id'], 'close-partial')

    def test_full_internal_compensation_returns_explicit_compensated_contract(self):
        api = self._api()
        api.get_position = Mock(return_value=None)
        api.exchange.create_order.return_value = {
            'id': 'open-uncertain', 'average': 100}
        api._confirm_market_order = Mock(side_effect=[
            (None, 10.0), (None, 10.0),
        ])
        api.close_position = Mock(return_value={
            'id': 'close-full', 'confirmed': True, 'fully_closed': True,
            'amount': 0.1, 'remaining_amount': 0.0, 'average': 99,
        })
        api._fetch_order_for_confirmation = Mock(
            return_value={'id': 'open-uncertain', 'status': 'canceled'})

        result = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertTrue(result['open_execution_compensated'])
        self.assertEqual(result['status'], 'compensated_flat')
        self.assertEqual(result['remaining_amount'], 0.0)
        self.assertEqual(result['compensation']['id'], 'close-full')

    def test_full_compensation_keeps_handle_when_original_order_still_live(self):
        api = self._api()
        api.get_position = Mock(return_value=None)
        api.exchange.create_order.return_value = {'id': 'open-live'}
        api._confirm_market_order = Mock(side_effect=[
            (None, 10.0), (None, 10.0),
        ])
        api.close_position = Mock(return_value={
            'id': 'close-full', 'confirmed': True, 'fully_closed': True,
            'amount': 0.1, 'remaining_amount': 0.0,
        })
        api._fetch_order_for_confirmation = Mock(
            return_value={'id': 'open-live', 'status': 'open'})

        result = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertTrue(result['open_execution_unresolved'])
        self.assertTrue(result['open_order_may_remain_live'])
        self.assertFalse(result.get('open_execution_compensated', False))


class ExistingOpenOrderLookupTest(unittest.TestCase):
    CLIENT_ID = 'TpendingLookup123'

    def _api(self):
        api = _bare_api()
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api._amount_precision_cache['BTC/USDT:USDT'] = 0
        api.exchange.amount_to_precision.side_effect = lambda _s, value: str(int(value))
        return api

    @staticmethod
    def _order(**changes):
        order = {
            'id': 'open-existing', 'symbol': 'BTC/USDT:USDT',
            'type': 'market', 'side': 'buy', 'amount': 10,
            'reduceOnly': False, 'status': 'closed', 'filled': 10,
        }
        order.update(changes)
        return order

    def test_strict_existing_order_is_returned_without_writes(self):
        api = self._api()
        api.exchange.fetch_order.return_value = self._order()
        order = api.find_existing_open_order(
            'BTC/USDT:USDT', 'long', 0.1, self.CLIENT_ID)
        self.assertEqual(order['id'], 'open-existing')
        api.exchange.create_order.assert_not_called()

    def test_order_not_found_returns_none(self):
        api = self._api()
        api.exchange.fetch_order.side_effect = OrderNotFound('absent')
        self.assertIsNone(api.find_existing_open_order(
            'BTC/USDT:USDT', 'long', 0.1, self.CLIENT_ID))

    def test_uncertain_query_propagates_and_never_creates(self):
        api = self._api()
        api.exchange.fetch_order.side_effect = RuntimeError('uncertain')
        with self.assertRaises(RuntimeError):
            api.find_existing_open_order(
                'BTC/USDT:USDT', 'long', 0.1, self.CLIENT_ID)
        api.exchange.create_order.assert_not_called()

    def test_mismatched_existing_order_raises(self):
        api = self._api()
        api.exchange.fetch_order.return_value = self._order(side='sell')
        with self.assertRaises(RuntimeError):
            api.find_existing_open_order(
                'BTC/USDT:USDT', 'long', 0.1, self.CLIENT_ID)
        api.exchange.create_order.assert_not_called()


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
        self.assertIn('chase', seen_types)

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

    def test_paginates_past_first_hundred_orders(self):
        api = _bare_api()
        all_items = [
            _native_stop(algo_id=f'stop-{index}') for index in range(101)]
        seen_after = []

        def paged(params):
            if params.get('ordType') != 'conditional':
                return {'code': '0', 'data': []}
            seen_after.append(params.get('after'))
            if params.get('after') is None:
                data = all_items[:100]
            elif params.get('after') == 'stop-99':
                data = all_items[100:]
            else:
                raise AssertionError(f"unexpected cursor {params.get('after')}")
            return {'code': '0', 'data': data}

        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = paged

        orders = api._fetch_algo_orders('BTC/USDT:USDT')

        self.assertEqual(101, len(orders))
        self.assertIn('stop-100', {order['id'] for order in orders})
        self.assertEqual([None, 'stop-99'], seen_after[:2])

    def test_repeated_full_page_cursor_raises_instead_of_truncating(self):
        api = _bare_api()
        page = [_native_stop(algo_id=f'stop-{index}') for index in range(100)]
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = (
            lambda params: {'code': '0', 'data': page}
            if params.get('ordType') == 'conditional'
            else {'code': '0', 'data': []})

        with self.assertRaises(RuntimeError):
            api._fetch_algo_orders('BTC/USDT:USDT')

    def test_normal_pending_paginates_past_first_hundred_orders(self):
        api = _bare_api()
        items = [
            _native_normal(order_id=f'order-{index}') for index in range(101)]
        seen_after = []

        def paged(params):
            seen_after.append(params.get('after'))
            if params.get('after') is None:
                data = items[:100]
            elif params.get('after') == 'order-99':
                data = items[100:]
            else:
                raise AssertionError(f"unexpected cursor {params.get('after')}")
            return {'code': '0', 'data': data}

        api.exchange.privateGetTradeOrdersPending.side_effect = paged

        orders = api._fetch_normal_orders('BTC/USDT:USDT')

        self.assertEqual(101, len(orders))
        self.assertIn('order-100', {order['id'] for order in orders})
        self.assertEqual([None, 'order-99'], seen_after)

    def test_repeated_normal_pending_cursor_raises(self):
        api = _bare_api()
        page = [
            _native_normal(order_id=f'order-{index}') for index in range(100)]
        api.exchange.privateGetTradeOrdersPending.return_value = {
            'code': '0', 'data': page}

        with self.assertRaises(RuntimeError):
            api._fetch_normal_orders('BTC/USDT:USDT')

    def test_combined_snapshot_catches_normal_order_at_endpoint_boundary(self):
        api = _bare_api()
        api._fetch_normal_orders = Mock(side_effect=[
            [], [{'id': 'boundary-order'}],
        ])
        api._fetch_algo_orders = Mock(return_value=[])

        normal, algos = api._fetch_pending_snapshot('BTC/USDT:USDT')

        self.assertEqual(['boundary-order'], [order['id'] for order in normal])
        self.assertEqual([], algos)

    def test_active_position_cancel_never_falls_back_to_cancel_all(self):
        api = _bare_api()
        api._cancel_algo_order = Mock(return_value=False)
        api.cancel_all_orders = Mock(return_value=True)

        self.assertFalse(api.cancel_stop_order_only(
            'BTC/USDT:USDT', 'stop-old'))
        api.cancel_all_orders.assert_not_called()

    def test_cancel_all_catches_algorithm_order_that_appears_late(self):
        api = _bare_api()
        api.exchange.cancel_all_orders.return_value = None
        late = {'id': 'late-stop'}
        api._fetch_pending_snapshot = Mock(side_effect=[
            ([], []), ([], []), ([], [late]), ([], []), ([], []),
        ])
        api._request_cancel_algo_orders = Mock()

        self.assertTrue(api.cancel_all_orders('BTC/USDT:USDT'))
        api._request_cancel_algo_orders.assert_any_call(
            'BTC/USDT:USDT', ['late-stop'])

    def test_cancel_all_catches_normal_order_that_appears_late(self):
        api = _bare_api()
        api.exchange.cancel_all_orders.return_value = None
        late = {'id': 'late-normal'}
        api._fetch_pending_snapshot = Mock(side_effect=[
            ([], []), ([], []), ([late], []), ([], []), ([], []),
        ])
        api._request_cancel_normal_orders = Mock()
        api.exchange.privateGetTradeOrder.return_value = {
            'code': '0', 'data': [{
                'ordId': 'late-normal', 'state': 'canceled', 'accFillSz': '0'}]}

        self.assertTrue(api.cancel_all_orders('BTC/USDT:USDT'))
        api._request_cancel_normal_orders.assert_any_call(
            'BTC/USDT:USDT', {'late-normal'})

    def test_cancel_all_rejects_normal_order_filled_during_cancel_race(self):
        api = _bare_api()
        normal = {'id': 'raced-fill'}
        api._fetch_pending_snapshot = Mock(side_effect=[
            ([normal], []), ([], []), ([], []),
        ])
        api.exchange.privateGetTradeOrder.return_value = {
            'code': '0', 'data': [{
                'ordId': 'raced-fill', 'state': 'filled', 'accFillSz': '10'}]}

        self.assertFalse(api.cancel_all_orders('BTC/USDT:USDT'))

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
    def test_normal_order_is_cancelled_and_verified_in_native_pending_list(self):
        api = _bare_api()
        phases = [[_native_normal('normal-1')], []]

        def pending(_params):
            data = phases.pop(0) if phases else []
            return {'code': '0', 'data': data}

        api.exchange.privateGetTradeOrdersPending.side_effect = pending
        api.exchange.privateGetTradeOrder.return_value = {
            'code': '0', 'data': [{
                'ordId': 'normal-1', 'state': 'canceled', 'accFillSz': '0'}]}

        self.assertTrue(api.cancel_order('BTC/USDT:USDT', 'normal-1'))
        api.exchange.privatePostTradeCancelOrder.assert_called_once_with({
            'instId': 'BTC-USDT-SWAP', 'ordId': 'normal-1'})

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


class StopCreationIdempotencyTest(unittest.TestCase):
    CLIENT_ID = 'StopStableClient123'

    def _api(self):
        api = _bare_api()
        api.margin_mode = 'cross'
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api._amount_precision_cache['BTC/USDT:USDT'] = 0
        api.exchange.amount_to_precision.side_effect = lambda _s, value: str(int(value))
        api.exchange.price_to_precision.side_effect = lambda _s, value: str(float(value))
        return api

    def test_preexisting_same_client_stop_is_reused_without_post(self):
        api = self._api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({
            'conditional': [_native_stop(client_id=self.CLIENT_ID)],
        })

        order = api.create_stop_loss_order(
            'BTC/USDT:USDT', 'long', 0.1, 55000,
            client_order_id=self.CLIENT_ID)

        self.assertEqual(order['id'], 'stop-1')
        api.exchange.privatePostTradeOrderAlgo.assert_not_called()

    def test_timeout_confirms_by_same_algo_client_id_and_never_reposts(self):
        api = self._api()
        api.exchange.privatePostTradeOrderAlgo.side_effect = RequestTimeout('timeout')
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _phased_algo_stub([
            [], [_native_stop(client_id=self.CLIENT_ID)],
        ])

        order = api.create_stop_loss_order(
            'BTC/USDT:USDT', 'long', 0.1, 55000,
            client_order_id=self.CLIENT_ID)

        self.assertEqual(order['id'], 'stop-1')
        api.exchange.privatePostTradeOrderAlgo.assert_called_once()
        request = api.exchange.privatePostTradeOrderAlgo.call_args.args[0]
        self.assertEqual(request['algoClOrdId'], self.CLIENT_ID)

    def test_timeout_and_uncertain_queries_never_blind_repost(self):
        api = self._api()
        calls = {'round': 0}

        def pending(params):
            # 预查完整成功为空；POST 后所有查询均不确定。
            calls['round'] += 1
            if calls['round'] <= len(OkxApi.ALGO_ORDER_TYPES):
                return {'code': '0', 'data': []}
            raise RuntimeError('query uncertain')

        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = pending
        api.exchange.privatePostTradeOrderAlgo.side_effect = RequestTimeout('timeout')

        order = api.create_stop_loss_order(
            'BTC/USDT:USDT', 'long', 0.1, 55000,
            client_order_id=self.CLIENT_ID)

        self.assertIsNone(order)
        api.exchange.privatePostTradeOrderAlgo.assert_called_once()

    def test_same_client_id_with_different_content_fails_before_post(self):
        api = self._api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({
            'conditional': [_native_stop(px='54000', client_id=self.CLIENT_ID)],
        })

        order = api.create_stop_loss_order(
            'BTC/USDT:USDT', 'long', 0.1, 55000,
            client_order_id=self.CLIENT_ID)

        self.assertIsNone(order)
        api.exchange.privatePostTradeOrderAlgo.assert_not_called()


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
        captured = {}

        def post_algo(request):
            captured.update(request)
            return {'code': '0', 'data': [{
                'algoId': 'stop-1', 'algoClOrdId': request['algoClOrdId'],
                'sCode': '0'}]}

        def pending(params):
            data = []
            if captured and params.get('ordType') == 'conditional':
                data = [_native_stop(
                    sz='10', px='55000.38',
                    client_id=captured['algoClOrdId'])]
            return {'code': '0', 'data': data}

        api.exchange.privatePostTradeOrderAlgo.side_effect = post_algo
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = pending
        order = api.create_stop_loss_order(
            'BTC/USDT:USDT', 'long', 0.1, 55000.384)

        self.assertEqual(order['id'], 'stop-1')
        self.assertEqual(captured['slTriggerPx'], '55000.38')
        self.assertEqual(captured['slOrdPx'], '-1')
        self.assertEqual(captured['reduceOnly'], 'true')
        self.assertEqual(len(captured['algoClOrdId']), 32)
        api.exchange.privatePostTradeOrderAlgo.assert_called_once()

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
    """止损存在性四态裁决：intact / adoptable / mismatch / missing。"""

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
        self.assertEqual(api.find_stop_order_state(
            'BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'), 'mismatch')

    def test_id_present_but_content_differs_is_mismatch(self):
        api = self._api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop(sz='5', px='50000')]})
        self.assertEqual(api.find_stop_order_state('BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'), 'mismatch')

    def test_empty_list_is_missing(self):
        api = self._api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({})
        self.assertEqual(api.find_stop_order_state('BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'), 'missing')

    def test_unique_matching_new_id_is_adoptable_not_missing(self):
        api = self._api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({
            'conditional': [_native_stop(algo_id='stop-new')],
        })
        self.assertEqual(
            api.find_stop_order_state(
                'BTCUSDT', 'long', 0.1, 55000.0, 'stop-old'),
            {'state': 'adoptable', 'order_id': 'stop-new'})

    def test_multiple_matching_stops_are_mismatch_not_adoptable(self):
        api = self._api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({
            'conditional': [
                _native_stop(algo_id='stop-a'),
                _native_stop(algo_id='stop-b'),
            ],
        })
        self.assertEqual(api.find_stop_order_state(
            'BTCUSDT', 'long', 0.1, 55000.0, 'stop-old'), 'mismatch')

    def test_recorded_stop_plus_duplicate_matching_stop_is_mismatch(self):
        api = self._api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({
            'conditional': [
                _native_stop(algo_id='stop-1'),
                _native_stop(algo_id='stop-2'),
            ],
        })
        self.assertEqual(api.find_stop_order_state(
            'BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'), 'mismatch')

    def test_recorded_conditional_plus_unknown_trigger_is_mismatch(self):
        api = self._api()
        trigger = _native_stop(algo_id='manual-trigger')
        trigger['ordType'] = 'trigger'
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({
            'conditional': [_native_stop(algo_id='stop-1')],
            'trigger': [trigger],
        })
        self.assertEqual(api.find_stop_order_state(
            'BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'), 'mismatch')

    def test_contract_size_failure_propagates(self):
        """面值不可得：fail-closed 异常向上传播，调用方按 fail-safe 跳过本轮。"""
        api = _bare_api()
        api.exchange.market.side_effect = RuntimeError('网络错误')
        with self.assertRaises(ContractSizeUnavailable):
            api.find_stop_order_state('BTCUSDT', 'long', 0.1, 55000.0)


class StopOrderMatchTest(unittest.TestCase):
    GOOD = {
        'id': 'stop-1', 'side': 'sell', 'amount': 25.0,
        'stopLossPrice': 98.5, 'reduceOnly': True,
        'info': {'ordType': 'conditional', 'slOrdPx': '-1',
                 'state': 'live', 'slTriggerPxType': 'last',
                 'posSide': 'net'},
    }

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

    def test_unreadable_fields_rejected(self):
        """字段读不到一律视为不匹配（宁可重试创建，不误认）。"""
        self.assertFalse(OkxApi._algo_order_matches({'side': 'sell', 'info': {}}, 'sell', 98.5, 25.0))

    def test_trigger_from_okx_info_field(self):
        o = {'side': 'sell', 'reduceOnly': True,
             'info': {'slTriggerPx': '98.5', 'sz': '25',
                      'ordType': 'conditional', 'slOrdPx': '-1',
                      'state': 'live', 'slTriggerPxType': 'last'}}
        self.assertTrue(OkxApi._algo_order_matches(o, 'sell', 98.5, 25.0))

    def test_non_reduce_only_or_non_market_stop_rejected(self):
        self.assertFalse(OkxApi._algo_order_matches(
            dict(self.GOOD, reduceOnly=False), 'sell', 98.5, 25.0))
        limit_stop = dict(self.GOOD, info={'ordType': 'conditional', 'slOrdPx': '97'})
        self.assertFalse(OkxApi._algo_order_matches(limit_stop, 'sell', 98.5, 25.0))

    def test_recorded_id_must_match(self):
        self.assertTrue(OkxApi._algo_order_matches(
            self.GOOD, 'sell', 98.5, 25.0, expected_order_id='stop-1'))
        self.assertFalse(OkxApi._algo_order_matches(
            self.GOOD, 'sell', 98.5, 25.0, expected_order_id='other'))

    def test_one_tick_or_one_contract_difference_never_matches_at_large_values(self):
        large = {
            'id': 'stop-large', 'side': 'sell', 'amount': 1_000_000,
            'stopLossPrice': 100_000.00, 'reduceOnly': True,
            'info': {'ordType': 'conditional', 'slOrdPx': '-1'},
        }
        self.assertFalse(OkxApi._algo_order_matches(
            dict(large, stopLossPrice=100_000.01),
            'sell', 100_000.00, 1_000_000))
        self.assertFalse(OkxApi._algo_order_matches(
            dict(large, amount=999_999),
            'sell', 100_000.00, 1_000_000))


class OpenPreflightAndLateFillTest(unittest.TestCase):
    CLIENT_ID = 'ILATE123'

    def _api(self):
        api = _bare_api()
        api.margin_mode = 'cross'
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api._amount_precision_cache['BTC/USDT:USDT'] = 0
        api._leverage_done = {'BTC/USDT:USDT'}
        api.exchange.amount_to_precision.side_effect = (
            lambda _symbol, value: str(int(value)))
        return api

    def test_flat_preflight_rejects_any_stale_reduce_only_algo(self):
        api = self._api()
        trigger = _native_stop(algo_id='stale', side='buy')
        trigger['ordType'] = 'trigger'
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({
            'trigger': [trigger],
        })
        api.exchange.fetch_positions.return_value = []

        self.assertIsNone(api.open_position(
            'BTC/USDT:USDT', 'long', 0.1))

        api.exchange.create_order.assert_not_called()

    def test_flat_preflight_rejects_non_reduce_only_algo(self):
        api = self._api()
        twap = _native_stop(algo_id='stale-twap', side='buy')
        twap['ordType'] = 'twap'
        twap['reduceOnly'] = 'false'
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({
            'twap': [twap],
        })
        api.exchange.fetch_positions.return_value = []

        self.assertIsNone(api.open_position(
            'BTC/USDT:USDT', 'long', 0.1))
        api.exchange.create_order.assert_not_called()

    def test_flat_preflight_rejects_normal_pending_order(self):
        api = self._api()
        api.exchange.privateGetTradeOrdersPending.return_value = {
            'code': '0', 'data': [_native_normal('manual-limit')]}
        api.exchange.fetch_positions.return_value = []

        self.assertIsNone(api.open_position(
            'BTC/USDT:USDT', 'long', 0.1))
        api.exchange.create_order.assert_not_called()

    def test_pending_order_fill_between_snapshots_blocks_new_market_order(self):
        api = self._api()
        api.exchange.fetch_positions.side_effect = [
            [],
            [{'symbol': 'BTC/USDT:USDT', 'contracts': 10,
              'side': 'long', 'hedged': False, 'info': {'posSide': 'net'}}],
        ]

        self.assertIsNone(api.open_position(
            'BTC/USDT:USDT', 'long', 0.1))
        api.exchange.create_order.assert_not_called()

    def test_zero_position_unresolved_order_is_cancelled_and_returned_as_pending(self):
        api = self._api()
        api.exchange.fetch_positions.return_value = []
        api.exchange.create_order.return_value = {'id': 'ack-1'}
        api._confirm_market_order = Mock(side_effect=[(None, 0.0), (None, 0.0)])
        api.exchange.fetch_order.return_value = {
            'id': 'ack-1', 'status': 'open', 'type': 'market',
            'side': 'buy', 'amount': 10, 'reduceOnly': False,
            'symbol': 'BTC/USDT:USDT',
        }

        result = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertTrue(result['open_order_may_remain_live'])
        api.exchange.cancel_order.assert_called_once_with(
            'ack-1', 'BTC/USDT:USDT')

    def test_existing_unresolved_client_order_uses_same_cancel_state_machine(self):
        api = self._api()
        existing = {
            'id': 'old-1', 'status': 'open', 'type': 'market',
            'side': 'buy', 'amount': 10, 'reduceOnly': False,
            'symbol': 'BTC/USDT:USDT',
        }
        api.exchange.fetch_order.return_value = existing
        api.exchange.fetch_positions.return_value = []
        api._confirm_market_order = Mock(side_effect=[(None, 0.0), (None, 0.0)])

        result = api.open_position(
            'BTC/USDT:USDT', 'long', 0.1,
            client_order_id=self.CLIENT_ID)

        self.assertTrue(result['open_order_may_remain_live'])
        api.exchange.cancel_order.assert_called_once_with(
            'old-1', 'BTC/USDT:USDT')
        api.exchange.create_order.assert_not_called()


class LeverageFailClosedTest(unittest.TestCase):
    def test_set_leverage_failure_blocks_symbol_setup(self):
        api = _bare_api()
        api.margin_mode = 'isolated'
        api.default_leverage = 5
        api.leverage_overrides = {}
        api._leverage_done = set()
        api.exchange.set_leverage.side_effect = RuntimeError('permission denied')

        with self.assertRaises(RuntimeError):
            api.setup_symbol('BTC/USDT:USDT')

        self.assertNotIn('BTC/USDT:USDT', api._leverage_done)


class PositionParsingStrictnessTest(unittest.TestCase):
    """终审缺陷反例：交易所持仓响应的「不确定」绝不能滑向「空仓」。"""

    def test_none_response_is_not_flat(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = None
        with self.assertRaises(PositionModeError):
            api.get_position('BTC/USDT:USDT')

    def test_missing_contracts_with_nonzero_raw_pos_is_not_flat(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDT:USDT', 'contracts': None,
             'info': {'pos': '5'}}]
        with self.assertRaises(PositionModeError):
            api.get_position('BTC/USDT:USDT')

    def test_nan_or_bool_contracts_rejected(self):
        for bad in (float('nan'), float('inf'), True, 'garbage'):
            api = _bare_api()
            api.exchange.fetch_positions.return_value = [
                {'symbol': 'BTC/USDT:USDT', 'contracts': bad,
                 'side': 'long', 'info': {'pos': '5'}}]
            with self.subTest(bad=bad), self.assertRaises(PositionModeError):
                api.get_position('BTC/USDT:USDT')

    def test_side_contradicting_raw_pos_sign_rejected(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDT:USDT', 'contracts': 5.0,
             'side': 'long', 'info': {'pos': '-5'}}]
        with self.assertRaises(PositionModeError):
            api.get_position('BTC/USDT:USDT')

    def test_contracts_and_raw_pos_zero_nonzero_contradictions_rejected(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDT:USDT', 'contracts': 0.0,
             'info': {'pos': '5'}}]
        with self.assertRaises(PositionModeError):
            api.get_position('BTC/USDT:USDT')
        api.exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDT:USDT', 'contracts': 5.0,
             'side': 'long', 'info': {'pos': '0'}}]
        with self.assertRaises(PositionModeError):
            api.get_position('BTC/USDT:USDT')

    def test_clean_flat_and_clean_position_still_work(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = []
        self.assertIsNone(api.get_position('BTC/USDT:USDT'))
        api.exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDT:USDT', 'contracts': None, 'info': {}}]
        self.assertIsNone(api.get_position('BTC/USDT:USDT'))
        good = {'contracts': '5', 'side': 'long', 'symbol': 'BTC/USDT:USDT',
                'info': {'pos': '5', 'posSide': 'net'}}
        api.exchange.fetch_positions.return_value = [good]
        self.assertEqual(good, api.get_position('BTC/USDT:USDT'))

    def test_entry_without_symbol_identity_rejected(self):
        """对抗复审反例 A：{} / 无标识条目是「无法归属」，不是空仓。"""
        for malformed in ({}, {'contracts': None, 'info': {}},
                          {'contracts': 5.0, 'side': 'long', 'info': {}}):
            api = _bare_api()
            api.exchange.fetch_positions.return_value = [malformed]
            with self.subTest(malformed=malformed), \
                    self.assertRaises(PositionModeError):
                api.get_position('BTC/USDT:USDT')

    def test_wrong_symbol_entry_rejected(self):
        """对抗复审反例 B：错误品种的持仓绝不能当成所查品种的仓。"""
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [
            {'symbol': 'ETH/USDT:USDT', 'contracts': '5', 'side': 'long',
             'info': {'pos': '5', 'posSide': 'net'}}]
        with self.assertRaises(PositionModeError):
            api.get_position('BTC/USDT:USDT')
        api.exchange.fetch_positions.return_value = [
            {'contracts': '5', 'side': 'long',
             'info': {'pos': '5', 'instId': 'ETH-USDT-SWAP'}}]
        with self.assertRaises(PositionModeError):
            api.get_position('BTC/USDT:USDT')

    def test_list_position_symbols_rejects_unidentifiable_entry(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [{'contracts': 2.0}]
        with self.assertRaises(PositionModeError):
            api.list_position_symbols()

    def test_list_position_symbols_refuses_uncertain_snapshot(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = None
        with self.assertRaises(PositionModeError):
            api.list_position_symbols()
        api.exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDT:USDT', 'contracts': None,
             'info': {'pos': '3'}}]
        with self.assertRaises(PositionModeError):
            api.list_position_symbols()

    def test_list_position_symbols_reports_clean_positions(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDT:USDT', 'contracts': 2.0,
             'info': {'pos': '2'}},
            {'symbol': 'ETH/USDT:USDT', 'contracts': 0.0, 'info': {'pos': '0'}},
            {'symbol': 'LTC/USD:LTC', 'contracts': 1.0, 'info': {'pos': '1'}},
        ]
        self.assertEqual(['BTCUSDT'], api.list_position_symbols())


class OrderTriStateAdjudicationTest(unittest.TestCase):
    """终审缺陷反例：只有 OrderNotFound 才是「明确不存在」。

    确定性 clOrdId 查询返回 {} 后再新发一张开仓 POST，就是重复建仓。
    """

    def _api(self):
        api = _bare_api()
        api.margin_mode = 'cross'
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api._amount_precision_cache['BTC/USDT:USDT'] = 0
        api._leverage_done = {'BTC/USDT:USDT'}
        api.exchange.amount_to_precision.side_effect = (
            lambda _symbol, value: str(int(value)))
        return api

    def test_find_existing_open_order_raises_on_malformed_response(self):
        for malformed in ({}, None, {'noise': 1}):
            api = self._api()
            api.exchange.fetch_order.return_value = malformed
            with self.subTest(malformed=malformed), \
                    self.assertRaises(RuntimeError):
                api.find_existing_open_order(
                    'BTCUSDT', 'long', 0.1, 'CID123')

    def test_find_existing_open_order_none_only_on_order_not_found(self):
        api = self._api()
        api.exchange.fetch_order.side_effect = OrderNotFound('gone')
        self.assertIsNone(
            api.find_existing_open_order('BTCUSDT', 'long', 0.1, 'CID123'))

    def test_open_position_refuses_new_post_after_malformed_lookup(self):
        api = self._api()
        api.exchange.fetch_order.return_value = {}
        result = api.open_position(
            'BTCUSDT', 'long', 0.1, client_order_id='CID123')
        self.assertIsNone(result)
        api.exchange.create_order.assert_not_called()

    def test_close_leg_recovery_raises_on_malformed_response(self):
        api = self._api()
        api.exchange.fetch_order.return_value = {}
        with self.assertRaises(RuntimeError):
            api._collect_existing_close_legs(
                'BTC/USDT:USDT', 'sell', 10.0, 'C' + 'a' * 31)


class PaginationDuplicateIdTest(unittest.TestCase):
    """终审缺陷反例：分页重复 ID 说明快照异常，静默去重会伪造完整清单。"""

    def test_algo_duplicate_across_pages_raises(self):
        api = _bare_api()
        full_page = [_native_stop(algo_id=f'a{i}')
                     for i in range(api.ALGO_PAGE_LIMIT)]
        duplicate_page = [_native_stop(algo_id=f'a{api.ALGO_PAGE_LIMIT - 1}')]
        responses = iter([
            {'code': '0', 'data': full_page},
            {'code': '0', 'data': duplicate_page},
        ])
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = (
            lambda params: next(responses))
        with self.assertRaises(RuntimeError):
            api._fetch_algo_pending_raw('BTC-USDT-SWAP', 'conditional')

    def test_algo_duplicate_within_page_raises(self):
        api = _bare_api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = (
            lambda params: {
                'code': '0',
                'data': [_native_stop(algo_id='dup'),
                         _native_stop(algo_id='dup')]})
        with self.assertRaises(RuntimeError):
            api._fetch_algo_pending_raw('BTC-USDT-SWAP', 'conditional')

    def test_normal_duplicate_across_pages_raises(self):
        api = _bare_api()
        full_page = [_native_normal(order_id=f'n{i}')
                     for i in range(api.NORMAL_PAGE_LIMIT)]
        duplicate_page = [_native_normal(
            order_id=f'n{api.NORMAL_PAGE_LIMIT - 1}')]
        responses = iter([
            {'code': '0', 'data': full_page},
            {'code': '0', 'data': duplicate_page},
        ])
        api.exchange.privateGetTradeOrdersPending.side_effect = (
            lambda params: next(responses))
        with self.assertRaises(RuntimeError):
            api._fetch_normal_pending_raw('BTC-USDT-SWAP')


class StopProtectionSemanticsTest(unittest.TestCase):
    """终审缺陷反例：已暂停/已触发/触发价类型错误的算法单不是完整保护。"""

    GOOD = StopOrderMatchTest.GOOD

    def test_non_live_states_rejected(self):
        for state in ('pause', 'canceled', 'effective', 'order_failed'):
            bad = dict(self.GOOD,
                       info=dict(self.GOOD['info'], state=state))
            with self.subTest(state=state):
                self.assertFalse(
                    OkxApi._algo_order_matches(bad, 'sell', 98.5, 25.0))

    def test_missing_state_rejected(self):
        info = dict(self.GOOD['info'])
        info.pop('state')
        self.assertFalse(OkxApi._algo_order_matches(
            dict(self.GOOD, info=info), 'sell', 98.5, 25.0))

    def test_wrong_or_missing_trigger_price_type_rejected(self):
        for trigger_type in ('mark', 'index', '', None):
            info = dict(self.GOOD['info'])
            if trigger_type is None:
                info.pop('slTriggerPxType')
            else:
                info['slTriggerPxType'] = trigger_type
            with self.subTest(trigger_type=trigger_type):
                self.assertFalse(OkxApi._algo_order_matches(
                    dict(self.GOOD, info=info), 'sell', 98.5, 25.0))

    def test_hedge_pos_side_rejected(self):
        for pos_side in ('long', 'short'):
            bad = dict(self.GOOD,
                       info=dict(self.GOOD['info'], posSide=pos_side))
            with self.subTest(pos_side=pos_side):
                self.assertFalse(
                    OkxApi._algo_order_matches(bad, 'sell', 98.5, 25.0))

    def test_unknown_reduce_only_forces_mismatch_not_missing(self):
        """对抗复审反例 C：唯一保护单 reduceOnly 字段暂缺 → mismatch。

        缺字段的单若在 reduce-only 清单里隐身，四态裁决会判 missing，
        上层随即补挂第二张止损——它可能就是真实保护单本身。
        """
        api = _bare_api()
        api.margin_mode = 'cross'
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api._amount_precision_cache['BTC/USDT:USDT'] = 0
        api.exchange.amount_to_precision.side_effect = (
            lambda _s, v: str(int(float(v))))
        api.exchange.price_to_precision = lambda _s, p: f'{float(p):.2f}'
        stop = _native_stop(algo_id='real-stop', sz='10', px='55000.38')
        del stop['reduceOnly']
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [stop]})
        self.assertEqual(
            'mismatch',
            api.find_stop_order_state(
                'BTCUSDT', 'long', 0.1, 55000.38, 'gone-id'))


class SafeCancelAccFillTest(unittest.TestCase):
    """终审缺陷反例：accFillSz 缺失是「不知道」，不是「明确零成交」。"""

    def test_missing_or_empty_acc_fill_is_not_proof_of_zero(self):
        self.assertFalse(OkxApi._normal_order_safely_cancelled(
            {'state': 'canceled'}))
        self.assertFalse(OkxApi._normal_order_safely_cancelled(
            {'state': 'canceled', 'accFillSz': ''}))
        self.assertFalse(OkxApi._normal_order_safely_cancelled(
            {'state': 'canceled', 'accFillSz': None}))
        self.assertFalse(OkxApi._normal_order_safely_cancelled(
            {'state': 'canceled', 'accFillSz': True}))

    def test_explicit_zero_fill_still_confirms(self):
        self.assertTrue(OkxApi._normal_order_safely_cancelled(
            {'state': 'canceled', 'accFillSz': '0'}))

    def test_partial_fill_or_live_state_rejected(self):
        self.assertFalse(OkxApi._normal_order_safely_cancelled(
            {'state': 'canceled', 'accFillSz': '2'}))
        self.assertFalse(OkxApi._normal_order_safely_cancelled(
            {'state': 'live', 'accFillSz': '0'}))


class OhlcvBoundaryValidationTest(unittest.TestCase):
    """终审缺陷反例：坏蜡烛必须在适配层被整批拒绝，不得进入策略计算。"""

    GOOD = [
        [1000, 10.0, 12.0, 9.0, 11.0, 100.0],
        [2000, 11.0, 13.0, 10.0, 12.0, 50.0],
    ]

    def test_good_batch_passes_through(self):
        self.assertEqual(self.GOOD, OkxApi.validate_ohlcv(self.GOOD, 'BTC'))
        self.assertEqual([], OkxApi.validate_ohlcv([], 'BTC'))

    def test_none_response_rejected(self):
        with self.assertRaises(ValueError):
            OkxApi.validate_ohlcv(None, 'BTC')

    def test_duplicate_or_out_of_order_timestamps_rejected(self):
        duplicate = [self.GOOD[0], list(self.GOOD[0])]
        with self.assertRaises(ValueError):
            OkxApi.validate_ohlcv(duplicate, 'BTC')
        reordered = [self.GOOD[1], self.GOOD[0]]
        with self.assertRaises(ValueError):
            OkxApi.validate_ohlcv(reordered, 'BTC')

    def test_nonfinite_bool_or_nonpositive_prices_rejected(self):
        for bad in (float('nan'), float('inf'), 0.0, -1.0, True, '10'):
            for column in (1, 2, 3, 4):
                row = list(self.GOOD[0])
                row[column] = bad
                with self.subTest(bad=bad, column=column), \
                        self.assertRaises(ValueError):
                    OkxApi.validate_ohlcv([row], 'BTC')

    def test_candle_internal_contradiction_rejected(self):
        high_below_close = [1000, 10.0, 10.5, 9.0, 11.0, 1.0]
        with self.assertRaises(ValueError):
            OkxApi.validate_ohlcv([high_below_close], 'BTC')
        low_above_open = [1000, 8.0, 12.0, 9.0, 11.0, 1.0]
        with self.assertRaises(ValueError):
            OkxApi.validate_ohlcv([low_above_open], 'BTC')
        high_below_low = [1000, 10.0, 9.0, 11.0, 10.0, 1.0]
        with self.assertRaises(ValueError):
            OkxApi.validate_ohlcv([high_below_low], 'BTC')

    def test_negative_or_nonfinite_volume_rejected(self):
        for bad in (-1.0, float('nan'), True):
            row = list(self.GOOD[0])
            row[5] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                OkxApi.validate_ohlcv([row], 'BTC')

    def test_fetch_ohlcv_entry_point_enforces_validation(self):
        api = _bare_api()
        api.exchange.fetch_ohlcv.return_value = [
            [1000, 10.0, float('nan'), 9.0, 11.0, 1.0]]
        with self.assertRaises(ValueError):
            api.fetch_ohlcv('BTC/USDT:USDT', '1d', limit=10)


class CompensationEvidenceReadOnlyTest(unittest.TestCase):
    """终审缺陷反例：补偿证据找回全程只读，任何分支都不得下单。"""

    def _api(self):
        api = _bare_api()
        api.margin_mode = 'cross'
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api._amount_precision_cache['BTC/USDT:USDT'] = 0
        api.exchange.amount_to_precision.side_effect = (
            lambda _symbol, value: str(int(value)))
        return api

    def test_not_found_returns_none_without_any_post(self):
        api = self._api()
        api.exchange.fetch_order.side_effect = OrderNotFound('no legs')
        self.assertIsNone(api.find_compensation_close_evidence(
            'BTC/USDT:USDT', 'long', 0.1, 'OPENID1'))
        api.exchange.create_order.assert_not_called()

    def test_full_terminal_legs_return_aggregate_without_any_post(self):
        api = self._api()
        base = OkxApi.compensation_client_order_id('OPENID1')
        leg = {'id': 'o1', 'clientOrderId': base, 'symbol': 'BTC/USDT:USDT',
               'type': 'market', 'side': 'sell', 'amount': 10.0,
               'filled': 10.0, 'remaining': 0.0, 'status': 'closed',
               'average': 50000.0, 'reduceOnly': True, 'info': {}}

        def fetch_order(order_id, _symbol, params=None):
            if (params or {}).get('clOrdId') == base:
                return dict(leg)
            raise OrderNotFound(str(order_id))

        api.exchange.fetch_order.side_effect = fetch_order
        result = api.find_compensation_close_evidence(
            'BTC/USDT:USDT', 'long', 0.1, 'OPENID1')
        self.assertTrue(result['fully_closed'])
        self.assertTrue(result['read_only_evidence'])
        self.assertEqual(50000.0, result['average'])
        self.assertEqual(['o1'], result['ids'])
        api.exchange.create_order.assert_not_called()

    def test_partial_evidence_is_reported_as_incomplete(self):
        api = self._api()
        base = OkxApi.compensation_client_order_id('OPENID1')
        leg = {'id': 'o1', 'clientOrderId': base, 'symbol': 'BTC/USDT:USDT',
               'type': 'market', 'side': 'sell', 'amount': 6.0,
               'filled': 6.0, 'remaining': 0.0, 'status': 'canceled',
               'average': 50000.0, 'reduceOnly': True, 'info': {}}

        def fetch_order(order_id, _symbol, params=None):
            if (params or {}).get('clOrdId') == base:
                return dict(leg)
            raise OrderNotFound(str(order_id))

        api.exchange.fetch_order.side_effect = fetch_order
        self.assertIsNone(api.find_compensation_close_evidence(
            'BTC/USDT:USDT', 'long', 0.1, 'OPENID1'))
        api.exchange.create_order.assert_not_called()

    def test_uncertain_lookup_raises_instead_of_guessing(self):
        api = self._api()
        api.exchange.fetch_order.return_value = {}
        with self.assertRaises(RuntimeError):
            api.find_compensation_close_evidence(
                'BTC/USDT:USDT', 'long', 0.1, 'OPENID1')
        api.exchange.create_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()
