"""Adversarial, network-free regression tests for the deployment no-open gate."""

import copy
import contextlib
import fcntl
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import deployment_no_open_gate as gate_module
from deployment_no_open_gate import (
    ALGO_HISTORY_STATES,
    ALGO_ORDER_TYPES,
    BASELINE_NAME,
    COMPLETION_NAME,
    MAX_PROOF_WINDOW_MS,
    PROOF_COMPLETION_SAFETY_MS,
    SENTINEL_NAME,
    GateError,
    ReadOnlyOkxGate,
    _account_fingerprint,
    _build_parser,
    _capture_baseline,
    _compare_positions,
    _validate_baseline,
    _verify,
    abandon_cycle,
    arm,
    commit_sealed,
    create_read_only_exchange,
    load_okx_config,
    probe_api_permission_mode,
    probe_public_history_exposure,
    require_completed_schedule_slot,
    run_baseline,
    run_quiescence,
    run_verify,
    seal,
)


SHA_A = 'a' * 40
SHA_B = 'b' * 40
NONCE = 'c' * 64


def _canonical_temp_directory():
    return tempfile.TemporaryDirectory(
        dir=os.path.realpath(tempfile.gettempdir()))


def _envelope(data):
    return {'code': '0', 'msg': '', 'data': data}


def _account(uid='1001', main_uid='1001', acct_lv='2', pos_mode='net_mode',
             perm='read_only,trade'):
    return {
        'uid': uid,
        'mainUid': main_uid,
        'acctLv': acct_lv,
        'posMode': pos_mode,
        'perm': perm,
    }


def _canonical_account(uid='1001', main_uid='1001'):
    return {
        'uid': uid,
        'main_uid': main_uid,
        'acct_lv': '2',
        'pos_mode': 'net_mode',
    }


def _position(size='2', pos_id='p1', c_time='900', pos_side='net',
              inst_id='BTC-USDT-SWAP'):
    return {
        'instType': 'SWAP',
        'instId': inst_id,
        'pos': size,
        'posSide': pos_side,
        'posId': pos_id,
        'cTime': c_time,
    }


def _normal_pending(order_id='n1', reduce_only='true'):
    return {
        'instType': 'SWAP',
        'instId': 'BTC-USDT-SWAP',
        'ordId': order_id,
        'state': 'live',
        'reduceOnly': reduce_only,
    }


def _algo_pending(order_id='a1', ord_type='conditional', reduce_only='true'):
    return {
        'instType': 'SWAP',
        'instId': 'BTC-USDT-SWAP',
        'algoId': order_id,
        'ordType': ord_type,
        'reduceOnly': reduce_only,
    }


def _history_order(order_id='h1', reduce_only='true', state='filled',
                   created='1500'):
    return {
        'instType': 'SWAP',
        'instId': 'BTC-USDT-SWAP',
        'ordId': order_id,
        'state': state,
        'cTime': created,
        'reduceOnly': reduce_only,
    }


def _fill(order_id='h1', bill_id='bill1', timestamp='1501'):
    return {
        'instType': 'SWAP',
        'instId': 'BTC-USDT-SWAP',
        'ordId': order_id,
        'billId': bill_id,
        'ts': timestamp,
    }


def _detail(order_id='h1', reduce_only='true'):
    return {
        'instType': 'SWAP',
        'instId': 'BTC-USDT-SWAP',
        'ordId': order_id,
        'reduceOnly': reduce_only,
    }


def _algo_history(order_id='ah1', ord_type='conditional', state='effective',
                  reduce_only='true', created='1500'):
    return {
        'instType': 'SWAP',
        'instId': 'BTC-USDT-SWAP',
        'algoId': order_id,
        'ordType': ord_type,
        'state': state,
        'reduceOnly': reduce_only,
        'cTime': created,
    }


def _okx():
    return {
        'apiKey': 'api-key-for-test',
        'secret': 'secret-for-test',
        'password': 'passphrase-for-test',
        'sandbox': False,
    }


def _canonical_position(size='2', direction='long', pos_id='p1'):
    return {
        'inst_id': 'BTC-USDT-SWAP',
        'pos_id': pos_id,
        'c_time_ms': 900,
        'direction': direction,
        'size_abs': size,
    }


def _baseline(positions=None, normal=None, algo=None):
    return {
        'schema_version': 2,
        'exchange': 'okx',
        'release_sha': SHA_A,
        'account_domain': 'live',
        'api_fingerprint': _account_fingerprint(_okx()),
        'account': _canonical_account(),
        'sentinel': {'nonce': NONCE, 'dev': 1, 'ino': 2},
        'runner_lock': {'dev': 1, 'ino': 3},
        't0_ms': 1000,
        'pre_t0_history': {
            'history_started_ms': 1000,
            'history_verified_through_ms': 1000,
            'orders_checked': 0,
            'fills_checked': 0,
            'fill_orders_checked': 0,
            'algo_orders_checked': 0,
        },
        'positions': copy.deepcopy(positions or []),
        'pending': {
            'normal': copy.deepcopy(normal or []),
            'algo': copy.deepcopy(algo or []),
        },
    }


