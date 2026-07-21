import threading
import unittest
from datetime import date
from types import SimpleNamespace

from tests.unit import _test_stubs

TradingSystem = _test_stubs.import_main().TradingSystem


class _FakeNotifier:
    def __init__(self, result=True):
        self.result = result
        self.calls = 0
        self.send_message_calls = 0
        self.trade_open_summary_calls = 0
        self.trade_open_summary_payload = None
        self.trade_close_summary_calls = 0
        self.trade_close_summary_payload = None

    def notify_position_summary(self, positions, symbols_config, total_equity):
        self.calls += 1
        return self.result

    def send_message(self, title, content):
        self.send_message_calls += 1
        return True

    def notify_trade_opened_summary(self, trades):
        self.trade_open_summary_calls += 1
        self.trade_open_summary_payload = trades
        return True

    def notify_trade_closed_summary(self, trades):
        self.trade_close_summary_calls += 1
        self.trade_close_summary_payload = trades
        return True


class DailySummaryDeliveryTest(unittest.TestCase):
    def _build_system(self, notify_result=True):
        system = TradingSystem.__new__(TradingSystem)
        system.trade_state = SimpleNamespace(get_all_open_positions=lambda: {'BTCUSDT': {'side': 'long'}})
        system.config = {'trading': {'symbols': [{'name': 'BTCUSDT', 'enabled': True}]}}
        system.exchange_api = SimpleNamespace(get_balance=lambda: {'total': {'USDT': 1000}})
        system.notifier = _FakeNotifier(result=notify_result)
        system._last_summary_date = None
        system._summary_lock = threading.Lock()
        system._pending_trade_open_notifications = []
        system._pending_trade_close_notifications = []
        return system

    def test_daily_summary_is_only_sent_once_per_day(self):
        system = self._build_system(notify_result=True)
        today = date.today().isoformat()

        self.assertTrue(system.send_daily_position_summary_if_due())
        self.assertFalse(system.send_daily_position_summary_if_due())
        self.assertEqual(today, system._last_summary_date)
        self.assertEqual(1, system.notifier.calls)

    def test_daily_summary_failure_does_not_mark_day_as_sent(self):
        system = self._build_system(notify_result=False)

        self.assertFalse(system.send_daily_position_summary_if_due())
        self.assertIsNone(system._last_summary_date)
        self.assertFalse(system.send_daily_position_summary_if_due())
        self.assertEqual(2, system.notifier.calls)

    def test_daily_summary_can_send_without_marking_day_as_sent(self):
        system = self._build_system(notify_result=True)

        self.assertTrue(system.send_daily_position_summary_if_due(mark_sent=False))
        self.assertIsNone(system._last_summary_date)
        self.assertEqual(1, system.notifier.calls)

    def test_manual_trade_check_summary_does_not_consume_scheduled_summary(self):
        system = self._build_system(notify_result=True)
        summary_calls = []
        system._trade_lock = SimpleNamespace(acquire=lambda blocking=False: True, release=lambda: None)
        system._last_check_date = None
        system._last_failure_notify_ts = 0
        system.equity_tracker = SimpleNamespace(
            record_daily_equity_snapshot=lambda: None,
            refresh_account_stats_state=lambda: None,
        )
        system.trade_state = SimpleNamespace(
            get_all_open_positions=lambda: {},
            get_open_position=lambda symbol: None,
        )
        system.config = {'strategy': {'default_risk_per_trade': 0.01}, 'trading': {'symbols': []}}
        system._retry_clear_stop_residues = lambda: None
        system._flush_pending_trade_notifications = lambda: None
        system.send_daily_position_summary_if_due = (
            lambda force=False, mark_sent=True: summary_calls.append((force, mark_sent)) or True
        )

        system.check_and_execute_trades(manual_run=True)

        self.assertEqual([(False, False)], summary_calls)
        self.assertIsNone(system._last_check_date)

    def test_daily_check_fetch_limit_tracks_large_ma_cross_config(self):
        system = self._build_system(notify_result=True)
        requested_limits = []
        system._trade_lock = SimpleNamespace(acquire=lambda blocking=False: True, release=lambda: None)
        system._last_check_date = None
        system._last_failure_notify_ts = 0
        system.equity_tracker = SimpleNamespace(
            record_daily_equity_snapshot=lambda: None,
            refresh_account_stats_state=lambda: None,
        )
        system.trade_state = SimpleNamespace(
            get_all_open_positions=lambda: {},
            get_open_position=lambda symbol: None,
        )
        system.config = {
            'strategy': {'default_risk_per_trade': 0.01, 'ma_long_period': 250, 'ma_stop_period': 400},
            'trading': {'symbols': [{'name': 'BTCUSDT', 'enabled': True, 'strategy': 'ma_cross'}]},
        }
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda symbol: symbol,
            fetch_ohlcv=lambda symbol, timeframe='1d', limit=100: requested_limits.append(limit) or [],
        )
        system.ma_cross_strategy = SimpleNamespace()
        system._retry_clear_stop_residues = lambda: None
        system._flush_pending_trade_notifications = lambda: None
        system.send_daily_position_summary_if_due = lambda force=False, mark_sent=True: True

        system.check_and_execute_trades()

        # required = max(ma_long*2, ma_stop+1) = max(500, 401) = 500；fetch_limit = max(365, 501) = 501
        self.assertEqual([501], requested_limits)

    def test_trade_open_notification_is_buffered_for_summary(self):
        system = self._build_system(notify_result=True)

        system._buffer_trade_open_notification('BTCUSDT', 'long', 101.2, 0.5, 95.0)

        self.assertEqual(0, system.notifier.trade_open_summary_calls)
        self.assertEqual(1, len(system._pending_trade_open_notifications))
        self.assertEqual('BTCUSDT', system._pending_trade_open_notifications[0]['symbol'])

    def test_trade_close_notification_is_buffered_for_summary(self):
        system = self._build_system(notify_result=True)

        system._buffer_trade_close_notification('ETHUSDT', 'short', 210.5, 18.6, 3.2)

        self.assertEqual(0, system.notifier.trade_close_summary_calls)
        self.assertEqual(1, len(system._pending_trade_close_notifications))
        self.assertEqual('ETHUSDT', system._pending_trade_close_notifications[0]['symbol'])

    def test_trade_open_and_close_summaries_are_sent_once(self):
        system = self._build_system(notify_result=True)
        system._pending_trade_open_notifications = [
            {'symbol': 'BTCUSDT', 'side': 'long', 'price': 101.2, 'size': 0.5, 'stop_loss_price': 95.0},
            {'symbol': 'ETHUSDT', 'side': 'short', 'price': 202.4, 'size': 1.2, 'stop_loss_price': 210.0},
        ]
        system._pending_trade_close_notifications = [
            {'symbol': 'SOLUSDT', 'side': 'long', 'exit_price': 99.1, 'pnl': 12.3, 'pnl_pct': 4.5},
            {'symbol': 'BNBUSDT', 'side': 'short', 'exit_price': 620.1, 'pnl': -8.7, 'pnl_pct': -1.4},
        ]

        system._flush_pending_trade_notifications()

        self.assertEqual(1, system.notifier.trade_open_summary_calls)
        self.assertEqual(1, system.notifier.trade_close_summary_calls)
        self.assertEqual(2, len(system.notifier.trade_open_summary_payload))
        self.assertEqual(2, len(system.notifier.trade_close_summary_payload))


if __name__ == '__main__':
    unittest.main()
