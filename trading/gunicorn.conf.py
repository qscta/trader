"""交易管理台的安全 Gunicorn 基线；从 trading 目录用 -c 加载。"""

import os


bind = os.environ.get('TRADING_BIND', '127.0.0.1:5000')
workers = 1
# sync worker 的静默超时会在外部 API 长调用期间误杀承载交易 daemon 线程的 worker。
# gthread worker 会持续向 master 报活，同时给 Web 与交易线程留出独立执行线程。
worker_class = 'gthread'
threads = int(os.environ.get('TRADING_WEB_THREADS', '4'))
timeout = int(os.environ.get('TRADING_GUNICORN_TIMEOUT', '120'))
graceful_timeout = int(os.environ.get('TRADING_GUNICORN_GRACEFUL_TIMEOUT', '900'))
keepalive = 5
preload_app = False

if threads < 2:
    raise RuntimeError('TRADING_WEB_THREADS 必须 >= 2')
if timeout < 60 or graceful_timeout < 120:
    raise RuntimeError('Gunicorn timeout 必须 >= 60 秒，graceful_timeout 必须 >= 120 秒')


def _graceful_stop_runner(worker):
    try:
        from api_server import stop_runner_thread
        ok = stop_runner_thread(timeout=max(1, graceful_timeout - 5))
        if not ok:
            worker.log.critical('交易 runner 未能在 graceful_timeout 内停止')
    except Exception:
        worker.log.exception('优雅停止交易 runner 失败')


def worker_int(worker):
    _graceful_stop_runner(worker)


def worker_exit(server, worker):
    _graceful_stop_runner(worker)
