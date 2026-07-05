import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tempfile

APP_DIR = Path(
    os.environ.get(
        "TRADING_SYSTEM_DIR",
        Path(__file__).resolve().parents[1],
    )
)
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("TRADING_API_TOKEN", "test-token")

import pandas as pd  # noqa: E402

import threading  # noqa: E402

import api_server  # noqa: E402
import equity_tracker  # noqa: E402
import exchange_base  # noqa: E402
import main  # noqa: E402
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


class TurtleStopLossFollowupTests(unittest.TestCase):
    def make_system(self):
        system = object.__new__(main.TradingSystem)
        system._stop_anomalies = {}
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=_fake_to_ccxt,
            get_position=Mock(return_value=None),
            cancel_order=Mock(return_value=True),
            cancel_all_orders=Mock(return_value=True),
        )
        system.notifier = SimpleNamespace(
            send_message=Mock(), notify_error=Mock(), notify_stop_loss_triggered=Mock())
        system.handle_open_signal_turtle = Mock()

        class FakeTradeState:
            def __init__(self):
                self.signal_state = {}
                self.closed = None

            def get_open_position(self, symbol):
                return {"symbol": symbol}

            def close_position(self, symbol, exit_price):
                self.closed = (symbol, exit_price)
                return {"symbol": symbol, "exit_price": exit_price}

            def set_signal_state(self, symbol, value):
                self.signal_state[symbol] = value

            def get_signal_state(self, symbol):
                return self.signal_state.get(symbol, False)

            def mark_stop_residue(self, symbol):
                self.residue = symbol

            def clear_stop_residue(self, symbol):
                self.residue = None

        system.trade_state = FakeTradeState()
        system.trade_state.residue = None
        return system

    def test_stop_loss_recovers_arm_but_does_not_reopen_when_signal_is_none(self):
        system = self.make_system()
        signal = {
            "action": None,
            "current_close": 72,
            "mid_line": 55,
        }
        position = {"side": "long", "stop_loss_price": 40, "position_size": 1}

        system.handle_open_position_turtle("BTCUSDT", signal, position, {"name": "BTCUSDT"})

        self.assertTrue(system.trade_state.signal_state["BTCUSDT"])
        system.handle_open_signal_turtle.assert_not_called()
        self.assertEqual(system.trade_state.closed, ("BTCUSDT", 40))

    def test_stop_loss_reverses_when_standard_signal_exists(self):
        system = self.make_system()
        signal = {
            "action": "short",
            "mid_line_crossed": True,
            "current_close": 38,
            "mid_line": 56,
        }
        position = {"side": "long", "stop_loss_price": 40, "position_size": 1}

        system.handle_open_position_turtle("BTCUSDT", signal, position, {"name": "BTCUSDT"})

        self.assertTrue(system.trade_state.signal_state["BTCUSDT"])
        system.handle_open_signal_turtle.assert_called_once_with(
            "BTCUSDT", "short", signal, {"name": "BTCUSDT"}
        )

    def test_stop_loss_branch_aborts_reverse_when_cancel_unconfirmed(self):
        """交易所无仓记平后旧止损撤销不可确认：标记残留，且不进行止损后反手开仓（P1）。"""
        system = self.make_system()
        system.exchange_api.cancel_order.return_value = False
        system.exchange_api.cancel_all_orders.return_value = None
        signal = {
            "action": "short",
            "mid_line_crossed": True,
            "current_close": 38,
            "mid_line": 56,
        }
        position = {"side": "long", "stop_loss_price": 40, "position_size": 1, "stop_order_id": "stop-1"}

        system.handle_open_position_turtle("BTCUSDT", signal, position, {"name": "BTCUSDT"})

        self.assertEqual(system.trade_state.closed, ("BTCUSDT", 40))  # 记平照常
        self.assertEqual(system.trade_state.residue, "BTCUSDT")       # 残留已标记
        system.handle_open_signal_turtle.assert_not_called()          # 不反手


