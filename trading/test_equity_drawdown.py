"""未创新高天数 / 历史最长未创新高天数 统计口径单测（只依赖标准库，可本机运行）。"""
import os
import sys
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import equity_tracker as eqt


def _make(equity, peak, peak_days_ago, longest):
    tmp = tempfile.mkdtemp()
    pt = (datetime.now() - timedelta(days=peak_days_ago)).isoformat()
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


class EquitySyncRollbackTest(unittest.TestCase):
    def test_midway_failure_restores_already_written_files(self):
        """equity_sync 跨多文件写入：求索指数状态写失败时，已重置的峰值/权益历史
        必须回滚到同步前的落盘原状，不留「峰值已重置、除数未更新」的撕裂状态。"""
        tmp, t = _make(equity=120, peak=100, peak_days_ago=5, longest=3)
        original_peak = _jload(os.path.join(tmp, 'peak_equity.json'))
        original_hist = _jload(os.path.join(tmp, 'equity_history.json'))
        # 先让指数状态初始化成功，再在 equity_sync 的写入步骤上注入失败
        t.ensure_qiusuo_index_state(current_equity=100, persist=True)
        original_qiusuo = _jload(os.path.join(tmp, 'qiusuo_index.json'))

        real_save = t.save_qiusuo_index_state
        calls = {'n': 0}

        def fail_on_sync_write(data):
            calls['n'] += 1
            # 第 1 次来自 equity_sync 开头 ensure_qiusuo_index_state 的前置持久化（放行），
            # 第 2 次才是同步流程真正的新除数写入——在这里失败，此时峰值/权益历史已被
            # 重置写盘，逼出回滚路径
            if calls['n'] == 2:
                return False
            return real_save(data)

        t.save_qiusuo_index_state = fail_on_sync_write
        with self.assertRaises(RuntimeError):
            t.equity_sync()

        self.assertEqual(_jload(os.path.join(tmp, 'peak_equity.json')), original_peak)
        self.assertEqual(_jload(os.path.join(tmp, 'equity_history.json')), original_hist)
        self.assertEqual(_jload(os.path.join(tmp, 'qiusuo_index.json')), original_qiusuo)


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
        d = t.build_account_stats(persist=True)
        self.assertEqual(d['days_since_peak'], 0)
        self.assertEqual(d['longest_drawdown_days'], 5)
        hist = _jload((os.path.join(tmp, 'equity_history.json')))
        self.assertEqual(hist['longest_drawdown_days'], 5)

    def test_new_high_keeps_longer_history(self):
        """创新高但旧周期(2)短于历史最长(10)：历史最长保留 10。"""
        _, t = _make(equity=110, peak=100, peak_days_ago=2, longest=10)
        d = t.build_account_stats(persist=True)
        self.assertEqual(d['days_since_peak'], 0)
        self.assertEqual(d['longest_drawdown_days'], 10)

    def test_tick_advancing_peak_still_settles_longest_streak(self):
        """生产时序：5 分钟权益采样先把新峰值落盘，随后的统计刷新已看不到「刚创新高」——
        结算已下沉到 reconcile_peak_equity，刚结束的未创新高周期(5)不得漏记。"""
        tmp, t = _make(equity=110, peak=100, peak_days_ago=5, longest=3)
        self.assertTrue(t.record_equity_tick(equity=110))   # 采样先推进峰值
        hist = _jload(os.path.join(tmp, 'equity_history.json'))
        self.assertEqual(hist['longest_drawdown_days'], 5)  # 采样路径已结算
        d = t.build_account_stats(persist=True)             # 统计刷新时峰值已是新高
        self.assertEqual(d['days_since_peak'], 0)
        self.assertEqual(d['longest_drawdown_days'], 5)


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
