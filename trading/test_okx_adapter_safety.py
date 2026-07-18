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
import okx_api  # noqa: E402
from okx_api import OkxApi, ContractSizeUnavailable, PositionModeError  # noqa: E402
from trade_executor import TradeExecutorMixin  # noqa: E402
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
    api.margin_mode = 'cross'
    api.default_leverage = 5
    api.leverage_overrides = {}
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
    api.exchange.privateGetTradeOrderAlgo.return_value = {
        'code': '51603', 'msg': 'Algo order does not exist', 'data': []}
    api.exchange.fetch_positions.return_value = []
    api.exchange.price_to_precision.side_effect = (
        lambda _symbol, value: str(float(value)))
    api.exchange.set_leverage.return_value = {
        'code': '0', 'data': [{
            'instId': 'BTC-USDT-SWAP', 'lever': '5',
            'mgnMode': 'cross', 'posSide': 'net',
        }]}
    return api


def _native_stop(algo_id='stop-1', side='sell', sz='10', px='55000',
                 client_id='', state='live', trigger_px_type='last',
                 pos_side='net'):
    """OKX orders-algo-pending 原生响应里的一条 conditional 止损单。"""
    return {'algoId': algo_id, 'algoClOrdId': client_id,
            'instType': 'SWAP', 'instId': 'BTC-USDT-SWAP', 'side': side,
            'sz': sz, 'slTriggerPx': px, 'slOrdPx': '-1',
            'ordType': 'conditional', 'reduceOnly': 'true',
            'state': state, 'slTriggerPxType': trigger_px_type,
            'posSide': pos_side}


def _native_algo_detail(algo_id='stop-1', state='canceled', actual_sz='0',
                        side='sell', sz='10', px='55000',
                        child_ids=None, inst_type='SWAP'):
    """OKX 单张算法单详情；缺省为可证明零成交撤销的严格终态。"""
    return {
        'algoId': algo_id,
        'instType': inst_type,
        'instId': 'BTC-USDT-SWAP',
        'state': state,
        'side': side,
        'reduceOnly': 'true',
        'ordType': 'conditional',
        'slTriggerPxType': 'last',
        'posSide': 'net',
        'actualSide': 'sl',
        'slOrdPx': '-1',
        'slTriggerPx': px,
        'sz': sz,
        'actualSz': actual_sz,
        'triggerTime': '1700000000000',
        'ordIdList': list(child_ids or []),
    }


def _algo_detail_response(item):
    return {'code': '0', 'msg': '', 'data': [item]}


def _native_stop_child(order_id='child-1', algo_id='stop-1',
                       size='10', filled='10', inst_type='SWAP'):
    return {
        'ordId': order_id,
        'instType': inst_type,
        'instId': 'BTC-USDT-SWAP',
        'algoId': algo_id,
        'state': 'filled',
        'side': 'sell',
        'posSide': 'net',
        'reduceOnly': 'true',
        'sz': size,
        'accFillSz': filled,
    }


def _native_normal(order_id='order-1', side='buy', size='10'):
    return {
        'ordId': order_id, 'clOrdId': '', 'instType': 'SWAP',
        'instId': 'BTC-USDT-SWAP', 'side': side, 'sz': size,
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
    _MISSING = object()

    @staticmethod
    def _config(mode=_MISSING):
        config = {'apiKey': 'k', 'secret': 's', 'password': 'p'}
        if mode is not MarginModeValidationTest._MISSING:
            config['margin_mode'] = mode
        return config

    def test_invalid_margin_mode_is_rejected_before_trading(self):
        for bad in ('corss', 'both', 5, ['cross'], '', None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                OkxApi(self._config(bad))

    def test_valid_and_default_margin_modes_are_normalized(self):
        with patch.object(OkxApi, '_load_market_cache'):
            for value, expected in (
                    ('cross', 'cross'), (' ISOLATED ', 'isolated')):
                with self.subTest(value=value):
                    self.assertEqual(
                        expected, OkxApi(self._config(value)).margin_mode)
            self.assertEqual('cross', OkxApi(self._config()).margin_mode)

    def test_daily_candle_timezone_is_explicitly_locked_to_utc(self):
        exchange = Mock()
        with patch.object(okx_api.ccxt, 'okx', return_value=exchange) as ctor, \
                patch.object(OkxApi, '_load_market_cache'):
            OkxApi(self._config('cross'))
        options = ctor.call_args.args[0]['options']
        self.assertEqual('swap', options['defaultType'])
        self.assertEqual('UTC', options['fetchOHLCV']['timezone'])

    def test_leverage_null_and_invalid_overrides_are_rejected(self):
        for key, value in (
                ('leverage', None), ('leverage_overrides', None),
                ('leverage_overrides', {'BTCUSDT': None})):
            config = self._config()
            config[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                OkxApi(config)


class SandboxEnvironmentValidationTest(unittest.TestCase):
    @staticmethod
    def _config(value, key='sandbox'):
        return {
            'apiKey': 'k', 'secret': 's', 'password': 'p', key: value,
        }

    def test_string_false_never_enables_demo(self):
        exchange = Mock()
        with patch.object(okx_api.ccxt, 'okx', return_value=exchange), \
                patch.object(OkxApi, '_load_market_cache'):
            api = OkxApi(self._config('false'))

        self.assertIs(api.config['sandbox'], False)
        exchange.set_sandbox_mode.assert_not_called()

    def test_true_enables_demo_and_invalid_or_conflicting_values_fail(self):
        exchange = Mock()
        with patch.object(okx_api.ccxt, 'okx', return_value=exchange), \
                patch.object(OkxApi, '_load_market_cache'):
            OkxApi(self._config(True))
        exchange.set_sandbox_mode.assert_called_once_with(True)

        for bad in ('0', 0, None, 'maybe'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                OkxApi(self._config(bad))
        conflict = self._config(False)
        conflict['demo'] = True
        with self.assertRaisesRegex(ValueError, '矛盾'):
            OkxApi(conflict)


class ContractSizeFailClosedTest(unittest.TestCase):
    def test_missing_contract_size_raises(self):
        """市场数据缺 contractSize：必须抛异常，不允许默认 1.0。"""
        api = _bare_api()
        api.exchange.market.return_value = {'contractSize': None}
        with self.assertRaises(ContractSizeUnavailable):
            api._get_contract_size('BTC/USDT:USDT')

    def test_nonfinite_contract_size_raises(self):
        """畸形面值（'1e999'→inf / NaN）不得越过校验进入换算缓存。"""
        for bad in (
                True, False, '1e999', float('inf'), float('nan'),
                '-0.01', 0):
            api = _bare_api()
            api.exchange.market.return_value = {'contractSize': bad}
            with self.subTest(bad=bad), \
                    self.assertRaises(ContractSizeUnavailable):
                api._get_contract_size('BTC/USDT:USDT')
            self.assertNotIn('BTC/USDT:USDT', api._contract_size_cache)

    def test_poisoned_contract_size_cache_is_revalidated(self):
        for bad in (True, float('nan'), float('inf'), -1, 0):
            api = _bare_api()
            api._contract_size_cache['BTC/USDT:USDT'] = bad
            with self.subTest(bad=bad), \
                    self.assertRaises(ContractSizeUnavailable):
                api._get_contract_size('BTC/USDT:USDT')

    def test_market_cache_skips_malformed_contract_sizes(self):
        api = _bare_api()
        bad_values = (True, float('nan'), float('inf'), -1, 0)
        markets = {
            f'BAD{i}/USDT:USDT': {
                'type': 'swap', 'quote': 'USDT', 'settle': 'USDT',
                'contractSize': bad,
            }
            for i, bad in enumerate(bad_values)
        }
        markets['GOOD/USDT:USDT'] = {
            'type': 'swap', 'quote': 'USDT', 'settle': 'USDT',
            'contractSize': '0.01',
        }
        api.exchange.load_markets.return_value = markets

        api._load_market_cache()

        self.assertEqual(
            {'GOOD/USDT:USDT': 0.01}, api._contract_size_cache)

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

    def test_amount_precision_result_cannot_be_malformed_or_round_up(self):
        for result in (
                True, float('nan'), float('inf'), -1, 'garbage', 11):
            api = _bare_api()
            api._contract_size_cache['BTC/USDT:USDT'] = 0.01
            api.exchange.amount_to_precision.return_value = result
            with self.subTest(result=result), self.assertRaises(ValueError):
                api._coin_to_contracts('BTC/USDT:USDT', 0.1)

    def test_position_symbol_listing_filters_coin_margined_contracts(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [
            {'contracts': 10, 'symbol': 'BTC/USDT:USDT', 'side': 'long'},
            {'contracts': 5, 'symbol': 'BTC/USD:BTC'},
            {'contracts': 0, 'symbol': 'ETH/USDT:USDT'},
            {'contracts': 2, 'symbol': 'DOGE/USDT:USDT', 'side': 'short'},
        ]
        self.assertEqual(
            ['BTCUSDT', 'DOGEUSDT'], sorted(api.list_position_symbols()))


class PositionModeFailClosedTest(unittest.TestCase):
    def test_readback_proves_net_mode_without_mutating_account(self):
        api = _bare_api()
        api.exchange.fetch_position_mode.return_value = {
            'hedged': False, 'info': {'posMode': 'net_mode'}}
        api.verify_one_way_mode()  # 不抛出
        api.exchange.set_position_mode.assert_not_called()

    def test_hedged_or_unreadable_mode_rejects_startup(self):
        api = _bare_api()
        api.exchange.fetch_position_mode.return_value = {
            'hedged': True, 'info': {'posMode': 'long_short_mode'}}
        with self.assertRaises(PositionModeError):
            api.verify_one_way_mode()

        api.exchange.fetch_position_mode.side_effect = RuntimeError('network')
        with self.assertRaises(PositionModeError):
            api.verify_one_way_mode()

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

    def test_short_position_contracts_are_normalized_to_positive_size(self):
        api = _bare_api()
        raw = {
            'contracts': -5, 'side': 'short',
            'symbol': 'BTC/USDT:USDT',
            'info': {'pos': '-5', 'posSide': 'net'}}
        api.exchange.fetch_positions.return_value = [raw]

        position = api.get_position('BTC/USDT:USDT')

        self.assertEqual(5.0, position['contracts'])
        self.assertEqual(-5, raw['contracts'])


class MarketDataBoundaryTest(unittest.TestCase):
    def test_last_price_requires_finite_positive_non_bool_number(self):
        for ticker in (
                None, [], {}, {'last': True}, {'last': False},
                {'last': float('nan')}, {'last': float('inf')},
                {'last': 0}, {'last': -1}, {'last': 'garbage'}):
            api = _bare_api()
            api.exchange.fetch_ticker.return_value = ticker
            with self.subTest(ticker=ticker), self.assertRaises(ValueError):
                api.get_last_price('BTC/USDT:USDT')

        api = _bare_api()
        api.exchange.fetch_ticker.return_value = {'last': '123.45'}
        self.assertEqual(123.45, api.get_last_price('BTC/USDT:USDT'))


class CancelAlgoVerifiedTest(unittest.TestCase):
    def test_cancel_command_ok_but_still_listed_is_failure(self):
        """即使详情已是 canceled0，pending 仍在也不能宣布撤销完成。"""
        api = _bare_api()
        api.exchange.privatePostTradeCancelAlgos.return_value = {'code': '0', 'data': []}
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail()))
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop()]})
        self.assertFalse(api._cancel_algo_order('BTC/USDT:USDT', 'stop-1'))

    def test_cancelled_zero_fill_and_absent_is_success(self):
        api = _bare_api()
        api.exchange.privatePostTradeCancelAlgos.return_value = {'code': '0', 'data': []}
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail()))
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({})
        self.assertTrue(api._cancel_algo_order('BTC/USDT:USDT', 'stop-1'))
        # 撤销指令必须走原生 cancel-algos，携带 algoId + instId 数组体
        api.exchange.privatePostTradeCancelAlgos.assert_called_once_with(
            [{'algoId': 'stop-1', 'instId': 'BTC-USDT-SWAP'}])

    def test_cancel_command_failure_but_proven_cancelled_is_success(self):
        """POST 异常不作结论；canceled0 详情与 pending 缺席仍是完整证据。"""
        api = _bare_api()
        api.exchange.privatePostTradeCancelAlgos.side_effect = OrderNotFound('不存在')
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail()))
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop(algo_id='other')]})
        self.assertTrue(api._cancel_algo_order('BTC/USDT:USDT', 'stop-1'))

    def test_effective_failed_partial_missing_or_malformed_is_failure(self):
        """pending 缺席不能把触发、失败、部分成交或未知详情冒充撤销。"""
        cases = (
            ('effective', _algo_detail_response(_native_algo_detail(
                state='effective', actual_sz='10'))),
            ('order_failed', _algo_detail_response(_native_algo_detail(
                state='order_failed', actual_sz='0'))),
            ('partial', _algo_detail_response(_native_algo_detail(
                state='canceled', actual_sz='1'))),
            ('missing', {'code': '51603', 'data': []}),
            ('malformed', {'code': '0', 'data': None}),
        )
        for name, response in cases:
            api = _bare_api()
            api.exchange.privateGetTradeOrderAlgo.return_value = response
            api.exchange.privateGetTradeOrdersAlgoPending.side_effect = (
                _algo_stub({}))
            with self.subTest(name=name):
                self.assertFalse(api._cancel_algo_order(
                    'BTC/USDT:USDT', 'stop-1'))

    def test_unverifiable_pending_query_is_failure(self):
        api = _bare_api()
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail()))
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = (
            RuntimeError('超时'))
        self.assertFalse(api._cancel_algo_order(
            'BTC/USDT:USDT', 'stop-1'))


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
        api.exchange.fetch_order.side_effect = lambda order_id, _symbol, params=None: {
            'id': 'timeout-open',
            'clientOrderId': (params or {}).get('clOrdId'),
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'buy', 'amount': 10, 'reduceOnly': False,
            'status': 'closed', 'filled': 10, 'average': None}
        api.exchange.fetch_positions.side_effect = [
            [],                                                # 开仓前：无仓
            [],                                                # 挂单预检后：仍无仓
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT'}],  # 已成交 10 张
        ]
        with patch.object(okx_api, 'time', Mock(sleep=lambda s: None)):
            order = api.open_position('BTC/USDT:USDT', 'long', 0.1)
        self.assertEqual(order['id'], 'timeout-open')
        self.assertAlmostEqual(order['amount'], 0.1)  # 10 张 × 0.01 = 0.1 币

    def test_open_unknown_create_exception_is_resolved_by_client_id(self):
        """create_order 未知异常也可能已到达 OKX，不得直接当作未发单。"""
        api = self._api()
        api.exchange.create_order.side_effect = RuntimeError(
            'POST outcome unknown')
        api.exchange.fetch_order.return_value = {
            'id': 'unknown-open', 'status': 'closed', 'filled': 10,
            'average': 100.0,
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'buy', 'amount': 10, 'reduceOnly': False,
            'clientOrderId': 'trader' + 'a' * 26}
        api.exchange.fetch_positions.side_effect = [
            [],
            [],
            [{'contracts': 10, 'side': 'long',
              'symbol': 'BTC/USDT:USDT'}],
        ]
        fake_uuid = Mock()
        fake_uuid.hex = 'a' * 32
        client_id = 'trader' + 'a' * 26

        with patch.object(okx_api.uuid, 'uuid4', return_value=fake_uuid), \
                patch.object(okx_api, 'time', Mock(sleep=lambda _s: None)):
            order = api.open_position(
                'BTC/USDT:USDT', 'long', 0.1)

        self.assertEqual('unknown-open', order['id'])
        self.assertTrue(order['confirmed'])
        api.exchange.fetch_order.assert_called_once_with(
            client_id, 'BTC/USDT:USDT', params={'clOrdId': client_id})

    def test_close_timeout_confirm_returns_coins(self):
        api = self._api()
        api.exchange.create_order.side_effect = RequestTimeout('超时')
        api.exchange.fetch_order.side_effect = lambda order_id, _symbol, params=None: {
            'id': 'timeout-close',
            'clientOrderId': (params or {}).get('clOrdId'),
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'sell', 'amount': 10, 'reduceOnly': True,
            'status': 'closed', 'filled': 10, 'average': None}
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT'}],  # 平仓前 10 张
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT'}],  # POST 紧前仍为 10 张
            [],                                                # 超时后确认：已平
        ]
        with patch.object(okx_api, 'time', Mock(sleep=lambda s: None)):
            order = api.close_position('BTC/USDT:USDT', 'long', 0.1)
        self.assertEqual(order['id'], 'timeout-close')
        self.assertAlmostEqual(order['amount'], 0.1)

    def test_already_closed_without_client_id_has_complete_close_contract(self):
        api = self._api()
        api.exchange.fetch_positions.return_value = []

        order = api.close_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertEqual('already_closed', order['id'])
        self.assertEqual(0.0, order['remaining_amount'])
        self.assertTrue(order['confirmed'])
        self.assertTrue(order['fully_closed'])
        api.exchange.create_order.assert_not_called()

    def test_flat_without_persisted_close_order_stays_unresolved_after_grace(self):
        api = self._api()
        api.exchange.fetch_positions.return_value = []
        api.exchange.fetch_order.side_effect = OrderNotFound('not visible')

        fake_time = Mock(sleep=Mock())
        with patch.object(okx_api, 'time', fake_time):
            order = api.close_position(
                'BTC/USDT:USDT', 'long', 0.1,
                client_order_id='AlreadyClosed20260718',
                require_existing=True)

        self.assertTrue(order['definitely_no_post'])
        self.assertTrue(order['position_flat_before_post'])
        self.assertEqual(
            api.ORDER_CONFIRM_ATTEMPTS,
            api.exchange.fetch_order.call_count)
        self.assertEqual(
            api.ORDER_CONFIRM_ATTEMPTS - 1,
            fake_time.sleep.call_count)
        api.exchange.create_order.assert_not_called()

    def test_flat_persisted_close_recovers_leg_that_becomes_visible_in_grace(self):
        api = self._api()
        client_id = 'DelayedClose20260718'
        api.exchange.fetch_positions.return_value = []
        api.exchange.fetch_order.side_effect = [
            OrderNotFound('not visible yet'),
            {
                'id': 'close-delayed', 'clientOrderId': client_id,
                'symbol': 'BTC/USDT:USDT', 'type': 'market',
                'side': 'sell', 'amount': 10, 'reduceOnly': True,
                'status': 'closed', 'filled': 10, 'remaining': 0,
                'average': 99.0, 'info': {},
            },
        ]

        fake_time = Mock(sleep=Mock())
        with patch.object(okx_api, 'time', fake_time):
            order = api.close_position(
                'BTC/USDT:USDT', 'long', 0.1,
                client_order_id=client_id, require_existing=True)

        self.assertEqual(
            'closed', TradeExecutorMixin()._classify_close_execution(order))
        self.assertEqual(['close-delayed'], order['ids'])
        self.assertEqual(1, fake_time.sleep.call_count)
        api.exchange.create_order.assert_not_called()

    def test_late_close_order_cannot_consume_intent_if_position_reappears(self):
        api = self._api()
        client_id = 'DelayedCloseRace20260718'
        api.exchange.fetch_positions.side_effect = [
            [],
            [{'contracts': 1, 'side': 'long',
              'symbol': 'BTC/USDT:USDT'}],
        ]
        api.exchange.fetch_order.side_effect = [
            OrderNotFound('not visible yet'),
            {
                'id': 'close-delayed', 'clientOrderId': client_id,
                'symbol': 'BTC/USDT:USDT', 'type': 'market',
                'side': 'sell', 'amount': 10, 'reduceOnly': True,
                'status': 'closed', 'filled': 10, 'remaining': 0,
                'average': 99.0, 'info': {},
            },
        ]

        with patch.object(okx_api, 'time', Mock(sleep=lambda _delay: None)):
            order = api.close_position(
                'BTC/USDT:USDT', 'long', 0.1,
                client_order_id=client_id, require_existing=True)

        self.assertIsNone(order)
        api.exchange.create_order.assert_not_called()


