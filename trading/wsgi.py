import hashlib
import os
import threading
import fcntl
import api_server as srv
from api_server import app, logger, _bootstrap

_lock_fp = None
# 锁文件按项目目录派生：同一目录的多 worker 仍互斥（防重复初始化交易系统），
# 而同机部署的另一套独立目录实例（如迁移期新旧并存）不会被误伤拒启
_LOCK_FILE = '/tmp/trading_system_runner.{}.lock'.format(
    hashlib.sha1(os.path.dirname(os.path.abspath(__file__)).encode('utf-8')).hexdigest()[:12])


def _try_start_runner_once():
    global _lock_fp
    # 先抢锁、再初始化：避免 gunicorn 多 worker 时每个 worker 都初始化管理器，
    # 重复连接交易所并执行 sync_positions（真钱风险）。本系统必须以 -w 1 单 worker 运行。
    try:
        _lock_fp = open(_LOCK_FILE, 'w')
        fcntl.flock(_lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        logger.critical('WSGI: 已有实例持锁，本 worker 启动失败并退出。'
                        '本系统必须以单 worker 运行（gunicorn -w 1）！')
        # 不留「半可用」worker（manager 未初始化会 503）：直接让本 worker 启动失败
        raise RuntimeError('检测到多 worker：本系统必须以 gunicorn -w 1 单 worker 运行')

    _bootstrap()
    t = threading.Thread(target=srv.trading_system.start, daemon=True)
    t.start()
    logger.info('WSGI 模式: 欧易交易线程已启动')


_try_start_runner_once()
application = app
