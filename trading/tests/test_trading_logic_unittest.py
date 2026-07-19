import os
import sys
import time
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch
import tempfile
from datetime import date, datetime

APP_DIR = Path(
    os.environ.get(
        "TRADING_SYSTEM_DIR",
        Path(__file__).resolve().parents[1],
    )
)
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-32-bytes-minimum!!")
os.environ.setdefault("TRADING_API_TOKEN", "test-token")

# 测试进程不落生产日志（与 _test_stubs.import_main 同一护栏）：下方 import main 会触发
# logging.basicConfig(handlers=[RotatingFileHandler(trading.log), ...])，它仅在根 logger
# 无 handler 时生效——先挂 NullHandler 让它空转，部署机上跑测试不再污染真实 trading.log
import logging  # noqa: E402

if not logging.getLogger().handlers:
    logging.getLogger().addHandler(logging.NullHandler())

import pandas as pd  # noqa: E402

import threading  # noqa: E402

import api_server  # noqa: E402
import equity_tracker  # noqa: E402
import exchange_base  # noqa: E402
import main  # noqa: E402
import runtime_guard  # noqa: E402
import trade_executor  # noqa: E402
import trade_state  # noqa: E402


def _fake_to_ccxt(symbol):
    """测试用：内部符号 -> 币安式 ccxt 符号（BTCUSDT -> BTC/USDT）。"""
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol


def _prep_system(system, persist=True):
    """给假 system 补上 api_server 单所路由需要的属性（_config_lock / _trade_lock / persist_config / config_file）。"""
    system._config_lock = threading.RLock()
    system._trade_lock = threading.RLock()
    system.persist_config = lambda: persist
    system.config_file = "config.json"
    if not hasattr(system, 'base_dir'):
        system.base_dir = str(APP_DIR)
    return system


class FilterClosedCandlesTests(unittest.TestCase):
    def setUp(self):
        self.api = object.__new__(exchange_base.ExchangeApi)

    def test_drops_last_candle_when_not_closed(self):
        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2026-03-10 00:00:00", "2026-03-11 00:00:00"]
                )
            }
        )
        with patch(
            "exchange_base.time.time",
            return_value=pd.Timestamp("2026-03-11 23:59:59").timestamp(),
        ):
            filtered = self.api.filter_closed_candles(df, "1d")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(
            filtered.iloc[-1]["timestamp"], pd.Timestamp("2026-03-10 00:00:00")
        )

    def test_keeps_last_candle_when_closed_after_grace_window(self):
        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2026-03-10 00:00:00", "2026-03-11 00:00:00"]
                )
            }
        )
        with patch(
            "exchange_base.time.time",
            return_value=pd.Timestamp("2026-03-12 00:00:03").timestamp(),
        ):
            filtered = self.api.filter_closed_candles(df, "1d")
        self.assertEqual(len(filtered), 2)


