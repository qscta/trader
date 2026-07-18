"""删除交易对后的持仓托管 + 状态归属护栏 单测（纯标准库，可本机运行）。

覆盖 Codex 审查要求：
- 有本地持仓的交易对从配置品种池删除后，check_and_execute_trades 仍会检查该 symbol。
- trade_state.json 的交易所归属护栏：归属不符或来路不明(有持仓无标记)时拒绝启动。
"""
import json
import os
import threading
import tempfile
import time
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _test_stubs

TradingSystem = _test_stubs.import_main().TradingSystem
import trade_state as trade_state_module  # noqa: E402
from trade_state import TradeState, TradeStatePersistenceError  # noqa: E402  真实现（纯标准库）


def _build_system(tmpdir, config_symbols):
    """组装一个只够跑 check_and_execute_trades 检查循环的最小系统。"""
    system = TradingSystem.__new__(TradingSystem)
    system.trade_state = TradeState(os.path.join(tmpdir, 'trade_state.json'))
    system.config = {'trading': {'symbols': config_symbols},
                     'strategy': {'default_risk_per_trade': 0.01}}
    system._trade_lock = threading.Lock()
    system._stop_anomalies = {}
    system._last_check_date = None
    system._last_failure_notify_ts = 0
    system._last_trade_check_failure = None
    system._last_successful_trade_check_ts = None
    system._last_guardian_failure = None
    system._last_successful_guardian_ts = None
    system._pending_trade_open_notifications = []
    system._pending_trade_close_notifications = []
    system.equity_tracker = SimpleNamespace(
        record_daily_equity_snapshot=lambda: None,
        refresh_account_stats_state=lambda: None)
    system.notifier = SimpleNamespace(
        send_message=lambda *a, **k: True,
        notify_error=lambda *a, **k: True)
    system.send_daily_position_summary_if_due = lambda force=False, mark_sent=True, **kwargs: False

    checked = []

    def record_symbol(symbol):
        if symbol not in checked:
            checked.append(symbol)
        return symbol
    # K线返回空 → 循环对每个 symbol 走到 fetch 后 continue，不进入信号分支
    system.exchange_api = SimpleNamespace(
        to_ccxt_symbol=record_symbol,
        list_position_symbols=lambda: [],
        get_position=lambda s: None,
        fetch_ohlcv=lambda *a, **k: [])
    return system, checked


def _make_health_ready(system):
    system.scheduler = SimpleNamespace(
        running=True,
        _thread=SimpleNamespace(is_alive=lambda: True))
    system._heartbeat_lock = threading.Lock()
    system._runner_heartbeat_ts = time.time()
    system._stop_event = threading.Event()
    system._last_check_date = system._daily_check_readiness(
        datetime.now())[1]
    return system


class OperationalHealthLatchTest(unittest.TestCase):
    def test_symbol_failure_degrades_health_until_full_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _ = _build_system(
                tmp, [{'name': 'BTCUSDT', 'enabled': True}])
            _make_health_ready(system)

            system.check_and_execute_trades(manual_run=True)

            self.assertEqual(
                'symbol_failures', system._last_trade_check_failure['kind'])
            self.assertFalse(system.health_snapshot()['healthy'])

            system.config['trading']['symbols'] = []
            system.check_and_execute_trades(manual_run=True)

            self.assertIsNone(system._last_trade_check_failure)
            self.assertTrue(system.health_snapshot()['healthy'])

    def test_trade_lock_conflict_degrades_health_until_full_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _ = _build_system(tmp, [])
            _make_health_ready(system)
            system._trade_lock.acquire()
            try:
                system.check_and_execute_trades(manual_run=True)
            finally:
                system._trade_lock.release()

            self.assertEqual(
                'trade_lock_busy', system._last_trade_check_failure['kind'])
            self.assertFalse(system.health_snapshot()['healthy'])

            system.check_and_execute_trades(manual_run=True)

            self.assertIsNone(system._last_trade_check_failure)
            self.assertTrue(system.health_snapshot()['healthy'])

    def test_outer_trade_check_exception_degrades_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _ = _build_system(tmp, [])
            _make_health_ready(system)
            system.equity_tracker.record_daily_equity_snapshot = Mock(
                side_effect=RuntimeError('snapshot broken'))

            system.check_and_execute_trades()

            self.assertEqual(
                'check_exception', system._last_trade_check_failure['kind'])
            self.assertFalse(system.health_snapshot()['healthy'])

    def test_guardian_failure_degrades_health_until_full_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _ = _build_system(
                tmp, [{'name': 'BTCUSDT', 'enabled': True}])
            _make_health_ready(system)
            system.exchange_api.list_position_symbols = Mock(
                side_effect=RuntimeError('position list broken'))

            self.assertFalse(system.reconcile_intraday_stop_losses())
            self.assertEqual(
                'guardian_failures', system._last_guardian_failure['kind'])
            self.assertFalse(system.health_snapshot()['healthy'])

            system.exchange_api.list_position_symbols = lambda: []
            self.assertTrue(system.reconcile_intraday_stop_losses())

            self.assertIsNone(system._last_guardian_failure)
            self.assertTrue(system.health_snapshot()['healthy'])


class RemovedSymbolStillManagedTest(unittest.TestCase):
    def test_removed_symbol_with_position_is_still_checked(self):
        """配置池已删但本地仍有持仓时继续托管。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, checked = _build_system(tmp, config_symbols=[])
            system.trade_state.add_open_position(
                'ETHUSDT', 'long', 3000.0, 1.0, 2800.0, strategy='ma_cross')

            system.check_and_execute_trades()

            self.assertEqual(checked, ['ETHUSDT'])

    def test_pool_symbols_and_held_orphans_are_both_checked(self):
        """配置池品种与已删除但有持仓的品种都进入检查集合。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, checked = _build_system(
                tmp, config_symbols=[{'name': 'BTCUSDT', 'enabled': True}])
            system.trade_state.add_open_position(
                'ETHUSDT', 'short', 3000.0, 1.0, 3200.0, strategy='ma_cross')

            system.check_and_execute_trades()

            self.assertEqual(sorted(checked), ['BTCUSDT', 'ETHUSDT'])

    def test_position_without_ma_audit_label_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, checked = _build_system(tmp, config_symbols=[])
            with self.assertRaises(TradeStatePersistenceError):
                system.trade_state.add_open_position(
                    'LTCUSDT', 'long', 100.0, 5.0, 90.0, strategy=None)

            self.assertIsNone(system.trade_state.get_open_position('LTCUSDT'))
            self.assertEqual(checked, [])