class MaCrossTPlusOneTests(unittest.TestCase):
    def make_system(self):
        system = object.__new__(main.TradingSystem)
        system._stop_anomalies = {}
        system.stop_loss_dates = {}
        system._save_stop_loss_dates = Mock()
        system._execute_open = Mock()
        # _execute_open 后主流程会确认持仓已形成，返回非 None 避免触发 missing-position 告警
        system.trade_state = SimpleNamespace(get_open_position=Mock(return_value={"symbol": "ETHUSDT"}))
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

    def test_initial_open_failure_records_tplus1_for_reentry(self):
        """初始金叉/死叉开仓腿失败：记 T+1，次日按 EMA 方向自动重入恢复「永远在市」。"""
        system = self.make_system()
        system.trade_state.get_open_position = Mock(return_value=None)  # 开仓腿失败
        signal = {"action": "long", "current_close": 100, "lower_stop": 90, "upper_stop": 110}
        system.handle_no_position_ma_cross("ETHUSDT", signal, {"name": "ETHUSDT"}, df=object())

        system._execute_open.assert_called_once()
        today = main.date.today().strftime("%Y-%m-%d")
        self.assertEqual(system.stop_loss_dates.get("ETHUSDT"), today)  # 记了 T+1
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

        df_marker = [None] * 30
        filter_mock = Mock(return_value=df_marker)
        execute_calls = []

        def execute_open(symbol, side, entry_price, stop_loss_price, symbol_config, buffer_notification=True):
            # 即时开仓路由必须传 buffer_notification=False：它自发专属钉钉，不进日检汇总缓冲
            assert buffer_notification is False, "instant_open 应关闭开仓通知缓冲"
            execute_calls.append((symbol, side, entry_price, stop_loss_price, symbol_config))
            trade_state.position = {
                "entry_price": entry_price,
                "position_size": 1.23,
                "stop_loss_price": stop_loss_price,
                "stop_order_id": "stop-1",
            }

        exchange_stub = SimpleNamespace(fetch_ticker=Mock(return_value={"last": 123.45}))
        system = SimpleNamespace(
            trade_state=trade_state,
            exchange_api=SimpleNamespace(
                to_ccxt_symbol=_fake_to_ccxt,
                fetch_ohlcv=Mock(return_value=[[1]] * 120),
                ohlcv_to_dataframe=Mock(return_value=df_marker),
                filter_closed_candles=filter_mock,
                exchange=exchange_stub,
                get_last_price=lambda s: float(exchange_stub.fetch_ticker(s)["last"]),
            ),
            turtle_strategy=SimpleNamespace(
                check_current_state=Mock(
                    return_value={
                        "action": "long",
                        "upper_line": 130,
                        "lower_line": 100,
                        "mid_line": 115,
                    }
                )
            ),
            ma_cross_strategy=SimpleNamespace(check_current_state=Mock()),
            _execute_open=execute_open,
            config={"trading": {"symbols": []}},
            config_file="config.json",
            reload_strategies=Mock(),
            execute_calls=execute_calls,
            label="欧易",
            exchange_id="okx",
        )
        return system

    def test_instant_open_rejects_when_position_exists(self):
        self.authenticate()
        fake_system = self.make_system(existing_position={"symbol": "BTCUSDT"})

        with patch.object(api_server, "trading_system", _prep_system(fake_system)), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.post(
                "/api/instant_open",
                json={"name": "BTCUSDT", "risk_per_trade": 0.01, "strategy": "turtle"},
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
                json={"name": "BTCUSDT", "risk_per_trade": 0.01, "strategy": "turtle"},
            )

        self.assertEqual(resp.status_code, 200)
        fake_system.exchange_api.filter_closed_candles.assert_called_once()
        self.assertEqual(len(fake_system.execute_calls), 1)
        symbol, side, entry_price, stop_loss_price, symbol_config = fake_system.execute_calls[0]
        self.assertEqual(symbol, "BTCUSDT")
        self.assertEqual(side, "long")
        self.assertEqual(entry_price, 123.45)
        self.assertEqual(stop_loss_price, 100)
        self.assertEqual(symbol_config["strategy"], "turtle")
        fake_system.exchange_api.fetch_ohlcv.assert_called_once_with("BTC/USDT", "1d", limit=365)

    def test_instant_open_fetch_limit_tracks_large_turtle_config(self):
        self.authenticate()
        fake_system = self.make_system()
        fake_system.config = {
            "strategy": {"channel_period": 500},
            "trading": {"symbols": []},
        }
        df_marker = [None] * 502
        fake_system.exchange_api.ohlcv_to_dataframe.return_value = df_marker
        fake_system.exchange_api.filter_closed_candles.return_value = df_marker

        with patch.object(api_server, "trading_system", _prep_system(fake_system)), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.post(
                "/api/instant_open",
                json={"name": "BTCUSDT", "risk_per_trade": 0.01, "strategy": "turtle"},
            )

        self.assertEqual(resp.status_code, 200)
        fake_system.exchange_api.fetch_ohlcv.assert_called_once_with("BTC/USDT", "1d", limit=503)


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

    def test_add_symbol_rejects_unknown_strategy(self):
        resp = self._post("/api/symbols", {"name": "BTCUSDT", "risk_per_trade": 0.01, "strategy": "martingale"})
        self.assertEqual(resp.status_code, 400)

    def test_instant_open_rejects_malformed_input(self):
        resp = self._post("/api/instant_open", {"name": "btc;rm -rf", "risk_per_trade": 0.01})
        self.assertEqual(resp.status_code, 400)

    def test_validate_symbol_input_normalizes(self):
        """API 归一化契约：返回规范化 clean——name 大写、risk→float、enabled→真 bool。
        杜绝 "0.01"/"false" 字符串混入下单/开仓资格路径（否则盘中 TypeError 或被当启用）。"""
        clean, err = api_server._validate_symbol_input("btcusdt", "0.01", "turtle", "false")
        self.assertIsNone(err)
        self.assertEqual(clean["name"], "BTCUSDT")
        self.assertIsInstance(clean["risk_per_trade"], float)
        self.assertEqual(clean["risk_per_trade"], 0.01)
        self.assertIs(clean["enabled"], False)   # "false" 解析为 False，而非 Python 真值陷阱
        self.assertEqual(clean["strategy"], "turtle")

    def test_validate_symbol_input_rejects_non_string_name(self):
        clean, err = api_server._validate_symbol_input(123)
        self.assertIsNotNone(err)
        self.assertIsNone(clean)

    def test_validate_symbol_input_rejects_ambiguous_enabled(self):
        clean, err = api_server._validate_symbol_input("BTCUSDT", enabled="maybe")
        self.assertIsNotNone(err)

    def test_equity_sync_rejects_nonfinite_flow_amount(self):
        """资金同步净变动金额必须有限：nan/inf/-inf 会写出 nan/0.0 除数污染求索指数，须 400。"""
        for bad in ("nan", "inf", "-inf"):
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

    def test_update_symbol_rejects_strategy_change_while_holding(self):
        """有持仓时禁止改策略：现有仓位的止损/出场逻辑不能被换掉。"""
        self.system.config = {"trading": {"symbols": [{"name": "BTCUSDT", "strategy": "turtle"}]}}
        self.system.trade_state = SimpleNamespace(get_open_position=Mock(return_value={"symbol": "BTCUSDT"}))
        with patch.object(api_server, "trading_system", self.system), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.put("/api/symbols/BTCUSDT", json={"strategy": "ma_cross"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.system.config["trading"]["symbols"][0]["strategy"], "turtle")

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
        self.system.config = {"strategy": {"channel_period": 28}}
        with patch.object(api_server, "trading_system", self.system), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.put("/api/strategy_params", json={"channel_period": 28.9})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.system.config["strategy"]["channel_period"], 28)  # 未写入截断值

    def test_update_symbol_rejects_out_of_range_risk(self):
        self.system.config = {"trading": {"symbols": [{"name": "BTCUSDT", "risk_per_trade": 0.01}]}}
        with patch.object(api_server, "trading_system", self.system), patch.object(
            api_server, "send_dingtalk", Mock()
        ):
            resp = self.client.put("/api/symbols/BTCUSDT", json={"risk_per_trade": 0.9})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.system.config["trading"]["symbols"][0]["risk_per_trade"], 0.01)


