"""账本/史书分离单测（纯标准库）。

命脉账本 trade_state.json 只保留最近 KEEP_RECENT_CLOSED 条平仓记录，
超出部分由 compact_closed_trades 按年份搬进 closed_trades_archive_YYYY.json：
- 账本从此恒定大小，每次落盘不再全量重写逐年增长的历史；
- 史书损坏只降级（展示近期、归档暂停），绝不阻断启动、绝不丢账本数据；
- 「史书已写、账本落盘失败回滚」的窄窗口由内容级去重消除重复。
"""
import os
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

import _test_stubs

TradingSystem = _test_stubs.import_main().TradingSystem  # noqa: F841  保证桩机制先行
import trade_state as trade_state_module
from trade_state import TradeState, TradeStatePersistenceError


def _make_state(tmp, keep=5):
    return TradeState(os.path.join(tmp, 'trade_state.json'), keep_recent_closed=keep)


def _close_n(ts, n, start=0):
    """开 n 笔并立即平掉，产生按序的平仓历史（符号 C<i>USDT 可追溯顺序）。"""
    for i in range(start, start + n):
        sym = f'C{i}USDT'
        ts.add_open_position(sym, 'long', 100.0 + i, 1.0, 90.0, strategy='ma_cross')
        ts.close_position(sym, 110.0 + i)


def _symbols(trades):
    return [t['symbol'] for t in trades]


def _year_archive(ts, year=None):
    year = year or str(__import__('datetime').datetime.now().year)
    return os.path.join(ts.archive_dir, f'{ts.archive_prefix}{year}.json')


