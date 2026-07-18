"""交易所 K 线单页边界回归测试。"""

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

    def to_ccxt_symbol(self, symbol):
        return symbol


class OhlcvSinglePageTest(unittest.TestCase):
    def test_small_request_keeps_single_call(self):
        exchange = _FakeExchange()
        api = _Api({'exchange': exchange})

        rows = api.fetch_ohlcv('BTC/USDT:USDT', '1d', limit=5)

        self.assertEqual(len(rows), 5)
        self.assertEqual(exchange.calls, [('BTC/USDT:USDT', '1d', None, 5)])

    def test_single_page_recent_listing_returns_latest_real_rows(self):
        exchange = _FakeExchange(total=1200, first_day=974)
        api = _Api({'exchange': exchange})

        rows = api.fetch_ohlcv('CL/USDT:USDT', '1d', limit=300)

        self.assertEqual(len(rows), 226)
        self.assertEqual(rows[-1][0], 1199 * DAY_MS)
        self.assertEqual(exchange.calls, [('CL/USDT:USDT', '1d', None, 300)])

    def test_request_over_300_is_rejected_before_exchange_call(self):
        exchange = _FakeExchange()
        api = _Api({'exchange': exchange})

        with self.assertRaisesRegex(ValueError, '超过 OKX 单页上限 300'):
            api.fetch_ohlcv('BTC/USDT:USDT', '1d', limit=301)
        self.assertEqual(exchange.calls, [])


if __name__ == '__main__':
    unittest.main()