class DeleteSymbolApiTests(unittest.TestCase):
    """删除交易对语义：只移出品种池，不平仓不撤单；老仓缺 strategy 须兜底。"""

    def setUp(self):
        self.client = api_server.app.test_client()
        with self.client.session_transaction() as sess:
            sess["authenticated"] = True

    def make_system(self, held_position=None, config_strategy="ma_cross"):
        strategy_backfills = []

        def set_position_strategy(symbol, strategy):
            strategy_backfills.append((symbol, strategy))
            if held_position is not None:
                held_position["strategy"] = strategy
            return held_position

        symbols = [{"name": "ETHUSDT", "enabled": True, "risk_per_trade": 0.01}]
        if config_strategy is not None:
            symbols[0]["strategy"] = config_strategy

        system = SimpleNamespace(
            trade_state=SimpleNamespace(
                get_open_position=lambda _s: held_position,
                set_position_strategy=set_position_strategy,
                close_position=Mock(side_effect=AssertionError("删除不得平仓")),
            ),
            exchange_api=SimpleNamespace(
                cancel_order=Mock(side_effect=AssertionError("删除不得撤单")),
                cancel_all_orders=Mock(side_effect=AssertionError("删除不得撤单")),
            ),
            config={"trading": {"symbols": symbols}},
            config_file="config.json",
            reload_strategies=Mock(),
            strategy_backfills=strategy_backfills,
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
        """有持仓且持仓已带 strategy：删除放行，持仓/挂单原样保留。"""
        system = self.make_system(held_position={"symbol": "ETHUSDT", "strategy": "ma_cross"})

        resp = self._delete(system)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(system.config["trading"]["symbols"], [])
        self.assertEqual(system.strategy_backfills, [])

    def test_delete_backfills_missing_strategy_from_config(self):
        """老仓缺 strategy：删除前先把配置里的策略固化进持仓。"""
        system = self.make_system(held_position={"symbol": "ETHUSDT"}, config_strategy="ma_cross")

        resp = self._delete(system)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(system.strategy_backfills, [("ETHUSDT", "ma_cross")])

    def test_delete_rejected_when_strategy_unknowable(self):
        """老仓缺 strategy 且配置也没有：拒绝删除，配置保持不变。"""
        system = self.make_system(held_position={"symbol": "ETHUSDT"}, config_strategy=None)

        resp = self._delete(system)

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(len(system.config["trading"]["symbols"]), 1)
        self.assertEqual(system.strategy_backfills, [])


class ExecuteOpenRiskGuardTests(unittest.TestCase):
    def make_system(self):
        system = object.__new__(main.TradingSystem)
        system._stop_anomalies = {}
        system.config = {"strategy": {"default_risk_per_trade": 0.01}}
        system.notifier = SimpleNamespace(
            notify_error=Mock(),
            notify_trade_opened=Mock(),
        )
        system.trade_state = SimpleNamespace(
            add_open_position=Mock(),
            has_stop_residue=Mock(return_value=False),
            clear_stop_residue=Mock(),
            mark_stop_residue=Mock(),
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
            open_position=Mock(return_value={"average": 100}),
            create_stop_loss_order=Mock(return_value={"id": "stop-1"}),
            cancel_order=Mock(return_value=True),
            cancel_all_orders=Mock(),
            close_position=Mock(return_value={"id": "close-1"}),
        )
        return system

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

    def test_rolls_back_when_fill_price_crosses_stop(self):
        system = self.make_system()
        system.exchange_api.exchange.fetch_ticker.return_value = {"last": 82}
        system.exchange_api.open_position.return_value = {"average": 79}

        system._execute_open(
            "BTCUSDT",
            "long",
            100,
            80,
            {"name": "BTCUSDT", "risk_per_trade": 0.01},
        )

        system.exchange_api.close_position.assert_called_once_with("BTC/USDT", "long", 2.5)
        system.exchange_api.create_stop_loss_order.assert_not_called()
        system.trade_state.add_open_position.assert_not_called()
        system.notifier.notify_trade_opened.assert_not_called()

    def test_rolls_back_when_stop_order_creation_fails(self):
        system = self.make_system()
        system.exchange_api.exchange.fetch_ticker.return_value = {"last": 100}
        system.exchange_api.open_position.return_value = {"average": 100}
        system.exchange_api.create_stop_loss_order.return_value = None
        # 用同一父 mock 记录调用顺序：清扫孤儿止损单必须在回滚平仓之前
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

        system.exchange_api.close_position.assert_called_once_with("BTC/USDT", "long", 2.5)
        # 止损创建失败回滚前，必须先撤光该品种挂单以清扫可能的孤儿止损单（防未来错价触发）
        system.exchange_api.cancel_all_orders.assert_called_once_with("BTC/USDT")
        self.assertEqual(
            [c[0] for c in calls.mock_calls],
            ["cancel_all_orders", "close_position"],
            "必须先清扫挂单再回滚平仓",
        )
        system.trade_state.add_open_position.assert_not_called()
        system.notifier.notify_trade_opened.assert_not_called()


class UpdateStopOrderTests(unittest.TestCase):
    def make_system(self):
        system = object.__new__(main.TradingSystem)
        system._stop_anomalies = {}
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=_fake_to_ccxt,
            cancel_order=Mock(return_value=True),
            cancel_all_orders=Mock(),
            create_stop_loss_order=Mock(return_value={"id": "new-stop"}),
        )
        system.trade_state = SimpleNamespace(
            update_stop_loss=Mock(),
            force_runtime_update_stop_loss=Mock(),
            has_stop_residue=Mock(return_value=False),
            clear_stop_residue=Mock(),
            mark_stop_residue=Mock(),
        )
        system.notifier = SimpleNamespace(send_message=Mock(), notify_error=Mock())
        system._pending_stop_loss_updates = []
        return system

    def test_updates_trade_state_when_new_stop_order_succeeds(self):
        system = self.make_system()
        position = {
            "side": "long",
            "position_size": 2.5,
            "stop_loss_price": 80,
            "stop_order_id": "old-stop",
        }

        system._update_stop_order("BTCUSDT", position, 90)

        system.exchange_api.cancel_order.assert_called_once_with("BTC/USDT", "old-stop")
        system.trade_state.update_stop_loss.assert_called_once_with("BTCUSDT", 90, "new-stop")
        self.assertEqual(len(system._pending_stop_loss_updates), 1)  # 现行为：缓冲，由轮末汇总推送
        system.notifier.notify_error.assert_not_called()

    def test_notifies_error_when_new_stop_order_creation_fails(self):
        system = self.make_system()
        system.exchange_api.create_stop_loss_order.return_value = None
        position = {
            "side": "long",
            "position_size": 2.5,
            "stop_loss_price": 80,
            "stop_order_id": "old-stop",
        }

        system._update_stop_order("BTCUSDT", position, 90)

        system.trade_state.update_stop_loss.assert_not_called()
        system.notifier.notify_error.assert_called_once()
        self.assertEqual(system._pending_stop_loss_updates, [])

    def test_blocks_update_when_old_stop_cancel_unconfirmed(self):
        """撤旧止损不可确认：不建新止损、不更新本地记录、标记残留并告警（Codex 红线）。"""
        system = self.make_system()
        system.exchange_api.cancel_order.return_value = False
        system.exchange_api.cancel_all_orders.return_value = None
        position = {
            "side": "long",
            "position_size": 2.5,
            "stop_loss_price": 80,
            "stop_order_id": "old-stop",
        }

        system._update_stop_order("BTCUSDT", position, 90)

        system.exchange_api.create_stop_loss_order.assert_not_called()
        system.trade_state.update_stop_loss.assert_not_called()
        system.trade_state.mark_stop_residue.assert_called_once_with("BTCUSDT")
        system.notifier.notify_error.assert_called_once()


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
                "entry_price": entry_price,
                "position_size": 1.0,
                "stop_loss_price": stop_loss_price,
                "stop_order_id": "stop-1",
            }

        original_config = {"trading": {"symbols": []}}
        exchange_stub = SimpleNamespace(fetch_ticker=Mock(return_value={"last": 123.45}))
        fake_system = SimpleNamespace(
            trade_state=trade_state,
            exchange_api=SimpleNamespace(
                to_ccxt_symbol=_fake_to_ccxt,
                fetch_ohlcv=Mock(return_value=[[1]] * 120),
                ohlcv_to_dataframe=Mock(return_value=[None] * 30),
                filter_closed_candles=Mock(return_value=[None] * 30),
                exchange=exchange_stub,
                get_last_price=lambda s: float(exchange_stub.fetch_ticker(s)["last"]),
            ),
            turtle_strategy=SimpleNamespace(
                check_current_state=Mock(
                    return_value={
                        "action": "long",
                        "upper_line": 130,
                        "lower_line": 100,
                        "mid_line": 115,
                    }
                )
            ),
            ma_cross_strategy=SimpleNamespace(check_current_state=Mock()),
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
                json={"name": "BTCUSDT", "risk_per_trade": 0.01, "strategy": "turtle"},
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
            trade_state=SimpleNamespace(get_all_open_positions=Mock(return_value={})),
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

    def test_build_account_stats_raises_when_peak_save_fails(self):
        tracker = self.make_tracker()

        with patch.object(tracker, "load_peak_equity", Mock(return_value={"peak_equity": 9000, "peak_time": None})), \
             patch.object(tracker, "load_equity_history", Mock(return_value={"initial_equity": 9000, "year_start_equity": 9000})), \
             patch.object(tracker, "save_peak_equity", Mock(return_value=False)):
            with self.assertRaises(RuntimeError):
                tracker.build_account_stats(persist=True)

    def test_record_daily_equity_snapshot_raises_on_save_failure(self):
        tracker = self.make_tracker()

        with patch.object(tracker, "load_daily_equity", Mock(return_value=[])), \
             patch.object(tracker, "save_daily_equity", Mock(return_value=False)):
            with self.assertLogs(equity_tracker.logger, level="ERROR") as logs:
                tracker.record_daily_equity_snapshot()

        self.assertTrue(any("记录权益快照失败" in line for line in logs.output))


class TradeStateIsolationTests(unittest.TestCase):
    def test_get_all_open_positions_returns_copy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "trade_state.json"
            ts = trade_state.TradeState(str(state_file))
            ts.add_open_position("BTCUSDT", "long", 100, 1, 90, "stop-1")

            positions = ts.get_all_open_positions()
            positions["BTCUSDT"]["entry_price"] = 999

            fresh = ts.get_all_open_positions()
            self.assertEqual(fresh["BTCUSDT"]["entry_price"], 100)

    def test_get_open_position_returns_copy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "trade_state.json"
            ts = trade_state.TradeState(str(state_file))
            ts.add_open_position("BTCUSDT", "long", 100, 1, 90, "stop-1")

            position = ts.get_open_position("BTCUSDT")
            position["entry_price"] = 999

            fresh = ts.get_open_position("BTCUSDT")
            self.assertEqual(fresh["entry_price"], 100)



class HandleCloseSignalTests(unittest.TestCase):
    def make_system(self):
        system = object.__new__(main.TradingSystem)
        system._stop_anomalies = {}
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=_fake_to_ccxt,
            close_position=Mock(return_value={"average": 123.45}),
            cancel_order=Mock(return_value=True),
            cancel_all_orders=Mock(),
        )
        system.trade_state = SimpleNamespace(
            close_position=Mock(return_value={"pnl": 10.5, "pnl_percent": 5.25}),
            force_runtime_close_position=Mock(return_value={"pnl": 10.5, "pnl_percent": 5.25}),
            has_stop_residue=Mock(return_value=False),
            clear_stop_residue=Mock(),
            mark_stop_residue=Mock(),
        )
        system.notifier = SimpleNamespace(
            notify_error=Mock(),
        )
        system._pending_trade_close_notifications = []
        system.handle_open_signal_turtle = Mock()
        return system

    def test_handle_close_signal_uses_actual_fill_price_and_skips_reopen(self):
        system = self.make_system()
        signal = {"current_close": 120, "action": None}
        position = {"side": "long", "position_size": 2.0, "stop_order_id": "stop-1"}

        result = system.handle_close_signal(
            "BTCUSDT", signal, position, {"name": "BTCUSDT"}, skip_reopen=True
        )

        self.assertTrue(result)
        system.trade_state.close_position.assert_called_once_with("BTCUSDT", 123.45)
        self.assertEqual(len(system._pending_trade_close_notifications), 1)  # 现行为：缓冲汇总
        self.assertEqual(system._pending_trade_close_notifications[0]["exit_price"], 123.45)
        system.handle_open_signal_turtle.assert_not_called()

    def test_handle_close_signal_reopens_when_new_signal_exists(self):
        system = self.make_system()
        signal = {"current_close": 120, "action": "short"}
        position = {"side": "long", "position_size": 2.0, "stop_order_id": "stop-1"}

        result = system.handle_close_signal(
            "BTCUSDT", signal, position, {"name": "BTCUSDT"}, skip_reopen=False
        )

        self.assertTrue(result)
        system.handle_open_signal_turtle.assert_called_once_with(
            "BTCUSDT", "short", signal, {"name": "BTCUSDT"}
        )

    def test_close_still_books_trade_when_stop_cancel_unconfirmed(self):
        """平仓后撤止损不可确认：仓位记账照常完成（仓确实平了），但标记残留阻断后续开仓。"""
        system = self.make_system()
        system.exchange_api.cancel_order.return_value = False
        system.exchange_api.cancel_all_orders.return_value = None
        signal = {"current_close": 120, "action": None}
        position = {"side": "long", "position_size": 2.0, "stop_order_id": "stop-1"}

        result = system.handle_close_signal(
            "BTCUSDT", signal, position, {"name": "BTCUSDT"}, skip_reopen=True
        )

        # 平仓已记账，但返回 False：本函数与调用方都不得进入任何再开仓流程
        self.assertFalse(result)
        system.trade_state.close_position.assert_called_once_with("BTCUSDT", 123.45)
        system.trade_state.mark_stop_residue.assert_called_once_with("BTCUSDT")
        system.handle_open_signal_turtle.assert_not_called()