class OhlcvBoundaryValidationTests(unittest.TestCase):
    """行情入口统一边界校验：坏蜡烛整批拒绝，好数据原样通过。"""

    GOOD = [[1000, 10.0, 12.0, 9.0, 11.0, 100.0],
            [2000, 11.0, 13.0, 10.0, 12.0, 50.0]]

    def test_good_batch_passes_and_bad_candles_reject(self):
        self.assertEqual(
            self.GOOD, exchange_base.ExchangeApi.validate_ohlcv(self.GOOD, "BTC"))
        for bad in (
            None,
            [self.GOOD[0], list(self.GOOD[0])],           # 重复时间戳
            [self.GOOD[1], self.GOOD[0]],                  # 乱序
            [[1000, 10.0, float("nan"), 9.0, 11.0, 1.0]],  # NaN 价格
            [[1000, 10.0, 10.5, 9.0, 11.0, 1.0]],          # 收盘越出高点
            [[1000, 10.0, 12.0, 9.0, 11.0, -1.0]],         # 负成交量
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                exchange_base.ExchangeApi.validate_ohlcv(bad, "BTC")


class MaCrossTPlusOneTests(unittest.TestCase):
    def make_system(self):
        system = object.__new__(main.TradingSystem)
        system._stop_anomalies = {}
        system.stop_loss_dates = {}
        system._save_stop_loss_dates = Mock()
        system._execute_open = Mock()
        # _execute_open 后主流程会确认持仓已形成，返回非 None 避免触发 missing-position 告警
        system.trade_state = SimpleNamespace(get_open_position=Mock(
            return_value={"symbol": "ETHUSDT", "side": "long"}))
        system.notifier = SimpleNamespace(notify_signal_missed=Mock(), notify_error=Mock())
        system.ma_cross_strategy = SimpleNamespace(check_reentry_condition=Mock())
        return system

    def test_same_day_stop_loss_blocks_reentry(self):
        system = self.make_system()
        today = main.date.today().strftime("%Y-%m-%d")
        system.stop_loss_dates["ETHUSDT"] = today

        signal = {"action": None}
        system.handle_no_position_ma_cross("ETHUSDT", signal, {"name": "ETHUSDT"}, df=object())

        system._execute_open.assert_not_called()

    def test_next_day_reentry_opens_and_clears_stop_loss_marker(self):
        system = self.make_system()
        system.stop_loss_dates["ETHUSDT"] = "2000-01-01"
        system.ma_cross_strategy.check_reentry_condition.return_value = (
            True,
            "long",
            {"current_close": 100, "lower_stop": 90, "upper_stop": 110},
        )

        signal = {"action": None}
        system.handle_no_position_ma_cross("ETHUSDT", signal, {"name": "ETHUSDT"}, df=object())

        system._execute_open.assert_called_once_with(
            "ETHUSDT", "long", 100, 90, {"name": "ETHUSDT"}
        )
        self.assertNotIn("ETHUSDT", system.stop_loss_dates)
        system._save_stop_loss_dates.assert_called()

    def test_next_day_reentry_keeps_marker_when_open_fails(self):
        """T+1 重入开仓腿失败（成交后仍无持仓）：保留 T+1 标记，次日再重试重入，
        不放弃「永远在市」（此前无条件删除标记会永久放弃）。"""
        system = self.make_system()
        system.trade_state.get_open_position = Mock(return_value=None)  # 开仓腿失败
        system.stop_loss_dates["ETHUSDT"] = "2000-01-01"
        system.ma_cross_strategy.check_reentry_condition.return_value = (
            True, "long", {"current_close": 100, "lower_stop": 90, "upper_stop": 110},
        )
        signal = {"action": None}
        system.handle_no_position_ma_cross("ETHUSDT", signal, {"name": "ETHUSDT"}, df=object())

        system._execute_open.assert_called_once()
        self.assertIn("ETHUSDT", system.stop_loss_dates)          # 标记保留
        system.notifier.notify_signal_missed.assert_called_once()

    def test_next_day_reentry_rejects_wrong_side_position(self):
        system = self.make_system()
        system.trade_state.get_open_position = Mock(
            return_value={'symbol': 'ETHUSDT', 'side': 'short'})
        system.stop_loss_dates['ETHUSDT'] = '2000-01-01'
        system.ma_cross_strategy.check_reentry_condition.return_value = (
            True, 'long', {
                'current_close': 100, 'lower_stop': 90, 'upper_stop': 110})

        outcome = system.handle_no_position_ma_cross(
            'ETHUSDT', {'action': None}, {'name': 'ETHUSDT'}, df=object())

        self.assertEqual('t1_reentry_failed', outcome)
        self.assertIn('ETHUSDT', system.stop_loss_dates)
        system.notifier.notify_signal_missed.assert_called_once()

    def test_initial_open_failure_does_not_fake_tplus1_stop(self):
        """网络/下单失败不是止损：保留交叉供日内重试，不得伪造 T+1。"""
        system = self.make_system()
        system.trade_state.get_open_position = Mock(return_value=None)  # 开仓腿失败
        signal = {"action": "long", "current_close": 100, "lower_stop": 90, "upper_stop": 110}
        system.handle_no_position_ma_cross("ETHUSDT", signal, {"name": "ETHUSDT"}, df=object())

        system._execute_open.assert_called_once()
        self.assertNotIn("ETHUSDT", system.stop_loss_dates)
        system.notifier.notify_signal_missed.assert_called_once()


class InstantOpenApiTests(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()

    def authenticate(self):
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True

    def make_system(self, existing_position=None):
        trade_state = SimpleNamespace(position=existing_position)

        def get_open_position(_symbol):
            return trade_state.position

        trade_state.get_open_position = get_open_position

        df_marker = [None] * 60
        filter_mock = Mock(return_value=df_marker)
        execute_calls = []

        def execute_open(symbol, side, entry_price, stop_loss_price, symbol_config, buffer_notification=True):
            # 即时开仓路由必须传 buffer_notification=False：它自发专属钉钉，不进日检汇总缓冲
            assert buffer_notification is False, "instant_open 应关闭开仓通知缓冲"
            execute_calls.append((symbol, side, entry_price, stop_loss_price, symbol_config))
            trade_state.position = {
                "side": side,
                "entry_price": entry_price,
                "position_size": 1.23,
                "stop_loss_price": stop_loss_price,
                "stop_order_id": "stop-1",
            }
            return {"status": "opened"}

        exchange_stub = SimpleNamespace(fetch_ticker=Mock(return_value={"last": 123.45}))
        system = SimpleNamespace(
            trade_state=trade_state,
            exchange_api=SimpleNamespace(
                to_ccxt_symbol=_fake_to_ccxt,
                fetch_ohlcv=Mock(return_value=[[1]] * 200),
                ohlcv_to_dataframe=Mock(return_value=df_marker),
                filter_closed_candles=filter_mock,
                exchange=exchange_stub,
                get_last_price=lambda s: float(exchange_stub.fetch_ticker(s)["last"]),
            ),
            ma_cross_strategy=SimpleNamespace(
                check_current_state=Mock(
                    return_value={
                        "action": "long",
                        "upper_stop": 130,
                        "lower_stop": 100,
                    }
                )
            ),
            _execute_open=execute_open,
            _daily_candle_is_fresh=lambda _df, _day: (True, 'today', 'minimum'),
            config={"trading": {"symbols": []}},
            config_file="config.json",
            reload_strategies=Mock(),
            execute_calls=execute_calls,
            label="欧易",
            exchange_id="okx",
        )
        return system

    def test_instant_open_rejects_stale_closed_candle(self):
        self.authenticate()
        fake_system = self.make_system()
        fake_system._daily_candle_is_fresh = (
            lambda _df, _day: (False, '2026-05-05', '2026-07-09'))

        with patch.object(api_server, "trading_system", _prep_system(fake_system)), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.post(
                "/api/instant_open",
                json={"name": "BTCUSDT", "risk_per_trade": 0.01},
            )

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(fake_system.execute_calls, [])

    def test_instant_open_rejects_missing_realtime_price(self):
        self.authenticate()
        fake_system = self.make_system()
        fake_system.exchange_api.get_last_price = Mock(
            side_effect=RuntimeError('ticker unavailable'))

        with patch.object(api_server, "trading_system", _prep_system(fake_system)), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.post(
                "/api/instant_open",
                json={"name": "BTCUSDT", "risk_per_trade": 0.01},
            )

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(fake_system.execute_calls, [])

    def test_instant_open_rejects_when_position_exists(self):
        self.authenticate()
        fake_system = self.make_system(existing_position={"symbol": "BTCUSDT"})

        with patch.object(api_server, "trading_system", _prep_system(fake_system)), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.post(
                "/api/instant_open",
                json={"name": "BTCUSDT", "risk_per_trade": 0.01},
            )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(fake_system.execute_calls, [])

    def test_instant_open_uses_closed_candle_filter_and_executes_trade(self):
        self.authenticate()
        fake_system = self.make_system()

        with patch.object(api_server, "trading_system", _prep_system(fake_system)), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.post(
                "/api/instant_open",
                json={"name": "BTCUSDT", "risk_per_trade": 0.01},
            )

        self.assertEqual(resp.status_code, 200)
        fake_system.exchange_api.filter_closed_candles.assert_called_once()
        self.assertEqual(len(fake_system.execute_calls), 1)
        symbol, side, entry_price, stop_loss_price, symbol_config = fake_system.execute_calls[0]
        self.assertEqual(symbol, "BTCUSDT")
        self.assertEqual(side, "long")
        self.assertEqual(entry_price, 123.45)
        self.assertEqual(stop_loss_price, 100)
        self.assertNotIn("strategy", symbol_config)
        fake_system.exchange_api.fetch_ohlcv.assert_called_once_with("BTC/USDT", "1d", limit=300)

    def test_instant_open_reports_central_t1_block_as_conflict(self):
        self.authenticate()
        fake_system = self.make_system()
        fake_system._execute_open = Mock(return_value={'status': 't1_blocked'})

        with patch.object(
                api_server, 'trading_system', _prep_system(fake_system)), \
                patch.object(api_server, 'send_dingtalk', Mock()):
            resp = self.client.post('/api/instant_open', json={
                'name': 'BTCUSDT', 'risk_per_trade': 0.01})

        self.assertEqual(409, resp.status_code)
        self.assertEqual('t1_blocked', resp.get_json()['outcome_status'])

    def test_instant_open_reports_runtime_gates_as_unavailable(self):
        self.authenticate()
        for outcome_status in ('maintenance_blocked', 'state_blocked'):
            with self.subTest(outcome_status=outcome_status):
                fake_system = self.make_system()
                fake_system._execute_open = Mock(
                    return_value={'status': outcome_status})

                with patch.object(
                        api_server, 'trading_system',
                        _prep_system(fake_system)), patch.object(
                            api_server, 'send_dingtalk', Mock()):
                    resp = self.client.post('/api/instant_open', json={
                        'name': 'BTCUSDT', 'risk_per_trade': 0.01})

                self.assertEqual(503, resp.status_code)
                self.assertEqual(
                    outcome_status, resp.get_json()['outcome_status'])

    def test_instant_open_uses_single_page_at_capacity_boundary(self):
        self.authenticate()
        fake_system = self.make_system()
        fake_system.config = {
            "strategy": {"ma_long_period": 149, "ma_stop_period": 298},
            "trading": {"symbols": []},
        }
        df_marker = [None] * 299
        fake_system.exchange_api.ohlcv_to_dataframe.return_value = df_marker
        fake_system.exchange_api.filter_closed_candles.return_value = df_marker

        with patch.object(api_server, "trading_system", _prep_system(fake_system)), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.post(
                "/api/instant_open",
                json={"name": "BTCUSDT", "risk_per_trade": 0.01},
            )

        self.assertEqual(resp.status_code, 200)
        fake_system.exchange_api.fetch_ohlcv.assert_called_once_with("BTC/USDT", "1d", limit=300)

    def test_instant_open_rejects_explicit_null_risk(self):
        """真钱风险字段显式 null 必须拒绝；字段缺失也不得猜默认值。"""
        self.authenticate()
        fake_system = self.make_system()

        with patch.object(api_server, "trading_system", _prep_system(fake_system)), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.post(
                "/api/instant_open",
                json={"name": "BTCUSDT", "risk_per_trade": None},
            )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(len(fake_system.execute_calls), 0)

    def test_partial_rollback_residual_is_409_not_false_success(self):
        self.authenticate()
        fake_system = self.make_system()

        def incomplete(symbol, side, entry_price, stop_loss_price,
                       symbol_config, buffer_notification=True):
            fake_system.trade_state.position = {
                'side': side, 'entry_price': entry_price,
                'position_size': 0.4, 'stop_loss_price': stop_loss_price,
                'stop_order_id': None,
            }
            return {'status': 'rollback_incomplete', 'position_size': 0.4}

        fake_system._execute_open = incomplete
        dingtalk = Mock()
        with patch.object(
                api_server, 'trading_system', _prep_system(fake_system)), patch.object(
                api_server, 'send_dingtalk', dingtalk):
            resp = self.client.post('/api/instant_open', json={
                'name': 'BTCUSDT', 'risk_per_trade': 0.01})

        self.assertEqual(409, resp.status_code)
        self.assertEqual('quarantined', resp.get_json()['status'])
        self.assertEqual('rollback_incomplete', resp.get_json()['outcome_status'])
        self.assertEqual('BTCUSDT', fake_system.config['trading']['symbols'][0]['name'])
        dingtalk.assert_not_called()


class ProxyfixHopsTests(unittest.TestCase):
    """反代跳数解析：登录防爆破的客户端 IP 还原依赖它，非法值必须拒绝启动（fail-loud）。"""

    def test_valid_hops_parsed(self):
        for value, want in (("0", 0), ("1", 1), ("2", 2), (10, 10), (" 1 ", 1)):
            self.assertEqual(api_server._parse_proxyfix_hops(value), want)

    def test_invalid_hops_rejected(self):
        for bad in ("abc", "", None, -1, 11, "1.5"):
            with self.assertRaises(RuntimeError, msg=f"{bad!r} 应拒绝启动"):
                api_server._parse_proxyfix_hops(bad)

    def test_flask_secret_key_rejects_missing_or_short_values(self):
        for bad in (None, "", "short-secret"):
            with self.assertRaises(RuntimeError):
                api_server._validate_flask_secret_key(bad)
        strong = "x" * 32
        self.assertEqual(api_server._validate_flask_secret_key(strong), strong)


class SymbolInputValidationTests(unittest.TestCase):
    """品种写接口输入校验：脏 symbol / 越界风险度 / 未知策略一律 400，不得入 config 或下单路径。"""

    def setUp(self):
        self.client = api_server.app.test_client()
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True
        self.system = _prep_system(SimpleNamespace())

    def _post(self, path, body):
        with patch.object(api_server, "trading_system", self.system), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            return self.client.post(path, json=body)

    def test_add_symbol_rejects_malformed_name(self):
        resp = self._post("/api/symbols", {"name": "<img src=x>", "risk_per_trade": 0.01})
        self.assertEqual(resp.status_code, 400)

    def test_add_symbol_rejects_non_usdt_suffix(self):
        resp = self._post("/api/symbols", {"name": "BTCUSD", "risk_per_trade": 0.01})
        self.assertEqual(resp.status_code, 400)

    def test_add_symbol_rejects_out_of_range_risk(self):
        # 防数量级笔误：把 1（想表达 1%）直接传成 100% 风险
        resp = self._post("/api/symbols", {"name": "BTCUSDT", "risk_per_trade": 1})
        self.assertEqual(resp.status_code, 400)

    def test_add_symbol_rejects_removed_strategy_field(self):
        resp = self._post("/api/symbols", {
            "name": "BTCUSDT", "risk_per_trade": 0.01,
            "strategy": "unsupported"})
        self.assertEqual(resp.status_code, 400)

    def test_instant_open_rejects_malformed_input(self):
        resp = self._post("/api/instant_open", {"name": "btc;rm -rf", "risk_per_trade": 0.01})
        self.assertEqual(resp.status_code, 400)

    def test_symbol_writes_reject_unicode_casefold_aliases(self):
        self.system._trade_lock = Mock()
        self.system._config_lock = Mock()
        self.system.trade_state = Mock()
        self.system.exchange_api = Mock()
        self.system.persist_config = Mock(return_value=True)
        for bad in ('ſUSDT', 'ßUSDT', 'ıUSDT'):
            requests = (
                ('post', '/api/symbols',
                 {'name': bad, 'risk_per_trade': 0.01}),
                ('post', '/api/instant_open',
                 {'name': bad, 'risk_per_trade': 0.01}),
                ('put', f'/api/symbols/{bad}', {'enabled': False}),
                ('delete', f'/api/symbols/{bad}', None),
                ('post', '/api/close_position', {'name': bad}),
            )
            for method, path, payload in requests:
                with self.subTest(bad=bad, method=method, path=path), \
                     patch.object(api_server, 'trading_system', self.system):
                    kwargs = {} if payload is None else {'json': payload}
                    resp = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(400, resp.status_code)
                self.system._trade_lock.assert_not_called()
                self.system._config_lock.assert_not_called()
                self.assertEqual([], self.system.trade_state.mock_calls)
                self.assertEqual([], self.system.exchange_api.mock_calls)
                self.system.persist_config.assert_not_called()

    def test_add_and_instant_open_require_explicit_risk(self):
        for path in ('/api/symbols', '/api/instant_open'):
            with self.subTest(path=path), patch.object(
                    api_server, 'trading_system', self.system):
                resp = self.client.post(path, json={'name': 'BTCUSDT'})
            self.assertEqual(400, resp.status_code)

    def test_validate_symbol_input_normalizes(self):
        """API 归一化契约：返回规范化 clean——name 大写、risk→float、enabled→真 bool。
        杜绝 "0.01"/"false" 字符串混入下单/开仓资格路径（否则盘中 TypeError 或被当启用）。"""
        clean, err = api_server._validate_symbol_input("btcusdt", "0.01", "false")
        self.assertIsNone(err)
        self.assertEqual(clean["name"], "BTCUSDT")
        self.assertIsInstance(clean["risk_per_trade"], float)
        self.assertEqual(clean["risk_per_trade"], 0.01)
        self.assertIs(clean["enabled"], False)   # "false" 解析为 False，而非 Python 真值陷阱
        self.assertNotIn("strategy", clean)

    def test_validate_symbol_input_rejects_non_string_name(self):
        clean, err = api_server._validate_symbol_input(123)
        self.assertIsNotNone(err)
        self.assertIsNone(clean)

    def test_validate_symbol_input_rejects_ambiguous_enabled(self):
        clean, err = api_server._validate_symbol_input("BTCUSDT", enabled="maybe")
        self.assertIsNotNone(err)

    def test_equity_sync_rejects_nonfinite_flow_amount(self):
        """资金同步净变动金额必须有限：nan/inf/-inf 会写出 nan/0.0 除数污染求索指数，须 400。"""
        for bad in ("nan", "inf", "-inf", True, False):
            with patch.object(api_server, "trading_system", self.system), patch.object(
                api_server, "send_dingtalk", Mock()
            ):
                resp = self.client.post("/api/equity_sync", json={"flow_amount": bad})
            self.assertEqual(resp.status_code, 400, msg=f"flow_amount={bad!r} 应 400")

    def test_manual_close_rejects_non_string_name(self):
        """手动平仓也走同源交易对规范化：非字符串 name 应干净 400，而非 .upper() 500。"""
        with patch.object(api_server, "trading_system", self.system), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.post("/api/close_position", json={"name": 123})
        self.assertEqual(resp.status_code, 400)

    def test_manual_close_rejects_legacy_or_partial_close_fields_before_state_read(self):
        self.system.trade_state = SimpleNamespace(get_open_position=Mock())
        self.system._submit_persisted_close = Mock()
        payloads = (
            {'name': 'BTCUSDT', 'strategy': 'removed'},
            {'name': 'BTCUSDT', 'side': 'long'},
            {'name': 'BTCUSDT', 'amount': 0.5},
        )
        for payload in payloads:
            with self.subTest(payload=payload), patch.object(
                    api_server, 'trading_system', self.system):
                resp = self.client.post('/api/close_position', json=payload)
            self.assertEqual(400, resp.status_code)
        self.system.trade_state.get_open_position.assert_not_called()
        self.system._submit_persisted_close.assert_not_called()

    def test_manual_check_rejects_nonempty_payload_without_starting_thread(self):
        self.system.check_and_execute_trades = Mock()
        payloads = (
            {'strategy': 'removed'},
            {'symbols': ['BTCUSDT']},
            {'manual_run': True},
        )
        with patch.object(api_server.threading, 'Thread') as thread:
            for payload in payloads:
                with self.subTest(payload=payload), patch.object(
                        api_server, 'trading_system', self.system):
                    resp = self.client.post('/api/manual_check', json=payload)
                self.assertEqual(400, resp.status_code)
        thread.assert_not_called()
        self.system.check_and_execute_trades.assert_not_called()

    def test_update_symbol_without_body_returns_400(self):
        """无 JSON body：优雅 400，而非 data.get 抛异常变 500。"""
        with patch.object(api_server, "trading_system", self.system), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.put("/api/symbols/BTCUSDT")
        self.assertEqual(resp.status_code, 400)

    def test_strategy_params_without_body_returns_400(self):
        self.system.config = {"strategy": {}}
        with patch.object(api_server, "trading_system", self.system), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.put("/api/strategy_params")
        self.assertEqual(resp.status_code, 400)

    def test_strategy_params_rejects_unknown_only_body(self):
        self.system.config = {"strategy": {"ma_long_period": 28}}
        with patch.object(api_server, "trading_system", self.system), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.put("/api/strategy_params", json={"ma_lnog_period": 30})
        self.assertEqual(resp.status_code, 400)

    def test_strategy_params_rejects_short_not_less_than_long(self):
        self.system.config = {"strategy": {"ma_short_period": 6, "ma_long_period": 28}}
        with patch.object(api_server, "trading_system", self.system), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.put("/api/strategy_params", json={"ma_short_period": 30})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.system.config["strategy"]["ma_short_period"], 6)  # 未写入

    def test_strategy_params_rejects_out_of_range_risk(self):
        self.system.config = {"strategy": {}}
        with patch.object(api_server, "trading_system", self.system), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.put("/api/strategy_params", json={"default_risk_per_trade": 0.9})
        self.assertEqual(resp.status_code, 400)

    def test_strategy_params_rejects_nonfinite_default_risk(self):
        self.system.config = {"strategy": {"default_risk_per_trade": 0.01}}
        for bad in ("nan", "inf", "-inf"):
            with patch.object(api_server, "trading_system", self.system), patch.object(
                api_server, "send_dingtalk", Mock()
            ):
                resp = self.client.put("/api/strategy_params", json={"default_risk_per_trade": bad})
            self.assertEqual(resp.status_code, 400, msg=f"default_risk={bad!r} 应 400")
            self.assertEqual(self.system.config["strategy"]["default_risk_per_trade"], 0.01)

    def test_strategy_params_rejects_fractional_period(self):
        """API 与启动校验同源 strict_int：小数周期 28.9 拒绝而非截断为 28（三入口口径一致）。"""
        self.system.config = {"strategy": {"ma_long_period": 28}}
        with patch.object(api_server, "trading_system", self.system), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.put("/api/strategy_params", json={"ma_long_period": 28.9})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.system.config["strategy"]["ma_long_period"], 28)  # 未写入截断值

    def test_update_symbol_rejects_out_of_range_risk(self):
        self.system.config = {"trading": {"symbols": [{"name": "BTCUSDT", "risk_per_trade": 0.01}]}}
        with patch.object(api_server, "trading_system", self.system), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.put("/api/symbols/BTCUSDT", json={"risk_per_trade": 0.9})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.system.config["trading"]["symbols"][0]["risk_per_trade"], 0.01)

    def test_update_symbol_all_null_fields_returns_400(self):
        """全部字段为显式 null（视为未提供）：干净 400，而非 clean[键] KeyError → 500。"""
        self.system.config = {"trading": {"symbols": [{"name": "BTCUSDT", "risk_per_trade": 0.01}]}}
        with patch.object(api_server, "trading_system", self.system), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.put(
                "/api/symbols/BTCUSDT",
                json={"risk_per_trade": None, "enabled": None})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.system.config["trading"]["symbols"][0]["risk_per_trade"], 0.01)

    def test_add_symbol_rejects_unresolved_open_intent(self):
        self.system.config = {'trading': {'symbols': []}}
        self.system.label = '欧易'
        self.system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda symbol: symbol,
            fetch_ohlcv=Mock(return_value=[[1, 1, 1, 1, 1, 1]]))
        self.system.trade_state = SimpleNamespace(
            get_open_intent=Mock(return_value={
                'status': 'pending', 'client_order_id': 'IOLD'}))
        with patch.object(api_server, 'trading_system', self.system), patch.object(
                api_server, 'send_dingtalk', Mock()):
            resp = self.client.post('/api/symbols', json={
                'name': 'BTCUSDT', 'risk_per_trade': 0.01})
        self.assertEqual(409, resp.status_code)
        self.assertEqual([], self.system.config['trading']['symbols'])

    def test_open_intent_allows_only_emergency_disable(self):
        self.system.config = {'trading': {'symbols': [{
            'name': 'BTCUSDT', 'enabled': True,
            'risk_per_trade': 0.01}]}}
        self.system.label = '欧易'
        self.system.reload_strategies = Mock()
        self.system.trade_state = SimpleNamespace(
            get_open_intent=Mock(return_value={
                'status': 'pending', 'client_order_id': 'IOLD'}),
            get_open_position=Mock(return_value=None))
        with patch.object(api_server, 'trading_system', self.system), patch.object(
                api_server, 'send_dingtalk', Mock()):
            rejected = self.client.put(
                '/api/symbols/BTCUSDT', json={'risk_per_trade': 0.02})
            disabled = self.client.put(
                '/api/symbols/BTCUSDT', json={'enabled': False})

        self.assertEqual(409, rejected.status_code)
        self.assertEqual(200, disabled.status_code)
        self.assertIs(
            False, self.system.config['trading']['symbols'][0]['enabled'])


