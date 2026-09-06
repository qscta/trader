"""一次性人工减仓离线全链路：真实适配/账本/HTTP，交易所为内存替身，绝不联网。"""
import copy
import logging
import os
import tempfile
import threading
import unittest
from decimal import Decimal, ROUND_DOWN
from types import SimpleNamespace
from unittest.mock import Mock, patch

logging.getLogger().addHandler(logging.NullHandler())
os.environ.setdefault('FLASK_SECRET_KEY', 'offline-test-secret')
os.environ.setdefault('TRADING_API_TOKEN', 'offline-test-token')
import api_server
import main
from okx_api import OkxApi
from partial_close import preview_reduction, execute_reduction
from trade_state import TradeState, TradeStatePersistenceError, completed_trade_groups


class PartialCloseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.system = s = main.TradingSystem.__new__(main.TradingSystem)
        s.trade_state = TradeState(os.path.join(tmp.name, 'trade_state.json'))
        s.trade_state.add_open_position('BTCUSDT', 'long', 100., 100., 80., 'stop-1', 'ma_cross')
        s.config = {'trading': {'symbols': [{'name': 'BTCUSDT', 'enabled': True, 'risk_per_trade': .005}]},
                    'strategy': {'default_risk_per_trade': .005}}
        s._trade_lock = threading.Lock()
        s._stop_anomalies, s._stop_anomaly_alerts, s.stop_loss_dates = {}, {}, {}
        s._known_orphans = set()
        s.notifier = Mock()
        s.label, s.exchange_id = 'offline', 'okx'
        s.exchange_api = self.api = api = OkxApi.__new__(OkxApi)
        api.margin_mode = 'cross'
        api.CANCEL_VERIFY_RECHECK_DELAY = 0
        api._contract_size_cache = {'BTC/USDT:USDT': 1.}
        api._amount_precision_cache = {}
        self.remaining, self.fill, self.side = 100., 30., 'long'
        self.order = None
        self.algos = [{'id': 'stop-1', 'side': 'sell', 'reduceOnly': True,
                       'info': {'sz': '100', 'slTriggerPx': '80', 'slOrdPx': '-1', 'ordType': 'conditional'}}]
        api.get_position = Mock(side_effect=lambda symbol:
            {'side': self.side, 'contracts': self.remaining} if self.remaining else None)
        api._fetch_algo_orders = Mock(side_effect=lambda symbol: copy.deepcopy(self.algos))
        api.exchange = SimpleNamespace(
            market=Mock(return_value={'limits': {'amount': {'min': 1}}, 'contractSize': 1.}),
            amount_to_precision=lambda symbol, value: str(Decimal(str(value)).to_integral_value(rounding=ROUND_DOWN)),
            price_to_precision=lambda symbol, value: str(value),
            privatePostTradeOrder=Mock(side_effect=self.submit),
            privateGetTradeOrder=Mock(side_effect=lambda params: {'code': '0', 'data': [copy.deepcopy(self.order)]}),
            privatePostTradeCancelOrder=Mock(),
            privatePostTradeAmendAlgos=Mock(side_effect=self.amend))
        self.enterContext(patch.object(api_server, 'trading_system', s))
        self.enterContext(patch.dict(os.environ, {'TRADING_ENABLE_PARTIAL_CLOSE': '1'}))
        self.client = api_server.app.test_client()
        with self.client.session_transaction() as session:
            session['authenticated'] = True

    def submit(self, params):
        pending = self.system.trade_state.get_open_position('BTCUSDT')['pending_reduction']
        self.assertEqual(pending['client_order_id'], params['clOrdId'])  # 先落盘才准发单
        self.assertIs(params['reduceOnly'], True)
        self.assertEqual(params['posSide'], 'net')
        self.remaining -= self.fill
        self.order = dict(params, ordId='real-offline-id', state='filled', accFillSz=str(self.fill),
                          avgPx='110', reduceOnly='true')
        return {'code': '0', 'data': [{'ordId': 'real-offline-id', 'sCode': '0'}]}

    def amend(self, params):
        self.assertIs(params['cxlOnFail'], False)
        self.assertNotIn('newSlTriggerPx', params)
        self.algos[0]['info']['sz'] = params['newSz']
        return {'code': '0', 'data': [{'sCode': '0'}]}

    def quote(self, percent=30):
        return preview_reduction(self.system, 'BTCUSDT', percent)

    def test_success_keeps_remainder_stop_and_future_risk(self):
        config = copy.deepcopy(self.system.config)
        before = self.system.trade_state.get_open_position('BTCUSDT')
        result = execute_reduction(self.system, self.quote())
        after = self.system.trade_state.get_open_position('BTCUSDT')
        self.assertEqual(result['filled_size'], 30.)
        self.assertEqual(after['position_size'], 70.)
        self.assertEqual(after['stop_loss_price'], 80.)
        self.assertEqual(after['open_time'], before['open_time'])
        self.assertEqual(self.system.config, config)
        self.assertEqual(self.system.stop_loss_dates, {})
        self.assertTrue(self.api.partial_stop_is_intact('BTCUSDT', 'long', 70., 80., 'stop-1'))
        self.assertNotIn('pending_reduction', after)
        self.api.exchange.privatePostTradeOrder.assert_called_once()

    def test_duplicate_and_two_previews_cannot_reduce_twice(self):
        first, second = self.quote(), self.quote()
        execute_reduction(self.system, first)
        for quote in (first, second):
            with self.assertRaises(ValueError):
                execute_reduction(self.system, quote)
        self.assertEqual(self.remaining, 70.)
        self.api.exchange.privatePostTradeOrder.assert_called_once()

    def test_deliberate_second_operation_uses_remaining_position(self):
        execute_reduction(self.system, self.quote())
        self.fill = 35.
        execute_reduction(self.system, self.quote(50))
        self.assertEqual(self.system.trade_state.get_open_position('BTCUSDT')['position_size'], 35.)
        self.assertEqual(self.api.exchange.privatePostTradeOrder.call_count, 2)

    def test_invalid_or_dust_percentage_never_sends_order(self):
        for value in (None, True, 0, -1, 100, 101, 'nan', 'inf', .1):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                self.quote(value)
        self.api.exchange.privatePostTradeOrder.assert_not_called()

    def test_rounding_never_exceeds_requested_fraction(self):
        quote = self.quote(99.9)
        self.assertEqual(quote['reduce_size'], 99.)
        self.assertEqual(quote['remaining_size'], 1.)

    def test_fractional_contract_value_preserves_exact_remaining_contracts(self):
        self.system.trade_state.close_position('BTCUSDT', 100.)
        self.system.trade_state.add_open_position('BTCUSDT', 'long', 100., 10.1, 80., 'stop-1', 'ma_cross')
        self.api._contract_size_cache['BTC/USDT:USDT'] = .1
        self.remaining = 101.
        self.algos[0]['info']['sz'] = '101'
        result = execute_reduction(self.system, self.quote())
        self.assertEqual(result['filled_size'], 3.)
        self.assertEqual(result['remaining_size'], 7.1)
        self.assertEqual(self.api._coin_to_contracts('BTC/USDT:USDT', 7.1), 71.)

    def test_short_position_reduces_with_buy_and_preserves_stop(self):
        self.side = 'short'
        self.system.trade_state.close_position('BTCUSDT', 100.)
        self.system.trade_state.add_open_position('BTCUSDT', 'short', 100., 100., 120., 'stop-1', 'ma_cross')
        self.algos[0]['side'], self.algos[0]['info']['slTriggerPx'] = 'buy', '120'
        execute_reduction(self.system, self.quote())
        self.assertEqual(self.api.exchange.privatePostTradeOrder.call_args.args[0]['side'], 'buy')
        self.assertEqual(self.system.trade_state.get_open_position('BTCUSDT')['position_size'], 70.)

    def test_stale_position_and_changed_stop_refuse_before_submit(self):
        quote = self.quote()
        self.remaining = 90.
        with self.assertRaises(ValueError):
            execute_reduction(self.system, quote)
        self.remaining = 100.
        self.algos[0]['id'] = 'foreign-stop'
        with self.assertRaises(ValueError):
            execute_reduction(self.system, quote)
        self.api.exchange.privatePostTradeOrder.assert_not_called()

    def test_amend_timeout_but_actual_success_can_be_confirmed(self):
        def timeout(params):
            self.amend(params)
            raise TimeoutError('response lost')
        self.api.exchange.privatePostTradeAmendAlgos.side_effect = timeout
        execute_reduction(self.system, self.quote())
        self.assertEqual(self.system.trade_state.get_open_position('BTCUSDT')['position_size'], 70.)
        self.api.exchange.privatePostTradeAmendAlgos.assert_called_once()

    def test_cannot_submit_expired_preview(self):
        response = self.client.post('/api/reduce_position/preview', json={'name': 'BTCUSDT', 'percent': 30})
        token = response.get_json()['token']
        import time
        later = time.time() + 121
        with patch('itsdangerous.timed.time.time', return_value=later):
            self.assertEqual(self.client.post('/api/reduce_position', json={'token': token}).status_code, 409)
        self.api.exchange.privatePostTradeOrder.assert_not_called()

    def test_timeout_after_fill_is_queried_without_resending(self):
        def timeout(params):
            self.submit(params)
            raise TimeoutError('response lost')
        self.api.exchange.privatePostTradeOrder.side_effect = timeout
        execute_reduction(self.system, self.quote())
        self.assertEqual(self.remaining, 70.)
        self.api.exchange.privatePostTradeOrder.assert_called_once()

    def test_terminal_partial_fill_commits_only_actual_amount(self):
        self.fill = 12.
        original = self.submit
        def partial(params):
            result = original(params)
            self.order['state'] = 'canceled'
            return result
        self.api.exchange.privatePostTradeOrder.side_effect = partial
        result = execute_reduction(self.system, self.quote())
        self.assertEqual(result['filled_size'], 12.)
        self.assertEqual(result['remaining_size'], 88.)
        self.api.exchange.privatePostTradeOrder.assert_called_once()

    def test_zero_fill_is_terminal_and_does_not_create_trade(self):
        self.fill = 0.
        execute_reduction(self.system, self.quote())
        self.assertEqual(self.system.trade_state.get_closed_trades(), [])
        self.assertEqual(self.system.trade_state.get_open_position('BTCUSDT')['reduction_revision'], 1)
        self.api.exchange.privatePostTradeAmendAlgos.assert_not_called()

    def test_live_partial_order_is_canceled_once_then_only_actual_fill_committed(self):
        self.fill = 12.
        def live(params):
            self.submit(params)
            self.order['state'] = 'partially_filled'
        def cancel(params):
            self.assertEqual(params['clOrdId'], self.order['clOrdId'])
            self.order['state'] = 'canceled'
        self.api.exchange.privatePostTradeOrder.side_effect = live
        self.api.exchange.privatePostTradeCancelOrder.side_effect = cancel
        result = execute_reduction(self.system, self.quote())
        self.assertEqual(result['remaining_size'], 88.)
        self.api.exchange.privatePostTradeOrder.assert_called_once()
        self.api.exchange.privatePostTradeCancelOrder.assert_called_once()

    def test_exchange_auto_resized_stop_needs_no_amend(self):
        def auto_resize(params):
            self.submit(params)
            self.algos[0]['info']['sz'] = str(self.remaining)
        self.api.exchange.privatePostTradeOrder.side_effect = auto_resize
        execute_reduction(self.system, self.quote())
        self.api.exchange.privatePostTradeAmendAlgos.assert_not_called()
        self.assertEqual(self.system.trade_state.get_open_position('BTCUSDT')['position_size'], 70.)

    def test_missing_extra_or_foreign_stop_refuses_before_submit(self):
        original = copy.deepcopy(self.algos)
        foreign = copy.deepcopy(original[0])
        foreign['info']['ordType'] = 'oco'
        for algos in ([], original * 2, [foreign]):
            self.algos = algos
            with self.assertRaises(ValueError):
                self.quote()
        self.api.exchange.privatePostTradeOrder.assert_not_called()

    def test_pending_daily_check_leaves_symbol_untouched(self):
        s = self.system
        s.trade_state.begin_reduction('BTCUSDT', self.quote())
        s.equity_tracker = Mock()
        s.send_daily_position_summary_if_due = Mock()
        s._last_check_date = None
        self.api.get_position.reset_mock()
        s.check_and_execute_trades()
        self.api.get_position.assert_not_called()
        self.api.exchange.privatePostTradeOrder.assert_not_called()
        self.assertIn('pending_reduction', s.trade_state.get_open_position('BTCUSDT'))

    def test_unknown_order_survives_restart_and_blocks_management(self):
        self.api.exchange.privateGetTradeOrder.side_effect = TimeoutError('offline')
        with self.assertRaises(RuntimeError):
            execute_reduction(self.system, self.quote())
        s = self.system
        s.trade_state = TradeState(s.trade_state.state_file)
        self.assertIn('pending_reduction', s.trade_state.get_open_position('BTCUSDT'))
        self.api.exchange.privatePostTradeOrder.assert_called_once()
        self.api.get_position.reset_mock()
        s.sync_positions_on_startup()
        s._reconcile_symbol_intraday('BTCUSDT', s.trade_state.get_open_position('BTCUSDT'), {})
        s._execute_open('BTCUSDT', 'long', 100, 80, {})
        response = self.client.post('/api/close_position', json={'name': 'BTCUSDT'})
        self.assertEqual(response.status_code, 409)
        self.api.get_position.assert_not_called()

    def test_stop_amend_failure_isolates_without_canceling_protection(self):
        self.api.exchange.privatePostTradeAmendAlgos.side_effect = TimeoutError('offline')
        with self.assertRaises(RuntimeError):
            execute_reduction(self.system, self.quote())
        self.assertEqual(self.algos[0]['info']['sz'], '100')
        self.assertIn('pending_reduction', self.system.trade_state.get_open_position('BTCUSDT'))
        self.api.exchange.privatePostTradeCancelOrder.assert_not_called()

    def test_limit_or_unverifiable_stop_refuses_before_submit(self):
        for price in ('79', '', None):
            with self.subTest(price=price):
                self.algos[0]['info']['slOrdPx'] = price
                with self.assertRaises(ValueError):
                    self.quote()
        self.api.exchange.privatePostTradeOrder.assert_not_called()

    def test_stop_changed_to_limit_after_fill_keeps_isolation(self):
        def changed_stop(params):
            self.submit(params)
            self.algos[0]['info']['slOrdPx'] = '79'
        self.api.exchange.privatePostTradeOrder.side_effect = changed_stop
        with self.assertRaises(RuntimeError):
            execute_reduction(self.system, self.quote())
        self.assertIn('pending_reduction', self.system.trade_state.get_open_position('BTCUSDT'))
        self.api.exchange.privatePostTradeAmendAlgos.assert_not_called()
        self.api.exchange.privatePostTradeOrder.assert_called_once()

    def test_stop_trigger_race_is_not_falsely_booked_as_manual_close(self):
        def stop_race(params):
            self.submit(params)
            self.remaining = 0.
        self.api.exchange.privatePostTradeOrder.side_effect = stop_race
        with self.assertRaises(RuntimeError):
            execute_reduction(self.system, self.quote())
        self.assertEqual(self.system.trade_state.get_closed_trades(), [])
        self.api.exchange.privatePostTradeAmendAlgos.assert_not_called()

    def test_bad_order_evidence_is_never_committed(self):
        for key, value in (('clOrdId', 'foreign'), ('reduceOnly', 'false'), ('avgPx', 'nan'), ('accFillSz', '40')):
            with self.subTest(key=key):
                q = self.quote()
                self.order = {'instId': 'BTC-USDT-SWAP', 'clOrdId': q['client_order_id'],
                              'ordId': '1', 'state': 'filled', 'side': 'sell', 'posSide': 'net',
                              'ordType': 'market', 'reduceOnly': 'true', 'sz': '30', 'accFillSz': '30', 'avgPx': '110'}
                self.order[key] = value
                with self.assertRaises(RuntimeError):
                    self.api._partial_order_terminal('BTCUSDT', q)

    def test_persist_failure_before_submit_sends_nothing(self):
        q = self.quote()
        with patch.object(self.system.trade_state, 'save_state', side_effect=TradeStatePersistenceError('disk')):
            with self.assertRaises(TradeStatePersistenceError):
                execute_reduction(self.system, q)
        self.api.exchange.privatePostTradeOrder.assert_not_called()
        self.assertNotIn('pending_reduction', self.system.trade_state.get_open_position('BTCUSDT'))

    def test_persist_failure_after_submit_keeps_durable_isolation(self):
        q = self.quote()
        save = self.system.trade_state.save_state
        calls = []
        def fail_second():
            calls.append(True)
            if len(calls) == 2:
                raise TradeStatePersistenceError('disk')
            return save()
        with patch.object(self.system.trade_state, 'save_state', side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                execute_reduction(self.system, q)
        reloaded = TradeState(self.system.trade_state.state_file)
        self.assertIn('pending_reduction', reloaded.get_open_position('BTCUSDT'))
        self.assertEqual(reloaded.get_closed_trades(), [])

    def test_http_preview_signed_confirmation_and_lock(self):
        response = self.client.post('/api/reduce_position/preview', json={'name': 'BTCUSDT', 'percent': 30})
        self.assertEqual(response.status_code, 200)
        token = response.get_json()['token']
        self.api.exchange.privatePostTradeOrder.assert_not_called()
        self.system._trade_lock.acquire()
        self.assertEqual(self.client.post('/api/reduce_position', json={'token': token}).status_code, 409)
        self.system._trade_lock.release()
        self.assertEqual(self.client.post('/api/reduce_position', json={'token': token + 'bad'}).status_code, 409)
        self.assertEqual(self.client.post('/api/reduce_position', json={'token': token}).status_code, 200)
        self.assertEqual(self.client.post('/api/reduce_position', json={'token': token}).status_code, 409)
        self.api.exchange.privatePostTradeOrder.assert_called_once()

    def test_http_requires_auth_and_explicit_feature_gate(self):
        with patch.dict(os.environ, {'TRADING_ENABLE_PARTIAL_CLOSE': '0'}):
            self.assertEqual(self.client.post('/api/reduce_position/preview', json={}).status_code, 403)
        anonymous = api_server.app.test_client()
        self.assertEqual(anonymous.post('/api/reduce_position/preview', json={}).status_code, 401)
        self.api.exchange.privatePostTradeOrder.assert_not_called()

    def test_win_rate_counts_complete_position_not_partial_exits(self):
        execute_reduction(self.system, self.quote())
        exits = self.system.trade_state.get_closed_trades()
        self.assertEqual(completed_trade_groups(exits), [])
        self.system.trade_state.close_position('BTCUSDT', 120.)
        exits = self.system.trade_state.get_closed_trades()
        groups = completed_trade_groups(exits)
        self.assertEqual(len(groups), 1)
        self.assertAlmostEqual(groups[0]['pnl'], sum(t['pnl'] for t in exits))

    def test_remainder_is_held_then_reversed_without_top_up(self):
        execute_reduction(self.system, self.quote())
        s = self.system
        s._flip_position, s._execute_open = Mock(), Mock()
        position = s.trade_state.get_open_position('BTCUSDT')
        s.handle_open_position_ma_cross('BTCUSDT', {'ema_short': 110, 'ema_long': 100}, position, {}, None)
        s._flip_position.assert_not_called()
        s._execute_open.assert_not_called()
        s.handle_open_position_ma_cross('BTCUSDT', {'ema_short': 90, 'ema_long': 100}, position, {}, None)
        self.assertEqual(s._flip_position.call_args.args[2]['position_size'], 70.)
        self.assertEqual(s._flip_position.call_args.args[3], 'short')