class MarketOrderConfirmationTest(unittest.TestCase):
    def _api(self):
        api = _bare_api()
        api.margin_mode = 'cross'
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api._amount_precision_cache['BTC/USDT:USDT'] = 0
        api._leverage_done = {'BTC/USDT:USDT'}
        api.exchange.amount_to_precision.side_effect = lambda _s, value: str(int(value))
        api._client_order_id = Mock(
            side_effect=lambda value=None: (
                'TESTCID' if value is None else OkxApi._client_order_id(value)))
        return api

    def test_ack_is_polled_and_actual_fill_fields_are_preserved(self):
        api = self._api()
        api.exchange.create_order.return_value = {
            'id': 'ord-1', 'clientOrderId': 'TESTCID'}  # 仅 ACK
        api.exchange.fetch_order.return_value = {
            'id': 'ord-1', 'clientOrderId': 'TESTCID',
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'buy', 'amount': 10, 'reduceOnly': False,
            'status': 'closed', 'filled': 10,
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

    def test_conflicting_native_price_and_fee_are_never_written_to_ledger_result(self):
        api = self._api()
        api.exchange.create_order.return_value = {
            'id': 'ord-finance', 'clientOrderId': 'TESTCID'}
        api.exchange.fetch_order.return_value = {
            'id': 'ord-finance', 'clientOrderId': 'TESTCID',
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'buy', 'amount': 10, 'filled': 10,
            'remaining': 0, 'reduceOnly': False, 'status': 'closed',
            'average': 100, 'fee': {'cost': 1, 'currency': 'USDT'},
            'info': {
                'ordId': 'ord-finance', 'clOrdId': 'TESTCID',
                'instId': 'BTC-USDT-SWAP', 'instType': 'SWAP',
                'ordType': 'market', 'side': 'buy', 'sz': '10',
                'accFillSz': '10', 'reduceOnly': 'false',
                'state': 'filled', 'avgPx': '200',
                'fee': '-2', 'feeCcy': 'USDT',
            },
        }
        api.exchange.fetch_positions.side_effect = [[], [], [{
            'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT',
            'info': {'pos': '10', 'posSide': 'net'},
        }]]

        order = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertTrue(order['confirmed'])
        self.assertTrue(order['execution_ambiguous'])
        for key in ('average', 'cost', 'fee', 'fees'):
            self.assertNotIn(key, order)

    def test_ack_financial_fields_never_fill_gaps_in_authoritative_order(self):
        api = self._api()
        api.exchange.create_order.return_value = {
            'id': 'ord-no-price', 'clientOrderId': 'TESTCID',
            'average': 999, 'fee': {'cost': 9, 'currency': 'USDT'},
        }
        api.exchange.fetch_order.return_value = {
            'id': 'ord-no-price', 'clientOrderId': 'TESTCID',
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'buy', 'amount': 10, 'filled': 10,
            'remaining': 0, 'reduceOnly': False, 'status': 'closed',
        }
        api.exchange.fetch_positions.side_effect = [[], [], [{
            'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT',
            'info': {'posSide': 'net'},
        }]]

        order = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertTrue(order['confirmed'])
        self.assertNotIn('average', order)
        self.assertNotIn('fee', order)

    def test_top_and_native_terminal_status_conflict_is_not_confirmation(self):
        api = self._api()
        with self.assertRaises(RuntimeError):
            api._terminal_fill({
                'status': 'closed', 'amount': 10, 'filled': 10,
                'remaining': 0, 'info': {
                    'state': 'live', 'sz': '10', 'accFillSz': '0',
                },
            }, 10, api._contracts_tolerance('BTC/USDT:USDT'))

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
        api.exchange.create_order.return_value = {
            'id': 'ord-partial', 'clientOrderId': 'TESTCID'}
        api.exchange.fetch_order.return_value = {
            'id': 'ord-partial', 'clientOrderId': 'TESTCID',
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'buy', 'amount': 10, 'reduceOnly': False,
            'status': 'canceled', 'filled': 4, 'average': 99}
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
        api.exchange.create_order.return_value = {
            'id': 'ord-live', 'clientOrderId': 'TESTCID'}
        live = {'id': 'ord-live', 'clientOrderId': 'TESTCID',
                'symbol': 'BTC/USDT:USDT', 'type': 'market',
                'side': 'buy', 'amount': 10, 'reduceOnly': False,
                'status': 'open', 'filled': 4, 'average': 99}
        terminal = {'id': 'ord-live', 'clientOrderId': 'TESTCID',
                    'symbol': 'BTC/USDT:USDT', 'type': 'market',
                    'side': 'buy', 'amount': 10, 'reduceOnly': False,
                    'status': 'canceled', 'filled': 4, 'average': 99}
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
        api.exchange.create_order.return_value = {
            'id': 'ord-live-full', 'clientOrderId': 'TESTCID'}
        live = {'id': 'ord-live-full', 'clientOrderId': 'TESTCID',
                'symbol': 'BTC/USDT:USDT', 'type': 'market',
                'side': 'buy', 'amount': 10, 'reduceOnly': False,
                'status': 'open', 'filled': 10, 'average': 100}
        terminal = {'id': 'ord-live-full', 'clientOrderId': 'TESTCID',
                    'symbol': 'BTC/USDT:USDT', 'type': 'market',
                    'side': 'buy', 'amount': 10, 'reduceOnly': False,
                    'status': 'closed', 'filled': 10, 'average': 100}
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
        api.exchange.create_order.return_value = {
            'id': 'close-partial', 'clientOrderId': 'TESTCID'}
        api.exchange.fetch_order.return_value = {
            'id': 'close-partial', 'clientOrderId': 'TESTCID',
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'sell', 'amount': 10, 'reduceOnly': True,
            'status': 'canceled', 'filled': 6, 'average': 102}
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
            [{'contracts': 4, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
        ]

        order = api.close_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertFalse(order['fully_closed'])
        self.assertAlmostEqual(order['amount'], 0.06)
        self.assertAlmostEqual(order['remaining_amount'], 0.04)

    def test_partial_close_returns_after_one_order_without_supplement(self):
        api = self._api()
        api.exchange.create_order.return_value = {
            'id': 'close-1', 'clientOrderId': 'TESTCID'}
        api.exchange.fetch_order.return_value = {
            'id': 'close-1', 'clientOrderId': 'TESTCID',
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'sell', 'amount': 10, 'reduceOnly': True,
            'status': 'canceled', 'filled': 6, 'remaining': 4,
            'average': 100, 'cost': 600,
            'fee': {'cost': 0.1, 'currency': 'USDT'}}
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
            [{'contracts': 4, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
        ]

        order = api.close_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertFalse(order['fully_closed'])
        self.assertEqual(order['ids'], ['close-1'])
        self.assertAlmostEqual(order['amount'], 0.06)
        self.assertAlmostEqual(order['remaining_amount'], 0.04)
        self.assertAlmostEqual(order['average'], 100.0)
        self.assertAlmostEqual(order['cost'], 600.0)
        self.assertAlmostEqual(order['fee']['cost'], 0.1)
        self.assertIs(order['fees_complete'], True)
        self.assertEqual(api.exchange.create_order.call_count, 1)

    def test_single_order_missing_fee_is_not_exchange_fee_evidence(self):
        api = self._api()
        order = api._build_close_result(
            'BTC/USDT:USDT',
            {'id': 'close-1', 'average': 100, 'cost': 100},
            1, 1, 0)

        self.assertIs(order['fees_complete'], False)
        self.assertEqual(
            (None, None), TradeExecutorMixin._extract_usdt_fee(order))

    def test_financial_aggregation_overflow_is_stripped_and_ambiguous(self):
        api = self._api()
        order = api._build_close_result(
            'BTC/USDT:USDT',
            {'id': 'close-big', 'average': float('inf'),
             'cost': float('inf'),
             'fee': {'cost': float('inf'), 'currency': 'USDT'}},
            1, 1, 0)

        self.assertTrue(order['execution_ambiguous'])
        for key in ('average', 'cost', 'fee', 'fees'):
            self.assertNotIn(key, order)
        self.assertEqual(
            (None, None),
            TradeExecutorMixin._extract_usdt_fee(order))

    def test_single_order_missing_price_and_cost_is_incomplete(self):
        api = self._api()
        order = api._build_close_result(
            'BTC/USDT:USDT', {'id': 'close-1'}, 10, 10, 0)

        self.assertIs(order['price_complete'], False)
        self.assertIs(order['cost_complete'], False)
        self.assertTrue(order['financial_evidence_incomplete'])
        self.assertIsNone(order.get('average'))
        self.assertIsNone(order.get('cost'))

    def test_concurrent_external_close_does_not_misattribute_order_price_or_fee(self):
        api = self._api()
        api.exchange.create_order.return_value = {
            'id': 'close-race', 'clientOrderId': 'TESTCID'}
        api.exchange.fetch_order.return_value = {
            'id': 'close-race', 'clientOrderId': 'TESTCID',
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'sell', 'amount': 10, 'reduceOnly': True,
            'status': 'canceled', 'filled': 6, 'remaining': 4,
            'average': 100, 'fee': {'cost': 0.1, 'currency': 'USDT'},
        }
        # 订单仅报 6 张，但仓位从 10 直接归零：其余 4 张可能由止损/人工平掉。
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
            [{'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
            []]

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
            'clientOrderId': 'btcOrder20260710',
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

    def test_recovery_only_open_never_posts_after_visibility_grace_absence(self):
        api = self._api()
        api.ORDER_CONFIRM_ATTEMPTS = 2
        api.exchange.fetch_order.side_effect = OrderNotFound('old open absent')

        fake_time = Mock(sleep=Mock())
        with patch.object(okx_api, 'time', fake_time):
            order = api.open_position(
                'BTC/USDT:USDT', 'long', 0.1,
                client_order_id='RecoverOnlyOpen123',
                require_existing=True)

        self.assertIsNone(order)
        self.assertEqual(2, api.exchange.fetch_order.call_count)
        self.assertEqual(1, fake_time.sleep.call_count)
        api.exchange.fetch_positions.assert_not_called()
        api.exchange.create_order.assert_not_called()

    def test_recovery_only_open_finds_late_visible_order_without_repost(self):
        api = self._api()
        client_id = 'RecoverLateOpen123'
        existing = {
            'id': 'open-existing', 'symbol': 'BTC/USDT:USDT',
            'clientOrderId': client_id,
            'type': 'market', 'side': 'buy', 'amount': 10,
            'reduceOnly': False, 'status': 'closed', 'filled': 10,
            'remaining': 0, 'average': 100,
        }
        api.exchange.fetch_order.side_effect = [
            OrderNotFound('index lag'), existing, existing]
        api.exchange.fetch_positions.return_value = [
            {'contracts': 10, 'side': 'long',
             'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}]

        with patch.object(okx_api, 'time', Mock(sleep=Mock())):
            order = api.open_position(
                'BTC/USDT:USDT', 'long', 0.1,
                client_order_id=client_id, require_existing=True)

        self.assertTrue(order['confirmed'])
        api.exchange.create_order.assert_not_called()

    def test_recovered_zero_fill_order_does_not_claim_or_close_manual_position(self):
        api = self._api()
        existing = {
            'id': 'ord-zero', 'symbol': 'BTC/USDT:USDT',
            'clientOrderId': 'btcRecoveredZeroFill',
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
            'clientOrderId': 'btcRecoveredPartialManual',
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
            'clientOrderId': 'btcRecoveredNoFilled',
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
            'clientOrderId': 'btcOrderCollision',
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
            'clientOrderId': 'btcClose20260710',
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

    def test_nonflat_base_leg_visible_during_grace_is_never_reposted(self):
        api = self._api()
        base_id = 'CloseBaseLateVisible'
        existing = {
            'id': 'close-late', 'clientOrderId': base_id,
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'sell', 'amount': 10, 'reduceOnly': True,
            'status': 'closed', 'filled': 10, 'remaining': 0,
            'average': 105,
        }
        api.exchange.fetch_order.side_effect = [
            OrderNotFound('base index lag'), existing]
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 10, 'side': 'long',
              'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
            [],
        ]

        fake_time = Mock(sleep=Mock())
        with patch.object(okx_api, 'time', fake_time):
            order = api.close_position(
                'BTC/USDT:USDT', 'long', 0.1,
                client_order_id=base_id, require_existing=True)

        self.assertTrue(order['fully_closed'])
        self.assertEqual(['close-late'], order['ids'])
        self.assertEqual(1, fake_time.sleep.call_count)
        api.exchange.create_order.assert_not_called()

    def test_fresh_close_posts_once_after_absent_precheck(self):
        api = self._api()
        api.ORDER_CONFIRM_ATTEMPTS = 2
        base_id = 'CloseBaseAbsentProof'
        terminal = {
            'id': 'close-new', 'clientOrderId': base_id,
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'sell', 'amount': 10, 'reduceOnly': True,
            'status': 'closed', 'filled': 10, 'remaining': 0,
            'average': 105,
        }
        api.exchange.fetch_order.side_effect = [
            OrderNotFound('base absent once'),
            OrderNotFound('base absent after grace'),
            terminal,
        ]
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 10, 'side': 'long',
              'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
            [{'contracts': 10, 'side': 'long',
              'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
            [],
            [],
        ]
        api.exchange.create_order.return_value = {
            'id': 'close-new', 'clientOrderId': base_id}

        fake_time = Mock(sleep=Mock())
        with patch.object(okx_api, 'time', fake_time):
            order = api.close_position(
                'BTC/USDT:USDT', 'long', 0.1,
                client_order_id=base_id)

        self.assertTrue(order['fully_closed'])
        self.assertEqual(1, fake_time.sleep.call_count)
        api.exchange.create_order.assert_called_once()

    def test_close_recovery_returns_single_terminal_partial_without_post(self):
        api = self._api()
        client_id = 'CloseIntentPartial'
        api.exchange.fetch_order.return_value = {
            'id': 'close-only', 'clientOrderId': client_id,
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'sell', 'amount': 10, 'reduceOnly': True,
            'status': 'canceled', 'filled': 6, 'remaining': 4,
            'average': 100,
        }
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 4, 'side': 'long',
              'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
            [{'contracts': 4, 'side': 'long',
              'symbol': 'BTC/USDT:USDT', 'info': {'posSide': 'net'}}],
        ]

        order = api.close_position(
            'BTC/USDT:USDT', 'long', 0.1,
            client_order_id=client_id, require_existing=True)

        self.assertFalse(order['fully_closed'])
        self.assertEqual(['close-only'], order['ids'])
        self.assertEqual([client_id], order['clientOrderIds'])
        self.assertAlmostEqual(0.06, order['amount'])
        self.assertAlmostEqual(0.04, order['remaining_amount'])
        api.exchange.create_order.assert_not_called()

    def test_close_recovery_absent_returns_explicit_no_post_evidence(self):
        api = self._api()
        client_id = 'CloseIntentAbsent'
        api.exchange.fetch_order.side_effect = OrderNotFound('absent')
        position = [{'contracts': 10, 'side': 'long',
                     'symbol': 'BTC/USDT:USDT',
                     'info': {'posSide': 'net'}}]
        api.exchange.fetch_positions.side_effect = [position, position]

        result = api.close_position(
            'BTC/USDT:USDT', 'long', 0.1,
            client_order_id=client_id, require_existing=True)

        self.assertTrue(result['definitely_no_post'])
        self.assertTrue(result['close_order_absent'])
        self.assertTrue(result['position_unchanged'])
        api.exchange.create_order.assert_not_called()

    def test_close_recovery_mismatch_with_live_residual_fails_closed(self):
        api = self._api()
        base_id = 'CloseIntentMismatch'
        api.exchange.fetch_order.return_value = {
            'id': 'close-only', 'clientOrderId': base_id,
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'sell', 'amount': 10, 'reduceOnly': True,
            'status': 'canceled', 'filled': 5, 'remaining': 5,
            'average': 100,
        }
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
            return_value={
                'id': 'open-uncertain', 'clientOrderId': 'TESTCID',
                'symbol': 'BTC/USDT:USDT', 'type': 'market',
                'side': 'buy', 'amount': 10, 'filled': 0,
                'remaining': 10, 'reduceOnly': False,
                'status': 'canceled', 'info': {
                    'ordId': 'open-uncertain', 'clOrdId': 'TESTCID',
                    'instId': 'BTC-USDT-SWAP', 'instType': 'SWAP',
                    'ordType': 'market', 'side': 'buy', 'sz': '10',
                    'accFillSz': '0', 'reduceOnly': 'false',
                    'state': 'canceled',
                }})

        result = api.open_position('BTC/USDT:USDT', 'long', 0.1)

        self.assertTrue(result['open_execution_compensated'])
        self.assertEqual(result['status'], 'compensated_flat')
        self.assertEqual(result['remaining_amount'], 0.0)
        self.assertEqual(result['compensation']['id'], 'close-full')
        self.assertNotIn('average', result)

    def test_full_compensation_requires_strict_zero_remaining_amount(self):
        for malformed in (None, 0.1, True, float('nan')):
            with self.subTest(remaining_amount=malformed):
                api = self._api()
                api.get_position = Mock(return_value=None)
                api.exchange.create_order.return_value = {'id': 'open-uncertain'}
                api._confirm_market_order = Mock(side_effect=[
                    (None, 10.0), (None, 10.0),
                ])
                compensation = {
                    'id': 'close-malformed', 'confirmed': True,
                    'fully_closed': True, 'amount': 0.1,
                }
                if malformed is not None:
                    compensation['remaining_amount'] = malformed
                api.close_position = Mock(return_value=compensation)
                api._fetch_order_for_confirmation = Mock(
                    return_value={'id': 'open-uncertain', 'status': 'canceled'})

                result = api.open_position(
                    'BTC/USDT:USDT', 'long', 0.1)

                self.assertFalse(
                    result.get('open_execution_compensated', False))
                self.assertTrue(result['open_execution_unresolved'])
                self.assertEqual('compensation_incomplete', result['status'])
                self.assertGreater(result['remaining_amount'], 0.0)

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
            'clientOrderId': 'TpendingLookup123',
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

    def test_existing_order_financial_conflict_is_sanitized_at_public_boundary(self):
        api = self._api()
        api.exchange.fetch_order.return_value = self._order(
            average=100.0,
            fee={'currency': 'USDT', 'cost': 0.01},
            info={
                'clOrdId': self.CLIENT_ID,
                'instId': 'BTC-USDT-SWAP',
                'avgPx': '101.0',
                'fee': '-0.02',
                'feeCcy': 'USDT',
            },
        )

        order = api.find_existing_open_order(
            'BTC/USDT:USDT', 'long', 0.1, self.CLIENT_ID)

        self.assertTrue(order['execution_ambiguous'])
        self.assertNotIn('average', order)
        self.assertNotIn('fee', order)
        self.assertNotIn('fees', order)

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
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail()))
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _phased_algo_stub(
            [[_native_stop()], []])  # 首轮仍在列表，复查轮已消失
        self.assertTrue(api._cancel_algo_order('BTC/USDT:USDT', 'stop-1'))

    def test_still_listed_after_recheck_is_failure(self):
        """复查后仍在列表：确实没撤掉 → 失败（残留标记生效）。"""
        api = _bare_api()
        api.exchange.cancel_order.return_value = {'id': 'stop-1'}
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail()))
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop()]})
        self.assertFalse(api._cancel_algo_order('BTC/USDT:USDT', 'stop-1'))


