import threading
import unittest
from datetime import date
from types import SimpleNamespace

import _test_stubs

TradingSystem = _test_stubs.import_main().TradingSystem


class _FakeNotifier:
    def __init__(self, result=True):
        self.result = result
        self.calls = 0
        self.send_message_calls = 0
        self.stop_loss_update_summary_calls = 0
        self.stop_loss_update_summary_payload = None
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

    def notify_stop_loss_updates_summary(self, updates):
        self.stop_loss_update_summary_calls += 1
        self.stop_loss_update_summary_payload = updates
        return True

    def notify_trade_opened_summary(self, trades):
        self.trade_open_summary_calls += 1
        self.trade_open_summary_payload = trades
        return True

    def notify_trade_closed_summary(self, trades):
        self.trade_close_summary_calls += 1
        self.trade_close_summary_payload = trades
        return True


class _FakeExchangeApi:
    def __init__(self):
        self.cancel_calls = 0
        self.stop_order_calls = 0

    def to_ccxt_symbol(self, symbol):
        return symbol[:-4] + '/USDT' if symbol.endswith('USDT') else symbol

    def cancel_order(self, symbol, order_id):
        self.cancel_calls += 1
        return True

    def cancel_all_orders(self, symbol):
        return True

    def create_stop_loss_order(self, symbol, side, amount, stop_price):
        self.stop_order_calls += 1
        return {'id': 'stop-123'}


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
        system._pending_stop_loss_updates = []
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

    def test_stop_loss_update_is_buffered_for_summary_notification(self):
        system = TradingSystem.__new__(TradingSystem)
        system.exchange_api = _FakeExchangeApi()
        system.notifier = _FakeNotifier(result=True)
        system.trade_state = SimpleNamespace(
            clear_stop_residue=lambda symbol: None,
            mark_stop_residue=lambda symbol: None,
        )
        system._pending_stop_loss_updates = []
        system._update_trade_state_stop_with_runtime_fallback = (
            lambda symbol, stop_price, order_id, context: ({'symbol': symbol}, True)
        )

        position = {
            'side': 'long',
            'position_size': 1.5,
            'stop_loss_price': 100.0,
            'stop_order_id': 'old-stop',
        }

        system._update_stop_order('BTCUSDT', position, 98.0)

        self.assertEqual(0, system.notifier.stop_loss_update_summary_calls)
        self.assertEqual(1, len(system._pending_stop_loss_updates))
        self.assertEqual('BTCUSDT', system._pending_stop_loss_updates[0]['symbol'])
        self.assertEqual(1, system.exchange_api.cancel_calls)
        self.assertEqual(1, system.exchange_api.stop_order_calls)

    def test_stop_loss_updates_are_sent_as_single_summary_message(self):
        system = self._build_system(notify_result=True)
        system._pending_stop_loss_updates = [
            {'symbol': 'BTCUSDT', 'old_stop_loss_price': 100, 'new_stop_loss_price': 98},
            {'symbol': 'ETHUSDT', 'old_stop_loss_price': 200, 'new_stop_loss_price': 190},
        ]
        system._last_check_date = None
        system._trade_lock = SimpleNamespace(acquire=lambda blocking=False: True, release=lambda: None)
        system._last_failure_notify_ts = 0
        system._last_summary_date = date.today().isoformat()
        system.trade_state = SimpleNamespace(
            get_all_open_positions=lambda: {},
            get_open_position=lambda symbol: None,
            get_signal_state=lambda symbol: False,
            set_signal_state=lambda symbol, value: None,
        )
        system.config = {'trading': {'symbols': []}}
        system.exchange_api = SimpleNamespace(fetch_ohlcv=lambda *args, **kwargs: [], get_balance=lambda: {'total': {'USDT': 1000}})
        system.send_daily_position_summary_if_due = lambda force=False: False

        system.notifier.notify_stop_loss_updates_summary(system._pending_stop_loss_updates)

        self.assertEqual(1, system.notifier.stop_loss_update_summary_calls)
        self.assertEqual(2, len(system.notifier.stop_loss_update_summary_payload))

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