class PerSymbolIsolationTest(unittest.TestCase):
    def test_one_symbol_failure_does_not_block_others(self):
        """单品种异常只跳过该品种：其余品种照常检查；当日不标记完成（等待重试调度整轮重跑）。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, _checked = _build_system(
                tmp, config_symbols=[
                    {'name': 'BTCUSDT', 'enabled': True},
                    {'name': 'ETHUSDT', 'enabled': True},
                ])
            fetched = []

            def fetch(symbol, *a, **k):
                fetched.append(symbol)
                if symbol == 'BTCUSDT':
                    raise RuntimeError('模拟单品种交易所异常')
                return []

            system.exchange_api.fetch_ohlcv = fetch

            system.check_and_execute_trades()  # 不应向外抛异常

            self.assertEqual(sorted(fetched), ['BTCUSDT', 'ETHUSDT'])
            self.assertIsNone(system._last_check_date)

    def test_empty_ohlcv_keeps_day_incomplete_for_retry(self):
        """空 OHLCV 是未完成而非成功：保留当日重试。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, _checked = _build_system(
                tmp, config_symbols=[{'name': 'BTCUSDT', 'enabled': True}])

            system.check_and_execute_trades()

            self.assertIsNone(system._last_check_date)

    def test_manual_run_does_not_mark_day_done(self):
        """手动检查不得标记当日完成：00:00–08:00 间手动触发跑的是昨日数据，
        若标记会让当天 08:00 的正式日检被跳过，整日的新信号与仓位检查丢失。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, _checked = _build_system(
                tmp, config_symbols=[{'name': 'BTCUSDT', 'enabled': True}])
            snapshot = Mock()
            system.equity_tracker.record_daily_equity_snapshot = snapshot

            system.check_and_execute_trades(manual_run=True)

            self.assertIsNone(system._last_check_date)
            snapshot.assert_not_called()

    def test_completed_day_guard_runs_before_equity_snapshot(self):
        """08:01/兜底触发在当日已完成后直接返回，不得覆盖 08:00 收盘快照。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, _checked = _build_system(tmp, config_symbols=[])
            system._last_check_date = date.today().isoformat()
            snapshot = Mock()
            system.equity_tracker.record_daily_equity_snapshot = snapshot

            system.check_and_execute_trades()

            snapshot.assert_not_called()


class StartupCatchupTest(unittest.TestCase):
    """启动补跑：错过 08:00 调度（如恰在该时段重启）时补跑当日日检，不错过则不跑。"""

    def _system(self, tmp):
        system, _ = _build_system(tmp, config_symbols=[])
        system.config['scheduler'] = {'check_hour': 8, 'check_minute': 0}
        system.label = '欧易'
        calls = []
        system.check_and_execute_trades = lambda **kwargs: calls.append(kwargs.get('scheduled_date'))
        return system, calls

    def test_catchup_runs_when_past_due_and_not_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, calls = self._system(tmp)
            system._run_startup_catchup_check(now=datetime(2026, 7, 3, 15, 0))
            self.assertEqual(calls, ['2026-07-03'])

    def test_no_catchup_before_check_time(self):
        """未到检查时间：不补跑（昨日的检查属于旧进程周期，今日的等 08:00 正点）。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, calls = self._system(tmp)
            system._run_startup_catchup_check(now=datetime(2026, 7, 3, 7, 30))
            self.assertEqual(calls, [])

    def test_no_catchup_within_scheduler_window(self):
        """恰在调度窗口（08:00–08:01）启动：让正常 cron 先走，不抢跑。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, calls = self._system(tmp)
            system._run_startup_catchup_check(now=datetime(2026, 7, 3, 8, 1))
            self.assertEqual(calls, [])

    def test_no_catchup_when_already_done_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, calls = self._system(tmp)
            system._last_check_date = '2026-07-03'
            system._run_startup_catchup_check(now=datetime(2026, 7, 3, 15, 0))
            self.assertEqual(calls, [])

    def test_no_environment_switch_can_mark_a_daily_check_complete(self):
        self.assertFalse(hasattr(
            TradingSystem, '_apply_deploy_restart_skip_catchup'))

    def test_maintenance_sentinel_skips_daily_check_without_marking(self):
        with tempfile.TemporaryDirectory() as tmp:
            sentinel = os.path.join(tmp, '.maintenance_no_open')
            with open(sentinel, 'w', encoding='utf-8') as handle:
                handle.write('{}')
            system = TradingSystem.__new__(TradingSystem)
            system._last_check_date = '2026-07-18'
            with patch.dict(os.environ, {
                    'TRADING_MAINTENANCE_SENTINEL': sentinel}, clear=False):
                system.check_and_execute_trades(
                    scheduled_date='2026-07-19')
            self.assertEqual('2026-07-18', system._last_check_date)
            self.assertFalse(hasattr(system, '_trade_lock'))

    def test_buffer_window_spans_hour_boundary(self):
        """check_minute 接近整点时缓冲窗口须跨整点：check_minute=59 的缓冲应到 09:01，
        08:59–09:00 内不抢跑（让正点 cron 先走），09:01 起才补跑。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, calls = self._system(tmp)
            system.config['scheduler'] = {'check_hour': 8, 'check_minute': 59}
            # 09:00 仍在 2 分钟缓冲窗口内（08:59 正点 + 2 分钟 = 09:01），不补跑
            system._run_startup_catchup_check(now=datetime(2026, 7, 3, 9, 0))
            self.assertEqual(calls, [])
            # 09:02 已过缓冲窗口且今日未跑：补跑一轮
            system._run_startup_catchup_check(now=datetime(2026, 7, 3, 9, 2))
            self.assertEqual(calls, ['2026-07-03'])


class DailyCheckFallbackJobTest(unittest.TestCase):
    """日检兜底任务：register_jobs 注册每 30 分钟的幂等兜底补跑，
    主执行与 +1 分钟重试整窗失败（如恰逢网络故障）后当日仍能补上。"""

    def _register(self, tmp):
        system, _ = _build_system(tmp, config_symbols=[])
        system.config['scheduler'] = {'check_hour': 8, 'check_minute': 0}
        system.exchange_id = 'okx'
        system.label = '欧易'
        jobs = {}
        system.scheduler = SimpleNamespace(
            add_job=lambda func, trigger, **kw: jobs.__setitem__(kw.get('id'), (func, trigger, kw)))
        system._record_equity_tick_with_alert = lambda: None
        calls = []
        system.check_and_execute_trades = lambda **kwargs: calls.append(kwargs.get('scheduled_date'))
        system.register_jobs(system.config['scheduler'])
        return system, jobs, calls

    def test_fallback_job_registered_every_30_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _system, jobs, _calls = self._register(tmp)
            self.assertIn('okx_daily_check_fallback', jobs)
            func, trigger, kw = jobs['okx_daily_check_fallback']
            self.assertEqual(trigger, 'cron')
            self.assertEqual(kw.get('minute'), '*/30')
            self.assertEqual(kw.get('max_instances'), 1)
            self.assertTrue(kw.get('coalesce'))

    def test_fallback_triggers_when_past_due_and_not_done(self):
        """到点未跑：兜底任务触发当日日检补跑。"""
        with tempfile.TemporaryDirectory() as tmp:
            _system, jobs, calls = self._register(tmp)
            func, _trigger, _kw = jobs['okx_daily_check_fallback']
            func(now=datetime(2026, 7, 3, 15, 0))
            self.assertEqual(calls, ['2026-07-03'])

    def test_fallback_skips_when_already_done_today(self):
        """今日已跑：兜底任务空转，不重复执行。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, jobs, calls = self._register(tmp)
            system._last_check_date = '2026-07-03'
            func, _trigger, _kw = jobs['okx_daily_check_fallback']
            func(now=datetime(2026, 7, 3, 15, 0))
            self.assertEqual(calls, [])


