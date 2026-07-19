#!/usr/bin/python3 -I
"""Local-only client for the old runner's same-lock no-open handshake."""

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
FINGERPRINT_RE = re.compile(r'^[0-9a-f]{64}$')
MAX_JSON_BYTES = 64 * 1024


class GateError(RuntimeError):
    """The old runner did not prove the reviewed handshake contract."""


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
    if (sentinel['schema_version'] != 1 or
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
        raw = response.read(65537)
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read(65537)
    except Exception as exc:
        raise GateError(
            f'旧 runner 本机 HTTP 不可验证: {exc.__class__.__name__}') from exc
    if status not in expected:
        if status == 404 and path.endswith('/no-open-capability'):
            raise FileNotFoundError('旧 runner 不支持 no-open handshake v1')
        raise GateError(f'旧 runner HTTP 状态异常: {status}')
    if len(raw) > 65536:
        raise GateError('旧 runner HTTP 响应过大')
    result = _loads_strict(raw, '旧 runner HTTP 响应')
    if not isinstance(result, dict):
        raise GateError('旧 runner HTTP 响应顶层不是对象')
    return status, result


def _canonical_path(value):
    if (not isinstance(value, str) or not os.path.isabs(value) or
            os.path.normpath(value) != value):
        raise GateError('旧 runner sentinel_path 不是规范绝对路径')
    return value


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


def verify_http_block():
    _status, result = _request(
        'POST', '/api/instant_open', {}, expected=(503,))
    if result.get('maintenance_no_open') is not True:
        raise GateError('旧 runner HTTP 写接口未证明 503 maintenance_no_open')
    return {'http_status': 503, 'maintenance_no_open': True}


def verify_handshake_boundary(evidence_path, current_data, service_cgroup):
    evidence = _protected_root_file(evidence_path, '旧 runner handshake 证据')
    sentinel = _validate_arm_result(evidence)
    expected_path = os.path.join(_canonical_path(current_data),
                                 '.maintenance_no_open')
    if sentinel['path'] != expected_path:
        raise GateError('handshake 证据未绑定当前数据目录 sentinel')
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
    if (not isinstance(service_cgroup, str) or
            not service_cgroup.startswith('/') or '..' in service_cgroup.split('/')):
        raise GateError('trading.service cgroup 非法')
    cgroup_procs = '/sys/fs/cgroup' + service_cgroup + '/cgroup.procs'
    try:
        with open(cgroup_procs, encoding='ascii') as handle:
            pids = {int(line.strip()) for line in handle if line.strip()}
    except (OSError, ValueError) as exc:
        raise GateError('无法验证旧 runner worker cgroup') from exc
    if sentinel['worker_pid'] not in pids:
        raise GateError('生成 handshake 的 worker 已不在 trading.service cgroup')
    http = verify_http_block()
    return {
        'mode': 'handshake',
        'protocol': evidence['protocol'],
        'sentinel': {
            'path': expected_path,
            'dev': info.st_dev,
            'ino': info.st_ino,
        },
        **http,
    }


def verify_release_sentinel(path, release_sha):
    if SHA_RE.fullmatch(release_sha or '') is None:
        raise GateError('release_sha 非法')
    payload, info = _runtime_sentinel(path, '正式 release sentinel')
    if (not isinstance(payload, dict) or set(payload) != {
            'schema_version', 'nonce', 'release_sha'} or
            payload['schema_version'] != 1 or
            payload['release_sha'] != release_sha or
            NONCE_RE.fullmatch(payload.get('nonce') or '') is None):
        raise GateError('正式 release sentinel schema/绑定非法')
    return {
        'mode': 'release_sentinel',
        'release_sha': release_sha,
        'sentinel': {'path': path, 'dev': info.st_dev, 'ino': info.st_ino},
    }


def credential_evidence(evidence_path):
    result = _protected_root_file(evidence_path, '只读 API 权限证据')
    if (not isinstance(result, dict) or set(result) != {
            'account_domain', 'api_fingerprint', 'mode', 'permissions'} or
            result['account_domain'] != 'live' or
            result['mode'] != 'read_only' or
            result['permissions'] != ['read_only'] or
            FINGERPRINT_RE.fullmatch(result.get('api_fingerprint') or '') is None):
        raise GateError('只读 API 权限证据 schema/账户域非法')
    return result


def _parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('capability')
    arm_parser = sub.add_parser('arm')
    arm_parser.add_argument('--release-sha', required=True)
    arm_parser.add_argument('--nonce', required=True)
    sub.add_parser('verify-http')
    handshake = sub.add_parser('verify-handshake')
    handshake.add_argument('--evidence', required=True)
    handshake.add_argument('--current-data', required=True)
    handshake.add_argument('--service-cgroup', required=True)
    release = sub.add_parser('verify-release-sentinel')
    release.add_argument('--path', required=True)
    release.add_argument('--release-sha', required=True)
    credential = sub.add_parser('credential-evidence')
    credential.add_argument('--evidence', required=True)
    return parser


def main(argv=None):
    if (os.path.realpath(sys.executable) != os.path.realpath('/usr/bin/python3') or
            not sys.flags.isolated or sys.flags.no_user_site != 1):
        print('[阻断] 必须由 /usr/bin/python3 -I 执行', file=sys.stderr)
        return 2
    args = _parser().parse_args(argv)
    try:
        if args.command == 'capability':
            result = capability()
        elif args.command == 'arm':
            result = arm(args.release_sha, args.nonce)
        elif args.command == 'verify-http':
            result = verify_http_block()
        elif args.command == 'verify-handshake':
            result = verify_handshake_boundary(
                args.evidence, args.current_data, args.service_cgroup)
        elif args.command == 'verify-release-sentinel':
            result = verify_release_sentinel(args.path, args.release_sha)
        else:
            result = credential_evidence(args.evidence)
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
