#!/usr/bin/python3 -I
"""Bind deployment no-open evidence to the actual runner lock holder."""

import argparse
import json
import os
import pwd
import re
import stat
import sys
import urllib.error
import urllib.request


BASE_URL = 'http://127.0.0.1:5000'
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
NONCE_RE = re.compile(r'^[0-9a-f]{64}$')
BOOT_ID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
MAX_JSON_BYTES = 64 * 1024


class GateError(RuntimeError):
    """The runner or evidence did not prove the reviewed boundary."""


def _stable_file_metadata(info):
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
        info.st_nlink, info.st_size, info.st_mtime_ns,
    )


def _reject_constant(value):
    raise GateError(f'JSON 包含非标准常量 {value!r}')


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GateError(f'JSON 包含重复字段 {key!r}')
        result[key] = value
    return result


def _loads_strict(raw, context):
    try:
        return json.loads(
            raw.decode('utf-8'),
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except GateError:
        raise
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise GateError(f'{context} 不是严格 UTF-8 JSON') from exc


def _protected_root_file(path, context):
    """Read a root-only evidence file without following replacement aliases."""
    _canonical_path(path)
    if os.path.realpath(path) != path:
        raise GateError(f'{context} 路径穿过符号链接')
    parent = os.path.dirname(path)
    current = parent
    while True:
        try:
            info = os.stat(current, follow_symlinks=False)
        except OSError as exc:
            raise GateError(f'{context} 父目录不可验证') from exc
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or
                stat.S_IMODE(info.st_mode) & 0o022):
            raise GateError(f'{context} 父目录不是 root 保护目录')
        if current == '/':
            break
        current = os.path.dirname(current)
    try:
        before = os.lstat(path)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or
                before.st_gid != 0 or
                stat.S_IMODE(before.st_mode) != 0o600 or
                before.st_nlink != 1 or before.st_size > MAX_JSON_BYTES):
            raise GateError(f'{context} 必须是 root:root 0600 单链接普通文件')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except GateError:
        raise
    except OSError as exc:
        raise GateError(f'{context} 无法安全打开') from exc
    try:
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
                not stat.S_ISREG(after.st_mode) or after.st_uid != 0 or
                after.st_gid != 0 or
                stat.S_IMODE(after.st_mode) != 0o600 or
                after.st_nlink != 1 or after.st_size > MAX_JSON_BYTES):
            raise GateError(f'{context} 在检查期间被替换')
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_JSON_BYTES:
                raise GateError(f'{context} 超过大小上限')
            chunks.append(chunk)
        final = os.fstat(fd)
        if _stable_file_metadata(after) != _stable_file_metadata(final):
            raise GateError(f'{context} 在读取期间被修改')
        return _loads_strict(b''.join(chunks), context)
    finally:
        os.close(fd)


