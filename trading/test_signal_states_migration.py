"""海龟死字段账本迁移测试（mid_line_crossed / signal_execution 一次性清除）。"""

import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import migrate_signal_states as migration


def _polluted_ledger():
    return {
        'open_positions': {},
        'closed_trades': [],
        'signal_states': {
            'BTCUSDT': {
                'last_processed_candle': '2026-07-15',
                'strategy': 'turtle',
                'last_update': '2026-07-15T08:00:00',
                'mid_line_crossed': True,
                'signal_execution': {
                    'strategy': 'turtle', 'signal_id': 'candle|long',
                    'client_order_id': 'T1', 'status': 'pending',
                },
            },
            'ETHUSDT': {  # 已经干净的记录：一个字段都不该动
                'last_processed_candle': '2026-07-15',
                'strategy': 'ma_cross',
                'last_update': '2026-07-15T08:00:00',
            },
        },
        'open_intents': {},
        'stop_loss_dates': {},
        'position_quarantines': {},
    }


def _write(path, payload):
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle)


def _read(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


class StripDeadFieldsTest(unittest.TestCase):
    def test_removes_dead_fields_and_reports(self):
        cleaned, report = migration.strip_dead_fields(_polluted_ledger())
        btc = cleaned['signal_states']['BTCUSDT']
        self.assertNotIn('mid_line_crossed', btc)
        self.assertNotIn('signal_execution', btc)
        # live 字段逐一保留
        self.assertEqual(btc['last_processed_candle'], '2026-07-15')
        self.assertEqual(btc['strategy'], 'turtle')
        self.assertEqual(btc['last_update'], '2026-07-15T08:00:00')
        self.assertEqual(
            report,
            {'BTCUSDT': ['mid_line_crossed', 'signal_execution']})

    def test_does_not_mutate_input(self):
        original = _polluted_ledger()
        snapshot = copy.deepcopy(original)
        migration.strip_dead_fields(original)
        self.assertEqual(original, snapshot)

    def test_clean_ledger_reports_nothing(self):
        clean = {'signal_states': {'ETHUSDT': {'strategy': 'ma_cross'}}}
        _, report = migration.strip_dead_fields(clean)
        self.assertEqual(report, {})

    def test_non_dict_record_left_alone(self):
        weird = {'signal_states': {'X': ['not', 'a', 'dict']}}
        cleaned, report = migration.strip_dead_fields(weird)
        self.assertEqual(cleaned['signal_states']['X'], ['not', 'a', 'dict'])
        self.assertEqual(report, {})

    def test_missing_signal_states_is_noop(self):
        cleaned, report = migration.strip_dead_fields({'open_positions': {}})
        self.assertEqual(report, {})
        self.assertEqual(cleaned, {'open_positions': {}})


class RunTest(unittest.TestCase):
    def test_dry_run_does_not_modify(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            _write(path, _polluted_ledger())
            self.assertEqual(0, migration.run(tmp, apply=False))
            self.assertEqual(_read(path), _polluted_ledger())  # 原样
            self.assertEqual(
                [], [n for n in os.listdir(tmp) if '.premigrate.' in n])

    def test_apply_cleans_backs_up_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            _write(path, _polluted_ledger())
            self.assertEqual(0, migration.run(tmp, apply=True))

            cleaned = _read(path)
            btc = cleaned['signal_states']['BTCUSDT']
            self.assertNotIn('mid_line_crossed', btc)
            self.assertNotIn('signal_execution', btc)
            self.assertEqual(btc['strategy'], 'turtle')  # 标签留痕不改写
            self.assertEqual(  # 干净记录一字未动
                cleaned['signal_states']['ETHUSDT'],
                _polluted_ledger()['signal_states']['ETHUSDT'])

            backups = [n for n in os.listdir(tmp) if '.premigrate.' in n]
            self.assertEqual(len(backups), 1)
            self.assertEqual(_read(os.path.join(tmp, backups[0])),
                             _polluted_ledger())  # 备份是原始污染态

            # 幂等：再跑一次无改动、不产生新备份
            self.assertEqual(0, migration.run(tmp, apply=True))
            self.assertEqual(
                1, len([n for n in os.listdir(tmp) if '.premigrate.' in n]))

    def test_clean_ledger_not_written_or_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            clean = {
                'open_positions': {}, 'closed_trades': [],
                'signal_states': {'ETHUSDT': {'strategy': 'ma_cross'}},
            }
            _write(path, clean)
            self.assertEqual(0, migration.run(tmp, apply=True))
            self.assertEqual(_read(path), clean)
            self.assertEqual(
                [], [n for n in os.listdir(tmp) if '.premigrate.' in n])

    def test_bak_file_cleaned_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            bak = path + '.bak'
            _write(path, _polluted_ledger())
            _write(bak, _polluted_ledger())
            self.assertEqual(0, migration.run(tmp, apply=True))
            for target in (path, bak):
                btc = _read(target)['signal_states']['BTCUSDT']
                self.assertNotIn('mid_line_crossed', btc)
                self.assertNotIn('signal_execution', btc)
            # 主文件与 .bak 各自留一份备份
            self.assertEqual(
                2, len([n for n in os.listdir(tmp) if '.premigrate.' in n]))

    def test_no_ledger_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(0, migration.run(tmp, apply=True))
            self.assertEqual([], os.listdir(tmp))


if __name__ == '__main__':
    unittest.main()
