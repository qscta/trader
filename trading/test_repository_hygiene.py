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
    def test_classifier_covers_runtime_backups_and_daily_equity(self):
        for path in (
                'trading/daily_equity.json',
                'trading/config.json.bak',
                'trading/trade_state.json.bak.empty.20260711',
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
