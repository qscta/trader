import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MemMonitorLoggingTests(unittest.TestCase):
    def test_environment_log_path_is_used(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / 'mem_monitor.log'
            env = os.environ.copy()
            env['TRADING_MEM_MONITOR_LOG'] = str(log_path)

            result = subprocess.run(
                [
                    sys.executable,
                    '-c',
                    'import mem_monitor; print(mem_monitor.LOG_FILE)',
                ],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(log_path))
            self.assertTrue(log_path.is_file())

    def test_missing_webhook_exits_with_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / 'mem_monitor.log'
            env = os.environ.copy()
            env.pop('DINGTALK_WEBHOOK', None)
            env['TRADING_MEM_MONITOR_LOG'] = str(log_path)

            result = subprocess.run(
                [
                    sys.executable,
                    '-c',
                    ('import mem_monitor; '
                     'mem_monitor.load_webhook = lambda: None; '
                     'mem_monitor.main()'),
                ],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 1, result.stderr)


if __name__ == '__main__':
    unittest.main()
