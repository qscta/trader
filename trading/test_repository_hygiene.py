"""阻止凭据、账本和运行备份再次进入 Git 当前树。"""

import subprocess
import unittest
from pathlib import Path


SENSITIVE_RUNTIME_PATHS = {
    'trading/config.json',
    'trading/trade_state.json',
    'trading/closed_trades_archive.json',
    'trading/stop_loss_dates.json',
    'trading/daily_equity.json',
    'trading/equity_history.json',
    'trading/equity_ticks.json',
    'trading/peak_equity.json',
    'trading/qiusuo_index.json',
}


def _is_forbidden_tracked_path(path):
    name = Path(path).name
    if (path.startswith('trading/closed_trades_archive_') and
            (path.endswith('.json') or '.json.bak' in path)):
        return True
    if path in SENSITIVE_RUNTIME_PATHS:
        return True
    # 原子状态保留 .bak / .bak.*；它们与主文件同样包含真实
    # 仓位/凭据。.gitignore 可被 git add -f 绕过，CI 必须再挡一层。
    if any(path.startswith(sensitive + '.bak') for sensitive in SENSITIVE_RUNTIME_PATHS):
        return True
    if path.startswith(('trading/backups/', 'trading/data/')):
        return True
    if name == '.env' or (name.startswith('.env.') and name != '.env.example'):
        return True
    return name.endswith(('.save', '.tgz', '.tar', '.tar.gz', '.zip'))


class RepositoryHygieneTest(unittest.TestCase):
    def test_resource_monitor_unit_uses_production_runtime(self):
        root = Path(__file__).resolve().parents[1]
        unit = (root / 'trading' / 'systemd' /
                'trading-mem-monitor.service').read_text(encoding='utf-8')
        for required in (
                'User=ubuntu',
                '/home/ubuntu/trader/trading/.venv/bin/python mem_monitor.py',
                'EnvironmentFile=-/etc/trading-mem-monitor.env',
                'Restart=on-failure', 'StartLimitBurst=3', 'UMask=0077'):
            self.assertIn(required, unit)
        # 最小权限：监控进程只需 webhook（从 config.json 回退取得），绝不通过
        # EnvironmentFile 加载含 OKX 密钥/登录口令/FLASK_SECRET_KEY 的整份 trading.env。
        # 只检查生效指令行（忽略注释），注释里出于说明目的提及该路径是允许的。
        directives = [
            line.strip() for line in unit.splitlines()
            if line.strip() and not line.strip().startswith('#')]
        self.assertFalse(
            any(line.startswith('EnvironmentFile=') and 'trading.env' in line
                for line in directives),
            '监控单元不得通过 EnvironmentFile 加载整份 trading.env')

    def test_classifier_covers_runtime_backups_and_daily_equity(self):
        for path in (
                'trading/daily_equity.json',
                'trading/config.json.bak',
                'trading/trade_state.json.bak.empty.20260711',
                'trading/closed_trades_archive_2026.json',
                'trading/closed_trades_archive_2026.json.bak',
                'trading/qiusuo_index.json.bak'):
            self.assertTrue(_is_forbidden_tracked_path(path), path)
        self.assertFalse(_is_forbidden_tracked_path('trading/config.example.json'))
        self.assertFalse(_is_forbidden_tracked_path('.env.example'))

    def test_runtime_secrets_and_archives_are_not_tracked(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ['git', 'ls-files', '-z'],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
        )
        tracked = [item for item in result.stdout.decode('utf-8').split('\0') if item]
        violations = [path for path in tracked if _is_forbidden_tracked_path(path)]

        self.assertEqual(
            violations,
            [],
            '检测到不应进入 Git 的凭据/状态/备份文件: ' + ', '.join(violations),
        )


if __name__ == '__main__':
    unittest.main()