class MaCrossFlipResidueTest(unittest.TestCase):
    """翻转平仓后旧止损撤销不可确认：不反手，但记录 T+1 交由次日重入（恢复永远在市）。"""

    def _build(self, tmp, cancel_ok):
        system = TradingSystem.__new__(TradingSystem)
        system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
        system.trade_state.add_open_position(
            'ETHUSDT', 'short', 3000.0, 1.0, 3200.0, 'stop-1', strategy='ma_cross')
        system._stop_anomalies = {}
        system._pending_trade_close_notifications = []
        system.stop_loss_file = os.path.join(tmp, 'stop_loss_dates.json')
        system.stop_loss_dates = {}
        system.notifier = SimpleNamespace(notify_error=lambda *a, **k: True,
                                          notify_signal_missed=lambda *a, **k: True)
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda s: s,
            exchange=SimpleNamespace(fetch_ticker=lambda s: {'last': 2900.0}),
            get_last_price=lambda s: 2900.0,
            close_position=lambda *a, **k: {
                'id': 'close-1', 'ids': ['close-1'],
                'average': 2900.0, 'confirmed': True,
                'fully_closed': True, 'fully_filled': True,
                'amount': 1.0, 'requested_amount': 1.0,
                'remaining_amount': 0.0},
            cancel_order=lambda *a, **k: cancel_ok,
            cancel_all_orders=lambda *a, **k: cancel_ok or None)
        opened = []

        def _fake_open(*a, **k):
            # 桩须模拟真实 _execute_open 成功建仓（add_open_position），否则 get_open_position
            # 仍返回 None，会被翻转的「开仓腿失败」检测误判为失败并错记 T+1
            opened.append(a)
            system.trade_state.add_open_position(a[0], a[1], 2900.0, 1.0, 2800.0,
                                                 'stop-new', strategy='ma_cross')
        system._execute_open = _fake_open
        return system, opened

    def test_unconfirmed_cancel_records_t1_and_blocks_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, opened = self._build(tmp, cancel_ok=False)
            position = system.trade_state.get_open_position('ETHUSDT')
            signal = {'current_close': 2900.0, 'lower_stop': 2800.0, 'upper_stop': 3100.0}

            system._flip_position('ETHUSDT', signal, position, 'long', {'name': 'ETHUSDT'})

            self.assertEqual(opened, [])  # 不反手开新仓
            self.assertEqual(system.stop_loss_dates.get('ETHUSDT'),
                             date.today().strftime('%Y-%m-%d'))  # 已记 T+1，次日按 EMA 方向重入
            self.assertTrue(system.trade_state.has_stop_residue('ETHUSDT'))  # 残留标记阻断开仓

    def test_confirmed_cancel_flips_without_t1(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, opened = self._build(tmp, cancel_ok=True)
            position = system.trade_state.get_open_position('ETHUSDT')
            signal = {'current_close': 2900.0, 'lower_stop': 2800.0, 'upper_stop': 3100.0}

            system._flip_position('ETHUSDT', signal, position, 'long', {'name': 'ETHUSDT'})

            self.assertEqual(len(opened), 1)  # 正常反手
            self.assertEqual(system.stop_loss_dates, {})  # 不记 T+1
            self.assertFalse(system.trade_state.has_stop_residue('ETHUSDT'))

    def test_retired_symbol_closes_without_reopening_or_t1(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, opened = self._build(tmp, cancel_ok=True)
            position = system.trade_state.get_open_position('ETHUSDT')
            signal = {'current_close': 2900.0, 'lower_stop': 2800.0,
                      'upper_stop': 3100.0}

            system._flip_position(
                'ETHUSDT', signal, position, 'long',
                {'name': 'ETHUSDT', '_retired_from_pool': True})

            self.assertEqual(opened, [])
            self.assertIsNone(system.trade_state.get_open_position('ETHUSDT'))
            self.assertEqual(system.stop_loss_dates, {})

    def test_retired_symbol_cancel_uncertain_never_creates_t1(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, opened = self._build(tmp, cancel_ok=False)
            position = system.trade_state.get_open_position('ETHUSDT')
            signal = {'current_close': 2900.0, 'lower_stop': 2800.0,
                      'upper_stop': 3100.0}

            system._flip_position(
                'ETHUSDT', signal, position, 'long',
                {'name': 'ETHUSDT', '_retired_from_pool': True})

            self.assertEqual(opened, [])
            self.assertEqual(system.stop_loss_dates, {})
            self.assertTrue(system.trade_state.has_stop_residue('ETHUSDT'))




class RetiredExternalFlatTest(unittest.TestCase):
    def test_retired_ma_flat_close_does_not_create_t1(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.config = {'trading': {'symbols': []}}
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.add_open_position(
                'ETHUSDT', 'long', 3000.0, 1.0, 2800.0,
                'stop-1', strategy='ma_cross')
            system.stop_loss_dates = {}
            system._stop_anomalies = {}
            system._cancel_stop_order_confirmed = Mock(return_value=True)

            position = system.trade_state.get_open_position('ETHUSDT')
            closed, saved, cleared = system._handle_exchange_flat_close(
                'ETHUSDT', 'ETH-USDT-SWAP', position, 2800.0,
                '退池外部平仓')

            self.assertIsNotNone(closed)
            self.assertTrue(saved)
            self.assertTrue(cleared)
            self.assertEqual('estimated_stop', closed['exit_price_source'])
            self.assertIs(True, closed['exit_price_estimated'])
            self.assertEqual(system.trade_state.get_stop_loss_dates(), {})
            self.assertEqual(system.stop_loss_dates, {})

    def test_disabled_in_pool_ma_flat_close_does_not_create_t1(self):
        """在池但已禁用的持仓品种：外部平仓（止损）按只平不开处理，不得记 T+1，
        否则次日会按 EMA 方向自动重入——与「删除品种」同规则。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.config = {'trading': {'symbols': [
                {'name': 'ETHUSDT', 'enabled': False}]}}
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.add_open_position(
                'ETHUSDT', 'long', 3000.0, 1.0, 2800.0,
                'stop-1', strategy='ma_cross')
            system.stop_loss_dates = {}
            system._stop_anomalies = {}
            system._cancel_stop_order_confirmed = Mock(return_value=True)

            position = system.trade_state.get_open_position('ETHUSDT')
            closed, saved, _cleared = system._handle_exchange_flat_close(
                'ETHUSDT', 'ETH-USDT-SWAP', position, 2800.0,
                '禁用仓外部平仓')

            self.assertIsNotNone(closed)
            self.assertTrue(saved)
            self.assertEqual(system.trade_state.get_stop_loss_dates(), {})
            self.assertEqual(system.stop_loss_dates, {})

    def test_enabled_in_pool_ma_flat_close_still_records_t1(self):
        """对照：在池且启用的正常品种，外部平仓仍记 T+1；禁用收口不得误伤正常品种。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.config = {'trading': {'symbols': [
                {'name': 'ETHUSDT', 'enabled': True}]}}
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.add_open_position(
                'ETHUSDT', 'long', 3000.0, 1.0, 2800.0,
                'stop-1', strategy='ma_cross')
            system.stop_loss_dates = {}
            system._stop_anomalies = {}
            system._cancel_stop_order_confirmed = Mock(return_value=True)

            position = system.trade_state.get_open_position('ETHUSDT')
            system._handle_exchange_flat_close(
                'ETHUSDT', 'ETH-USDT-SWAP', position, 2800.0,
                'ma_cross 日检平仓')

            self.assertIn('ETHUSDT', system.trade_state.get_stop_loss_dates())


class StateOwnerGuardTest(unittest.TestCase):
    def _system(self, tmpdir):
        system = TradingSystem.__new__(TradingSystem)
        system.exchange_id = 'okx'
        system.label = '欧易'
        system.base_dir = tmpdir
        system.trade_state = TradeState(os.path.join(tmpdir, 'trade_state.json'))
        return system

    def test_fresh_empty_state_is_claimed(self):
        """全新空状态：放行并打上 okx 归属标记。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system._guard_state_owner()
            self.assertEqual(system.trade_state.get_owner_exchange(), 'okx')
            with open(os.path.join(tmp, 'trade_state.json')) as f:
                self.assertEqual(json.load(f)['exchange'], 'okx')

    def test_owned_by_okx_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.trade_state.claim_owner_exchange('okx')
            system.trade_state.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, strategy='ma_cross')
            system._guard_state_owner()  # 不应抛异常

    def test_owned_by_other_exchange_is_rejected_at_schema_boundary(self):
        """其它交易所归属在账本加载边界就拒绝，不能进入启动流程。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            payload = TradeState.get_default_state()
            payload['exchange'] = 'other'
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle)
            os.chmod(path, 0o600)
            with self.assertRaises(TradeStatePersistenceError):
                TradeState(path)

    def test_unmarked_state_with_positions_blocks_startup(self):
        """有持仓但无归属标记(可能是旧币安遗留)：拒绝启动，要求人工确认。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.trade_state.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, strategy='ma_cross')
            with self.assertRaises(RuntimeError):
                system._guard_state_owner()

    def test_unmarked_pending_state_blocks_cross_exchange_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IOLD123',
                {'side': 'long', 'entry_price': 100,
                 'stop_loss_price': 90})

            with self.assertRaises(RuntimeError):
                system._guard_state_owner()

    def test_unmarked_external_history_blocks_directory_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            with open(os.path.join(tmp, 'closed_trades_archive.json'),
                      'w', encoding='utf-8') as handle:
                json.dump([{'symbol': 'BTCUSDT'}], handle)

            with self.assertRaises(RuntimeError):
                system._guard_state_owner()

    def test_unmarked_annual_history_blocks_directory_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            with open(os.path.join(tmp, 'closed_trades_archive_2026.json'),
                      'w', encoding='utf-8') as handle:
                json.dump([{'symbol': 'BTCUSDT'}], handle)

            with self.assertRaises(RuntimeError):
                system._guard_state_owner()


class StopResidueBlockTest(unittest.TestCase):
    def test_execute_open_blocked_while_residue_marked(self):
        """品种被标记止损残留时，_execute_open 直接拒绝开仓（不触达交易所）。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, _ = _build_system(tmp, config_symbols=[])
            system.exchange_api.open_position = lambda *a, **k: self.fail("残留期间不得开仓")
            system.trade_state.mark_stop_residue('BTCUSDT')

            system._execute_open('BTCUSDT', 'long', 100.0, 90.0, {'name': 'BTCUSDT'})

            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))

    def test_residue_mark_survives_restart(self):
        """残留标记持久化：重启后仍然阻断。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            TradeState(path).mark_stop_residue('ETHUSDT')
            reloaded = TradeState(path)
            self.assertTrue(reloaded.has_stop_residue('ETHUSDT'))
            reloaded.clear_stop_residue('ETHUSDT')
            self.assertFalse(TradeState(path).has_stop_residue('ETHUSDT'))


class IntradayReconcileResidueTest(unittest.TestCase):
    """盘中巡检发现「交易所无仓、本地有仓」：记平后必须确认旧止损单也已消失。"""

    def _system_with_position(self, tmp, cancel_ok):
        system, _ = _build_system(tmp, config_symbols=[])
        system.trade_state.add_open_position(
            'BTCUSDT', 'long', 60000.0, 0.1, 55000.0, stop_order_id='stop-1', strategy='ma_cross')
        system.exchange_api.get_position = lambda s: None  # 交易所端已无仓
        system.exchange_api.cancel_order = lambda s, oid: cancel_ok
        system.exchange_api.cancel_all_orders = lambda s: (True if cancel_ok else None)
        system.notifier.notify_stop_loss_triggered = lambda *a, **k: True
        return system

    def test_residue_cleared_when_cancel_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system_with_position(tmp, cancel_ok=True)
            system.reconcile_intraday_stop_losses()
            self.assertIsNone(system.trade_state.get_open_position('BTCUSDT'))  # 已记平
            self.assertFalse(system.trade_state.has_stop_residue('BTCUSDT'))

    def test_residue_marked_when_cancel_unconfirmed(self):
        """撤销不可确认：记平照常完成，但标记残留（持久化）阻断该品种后续开仓。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system_with_position(tmp, cancel_ok=False)
            system.reconcile_intraday_stop_losses()
            self.assertIsNone(system.trade_state.get_open_position('BTCUSDT'))
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))

    def test_preexisting_unknown_residue_observes_visibility_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system_with_position(tmp, cancel_ok=True)
            system.trade_state.mark_stop_residue('BTCUSDT')

            system.reconcile_intraday_stop_losses()

            self.assertIsNone(system.trade_state.get_open_position('BTCUSDT'))
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))


