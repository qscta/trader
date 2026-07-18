#!/usr/bin/env python3
"""OKX deployment no-open gate.

The gate is deliberately independent from the trading adapter.  It only uses
authenticated/public GET endpoints and the same runner lock and sentinel that
the production runner consumes.  Its durable state machine is::

    ABSENT -> ARMED -> BASELINED -> SEALED -> COMMITTED

``SEALED`` retains the sentinel. ``commit`` alone removes it, and only after
proving the formal runner holds the exact lock. Baseline/completion remain so a
later ``arm`` can distinguish a committed cycle from an unproved one.
"""

import argparse
import contextlib
import copy
import errno
import fcntl
import hashlib
import importlib
import json
import os
import re
import secrets
import stat
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

import config_validation as cfgv
from runtime_guard import catchup_schedule_slot


EXPECTED_CCXT_VERSION = '4.5.64'
SCHEMA_VERSION = 2
SENTINEL_SCHEMA_VERSION = 1
COMPLETION_SCHEMA_VERSION = 2
SENTINEL_NAME = '.maintenance_no_open'
BASELINE_NAME = 'deployment_no_open_baseline.json'
COMPLETION_NAME = 'deployment_no_open_completion.json'
PAGE_LIMIT = 100
MAX_PAGES = 100
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_PROOF_WINDOW_MS = 2 * 60 * 60 * 1000
# OKX 对已撤未成交普通单只保证两小时可查询。门禁不能在 1:59:59
# 才开始最后一批请求；预留五分钟给末轮历史、快照和本地完成证据落盘。
PROOF_COMPLETION_SAFETY_MS = 5 * 60 * 1000
RELEASE_SHA_RE = re.compile(r'^[0-9a-f]{40}$')
NONCE_RE = re.compile(r'^[0-9a-f]{64}$')
SWAP_RE = re.compile(r'^[A-Z0-9]+-(?:USD|USDT|USDC)-SWAP$')

# Complete OKX V5 algo-pending/algo-history ordType surface for SWAP.
ALGO_ORDER_TYPES = (
    'conditional', 'oco', 'trigger', 'move_order_stop',
    'iceberg', 'twap', 'chase', 'smart_iceberg',
)
ALGO_HISTORY_STATES = ('effective', 'canceled', 'order_failed')


class GateError(RuntimeError):
    """The requested transition cannot prove that no position was opened."""


def _current_uid():
    return os.geteuid() if hasattr(os, 'geteuid') else os.getuid()


def _reject_nonfinite_json(value):
    raise GateError(f'JSON 含非标准数值常量 {value!r}')


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GateError(f'JSON 含重复字段 {key!r}')
        result[key] = value
    return result


def _loads_strict(text, context):
    try:
        return json.loads(
            text,
            parse_constant=_reject_nonfinite_json,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except GateError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GateError(f'{context} 不是合法 JSON') from exc


def _dumps_strict(payload):
    try:
        return (json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ) + '\n').encode('utf-8')
    except (TypeError, ValueError, OverflowError) as exc:
        raise GateError('门禁证据无法编码为有限 JSON') from exc


def _require_release_sha(release_sha):
    if not isinstance(release_sha, str) or not RELEASE_SHA_RE.fullmatch(release_sha):
        raise GateError('release SHA 必须是 40 位小写十六进制提交')
    return release_sha


def _validate_regular(info, context, mode=0o600):
    if not stat.S_ISREG(info.st_mode):
        raise GateError(f'{context} 必须是普通文件（拒绝符号链接）')
    if info.st_uid != _current_uid():
        raise GateError(f'{context} 不属于当前用户')
    actual_mode = stat.S_IMODE(info.st_mode)
    if actual_mode != mode:
        raise GateError(f'{context} 权限必须为 {mode:04o}（当前 {actual_mode:04o}）')
    if info.st_nlink != 1:
        raise GateError(f'{context} 必须只有一个硬链接')


def _validate_directory(info, context, exact_mode=None):
    if not stat.S_ISDIR(info.st_mode):
        raise GateError(f'{context} 必须是真实目录（拒绝符号链接）')
    if info.st_uid != _current_uid():
        raise GateError(f'{context} 不属于当前用户')
    mode = stat.S_IMODE(info.st_mode)
    if exact_mode is not None:
        if mode != exact_mode:
            raise GateError(f'{context} 权限必须精确为 {exact_mode:04o}')
    elif (mode & 0o700) != 0o700 or mode & 0o022:
        raise GateError(
            f'{context} 必须由当前用户可读写执行，且组/其他用户不可写'
            f'（当前 {mode:04o}）')


def _identity(info):
    return {'dev': info.st_dev, 'ino': info.st_ino}


def _same_identity(left, right):
    return left['dev'] == right['dev'] and left['ino'] == right['ino']


@contextlib.contextmanager
def _open_data_directory(data_dir):
    if not isinstance(data_dir, str) or not os.path.isabs(data_dir):
        raise GateError('--data-dir 必须是绝对路径')
    if (os.path.normpath(data_dir) != data_dir or
            os.path.realpath(data_dir) != data_dir):
        raise GateError('--data-dir 必须是无别名、无符号链接的 canonical 绝对路径')
    try:
        before = os.lstat(data_dir)
        _validate_directory(before, 'data-dir')
        flags = (os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) |
                 getattr(os, 'O_NOFOLLOW', 0))
        fd = os.open(data_dir, flags)
    except GateError:
        raise
    except OSError as exc:
        raise GateError(f'无法安全打开 data-dir（{exc.__class__.__name__}）') from exc
    try:
        after = os.fstat(fd)
        _validate_directory(after, 'data-dir')
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise GateError('data-dir 在检查与打开之间被替换')
        yield fd
        # ``dir_fd`` remains valid after rename, which is dangerous here: a
        # detached release could receive a sentinel while the canonical path
        # points at a fresh unprotected tree.  A command may return success only
        # while its absolute path still resolves to the held inode.
        _recheck_data_directory_path(data_dir, fd)
    finally:
        os.close(fd)


def _recheck_data_directory_path(data_dir, held_fd):
    current_fd = None
    try:
        before = os.lstat(data_dir)
        _validate_directory(before, 'canonical data-dir')
        current_fd = os.open(
            data_dir,
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) |
            getattr(os, 'O_NOFOLLOW', 0),
        )
        current = os.fstat(current_fd)
        held = os.fstat(held_fd)
        _validate_directory(current, 'canonical data-dir')
        _validate_directory(held, 'held data-dir')
        if (before.st_dev, before.st_ino) != (
                current.st_dev, current.st_ino):
            raise GateError('canonical data-dir 在复核打开期间被替换')
        if (held.st_dev, held.st_ino) != (
                current.st_dev, current.st_ino):
            raise GateError('canonical data-dir 已被替换或重命名')
    except GateError:
        raise
    except OSError as exc:
        raise GateError(
            'canonical data-dir 无法复核（可能已被替换或重命名）') from exc
    finally:
        if current_fd is not None:
            os.close(current_fd)


def _open_verified_entry(dir_fd, name, context):
    try:
        before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise GateError(f'无法检查{context}（{exc.__class__.__name__}）') from exc
    _validate_regular(before, context)
    try:
        fd = os.open(
            name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=dir_fd)
    except OSError as exc:
        raise GateError(f'无法安全打开{context}（{exc.__class__.__name__}）') from exc
    try:
        after = os.fstat(fd)
        _validate_regular(after, context)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise GateError(f'{context} 在检查与打开之间被替换')
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_optional_entry(dir_fd, name, context):
    try:
        return _open_verified_entry(dir_fd, name, context)
    except FileNotFoundError:
        return None


def _recheck_held_entry(dir_fd, name, fd, context):
    held = os.fstat(fd)
    _validate_regular(held, context)
    try:
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        raise GateError(f'{context} 在门禁运行期间消失') from exc
    _validate_regular(current, context)
    if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
        raise GateError(f'{context} 在门禁运行期间被替换')


def _unlink_held_entry(dir_fd, name, fd, context):
    _recheck_held_entry(dir_fd, name, fd, context)
    try:
        os.unlink(name, dir_fd=dir_fd)
        # If unlink succeeded but fsync fails, durable completion plus the
        # root COMMIT_READY journal still distinguishes the committed state.
        os.fsync(dir_fd)
    except OSError as exc:
        raise GateError(f'无法安全删除{context}（{exc.__class__.__name__}）') from exc


def _make_held_evidence_durable(dir_fd, name, fd, context):
    """Retry durability after a prior file/directory fsync may have failed."""
    _recheck_held_entry(dir_fd, name, fd, context)
    try:
        os.fsync(fd)
        os.fsync(dir_fd)
    except OSError as exc:
        raise GateError(f'无法确认{context}已持久化（{exc.__class__.__name__}）') from exc
    _recheck_held_entry(dir_fd, name, fd, context)


def _read_fd_text(fd, context):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise GateError(f'无法定位{context}') from exc
    chunks = []
    total = 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_JSON_BYTES:
            raise GateError(f'{context} 超过安全大小上限')
        chunks.append(chunk)
    try:
        return b''.join(chunks).decode('utf-8')
    except UnicodeDecodeError as exc:
        raise GateError(f'{context} 不是 UTF-8') from exc


