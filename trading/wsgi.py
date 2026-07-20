import os

from gunicorn.arbiter import Arbiter

import api_server as srv
from api_server import app, logger, _bootstrap, start_runner_thread
from runtime_guard import acquire_runner_lock

_lock_path = None


def _halt_gunicorn_master(_standalone_exit_code):
    """用 Gunicorn 的保留退出码结束 worker，并让 master 一并失败退出。"""
    os._exit(Arbiter.WORKER_BOOT_ERROR)


def _try_start_runner_once():
    global _lock_path
    # 先抢锁、再初始化：避免 gunicorn 多 worker 时每个 worker 都初始化管理器，
    # 重复连接交易所并执行 sync_positions（真钱风险）。本系统必须以 -w 1 单 worker 运行。
    try:
        _lock_path = acquire_runner_lock()
    except Exception:
        logger.critical('WSGI: 已有实例持锁，本 worker 启动失败并退出。'
                        '本系统必须使用 gunicorn.conf.py 的单 worker 配置！')
        # 不留「半可用」worker（manager 未初始化会 503）：直接让本 worker 启动失败
        raise RuntimeError('检测到重复 runner：请使用 gunicorn -c gunicorn.conf.py')

    _bootstrap()
    start_runner_thread(
        srv.trading_system, fatal_exit=_halt_gunicorn_master)
    logger.info(f'WSGI 模式: 欧易交易线程已启动（runner lock: {_lock_path}）')


_try_start_runner_once()
application = app
