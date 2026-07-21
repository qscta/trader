"""启动装配链路冒烟测试（本机可运行）。

其余测试均用 TradingSystem.__new__ 绕过构造——本文件专门端到端验证 __init__ 全链：
load_config（含旧格式拍平/环境变量/凭据校验）→ 归属护栏 → 策略构建 →
TradeState/EquityTracker 装配 → 权益获取 → RiskManager → 启动持仓同步 → 定时任务注册。
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tests.unit import _test_stubs

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

    def get_balance(self):
        return {'total': {'USDT': 10000.0}, 'free': {'USDT': 10000.0}}

    def get_position(self, symbol):
        return None

    def list_position_symbols(self):
        return []

    def to_ccxt_symbol(self, symbol):
        return symbol[:-4] + '/USDT:USDT' if symbol.endswith('USDT') else symbol


def _write_config(tmp, extra=None):
    cfg = {
        'okx': {'label': '欧易', 'apiKey': 'k', 'secret': 's', 'password': 'p', 'sandbox': True},
        'strategy': {'ma_short_period': 6, 'ma_long_period': 28,
                     'ma_stop_period': 28, 'default_risk_per_trade': 0.01},
        'trading': {'symbols': [{'name': 'BTCUSDT', 'enabled': True, 'strategy': 'ma_cross'}]},
        'scheduler': {}, 'dingtalk': {},
    }
    if extra:
        cfg.update(extra)
    path = os.path.join(tmp, 'config.json')
    with open(path, 'w') as f:
        json.dump(cfg, f)
    return path


class RunnerLockTest(unittest.TestCase):
    def test_all_entrypoints_share_one_nonblocking_process_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'runner.lock')
            first = main.acquire_runner_lock(path)
            try:
                with self.assertRaisesRegex(RuntimeError, '拒绝重复启动'):
                    main.acquire_runner_lock(path)
            finally:
                first.close()

            released = main.acquire_runner_lock(path)
            released.close()


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

    def test_register_jobs_smoke(self):
        """定时任务注册链路可空转（调度器为桩）。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._boot(tmp)
            system.register_jobs(system.config.get('scheduler', {}))  # 不应抛异常

    def test_deployment_gate_is_loaded_from_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {'TRADING_DISABLE_NEW_OPENS': '1'}):
                system = self._boot(tmp)
            self.assertTrue(system.new_entries_disabled)

    def test_legacy_nested_config_flattened(self):
        """旧多所格式 {exchanges:{okx:...}} 自动拍平后可正常启动。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {'exchanges': {'okx': {'label': '欧易', 'apiKey': 'k', 'secret': 's', 'password': 'p',
                                          'strategy': {'default_risk_per_trade': 0.01},
                                          'trading': {'symbols': []}}},
                   'scheduler': {}, 'dingtalk': {}}
            path = os.path.join(tmp, 'config.json')
            with open(path, 'w') as f:
                json.dump(cfg, f)
            with patch.object(main, 'OkxApi', _FakeOkxApi):
                system = TradingSystem(config_file=path)
            self.assertIn('okx', system.config)
            self.assertEqual(system.config['okx']['apiKey'], 'k')

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

    def test_out_of_range_strategy_param_rejected(self):
        """手写 config.json 的非法范围值：启动即拒（与前端/API 改参同口径），
        不带着 ma_long_period=501（周期越界）/ 负风险度（负仓位）等危险配置运行。"""
        bad_cases = [
            {'ma_long_period': 1},                    # 周期下限（< PERIOD_MIN）
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
        堵住「手写配置绕过风控」——100% 风险度 / 非法策略名 / 脏交易对名都不得带病启动。"""
        bad_symbol_lists = [
            [{'name': 'BTC-USDT', 'strategy': 'ma_cross'}],         # 含非法字符
            [{'name': 'BTCUSD', 'strategy': 'ma_cross'}],           # 非 USDT 结尾
            [{'name': 123, 'strategy': 'ma_cross'}],                # 非字符串名
            [{'name': 'BTCUSDT', 'risk_per_trade': 1.0}],           # 风险度 100% 超上限
            [{'name': 'BTCUSDT', 'risk_per_trade': -0.01}],         # 负风险度
            [{'name': 'BTCUSDT', 'risk_per_trade': 'inf'}],         # 非有限风险度
            [{'name': 'BTCUSDT', 'strategy': 'foobar'}],            # 非法策略名（不在白名单）
            [{'name': 'BTCUSDT', 'enabled': 'maybe'}],             # 非法布尔（歧义值拒绝）
            [{'name': 'BTCUSDT', 'strategy': 'ma_cross'},
             {'name': 'BTCUSDT', 'strategy': 'ma_cross'}],          # 重复交易对
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
            cfg['strategy']['ma_long_period'] = "28"
            cfg['strategy']['default_risk_per_trade'] = "0.01"
            cfg['trading']['symbols'] = [
                {'name': ' btcusdt ', 'risk_per_trade': "0.02", 'strategy': 'ma_cross', 'enabled': 'true'}]
            _jdump(cfg, path)
            with patch.object(main, 'OkxApi', _FakeOkxApi):
                system = TradingSystem(config_file=path)
            self.assertIsInstance(system.config['strategy']['ma_long_period'], int)
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
            cfg['trading']['symbols'] = [{'name': 'BTCUSDT', 'enabled': 'false', 'strategy': 'ma_cross'}]
            _jdump(cfg, path)
            with patch.object(main, 'OkxApi', _FakeOkxApi):
                system = TradingSystem(config_file=path)
            self.assertIs(system.config['trading']['symbols'][0]['enabled'], False)

    def test_fractional_period_rejected(self):
        """严格整数：小数周期（28.9 / "28.9"）拒绝而非静默截断为 28；
        inf/-inf/nan 走干净 ValueError（而非 OverflowError 崩溃/500）。"""
        for bad_period in (28.9, "28.9", "inf", "-inf", "nan"):
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_config(tmp)
                cfg = _jload(path)
                cfg['strategy']['ma_stop_period'] = bad_period
                _jdump(cfg, path)
                with patch.object(main, 'OkxApi', _FakeOkxApi):
                    with self.assertRaises(ValueError, msg=f"应以 ValueError 拒绝: {bad_period!r}"):
                        TradingSystem(config_file=path)

    def test_example_config_is_bootable(self):
        """config.example.json 填上凭据即可启动——保证示例配置永远与代码同步。"""
        with tempfile.TemporaryDirectory() as tmp:
            # config.example.json 在仓库根（tests/unit 的上两级）
            _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            example = _jload(os.path.join(_root, 'config.example.json'))
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
        """巡检间隔 ≥ 60 分钟必须通过启动校验（_validate_scheduler_config 放行 [1,1440]）。

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


if __name__ == '__main__':
    unittest.main()