class CompactionTest(unittest.TestCase):
    def test_no_compact_below_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _make_state(tmp, keep=5)
            _close_n(ts, 3)
            self.assertEqual(ts.compact_closed_trades(), 0)
            self.assertFalse(os.path.exists(_year_archive(ts)))
            self.assertEqual(len(ts.get_closed_trades()), 3)

    def test_compact_moves_oldest_overflow_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _make_state(tmp, keep=5)
            _close_n(ts, 8)
            self.assertEqual(ts.compact_closed_trades(), 3)
            # 账本只剩最近 5 条，史书按原顺序收下最旧 3 条，合并视图完整且有序
            self.assertEqual(len(ts.state['closed_trades']), 5)
            merged = ts.get_closed_trades()
            self.assertEqual(_symbols(merged), [f'C{i}USDT' for i in range(8)])
            archive, ok = ts._read_archive()
            self.assertTrue(ok)
            self.assertEqual(_symbols(archive), ['C0USDT', 'C1USDT', 'C2USDT'])

    def test_merged_view_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _make_state(tmp, keep=5)
            _close_n(ts, 8)
            ts.compact_closed_trades()
            reloaded = _make_state(tmp, keep=5)
            self.assertEqual(_symbols(reloaded.get_closed_trades()),
                             [f'C{i}USDT' for i in range(8)])

    def test_compact_is_idempotent_and_incremental(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _make_state(tmp, keep=5)
            _close_n(ts, 8)
            ts.compact_closed_trades()
            self.assertEqual(ts.compact_closed_trades(), 0)  # 已在窗口内：空转
            _close_n(ts, 4, start=8)  # 再平 4 笔 → 账本 9 条，再归 4 条
            self.assertEqual(ts.compact_closed_trades(), 4)
            self.assertEqual(_symbols(ts.get_closed_trades()),
                             [f'C{i}USDT' for i in range(12)])

    def test_compact_splits_records_by_close_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _make_state(tmp, keep=1)
            ts.state['closed_trades'] = [
                {'symbol': 'OLDUSDT', 'close_time': '2025-12-31T23:59:00'},
                {'symbol': 'NEWUSDT', 'close_time': '2026-01-01T00:01:00'},
                {'symbol': 'RECENTUSDT', 'close_time': '2026-02-01T00:00:00'},
            ]
            ts.save_state()

            self.assertEqual(ts.compact_closed_trades(), 2)
            with open(_year_archive(ts, '2025'), encoding='utf-8') as handle:
                self.assertEqual(_symbols(json.load(handle)), ['OLDUSDT'])
            with open(_year_archive(ts, '2026'), encoding='utf-8') as handle:
                self.assertEqual(_symbols(json.load(handle)), ['NEWUSDT'])
            self.assertEqual(
                _symbols(ts.get_closed_trades()),
                ['OLDUSDT', 'NEWUSDT', 'RECENTUSDT'])

    def test_legacy_archive_remains_readable_with_year_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _make_state(tmp, keep=1)
            with open(ts.archive_file, 'w', encoding='utf-8') as handle:
                json.dump([{'symbol': 'LEGACYUSDT',
                            'close_time': '2024-01-01T00:00:00'}], handle)
            with open(_year_archive(ts, '2025'), 'w', encoding='utf-8') as handle:
                json.dump([{'symbol': 'YEARUSDT',
                            'close_time': '2025-01-01T00:00:00'}], handle)
            ts.state['closed_trades'] = [
                {'symbol': 'RECENTUSDT', 'close_time': '2026-01-01T00:00:00'}]

            self.assertEqual(
                _symbols(ts.get_closed_trades()),
                ['LEGACYUSDT', 'YEARUSDT', 'RECENTUSDT'])


class ArchiveFailSafeTest(unittest.TestCase):
    def test_corrupt_archive_keeps_ledger_and_skips_compaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _make_state(tmp, keep=5)
            _close_n(ts, 8)
            with open(_year_archive(ts), 'w', encoding='utf-8') as f:
                f.write('{"corrupt": tru')  # 半截 JSON
            self.assertEqual(ts.compact_closed_trades(), 0)
            self.assertEqual(len(ts.state['closed_trades']), 8)  # 账本一条不丢
            # 展示降级：史书读不出时只出账本近期记录，不抛异常
            self.assertEqual(len(ts.get_closed_trades()), 8)

    def test_archive_write_failure_keeps_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _make_state(tmp, keep=5)
            _close_n(ts, 8)
            real_write = trade_state_module.atomic_write_json

            def fail_archive_only(filepath, data):
                if filepath == _year_archive(ts):
                    return False
                return real_write(filepath, data)

            with patch.object(trade_state_module, 'atomic_write_json', fail_archive_only):
                self.assertEqual(ts.compact_closed_trades(), 0)
            self.assertEqual(len(ts.state['closed_trades']), 8)
            self.assertEqual(len(ts.get_closed_trades()), 8)

    def test_ledger_save_failure_rolls_back_and_no_duplicates_on_retry(self):
        """窄窗口：史书已写、账本落盘失败回滚——重试不得在史书里产生重复记录。"""
        with tempfile.TemporaryDirectory() as tmp:
            ts = _make_state(tmp, keep=5)
            _close_n(ts, 8)
            with patch.object(TradeState, 'save_state',
                              side_effect=TradeStatePersistenceError('模拟落盘失败')):
                with self.assertRaises(TradeStatePersistenceError):
                    ts.compact_closed_trades()
            # 回滚后账本仍持有全部 8 条；史书已收下 3 条（窄窗口的中间态）
            self.assertEqual(len(ts.state['closed_trades']), 8)
            archive, _ = ts._read_archive()
            self.assertEqual(len(archive), 3)
            # 重试：内容级去重，史书不得出现重复
            self.assertEqual(ts.compact_closed_trades(), 3)
            archive, _ = ts._read_archive()
            self.assertEqual(_symbols(archive), ['C0USDT', 'C1USDT', 'C2USDT'])
            self.assertEqual(_symbols(ts.get_closed_trades()),
                             [f'C{i}USDT' for i in range(8)])

    def test_ordered_overlap_preserves_two_identical_real_trades(self):
        """史书已有第一笔、账本前两笔内容相同：只跳过有序重叠的一笔。"""
        with tempfile.TemporaryDirectory() as tmp:
            ts = _make_state(tmp, keep=1)
            duplicate = {'symbol': 'SAMEUSDT', 'pnl': 1, 'close_time': 'legacy'}
            recent = {'symbol': 'RECENTUSDT', 'pnl': 2, 'close_time': 'new'}
            ts.state['closed_trades'] = [
                copy.deepcopy(duplicate), copy.deepcopy(duplicate), recent]
            ts.save_state()
            with open(_year_archive(ts), 'w', encoding='utf-8') as handle:
                json.dump([duplicate], handle)

            self.assertEqual(2, ts.compact_closed_trades())

            archive, ok = ts._read_archive()
            self.assertTrue(ok)
            self.assertEqual([duplicate, duplicate], archive)

    def test_partial_multi_year_write_retries_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _make_state(tmp, keep=1)
            ts.state['closed_trades'] = [
                {'symbol': 'OLDUSDT', 'close_time': '2025-12-31T00:00:00'},
                {'symbol': 'NEWUSDT', 'close_time': '2026-01-01T00:00:00'},
                {'symbol': 'RECENTUSDT', 'close_time': '2026-02-01T00:00:00'},
            ]
            ts.save_state()
            real_write = trade_state_module.atomic_write_json

            def fail_second_year(filepath, data):
                if filepath == _year_archive(ts, '2026'):
                    return False
                return real_write(filepath, data)

            with patch.object(
                    trade_state_module, 'atomic_write_json', fail_second_year):
                self.assertEqual(ts.compact_closed_trades(), 0)
            self.assertEqual(len(ts.state['closed_trades']), 3)
            self.assertEqual(ts.compact_closed_trades(), 2)
            self.assertEqual(
                _symbols(ts.get_closed_trades()),
                ['OLDUSDT', 'NEWUSDT', 'RECENTUSDT'])


class DailyCheckCompactionWiringTest(unittest.TestCase):
    def test_daily_check_triggers_compaction(self):
        """日检开头触发归档：账本超窗时史书文件应在一轮日检后出现。"""
        from test_symbol_removal_management import _build_system
        with tempfile.TemporaryDirectory() as tmp:
            system, _checked = _build_system(tmp, config_symbols=[])
            system.trade_state.keep_recent_closed = 5
            _close_n(system.trade_state, 8)
            system.check_and_execute_trades()
            self.assertTrue(os.path.exists(_year_archive(system.trade_state)))
            self.assertEqual(len(system.trade_state.state['closed_trades']), 5)
            self.assertEqual(len(system.trade_state.get_closed_trades()), 8)


if __name__ == '__main__':
    unittest.main()