def _read_json_fd(fd, context):
    return _loads_strict(_read_fd_text(fd, context), context)


def _read_private_json_path(path, context):
    if not isinstance(path, str) or not os.path.isabs(path):
        raise GateError(f'{context}路径必须是绝对路径')
    try:
        before = os.lstat(path)
        _validate_regular(before, context)
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except GateError:
        raise
    except OSError as exc:
        raise GateError(f'无法安全读取{context}（{exc.__class__.__name__}）') from exc
    try:
        after = os.fstat(fd)
        _validate_regular(after, context)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise GateError(f'{context}在检查与打开之间被替换')
        return _read_json_fd(fd, context)
    finally:
        os.close(fd)


def _write_all(fd, payload):
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise GateError('门禁证据写入未前进')
        offset += written


def _create_json_exclusive(dir_fd, name, payload, context):
    """Write-once evidence; never replace or truncate an existing path."""
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
             getattr(os, 'O_NOFOLLOW', 0))
    fd = None
    try:
        fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
        os.fchmod(fd, 0o600)
        _write_all(fd, _dumps_strict(payload))
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.fsync(dir_fd)
        verify_fd = _open_verified_entry(dir_fd, name, context)
        return verify_fd
    except FileExistsError as exc:
        raise GateError(f'{context} 已存在；禁止覆盖权威证据') from exc
    except GateError:
        raise
    except OSError as exc:
        raise GateError(f'无法持久化{context}（{exc.__class__.__name__}）') from exc
    finally:
        if fd is not None:
            os.close(fd)


def _expected_runner_lock_path(data_dir, environ=None):
    env = os.environ if environ is None else environ
    expected = os.path.join(os.path.realpath(data_dir), '.runtime', 'runner.lock')
    override = env.get('TRADING_RUNNER_LOCK_FILE')
    if override:
        if override != expected:
            raise GateError(
                'TRADING_RUNNER_LOCK_FILE 必须精确等于 data-dir/.runtime/runner.lock')
    else:
        module_default = os.path.join(
            os.path.realpath(os.path.dirname(__file__)), '.runtime', 'runner.lock')
        if module_default != expected:
            raise GateError(
                'data-dir 不是门禁脚本目录时必须显式设置精确的 '
                'TRADING_RUNNER_LOCK_FILE')
    return expected


@contextlib.contextmanager
def _hold_runner_lock(data_dir_fd, data_dir, environ=None, exclusive=True):
    """Open the actual runner lock, optionally proving the runner is stopped."""
    _expected_runner_lock_path(data_dir, environ=environ)
    runtime_name = '.runtime'
    try:
        before_dir = os.stat(
            runtime_name, dir_fd=data_dir_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise GateError('缺少 data-dir/.runtime，无法验证实际 runner 锁') from exc
    except OSError as exc:
        raise GateError('无法安全检查 runner 锁目录') from exc
    _validate_directory(before_dir, 'runner 锁目录', exact_mode=0o700)
    try:
        runtime_fd = os.open(
            runtime_name,
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) |
            getattr(os, 'O_NOFOLLOW', 0),
            dir_fd=data_dir_fd,
        )
    except OSError as exc:
        raise GateError('无法安全打开 runner 锁目录') from exc
    lock_fd = None
    locked = False
    try:
        after_dir = os.fstat(runtime_fd)
        _validate_directory(after_dir, 'runner 锁目录', exact_mode=0o700)
        if (before_dir.st_dev, before_dir.st_ino) != (
                after_dir.st_dev, after_dir.st_ino):
            raise GateError('runner 锁目录在检查与打开之间被替换')
        try:
            lock_fd = _open_verified_entry(runtime_fd, 'runner.lock', 'runner 锁')
        except FileNotFoundError as exc:
            raise GateError('实际 runner 锁文件不存在') from exc
        if exclusive:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (BlockingIOError, OSError) as exc:
                if isinstance(exc, BlockingIOError) or exc.errno in {
                        errno.EACCES, errno.EAGAIN}:
                    raise GateError('runner 仍持锁（服务未停），门禁状态不可变更') from exc
                raise GateError('无法获取实际 runner 锁') from exc
        yield lock_fd, _identity(os.fstat(lock_fd))
        _recheck_held_entry(runtime_fd, 'runner.lock', lock_fd, 'runner 锁')
        current_dir = os.stat(
            runtime_name, dir_fd=data_dir_fd, follow_symlinks=False)
        _validate_directory(current_dir, 'runner 锁目录', exact_mode=0o700)
        if (after_dir.st_dev, after_dir.st_ino) != (
                current_dir.st_dev, current_dir.st_ino):
            raise GateError('门禁期间 runner 锁目录被替换')
    finally:
        if lock_fd is not None:
            try:
                if locked:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(runtime_fd)


def _require_runner_lock_held_elsewhere(lock_fd):
    """Prove the live runner holds this exact inode before commit."""
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        if isinstance(exc, BlockingIOError) or exc.errno in {
                errno.EACCES, errno.EAGAIN}:
            return
        raise GateError('无法复验正式 runner 锁') from exc
    else:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        raise GateError('commit 前正式 runner 未持有精确 runner 锁')


def _require_exact_fields(value, expected, context):
    if not isinstance(value, dict):
        raise GateError(f'{context} 必须是对象')
    if set(value) != set(expected):
        raise GateError(f'{context} schema 字段不精确')


def _validate_file_identity(value, context):
    _require_exact_fields(value, {'dev', 'ino'}, context)
    for key in ('dev', 'ino'):
        if isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] <= 0:
            raise GateError(f'{context}.{key} 非法')


def _validate_sentinel(payload):
    _require_exact_fields(
        payload, {'schema_version', 'nonce', 'release_sha'}, 'sentinel')
    if payload['schema_version'] != SENTINEL_SCHEMA_VERSION:
        raise GateError('sentinel schema_version 不兼容')
    if not isinstance(payload['nonce'], str) or not NONCE_RE.fullmatch(payload['nonce']):
        raise GateError('sentinel nonce 非法')
    _require_release_sha(payload['release_sha'])
    return payload


def _read_sentinel_fd(fd):
    return _validate_sentinel(_read_json_fd(fd, '部署禁开仓哨兵'))


@contextlib.contextmanager
def _hold_sentinel(dir_fd):
    try:
        fd = _open_verified_entry(dir_fd, SENTINEL_NAME, '部署禁开仓哨兵')
    except FileNotFoundError as exc:
        raise GateError(f'缺少 {SENTINEL_NAME}；禁止绕过禁开仓状态') from exc
    try:
        payload = _read_sentinel_fd(fd)
        yield fd, payload, _identity(os.fstat(fd))
        _recheck_held_entry(dir_fd, SENTINEL_NAME, fd, '部署禁开仓哨兵')
    finally:
        os.close(fd)


def _credentials_overlay(okx, environ):
    try:
        passphrase = cfgv.resolve_optional_alias(
            environ.get('OKX_API_PASSPHRASE'),
            environ.get('OKX_PASSWORD'),
            'OKX API passphrase 环境变量')
    except ValueError as exc:
        raise GateError(str(exc)) from exc
    overlay = {
        'apiKey': environ.get('OKX_API_KEY'),
        'secret': environ.get('OKX_API_SECRET'),
        'password': passphrase,
    }
    for key, value in overlay.items():
        if value:
            okx[key] = value
    for key in ('apiKey', 'secret', 'password'):
        value = okx.get(key)
        if not isinstance(value, str) or not value or value != value.strip():
            raise GateError(f'缺少或非法的 OKX {key} 凭据')


def load_okx_config(config_path, environ=None):
    config = _read_private_json_path(config_path, 'config.json')
    if not isinstance(config, dict):
        raise GateError('config.json 顶层必须是对象')
    config = copy.deepcopy(config)
    try:
        cfgv.canonicalize_single_okx_config(config)
    except (TypeError, ValueError) as exc:
        raise GateError(f'config.json 严格校验失败: {exc}') from exc
    okx = config.setdefault('okx', {})
    if not isinstance(okx, dict):
        raise GateError('config.okx 必须是对象')
    _credentials_overlay(okx, os.environ if environ is None else environ)
    config.setdefault('strategy', {})
    config.setdefault('trading', {'symbols': []})
    if isinstance(config['trading'], dict):
        config['trading'].setdefault('symbols', [])
    config.setdefault('scheduler', {})
    try:
        cfgv.validate_and_normalize_execution_config(config)
    except (TypeError, ValueError) as exc:
        raise GateError(f'config.json 严格校验失败: {exc}') from exc
    return copy.deepcopy(config['okx'])


def require_completed_schedule_slot(config_path, data_dir, now=None):
    """Return the current deployable slot only after the old runner completed it.

    This is intentionally read-only and runs before the deployment installs a
    start block or stops any production unit.  The scheduler's own slot
    resolver remains the single source of date/buffer semantics.
    """
    _require_config_in_data_dir(config_path, data_dir)
    config = _read_private_json_path(config_path, 'config.json')
    state_path = os.path.join(data_dir, 'trade_state.json')
    state = _read_private_json_path(state_path, 'trade_state.json')
    if not isinstance(config, dict) or not isinstance(state, dict):
        raise GateError('部署前配置与账本必须是 JSON 对象')
    try:
        slot = catchup_schedule_slot(
            config, datetime.now() if now is None else now)
    except Exception as exc:
        raise GateError('无法按正式 scheduler 语义计算部署调度槽') from exc
    if slot is None:
        raise GateError('当前调度槽尚未结束宽限期；生产必须保持运行，拒绝停机部署')
    expected = slot.date().isoformat()
    if state.get('last_daily_check_date') != expected:
        raise GateError(
            f'当前调度日 {expected} 尚未成功完成；生产必须保持运行，拒绝停机部署')
    return expected


