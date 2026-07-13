"""未创新高天数 / 历史最长未创新高天数 统计口径单测（只依赖标准库，可本机运行）。"""
import os
import sys
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import equity_tracker as eqt


def _make(equity, peak, peak_days_ago, longest):
    tmp = tempfile.mkdtemp()
    now = datetime.now()
    current_close = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now < current_close:
        current_close -= timedelta(days=1)
    pt = (current_close - timedelta(days=peak_days_ago)).isoformat()
    _jdump({'peak_equity': peak, 'peak_time': pt}, os.path.join(tmp, 'peak_equity.json'))
    _jdump({'longest_drawdown_days': longest, 'initial_equity': peak, 'initial_time': pt,
            'year_start_equity': peak, 'year_start_time': pt}, os.path.join(tmp, 'equity_history.json'))
    system = SimpleNamespace(
        exchange_api=SimpleNamespace(
            get_balance=lambda: {'total': {'USDT': equity}, 'free': {'USDT': equity}},
            to_ccxt_symbol=lambda s: s,
            exchange=SimpleNamespace(fetch_ticker=lambda s: {'last': 1})),
        trade_state=SimpleNamespace(get_all_open_positions=lambda: {}))
    return tmp, eqt.EquityTracker(tmp, system)



def _jload(path):
    with open(path) as f:
        return json.load(f)


def _jdump(data, path):
    with open(path, 'w') as f:
        json.dump(data, f)


class CoercePositiveFloatTest(unittest.TestCase):
    def test_rejects_nonfinite_values(self):
        for bad in ("inf", "-inf", "nan", float('inf'), float('nan')):
            self.assertIsNone(eqt._coerce_positive_float(bad), msg=repr(bad))

    def test_accepts_positive_finite_values(self):
        self.assertEqual(eqt._coerce_positive_float("123.45"), 123.45)


