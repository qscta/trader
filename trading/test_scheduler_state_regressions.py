"""调度/状态机审计回归（纯标准库）。"""

import json
import os
import tempfile
import threading
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _test_stubs

main = _test_stubs.import_main()
TradingSystem = main.TradingSystem
from trade_state import TradeState, TradeStatePersistenceError


class SignalExecutionStateTest(unittest.TestCase):
    def test_close_intent_survives_restart_and_is_consumed_with_full_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            state = TradeState(path)
            state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-1',
                strategy='ma_cross')
            first = state.prepare_close_intent(
                'BTCUSDT', 'CloseIntent123', '信号平仓')
            retry = TradeState(path).prepare_close_intent(
                'BTCUSDT', 'DifferentIgnored', 'API 重试')
            self.assertEqual(first['client_order_id'], retry['client_order_id'])

            reloaded = TradeState(path)
            with self.assertRaises(TradeStatePersistenceError):
                reloaded.close_position('BTCUSDT', 105.0)
            closed = reloaded.close_position(
                'BTCUSDT', 105.0,
                close_intent_client_id='CloseIntent123')

            self.assertIsNone(reloaded.get_open_position('BTCUSDT'))
            self.assertEqual(
                'CloseIntent123', closed['last_close_client_order_id'])
            self.assertIsNone(TradeState(path).get_close_intent('BTCUSDT'))

    def test_partial_close_atomically_consumes_close_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            state = TradeState(path)
            state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-1',
                strategy='ma_cross')
            state.prepare_close_intent(
                'BTCUSDT', 'ClosePartial123', '手动平仓')

            state.apply_partial_close(
                'BTCUSDT', 0.4, 105.0, remaining_size=0.6,
                new_stop_order_id='stop-2', stop_order_size=0.6,
                close_intent_client_id='ClosePartial123')

            reloaded = TradeState(path)
            position = reloaded.get_open_position('BTCUSDT')
            self.assertAlmostEqual(0.6, position['position_size'])
            self.assertIsNone(reloaded.get_close_intent('BTCUSDT'))
            self.assertEqual(
                'ClosePartial123', position['last_close_client_order_id'])



class OpenIntentStateTest(unittest.TestCase):
    def test_intent_and_planned_amount_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            state = TradeState(path)
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                {'side': 'long', 'entry_price': 100,
                 'stop_loss_price': 90}, planned_position_size=1.25)

            pending = TradeState(path).get_open_intent('BTCUSDT')

            self.assertEqual('IABC123', pending['client_order_id'])
            self.assertEqual(1.25, pending['planned_position_size'])

    def test_position_and_intent_clear_commit_in_one_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            state = TradeState(path)
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                {'side': 'long', 'entry_price': 100,
                 'stop_loss_price': 90})
            state.set_open_intent_amount('BTCUSDT', 'IABC123', 1.0)

            state.add_open_position(
                'BTCUSDT', 'long', 100, 1, 90, 'stop-1',
                strategy='ma_cross', open_intent_client_id='IABC123')

            reloaded = TradeState(path)
            self.assertIsNotNone(reloaded.get_open_position('BTCUSDT'))
            self.assertIsNone(reloaded.get_open_intent('BTCUSDT'))

    def test_failed_atomic_position_save_keeps_intent_and_no_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                {'side': 'long', 'entry_price': 100,
                 'stop_loss_price': 90})
            state.set_open_intent_amount('BTCUSDT', 'IABC123', 1.0)
            with patch('trade_state.atomic_write_json', return_value=False):
                with self.assertRaises(TradeStatePersistenceError):
                    state.add_open_position(
                        'BTCUSDT', 'long', 100, 1, 90, 'stop-1',
                        strategy='ma_cross',
                        open_intent_client_id='IABC123')

            self.assertIsNone(state.get_open_position('BTCUSDT'))
            self.assertIsNotNone(state.get_open_intent('BTCUSDT'))