class FetchAlgoNativeTest(unittest.TestCase):
    """原生端点查询：单一权威来源，信封校验 + 全 ordType 覆盖 + 任一失败即抛。"""

    def test_queries_all_ord_types_and_merges(self):
        """含 smart_iceberg 在内的全部算法类型都必须查询并合并。"""
        api = _bare_api()
        seen_types = []

        def record(params):
            seen_types.append(params.get('ordType'))
            return _algo_stub({
                'conditional': [_native_stop()],
                'trigger': [{'algoId': 'manual-1',
                             'instType': 'SWAP', 'instId': 'BTC-USDT-SWAP',
                             'side': 'buy', 'sz': '2',
                             'triggerPx': '60000', 'ordType': 'trigger'}],
                'smart_iceberg': [{
                    'algoId': 'smart-1', 'instType': 'SWAP',
                    'instId': 'BTC-USDT-SWAP', 'side': 'sell', 'sz': '3',
                    'ordType': 'smart_iceberg'}],
            })(params)

        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = record
        orders = api._fetch_algo_orders('BTC/USDT:USDT')
        self.assertEqual(
            sorted(o['id'] for o in orders),
            ['manual-1', 'smart-1', 'stop-1'])
        self.assertEqual(tuple(seen_types), OkxApi.ALGO_ORDER_TYPES)
        self.assertIn('chase', seen_types)
        self.assertIn('smart_iceberg', seen_types)

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

    def test_pending_items_must_match_requested_symbol_and_order_type(self):
        wrong_algo_symbol = _native_stop()
        wrong_algo_symbol['instId'] = 'ETH-USDT-SWAP'
        wrong_algo_type = _native_stop()
        wrong_algo_type['ordType'] = 'oco'
        for item in (wrong_algo_symbol, wrong_algo_type):
            api = _bare_api()
            api.exchange.privateGetTradeOrdersAlgoPending.side_effect = (
                _algo_stub({'conditional': [item]}))
            with self.subTest(item=item), self.assertRaises(RuntimeError):
                api._fetch_algo_orders('BTC/USDT:USDT')

        api = _bare_api()
        wrong_normal = _native_normal()
        wrong_normal['instId'] = 'ETH-USDT-SWAP'
        api.exchange.privateGetTradeOrdersPending.return_value = {
            'code': '0', 'data': [wrong_normal]}
        with self.assertRaises(RuntimeError):
            api._fetch_normal_orders('BTC/USDT:USDT')

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

    def test_cancel_all_never_writes_when_position_is_open_or_unknown(self):
        position = {
            'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT',
            'info': {'posSide': 'net'}}
        for observed in (position, RuntimeError('unknown')):
            api = _bare_api()
            api.get_position = (
                Mock(side_effect=observed) if isinstance(observed, Exception)
                else Mock(return_value=observed))
            with self.subTest(observed=observed):
                self.assertFalse(api.cancel_all_orders('BTC/USDT:USDT'))
                api.exchange.cancel_all_orders.assert_not_called()
                api.exchange.privatePostTradeCancelBatchOrders.assert_not_called()
                api.exchange.privatePostTradeCancelAlgos.assert_not_called()

    def test_cancel_all_rechecks_flat_after_snapshot_before_any_write(self):
        api = _bare_api()
        api.get_position = Mock(side_effect=[None, {
            'contracts': 10, 'side': 'long', 'symbol': 'BTC/USDT:USDT',
            'info': {'posSide': 'net'},
        }])
        api._fetch_pending_snapshot = Mock(return_value=(
            [_native_normal()], [_native_stop()]))

        self.assertFalse(api.cancel_all_orders('BTC/USDT:USDT'))
        api.exchange.cancel_all_orders.assert_not_called()
        api.exchange.privatePostTradeCancelBatchOrders.assert_not_called()
        api.exchange.privatePostTradeCancelAlgos.assert_not_called()

    def test_cancel_all_catches_algorithm_order_that_appears_late(self):
        api = _bare_api()
        api.exchange.cancel_all_orders.return_value = None
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail(
                algo_id='late-stop')))
        late = {'id': 'late-stop'}
        api._fetch_pending_snapshot = Mock(side_effect=[
            ([], []), ([], []), ([], [late]), ([], []), ([], []),
        ])
        api._request_cancel_algo_orders = Mock()

        self.assertTrue(api.cancel_all_orders('BTC/USDT:USDT'))
        api._request_cancel_algo_orders.assert_any_call(
            'BTC/USDT:USDT', ['late-stop'])

    def test_cancel_all_queries_and_cleans_smart_iceberg(self):
        api = _bare_api()
        calls = {'count': 0}
        per_snapshot = len(OkxApi.ALGO_ORDER_TYPES)

        def pending(params):
            snapshot = calls['count'] // per_snapshot
            calls['count'] += 1
            data = []
            if snapshot == 0 and params.get('ordType') == 'smart_iceberg':
                data = [{
                    'algoId': 'smart-1', 'instType': 'SWAP',
                    'instId': 'BTC-USDT-SWAP', 'side': 'sell', 'sz': '3',
                    'ordType': 'smart_iceberg'}]
            return {'code': '0', 'msg': '', 'data': data}

        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = pending
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail(
                algo_id='smart-1')))

        self.assertTrue(api.cancel_all_orders('BTC/USDT:USDT'))
        api.exchange.privatePostTradeCancelAlgos.assert_called_once_with([
            {'algoId': 'smart-1', 'instId': 'BTC-USDT-SWAP'},
        ])
        asked = [
            call.args[0]['ordType']
            for call in api.exchange.privateGetTradeOrdersAlgoPending.call_args_list]
        self.assertIn('smart_iceberg', asked)

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
                'ordId': 'late-normal', 'instType': 'SWAP',
                'instId': 'BTC-USDT-SWAP',
                'state': 'canceled', 'accFillSz': '0'}]}

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


