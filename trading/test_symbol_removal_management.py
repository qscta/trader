"""删除交易对后的持仓托管 + 状态归属护栏 单测（纯标准库，可本机运行）。

覆盖 Codex 审查要求：
- 有本地持仓的交易对从配置品种池删除后，check_and_execute_trades 仍会检查该 symbol，
  并按持仓记录的 strategy 选择对应策略托管（不会错按默认 turtle）。
- trade_state.json 的交易所归属护栏：归属不符或来路不明(有持仓无标记)时拒绝启动。
"""
import json
import os
import threading
import tempfile
import unittest
from datetime import date, datetime
from types import SimpleNamespace

import _test_stubs

TradingSystem = _test_stubs.import_main().TradingSystem
import trade_state as trade_state_module
from trade_state import TradeState, TradeStatePersistenceError  # 真实现（纯标准库）


def _build_system(tmpdir, config_symbols):
    """组装一个只够跑 check_and_execute_trades 检查循环的最小系统。"""
    system = TradingSystem.__new__(TradingSystem)
    system.trade_state = TradeState(os.path.join(tmpdir, 'trade_state.json'))
    system.config = {'trading': {'symbols': config_symbols},
                     'strategy': {'default_risk_per_trade': 0.01}}
    system._trade_lock = threading.Lock()
    system._config_lock = threading.RLock()  # check_and_execute_trades 快照手动池时用（与真实 __init__ 一致）
    system._stop_anomalies = {}
    system._last_check_date = None
    system._last_failure_notify_ts = 0
    system._pending_trade_open_notifications = []
    system._pending_trade_close_notifications = []
    system._pending_stop_loss_updates = []
    system.equity_tracker = SimpleNamespace(
        record_daily_equity_snapshot=lambda: None,
        refresh_account_stats_state=lambda: None)
    system.notifier = SimpleNamespace(
        send_message=lambda *a, **k: True,
        notify_error=lambda *a, **k: True,
        notify_stop_loss_updates_summary=lambda *a, **k: True)
    system.send_daily_position_summary_if_due = lambda force=False: False

    checked = []  # [(symbol, strategy_type)]，由策略选择处记录

    def record_strategy(symbol_config):
        checked.append((symbol_config['name'], symbol_config.get('strategy', 'turtle')))
        return SimpleNamespace(check_signal=lambda *a, **k: None), symbol_config.get('strategy', 'turtle')

    system.get_strategy_for_symbol = record_strategy
    # K线返回空 → 循环对每个 symbol 走到 fetch 后 continue，不进入信号分支
    system.exchange_api = SimpleNamespace(
        to_ccxt_symbol=lambda s: s,
        fetch_ohlcv=lambda *a, **k: [])
    return system, checked


