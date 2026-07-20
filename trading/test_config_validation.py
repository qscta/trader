"""config_validation 共享校验原语单测（纯标准库）。

三入口（前端/API/手写 config）的一致性由这一层保证，故直接锁定其边界行为。
"""
import unittest

import config_validation as cv


class AliasResolutionTest(unittest.TestCase):
    def test_equal_or_single_values_are_accepted(self):
        self.assertEqual(cv.resolve_optional_alias('x', None, 'secret'), 'x')
        self.assertEqual(cv.resolve_optional_alias(None, 'x', 'secret'), 'x')
        self.assertEqual(cv.resolve_optional_alias('x', 'x', 'secret'), 'x')
        self.assertIsNone(cv.resolve_optional_alias(None, None, 'secret'))

    def test_conflicting_aliases_fail_without_echoing_values(self):
        with self.assertRaises(ValueError) as caught:
            cv.resolve_optional_alias('primary-secret', 'legacy-secret', 'passphrase')
        message = str(caught.exception)
        self.assertNotIn('primary-secret', message)
        self.assertNotIn('legacy-secret', message)


class StrictIntTest(unittest.TestCase):
    def test_accepts_integral(self):
        for v in (28, 28.0, "28", " 28 "):
            self.assertEqual(cv.strict_int(v, 'x'), 28)

    def test_rejects_fractional_and_nonfinite(self):
        for v in (28.9, "28.9", "inf", "-inf", "nan", "abc", None,
                  True, False):
            with self.assertRaises(ValueError, msg=repr(v)):
                cv.strict_int(v, 'x')


class StrictFloatFiniteTest(unittest.TestCase):
    def test_accepts_finite(self):
        self.assertEqual(cv.strict_float_finite("0.01", 'x'), 0.01)
        self.assertEqual(cv.strict_float_finite(-5, 'x'), -5.0)

    def test_rejects_nonfinite(self):
        values = ("inf", "-inf", "nan", float('inf'), float('nan'),
                  "abc", None, True, False, 10 ** 10000)
        for index, v in enumerate(values):
            # Python 3.11+ 对超过 4300 位的整数转字符串会自身
            # 拒绝；错误消息不得在被测函数之前先 repr(v) 崩溃。
            with self.assertRaises(
                    ValueError,
                    msg=f'case={index}, type={type(v).__name__}') as caught:
                cv.strict_float_finite(v, 'x')
            self.assertIn('x 不是', str(caught.exception))


class StrictRiskTest(unittest.TestCase):
    def test_in_range(self):
        self.assertEqual(cv.strict_risk_per_trade("0.01"), 0.01)
        self.assertEqual(cv.strict_risk_per_trade(0.5), 0.5)  # 上限含端点

    def test_out_of_range(self):
        for v in (0, -0.1, 0.51, 1.0, "inf", "nan"):
            with self.assertRaises(ValueError, msg=repr(v)):
                cv.strict_risk_per_trade(v)


class StrictBoolTest(unittest.TestCase):
    def test_real_bool_passthrough(self):
        self.assertIs(cv.strict_bool(True), True)
        self.assertIs(cv.strict_bool(False), False)

    def test_string_parsing(self):
        self.assertIs(cv.strict_bool("true"), True)
        self.assertIs(cv.strict_bool("false"), False)
        self.assertIs(cv.strict_bool(" TRUE "), True)

    def test_rejects_ambiguous(self):
        # 关键回归：Python bool("false")==True，必须显式拒绝而非当真
        for v in ("maybe", "1", "0", 1, 0, None, "yes"):
            with self.assertRaises(ValueError, msg=repr(v)):
                cv.strict_bool(v)


class NormalizeSymbolTest(unittest.TestCase):
    def test_normalizes(self):
        self.assertEqual(cv.normalize_symbol_name("btcusdt"), "BTCUSDT")
        self.assertEqual(cv.normalize_symbol_name(" ethusdt "), "ETHUSDT")

    def test_rejects_bad(self):
        for v in ("BTC-USDT", "BTCUSD", "", 123, None, "USDT",
                  "ſUSDT", "ßUSDT", "ıUSDT"):
            with self.assertRaises(ValueError, msg=repr(v)):
                cv.normalize_symbol_name(v)


class MaOhlcvLimitTest(unittest.TestCase):
    def test_default_periods_always_request_one_full_okx_page(self):
        config = {'ma_long_period': 28, 'ma_stop_period': 28}

        self.assertEqual(cv.ohlcv_fetch_limit(config), 300)

    def test_capacity_boundary_leaves_one_open_candle_buffer(self):
        config = {'ma_long_period': 149, 'ma_stop_period': 298}

        self.assertTrue(cv.validate_ohlcv_capacity(config))
        self.assertEqual(cv.required_closed_candles(config), 299)

    def test_ma_period_over_single_page_capacity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '超过单次 300 根上限'):
            cv.validate_ohlcv_capacity({
                'ma_long_period': 150,
                'ma_stop_period': 28})