class CancelOrderTypeAndTerminalTest(unittest.TestCase):
    def test_public_cancel_ids_reject_type_confusion_before_any_exchange_io(self):
        for method in ('cancel_order', 'cancel_stop_order_only'):
            for bad in (True, 123, {}, [], '', ' spaced ', 'bad/id'):
                api = _bare_api()
                with self.subTest(method=method, bad=bad):
                    self.assertFalse(getattr(api, method)(
                        'BTC/USDT:USDT', bad))
                    api.exchange.privateGetTradeOrdersPending.assert_not_called()
                    api.exchange.privateGetTradeOrder.assert_not_called()
                    api.exchange.privateGetTradeOrderAlgo.assert_not_called()
                    api.exchange.privatePostTradeCancelOrder.assert_not_called()
                    api.exchange.privatePostTradeCancelAlgos.assert_not_called()

    def test_normal_order_is_cancelled_and_verified_in_native_pending_list(self):
        api = _bare_api()
        phases = [[_native_normal('normal-1')], []]

        def pending(_params):
            data = phases.pop(0) if phases else []
            return {'code': '0', 'data': data}

        api.exchange.privateGetTradeOrdersPending.side_effect = pending
        api.exchange.privateGetTradeOrder.return_value = {
            'code': '0', 'data': [{
                'ordId': 'normal-1', 'instType': 'SWAP',
                'instId': 'BTC-USDT-SWAP',
                'state': 'canceled', 'accFillSz': '0'}]}

        self.assertTrue(api.cancel_order('BTC/USDT:USDT', 'normal-1'))
        api.exchange.privatePostTradeCancelOrder.assert_called_once_with({
            'instId': 'BTC-USDT-SWAP', 'ordId': 'normal-1'})

    def test_algo_cancelled_detail_but_still_pending_is_failure(self):
        api = _bare_api()
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail()))
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop()]})

        self.assertFalse(api.cancel_order('BTC/USDT:USDT', 'stop-1'))

    def test_algo_type_cancelled_zero_and_absent_is_success(self):
        api = _bare_api()
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail()))
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({})

        self.assertTrue(api.cancel_order('BTC/USDT:USDT', 'stop-1'))

    def test_missing_from_both_order_domains_is_failure(self):
        api = _bare_api()
        self.assertFalse(api.cancel_order('BTC/USDT:USDT', 'ghost-1'))


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

    def test_wrong_symbol_pending_stop_is_never_adopted(self):
        api = self._api()
        wrong = _native_stop(client_id=self.CLIENT_ID)
        wrong['instId'] = 'ETH-USDT-SWAP'
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({
            'conditional': [wrong],
        })

        self.assertIsNone(api.create_stop_loss_order(
            'BTC/USDT:USDT', 'long', 0.1, 55000,
            client_order_id=self.CLIENT_ID))
        api.exchange.privatePostTradeOrderAlgo.assert_not_called()

    def test_invalid_side_or_amount_never_posts_or_reads_position(self):
        for side, amount in (
                ('invalid', 0.1), ('long', 0), ('long', -1), ('long', 0.001),
                ('long', float('nan')), ('long', float('inf')),
                ('long', True), ('long', False), ('long', 'garbage')):
            api = self._api()
            with self.subTest(side=side, amount=amount):
                self.assertIsNone(api.create_stop_loss_order(
                    'BTC/USDT:USDT', side, amount, 55000,
                    client_order_id=self.CLIENT_ID))
                api.exchange.fetch_positions.assert_not_called()
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

    def test_malformed_ack_or_unknown_exception_confirms_same_id_without_repost(self):
        for outcome in ({'code': '0', 'data': []}, RuntimeError('parser failed')):
            api = self._api()
            api.exchange.privateGetTradeOrdersAlgoPending.side_effect = (
                _phased_algo_stub([
                    [], [_native_stop(client_id=self.CLIENT_ID)],
                ]))
            if isinstance(outcome, Exception):
                api.exchange.privatePostTradeOrderAlgo.side_effect = outcome
            else:
                api.exchange.privatePostTradeOrderAlgo.return_value = outcome

            with self.subTest(outcome=outcome):
                order = api.create_stop_loss_order(
                    'BTC/USDT:USDT', 'long', 0.1, 55000,
                    client_order_id=self.CLIENT_ID)
                self.assertEqual('stop-1', order['id'])
                api.exchange.privatePostTradeOrderAlgo.assert_called_once()

    def test_explicit_stop_business_rejection_is_not_misreported_as_uncertain(self):
        api = self._api()
        api.exchange.privatePostTradeOrderAlgo.return_value = {
            'code': '51000', 'msg': 'rejected', 'data': []}

        self.assertIsNone(api.create_stop_loss_order(
            'BTC/USDT:USDT', 'long', 0.1, 55000,
            client_order_id=self.CLIENT_ID))
        self.assertEqual(
            len(OkxApi.ALGO_ORDER_TYPES),
            api.exchange.privateGetTradeOrdersAlgoPending.call_count)

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

    def test_amount_and_price_formatters_are_each_used_exactly_once(self):
        """第二次 formatter 即使被投毒，也不能改变幂等预查后的 POST 内容。"""
        api = self._api()
        api.exchange.amount_to_precision.side_effect = ['10', True]
        api.exchange.price_to_precision.side_effect = ['55000', True]
        captured = {}

        def post_algo(request):
            captured.update(request)
            return {'code': '0', 'data': [{
                'algoId': 'stop-1',
                'algoClOrdId': request['algoClOrdId'],
                'sCode': '0'}]}

        def pending(params):
            data = []
            if captured and params.get('ordType') == 'conditional':
                data = [_native_stop(
                    client_id=captured['algoClOrdId'], px='55000', sz='10')]
            return {'code': '0', 'msg': '', 'data': data}

        api.exchange.privatePostTradeOrderAlgo.side_effect = post_algo
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = pending

        order = api.create_stop_loss_order(
            'BTC/USDT:USDT', 'long', 0.1, 55000)

        self.assertEqual('stop-1', order['id'])
        self.assertEqual(1, api.exchange.amount_to_precision.call_count)
        self.assertEqual(1, api.exchange.price_to_precision.call_count)
        self.assertEqual('10.0', captured['sz'])
        self.assertEqual('55000.0', captured['slTriggerPx'])
        api.exchange.privatePostTradeOrderAlgo.assert_called_once()


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

    def test_align_rejects_when_precision_unavailable(self):
        """精度元数据不可得时不能拿未经 tick 对齐的原价继续创建。"""
        api = _bare_api()
        api.exchange.price_to_precision = Mock(side_effect=RuntimeError('markets 未加载'))
        with self.assertRaises(ValueError):
            api._align_stop_price('BTC/USDT:USDT', 39.384)
        api.exchange.privatePostTradeOrderAlgo.assert_not_called()

    def test_invalid_input_price_never_posts(self):
        for value in (True, float('nan'), float('inf'), float('-inf'), 0, -1):
            api = self._api()
            with self.subTest(value=value), self.assertRaises(ValueError):
                api.create_stop_loss_order(
                    'BTC/USDT:USDT', 'long', 0.1, value)
            api.exchange.privatePostTradeOrderAlgo.assert_not_called()

    def test_invalid_price_formatter_result_never_posts(self):
        for result in (
                True, float('nan'), float('inf'), float('-inf'), 0, -1,
                'garbage'):
            api = self._api()
            api.exchange.price_to_precision = Mock(return_value=result)
            with self.subTest(result=result), self.assertRaises(ValueError):
                api.create_stop_loss_order(
                    'BTC/USDT:USDT', 'long', 0.1, 55000)
            api.exchange.privatePostTradeOrderAlgo.assert_not_called()


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
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail()))
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop(algo_id='other', sz='5', px='55000')]})
        self.assertEqual(api.find_stop_order_state(
            'BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'), 'mismatch')

    def test_id_present_but_content_differs_is_mismatch(self):
        api = self._api()
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub(
            {'conditional': [_native_stop(sz='5', px='50000')]})
        self.assertEqual(api.find_stop_order_state('BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'), 'mismatch')

    def test_cancelled_zero_detail_and_empty_list_is_missing(self):
        api = self._api()
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail()))
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({})
        self.assertEqual(api.find_stop_order_state('BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'), 'missing')

    def test_zero_fill_inactive_detail_allows_adopting_unique_replacement(self):
        for state in ('canceled', 'order_failed'):
            api = self._api()
            api.exchange.privateGetTradeOrderAlgo.return_value = (
                _algo_detail_response(_native_algo_detail(
                    algo_id='stop-old', state=state)))
            api.exchange.privateGetTradeOrdersAlgoPending.side_effect = (
                _algo_stub({
                    'conditional': [_native_stop(algo_id='stop-new')],
                }))
            with self.subTest(state=state):
                self.assertEqual(
                    api.find_stop_order_state(
                        'BTCUSDT', 'long', 0.1, 55000.0, 'stop-old'),
                    {'state': 'adoptable', 'order_id': 'stop-new'})

    def test_multiple_matching_stops_are_mismatch_not_adoptable(self):
        api = self._api()
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail(
                algo_id='stop-old')))
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

    def test_order_failed_zero_detail_and_no_replacement_is_missing(self):
        api = self._api()
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail(
                state='order_failed')))
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({})

        self.assertEqual(api.find_stop_order_state(
            'BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'), 'missing')

    def test_unsafe_or_unknown_terminal_never_becomes_missing(self):
        cases = (
            ('effective', _algo_detail_response(_native_algo_detail(
                state='effective', actual_sz='10'))),
            ('partial_cancel', _algo_detail_response(_native_algo_detail(
                state='canceled', actual_sz='1'))),
            ('partial_failed', _algo_detail_response(_native_algo_detail(
                state='order_failed', actual_sz='1'))),
            ('absent_detail', {'code': '51603', 'data': []}),
        )
        for name, response in cases:
            api = self._api()
            api.exchange.privateGetTradeOrderAlgo.return_value = response
            api.exchange.privateGetTradeOrdersAlgoPending.side_effect = (
                _algo_stub({}))
            with self.subTest(name=name):
                self.assertEqual(api.find_stop_order_state(
                    'BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'),
                    'mismatch')

    def test_malformed_or_wrong_identity_detail_raises(self):
        cases = (
            {'code': '0', 'data': None},
            _algo_detail_response(dict(
                _native_algo_detail(), instId='ETH-USDT-SWAP')),
            _algo_detail_response(dict(
                _native_algo_detail(), algoId='other-stop')),
        )
        for response in cases:
            api = self._api()
            api.exchange.privateGetTradeOrderAlgo.return_value = response
            api.exchange.privateGetTradeOrdersAlgoPending.side_effect = (
                _algo_stub({}))
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                api.find_stop_order_state(
                    'BTCUSDT', 'long', 0.1, 55000.0, 'stop-1')

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


