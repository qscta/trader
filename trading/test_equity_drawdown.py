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


if __name__ == '__main__':
    unittest.main()