def _account_fingerprint(okx):
    return hashlib.sha256(okx['apiKey'].encode('utf-8')).hexdigest()


def _account_domain(okx):
    return 'demo' if okx.get('sandbox', False) or okx.get('demo', False) else 'live'


def create_read_only_exchange(okx, ccxt_module=None):
    if ccxt_module is None:
        try:
            ccxt_module = importlib.import_module('ccxt')
        except ImportError as exc:
            raise GateError('缺少锁定依赖 ccxt==4.5.64') from exc
    if getattr(ccxt_module, '__version__', None) != EXPECTED_CCXT_VERSION:
        raise GateError(f'ccxt 版本必须是锁定值 {EXPECTED_CCXT_VERSION}')
    factory = getattr(ccxt_module, 'okx', None)
    if not callable(factory):
        raise GateError('锁定 ccxt 不含 okx 构造器')
    exchange = factory({
        'apiKey': okx['apiKey'],
        'secret': okx['secret'],
        'password': okx['password'],
        'enableRateLimit': True,
        'timeout': 15000,
        'options': {'defaultType': 'swap'},
    })
    if okx.get('sandbox', False) or okx.get('demo', False):
        setter = getattr(exchange, 'set_sandbox_mode', None)
        if not callable(setter):
            raise GateError('锁定 ccxt 缺少 sandbox 账户域设置')
        setter(True)
    return exchange


def _strict_positive_ms(value, context):
    if not isinstance(value, str) or not value.isdigit() or int(value) <= 0:
        raise GateError(f'{context} 缺少合法毫秒时间')
    return int(value)


def _strict_decimal(value, context):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise GateError(f'{context} 不是可证明的有限数')
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise GateError(f'{context} 不是可证明的有限数') from exc
    if not parsed.is_finite():
        raise GateError(f'{context} 不是有限数')
    return parsed


