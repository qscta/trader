"""交易 runner 的跨入口运行时护栏。"""

import copy
import errno
import fcntl
import math
import os
import stat
from datetime import timedelta
from pathlib import Path


RUNNER_HEARTBEAT_MAX_AGE_SECONDS = 150
_SAFETY_BLOCKER_FIELDS = frozenset({
    'open_intents', 'close_intents',
    'position_quarantines', 'stop_residues',
})


def runtime_data_path(filename, environ=None, default_dir=None):
    """Resolve one runtime file from the deployment's canonical data root."""
    if (not isinstance(filename, str) or not filename or
            os.path.basename(filename) != filename):
        raise RuntimeError('runtime filename must be one plain path component')
    env = os.environ if environ is None else environ
    root = env.get('TRADING_DATA_DIR')
    if root is None:
        root = default_dir
    if (not isinstance(root, str) or not os.path.isabs(root) or
            os.path.normpath(root) != root):
        raise RuntimeError('TRADING_DATA_DIR must be a canonical absolute path')
    return os.path.join(root, filename)


def maintenance_sentinel_active(path):
    """Fail closed unless a canonical absolute sentinel path is absent."""
    if (not isinstance(path, str) or not os.path.isabs(path) or
            os.path.normpath(path) != path):
        return True
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def catchup_schedule_slot(config, now):
    """Single pure resolver for runner catch-up and pre-stop deployment gates."""
    sched = config.get('scheduler', {})
    check_hour = sched.get('check_hour', 8)
    check_minute = sched.get('check_minute', 0)
    scheduled_today = now.replace(
        hour=check_hour, minute=check_minute, second=0, microsecond=0)
    if now >= scheduled_today:
        slot = scheduled_today
    else:
        previous = scheduled_today - timedelta(days=1)
        if (previous + timedelta(minutes=2)).date() == previous.date():
            return None
        slot = previous
    if now < slot + timedelta(minutes=2):
        return None
    return slot


def assess_runtime_health(raw_snapshot, now):
    """纯函数：规范化运行时快照，并给出唯一的健康判定与问题清单。

    ``healthy`` 若存在，代表上游已报告的结论；这里仍会从原始事实重新判定，
    同时保留 API 原有的契约完整性检查。调用方必须显式传入 ``now``，避免测试
    与 Web 层各自隐藏一套时钟/超时口径。
    """
    if not isinstance(raw_snapshot, dict):
        raw_snapshot = {}

    issues = []
    missing = object()

    reported_healthy = raw_snapshot.get('healthy', missing)
    if reported_healthy is not missing:
        if not isinstance(reported_healthy, bool):
            issues.append('system_health_state_invalid')
        elif not reported_healthy:
            issues.append('system_reported_unhealthy')

    scheduler_running = raw_snapshot.get('scheduler_running')
    if not isinstance(scheduler_running, bool):
        issues.append('scheduler_running_invalid')
        scheduler_running = False
    scheduler_thread_alive = raw_snapshot.get('scheduler_thread_alive')
    if not isinstance(scheduler_thread_alive, bool):
        issues.append('scheduler_thread_state_invalid')
        scheduler_thread_alive = False
    if not scheduler_running:
        issues.append('scheduler_not_running')
    elif not scheduler_thread_alive:
        issues.append('scheduler_thread_stopped')

    heartbeat = raw_snapshot.get('runner_heartbeat_ts')
    heartbeat_age = None
    if heartbeat is not None and not isinstance(heartbeat, bool):
        try:
            heartbeat_value = float(heartbeat)
            now_value = float(now)
            heartbeat_age = now_value - heartbeat_value
            if (isinstance(now, bool) or
                    not math.isfinite(now_value) or
                    not math.isfinite(heartbeat_value) or
                    not math.isfinite(heartbeat_age) or heartbeat_age < 0):
                heartbeat_age = None
                issues.append('runner_heartbeat_invalid')
            elif heartbeat_age > RUNNER_HEARTBEAT_MAX_AGE_SECONDS:
                issues.append('runner_heartbeat_stale')
        except (TypeError, ValueError, OverflowError):
            issues.append('runner_heartbeat_invalid')
    else:
        issues.append(
            'runner_heartbeat_missing' if heartbeat is None
            else 'runner_heartbeat_invalid')

    stopping = raw_snapshot.get('stopping')
    if not isinstance(stopping, bool):
        issues.append('runner_stopping_state_invalid')
        stopping = True
    elif stopping:
        issues.append('runner_stopping')

    persistence_degraded = raw_snapshot.get('persistence_degraded')
    persistence_context = raw_snapshot.get('persistence_degraded_context')
    if not isinstance(persistence_degraded, bool):
        issues.append('runtime_persistence_state_invalid')
        persistence_degraded = True
    elif persistence_degraded:
        issues.append('runtime_persistence_degraded')
    if (persistence_context is not None and
            not isinstance(persistence_context, str)):
        issues.append('runtime_persistence_context_invalid')
        persistence_context = None

    safety_blockers = raw_snapshot.get('safety_blockers')
    if (not isinstance(safety_blockers, dict) or
            set(safety_blockers) != _SAFETY_BLOCKER_FIELDS or
            any(isinstance(value, bool) or not isinstance(value, int) or
                value < 0 for value in safety_blockers.values())):
        issues.append('safety_blocker_state_invalid')
        safety_blockers = None
    else:
        safety_blockers = copy.deepcopy(safety_blockers)
        if any(safety_blockers.values()):
            issues.append('safety_blockers_present')

    trade_check_failure = raw_snapshot.get('trade_check_failure', missing)
    if trade_check_failure is missing:
        issues.append('trade_check_state_missing')
        trade_check_failure = {'kind': 'unavailable'}
    elif trade_check_failure is not None:
        if not isinstance(trade_check_failure, dict):
            issues.append('trade_check_state_invalid')
            trade_check_failure = {'kind': 'invalid'}
        else:
            trade_check_failure = copy.deepcopy(trade_check_failure)
        issues.append('trade_check_failed')

    guardian_failure = raw_snapshot.get('guardian_failure', missing)
    if guardian_failure is missing:
        issues.append('guardian_state_missing')
        guardian_failure = {'kind': 'unavailable'}
    elif guardian_failure is not None:
        if not isinstance(guardian_failure, dict):
            issues.append('guardian_state_invalid')
            guardian_failure = {'kind': 'invalid'}
        else:
            guardian_failure = copy.deepcopy(guardian_failure)
    if guardian_failure is not None:
        issues.append('guardian_failed')

    daily_check_overdue = raw_snapshot.get('daily_check_overdue')
    if not isinstance(daily_check_overdue, bool):
        issues.append('daily_check_state_invalid')
        daily_check_overdue = True
    elif daily_check_overdue:
        issues.append('daily_check_overdue')
    expected_daily_check_date = raw_snapshot.get('expected_daily_check_date')
    if (expected_daily_check_date is not None and
            not isinstance(expected_daily_check_date, str)):
        issues.append('expected_daily_check_date_invalid')
        expected_daily_check_date = None

    return {
        'healthy': not issues,
        'scheduler_running': scheduler_running,
        'scheduler_thread_alive': scheduler_thread_alive,
        'runner_heartbeat_ts': heartbeat,
        'heartbeat_age_seconds': heartbeat_age,
        'persistence_degraded': persistence_degraded,
        'persistence_degraded_context': persistence_context,
        'safety_blockers': safety_blockers,
        'trade_check_failure': trade_check_failure,
        'last_successful_trade_check_ts': raw_snapshot.get(
            'last_successful_trade_check_ts'),
        'guardian_failure': guardian_failure,
        'last_successful_guardian_ts': raw_snapshot.get(
            'last_successful_guardian_ts'),
        'daily_check_overdue': daily_check_overdue,
        'expected_daily_check_date': expected_daily_check_date,
        'stopping': stopping,
        'issues': issues,
    }


