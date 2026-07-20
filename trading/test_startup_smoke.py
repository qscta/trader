"""启动装配链路冒烟测试（本机可运行）。

其余测试均用 TradingSystem.__new__ 绕过构造——本文件专门端到端验证 __init__ 全链：
load_config（旧布局拒绝/环境变量/凭据校验）→ 离线迁移门禁 → 归属护栏 → 策略构建 →
TradeState/EquityTracker 装配 → 权益获取 → RiskManager → 启动持仓同步 → 定时任务注册。
"""
import json
import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

import _test_stubs

main = _test_stubs.import_main()
TradingSystem = main.TradingSystem



def _jload(path):
    with open(path) as f:
        return json.load(f)


def _jdump(data, path):
    with open(path, 'w') as f:
        json.dump(data, f)

class _FakeOkxApi:
    """启动冒烟用：返回正常余额、无持仓。"""

    def __init__(self, config):
        self.config = config

    def verify_one_way_mode(self):
        return None

    def get_balance(self):
        return {'total': {'USDT': 10000.0}, 'free': {'USDT': 10000.0}}

    def get_position(self, symbol):
        return None

    def list_position_symbols(self):
        return []

    def to_ccxt_symbol(self, symbol):
        return symbol[:-4] + '/USDT:USDT' if symbol.endswith('USDT') else symbol

    def open_position(
            self, symbol, side, amount, client_order_id=None, *,
            require_existing=False):
        raise AssertionError('无信号启动冒烟不得开仓')

    def close_position(
            self, symbol, side, amount, client_order_id=None, *,
            require_existing=False):
        raise AssertionError('空仓启动冒烟不得平仓')

    @staticmethod
    def compensation_client_order_id(open_client_order_id):
        return f'R{str(open_client_order_id)[1:]}'

    def find_existing_open_order(self, *args, **kwargs):
        return None

    def find_compensation_close_progress(
            self, _symbol, _side, amount, open_client_order_id):
        client_order_id = self.compensation_client_order_id(
            open_client_order_id)
        return {
            'terminal': None, 'absent': True, 'confirmed': False,
            'filled': 0.0, 'amount': 0.0,
            'requested_amount': float(amount),
            'remaining_amount': float(amount),
            'clientOrderId': client_order_id,
            'ids': [], 'read_only_evidence': True, 'order': None,
            'order_state': {
                'client_order_id': client_order_id,
                'presence': 'absent', 'terminal': None, 'filled': None,
            },
        }

    def confirm_stop_execution(self, *args, **kwargs):
        return False

    def cancel_stop_order_only(self, symbol, order_id):
        return True

    def get_last_price(self, symbol):
        return 100.0

    def setup_symbol(self, symbol):
        return None

    def round_quantity(self, symbol, quantity):
        return float(quantity)

    def get_quantity_precision(self, symbol):
        return 3

    def create_stop_loss_order(
            self, symbol, side, amount, stop_price, *,
            require_existing=False):
        raise AssertionError('无信号启动冒烟不得挂止损')

    def cancel_order(self, symbol, order_id):
        return True

    def cancel_all_orders(self, symbol):
        return True

    def find_stop_order_state(self, *args, **kwargs):
        return {'status': 'missing'}

    def _get_contract_size(self, symbol):
        return 1.0

    def _coin_to_contracts(self, symbol, amount):
        return float(amount)

    def _contracts_to_coins(self, symbol, contracts):
        return float(contracts)


def _write_config(tmp, extra=None):
    cfg = {
        'okx': {'label': '欧易', 'apiKey': 'k', 'secret': 's', 'password': 'p', 'sandbox': True},
        'strategy': {'ma_short_period': 6, 'ma_long_period': 28,
                     'ma_stop_period': 28, 'default_risk_per_trade': 0.01},
        'trading': {'symbols': [{'name': 'BTCUSDT', 'enabled': True}]},
        'scheduler': {}, 'dingtalk': {},
    }
    if extra:
        cfg.update(extra)
    path = os.path.join(tmp, 'config.json')
    with open(path, 'w') as f:
        json.dump(cfg, f)
    return path


