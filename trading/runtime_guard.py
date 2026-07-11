"""交易 runner 的跨入口、跨进程单实例锁。"""

import errno
import fcntl
import os
import stat
from pathlib import Path


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
