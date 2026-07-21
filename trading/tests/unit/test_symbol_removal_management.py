"""删除交易对后的持仓托管 + 状态归属护栏 单测（纯标准库，可本机运行）。

覆盖 Codex 审查要求：
- 有本地持仓的交易对从配置品种池删除后，check_and_execute_trades 仍会检查该 symbol，
  并按持仓记录的 strategy 选择对应策略托管（当前仅 ma_cross）。
- trade_state.json 的交易所归属护栏：归属不符或来路不明(有持仓无标记)时拒绝启动。
"""
import json
import os
import threading
import tempfile
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from tests.unit import _test_stubs

main_module = _test_stubs.import_main()
TradingSystem = main_module.TradingSystem
import trade_state as trade_state_module
from trade_state import TradeState, TradeStatePersistenceError  # 真实现（纯标准库）


def _build_system(tmpdir, config_symbols):
    """组装一个只够跑 check_and_execute_trades 检查循环的最小系统。"""
    system = TradingSystem.__new__(TradingSystem)
    system.trade_state = TradeState(os.path.join(tmpdir, 'trade_state.json'))
    system.config = {'trading': {'symbols': config_symbols},
                     'strategy': {'default_risk_per_trade': 0.01}}
    system._trade_lock = threading.Lock()
    system._stop_anomalies = {}
    system._known_orphans = set()
    system._last_check_date = None
    system._last_failure_notify_ts = 0
    system._pending_trade_open_notifications = []
    system._pending_trade_close_notifications = []
    system.stop_loss_dates = {}
    system._save_stop_loss_dates = lambda: None
    system.equity_tracker = SimpleNamespace(
        record_daily_equity_snapshot=lambda: None,
        refresh_account_stats_state=lambda: None)
    system.notifier = SimpleNamespace(
        send_message=lambda *a, **k: True,
        notify_error=lambda *a, **k: True)
    system.send_daily_position_summary_if_due = lambda force=False, mark_sent=True: False

    checked = []  # [(symbol, strategy_type, exit_only)]，由策略选择处记录

    def record_strategy(symbol_config):
        checked.append((symbol_config['name'], symbol_config.get('strategy', 'ma_cross'),
                        bool(symbol_config.get('exit_only'))))
        return SimpleNamespace(check_signal=lambda *a, **k: None), symbol_config.get('strategy', 'ma_cross')

    system.get_strategy_for_symbol = record_strategy
    # K线返回空 → 循环对每个 symbol 走到 fetch 后 continue，不进入信号分支
    system.exchange_api = SimpleNamespace(
        to_ccxt_symbol=lambda s: s,
        fetch_ohlcv=lambda *a, **k: [],
        confirm_position_flat=lambda s: True,
        list_position_symbols=lambda: [])
    return system, checked


class RemovedSymbolStillManagedTest(unittest.TestCase):
    def test_removed_symbol_with_position_is_checked_with_its_strategy(self):
        """配置池已删但本地有仓：仍被检查，但明确标记只平不开。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, checked = _build_system(tmp, config_symbols=[])
            system.trade_state.add_open_position(
                'ETHUSDT', 'long', 3000.0, 1.0, 2800.0, strategy='ma_cross')

            system.check_and_execute_trades()

            self.assertEqual(checked, [('ETHUSDT', 'ma_cross', True)])

    def test_pool_symbols_and_held_orphans_are_both_checked(self):
        """配置池品种与已删除但有持仓的品种都进入检查集合，各按各的策略。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, checked = _build_system(
                tmp, config_symbols=[{'name': 'BTCUSDT', 'enabled': True, 'strategy': 'ma_cross'}])
            system.trade_state.add_open_position(
                'ETHUSDT', 'short', 3000.0, 1.0, 3200.0, strategy='ma_cross')

            system.check_and_execute_trades()

            self.assertEqual(sorted(checked), [
                ('BTCUSDT', 'ma_cross', False), ('ETHUSDT', 'ma_cross', True)])

    def test_orphan_position_without_strategy_falls_back_to_ma_cross(self):
        """老仓缺 strategy 字段时按 ma_cross 兜底（当前唯一策略；删除入口已被阻止，此为最后防线）。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, checked = _build_system(tmp, config_symbols=[])
            system.trade_state.add_open_position(
                'LTCUSDT', 'long', 100.0, 5.0, 90.0, strategy=None)

            system.check_and_execute_trades()

            self.assertEqual(checked, [('LTCUSDT', 'ma_cross', True)])

    def test_disabled_symbol_with_position_is_exit_only(self):
        """禁用后已有仓只托管到结束，反向信号不得借翻转重新开仓。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, checked = _build_system(
                tmp, config_symbols=[
                    {'name': 'BTCUSDT', 'enabled': False, 'strategy': 'ma_cross'}])
            system.trade_state.add_open_position(
                'BTCUSDT', 'long', 60000.0, 0.1, 55000.0, strategy='ma_cross')

            system.check_and_execute_trades()

            self.assertEqual(checked, [('BTCUSDT', 'ma_cross', True)])


