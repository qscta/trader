"""海龟止损重入资格的跨日存活与两条发现路径的一致性（纯标准库，可本机运行）。

修复的缺陷：
1. 日检路径止损后按「价格仍在原方向一侧」置回的允许开仓状态（True），会在次日
   日检开头被历史回溯（is_first_breakout_armed 只看 K 线，历史里那次突破已消耗）
   无条件清洗成 False——重入资格只活一天。现在该状态带 sticky 标记，回溯豁免。
2. 盘中巡检发现的止损直接把资格硬重置为 False，永远拿不到重入资格——与日检
   路径对同一次止损给出两套语义。现在盘中记「重入待裁决」标记，次日日检按
   同一条中轨规则统一裁决。
"""
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import _test_stubs

TradingSystem = _test_stubs.import_main().TradingSystem
from trade_state import TradeState


def _system_with_real_state(tmp):
    system = TradingSystem.__new__(TradingSystem)
    system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
    system.config = {'trading': {'symbols': []},
                     'strategy': {'default_risk_per_trade': 0.01}}
    system._stop_anomalies = {}
    system.notifier = SimpleNamespace(
        send_message=Mock(return_value=True),
        notify_error=Mock(return_value=True),
        notify_stop_loss_triggered=Mock(return_value=True),
        notify_stop_loss_updates_summary=Mock(return_value=True),
        notify_signal_missed=Mock(return_value=True))
    system.turtle_strategy = SimpleNamespace(
        check_signal=Mock(return_value=None),
        is_first_breakout_armed=Mock(return_value=False))
    system.ma_cross_strategy = SimpleNamespace()
    return system


class StickySignalStateTest(unittest.TestCase):
    def test_sticky_roundtrip_and_cleared_by_open(self):
        """sticky 标记随状态持久化；开仓成功（add_open_position）后随条目重写解除。"""
        with tempfile.TemporaryDirectory() as tmp:
            ts = TradeState(os.path.join(tmp, 'trade_state.json'))
            ts.set_signal_state('BTCUSDT', True, sticky=True)
            self.assertTrue(ts.get_signal_state('BTCUSDT'))
            self.assertTrue(ts.is_signal_rearm_sticky('BTCUSDT'))
            # 重启后仍在（持久化）
            ts2 = TradeState(os.path.join(tmp, 'trade_state.json'))
            self.assertTrue(ts2.is_signal_rearm_sticky('BTCUSDT'))
            # 开仓后条目被重写为 False，sticky 一并解除
            ts2.add_open_position('BTCUSDT', 'long', 100.0, 1.0, 90.0, strategy='turtle')
            self.assertFalse(ts2.get_signal_state('BTCUSDT'))
            self.assertFalse(ts2.is_signal_rearm_sticky('BTCUSDT'))

    def test_plain_true_has_no_sticky(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = TradeState(os.path.join(tmp, 'trade_state.json'))
            ts.set_signal_state('BTCUSDT', True)
            self.assertFalse(ts.is_signal_rearm_sticky('BTCUSDT'))

    def test_rearm_pending_marker_roundtrip_and_cleared_by_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = TradeState(os.path.join(tmp, 'trade_state.json'))
            ts.mark_turtle_rearm_pending('ETHUSDT', 'short')
            entry = ts.pop_turtle_rearm_pending('ETHUSDT')
            self.assertEqual(entry['side'], 'short')
            self.assertIsNone(ts.pop_turtle_rearm_pending('ETHUSDT'))  # 已取走
            # 持仓形成后遗留标记作废
            ts.mark_turtle_rearm_pending('ETHUSDT', 'short')
            ts.add_open_position('ETHUSDT', 'long', 100.0, 1.0, 90.0, strategy='turtle')
            self.assertIsNone(ts.pop_turtle_rearm_pending('ETHUSDT'))


class BackfillRespectsStickyTest(unittest.TestCase):
    """日检开头的历史回溯：sticky True 不清洗；普通 True 照常按历史重置。"""

    def _build_system(self, tmp, armed_from_history):
        system = _system_with_real_state(tmp)
        system.config['trading']['symbols'] = [
            {'name': 'BTCUSDT', 'enabled': True, 'strategy': 'turtle'}]
        system._trade_lock = threading.Lock()
        system._last_check_date = None
        system._last_failure_notify_ts = 0
        system._pending_trade_open_notifications = []
        system._pending_trade_close_notifications = []
        system._pending_stop_loss_updates = []
        system.equity_tracker = SimpleNamespace(
            record_daily_equity_snapshot=lambda: None,
            refresh_account_stats_state=lambda: None)
        system.send_daily_position_summary_if_due = lambda force=False, mark_sent=True: False
        system.turtle_strategy = SimpleNamespace(
            is_first_breakout_armed=lambda df, include_latest_bar=True: armed_from_history,
            check_signal=lambda df, mid_line_crossed=False: None)  # 无信号，仅测回填
        system.ma_cross_strategy = SimpleNamespace()
        # K线管线：40 根已收盘（≥ 默认海龟需求 30），回填分支可达
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda s: s,
            fetch_ohlcv=lambda *a, **k: list(range(40)),
            ohlcv_to_dataframe=lambda ohlcv: ohlcv,
            filter_closed_candles=lambda df, timeframe='1d': df)
        return system

    def test_sticky_true_survives_backfill_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._build_system(tmp, armed_from_history=False)
            system.trade_state.set_signal_state('BTCUSDT', True, sticky=True)

            system.check_and_execute_trades()

            self.assertTrue(system.trade_state.get_signal_state('BTCUSDT'))
            self.assertTrue(system.trade_state.is_signal_rearm_sticky('BTCUSDT'))

    def test_plain_true_still_reset_by_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._build_system(tmp, armed_from_history=False)
            system.trade_state.set_signal_state('BTCUSDT', True)

            system.check_and_execute_trades()

            self.assertFalse(system.trade_state.get_signal_state('BTCUSDT'))

    def test_backfill_can_still_arm_from_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._build_system(tmp, armed_from_history=True)
            system.trade_state.set_signal_state('BTCUSDT', False)

            system.check_and_execute_trades()

            self.assertTrue(system.trade_state.get_signal_state('BTCUSDT'))