class MaCrossFlipTests(unittest.TestCase):
    def make_system(self):
        system = object.__new__(main.TradingSystem)
        system._stop_anomalies = {}
        exchange_stub = SimpleNamespace(fetch_ticker=Mock(return_value={"last": 111}))
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=_fake_to_ccxt,
            exchange=exchange_stub,
            get_last_price=lambda s: float(exchange_stub.fetch_ticker(s)["last"]),
            close_position=Mock(return_value={"average": 112}),
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
        system._execute_open = Mock()
        system.record_stop_loss = Mock()
        return system

    def test_flip_position_records_actual_exit_and_opens_new_side(self):
        system = self.make_system()
        old_position = {"side": "short", "position_size": 2.0, "stop_order_id": "stop-1"}
        signal = {"current_close": 110, "lower_stop": 95, "upper_stop": 125}

        system._flip_position("BTCUSDT", signal, old_position, "long", {"name": "BTCUSDT"})

        system.trade_state.close_position.assert_called_once_with("BTCUSDT", 112)
        self.assertEqual(len(system._pending_trade_close_notifications), 1)  # 现行为：缓冲汇总
        system._execute_open.assert_called_once_with(
            "BTCUSDT", "long", 110, 95, {"name": "BTCUSDT"}
        )
        system.record_stop_loss.assert_not_called()  # 正常反手不记 T+1

    def test_flip_aborts_reopen_when_stop_cancel_unconfirmed(self):
        """翻转时撤旧止损不可确认：平仓记账完成、不反手开新仓，
        但记录 T+1 交由次日重入（残留清理确认后恢复永远在市）。"""
        system = self.make_system()
        system.exchange_api.cancel_order.return_value = False
        system.exchange_api.cancel_all_orders.return_value = None
        old_position = {"side": "short", "position_size": 2.0, "stop_order_id": "stop-1"}
        signal = {"current_close": 110, "lower_stop": 95, "upper_stop": 125}

        system._flip_position("BTCUSDT", signal, old_position, "long", {"name": "BTCUSDT"})

        system.trade_state.close_position.assert_called_once_with("BTCUSDT", 112)
        system.trade_state.mark_stop_residue.assert_called_once_with("BTCUSDT")
        system._execute_open.assert_not_called()
        system.record_stop_loss.assert_called_once_with("BTCUSDT")  # 记 T+1，次日按 EMA 方向重入

    def test_flip_records_tplus1_when_reopen_leg_fails(self):
        """翻转平旧成功、反手开新腿失败（成交后仍无持仓）：记 T+1，次日按 EMA 方向重入，
        恢复「永远在市」（此前失败后不留恢复线索，会空到下一次全新交叉）。"""
        system = self.make_system()
        system.notifier.notify_signal_missed = Mock()
        system.trade_state.get_open_position = Mock(return_value=None)  # 反手开仓腿失败
        old_position = {"side": "short", "position_size": 2.0, "stop_order_id": "stop-1"}
        signal = {"current_close": 110, "lower_stop": 95, "upper_stop": 125}

        system._flip_position("BTCUSDT", signal, old_position, "long", {"name": "BTCUSDT"})

        system._execute_open.assert_called_once()                 # 尝试了反手
        system.record_stop_loss.assert_called_once_with("BTCUSDT")  # 记 T+1 次日重入
        system.notifier.notify_signal_missed.assert_called_once()

    def test_handle_open_position_ma_cross_records_stop_loss_and_returns_when_exchange_position_missing(self):
        system = self.make_system()
        system.exchange_api.get_position = Mock(return_value=None)

        signal = {"current_close": 101}
        position = {"side": "long", "position_size": 2.0, "stop_loss_price": 99}

        system.handle_open_position_ma_cross(
            "BTCUSDT", signal, position, {"name": "BTCUSDT"}, df=object()
        )

        system.trade_state.close_position.assert_called_once_with("BTCUSDT", 99)
        system.record_stop_loss.assert_called_once_with("BTCUSDT")
        system._execute_open.assert_not_called()

