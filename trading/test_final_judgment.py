"""时间边界 / 真实并发混沌 / 灾难恢复 / 风控数学性质 / 孤儿仓告警。

这些回归补充常规行为测试不易覆盖的系统级维度；纯标准库，本机可跑。
"""
import json
import os
import random
import tempfile
import threading
import unittest
from datetime import datetime
from types import SimpleNamespace

import _test_stubs

main = _test_stubs.import_main()
TradingSystem = main.TradingSystem
import equity_tracker as eqt  # noqa: E402
from risk_manager import RiskManager  # noqa: E402
from trade_state import TradeState, TradeStatePersistenceError  # noqa: E402


class TimeBoundaryTest(unittest.TestCase):
    """求索指数切日（08:00）与跨年边界的归属正确性。"""

    def _tracker(self, tmp):
        system = SimpleNamespace(exchange_api=None, trade_state=None)
        return eqt.EquityTracker(tmp, system)

    def test_rollover_boundary_belongs_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = self._tracker(tmp)
            # 07:59:59 归前一交易日；08:00:00 整点起归当日
            self.assertEqual(t._qiusuo_trading_day(datetime(2026, 6, 10, 7, 59, 59)), '2026-06-09')
            self.assertEqual(t._qiusuo_trading_day(datetime(2026, 6, 10, 8, 0, 0)), '2026-06-10')

    def test_new_year_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = self._tracker(tmp)
            # 元旦凌晨 00:30 仍属去年 12-31 的交易日（未到 08:00 切日）
            self.assertEqual(t._qiusuo_trading_day(datetime(2027, 1, 1, 0, 30)), '2026-12-31')

    def test_leap_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = self._tracker(tmp)
            self.assertEqual(t._qiusuo_trading_day(datetime(2028, 2, 29, 12, 0)), '2028-02-29')

    def test_tick_bucket_alignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = self._tracker(tmp)
            bucket = t._equity_tick_bucket(datetime(2026, 6, 10, 23, 58, 47))
            self.assertEqual((bucket.hour, bucket.minute, bucket.second), (23, 55, 0))


