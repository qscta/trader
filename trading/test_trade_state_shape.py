"""账本形状校验单测：json.load 成功 ≠ 是账本。

顶层被误改成数组、或关键段类型错误的文件若放行，会在运行中以裸
KeyError/TypeError 崩溃——绕过 fail-closed 精心准备的人工修复指引。
形状非法必须与解析失败同等对待：走备份恢复，仍失败则拒启。
缺键按默认补齐（兼容老版本账本），不误伤。
"""
import json
import os
import tempfile
import unittest

from trade_state import TradeState, TradeStatePersistenceError


class StateShapeValidationTest(unittest.TestCase):
    def _make(self, main_data=None, backup_data=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'trade_state.json')
        if main_data is not None:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(main_data, f)
        if backup_data is not None:
            with open(path + '.bak', 'w', encoding='utf-8') as f:
                json.dump(backup_data, f)
        return path

    def test_top_level_array_rejected(self):
        """顶层是数组（合法 JSON、非法账本）且无备份：fail-closed 拒启。"""
        path = self._make(main_data=['not', 'a', 'ledger'])
        with self.assertRaises(TradeStatePersistenceError):
            TradeState(path)

    def test_wrong_section_type_rejected(self):
        """open_positions 被改成数组：拒启，不放行到运行中裸崩。"""
        path = self._make(main_data={'open_positions': [], 'closed_trades': []})
        with self.assertRaises(TradeStatePersistenceError):
            TradeState(path)

    def test_missing_sections_backfilled(self):
        """老版本账本缺 closed_trades/signal_states：按默认补齐，不拒启（兼容）。"""
        path = self._make(main_data={'open_positions': {}})
        ts = TradeState(path)
        self.assertEqual(ts.state['closed_trades'], [])
        self.assertEqual(ts.state['signal_states'], {})

    def test_shape_corrupt_main_recovers_from_backup(self):
        """主文件形状非法、备份完好：与解析失败同一恢复路径，从备份加载。"""
        good = {'open_positions': {'BTCUSDT': {'symbol': 'BTCUSDT'}},
                'closed_trades': [], 'signal_states': {}}
        path = self._make(main_data=['corrupted'], backup_data=good)
        ts = TradeState(path)
        self.assertIn('BTCUSDT', ts.state['open_positions'])

    def test_shape_corrupt_backup_still_fail_closed(self):
        """主、备形状都非法：拒启（绝不失忆运行）。"""
        path = self._make(main_data=['bad'], backup_data={'open_positions': 42})
        with self.assertRaises(TradeStatePersistenceError):
            TradeState(path)


class ClearSignalStateTest(unittest.TestCase):
    """品种出池后的信号状态清理：删除生效且持久化；不存在的品种为无害空操作。"""

    def _state(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'trade_state.json')
        return TradeState(path), path

    def test_clear_removes_and_persists(self):
        ts, path = self._state()
        ts.set_signal_state('BTCUSDT', True)
        ts.clear_signal_state('BTCUSDT')
        self.assertNotIn('BTCUSDT', ts.state['signal_states'])
        with open(path, encoding='utf-8') as f:
            on_disk = json.load(f)
        self.assertNotIn('BTCUSDT', on_disk.get('signal_states', {}))

    def test_clear_missing_symbol_is_noop(self):
        ts, path = self._state()
        ts.clear_signal_state('NOPEUSDT')  # 不抛异常
        self.assertFalse(os.path.exists(path))  # 空操作不落盘


if __name__ == '__main__':
    unittest.main()