class RemovedSymbolStillManagedTest(unittest.TestCase):
    def test_removed_symbol_with_position_is_checked_with_its_strategy(self):
        """配置池已删但本地有 ma_cross 持仓：仍被检查，且按持仓的 ma_cross 托管。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, checked = _build_system(tmp, config_symbols=[])
            system.trade_state.add_open_position(
                'ETHUSDT', 'long', 3000.0, 1.0, 2800.0, strategy='ma_cross')

            system.check_and_execute_trades()

            self.assertEqual(checked, [('ETHUSDT', 'ma_cross')])

    def test_pool_symbols_and_held_orphans_are_both_checked(self):
        """配置池品种与已删除但有持仓的品种都进入检查集合，各按各的策略。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, checked = _build_system(
                tmp, config_symbols=[{'name': 'BTCUSDT', 'enabled': True, 'strategy': 'turtle'}])
            system.trade_state.add_open_position(
                'ETHUSDT', 'short', 3000.0, 1.0, 3200.0, strategy='ma_cross')

            system.check_and_execute_trades()

            self.assertEqual(sorted(checked), [('BTCUSDT', 'turtle'), ('ETHUSDT', 'ma_cross')])

    def test_orphan_position_without_strategy_falls_back_to_turtle(self):
        """老仓缺 strategy 字段时按 turtle 兜底（删除入口已被阻止，此为最后防线）。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, checked = _build_system(tmp, config_symbols=[])
            system.trade_state.add_open_position(
                'LTCUSDT', 'long', 100.0, 5.0, 90.0, strategy=None)

            system.check_and_execute_trades()

            self.assertEqual(checked, [('LTCUSDT', 'turtle')])


class PerSymbolIsolationTest(unittest.TestCase):
    def test_one_symbol_failure_does_not_block_others(self):
        """单品种异常只跳过该品种：其余品种照常检查；当日不标记完成（等待重试调度整轮重跑）。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, _checked = _build_system(
                tmp, config_symbols=[
                    {'name': 'BTCUSDT', 'enabled': True, 'strategy': 'turtle'},
                    {'name': 'ETHUSDT', 'enabled': True, 'strategy': 'turtle'},
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
                tmp, config_symbols=[{'name': 'BTCUSDT', 'enabled': True, 'strategy': 'turtle'}])

            system.check_and_execute_trades()

            self.assertEqual(system._last_check_date, date.today().isoformat())

    def test_manual_run_does_not_mark_day_done(self):
        """手动检查不得标记当日完成：00:00–08:00 间手动触发跑的是昨日数据，
        若标记会让当天 08:00 的正式日检被跳过，整日的新信号与止损推进丢失。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, _checked = _build_system(
                tmp, config_symbols=[{'name': 'BTCUSDT', 'enabled': True, 'strategy': 'turtle'}])

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
            system._last_check_date = date.today().isoformat()
            system._run_startup_catchup_check(now=datetime(2026, 7, 3, 15, 0))
            self.assertEqual(calls, [])

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
            system._last_check_date = date.today().isoformat()
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
        system.notifier = SimpleNamespace(notify_error=lambda *a, **k: True)
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda s: s,
            exchange=SimpleNamespace(fetch_ticker=lambda s: {'last': 2900.0}),
            get_last_price=lambda s: 2900.0,
            close_position=lambda *a, **k: {'average': 2900.0},
            cancel_order=lambda *a, **k: cancel_ok,
            cancel_all_orders=lambda *a, **k: cancel_ok or None)
        opened = []
        system._execute_open = lambda *a, **k: opened.append(a)
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


class StopUpdateGapGuardTest(unittest.TestCase):
    """撤旧确认与挂新止损之间的缝隙：交易所已无持仓时不得再挂新止损（防孤儿 reduce-only 单）。"""

    def _build(self, tmp, position_after_cancel):
        system = TradingSystem.__new__(TradingSystem)
        system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
        system.trade_state.add_open_position(
            'BTCUSDT', 'long', 50000.0, 0.1, 48000.0, 'stop-1', strategy='turtle')
        system._pending_stop_loss_updates = []
        system.notifier = SimpleNamespace(notify_error=lambda *a, **k: True)
        created = []
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda s: s,
            cancel_order=lambda *a, **k: True,
            get_position=position_after_cancel,
            create_stop_loss_order=lambda *a, **k: created.append(a) or {'id': 'stop-2'})
        return system, created

    def test_no_position_after_cancel_skips_new_stop(self):
        """撤旧确认后交易所已无仓（止损恰在撤销瞬间触发/人工平仓）：不挂新止损。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, created = self._build(tmp, position_after_cancel=lambda s: None)
            position = system.trade_state.get_open_position('BTCUSDT')

            system._update_stop_order('BTCUSDT', position, 49000.0)

            self.assertEqual(created, [])  # 不留孤儿 reduce-only 单
            # 本地保持旧止损记录，交由巡检/日检确认记平
            self.assertEqual(system.trade_state.get_open_position('BTCUSDT')['stop_loss_price'], 48000.0)

    def test_query_failure_still_creates_stop(self):
        """持仓复核查询失败：按持仓仍在处理继续挂新（保护现有仓位优先于避免孤儿单）。"""
        with tempfile.TemporaryDirectory() as tmp:
            def boom(_s):
                raise RuntimeError('查询失败')
            system, created = self._build(tmp, position_after_cancel=boom)
            position = system.trade_state.get_open_position('BTCUSDT')

            system._update_stop_order('BTCUSDT', position, 49000.0)

            self.assertEqual(len(created), 1)  # 仍挂新止损
            self.assertEqual(system.trade_state.get_open_position('BTCUSDT')['stop_loss_price'], 49000.0)

    def test_position_present_updates_stop_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, created = self._build(tmp, position_after_cancel=lambda s: {'contracts': 10})
            position = system.trade_state.get_open_position('BTCUSDT')

            system._update_stop_order('BTCUSDT', position, 49000.0)

            self.assertEqual(len(created), 1)
            self.assertEqual(system.trade_state.get_open_position('BTCUSDT')['stop_loss_price'], 49000.0)
            self.assertEqual(len(system._pending_stop_loss_updates), 1)  # 汇总通知照常入队


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
            system.trade_state.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, strategy='turtle')
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
            system.trade_state.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, strategy='turtle')
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
            'BTCUSDT', 'long', 60000.0, 0.1, 55000.0, stop_order_id='stop-1', strategy='turtle')
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