class ConcurrencyChaosTest(unittest.TestCase):
    """真实多线程压榨 TradeState：终态必须自洽、文件必须可解析、无任何异常逃逸。"""

    def test_parallel_lifecycle_consistency(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = TradeState(os.path.join(tmp, 'trade_state.json'))
            errors = []
            N_THREADS, N_OPS = 8, 30

            def worker(tid):
                try:
                    for i in range(N_OPS):
                        sym = f'T{tid}S{i}USDT'
                        ts.add_open_position(sym, 'long', 100.0, 1.0, 90.0, f'stop-{tid}-{i}', strategy='ma_cross')
                        ts.update_stop_loss(sym, 95.0, f'stop2-{tid}-{i}')
                        if i % 2 == 0:
                            ts.close_position(sym, 110.0)
                        if i % 5 == 0:
                            ts.mark_stop_residue(sym)
                            ts.clear_stop_residue(sym)
                except Exception as e:  # 任何异常逃逸都是失败
                    errors.append((tid, repr(e)))

            threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()

            self.assertEqual(errors, [])
            # 终态自洽：开仓总数 = 留存 + 已平；半数平仓
            opened = N_THREADS * N_OPS
            still_open = len(ts.get_all_open_positions())
            closed = len(ts.get_closed_trades())
            self.assertEqual(still_open + closed, opened)
            self.assertEqual(closed, N_THREADS * (N_OPS // 2))
            # 磁盘文件必须是合法 JSON 且与内存一致
            with open(os.path.join(tmp, 'trade_state.json')) as f:
                on_disk = json.load(f)
            self.assertEqual(len(on_disk['open_positions']), still_open)


class DisasterRecoveryTest(unittest.TestCase):
    """状态文件灾难场景（fail-closed 账本语义）：
    主文件损坏 → 拒绝自动提升上一代 .bak；主备全毁 → 拒绝启动；
    主文件被删但 .bak 在 → 拒绝启动；
    主备都不存在 → 全新部署正常启动；人工仓/账本彻底丢失 → 孤儿仓告警兜底。"""

    def test_corrupted_main_refuses_stale_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            ts = TradeState(path)
            ts.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, 'stop-1', strategy='ma_cross')
            ts.update_stop_loss('BTCUSDT', 56000.0, 'stop-2')  # 第二次保存 → .bak 含持仓
            with open(path, 'w') as f:
                f.write('{“损坏的JSON')

            with self.assertRaisesRegex(
                    TradeStatePersistenceError, '不自动提升'):
                TradeState(path)

    def test_corrupt_main_never_drops_newer_open_intent_from_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            state = TradeState(path)
            state.save_state()
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'INewerIntent1',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=1.0)
            with open(path + '.bak', encoding='utf-8') as handle:
                self.assertEqual({}, json.load(handle)['open_intents'])
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('{broken')

            with self.assertRaises(TradeStatePersistenceError):
                TradeState(path)

    def test_foreign_owner_main_is_never_overwritten_by_okx_backup(self):
        """主账本可严格解析且显式属于 Binance 时，归属冲突不是损坏。
        即使 .bak 是完整 OKX 账本，也必须拒绝启动且不得重写主文件。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            foreign = TradeState.get_default_state()
            foreign['exchange'] = 'binance'
            okx_backup = TradeState.get_default_state()
            okx_backup['exchange'] = 'okx'
            with open(path, 'w') as f:
                json.dump(foreign, f)
            with open(path + '.bak', 'w') as f:
                json.dump(okx_backup, f)

            with open(path, 'rb') as f:
                before_bytes = f.read()
            before_inode = os.stat(path).st_ino
            with self.assertRaisesRegex(
                    TradeStatePersistenceError, '归属冲突'):
                TradeState(path)

            self.assertEqual(os.stat(path).st_ino, before_inode)
            with open(path, 'rb') as f:
                self.assertEqual(f.read(), before_bytes)

    def test_corrupted_main_and_backup_refuses_startup(self):
        """主备全毁：账本曾存在却无法确认记录过什么 → 抛异常拒绝启动，
        绝不以空状态「失忆」运行（失忆后日检会对有真实仓位的品种重复开仓）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            ts = TradeState(path)
            ts.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, 'stop-1', strategy='ma_cross')
            ts.update_stop_loss('BTCUSDT', 56000.0, 'stop-2')
            for p in (path, path + '.bak'):
                with open(p, 'w') as f:
                    f.write('{“损坏的JSON')

            with self.assertRaises(TradeStatePersistenceError):
                TradeState(path)

    def test_corrupted_main_without_backup_refuses_startup(self):
        """主文件损坏且从无备份：同样拒绝启动。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            with open(path, 'w') as f:
                f.write('{“损坏的JSON')

            with self.assertRaises(TradeStatePersistenceError):
                TradeState(path)

    def test_missing_main_starts_fresh(self):
        """主文件不存在：全新部署，正常以空状态启动（不误伤新装机）。"""
        with tempfile.TemporaryDirectory() as tmp:
            ts = TradeState(os.path.join(tmp, 'trade_state.json'))
            self.assertEqual(ts.get_all_open_positions(), {})

    def test_missing_main_with_stray_backup_refuses_startup(self):
        """主文件被删但 .bak 仍在（疑似误删）：拒绝启动——不自动恢复（.bak 可能落后于
        被删主文件，静默复活等于捏造持仓），也不空启动（失忆），留人工显式二选一。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            ts = TradeState(path)
            ts.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, 'stop-1', strategy='ma_cross')
            ts.update_stop_loss('BTCUSDT', 56000.0, 'stop-2')  # 生成 .bak
            os.remove(path)

            with self.assertRaises(TradeStatePersistenceError):
                TradeState(path)

    def test_reset_requires_removing_backup_too(self):
        """有意重置：主文件与 .bak 一并删除后，方可全新启动。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            ts = TradeState(path)
            ts.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, 'stop-1', strategy='ma_cross')
            ts.update_stop_loss('BTCUSDT', 56000.0, 'stop-2')
            os.remove(path)
            os.remove(path + '.bak')

            fresh = TradeState(path)
            self.assertEqual(fresh.get_all_open_positions(), {})

    def test_orphan_position_alert_on_total_loss(self):
        """账本丢失（文件不存在）或存在人工仓时：启动同步的反向核对告警
        「交易所有仓本地无记录」，并逐品种持久化隔离阻断后续开仓。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))  # 空（模拟全毁后）
            system._stop_anomalies = {}
            alerts = []
            system.notifier = SimpleNamespace(notify_error=lambda m: alerts.append(m) or True)
            system.exchange_api = SimpleNamespace(
                to_ccxt_symbol=lambda s: s,
                get_position=lambda s: None,
                list_position_symbols=lambda: ['BTCUSDT', 'ETHUSDT'])  # 交易所仍有真实仓

            system.sync_positions_on_startup()

            self.assertEqual(len(alerts), 2)
            self.assertEqual(
                set(system.trade_state.get_position_quarantines()),
                {'BTCUSDT', 'ETHUSDT'})

    def test_no_orphan_no_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.add_open_position('BTCUSDT', 'long', 60000.0, 0.1, 55000.0, strategy='ma_cross')
            system._stop_anomalies = {}
            alerts = []
            system.notifier = SimpleNamespace(notify_error=lambda m: alerts.append(m) or True)
            system.exchange_api = SimpleNamespace(
                to_ccxt_symbol=lambda s: s,
                _coin_to_contracts=lambda s, amount: 10.0,
                _contracts_to_coins=lambda s, contracts: contracts / 100.0,
                get_position=lambda s: {'contracts': 10.0, 'side': 'long'},
                find_stop_order_state=lambda *args, **kwargs: 'intact',
                list_position_symbols=lambda: ['BTCUSDT'])  # 两边一致

            system.sync_positions_on_startup()

            self.assertEqual(alerts, [])