class DrawdownStatsTest(unittest.TestCase):
    def test_not_new_high_includes_current_streak(self):
        """当前未创新高：历史最长 = max(历史已记录, 当前未创新高天数)。"""
        _, t = _make(equity=90, peak=100, peak_days_ago=5, longest=3)
        d = t.build_account_stats(persist=False)
        self.assertEqual(d['days_since_peak'], 5)
        self.assertEqual(d['longest_drawdown_days'], 5)

    def test_new_high_resets_days_and_closes_streak(self):
        """创新高当天：days_since_peak 重置为 0，且结算旧周期(5)写入历史最长。"""
        tmp, t = _make(equity=110, peak=100, peak_days_ago=5, longest=3)
        t.record_daily_equity_snapshot()
        d = t.build_account_stats(persist=True)
        self.assertEqual(d['days_since_peak'], 0)
        self.assertEqual(d['longest_drawdown_days'], 5)
        hist = _jload((os.path.join(tmp, 'equity_history.json')))
        self.assertEqual(hist['longest_drawdown_days'], 5)

    def test_new_high_keeps_longer_history(self):
        """创新高但旧周期(2)短于历史最长(10)：历史最长保留 10。"""
        _, t = _make(equity=110, peak=100, peak_days_ago=2, longest=10)
        t.record_daily_equity_snapshot()
        d = t.build_account_stats(persist=True)
        self.assertEqual(d['days_since_peak'], 0)
        self.assertEqual(d['longest_drawdown_days'], 10)

    def test_5min_tick_does_not_advance_peak_daily_snapshot_does(self):
        """峰值按「日收盘」推进：5 分钟采样只维护求索指数，绝不 ratchet 峰值/结算周期；
        每日收盘快照才用当日收盘权益结算旧周期(5)并推进峰值。"""
        tmp, t = _make(equity=110, peak=100, peak_days_ago=5, longest=3)
        # 5 分钟采样：峰值与最长回撤都不动
        self.assertTrue(t.record_equity_tick(equity=110))
        self.assertEqual(100, _jload(os.path.join(tmp, 'peak_equity.json'))['peak_equity'])
        self.assertEqual(3, _jload(os.path.join(tmp, 'equity_history.json'))['longest_drawdown_days'])
        # 每日收盘快照（_make 的余额=当日权益 110）：结算旧周期(5)并按日推进峰值
        t.record_daily_equity_snapshot()
        self.assertEqual(110, _jload(os.path.join(tmp, 'peak_equity.json'))['peak_equity'])
        self.assertEqual(5, _jload(os.path.join(tmp, 'equity_history.json'))['longest_drawdown_days'])

    def test_intraday_tick_new_high_does_not_zero_days_since_peak(self):
        """回归（本次修复）：日内 5 分钟采样冒出高于按日峰值的点，不得把「未创新高天数」
        永久清零。旧实现里一次含浮盈的采样会 ratchet 峰值→days_since_peak 卡 0 达 24h。"""
        tmp, t = _make(equity=1000, peak=1000, peak_days_ago=8, longest=8)
        original_peak_time = _jload(
            os.path.join(tmp, 'peak_equity.json'))['peak_time']
        # 日内浮盈把按市值权益冲到 1001（旧实现：此处 ratchet 峰值并把天数清零）
        self.assertTrue(t.record_equity_tick(equity=1001))
        self.assertEqual(1000, _jload(os.path.join(tmp, 'peak_equity.json'))['peak_equity'])
        self.assertEqual(
            original_peak_time,
            _jload(os.path.join(tmp, 'peak_equity.json'))['peak_time'])
        # 浮盈回落到峰值之下后，统计仍如实反映「已 8 天未创新高」
        t.system.exchange_api.get_balance = lambda: {'total': {'USDT': 995}, 'free': {'USDT': 995}}
        d = t.build_account_stats(persist=False)
        self.assertEqual(d['days_since_peak'], 8)
        self.assertEqual(d['longest_drawdown_days'], 8)

    def test_close_below_peak_still_marks_day_and_blocks_afternoon_high(self):
        """判别性回归：08:00 未创新高也必须消费今日节拍。Claude 版本只在
        创新高时写 marker，导致下午浮盈越过旧峰值后仍会落盘。"""
        tmp, tracker = _make(
            equity=990, peak=1000, peak_days_ago=3, longest=3)
        close_time = datetime(2026, 7, 13, 8, 0, 5)
        afternoon = datetime(2026, 7, 13, 15, 0, 0)

        tracker.reconcile_peak_equity(
            990,
            persist=True,
            now=close_time,
            daily_close=True,
        )
        after_close = _jload(os.path.join(tmp, 'peak_equity.json'))
        self.assertEqual(after_close['peak_equity'], 1000)
        self.assertEqual(after_close['peak_observed_day'], '2026-07-13')

        with self.assertRaises(ValueError):
            tracker.reconcile_peak_equity(1050, persist=True, now=afternoon)
        tracker.reconcile_peak_equity(
            1050,
            persist=True,
            now=afternoon,
            daily_close=True,
        )
        after_afternoon = _jload(os.path.join(tmp, 'peak_equity.json'))
        self.assertEqual(after_afternoon['peak_equity'], 1000)
        self.assertEqual(after_afternoon['peak_time'], after_close['peak_time'])

    def test_persistent_stats_refresh_cannot_ratchet_intraday_peak_or_history(self):
        tmp, tracker = _make(
            equity=1050, peak=1000, peak_days_ago=5, longest=3)
        stats = tracker.build_account_stats(persist=True)
        self.assertEqual(stats['peak_equity'], 1050)  # 当前展示仍可显示临时新高
        self.assertEqual(stats['days_since_peak'], 0)
        self.assertEqual(stats['longest_drawdown_days'], 5)
        self.assertEqual(
            _jload(os.path.join(tmp, 'peak_equity.json'))['peak_equity'], 1000)
        self.assertEqual(
            _jload(os.path.join(tmp, 'equity_history.json'))[
                'longest_drawdown_days'],
            3,
        )

    def test_daily_snapshot_is_first_write_wins(self):
        tmp, tracker = _make(
            equity=990, peak=1000, peak_days_ago=3, longest=3)
        tracker.record_daily_equity_snapshot()
        first = _jload(os.path.join(tmp, 'daily_equity.json'))[-1]

        tracker.system.exchange_api.get_balance = lambda: {
            'total': {'USDT': 1200}, 'free': {'USDT': 1200}}
        tracker.record_daily_equity_snapshot()
        second = _jload(os.path.join(tmp, 'daily_equity.json'))[-1]

        self.assertEqual(first['date'], second['date'])
        self.assertEqual(first['equity'], 990)
        self.assertEqual(second['equity'], 990)
        self.assertEqual(
            _jload(os.path.join(tmp, 'peak_equity.json'))['peak_equity'], 1000)

    def test_peak_does_not_advance_when_closed_streak_cannot_persist(self):
        tmp, tracker = _make(equity=110, peak=100, peak_days_ago=5, longest=3)
        tracker.save_equity_history = Mock(return_value=False)

        with self.assertRaises(RuntimeError):
            tracker.reconcile_peak_equity(
                110,
                persist=True,
                now=datetime.now(),
                daily_close=True,
            )

        self.assertEqual(100, _jload(os.path.join(tmp, 'peak_equity.json'))['peak_equity'])