class FakeExchange:
    """Only the nine read methods explicitly allowed by the gate are exposed."""

    def __init__(
            self, *, server_times=None, account_configs=None, positions=None,
            position_snapshots=None, normal_pending=None, algo_pending=None,
            orders_history=None, fills_history=None, details=None,
            algo_history=None):
        self.server_times = list(server_times or [2000, 2100])
        self.account_configs = list(account_configs or [_account()])
        self.positions_data = list(positions or [])
        self.position_snapshots = (
            [list(items) for items in position_snapshots]
            if position_snapshots is not None else None)
        self.normal_pending_data = list(normal_pending or [])
        self.algo_pending_data = {
            key: list(value) for key, value in (algo_pending or {}).items()
        }
        self.orders_history_data = list(orders_history or [])
        self.fills_history_data = list(fills_history or [])
        self.details = dict(details or {})
        self.algo_history_data = {
            key: list(value) for key, value in (algo_history or {}).items()
        }
        self.calls = []
        self.fail_second_normal_page = False
        self.fail_second_orders_page = False
        self.fail_second_fills_page = False
        self.fail_second_algo_history_page = False

    @staticmethod
    def _stable_next(values):
        if len(values) > 1:
            return values.pop(0)
        return values[0]

    def publicGetPublicTime(self, params):
        self.calls.append(('time', dict(params)))
        if not self.server_times:
            raise RuntimeError('unexpected time call')
        return _envelope([{'ts': str(self.server_times.pop(0))}])

    def privateGetAccountConfig(self, params):
        self.calls.append(('account_config', dict(params)))
        return _envelope([copy.deepcopy(self._stable_next(self.account_configs))])

    def privateGetAccountPositions(self, params):
        self.calls.append(('positions', dict(params)))
        if self.position_snapshots is None:
            data = self.positions_data
        else:
            data = self._stable_next(self.position_snapshots)
        return _envelope(copy.deepcopy(data))

    def privateGetTradeOrdersPending(self, params):
        self.calls.append(('normal_pending', dict(params)))
        if params.get('after') is not None:
            if self.fail_second_normal_page:
                raise RuntimeError('second page unavailable')
            return _envelope([])
        return _envelope(copy.deepcopy(self.normal_pending_data))

    def privateGetTradeOrdersAlgoPending(self, params):
        self.calls.append(('algo_pending', dict(params)))
        if params.get('after') is not None:
            return _envelope([])
        return _envelope(copy.deepcopy(
            self.algo_pending_data.get(params['ordType'], [])))

    def privateGetTradeOrdersHistory(self, params):
        self.calls.append(('orders_history', dict(params)))
        if params.get('after') is not None:
            if self.fail_second_orders_page:
                raise RuntimeError('second orders page unavailable')
            return _envelope([])
        begin = int(params['begin'])
        end = int(params['end'])
        data = [
            item for item in self.orders_history_data
            if begin <= int(item['cTime']) <= end
        ]
        return _envelope(copy.deepcopy(data))

    def privateGetTradeFillsHistory(self, params):
        self.calls.append(('fills_history', dict(params)))
        if params.get('after') is not None:
            if self.fail_second_fills_page:
                raise RuntimeError('second fills page unavailable')
            return _envelope([])
        begin = int(params['begin'])
        end = int(params['end'])
        data = [
            item for item in self.fills_history_data
            if begin <= int(item['ts']) <= end
        ]
        return _envelope(copy.deepcopy(data))

    def privateGetTradeOrder(self, params):
        self.calls.append(('order_detail', dict(params)))
        key = (params['instId'], params['ordId'])
        return _envelope([copy.deepcopy(self.details[key])])

    def privateGetTradeOrdersAlgoHistory(self, params):
        self.calls.append(('algo_history', dict(params)))
        if params.get('after') is not None:
            if self.fail_second_algo_history_page:
                raise RuntimeError('second algo history page unavailable')
            return _envelope([])
        key = (params['ordType'], params['state'])
        return _envelope(copy.deepcopy(self.algo_history_data.get(key, [])))