class UnknownStopResidueCleanupTest(unittest.TestCase):
    def test_known_cancel_failure_cannot_be_washed_by_clean_cancel_all(self):
        """精确 ID 撤销失败是终态不明；批量清扫成功也不得清除 residue。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, 'trade_state.json')
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(state_path)
            system.trade_state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0,
                'stop-known', strategy='ma_cross')
            cancel_all = Mock(return_value=True)
            system.exchange_api = SimpleNamespace(
                cancel_order=Mock(return_value=False),
                cancel_all_orders=cancel_all,
            )
            system.notifier = SimpleNamespace(notify_error=Mock())

            self.assertFalse(system._cancel_stop_order_confirmed(
                'BTCUSDT', 'BTC/USDT:USDT', 'stop-known'))

            cancel_all.assert_called_once_with('BTC/USDT:USDT')
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))
            self.assertTrue(TradeState(state_path).has_stop_residue('BTCUSDT'))

    def test_known_id_success_still_requires_cancel_all_when_residue_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0,
                'stop-known', strategy='ma_cross')
            system.trade_state.mark_stop_residue('BTCUSDT')
            cancel_all = Mock(return_value=True)
            system.exchange_api = SimpleNamespace(
                cancel_order=Mock(return_value=True),
                cancel_all_orders=cancel_all,
            )
            system.notifier = SimpleNamespace(notify_error=Mock())
            system.STOP_RESIDUE_VISIBILITY_GRACE_SECONDS = 0

            self.assertTrue(system._cancel_stop_order_confirmed(
                'BTCUSDT', 'BTC/USDT:USDT', 'stop-known'))

            cancel_all.assert_called_once_with('BTC/USDT:USDT')
            self.assertFalse(system.trade_state.has_stop_residue('BTCUSDT'))

    def test_fresh_unknown_residue_survives_first_clean_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.mark_stop_residue('BTCUSDT')
            system.exchange_api = SimpleNamespace(
                cancel_order=Mock(return_value=True),
                cancel_all_orders=Mock(return_value=True),
            )
            system.notifier = SimpleNamespace(notify_error=Mock())

            self.assertFalse(system._cancel_stop_order_confirmed(
                'BTCUSDT', 'BTC/USDT:USDT', None))
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))


class IntradayPerSymbolIsolationTest(unittest.TestCase):
    """盘中巡检的单品种隔离：一个品种异常（如止损自愈时面值不可得）不得中断其余品种巡检。"""

    def test_one_symbol_failure_does_not_block_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _ = _build_system(tmp, config_symbols=[])
            system.trade_state.add_open_position('AAAUSDT', 'long', 100.0, 1.0, 90.0, strategy='ma_cross')
            system.trade_state.add_open_position('BBBUSDT', 'long', 100.0, 1.0, 90.0, strategy='ma_cross')
            # 盘中巡检先完整核对方向和张数，再进入止损自愈。
            system.exchange_api.get_position = lambda s: {'contracts': 1, 'side': 'long'}
            system.exchange_api._coin_to_contracts = lambda s, amount: amount
            system.exchange_api._contracts_to_coins = lambda s, contracts: contracts
            ensured = []

            def ensure(symbol, ccxt_symbol, position, strategy_name):
                if symbol == 'AAAUSDT':
                    raise RuntimeError('模拟止损自愈异常（如面值不可得）')
                ensured.append(symbol)

            system._ensure_stop_order_alive = ensure

            system.reconcile_intraday_stop_losses()  # 不应向外抛异常

            self.assertEqual(ensured, ['BBBUSDT'])  # 后续品种照常巡检

    def test_negative_short_contracts_are_never_treated_as_flat(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _ = _build_system(tmp, config_symbols=[])
            system.trade_state.add_open_position(
                'ETHUSDT', 'short', 100.0, 1.0, 110.0,
                strategy='ma_cross')
            system.exchange_api.list_position_symbols = lambda: ['ETHUSDT']
            system.exchange_api.get_position = lambda _symbol: {
                'contracts': -1.0, 'side': 'short'}
            system.exchange_api._coin_to_contracts = (
                lambda _symbol, amount: amount)
            system.exchange_api._contracts_to_coins = (
                lambda _symbol, contracts: contracts)
            system._ensure_stop_order_alive = Mock(return_value=True)
            system._handle_exchange_flat_close = Mock(
                side_effect=AssertionError('真实空头不得走空仓删账'))

            self.assertTrue(system.reconcile_intraday_stop_losses())

            self.assertIsNotNone(
                system.trade_state.get_open_position('ETHUSDT'))
            system._handle_exchange_flat_close.assert_not_called()


class ResidueAutoClearGuardTest(unittest.TestCase):
    """残留自动清理：本地无仓但交易所有仓（疑似人工仓位）时绝不盲撤。"""

    def _system_with_residue(self, tmp, exchange_position):
        system, _ = _build_system(tmp, config_symbols=[])
        system.STOP_RESIDUE_VISIBILITY_GRACE_SECONDS = 0
        system.trade_state.mark_stop_residue('BTCUSDT')
        calls = []
        system.exchange_api.get_position = lambda s: exchange_position
        system.exchange_api.cancel_all_orders = lambda s: calls.append(s) or True
        return system, calls

    def test_skips_when_exchange_has_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, calls = self._system_with_residue(tmp, {'contracts': 2.0})
            system._retry_clear_stop_residues()
            self.assertEqual(calls, [])  # 不盲撤（手动仓的止损也会被撤掉）
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))  # 残留保留，继续阻断

    def test_clears_when_exchange_flat(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, calls = self._system_with_residue(tmp, None)
            system._retry_clear_stop_residues()
            self.assertEqual(calls, ['BTCUSDT'])
            self.assertFalse(system.trade_state.has_stop_residue('BTCUSDT'))  # 清理确认，解除阻断


class MigrationGuardTest(unittest.TestCase):
    """旧状态不得在单策略预检之后由 runtime 自动导入。"""

    def _system(self, tmp):
        system = TradingSystem.__new__(TradingSystem)
        system.base_dir = tmp
        return system

    @staticmethod
    def _write(path, positions):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump({'open_positions': positions, 'closed_trades': []}, f)

    def test_unmarked_legacy_positions_block_without_touching_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(os.path.join(tmp, 'trade_state.json'), {})
            legacy = {'BTCUSDT': {
                'symbol': 'BTCUSDT', 'side': 'long', 'entry_price': 60000,
                'position_size': 0.1, 'stop_loss_price': 55000,
            }}
            self._write(os.path.join(tmp, 'data', 'okx', 'trade_state.json'), legacy)

            with self.assertRaisesRegex(RuntimeError, '禁止.*自动导入'):
                self._system(tmp)._migrate_okx_legacy_state()

            with open(os.path.join(tmp, 'trade_state.json')) as f:
                self.assertEqual({}, json.load(f)['open_positions'])
            self.assertFalse(any(
                name.startswith('trade_state.json.bak.empty.')
                for name in os.listdir(tmp)))

    def test_empty_legacy_directory_is_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'data', 'okx'))
            self._system(tmp)._migrate_okx_legacy_state()

    def test_completed_marker_makes_legacy_directory_inert(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = {'BTCUSDT': {
                'symbol': 'BTCUSDT', 'side': 'long', 'entry_price': 60000,
                'position_size': 0.1, 'stop_loss_price': 55000,
            }}
            self._write(
                os.path.join(tmp, 'data', 'okx', 'trade_state.json'), legacy)
            marker = os.path.join(
                tmp, '.okx_legacy_migration_complete.json')
            with open(marker, 'w', encoding='utf-8') as handle:
                json.dump({'exchange': 'okx'}, handle)

            self._system(tmp)._migrate_okx_legacy_state()

            self.assertFalse(os.path.exists(os.path.join(tmp, 'trade_state.json')))

    def test_invalid_completed_marker_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'data', 'okx'))
            marker = os.path.join(
                tmp, '.okx_legacy_migration_complete.json')
            with open(marker, 'w', encoding='utf-8') as handle:
                json.dump({'exchange': 'other'}, handle)
            with self.assertRaisesRegex(RuntimeError, '迁移标记非法'):
                self._system(tmp)._migrate_okx_legacy_state()


class StopSelfHealTest(unittest.TestCase):
    """止损自愈四态：intact / adoptable / mismatch / missing。"""

    def _system(self, tmp, state=None, state_error=None, create_result=None):
        system = TradingSystem.__new__(TradingSystem)
        system.label = '欧易'
        system._stop_anomalies = {}
        system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
        system.trade_state.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0,
                                             stop_order_id='stop-1', strategy='ma_cross')
        self.created, self.find_calls = [], []

        def find_state(s, side, amount, price, oid=None):
            self.find_calls.append((side, amount, price, oid))
            if state_error:
                raise state_error
            return state

        def create_stop(s, side, amount, price):
            self.created.append((side, amount, price))
            return create_result

        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda s: s,
            find_stop_order_state=find_state,
            create_stop_loss_order=create_stop)
        self.messages, self.errors = [], []
        system.notifier = SimpleNamespace(
            send_message=lambda *a, **k: self.messages.append(a) or True,
            notify_error=lambda m: self.errors.append(m) or True)
        return system

    def _run(self, system):
        position = system.trade_state.get_open_position('BTCUSDT')
        system._ensure_stop_order_alive('BTCUSDT', 'BTCUSDT', position, '双均线 EMA')

    def test_intact_no_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp, state='intact')
            self._run(system)
            self.assertEqual(self.created, [])
            # 严格匹配的输入完整传递：方向+币数+触发价+本地单号
            self.assertEqual(self.find_calls, [('long', 0.1, 55000.0, 'stop-1')])

    def test_mismatch_alerts_human_no_replant(self):
        """id 还在但内容不符（疑被人工改挂）：自动补挂会双止损，告警人工且记入异常状态。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp, state='mismatch')
            self._run(system)
            self._run(system)  # 第二轮巡检重复判定
            self.assertEqual(self.created, [])
            self.assertEqual(len(self.errors), 1)  # 告警只在状态首次进入时发（节流，防巡检轰炸）
            self.assertEqual(system._stop_anomalies, {'BTCUSDT': 'mismatch'})
            self.assertTrue(system.trade_state.is_position_quarantined('BTCUSDT'))

    def test_unique_matching_new_stop_id_is_adopted_without_replant(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(
                tmp, state={'state': 'adoptable', 'order_id': 'stop-new'})
            self._run(system)

            self.assertEqual(self.created, [])
            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual(position['stop_order_id'], 'stop-new')
            self.assertEqual(position['stop_order_size'], 0.1)
            self.assertEqual(system._stop_anomalies, {})
            self.assertEqual(len(self.messages), 1)

    def test_adoptable_without_id_becomes_mismatch_and_never_replants(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp, state={'state': 'adoptable'})
            self._run(system)

            self.assertEqual(self.created, [])
            self.assertEqual(system._stop_anomalies, {'BTCUSDT': 'mismatch'})

    def test_intact_clears_anomaly_state(self):
        """状态恢复正常：清除异常记录，前端警示消失。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp, state='intact')
            system._stop_anomalies['BTCUSDT'] = 'mismatch'
            self._run(system)
            self.assertEqual(system._stop_anomalies, {})

    def test_position_close_clears_anomaly_state(self):
        """仓位结束（记平成功）：止损异常警示随仓位生命周期终结，不留僵尸警示。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp, state='mismatch')
            system.notifier.notify_error = lambda m: True
            system._stop_anomalies['BTCUSDT'] = 'mismatch'

            system._close_trade_state_with_runtime_fallback('BTCUSDT', 58000.0, '测试平仓')

            self.assertEqual(system._stop_anomalies, {})
            self.assertIsNone(system.trade_state.get_open_position('BTCUSDT'))

    def test_missing_stop_gets_replanted(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp, state='missing', create_result={'id': 'stop-new'})
            self._run(system)
            self.assertEqual(self.created, [('long', 0.1, 55000.0)])  # 按本地止损价补挂
            pos = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual(pos['stop_order_id'], 'stop-new')        # 本地记录同步新 id
            self.assertEqual(len(self.messages), 1)                    # 推送补挂通知

    def test_replant_failure_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp, state='missing', create_result=None)
            self._run(system)
            self._run(system)
            self.assertEqual(len(self.errors), 1)  # 节流：持续失败不重复轰炸
            self.assertEqual(system._stop_anomalies, {'BTCUSDT': 'replant_failed'})
            self.assertEqual(system.trade_state.get_open_position('BTCUSDT')['stop_order_id'], 'stop-1')

    def test_replant_exception_marks_unknown_residue_before_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp, state='missing')
            system.exchange_api.create_stop_loss_order = Mock(
                side_effect=RuntimeError('POST outcome unknown'))

            self._run(system)

            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))
            self.assertTrue(system.trade_state.is_position_quarantined('BTCUSDT'))
            self.assertEqual(
                {'BTCUSDT': 'replant_failed'}, system._stop_anomalies)

    def test_fresh_residue_missing_keeps_marker_and_never_replants(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp, state='missing')
            system.trade_state.mark_stop_residue('BTCUSDT')
            self._run(system)
            self.assertEqual(
                self.find_calls, [('long', 0.1, 55000.0, 'stop-1')])
            self.assertEqual(self.created, [])
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))
            self.assertTrue(system.trade_state.is_position_quarantined('BTCUSDT'))

    def test_fresh_residue_intact_protects_but_does_not_release_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp, state='intact')
            system.trade_state.mark_stop_residue('BTCUSDT')

            self.assertTrue(system._ensure_stop_order_alive(
                'BTCUSDT', 'BTCUSDT',
                system.trade_state.get_open_position('BTCUSDT'),
                '双均线 EMA'))

            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))
            self.assertTrue(system.trade_state.is_position_quarantined('BTCUSDT'))
            self.assertEqual(self.created, [])

    def test_fresh_residue_adopts_visible_stop_but_keeps_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(
                tmp, state={'state': 'adoptable', 'order_id': 'stop-new'})
            system.trade_state.mark_stop_residue('BTCUSDT')

            self.assertTrue(system._ensure_stop_order_alive(
                'BTCUSDT', 'BTCUSDT',
                system.trade_state.get_open_position('BTCUSDT'),
                '双均线 EMA'))

            self.assertEqual(
                'stop-new', system.trade_state.get_open_position(
                    'BTCUSDT')['stop_order_id'])
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))
            self.assertTrue(system.trade_state.is_position_quarantined('BTCUSDT'))
            self.assertEqual(self.created, [])

    def test_second_stop_visible_after_grace_becomes_mismatch_without_repost(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.trade_state.mark_stop_residue('BTCUSDT')
            system.exchange_api.find_stop_order_state = Mock(
                side_effect=['intact', 'mismatch'])
            position = system.trade_state.get_open_position('BTCUSDT')

            self.assertTrue(system._ensure_stop_order_alive(
                'BTCUSDT', 'BTCUSDT', position, '双均线 EMA'))
            system.STOP_RESIDUE_VISIBILITY_GRACE_SECONDS = 0
            self.assertFalse(system._ensure_stop_order_alive(
                'BTCUSDT', 'BTCUSDT', position, '双均线 EMA'))

            self.assertEqual(self.created, [])
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))
            self.assertTrue(system.trade_state.is_position_quarantined('BTCUSDT'))

    def test_elapsed_residue_missing_rechecks_then_replants(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(
                tmp, state='missing', create_result={'id': 'stop-new'})
            system.trade_state.mark_stop_residue('BTCUSDT')
            system.STOP_RESIDUE_VISIBILITY_GRACE_SECONDS = 0

            self._run(system)

            self.assertEqual(self.created, [('long', 0.1, 55000.0)])
            self.assertFalse(system.trade_state.has_stop_residue('BTCUSDT'))

    def test_query_failure_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp, state_error=RuntimeError('查询失败'))
            self._run(system)
            self.assertEqual(self.created, [])  # fail-safe：不确定时不补挂


