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
            get_signal_state=lambda symbol: False,
            set_signal_state=lambda symbol, value: None,
        )
        system.config = {'strategy': {'channel_period': 28}, 'trading': {'symbols': []}}
        system._retry_clear_stop_residues = lambda: None
        system._flush_pending_trade_notifications = lambda: None
        system.send_daily_position_summary_if_due = (
            lambda force=False, mark_sent=True: summary_calls.append((force, mark_sent)) or True
        )

        system.check_and_execute_trades(manual_run=True)

        self.assertEqual([(False, False)], summary_calls)
        self.assertIsNone(system._last_check_date)

    def test_daily_check_fetch_limit_tracks_large_turtle_config(self):
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
            get_signal_state=lambda symbol: False,
            set_signal_state=lambda symbol, value: None,
        )
        system.config = {
            'strategy': {'channel_period': 500},
            'trading': {'symbols': [{'name': 'BTCUSDT', 'enabled': True, 'strategy': 'turtle'}]},
        }
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda symbol: symbol,
            fetch_ohlcv=lambda symbol, timeframe='1d', limit=100: requested_limits.append(limit) or [],
        )
        system.turtle_strategy = SimpleNamespace()
        system.ma_cross_strategy = SimpleNamespace()
        system._retry_clear_stop_residues = lambda: None
        system._flush_pending_trade_notifications = lambda: None
        system.send_daily_position_summary_if_due = lambda force=False, mark_sent=True: True

        system.check_and_execute_trades()

        self.assertEqual([503], requested_limits)

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
        system.send_daily_position_summary_if_due = lambda force=False, mark_sent=True: False

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

    def test_flush_is_idempotent_after_send(self):
        """flush 发出后清空缓冲：重复调用不双发（成功路径发完，异常路径补发不会重复）。"""
        system = self._build_system(notify_result=True)
        system._pending_trade_close_notifications = [
            {'symbol': 'SOLUSDT', 'side': 'long', 'exit_price': 99.1, 'pnl': 12.3, 'pnl_pct': 4.5}]
        system._pending_stop_loss_updates = [
            {'symbol': 'BTCUSDT', 'old_stop_loss_price': 100, 'new_stop_loss_price': 98}]

        system._flush_pending_trade_notifications()
        system._flush_pending_trade_notifications()
        system._flush_pending_stop_loss_updates()
        system._flush_pending_stop_loss_updates()

        self.assertEqual(1, system.notifier.trade_close_summary_calls)
        self.assertEqual(1, system.notifier.stop_loss_update_summary_calls)
        self.assertEqual([], system._pending_trade_close_notifications)
        self.assertEqual([], system._pending_stop_loss_updates)

    def test_round_exception_still_delivers_buffered_trade_notifications(self):
        """整轮异常时已成交的开/平仓通知必须补发，不得随 finally 清缓冲静默丢失。
        模拟：成功路径 flush 首发即抛（钉钉抖动）→ 整轮 except 补发成功。"""
        system = self._build_system(notify_result=True)
        system._trade_lock = threading.Lock()
        system._last_check_date = None
        system._last_failure_notify_ts = 0
        system.equity_tracker = SimpleNamespace(
            record_daily_equity_snapshot=lambda: None,
            refresh_account_stats_state=lambda: None)
        system.trade_state = SimpleNamespace(
            get_all_open_positions=lambda: {},
            get_open_position=lambda symbol: None,
            get_signal_state=lambda symbol: False,
            set_signal_state=lambda symbol, value: None,
            get_stop_residues=lambda: {},
            compact_closed_trades=lambda: 0)
        system.config = {'trading': {'symbols': []},
                         'strategy': {'default_risk_per_trade': 0.01}}
        system.exchange_api = SimpleNamespace(fetch_ohlcv=lambda *a, **k: [])
        system.send_daily_position_summary_if_due = lambda force=False, mark_sent=True: False
        system._retry_clear_stop_residues = lambda: None

        # 让检查循环“产出”一笔平仓缓冲：品种检查阶段本身无事，直接预填后由 flush 抛错
        raised = {'n': 0}
        real_flush = system._flush_pending_trade_notifications

        def flush_raises_once():
            if raised['n'] == 0:
                raised['n'] += 1
                raise RuntimeError('钉钉网络抖动')
            return real_flush()

        system._flush_pending_trade_notifications = flush_raises_once

        def fill_buffer():
            system._pending_trade_close_notifications.append(
                {'symbol': 'SOLUSDT', 'side': 'long', 'exit_price': 99.1, 'pnl': 12.3, 'pnl_pct': 4.5})

        system.trade_state.compact_closed_trades = lambda: fill_buffer() or 0

        system.check_and_execute_trades()

        self.assertEqual(1, system.notifier.trade_close_summary_calls)   # 异常路径补发成功
        self.assertEqual('SOLUSDT', system.notifier.trade_close_summary_payload[0]['symbol'])

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