class ProofWindowTest(unittest.TestCase):
    def test_baseline_t0_precedes_snapshot_and_rechecks_account(self):
        exchange = FakeExchange(server_times=[1000])
        payload = _capture_baseline(
            ReadOnlyOkxGate(exchange), _okx(), SHA_A,
            {'nonce': NONCE, 'release_sha': SHA_A},
            {'dev': 1, 'ino': 2}, {'dev': 1, 'ino': 3})

        names = [name for name, _ in exchange.calls]
        time_index = names.index('time')
        self.assertLess(time_index, names.index('positions'))
        self.assertLess(time_index, min(
            index for index, name in enumerate(names)
            if name in {'normal_pending', 'algo_pending'}))
        self.assertEqual('account_config', names[0])
        self.assertEqual('account_config', names[-1])
        self.assertEqual(1000, payload['t0_ms'])

    def test_pre_t0_bridge_catches_open_then_flat_between_q2_and_t0(self):
        exchange = FakeExchange(
            server_times=[1000],
            orders_history=[
                _history_order('gap-open', 'false', created='950'),
                _history_order('gap-close', 'true', created='960'),
            ])
        with self.assertRaisesRegex(GateError, '非 reduceOnly normal order'):
            _capture_baseline(
                ReadOnlyOkxGate(exchange), _okx(), SHA_A,
                {'nonce': NONCE, 'release_sha': SHA_A},
                {'dev': 1, 'ino': 2}, {'dev': 1, 'ino': 3},
                history_start_ms=900)

    def test_verify_uses_exact_two_phase_closure_order(self):
        exchange = FakeExchange(server_times=[2000, 2100])
        _verify(ReadOnlyOkxGate(exchange), _baseline(), _okx())

        names = [name for name, _ in exchange.calls]
        time_indices = [i for i, name in enumerate(names) if name == 'time']
        self.assertEqual(2, len(time_indices))
        t1, t2 = time_indices
        first_position = names.index('positions')
        first_order_history = names.index('orders_history')
        second_order_history = names.index('orders_history', first_order_history + 1)
        second_position = names.index('positions', first_position + 1)
        self.assertLess(t1, first_position)
        self.assertLess(first_position, first_order_history)
        self.assertLess(first_order_history, t2)
        self.assertLess(t2, second_order_history)
        account_end = len(names) - 1 - names[::-1].index('account_config')
        self.assertLess(second_order_history, account_end)
        self.assertLess(account_end, second_position)
        self.assertLess(second_order_history, second_position)
        self.assertEqual('account_config', names[0])
        self.assertIn(names[-1], {'normal_pending', 'algo_pending'})

    def test_position_created_during_final_account_check_is_caught(self):
        class LatePosition(FakeExchange):
            def __init__(self):
                super().__init__(position_snapshots=[[], []])
                self.account_calls = 0

            def privateGetAccountConfig(self, params):
                self.account_calls += 1
                if self.account_calls == 2:
                    self.position_snapshots[-1] = [
                        _position(size='1', pos_id='late')]
                return super().privateGetAccountConfig(params)

        with self.assertRaisesRegex(GateError, '新增/反向/身份变化'):
            _verify(ReadOnlyOkxGate(LatePosition()), _baseline(), _okx())

    def test_summary_never_claims_proof_after_last_authoritative_history_end(self):
        result = _verify(
            ReadOnlyOkxGate(FakeExchange(server_times=[2000, 2100])),
            _baseline(), _okx())
        self.assertEqual(2100, result['t2_ms'])
        self.assertEqual(2100, result['history_verified_through_ms'])
        self.assertNotIn('proof_end_estimate_ms', result)

    def test_history_windows_are_t0_t1_then_t1_t2(self):
        exchange = FakeExchange(server_times=[2000, 2100])
        _verify(ReadOnlyOkxGate(exchange), _baseline(), _okx())

        for name in ('orders_history', 'fills_history'):
            calls = [params for called, params in exchange.calls if called == name]
            self.assertEqual(('1000', '2000'), (calls[0]['begin'], calls[0]['end']))
            self.assertEqual(('2000', '2100'), (calls[1]['begin'], calls[1]['end']))

    def test_open_then_flat_and_canceled_open_are_caught(self):
        for state in ('filled', 'canceled'):
            with self.subTest(state=state):
                exchange = FakeExchange(orders_history=[
                    _history_order('open', 'false', state=state),
                    _history_order('close', 'true'),
                ])
                with self.assertRaisesRegex(GateError, '非 reduceOnly normal order'):
                    _verify(ReadOnlyOkxGate(exchange), _baseline(), _okx())

    def test_algo_history_catches_open_even_when_no_longer_pending(self):
        exchange = FakeExchange(algo_history={
            ('trigger', 'canceled'): [
                _algo_history('hidden', 'trigger', 'canceled', 'false')],
        })
        with self.assertRaisesRegex(GateError, '非 reduceOnly algo order'):
            _verify(ReadOnlyOkxGate(exchange), _baseline(), _okx())

    def test_baseline_rejects_any_non_reduce_pending_surface(self):
        cases = (
            FakeExchange(
                server_times=[1000],
                normal_pending=[_normal_pending('open', 'false')]),
            FakeExchange(
                server_times=[1000],
                algo_pending={'conditional': [
                    _algo_pending('open-algo', 'conditional', 'false')]}),
        )
        for exchange in cases:
            with self.subTest(exchange=exchange), self.assertRaises(GateError):
                _capture_baseline(
                    ReadOnlyOkxGate(exchange), _okx(), SHA_A,
                    {'nonce': NONCE, 'release_sha': SHA_A},
                    {'dev': 1, 'ino': 2}, {'dev': 1, 'ino': 3})

    def test_reduce_only_protective_activity_is_allowed_and_counted(self):
        exchange = FakeExchange(
            normal_pending=[_normal_pending('new-close')],
            orders_history=[_history_order('close')],
            fills_history=[_fill('close')],
            details={('BTC-USDT-SWAP', 'close'): _detail('close')},
            algo_history={
                ('conditional', 'effective'): [
                    _algo_history('protective-stop')],
            })
        result = _verify(ReadOnlyOkxGate(exchange), _baseline(), _okx())
        self.assertEqual(1, result['new_pending'])
        self.assertEqual(1, result['orders_checked'])
        self.assertEqual(1, result['fills_checked'])
        self.assertEqual(1, result['algo_orders_checked'])

    def test_fill_detail_is_always_read_and_must_be_reduce_only(self):
        exchange = FakeExchange(
            fills_history=[_fill('hidden-open')],
            details={
                ('BTC-USDT-SWAP', 'hidden-open'):
                    _detail('hidden-open', 'false'),
            })
        with self.assertRaisesRegex(GateError, '对应订单不是 reduceOnly'):
            _verify(ReadOnlyOkxGate(exchange), _baseline(), _okx())
        self.assertIn('order_detail', [name for name, _ in exchange.calls])

    def test_s1_position_that_disappears_by_s2_is_still_caught(self):
        exchange = FakeExchange(
            position_snapshots=[[_position(size='1', pos_id='new')], []])
        with self.assertRaisesRegex(GateError, '新增/反向/身份变化'):
            _verify(ReadOnlyOkxGate(exchange), _baseline(), _okx())

    def test_non_reduce_pending_created_after_second_history_is_caught_by_s2(self):
        class LatePending(FakeExchange):
            def __init__(self):
                super().__init__()
                self.normal_calls = 0

            def privateGetTradeOrdersPending(self, params):
                self.normal_calls += 1
                if self.normal_calls == 2:
                    self.normal_pending_data = [_normal_pending('late-open', 'false')]
                return super().privateGetTradeOrdersPending(params)

        with self.assertRaisesRegex(GateError, '非 reduceOnly 普通 pending'):
            _verify(ReadOnlyOkxGate(LatePending()), _baseline(), _okx())

    def test_position_identity_direction_and_size_must_not_expand(self):
        old = [_canonical_position('1', 'long')]
        for current in (
                [_canonical_position('1', 'short')],
                [_canonical_position('2', 'long')],
                [_canonical_position('1', 'long', pos_id='new')]):
            with self.subTest(current=current), self.assertRaises(GateError):
                _compare_positions(old, current)
        _compare_positions(old, [])
        _compare_positions(old, [_canonical_position('0.5')])

    def test_net_mode_requires_net_position_shape(self):
        exchange = FakeExchange(positions=[_position(pos_side='long')])
        with self.assertRaisesRegex(GateError, 'posSide 必须为 net'):
            ReadOnlyOkxGate(exchange).positions()

    def test_account_config_must_bind_identity_and_safe_reduce_only_semantics(self):
        for config in (
                _account(acct_lv='1'),
                _account(acct_lv='4'),
                _account(pos_mode='long_short_mode'),
                _account(uid=''),
                _account(main_uid='')):
            with self.subTest(config=config), self.assertRaises(GateError):
                ReadOnlyOkxGate(FakeExchange(account_configs=[config])).account_config()

    def test_api_permission_parser_is_exact_and_never_returns_credentials(self):
        self.assertEqual(
            ('read_only',),
            ReadOnlyOkxGate(FakeExchange(account_configs=[
                _account(perm='read_only')])).api_permissions())
        self.assertEqual(
            ('read_only', 'trade'),
            ReadOnlyOkxGate(FakeExchange(account_configs=[
                _account(perm='read_only,trade')])).api_permissions())
        for malformed in ('', 'trade', 'read_only, trade',
                          'read_only,read_only', 'read_only,admin'):
            with self.subTest(perm=malformed), self.assertRaises(GateError):
                ReadOnlyOkxGate(FakeExchange(account_configs=[
                    _account(perm=malformed)])).api_permissions()

    def test_account_identity_change_during_window_fails(self):
        exchange = FakeExchange(account_configs=[
            _account(), _account(uid='2002', main_uid='2002')])
        with self.assertRaisesRegex(GateError, 'account config 发生变化'):
            _verify(ReadOnlyOkxGate(exchange), _baseline(), _okx())

    def test_baseline_account_change_fails(self):
        exchange = FakeExchange(
            server_times=[1000],
            account_configs=[_account(), _account(uid='2002', main_uid='2002')])
        with self.assertRaisesRegex(GateError, 'account config 发生变化'):
            _capture_baseline(
                ReadOnlyOkxGate(exchange), _okx(), SHA_A,
                {'nonce': NONCE, 'release_sha': SHA_A},
                {'dev': 1, 'ino': 2}, {'dev': 1, 'ino': 3})

    def test_proof_window_is_strictly_less_than_two_hours(self):
        exchange = FakeExchange(server_times=[1000 + MAX_PROOF_WINDOW_MS, 0])
        with self.assertRaisesRegex(GateError, '两小时'):
            _verify(ReadOnlyOkxGate(exchange), _baseline(), _okx())
        self.assertEqual(['account_config', 'time'], [name for name, _ in exchange.calls])

    def test_t2_at_retention_safety_deadline_is_rejected(self):
        safe_limit = MAX_PROOF_WINDOW_MS - PROOF_COMPLETION_SAFETY_MS
        exchange = FakeExchange(server_times=[1050, 1000 + safe_limit])
        with self.assertRaisesRegex(GateError, '安全截止线'):
            _verify(ReadOnlyOkxGate(exchange), _baseline(), _okx())

    def test_slow_post_t2_queries_cannot_cross_retention_deadline(self):
        safe_limit = MAX_PROOF_WINDOW_MS - PROOF_COMPLETION_SAFETY_MS
        t2_ms = 1000 + safe_limit - 50
        exchange = FakeExchange(server_times=[1050, t2_ms])
        with mock.patch.object(
                gate_module.time, 'monotonic_ns',
                side_effect=[0, 50 * 1_000_000]), \
                self.assertRaisesRegex(GateError, '安全截止线'):
            _verify(ReadOnlyOkxGate(exchange), _baseline(), _okx())

    def test_post_t2_deadline_guard_never_extends_history_claim(self):
        exchange = FakeExchange(server_times=[2000, 2100])
        with mock.patch.object(
                gate_module.time, 'monotonic_ns',
                side_effect=[0, 25 * 1_000_000]):
            result = _verify(ReadOnlyOkxGate(exchange), _baseline(), _okx())
        self.assertEqual(2100, result['history_verified_through_ms'])
        self.assertNotIn('proof_end_estimate_ms', result)

    def test_all_documented_algo_type_state_pairs_are_queried_in_both_phases(self):
        exchange = FakeExchange()
        _verify(ReadOnlyOkxGate(exchange), _baseline(), _okx())
        pairs = [
            (params['ordType'], params['state'])
            for name, params in exchange.calls if name == 'algo_history'
        ]
        expected = {
            (ord_type, state_name)
            for ord_type in ALGO_ORDER_TYPES
            for state_name in ALGO_HISTORY_STATES
        }
        self.assertEqual(expected, set(pairs))
        self.assertEqual(2 * len(expected), len(pairs))

    def test_full_page_then_query_error_never_looks_complete(self):
        cases = []
        normal = FakeExchange(normal_pending=[
            _normal_pending(f'n{index}') for index in range(100)])
        normal.fail_second_normal_page = True
        cases.append(normal)
        orders = FakeExchange(orders_history=[
            _history_order(f'h{index}') for index in range(100)])
        orders.fail_second_orders_page = True
        cases.append(orders)
        fills = FakeExchange(
            fills_history=[_fill(f'h{index}', f'b{index}') for index in range(100)],
            details={
                ('BTC-USDT-SWAP', f'h{index}'): _detail(f'h{index}')
                for index in range(100)
            })
        fills.fail_second_fills_page = True
        cases.append(fills)
        algos = FakeExchange(algo_history={
            ('conditional', 'effective'): [
                _algo_history(f'a{index}') for index in range(100)]})
        algos.fail_second_algo_history_page = True
        cases.append(algos)

        for exchange in cases:
            with self.subTest(exchange=exchange), self.assertRaisesRegex(
                    GateError, '查询失败'):
                _verify(ReadOnlyOkxGate(exchange), _baseline(), _okx())

    def test_malformed_baseline_schema_and_types_fail_closed(self):
        malformed = []
        unknown = _baseline()
        unknown['unknown'] = True
        malformed.append(unknown)
        wrong_schema = _baseline()
        wrong_schema['schema_version'] = 1
        malformed.append(wrong_schema)
        wrong_account = _baseline()
        wrong_account['account']['pos_mode'] = 'long_short_mode'
        malformed.append(wrong_account)
        wrong_nonce = _baseline()
        wrong_nonce['sentinel']['nonce'] = 'short'
        malformed.append(wrong_nonce)
        wrong_position = _baseline(positions=[_canonical_position()])
        wrong_position['positions'][0]['size_abs'] = 2
        malformed.append(wrong_position)
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(GateError):
                _validate_baseline(payload)