class TradeStatePersistenceFailureTests(unittest.TestCase):
    def test_add_open_position_rolls_back_when_save_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "trade_state.json"
            ts = trade_state.TradeState(str(state_file))

            with patch.object(trade_state, "atomic_write_json", Mock(return_value=False)):
                with self.assertRaises(trade_state.TradeStatePersistenceError):
                    ts.add_open_position("BTCUSDT", "long", 100, 1, 90, "stop-1")

            self.assertIsNone(ts.get_open_position("BTCUSDT"))

    def test_update_stop_loss_rolls_back_when_save_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "trade_state.json"
            ts = trade_state.TradeState(str(state_file))
            ts.add_open_position("BTCUSDT", "long", 100, 1, 90, "stop-1")

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
            ts.add_open_position("BTCUSDT", "long", 100, 1, 90, "stop-1")

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

        system._execute_open(
            "BTCUSDT",
            "long",
            100,
            80,
            {"name": "BTCUSDT", "risk_per_trade": 0.01},
        )

        system.exchange_api.cancel_order.assert_called_once_with("BTC/USDT", "stop-1")
        system.exchange_api.close_position.assert_called_once_with("BTC/USDT", "long", 2.5)
        system.notifier.notify_trade_opened.assert_not_called()

    def test_update_stop_order_uses_runtime_fallback_when_persist_fails(self):
        system = UpdateStopOrderTests().make_system()
        system.trade_state.update_stop_loss.side_effect = trade_state.TradeStatePersistenceError("disk full")
        position = {
            "side": "long",
            "position_size": 2.5,
            "stop_loss_price": 80,
            "stop_order_id": "old-stop",
        }

        system._update_stop_order("BTCUSDT", position, 90)

        system.trade_state.force_runtime_update_stop_loss.assert_called_once_with("BTCUSDT", 90, "new-stop")
        system.notifier.notify_error.assert_called_once()
        system.notifier.send_message.assert_not_called()

    def test_handle_close_signal_skips_reopen_when_persist_fails(self):
        system = HandleCloseSignalTests().make_system()
        system.trade_state.close_position.side_effect = trade_state.TradeStatePersistenceError("disk full")
        signal = {"current_close": 120, "action": "short"}
        position = {"side": "long", "position_size": 2.0, "stop_order_id": "stop-1"}

        result = system.handle_close_signal(
            "BTCUSDT", signal, position, {"name": "BTCUSDT"}, skip_reopen=False
        )

        self.assertFalse(result)
        system.trade_state.force_runtime_close_position.assert_called_once_with("BTCUSDT", 123.45)
        system.handle_open_signal_turtle.assert_not_called()

    def test_flip_position_stops_when_persist_fails(self):
        system = MaCrossFlipTests().make_system()
        system.trade_state.close_position.side_effect = trade_state.TradeStatePersistenceError("disk full")
        old_position = {"side": "short", "position_size": 2.0, "stop_order_id": "stop-1"}
        signal = {"current_close": 110, "lower_stop": 95, "upper_stop": 125}

        system._flip_position("BTCUSDT", signal, old_position, "long", {"name": "BTCUSDT"})

        system.trade_state.force_runtime_close_position.assert_called_once_with("BTCUSDT", 112)
        system._execute_open.assert_not_called()