class StopConfirmOnPersistFailureTest(unittest.TestCase):
    """「交易所无仓」分支：落盘失败走 runtime 补偿时，撤旧止损确认不得被跳过（第八轮审查边界）。"""

    def _system(self):
        system = TradingSystem.__new__(TradingSystem)
        system._stop_anomalies = {}
        cancel_calls = []
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda s: s,
            get_position=lambda s: None,  # 交易所端已无仓
            cancel_order=lambda s, oid: cancel_calls.append(oid) or True,
            cancel_all_orders=lambda s: True)
        system.notifier = SimpleNamespace(
            notify_error=lambda *a, **k: True,
            notify_stop_loss_triggered=lambda *a, **k: True)
        system.trade_state = SimpleNamespace(
            get_open_position=lambda s: {'symbol': s},
            close_position=self._raise_persistence_error,
            force_runtime_close_position=(
                lambda s, p, **kwargs: {'symbol': s, 'exit_price': p}),
            clear_stop_residue=lambda s: None,
            mark_stop_residue=lambda s: None)
        system._execute_open = lambda *a, **k: self.fail("落盘失败后不得重入开仓")
        system.record_stop_loss = lambda s: self.fail("落盘失败后不得记录 T+1")
        return system, cancel_calls

    @staticmethod
    def _raise_persistence_error(symbol, exit_price, **kwargs):
        raise TradeStatePersistenceError('磁盘故障')

    def test_ma_cross_branch_still_confirms_stop_cancel(self):
        system, cancel_calls = self._system()
        signal = {'current_close': 101}
        position = {'side': 'long', 'stop_loss_price': 99, 'position_size': 2, 'stop_order_id': 'stop-9'}

        system.handle_open_position_ma_cross('ETHUSDT', signal, position, {'name': 'ETHUSDT'})

        self.assertEqual(cancel_calls, ['stop-9'])