class SecureStateMachineTest(unittest.TestCase):
    @staticmethod
    def _write_config(directory):
        path = os.path.join(directory, 'config.json')
        payload = {
            'okx': _okx(),
            'strategy': {'default_risk_per_trade': 0.01},
            'trading': {'symbols': []},
            'scheduler': {'check_hour': 8, 'check_minute': 0},
        }
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle)
        os.chmod(path, 0o600)
        return path

    @staticmethod
    def _write_state(directory, last_daily_check_date):
        path = os.path.join(directory, 'trade_state.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({
                'open_positions': {},
                'closed_trades': [],
                'last_daily_check_date': last_daily_check_date,
            }, handle)
        os.chmod(path, 0o600)
        return path

    @staticmethod
    def _runner_lock(directory):
        runtime = os.path.join(directory, '.runtime')
        os.mkdir(runtime, 0o700)
        path = os.path.join(runtime, 'runner.lock')
        with open(path, 'w', encoding='ascii') as handle:
            handle.write('stopped\n')
        os.chmod(path, 0o600)
        return os.path.realpath(path)

    @staticmethod
    def _env(lock_path):
        return {'TRADING_RUNNER_LOCK_FILE': lock_path}

    def _prepare(self, directory):
        config = self._write_config(directory)
        lock = self._runner_lock(directory)
        env = self._env(lock)
        self.assertTrue(arm(directory, SHA_A, environ=env))
        run_baseline(
            config, directory, SHA_A, 900,
            exchange=FakeExchange(server_times=[1000], positions=[_position()]),
            environ=env)
        return config, lock, env

    def test_completed_slot_blocks_before_window_and_incomplete_day(self):
        with _canonical_temp_directory() as directory:
            config = self._write_config(directory)
            self._write_state(directory, '2026-07-18')
            with self.assertRaisesRegex(GateError, '宽限期'):
                require_completed_schedule_slot(
                    config, directory, datetime(2026, 7, 19, 7, 0))
            with self.assertRaisesRegex(GateError, '尚未成功完成'):
                require_completed_schedule_slot(
                    config, directory, datetime(2026, 7, 19, 8, 2))

            self._write_state(directory, '2026-07-19')
            self.assertEqual(
                '2026-07-19',
                require_completed_schedule_slot(
                    config, directory, datetime(2026, 7, 19, 8, 2)))

    def test_arm_requires_actual_lock_free_and_creates_random_json_sentinel(self):
        with _canonical_temp_directory() as directory:
            lock = self._runner_lock(directory)
            env = self._env(lock)
            held = os.open(lock, os.O_RDONLY)
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaisesRegex(GateError, 'runner 仍持锁'):
                    arm(directory, SHA_A, environ=env)
                self.assertFalse(os.path.lexists(os.path.join(directory, SENTINEL_NAME)))
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)
                os.close(held)

            self.assertTrue(arm(directory, SHA_A, environ=env))
            path = os.path.join(directory, SENTINEL_NAME)
            with open(path, encoding='utf-8') as handle:
                first = json.load(handle)
            self.assertEqual(SHA_A, first['release_sha'])
            self.assertEqual(64, len(first['nonce']))
            self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
            self.assertFalse(arm(directory, SHA_A, environ=env))

    def test_data_dir_must_be_canonical_absolute_path(self):
        with _canonical_temp_directory() as directory:
            lock = self._runner_lock(directory)
            alias = directory + os.sep + '.'
            with self.assertRaisesRegex(GateError, 'canonical'):
                arm(alias, SHA_A, environ=self._env(lock))
            self.assertFalse(os.path.exists(
                os.path.join(directory, SENTINEL_NAME)))

    def test_arm_cannot_succeed_on_renamed_detached_data_dir(self):
        with _canonical_temp_directory() as root:
            directory = os.path.join(root, 'data')
            detached = os.path.join(root, 'detached')
            os.mkdir(directory, 0o700)
            lock = self._runner_lock(directory)
            env = self._env(lock)
            original = gate_module._new_sentinel

            def replace_canonical_path(release_sha):
                os.rename(directory, detached)
                os.mkdir(directory, 0o700)
                self._runner_lock(directory)
                return original(release_sha)

            with mock.patch.object(
                    gate_module, '_new_sentinel',
                    side_effect=replace_canonical_path), \
                    self.assertRaisesRegex(
                        GateError, 'canonical data-dir.*替换|重命名'):
                arm(directory, SHA_A, environ=env)

            self.assertFalse(os.path.exists(
                os.path.join(directory, SENTINEL_NAME)))
            self.assertTrue(os.path.exists(
                os.path.join(detached, SENTINEL_NAME)))
            self.assertNotEqual(
                os.stat(os.path.join(
                    detached, '.runtime', 'runner.lock')).st_ino,
                os.stat(os.path.join(
                    directory, '.runtime', 'runner.lock')).st_ino)

    def test_arm_requires_exact_runner_lock_environment(self):
        with _canonical_temp_directory() as directory:
            lock = self._runner_lock(directory)
            for env in (
                    {'TRADING_RUNNER_LOCK_FILE': lock + '.other'},
                    {'TRADING_RUNNER_LOCK_FILE': os.path.relpath(lock)},
                    {}):
                with self.subTest(env=env), self.assertRaisesRegex(
                        GateError, 'TRADING_RUNNER_LOCK_FILE'):
                    arm(directory, SHA_A, environ=env)
            self.assertFalse(os.path.exists(os.path.join(directory, SENTINEL_NAME)))

    def test_seal_commit_requires_live_exact_runner_lock(self):
        with _canonical_temp_directory() as directory:
            config, lock, env = self._prepare(directory)
            result = run_verify(
                config, directory, SHA_A,
                exchange=FakeExchange(
                    server_times=[2000, 2100], positions=[_position(size='1')]),
                environ=env)
            self.assertEqual(1, result['positions'])

            result = seal(
                config, directory, SHA_A,
                exchange=FakeExchange(
                    server_times=[2200, 2300], positions=[_position(size='0.5')]),
                environ=env)
            self.assertEqual(1, result['positions'])
            self.assertTrue(os.path.exists(os.path.join(directory, SENTINEL_NAME)))
            self.assertTrue(os.path.exists(os.path.join(directory, BASELINE_NAME)))
            self.assertTrue(os.path.exists(os.path.join(directory, COMPLETION_NAME)))
            with self.assertRaisesRegex(GateError, 'runner 未持有'):
                commit_sealed(config, directory, SHA_A, environ=env)
            held = os.open(lock, os.O_RDONLY)
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                committed = commit_sealed(config, directory, SHA_A, environ=env)
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)
                os.close(held)
            self.assertEqual(1, committed['positions'])
            self.assertFalse(os.path.lexists(os.path.join(directory, SENTINEL_NAME)))
            with self.assertRaisesRegex(GateError, 'committed/damaged'):
                arm(directory, SHA_B, environ=self._env(lock))
            self.assertTrue(os.path.exists(os.path.join(directory, BASELINE_NAME)))
            self.assertTrue(os.path.exists(os.path.join(directory, COMPLETION_NAME)))

    def test_quiescence_proves_q0_through_q2_without_writing_t0(self):
        with _canonical_temp_directory() as directory:
            config = self._write_config(directory)
            lock = self._runner_lock(directory)
            env = self._env(lock)
            arm(directory, SHA_A, environ=env)
            result = run_quiescence(
                config, directory, SHA_A,
                exchange=FakeExchange(server_times=[1000, 2000, 2100]),
                environ=env)
            self.assertEqual(1000, result['q0_ms'])
            self.assertEqual(2000, result['t1_ms'])
            self.assertEqual(2100, result['t2_ms'])
            self.assertEqual(2100, result['history_verified_through_ms'])
            self.assertFalse(os.path.exists(os.path.join(directory, BASELINE_NAME)))

    def test_sealed_cycle_abandon_is_durable_and_idempotent(self):
        with _canonical_temp_directory() as directory:
            config, _, env = self._prepare(directory)
            seal(
                config, directory, SHA_A,
                exchange=FakeExchange(server_times=[2000, 2100]), environ=env)
            first = abandon_cycle(directory, SHA_A, '0001', environ=env)
            second = abandon_cycle(directory, SHA_A, '0001', environ=env)
            self.assertEqual(first, second)
            self.assertEqual(
                {SENTINEL_NAME, BASELINE_NAME, COMPLETION_NAME},
                set(first['archived_sha256']))
            for name in first['archived_sha256']:
                self.assertTrue(os.path.exists(os.path.join(
                    directory, f'.abandoned.0001.{name}')))
                self.assertFalse(os.path.exists(os.path.join(directory, name)))

    def test_baseline_is_v2_binds_every_identity_and_is_write_once(self):
        with _canonical_temp_directory() as directory:
            config = self._write_config(directory)
            lock = self._runner_lock(directory)
            env = self._env(lock)
            arm(directory, SHA_A, environ=env)
            run_baseline(
                config, directory, SHA_A, 900,
                exchange=FakeExchange(server_times=[1000]), environ=env)
            path = os.path.join(directory, BASELINE_NAME)
            before = os.stat(path)
            with open(path, encoding='utf-8') as handle:
                payload = json.load(handle)
            sentinel_info = os.stat(os.path.join(directory, SENTINEL_NAME))
            lock_info = os.stat(lock)
            self.assertEqual(2, payload['schema_version'])
            self.assertEqual(SHA_A, payload['release_sha'])
            self.assertEqual(_canonical_account(), payload['account'])
            self.assertEqual(sentinel_info.st_ino, payload['sentinel']['ino'])
            self.assertEqual(lock_info.st_ino, payload['runner_lock']['ino'])
            with self.assertRaisesRegex(GateError, '严禁覆盖'):
                run_baseline(
                    config, directory, SHA_A, 900,
                    exchange=FakeExchange(server_times=[1100]), environ=env)
            after = os.stat(path)
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))

    def test_baseline_verify_and_seal_require_runner_stopped(self):
        for operation in ('baseline', 'verify', 'seal'):
            with self.subTest(operation=operation), _canonical_temp_directory() as directory:
                config = self._write_config(directory)
                lock = self._runner_lock(directory)
                env = self._env(lock)
                arm(directory, SHA_A, environ=env)
                if operation in ('verify', 'seal'):
                    run_baseline(
                        config, directory, SHA_A, 900,
                        exchange=FakeExchange(server_times=[1000]), environ=env)
                held = os.open(lock, os.O_RDONLY)
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    with self.assertRaisesRegex(GateError, '服务未停'):
                        if operation == 'baseline':
                            run_baseline(
                                config, directory, SHA_A, 900,
                                exchange=FakeExchange(server_times=[1000]), environ=env)
                        elif operation == 'verify':
                            run_verify(
                                config, directory, SHA_A,
                                exchange=FakeExchange(), environ=env)
                        else:
                            seal(
                                config, directory, SHA_A,
                                exchange=FakeExchange(), environ=env)
                finally:
                    fcntl.flock(held, fcntl.LOCK_UN)
                    os.close(held)
                self.assertTrue(os.path.exists(os.path.join(directory, SENTINEL_NAME)))

    def test_seal_verify_failure_preserves_sentinel_baseline_and_no_completion(self):
        with _canonical_temp_directory() as directory:
            config, _, env = self._prepare(directory)
            with self.assertRaisesRegex(GateError, '非 reduceOnly normal order'):
                seal(
                    config, directory, SHA_A,
                    exchange=FakeExchange(
                        orders_history=[_history_order('open', 'false')]),
                    environ=env)
            self.assertTrue(os.path.exists(os.path.join(directory, SENTINEL_NAME)))
            self.assertTrue(os.path.exists(os.path.join(directory, BASELINE_NAME)))
            self.assertFalse(os.path.exists(os.path.join(directory, COMPLETION_NAME)))

    def test_seal_rejects_same_inode_baseline_content_mutation(self):
        with _canonical_temp_directory() as directory:
            config, _, env = self._prepare(directory)
            baseline_path = os.path.join(directory, BASELINE_NAME)
            original_verify = gate_module._verify

            def mutate_baseline(active_gate, baseline, okx):
                summary = original_verify(active_gate, baseline, okx)
                with open(baseline_path, encoding='utf-8') as handle:
                    changed = json.load(handle)
                changed['account']['uid'] = 'mutated-but-schema-valid'
                with open(baseline_path, 'w', encoding='utf-8') as handle:
                    json.dump(changed, handle)
                os.chmod(baseline_path, 0o600)
                return summary

            inode_before = os.stat(baseline_path).st_ino
            with mock.patch.object(
                    gate_module, '_verify', side_effect=mutate_baseline), \
                    self.assertRaisesRegex(GateError, '原位修改'):
                seal(
                    config, directory, SHA_A,
                    exchange=FakeExchange(server_times=[2000, 2100]),
                    environ=env)

            self.assertEqual(inode_before, os.stat(baseline_path).st_ino)
            self.assertTrue(os.path.exists(
                os.path.join(directory, SENTINEL_NAME)))

    def test_completion_fsync_failure_keeps_gate_for_explicit_abandon(self):
        with _canonical_temp_directory() as directory:
            config, _, env = self._prepare(directory)
            original_fsync = gate_module.os.fsync
            failed = False

            def fail_first_fsync(fd):
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError('injected completion fsync failure')
                return original_fsync(fd)

            with mock.patch.object(
                    gate_module.os, 'fsync', side_effect=fail_first_fsync):
                with self.assertRaisesRegex(GateError, '持久化'):
                    seal(
                        config, directory, SHA_A,
                        exchange=FakeExchange(server_times=[2000, 2100]),
                        environ=env)

            self.assertTrue(os.path.exists(os.path.join(directory, SENTINEL_NAME)))
            self.assertTrue(os.path.exists(os.path.join(directory, COMPLETION_NAME)))
            audit = abandon_cycle(directory, SHA_A, '0002', environ=env)
            self.assertIn(COMPLETION_NAME, audit['archived_sha256'])

    def test_replaced_sentinel_or_baseline_inode_blocks_verify_or_seal(self):
        for target_name, operation in (
                (SENTINEL_NAME, 'verify'),
                (BASELINE_NAME, 'seal')):
            with self.subTest(target=target_name), _canonical_temp_directory() as directory:
                config, _, env = self._prepare(directory)
                target = os.path.join(directory, target_name)
                if operation == 'verify':
                    with open(target, encoding='utf-8') as handle:
                        payload = json.load(handle)
                    replacement = target + '.new'
                    with open(replacement, 'w', encoding='utf-8') as handle:
                        json.dump(payload, handle)
                    os.chmod(replacement, 0o600)
                    os.replace(replacement, target)
                    with self.assertRaisesRegex(GateError, 'inode 不一致'):
                        run_verify(
                            config, directory, SHA_A,
                            exchange=FakeExchange(), environ=env)
                else:
                    class ReplacingExchange(FakeExchange):
                        replaced = False

                        def publicGetPublicTime(self, params):
                            if not self.replaced:
                                self.replaced = True
                                replacement = target + '.new'
                                with open(replacement, 'w', encoding='utf-8') as handle:
                                    handle.write('{}\n')
                                os.chmod(replacement, 0o600)
                                os.replace(replacement, target)
                            return super().publicGetPublicTime(params)

                    with self.assertRaisesRegex(GateError, '被替换|硬链接'):
                        seal(
                            config, directory, SHA_A,
                            exchange=ReplacingExchange(), environ=env)
                self.assertTrue(os.path.exists(os.path.join(directory, SENTINEL_NAME)))

    def test_release_sha_and_sentinel_nonce_are_bound(self):
        with _canonical_temp_directory() as directory:
            config, _, env = self._prepare(directory)
            with self.assertRaisesRegex(GateError, 'release SHA'):
                run_verify(
                    config, directory, SHA_B,
                    exchange=FakeExchange(), environ=env)

            path = os.path.join(directory, SENTINEL_NAME)
            with open(path, encoding='utf-8') as handle:
                payload = json.load(handle)
            payload['nonce'] = 'd' * 64
            replacement = path + '.new'
            with open(replacement, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle)
            os.chmod(replacement, 0o600)
            os.replace(replacement, path)
            with self.assertRaises(GateError):
                run_verify(
                    config, directory, SHA_A,
                    exchange=FakeExchange(), environ=env)

    def test_sentinel_absent_without_valid_completion_never_reports_success(self):
        with _canonical_temp_directory() as directory:
            config, _, env = self._prepare(directory)
            os.unlink(os.path.join(directory, SENTINEL_NAME))
            with self.assertRaisesRegex(GateError, 'sentinel/baseline'):
                seal(
                    config, directory, SHA_A,
                    exchange=FakeExchange(), environ=env)

    def test_config_environment_overlay_and_alias_conflict(self):
        with _canonical_temp_directory() as directory:
            path = self._write_config(directory)
            env = {
                'OKX_API_KEY': 'env-key',
                'OKX_API_SECRET': 'env-secret',
                'OKX_API_PASSPHRASE': 'env-pass',
            }
            okx = load_okx_config(path, environ=env)
            self.assertEqual('env-key', okx['apiKey'])
            with self.assertRaisesRegex(GateError, '值不一致'):
                load_okx_config(path, environ=dict(env, OKX_PASSWORD='other'))

    def test_bootstrap_permission_probe_requires_exact_read_only_then_trade(self):
        with _canonical_temp_directory() as directory:
            path = self._write_config(directory)
            read_only = probe_api_permission_mode(
                path, 'read_only', exchange=FakeExchange(
                    account_configs=[_account(perm='read_only')]), environ={})
            self.assertEqual(['read_only'], read_only['permissions'])
            with self.assertRaisesRegex(GateError, '精确为 read_only'):
                probe_api_permission_mode(
                    path, 'read_only', exchange=FakeExchange(
                        account_configs=[_account()]), environ={})
            restored = probe_api_permission_mode(
                path, 'trade', exchange=FakeExchange(
                    account_configs=[_account()]), environ={})
            self.assertIn('trade', restored['permissions'])
            with self.assertRaisesRegex(GateError, '禁止 withdraw'):
                probe_api_permission_mode(
                    path, 'trade', exchange=FakeExchange(account_configs=[
                        _account(perm='read_only,trade,withdraw')]), environ={})

    def test_public_history_exposure_gate_returns_only_safe_booleans(self):
        with _canonical_temp_directory() as directory:
            path = self._write_config(directory)
            summary = probe_public_history_exposure(path, environ={})
            self.assertFalse(
                summary['current_okx_key_matches_exposed_history'])
            self.assertFalse(
                summary['current_dingtalk_matches_exposed_history'])
            self.assertNotIn('api_fingerprint', summary)

            api_hash = hashlib.sha256(
                _okx()['apiKey'].encode('utf-8')).hexdigest()
            with mock.patch.object(
                    gate_module, 'EXPOSED_OKX_API_KEY_FINGERPRINTS',
                    frozenset({api_hash})), self.assertRaisesRegex(
                        GateError, 'OKX API Key'):
                probe_public_history_exposure(path, environ={})

            hook = 'https://example.invalid/test-hook'
            hook_hash = hashlib.sha256(hook.encode('utf-8')).hexdigest()
            with mock.patch.object(
                    gate_module, 'EXPOSED_DINGTALK_WEBHOOK_FINGERPRINTS',
                    frozenset({hook_hash})), self.assertRaisesRegex(
                        GateError, 'DingTalk webhook'):
                probe_public_history_exposure(
                    path, environ={'DINGTALK_WEBHOOK': hook})

    def test_symlinks_hardlinks_and_unsafe_runtime_mode_are_rejected(self):
        with _canonical_temp_directory() as directory:
            lock = self._runner_lock(directory)
            env = self._env(lock)
            os.chmod(os.path.dirname(lock), 0o755)
            with self.assertRaisesRegex(GateError, '精确为 0700'):
                arm(directory, SHA_A, environ=env)
        with _canonical_temp_directory() as directory:
            lock = self._runner_lock(directory)
            env = self._env(lock)
            target = os.path.join(directory, 'target')
            Path(target).touch()
            os.chmod(target, 0o600)
            os.symlink(target, os.path.join(directory, SENTINEL_NAME))
            with self.assertRaises(GateError):
                arm(directory, SHA_A, environ=env)

    def test_cli_requires_release_sha_and_config_on_non_arm_commands(self):
        parser = _build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(['arm', '--data-dir', '/tmp'])
            with self.assertRaises(SystemExit):
                parser.parse_args([
                    'seal', '--data-dir', '/tmp', '--release-sha', SHA_A])

    def test_only_locked_ccxt_constructor_and_read_methods_are_used(self):
        calls = []

        class Exchange:
            def set_sandbox_mode(self, value):
                calls.append(('sandbox', value))

        class FakeCcxt:
            __version__ = '4.5.64'

            @staticmethod
            def okx(config):
                calls.append(('okx', copy.deepcopy(config)))
                return Exchange()

        okx = _okx()
        okx['sandbox'] = True
        self.assertIsInstance(
            create_read_only_exchange(okx, ccxt_module=FakeCcxt), Exchange)
        self.assertEqual('okx', calls[0][0])
        self.assertEqual(('sandbox', True), calls[1])

        source = Path(gate_module.__file__).read_text(encoding='utf-8')
        self.assertNotIn('private' + 'Post', source)
        self.assertNotIn('create_' + 'order(', source)
        self.assertNotIn('Okx' + 'Api(', source)
        for method in ReadOnlyOkxGate.REQUIRED_METHODS:
            self.assertIn(method, source)

    def test_missing_new_read_capability_fails_at_construction(self):
        exchange = FakeExchange()
        exchange.privateGetTradeOrdersAlgoHistory = None
        with self.assertRaisesRegex(GateError, 'OrdersAlgoHistory'):
            ReadOnlyOkxGate(exchange)


if __name__ == '__main__':
    unittest.main()