class ConfirmStopExecutionTest(unittest.TestCase):
    """触发成功必须由精确算法单终态与唯一全额成交子单共同证明。"""

    def _api(self):
        api = _bare_api()
        api._contract_size_cache['BTC/USDT:USDT'] = 0.01
        api._amount_precision_cache['BTC/USDT:USDT'] = 0
        api.exchange.amount_to_precision.return_value = '10'
        api.exchange.price_to_precision.side_effect = (
            lambda _symbol, value: str(float(value)))
        return api

    @staticmethod
    def _effective_detail(**changes):
        detail = _native_algo_detail(
            state='effective', actual_sz='10', child_ids=['child-1'])
        detail.update(changes)
        return detail

    def test_exact_effective_algo_and_unique_filled_child_pass(self):
        api = self._api()
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(self._effective_detail()))
        api.exchange.privateGetTradeOrder.return_value = {
            'code': '0', 'data': [_native_stop_child()]}

        self.assertTrue(api.confirm_stop_execution(
            'BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'))
        api.exchange.privateGetTradeOrderAlgo.assert_called_once_with({
            'algoId': 'stop-1'})
        api.exchange.privateGetTradeOrder.assert_called_once_with({
            'instId': 'BTC-USDT-SWAP', 'ordId': 'child-1'})

    def test_wrong_child_attribution_or_incomplete_fill_fails(self):
        variants = []
        wrong_algo = _native_stop_child(algo_id='other-stop')
        variants.append(('wrong_algo_id', wrong_algo))
        partial = _native_stop_child(filled='5')
        variants.append(('partial_fill', partial))
        missing_reduce_only = _native_stop_child()
        missing_reduce_only.pop('reduceOnly')
        variants.append(('missing_reduce_only', missing_reduce_only))
        wrong_inst_type = _native_stop_child(inst_type='FUTURES')
        variants.append(('wrong_inst_type', wrong_inst_type))

        for name, child in variants:
            api = self._api()
            api.exchange.privateGetTradeOrderAlgo.return_value = (
                _algo_detail_response(self._effective_detail()))
            api.exchange.privateGetTradeOrder.return_value = {
                'code': '0', 'data': [child]}
            with self.subTest(name=name):
                self.assertFalse(api.confirm_stop_execution(
                    'BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'))

    def test_wrong_algo_inst_type_fails_before_child_lookup(self):
        api = self._api()
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(self._effective_detail(
                instType='FUTURES')))

        self.assertFalse(api.confirm_stop_execution(
            'BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'))
        api.exchange.privateGetTradeOrder.assert_not_called()

    def test_multiple_child_ids_are_ambiguous_and_fail(self):
        api = self._api()
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(self._effective_detail(
                ordIdList=['child-1', 'child-2'])))

        self.assertFalse(api.confirm_stop_execution(
            'BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'))
        api.exchange.privateGetTradeOrder.assert_not_called()


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
        self.assertFalse(OkxApi._algo_order_matches(
            dict(self.GOOD, reduceOnly=1), 'sell', 98.5, 25.0))
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
        api._client_order_id = Mock(return_value=self.CLIENT_ID)
        api.exchange.fetch_positions.return_value = []
        api.exchange.create_order.return_value = {
            'id': 'ack-1', 'clientOrderId': self.CLIENT_ID}
        api._confirm_market_order = Mock(side_effect=[(None, 0.0), (None, 0.0)])
        api.exchange.fetch_order.return_value = {
            'id': 'ack-1', 'clientOrderId': self.CLIENT_ID,
            'status': 'open', 'type': 'market',
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
            'clientOrderId': self.CLIENT_ID,
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
        api.exchange.set_leverage.side_effect = RuntimeError('permission denied')

        with self.assertRaises(RuntimeError):
            api.setup_symbol('BTC/USDT:USDT')

        api.exchange.set_leverage.assert_called_once()

    def test_set_leverage_ack_must_bind_symbol_mode_and_value(self):
        bad_responses = (
            None,
            {'code': '0', 'data': []},
            {'code': '1', 'data': []},
            {'code': '0', 'data': [{
                'instId': 'ETH-USDT-SWAP', 'lever': '5',
                'mgnMode': 'cross'}]},
            {'code': '0', 'data': [{
                'instId': 'BTC-USDT-SWAP', 'lever': '10',
                'mgnMode': 'cross'}]},
            {'code': '0', 'data': [{
                'instId': 'BTC-USDT-SWAP', 'lever': '5',
                'mgnMode': 'isolated'}]},
            {'code': '0', 'data': [{
                'instId': 'BTC-USDT-SWAP', 'lever': True,
                'mgnMode': 'cross'}]},
        )
        for response in bad_responses:
            api = _bare_api()
            api.margin_mode = 'cross'
            api.default_leverage = 5
            api.leverage_overrides = {}
            api.exchange.set_leverage.return_value = response
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                api.setup_symbol('BTC/USDT:USDT')
            api.exchange.set_leverage.assert_called_once()

        api = _bare_api()
        api.margin_mode = 'cross'
        api.default_leverage = 5
        api.leverage_overrides = {}
        api.exchange.set_leverage.return_value = {
            'code': '0', 'data': [{
                'instId': 'BTC-USDT-SWAP', 'lever': '5',
                'mgnMode': 'cross', 'posSide': 'net'}]}
        api.setup_symbol('BTC/USDT:USDT')
        api.setup_symbol('BTC/USDT:USDT')
        self.assertEqual(2, api.exchange.set_leverage.call_count)


class SymbolDomainBoundaryTest(unittest.TestCase):
    def test_only_internal_or_exact_usdt_swap_symbols_are_accepted(self):
        api = _bare_api()
        self.assertEqual('BTC/USDT:USDT', api._resolve_symbol('BTCUSDT'))
        self.assertEqual(
            'BTC/USDT:USDT', api._resolve_symbol('BTC/USDT:USDT'))

        invalid = (
            'BTC/USDT', 'BTC/USD:BTC', 'btc/USDT:USDT',
            'BTC-USDT-SWAP', 'BTC//USDT:USDT', '', None, True,
        )
        for symbol in invalid:
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                api._resolve_symbol(symbol)

    def test_invalid_market_domain_never_reaches_any_write_api(self):
        for operation in ('open', 'close', 'stop', 'cancel_all'):
            api = _bare_api()
            api.margin_mode = 'cross'
            api._contract_size_cache['BTC/USDT:USDT'] = 0.01
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                if operation == 'open':
                    api.open_position('BTC/USDT', 'long', 0.1)
                elif operation == 'close':
                    api.close_position('BTC/USDT', 'long', 0.1)
                elif operation == 'stop':
                    api.create_stop_loss_order('BTC/USDT', 'long', 0.1, 100)
                else:
                    api.cancel_all_orders('BTC/USDT')
            api.exchange.create_order.assert_not_called()
            api.exchange.cancel_all_orders.assert_not_called()
            api.exchange.privatePostTradeOrderAlgo.assert_not_called()
            api.exchange.privatePostTradeCancelAlgos.assert_not_called()

    def test_invalid_market_domain_never_reaches_market_data_api(self):
        api = _bare_api()
        for method, args in (
                ('get_last_price', ('BTC/USDT',)),
                ('fetch_ohlcv', ('BTC/USDT', '1d', 10))):
            with self.subTest(method=method), self.assertRaises(ValueError):
                getattr(api, method)(*args)
        api.exchange.fetch_ticker.assert_not_called()
        api.exchange.fetch_ohlcv.assert_not_called()


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

    def test_position_metadata_types_are_not_silently_normalized(self):
        malformed = (
            {'symbol': 'BTC/USDT:USDT', 'contracts': 5, 'side': 'long',
             'info': []},
            {'symbol': 'BTC/USDT:USDT', 'contracts': 5, 'side': 'long',
             'hedged': 'false', 'info': {'pos': '5', 'posSide': 'net'}},
            {'symbol': 'BTC/USDT:USDT', 'contracts': 5, 'side': 'LONG',
             'info': {'pos': '5', 'posSide': 'net'}},
            {'symbol': 'BTC/USDT:USDT', 'contracts': 5, 'side': 'long',
             'info': {'pos': '5', 'posSide': 'garbage'}},
        )
        for position in malformed:
            api = _bare_api()
            api.exchange.fetch_positions.return_value = [position]
            with self.subTest(position=position), \
                    self.assertRaises(PositionModeError):
                api.get_position('BTC/USDT:USDT')

    def test_side_contradicting_raw_pos_sign_rejected(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDT:USDT', 'contracts': 5.0,
             'side': 'long', 'info': {'pos': '-5'}}]
        with self.assertRaises(PositionModeError):
            api.get_position('BTC/USDT:USDT')

    def test_standard_and_native_nonzero_sizes_must_agree(self):
        for contracts, side, raw_pos in (
                (10, 'long', '9'), (-10, 'short', '-9')):
            api = _bare_api()
            api.exchange.fetch_positions.return_value = [{
                'symbol': 'BTC/USDT:USDT', 'contracts': contracts,
                'side': side, 'info': {'pos': raw_pos, 'posSide': 'net'},
            }]
            with self.subTest(contracts=contracts, raw_pos=raw_pos), \
                    self.assertRaises(PositionModeError):
                api.get_position('BTC/USDT:USDT')

        api = _bare_api()
        api._amount_precision_cache['BTC/USDT:USDT'] = 0
        api.exchange.fetch_positions.return_value = [{
            'symbol': 'BTC/USDT:USDT', 'contracts': 10 + 5e-10,
            'side': 'long', 'info': {'pos': '10', 'posSide': 'net'},
        }]
        self.assertAlmostEqual(
            10 + 5e-10,
            api.get_position('BTC/USDT:USDT')['contracts'])

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
            {'symbol': 'BTC/USDT:USDT', 'contracts': 0, 'info': {'pos': '0'}}]
        self.assertIsNone(api.get_position('BTC/USDT:USDT'))
        good = {'contracts': '5', 'side': 'long', 'symbol': 'BTC/USDT:USDT',
                'info': {'pos': '5', 'posSide': 'net'}}
        api.exchange.fetch_positions.return_value = [good]
        normalized = api.get_position('BTC/USDT:USDT')
        self.assertEqual(5.0, normalized['contracts'])
        self.assertEqual('5', good['contracts'])
        self.assertIsNot(normalized, good)

    def test_entry_without_symbol_identity_rejected(self):
        """对抗复审反例 A：{} / 无标识条目是「无法归属」，不是空仓。"""
        for malformed in ({}, {'contracts': None, 'info': {}},
                          {'contracts': 5.0, 'side': 'long', 'info': {}}):
            api = _bare_api()
            api.exchange.fetch_positions.return_value = [malformed]
            with self.subTest(malformed=malformed), \
                    self.assertRaises(PositionModeError):
                api.get_position('BTC/USDT:USDT')

    def test_none_or_missing_both_sizes_is_not_flat(self):
        api = _bare_api()
        for malformed in (
                None,
                {'symbol': 'BTC/USDT:USDT', 'contracts': None, 'info': {}},
                {'info': {'instId': 'BTC-USDT-SWAP'}}):
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
        for malformed in (
                None,
                {'symbol': 'BTC/USDT:USDT', 'contracts': None, 'info': {}},
                {'info': {'instId': 'BTC-USDT-SWAP'}}):
            api.exchange.fetch_positions.return_value = [malformed]
            with self.subTest(malformed=malformed), \
                    self.assertRaises(PositionModeError):
                api.list_position_symbols()
        api.exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDT:USDT', 'contracts': 0,
             'side': 'long', 'info': {'pos': '3'}}]
        with self.assertRaises(PositionModeError):
            api.list_position_symbols()

    def test_list_position_symbols_recovers_usdt_swap_from_inst_id(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [{
            'contracts': 2.0, 'side': 'long',
            'info': {'pos': '2', 'instId': 'BTC-USDT-SWAP'}}]

        self.assertEqual(['BTCUSDT'], api.list_position_symbols())

        api.exchange.fetch_positions.return_value = [{
            'symbol': 'BTC-USDT-SWAP', 'contracts': 2.0, 'side': 'long',
            'info': {'pos': '2'}}]
        self.assertEqual(['BTCUSDT'], api.list_position_symbols())

    def test_list_position_symbols_rejects_malformed_usdt_identity(self):
        api = _bare_api()
        for symbol in ('btc/USDT:USDT', 'BTC//USDT:USDT'):
            api.exchange.fetch_positions.return_value = [{
                'symbol': symbol, 'contracts': 2.0, 'side': 'long',
                'info': {'pos': '2'}}]
            with self.subTest(symbol=symbol), self.assertRaises(PositionModeError):
                api.list_position_symbols()
        api.exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDT:USDT', 'contracts': None,
             'info': {'pos': '3'}}]
        with self.assertRaises(PositionModeError):
            api.list_position_symbols()

    def test_list_position_symbols_rejects_unknown_nonzero_identity(self):
        api = _bare_api()
        for malformed in (
                {'symbol': 'garbage', 'contracts': 2, 'side': 'long',
                 'info': {'pos': '2'}},
                {'symbol': 'garbage', 'contracts': None, 'side': 'short',
                 'info': {'pos': '-2'}}):
            api.exchange.fetch_positions.return_value = [malformed]
            with self.subTest(malformed=malformed), \
                    self.assertRaises(PositionModeError):
                api.list_position_symbols()

        api.exchange.fetch_positions.return_value = [
            {'symbol': 'garbage', 'contracts': 0, 'info': {'pos': '0'}}]
        self.assertEqual([], api.list_position_symbols())

    def test_list_position_symbols_ignores_authoritative_non_swap_products(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USD:BTC-260925', 'contracts': 2,
             'info': {'instType': 'FUTURES', 'instId': 'BTC-USD-260925'}},
            {'symbol': 'BTC/USD:BTC', 'contracts': 3,
             'info': {'instType': 'MARGIN', 'instId': 'BTC-USD'}},
            {'symbol': 'BTC/USD:BTC-260925-50000-C', 'contracts': 1,
             'info': {'instType': 'OPTION',
                      'instId': 'BTC-USD-260925-50000-C'}},
        ]

        self.assertEqual([], api.list_position_symbols())

    def test_non_swap_inst_type_with_swap_identity_is_hard_failure(self):
        """不能借非 SWAP instType 把明确的 U 本位永续身份静默排除。"""
        malformed = (
            {'symbol': 'BTC/USDT:USDT', 'contracts': 2, 'side': 'long',
             'info': {'instType': 'FUTURES',
                      'instId': 'BTC-USDT-SWAP', 'pos': '2'}},
            {'symbol': 'BTC-USDT-SWAP', 'contracts': 2, 'side': 'long',
             'info': {'instType': 'MARGIN',
                      'instId': 'BTC-USDT-SWAP', 'pos': '2'}},
        )
        for entry in malformed:
            api = _bare_api()
            api.exchange.fetch_positions.return_value = [entry]
            with self.subTest(entry=entry), self.assertRaises(PositionModeError):
                api.list_position_symbols()

    def test_list_position_symbols_rejects_unknown_product_type_or_conflict(self):
        api = _bare_api()
        for malformed in (
                {'symbol': 'garbage', 'contracts': 2,
                 'info': {'instType': 'MYSTERY'}},
                {'symbol': 'BTC/USD:BTC', 'contracts': 2,
                 'info': {'instType': 'SWAP', 'instId': 'ETH-USD-SWAP'}}):
            api.exchange.fetch_positions.return_value = [malformed]
            with self.subTest(malformed=malformed), \
                    self.assertRaises(PositionModeError):
                api.list_position_symbols()

    def test_list_position_symbols_reports_clean_positions(self):
        api = _bare_api()
        api.exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDT:USDT', 'contracts': 2.0, 'side': 'long',
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
        for malformed in (
                {}, None, {'noise': 1}, {'status': 'closed'},
                {'id': 'order-without-client'},
                {'id': 'wrong-client', 'clientOrderId': 'OTHER123'}):
            api = self._api()
            api.exchange.fetch_order.return_value = malformed
            with self.subTest(malformed=malformed), \
                    self.assertRaises(RuntimeError):
                api.find_existing_open_order(
                    'BTCUSDT', 'long', 0.1, 'CID123')

    def test_confirmation_query_requires_exact_order_identity(self):
        cases = (
            ('ordId-missing', 'OID123', {
                'status': 'closed'}),
            ('ordId-wrong', 'OID123', {
                'id': 'OTHER', 'status': 'closed'}),
            ('ordId-missing-client', 'OID123', {
                'id': 'OID123', 'symbol': 'BTC/USDT:USDT',
                'status': 'closed'}),
            ('ordId-missing-symbol', 'OID123', {
                'id': 'OID123', 'clientOrderId': 'CID123',
                'status': 'closed'}),
            ('clOrdId-missing', None, {
                'id': 'OID123', 'status': 'closed'}),
            ('clOrdId-wrong', None, {
                'id': 'OID123', 'clientOrderId': 'OTHER123',
                'status': 'closed'}),
            ('ordId-conflicting-native-id', 'OID123', {
                'id': 'OID123', 'clientOrderId': 'CID123',
                'symbol': 'BTC/USDT:USDT',
                'info': {'ordId': 'OTHER', 'clOrdId': 'CID123',
                         'instId': 'BTC-USDT-SWAP'}}),
            ('ordId-conflicting-native-client', 'OID123', {
                'id': 'OID123', 'clientOrderId': 'CID123',
                'symbol': 'BTC/USDT:USDT',
                'info': {'ordId': 'OID123', 'clOrdId': 'OTHER',
                         'instId': 'BTC-USDT-SWAP'}}),
            ('ordId-wrong-symbol', 'OID123', {
                'id': 'OID123', 'clientOrderId': 'CID123',
                'symbol': 'ETH/USDT:USDT',
                'info': {'ordId': 'OID123', 'clOrdId': 'CID123',
                         'instId': 'BTC-USDT-SWAP'}}),
            ('clOrdId-wrong-instId', None, {
                'id': 'OID123', 'clientOrderId': 'CID123',
                'symbol': 'BTC/USDT:USDT',
                'info': {'ordId': 'OID123', 'clOrdId': 'CID123',
                         'instId': 'ETH-USDT-SWAP'}}),
            ('clOrdId-conflicting-native-order-id', None, {
                'id': 'OID123', 'clientOrderId': 'CID123',
                'symbol': 'BTC/USDT:USDT',
                'info': {'ordId': 'OTHER', 'clOrdId': 'CID123',
                         'instId': 'BTC-USDT-SWAP'}}),
        )
        for name, order_id, response in cases:
            api = self._api()
            api.exchange.fetch_order.return_value = response
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                api._fetch_order_for_confirmation(
                    'BTC/USDT:USDT', order_id, 'CID123')

    def test_confirmation_never_uses_wrong_symbol_order_with_matching_position(self):
        api = self._api()
        api.ORDER_CONFIRM_ATTEMPTS = 1
        api.exchange.fetch_order.return_value = {
            'id': 'OID123', 'clientOrderId': 'CID123',
            'symbol': 'ETH/USDT:USDT', 'status': 'closed', 'filled': 10,
        }
        api.exchange.fetch_positions.return_value = [{
            'symbol': 'BTC/USDT:USDT', 'contracts': 10, 'side': 'long',
            'hedged': False, 'info': {'posSide': 'net'},
        }]

        result, observed = api._confirm_market_order(
            'BTC/USDT:USDT', {'id': 'OID123', 'status': 'closed'},
            'CID123', operation='open', side='long', pre_contracts=0.0,
            requested_contracts=10.0)

        self.assertIsNone(result)
        self.assertEqual(10.0, observed)

    def test_wrong_client_ack_cannot_poison_authoritative_client_id(self):
        api = self._api()
        api.ORDER_CONFIRM_ATTEMPTS = 1
        api.exchange.fetch_order.return_value = {
            'id': 'OID123', 'status': 'closed', 'filled': 10,
            'symbol': 'BTC/USDT:USDT',
        }
        api.exchange.fetch_positions.return_value = [{
            'symbol': 'BTC/USDT:USDT', 'contracts': 10, 'side': 'long',
            'hedged': False, 'info': {'posSide': 'net'},
        }]

        result, observed = api._confirm_market_order(
            'BTC/USDT:USDT',
            {'id': 'OID123', 'clientOrderId': 'WRONG123',
             'status': 'closed', 'filled': 10},
            'CID123', operation='open', side='long', pre_contracts=0.0,
            requested_contracts=10.0)

        self.assertIsNone(result)
        self.assertEqual(10.0, observed)
        api.exchange.fetch_order.assert_called_once_with(
            'CID123', 'BTC/USDT:USDT', params={'clOrdId': 'CID123'})

    def test_non_string_ack_or_fetched_identity_is_never_coerced(self):
        api = self._api()
        for ack in (
                {'id': True, 'clientOrderId': 'CID123'},
                {'id': 'OID123', 'clientOrderId': True}):
            with self.subTest(ack=ack):
                self.assertIsNone(api._sanitize_order_ack(
                    'BTC/USDT:USDT', 'CID123', ack))

        api.exchange.fetch_order.return_value = {
            'id': 123, 'clientOrderId': 'CID123',
            'symbol': 'BTC/USDT:USDT',
        }
        with self.assertRaises(RuntimeError):
            api._fetch_order_for_confirmation(
                'BTC/USDT:USDT', None, 'CID123')

    def test_ack_without_client_id_is_discarded_and_queried_by_client_id(self):
        api = self._api()
        api.ORDER_CONFIRM_ATTEMPTS = 1
        api.exchange.fetch_order.return_value = {
            'id': 'CORRECTOID', 'clientOrderId': 'CID123',
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'buy', 'amount': 10, 'reduceOnly': False,
            'status': 'closed', 'filled': 10,
        }
        api.exchange.fetch_positions.return_value = [{
            'symbol': 'BTC/USDT:USDT', 'contracts': 10, 'side': 'long',
            'hedged': False, 'info': {'posSide': 'net'},
        }]

        result, _ = api._confirm_market_order(
            'BTC/USDT:USDT',
            {'id': 'WRONGOID', 'symbol': 'BTC/USDT:USDT',
             'status': 'closed', 'filled': 10},
            'CID123', operation='open', side='long', pre_contracts=0.0,
            requested_contracts=10.0)

        self.assertEqual('CORRECTOID', result['id'])
        self.assertEqual('CID123', result['clientOrderId'])
        api.exchange.fetch_order.assert_called_once_with(
            'CID123', 'BTC/USDT:USDT', params={'clOrdId': 'CID123'})

    def test_confirmation_requires_exact_market_order_semantics(self):
        api = self._api()
        api.ORDER_CONFIRM_ATTEMPTS = 1
        wrong = {
            'id': 'OID123', 'clientOrderId': 'CID123',
            'symbol': 'BTC/USDT:USDT', 'type': 'limit',
            'side': 'sell', 'amount': 99, 'reduceOnly': True,
            'status': 'closed', 'filled': 10,
        }
        api.exchange.fetch_order.return_value = wrong
        api.exchange.fetch_positions.return_value = [{
            'symbol': 'BTC/USDT:USDT', 'contracts': 10, 'side': 'long',
            'hedged': False, 'info': {'posSide': 'net'},
        }]

        result, _ = api._confirm_market_order(
            'BTC/USDT:USDT', None, 'CID123', operation='open',
            side='long', pre_contracts=0.0, requested_contracts=10.0)

        self.assertIsNone(result)

    def test_standard_order_fields_cannot_hide_conflicting_native_fields(self):
        api = self._api()
        conflicted = {
            'id': 'OID123', 'clientOrderId': 'CID123',
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'buy', 'amount': 10, 'reduceOnly': False,
            'info': {
                'instId': 'BTC-USDT-SWAP', 'ordType': 'limit',
                'side': 'sell', 'sz': '99', 'reduceOnly': 'true',
            },
        }

        self.assertFalse(api._existing_order_matches_request(
            conflicted, 'BTC/USDT:USDT', 'buy', 10.0,
            reduce_only=False))

    def test_invalid_close_amount_or_side_never_reads_position_or_posts(self):
        for value in (
                0, -1, float('nan'), float('inf'), float('-inf'),
                True, False, None, 'garbage'):
            api = self._api()
            with self.subTest(amount=value):
                self.assertIsNone(api.close_position(
                    'BTC/USDT:USDT', 'long', value))
                api.exchange.fetch_positions.assert_not_called()
                api.exchange.create_order.assert_not_called()

    def test_unrepresentable_amount_and_invalid_client_id_have_zero_external_io(self):
        for operation in ('open', 'close', 'stop'):
            api = self._api()
            with self.subTest(operation=operation, kind='amount'):
                if operation == 'stop':
                    result = api.create_stop_loss_order(
                        'BTC/USDT:USDT', 'long', '1e9999', 100)
                else:
                    result = getattr(api, f'{operation}_position')(
                        'BTC/USDT:USDT', 'long', '1e9999')
                self.assertIsNone(result)
                api.exchange.market.assert_not_called()
                api.exchange.amount_to_precision.assert_not_called()
                api.exchange.price_to_precision.assert_not_called()
                api.exchange.fetch_positions.assert_not_called()
                api.exchange.create_order.assert_not_called()
                api.exchange.privatePostTradeOrderAlgo.assert_not_called()

            api = self._api()
            with self.subTest(operation=operation, kind='client_id'):
                if operation == 'stop':
                    result = api.create_stop_loss_order(
                        'BTC/USDT:USDT', 'long', 0.1, 100,
                        client_order_id=True)
                else:
                    result = getattr(api, f'{operation}_position')(
                        'BTC/USDT:USDT', 'long', 0.1,
                        client_order_id=True)
                self.assertIsNone(result)
                api.exchange.market.assert_not_called()
                api.exchange.amount_to_precision.assert_not_called()
                api.exchange.price_to_precision.assert_not_called()
                api.exchange.fetch_positions.assert_not_called()
                api.exchange.create_order.assert_not_called()
                api.exchange.privatePostTradeOrderAlgo.assert_not_called()

        for operation in ('open', 'close'):
            api = self._api()
            with self.subTest(operation=operation):
                result = getattr(api, f'{operation}_position')(
                    'BTC/USDT:USDT', 'invalid-side', 0.1)
                self.assertIsNone(result)
                api.exchange.fetch_positions.assert_not_called()
                api.exchange.create_order.assert_not_called()

    def test_find_existing_open_order_none_only_on_order_not_found(self):
        api = self._api()
        api.exchange.fetch_order.side_effect = OrderNotFound('gone')
        self.assertIsNone(
            api.find_existing_open_order('BTCUSDT', 'long', 0.1, 'CID123'))

    def test_open_intent_lookup_recovers_order_visible_on_second_grace_read(self):
        api = self._api()
        late = {
            'id': 'open-late', 'clientOrderId': 'CID123',
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'buy', 'amount': 10, 'reduceOnly': False,
            'status': 'closed', 'filled': 10, 'remaining': 0,
            'average': 100, 'info': {},
        }
        api.exchange.fetch_order.side_effect = [
            OrderNotFound('index lag'), late]

        fake_time = Mock(sleep=Mock())
        with patch.object(okx_api, 'time', fake_time):
            order = api.find_existing_open_order(
                'BTCUSDT', 'long', 0.1, 'CID123',
                wait_for_visibility=True)

        self.assertEqual('open-late', order['id'])
        self.assertEqual(1, fake_time.sleep.call_count)

    def test_open_position_refuses_new_post_after_malformed_lookup(self):
        api = self._api()
        api.exchange.fetch_order.return_value = {}
        result = api.open_position(
            'BTCUSDT', 'long', 0.1, client_order_id='CID123')
        self.assertIsNone(result)
        api.exchange.create_order.assert_not_called()

    def test_close_order_recovery_raises_on_malformed_response(self):
        api = self._api()
        api.exchange.fetch_order.return_value = {}
        with self.assertRaises(RuntimeError):
            api._find_existing_close_order(
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

    def test_non_string_exchange_or_client_ids_are_rejected(self):
        api = _bare_api()
        bad_algo = _native_stop()
        bad_algo['algoId'] = True
        api.exchange.privateGetTradeOrdersAlgoPending.side_effect = _algo_stub({
            'conditional': [bad_algo],
        })
        with self.assertRaises(RuntimeError):
            api._fetch_algo_orders('BTC/USDT:USDT')

        api = _bare_api()
        bad_normal = _native_normal()
        bad_normal['clOrdId'] = True
        api.exchange.privateGetTradeOrdersPending.return_value = {
            'code': '0', 'data': [bad_normal]}
        with self.assertRaises(RuntimeError):
            api._fetch_normal_orders('BTC/USDT:USDT')


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
        api.exchange.privateGetTradeOrderAlgo.return_value = (
            _algo_detail_response(_native_algo_detail(
                algo_id='gone-id')))
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

    @staticmethod
    def _raw_day(timestamp, open_=10, high=12, low=9, close=11,
                 volume=1, confirm='1'):
        return [str(timestamp), str(open_), str(high), str(low), str(close),
                str(volume), '0', '0', confirm]

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
        api.exchange.publicGetMarketCandles.return_value = {
            'code': '0', 'data': [self._raw_day(0, high='nan')]}
        with self.assertRaises(ValueError):
            api.fetch_ohlcv('BTC/USDT:USDT', '1d', limit=10)
        api.exchange.fetch_ohlcv.assert_not_called()

    def test_daily_fetch_rejects_internal_calendar_gap(self):
        api = _bare_api()
        day = 86_400_000
        api.exchange.publicGetMarketCandles.return_value = {
            'code': '0', 'data': [
                self._raw_day(day * 2, 11, 13, 10, 12),
                self._raw_day(0),
            ]}

        with self.assertRaisesRegex(ValueError, '不连续'):
            api.fetch_ohlcv('BTC/USDT:USDT', '1d', limit=10)

    def test_daily_fetch_requires_raw_confirm_and_utc_bar(self):
        api = _bare_api()
        day = 86_400_000
        api.exchange.publicGetMarketCandles.return_value = {
            'code': '0', 'data': [
                self._raw_day(day * 2, confirm='0'),
                self._raw_day(day), self._raw_day(0),
            ]}

        rows = api.fetch_ohlcv('BTCUSDT', '1d', limit=300)

        self.assertEqual([0, day], [row[0] for row in rows])
        api.exchange.publicGetMarketCandles.assert_called_once_with({
            'instId': 'BTC-USDT-SWAP', 'bar': '1Dutc', 'limit': '300'})
        api.exchange.fetch_ohlcv.assert_not_called()

    def test_daily_capacity_keeps_299_confirmed_when_current_is_incomplete(self):
        api = _bare_api()
        day = 86_400_000
        data = [self._raw_day(299 * day, confirm='0')]
        data.extend(self._raw_day(index * day) for index in reversed(range(299)))
        api.exchange.publicGetMarketCandles.return_value = {
            'code': '0', 'data': data}

        rows = api.fetch_ohlcv('BTCUSDT', '1d', limit=300)

        self.assertEqual(299, len(rows))
        self.assertEqual(0, rows[0][0])
        self.assertEqual(298 * day, rows[-1][0])

    def test_daily_raw_envelope_confirm_shape_and_utc_anchor_are_strict(self):
        day = 86_400_000
        bad_responses = (
            None,
            {'code': '1', 'data': []},
            {'code': '0', 'data': None},
            {'code': '0', 'data': [['0'] * 8]},
            {'code': '0', 'data': [self._raw_day(0, confirm=1)]},
            {'code': '0', 'data': [self._raw_day(0, confirm='x')]},
            {'code': '0', 'data': [self._raw_day(day - 8 * 3_600_000)]},
        )
        for response in bad_responses:
            api = _bare_api()
            api.exchange.publicGetMarketCandles.return_value = response
            with self.subTest(response=response), self.assertRaises(ValueError):
                api.fetch_ohlcv('BTCUSDT', '1d', limit=10)

    def test_intraday_validation_does_not_apply_daily_spacing(self):
        api = _bare_api()
        api.exchange.fetch_ohlcv.return_value = self.GOOD

        self.assertEqual(
            self.GOOD,
            api.fetch_ohlcv('BTC/USDT:USDT', '1h', limit=10))


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
        api.exchange.fetch_order.side_effect = OrderNotFound('no order')
        self.assertIsNone(api.find_compensation_close_evidence(
            'BTC/USDT:USDT', 'long', 0.1, 'OPENID1'))
        api.exchange.create_order.assert_not_called()

    def test_full_terminal_order_returns_evidence_without_any_post(self):
        api = self._api()
        base = OkxApi.compensation_client_order_id('OPENID1')
        order = {'id': 'o1', 'clientOrderId': base, 'symbol': 'BTC/USDT:USDT',
               'type': 'market', 'side': 'sell', 'amount': 10.0,
               'filled': 10.0, 'remaining': 0.0, 'status': 'closed',
               'average': 50000.0, 'reduceOnly': True, 'info': {}}

        def fetch_order(order_id, _symbol, params=None):
            if (params or {}).get('clOrdId') == base:
                return dict(order)
            raise OrderNotFound(str(order_id))

        api.exchange.fetch_order.side_effect = fetch_order
        result = api.find_compensation_close_evidence(
            'BTC/USDT:USDT', 'long', 0.1, 'OPENID1')
        self.assertTrue(result['fully_closed'])
        self.assertTrue(result['read_only_evidence'])
        self.assertEqual(50000.0, result['average'])
        self.assertEqual(['o1'], result['ids'])
        api.exchange.create_order.assert_not_called()

    def test_late_compensation_order_is_found_during_shared_visibility_grace(self):
        api = self._api()
        base = OkxApi.compensation_client_order_id('OPENID1')
        order = {
            'id': 'o-late', 'clientOrderId': base,
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'sell', 'amount': 10.0, 'filled': 10.0,
            'remaining': 0.0, 'status': 'closed', 'average': 49999.0,
            'reduceOnly': True, 'info': {},
        }
        api.exchange.fetch_order.side_effect = [
            OrderNotFound('not visible yet'), order]

        fake_time = Mock(sleep=Mock())
        with patch.object(okx_api, 'time', fake_time):
            result = api.find_compensation_close_evidence(
                'BTC/USDT:USDT', 'long', 0.1, 'OPENID1')

        self.assertTrue(result['read_only_evidence'])
        self.assertEqual(['o-late'], result['ids'])
        self.assertEqual(1, fake_time.sleep.call_count)
        api.exchange.create_order.assert_not_called()

    def test_partial_evidence_is_reported_as_incomplete(self):
        api = self._api()
        base = OkxApi.compensation_client_order_id('OPENID1')
        order = {'id': 'o1', 'clientOrderId': base, 'symbol': 'BTC/USDT:USDT',
               'type': 'market', 'side': 'sell', 'amount': 10.0,
               'filled': 6.0, 'remaining': 4.0, 'status': 'canceled',
               'average': 50000.0, 'reduceOnly': True, 'info': {}}

        def fetch_order(order_id, _symbol, params=None):
            if (params or {}).get('clOrdId') == base:
                return dict(order)
            raise OrderNotFound(str(order_id))

        api.exchange.fetch_order.side_effect = fetch_order
        self.assertIsNone(api.find_compensation_close_evidence(
            'BTC/USDT:USDT', 'long', 0.1, 'OPENID1'))
        api.exchange.create_order.assert_not_called()

    def test_progress_normalizes_sub_tolerance_fill_to_zero_everywhere(self):
        api = self._api()
        base = OkxApi.compensation_client_order_id('OPENID1')
        api.exchange.fetch_order.return_value = {
            'id': 'o-noise-zero', 'clientOrderId': base,
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'sell', 'amount': 10.0, 'filled': 5e-10,
            'remaining': 10.0 - 5e-10, 'status': 'canceled',
            'reduceOnly': True,
            'info': {'sz': '10', 'accFillSz': '0.0000000005'},
        }

        progress = api.find_compensation_close_progress(
            'BTC/USDT:USDT', 'long', 0.1, 'OPENID1')
        normalized = TradeExecutorMixin()._normalize_compensation_close_progress(
            progress, expected_client_order_id=base,
            expected_contracts=10.0, expected_amount=0.1)

        self.assertEqual(0.0, progress['filled'])
        self.assertEqual(0.0, normalized['filled_contracts'])
        self.assertEqual(0.0, progress['amount'])
        self.assertEqual(10.0, progress['order_state']['remaining'])
        self.assertEqual(0.0, progress['order_state']['filled'])
        self.assertEqual(0.0, progress['order']['filled_contracts'])
        self.assertEqual(0.0, progress['order']['filled_amount'])
        self.assertEqual(0.0, progress['order']['filled'])
        self.assertEqual(10.0, progress['order']['remaining'])
        self.assertEqual(0.0, progress['order']['info']['accFillSz'])
        self.assertEqual('partial', progress['status'])
        self.assertEqual([], progress['ids'])
        api.exchange.create_order.assert_not_called()

    def test_progress_normalizes_near_full_fill_to_requested_everywhere(self):
        api = self._api()
        base = OkxApi.compensation_client_order_id('OPENID1')
        near_full = 10.0 - 5e-10
        api.exchange.fetch_order.return_value = {
            'id': 'o-noise-full', 'clientOrderId': base,
            'symbol': 'BTC/USDT:USDT', 'type': 'market',
            'side': 'sell', 'amount': 10.0, 'filled': near_full,
            'remaining': 5e-10, 'status': 'closed',
            'average': 50000.0, 'reduceOnly': True,
            'info': {'sz': '10', 'accFillSz': str(near_full)},
        }

        progress = api.find_compensation_close_progress(
            'BTC/USDT:USDT', 'long', 0.1, 'OPENID1')
        normalized = TradeExecutorMixin()._normalize_compensation_close_progress(
            progress, expected_client_order_id=base,
            expected_contracts=10.0, expected_amount=0.1)

        self.assertEqual(10.0, progress['filled'])
        self.assertEqual(10.0, normalized['filled_contracts'])
        self.assertEqual(0.1, progress['amount'])
        self.assertEqual(0.0, progress['remaining_amount'])
        self.assertEqual(10.0, progress['order_state']['filled'])
        self.assertEqual(0.0, progress['order_state']['remaining'])
        self.assertEqual(10.0, progress['order']['filled_contracts'])
        self.assertEqual(0.1, progress['order']['filled_amount'])
        self.assertEqual(10.0, progress['order']['filled'])
        self.assertEqual(0.0, progress['order']['remaining'])
        self.assertEqual(10.0, progress['order']['info']['accFillSz'])
        self.assertEqual('closed', progress['status'])
        self.assertTrue(progress['fully_filled'])
        self.assertTrue(progress['fully_closed'])
        self.assertEqual(['o-noise-full'], progress['ids'])
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