class RunnerAlreadyActiveError(RuntimeError):
    """另一交易 runner 已持有项目锁。"""


_lock_fd = None
_lock_path = None


def runner_lock_path():
    override = os.environ.get('TRADING_RUNNER_LOCK_FILE')
    if override:
        return os.path.abspath(override)
    return str(Path(__file__).resolve().parent / '.runtime' / 'runner.lock')


def acquire_runner_lock():
    """获取并终身持有交易 runner 锁；同进程重复调用幂等。

    默认文件位于项目内权限为 0700 的运行目录，避免可预测 /tmp 路径被共享主机
    其他用户预占/替换。部署目录只读时可用 TRADING_RUNNER_LOCK_FILE 指向私有目录。
    """
    global _lock_fd, _lock_path
    if _lock_fd is not None:
        return _lock_path

    path = runner_lock_path()
    directory = os.path.dirname(path)
    # 只创建/使用专用 0700 目录，绝不对既有父目录盲 chmod。
    # 否则 TRADING_RUNNER_LOCK_FILE=/tmp/trader.lock 会把整个 /tmp 改成
    # 0700；服务以 root 运行时可令整机其它进程故障。
    if not os.path.lexists(directory):
        try:
            os.makedirs(directory, mode=0o700, exist_ok=False)
        except FileExistsError:
            # 与另一个启动进程竞态：交给下方 lstat 复核。
            pass

    try:
        directory_info = os.lstat(directory)
    except OSError as exc:
        raise RuntimeError(f'无法检查 runner 锁目录 {directory}: {exc}') from exc
    if not stat.S_ISDIR(directory_info.st_mode):
        raise RuntimeError(f'runner 锁目录不是真实目录（拒绝符号链接）: {directory}')
    if hasattr(os, 'geteuid') and directory_info.st_uid != os.geteuid():
        raise RuntimeError(f'runner 锁目录不属于当前用户: {directory}')
    directory_mode = stat.S_IMODE(directory_info.st_mode)
    if (directory_mode & 0o700) != 0o700 or directory_mode & 0o077:
        raise RuntimeError(
            f'runner 锁必须放在当前用户专用 0700 目录，'
            f'拒绝修改既有共享目录 {directory} '
            f'(当前权限 {directory_mode:04o})')

    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise RuntimeError(f'runner 锁文件拒绝符号链接: {path}') from exc
        raise RuntimeError(f'无法打开 runner 锁文件 {path}: {exc}') from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f'runner 锁不是普通文件: {path}')
        if hasattr(os, 'geteuid') and info.st_uid != os.geteuid():
            raise RuntimeError(f'runner 锁不属于当前用户: {path}')
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunnerAlreadyActiveError(
                f'已有交易 runner 持锁: {path}；禁止重复启动 main/api_server/gunicorn'
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f'{os.getpid()}\n'.encode('ascii'))
        os.fsync(fd)
    except Exception:
        os.close(fd)
        raise

    _lock_fd = fd
    _lock_path = path
    return path