class DeleteSymbolApiTests(unittest.TestCase):
    """删除交易对只移出品种池，不平仓、不撤单。"""

    def setUp(self):
        self.client = api_server.app.test_client()
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True

    def make_system(self, held_position=None):
        symbols = [{"name": "ETHUSDT", "enabled": True, "risk_per_trade": 0.01}]

        system = SimpleNamespace(
            trade_state=SimpleNamespace(
                get_open_position=lambda _s: held_position,
                close_position=Mock(side_effect=AssertionError("删除不得平仓")),
            ),
            exchange_api=SimpleNamespace(
                cancel_order=Mock(side_effect=AssertionError("删除不得撤单")),
                cancel_all_orders=Mock(side_effect=AssertionError("删除不得撤单")),
            ),
            config={"trading": {"symbols": symbols}},
            config_file="config.json",
            reload_strategies=Mock(),
            label="欧易",
            exchange_id="okx",
        )
        return system

    def _delete(self, system):
        with patch.object(api_server, "trading_system", _prep_system(system)), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            return self.client.delete("/api/symbols/ETHUSDT")

    def test_delete_only_removes_from_pool(self):
        """无持仓：删除只清配置，不触发平仓/撤单。"""
        system = self.make_system(held_position=None)

        resp = self._delete(system)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(system.config["trading"]["symbols"], [])
        system.trade_state.close_position.assert_not_called()
        system.exchange_api.cancel_all_orders.assert_not_called()

    def test_delete_with_position_keeps_state_untouched(self):
        """有持仓时删除放行，持仓和挂单原样保留。"""
        held = {"symbol": "ETHUSDT", "side": "long"}
        system = self.make_system(held_position=held)

        resp = self._delete(system)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(system.config["trading"]["symbols"], [])
        self.assertEqual(held, {"symbol": "ETHUSDT", "side": "long"})

    def test_delete_rejects_nonempty_body_before_state_or_config_write(self):
        system = _prep_system(self.make_system())
        get_open_position = Mock()
        system.trade_state.get_open_position = get_open_position
        system.persist_config = Mock(return_value=True)
        original = json.loads(json.dumps(system.config))

        with patch.object(api_server, 'trading_system', system), patch.object(
                api_server, 'send_dingtalk', Mock()):
            response = self.client.delete('/api/symbols/ETHUSDT', json={
                'strategy': 'removed', 'amount': 1})

        self.assertEqual(400, response.status_code)
        self.assertEqual(original, system.config)
        get_open_position.assert_not_called()
        system.persist_config.assert_not_called()

    def test_delete_rejects_non_json_or_malformed_nonempty_body(self):
        payloads = (
            ('legacy=true', 'text/plain'),
            ('{bad-json', 'application/json'),
            ('[]', 'application/json'),
            ('null', 'application/json'),
        )
        for raw, content_type in payloads:
            system = _prep_system(self.make_system())
            system.trade_state.get_open_position = Mock()
            system.persist_config = Mock(return_value=True)
            original = json.loads(json.dumps(system.config))
            with self.subTest(raw=raw), patch.object(
                    api_server, 'trading_system', system), patch.object(
                    api_server, 'send_dingtalk', Mock()):
                response = self.client.delete(
                    '/api/symbols/ETHUSDT', data=raw,
                    content_type=content_type)
            self.assertEqual(400, response.status_code)
            self.assertEqual(original, system.config)
            system.trade_state.get_open_position.assert_not_called()
            system.persist_config.assert_not_called()


