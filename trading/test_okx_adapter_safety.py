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
NetworkError = _ccxt.NetworkError


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


class CoinContractRoundTripTest(unittest.TestCase):
    """币↔张往返浮点吸附：账本币数 = 整步长张数 × 面值，往返除法的浮点尘埃
    （49×0.0001/0.0001 = 48.999…99）若直接截断会少一张——实际下单比账本记的
    小一张，盈亏/风险校验/持仓汇总系统性偏差。截断前必须吸附回最近步长。"""

    def _api(self, cs=0.0001, precision=None):
        api = _bare_api()
        api._contract_size_cache['X/USDT:USDT'] = cs
        if precision is not None:
            api._amount_precision_cache['X/USDT:USDT'] = precision
        return api

    def test_roundtrip_dust_snaps_back_to_whole_contracts(self):
        api = self._api()
        # 模拟 ccxt amount_to_precision 的 TRUNCATE 语义（整张步长）
        api.exchange.amount_to_precision.side_effect = lambda s, a: str(int(a))
        for n in (49, 59, 81, 98, 115):  # 实证均产生 48.999…99 级下侧尘埃的张数
            self.assertEqual(api._coin_to_contracts('X/USDT:USDT', n * 0.0001), float(n),
                             msg=f'n={n} 往返丢张')

    def test_fallback_floor_path_also_snapped(self):
        """ccxt 精度接口不可用走 floor 兜底：同样必须先吸附。"""
        api = self._api()
        api.exchange.amount_to_precision.side_effect = RuntimeError('markets 未加载')
        self.assertEqual(api._coin_to_contracts('X/USDT:USDT', 49 * 0.0001), 49.0)

    def test_genuine_fraction_still_truncates(self):
        """真实的不足一张部分仍截断丢弃：吸附只救 1e-6 步以内的浮点尘埃，
        不改变「向下取整到可交易步长」的既有语义。"""
        api = self._api()
        api.exchange.amount_to_precision.side_effect = lambda s, a: str(int(a))
        # 0.00049 币 / 0.0001 面值 = 4.9 张（真实小数，非尘埃）→ 4 张
        self.assertEqual(api._coin_to_contracts('X/USDT:USDT', 0.00049), 4.0)

    def test_fractional_step_snap_uses_precision_cache(self):
        """步长 0.1 张（precision=1）：尘埃吸附到最近 0.1 倍数，而非整数。
        3.1 张 × 0.0001 币往返 = 3.0999999999999996，无吸附会被截成 3.0。"""
        import math
        api = self._api(precision=1)
        api.exchange.amount_to_precision.side_effect = lambda s, a: str(math.floor(a * 10) / 10)
        self.assertEqual(api._coin_to_contracts('X/USDT:USDT', 3.1 * 0.0001), 3.1)


class PositionQuerySingleShotTest(unittest.TestCase):
    """下单超时确认循环使用单次持仓查询：循环自带「3 次 × 2 秒」重试节奏，
    内层不得再叠网络退避重试，否则最坏确认等待成倍拉长、确认窗口被动错过。"""

    def test_position_contracts_does_not_retry(self):
        api = _bare_api()
        api.exchange.fetch_positions.side_effect = NetworkError('查询失败')
        with self.assertRaises(NetworkError):
            api._position_contracts('BTC/USDT:USDT')
        self.assertEqual(api.exchange.fetch_positions.call_count, 1)

    def test_get_position_keeps_retry(self):
        """常规读路径 get_position 保留 3 次退避重试（语义不变）。"""
        api = _bare_api()
        api.exchange.fetch_positions.side_effect = NetworkError('查询失败')
        # get_position 是 exchange_base 装饰器包装的函数，经 __globals__ 桩掉其 time.sleep
        with patch.dict(OkxApi.get_position.__globals__, {'time': Mock(sleep=lambda s: None)}):
            with self.assertRaises(NetworkError):
                api.get_position('BTC/USDT:USDT')
        self.assertEqual(api.exchange.fetch_positions.call_count, 3)


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
            [{'contracts': 10, 'symbol': 'BTC/USDT:USDT'}],    # 超时后确认：已成交 10 张
        ]
        with patch.object(okx_api, 'time', Mock(sleep=lambda s: None)):
            order = api.open_position('BTC/USDT:USDT', 'long', 0.1)
        self.assertEqual(order['id'], 'timeout_confirmed')
        self.assertAlmostEqual(order['amount'], 0.1)  # 10 张 × 0.01 = 0.1 币

    def test_close_timeout_confirm_returns_coins(self):
        api = self._api()
        api.exchange.create_order.side_effect = RequestTimeout('超时')
        api.exchange.fetch_positions.side_effect = [
            [{'contracts': 10, 'symbol': 'BTC/USDT:USDT'}],    # 平仓前：持仓 10 张
            [],                                                # 超时后确认：已平
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
        self.assertEqual(api.find_stop_order_state('BTCUSDT', 'long', 0.1, 55000.0, 'stop-1'), 'missing')

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
    GOOD = {'side': 'sell', 'amount': 25.0, 'stopLossPrice': 98.5, 'info': {}}

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
        o = {'side': 'sell', 'info': {'slTriggerPx': '98.5', 'sz': '25'}}
        self.assertTrue(OkxApi._algo_order_matches(o, 'sell', 98.5, 25.0))


if __name__ == '__main__':
    unittest.main()