class StartupSmokeTest(unittest.TestCase):
    def _boot(self, tmp):
        with patch.object(main, 'OkxApi', _FakeOkxApi):
            return TradingSystem(config_file=_write_config(tmp))

    def test_full_init_chain(self):
        """完整构造一遍：所有装配步骤不炸，关键部件就位。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._boot(tmp)
            self.assertEqual(system.exchange_id, 'okx')
            self.assertEqual(system.label, '欧易')
            self.assertIsInstance(system.exchange_api, _FakeOkxApi)   # 适配器装配
            self.assertEqual(system.config['strategy']['ma_short_period'], 6)
            self.assertEqual(system._stop_anomalies, {})
            self.assertEqual(system.data_dir, tmp)                    # 状态落在配置所在目录
            # 归属护栏已认领全新状态（trade_state / equity_tracker 为真实现）
            self.assertEqual(system.trade_state.get_owner_exchange(), 'okx')
            self.assertEqual(system.equity_tracker.data_dir, tmp)

    def test_non_utc8_runtime_is_rejected_before_exchange_reads(self):
        class UtcDateTime:
            @classmethod
            def now(cls):
                return type('UtcNow', (), {
                    'astimezone': lambda self: type('UtcLocal', (), {
                        'utcoffset': lambda self: timedelta(0)})()
                })()

        class CountingApi(_FakeOkxApi):
            init_calls = 0
            balance_reads = 0

            def __init__(self, config):
                type(self).init_calls += 1
                super().__init__(config)

            def get_balance(self):
                type(self).balance_reads += 1
                return super().get_balance()

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(main, 'OkxApi', CountingApi), \
                patch.object(main, 'datetime', UtcDateTime), \
                self.assertRaisesRegex(RuntimeError, r'UTC\+8'):
            TradingSystem(config_file=_write_config(tmp))
        self.assertEqual(0, CountingApi.init_calls)
        self.assertEqual(0, CountingApi.balance_reads)

    def test_missing_post_fill_capability_blocks_before_balance_or_post(self):
        class MissingStopApi(_FakeOkxApi):
            create_stop_loss_order = None
            balance_reads = 0

            def get_balance(self):
                type(self).balance_reads += 1
                return super().get_balance()

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(main, 'OkxApi', MissingStopApi), \
                self.assertRaisesRegex(RuntimeError, 'create_stop_loss_order'):
            TradingSystem(config_file=_write_config(tmp))
        self.assertEqual(0, MissingStopApi.balance_reads)

    def test_adapter_internal_helpers_are_not_outer_startup_contract(self):
        internal_helpers = (
            '_confirm_market_order', '_resolve_unconfirmed_open',
            '_build_close_result', '_fetch_order_for_confirmation',
            '_confirmed_order_result', '_contracts_tolerance',
            '_finite_nonnegative', '_position_side',
        )
        private_api = type(
            'PrivateHelpersHiddenApi', (_FakeOkxApi,),
            {helper: None for helper in internal_helpers})
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(main, 'OkxApi', private_api):
            system = TradingSystem(config_file=_write_config(tmp))
        self.assertIs(type(system.exchange_api), private_api)

    def test_directly_used_okx_helpers_remain_required_before_exchange_reads(self):
        for helper in (
                '_get_contract_size', '_coin_to_contracts',
                '_contracts_to_coins'):
            with self.subTest(helper=helper):
                missing_api = type(
                    f'Missing{helper}Api', (_FakeOkxApi,),
                    {helper: None, 'balance_reads': 0})
                with tempfile.TemporaryDirectory() as tmp, \
                        patch.object(main, 'OkxApi', missing_api), \
                        self.assertRaisesRegex(RuntimeError, helper):
                    TradingSystem(config_file=_write_config(tmp))
                self.assertEqual(0, missing_api.balance_reads)

    def test_missing_open_intent_finalizer_blocks_before_exchange_reads(self):
        original = TradingSystem._finalize_open_intent_rollback
        try:
            TradingSystem._finalize_open_intent_rollback = None
            with tempfile.TemporaryDirectory() as tmp, \
                    patch.object(main, 'OkxApi', _FakeOkxApi), \
                    self.assertRaisesRegex(
                        RuntimeError, '_finalize_open_intent_rollback'):
                TradingSystem(config_file=_write_config(tmp))
        finally:
            TradingSystem._finalize_open_intent_rollback = original

    def test_missing_ledger_commit_capability_blocks_before_exchange_reads(self):
        class MissingAddTradeState(main.TradeState):
            add_open_position = None

        class CountingApi(_FakeOkxApi):
            balance_reads = 0

            def get_balance(self):
                type(self).balance_reads += 1
                return super().get_balance()

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(main, 'TradeState', MissingAddTradeState), \
                patch.object(main, 'OkxApi', CountingApi), \
                self.assertRaisesRegex(RuntimeError, 'add_open_position'):
            TradingSystem(config_file=_write_config(tmp))
        self.assertEqual(0, CountingApi.balance_reads)

    def test_every_public_exchange_stub_requires_concrete_okx_override(self):
        public_capabilities = (
            'to_ccxt_symbol', 'get_position', 'list_position_symbols',
            'verify_one_way_mode', 'setup_symbol',
            'open_position', 'close_position',
            'compensation_client_order_id', 'create_stop_loss_order',
            'cancel_stop_order_only', 'cancel_order', 'cancel_all_orders',
            'round_quantity', 'get_quantity_precision',
            'find_stop_order_state', 'find_existing_open_order',
            'find_compensation_close_progress',
            'confirm_stop_execution',
        )
        for capability in public_capabilities:
            with self.subTest(capability=capability):
                base_stub_api = type(
                    f'BaseStub{capability}Api', (_FakeOkxApi,),
                    {capability: getattr(main.ExchangeApi, capability)})
                with tempfile.TemporaryDirectory() as tmp, \
                        patch.object(main, 'OkxApi', base_stub_api), \
                        self.assertRaisesRegex(
                            RuntimeError, rf'{capability}.*base_stub'):
                    TradingSystem(config_file=_write_config(tmp))

    def test_unowned_lifecycle_state_is_never_auto_claimed(self):
        lifecycle_cases = (
            {'last_daily_summary_date': '2026-07-17'},
            {'stop_loss_dates_migrated': True},
        )
        for lifecycle in lifecycle_cases:
            with self.subTest(lifecycle=lifecycle), \
                    tempfile.TemporaryDirectory() as tmp:
                path = _write_config(tmp)
                state = {
                    'open_positions': {}, 'closed_trades': [],
                    **lifecycle,
                }
                state_path = os.path.join(tmp, 'trade_state.json')
                _jdump(state, state_path)
                os.chmod(state_path, 0o600)
                with patch.object(main, 'OkxApi', _FakeOkxApi), \
                        self.assertRaisesRegex(RuntimeError, '无交易所归属'):
                    TradingSystem(config_file=path)

    def test_okx_owner_allows_existing_lifecycle_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp)
            state_path = os.path.join(tmp, 'trade_state.json')
            _jdump({
                'open_positions': {}, 'closed_trades': [],
                'exchange': 'okx',
                'last_daily_summary_date': '2026-07-17',
            }, state_path)
            os.chmod(state_path, 0o600)

            with patch.object(main, 'OkxApi', _FakeOkxApi):
                system = TradingSystem(config_file=path)

            self.assertEqual('okx', system.trade_state.get_owner_exchange())

    def test_foreign_or_empty_exchange_claim_is_rejected(self):
        for owner in ('', 'other'):
            with self.subTest(owner=owner), \
                    tempfile.TemporaryDirectory() as tmp:
                path = _write_config(tmp)
                state_path = os.path.join(tmp, 'trade_state.json')
                _jdump({
                    'open_positions': {}, 'closed_trades': [],
                    'exchange': owner,
                }, state_path)
                os.chmod(state_path, 0o600)
                with patch.object(main, 'OkxApi', _FakeOkxApi), \
                        self.assertRaises(main.TradeStatePersistenceError):
                    TradingSystem(config_file=path)

    def test_owner_manifest_uses_the_same_strict_schema_at_startup(self):
        bad_manifests = (
            {'exchange': 'okx', 'obsolete': True},
            {'exchange': 'okx', 'claimed_at': 123},
            {'exchange': 'okx', 'claimed_at': 'not-an-iso-time'},
        )
        for manifest in bad_manifests:
            with self.subTest(manifest=manifest), \
                    tempfile.TemporaryDirectory() as tmp:
                path = _write_config(tmp)
                state_path = os.path.join(tmp, 'trade_state.json')
                _jdump({
                    'open_positions': {}, 'closed_trades': [],
                    'exchange': 'okx',
                }, state_path)
                os.chmod(state_path, 0o600)
                manifest_path = os.path.join(
                    tmp, '.trading_data_owner.json')
                _jdump(manifest, manifest_path)
                os.chmod(manifest_path, 0o600)
                with patch.object(main, 'OkxApi', _FakeOkxApi), \
                        self.assertRaisesRegex(RuntimeError, '归属标记非法'):
                    TradingSystem(config_file=path)

    def test_register_jobs_smoke(self):
        """定时任务注册链路可空转（调度器为桩）。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._boot(tmp)
            system.register_jobs(system.config.get('scheduler', {}))  # 不应抛异常

    def test_legacy_nested_config_requires_offline_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {'exchanges': {'okx': {'label': '欧易', 'apiKey': 'k', 'secret': 's', 'password': 'p',
                                          'strategy': {'default_risk_per_trade': 0.01},
                                          'trading': {'symbols': []}}},
                   'scheduler': {}, 'dingtalk': {}}
            path = os.path.join(tmp, 'config.json')
            with open(path, 'w') as f:
                json.dump(cfg, f)
            with patch.object(main, 'OkxApi', _FakeOkxApi), \
                    self.assertRaisesRegex(ValueError, '离线单策略迁移'):
                TradingSystem(config_file=path)

    def test_dual_okx_config_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp)
            config = _jload(path)
            config['exchanges'] = {'okx': {'sandbox': False}}
            _jdump(config, path)
            with patch.object(main, 'OkxApi', _FakeOkxApi), \
                    self.assertRaisesRegex(ValueError, '旧 config.exchanges'):
                TradingSystem(config_file=path)

    def test_duplicate_config_fields_are_rejected_instead_of_last_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.json')
            raw = (
                '{"okx":{"apiKey":"k","secret":"s","password":"p",'
                '"sandbox":true},'
                '"strategy":{"default_risk_per_trade":0.01,'
                '"default_risk_per_trade":0.5},'
                '"trading":{"symbols":[]},"scheduler":{},"dingtalk":{}}')
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(raw)
            with patch.object(main, 'OkxApi', _FakeOkxApi), \
                    self.assertRaisesRegex(ValueError, '重复字段'):
                TradingSystem(config_file=path)

    def test_missing_credentials_rejected(self):
        """凭据缺失：构造必须拒绝（fail closed），不能带病启动。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp)
            cfg = _jload((path))
            del cfg['okx']['secret']
            _jdump(cfg, path)
            env_backup = {k: os.environ.pop(k, None) for k in
                          ('OKX_API_KEY', 'OKX_API_SECRET', 'OKX_API_PASSPHRASE', 'OKX_PASSWORD')}
            try:
                with patch.object(main, 'OkxApi', _FakeOkxApi):
                    with self.assertRaises(ValueError):
                        TradingSystem(config_file=path)
            finally:
                for k, v in env_backup.items():
                    if v is not None:
                        os.environ[k] = v

    def test_missing_strategy_key_rejected(self):
        """策略必需键（default_risk_per_trade）缺失：清晰 ValueError 拒绝，
        不裸 KeyError 崩溃、更不静默塞默认值（真钱系统默认策略参数比拒启更危险）。"""
        for missing_key in ('default_risk_per_trade',):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_config(tmp)
                cfg = _jload(path)
                del cfg['strategy'][missing_key]
                _jdump(cfg, path)
                with patch.object(main, 'OkxApi', _FakeOkxApi):
                    with self.assertRaises(ValueError):
                        TradingSystem(config_file=path)

    def test_explicit_null_execution_fields_rejected_at_startup(self):
        mutations = (
            lambda c: c.__setitem__('okx', None),
            lambda c: c.__setitem__('strategy', None),
            lambda c: c.__setitem__('trading', None),
            lambda c: c.__setitem__('scheduler', None),
            lambda c: c['strategy'].__setitem__('ma_short_period', None),
            lambda c: c['trading']['symbols'][0].__setitem__(
                'risk_per_trade', None),
            lambda c: c['trading']['symbols'][0].__setitem__('enabled', None),
            lambda c: c['scheduler'].__setitem__('summary_minute', None),
            lambda c: c['scheduler'].__setitem__(
                'stop_loss_scan_interval_minutes', None),
            lambda c: c['okx'].__setitem__('margin_mode', None),
            lambda c: c['okx'].__setitem__('leverage_overrides', None),
            lambda c: c.__setitem__('equity_tick_retention_days', None),
        )
        for mutate in mutations:
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_config(tmp)
                config = _jload(path)
                mutate(config)
                _jdump(config, path)
                with self.subTest(config=config), \
                        patch.object(main, 'OkxApi', _FakeOkxApi), \
                        self.assertRaises(ValueError):
                    TradingSystem(config_file=path)

    def test_non_object_top_level_config_is_rejected_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.json')
            _jdump([], path)
            with patch.object(main, 'OkxApi', _FakeOkxApi), \
                    self.assertRaisesRegex(ValueError, '顶层必须是对象'):
                TradingSystem(config_file=path)

    def test_sandbox_string_false_normalizes_to_real_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp)
            config = _jload(path)
            config['okx']['sandbox'] = 'false'
            _jdump(config, path)
            with patch.object(main, 'OkxApi', _FakeOkxApi):
                system = TradingSystem(config_file=path)
            self.assertIs(system.config['okx']['sandbox'], False)

    def test_out_of_range_strategy_param_rejected(self):
        """手写 config.json 的非法范围值：启动即拒（与前端/API 改参同口径），
        不带着 ma_long_period=0（EMA 计算崩溃）/ 负风险度（负仓位）等危险配置运行。"""
        bad_cases = [
            {'ma_long_period': 0},                    # 周期下限
            {'ma_long_period': 501},                  # 周期上限
            {'default_risk_per_trade': -0.1},         # 负风险度
            {'default_risk_per_trade': 0},            # 零风险度
            {'default_risk_per_trade': 0.6},          # 超 50% 上限
            {'ma_short_period': 28, 'ma_long_period': 28},  # 短 >= 长
            {'ma_short_period': 30, 'ma_long_period': 20},  # 短 > 长
        ]
        for bad in bad_cases:
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_config(tmp)
                cfg = _jload(path)
                cfg['strategy'].update(bad)
                _jdump(cfg, path)
                with patch.object(main, 'OkxApi', _FakeOkxApi):
                    with self.assertRaises(ValueError, msg=f"应拒绝非法配置: {bad}"):
                        TradingSystem(config_file=path)

    def test_out_of_range_symbol_config_rejected(self):
        """手写 config.json 的品种池非法值：启动即拒（与增删品种的 API 入口同口径），
        堵住「手写配置绕过风控」——100% 风险度 / 脏交易对名都不得带病启动。"""
        bad_symbol_lists = [
            [{'name': 'BTC-USDT'}],                                # 含非法字符
            [{'name': 'BTCUSD'}],                                  # 非 USDT 结尾
            [{'name': 123}],                                       # 非字符串名
            [{'name': 'BTCUSDT', 'risk_per_trade': 1.0}],           # 风险度 100% 超上限
            [{'name': 'BTCUSDT', 'risk_per_trade': -0.01}],         # 负风险度
            [{'name': 'BTCUSDT', 'risk_per_trade': 'inf'}],         # 非有限风险度
            [{'name': 'BTCUSDT', 'enabled': 'maybe'}],             # 非法布尔（歧义值拒绝）
            [{'name': 'BTCUSDT'}, {'name': 'BTCUSDT'}],            # 重复交易对
        ]
        for bad in bad_symbol_lists:
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_config(tmp)
                cfg = _jload(path)
                cfg['trading']['symbols'] = bad
                _jdump(cfg, path)
                with patch.object(main, 'OkxApi', _FakeOkxApi):
                    with self.assertRaises(ValueError, msg=f"应拒绝非法品种池: {bad}"):
                        TradingSystem(config_file=path)

    def test_string_typed_params_normalized(self):
        """字符串数值（"28" / "0.01"）通过校验后必须规范化为 int/float 写回——
        否则构造 MaCrossStrategy("28")/RiskManager 权益×"0.01" 会在盘中 TypeError。
        品种名小写/带空格规范化为大写；字符串 "true"/"false" 解析为真 bool。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp)
            cfg = _jload(path)
            cfg['strategy']['ma_short_period'] = "6"
            cfg['strategy']['default_risk_per_trade'] = "0.01"
            cfg['trading']['symbols'] = [
                {'name': ' btcusdt ', 'risk_per_trade': "0.02", 'enabled': 'true'}]
            _jdump(cfg, path)
            with patch.object(main, 'OkxApi', _FakeOkxApi):
                system = TradingSystem(config_file=path)
            self.assertIsInstance(system.config['strategy']['ma_short_period'], int)
            self.assertIsInstance(system.config['strategy']['default_risk_per_trade'], float)
            sym = system.config['trading']['symbols'][0]
            self.assertEqual(sym['name'], 'BTCUSDT')                 # 去空格+大写
            self.assertIsInstance(sym['risk_per_trade'], float)
            self.assertIs(sym['enabled'], True)                     # "true" → 真 bool

    def test_string_false_enabled_parsed_not_truthy(self):
        """关键回归：字符串 "false" 必须解析为 False（Python bool("false")==True 的陷阱），
        否则被禁用的品种会被当成启用继续开仓。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp)
            cfg = _jload(path)
            cfg['trading']['symbols'] = [{'name': 'BTCUSDT', 'enabled': 'false'}]
            _jdump(cfg, path)
            with patch.object(main, 'OkxApi', _FakeOkxApi):
                system = TradingSystem(config_file=path)
            self.assertIs(system.config['trading']['symbols'][0]['enabled'], False)

    def test_incompatible_symbol_strategy_field_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp)
            cfg = _jload(path)
            cfg['trading']['symbols'] = [
                {'name': 'BTCUSDT', 'enabled': True, 'strategy': 'unsupported',
                 'risk_per_trade': 0.01}]
            _jdump(cfg, path)
            with patch.object(main, 'OkxApi', _FakeOkxApi):
                with self.assertRaisesRegex(ValueError, '未知字段'):
                    TradingSystem(config_file=path)

    def test_unknown_strategy_and_symbol_fields_are_rejected(self):
        for section, field in (('strategy', 'obsolete_period'),
                               ('symbol', 'obsolete_flag')):
            with self.subTest(section=section):
                with tempfile.TemporaryDirectory() as tmp:
                    path = _write_config(tmp)
                    cfg = _jload(path)
                    if section == 'strategy':
                        cfg['strategy'][field] = 20
                    else:
                        cfg['trading']['symbols'][0][field] = True
                    _jdump(cfg, path)
                    with patch.object(main, 'OkxApi', _FakeOkxApi):
                        with self.assertRaisesRegex(ValueError, '未知字段'):
                            TradingSystem(config_file=path)

    def test_fractional_period_rejected(self):
        """严格整数：小数周期（28.9 / "28.9"）拒绝而非静默截断为 28；
        inf/-inf/nan 走干净 ValueError（而非 OverflowError 崩溃/500）。"""
        for bad_period in (28.9, "28.9", "inf", "-inf", "nan"):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_config(tmp)
                cfg = _jload(path)
                cfg['strategy']['ma_long_period'] = bad_period
                _jdump(cfg, path)
                with patch.object(main, 'OkxApi', _FakeOkxApi):
                    with self.assertRaises(ValueError, msg=f"应以 ValueError 拒绝: {bad_period!r}"):
                        TradingSystem(config_file=path)

    def test_example_config_is_bootable(self):
        """config.example.json 填上凭据即可启动——保证示例配置永远与代码同步。"""
        with tempfile.TemporaryDirectory() as tmp:
            example = _jload((os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  'config.example.json')))
            example['okx'].update({'apiKey': 'k', 'secret': 's', 'password': 'p'})
            path = os.path.join(tmp, 'config.json')
            _jdump(example, path)
            with patch.object(main, 'OkxApi', _FakeOkxApi):
                system = TradingSystem(config_file=path)
            self.assertEqual(system.exchange_id, 'okx')

    def test_out_of_range_scheduler_config_rejected(self):
        """手写 config.json 的调度参数非法值：启动即拒（给清晰 ValueError，
        而非 register_jobs 里 check_minute+1 的 TypeError / APScheduler 内部错）。"""
        bad_cases = [
            {'check_hour': 24},                         # 小时上限
            {'check_hour': -1},                         # 小时下限
            {'check_minute': 60},                       # 分钟上限
            {'summary_minute': "xx"},                   # 非数字字符串
            {'weekly_hour': 25},                        # 小时越界
            {'stop_loss_scan_interval_minutes': 0},     # 间隔下限
            {'check_minute': 28.9},                     # 非整数
        ]
        for bad in bad_cases:
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_config(tmp)
                cfg = _jload(path)
                cfg.setdefault('scheduler', {}).update(bad)
                _jdump(cfg, path)
                with patch.object(main, 'OkxApi', _FakeOkxApi):
                    with self.assertRaises(ValueError, msg=f"应拒绝非法调度配置: {bad}"):
                        TradingSystem(config_file=path)

    def test_string_typed_scheduler_params_normalized(self):
        """字符串数值（"8" / "0"）通过校验后规范化为 int 写回——
        否则 register_jobs 的 check_minute + 1 / {check_hour:02d} 会 TypeError。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp)
            cfg = _jload(path)
            cfg.setdefault('scheduler', {}).update(
                {'check_hour': "8", 'check_minute': "0", 'stop_loss_scan_interval_minutes': "5"})
            _jdump(cfg, path)
            with patch.object(main, 'OkxApi', _FakeOkxApi):
                system = TradingSystem(config_file=path)
            sched = system.config['scheduler']
            self.assertIsInstance(sched['check_hour'], int)
            self.assertIsInstance(sched['check_minute'], int)
            self.assertIsInstance(sched['stop_loss_scan_interval_minutes'], int)

    def test_equity_tick_retention_days_validated(self):
        """equity_tick_retention_days 与其余数值配置同标准 fail-loud：
        非法值启动即拒（此前由 EquityTracker 静默吞掉，配置值与生效值悄悄不一致）；
        合法字符串数值规范化为 int 写回。"""
        for bad in (0, 6, 3651, "abc", 28.9):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_config(tmp)
                cfg = _jload(path)
                cfg['equity_tick_retention_days'] = bad
                _jdump(cfg, path)
                with patch.object(main, 'OkxApi', _FakeOkxApi):
                    with self.assertRaises(ValueError, msg=f"应拒绝非法保留天数: {bad!r}"):
                        TradingSystem(config_file=path)
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp)
            cfg = _jload(path)
            cfg['equity_tick_retention_days'] = "30"
            _jdump(cfg, path)
            with patch.object(main, 'OkxApi', _FakeOkxApi):
                system = TradingSystem(config_file=path)
            self.assertEqual(system.config['equity_tick_retention_days'], 30)
            self.assertEqual(system.equity_tracker.EQUITY_TICK_RETENTION_DAYS, 30)

    def test_balance_fetch_exception_exits_with_alert(self):
        """启动权益获取抛异常（网络重试耗尽/密钥错误）：必须走「钉钉告警 + sys.exit(1)」
        路径退出，不得裸 traceback 静默死亡（历轮审查确立的最贵故障模式红线）。"""
        class _BoomApi(_FakeOkxApi):
            def get_balance(self):
                raise RuntimeError('模拟网络/认证异常')

        alerts = []

        class _RecordingNotifier:
            def __init__(self, webhook):
                pass

            def notify_error(self, msg):
                alerts.append(msg)
                return True

        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp)
            from types import SimpleNamespace
            import time as _time
            with patch.object(main, 'OkxApi', _BoomApi), \
                 patch.object(main, 'DingTalkNotifier', _RecordingNotifier), \
                 patch.object(main, 'time', SimpleNamespace(sleep=lambda s: None, time=_time.time)):
                with self.assertRaises(SystemExit) as ctx:
                    TradingSystem(config_file=path)
        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(any('无法获取初始账户权益' in m for m in alerts),
                        f'退出前必须补发钉钉告警，实际: {alerts}')

    def test_large_scan_interval_passes_validation(self):
        """巡检间隔 ≥ 60 分钟必须通过共享启动校验（允许 [1,1440]）。

        注意：本标准库套件把 BackgroundScheduler 换成 Dummy 桩，add_job 空转、不校验
        cron 表达式——所以「register_jobs 对 '*/60' 是否崩溃」只能在装了真 apscheduler
        的依赖套件（tests/test_trading_logic_unittest.SchedulerIntervalTests）里验证。
        这里只锁定「校验放行」这半边契约，与那边的「注册不崩」共同构成完整回归。"""
        for interval in (60, 120, 1440):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_config(tmp)
                cfg = _jload(path)
                cfg.setdefault('scheduler', {})['stop_loss_scan_interval_minutes'] = interval
                _jdump(cfg, path)
                with patch.object(main, 'OkxApi', _FakeOkxApi):
                    system = TradingSystem(config_file=path)   # 校验须放行、不抛
                self.assertEqual(
                    system.config['scheduler']['stop_loss_scan_interval_minutes'], interval)