class StartupSyncCompensationTests(unittest.TestCase):
    def make_system(self):
        system = object.__new__(main.TradingSystem)
        system._stop_anomalies = {}
        exchange_stub = SimpleNamespace(fetch_ticker=Mock(return_value={"last": 123.45}))
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=_fake_to_ccxt,
            get_position=Mock(return_value=None),
            cancel_order=Mock(return_value=True),
            cancel_all_orders=Mock(return_value=True),
            list_position_symbols=Mock(return_value=[]),
            exchange=exchange_stub,
            get_last_price=lambda s: float(exchange_stub.fetch_ticker(s)["last"]),
        )
        system.trade_state = SimpleNamespace(
            get_all_open_positions=Mock(
                return_value={
                    "BTCUSDT": {
                        "entry_price": 100,
                        "position_size": 2.0,
                        "side": "long",
                    }
                }
            ),
            force_runtime_close_position=Mock(return_value={"pnl": 46.9, "pnl_percent": 23.45}),
            close_position=Mock(return_value={"pnl": 46.9, "pnl_percent": 23.45}),
            mark_stop_residue=Mock(),
            clear_stop_residue=Mock(),
        )
        system.notifier = SimpleNamespace(notify_error=Mock())
        return system

    def test_sync_positions_uses_runtime_fallback_when_persist_fails(self):
        system = self.make_system()
        system.trade_state.close_position.side_effect = trade_state.TradeStatePersistenceError("disk full")

        system.sync_positions_on_startup()

        system.trade_state.force_runtime_close_position.assert_called_once_with("BTCUSDT", 123.45)
        system.notifier.notify_error.assert_called_once()


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

    缺陷背景：_validate_scheduler_config 放行 stop_loss_scan_interval_minutes ∈ [1,1440]，
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


if __name__ == "__main__":
    unittest.main()
