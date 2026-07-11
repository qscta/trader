"""交易所 K 线分页回归测试。

测试桩必须模拟 OKX 的真实窗口语义：带 ``since`` 的请求只返回
``[since, since + limit×timeframe)`` 时间窗内的数据——上市前的窗口返回空页、
数据不满的窗口返回短页。2026-07-11 生产事故（上市不满一年的品种拿到截至
now-67 天的陈旧历史，指标全部失真）正是旧桩用「since 起全部数据」的简化语义
掩盖的：短页被误判为「历史已取完」。本文件的回归用例按事故形态命名锁定。
"""

import sys
import types
import unittest

# 本文件属于零依赖 discover 套件；只测试分页编排，不应要求安装 ccxt/pandas。
_saved = {name: sys.modules.get(name) for name in ('ccxt', 'pandas', 'exchange_base')}
_ccxt = types.ModuleType('ccxt')
for _name in (
        'RequestTimeout', 'NetworkError', 'ExchangeNotAvailable', 'DDoSProtection',
        'RateLimitExceeded', 'InsufficientFunds', 'InvalidOrder', 'BadRequest',
        'AuthenticationError', 'PermissionDenied', 'BadSymbol'):
    setattr(_ccxt, _name, type(_name, (Exception,), {}))
sys.modules['ccxt'] = _ccxt
sys.modules['pandas'] = types.ModuleType('pandas')
sys.modules.pop('exchange_base', None)
try:
    from exchange_base import ExchangeApi
except ImportError:  # pragma: no cover - 从仓库根目录运行时
    from trading.exchange_base import ExchangeApi
for _name, _module in _saved.items():
    if _module is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _module


DAY_MS = 86_400_000


class _FakeExchange:
    """OKX 窗口语义的假交易所。

    行情从 ``first_day`` 开始（模拟上市日），到 ``total - 1`` 天为止（当下）。
    带 since 的请求只返回 [since, since + limit×tf) 窗口内的数据；
    不带 since 的请求返回最近 limit 根。两者单页都不超过 300。
    """

    def __init__(self, total=1200, first_day=0):
        self.rows = [[i * DAY_MS, i, i, i, i, i] for i in range(first_day, total)]
        self.now = (total - 1) * DAY_MS
        self.calls = []

    def parse_timeframe(self, timeframe):
        if timeframe != '1d':
            raise ValueError(timeframe)
        return 86_400

    def milliseconds(self):
        return self.now

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append((symbol, timeframe, since, limit))
        limit = min(limit or 300, 300)
        if since is None:
            return self.rows[-limit:]
        window_end = since + limit * DAY_MS
        return [row for row in self.rows if since <= row[0] < window_end][:limit]


class _Api(ExchangeApi):
    def _create_exchange(self, config):
        return config['exchange']


class OhlcvPaginationTest(unittest.TestCase):
    def test_small_request_keeps_single_call(self):
        exchange = _FakeExchange()
        api = _Api({'exchange': exchange})

        rows = api.fetch_ohlcv('BTC/USDT:USDT', '1d', limit=5)

        self.assertEqual(len(rows), 5)
        self.assertEqual(exchange.calls, [('BTC/USDT:USDT', '1d', None, 5)])

    def test_large_request_is_paginated_and_returns_latest_unique_rows(self):
        exchange = _FakeExchange(total=1200)
        api = _Api({'exchange': exchange})

        rows = api.fetch_ohlcv('BTC/USDT:USDT', '1d', limit=1001)

        self.assertEqual(len(rows), 1001)
        self.assertEqual(rows[0][0], 199 * DAY_MS)
        self.assertEqual(rows[-1][0], 1199 * DAY_MS)
        self.assertGreater(len(exchange.calls), 1)
        self.assertTrue(all(call[3] == 300 for call in exchange.calls))

    def test_short_history_returns_only_real_rows(self):
        exchange = _FakeExchange(total=120)
        api = _Api({'exchange': exchange})

        rows = api.fetch_ohlcv('NEW/USDT:USDT', '1d', limit=503)

        self.assertEqual(len(rows), 120)
        self.assertEqual(len({row[0] for row in rows}), 120)

    def test_recent_listing_short_first_window_must_reach_now(self):
        """CL/ASTER 事故形态：上市 226 天的品种，首窗只有 158 根（短页）。

        旧实现把短页当「历史已取完」终止，返回的历史停在 now-68 天——
        指标计算随之整体陈旧。修复后必须继续推进窗口直到覆盖当下。
        """
        exchange = _FakeExchange(total=1200, first_day=974)
        api = _Api({'exchange': exchange})

        rows = api.fetch_ohlcv('CL/USDT:USDT', '1d', limit=365)

        self.assertEqual(len(rows), 226)              # 上市以来全部 K 线
        self.assertEqual(rows[0][0], 974 * DAY_MS)    # 从上市日开始
        self.assertEqual(rows[-1][0], 1199 * DAY_MS)  # 必须到达当下，绝不陈旧

    def test_infant_listing_empty_first_window_must_not_fail(self):
        """GRAM/VVV 事故形态：上市 24 天的品种，首窗完全落在上市前（空页）。

        旧实现把空页当「没有数据」直接返回空列表——上层报「获取K线数据失败」
        并阻断当日完成标记。修复后空页必须按整窗推进，取到真实的 24 根。
        """
        exchange = _FakeExchange(total=1200, first_day=1176)
        api = _Api({'exchange': exchange})

        rows = api.fetch_ohlcv('GRAM/USDT:USDT', '1d', limit=365)

        self.assertEqual(len(rows), 24)
        self.assertEqual(rows[0][0], 1176 * DAY_MS)
        self.assertEqual(rows[-1][0], 1199 * DAY_MS)


if __name__ == '__main__':
    unittest.main()