class GenericOpenIntentIntegrationTest(unittest.TestCase):
    def _system(self, tmp, *, exchange_position=None):
        system = TradingSystem.__new__(TradingSystem)
        system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
        seen_client_ids = []

        def open_position(_symbol, _side, amount, client_order_id=None):
            seen_client_ids.append(client_order_id)
            return {
                'id': 'open-1', 'average': 100.0, 'amount': amount,
                'confirmed': True, 'fully_filled': True,
            }

        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda symbol: symbol,
            get_last_price=lambda _symbol: 100.0,
            get_balance=lambda: {'total': {'USDT': 1000.0}},
            round_quantity=lambda _symbol, amount: amount,
            get_quantity_precision=lambda _symbol: 3,
            open_position=open_position,
            create_stop_loss_order=lambda *args, **kwargs: {'id': 'stop-1'},
            get_position=lambda _symbol: exchange_position,
        )
        system.config = {
            'strategy': {'default_risk_per_trade': 0.01},
            'trading': {'symbols': [{
                'name': 'BTCUSDT', 'enabled': True,
                'strategy': 'ma_cross', 'risk_per_trade': 0.01,
            }]},
        }
        system.risk_manager = SimpleNamespace(
            account_equity=1000.0, risk_per_trade=0.01,
            calculate_position_size=lambda *_args: 1.0)
        system.notifier = SimpleNamespace(
            notify_error=Mock(), send_message=Mock())
        system._pending_trade_open_notifications = []
        system._pending_stop_loss_updates = []
        system._stop_anomalies = {}
        system.stop_loss_dates = {}
        return system, seen_client_ids

    def test_ma_open_persists_intent_before_post_and_clears_with_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(tmp)

            outcome = system._execute_open(
                'BTCUSDT', 'long', 100.0, 90.0,
                {'name': 'BTCUSDT', 'strategy': 'ma_cross',
                 'risk_per_trade': 0.01})

            self.assertEqual('opened', outcome['status'])
            self.assertEqual(1, len(seen_client_ids))
            self.assertTrue(seen_client_ids[0].startswith('I'))
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            self.assertIsNotNone(system.trade_state.get_open_position('BTCUSDT'))

    def test_orphan_position_resumes_same_intent_without_recalculating_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(
                tmp, exchange_position={'side': 'long', 'contracts': 1})
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IRECOVER123',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0})
            system.trade_state.set_open_intent_amount(
                'BTCUSDT', 'IRECOVER123', 1.0)
            system.risk_manager.calculate_position_size = Mock(
                side_effect=AssertionError('恢复 open intent 不得重算风险'))

            self.assertEqual(set(), system._reconcile_all_open_intents('test'))

            self.assertEqual(['IRECOVER123'], seen_client_ids)
            system.risk_manager.calculate_position_size.assert_not_called()
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            self.assertEqual(
                'long', system.trade_state.get_open_position('BTCUSDT')['side'])

    def test_pre_post_crash_intent_without_amount_is_consumed_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(tmp, exchange_position=None)
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IUNSUBMITTED123',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0})

            self.assertEqual(set(), system._reconcile_all_open_intents('test'))

            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            self.assertEqual([], seen_client_ids)

    def test_flat_unsubmitted_intent_is_consumed_when_symbol_retired(self):
        """删除/禁用发生在 intent 落盘后时，不得把恢复事务变成一笔新开仓。"""
        retired_configs = (
            [],
            [{'name': 'BTCUSDT', 'enabled': False,
              'strategy': 'ma_cross', 'risk_per_trade': 0.01}],
        )
        for symbols in retired_configs:
            with self.subTest(symbols=symbols), tempfile.TemporaryDirectory() as tmp:
                system, seen_client_ids = self._system(
                    tmp, exchange_position=None)
                system.exchange_api.find_existing_open_order = Mock(
                    return_value=None)
                system.trade_state.prepare_open_intent(
                    'BTCUSDT', 'ma_cross', 'long', 'IRETIRED123',
                    {'side': 'long', 'entry_price': 100.0,
                     'stop_loss_price': 90.0},
                    planned_position_size=1.0)
                system.config['trading']['symbols'] = symbols

                self.assertEqual(
                    set(), system._reconcile_all_open_intents('test'))

                self.assertEqual([], seen_client_ids)
                self.assertIsNone(
                    system.trade_state.get_open_intent('BTCUSDT'))
                self.assertIsNone(
                    system.trade_state.get_open_position('BTCUSDT'))

    def test_disabled_symbol_still_recovers_already_existing_exchange_position(self):
        """只平不开不能吞掉真钱孤儿仓：交易所有仓时仍须补账和止损。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(
                tmp, exchange_position={'side': 'long', 'contracts': 1})
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IRECOVERRETIRED',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=1.0)
            system.config['trading']['symbols'][0]['enabled'] = False

            self.assertEqual(
                set(), system._reconcile_all_open_intents('test'))

            self.assertEqual(['IRECOVERRETIRED'], seen_client_ids)
            self.assertEqual(
                'long', system.trade_state.get_open_position('BTCUSDT')['side'])

    def test_generic_executor_blocks_non_recovery_open_for_disabled_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(tmp)

            outcome = system._execute_open(
                'BTCUSDT', 'long', 100.0, 90.0,
                {'name': 'BTCUSDT', 'enabled': False,
                 'strategy': 'ma_cross', 'risk_per_trade': 0.01})

            self.assertEqual('retired_blocked', outcome['status'])
            self.assertEqual([], seen_client_ids)
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))

    def test_generic_full_rollback_immediately_books_real_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _seen_client_ids = self._system(tmp)
            system.exchange_api.open_position = Mock(return_value={
                'id': 'open-rollback', 'average': 100.0, 'amount': 1.0,
                'fee': {'cost': 0.1, 'currency': 'USDT'},
                'open_execution_compensated': True,
                'compensation': {
                    'id': 'close-rollback', 'fully_closed': True,
                    'average': 99.0, 'amount': 1.0,
                    'fee': {'cost': 0.2, 'currency': 'USDT'},
                },
            })

            outcome = system._execute_open(
                'BTCUSDT', 'long', 100.0, 90.0,
                {'name': 'BTCUSDT', 'strategy': 'ma_cross',
                 'risk_per_trade': 0.01})

            self.assertEqual('rolled_back', outcome['status'])
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            closed = system.trade_state.get_closed_trades()
            self.assertEqual(1, len(closed))
            self.assertEqual(['open-rollback'], closed[0]['entry_order_ids'])
            self.assertEqual(['close-rollback'], closed[0]['exit_order_ids'])
            self.assertEqual('actual', closed[0]['fee_source'])
            self.assertAlmostEqual(0.3, closed[0]['total_fee'])

    def test_position_save_failure_books_real_compensation_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _seen_client_ids = self._system(tmp)
            system.exchange_api.close_position = Mock(return_value={
                'id': 'close-after-save-fail', 'fully_closed': True,
                'average': 98.0, 'amount': 1.0, 'remaining_amount': 0.0,
                'fee': {'cost': 0.2, 'currency': 'USDT'},
            })
            system._cancel_stop_order_confirmed = Mock(return_value=True)
            system.trade_state.add_open_position = Mock(
                side_effect=TradeStatePersistenceError('single write failure'))

            outcome = system._execute_open(
                'BTCUSDT', 'long', 100.0, 90.0,
                {'name': 'BTCUSDT', 'strategy': 'ma_cross',
                 'risk_per_trade': 0.01})

            self.assertEqual('rolled_back', outcome['status'])
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            closed = system.trade_state.get_closed_trades()
            self.assertEqual(['close-after-save-fail'], closed[0]['exit_order_ids'])
            self.assertAlmostEqual(0.2, closed[0]['exit_fee'])

    def test_recovered_intent_rollback_is_not_finalized_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _seen_client_ids = self._system(
                tmp, exchange_position={'side': 'long', 'contracts': 1})
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IRECOVERROLLBACK',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=1.0)
            system.exchange_api.get_last_price = lambda _symbol: 80.0
            system.exchange_api.close_position = Mock(return_value={
                'id': 'close-recovered', 'fully_closed': True,
                'average': 79.0, 'amount': 1.0, 'remaining_amount': 0.0,
            })
            system.risk_manager.calculate_position_size = Mock(
                side_effect=AssertionError('恢复不得重算风险'))

            self.assertEqual(set(), system._reconcile_all_open_intents('test'))

            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            self.assertEqual(1, len(system.trade_state.get_closed_trades()))




class PositionReconciliationStateTest(unittest.TestCase):
    def _system(self, tmp):
        system = TradingSystem.__new__(TradingSystem)
        system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda symbol: symbol,
            _coin_to_contracts=lambda symbol, amount: amount * 10,
        )
        system.notifier = SimpleNamespace(notify_error=lambda *args, **kwargs: True)
        return system

    def test_direction_or_quantity_mismatch_is_persistently_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            local = {
                'symbol': 'BTCUSDT', 'side': 'long', 'position_size': 1,
                'entry_price': 100, 'stop_loss_price': 90,
            }
            self.assertFalse(system._verify_existing_position_or_quarantine(
                'BTCUSDT', local, {'side': 'short', 'contracts': 10}))
            self.assertTrue(TradeState(
                os.path.join(tmp, 'trade_state.json')).is_position_quarantined('BTCUSDT'))

            self.assertTrue(system._verify_existing_position_or_quarantine(
                'BTCUSDT', local, {'side': 'long', 'contracts': 10}))
            self.assertFalse(system.trade_state.is_position_quarantined('BTCUSDT'))

    def test_quarantine_disk_failure_still_blocks_in_current_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.notifier = SimpleNamespace(notify_error=Mock())
            system.trade_state.mark_position_quarantine = Mock(
                side_effect=TradeStatePersistenceError('disk full'))

            self.assertFalse(system._quarantine_position_mismatch(
                'BTCUSDT', '方向不一致'))

            self.assertTrue(system.trade_state.is_position_quarantined('BTCUSDT'))
            system.notifier.notify_error.assert_called_once()


class MaCatchupStateTest(unittest.TestCase):
    class _Series:
        def __init__(self, values):
            self.values = values

        def tolist(self):
            return list(self.values)

    class _ILoc:
        def __init__(self, frame):
            self.frame = frame

        def __getitem__(self, item):
            if isinstance(item, slice):
                return MaCatchupStateTest._Frame(self.frame.timestamps[item])
            raise TypeError(item)

    class _Frame:
        def __init__(self, timestamps):
            self.timestamps = list(timestamps)
            self.iloc = MaCatchupStateTest._ILoc(self)

        def __len__(self):
            return len(self.timestamps)

        def __getitem__(self, key):
            if key == 'timestamp':
                return MaCatchupStateTest._Series(self.timestamps)
            raise KeyError(key)

    class _Strategy:
        long_period = 2
        stop_loss_period = 2

        def check_current_state(self, _df):
            return {
                'action': 'long', 'ema_short': 2, 'ema_long': 1,
                'upper_stop': 12, 'lower_stop': 8, 'current_close': 11,
            }

        def check_signal(self, df):
            action = {4: 'short', 5: 'long'}.get(len(df))
            return {'action': action} if action else {'action': None}

    class _OldCrossOnlyStrategy(_Strategy):
        def check_signal(self, df):
            return {'action': 'short' if len(df) == 4 else None}

    def test_only_checks_latest_cross_instead_of_replaying_missing_bars(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.mark_candle_processed('BTCUSDT', 'ma_cross', 't3')
            frame = self._Frame(['t1', 't2', 't3', 't4', 't5'])

            signal, candle_id, missed = system._ma_signal_with_catchup(
                'BTCUSDT', self._Strategy(), frame)
            self.assertEqual(1, missed)
            self.assertEqual('long', signal['action'])
            self.assertEqual('t5', candle_id)

            system.trade_state.mark_candle_processed('BTCUSDT', 'ma_cross', 't5')
            signal, _candle_id, missed = system._ma_signal_with_catchup(
                'BTCUSDT', self._Strategy(), frame)
            self.assertEqual(0, missed)
            self.assertIsNone(signal['action'])

    def test_large_history_gap_ignores_old_bars_but_keeps_latest_cross(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.mark_candle_processed('BTCUSDT', 'ma_cross', 't1')
            frame = self._Frame(['t1', 't2', 't3', 't4', 't5'])

            signal, candle_id, missed = system._ma_signal_with_catchup(
                'BTCUSDT', self._Strategy(), frame)

            self.assertEqual('t5', candle_id)
            self.assertEqual(1, missed)
            self.assertEqual('long', signal['action'])
            self.assertTrue(signal['_history_discontinuity'])
            self.assertEqual(4, signal['_history_gap_candles'])

    def test_large_history_gap_does_not_replay_an_old_cross(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.mark_candle_processed('BTCUSDT', 'ma_cross', 't1')
            frame = self._Frame(['t1', 't2', 't3', 't4', 't5'])

            signal, candle_id, count = system._ma_signal_with_catchup(
                'BTCUSDT', self._OldCrossOnlyStrategy(), frame)

            self.assertEqual('t5', candle_id)
            self.assertEqual(0, count)
            self.assertIsNone(signal['action'])
            self.assertTrue(signal['_history_discontinuity'])

    def test_invisible_previous_marker_still_checks_latest_cross(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.mark_candle_processed(
                'BTCUSDT', 'ma_cross', 'outside-visible-window')
            frame = self._Frame(['t1', 't2', 't3', 't4', 't5'])

            signal, candle_id, missed = system._ma_signal_with_catchup(
                'BTCUSDT', self._Strategy(), frame)

            self.assertEqual('t5', candle_id)
            self.assertEqual(1, missed)
            self.assertEqual('long', signal['action'])
            self.assertTrue(signal['_history_discontinuity'])

    def test_missing_marker_checks_latest_without_replaying_visible_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            frame = self._Frame(['t1', 't2', 't3', 't4', 't5'])

            signal, candle_id, missed = system._ma_signal_with_catchup(
                'BTCUSDT', self._Strategy(), frame)

            self.assertEqual('t5', candle_id)
            self.assertEqual(1, missed)
            self.assertEqual('long', signal['action'])
            self.assertTrue(signal['_history_discontinuity'])

    def test_missing_marker_requires_rebaseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            frame = self._Frame(['t1', 't2', 't3', 't4', 't5'])

            rebaseline, previous, current, gap = (
                system._history_requires_rebaseline('BTCUSDT', 'ma_cross', frame))

            self.assertTrue(rebaseline)
            self.assertIsNone(previous)
            self.assertEqual('t5', current)
            self.assertIsNone(gap)

    def test_large_gap_requires_rebaseline_but_short_gap_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            frame = self._Frame(['t1', 't2', 't3', 't4', 't5'])
            system.trade_state.mark_candle_processed('BTCUSDT', 'ma_cross', 't1')

            rebaseline, _, _, gap = system._history_requires_rebaseline(
                'BTCUSDT', 'ma_cross', frame)
            self.assertTrue(rebaseline)
            self.assertEqual(4, gap)

            system.trade_state.mark_candle_processed('BTCUSDT', 'ma_cross', 't2')
            rebaseline, _, _, gap = system._history_requires_rebaseline(
                'BTCUSDT', 'ma_cross', frame)
            self.assertFalse(rebaseline)
            self.assertEqual(3, gap)

    def test_sparse_rows_with_large_calendar_gap_require_rebaseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.mark_candle_processed(
                'BTCUSDT', 'ma_cross', '2026-05-05T00:00:00')
            frame = self._Frame([
                '2026-05-05T00:00:00',
                '2026-07-09T00:00:00',
                '2026-07-10T00:00:00',
            ])

            rebaseline, _, _, gap = system._history_requires_rebaseline(
                'BTCUSDT', 'ma_cross', frame)

            self.assertTrue(rebaseline)
            self.assertEqual(66, gap)


class DailyCandleFreshnessTest(unittest.TestCase):
    class _TimestampSeries:
        def __init__(self, values):
            self._values = values

        def tolist(self):
            return list(self._values)

    class _Frame:
        def __init__(self, values):
            self._timestamps = DailyCandleFreshnessTest._TimestampSeries(values)

        def __getitem__(self, key):
            if key == 'timestamp':
                return self._timestamps
            raise KeyError(key)

    def test_expected_previous_day_is_fresh(self):
        frame = self._Frame([datetime(2026, 7, 10)])
        fresh, latest, minimum = TradingSystem._daily_candle_is_fresh(
            frame, '2026-07-11')
        self.assertTrue(fresh)
        self.assertEqual(date(2026, 7, 10), latest)
        self.assertEqual(date(2026, 7, 9), minimum)

    def test_one_extra_missing_day_is_tolerated_for_market_holidays(self):
        frame = self._Frame([datetime(2026, 7, 9)])
        fresh, _, _ = TradingSystem._daily_candle_is_fresh(
            frame, '2026-07-11')
        self.assertTrue(fresh)

    def test_multiweek_stale_candle_is_rejected(self):
        frame = self._Frame([datetime(2026, 5, 5)])
        fresh, latest, minimum = TradingSystem._daily_candle_is_fresh(
            frame, '2026-07-11')
        self.assertFalse(fresh)
        self.assertEqual(date(2026, 5, 5), latest)
        self.assertEqual(date(2026, 7, 9), minimum)

    def test_unparseable_timestamp_fails_closed(self):
        frame = self._Frame(['not-a-timestamp'])
        fresh, latest, minimum = TradingSystem._daily_candle_is_fresh(
            frame, '2026-07-11')
        self.assertFalse(fresh)
        self.assertIsNone(latest)
        self.assertIsNone(minimum)


class IndicatorPriceFormattingTest(unittest.TestCase):
    def test_preserves_meaningful_digits_for_each_price_scale(self):
        fmt = TradingSystem._format_indicator_price
        self.assertEqual('4113.60', fmt(4113.6))
        self.assertEqual('7.9100', fmt(7.91))
        self.assertEqual('0.169064', fmt(0.169063860497983))
        self.assertEqual('0.00147774', fmt(0.00147774092545))
        self.assertEqual('0.0000051234', fmt(0.0000051234))


class MaMarkerIntegrationTest(unittest.TestCase):
    class _CloseILoc:
        def __getitem__(self, index):
            if index == -1:
                return 11.0
            raise IndexError(index)

    class _CloseSeries:
        iloc = None

        def __init__(self):
            self.iloc = MaMarkerIntegrationTest._CloseILoc()

    class _Frame:
        def __len__(self):
            return 5

        def __getitem__(self, key):
            if key == 'close':
                return MaMarkerIntegrationTest._CloseSeries()
            raise KeyError(key)

    def _system(self, tmp, *, held_side=None):
        system = TradingSystem.__new__(TradingSystem)
        system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
        if held_side:
            system.trade_state.add_open_position(
                'BTCUSDT', held_side, 10.0, 1.0,
                8.0 if held_side == 'long' else 12.0,
                'stop-old', strategy='ma_cross')
        system.trade_state.mark_candle_processed('BTCUSDT', 'ma_cross', 't3')
        system.config = {
            'strategy': {
                'default_risk_per_trade': 0.01,
                'ma_short_period': 2, 'ma_long_period': 2, 'ma_stop_period': 2,
            },
            'trading': {'symbols': [{
                'name': 'BTCUSDT', 'enabled': True,
                'strategy': 'ma_cross', 'risk_per_trade': 0.01,
            }]},
        }
        frame = self._Frame()
        exchange_position = (
            {'side': held_side, 'contracts': 1.0} if held_side else None)
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda symbol: symbol,
            get_position=Mock(return_value=exchange_position),
            _coin_to_contracts=lambda _symbol, amount: amount,
            find_stop_order_state=lambda *args, **kwargs: 'intact',
            fetch_ohlcv=lambda *args, **kwargs: [[1]],
            ohlcv_to_dataframe=lambda _rows: frame,
            filter_closed_candles=lambda df, timeframe='1d': df,
        )
        system.get_strategy_for_symbol = (
            lambda _cfg: (SimpleNamespace(), 'ma_cross'))
        system._trade_lock = threading.Lock()
        system._last_check_date = None
        system._last_failure_notify_ts = 0
        system._pending_trade_open_notifications = []
        system._pending_trade_close_notifications = []
        system._pending_stop_loss_updates = []
        system._stop_anomalies = {}
        system.stop_loss_dates = {}
        system.equity_tracker = SimpleNamespace(
            record_daily_equity_snapshot=lambda: None,
            refresh_account_stats_state=lambda: None)
        system.notifier = SimpleNamespace(
            notify_error=Mock(), notify_signal_missed=Mock(),
            notify_stop_loss_updates_summary=Mock(), send_message=Mock())
        system._retry_clear_stop_residues = lambda: None
        system._flush_pending_trade_notifications = lambda: None
        system.send_daily_position_summary_if_due = lambda **kwargs: True
        system._closed_candle_id = lambda _df: 't5'
        system._daily_candle_is_fresh = (
            lambda _df, _scheduled_date: (True, date(2026, 7, 10), date(2026, 7, 9)))
        return system

    @staticmethod
    def _signal(action='long', target='long'):
        return {
            'action': action, 'target_side': target,
            'ema_short': 2.0, 'ema_long': 1.0,
            'upper_stop': 12.0, 'lower_stop': 8.0,
            'current_close': 11.0,
        }

    def test_failed_ma_open_does_not_advance_candle_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system._ma_signal_with_catchup = (
                lambda *args, **kwargs: (self._signal(), 't5', 1))
            system._execute_open = Mock(return_value=None)

            system.check_and_execute_trades()

            metadata = system.trade_state.get_signal_metadata('BTCUSDT')
            self.assertEqual('t3', metadata['last_processed_candle'])
            self.assertIsNone(system._last_check_date)
            system.notifier.notify_signal_missed.assert_called_once()

    def test_opposite_held_position_does_not_flip_without_latest_cross(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp, held_side='short')
            system.trade_state.mark_candle_processed('BTCUSDT', 'ma_cross', 't5')
            system._ma_signal_with_catchup = (
                lambda *args, **kwargs: (self._signal(action=None), 't5', 0))
            seen_actions = []

            def observe(symbol, signal, position, _config, _df):
                seen_actions.append(signal.get('action'))

            system.handle_open_position_ma_cross = observe

            system.check_and_execute_trades()

            self.assertEqual([None], seen_actions)
            self.assertEqual('short',
                             system.trade_state.get_open_position('BTCUSDT')['side'])


class CrossMidnightScheduleTest(unittest.TestCase):
    def _system(self, check_hour=23, check_minute=59):
        system = TradingSystem.__new__(TradingSystem)
        system.config = {'scheduler': {
            'check_hour': check_hour, 'check_minute': check_minute}}
        system.label = '欧易'
        system._last_check_date = None
        calls = []
        system.check_and_execute_trades = (
            lambda **kwargs: calls.append(kwargs.get('scheduled_date')))
        return system, calls

    def test_2359_buffer_and_catchup_keep_previous_schedule_date(self):
        system, calls = self._system()
        system._run_startup_catchup_check(now=datetime(2026, 7, 11, 0, 0))
        self.assertEqual([], calls)
        system._run_startup_catchup_check(now=datetime(2026, 7, 11, 0, 2))
        self.assertEqual(['2026-07-10'], calls)

    def test_cross_midnight_retry_uses_previous_schedule_date(self):
        system, calls = self._system()
        system._run_daily_check_retry(now=datetime(2026, 7, 11, 0, 0))
        self.assertEqual(['2026-07-10'], calls)

    def test_cross_midnight_summary_retry_uses_previous_schedule_date(self):
        system, _calls = self._system()
        system.config['scheduler'].update({'summary_hour': 23, 'summary_minute': 59})
        summary_dates = []
        system.send_daily_position_summary_if_due = (
            lambda **kwargs: summary_dates.append(kwargs.get('summary_date')))
        system._run_daily_summary_retry(now=datetime(2026, 7, 11, 0, 0))
        self.assertEqual(['2026-07-10'], summary_dates)

    def test_registers_2359_retries_at_next_day_midnight(self):
        system, _calls = self._system()
        system.exchange_id = 'okx'
        jobs = {}
        system.scheduler = SimpleNamespace(
            add_job=lambda func, trigger, **kwargs: jobs.__setitem__(kwargs['id'], kwargs))
        system._record_equity_tick_with_alert = lambda: None
        system.config['scheduler'].update({'summary_hour': 23, 'summary_minute': 59})
        system.register_jobs(system.config['scheduler'])
        self.assertEqual(0, jobs['okx_daily_check_retry']['hour'])
        self.assertEqual(0, jobs['okx_daily_check_retry']['minute'])
        self.assertEqual(0, jobs['okx_daily_summary_retry']['hour'])
        self.assertEqual(0, jobs['okx_daily_summary_retry']['minute'])

    def test_registers_0859_retry_at_0900(self):
        system, _calls = self._system(check_hour=8, check_minute=59)
        system.exchange_id = 'okx'
        jobs = {}
        system.scheduler = SimpleNamespace(
            add_job=lambda func, trigger, **kwargs: jobs.__setitem__(kwargs['id'], kwargs))
        system._record_equity_tick_with_alert = lambda: None
        system.register_jobs(system.config['scheduler'])
        self.assertEqual(9, jobs['okx_daily_check_retry']['hour'])
        self.assertEqual(0, jobs['okx_daily_check_retry']['minute'])


class MigrationAndT1FailClosedTest(unittest.TestCase):
    @staticmethod
    def _write_state(path, positions):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({'open_positions': positions, 'closed_trades': []}, handle)

    def test_migration_atomic_failure_keeps_original_root_and_refuses_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, 'trade_state.json')
            legacy = os.path.join(tmp, 'data', 'okx', 'trade_state.json')
            self._write_state(root, {})
            position = {
                'BTCUSDT': {
                    'symbol': 'BTCUSDT', 'side': 'long', 'entry_price': 100,
                    'position_size': 1, 'stop_loss_price': 90,
                }}
            self._write_state(legacy, position)
            system = TradingSystem.__new__(TradingSystem)
            system.base_dir = tmp
            with patch.object(main, 'atomic_write_json', return_value=False):
                with self.assertRaises(RuntimeError):
                    system._migrate_okx_legacy_state()
            with open(root, encoding='utf-8') as handle:
                self.assertEqual({}, json.load(handle)['open_positions'])

    def test_corrupt_legacy_t1_refuses_empty_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.claim_owner_exchange('okx')
            t1_path = os.path.join(tmp, 'stop_loss_dates.json')
            with open(t1_path, 'w', encoding='utf-8') as handle:
                handle.write('[]')
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = state
            system.stop_loss_file = t1_path
            with self.assertRaises(TradeStatePersistenceError):
                system._load_stop_loss_dates()

    def test_broken_symlink_legacy_t1_refuses_empty_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.claim_owner_exchange('okx')
            t1_path = os.path.join(tmp, 'stop_loss_dates.json')
            os.symlink(os.path.join(tmp, 'missing-target.json'), t1_path)
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = state
            system.stop_loss_file = t1_path

            with self.assertRaises(TradeStatePersistenceError):
                system._load_stop_loss_dates()
            self.assertFalse(state.stop_loss_dates_migrated())


class ConfigSecretPersistenceTest(unittest.TestCase):
    def test_environment_okx_credentials_never_spill_to_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'okx': {'sandbox': True},
                    'strategy': {'default_risk_per_trade': 0.01},
                    'trading': {'symbols': []},
                }, handle)
            system = TradingSystem.__new__(TradingSystem)
            with patch.dict(os.environ, {
                    'OKX_API_KEY': 'env-key',
                    'OKX_API_SECRET': 'env-secret',
                    'OKX_API_PASSPHRASE': 'env-pass',
            }, clear=False):
                system.config = system.load_config(path)
            self.assertEqual('env-key', system.config['okx']['apiKey'])
            system.config_file = path
            system._config_lock = threading.RLock()
            self.assertTrue(system.persist_config())
            with open(path, encoding='utf-8') as handle:
                persisted = json.load(handle)
            self.assertNotIn('apiKey', persisted['okx'])
            self.assertNotIn('secret', persisted['okx'])
            self.assertNotIn('password', persisted['okx'])
            self.assertEqual(0o600, os.stat(path).st_mode & 0o777)

    def test_config_symlink_is_rejected_before_credentials_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, 'real-config.json')
            with open(target, 'w', encoding='utf-8') as handle:
                json.dump({
                    'okx': {
                        'apiKey': 'disk-key', 'secret': 'disk-secret',
                        'password': 'disk-pass',
                    },
                    'strategy': {
                        'default_risk_per_trade': 0.01,
                    },
                    'trading': {'symbols': []},
                }, handle)
            link = os.path.join(tmp, 'config.json')
            os.symlink(target, link)
            system = TradingSystem.__new__(TradingSystem)

            with self.assertRaises(ValueError):
                system.load_config(link)


if __name__ == '__main__':
    unittest.main()