class PerSymbolIsolationTest(unittest.TestCase):
    def test_one_symbol_failure_does_not_block_others(self):
        """单品种异常只跳过该品种：其余品种照常检查；当日不标记完成（等待重试调度整轮重跑）。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, _checked = _build_system(
                tmp, config_symbols=[
                    {'name': 'BTCUSDT', 'enabled': True, 'strategy': 'ma_cross'},
                    {'name': 'ETHUSDT', 'enabled': True, 'strategy': 'ma_cross'},
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

    def test_all_success_marks_day_done(self):
        """全部品种正常：标记当日已完成，防止重复执行。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, _checked = _build_system(
                tmp, config_symbols=[{'name': 'BTCUSDT', 'enabled': True, 'strategy': 'ma_cross'}])

            system.check_and_execute_trades()

            self.assertEqual(system._last_check_date, date.today().isoformat())

    def test_manual_run_does_not_mark_day_done(self):
        """手动检查不得标记当日完成：00:00–08:00 间手动触发跑的是昨日数据，
        若标记会让当天 08:00 的正式日检被跳过，整日的新信号与止损推进丢失。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, _checked = _build_system(
                tmp, config_symbols=[{'name': 'BTCUSDT', 'enabled': True, 'strategy': 'ma_cross'}])

            system.check_and_execute_trades(manual_run=True)

            self.assertIsNone(system._last_check_date)


class StartupCatchupTest(unittest.TestCase):
    """启动补跑：错过 08:00 调度（如恰在该时段重启）时补跑当日日检，不错过则不跑。"""

    def _system(self, tmp):
        system, _ = _build_system(tmp, config_symbols=[])
        system.config['scheduler'] = {'check_hour': 8, 'check_minute': 0}
        system.label = '欧易'
        calls = []
        system.check_and_execute_trades = lambda: calls.append(1)
        return system, calls

    def test_catchup_runs_when_past_due_and_not_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, calls = self._system(tmp)
            system._run_startup_catchup_check(now=datetime(2026, 7, 3, 15, 0))
            self.assertEqual(calls, [1])

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

    def test_deploy_restart_skip_marks_today_done(self):
        """部署晚间重启可显式跳过启动兜底，避免重启后立刻再跑一轮日检。"""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            system, calls = self._system(tmp)
            with patch.dict(os.environ, {'TRADING_SKIP_STARTUP_CATCHUP_ONCE': '1'}):
                self.assertTrue(system._apply_deploy_restart_skip_catchup(now=datetime(2026, 7, 3, 21, 0)))
            self.assertEqual(system._last_check_date, '2026-07-03')
            system._run_startup_catchup_check(now=datetime(2026, 7, 3, 21, 30))
            self.assertEqual(calls, [])

    def test_deploy_restart_skip_ignored_before_check_time(self):
        """未到今日检查时间的重启：标志必须失效——此时本无兜底可跳，
        若也标记当日已检，当天 08:00 的正点日检会被拦截，整日信号与止损推进丢失。"""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            system, calls = self._system(tmp)
            with patch.dict(os.environ, {'TRADING_SKIP_STARTUP_CATCHUP_ONCE': '1'}):
                self.assertFalse(system._apply_deploy_restart_skip_catchup(now=datetime(2026, 7, 3, 7, 30)))
            self.assertIsNone(system._last_check_date)
            # 正点后兜底照常可跑（标志未生效，没有吞掉当日日检）
            system._run_startup_catchup_check(now=datetime(2026, 7, 3, 8, 30))
            self.assertEqual(calls, [1])

    def test_deploy_restart_skip_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _calls = self._system(tmp)
            self.assertFalse(system._apply_deploy_restart_skip_catchup())
            self.assertIsNone(system._last_check_date)

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
            self.assertEqual(calls, [1])


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
        system.check_and_execute_trades = lambda: calls.append(1)
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
            self.assertEqual(calls, [1])

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
            close_position=lambda *a, **k: {'average': 2900.0},
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

    def test_exit_only_closes_without_reopening_or_t1(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, opened = self._build(tmp, cancel_ok=True)
            system.stop_loss_dates['ETHUSDT'] = '2000-01-01'
            position = system.trade_state.get_open_position('ETHUSDT')
            signal = {'current_close': 2900.0, 'lower_stop': 2800.0, 'upper_stop': 3100.0}

            system._flip_position(
                'ETHUSDT', signal, position, 'long', {'name': 'ETHUSDT'}, exit_only=True)

            self.assertEqual(opened, [])
            self.assertIsNone(system.trade_state.get_open_position('ETHUSDT'))
            self.assertNotIn('ETHUSDT', system.stop_loss_dates)

    def test_exit_only_cancel_residue_still_never_reopens_or_records_t1(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, opened = self._build(tmp, cancel_ok=False)
            position = system.trade_state.get_open_position('ETHUSDT')
            signal = {'current_close': 2900.0, 'lower_stop': 2800.0, 'upper_stop': 3100.0}

            system._flip_position(
                'ETHUSDT', signal, position, 'long', {'name': 'ETHUSDT'}, exit_only=True)

            self.assertEqual(opened, [])
            self.assertEqual(system.stop_loss_dates, {})
            self.assertTrue(system.trade_state.has_stop_residue('ETHUSDT'))


class StateOwnerGuardTest(unittest.TestCase):
    def _system(self, tmpdir):
        system = TradingSystem.__new__(TradingSystem)
        system.exchange_id = 'okx'
        system.label = '欧易'
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

    def test_owned_by_other_exchange_blocks_startup(self):
        """归属为其它交易所(如旧币安)：拒绝启动，防串仓。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.trade_state.claim_owner_exchange('binance')
            with self.assertRaises(RuntimeError):
                system._guard_state_owner()

    def test_unmarked_state_with_positions_blocks_startup(self):
        """有持仓但无归属标记(可能是旧币安遗留)：拒绝启动，要求人工确认。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.trade_state.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, strategy='ma_cross')
            with self.assertRaises(RuntimeError):
                system._guard_state_owner()


class OrphanPreOpenGuardTest(unittest.TestCase):
    """开仓前孤儿仓阻断：交易所端已有本地无记录的持仓时拒绝叠加开仓（真实 TradeState）。"""

    def test_open_blocked_when_unmanaged_exchange_position_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _ = _build_system(tmp, config_symbols=[])
            alerts = []
            system.notifier = SimpleNamespace(notify_error=lambda m: alerts.append(m) or True)
            system.exchange_api.get_position = lambda s: {'contracts': 3.0, 'side': 'long'}
            system.exchange_api.open_position = lambda *a, **k: self.fail("孤儿仓存在时不得开仓")

            system._execute_open('BTCUSDT', 'long', 100.0, 90.0, {'name': 'BTCUSDT'})

            self.assertEqual(len(alerts), 1)
            self.assertIn('孤儿仓', alerts[0])
            self.assertIsNone(system.trade_state.get_open_position('BTCUSDT'))

    def test_open_blocked_when_orphan_query_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _ = _build_system(tmp, config_symbols=[])
            alerts = []
            system.notifier = SimpleNamespace(notify_error=lambda m: alerts.append(m) or True)

            def query_failed(_symbol):
                raise RuntimeError('网络故障')

            system.exchange_api.get_position = query_failed
            system.exchange_api.open_position = lambda *a, **k: self.fail("持仓不可确认时不得开仓")

            system._execute_open('BTCUSDT', 'long', 100.0, 90.0, {'name': 'BTCUSDT'})

            self.assertEqual(len(alerts), 1)
            self.assertIn('无法证明交易所空仓', alerts[0])


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
        system.exchange_api.confirm_position_flat = lambda s: True
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
            self.assertEqual(system.stop_loss_dates, {})  # 已删除品种：不记 T+1

    def test_residue_marked_when_cancel_unconfirmed(self):
        """撤销不可确认：记平照常完成，但标记残留（持久化）阻断该品种后续开仓。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system_with_position(tmp, cancel_ok=False)
            system.reconcile_intraday_stop_losses()
            self.assertIsNone(system.trade_state.get_open_position('BTCUSDT'))
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))
            self.assertEqual(system.stop_loss_dates, {})

    def test_single_empty_response_does_not_erase_local_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system_with_position(tmp, cancel_ok=True)
            system.exchange_api.confirm_position_flat = lambda s: False

            system.reconcile_intraday_stop_losses()

            self.assertIsNotNone(system.trade_state.get_open_position('BTCUSDT'))
            self.assertEqual(system._stop_anomalies['BTCUSDT'], 'flat_unconfirmed')