class TradeStateTransactionTest(unittest.TestCase):
    """核心状态方法事务化：落盘失败时内存必须回滚，不得留下与磁盘/交易所不一致的假状态。"""

    def _ts_with_failing_save(self, tmp):
        ts = TradeState(os.path.join(tmp, 'trade_state.json'))
        return ts

    def test_add_open_position_rolls_back_memory_on_save_failure(self):
        """开仓保存失败：内存不得留下假仓（交易所侧已回滚平仓，假仓会被巡检再记一笔假平仓）。"""
        with tempfile.TemporaryDirectory() as tmp:
            ts = self._ts_with_failing_save(tmp)
            from unittest.mock import patch, Mock
            with patch.object(trade_state_module, 'atomic_write_json', Mock(return_value=False)):
                with self.assertRaises(TradeStatePersistenceError):
                    ts.add_open_position(
                        'BTCUSDT', 'long', 60000.0, 0.1, 55000.0,
                        'stop-1', strategy='ma_cross')
            self.assertIsNone(ts.get_open_position('BTCUSDT'))

    def test_update_stop_loss_rolls_back_memory_on_save_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = self._ts_with_failing_save(tmp)
            ts.add_open_position(
                'BTCUSDT', 'long', 60000.0, 0.1, 55000.0,
                'stop-1', strategy='ma_cross')
            from unittest.mock import patch, Mock
            with patch.object(trade_state_module, 'atomic_write_json', Mock(return_value=False)):
                with self.assertRaises(TradeStatePersistenceError):
                    ts.update_stop_loss('BTCUSDT', 58000.0, 'stop-2')
            pos = ts.get_open_position('BTCUSDT')
            self.assertEqual(pos['stop_loss_price'], 55000.0)
            self.assertEqual(pos['stop_order_id'], 'stop-1')

    def test_close_position_rolls_back_memory_on_save_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = self._ts_with_failing_save(tmp)
            ts.add_open_position(
                'BTCUSDT', 'long', 60000.0, 0.1, 55000.0,
                'stop-1', strategy='ma_cross')
            from unittest.mock import patch, Mock
            with patch.object(trade_state_module, 'atomic_write_json', Mock(return_value=False)):
                with self.assertRaises(TradeStatePersistenceError):
                    ts.close_position('BTCUSDT', 62000.0)
            self.assertIsNotNone(ts.get_open_position('BTCUSDT'))
            self.assertEqual(ts.get_closed_trades(), [])

    def test_exchange_flat_close_commits_trade_t1_and_cleanup_marker_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = self._ts_with_failing_save(tmp)
            ts.add_open_position(
                'BTCUSDT', 'long', 60000.0, 0.1, 55000.0,
                'stop-1', strategy='ma_cross')
            today = date.today().strftime('%Y-%m-%d')

            closed = ts.close_position(
                'BTCUSDT', 55000.0, stop_loss_date=today,
                stop_cleanup_pending=True)

            self.assertIsNotNone(closed)
            self.assertIsNone(ts.get_open_position('BTCUSDT'))
            self.assertEqual(today, ts.get_stop_loss_dates()['BTCUSDT'])
            self.assertTrue(ts.has_stop_residue('BTCUSDT'))

    def test_exchange_flat_atomic_close_rolls_back_every_field_on_save_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = self._ts_with_failing_save(tmp)
            ts.add_open_position(
                'BTCUSDT', 'long', 60000.0, 0.1, 55000.0,
                'stop-1', strategy='ma_cross')
            today = date.today().strftime('%Y-%m-%d')
            from unittest.mock import patch, Mock

            with patch.object(
                    trade_state_module, 'atomic_write_json', Mock(return_value=False)):
                with self.assertRaises(TradeStatePersistenceError):
                    ts.close_position(
                        'BTCUSDT', 55000.0, stop_loss_date=today,
                        stop_cleanup_pending=True)

            self.assertIsNotNone(ts.get_open_position('BTCUSDT'))
            self.assertEqual([], ts.get_closed_trades())
            self.assertNotIn('BTCUSDT', ts.get_stop_loss_dates())
            self.assertFalse(ts.has_stop_residue('BTCUSDT'))

    def test_force_runtime_close_still_updates_memory_without_save(self):
        """补偿通道不受影响：交易所动作已发生时，调用方用 force_runtime_* 强制内存反映现实。"""
        with tempfile.TemporaryDirectory() as tmp:
            ts = self._ts_with_failing_save(tmp)
            ts.add_open_position(
                'BTCUSDT', 'long', 60000.0, 0.1, 55000.0,
                'stop-1', strategy='ma_cross')
            closed = ts.force_runtime_close_position('BTCUSDT', 62000.0)
            self.assertIsNotNone(closed)
            self.assertIsNone(ts.get_open_position('BTCUSDT'))


if __name__ == '__main__':
    unittest.main()
