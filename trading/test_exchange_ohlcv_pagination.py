"""交易所 K 线分页回归测试。"""

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
    def __init__(self, total=1200):
        self.rows = [[i * DAY_MS, i, i, i, i, i] for i in range(total)]
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
        candidates = self.rows if since is None else [row for row in self.rows if row[0] >= since]
        return candidates[:min(limit, 300)]


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


if __name__ == '__main__':
    unittest.main()