def _decimal_text(value):
    text = format(value, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


def _strict_reduce_only(value, context):
    if value is True or value == 'true':
        return True
    if value is False or value == 'false':
        return False
    raise GateError(f'{context}.reduceOnly 缺失或语义未知')


def _response_data(response, context):
    if (not isinstance(response, dict) or response.get('code') != '0' or
            not isinstance(response.get('data'), list)):
        raise GateError(f'{context} 响应信封异常')
    if len(response['data']) > PAGE_LIMIT:
        raise GateError(f'{context} 单页超过官方上限')
    return response['data']


def _call(method, params, context):
    try:
        return method(params)
    except GateError:
        raise
    except Exception as exc:
        # Third-party exception strings can contain signed requests or secrets.
        raise GateError(f'{context} 查询失败（{exc.__class__.__name__}）') from exc


class ReadOnlyOkxGate:
    REQUIRED_METHODS = (
        'publicGetPublicTime',
        'privateGetAccountConfig',
        'privateGetAccountPositions',
        'privateGetTradeOrdersPending',
        'privateGetTradeOrdersAlgoPending',
        'privateGetTradeOrdersHistory',
        'privateGetTradeFillsHistory',
        'privateGetTradeOrder',
        'privateGetTradeOrdersAlgoHistory',
    )

    def __init__(self, exchange):
        self.exchange = exchange
        for name in self.REQUIRED_METHODS:
            if not callable(getattr(exchange, name, None)):
                raise GateError(f'锁定 ccxt 缺少只读方法 {name}')

    def _paginate(self, method_name, base_params, id_field, context):
        method = getattr(self.exchange, method_name)
        records = []
        seen_ids = set()
        seen_cursors = set()
        after = None
        for _page in range(MAX_PAGES):
            params = dict(base_params)
            params['limit'] = str(PAGE_LIMIT)
            if after is not None:
                params['after'] = after
            page = _response_data(_call(method, params, context), context)
            ids = []
            for item in page:
                if not isinstance(item, dict):
                    raise GateError(f'{context} 分页项不是对象')
                raw_id = item.get(id_field)
                if not isinstance(raw_id, str) or not raw_id:
                    raise GateError(f'{context} 分页项缺少 {id_field}')
                if raw_id in seen_ids:
                    raise GateError(f'{context} 分页出现重复 ID')
                seen_ids.add(raw_id)
                ids.append(raw_id)
                records.append(item)
            if len(page) < PAGE_LIMIT:
                return records
            next_after = ids[-1]
            if next_after == after or next_after in seen_cursors:
                raise GateError(f'{context} 分页游标未前进')
            seen_cursors.add(next_after)
            after = next_after
        raise GateError(f'{context} 超过安全分页上限')

    @staticmethod
    def _validate_swap_item(item, context):
        if item.get('instType') != 'SWAP':
            raise GateError(f'{context}.instType 不是 SWAP')
        inst_id = item.get('instId')
        if not isinstance(inst_id, str) or not SWAP_RE.fullmatch(inst_id):
            raise GateError(f'{context}.instId 非法或未知')
        return inst_id

    def server_time_ms(self):
        data = _response_data(
            _call(self.exchange.publicGetPublicTime, {}, 'OKX server time'),
            'OKX server time')
        if len(data) != 1 or not isinstance(data[0], dict):
            raise GateError('OKX server time 数据数量异常')
        return _strict_positive_ms(data[0].get('ts'), 'OKX server time')

    def account_config(self):
        data = _response_data(
            _call(self.exchange.privateGetAccountConfig, {}, 'OKX account config'),
            'OKX account config')
        if len(data) != 1 or not isinstance(data[0], dict):
            raise GateError('OKX account config 数据数量异常')
        item = data[0]
        result = {
            'uid': item.get('uid'),
            'main_uid': item.get('mainUid'),
            'acct_lv': item.get('acctLv'),
            'pos_mode': item.get('posMode'),
        }
        for key in ('uid', 'main_uid'):
            if not isinstance(result[key], str) or not result[key]:
                raise GateError(f'OKX account config.{key} 非法')
        if result['acct_lv'] not in {'2', '3'}:
            raise GateError('OKX account config.acctLv 必须为 Futures/Multi-currency (2/3)')
        if result['pos_mode'] != 'net_mode':
            raise GateError('OKX account config.posMode 必须为 net_mode')
        return result

    def positions(self):
        data = _response_data(
            _call(
                self.exchange.privateGetAccountPositions,
                {'instType': 'SWAP'}, 'OKX positions'),
            'OKX positions')
        positions = []
        keys = set()
        for item in data:
            if not isinstance(item, dict):
                raise GateError('OKX positions 项不是对象')
            inst_id = self._validate_swap_item(item, 'position')
            size = _strict_decimal(item.get('pos'), 'position.pos')
            if size == 0:
                continue
            if item.get('posSide') != 'net':
                raise GateError('net_mode 的非零 position.posSide 必须为 net')
            pos_id = item.get('posId')
            if not isinstance(pos_id, str) or not pos_id:
                raise GateError('非零 position 缺少 posId')
            c_time = _strict_positive_ms(item.get('cTime'), 'position.cTime')
            direction = 'long' if size > 0 else 'short'
            key = (inst_id, pos_id, c_time, direction)
            if key in keys:
                raise GateError('positions 返回重复仓位身份')
            keys.add(key)
            positions.append({
                'inst_id': inst_id,
                'pos_id': pos_id,
                'c_time_ms': c_time,
                'direction': direction,
                'size_abs': _decimal_text(abs(size)),
            })
        positions.sort(key=lambda item: (
            item['inst_id'], item['pos_id'], item['c_time_ms'], item['direction']))
        return positions

    def pending_normal(self):
        raw = self._paginate(
            'privateGetTradeOrdersPending', {'instType': 'SWAP'},
            'ordId', 'normal pending')
        result = []
        for item in raw:
            inst_id = self._validate_swap_item(item, 'normal pending')
            if item.get('state') not in {'live', 'partially_filled'}:
                raise GateError('normal pending.state 非 pending 状态')
            if not _strict_reduce_only(item.get('reduceOnly'), 'normal pending'):
                raise GateError('发现非 reduceOnly 普通 pending，部署门禁拒绝')
            result.append({'id': item['ordId'], 'inst_id': inst_id, 'reduce_only': True})
        result.sort(key=lambda item: (item['inst_id'], item['id']))
        return result

    def pending_algo(self):
        result = []
        seen = set()
        for ord_type in ALGO_ORDER_TYPES:
            raw = self._paginate(
                'privateGetTradeOrdersAlgoPending',
                {'instType': 'SWAP', 'ordType': ord_type},
                'algoId', f'algo pending {ord_type}')
            for item in raw:
                inst_id = self._validate_swap_item(item, 'algo pending')
                if item.get('ordType') != ord_type:
                    raise GateError('algo pending.ordType 与查询类型不一致')
                if not _strict_reduce_only(item.get('reduceOnly'), 'algo pending'):
                    raise GateError('发现非 reduceOnly 算法 pending，部署门禁拒绝')
                identity = item['algoId']
                if identity in seen:
                    raise GateError('算法 pending 在类型查询间重复')
                seen.add(identity)
                result.append({
                    'id': identity,
                    'inst_id': inst_id,
                    'ord_type': ord_type,
                    'reduce_only': True,
                })
        result.sort(key=lambda item: (item['inst_id'], item['ord_type'], item['id']))
        return result

    def orders_history(self, begin_ms, end_ms):
        raw = self._paginate(
            'privateGetTradeOrdersHistory', {
                'instType': 'SWAP', 'begin': str(begin_ms), 'end': str(end_ms),
            }, 'ordId', 'orders history')
        result = []
        for item in raw:
            inst_id = self._validate_swap_item(item, 'orders history')
            created = _strict_positive_ms(item.get('cTime'), 'orders history.cTime')
            if not begin_ms <= created <= end_ms:
                raise GateError('orders history 返回裁决窗口之外的记录')
            if not _strict_reduce_only(item.get('reduceOnly'), 'orders history'):
                raise GateError('证明窗口内发现非 reduceOnly normal order')
            result.append((item['ordId'], inst_id))
        return result

    def fills_history(self, begin_ms, end_ms):
        raw = self._paginate(
            'privateGetTradeFillsHistory', {
                'instType': 'SWAP', 'begin': str(begin_ms), 'end': str(end_ms),
            }, 'billId', 'fills history')
        result = []
        for item in raw:
            inst_id = self._validate_swap_item(item, 'fills history')
            filled_at = _strict_positive_ms(item.get('ts'), 'fills history.ts')
            if not begin_ms <= filled_at <= end_ms:
                raise GateError('fills history 返回裁决窗口之外的记录')
            order_id = item.get('ordId')
            if not isinstance(order_id, str) or not order_id:
                raise GateError('fills history 缺少 ordId')
            result.append({
                'bill_id': item['billId'],
                'inst_id': inst_id,
                'order_id': order_id,
            })
        return result

    def assert_fill_orders_reduce_only(self, fills):
        checked = set()
        for fill in fills:
            key = (fill['inst_id'], fill['order_id'])
            if key in checked:
                continue
            checked.add(key)
            data = _response_data(
                _call(
                    self.exchange.privateGetTradeOrder,
                    {'instId': key[0], 'ordId': key[1]},
                    'fill order detail'),
                'fill order detail')
            if len(data) != 1 or not isinstance(data[0], dict):
                raise GateError('fill order detail 数据数量异常')
            detail = data[0]
            if detail.get('instId') != key[0] or detail.get('ordId') != key[1]:
                raise GateError('fill order detail 身份不匹配')
            self._validate_swap_item(detail, 'fill order detail')
            if not _strict_reduce_only(detail.get('reduceOnly'), 'fill order detail'):
                raise GateError('证明窗口内 fill 对应订单不是 reduceOnly')
        return checked

    def algo_history(self, begin_ms, end_ms):
        result = []
        seen = set()
        for ord_type in ALGO_ORDER_TYPES:
            for state_name in ALGO_HISTORY_STATES:
                raw = self._paginate(
                    'privateGetTradeOrdersAlgoHistory', {
                        'instType': 'SWAP',
                        'ordType': ord_type,
                        'state': state_name,
                    }, 'algoId', f'algo history {ord_type}/{state_name}')
                for item in raw:
                    inst_id = self._validate_swap_item(item, 'algo history')
                    if item.get('ordType') != ord_type or item.get('state') != state_name:
                        raise GateError('algo history 查询维度与响应不一致')
                    identity = item['algoId']
                    if identity in seen:
                        raise GateError('algo history 在类型/状态查询间重复')
                    seen.add(identity)
                    created = _strict_positive_ms(item.get('cTime'), 'algo history.cTime')
                    if begin_ms <= created <= end_ms:
                        if not _strict_reduce_only(item.get('reduceOnly'), 'algo history'):
                            raise GateError('证明窗口内发现非 reduceOnly algo order')
                        result.append((identity, inst_id, ord_type))
        return result


def _canonical_account(value, context='account'):
    _require_exact_fields(value, {'uid', 'main_uid', 'acct_lv', 'pos_mode'}, context)
    for key in ('uid', 'main_uid'):
        if not isinstance(value[key], str) or not value[key]:
            raise GateError(f'{context}.{key} 非法')
    if value['acct_lv'] not in {'2', '3'}:
        raise GateError(f'{context}.acct_lv 非法')
    if value['pos_mode'] != 'net_mode':
        raise GateError(f'{context}.pos_mode 必须为 net_mode')
    return value


def _validate_pending(pending):
    _require_exact_fields(pending, {'normal', 'algo'}, 'baseline pending')
    for kind, fields in (
            ('normal', {'id', 'inst_id', 'reduce_only'}),
            ('algo', {'id', 'inst_id', 'ord_type', 'reduce_only'})):
        records = pending[kind]
        if not isinstance(records, list):
            raise GateError(f'baseline pending.{kind} 必须是数组')
        seen = set()
        for item in records:
            _require_exact_fields(item, fields, f'baseline pending.{kind}')
            if (not isinstance(item['id'], str) or not item['id'] or
                    not isinstance(item['inst_id'], str) or
                    not SWAP_RE.fullmatch(item['inst_id']) or
                    item['reduce_only'] is not True):
                raise GateError(f'baseline pending.{kind} 字段非法')
            if kind == 'algo' and item['ord_type'] not in ALGO_ORDER_TYPES:
                raise GateError('baseline pending.algo.ord_type 非法')
            identity = tuple(item[field] for field in sorted(fields - {'reduce_only'}))
            if identity in seen:
                raise GateError(f'baseline pending.{kind} 含重复身份')
            seen.add(identity)


def _validate_positions(positions):
    if not isinstance(positions, list):
        raise GateError('baseline positions 必须是数组')
    seen = set()
    for item in positions:
        _require_exact_fields(item, {
            'inst_id', 'pos_id', 'c_time_ms', 'direction', 'size_abs',
        }, 'baseline position')
        if not isinstance(item['inst_id'], str) or not SWAP_RE.fullmatch(item['inst_id']):
            raise GateError('baseline position.inst_id 非法')
        if not isinstance(item['pos_id'], str) or not item['pos_id']:
            raise GateError('baseline position.pos_id 非法')
        if (isinstance(item['c_time_ms'], bool) or
                not isinstance(item['c_time_ms'], int) or item['c_time_ms'] <= 0):
            raise GateError('baseline position.c_time_ms 非法')
        if item['direction'] not in {'long', 'short'}:
            raise GateError('baseline position.direction 非法')
        if not isinstance(item['size_abs'], str):
            raise GateError('baseline position.size_abs 必须是规范字符串')
        size = _strict_decimal(item['size_abs'], 'baseline position.size_abs')
        if size <= 0 or item['size_abs'] != _decimal_text(size):
            raise GateError('baseline position.size_abs 必须是规范正数字符串')
        identity = (
            item['inst_id'], item['pos_id'], item['c_time_ms'], item['direction'])
        if identity in seen:
            raise GateError('baseline positions 含重复身份')
        seen.add(identity)


def _validate_baseline(payload):
    _require_exact_fields(payload, {
        'schema_version', 'exchange', 'release_sha', 'account_domain',
        'api_fingerprint', 'account', 'sentinel', 'runner_lock',
        't0_ms', 'pre_t0_history', 'positions', 'pending',
    }, 'baseline')
    if payload['schema_version'] != SCHEMA_VERSION:
        raise GateError('baseline schema_version 不兼容')
    if payload['exchange'] != 'okx':
        raise GateError('baseline exchange 不是 okx')
    _require_release_sha(payload['release_sha'])
    if payload['account_domain'] not in {'live', 'demo'}:
        raise GateError('baseline account_domain 非法')
    if (not isinstance(payload['api_fingerprint'], str) or
            not re.fullmatch(r'[0-9a-f]{64}', payload['api_fingerprint'])):
        raise GateError('baseline api_fingerprint 非法')
    _canonical_account(payload['account'], 'baseline account')
    _require_exact_fields(payload['sentinel'], {'nonce', 'dev', 'ino'}, 'baseline sentinel')
    if (not isinstance(payload['sentinel']['nonce'], str) or
            not NONCE_RE.fullmatch(payload['sentinel']['nonce'])):
        raise GateError('baseline sentinel.nonce 非法')
    _validate_file_identity({
        'dev': payload['sentinel']['dev'], 'ino': payload['sentinel']['ino'],
    }, 'baseline sentinel identity')
    _validate_file_identity(payload['runner_lock'], 'baseline runner_lock')
    if (isinstance(payload['t0_ms'], bool) or
            not isinstance(payload['t0_ms'], int) or payload['t0_ms'] <= 0):
        raise GateError('baseline t0_ms 非法')
    pre_t0 = payload['pre_t0_history']
    pre_t0_fields = {
        'history_started_ms', 'history_verified_through_ms',
        'orders_checked', 'fills_checked', 'fill_orders_checked',
        'algo_orders_checked',
    }
    _require_exact_fields(pre_t0, pre_t0_fields, 'baseline pre_t0_history')
    for key in pre_t0_fields:
        if (isinstance(pre_t0[key], bool) or
                not isinstance(pre_t0[key], int) or pre_t0[key] < 0):
            raise GateError(f'baseline pre_t0_history.{key} 非法')
    if (pre_t0['history_started_ms'] <= 0 or
            pre_t0['history_verified_through_ms'] != payload['t0_ms'] or
            pre_t0['history_started_ms'] > payload['t0_ms']):
        raise GateError('baseline pre-T0 历史证明时间非法')
    _validate_positions(payload['positions'])
    _validate_pending(payload['pending'])
    return payload


def _read_baseline_fd(fd):
    return _validate_baseline(_read_json_fd(fd, '部署只读基线'))


def _baseline_digest(payload):
    return hashlib.sha256(_dumps_strict(payload)).hexdigest()


def _validate_summary(summary):
    fields = {
        't1_ms', 't2_ms', 'history_verified_through_ms',
        'positions', 'pending_normal', 'pending_algo',
        'new_pending', 'orders_checked', 'fills_checked',
        'fill_orders_checked', 'algo_orders_checked',
    }
    _require_exact_fields(summary, fields, 'completion summary')
    for key in fields:
        if isinstance(summary[key], bool) or not isinstance(summary[key], int) or summary[key] < 0:
            raise GateError(f'completion summary.{key} 非法')
    if summary['t1_ms'] <= 0 or summary['t2_ms'] < summary['t1_ms']:
        raise GateError('completion summary 时间非法')
    if summary['history_verified_through_ms'] != summary['t2_ms']:
        raise GateError('completion summary 历史证明截止时刻非法')


def _validate_completion(payload):
    _require_exact_fields(payload, {
        'schema_version', 'status', 'release_sha', 'nonce',
        'baseline_sha256', 'baseline_file', 'sentinel', 'runner_lock',
        'account_domain', 'api_fingerprint', 'account', 't0_ms', 'summary',
    }, 'completion')
    if payload['schema_version'] != COMPLETION_SCHEMA_VERSION:
        raise GateError('completion schema_version 不兼容')
    if payload['status'] != 'verified_no_open_through_history_end':
        raise GateError('completion status 非法')
    _require_release_sha(payload['release_sha'])
    if not isinstance(payload['nonce'], str) or not NONCE_RE.fullmatch(payload['nonce']):
        raise GateError('completion nonce 非法')
    if (not isinstance(payload['baseline_sha256'], str) or
            not re.fullmatch(r'[0-9a-f]{64}', payload['baseline_sha256'])):
        raise GateError('completion baseline_sha256 非法')
    _validate_file_identity(payload['baseline_file'], 'completion baseline_file')
    _validate_file_identity(payload['sentinel'], 'completion sentinel')
    _validate_file_identity(payload['runner_lock'], 'completion runner_lock')
    if payload['account_domain'] not in {'live', 'demo'}:
        raise GateError('completion account_domain 非法')
    if (not isinstance(payload['api_fingerprint'], str) or
            not re.fullmatch(r'[0-9a-f]{64}', payload['api_fingerprint'])):
        raise GateError('completion api_fingerprint 非法')
    _canonical_account(payload['account'], 'completion account')
    if isinstance(payload['t0_ms'], bool) or not isinstance(payload['t0_ms'], int) or payload['t0_ms'] <= 0:
        raise GateError('completion t0_ms 非法')
    _validate_summary(payload['summary'])
    return payload


def _read_completion_fd(fd):
    return _validate_completion(_read_json_fd(fd, '部署完成证明'))


def _validate_baseline_bindings(
        baseline, sentinel, sentinel_info, lock_info, okx, release_sha):
    if baseline['release_sha'] != release_sha or sentinel['release_sha'] != release_sha:
        raise GateError('门禁证据与候选 release SHA 不一致')
    if baseline['sentinel']['nonce'] != sentinel['nonce']:
        raise GateError('baseline 与 sentinel nonce 不一致')
    if not _same_identity(
            {'dev': baseline['sentinel']['dev'], 'ino': baseline['sentinel']['ino']},
            sentinel_info):
        raise GateError('baseline 与当前 sentinel inode 不一致')
    if not _same_identity(baseline['runner_lock'], lock_info):
        raise GateError('baseline 与当前实际 runner lock inode 不一致')
    if baseline['account_domain'] != _account_domain(okx):
        raise GateError('baseline 与当前 OKX 账户域不一致')
    if baseline['api_fingerprint'] != _account_fingerprint(okx):
        raise GateError('baseline 与当前 OKX API Key 不一致')


def _validate_completed_pair(
        baseline, baseline_info, completion, lock_info, expected_release=None):
    if expected_release is not None and baseline['release_sha'] != expected_release:
        raise GateError('已完成证据的 release SHA 不匹配')
    pairs = (
        ('release_sha', baseline['release_sha'], completion['release_sha']),
        ('nonce', baseline['sentinel']['nonce'], completion['nonce']),
        ('baseline_sha256', _baseline_digest(baseline), completion['baseline_sha256']),
        ('account_domain', baseline['account_domain'], completion['account_domain']),
        ('api_fingerprint', baseline['api_fingerprint'], completion['api_fingerprint']),
        ('account', baseline['account'], completion['account']),
        ('t0_ms', baseline['t0_ms'], completion['t0_ms']),
    )
    for label, left, right in pairs:
        if left != right:
            raise GateError(f'completion 与 baseline 的 {label} 不一致')
    if not _same_identity(completion['baseline_file'], baseline_info):
        raise GateError('completion 与 baseline inode 不一致')
    if not _same_identity(completion['sentinel'], {
            'dev': baseline['sentinel']['dev'], 'ino': baseline['sentinel']['ino']}):
        raise GateError('completion 与 sentinel inode 不一致')
    if not _same_identity(completion['runner_lock'], baseline['runner_lock']):
        raise GateError('completion 与 baseline runner lock 不一致')
    if not _same_identity(baseline['runner_lock'], lock_info):
        raise GateError('已完成证据与当前 runner lock inode 不一致')
    t0_ms = baseline['t0_ms']
    t1_ms = completion['summary']['t1_ms']
    t2_ms = completion['summary']['t2_ms']
    safe_limit = MAX_PROOF_WINDOW_MS - PROOF_COMPLETION_SAFETY_MS
    if not (t0_ms <= t1_ms <= t2_ms):
        raise GateError('completion 时间关系早于 baseline T0 或发生倒退')
    if t2_ms - t0_ms >= safe_limit:
        raise GateError('completion 证明窗口超过历史保留安全线')


def _validate_completion_standalone(completion, lock_info):
    if not _same_identity(completion['runner_lock'], lock_info):
        raise GateError('已完成证据与当前 runner lock inode 不一致')


def _require_payload_unchanged(expected, current, context):
    """原 inode 内容也属于证据身份；原位覆盖不得绕过 inode 复核。"""
    if current != expected:
        raise GateError(f'{context} 在门禁运行期间被原位修改')


def _new_sentinel(release_sha):
    return {
        'schema_version': SENTINEL_SCHEMA_VERSION,
        'nonce': secrets.token_hex(32),
        'release_sha': _require_release_sha(release_sha),
    }


def _create_sentinel(dir_fd, release_sha):
    payload = _new_sentinel(release_sha)
    fd = _create_json_exclusive(
        dir_fd, SENTINEL_NAME, payload, '部署禁开仓哨兵')
    return fd, payload, _identity(os.fstat(fd))


def arm(data_dir, release_sha, environ=None):
    """Atomically protect the release, but only while the real lock is held."""
    _require_release_sha(release_sha)
    with _open_data_directory(data_dir) as dir_fd:
        with _hold_runner_lock(
                dir_fd, data_dir, environ=environ, exclusive=True) as (_, lock_info):
            sentinel_fd = _open_optional_entry(
                dir_fd, SENTINEL_NAME, '部署禁开仓哨兵')
            baseline_fd = _open_optional_entry(
                dir_fd, BASELINE_NAME, '部署只读基线')
            completion_fd = _open_optional_entry(
                dir_fd, COMPLETION_NAME, '部署完成证明')
            try:
                if sentinel_fd is None:
                    if baseline_fd is not None or completion_fd is not None:
                        raise GateError(
                            'sentinel 缺失但存在持久证据；'
                            '必须先人工判定 committed/damaged')
                    _recheck_data_directory_path(data_dir, dir_fd)
                    new_fd, _, _ = _create_sentinel(dir_fd, release_sha)
                    os.close(new_fd)
                    return True

                sentinel = _read_sentinel_fd(sentinel_fd)
                if sentinel['release_sha'] != release_sha:
                    raise GateError('既有 sentinel 绑定了不同 release SHA')
                _make_held_evidence_durable(
                    dir_fd, SENTINEL_NAME, sentinel_fd, '部署禁开仓哨兵')

                if completion_fd is not None and baseline_fd is None:
                    raise GateError('completion 存在但 baseline 缺失')
                if baseline_fd is not None:
                    baseline = _read_baseline_fd(baseline_fd)
                    if (baseline['release_sha'] != release_sha or
                            baseline['sentinel']['nonce'] != sentinel['nonce'] or
                            not _same_identity({
                                'dev': baseline['sentinel']['dev'],
                                'ino': baseline['sentinel']['ino'],
                            }, _identity(os.fstat(sentinel_fd))) or
                            not _same_identity(baseline['runner_lock'], lock_info)):
                        raise GateError('既有 baseline 与当前门禁/runner lock 不一致')
                    if completion_fd is not None:
                        completion = _read_completion_fd(completion_fd)
                        _validate_completed_pair(
                            baseline, _identity(os.fstat(baseline_fd)),
                            completion, lock_info,
                            expected_release=release_sha)
                return False
            finally:
                for fd in (sentinel_fd, baseline_fd, completion_fd):
                    if fd is not None:
                        os.close(fd)


def _capture_snapshot(gate):
    return {
        'positions': gate.positions(),
        'normal': gate.pending_normal(),
        'algo': gate.pending_algo(),
    }


def _capture_baseline(
        gate, okx, release_sha, sentinel, sentinel_info, lock_info,
        history_start_ms=None):
    account_before = gate.account_config()
    # T0 is authoritative before any baseline snapshot.  Therefore an order
    # created after T0 but closed before the snapshot is still covered by the
    # later history proof.  For the durable baseline, bridge the preceding Q2
    # endpoint through T0 so the shell-command boundary has no blind interval.
    t0_ms = gate.server_time_ms()
    if history_start_ms is None:
        history_start_ms = t0_ms
        pre_t0_evidence = {
            'orders': set(), 'fills': set(),
            'fill_orders': set(), 'algo_orders': set(),
        }
    else:
        if (isinstance(history_start_ms, bool) or
                not isinstance(history_start_ms, int) or history_start_ms <= 0 or
                history_start_ms > t0_ms):
            raise GateError('pre-T0 历史起点非法')
        _assert_time_window(history_start_ms, t0_ms, 'pre-T0 bridge')
        pre_t0_evidence = _history_evidence(
            gate, history_start_ms, t0_ms)
    snapshot = _capture_snapshot(gate)
    account_after = gate.account_config()
    if account_before != account_after:
        raise GateError('baseline 期间 OKX account config 发生变化')
    payload = {
        'schema_version': SCHEMA_VERSION,
        'exchange': 'okx',
        'release_sha': _require_release_sha(release_sha),
        'account_domain': _account_domain(okx),
        'api_fingerprint': _account_fingerprint(okx),
        'account': account_before,
        'sentinel': {
            'nonce': sentinel['nonce'],
            'dev': sentinel_info['dev'],
            'ino': sentinel_info['ino'],
        },
        'runner_lock': dict(lock_info),
        't0_ms': t0_ms,
        'pre_t0_history': {
            'history_started_ms': history_start_ms,
            'history_verified_through_ms': t0_ms,
            'orders_checked': len(pre_t0_evidence['orders']),
            'fills_checked': len(pre_t0_evidence['fills']),
            'fill_orders_checked': len(pre_t0_evidence['fill_orders']),
            'algo_orders_checked': len(pre_t0_evidence['algo_orders']),
        },
        'positions': snapshot['positions'],
        'pending': {
            'normal': snapshot['normal'],
            'algo': snapshot['algo'],
        },
    }
    return _validate_baseline(payload)


def _compare_positions(baseline, current):
    old = {
        (item['inst_id'], item['pos_id'], item['c_time_ms'], item['direction']):
        _strict_decimal(item['size_abs'], 'baseline position.size_abs')
        for item in baseline
    }
    for item in current:
        key = (item['inst_id'], item['pos_id'], item['c_time_ms'], item['direction'])
        if key not in old:
            raise GateError('verify 发现新增/反向/身份变化的 SWAP 仓位')
        size = _strict_decimal(item['size_abs'], 'current position.size_abs')
        if size > old[key]:
            raise GateError('verify 发现既有 SWAP 仓位绝对数量增加')


def _pending_identity(kind, item):
    if kind == 'normal':
        return item['id'], item['inst_id']
    return item['id'], item['inst_id'], item['ord_type']


def _assert_time_window(t0_ms, value, label):
    if value < t0_ms:
        raise GateError(f'OKX server time 在 {label} 倒退')
    safe_limit = MAX_PROOF_WINDOW_MS - PROOF_COMPLETION_SAFETY_MS
    if value - t0_ms >= safe_limit:
        raise GateError(
            'T0 已达到普通取消订单两小时保留窗口的安全截止线'
            f'（预留 {PROOF_COMPLETION_SAFETY_MS // 60000} 分钟收口）')


def _history_evidence(gate, begin_ms, end_ms):
    orders = gate.orders_history(begin_ms, end_ms)
    fills = gate.fills_history(begin_ms, end_ms)
    fill_orders = gate.assert_fill_orders_reduce_only(fills)
    algo_orders = gate.algo_history(begin_ms, end_ms)
    return {
        'orders': set(orders),
        'fills': {item['bill_id'] for item in fills},
        'fill_orders': set(fill_orders),
        'algo_orders': set(algo_orders),
    }


def _verify(gate, baseline, okx):
    if baseline['account_domain'] != _account_domain(okx):
        raise GateError('baseline 与 verify 的 OKX 账户域不一致')
    if baseline['api_fingerprint'] != _account_fingerprint(okx):
        raise GateError('baseline 与 verify 使用了不同 OKX API Key')

    account_start = gate.account_config()
    if account_start != baseline['account']:
        raise GateError('verify 的 OKX account config 与 baseline 不一致')

    # Two-phase history proof.  Do not overstate the result: authenticated
    # history is proven only through T2.  The post-history account/snapshot is
    # a current-state check; afterward the sentinel remains the invariant
    # preventing known writers from opening before the command returns.
    t1_ms = gate.server_time_ms()
    _assert_time_window(baseline['t0_ms'], t1_ms, 'T1')
    snapshot1 = _capture_snapshot(gate)
    evidence1 = _history_evidence(gate, baseline['t0_ms'], t1_ms)

    # T2 remains the last authoritative history endpoint.  The monotonic
    # clock below is only a fail-closed retention deadline: it must never be
    # persisted or presented as additional exchange-history evidence.
    t2_local_start_ns = time.monotonic_ns()
    t2_ms = gate.server_time_ms()
    if t2_ms < t1_ms:
        raise GateError('OKX server time 从 T1 到 T2 倒退')
    _assert_time_window(baseline['t0_ms'], t2_ms, 'T2')
    evidence2 = _history_evidence(gate, t1_ms, t2_ms)

    account_end = gate.account_config()
    if account_end != account_start or account_end != baseline['account']:
        raise GateError('verify 期间 OKX account config 发生变化')
    # 必须在账户复核之后再取最终快照；否则 account_config 调用期间出现的
    # 新仓/新挂单会在函数返回时已存在，却从 S2 中漏掉。
    snapshot2 = _capture_snapshot(gate)

    t2_local_end_ns = time.monotonic_ns()
    if t2_local_end_ns < t2_local_start_ns:
        raise GateError('T2 后本地单调时钟倒退')
    elapsed_after_t2_ms = (
        t2_local_end_ns - t2_local_start_ns + 999_999) // 1_000_000
    _assert_time_window(
        baseline['t0_ms'], t2_ms + elapsed_after_t2_ms, 'T2 后收口')

    _compare_positions(baseline['positions'], snapshot1['positions'])
    _compare_positions(baseline['positions'], snapshot2['positions'])

    new_pending = set()
    for snapshot in (snapshot1, snapshot2):
        for kind in ('normal', 'algo'):
            old_ids = {
                _pending_identity(kind, item)
                for item in baseline['pending'][kind]
            }
            new_pending.update(
                (kind, *_pending_identity(kind, item))
                for item in snapshot[kind]
                if _pending_identity(kind, item) not in old_ids)

    return {
        't1_ms': t1_ms,
        't2_ms': t2_ms,
        'history_verified_through_ms': t2_ms,
        'positions': len(snapshot2['positions']),
        'pending_normal': len(snapshot2['normal']),
        'pending_algo': len(snapshot2['algo']),
        'new_pending': len(new_pending),
        'orders_checked': len(evidence1['orders'] | evidence2['orders']),
        'fills_checked': len(evidence1['fills'] | evidence2['fills']),
        'fill_orders_checked': len(
            evidence1['fill_orders'] | evidence2['fill_orders']),
        'algo_orders_checked': len(
            evidence1['algo_orders'] | evidence2['algo_orders']),
    }


def _make_completion(
        baseline, baseline_info, sentinel_info, lock_info, summary):
    payload = {
        'schema_version': COMPLETION_SCHEMA_VERSION,
        'status': 'verified_no_open_through_history_end',
        'release_sha': baseline['release_sha'],
        'nonce': baseline['sentinel']['nonce'],
        'baseline_sha256': _baseline_digest(baseline),
        'baseline_file': dict(baseline_info),
        'sentinel': dict(sentinel_info),
        'runner_lock': dict(lock_info),
        'account_domain': baseline['account_domain'],
        'api_fingerprint': baseline['api_fingerprint'],
        'account': copy.deepcopy(baseline['account']),
        't0_ms': baseline['t0_ms'],
        'summary': copy.deepcopy(summary),
    }
    return _validate_completion(payload)


def _require_config_in_data_dir(config_path, data_dir):
    if not os.path.isabs(config_path):
        raise GateError('--config 必须是绝对路径')
    if os.path.realpath(os.path.dirname(config_path)) != os.path.realpath(data_dir):
        raise GateError('config.json 必须位于 --data-dir')


def run_baseline(
        config_path, data_dir, release_sha, history_start_ms, exchange=None,
        ccxt_module=None, environ=None):
    _require_release_sha(release_sha)
    _require_config_in_data_dir(config_path, data_dir)
    okx = load_okx_config(config_path, environ=environ)
    with _open_data_directory(data_dir) as dir_fd:
        with _hold_sentinel(dir_fd) as (sentinel_fd, sentinel, sentinel_info):
            with _hold_runner_lock(
                    dir_fd, data_dir, environ=environ,
                    exclusive=True) as (_, lock_info):
                _make_held_evidence_durable(
                    dir_fd, SENTINEL_NAME, sentinel_fd, '部署禁开仓哨兵')
                if sentinel['release_sha'] != release_sha:
                    raise GateError('sentinel 与 baseline release SHA 不一致')
                for name, context in (
                        (BASELINE_NAME, '部署只读基线'),
                        (COMPLETION_NAME, '部署完成证明')):
                    fd = _open_optional_entry(dir_fd, name, context)
                    if fd is not None:
                        os.close(fd)
                        raise GateError(f'{context} 已存在；baseline 严禁覆盖')
                active_exchange = (
                    exchange if exchange is not None else
                    create_read_only_exchange(okx, ccxt_module=ccxt_module))
                payload = _capture_baseline(
                    ReadOnlyOkxGate(active_exchange), okx, release_sha,
                    sentinel, sentinel_info, lock_info,
                    history_start_ms=history_start_ms)
                _recheck_data_directory_path(data_dir, dir_fd)
                baseline_fd = _create_json_exclusive(
                    dir_fd, BASELINE_NAME, payload, '部署只读基线')
                os.close(baseline_fd)
    return {
        'positions': len(payload['positions']),
        'pending_normal': len(payload['pending']['normal']),
        'pending_algo': len(payload['pending']['algo']),
        't0_ms': payload['t0_ms'],
        'pre_t0_history': copy.deepcopy(payload['pre_t0_history']),
    }


def run_verify(
        config_path, data_dir, release_sha, exchange=None,
        ccxt_module=None, environ=None):
    _require_release_sha(release_sha)
    _require_config_in_data_dir(config_path, data_dir)
    okx = load_okx_config(config_path, environ=environ)
    with _open_data_directory(data_dir) as dir_fd:
        with _hold_sentinel(dir_fd) as (_, sentinel, sentinel_info):
            with _hold_runner_lock(
                    dir_fd, data_dir, environ=environ,
                    exclusive=True) as (_, lock_info):
                baseline_fd = _open_optional_entry(
                    dir_fd, BASELINE_NAME, '部署只读基线')
                if baseline_fd is None:
                    raise GateError(f'缺少 {BASELINE_NAME}；必须先生成 baseline')
                try:
                    baseline = _read_baseline_fd(baseline_fd)
                    _validate_baseline_bindings(
                        baseline, sentinel, sentinel_info, lock_info,
                        okx, release_sha)
                    completion_fd = _open_optional_entry(
                        dir_fd, COMPLETION_NAME, '部署完成证明')
                    if completion_fd is not None:
                        os.close(completion_fd)
                        raise GateError('completion 已存在；必须显式 abandon 当前 attempt')
                    active_exchange = (
                        exchange if exchange is not None else
                        create_read_only_exchange(okx, ccxt_module=ccxt_module))
                    summary = _verify(
                        ReadOnlyOkxGate(active_exchange), baseline, okx)
                    _recheck_held_entry(
                        dir_fd, BASELINE_NAME, baseline_fd, '部署只读基线')
                    return summary
                finally:
                    os.close(baseline_fd)


def run_quiescence(config_path, data_dir, release_sha, exchange=None,
                   ccxt_module=None, environ=None):
    """Prove one Q0..Q2 drain interval without creating durable T0."""
    _require_release_sha(release_sha)
    _require_config_in_data_dir(config_path, data_dir)
    okx = load_okx_config(config_path, environ=environ)
    with _open_data_directory(data_dir) as dir_fd:
        with _hold_sentinel(dir_fd) as (_, sentinel, sentinel_info):
            with _hold_runner_lock(
                    dir_fd, data_dir, environ=environ,
                    exclusive=True) as (_, lock_info):
                active_exchange = (
                    exchange if exchange is not None else
                    create_read_only_exchange(okx, ccxt_module=ccxt_module))
                gate = ReadOnlyOkxGate(active_exchange)
                probe = _capture_baseline(
                    gate, okx, release_sha, sentinel, sentinel_info, lock_info)
                summary = _verify(gate, probe, okx)
                return {'q0_ms': probe['t0_ms'], **summary}


def seal(config_path, data_dir, release_sha, exchange=None,
         ccxt_module=None, environ=None):
    """Verify with the runner stopped and write completion, retaining sentinel."""
    _require_release_sha(release_sha)
    _require_config_in_data_dir(config_path, data_dir)
    okx = load_okx_config(config_path, environ=environ)
    with _open_data_directory(data_dir) as dir_fd:
        with _hold_runner_lock(
                dir_fd, data_dir, environ=environ,
                exclusive=True) as (_, lock_info):
            sentinel_fd = _open_optional_entry(
                dir_fd, SENTINEL_NAME, '部署禁开仓哨兵')
            baseline_fd = _open_optional_entry(
                dir_fd, BASELINE_NAME, '部署只读基线')
            completion_fd = _open_optional_entry(
                dir_fd, COMPLETION_NAME, '部署完成证明')
            try:
                if sentinel_fd is None or baseline_fd is None:
                    raise GateError('seal 要求 sentinel/baseline 同时存在')
                if completion_fd is not None:
                    raise GateError('completion 已存在；必须显式 abandon 当前 attempt')
                sentinel = _read_sentinel_fd(sentinel_fd)
                sentinel_info = _identity(os.fstat(sentinel_fd))
                baseline = _read_baseline_fd(baseline_fd)
                baseline_info = _identity(os.fstat(baseline_fd))
                _validate_baseline_bindings(
                    baseline, sentinel, sentinel_info, lock_info,
                    okx, release_sha)
                active_exchange = (
                    exchange if exchange is not None else
                    create_read_only_exchange(okx, ccxt_module=ccxt_module))
                summary = _verify(ReadOnlyOkxGate(active_exchange), baseline, okx)
                completion = _make_completion(
                    baseline, baseline_info, sentinel_info, lock_info, summary)
                _recheck_data_directory_path(data_dir, dir_fd)
                completion_fd = _create_json_exclusive(
                    dir_fd, COMPLETION_NAME, completion, '部署完成证明')
                current_sentinel = _read_sentinel_fd(sentinel_fd)
                current_baseline = _read_baseline_fd(baseline_fd)
                current_completion = _read_completion_fd(completion_fd)
                _require_payload_unchanged(
                    sentinel, current_sentinel, '部署禁开仓哨兵')
                _require_payload_unchanged(
                    baseline, current_baseline, '部署只读基线')
                _require_payload_unchanged(
                    completion, current_completion, '部署完成证明')
                _validate_baseline_bindings(
                    current_baseline, current_sentinel,
                    _identity(os.fstat(sentinel_fd)), lock_info,
                    okx, release_sha)
                _validate_completed_pair(
                    current_baseline, _identity(os.fstat(baseline_fd)),
                    current_completion, lock_info,
                    expected_release=release_sha)
                _make_held_evidence_durable(
                    dir_fd, SENTINEL_NAME, sentinel_fd, '部署禁开仓哨兵')
                _make_held_evidence_durable(
                    dir_fd, BASELINE_NAME, baseline_fd, '部署只读基线')
                _make_held_evidence_durable(
                    dir_fd, COMPLETION_NAME, completion_fd, '部署完成证明')
                durable_sentinel = _read_sentinel_fd(sentinel_fd)
                durable_baseline = _read_baseline_fd(baseline_fd)
                durable_completion = _read_completion_fd(completion_fd)
                _require_payload_unchanged(
                    sentinel, durable_sentinel, '部署禁开仓哨兵')
                _require_payload_unchanged(
                    baseline, durable_baseline, '部署只读基线')
                _require_payload_unchanged(
                    completion, durable_completion, '部署完成证明')
                _validate_baseline_bindings(
                    durable_baseline, durable_sentinel,
                    _identity(os.fstat(sentinel_fd)), lock_info,
                    okx, release_sha)
                _validate_completed_pair(
                    durable_baseline, _identity(os.fstat(baseline_fd)),
                    durable_completion, lock_info,
                    expected_release=release_sha)
                _recheck_data_directory_path(data_dir, dir_fd)
                return summary
            finally:
                for fd in (sentinel_fd, baseline_fd, completion_fd):
                    if fd is not None:
                        os.close(fd)


def commit_sealed(config_path, data_dir, release_sha, environ=None):
    """Atomically remove a sealed sentinel while the validated runner is live."""
    _require_release_sha(release_sha)
    _require_config_in_data_dir(config_path, data_dir)
    okx = load_okx_config(config_path, environ=environ)
    with _open_data_directory(data_dir) as dir_fd:
        with _hold_runner_lock(
                dir_fd, data_dir, environ=environ,
                exclusive=False) as (lock_fd, lock_info):
            _require_runner_lock_held_elsewhere(lock_fd)
            sentinel_fd = _open_optional_entry(
                dir_fd, SENTINEL_NAME, '部署禁开仓哨兵')
            baseline_fd = _open_optional_entry(
                dir_fd, BASELINE_NAME, '部署只读基线')
            completion_fd = _open_optional_entry(
                dir_fd, COMPLETION_NAME, '部署完成证明')
            try:
                if None in (sentinel_fd, baseline_fd, completion_fd):
                    raise GateError('commit 要求 sentinel/baseline/completion 全部存在')
                sentinel = _read_sentinel_fd(sentinel_fd)
                baseline = _read_baseline_fd(baseline_fd)
                completion = _read_completion_fd(completion_fd)
                _validate_baseline_bindings(
                    baseline, sentinel, _identity(os.fstat(sentinel_fd)),
                    lock_info, okx, release_sha)
                _validate_completed_pair(
                    baseline, _identity(os.fstat(baseline_fd)), completion,
                    lock_info, expected_release=release_sha)
                for name, fd, context in (
                        (SENTINEL_NAME, sentinel_fd, '部署禁开仓哨兵'),
                        (BASELINE_NAME, baseline_fd, '部署只读基线'),
                        (COMPLETION_NAME, completion_fd, '部署完成证明')):
                    _make_held_evidence_durable(dir_fd, name, fd, context)
                _recheck_data_directory_path(data_dir, dir_fd)
                _unlink_held_entry(
                    dir_fd, SENTINEL_NAME, sentinel_fd, '部署禁开仓哨兵')
                return dict(completion['summary'])
            finally:
                for fd in (sentinel_fd, baseline_fd, completion_fd):
                    if fd is not None:
                        os.close(fd)


def abandon_cycle(data_dir, release_sha, attempt_id, environ=None):
    """Journal, then archive an unfinished attempt; retries finish the journal."""
    _require_release_sha(release_sha)
    if not isinstance(attempt_id, str) or not re.fullmatch(r'[0-9]{4}', attempt_id):
        raise GateError('attempt-id 必须是四位数字')
    with _open_data_directory(data_dir) as dir_fd:
        with _hold_runner_lock(
                dir_fd, data_dir, environ=environ,
                exclusive=True) as (_, lock_info):
            held = {}
            locations = {}
            audit_fd = None
            audit_name = f'deployment_abandon_{attempt_id}.json'
            try:
                for name, context in (
                        (SENTINEL_NAME, '部署禁开仓哨兵'),
                        (BASELINE_NAME, '部署只读基线'),
                        (COMPLETION_NAME, '部署完成证明')):
                    archive = f'.abandoned.{attempt_id}.{name}'
                    current_fd = _open_optional_entry(dir_fd, name, context)
                    archive_fd = _open_optional_entry(
                        dir_fd, archive, f'{context} abandon 归档')
                    if current_fd is not None and archive_fd is not None:
                        os.close(current_fd)
                        os.close(archive_fd)
                        raise GateError('abandon 原件与归档同时存在')
                    held[name] = current_fd if current_fd is not None else archive_fd
                    locations[name] = 'current' if current_fd is not None else (
                        'archive' if archive_fd is not None else 'missing')
                if held[SENTINEL_NAME] is None:
                    raise GateError(
                        'sentinel 缺失且无 abandon 归档：已提交周期不得 abandon')
                sentinel = _read_sentinel_fd(held[SENTINEL_NAME])
                if sentinel['release_sha'] != release_sha:
                    raise GateError('abandon release SHA 不匹配')
                archived = {}
                baseline = None
                if held[BASELINE_NAME] is not None:
                    baseline = _read_baseline_fd(held[BASELINE_NAME])
                    if (baseline['release_sha'] != release_sha or
                            baseline['sentinel']['nonce'] != sentinel['nonce'] or
                            not _same_identity(
                                baseline['sentinel'],
                                _identity(os.fstat(held[SENTINEL_NAME]))) or
                            not _same_identity(baseline['runner_lock'], lock_info)):
                        raise GateError('abandon baseline 绑定非法')
                if held[COMPLETION_NAME] is not None:
                    if baseline is None:
                        raise GateError('completion 存在但 baseline 缺失')
                    completion = _read_completion_fd(held[COMPLETION_NAME])
                    _validate_completed_pair(
                        baseline, _identity(os.fstat(held[BASELINE_NAME])),
                        completion, lock_info, expected_release=release_sha)
                for name, fd in held.items():
                    if fd is not None:
                        payload = os.pread(fd, os.fstat(fd).st_size, 0)
                        archived[name] = hashlib.sha256(payload).hexdigest()
                audit = {
                    'schema_version': 1,
                    'release_sha': release_sha,
                    'attempt_id': attempt_id,
                    'archived_sha256': archived,
                }
                audit_fd = _open_optional_entry(
                    dir_fd, audit_name, '部署 abandon 审计')
                if audit_fd is None:
                    audit_fd = _create_json_exclusive(
                        dir_fd, audit_name, audit, '部署 abandon 审计')
                elif _read_json_fd(audit_fd, '部署 abandon 审计') != audit:
                    raise GateError('abandon 审计与当前证据不匹配')
                # The write-once audit is durable before the first rename, so
                # an interruption can only leave a deterministically resumable
                # subset of the declared archive operations.
                for name in (BASELINE_NAME, COMPLETION_NAME, SENTINEL_NAME):
                    location = locations[name]
                    if location == 'current':
                        os.rename(
                            name, f'.abandoned.{attempt_id}.{name}',
                            src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                        os.fsync(dir_fd)
                for name, expected_sha in archived.items():
                    verify_fd = _open_verified_entry(
                        dir_fd, f'.abandoned.{attempt_id}.{name}',
                        f'{name} abandon 归档')
                    try:
                        payload = os.pread(
                            verify_fd, os.fstat(verify_fd).st_size, 0)
                        if hashlib.sha256(payload).hexdigest() != expected_sha:
                            raise GateError('abandon 归档摘要不匹配')
                    finally:
                        os.close(verify_fd)
                os.fsync(dir_fd)
                return audit
            finally:
                if audit_fd is not None:
                    os.close(audit_fd)
                for fd in held.values():
                    if fd is not None:
                        os.close(fd)


def _build_parser():
    parser = argparse.ArgumentParser(description='OKX 部署期间只读零开仓门禁')
    subparsers = parser.add_subparsers(dest='command', required=True)
    completed = subparsers.add_parser('completed-slot')
    completed.add_argument('--data-dir', required=True)
    completed.add_argument('--config', required=True)
    for command in ('arm', 'baseline', 'verify', 'quiesce', 'seal', 'commit',
                    'abandon'):
        child = subparsers.add_parser(command)
        child.add_argument('--data-dir', required=True)
        child.add_argument('--release-sha', required=True)
        if command not in ('arm', 'abandon'):
            child.add_argument('--config', required=True)
        if command == 'baseline':
            child.add_argument('--history-start-ms', required=True, type=int)
        if command == 'abandon':
            child.add_argument('--attempt-id', required=True)
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        if args.command == 'completed-slot':
            print(require_completed_schedule_slot(args.config, args.data_dir))
        elif args.command == 'arm':
            created = arm(args.data_dir, args.release_sha)
            print('[通过] 部署禁开仓哨兵已安全启用' if created else
                  '[通过] 既有部署禁开仓哨兵安全且已启用')
        elif args.command == 'baseline':
            summary = run_baseline(
                args.config, args.data_dir, args.release_sha,
                args.history_start_ms)
            print(
                '[通过] B0 后的 T0 基线已写一次保存：'
                f"positions={summary['positions']}, "
                f"pending_normal={summary['pending_normal']}, "
                f"pending_algo={summary['pending_algo']}")
        elif args.command == 'verify':
            summary = run_verify(
                args.config, args.data_dir, args.release_sha)
            print(
                '[通过] 两阶段闭合未发现新开仓：'
                f"positions={summary['positions']}, "
                f"orders={summary['orders_checked']}, "
                f"fills={summary['fills_checked']}, "
                f"algo={summary['algo_orders_checked']}")
        elif args.command == 'quiesce':
            summary = run_quiescence(
                args.config, args.data_dir, args.release_sha)
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        elif args.command == 'seal':
            summary = seal(args.config, args.data_dir, args.release_sha)
            print(
                '[通过] runner 已停且完成证明已封印，sentinel 保留：'
                f"positions={summary['positions']}, "
                f"orders={summary['orders_checked']}, "
                f"fills={summary['fills_checked']}, "
                f"algo={summary['algo_orders_checked']}")
        elif args.command == 'commit':
            summary = commit_sealed(
                args.config, args.data_dir, args.release_sha)
            print('[通过] sealed sentinel 已原子提交解除：'
                  f"history_end={summary['history_verified_through_ms']}")
        else:
            audit = abandon_cycle(
                args.data_dir, args.release_sha, args.attempt_id)
            print('[通过] 未完成周期已审计封存：'
                  f"attempt={audit['attempt_id']}")
        return 0
    except GateError as exc:
        print(f'[阻断] {exc}', file=sys.stderr)
        return 2
    except Exception as exc:
        print(f'[阻断] 未预期门禁异常（{exc.__class__.__name__}）', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