class ExecuteOpenRiskGuardTests(unittest.TestCase):
    def make_system(self):
        system = object.__new__(main.TradingSystem)
        system.base_dir = str(APP_DIR)
        system._stop_anomalies = {}
        system.config = {"strategy": {"default_risk_per_trade": 0.01}}
        system.notifier = SimpleNamespace(
            notify_error=Mock(),
            notify_trade_opened=Mock(),
        )
        open_intents = {}

        def get_open_intent(symbol, client_order_id=None):
            intent = open_intents.get(symbol)
            if (intent and client_order_id is not None and
                    intent['client_order_id'] != str(client_order_id)):
                return None
            return intent

        def prepare_open_intent(
                symbol, strategy, side, client_order_id, payload,
                planned_position_size=None):
            intent = {
                'strategy': strategy, 'side': side,
                'client_order_id': str(client_order_id),
                'payload': payload,
                'planned_position_size': float(planned_position_size),
                'status': 'pending',
            }
            open_intents[symbol] = intent
            return intent

        mark_quarantine = Mock()

        def mark_unresolved(
                symbol, client_order_id, kind, expected_position_size,
                **kwargs):
            intent = get_open_intent(symbol, client_order_id)
            if intent is None:
                raise RuntimeError('open intent mismatch')
            unresolved = {
                'kind': kind,
                'open_client_order_id': str(client_order_id),
                'expected_position_size': float(expected_position_size),
                'compensation_client_order_id': kwargs.get(
                    'compensation_client_order_id'),
            }
            intent['unresolved_execution'] = unresolved
            mark_quarantine(
                symbol, kwargs.get('reason'), kwargs.get('details'))
            return unresolved

        def add_untracked(**kwargs):
            return {
                'symbol': kwargs['symbol'], 'side': kwargs['side'],
                'position_size': kwargs['position_size'],
                'stop_order_id': kwargs.get('stop_order_id'),
                'execution_recovery_finalized': not kwargs.get(
                    'preserve_open_intent', False),
            }

        system.trade_state = SimpleNamespace(
            add_open_position=Mock(),
            add_untracked_open_position=Mock(side_effect=add_untracked),
            force_runtime_add_untracked_open_position=Mock(
                side_effect=add_untracked),
            get_open_position=Mock(return_value=None),
            get_open_intent=Mock(side_effect=get_open_intent),
            get_runtime_persistence_status=Mock(return_value={
                'degraded': False, 'context': None}),
            get_stop_loss_dates=Mock(return_value={}),
            prepare_open_intent=Mock(side_effect=prepare_open_intent),
            is_position_quarantined=Mock(return_value=False),
            has_stop_residue=Mock(return_value=False),
            clear_stop_residue=Mock(),
            mark_stop_residue=Mock(),
            mark_position_quarantine=mark_quarantine,
            force_runtime_mark_position_quarantine=Mock(),
            mark_open_intent_unresolved_execution=Mock(
                side_effect=mark_unresolved),
            force_runtime_mark_open_intent_unresolved_execution=Mock(
                side_effect=mark_unresolved),
        )
        system._pending_trade_open_notifications = []
        system.risk_manager = SimpleNamespace(
            account_equity=10000,
            risk_per_trade=0.01,
            calculate_position_size=Mock(return_value=2.5),
        )
        exchange_stub = SimpleNamespace(fetch_ticker=Mock(return_value={"last": 100}))
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=_fake_to_ccxt,
            exchange=exchange_stub,
            get_last_price=lambda s: float(exchange_stub.fetch_ticker(s)["last"]),
            get_balance=Mock(return_value={"total": {"USDT": 10000}}),
            round_quantity=Mock(return_value=2.5),
            get_quantity_precision=Mock(return_value=3),
            open_position=Mock(return_value={
                "id": "open-1", "average": 100,
                "amount": 2.5, "confirmed": True}),
            get_position=Mock(return_value={
                "side": "long", "contracts": 2.5}),
            _contracts_to_coins=lambda _symbol, contracts: float(contracts),
            create_stop_loss_order=Mock(return_value={"id": "stop-1"}),
            cancel_order=Mock(return_value=True),
            cancel_all_orders=Mock(),
            close_position=Mock(return_value={
                "id": "close-1", "confirmed": True,
                "fully_closed": True, "remaining_amount": 0.0,
                "amount": 2.5, "average": 99.0}),
            compensation_client_order_id=lambda value: f'R{value[1:]}',
        )
        system._finalize_open_intent_rollback = Mock(return_value=True)
        return system

    def test_confirmed_open_rollback_helper_forwards_exact_contract(self):
        system = self.make_system()
        open_order = {'id': 'open-1', 'confirmed': True, 'amount': 2.75}
        rollback = {
            'id': 'close-1', 'confirmed': True, 'fully_closed': True,
            'remaining_amount': 0.0,
        }
        outcome = {'status': 'rolled_back'}
        system._submit_compensation_close = Mock(return_value=rollback)
        system._finalize_open_rollback = Mock(return_value=outcome)
        ordered = Mock()
        ordered.attach_mock(
            system._submit_compensation_close, 'submit_compensation')
        ordered.attach_mock(system._finalize_open_rollback, 'finalize')
        context = trade_executor._ConfirmedOpenRollbackContext(
            symbol='BTCUSDT', ccxt_symbol='BTC/USDT', side='long',
            stop_loss_price=80.0, open_order=open_order,
            open_client_order_id='Iopen',
            open_intent_client_order_id='Iintent',
            requested_position_size=2.5)

        result = system._rollback_confirmed_open(
            context, 101.0, 2.75, '测试回滚',
            existing_stop_order_id='stop-1',
            existing_stop_order_size=2.75,
            allow_stop_rebuild=False, stop_residue_possible=True)

        self.assertIs(outcome, result)
        self.assertEqual(
            ['submit_compensation', 'finalize'],
            [item[0] for item in ordered.mock_calls])
        system._submit_compensation_close.assert_called_once_with(
            'BTC/USDT', 'long', 2.75, open_order, 'Iopen')
        system._finalize_open_rollback.assert_called_once_with(
            'BTCUSDT', 'BTC/USDT', 'long', 101.0, 2.75, 80.0,
            'ma_cross', open_order, rollback, '测试回滚',
            existing_stop_order_id='stop-1',
            existing_stop_order_size=2.75,
            allow_stop_rebuild=False, stop_residue_possible=True,
            open_intent_client_id='Iintent',
            requested_position_size=2.5,
            preserve_open_intent=False,
            unresolved_execution_kind='open_compensation')

    def test_confirmed_open_rollback_branches_preserve_action_order(self):
        cases = (
            {
                'name': 'execution_ambiguous',
                'open_order': {
                    'id': 'open-ambiguous', 'confirmed': True,
                    'execution_ambiguous': True, 'average': 100.0,
                    'amount': 2.5,
                },
                'expected': ['rollback', 'notify', 'intent'],
                'context': '歧义开仓紧急回滚',
                'policy': {
                    'preserve_open_intent': True,
                    'unresolved_execution_kind': 'open_attribution',
                },
            },
            {
                'name': 'overfill',
                'open_order': {
                    'id': 'open-overfill', 'confirmed': True,
                    'average': 100.0, 'amount': 3.0,
                },
                'expected': ['rollback', 'notify', 'intent'],
                'context': '超量开仓紧急回滚',
                'policy': {
                    'preserve_open_intent': True,
                    'unresolved_execution_kind': 'open_attribution',
                },
            },
            {
                'name': 'stop_invalid',
                'open_order': {
                    'id': 'open-stop-invalid', 'confirmed': True,
                    'average': 79.0, 'amount': 2.5,
                },
                'market_price': 82.0,
                'expected': ['notify', 'rollback', 'intent'],
                'context': '止损失效后的紧急回滚',
                'policy': {'allow_stop_rebuild': False},
            },
            {
                'name': 'stop_creation_failure',
                'open_order': {
                    'id': 'open-stop-failure', 'confirmed': True,
                    'average': 100.0, 'amount': 2.5,
                },
                'stop_order': None,
                'expected': [
                    'mark_residue', 'notify', 'rollback', 'cancel_stop',
                    'intent'],
                'context': '止损创建失败后的紧急回滚',
                'policy': {'stop_residue_possible': True},
            },
            {
                'name': 'risk_overrun',
                'open_order': {
                    'id': 'open-risk', 'confirmed': True,
                    'average': 150.0, 'amount': 2.5,
                },
                'expected': [
                    'mark_residue', 'notify', 'mark_residue', 'rollback',
                    'cancel_stop', 'intent'],
                'context': '成交后风险超标紧急回滚',
                'policy': {
                    'existing_stop_order_id': 'stop-1',
                    'existing_stop_order_size': 2.5,
                    'allow_stop_rebuild': False,
                    'stop_residue_possible': True,
                },
            },
        )
        for case in cases:
            with self.subTest(case=case['name']):
                system = self.make_system()
                events = []
                system.exchange_api.open_position.return_value = case['open_order']
                system.exchange_api.exchange.fetch_ticker.return_value = {
                    'last': case.get('market_price', 100.0)}
                if 'stop_order' in case:
                    system.exchange_api.create_stop_loss_order.return_value = (
                        case['stop_order'])
                system.notifier.notify_error = Mock(
                    side_effect=lambda _message: events.append('notify'))
                system._mark_possible_unknown_stop_residue = Mock(
                    side_effect=lambda _symbol: events.append('mark_residue') or True)
                system._cancel_stop_order_confirmed = Mock(
                    side_effect=lambda *_args: events.append('cancel_stop') or True)
                system._rollback_confirmed_open = Mock(
                    side_effect=lambda *_args, **_kwargs:
                    events.append('rollback') or {'status': 'rolled_back'})
                system._finalize_open_intent_rollback = Mock(
                    side_effect=lambda *_args:
                    events.append('intent') or True)

                outcome = system._execute_open(
                    'BTCUSDT', 'long', 100, 80,
                    {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

                self.assertEqual('rolled_back', outcome['status'])
                self.assertEqual(case['expected'], events)
                rollback_call = system._rollback_confirmed_open.call_args
                self.assertEqual(case['context'], rollback_call.args[3])
                self.assertEqual(case['policy'], rollback_call.kwargs)

    def test_missing_runtime_directory_blocks_before_any_open_work(self):
        system = self.make_system()
        del system.base_dir

        result = system._execute_open(
            'BTCUSDT', 'long', 100, 80,
            {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

        self.assertEqual('maintenance_blocked', result['status'])
        system.exchange_api.open_position.assert_not_called()
        system.exchange_api.get_balance.assert_not_called()

    def test_open_intent_persistence_failure_never_posts(self):
        system = self.make_system()
        system.trade_state.prepare_open_intent.side_effect = OSError('disk full')

        system._execute_open(
            'BTCUSDT', 'long', 100, 80,
            {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

        system.exchange_api.open_position.assert_not_called()

    def test_malformed_open_confirmation_never_posts_stop_or_writes_ledger(self):
        missing = object()
        for confirmed in (missing, None, False, 'true', 1):
            with self.subTest(confirmed=confirmed):
                system = self.make_system()
                result = {
                    'id': 'open-malformed', 'average': 100.0,
                    'amount': 2.5,
                }
                if confirmed is not missing:
                    result['confirmed'] = confirmed
                system.exchange_api.open_position.return_value = result

                system._execute_open(
                    'BTCUSDT', 'long', 100, 80,
                    {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

                system.exchange_api.create_stop_loss_order.assert_not_called()
                system.exchange_api.close_position.assert_not_called()
                system.trade_state.add_open_position.assert_not_called()
                system.trade_state.mark_position_quarantine.assert_called_once()

    def test_malformed_open_amount_is_quarantined_before_stop_or_ledger(self):
        for amount in (
                True, False, float('nan'), float('inf'), float('-inf'),
                'garbage', 0, -1, None):
            with self.subTest(amount=amount):
                system = self.make_system()
                system.exchange_api.open_position.return_value = {
                    'id': 'open-bad-amount', 'confirmed': True,
                    'average': 100.0, 'amount': amount,
                }

                system._execute_open(
                    'BTCUSDT', 'long', 100, 80,
                    {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

                system.exchange_api.create_stop_loss_order.assert_not_called()
                system.trade_state.add_open_position.assert_not_called()
                system.trade_state.mark_position_quarantine.assert_called_once()

    def test_confirmed_open_without_unique_authoritative_financial_evidence_rolls_back(self):
        cases = (
            {
                'name': 'missing_average_flag_absent',
                'order': {'id': 'open-no-average', 'confirmed': True,
                          'amount': 2.5},
            },
            {
                'name': 'missing_average_flag_false',
                'order': {'id': 'open-no-average', 'confirmed': True,
                          'amount': 2.5,
                          'financial_evidence_incomplete': False},
            },
            {
                'name': 'missing_order_id',
                'order': {'average': 100.0, 'confirmed': True,
                          'amount': 2.5},
            },
            {
                'name': 'multiple_order_ids',
                'order': {'id': 'open-1', 'ids': ['open-1', 'open-2'],
                          'average': 100.0, 'confirmed': True,
                          'amount': 2.5},
            },
        )
        for case in cases:
            with self.subTest(case=case['name']):
                system = self.make_system()
                system.exchange_api.open_position.return_value = case['order']
                system._rollback_confirmed_open = Mock(return_value={
                    'status': 'rollback_incomplete'})

                outcome = system._execute_open(
                    'BTCUSDT', 'long', 100, 80,
                    {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

                self.assertEqual('rollback_incomplete', outcome['status'])
                system._rollback_confirmed_open.assert_called_once()
                call = system._rollback_confirmed_open.call_args
                self.assertEqual('开仓财务证据未决紧急回滚', call.args[3])
                self.assertTrue(call.kwargs['preserve_open_intent'])
                self.assertEqual(
                    'open_compensation',
                    call.kwargs['unresolved_execution_kind'])
                system.exchange_api.create_stop_loss_order.assert_not_called()
                system.trade_state.add_open_position.assert_not_called()

    def test_conflicting_open_execution_flags_preserve_intent(self):
        system = self.make_system()
        system.exchange_api.open_position.return_value = {
            'id': 'open-conflict', 'confirmed': False,
            'amount': 2.5, 'open_execution_compensated': True,
            'open_order_may_remain_live': True,
            'compensation': {
                'id': 'close-full', 'confirmed': True,
                'fully_closed': True, 'amount': 2.5,
                'remaining_amount': 0.0},
        }

        system._execute_open(
            'BTCUSDT', 'long', 100, 80,
            {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

        system.exchange_api.create_stop_loss_order.assert_not_called()
        system.trade_state.add_open_position.assert_not_called()
        system._finalize_open_intent_rollback.assert_not_called()
        system.trade_state.mark_position_quarantine.assert_called_once()

    def test_existing_open_intent_is_never_replayed_by_execute_open(self):
        system = self.make_system()
        system.trade_state.prepare_open_intent(
            'BTCUSDT', 'ma_cross', 'long', 'Iunbound123',
            {'side': 'long', 'entry_price': 100.0,
             'stop_loss_price': 80.0},
            planned_position_size=2.5)

        result = system._execute_open(
            'BTCUSDT', 'long', 100, 80,
            {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

        self.assertEqual('state_blocked', result['status'])
        system.exchange_api.open_position.assert_not_called()

    def test_rejects_open_when_realtime_price_has_crossed_stop(self):
        system = self.make_system()
        system.exchange_api.exchange.fetch_ticker.return_value = {"last": 78}

        system._execute_open(
            "BTCUSDT",
            "long",
            100,
            80,
            {"name": "BTCUSDT", "risk_per_trade": 0.01},
        )

        system.exchange_api.open_position.assert_not_called()
        system.trade_state.add_open_position.assert_not_called()

    def test_missing_realtime_price_never_falls_back_to_stale_signal_price(self):
        system = self.make_system()
        system.exchange_api.exchange.fetch_ticker.return_value = {'last': None}

        system._execute_open(
            'BTCUSDT', 'long', 100, 80,
            {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

        system.exchange_api.open_position.assert_not_called()
        system.exchange_api.get_balance.assert_not_called()

    def test_missing_current_usdt_equity_never_uses_startup_snapshot(self):
        system = self.make_system()
        system.exchange_api.get_balance.return_value = {'total': {}}

        system._execute_open(
            'BTCUSDT', 'long', 100, 80,
            {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

        system.exchange_api.open_position.assert_not_called()
        self.assertEqual(10000, system.risk_manager.account_equity)

    def test_malformed_current_usdt_equity_never_opens(self):
        for value in (
                True, False, float('nan'), float('inf'),
                float('-inf'), 0, -1, None):
            with self.subTest(value=value):
                system = self.make_system()
                system.exchange_api.get_balance.return_value = {
                    'total': {'USDT': value}}

                system._execute_open(
                    'BTCUSDT', 'long', 100, 80,
                    {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

                system.exchange_api.open_position.assert_not_called()
                self.assertEqual(10000, system.risk_manager.account_equity)

    def test_rolls_back_when_fill_price_crosses_stop(self):
        system = self.make_system()
        system.exchange_api.exchange.fetch_ticker.return_value = {"last": 82}
        system.exchange_api.open_position.return_value = {
            "id": "open-stop-crossed", "average": 79,
            "amount": 2.5, "confirmed": True}

        system._execute_open(
            "BTCUSDT",
            "long",
            100,
            80,
            {"name": "BTCUSDT", "risk_per_trade": 0.01},
        )

        system.exchange_api.close_position.assert_called_once_with(
            "BTC/USDT", "long", 2.5, client_order_id=ANY)
        system.exchange_api.create_stop_loss_order.assert_not_called()
        system.trade_state.add_open_position.assert_not_called()
        system.notifier.notify_trade_opened.assert_not_called()

    def test_compensation_none_and_flat_keeps_open_intent_unresolved(self):
        system = self.make_system()
        system.exchange_api.exchange.fetch_ticker.return_value = {'last': 82}
        system.exchange_api.open_position.return_value = {
            'id': 'open-flat-unresolved', 'average': 79.0,
            'amount': 2.5, 'confirmed': True,
        }
        system.exchange_api.close_position.return_value = None
        system.exchange_api.get_position = Mock(return_value=None)
        system.exchange_api.find_compensation_close_evidence = Mock(
            return_value=None)
        system._clear_position_quarantine_after_reconcile = Mock()

        outcome = system._execute_open(
            'BTCUSDT', 'long', 100, 80,
            {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

        self.assertEqual('rollback_incomplete', outcome['status'])
        self.assertTrue(outcome['close_order']['position_flat_observed'])
        self.assertIsNotNone(system.trade_state.get_open_intent('BTCUSDT'))
        system._finalize_open_intent_rollback.assert_not_called()
        system._clear_position_quarantine_after_reconcile.assert_not_called()
        self.assertEqual(
            2, system.trade_state.mark_position_quarantine.call_count)
        system.exchange_api.find_compensation_close_evidence.assert_called_once()

    def test_compensation_none_and_flat_finalizes_only_with_late_order_evidence(self):
        system = self.make_system()
        system.exchange_api.exchange.fetch_ticker.return_value = {'last': 82}
        system.exchange_api.open_position.return_value = {
            'id': 'open-flat-evidenced', 'average': 79.0,
            'amount': 2.5, 'confirmed': True,
        }
        system.exchange_api.close_position.return_value = None
        system.exchange_api.get_position = Mock(return_value=None)
        system.exchange_api.find_compensation_close_evidence = Mock(
            return_value={
                'id': 'close-late', 'confirmed': True,
                'fully_closed': True, 'remaining_amount': 0.0,
                'amount': 2.5, 'average': 78.5,
            })

        outcome = system._execute_open(
            'BTCUSDT', 'long', 100, 80,
            {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

        self.assertEqual('rolled_back', outcome['status'])
        self.assertEqual('close-late', outcome['close_order']['id'])
        self.assertTrue(outcome['open_intent_finalized'])
        self.assertEqual(2, system.exchange_api.get_position.call_count)
        system._finalize_open_intent_rollback.assert_called_once()

    def test_rolls_back_when_stop_order_creation_fails(self):
        system = self.make_system()
        system.exchange_api.exchange.fetch_ticker.return_value = {"last": 100}
        system.exchange_api.open_position.return_value = {
            "id": "open-stop-failed", "average": 100,
            "amount": 2.5, "confirmed": True}
        system.exchange_api.create_stop_loss_order.return_value = None
        system.exchange_api.close_position.return_value = {
            "id": "close-1", "confirmed": True,
            "fully_closed": True, "remaining_amount": 0.0,
            "amount": 2.5, "average": 99.0}
        # 用同一父 mock 记录调用顺序：必须先在可能存在的 reduce-only 保护下
        # 确认归零，之后才可安全清扫未知止损。
        calls = Mock()
        calls.attach_mock(system.exchange_api.cancel_all_orders, "cancel_all_orders")
        calls.attach_mock(system.exchange_api.close_position, "close_position")

        system._execute_open(
            "BTCUSDT",
            "long",
            100,
            80,
            {"name": "BTCUSDT", "risk_per_trade": 0.01},
        )

        system.exchange_api.close_position.assert_called_once_with(
            "BTC/USDT", "long", 2.5, client_order_id=ANY)
        # 全平确认后撤光该品种挂单，清扫可能的未知止损（防未来错价触发）。
        system.exchange_api.cancel_all_orders.assert_called_once_with("BTC/USDT")
        self.assertEqual(
            [c[0] for c in calls.mock_calls],
            ["close_position", "cancel_all_orders"],
            "必须先保留保护完成回滚，再清扫挂单",
        )
        system.trade_state.add_open_position.assert_not_called()
        system.notifier.notify_trade_opened.assert_not_called()

    def test_terminal_partial_fill_uses_actual_amount_for_stop_and_ledger(self):
        system = self.make_system()
        system.exchange_api.open_position.return_value = {
            "id": "open-partial", "confirmed": True, "fully_filled": False,
            "amount": 1.25, "average": 100,
        }
        system.exchange_api.get_position.return_value = {
            'side': 'long', 'contracts': 1.25}

        system._execute_open(
            "BTCUSDT", "long", 100, 80,
            {"name": "BTCUSDT", "risk_per_trade": 0.01},
        )

        system.exchange_api.create_stop_loss_order.assert_called_once_with(
            "BTC/USDT", "long", 1.25, 80)
        add_args = system.trade_state.add_open_position.call_args.args
        self.assertEqual(add_args[3], 1.25)

    def test_post_fill_risk_overrun_is_automatically_rolled_back(self):
        system = self.make_system()
        system.exchange_api.open_position.return_value = {
            'id': 'open-jump', 'confirmed': True, 'fully_filled': True,
            'amount': 2.5, 'average': 150.0,
        }
        system.exchange_api.close_position.return_value = {
            'id': 'close-risk', 'confirmed': True, 'fully_closed': True,
            'remaining_amount': 0.0, 'average': 149.0,
        }

        result = system._execute_open(
            'BTCUSDT', 'long', 100, 80,
            {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

        self.assertEqual('rolled_back', result['status'])
        system.exchange_api.create_stop_loss_order.assert_called_once_with(
            'BTC/USDT', 'long', 2.5, 80)
        system.exchange_api.close_position.assert_called_once_with(
            'BTC/USDT', 'long', 2.5, client_order_id=ANY)
        system.exchange_api.cancel_order.assert_called_once_with(
            'BTC/USDT', 'stop-1')
        system.trade_state.add_open_position.assert_not_called()

    def test_unresolved_open_compensation_is_quarantined_and_returned(self):
        system = self.make_system()
        unresolved = {
            'id': 'open-uncertain', 'confirmed': False,
            'open_execution_unresolved': True,
            'clientOrderId': 'Tpending123', 'remaining_amount': 0.4,
            'compensation': {
                'id': 'close-partial', 'average': 99.0,
                'fully_closed': False,
                'remaining_amount': 0.4,
            },
        }
        system.exchange_api.open_position.return_value = unresolved
        system.exchange_api.get_position.return_value = {
            'side': 'long', 'contracts': 0.4}

        result = system._execute_open(
            'BTCUSDT', 'long', 100, 80,
            {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

        self.assertEqual(result['status'], 'rollback_incomplete')
        self.assertEqual(result['position_size'], 0.4)
        system.trade_state.mark_position_quarantine.assert_called_once()
        system.exchange_api.create_stop_loss_order.assert_called_once_with(
            'BTC/USDT', 'long', 0.4, 80)
        system.trade_state.add_open_position.assert_not_called()
        system.trade_state.add_untracked_open_position.assert_called_once()

    def test_fully_compensated_unconfirmed_open_returns_rolled_back_outcome(self):
        system = self.make_system()
        system.exchange_api.open_position.return_value = {
            'id': 'open-uncertain', 'confirmed': False,
            'open_execution_compensated': True,
            'amount': 0.5, 'average': 100,
            'compensation': {
                'id': 'close-full', 'confirmed': True,
                'fully_closed': True,
                'amount': 0.5, 'average': 99,
                'remaining_amount': 0.0,
            },
        }

        result = system._execute_open(
            'BTCUSDT', 'long', 100, 80,
            {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(result['close_order']['id'], 'close-full')
        self.assertEqual(result['position_size'], 0.5)
        system.exchange_api.create_stop_loss_order.assert_not_called()
        system.trade_state.add_open_position.assert_not_called()

    def test_attribution_ambiguity_quarantines_without_closing_manual_position(self):
        system = self.make_system()
        system.exchange_api.open_position.return_value = {
            'id': 'old-zero-fill', 'confirmed': True,
            'execution_ambiguous': True,
            'open_execution_attribution_ambiguous': True,
            'amount': 0.0, 'observed_position_amount': 2.5,
            'clientOrderId': 'Iambiguous123',
        }

        result = system._execute_open(
            'BTCUSDT', 'long', 100, 80,
            {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

        self.assertEqual(result['status'], 'attribution_unresolved')
        system.exchange_api.close_position.assert_not_called()
        system.trade_state.mark_position_quarantine.assert_called_once()
        system.trade_state.add_open_position.assert_not_called()

    def test_position_quarantine_blocks_every_open_entrypoint(self):
        system = self.make_system()
        system.trade_state.is_position_quarantined = Mock(return_value=True)

        system._execute_open(
            "BTCUSDT", "long", 100, 80,
            {"name": "BTCUSDT", "risk_per_trade": 0.01},
        )

        system.exchange_api.open_position.assert_not_called()
        system.exchange_api.get_balance.assert_not_called()


class InstantOpenConfigRollbackTests(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True

    def test_rolls_back_config_when_auto_add_write_fails(self):
        trade_state = SimpleNamespace(position=None)

        def get_open_position(_symbol):
            return trade_state.position

        trade_state.get_open_position = get_open_position

        def execute_open(symbol, side, entry_price, stop_loss_price, symbol_config, buffer_notification=True):
            trade_state.position = {
                "side": side,
                "entry_price": entry_price,
                "position_size": 1.0,
                "stop_loss_price": stop_loss_price,
                "stop_order_id": "stop-1",
            }
            return {"status": "opened"}

        original_config = {"trading": {"symbols": []}}
        exchange_stub = SimpleNamespace(fetch_ticker=Mock(return_value={"last": 123.45}))
        fake_system = SimpleNamespace(
            trade_state=trade_state,
            exchange_api=SimpleNamespace(
                to_ccxt_symbol=_fake_to_ccxt,
                fetch_ohlcv=Mock(return_value=[[1]] * 200),
                ohlcv_to_dataframe=Mock(return_value=[None] * 60),
                filter_closed_candles=Mock(return_value=[None] * 60),
                exchange=exchange_stub,
                get_last_price=lambda s: float(exchange_stub.fetch_ticker(s)["last"]),
            ),
            ma_cross_strategy=SimpleNamespace(
                check_current_state=Mock(
                    return_value={
                        "action": "long",
                        "upper_stop": 130,
                        "lower_stop": 100,
                    }
                )
            ),
            _execute_open=execute_open,
            config={"trading": {"symbols": []}},
            config_file="config.json",
            reload_strategies=Mock(),
            label="欧易",
            exchange_id="okx",
        )

        with patch.object(api_server, "trading_system", _prep_system(fake_system, persist=False)), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.post(
                "/api/instant_open",
                json={"name": "BTCUSDT", "risk_per_trade": 0.01},
            )

        self.assertEqual(resp.status_code, 500)
        self.assertEqual(fake_system.config, original_config)
        fake_system.reload_strategies.assert_not_called()


class AccountStatsPersistenceTests(unittest.TestCase):
    """权益统计已迁入 EquityTracker（每个交易所一份），改为直接测该类。"""

    def make_balance(self):
        return {"total": {"USDT": 10000}, "free": {"USDT": 9000}}

    def make_tracker(self):
        system = SimpleNamespace(
            exchange_api=SimpleNamespace(
                to_ccxt_symbol=_fake_to_ccxt,
                get_balance=Mock(return_value=self.make_balance()),
                exchange=SimpleNamespace(fetch_ticker=Mock(return_value={"last": 100})),
            ),
            trade_state=SimpleNamespace(
                get_all_open_positions=Mock(return_value={}),
                mark_runtime_persistence_degraded=Mock()),
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return equity_tracker.EquityTracker(tmp.name, system)

    def test_build_account_stats_does_not_persist_when_persist_false(self):
        tracker = self.make_tracker()

        with patch.object(tracker, "load_peak_equity", Mock(return_value={"peak_equity": 9000, "peak_time": None})), \
             patch.object(tracker, "load_equity_history", Mock(return_value={"initial_equity": 9000, "year_start_equity": 9000})), \
             patch.object(tracker, "save_peak_equity", Mock()) as save_peak, \
             patch.object(tracker, "save_equity_history", Mock()) as save_hist:
            data = tracker.build_account_stats(persist=False)

        self.assertEqual(data["current_equity"], 10000)
        save_peak.assert_not_called()
        save_hist.assert_not_called()

    def test_build_account_stats_persist_never_writes_intraday_peak(self):
        tracker = self.make_tracker()

        with patch.object(tracker, "load_peak_equity", Mock(return_value={"peak_equity": 9000, "peak_time": None})), \
             patch.object(tracker, "load_equity_history", Mock(return_value={"initial_equity": 9000, "year_start_equity": 9000})), \
             patch.object(tracker, "save_peak_equity", Mock(return_value=False)) as save_peak:
            data = tracker.build_account_stats(persist=True)

        self.assertEqual(data["peak_equity"], 10000)  # provisional 展示值
        save_peak.assert_not_called()

    def test_record_daily_equity_snapshot_raises_on_save_failure(self):
        tracker = self.make_tracker()

        with patch.object(tracker, "load_daily_equity", Mock(return_value=[])), \
             patch.object(tracker, "save_daily_equity", Mock(return_value=False)):
            with self.assertLogs(equity_tracker.logger, level="ERROR") as logs:
                tracker.record_daily_equity_snapshot()

        self.assertTrue(any("记录权益快照失败" in line for line in logs.output))

    def test_equity_committed_but_not_durable_latches_trading_process(self):
        tracker = self.make_tracker()
        failure = trade_state.AtomicWriteCommitDurabilityError(
            tracker.PEAK_EQUITY_FILE, OSError('directory fsync failed'))

        with patch.object(
                equity_tracker, 'atomic_write_json', side_effect=failure), \
                self.assertRaises(
                    trade_state.AtomicWriteCommitDurabilityError):
            tracker._atomic_write_json(
                tracker.PEAK_EQUITY_FILE, {'peak_equity': 10000})

        tracker.system.trade_state.mark_runtime_persistence_degraded.assert_called_once_with(
            'equity_directory_fsync_failed_after_replace')


class TradeStateIsolationTests(unittest.TestCase):
    def test_get_all_open_positions_returns_copy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "trade_state.json"
            ts = trade_state.TradeState(str(state_file))
            ts.add_open_position(
                "BTCUSDT", "long", 100, 1, 90, "stop-1",
                strategy="ma_cross")

            positions = ts.get_all_open_positions()
            positions["BTCUSDT"]["entry_price"] = 999

            fresh = ts.get_all_open_positions()
            self.assertEqual(fresh["BTCUSDT"]["entry_price"], 100)

    def test_get_open_position_returns_copy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "trade_state.json"
            ts = trade_state.TradeState(str(state_file))
            ts.add_open_position(
                "BTCUSDT", "long", 100, 1, 90, "stop-1",
                strategy="ma_cross")

            position = ts.get_open_position("BTCUSDT")
            position["entry_price"] = 999

            fresh = ts.get_open_position("BTCUSDT")
            self.assertEqual(fresh["entry_price"], 100)



class MaCrossFlipTests(unittest.TestCase):
    def make_system(self):
        system = object.__new__(main.TradingSystem)
        system._stop_anomalies = {}
        exchange_stub = SimpleNamespace(fetch_ticker=Mock(return_value={"last": 111}))
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=_fake_to_ccxt,
            exchange=exchange_stub,
            get_last_price=lambda s: float(exchange_stub.fetch_ticker(s)["last"]),
            close_position=Mock(return_value={
                "id": "close-1", "ids": ["close-1"],
                "average": 112, "amount": 2.0,
                "confirmed": True, "fully_closed": True,
                "remaining_amount": 0.0}),
            cancel_order=Mock(return_value=True),
            cancel_all_orders=Mock(),
        )
        system.trade_state = SimpleNamespace(
            close_position=Mock(return_value={"pnl": 12.0, "pnl_percent": 6.0}),
            force_runtime_close_position=Mock(return_value={"pnl": 12.0, "pnl_percent": 6.0}),
            get_open_position=Mock(return_value={"symbol": "BTCUSDT"}),
            has_stop_residue=Mock(return_value=False),
            clear_stop_residue=Mock(),
            mark_stop_residue=Mock(),
        )
        system.notifier = SimpleNamespace(
            notify_error=Mock(),
            send_message=Mock(),
            notify_stop_loss_triggered=Mock(),
        )
        system._pending_trade_close_notifications = []
        system.stop_loss_dates = {}
        system._execute_open = Mock()
        system.record_stop_loss = Mock()
        system._submit_persisted_close = Mock(
            side_effect=lambda symbol, ccxt_symbol, position, context:
                system.exchange_api.close_position(
                    ccxt_symbol, position['side'], position['position_size']))
        return system

    def test_flip_position_records_actual_exit_and_opens_new_side(self):
        system = self.make_system()
        old_position = {"side": "short", "position_size": 2.0, "stop_order_id": "stop-1"}
        signal = {"current_close": 110, "lower_stop": 95, "upper_stop": 125}

        system._flip_position("BTCUSDT", signal, old_position, "long", {"name": "BTCUSDT"})

        system.trade_state.close_position.assert_called_once_with(
            "BTCUSDT", 112.0, exit_fee=None, exit_fee_currency=None,
            exit_order_ids=["close-1"])
        self.assertEqual(len(system._pending_trade_close_notifications), 1)  # 现行为：缓冲汇总
        system._execute_open.assert_called_once_with(
            "BTCUSDT", "long", 110, 95, {"name": "BTCUSDT"}
        )
        system.record_stop_loss.assert_not_called()  # 正常反手不记 T+1

    def test_flip_never_deletes_or_reverses_on_unproven_close_result(self):
        malformed = (
            {'fully_closed': True},
            {'confirmed': None, 'fully_closed': True},
            {'confirmed': 'true', 'fully_closed': True},
            {'confirmed': False, 'fully_closed': True},
            {'confirmed': True},
            {'confirmed': True, 'fully_closed': 'true'},
        )
        for close_result in malformed:
            with self.subTest(close_result=close_result):
                system = self.make_system()
                system._submit_persisted_close = Mock(
                    return_value=close_result)
                system._quarantine_position_mismatch = Mock()
                old_position = {
                    'side': 'short', 'position_size': 2.0,
                    'stop_order_id': 'stop-1'}
                signal = {
                    'current_close': 110, 'lower_stop': 95,
                    'upper_stop': 125}

                system._flip_position(
                    'BTCUSDT', signal, old_position, 'long',
                    {'name': 'BTCUSDT'})

                system.exchange_api.cancel_order.assert_not_called()
                system.trade_state.close_position.assert_not_called()
                system._execute_open.assert_not_called()
                system._quarantine_position_mismatch.assert_called_once()

    def test_flip_aborts_reopen_when_stop_cancel_unconfirmed(self):
        """翻转时撤旧止损不可确认：平仓记账完成、不反手开新仓，
        但记录 T+1 交由次日重入（残留清理确认后恢复永远在市）。"""
        system = self.make_system()
        system.exchange_api.cancel_order.return_value = False
        system.exchange_api.cancel_all_orders.return_value = None
        old_position = {"side": "short", "position_size": 2.0, "stop_order_id": "stop-1"}
        signal = {"current_close": 110, "lower_stop": 95, "upper_stop": 125}

        system._flip_position("BTCUSDT", signal, old_position, "long", {"name": "BTCUSDT"})

        system.trade_state.close_position.assert_called_once_with(
            "BTCUSDT", 112.0, exit_fee=None, exit_fee_currency=None,
            exit_order_ids=["close-1"])
        system.trade_state.mark_stop_residue.assert_called_once_with("BTCUSDT")
        system._execute_open.assert_not_called()
        system.record_stop_loss.assert_called_once_with("BTCUSDT")  # 记 T+1，次日按 EMA 方向重入

    def test_flip_reopen_failure_does_not_fake_tplus1_stop(self):
        """翻转新腿失败不是止损：不推进 candle，由日内重试恢复目标方向。"""
        system = self.make_system()
        system.notifier.notify_signal_missed = Mock()
        system.trade_state.get_open_position = Mock(return_value=None)  # 反手开仓腿失败
        old_position = {"side": "short", "position_size": 2.0, "stop_order_id": "stop-1"}
        signal = {"current_close": 110, "lower_stop": 95, "upper_stop": 125}

        system._flip_position("BTCUSDT", signal, old_position, "long", {"name": "BTCUSDT"})

        system._execute_open.assert_called_once()                 # 尝试了反手
        system.record_stop_loss.assert_not_called()
        system.notifier.notify_signal_missed.assert_called_once()

    def test_handle_open_position_ma_cross_records_stop_loss_and_returns_when_exchange_position_missing(self):
        system = self.make_system()
        system.exchange_api.get_position = Mock(return_value=None)

        signal = {"current_close": 101}
        position = {"side": "long", "position_size": 2.0, "stop_loss_price": 99}

        system.handle_open_position_ma_cross(
            "BTCUSDT", signal, position, {"name": "BTCUSDT"}
        )

        system.trade_state.close_position.assert_called_once_with(
            "BTCUSDT", 99,
            stop_loss_date=date.today().strftime('%Y-%m-%d'),
            stop_cleanup_pending=True,
            exit_price_source='estimated_stop')
        self.assertEqual(system.stop_loss_dates['BTCUSDT'],
                         date.today().strftime('%Y-%m-%d'))
        system.record_stop_loss.assert_not_called()
        system._execute_open.assert_not_called()

class TradeStatePersistenceFailureTests(unittest.TestCase):
    def test_add_open_position_rolls_back_when_save_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "trade_state.json"
            ts = trade_state.TradeState(str(state_file))

            with patch.object(trade_state, "atomic_write_json", Mock(return_value=False)):
                with self.assertRaises(trade_state.TradeStatePersistenceError):
                    ts.add_open_position(
                        "BTCUSDT", "long", 100, 1, 90, "stop-1",
                        strategy="ma_cross")

            self.assertIsNone(ts.get_open_position("BTCUSDT"))

    def test_update_stop_loss_rolls_back_when_save_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "trade_state.json"
            ts = trade_state.TradeState(str(state_file))
            ts.add_open_position(
                "BTCUSDT", "long", 100, 1, 90, "stop-1",
                strategy="ma_cross")

            with patch.object(trade_state, "atomic_write_json", Mock(return_value=False)):
                with self.assertRaises(trade_state.TradeStatePersistenceError):
                    ts.update_stop_loss("BTCUSDT", 95, "stop-2")

            fresh = ts.get_open_position("BTCUSDT")
            self.assertEqual(fresh["stop_loss_price"], 90)
            self.assertEqual(fresh["stop_order_id"], "stop-1")

    def test_close_position_rolls_back_when_save_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "trade_state.json"
            ts = trade_state.TradeState(str(state_file))
            ts.add_open_position(
                "BTCUSDT", "long", 100, 1, 90, "stop-1",
                strategy="ma_cross")

            with patch.object(trade_state, "atomic_write_json", Mock(return_value=False)):
                with self.assertRaises(trade_state.TradeStatePersistenceError):
                    ts.close_position("BTCUSDT", 110)

            self.assertIsNotNone(ts.get_open_position("BTCUSDT"))
            self.assertEqual(len(ts.get_closed_trades()), 0)


class TradeStateCallsiteCompensationTests(unittest.TestCase):
    def test_execute_open_rolls_back_exchange_when_trade_state_persist_fails(self):
        system = ExecuteOpenRiskGuardTests().make_system()
        system.trade_state.add_open_position = Mock(
            side_effect=trade_state.TradeStatePersistenceError("disk full")
        )
        system.exchange_api.close_position.return_value = {
            "id": "close-1", "confirmed": True, "fully_closed": True,
            "amount": 2.5, "remaining_amount": 0.0, "average": 99.0,
        }

        system._execute_open(
            "BTCUSDT",
            "long",
            100,
            80,
            {"name": "BTCUSDT", "risk_per_trade": 0.01},
        )

        system.exchange_api.cancel_order.assert_called_once_with("BTC/USDT", "stop-1")
        system.exchange_api.close_position.assert_called_once_with(
            "BTC/USDT", "long", 2.5, client_order_id=ANY)
        system.notifier.notify_trade_opened.assert_not_called()

    def test_flip_position_stops_when_persist_fails(self):
        system = MaCrossFlipTests().make_system()
        system.trade_state.close_position.side_effect = trade_state.TradeStatePersistenceError("disk full")
        old_position = {"side": "short", "position_size": 2.0, "stop_order_id": "stop-1"}
        signal = {"current_close": 110, "lower_stop": 95, "upper_stop": 125}

        system._flip_position("BTCUSDT", signal, old_position, "long", {"name": "BTCUSDT"})

        system.trade_state.force_runtime_close_position.assert_called_once_with(
            "BTCUSDT", 112.0, exit_fee=None, exit_fee_currency=None,
            exit_order_ids=["close-1"])
        system._execute_open.assert_not_called()


class StartupSyncCompensationTests(unittest.TestCase):
    def make_system(self, *, side="long", stop_loss_price=90.0,
                    current_price=123.45):
        system = object.__new__(main.TradingSystem)
        system._stop_anomalies = {}
        last_price = Mock(return_value=current_price)
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=_fake_to_ccxt,
            get_position=Mock(return_value=None),
            cancel_order=Mock(return_value=True),
            cancel_all_orders=Mock(return_value=True),
            list_position_symbols=Mock(return_value=[]),
            get_last_price=last_price,
        )
        open_positions = {
            "BTCUSDT": {
                "entry_price": 100,
                "position_size": 2.0,
                "side": side,
                "stop_loss_price": stop_loss_price,
            }
        }
        system.trade_state = SimpleNamespace(
            get_all_open_positions=Mock(return_value=open_positions),
            get_open_position=Mock(
                side_effect=lambda symbol: open_positions.get(symbol)),
            force_runtime_close_position=Mock(return_value={"pnl": -20.0, "pnl_percent": -10.0}),
            close_position=Mock(return_value={"pnl": -20.0, "pnl_percent": -10.0}),
            mark_stop_residue=Mock(),
            force_runtime_mark_stop_residue=Mock(),
            clear_stop_residue=Mock(),
            has_stop_residue=Mock(return_value=False),
            get_stop_residues=Mock(return_value={}),
            mark_position_quarantine=Mock(),
            force_runtime_mark_position_quarantine=Mock(),
            get_position_quarantines=Mock(return_value={}),
            clear_position_quarantine=Mock(return_value=False),
            get_close_intent=Mock(return_value=None),
            get_open_intent=Mock(return_value=None),
            get_open_intents=Mock(return_value={}),
        )
        system.notifier = SimpleNamespace(notify_error=Mock())
        return system

    def test_sync_positions_uses_stop_estimate_and_runtime_fallback(self):
        # 旧代码会用重启时的裸市价：long 止损后反弹到 123.45、short 止损后
        # 下跌到 80 都会被错误记成盈利。正常持久化与磁盘故障运行时回退
        # 两条分支、两个方向都必须固定采用账本止损估值。
        for persist_fails in (False, True):
            for side, stop_price, current_price, expected_exit in (
                    ("long", 90.0, 123.45, 90.0),
                    ("short", 110.0, 80.0, 110.0),
                    ("long", None, 1_000_000.0, 100.0)):
                with self.subTest(
                        side=side, persist_fails=persist_fails):
                    system = self.make_system(
                        side=side, stop_loss_price=stop_price,
                        current_price=current_price)
                    if persist_fails:
                        system.trade_state.close_position.side_effect = (
                            trade_state.TradeStatePersistenceError("disk full"))

                    system.sync_positions_on_startup()

                    # 已停过的 ma_cross 仓在启动同步平仓时同事务记 T+1（今日），
                    # 防止当日 EMA 方向仍成立就立刻反手重入。
                    today = date.today().strftime('%Y-%m-%d')
                    expected_source = (
                        'estimated_stop'
                        if stop_price is not None
                        else 'estimated_entry_fallback')
                    system.trade_state.close_position.assert_called_once_with(
                        "BTCUSDT", expected_exit,
                        stop_loss_date=today, stop_cleanup_pending=True,
                        exit_price_source=expected_source)
                    if persist_fails:
                        system.trade_state.force_runtime_close_position.assert_called_once_with(
                            "BTCUSDT", expected_exit,
                            stop_loss_date=today, stop_cleanup_pending=True,
                            exit_price_source=expected_source)
                        system.notifier.notify_error.assert_called_once()
                    else:
                        system.trade_state.force_runtime_close_position.assert_not_called()
                        system.notifier.notify_error.assert_not_called()
                    system.exchange_api.get_last_price.assert_not_called()


class StartupSyncIsolationTests(unittest.TestCase):
    """启动对账：单品种异常必须隔离后继续，绝不连累其余品种或让构造裸崩。"""

    def test_single_symbol_exception_is_quarantined_not_fatal(self):
        system = StartupSyncCompensationTests().make_system()
        system.trade_state.get_all_open_positions = Mock(return_value={
            "AAAUSDT": {"entry_price": 100, "position_size": 2.0,
                        "side": "long", "stop_loss_price": 90.0},
            "BBBUSDT": {"entry_price": 100, "position_size": 2.0,
                        "side": "long", "stop_loss_price": 90.0},
        })
        aaa_position = {"contracts": 2, "side": "long", "symbol": "AAA/USDT",
                        "info": {"pos": "2", "posSide": "net"}}
        system.exchange_api.get_position = Mock(
            side_effect=lambda s: aaa_position if s.startswith("AAA") else None)
        # 模拟张数换算等未被内层捕获的崩溃（如启动时市场缓存缺失）。
        system._verify_existing_position_or_quarantine = Mock(
            side_effect=RuntimeError("张数换算崩溃"))
        system._quarantine_position_mismatch = Mock()

        system.sync_positions_on_startup()  # 不得抛出

        quarantined = [c.args for c in
                       system._quarantine_position_mismatch.call_args_list]
        self.assertTrue(any(
            args[0] == "AAAUSDT" and "启动对账异常" in args[1]
            for args in quarantined))
        # BBBUSDT 仍被正常对账（交易所空仓 → 按止损估值补记平仓，同事务记 T+1）。
        system.trade_state.close_position.assert_called_once_with(
            "BBBUSDT", 90.0,
            stop_loss_date=date.today().strftime('%Y-%m-%d'),
            stop_cleanup_pending=True,
            exit_price_source='estimated_stop')


class LoginBackoffTests(unittest.TestCase):
    """登录防爆破：连续失败按 IP 锁定，成功登录清零计数。"""

    def setUp(self):
        self.client = api_server.app.test_client()
        api_server._login_failures.clear()
        self.addCleanup(api_server._login_failures.clear)
        patcher = patch.object(api_server, "LOGIN_PASSWORD", "right-pass")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _login(self, password):
        return self.client.post("/api/login", json={"password": password})

    def test_lockout_after_max_failures(self):
        for _ in range(api_server.LOGIN_MAX_FAILURES):
            self.assertEqual(self._login("wrong").status_code, 401)
        resp = self._login("wrong")
        self.assertEqual(resp.status_code, 429)
        # 锁定期内连正确密码也被拒（退避先于校验）
        self.assertEqual(self._login("right-pass").status_code, 429)

    def test_success_clears_failure_streak(self):
        for _ in range(api_server.LOGIN_MAX_FAILURES - 1):
            self.assertEqual(self._login("wrong").status_code, 401)
        self.assertEqual(self._login("right-pass").status_code, 200)
        # 计数已清零：再错一次只是普通 401，不触发锁定
        self.assertEqual(self._login("wrong").status_code, 401)
        self.assertEqual(self._login("right-pass").status_code, 200)


class ApiTokenAuthTests(unittest.TestCase):
    """API Token 认证：非 ASCII token 头不得触发 compare_digest 的 TypeError → 500。"""

    def setUp(self):
        self.client = api_server.app.test_client()
        patcher = patch.object(api_server, "API_TOKEN", "real-token-abc")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_non_ascii_token_returns_401_not_500(self):
        # 此前 compare_digest(str,str) 对非 ASCII 抛 TypeError（装饰器无捕获）→ 500；现应干净 401
        resp = self.client.get("/api/status", headers={"X-API-Token": "café-café"})
        self.assertEqual(resp.status_code, 401)

    def test_wrong_token_returns_401(self):
        resp = self.client.get("/api/status", headers={"X-API-Token": "wrong-token"})
        self.assertEqual(resp.status_code, 401)


class DingtalkRedactionTests(unittest.TestCase):
    """钉钉 webhook access_token 不得随 requests 连接异常泄露到日志。"""

    def test_access_token_redacted_from_connection_error(self):
        import dingtalk_notifier
        # 复刻 requests ConnectionError 的真实形态（含完整 URL + token）
        leaked = ("HTTPSConnectionPool(host='oapi.dingtalk.com', port=443): Max retries "
                  "exceeded with url: /robot/send?access_token=SECRET_abc123def (Caused by ...)")
        red = dingtalk_notifier._redact_secrets(leaked)
        self.assertNotIn("SECRET_abc123def", red)
        self.assertIn("access_token=***", red)

    def test_redaction_leaves_ordinary_text_intact(self):
        import dingtalk_notifier
        self.assertEqual(dingtalk_notifier._redact_secrets("connection refused"), "connection refused")


class SchedulerIntervalTests(unittest.TestCase):
    """盘中止损巡检间隔的调度注册——用真实 BackgroundScheduler 触发 APScheduler
    的表达式校验（标准库套件把调度器换成 Dummy 桩，测不到这条真实崩溃路径）。

    缺陷背景：共享配置校验放行 stop_loss_scan_interval_minutes ∈ [1,1440]，
    但 register_jobs 曾一律用 cron minute='*/N'——N≥60 时 APScheduler 抛
    "step value higher than the total range (59)"，在 start() 的守护线程里让整个调度
    注册崩溃：Web 面板照常，但日检/巡检/采样一个都不注册（静默僵死）。
    """

    def _make(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        system = object.__new__(main.TradingSystem)
        system.scheduler = BackgroundScheduler()
        system.exchange_id = "okx"
        system.label = "欧易"
        # register_jobs 末尾会真正调用一次采样；其余 add_job 只登记 bound method 不执行
        system._record_equity_tick_with_alert = lambda: None
        return system

    def test_large_interval_registers_via_interval_trigger(self):
        from apscheduler.triggers.interval import IntervalTrigger
        for interval in (60, 120, 1440):
            system = self._make()
            # 旧实现在此对 '*/60' 抛 ValueError；修复后走 interval 触发器
            system.register_jobs({"stop_loss_scan_interval_minutes": interval})
            job = system.scheduler.get_job("okx_stoploss_scan")
            self.assertIsNotNone(job, f"间隔 {interval} 分钟应注册出巡检任务")
            self.assertIsInstance(job.trigger, IntervalTrigger)

    def test_sub_hour_interval_stays_cron(self):
        from apscheduler.triggers.cron import CronTrigger
        system = self._make()
        system.register_jobs({"stop_loss_scan_interval_minutes": 5})
        job = system.scheduler.get_job("okx_stoploss_scan")
        self.assertIsNotNone(job)
        self.assertIsInstance(job.trigger, CronTrigger)


class ApiProcessSafetyTests(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True

    def test_shared_runtime_health_owns_exact_heartbeat_boundary(self):
        now = 10_000.0
        raw = {
            'scheduler_running': True,
            'scheduler_thread_alive': True,
            'runner_heartbeat_ts': now - 150,
            'persistence_degraded': False,
            'persistence_degraded_context': None,
            'safety_blockers': {
                'open_intents': 0, 'close_intents': 0,
                'position_quarantines': 0, 'stop_residues': 0},
            'trade_check_failure': None,
            'guardian_failure': None,
            'daily_check_overdue': False,
            'expected_daily_check_date': None,
            'stopping': False,
        }

        boundary = runtime_guard.assess_runtime_health(raw, now)
        raw['runner_heartbeat_ts'] = now - 150.001
        stale = runtime_guard.assess_runtime_health(raw, now)

        self.assertTrue(boundary['healthy'])
        self.assertEqual(150, boundary['heartbeat_age_seconds'])
        self.assertFalse(stale['healthy'])
        self.assertIn('runner_heartbeat_stale', stale['issues'])

    def test_main_and_api_delegate_runtime_health_to_shared_assessor(self):
        system = object.__new__(main.TradingSystem)
        system.scheduler = SimpleNamespace(
            running=True,
            _thread=SimpleNamespace(is_alive=lambda: True))
        system.trade_state = SimpleNamespace(
            get_runtime_persistence_status=lambda: {
                'degraded': False, 'context': None},
            get_safety_blocker_counts=lambda: {
                'open_intents': 0, 'close_intents': 0,
                'position_quarantines': 0, 'stop_residues': 0})
        system._heartbeat_lock = threading.Lock()
        system._runner_heartbeat_ts = 9_900.0
        system._stop_event = threading.Event()
        system._daily_check_readiness = lambda: (False, '2026-07-19')
        system._last_trade_check_failure = None
        system._last_guardian_failure = None

        with patch.object(
                main, 'assess_runtime_health',
                wraps=runtime_guard.assess_runtime_health) as main_assessor, \
                patch.object(main.time, 'time', return_value=10_000.0):
            main_snapshot = system.health_snapshot()
        main_assessor.assert_called_once()
        self.assertTrue(main_snapshot['healthy'])

        live_thread = SimpleNamespace(is_alive=lambda: True)
        with patch.object(
                api_server, 'assess_runtime_health',
                wraps=runtime_guard.assess_runtime_health) as api_assessor, \
                patch.object(api_server, '_runner_thread', live_thread), \
                patch.object(api_server, '_runner_failure', None), \
                patch.object(api_server.time, 'time', return_value=10_000.0):
            api_snapshot = api_server._runner_health(
                SimpleNamespace(health_snapshot=lambda: main_snapshot))
        api_assessor.assert_called_once_with(main_snapshot, 10_000.0)
        self.assertTrue(api_snapshot['healthy'])

    def test_main_health_collector_never_coerces_invalid_probes(self):
        system = object.__new__(main.TradingSystem)
        system.scheduler = SimpleNamespace(
            running='false',
            _thread=SimpleNamespace(is_alive=lambda: 'false'))
        system.trade_state = SimpleNamespace(
            get_runtime_persistence_status=lambda: {
                'degraded': False, 'context': None},
            get_safety_blocker_counts=lambda: {
                'open_intents': 0, 'close_intents': 0,
                'position_quarantines': 0, 'stop_residues': 0})
        system._heartbeat_lock = threading.Lock()
        system._runner_heartbeat_ts = time.time()
        system._stop_event = threading.Event()
        system._daily_check_readiness = lambda: (False, '2026-07-19')
        system._last_trade_check_failure = None
        system._last_guardian_failure = None

        snapshot = system.health_snapshot()

        self.assertFalse(snapshot['healthy'])
        self.assertFalse(snapshot['scheduler_running'])
        self.assertFalse(snapshot['scheduler_thread_alive'])
        self.assertIn('scheduler_running_invalid', snapshot['issues'])
        self.assertIn('scheduler_thread_state_invalid', snapshot['issues'])
        with patch.object(
                api_server, '_runner_thread',
                SimpleNamespace(is_alive=lambda: True)), \
                patch.object(api_server, '_runner_failure', None):
            api_snapshot = api_server._runner_health(system)
        self.assertIn('scheduler_running_invalid', api_snapshot['issues'])
        self.assertIn(
            'scheduler_thread_state_invalid', api_snapshot['issues'])

    def test_main_health_collector_requires_both_failure_latches(self):
        for missing in ('_last_trade_check_failure',
                        '_last_guardian_failure'):
            system = object.__new__(main.TradingSystem)
            system.scheduler = SimpleNamespace(
                running=True,
                _thread=SimpleNamespace(is_alive=lambda: True))
            system.trade_state = SimpleNamespace(
                get_runtime_persistence_status=lambda: {
                    'degraded': False, 'context': None},
                get_safety_blocker_counts=lambda: {
                    'open_intents': 0, 'close_intents': 0,
                    'position_quarantines': 0, 'stop_residues': 0})
            system._heartbeat_lock = threading.Lock()
            system._runner_heartbeat_ts = time.time()
            system._stop_event = threading.Event()
            system._daily_check_readiness = lambda: (False, '2026-07-19')
            system._last_trade_check_failure = None
            system._last_guardian_failure = None
            delattr(system, missing)

            with self.subTest(missing=missing):
                main_snapshot = system.health_snapshot()
                expected = (
                    'trade_check_state_missing'
                    if missing == '_last_trade_check_failure'
                    else 'guardian_state_missing')
                self.assertFalse(main_snapshot['healthy'])
                self.assertIn(expected, main_snapshot['issues'])
                with patch.object(
                        api_server, '_runner_thread',
                        SimpleNamespace(is_alive=lambda: True)), \
                        patch.object(api_server, '_runner_failure', None):
                    api_snapshot = api_server._runner_health(system)
                self.assertIn(expected, api_snapshot['issues'])

    def test_api_runner_thread_probe_is_strict_and_exception_safe(self):
        now = time.time()
        healthy = {
            'healthy': True,
            'scheduler_running': True,
            'scheduler_thread_alive': True,
            'runner_heartbeat_ts': now,
            'persistence_degraded': False,
            'persistence_degraded_context': None,
            'safety_blockers': {
                'open_intents': 0, 'close_intents': 0,
                'position_quarantines': 0, 'stop_residues': 0},
            'trade_check_failure': None,
            'guardian_failure': None,
            'daily_check_overdue': False,
            'expected_daily_check_date': None,
            'stopping': False,
        }

        def raise_probe_error():
            raise RuntimeError('thread probe unavailable')

        for label, thread in (
                ('invalid', SimpleNamespace(is_alive=lambda: 'false')),
                ('raises', SimpleNamespace(is_alive=raise_probe_error))):
            with self.subTest(label=label), \
                    patch.object(api_server, '_runner_thread', thread), \
                    patch.object(api_server, '_runner_failure', None), \
                    patch.object(api_server.time, 'time', return_value=now):
                snapshot = api_server._runner_health(
                    SimpleNamespace(health_snapshot=lambda: healthy))
            self.assertFalse(snapshot['healthy'])
            self.assertFalse(snapshot['runner_thread_alive'])
            self.assertIn('runner_thread_state_invalid', snapshot['issues'])

    def test_status_is_503_when_scheduler_stopped(self):
        system = SimpleNamespace(
            trade_state=SimpleNamespace(
                get_all_open_positions=lambda: {}, get_stop_residues=lambda: {},
                get_open_intents=lambda: {},
                get_position_quarantines=lambda: {},
                get_last_daily_check_date=lambda: '2026-07-18'),
            config={"trading": {"symbols": []}},
            config_file="/missing/config.json",
            scheduler=SimpleNamespace(running=False),
            exchange_id="okx", label="欧易", _stop_anomalies={},
        )
        dead_thread = SimpleNamespace(is_alive=lambda: False)
        with patch.object(api_server, "trading_system", system), \
             patch.object(api_server, "_runner_thread", dead_thread), \
             patch.object(api_server, "_runner_failure", "runner crashed"):
            resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["status"], "degraded")
        self.assertEqual(resp.get_json()["open_intents_count"], 0)
        self.assertEqual(resp.get_json()["last_daily_check_date"], "2026-07-18")

    def test_live_runner_without_health_contract_is_never_healthy(self):
        system = SimpleNamespace(
            trade_state=SimpleNamespace(
                get_all_open_positions=lambda: {}, get_stop_residues=lambda: {},
                get_open_intents=lambda: {},
                get_position_quarantines=lambda: {},
                get_last_daily_check_date=lambda: None),
            config={"trading": {"symbols": []}},
            config_file="/missing/config.json",
            scheduler=SimpleNamespace(running=True),
            exchange_id="okx", label="欧易", _stop_anomalies={},
        )
        live_thread = SimpleNamespace(is_alive=lambda: True)

        with patch.object(api_server, "trading_system", system), \
             patch.object(api_server, "_runner_thread", live_thread), \
             patch.object(api_server, "_runner_failure", None):
            resp = self.client.get("/api/status")

        self.assertEqual(503, resp.status_code)
        self.assertIn(
            'system_health_unavailable', resp.get_json()['health']['issues'])

    def test_status_is_503_when_scheduler_thread_died_but_state_says_running(self):
        system = SimpleNamespace(
            trade_state=SimpleNamespace(
                get_all_open_positions=lambda: {}, get_stop_residues=lambda: {},
                get_open_intents=lambda: {},
                get_position_quarantines=lambda: {},
                get_last_daily_check_date=lambda: None),
            config={"trading": {"symbols": []}},
            config_file="/missing/config.json",
            scheduler=SimpleNamespace(running=True),
            exchange_id="okx", label="欧易", _stop_anomalies={},
            health_snapshot=lambda: {
                "healthy": False,
                "scheduler_running": True,
                "scheduler_thread_alive": False,
                "runner_heartbeat_ts": api_server.time.time(),
                "persistence_degraded": False,
                "persistence_degraded_context": None,
                "safety_blockers": {
                    "open_intents": 0, "close_intents": 0,
                    "position_quarantines": 0, "stop_residues": 0},
                "trade_check_failure": None,
                "guardian_failure": None,
                "daily_check_overdue": False,
                "expected_daily_check_date": None,
                "stopping": False,
            },
        )
        live_thread = SimpleNamespace(is_alive=lambda: True)
        with patch.object(api_server, "trading_system", system), \
             patch.object(api_server, "_runner_thread", live_thread), \
             patch.object(api_server, "_runner_failure", None):
            resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("scheduler_thread_stopped", resp.get_json()["health"]["issues"])

    def test_health_snapshot_never_infers_missing_scheduler_thread_alive(self):
        system = object.__new__(main.TradingSystem)
        system.scheduler = SimpleNamespace(running=True)
        system.trade_state = SimpleNamespace(
            get_runtime_persistence_status=lambda: {
                'degraded': False, 'context': None},
            get_safety_blocker_counts=lambda: {
                'open_intents': 0, 'close_intents': 0,
                'position_quarantines': 0, 'stop_residues': 0})
        system._heartbeat_lock = threading.Lock()
        system._runner_heartbeat_ts = time.time()
        system._stop_event = threading.Event()

        snapshot = system.health_snapshot()

        self.assertFalse(snapshot['healthy'])
        self.assertFalse(snapshot['scheduler_thread_alive'])

    def test_scheduler_thread_probe_failure_is_degraded_not_exception(self):
        class BrokenThread:
            def is_alive(self):
                raise RuntimeError('thread state unavailable')

        system = object.__new__(main.TradingSystem)
        system.scheduler = SimpleNamespace(running=True, _thread=BrokenThread())
        system.trade_state = SimpleNamespace(
            get_runtime_persistence_status=lambda: {
                'degraded': False, 'context': None},
            get_safety_blocker_counts=lambda: {
                'open_intents': 0, 'close_intents': 0,
                'position_quarantines': 0, 'stop_residues': 0})
        system._heartbeat_lock = threading.Lock()
        system._runner_heartbeat_ts = time.time()
        system._stop_event = threading.Event()

        snapshot = system.health_snapshot()

        self.assertFalse(snapshot['healthy'])
        self.assertFalse(snapshot['scheduler_thread_alive'])

    def test_nonfinite_or_future_heartbeat_can_never_be_healthy(self):
        live_thread = SimpleNamespace(is_alive=lambda: True)
        for heartbeat in (
                float('nan'), float('inf'), float('-inf'),
                api_server.time.time() + 60):
            with self.subTest(heartbeat=heartbeat):
                system = SimpleNamespace(health_snapshot=lambda value=heartbeat: {
                    'healthy': True,
                    'scheduler_running': True,
                    'scheduler_thread_alive': True,
                    'runner_heartbeat_ts': value,
                    'persistence_degraded': False,
                    'persistence_degraded_context': None,
                    'safety_blockers': {
                        'open_intents': 0, 'close_intents': 0,
                        'position_quarantines': 0, 'stop_residues': 0},
                    'trade_check_failure': None,
                    'guardian_failure': None,
                    'daily_check_overdue': False,
                    'expected_daily_check_date': None,
                    'stopping': False,
                })
                with patch.object(api_server, '_runner_thread', live_thread), \
                        patch.object(api_server, '_runner_failure', None):
                    snapshot = api_server._runner_health(system)
                self.assertFalse(snapshot['healthy'])
                self.assertIn(
                    'runner_heartbeat_invalid', snapshot['issues'])

    def test_health_boolean_fields_require_actual_bools(self):
        live_thread = SimpleNamespace(is_alive=lambda: True)
        system = SimpleNamespace(health_snapshot=lambda: {
            'healthy': True,
            'scheduler_running': 'false',
            'scheduler_thread_alive': 1,
            'runner_heartbeat_ts': api_server.time.time(),
            'persistence_degraded': False,
            'persistence_degraded_context': None,
            'safety_blockers': {
                'open_intents': 0, 'close_intents': 0,
                'position_quarantines': 0, 'stop_residues': 0},
            'trade_check_failure': None,
            'guardian_failure': None,
            'daily_check_overdue': False,
            'expected_daily_check_date': None,
            'stopping': '',
        })
        with patch.object(api_server, '_runner_thread', live_thread), \
                patch.object(api_server, '_runner_failure', None):
            snapshot = api_server._runner_health(system)

        self.assertFalse(snapshot['healthy'])
        self.assertIn('scheduler_running_invalid', snapshot['issues'])
        self.assertIn('scheduler_thread_state_invalid', snapshot['issues'])
        self.assertIn('runner_stopping_state_invalid', snapshot['issues'])

    def test_system_reported_unhealthy_cannot_be_reassembled_as_healthy(self):
        live_thread = SimpleNamespace(is_alive=lambda: True)
        system = SimpleNamespace(health_snapshot=lambda: {
            'healthy': False,
            'scheduler_running': True,
            'scheduler_thread_alive': True,
            'runner_heartbeat_ts': api_server.time.time(),
            'persistence_degraded': False,
            'persistence_degraded_context': None,
            'safety_blockers': {
                'open_intents': 0, 'close_intents': 0,
                'position_quarantines': 0, 'stop_residues': 0},
            'trade_check_failure': None,
            'guardian_failure': {'kind': 'guardian_failures'},
            'daily_check_overdue': False,
            'expected_daily_check_date': None,
            'stopping': False,
        })
        with patch.object(api_server, '_runner_thread', live_thread), \
                patch.object(api_server, '_runner_failure', None):
            snapshot = api_server._runner_health(system)

        self.assertFalse(snapshot['healthy'])
        self.assertIn('system_reported_unhealthy', snapshot['issues'])
        self.assertIn('guardian_failed', snapshot['issues'])

    def test_trade_check_health_state_is_required_and_typed(self):
        live_thread = SimpleNamespace(is_alive=lambda: True)
        base = {
            'healthy': True,
            'scheduler_running': True,
            'scheduler_thread_alive': True,
            'runner_heartbeat_ts': api_server.time.time(),
            'persistence_degraded': False,
            'persistence_degraded_context': None,
            'safety_blockers': {
                'open_intents': 0, 'close_intents': 0,
                'position_quarantines': 0, 'stop_residues': 0},
            'guardian_failure': None,
            'daily_check_overdue': False,
            'expected_daily_check_date': None,
            'stopping': False,
        }
        for label, value, expected in (
                ('missing', None, 'trade_check_state_missing'),
                ('invalid', 'bad', 'trade_check_state_invalid')):
            payload = dict(base)
            if label != 'missing':
                payload['trade_check_failure'] = value
            system = SimpleNamespace(
                health_snapshot=lambda payload=payload: payload)
            with self.subTest(label=label), \
                    patch.object(api_server, '_runner_thread', live_thread), \
                    patch.object(api_server, '_runner_failure', None):
                snapshot = api_server._runner_health(system)
            self.assertFalse(snapshot['healthy'])
            self.assertIn(expected, snapshot['issues'])

    def test_guardian_and_daily_health_fields_are_required(self):
        live_thread = SimpleNamespace(is_alive=lambda: True)
        base = {
            'healthy': True,
            'scheduler_running': True,
            'scheduler_thread_alive': True,
            'runner_heartbeat_ts': api_server.time.time(),
            'persistence_degraded': False,
            'persistence_degraded_context': None,
            'safety_blockers': {
                'open_intents': 0, 'close_intents': 0,
                'position_quarantines': 0, 'stop_residues': 0},
            'trade_check_failure': None,
            'guardian_failure': None,
            'daily_check_overdue': False,
            'expected_daily_check_date': None,
            'stopping': False,
        }
        cases = (
            ('guardian_failure', 'guardian_state_missing'),
            ('daily_check_overdue', 'daily_check_state_invalid'),
        )
        for missing, expected in cases:
            payload = dict(base)
            payload.pop(missing)
            system = SimpleNamespace(
                health_snapshot=lambda payload=payload: payload)
            with self.subTest(missing=missing), \
                    patch.object(api_server, '_runner_thread', live_thread), \
                    patch.object(api_server, '_runner_failure', None):
                snapshot = api_server._runner_health(system)
            self.assertFalse(snapshot['healthy'])
            self.assertIn(expected, snapshot['issues'])

    def test_runtime_only_ledger_fallback_latches_health_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = trade_state.TradeState(
                str(Path(tmp) / 'trade_state.json'))
            state.force_runtime_mark_stop_residue('BTCUSDT')
            persistence = state.get_runtime_persistence_status()
            self.assertTrue(persistence['degraded'])

            system = object.__new__(main.TradingSystem)
            system.trade_state = state
            system.scheduler = SimpleNamespace(
                running=True,
                _thread=SimpleNamespace(is_alive=lambda: True))
            system._heartbeat_lock = threading.Lock()
            system._runner_heartbeat_ts = time.time()
            system._stop_event = threading.Event()
            live_thread = SimpleNamespace(is_alive=lambda: True)
            with patch.object(api_server, '_runner_thread', live_thread), \
                    patch.object(api_server, '_runner_failure', None):
                snapshot = api_server._runner_health(system)

            self.assertFalse(snapshot['healthy'])
            self.assertIn(
                'runtime_persistence_degraded', snapshot['issues'])
            self.assertEqual(
                'runtime_only_trade_state_mutation',
                snapshot['persistence_degraded_context'])

    def test_trades_are_bounded_and_paginated_newest_first(self):
        trades = [{"symbol": f"T{i}"} for i in range(250)]
        system = SimpleNamespace(
            trade_state=SimpleNamespace(get_closed_trades=lambda: trades))
        with patch.object(api_server, "trading_system", system):
            resp = self.client.get("/api/trades?page=2&page_size=100")
        payload = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(payload["total"], 250)
        self.assertEqual(len(payload["trades"]), 100)
        self.assertEqual(payload["trades"][0]["symbol"], "T149")

    def test_logs_reads_tail_and_skips_noisy_http_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "trading.log"
            with log_path.open("w", encoding="utf-8") as f:
                f.write("old\n")
                f.write("X" * 12000 + "\n")
                f.write('127.0.0.1 "GET / HTTP/1.1" 200\n')
                f.write("keep-one\n")
                f.write("code 400, message Bad request\n")
                f.write("keep-two\n")
            with patch.object(api_server, "__file__", str(Path(tmp) / "api_server.py")):
                resp = self.client.get("/api/logs?lines=2")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["logs"], ["keep-two\n", "keep-one\n"])

    def test_logs_use_runtime_data_dir_not_release_code_tree(self):
        with tempfile.TemporaryDirectory() as runtime, \
                tempfile.TemporaryDirectory() as code:
            runtime_log = Path(runtime) / "trading.log"
            runtime_log.write_text("runtime-log\n", encoding="utf-8")
            (Path(code) / "trading.log").write_text(
                "wrong-code-tree-log\n", encoding="utf-8")
            with patch.dict(
                    os.environ, {"TRADING_DATA_DIR": runtime}, clear=False), \
                    patch.object(
                        api_server, "__file__", str(Path(code) / "api_server.py")):
                resp = self.client.get("/api/logs?lines=1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["logs"], ["runtime-log\n"])

    def test_delete_cleans_metadata_inside_trade_lock_after_exchange_flat(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = trade_state.TradeState(str(Path(tmp) / "trade_state.json"))
            state.mark_candle_processed("BTCUSDT", "c1")
            state.replace_stop_loss_dates({"BTCUSDT": "2026-07-10"})
            state.mark_position_quarantine("BTCUSDT", "old mismatch")
            trade_lock = threading.Lock()
            real_remove = state.remove_symbol_metadata

            def checked_remove(*args, **kwargs):
                self.assertTrue(trade_lock.locked())
                return real_remove(*args, **kwargs)

            state.remove_symbol_metadata = checked_remove
            system = SimpleNamespace(
                _trade_lock=trade_lock,
                _config_lock=threading.RLock(),
                trade_state=state,
                exchange_api=SimpleNamespace(
                    to_ccxt_symbol=lambda _s: "BTC/USDT:USDT",
                    get_position=lambda _s: None,
                ),
                config={"trading": {"symbols": [{
                    "name": "BTCUSDT", "enabled": True, "risk_per_trade": 0.01,
                }]}},
                persist_config=lambda: True,
                reload_strategies=Mock(), label="欧易",
            )
            with patch.object(api_server, "trading_system", system), \
                 patch.object(api_server, "send_dingtalk", Mock()):
                resp = self.client.delete("/api/symbols/BTCUSDT")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(state.get_signal_metadata("BTCUSDT"), {})
            self.assertNotIn("BTCUSDT", state.get_stop_loss_dates())
            self.assertFalse(state.is_position_quarantined("BTCUSDT"))

    def test_equity_sync_rejects_non_json_even_when_body_empty(self):
        system = SimpleNamespace()
        with patch.object(api_server, "trading_system", system):
            resp = self.client.post("/api/equity_sync", data="")
        self.assertEqual(resp.status_code, 400)

    def test_equity_sync_rejects_unknown_field_without_mutating_state(self):
        sync = Mock()
        system = SimpleNamespace(equity_tracker=SimpleNamespace(equity_sync=sync))
        with patch.object(api_server, "trading_system", system):
            resp = self.client.post("/api/equity_sync", json={"flow_amout": 1000})
        self.assertEqual(resp.status_code, 400)
        sync.assert_not_called()

    def test_config_notifications_run_after_trade_lock_release(self):
        trade_lock = threading.Lock()

        def assert_unlocked(_message):
            self.assertFalse(trade_lock.locked())

        system = SimpleNamespace(
            _trade_lock=trade_lock,
            _config_lock=threading.RLock(),
            trade_state=SimpleNamespace(
                get_open_position=lambda _s: None,
                get_open_intent=lambda _s: None,
            ),
            config={
                "trading": {"symbols": [{
                    "name": "BTCUSDT", "enabled": True,
                    "risk_per_trade": 0.01,
                }]},
                "strategy": {
                    "ma_short_period": 7,
                    "ma_long_period": 28, "default_risk_per_trade": 0.01,
                },
            },
            persist_config=lambda: True,
            reload_strategies=Mock(),
            label="欧易",
        )
        with patch.object(api_server, "trading_system", system), \
             patch.object(api_server, "send_dingtalk", side_effect=assert_unlocked):
            symbol_resp = self.client.put(
                "/api/symbols/BTCUSDT", json={"risk_per_trade": 0.02})
            strategy_resp = self.client.put(
                "/api/strategy_params", json={"ma_short_period": 6})
        self.assertEqual(symbol_resp.status_code, 200)
        self.assertEqual(strategy_resp.status_code, 200)

    def test_login_failure_cache_has_hard_limit(self):
        with api_server._login_guard:
            api_server._login_failures.clear()
            api_server._login_failures.update({
                "1.1.1.1": (1, 0), "2.2.2.2": (1, 0), "3.3.3.3": (1, 0)})
            with patch.object(api_server, "LOGIN_FAILURE_CACHE_MAX", 3):
                api_server._prune_login_failures(100)
            self.assertEqual(len(api_server._login_failures), 2)
            api_server._login_failures.clear()

    def test_runner_stop_waits_for_registered_thread(self):
        stopped = threading.Event()
        started = threading.Event()

        class Runner:
            def start(self):
                started.set()
                stopped.wait(2)

            def stop(self):
                stopped.set()

        runner = Runner()
        with patch.object(api_server, "_runner_thread", None), \
             patch.object(api_server, "_runner_started_at", None), \
             patch.object(api_server, "_runner_failure", None), \
             patch.object(api_server, "trading_system", runner):
            thread = api_server.start_runner_thread(runner)
            self.assertTrue(started.wait(1))
            self.assertTrue(api_server.stop_runner_thread(timeout=1))
            self.assertFalse(thread.is_alive())

    def test_real_runner_lifecycle_reports_healthy_then_shuts_scheduler_down(self):
        started = threading.Event()
        shutdown_wait = []

        class Scheduler:
            def __init__(self):
                self.running = False
                self._thread = SimpleNamespace(
                    is_alive=lambda: self.running)

            def start(self):
                self.running = True
                started.set()

            def shutdown(self, wait=True):
                shutdown_wait.append(wait)
                self.running = False

        system = object.__new__(main.TradingSystem)
        system.label = "欧易"
        system.config = {"scheduler": {}}
        system.scheduler = Scheduler()
        system.trade_state = SimpleNamespace(
            get_runtime_persistence_status=lambda: {
                'degraded': False, 'context': None},
            get_safety_blocker_counts=lambda: {
                'open_intents': 0, 'close_intents': 0,
                'position_quarantines': 0, 'stop_residues': 0})
        system._stop_event = threading.Event()
        system._heartbeat_lock = threading.Lock()
        system._runner_heartbeat_ts = None
        system._last_trade_check_failure = None
        system._last_guardian_failure = None
        system._last_check_date = None
        system._last_check_date = system._daily_check_readiness(
            datetime.now())[1]
        system.register_jobs = lambda _cfg: None
        system._run_startup_catchup_check = lambda: None

        with patch.object(api_server, "_runner_thread", None), \
             patch.object(api_server, "_runner_started_at", None), \
             patch.object(api_server, "_runner_failure", None), \
             patch.object(api_server, "trading_system", system):
            thread = api_server.start_runner_thread(system)
            self.assertTrue(started.wait(1))
            self.assertTrue(api_server._runner_health(system)["healthy"])
            self.assertTrue(api_server.stop_runner_thread(timeout=1))
            self.assertFalse(thread.is_alive())
            self.assertEqual(shutdown_wait, [True])
            self.assertIsNone(system._runner_heartbeat_ts)

    def test_startup_exception_clears_heartbeat_and_stops_partial_scheduler(self):
        class Scheduler:
            running = True

            def shutdown(self, wait=True):
                self.running = False

        system = object.__new__(main.TradingSystem)
        system.label = "欧易"
        system.config = {"scheduler": {}}
        system.scheduler = Scheduler()
        system._stop_event = threading.Event()
        system._heartbeat_lock = threading.Lock()
        system._runner_heartbeat_ts = 123
        system.register_jobs = Mock(side_effect=RuntimeError("register failed"))

        with self.assertRaises(RuntimeError):
            system.start()
        self.assertFalse(system.scheduler.running)
        self.assertIsNone(system._runner_heartbeat_ts)

    def test_preexisting_stop_request_is_not_cleared_or_allowed_to_run_catchup(self):
        class Scheduler:
            running = False

            def start(self):
                self.running = True

            def shutdown(self, wait=True):
                self.running = False

        system = object.__new__(main.TradingSystem)
        system.label = "欧易"
        system.config = {"scheduler": {}}
        system.scheduler = Scheduler()
        system._stop_event = threading.Event()
        system._stop_event.set()
        system._heartbeat_lock = threading.Lock()
        system._runner_heartbeat_ts = None
        system.register_jobs = lambda _cfg: None
        system._run_startup_catchup_check = Mock()

        system.start()

        system._run_startup_catchup_check.assert_not_called()
        self.assertFalse(system.scheduler.running)
        self.assertIsNone(system._runner_heartbeat_ts)

    def test_gunicorn_worker_hook_requests_graceful_runner_stop(self):
        import importlib.util
        config_path = APP_DIR / "gunicorn.conf.py"
        spec = importlib.util.spec_from_file_location("gunicorn_config_for_test", config_path)
        gunicorn_config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gunicorn_config)
        worker = SimpleNamespace(log=SimpleNamespace(critical=Mock(), exception=Mock()))
        with patch.object(api_server, "stop_runner_thread", return_value=True) as stop:
            gunicorn_config.worker_int(worker)
        stop.assert_called_once_with(timeout=gunicorn_config.graceful_timeout - 5)

    def test_graceful_stop_also_waits_for_manual_trade_check(self):
        release = threading.Event()
        manual = threading.Thread(target=lambda: release.wait(2), daemon=True)
        manual.start()
        system = SimpleNamespace(stop=release.set)
        with patch.object(api_server, "trading_system", system), \
             patch.object(api_server, "_runner_thread", None), \
             patch.object(api_server, "_manual_check_thread", manual):
            self.assertTrue(api_server.stop_runner_thread(timeout=1))
        self.assertFalse(manual.is_alive())

    def test_manual_partial_close_keeps_ledger_and_stop(self):
        system = object.__new__(main.TradingSystem)
        system._trade_lock = threading.Lock()
        system.label = "欧易"
        system.trade_state = SimpleNamespace(
            get_open_position=lambda _s: {
                "symbol": "BTCUSDT", "side": "long", "position_size": 1.0,
                "entry_price": 100.0, "stop_loss_price": 90.0, "stop_order_id": "stop-1",
            },
        )
        system.notifier = SimpleNamespace(notify_error=Mock())
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda _s: "BTC/USDT:USDT",
            close_position=lambda *_args: {
                "id": "partial", "average": 101.0,
                "amount": 0.4, "remaining_amount": 0.6,
                "confirmed": True, "fully_closed": False,
            },
        )
        system._cancel_stop_order_confirmed = Mock()
        system._close_trade_state_with_runtime_fallback = Mock()
        system._handle_partial_close = Mock(return_value=False)
        system._submit_persisted_close = Mock(return_value={
            "id": "partial", "average": 101.0, "amount": 0.4,
            "remaining_amount": 0.6, "confirmed": True,
            "fully_closed": False,
        })

        with patch.object(api_server, "trading_system", system):
            resp = self.client.post("/api/close_position", json={"name": "BTCUSDT"})

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["status"], "partial")
        system._cancel_stop_order_confirmed.assert_not_called()
        system._close_trade_state_with_runtime_fallback.assert_not_called()
        system._handle_partial_close.assert_called_once()

    def test_manual_close_preserves_ledger_and_stop_on_unproven_result(self):
        malformed = (
            {'fully_closed': True},
            {'confirmed': None, 'fully_closed': True},
            {'confirmed': 'true', 'fully_closed': True},
            {'confirmed': False, 'fully_closed': True},
            {'confirmed': True},
            {'confirmed': True, 'fully_closed': 'true'},
        )
        for close_result in malformed:
            with self.subTest(close_result=close_result):
                system = object.__new__(main.TradingSystem)
                system._trade_lock = threading.Lock()
                system.label = '欧易'
                system.trade_state = SimpleNamespace(
                    get_open_position=lambda _symbol: {
                        'symbol': 'BTCUSDT', 'side': 'long',
                        'position_size': 1.0, 'entry_price': 100.0,
                        'stop_loss_price': 90.0,
                        'stop_order_id': 'stop-old'})
                system.exchange_api = SimpleNamespace(
                    to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT')
                system._submit_persisted_close = Mock(
                    return_value=close_result)
                system._quarantine_position_mismatch = Mock()
                system._cancel_stop_order_confirmed = Mock()
                system._close_trade_state_with_runtime_fallback = Mock()
                system.notifier = SimpleNamespace(notify_error=Mock())

                with patch.object(api_server, 'trading_system', system):
                    resp = self.client.post(
                        '/api/close_position', json={'name': 'BTCUSDT'})

                self.assertEqual(409, resp.status_code)
                self.assertEqual('unresolved', resp.get_json()['status'])
                system._cancel_stop_order_confirmed.assert_not_called()
                system._close_trade_state_with_runtime_fallback.assert_not_called()
                system._quarantine_position_mismatch.assert_called_once()

    def test_manual_partial_close_delegates_atomic_reconciliation(self):
        handler = Mock(return_value=True)
        system = SimpleNamespace(
            _trade_lock=threading.Lock(), label="欧易",
            trade_state=SimpleNamespace(get_open_position=lambda _s: {
                "symbol": "BTCUSDT", "side": "long", "position_size": 1.0,
                "entry_price": 100.0, "stop_loss_price": 90.0,
                "stop_order_id": "stop-old",
            }),
            exchange_api=SimpleNamespace(
                to_ccxt_symbol=lambda _s: "BTC/USDT:USDT",
                close_position=lambda *_args: {
                    "id": "partial", "average": 101.0,
                    "amount": 0.4, "remaining_amount": 0.6,
                    "confirmed": True, "fully_closed": False,
                },
            ),
            _handle_partial_close=handler,
            _submit_persisted_close=Mock(return_value={
                "id": "partial", "average": 101.0,
                "amount": 0.4, "remaining_amount": 0.6,
                "confirmed": True, "fully_closed": False,
            }),
            _classify_close_execution=(
                trade_executor.TradeExecutorMixin()
                ._classify_close_execution),
            _cancel_stop_order_confirmed=Mock(),
            _close_trade_state_with_runtime_fallback=Mock(),
        )
        with patch.object(api_server, "trading_system", system):
            resp = self.client.post("/api/close_position", json={"name": "BTCUSDT"})
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.get_json()["safely_reconciled"])
        handler.assert_called_once()
        system._cancel_stop_order_confirmed.assert_not_called()
        system._close_trade_state_with_runtime_fallback.assert_not_called()


class DingTalkResponseValidationTests(unittest.TestCase):
    def test_http_200_non_json_is_failure(self):
        import dingtalk_notifier

        response = SimpleNamespace(
            status_code=200,
            text="<html>gateway</html>",
            json=Mock(side_effect=ValueError("not json")),
        )
        notifier = dingtalk_notifier.DingTalkNotifier(
            "https://oapi.dingtalk.com/robot/send?access_token=secret")
        with patch.object(dingtalk_notifier.requests, "post", return_value=response) as post, \
             patch.object(dingtalk_notifier.time, "sleep", return_value=None):
            self.assertFalse(notifier.send_message("title", "body"))
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