class TickCompactionConcurrencyTest(unittest.TestCase):
    def test_compaction_cannot_overwrite_a_concurrent_new_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = SimpleNamespace(
                exchange_api=SimpleNamespace(),
                trade_state=SimpleNamespace(get_all_open_positions=lambda: {}),
            )
            tracker = eqt.EquityTracker(tmp, system)
            old_tick = {
                'timestamp': '2026-07-10T09:00',
                'recorded_at': '2026-07-10T09:00:01',
                'equity': 1000, 'qiusuo_index': 1853,
            }
            new_tick = {
                'timestamp': '2026-07-11T09:00',
                'recorded_at': '2026-07-11T09:00:01',
                'equity': 1001, 'qiusuo_index': 1854,
            }
            self.assertTrue(tracker.save_equity_ticks([old_tick]))
            loaded = threading.Event()
            release = threading.Event()
            writer_done = threading.Event()
            original_load = tracker.load_equity_ticks

            def paused_load():
                result = original_load()
                if threading.current_thread().name == 'compact':
                    loaded.set()
                    release.wait(2)
                return result

            tracker.load_equity_ticks = paused_load

            def writer():
                with tracker._lock:
                    ticks = original_load()
                    ticks.append(new_tick)
                    tracker.save_equity_ticks(ticks)
                writer_done.set()

            compact = threading.Thread(
                name='compact', target=tracker._compact_closed_ticks,
                kwargs={'now': datetime(2026, 7, 11, 9, 5)})
            compact.start()
            self.assertTrue(loaded.wait(1))
            write_thread = threading.Thread(target=writer)
            write_thread.start()
            time.sleep(0.05)
            self.assertFalse(writer_done.is_set())  # compact 从首次读取起持锁
            release.set()
            compact.join(2)
            write_thread.join(2)

            self.assertTrue(writer_done.is_set())
            self.assertEqual(
                ['2026-07-11T09:00'],
                [item['timestamp'] for item in original_load()])


class EquitySyncFlowTest(unittest.TestCase):
    """资金变动同步：提供净变动金额时按「变动前权益 ÷ 旧除数」精确锚定，不受点击时机影响。"""

    def _tracker(self, tmp, equity=1000.0):
        bal = {'equity': equity}
        system = SimpleNamespace(
            exchange_api=SimpleNamespace(
                get_balance=lambda: {'total': {'USDT': bal['equity']}, 'free': {'USDT': bal['equity']}}),
            trade_state=SimpleNamespace(get_all_open_positions=lambda: {}))
        return eqt.EquityTracker(tmp, system), bal

    def test_flow_amount_corrects_polluted_tick(self):
        """入金后 5 分钟采样先跑（指数被旧除数记成暴涨）→ 迟到的同步只要填了净变动，
        锚点仍精确回到真实盈亏轨迹——时效性问题消除。"""
        with tempfile.TemporaryDirectory() as tmp:
            t, bal = self._tracker(tmp)
            self.assertTrue(t.record_equity_tick(equity=1000.0))   # 基线：指数 1853
            self.assertTrue(t.record_equity_tick(equity=1500.0))   # 入金 500 后采样先跑：指数被污染
            self.assertAlmostEqual(t.calculate_qiusuo_index(1500.0), 2779.5, places=4)

            bal['equity'] = 1500.0
            r = t.equity_sync(flow_amount=500.0)                    # 迟到的同步，但填了净变动

            self.assertAlmostEqual(r['qiusuo_index'], 1853.0, places=6)           # 锚点校正回真实轨迹
            self.assertAlmostEqual(t.calculate_qiusuo_index(1500.0), 1853.0, places=6)

    def test_unreasonable_flow_rejected(self):
        """净变动 ≥ 当前权益（反推变动前权益不为正）：拒绝，防正负号/数量级填错。"""
        with tempfile.TemporaryDirectory() as tmp:
            t, _bal = self._tracker(tmp, equity=1500.0)
            t.record_equity_tick(equity=1500.0)
            with self.assertRaises(ValueError):
                t.equity_sync(flow_amount=1500.0)
            with self.assertRaises(ValueError):
                t.equity_sync(flow_amount=2000.0)

    def test_legacy_no_flow_uses_latest_tick_anchor(self):
        """留空净变动 = 旧行为：以最近已记录指数为锚——在采样跑过之前及时点击仍然正确。"""
        with tempfile.TemporaryDirectory() as tmp:
            t, bal = self._tracker(tmp)
            t.record_equity_tick(equity=1000.0)     # 指数 1853；入金到账但采样尚未跑
            bal['equity'] = 1500.0
            r = t.equity_sync()                     # 及时点击（时效窗口内）

            self.assertAlmostEqual(r['qiusuo_index'], 1853.0, places=4)
            self.assertAlmostEqual(t.calculate_qiusuo_index(1500.0), 1853.0, places=4)


if __name__ == '__main__':
    unittest.main()