class IntradayPerSymbolIsolationTest(unittest.TestCase):
    """盘中巡检的单品种隔离：一个品种异常（如止损自愈时面值不可得）不得中断其余品种巡检。"""

    def test_one_symbol_failure_does_not_block_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _ = _build_system(tmp, config_symbols=[])
            system.trade_state.add_open_position('AAAUSDT', 'long', 100.0, 1.0, 90.0, strategy='ma_cross')
            system.trade_state.add_open_position('BBBUSDT', 'long', 100.0, 1.0, 90.0, strategy='ma_cross')
            system.exchange_api.get_position = lambda s: {'contracts': 1}
            system.exchange_api.managed_position_matches = lambda *a, **k: True
            ensured = []

            def ensure(symbol, ccxt_symbol, position, strategy_name):
                if symbol == 'AAAUSDT':
                    raise RuntimeError('模拟止损自愈异常（如面值不可得）')
                ensured.append(symbol)

            system._ensure_stop_order_alive = ensure

            system.reconcile_intraday_stop_losses()  # 不应向外抛异常

            self.assertEqual(ensured, ['BBBUSDT'])  # 后续品种照常巡检


class IntradayOrphanCheckTest(unittest.TestCase):
    """盘中巡检的孤儿仓核对：运行期出现「交易所有仓、本地无记录」（开仓超时后迟到成交/
    人工开仓/状态丢失）时不再等到下次重启才可见——每轮巡检核对，新增告警一次（集合节流）、
    消失即解除、再次出现再告警；核对失败不影响本轮对本地持仓的巡检。"""

    def _system(self, tmp, exchange_side):
        system, _ = _build_system(tmp, config_symbols=[])
        alerts = []
        system.notifier = SimpleNamespace(
            notify_error=lambda m: alerts.append(m) or True,
            send_message=lambda *a, **k: True)
        system.exchange_api.list_position_symbols = lambda: list(exchange_side)
        return system, alerts

    def test_orphan_detected_even_with_zero_local_positions(self):
        """本地零持仓也必须核对（旧实现此时直接 return，孤儿裸仓完全不可见）；
        同一孤儿持续存在不重复轰炸。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, alerts = self._system(tmp, exchange_side=['GHOSTUSDT'])

            system.reconcile_intraday_stop_losses()

            self.assertEqual(len(alerts), 1)
            self.assertIn('GHOSTUSDT', alerts[0])
            system.reconcile_intraday_stop_losses()  # 第二轮巡检：状态未变，不再告警
            self.assertEqual(len(alerts), 1)

    def test_orphan_cleared_then_realerts_on_reappearance(self):
        with tempfile.TemporaryDirectory() as tmp:
            exchange_side = ['GHOSTUSDT']
            system, alerts = self._system(tmp, exchange_side)
            system.reconcile_intraday_stop_losses()
            self.assertEqual(len(alerts), 1)

            exchange_side.clear()                     # 人工处理完成，交易所端消失
            system.reconcile_intraday_stop_losses()
            self.assertEqual(system._known_orphans, set())

            exchange_side.append('GHOSTUSDT')         # 再次出现：新状态进入，再次告警
            system.reconcile_intraday_stop_losses()
            self.assertEqual(len(alerts), 2)

    def test_orphan_alert_failure_retries_until_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _alerts = self._system(tmp, exchange_side=['GHOSTUSDT'])
            delivery_results = iter((False, True))
            attempts = []
            system.notifier.notify_error = (
                lambda msg: attempts.append(msg) or next(delivery_results))

            system.reconcile_intraday_stop_losses()
            self.assertEqual(system._known_orphans, set())
            system.reconcile_intraday_stop_losses()

            self.assertEqual(len(attempts), 2)
            self.assertEqual(system._known_orphans, {'GHOSTUSDT'})

    def test_locally_managed_position_is_not_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, alerts = self._system(tmp, exchange_side=['BTCUSDT'])
            system.trade_state.add_open_position('BTCUSDT', 'long', 100.0, 1.0, 90.0, strategy='ma_cross')
            system.exchange_api.get_position = lambda s: {'contracts': 1}
            system.exchange_api.managed_position_matches = lambda *a, **k: True
            system._ensure_stop_order_alive = lambda *a, **k: None

            system.reconcile_intraday_stop_losses()

            self.assertEqual(alerts, [])

    def test_orphan_query_failure_does_not_break_scan(self):
        """核对查询失败：fail-safe 记日志，本轮对本地持仓的巡检照常进行。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, _alerts = self._system(tmp, exchange_side=[])

            def boom():
                raise RuntimeError('查询失败')

            system.exchange_api.list_position_symbols = boom
            system.trade_state.add_open_position('BTCUSDT', 'long', 100.0, 1.0, 90.0, strategy='ma_cross')
            reconciled = []
            system._reconcile_symbol_intraday = lambda sym, pos, cfgs: reconciled.append(sym)

            system.reconcile_intraday_stop_losses()

            self.assertEqual(reconciled, ['BTCUSDT'])


