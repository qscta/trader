#!/usr/bin/env python3
"""Remove one explicitly reviewed config key with full before/after binding.

The release owns this executable.  Host review supplies only a credential-free
spec containing hashes and a JSON object path; arbitrary host code is never
accepted by the deployment stage.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone


SHA_RE = re.compile(r'^[0-9a-f]{40}$')
DIGEST_RE = re.compile(r'^[0-9a-f]{64}$')
KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_-]{0,63}$')
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_SPEC_BYTES = 64 * 1024
SPEC_FIELDS = {
    'schema_version', 'release_sha', 'path', 'before_sha256',
    'value_sha256', 'after_sha256', 'reason',
}
AUDIT_FIELDS = SPEC_FIELDS | {'spec_sha256', 'applied_at'}


class CleanupError(RuntimeError):
    """The reviewed one-key transform cannot be proven exactly."""


def _reject_constant(value):
    raise CleanupError(f'JSON 含非标准常量 {value!r}')


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CleanupError(f'JSON 含重复字段 {key!r}')
        result[key] = value
    return result


def _loads(raw, context):
    try:
        return json.loads(
            raw.decode('utf-8'),
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except CleanupError:
        raise
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise CleanupError(f'{context} 不是严格 UTF-8 JSON') from exc


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value):
    try:
        return json.dumps(
            value, ensure_ascii=False, indent=2, allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError, OverflowError) as exc:
        raise CleanupError('清理结果不能编码为标准 JSON') from exc


def _value_bytes(value):
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError, OverflowError) as exc:
        raise CleanupError('目标值不能规范编码') from exc


def _read_regular(path, context, max_bytes, require_mode=None):
    if not isinstance(path, str) or not os.path.isabs(path):
        raise CleanupError(f'{context}路径必须是绝对路径')
    if os.path.normpath(path) != path or os.path.realpath(path) != path:
        raise CleanupError(f'{context}路径必须规范且不得穿过符号链接')
    try:
        before = os.lstat(path)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
                before.st_size > max_bytes):
            raise CleanupError(f'{context}必须是大小受限的单链接普通文件')
        if (require_mode is not None and
                stat.S_IMODE(before.st_mode) != require_mode):
            raise CleanupError(f'{context}权限必须为 {require_mode:04o}')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except CleanupError:
        raise
    except OSError as exc:
        raise CleanupError(f'无法安全打开{context}') from exc
    try:
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
                not stat.S_ISREG(after.st_mode) or after.st_nlink != 1 or
                after.st_size > max_bytes):
            raise CleanupError(f'{context}在检查期间被替换')
        if (require_mode is not None and
                stat.S_IMODE(after.st_mode) != require_mode):
            raise CleanupError(f'{context}权限必须为 {require_mode:04o}')
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise CleanupError(f'{context}超过大小上限')
            chunks.append(chunk)
        final = os.fstat(fd)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
        ) != (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
            final.st_mode,
            final.st_uid,
            final.st_gid,
            final.st_nlink,
        ):
            raise CleanupError(f'{context}在读取期间被修改')
        return b''.join(chunks), final
    finally:
        os.close(fd)


def _validate_path(value):
    if (not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 8 or
            any(not isinstance(key, str) or KEY_RE.fullmatch(key) is None
                for key in value)):
        raise CleanupError('spec.path 必须是 1..8 个安全对象键')
    return tuple(value)


def _validate_spec(value, release_sha):
    if not isinstance(value, dict) or set(value) != SPEC_FIELDS:
        raise CleanupError('清理 spec schema 字段不精确')
    if value['schema_version'] != 1:
        raise CleanupError('清理 spec 版本不兼容')
    if value['release_sha'] != release_sha:
        raise CleanupError('清理 spec 未绑定当前 release SHA')
    if not isinstance(value['path'], list):
        raise CleanupError('spec.path 必须是 JSON 数组')
    _validate_path(value['path'])
    for key in ('before_sha256', 'value_sha256', 'after_sha256'):
        if not isinstance(value[key], str) or DIGEST_RE.fullmatch(value[key]) is None:
            raise CleanupError(f'spec.{key} 非法')
    reason = value['reason']
    if (not isinstance(reason, str) or not 1 <= len(reason) <= 200 or
            reason != reason.strip() or '\n' in reason or '\r' in reason):
        raise CleanupError('spec.reason 必须是单行非空说明')
    return value


def _remove_path(config, path):
    current = config
    for index, key in enumerate(path[:-1]):
        if not isinstance(current, dict) or key not in current:
            raise CleanupError(f'目标父路径在第 {index + 1} 层不存在')
        current = current[key]
    leaf = path[-1]
    if not isinstance(current, dict) or leaf not in current:
        raise CleanupError('经确认待删配置键不存在')
    value = current[leaf]
    del current[leaf]
    return value


def _assess(config_raw, spec):
    if _digest(config_raw) != spec['before_sha256']:
        raise CleanupError('config 原始 SHA-256 与 spec.before_sha256 不一致')
    config = _loads(config_raw, 'config')
    if not isinstance(config, dict):
        raise CleanupError('config 顶层必须是对象')
    removed = _remove_path(config, _validate_path(spec['path']))
    if _digest(_value_bytes(removed)) != spec['value_sha256']:
        raise CleanupError('目标配置值与已审查 value_sha256 不一致')
    after = _json_bytes(config)
    if _digest(after) != spec['after_sha256']:
        raise CleanupError('清理结果与 spec.after_sha256 不一致')
    return after


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_new_private_path(path, context):
    if (not isinstance(path, str) or not os.path.isabs(path) or
            os.path.normpath(path) != path):
        raise CleanupError(f'{context}路径必须是规范绝对路径')
    parent = os.path.dirname(path)
    if os.path.realpath(parent) != parent:
        raise CleanupError(f'{context}父目录不得穿过符号链接')
    try:
        info = os.lstat(parent)
    except OSError as exc:
        raise CleanupError(f'{context}父目录不可验证') from exc
    current_uid = os.geteuid() if hasattr(os, 'geteuid') else os.getuid()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != current_uid or
            stat.S_IMODE(info.st_mode) & 0o022):
        raise CleanupError(f'{context}父目录必须由当前用户保护')
    if os.path.lexists(path):
        raise CleanupError(f'{context}已存在；拒绝覆盖')


def _write_exclusive(path, raw, mode=0o600):
    _validate_new_private_path(path, '输出文件')
    parent = os.path.dirname(path)
    fd = None
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL |
            getattr(os, 'O_NOFOLLOW', 0),
            mode,
        )
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise CleanupError('证据文件写入无进展')
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(path)
            _fsync_dir(parent)
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    _fsync_dir(parent)


def _replace_config(path, raw, original):
    parent = os.path.dirname(path)
    fd = None
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.config-cleanup-', dir=parent)
        os.fchmod(fd, 0o600)
        os.fchown(fd, original.st_uid, original.st_gid)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise CleanupError('配置临时文件写入无进展')
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, path)
        temporary = None
        _fsync_dir(parent)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _read_spec(spec_path, release_sha):
    spec_raw, spec_info = _read_regular(
        spec_path, '清理 spec', MAX_SPEC_BYTES)
    mode = stat.S_IMODE(spec_info.st_mode)
    current_uid = os.geteuid() if hasattr(os, 'geteuid') else os.getuid()
    private_current_user = mode == 0o600 and spec_info.st_uid == current_uid
    public_root_owned = mode == 0o644 and spec_info.st_uid == 0
    if not (private_current_user or public_root_owned):
        raise CleanupError(
            '清理 spec 必须是当前用户拥有的 0600 文件，或 root 拥有的 '
            '0644 部署预览副本')
    return spec_raw, _validate_spec(_loads(spec_raw, '清理 spec'), release_sha)


def _read_inputs(config_path, spec_path, release_sha):
    config_raw, config_info = _read_regular(
        config_path, 'config', MAX_CONFIG_BYTES, require_mode=0o600)
    spec_raw, spec = _read_spec(spec_path, release_sha)
    return config_raw, config_info, spec_raw, spec


def _audit_payload(spec, spec_raw):
    return {
        **spec,
        'spec_sha256': _digest(spec_raw),
        'applied_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }


def _validate_applied_config(config_raw, spec):
    if _digest(config_raw) != spec['after_sha256']:
        raise CleanupError('应用后 config SHA-256 不匹配')
    config = _loads(config_raw, '应用后 config')
    current = config
    for key in spec['path'][:-1]:
        if not isinstance(current, dict) or key not in current:
            raise CleanupError('应用后目标父路径意外消失')
        current = current[key]
    if not isinstance(current, dict) or spec['path'][-1] in current:
        raise CleanupError('应用后目标键仍存在或父节点不是对象')


def check(config_path, spec_path, release_sha):
    config_raw, _info, _spec_raw, spec = _read_inputs(
        config_path, spec_path, release_sha)
    _assess(config_raw, spec)
    return {
        'status': 'ready',
        'release_sha': release_sha,
        'path': spec['path'],
        'before_sha256': spec['before_sha256'],
        'after_sha256': spec['after_sha256'],
    }


def preview_config(config_path, spec_path, release_sha):
    """返回已受审单键清理后的内存对象，绝不写入 config。"""
    config_raw, _info, _spec_raw, spec = _read_inputs(
        config_path, spec_path, release_sha)
    return _loads(_assess(config_raw, spec), '清理预览 config')


def apply(config_path, spec_path, audit_path, release_sha):
    config_raw, config_info = _read_regular(
        config_path, 'config', MAX_CONFIG_BYTES, require_mode=0o600)
    spec_raw, spec = _read_spec(spec_path, release_sha)
    if _digest(config_raw) == spec['after_sha256']:
        # os.replace 已发生、进程却在审计落盘前中断时，精确 after 哈希就是
        # 唯一可接受的恢复点。不得要求人工改回含废弃键的 preimage。
        _validate_applied_config(config_raw, spec)
        if os.path.lexists(audit_path):
            verify_applied(config_path, spec_path, audit_path, release_sha)
            return {
                'status': 'already_applied',
                'after_sha256': spec['after_sha256'],
            }
        audit = _audit_payload(spec, spec_raw)
        _write_exclusive(
            audit_path,
            (json.dumps(
                audit, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'), allow_nan=False,
            ) + '\n').encode('utf-8'),
        )
        return {
            'status': 'recovered_applied',
            'after_sha256': spec['after_sha256'],
        }
    after = _assess(config_raw, spec)
    if os.path.lexists(audit_path):
        raise CleanupError('audit 已存在；禁止覆盖或复用一次性 apply')
    _replace_config(config_path, after, config_info)
    audit = _audit_payload(spec, spec_raw)
    _write_exclusive(
        audit_path,
        (json.dumps(
            audit, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False,
        ) + '\n').encode('utf-8'),
    )
    return {'status': 'applied', 'after_sha256': spec['after_sha256']}


def verify_applied(config_path, spec_path, audit_path, release_sha):
    spec_raw, spec = _read_spec(spec_path, release_sha)
    config_raw, _ = _read_regular(
        config_path, 'config', MAX_CONFIG_BYTES, require_mode=0o600)
    _validate_applied_config(config_raw, spec)
    audit_raw, _ = _read_regular(
        audit_path, '清理 audit', MAX_SPEC_BYTES, require_mode=0o600)
    audit = _loads(audit_raw, '清理 audit')
    if not isinstance(audit, dict) or set(audit) != AUDIT_FIELDS:
        raise CleanupError('清理 audit schema 字段不精确')
    if {key: audit[key] for key in SPEC_FIELDS} != spec:
        raise CleanupError('清理 audit 与 spec 不一致')
    if audit['spec_sha256'] != _digest(spec_raw):
        raise CleanupError('清理 audit 未绑定精确 spec')
    try:
        parsed = datetime.fromisoformat(audit['applied_at'].replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CleanupError('清理 audit.applied_at 非法') from exc
    if parsed.tzinfo is None:
        raise CleanupError('清理 audit.applied_at 缺少时区')
    return {'status': 'verified', 'after_sha256': spec['after_sha256']}


def generate_spec(config_path, output_path, release_sha, path, reason):
    if SHA_RE.fullmatch(release_sha or '') is None:
        raise CleanupError('release SHA 非法')
    config_raw, _ = _read_regular(
        config_path, 'config', MAX_CONFIG_BYTES, require_mode=0o600)
    config = _loads(config_raw, 'config')
    removed = _remove_path(config, _validate_path(path))
    after = _json_bytes(config)
    spec = {
        'schema_version': 1,
        'release_sha': release_sha,
        'path': list(path),
        'before_sha256': _digest(config_raw),
        'value_sha256': _digest(_value_bytes(removed)),
        'after_sha256': _digest(after),
        'reason': reason,
    }
    _validate_spec(spec, release_sha)
    _write_exclusive(
        output_path,
        (json.dumps(
            spec, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False,
        ) + '\n').encode('utf-8'),
    )
    return {
        'status': 'generated',
        'path': list(path),
        'before_sha256': spec['before_sha256'],
        'after_sha256': spec['after_sha256'],
    }


def _parser():
    parser = argparse.ArgumentParser(
        description='一次性、全哈希绑定地删除一个已确认配置键')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true')
    action.add_argument('--apply', action='store_true')
    action.add_argument('--verify-applied', action='store_true')
    action.add_argument('--generate-spec', action='store_true')
    parser.add_argument('--config', required=True)
    parser.add_argument('--spec')
    parser.add_argument('--audit')
    parser.add_argument('--release-sha', required=True)
    parser.add_argument('--output')
    parser.add_argument('--key', action='append')
    parser.add_argument('--reason')
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if SHA_RE.fullmatch(args.release_sha or '') is None:
            raise CleanupError('release SHA 非法')
        if args.generate_spec:
            if (not args.output or not args.key or args.reason is None or
                    args.spec or args.audit):
                raise CleanupError(
                    'generate-spec 只接受 config/output/key/reason/release-sha')
            result = generate_spec(
                args.config, args.output, args.release_sha,
                tuple(args.key), args.reason)
        else:
            if (not args.spec or args.output or args.key or
                    args.reason is not None):
                raise CleanupError('check/apply/verify 参数组合非法')
            if args.check:
                if args.audit:
                    raise CleanupError('check 不接受 audit')
                result = check(args.config, args.spec, args.release_sha)
            elif args.apply:
                if not args.audit:
                    raise CleanupError('apply 必须指定 audit')
                result = apply(
                    args.config, args.spec, args.audit, args.release_sha)
            else:
                if not args.audit:
                    raise CleanupError('verify-applied 必须指定 audit')
                result = verify_applied(
                    args.config, args.spec, args.audit, args.release_sha)
        print(json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
        return 0
    except CleanupError as exc:
        print(f'[阻断] {exc}', file=sys.stderr)
        return 2
    except Exception as exc:
        print(f'[阻断] 未预期异常（{exc.__class__.__name__}）', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
