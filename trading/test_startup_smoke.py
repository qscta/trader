"""启动装配链路冒烟测试（本机可运行）。

其余测试均用 TradingSystem.__new__ 绕过构造——本文件专门端到端验证 __init__ 全链：
load_config（含旧格式拍平/环境变量/凭据校验）→ 状态迁移 → 归属护栏 → 策略构建 →
TradeState/EquityTracker 装配 → 权益获取 → RiskManager → 启动持仓同步 → 定时任务注册。
"""
import json
import os
import sys
import tempfile
import unittest
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
        'strategy': {'channel_period': 28, 'ma_short_period': 6, 'ma_long_period': 28,
                     'ma_stop_period': 28, 'default_risk_per_trade': 0.01},
        'trading': {'symbols': [{'name': 'BTCUSDT', 'enabled': True, 'strategy': 'turtle'}]},
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

    def test_register_jobs_smoke(self):
        """定时任务注册链路可空转（调度器为桩）。"""
        with tempfile.TemporaryDirectory() as tmp:
            system = self._boot(tmp)
            system.register_jobs(system.config.get('scheduler', {}))  # 不应抛异常

    def test_legacy_nested_config_flattened(self):
        """旧多所格式 {exchanges:{okx:...}} 自动拍平后可正常启动。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {'exchanges': {'okx': {'label': '欧易', 'apiKey': 'k', 'secret': 's', 'password': 'p',
                                          'strategy': {'channel_period': 28, 'default_risk_per_trade': 0.01},
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


if __name__ == '__main__':
    unittest.main()