class ManagedPositionConsistencyTest(unittest.TestCase):
    """人工加减仓/反向后，方向或数量与本地记录不一致时隔离该品种。"""

    def _system(self, tmp):
        system, _ = _build_system(tmp, config_symbols=[])
        system.trade_state.add_open_position(
            'BTCUSDT', 'long', 100.0, 1.0, 90.0, stop_order_id='stop-1', strategy='ma_cross')
        alerts = []
        system.notifier = SimpleNamespace(
            notify_error=lambda m: alerts.append(m) or True,
            send_message=lambda *a, **k: True,
            notify_stop_loss_triggered=lambda *a, **k: True)
        system.exchange_api.get_position = lambda s: {'contracts': 2, 'side': 'long'}
        return system, alerts

    def test_intraday_mismatch_skips_stop_management_and_alerts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, alerts = self._system(tmp)
            system.exchange_api.managed_position_matches = lambda *a, **k: False
            system._ensure_stop_order_alive = lambda *a, **k: self.fail(
                "持仓不一致时不得补挂/管理止损")

            system.reconcile_intraday_stop_losses()
            system.reconcile_intraday_stop_losses()

            self.assertEqual(len(alerts), 1)
            self.assertEqual(system._stop_anomalies['BTCUSDT'], 'position_mismatch')

    def test_daily_mismatch_skips_flip(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, alerts = self._system(tmp)
            system.exchange_api.managed_position_matches = lambda *a, **k: False
            system._flip_position = lambda *a, **k: self.fail("持仓不一致时不得翻转")
            position = system.trade_state.get_open_position('BTCUSDT')

            system.handle_open_position_ma_cross(
                'BTCUSDT', {'action': 'short'}, position, {'name': 'BTCUSDT'}, df=object())

            self.assertEqual(len(alerts), 1)
            self.assertEqual(system._stop_anomalies['BTCUSDT'], 'position_mismatch')

    def test_exact_match_resumes_management(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _alerts = self._system(tmp)
            system._stop_anomalies['BTCUSDT'] = 'position_mismatch'
            system.exchange_api.managed_position_matches = lambda *a, **k: True
            ensured = []
            system._ensure_stop_order_alive = lambda *a, **k: ensured.append(True)

            system.reconcile_intraday_stop_losses()

            self.assertEqual(ensured, [True])
            self.assertNotIn('BTCUSDT', system._stop_anomalies)

class ResidueAutoClearGuardTest(unittest.TestCase):
    """残留自动清理：本地无仓但交易所有仓（疑似人工仓位）时绝不盲撤。"""

    def _system_with_residue(self, tmp, exchange_position):
        system, _ = _build_system(tmp, config_symbols=[])
        system.trade_state.mark_stop_residue('BTCUSDT')
        calls = []
        system.exchange_api.get_position = lambda s: exchange_position
        system.exchange_api.confirm_position_flat = lambda s: exchange_position is None
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

    def test_unconfirmed_flat_never_authorizes_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, calls = self._system_with_residue(tmp, None)
            system.exchange_api.confirm_position_flat = lambda s: False
            system._retry_clear_stop_residues()
            self.assertEqual(calls, [])
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))