class IntradayStopRecordsRearmPendingTest(unittest.TestCase):
    def test_intraday_flat_close_records_pending_marker(self):
        """盘中巡检发现交易所已无仓：记平 + 记「重入待裁决」，不再硬重置资格。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = _system_with_real_state(tmp)
            system.trade_state.add_open_position(
                'BTCUSDT', 'short', 100.0, 1.0, 110.0, strategy='turtle')
            system.exchange_api = SimpleNamespace(
                to_ccxt_symbol=lambda s: s,
                get_position=lambda s: None,          # 交易所端已无仓（止损已触发）
                cancel_order=lambda s, oid: True,
                cancel_all_orders=lambda s: True)

            system._reconcile_symbol_intraday(
                'BTCUSDT', system.trade_state.get_open_position('BTCUSDT') or
                {'side': 'short', 'stop_loss_price': 110.0, 'entry_price': 100.0,
                 'position_size': 1.0, 'strategy': 'turtle'},
                {})

            self.assertIsNone(system.trade_state.get_open_position('BTCUSDT'))
            entry = system.trade_state.pop_turtle_rearm_pending('BTCUSDT')
            self.assertIsNotNone(entry)
            self.assertEqual(entry['side'], 'short')


class RearmPendingResolutionTest(unittest.TestCase):
    """次日日检对「重入待裁决」的裁决——与日检路径亲自发现止损时的规则同一条。"""

    def _system(self, tmp):
        system = _system_with_real_state(tmp)
        system.handle_open_signal_turtle = Mock()
        return system

    def test_price_still_on_original_side_rearms_sticky(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.trade_state.mark_turtle_rearm_pending('BTCUSDT', 'short')
            signal = {'action': None, 'current_close': 40.0, 'mid_line': 55.0}

            system.handle_no_position_turtle('BTCUSDT', signal, {'name': 'BTCUSDT'}, df=object())

            self.assertTrue(system.trade_state.get_signal_state('BTCUSDT'))
            self.assertTrue(system.trade_state.is_signal_rearm_sticky('BTCUSDT'))
            self.assertIsNone(system.trade_state.pop_turtle_rearm_pending('BTCUSDT'))

    def test_price_crossed_to_other_side_waits_for_new_cross(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.trade_state.mark_turtle_rearm_pending('BTCUSDT', 'short')
            signal = {'action': None, 'current_close': 70.0, 'mid_line': 55.0}

            system.handle_no_position_turtle('BTCUSDT', signal, {'name': 'BTCUSDT'}, df=object())

            self.assertFalse(system.trade_state.get_signal_state('BTCUSDT'))
            system.handle_open_signal_turtle.assert_not_called()

    def test_same_day_valid_mid_cross_supersedes_marker(self):
        """当日本就有效穿越中轨：常规资格链已激活，标记作废且不得反向覆盖新资格。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.trade_state.set_signal_state('BTCUSDT', True)  # 主循环按新穿越已置位
            system.trade_state.mark_turtle_rearm_pending('BTCUSDT', 'short')
            signal = {'action': None, 'mid_line_crossed': True,
                      'current_close': 70.0, 'mid_line': 55.0}

            system.handle_no_position_turtle('BTCUSDT', signal, {'name': 'BTCUSDT'}, df=object())

            self.assertTrue(system.trade_state.get_signal_state('BTCUSDT'))
            self.assertIsNone(system.trade_state.pop_turtle_rearm_pending('BTCUSDT'))

    def test_rearm_refreshes_signal_and_opens_on_same_day_breakout(self):
        """裁决置回 True 后须用新资格重算当日信号：止损当日 V 型反转再突破不得漏单。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            refreshed = {'action': 'long', 'current_close': 70.0, 'mid_line': 55.0,
                         'upper_line': 68.0, 'lower_line': 42.0}
            system.turtle_strategy.check_signal = Mock(return_value=refreshed)
            system.trade_state.mark_turtle_rearm_pending('BTCUSDT', 'long')
            stale_signal = {'action': None, 'current_close': 70.0, 'mid_line': 55.0}

            system.handle_no_position_turtle('BTCUSDT', stale_signal, {'name': 'BTCUSDT'}, df='DF')

            system.turtle_strategy.check_signal.assert_called_once_with('DF', mid_line_crossed=True)
            system.handle_open_signal_turtle.assert_called_once_with(
                'BTCUSDT', 'long', refreshed, {'name': 'BTCUSDT'})


if __name__ == '__main__':
    unittest.main()