class StartupSyncFailureAlertTest(unittest.TestCase):
    """启动对账失败：拒绝启动前必须发出钉钉告警——裸 traceback 静默死亡
    是本仓库定义的最贵故障模式，构造三阶段（配置/权益/对账）须同标准。"""

    def test_sync_failure_alerts_before_refusing_to_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp)
            with patch.object(main, 'OkxApi', _FakeOkxApi), \
                    patch.object(main.TradingSystem, 'sync_positions_on_startup',
                                 side_effect=RuntimeError('sync boom')), \
                    patch.object(main, 'DingTalkNotifier') as notifier_cls:
                with self.assertRaises(RuntimeError):
                    TradingSystem(config_file=path)
        # 非 UTC+8 环境跑测试时还会有时区自检告警，故按内容匹配而非次数。
        messages = [c.args[0] for c in
                    notifier_cls.return_value.notify_error.call_args_list]
        self.assertTrue(any('启动持仓对账失败' in m for m in messages))


class StartupEquityParsingTest(unittest.TestCase):
    """启动权益必须是有限非负数：NaN 住进 RiskManager 会静默禁用
    pending 恢复分支的成交后风险校验（NaN 与 0 比较恒 False）。"""

    def test_garbage_equity_rejected(self):
        for bad in (None, {}, {'total': None}, {'total': {}},
                    {'total': '垃圾'}, {'total': 5}, {'total': ['USDT']},
                    {'total': {'USDT': None}}, {'total': {'USDT': True}},
                    {'total': {'USDT': float('nan')}},
                    {'total': {'USDT': float('inf')}},
                    {'total': {'USDT': -1.0}}, {'total': {'USDT': '垃圾'}}):
            with self.subTest(bad=bad):
                self.assertIsNone(main._parse_startup_equity(bad))

    def test_valid_equity_accepted_including_empty_account(self):
        self.assertEqual(10000.5, main._parse_startup_equity(
            {'total': {'USDT': 10000.5}}))
        self.assertEqual(10000.5, main._parse_startup_equity(
            {'total': {'USDT': '10000.5'}}))
        self.assertEqual(0.0, main._parse_startup_equity(
            {'total': {'USDT': 0}}))

    def test_huge_integer_equity_is_rejected_without_overflow(self):
        self.assertIsNone(main._parse_startup_equity(
            {'total': {'USDT': 10 ** 10000}}))


if __name__ == '__main__':
    unittest.main()