class StopSelfHealTest(unittest.TestCase):
    """止损自愈：三态判定（intact 不动 / mismatch 告警人工 / missing 补挂）；不确定时一律不动。"""

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

    def test_residue_marked_symbol_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp, state='missing')
            system.trade_state.mark_stop_residue('BTCUSDT')
            self._run(system)
            self.assertEqual(self.find_calls, [])  # 状态不明，连查询都不做
            self.assertEqual(self.created, [])

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
            confirm_position_flat=lambda s: True,
            cancel_order=lambda s, oid: cancel_calls.append(oid) or True,
            cancel_all_orders=lambda s: True)
        system.notifier = SimpleNamespace(
            notify_error=lambda *a, **k: True,
            notify_stop_loss_triggered=lambda *a, **k: True)
        system.trade_state = SimpleNamespace(
            get_open_position=lambda s: {'symbol': s},
            close_position=self._raise_persistence_error,
            force_runtime_close_position=lambda s, p: {'symbol': s, 'exit_price': p},
            clear_stop_residue=lambda s: None,
            mark_stop_residue=lambda s: None)
        system._execute_open = lambda *a, **k: self.fail("落盘失败后不得重入开仓")
        from unittest.mock import Mock
        system.record_stop_loss = Mock()
        return system, cancel_calls

    @staticmethod
    def _raise_persistence_error(symbol, exit_price):
        raise TradeStatePersistenceError('磁盘故障')

    def test_ma_cross_branch_still_confirms_stop_cancel(self):
        system, cancel_calls = self._system()
        signal = {'current_close': 101}
        position = {'side': 'long', 'stop_loss_price': 99, 'position_size': 2, 'stop_order_id': 'stop-9'}

        system.handle_open_position_ma_cross('ETHUSDT', signal, position, {'name': 'ETHUSDT'}, df=object())

        system.record_stop_loss.assert_called_once_with('ETHUSDT')
        self.assertEqual(cancel_calls, ['stop-9'])

    def test_t1_save_failure_keeps_local_position_and_stop(self):
        from unittest.mock import Mock
        system, cancel_calls = self._system()
        system.trade_state.close_position = Mock(return_value={'symbol': 'ETHUSDT'})
        system.record_stop_loss.side_effect = TradeStatePersistenceError('T+1 磁盘故障')
        signal = {'current_close': 101}
        position = {'side': 'long', 'stop_loss_price': 99, 'position_size': 2, 'stop_order_id': 'stop-9'}

        with self.assertRaises(TradeStatePersistenceError):
            system.handle_open_position_ma_cross(
                'ETHUSDT', signal, position, {'name': 'ETHUSDT'}, df=object())

        system.trade_state.close_position.assert_not_called()
        self.assertEqual(cancel_calls, [])