class IntradayPerSymbolIsolationTest(unittest.TestCase):
    """盘中巡检的单品种隔离：一个品种异常（如止损自愈时面值不可得）不得中断其余品种巡检。"""

    def test_one_symbol_failure_does_not_block_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _ = _build_system(tmp, config_symbols=[])
            system.trade_state.add_open_position('AAAUSDT', 'long', 100.0, 1.0, 90.0, strategy='turtle')
            system.trade_state.add_open_position('BBBUSDT', 'long', 100.0, 1.0, 90.0, strategy='turtle')
            system.exchange_api.get_position = lambda s: {'contracts': 1}
            ensured = []

            def ensure(symbol, ccxt_symbol, position, strategy_name):
                if symbol == 'AAAUSDT':
                    raise RuntimeError('模拟止损自愈异常（如面值不可得）')
                ensured.append(symbol)

            system._ensure_stop_order_alive = ensure

            system.reconcile_intraday_stop_losses()  # 不应向外抛异常

            self.assertEqual(ensured, ['BBBUSDT'])  # 后续品种照常巡检


class ResidueAutoClearGuardTest(unittest.TestCase):
    """残留自动清理：本地无仓但交易所有仓（疑似人工仓位）时绝不盲撤。"""

    def _system_with_residue(self, tmp, exchange_position):
        system, _ = _build_system(tmp, config_symbols=[])
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
    """状态迁移边界：本地持仓状态是命脉，不能被空文件覆盖或绕过。"""

    def _system(self, tmp):
        system = TradingSystem.__new__(TradingSystem)
        system.base_dir = tmp
        return system

    @staticmethod
    def _write(path, positions):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump({'open_positions': positions, 'closed_trades': []}, f)

    def test_empty_root_with_legacy_positions_migrates_with_backup(self):
        """根目录空仓文件 + data/okx 有旧持仓：备份空文件后迁移旧持仓（不许静默跳过）。"""
        with tempfile.TemporaryDirectory() as tmp:
            self._write(os.path.join(tmp, 'trade_state.json'), {})
            legacy = {'BTCUSDT': {'side': 'long', 'entry_price': 60000}}
            self._write(os.path.join(tmp, 'data', 'okx', 'trade_state.json'), legacy)

            self._system(tmp)._migrate_okx_legacy_state()

            with open(os.path.join(tmp, 'trade_state.json')) as f:
                migrated = json.load(f)
            self.assertIn('BTCUSDT', migrated['open_positions'])
            self.assertEqual(migrated.get('exchange'), 'okx')  # 迁移顺带打归属标记
            backups = [n for n in os.listdir(tmp) if n.startswith('trade_state.json.bak.empty.')]
            self.assertEqual(len(backups), 1)

    def test_both_have_positions_blocks_startup(self):
        """两边都有持仓：无法自动裁决，拒绝启动等人工。"""
        with tempfile.TemporaryDirectory() as tmp:
            self._write(os.path.join(tmp, 'trade_state.json'), {'ETHUSDT': {'side': 'short'}})
            self._write(os.path.join(tmp, 'data', 'okx', 'trade_state.json'), {'BTCUSDT': {'side': 'long'}})
            with self.assertRaises(RuntimeError):
                self._system(tmp)._migrate_okx_legacy_state()

    def test_legacy_empty_keeps_root_untouched(self):
        """旧文件无持仓：根目录维持现状，不迁移。"""
        with tempfile.TemporaryDirectory() as tmp:
            self._write(os.path.join(tmp, 'trade_state.json'), {'ETHUSDT': {'side': 'short'}})
            self._write(os.path.join(tmp, 'data', 'okx', 'trade_state.json'), {})

            self._system(tmp)._migrate_okx_legacy_state()

            with open(os.path.join(tmp, 'trade_state.json')) as f:
                self.assertIn('ETHUSDT', json.load(f)['open_positions'])

    def test_unreadable_legacy_blocks_startup(self):
        """旧文件损坏读不出：持仓不明，拒绝启动。"""
        with tempfile.TemporaryDirectory() as tmp:
            self._write(os.path.join(tmp, 'trade_state.json'), {})
            legacy_path = os.path.join(tmp, 'data', 'okx', 'trade_state.json')
            os.makedirs(os.path.dirname(legacy_path), exist_ok=True)
            with open(legacy_path, 'w') as f:
                f.write('{损坏的json')
            with self.assertRaises(RuntimeError):
                self._system(tmp)._migrate_okx_legacy_state()