def _runtime_sentinel(path, context):
    """Read the exact ubuntu-owned sentinel inode observed by the runner."""
    _canonical_path(path)
    if os.path.realpath(path) != path:
        raise GateError(f'{context} 路径穿过符号链接')
    try:
        ubuntu = pwd.getpwnam('ubuntu')
        ubuntu_uid = ubuntu.pw_uid
        ubuntu_gid = ubuntu.pw_gid
        before = os.lstat(path)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != ubuntu_uid or
                before.st_gid != ubuntu_gid or
                stat.S_IMODE(before.st_mode) != 0o600 or
                before.st_nlink != 1 or before.st_size > MAX_JSON_BYTES):
            raise GateError(f'{context} 必须是 ubuntu 0600 单链接普通文件')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except GateError:
        raise
    except (KeyError, OSError) as exc:
        raise GateError(f'{context} 无法安全打开') from exc
    try:
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
                after.st_uid != ubuntu_uid or
                after.st_gid != ubuntu_gid or
                stat.S_IMODE(after.st_mode) != 0o600 or
                after.st_nlink != 1):
            raise GateError(f'{context} 在检查期间被替换')
        raw = bytearray()
        while len(raw) <= MAX_JSON_BYTES:
            chunk = os.read(fd, min(65536, MAX_JSON_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > MAX_JSON_BYTES:
            raise GateError(f'{context} 超过大小上限')
        final = os.fstat(fd)
        if _stable_file_metadata(after) != _stable_file_metadata(final):
            raise GateError(f'{context} 在读取期间被修改')
        return _loads_strict(bytes(raw), context), final
    finally:
        os.close(fd)


def _runtime_gate_object(path, context):
    """Observe the exact object whose mere presence makes runtime fail closed."""
    _canonical_path(path)
    parent = os.path.dirname(path)
    if os.path.realpath(parent) != parent:
        raise GateError(f'{context} 父目录不是规范真实路径')
    try:
        before = os.lstat(path)
        after = os.lstat(path)
    except OSError as exc:
        raise GateError(f'{context} 对象不可验证') from exc
    if _stable_file_metadata(before) != _stable_file_metadata(after):
        raise GateError(f'{context} 对象在检查期间变化')
    return after


def _durabilize_runtime_gate(path, expected, context):
    """Make an already-visible fail-closed entry durable without replacing it."""
    _canonical_path(path)
    parent = os.path.dirname(path)
    if os.path.realpath(parent) != parent:
        raise GateError(f'{context} 父目录不是规范真实路径')
    gate_fd = parent_fd = None
    try:
        if stat.S_ISREG(expected.st_mode):
            gate_fd = os.open(
                path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            opened = os.fstat(gate_fd)
            if _stable_file_metadata(opened) != _stable_file_metadata(expected):
                raise GateError(f'{context} 在持久化打开期间变化')
            os.fsync(gate_fd)
        parent_before = os.lstat(parent)
        if not stat.S_ISDIR(parent_before.st_mode):
            raise GateError(f'{context} 父路径不是目录')
        parent_fd = os.open(
            parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) |
            getattr(os, 'O_NOFOLLOW', 0))
        parent_opened = os.fstat(parent_fd)
        if _stable_file_metadata(parent_before) != _stable_file_metadata(
                parent_opened):
            raise GateError(f'{context} 父目录在持久化打开期间变化')
        os.fsync(parent_fd)
        final = os.lstat(path)
        if _stable_file_metadata(final) != _stable_file_metadata(expected):
            raise GateError(f'{context} 在持久化期间变化')
        return final
    except GateError:
        raise
    except OSError as exc:
        raise GateError(f'{context} 无法证明掉电耐久性') from exc
    finally:
        if gate_fd is not None:
            os.close(gate_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _canonical_path(value):
    if (not isinstance(value, str) or not os.path.isabs(value) or
            os.path.normpath(value) != value):
        raise GateError('旧 runner sentinel_path 不是规范绝对路径')
    return value


def _validate_arm_result(result, release_sha=None, nonce=None):
    if not isinstance(result, dict) or set(result) != {
            'protocol', 'status', 'inflight_open_boundary_drained',
            'sentinel'}:
        raise GateError('旧 runner arm response schema 非法')
    if (result['protocol'] != 'trade-lock-no-open-v1' or
            result['status'] != 'maintenance_blocked' or
            result['inflight_open_boundary_drained'] is not True):
        raise GateError('旧 runner 未证明同锁排空并进入 maintenance_blocked')
    sentinel = result['sentinel']
    if not isinstance(sentinel, dict) or set(sentinel) != {
            'schema_version', 'kind', 'release_sha', 'nonce', 'worker_pid',
            'path', 'dev', 'ino'}:
        raise GateError('旧 runner sentinel evidence schema 非法')
    if (type(sentinel['schema_version']) is not int or
            sentinel['schema_version'] != 1 or
            sentinel['kind'] != 'old_runner_no_open' or
            SHA_RE.fullmatch(sentinel.get('release_sha') or '') is None or
            NONCE_RE.fullmatch(sentinel.get('nonce') or '') is None):
        raise GateError('旧 runner sentinel evidence 字段非法')
    if release_sha is not None and sentinel['release_sha'] != release_sha:
        raise GateError('旧 runner sentinel release_sha 绑定不一致')
    if nonce is not None and sentinel['nonce'] != nonce:
        raise GateError('旧 runner sentinel nonce 绑定不一致')
    _canonical_path(sentinel['path'])
    for field in ('worker_pid', 'dev', 'ino'):
        if (isinstance(sentinel[field], bool) or
                not isinstance(sentinel[field], int) or sentinel[field] <= 0):
            raise GateError(f'旧 runner sentinel.{field} 非法')
    return sentinel


def _token():
    value = os.environ.get('TRADING_API_TOKEN')
    if not isinstance(value, str) or not value or value != value.strip():
        raise GateError('TRADING_API_TOKEN 缺失或非法')
    return value


def _request(method, path, payload=None, expected=(200,)):
    body = None
    headers = {'X-API-Token': _token()}
    if payload is not None:
        body = json.dumps(
            payload, sort_keys=True, separators=(',', ':')).encode('ascii')
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(
        BASE_URL + path, data=body, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=150)
        status = response.status
        raw = response.read(MAX_JSON_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read(MAX_JSON_BYTES + 1)
    except Exception as exc:
        raise GateError(
            f'旧 runner 本机 HTTP 不可验证: {exc.__class__.__name__}') from exc
    if status not in expected:
        if status == 404 and path.endswith('/no-open-capability'):
            raise FileNotFoundError('旧 runner 不支持 no-open handshake v1')
        raise GateError(f'旧 runner HTTP 状态异常: {status}')
    if len(raw) > MAX_JSON_BYTES:
        raise GateError('旧 runner HTTP 响应过大')
    result = _loads_strict(raw, '旧 runner HTTP 响应')
    if not isinstance(result, dict):
        raise GateError('旧 runner HTTP 响应顶层不是对象')
    return status, result


def capability():
    _status, result = _request(
        'GET', '/api/deployment/no-open-capability')
    if set(result) != {
            'protocol', 'worker_pid', 'sentinel_path',
            'maintenance_active'}:
        raise GateError('旧 runner capability schema 非法')
    if result['protocol'] != 'trade-lock-no-open-v1':
        raise GateError('旧 runner handshake protocol 不兼容')
    if (isinstance(result['worker_pid'], bool) or
            not isinstance(result['worker_pid'], int) or
            result['worker_pid'] <= 0):
        raise GateError('旧 runner worker_pid 非法')
    _canonical_path(result['sentinel_path'])
    if not isinstance(result['maintenance_active'], bool):
        raise GateError('旧 runner maintenance_active 非布尔值')
    return result


def arm(release_sha, nonce):
    if SHA_RE.fullmatch(release_sha or '') is None:
        raise GateError('release_sha 非法')
    if NONCE_RE.fullmatch(nonce or '') is None:
        raise GateError('nonce 非法')
    _status, result = _request(
        'POST', '/api/deployment/arm-no-open',
        {'release_sha': release_sha, 'nonce': nonce})
    _validate_arm_result(result, release_sha=release_sha, nonce=nonce)
    return result


def _validate_drain_result(result):
    if not isinstance(result, dict) or set(result) != {
            'protocol', 'status', 'inflight_open_boundary_drained',
            'worker_pid', 'sentinel'}:
        raise GateError('runner 最终排空响应 schema 非法')
    if (result['protocol'] != 'trade-lock-no-open-v1' or
            result['status'] != 'maintenance_blocked' or
            result['inflight_open_boundary_drained'] is not True or
            isinstance(result['worker_pid'], bool) or
            not isinstance(result['worker_pid'], int) or
            result['worker_pid'] <= 0):
        raise GateError('runner 最终排空未证明同锁 maintenance boundary')
    sentinel = result['sentinel']
    if not isinstance(sentinel, dict) or set(sentinel) != {
            'path', 'dev', 'ino'}:
        raise GateError('runner 最终排空 sentinel schema 非法')
    _canonical_path(sentinel['path'])
    for field in ('dev', 'ino'):
        if (isinstance(sentinel[field], bool) or
                not isinstance(sentinel[field], int) or
                sentinel[field] <= 0):
            raise GateError(f'runner 最终排空 sentinel.{field} 非法')
    return result


def verify_http_block():
    _status, result = _request(
        'POST', '/api/instant_open', {}, expected=(503,))
    if result.get('maintenance_no_open') is not True:
        raise GateError('旧 runner HTTP 写接口未证明 503 maintenance_no_open')
    return {'http_status': 503, 'maintenance_no_open': True}


def drain_maintenance_boundary(release_sha, runner_lock, service_cgroup,
                               expected_cwd, current_data):
    """Drain the shared trade lock and bind it to the final release sentinel."""
    if SHA_RE.fullmatch(release_sha or '') is None:
        raise GateError('release_sha 非法')
    expected_path = os.path.join(
        _canonical_path(current_data), '.maintenance_no_open')
    if os.path.realpath(current_data) != current_data:
        raise GateError('runner 数据目录不是规范真实路径')
    before = capture_process_binding(
        runner_lock, service_cgroup, expected_cwd)
    _status, result = _request('GET', '/api/deployment/drain-no-open')
    result = _validate_drain_result(result)
    if (result['worker_pid'] != before['worker_pid'] or
            result['sentinel']['path'] != expected_path):
        raise GateError('runner 最终排空未绑定实际 FLOCK worker/数据目录')
    payload, info = _runtime_sentinel(expected_path, '最终 release sentinel')
    _validate_release_sentinel_payload(payload, release_sha)
    if ((info.st_dev, info.st_ino) !=
            (result['sentinel']['dev'], result['sentinel']['ino'])):
        raise GateError('runner 最终排空 sentinel inode 与实际对象不一致')
    verify_http_block()
    after = capture_process_binding(
        runner_lock, service_cgroup, expected_cwd)
    if after != before:
        raise GateError('runner 身份在最终同锁排空期间发生变化')
    return result


def _read_limited(path, limit, context):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except OSError as exc:
        raise GateError(f'{context} 无法安全打开') from exc
    try:
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise GateError(f'{context} 超过大小上限')
        return b''.join(chunks)
    finally:
        os.close(fd)


def _process_start(proc_root, pid):
    raw = _read_limited(
        os.path.join(proc_root, str(pid), 'stat'), 64 * 1024,
        '旧 runner worker stat')
    marker = raw.rfind(b') ')
    if marker < 0:
        raise GateError('旧 runner worker stat 格式非法')
    fields = raw[marker + 2:].split()
    # The tail starts at field 3 (state); starttime is field 22.
    if len(fields) <= 19 or fields[0] == b'Z':
        raise GateError('旧 runner worker 已退出或为 zombie')
    try:
        start_ticks = int(fields[19])
    except ValueError as exc:
        raise GateError('旧 runner worker starttime 非法') from exc
    if start_ticks <= 0:
        raise GateError('旧 runner worker starttime 非法')
    return start_ticks


def _boot_id(proc_root):
    try:
        value = _read_limited(
            os.path.join(proc_root, 'sys/kernel/random/boot_id'), 128,
            'kernel boot_id').decode('ascii').strip()
    except UnicodeDecodeError as exc:
        raise GateError('kernel boot_id 非 ASCII') from exc
    if BOOT_ID_RE.fullmatch(value) is None:
        raise GateError('kernel boot_id 非法')
    return value


def _flock_owner(proc_root, lock_info):
    """Return the kernel-reported exclusive FLOCK owner for this inode."""
    raw = _read_limited(
        os.path.join(proc_root, 'locks'), 16 * 1024 * 1024,
        'kernel lock table')
    try:
        lines = raw.decode('ascii').splitlines()
    except UnicodeDecodeError as exc:
        raise GateError('kernel lock table 非 ASCII') from exc
    owners = []
    for line in lines:
        fields = line.split()
        if len(fields) != 8 or fields[1:4] != ['FLOCK', 'ADVISORY', 'WRITE']:
            continue
        device = fields[5].split(':')
        if (len(device) != 3 or fields[6:] != ['0', 'EOF'] or
                not fields[0].endswith(':')):
            continue
        try:
            pid = int(fields[4], 10)
            major = int(device[0], 16)
            minor = int(device[1], 16)
            inode = int(device[2], 10)
        except ValueError:
            continue
        if (major, minor, inode) == (
                os.major(lock_info.st_dev), os.minor(lock_info.st_dev),
                lock_info.st_ino):
            owners.append(pid)
    if len(owners) != 1 or owners[0] <= 0:
        raise GateError('runner lock 没有唯一的内核 FLOCK WRITE 持有者')
    return owners[0]


def _cgroup_pids(service_cgroup, cgroup_root):
    if (not isinstance(service_cgroup, str) or
            not service_cgroup.startswith('/') or
            '..' in service_cgroup.split('/')):
        raise GateError('trading.service cgroup 非法')
    path = os.path.join(cgroup_root, service_cgroup.lstrip('/'), 'cgroup.procs')
    try:
        raw = _read_limited(path, 1024 * 1024, 'trading.service cgroup.procs')
        values = raw.decode('ascii').splitlines()
        pids = {int(value) for value in values if value}
    except (UnicodeDecodeError, ValueError) as exc:
        raise GateError('trading.service cgroup.procs 非法') from exc
    if not pids or any(pid <= 0 for pid in pids):
        raise GateError('trading.service cgroup 无有效进程')
    return pids


def capture_process_binding(runner_lock, service_cgroup, expected_cwd,
                            proc_root='/proc', cgroup_root='/sys/fs/cgroup'):
    """Bind one handshake observation to the worker holding runner.lock."""
    runner_lock = _canonical_path(runner_lock)
    expected_cwd = _canonical_path(expected_cwd)
    if os.path.realpath(runner_lock) != runner_lock:
        raise GateError('runner lock 路径穿过符号链接')
    try:
        ubuntu = pwd.getpwnam('ubuntu')
        before = os.lstat(runner_lock)
        if (not stat.S_ISREG(before.st_mode) or
                before.st_uid != ubuntu.pw_uid or
                before.st_gid != ubuntu.pw_gid or
                stat.S_IMODE(before.st_mode) != 0o600 or
                before.st_nlink != 1):
            raise GateError('runner lock 必须是 ubuntu:ubuntu 0600 单链接普通文件')
        lock_fd = os.open(
            runner_lock, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except GateError:
        raise
    except (KeyError, OSError) as exc:
        raise GateError('runner lock 无法安全打开') from exc
    try:
        opened = os.fstat(lock_fd)
        if ((before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or
                _stable_file_metadata(before) != _stable_file_metadata(opened)):
            raise GateError('runner lock 在打开期间被替换')
        raw_pid = os.read(lock_fd, 64)
        if not re.fullmatch(rb'[1-9][0-9]*\n', raw_pid):
            raise GateError('runner lock 内容不是精确 worker PID')
        claimed_pid = int(raw_pid)
        boot_before = _boot_id(proc_root)
        if _flock_owner(proc_root, opened) != claimed_pid:
            raise GateError('runner lock 内容 PID 不是内核 FLOCK 持有者')
        if claimed_pid not in _cgroup_pids(service_cgroup, cgroup_root):
            raise GateError('runner lock 持有者不在 trading.service cgroup')
        process_dir = os.path.join(proc_root, str(claimed_pid))
        if os.stat(process_dir).st_uid != ubuntu.pw_uid:
            raise GateError('runner lock 持有者不是 ubuntu 进程')
        start_before = _process_start(proc_root, claimed_pid)
        try:
            cwd = os.path.realpath(os.path.join(process_dir, 'cwd'))
        except OSError as exc:
            raise GateError('无法读取旧 runner worker cwd') from exc
        if cwd != expected_cwd:
            raise GateError('旧 runner worker cwd 与部署源目录不一致')
        fd_matches = 0
        try:
            for name in os.listdir(os.path.join(process_dir, 'fd')):
                if not name.isdigit():
                    continue
                try:
                    info = os.stat(os.path.join(process_dir, 'fd', name))
                except FileNotFoundError:
                    continue
                if (info.st_dev, info.st_ino) == (opened.st_dev, opened.st_ino):
                    fd_matches += 1
        except OSError as exc:
            raise GateError('无法验证旧 runner worker 的 runner lock fd') from exc
        if fd_matches < 1:
            raise GateError('实际 FLOCK worker 未持有 runner lock fd')
        start_after = _process_start(proc_root, claimed_pid)
        final = os.fstat(lock_fd)
        if (boot_before != _boot_id(proc_root) or
                _flock_owner(proc_root, final) != claimed_pid or
                start_before != start_after or
                claimed_pid not in _cgroup_pids(service_cgroup, cgroup_root) or
                _stable_file_metadata(opened) != _stable_file_metadata(final)):
            raise GateError('旧 runner worker/lock 在凭据观察期间发生变化')
        return {
            'schema_version': 1,
            'boot_id': boot_before,
            'worker_pid': claimed_pid,
            'worker_start_ticks': start_before,
            'service_cgroup': service_cgroup,
            'worker_cwd': cwd,
            'runner_lock': {
                'path': runner_lock,
                'dev': opened.st_dev,
                'ino': opened.st_ino,
            },
        }
    finally:
        os.close(lock_fd)


def _validate_process_binding(value):
    if not isinstance(value, dict) or set(value) != {
            'schema_version', 'boot_id', 'worker_pid', 'worker_start_ticks',
            'service_cgroup', 'worker_cwd', 'runner_lock'}:
        raise GateError('旧 runner process binding schema 非法')
    if (type(value['schema_version']) is not int or
            value['schema_version'] != 1 or
            BOOT_ID_RE.fullmatch(value.get('boot_id') or '') is None):
        raise GateError('旧 runner process binding 字段非法')
    for key in ('worker_pid', 'worker_start_ticks'):
        if (isinstance(value[key], bool) or not isinstance(value[key], int) or
                value[key] <= 0):
            raise GateError(f'旧 runner process binding.{key} 非法')
    _canonical_path(value['worker_cwd'])
    if (not isinstance(value['service_cgroup'], str) or
            not value['service_cgroup'].startswith('/') or
            '..' in value['service_cgroup'].split('/')):
        raise GateError('旧 runner process binding cgroup 非法')
    lock = value['runner_lock']
    if not isinstance(lock, dict) or set(lock) != {'path', 'dev', 'ino'}:
        raise GateError('旧 runner process binding lock schema 非法')
    _canonical_path(lock['path'])
    for key in ('dev', 'ino'):
        if (isinstance(lock[key], bool) or not isinstance(lock[key], int) or
                lock[key] <= 0):
            raise GateError(f'旧 runner process binding lock.{key} 非法')
    return value


def _validate_handshake_boundary(value, release_sha=None):
    if not isinstance(value, dict) or set(value) != {
            'schema_version', 'mode', 'handshake', 'process_binding'}:
        raise GateError('runtime sentinel boundary schema 非法')
    if (type(value['schema_version']) is not int or
            value['schema_version'] != 1 or
            value['mode'] != 'runtime_sentinel'):
        raise GateError('runtime sentinel boundary mode 非法')
    sentinel = _validate_arm_result(
        value['handshake'], release_sha=release_sha)
    binding = _validate_process_binding(value['process_binding'])
    if sentinel['worker_pid'] != binding['worker_pid']:
        raise GateError('同锁握手 worker 不是实际 runner FLOCK 持有者')
    return value


def _validate_arm_intent(value, release_sha):
    if (not isinstance(value, dict) or set(value) != {
            'schema_version', 'kind', 'release_sha'} or
            type(value['schema_version']) is not int or
            value['schema_version'] != 1 or
            value['kind'] != 'old_runner_no_open_arm_intent' or
            value['release_sha'] != release_sha or
            SHA_RE.fullmatch(value.get('release_sha') or '') is None):
        raise GateError('runtime sentinel arm intent schema/绑定非法')
    return value


def _validate_old_sentinel_payload(payload, release_sha):
    if (not isinstance(payload, dict) or set(payload) != {
            'schema_version', 'kind', 'release_sha', 'nonce', 'worker_pid'} or
            type(payload['schema_version']) is not int or
            payload['schema_version'] != 1 or
            payload['kind'] != 'old_runner_no_open' or
            payload['release_sha'] != release_sha or
            NONCE_RE.fullmatch(payload.get('nonce') or '') is None or
            isinstance(payload.get('worker_pid'), bool) or
            not isinstance(payload.get('worker_pid'), int) or
            payload['worker_pid'] <= 0):
        raise GateError('旧 runner sentinel 持续性绑定非法')
    return payload


def _validate_release_sentinel_payload(payload, release_sha):
    if (not isinstance(payload, dict) or set(payload) != {
            'schema_version', 'nonce', 'release_sha'} or
            type(payload['schema_version']) is not int or
            payload['schema_version'] != 1 or
            payload['release_sha'] != release_sha or
            NONCE_RE.fullmatch(payload.get('nonce') or '') is None):
        raise GateError('正式 release sentinel schema/绑定非法')
    return payload


def _validate_partial_arm_object(info):
    """Accept only the inode shape published by v1 before its JSON write."""
    try:
        ubuntu = pwd.getpwnam('ubuntu')
    except KeyError as exc:
        raise GateError('无法解析旧 runner 用户') from exc
    if (not stat.S_ISREG(info.st_mode) or
            info.st_uid != ubuntu.pw_uid or info.st_gid != ubuntu.pw_gid or
            stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or
            info.st_size > MAX_JSON_BYTES):
        raise GateError('不完整旧 runner sentinel inode 属性非法')
    return info


def _verify_handshake_state(boundary, runner_lock, service_cgroup,
                            expected_cwd, current_data):
    boundary = _validate_handshake_boundary(boundary)
    expected_path = os.path.join(_canonical_path(current_data),
                                 '.maintenance_no_open')
    if os.path.realpath(current_data) != current_data:
        raise GateError('旧 runner 数据目录不是规范真实路径')
    sentinel = boundary['handshake']['sentinel']
    if sentinel['path'] != expected_path:
        raise GateError('handshake 证据未绑定当前数据目录 sentinel')
    current = capture_process_binding(
        runner_lock, service_cgroup, expected_cwd)
    if current != boundary['process_binding']:
        raise GateError('实际 runner 身份在同锁握手后发生变化')
    payload, info = _runtime_sentinel(expected_path, '旧 runner sentinel')
    expected_payload = {
        key: sentinel[key]
        for key in ('schema_version', 'kind', 'release_sha', 'nonce',
                    'worker_pid')
    }
    if payload != expected_payload:
        raise GateError('旧 runner sentinel 内容与同锁证据不一致')
    if (info.st_dev, info.st_ino) != (sentinel['dev'], sentinel['ino']):
        raise GateError('旧 runner sentinel inode 与同锁证据不一致')
    current_capability = capability()
    if current_capability != {
            'protocol': 'trade-lock-no-open-v1',
            'worker_pid': current['worker_pid'],
            'sentinel_path': expected_path,
            'maintenance_active': True,
    }:
        raise GateError('旧 runner 未持续报告同一 maintenance boundary')
    verify_http_block()
    return {
        'mode': 'runtime_sentinel',
        'worker_pid': current['worker_pid'],
        'sentinel': {
            'path': expected_path,
            'dev': info.st_dev,
            'ino': info.st_ino,
        },
    }


def probe_handshake(runner_lock, service_cgroup, expected_cwd, current_data):
    """Prove handshake compatibility without changing runner state."""
    expected_path = os.path.join(_canonical_path(current_data),
                                 '.maintenance_no_open')
    if os.path.realpath(current_data) != current_data:
        raise GateError('旧 runner 数据目录不是规范真实路径')
    before = capture_process_binding(
        runner_lock, service_cgroup, expected_cwd)
    declared = capability()
    if declared != {
            'protocol': 'trade-lock-no-open-v1',
            'worker_pid': before['worker_pid'],
            'sentinel_path': expected_path,
            'maintenance_active': False,
    }:
        raise GateError('旧 runner capability 未绑定实际 FLOCK worker/数据目录')
    after = capture_process_binding(
        runner_lock, service_cgroup, expected_cwd)
    if before != after:
        raise GateError('旧 runner 身份在 capability 探测期间发生变化')
    return {
        'protocol': declared['protocol'],
        'worker_pid': before['worker_pid'],
        'sentinel_path': expected_path,
    }


def establish_handshake(release_sha, nonce, runner_lock, service_cgroup,
                        expected_cwd, current_data):
    """Drain opens and bind the resulting sentinel to the exact lock holder."""
    if SHA_RE.fullmatch(release_sha or '') is None:
        raise GateError('release_sha 非法')
    if NONCE_RE.fullmatch(nonce or '') is None:
        raise GateError('nonce 非法')
    before_probe = probe_handshake(
        runner_lock, service_cgroup, expected_cwd, current_data)
    before = capture_process_binding(
        runner_lock, service_cgroup, expected_cwd)
    if before_probe['worker_pid'] != before['worker_pid']:
        raise GateError('旧 runner 身份在 capability 与 arm 之间发生变化')
    handshake = arm(release_sha, nonce)
    after = capture_process_binding(
        runner_lock, service_cgroup, expected_cwd)
    if before != after:
        raise GateError('旧 runner 身份在同锁握手期间发生变化')
    boundary = {
        'schema_version': 1,
        'mode': 'runtime_sentinel',
        'handshake': handshake,
        'process_binding': before,
    }
    _validate_handshake_boundary(boundary, release_sha=release_sha)
    _verify_handshake_state(
        boundary, runner_lock, service_cgroup, expected_cwd, current_data)
    return boundary


def verify_maintenance_continuity(release_sha, runner_lock, service_cgroup,
                                  expected_cwd, current_data,
                                  evidence_path=None, arm_intent_path=None):
    """Prove a restarted worker still obeys the creator's durable sentinel."""
    if SHA_RE.fullmatch(release_sha or '') is None:
        raise GateError('release_sha 非法')
    expected_path = os.path.join(
        _canonical_path(current_data), '.maintenance_no_open')
    if os.path.realpath(current_data) != current_data:
        raise GateError('旧 runner 数据目录不是规范真实路径')
    if arm_intent_path is None:
        raise GateError('持续性复验缺少 root arm intent')
    _validate_arm_intent(
        _protected_root_file(arm_intent_path, '旧 runner arm intent'),
        release_sha)
    before = capture_process_binding(
        runner_lock, service_cgroup, expected_cwd)
    object_before = _runtime_gate_object(expected_path, '旧 runner sentinel')
    payload = info = None
    old_payload = False
    try:
        payload, info = _runtime_sentinel(expected_path, '旧 runner sentinel')
    except GateError:
        if evidence_path is not None:
            raise
        # v1 intentionally publishes the final O_EXCL inode only after taking
        # the trade lock.  A crash/short write can therefore leave invalid JSON,
        # but the valid root intent plus this exact inode shape still proves the
        # pre-create in-flight boundary was drained.  Do not invent creator
        # fields; recovery will stop first and publish recovery_inactive.
        _validate_partial_arm_object(object_before)
    else:
        try:
            _validate_old_sentinel_payload(payload, release_sha)
            old_payload = True
        except GateError:
            if evidence_path is not None:
                raise
            _validate_release_sentinel_payload(payload, release_sha)
    if evidence_path is not None:
        historical = _protected_root_file(
            evidence_path, '旧 runner runtime sentinel 证据')
        _validate_handshake_boundary(historical, release_sha=release_sha)
        recorded = historical['handshake']['sentinel']
        expected_payload = {
            key: recorded[key]
            for key in ('schema_version', 'kind', 'release_sha', 'nonce',
                        'worker_pid')
        }
        if (payload != expected_payload or recorded['path'] != expected_path or
                (recorded['dev'], recorded['ino']) !=
                (info.st_dev, info.st_ino)):
            raise GateError('历史握手证据与当前持久 sentinel 不一致')
    else:
        # A creator response may be lost before the old v1 implementation
        # fsyncs the directory (notably after a short JSON write).  Persist the
        # exact observed entry now; mere current visibility is not reboot proof.
        object_before = _durabilize_runtime_gate(
            expected_path, object_before, '旧 runner sentinel')
    current_capability = capability()
    if current_capability != {
            'protocol': 'trade-lock-no-open-v1',
            'worker_pid': before['worker_pid'],
            'sentinel_path': expected_path,
            'maintenance_active': True,
    }:
        raise GateError('当前 runner 未持续报告同一 maintenance boundary')
    verify_http_block()
    after = capture_process_binding(
        runner_lock, service_cgroup, expected_cwd)
    object_after = _runtime_gate_object(expected_path, '旧 runner sentinel')
    if (after != before or
            _stable_file_metadata(object_before) !=
            _stable_file_metadata(object_after)):
        raise GateError('旧 runner 身份在持续禁开仓复验期间变化')
    return {
        'mode': 'runtime_sentinel_continuity',
        'inflight_open_boundary_drained': True,
        'strict_creator_evidence': old_payload,
        'creator_worker_pid': (
            payload['worker_pid'] if old_payload else None),
        'current_worker_pid': before['worker_pid'],
        'sentinel': {
            'path': expected_path,
            'dev': object_after.st_dev,
            'ino': object_after.st_ino,
        },
    }


def verify_persistent_old_sentinel(path, release_sha, evidence_path=None):
    if SHA_RE.fullmatch(release_sha or '') is None:
        raise GateError('release_sha 非法')
    payload, info = _runtime_sentinel(path, '旧 runner 持久 sentinel')
    _validate_old_sentinel_payload(payload, release_sha)
    if evidence_path is not None:
        historical = _protected_root_file(
            evidence_path, '旧 runner runtime sentinel 证据')
        _validate_handshake_boundary(historical, release_sha=release_sha)
        recorded = historical['handshake']['sentinel']
        if (payload != {
                key: recorded[key]
                for key in ('schema_version', 'kind', 'release_sha', 'nonce',
                            'worker_pid')} or
                recorded['path'] != path or
                (recorded['dev'], recorded['ino']) !=
                (info.st_dev, info.st_ino)):
            raise GateError('历史握手证据与持久 sentinel 不一致')
    return {
        'mode': 'old_runner_persistent_sentinel',
        'creator_worker_pid': payload['worker_pid'],
        'sentinel': {'path': path, 'dev': info.st_dev, 'ino': info.st_ino},
    }


def verify_persistent_gate_object(path):
    info = _runtime_gate_object(path, '旧 runner 持久禁开仓')
    info = _durabilize_runtime_gate(path, info, '旧 runner 持久禁开仓')
    return {
        'mode': 'old_runner_fail_closed_object',
        'sentinel': {'path': path, 'dev': info.st_dev, 'ino': info.st_ino},
    }


def verify_handshake_boundary(evidence_path, runner_lock, service_cgroup,
                              expected_cwd, current_data):
    boundary = _protected_root_file(
        evidence_path, '旧 runner runtime sentinel 证据')
    return _verify_handshake_state(
        boundary, runner_lock, service_cgroup, expected_cwd, current_data)


def verify_arm_intent(evidence_path, release_sha):
    return _validate_arm_intent(
        _protected_root_file(evidence_path, '旧 runner arm intent'),
        release_sha)


def verify_release_sentinel(path, release_sha):
    if SHA_RE.fullmatch(release_sha or '') is None:
        raise GateError('release_sha 非法')
    payload, info = _runtime_sentinel(path, '正式 release sentinel')
    _validate_release_sentinel_payload(payload, release_sha)
    return {
        'mode': 'recovery_inactive',
        'release_sha': release_sha,
        'sentinel': {'path': path, 'dev': info.st_dev, 'ino': info.st_ino},
    }


def boundary_summary(evidence_path, release_sha):
    if SHA_RE.fullmatch(release_sha or '') is None:
        raise GateError('release_sha 非法')
    value = _protected_root_file(evidence_path, '旧 runner no-open 证据')
    keys = set(value) if isinstance(value, dict) else set()
    if keys == {'schema_version', 'mode', 'handshake', 'process_binding'}:
        _validate_handshake_boundary(value, release_sha=release_sha)
        return {'mode': 'runtime_sentinel'}
    if keys == {'mode', 'release_sha', 'sentinel'}:
        sentinel = value['sentinel']
        if (value['mode'] != 'recovery_inactive' or
                value.get('release_sha') != release_sha or
                not isinstance(sentinel, dict) or
                set(sentinel) != {'path', 'dev', 'ino'}):
            raise GateError('正式 release sentinel 证据 schema 非法')
        _canonical_path(sentinel['path'])
        for field in ('dev', 'ino'):
            if (isinstance(sentinel[field], bool) or
                    not isinstance(sentinel[field], int) or
                    sentinel[field] <= 0):
                raise GateError(f'正式 release sentinel.{field} 非法')
        return {'mode': 'recovery_inactive'}
    raise GateError('旧 runner no-open 证据 schema 非法')


def _parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    probe = sub.add_parser('probe-handshake')
    probe.add_argument('--runner-lock', required=True)
    probe.add_argument('--service-cgroup', required=True)
    probe.add_argument('--expected-cwd', required=True)
    probe.add_argument('--current-data', required=True)
    establish = sub.add_parser('establish-handshake')
    establish.add_argument('--release-sha', required=True)
    establish.add_argument('--nonce', required=True)
    establish.add_argument('--runner-lock', required=True)
    establish.add_argument('--service-cgroup', required=True)
    establish.add_argument('--expected-cwd', required=True)
    establish.add_argument('--current-data', required=True)
    continuity = sub.add_parser('verify-maintenance-continuity')
    continuity.add_argument('--release-sha', required=True)
    continuity.add_argument('--runner-lock', required=True)
    continuity.add_argument('--service-cgroup', required=True)
    continuity.add_argument('--expected-cwd', required=True)
    continuity.add_argument('--current-data', required=True)
    continuity.add_argument('--arm-intent', required=True)
    continuity.add_argument('--evidence')
    drain = sub.add_parser('drain-maintenance-boundary')
    drain.add_argument('--release-sha', required=True)
    drain.add_argument('--runner-lock', required=True)
    drain.add_argument('--service-cgroup', required=True)
    drain.add_argument('--expected-cwd', required=True)
    drain.add_argument('--current-data', required=True)
    persistent = sub.add_parser('verify-persistent-old-sentinel')
    persistent.add_argument('--path', required=True)
    persistent.add_argument('--release-sha', required=True)
    persistent.add_argument('--evidence')
    gate_object = sub.add_parser('verify-persistent-gate-object')
    gate_object.add_argument('--path', required=True)
    handshake = sub.add_parser('verify-handshake')
    handshake.add_argument('--evidence', required=True)
    handshake.add_argument('--runner-lock', required=True)
    handshake.add_argument('--service-cgroup', required=True)
    handshake.add_argument('--expected-cwd', required=True)
    handshake.add_argument('--current-data', required=True)
    intent = sub.add_parser('verify-arm-intent')
    intent.add_argument('--evidence', required=True)
    intent.add_argument('--release-sha', required=True)
    release = sub.add_parser('verify-release-sentinel')
    release.add_argument('--path', required=True)
    release.add_argument('--release-sha', required=True)
    process = sub.add_parser('process-binding')
    process.add_argument('--runner-lock', required=True)
    process.add_argument('--service-cgroup', required=True)
    process.add_argument('--expected-cwd', required=True)
    summary = sub.add_parser('boundary-summary')
    summary.add_argument('--evidence', required=True)
    summary.add_argument('--release-sha', required=True)
    return parser


def main(argv=None):
    if (os.path.realpath(sys.executable) != os.path.realpath('/usr/bin/python3') or
            not sys.flags.isolated or sys.flags.no_user_site != 1):
        print('[阻断] 必须由 /usr/bin/python3 -I 执行', file=sys.stderr)
        return 2
    args = _parser().parse_args(argv)
    try:
        if args.command == 'probe-handshake':
            result = probe_handshake(
                args.runner_lock, args.service_cgroup, args.expected_cwd,
                args.current_data)
        elif args.command == 'establish-handshake':
            result = establish_handshake(
                args.release_sha, args.nonce, args.runner_lock,
                args.service_cgroup, args.expected_cwd, args.current_data)
        elif args.command == 'verify-maintenance-continuity':
            result = verify_maintenance_continuity(
                args.release_sha, args.runner_lock, args.service_cgroup,
                args.expected_cwd, args.current_data, args.evidence,
                args.arm_intent)
        elif args.command == 'drain-maintenance-boundary':
            result = drain_maintenance_boundary(
                args.release_sha, args.runner_lock, args.service_cgroup,
                args.expected_cwd, args.current_data)
        elif args.command == 'verify-persistent-old-sentinel':
            result = verify_persistent_old_sentinel(
                args.path, args.release_sha, args.evidence)
        elif args.command == 'verify-persistent-gate-object':
            result = verify_persistent_gate_object(args.path)
        elif args.command == 'verify-handshake':
            result = verify_handshake_boundary(
                args.evidence, args.runner_lock, args.service_cgroup,
                args.expected_cwd, args.current_data)
        elif args.command == 'verify-arm-intent':
            result = verify_arm_intent(args.evidence, args.release_sha)
        elif args.command == 'verify-release-sentinel':
            result = verify_release_sentinel(args.path, args.release_sha)
        elif args.command == 'process-binding':
            result = capture_process_binding(
                args.runner_lock, args.service_cgroup, args.expected_cwd)
        else:
            result = boundary_summary(args.evidence, args.release_sha)
        print(json.dumps(result, sort_keys=True, separators=(',', ':')))
        return 0
    except FileNotFoundError as exc:
        print(f'[不支持] {exc}', file=sys.stderr)
        return 3
    except GateError as exc:
        print(f'[阻断] {exc}', file=sys.stderr)
        return 2
    except Exception as exc:
        print(f'[阻断] 未预期异常（{exc.__class__.__name__}）', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