class StopLossDatePersistenceSafetyTest(unittest.TestCase):
    def _system(self, tmp):
        system = TradingSystem.__new__(TradingSystem)
        system.stop_loss_file = os.path.join(tmp, 'stop_loss_dates.json')
        system.stop_loss_dates = {}
        system.notifier = SimpleNamespace(notify_error=lambda *a, **k: True)
        return system

    def test_existing_corrupt_file_refuses_empty_state_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            with open(system.stop_loss_file, 'w', encoding='utf-8') as f:
                f.write('{broken')
            with self.assertRaises(TradeStatePersistenceError):
                system._load_stop_loss_dates()

    def test_future_date_file_refuses_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            future = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
            with open(system.stop_loss_file, 'w', encoding='utf-8') as f:
                json.dump({'BTCUSDT': future}, f)
            with self.assertRaises(TradeStatePersistenceError):
                system._load_stop_loss_dates()

    def test_save_failure_raises_but_keeps_runtime_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            from unittest.mock import patch, Mock
            with patch.object(main_module, 'atomic_write_json', Mock(return_value=False)):
                with self.assertRaises(TradeStatePersistenceError):
                    system.record_stop_loss('BTCUSDT')
            self.assertTrue(system.is_stop_loss_today('BTCUSDT'))

    def test_clear_failure_restores_runtime_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.stop_loss_dates['BTCUSDT'] = '2000-01-01'
            from unittest.mock import patch, Mock
            with patch.object(main_module, 'atomic_write_json', Mock(return_value=False)):
                with self.assertRaises(TradeStatePersistenceError):
                    system.clear_stop_loss('BTCUSDT')
            self.assertEqual(system.stop_loss_dates['BTCUSDT'], '2000-01-01')

    def test_future_runtime_marker_blocks_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.stop_loss_dates['BTCUSDT'] = (
                date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
            self.assertTrue(system.is_stop_loss_today('BTCUSDT'))


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
                    ts.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, 'stop-1')
            self.assertIsNone(ts.get_open_position('BTCUSDT'))

    def test_update_stop_loss_rolls_back_memory_on_save_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = self._ts_with_failing_save(tmp)
            ts.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, 'stop-1')
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
            ts.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, 'stop-1')
            from unittest.mock import patch, Mock
            with patch.object(trade_state_module, 'atomic_write_json', Mock(return_value=False)):
                with self.assertRaises(TradeStatePersistenceError):
                    ts.close_position('BTCUSDT', 62000.0)
            self.assertIsNotNone(ts.get_open_position('BTCUSDT'))
            self.assertEqual(ts.get_closed_trades(), [])

    def test_force_runtime_close_still_updates_memory_without_save(self):
        """补偿通道不受影响：交易所动作已发生时，调用方用 force_runtime_* 强制内存反映现实。"""
        with tempfile.TemporaryDirectory() as tmp:
            ts = self._ts_with_failing_save(tmp)
            ts.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, 'stop-1')
            closed = ts.force_runtime_close_position('BTCUSDT', 62000.0)
            self.assertIsNotNone(closed)
            self.assertIsNone(ts.get_open_position('BTCUSDT'))

    def test_force_runtime_add_keeps_position_without_writing(self):
        """开仓回滚未确认且磁盘不可写时，当前进程仍必须托管真实残仓。"""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, 'trade_state.json')
            ts = TradeState(state_path)

            position = ts.force_runtime_add_open_position(
                'BTCUSDT', 'long', 60000.0, 0.1, 55000.0,
                'stop-1', strategy='ma_cross')

            self.assertEqual(position, ts.get_open_position('BTCUSDT'))
            self.assertFalse(os.path.exists(state_path))

    def test_force_runtime_residue_marker_blocks_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = self._ts_with_failing_save(tmp)
            ts.force_runtime_mark_stop_residue('BTCUSDT')
            self.assertTrue(ts.has_stop_residue('BTCUSDT'))

    def test_residue_mark_rolls_back_memory_on_save_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = self._ts_with_failing_save(tmp)
            from unittest.mock import patch, Mock
            with patch.object(trade_state_module, 'atomic_write_json', Mock(return_value=False)):
                with self.assertRaises(TradeStatePersistenceError):
                    ts.mark_stop_residue('BTCUSDT')
            self.assertFalse(ts.has_stop_residue('BTCUSDT'))

    def test_residue_clear_rolls_back_memory_on_save_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = self._ts_with_failing_save(tmp)
            ts.mark_stop_residue('BTCUSDT')
            from unittest.mock import patch, Mock
            with patch.object(trade_state_module, 'atomic_write_json', Mock(return_value=False)):
                with self.assertRaises(TradeStatePersistenceError):
                    ts.clear_stop_residue('BTCUSDT')
            self.assertTrue(ts.has_stop_residue('BTCUSDT'))

    def test_invalid_residue_structure_refuses_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({
                    'open_positions': {}, 'closed_trades': [],
                    'stop_residues': [],
                }, f)
            with self.assertRaises(TradeStatePersistenceError):
                TradeState(path)

    def test_invalid_open_position_fields_refuse_startup(self):
        valid = {
            'symbol': 'BTCUSDT', 'side': 'long', 'entry_price': 60000.0,
            'position_size': 0.1, 'stop_loss_price': 55000.0,
            'stop_order_id': 'stop-1', 'strategy': 'ma_cross',
        }
        bad_cases = {
            'missing_stop': {'stop_loss_price': None},
            'zero_size': {'position_size': 0},
            'wrong_side': {'side': 'both'},
            'crossed_stop': {'stop_loss_price': 61000.0},
            'unknown_strategy': {'strategy': 'unknown'},
        }
        for label, updates in bad_cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, 'trade_state.json')
                position = dict(valid, **updates)
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump({
                        'open_positions': {'BTCUSDT': position},
                        'closed_trades': [],
                    }, f)
                with self.assertRaises(TradeStatePersistenceError):
                    TradeState(path)


class SetPositionStrategyTest(unittest.TestCase):
    def test_backfills_strategy_and_persists(self):
        """老仓缺 strategy 时可补写并持久化（删除前兜底用）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            ts = TradeState(path)
            ts.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, strategy=None)

            updated = ts.set_position_strategy('BTCUSDT', 'ma_cross')

            self.assertEqual(updated['strategy'], 'ma_cross')
            self.assertEqual(ts.get_open_position('BTCUSDT')['strategy'], 'ma_cross')
            with open(path) as f:
                self.assertEqual(json.load(f)['open_positions']['BTCUSDT']['strategy'], 'ma_cross')

    def test_missing_symbol_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = TradeState(os.path.join(tmp, 'trade_state.json'))
            self.assertIsNone(ts.set_position_strategy('NONEXIST', 'ma_cross'))


if __name__ == '__main__':
    unittest.main()