class RiskInvariantTest(unittest.TestCase):
    """以损定量的数学性质：随机 2000 组参数下，实际风险绝不实质性超出预期风险。"""

    def test_position_size_never_exceeds_risk_budget(self):
        rng = random.Random(20260612)  # 固定种子，可复现
        for _ in range(2000):
            equity = rng.uniform(100, 1_000_000)
            entry = rng.choice([rng.uniform(0.00001, 0.01),   # SHIB 类极小价
                                rng.uniform(0.1, 100),
                                rng.uniform(1000, 1_000_000)])  # BTC 类极大价
            stop = entry * (1 - rng.uniform(0.005, 0.5))        # 多单止损在下方
            risk = rng.uniform(0.001, 0.5)
            rm = RiskManager(equity)

            size = rm.calculate_position_size(entry, stop, risk)

            self.assertGreaterEqual(size, 0)
            if size > 0:
                actual_risk = (entry - stop) * size
                budget = equity * risk
                tolerance = max(1e-9, budget * 1e-12)
                self.assertLessEqual(actual_risk, budget + tolerance,
                                     f'风险超预算: equity={equity}, entry={entry}, stop={stop}, risk={risk}')

    def test_degenerate_inputs_yield_zero(self):
        rm = RiskManager(10000)
        self.assertEqual(rm.calculate_position_size(100, 100, 0.01), 0)  # 止损=入场
        self.assertEqual(rm.calculate_position_size(0, 90, 0.01), 0)     # 零价格

    def test_position_size_keeps_sub_milli_precision_for_exchange_rounding(self):
        rm = RiskManager(1000)

        size = rm.calculate_position_size(50000, 36242.09078404402, 0.01)

        self.assertGreater(size, 0)
        self.assertLess(size, 0.001)
        self.assertNotEqual(size, round(size, 3))


if __name__ == '__main__':
    unittest.main()