class StopSelfHealTest(unittest.TestCase):
    """止损自愈：三态判定（intact 不动 / mismatch 告警人工 / missing 补挂）；不确定时一律不动。"""

    def _system(self, tmp, state=None, state_error=None, create_result=None):
        system = TradingSystem.__new__(TradingSystem)
        system.label = '欧易'
        system._stop_anomalies = {}
        system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
        system.trade_state.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0,
                                             stop_order_id='stop-1', strategy='turtle')
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
        system._ensure_stop_order_alive('BTCUSDT', 'BTCUSDT', position, '海龟通道')

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
            cancel_order=lambda s, oid: cancel_calls.append(oid) or True,
            cancel_all_orders=lambda s: True)
        system.notifier = SimpleNamespace(
            notify_error=lambda *a, **k: True,
            notify_stop_loss_triggered=lambda *a, **k: True)
        system.trade_state = SimpleNamespace(
            get_open_position=lambda s: {'symbol': s},
            close_position=self._raise_persistence_error,
            force_runtime_close_position=lambda s, p: {'symbol': s, 'exit_price': p},
            set_signal_state=lambda *a: None,
            get_signal_state=lambda s: True,
            clear_stop_residue=lambda s: None,
            mark_stop_residue=lambda s: None)
        system.handle_open_signal_turtle = lambda *a, **k: self.fail("落盘失败后不得反手开仓")
        system._execute_open = lambda *a, **k: self.fail("落盘失败后不得重入开仓")
        system.record_stop_loss = lambda s: self.fail("落盘失败后不得记录 T+1")
        return system, cancel_calls

    @staticmethod
    def _raise_persistence_error(symbol, exit_price):
        raise TradeStatePersistenceError('磁盘故障')

    def test_turtle_branch_still_confirms_stop_cancel(self):
        system, cancel_calls = self._system()
        signal = {'action': 'short', 'mid_line_crossed': True, 'current_close': 38, 'mid_line': 56}
        position = {'side': 'long', 'stop_loss_price': 40, 'position_size': 1, 'stop_order_id': 'stop-1'}

        system.handle_open_position_turtle('BTCUSDT', signal, position, {'name': 'BTCUSDT'})

        self.assertEqual(cancel_calls, ['stop-1'])  # 撤旧止损确认必须执行

    def test_ma_cross_branch_still_confirms_stop_cancel(self):
        system, cancel_calls = self._system()
        signal = {'current_close': 101}
        position = {'side': 'long', 'stop_loss_price': 99, 'position_size': 2, 'stop_order_id': 'stop-9'}

        system.handle_open_position_ma_cross('ETHUSDT', signal, position, {'name': 'ETHUSDT'}, df=object())

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
            self.assertIsNone(ts.set_position_strategy('NONEXIST', 'turtle'))


if __name__ == '__main__':
    unittest.main()