class CompleteExecutionConfigTest(unittest.TestCase):
    @staticmethod
    def _config():
        return {
            'okx': {'sandbox': False},
            'strategy': {'default_risk_per_trade': 0.01},
            'trading': {'symbols': [{'name': 'BTCUSDT'}]},
            'scheduler': {},
        }

    def test_missing_optional_values_use_runtime_defaults(self):
        config = self._config()

        self.assertIs(config, cv.validate_and_normalize_execution_config(config))

    def test_explicit_null_never_means_missing(self):
        cases = (
            lambda c: c['strategy'].__setitem__('ma_long_period', None),
            lambda c: c['trading']['symbols'][0].__setitem__(
                'risk_per_trade', None),
            lambda c: c['trading']['symbols'][0].__setitem__('enabled', None),
            lambda c: c['scheduler'].__setitem__('check_hour', None),
            lambda c: c['scheduler'].__setitem__(
                'stop_loss_scan_interval_minutes', None),
            lambda c: c['okx'].__setitem__('margin_mode', None),
            lambda c: c['okx'].__setitem__('leverage', None),
            lambda c: c['okx'].__setitem__('leverage_overrides', None),
            lambda c: c.__setitem__('equity_tick_retention_days', None),
        )
        for mutate in cases:
            config = self._config()
            mutate(config)
            with self.subTest(config=config), self.assertRaises(ValueError):
                cv.validate_and_normalize_execution_config(config)

    def test_okx_environment_boolean_is_normalized_and_conflicts_rejected(self):
        config = self._config()
        config['okx']['sandbox'] = 'false'
        cv.validate_and_normalize_execution_config(config)
        self.assertIs(config['okx']['sandbox'], False)

        for bad in ('0', 0, None, 'maybe'):
            config = self._config()
            config['okx']['sandbox'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cv.validate_and_normalize_execution_config(config)

        config = self._config()
        config['okx'].update({'sandbox': False, 'demo': True})
        with self.assertRaisesRegex(ValueError, '矛盾'):
            cv.validate_and_normalize_execution_config(config)

    def test_unknown_block_fields_and_scheduler_booleans_are_rejected(self):
        mutations = (
            lambda c: c.__setitem__('obsolete_execution', {}),
            lambda c: c['trading'].__setitem__('obsolete_strategy', {}),
            lambda c: c['scheduler'].__setitem__('check_huor', 8),
            lambda c: c['okx'].__setitem__('obsolete_strategy', {}),
            lambda c: c['scheduler'].__setitem__('check_hour', True),
        )
        for mutate in mutations:
            config = self._config()
            mutate(config)
            with self.subTest(config=config), self.assertRaises(ValueError):
                cv.validate_and_normalize_execution_config(config)

    def test_dingtalk_and_okx_text_fields_are_strictly_typed(self):
        mutations = (
            lambda c: c.__setitem__('dingtalk', None),
            lambda c: c.__setitem__('dingtalk', {'obsolete': True}),
            lambda c: c.__setitem__('dingtalk', {'webhook_url': 123}),
            lambda c: c['okx'].__setitem__('apiKey', {}),
            lambda c: c['okx'].__setitem__('secret', []),
            lambda c: c['okx'].__setitem__('password', 123),
            lambda c: c['okx'].__setitem__('label', False),
        )
        for mutate in mutations:
            config = self._config()
            mutate(config)
            with self.subTest(config=config), self.assertRaises(ValueError):
                cv.validate_and_normalize_execution_config(config)

    def test_okx_execution_values_are_normalized_and_null_overrides_rejected(self):
        config = self._config()
        config['okx'].update({
            'margin_mode': ' ISOLATED ', 'leverage': '9',
            'leverage_overrides': {'btcusdt': '3.5'},
        })

        cv.validate_and_normalize_execution_config(config)

        self.assertEqual('isolated', config['okx']['margin_mode'])
        self.assertEqual(9, config['okx']['leverage'])
        self.assertEqual(
            {'BTCUSDT': 3.5}, config['okx']['leverage_overrides'])
        config = self._config()
        config['okx']['leverage_overrides'] = {'BTCUSDT': None}
        with self.assertRaises(ValueError):
            cv.validate_and_normalize_execution_config(config)

    def test_legacy_layout_is_only_canonicalized_by_explicit_migration_helper(self):
        config = self._config()
        old = {
            'exchanges': {'okx': dict(
                config['okx'], strategy=config['strategy'],
                trading=config['trading'])},
            'scheduler': {},
        }

        self.assertTrue(cv.canonicalize_single_okx_config(old))
        cv.validate_and_normalize_execution_config(old)

        self.assertNotIn('exchanges', old)
        self.assertIn('okx', old)
        dual = self._config()
        dual['exchanges'] = {'okx': {'sandbox': True}}
        with self.assertRaisesRegex(ValueError, 'exchanges'):
            cv.validate_and_normalize_execution_config(dual)

        legacy = {
            'exchanges': {'okx': dict(config['okx'])},
            'strategy': config['strategy'], 'trading': config['trading'],
            'scheduler': {},
        }
        with self.assertRaisesRegex(ValueError, 'exchanges'):
            cv.validate_and_normalize_execution_config(legacy)


if __name__ == '__main__':
    unittest.main()
