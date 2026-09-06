"""日检恢复回归：真实 EMA/行情校验/账本，交易所与推送全隔离。"""
import logging
import os
import tempfile
import threading
import unittest
from datetime import date, timedelta
from unittest.mock import Mock, patch

import pandas as pd

logging.getLogger().addHandler(logging.NullHandler())
import main
from exchange_base import ExchangeApi
from ma_cross_strategy import MaCrossStrategy
from trade_state import TradeState


class DailyCheckRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.today = date(2026, 9, 4)
        self.enterContext(patch.object(main, 'date', Mock(
            wraps=date, today=Mock(side_effect=lambda: self.today))))
        self.enterContext(patch('exchange_base.time.time', side_effect=lambda:
            pd.Timestamp(self.today, tz='UTC').timestamp() + 10))
        s = self.system = main.TradingSystem.__new__(main.TradingSystem)
        s.label = 'offline'
        s.config = {'trading': {'symbols': [
            {'name': 'BTCUSDT', 'enabled': True, 'strategy': 'ma_cross', 'risk_per_trade': .01}
        ]}, 'strategy': {'default_risk_per_trade': .01}}
        s.trade_state = TradeState(os.path.join(tmp.name, 'trade_state.json'))
        s.trade_state.add_open_position('BTCUSDT', 'long', 100., 1., 70., 's1', 'ma_cross')
        s._trade_lock = threading.Lock()
        s._last_check_date = None
        s._last_failure_notify_ts = 0
        s._stop_anomalies = {}
        s._stop_anomaly_alerts = {}
        s.stop_loss_dates = {}
        s.stop_loss_file = os.path.join(tmp.name, 'stop_loss_dates.json')
        s.equity_tracker = Mock()
        s.send_daily_position_summary_if_due = Mock()
        s.notifier = Mock()
        s.exchange_api = Mock()
        s.exchange_api.to_ccxt_symbol.return_value = 'BTC/USDT:USDT'
        s.exchange_api.get_position.return_value = {'side': 'long', 'contracts': 1.}
        s.exchange_api.managed_position_matches.side_effect = (
            lambda _, actual, side, size: actual['side'] == side and actual['contracts'] == size)
        s.exchange_api.get_last_price.return_value = 90.
        s.exchange_api.close_position.return_value = None
        converter = ExchangeApi.__new__(ExchangeApi)
        s.exchange_api.ohlcv_to_dataframe.side_effect = converter.ohlcv_to_dataframe
        s.exchange_api.filter_closed_candles.side_effect = converter.filter_closed_candles
        s.ma_cross_strategy = MaCrossStrategy()
        s._execute_open = Mock(side_effect=lambda symbol, side, price, stop, config:
            s.trade_state.add_open_position(symbol, side, price, 1., stop, 's2', 'ma_cross'))
        self.market([100.] * 80 + [90.])

    def market(self, closes, end=None):
        stamps = pd.date_range(end=end or self.today - timedelta(days=1),
                               periods=len(closes), freq='D', tz='UTC')
        self.system.exchange_api.fetch_ohlcv.return_value = [
            [int(ts.timestamp() * 1000), v, v, v, v, 1] for ts, v in zip(stamps, closes)]
        frame = self.system.exchange_api.ohlcv_to_dataframe(
            self.system.exchange_api.fetch_ohlcv.return_value)
        return self.system.ma_cross_strategy.check_signal(frame)

    def assert_old_position_protected(self):
        s = self.system
        self.assertEqual(s.trade_state.get_open_position('BTCUSDT')['stop_order_id'], 's1')
        s.exchange_api.cancel_order.assert_not_called()
        s.exchange_api.cancel_all_orders.assert_not_called()
        s._execute_open.assert_not_called()

    def test_failed_close_retries_same_day_then_success_is_not_repeated(self):
        s = self.system
        s.check_and_execute_trades()
        self.assertIsNone(s._last_check_date)
        self.assert_old_position_protected()
        s.exchange_api.close_position.return_value = {'average': 90.}
        s.check_and_execute_trades()
        self.assertEqual(s.trade_state.get_open_position('BTCUSDT')['side'], 'short')
        self.assertEqual(s._last_check_date, self.today.isoformat())
        s.check_and_execute_trades()
        self.assertEqual(s.exchange_api.close_position.call_count, 2)
        s._execute_open.assert_called_once()

    def test_failed_close_retries_next_day_without_fresh_cross(self):
        s = self.system
        s.check_and_execute_trades()
        self.today += timedelta(days=1)
        self.assertIsNone(self.market([100.] * 80 + [90., 90.])['action'])
        s.check_and_execute_trades()
        self.assertEqual(s.exchange_api.close_position.call_count, 2)
        self.assert_old_position_protected()

    def test_current_direction_overrides_old_cross_and_equal_means_hold(self):
        s = self.system
        position = s.trade_state.get_open_position('BTCUSDT')
        s._flip_position = Mock(return_value=False)
        for held, fast, slow, expected in (
                ('long', 90, 100, 'short'), ('short', 110, 100, 'long'),
                ('long', 110, 100, None), ('short', 90, 100, None),
                ('long', 100, 100, None), ('short', 100, 100, None)):
            with self.subTest(held=held, fast=fast):
                s._flip_position.reset_mock()
                position['side'] = held
                s.exchange_api.get_position.return_value['side'] = held
                signal = {'ema_short': fast, 'ema_long': slow, 'action': None}
                result = s.handle_open_position_ma_cross('BTCUSDT', signal, position, {}, None)
                if expected:
                    self.assertIs(result, False)
                    self.assertEqual(s._flip_position.call_args.args[3], expected)
                else:
                    s._flip_position.assert_not_called()

    def test_partial_close_is_isolated_instead_of_blind_resend(self):
        s = self.system
        s.check_and_execute_trades()
        s.exchange_api.get_position.return_value['contracts'] = .5
        s.check_and_execute_trades()
        self.assertEqual(s.exchange_api.close_position.call_count, 1)
        self.assertEqual(s._stop_anomalies['BTCUSDT'], 'position_mismatch')
        self.assert_old_position_protected()

    def test_removed_symbol_retry_only_closes(self):
        s = self.system
        s.config['trading']['symbols'] = []
        s.check_and_execute_trades()
        self.assertIsNone(s._last_check_date)
        s.exchange_api.close_position.return_value = {'average': 90.}
        self.today += timedelta(days=1)
        self.assertIsNone(self.market([100.] * 80 + [90., 90.])['action'])
        s.check_and_execute_trades()
        self.assertIsNone(s.trade_state.get_open_position('BTCUSDT'))
        self.assertEqual(s.stop_loss_dates, {})
        s._execute_open.assert_not_called()

    def test_empty_market_and_no_closed_candles_are_retried(self):
        s = self.system
        for kind in ('empty', 'unclosed'):
            with self.subTest(kind=kind):
                s._last_check_date = None
                if kind == 'empty':
                    s.exchange_api.fetch_ohlcv.return_value = []
                else:
                    self.market([100.], end=self.today)
                s.check_and_execute_trades()
                self.assertIsNone(s._last_check_date)
                s.exchange_api.close_position.assert_not_called()
        self.market([100.] * 81)  # 行情恢复但无反向方向，完成日检
        s.check_and_execute_trades()
        self.assertEqual(s._last_check_date, self.today.isoformat())

    def test_missing_strategy_result_is_not_success(self):
        self.system.ma_cross_strategy.check_signal = Mock(return_value=None)
        self.system.check_and_execute_trades()
        self.assertIsNone(self.system._last_check_date)
        self.assert_old_position_protected()
