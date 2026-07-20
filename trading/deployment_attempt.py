#!/usr/bin/python3 -I
"""Write-once, root-owned deployment-attempt phase journal."""

import argparse
import ctypes
import errno
import hashlib
import json
import os
import pwd
import re
import stat
import sys
from datetime import date


SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ATTEMPT_RE = re.compile(r"^[0-9]{4}$")
FACT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PHASES = (
    "PREPARED",
    "G0",
    "RUNTIME_READY",
    "QUIESCED",
    "T0",
    "VALIDATED",
    "SEALED",
    "COMMIT_READY",
)
RECOVERY_SEED_NAME = "recovery-seed.json"
SOURCE_CONTRACT_NAME = "source-runtime.json"
ARM_INTENT_NAME = "old-no-open-arm-intent.json"
PUBLISH_NAMES = {
    ARM_INTENT_NAME, "old-no-open-boundary.json", SOURCE_CONTRACT_NAME,
}
ROOT_UID = 0
ROOT_GID = 0
RUNTIME_BASE = "/var/lib/trading-runtime"
DEPLOY_STAGE_BASE = "/var/lib/trading-deploy"
LIVE_TRADING = "/home/ubuntu/trader/trading"
RELEASE_TRADING_RE = re.compile(
    r"^/opt/trader-releases/[0-9a-f]{40}/trading$")
RUNTIME_RE = re.compile(r"^/var/lib/trading-runtime/[0-9a-f]{40}$")
SOURCE_DATA_STATES = frozenset({"requires_migration", "migration_complete"})
RUNTIME_GATE_CONTROL = frozenset({
    ".maintenance_no_open",
    "deployment_no_open_baseline.json",
    "deployment_no_open_completion.json",
})
HARD_KILL_UNITS = frozenset({
    "trading.service",
    "trading-state-backup.service",
    "cloudflared.service",
    "trading-mem-monitor.service",
})


class JournalError(RuntimeError):
    pass


def _reject_constant(value):
    raise JournalError(f"{value!r} is not finite JSON")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JournalError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _loads_strict(raw, context):
    try:
        return json.loads(
            raw.decode("utf-8"), parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys)
    except JournalError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalError(f"{context} is not strict UTF-8 JSON") from exc


def _canonical(payload):
    return (json.dumps(
        payload, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")


def _digest(payload):
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _validate_inputs(attempt_dir, release_sha, attempt_id, driver_sha256):
    if os.geteuid() != ROOT_UID:
        raise JournalError("phase journal must run as root")
    if not os.path.isabs(attempt_dir):
        raise JournalError("attempt-dir must be absolute")
    if not SHA40_RE.fullmatch(release_sha):
        raise JournalError("invalid release SHA")
    if not ATTEMPT_RE.fullmatch(attempt_id):
        raise JournalError("invalid attempt id")
    if not SHA256_RE.fullmatch(driver_sha256):
        raise JournalError("invalid driver SHA-256")


def _open_root_private_dir(path, context):
    before = os.lstat(path)
    if (not stat.S_ISDIR(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o700 or
            before.st_uid != ROOT_UID or before.st_gid != ROOT_GID):
        raise JournalError(f"{context} must be root:root 0700")
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
        getattr(os, "O_NOFOLLOW", 0))
    after = os.fstat(fd)
    if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
            not stat.S_ISDIR(after.st_mode)):
        os.close(fd)
        raise JournalError(f"{context} changed while opening")
    return fd


def _open_attempt_dir(path):
    return _open_root_private_dir(path, "attempt directory")


def _read_entry(dir_fd, name):
    before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600 or
            before.st_uid != ROOT_UID or before.st_gid != ROOT_GID or
            before.st_nlink != 1):
        raise JournalError(f"unsafe journal entry: {name}")
    fd = os.open(
        name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    try:
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise JournalError(f"journal entry changed while opening: {name}")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(map(len, chunks)) > 131072:
                raise JournalError("journal entry is too large")
        return _loads_strict(b"".join(chunks), f"journal entry {name}")
    finally:
        os.close(fd)


def _make_entry_durable(dir_fd, name):
    before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if (not stat.S_ISREG(before.st_mode) or
            stat.S_IMODE(before.st_mode) != 0o600 or
            before.st_uid != ROOT_UID or before.st_gid != ROOT_GID or
            before.st_nlink != 1):
        raise JournalError(f"unsafe journal entry: {name}")
    fd = os.open(
        name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    try:
        opened = os.fstat(fd)
        if _stable_identity(before) != _stable_identity(opened):
            raise JournalError(f"journal entry changed before fsync: {name}")
        os.fsync(fd)
        os.fsync(dir_fd)
        final = os.fstat(fd)
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if (_stable_identity(opened) != _stable_identity(final) or
                _stable_identity(final) != _stable_identity(current)):
            raise JournalError(f"journal entry changed during fsync: {name}")
    finally:
        os.close(fd)


def _link_named_noreplace(dir_fd, source, target):
    """Non-Linux fallback: publish a complete temp without replacement."""
    os.link(
        source, target, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
        follow_symlinks=False)
    os.unlink(source, dir_fd=dir_fd)


def _link_unnamed_noreplace(fd, dir_fd, target):
    libc = ctypes.CDLL(None, use_errno=True)
    linkat = getattr(libc, "linkat", None)
    if linkat is None:
        raise JournalError("linkat is unavailable")
    linkat.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_int,
    )
    linkat.restype = ctypes.c_int
    # Following the proc-fd magic link publishes the O_TMPFILE inode without
    # requiring CAP_DAC_READ_SEARCH. linkat itself is exclusive at target.
    source = os.fsencode(f"/proc/self/fd/{fd}")
    if linkat(-100, source, dir_fd, os.fsencode(target), 0x400) == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), target)
    raise OSError(error, os.strerror(error), target)


def _write_all_and_sync(fd, data):
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise JournalError("journal write made no progress")
        offset += written
    os.fsync(fd)


def _write_bytes_once(dir_fd, name, data):
    if not isinstance(data, bytes) or not data:
        raise JournalError("write-once data must be non-empty bytes")
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise JournalError(f"write-once entry already exists: {name}")
    if sys.platform.startswith("linux"):
        flags = os.O_WRONLY | getattr(os, "O_TMPFILE", 0)
        if not getattr(os, "O_TMPFILE", 0):
            raise JournalError("O_TMPFILE is unavailable")
        fd = os.open(".", flags, 0o600, dir_fd=dir_fd)
        try:
            os.fchmod(fd, 0o600)
            _write_all_and_sync(fd, data)
            _link_unnamed_noreplace(fd, dir_fd, name)
        finally:
            os.close(fd)
    else:
        pending = f".{name}.pending.{os.getpid()}"
        try:
            flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0))
            fd = os.open(pending, flags, 0o600, dir_fd=dir_fd)
            try:
                os.fchmod(fd, 0o600)
                _write_all_and_sync(fd, data)
            finally:
                os.close(fd)
            _link_named_noreplace(dir_fd, pending, name)
        finally:
            try:
                os.unlink(pending, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
    os.fsync(dir_fd)


def _write_once(dir_fd, name, payload):
    _write_bytes_once(dir_fd, name, _canonical(payload))


def _read_source_json(path):
    if (not os.path.isabs(path) or os.path.normpath(path) != path or
            os.path.realpath(path) != path):
        raise JournalError("artifact source must be a canonical real path")
    before = os.lstat(path)
    if (not stat.S_ISREG(before.st_mode) or
            stat.S_IMODE(before.st_mode) != 0o600 or
            before.st_uid != ROOT_UID or before.st_gid != ROOT_GID or
            before.st_nlink != 1 or before.st_size > 131072):
        raise JournalError("artifact source must be root:root 0600 and bounded")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise JournalError("artifact source changed while opening")
        raw = bytearray()
        while len(raw) <= 131072:
            chunk = os.read(fd, min(65536, 131073 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        final = os.fstat(fd)
        if len(raw) > 131072 or _stable_identity(opened) != _stable_identity(final):
            raise JournalError("artifact source changed or exceeds its size limit")
    finally:
        os.close(fd)

    return _loads_strict(bytes(raw), "artifact source")


def _stable_identity(info):
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
        info.st_nlink, info.st_size, info.st_mtime_ns,
    )


def _file_identity(info):
    return {"dev": info.st_dev, "ino": info.st_ino}


def _validate_identity(value, context):
    if not isinstance(value, dict) or set(value) != {"dev", "ino"}:
        raise JournalError(f"invalid {context} identity")
    for field in ("dev", "ino"):
        if (isinstance(value[field], bool) or
                not isinstance(value[field], int) or value[field] <= 0):
            raise JournalError(f"invalid {context} identity")


def _read_attempt_pointer(dir_fd, name):
    before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if (not stat.S_ISREG(before.st_mode) or
            stat.S_IMODE(before.st_mode) != 0o600 or
            before.st_uid != ROOT_UID or before.st_gid != ROOT_GID or
            before.st_nlink != 1 or before.st_size != 5):
        raise JournalError(f"unsafe attempt pointer: {name}")
    fd = os.open(
        name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    try:
        opened = os.fstat(fd)
        raw = bytearray()
        while len(raw) < 6:
            chunk = os.read(fd, 6 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        final = os.fstat(fd)
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if (_stable_identity(before) != _stable_identity(opened) or
                _stable_identity(opened) != _stable_identity(final) or
                _stable_identity(final) != _stable_identity(current)):
            raise JournalError(f"attempt pointer changed while reading: {name}")
    finally:
        os.close(fd)
    raw = bytes(raw)
    if len(raw) != 5 or raw[4:] != b"\n":
        raise JournalError(f"invalid attempt pointer: {name}")
    try:
        attempt_id = raw[:4].decode("ascii")
    except UnicodeDecodeError as exc:
        raise JournalError(f"invalid attempt pointer: {name}") from exc
    if not ATTEMPT_RE.fullmatch(attempt_id) or attempt_id == "0000":
        raise JournalError(f"invalid attempt pointer: {name}")
    return attempt_id


def switch_active_attempt(release_sha, expected_attempt, next_attempt):
    """Durably replace ACTIVE through one complete, retryable pending inode."""
    if os.geteuid() != ROOT_UID or not SHA40_RE.fullmatch(release_sha or ""):
        raise JournalError("active-attempt switch requires root and a release SHA")
    if (not ATTEMPT_RE.fullmatch(expected_attempt or "") or
            not ATTEMPT_RE.fullmatch(next_attempt or "") or
            expected_attempt == "0000" or next_attempt == "0000" or
            int(next_attempt) != int(expected_attempt) + 1):
        raise JournalError("active-attempt transition is invalid")
    stage = os.path.join(DEPLOY_STAGE_BASE, release_sha)
    if os.path.realpath(stage) != stage:
        raise JournalError("deployment stage must be canonical")
    dir_fd = _open_root_private_dir(stage, "deployment stage")
    pending_name = ".active-attempt.pending"
    active_name = "active-attempt"
    try:
        active = _read_attempt_pointer(dir_fd, active_name)
        try:
            pending = _read_attempt_pointer(dir_fd, pending_name)
        except FileNotFoundError:
            pending = None
        if active == next_attempt:
            if pending is not None:
                if pending != next_attempt:
                    raise JournalError("stale active-attempt pending value")
                os.unlink(pending_name, dir_fd=dir_fd)
                os.fsync(dir_fd)
            _make_entry_durable(dir_fd, active_name)
            return {"active_attempt": next_attempt}
        if active != expected_attempt:
            raise JournalError("active-attempt does not match expected value")
        if pending is None:
            _write_bytes_once(
                dir_fd, pending_name, f"{next_attempt}\n".encode("ascii"))
        elif pending != next_attempt:
            raise JournalError("stale active-attempt pending value")
        _make_entry_durable(dir_fd, pending_name)
        if (_read_attempt_pointer(dir_fd, active_name) != expected_attempt or
                _read_attempt_pointer(dir_fd, pending_name) != next_attempt):
            raise JournalError("active-attempt transition changed before replace")
        os.replace(
            pending_name, active_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        # This is deliberately the first syscall after rename.  On a crash,
        # retry observes either the old ACTIVE plus complete pending or new ACTIVE.
        os.fsync(dir_fd)
        if _read_attempt_pointer(dir_fd, active_name) != next_attempt:
            raise JournalError("active-attempt replacement did not persist")
        return {"active_attempt": next_attempt}
    finally:
        os.close(dir_fd)


def _source_directory(path, context):
    if (not isinstance(path, str) or not os.path.isabs(path) or
            os.path.normpath(path) != path or os.path.realpath(path) != path):
        raise JournalError(f"{context} must be a canonical real path")
    before = os.lstat(path)
    if not stat.S_ISDIR(before.st_mode):
        raise JournalError(f"{context} must be a real directory")
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
        getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if ((before.st_dev, before.st_ino) !=
                (opened.st_dev, opened.st_ino) or
                not stat.S_ISDIR(opened.st_mode)):
            raise JournalError(f"{context} changed while opening")
        return _file_identity(opened)
    finally:
        os.close(fd)


def _validate_completed_schedule_slot(value):
    if not isinstance(value, str):
        raise JournalError("completed schedule slot must be a date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise JournalError("completed schedule slot must be a date") from exc
    if parsed.isoformat() != value:
        raise JournalError("completed schedule slot is not canonical")
    if parsed > date.today():
        raise JournalError("completed schedule slot cannot be in the future")
    return value


def describe_source(source_trading, source_data, completed_schedule_slot,
                    data_state, runtime_user="ubuntu"):
    """Bind the stopped pre-deployment source to stable filesystem objects."""
    if os.geteuid() != ROOT_UID:
        raise JournalError("describe-source must run as root")
    if not (source_trading == LIVE_TRADING or
            RELEASE_TRADING_RE.fullmatch(source_trading or "")):
        raise JournalError("source trading path is outside the reviewed roots")
    if not (source_data == source_trading or
            RUNTIME_RE.fullmatch(source_data or "")):
        raise JournalError("source data path is outside the reviewed roots")
    _validate_completed_schedule_slot(completed_schedule_slot)
    if data_state not in SOURCE_DATA_STATES:
        raise JournalError("source data state is invalid")
    assert_no_mounts([source_trading, source_data])
    trading_identity = _source_directory(source_trading, "source trading")
    data_identity = _source_directory(source_data, "source data")
    runner_lock = os.path.join(source_data, ".runtime", "runner.lock")
    try:
        account = pwd.getpwnam(runtime_user)
        before = os.lstat(runner_lock)
        if (not stat.S_ISREG(before.st_mode) or
                before.st_uid != account.pw_uid or
                before.st_gid != account.pw_gid or
                stat.S_IMODE(before.st_mode) != 0o600 or
                before.st_nlink != 1 or os.path.realpath(runner_lock) != runner_lock):
            raise JournalError("source runner lock is unsafe")
        fd = os.open(
            runner_lock, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except KeyError as exc:
        raise JournalError("runtime user does not exist") from exc
    try:
        opened = os.fstat(fd)
        if _stable_identity(before) != _stable_identity(opened):
            raise JournalError("source runner lock changed while opening")
        lock_identity = _file_identity(opened)
    finally:
        os.close(fd)
    return {
        "schema_version": 1,
        "source_trading": source_trading,
        "source_data": source_data,
        "source_trading_identity": trading_identity,
        "source_data_identity": data_identity,
        "runner_lock": {"path": runner_lock, **lock_identity},
        "completed_schedule_slot": completed_schedule_slot,
        "data_state": data_state,
    }


def _validate_source_contract(payload):
    expected = {
        "schema_version", "source_trading", "source_data",
        "source_trading_identity", "source_data_identity", "runner_lock",
        "completed_schedule_slot", "data_state",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise JournalError("source contract schema is not exact")
    if (type(payload["schema_version"]) is not int or
            payload["schema_version"] != 1):
        raise JournalError("source contract schema version is invalid")
    _validate_identity(payload["source_trading_identity"], "source trading")
    _validate_identity(payload["source_data_identity"], "source data")
    _validate_completed_schedule_slot(payload["completed_schedule_slot"])
    if payload["data_state"] not in SOURCE_DATA_STATES:
        raise JournalError("source data state is invalid")
    lock = payload["runner_lock"]
    if not isinstance(lock, dict) or set(lock) != {"path", "dev", "ino"}:
        raise JournalError("source runner-lock schema is not exact")
    _validate_identity({"dev": lock["dev"], "ino": lock["ino"]},
                       "source runner-lock")
    if lock["path"] != os.path.join(
            payload["source_data"], ".runtime", "runner.lock"):
        raise JournalError("source runner-lock path is inconsistent")
    current = describe_source(
        payload["source_trading"], payload["source_data"],
        payload["completed_schedule_slot"], payload["data_state"])
    if current != payload:
        raise JournalError("source contract filesystem identity changed")
    return payload


def publish_artifact(attempt_dir, release_sha, attempt_id, driver_sha256,
                     name, source):
    _validate_inputs(attempt_dir, release_sha, attempt_id, driver_sha256)
    if name not in PUBLISH_NAMES:
        raise JournalError("artifact name is not approved")
    payload = _read_source_json(source)
    if name == SOURCE_CONTRACT_NAME:
        _validate_source_contract(payload)
    dir_fd = _open_attempt_dir(attempt_dir)
    try:
        chain, _, abandoned = _load_chain(
            dir_fd, release_sha, attempt_id, driver_sha256)
        if abandoned or not chain or chain[-1]["phase"] != "PREPARED":
            raise JournalError("G0 artifact requires an active PREPARED attempt")
        _write_once(dir_fd, name, payload)
        return _read_entry(dir_fd, name)
    finally:
        os.close(dir_fd)


def read_source_contract(attempt_dir, release_sha, attempt_id, driver_sha256):
    _validate_inputs(attempt_dir, release_sha, attempt_id, driver_sha256)
    dir_fd = _open_attempt_dir(attempt_dir)
    try:
        return _validate_source_contract(
            _read_entry(dir_fd, SOURCE_CONTRACT_NAME))
    finally:
        os.close(dir_fd)


def sync_paths(paths):
    if os.geteuid() != ROOT_UID or not paths:
        raise JournalError("sync-paths requires root and at least one path")
    for path in paths:
        if (not os.path.isabs(path) or os.path.normpath(path) != path or
                os.path.realpath(path) != path):
            raise JournalError("sync path must be canonical and real")
        before = os.lstat(path)
        if not (stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode)):
            raise JournalError("sync path must be a regular file or directory")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if stat.S_ISDIR(before.st_mode):
            flags |= getattr(os, "O_DIRECTORY", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if ((before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or
                    stat.S_IFMT(before.st_mode) != stat.S_IFMT(opened.st_mode)):
                raise JournalError("sync path changed while opening")
            os.fsync(fd)
        finally:
            os.close(fd)
    return {"synced": paths}


def _mount_points():
    if not sys.platform.startswith("linux"):
        return []
    try:
        with open("/proc/self/mountinfo", encoding="ascii") as handle:
            lines = handle.readlines()
    except OSError as exc:
        raise JournalError("cannot inspect the Linux mount table") from exc

    def unescape(value):
        return re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            value,
        )

    result = []
    for line in lines:
        fields = line.rstrip("\n").split(" ")
        if len(fields) < 6:
            raise JournalError("malformed Linux mount table")
        result.append(unescape(fields[4]))
    return result


def assert_no_mounts(paths):
    """Reject bind/submount deletion and traversal below reviewed state roots."""
    if os.geteuid() != ROOT_UID or not paths:
        raise JournalError("assert-no-mounts requires root and at least one path")
    roots = []
    for path in paths:
        if (not isinstance(path, str) or not os.path.isabs(path) or
                os.path.normpath(path) != path or os.path.realpath(path) != path):
            raise JournalError("mount-check path must be canonical and real")
        if not (path == LIVE_TRADING or RELEASE_TRADING_RE.fullmatch(path) or
                RUNTIME_RE.fullmatch(path)):
            raise JournalError("mount-check path is outside reviewed roots")
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise JournalError("mount-check path must be a real directory")
        roots.append(path)
    for root in roots:
        _reject_mounts_under_path(root)
    return {"mount_free": roots}


def _reject_mounts_under_path(root):
    for mount_point in _mount_points():
        if mount_point == root or mount_point.startswith(root + os.sep):
            raise JournalError(
                f"reviewed state root contains a mount point: {mount_point}")


def kill_bound_cgroup(unit, cgroup, cgroup_root="/sys/fs/cgroup"):
    """Use cgroup v2's atomic subtree kill after systemd kill failed."""
    if os.geteuid() != ROOT_UID or not sys.platform.startswith("linux"):
        raise JournalError("kill-bound-cgroup requires Linux root")
    if (unit not in HARD_KILL_UNITS or not isinstance(cgroup, str) or
            not cgroup.startswith("/") or os.path.normpath(cgroup) != cgroup or
            os.path.basename(cgroup) != unit or "\n" in cgroup or
            not isinstance(cgroup_root, str) or
            not os.path.isabs(cgroup_root) or
            os.path.normpath(cgroup_root) != cgroup_root or
            os.path.realpath(cgroup_root) != cgroup_root):
        raise JournalError("invalid bound cgroup identity")
    cgroup_path = os.path.join(cgroup_root, cgroup.lstrip("/"))
    if (os.path.commonpath((cgroup_root, cgroup_path)) != cgroup_root or
            os.path.realpath(cgroup_path) != cgroup_path):
        raise JournalError("bound cgroup escapes the unified hierarchy")
    try:
        before = os.lstat(cgroup_path)
    except OSError as exc:
        raise JournalError("bound cgroup is unavailable") from exc
    if not stat.S_ISDIR(before.st_mode) or before.st_uid != ROOT_UID:
        raise JournalError("unsafe bound cgroup directory")
    directory_fd = os.open(
        cgroup_path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
        getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened_dir = os.fstat(directory_fd)
        if ((before.st_dev, before.st_ino) !=
                (opened_dir.st_dev, opened_dir.st_ino) or
                not stat.S_ISDIR(opened_dir.st_mode)):
            raise JournalError("bound cgroup changed while opening")
        try:
            kill_before = os.stat(
                "cgroup.kill", dir_fd=directory_fd, follow_symlinks=False)
            if (not stat.S_ISREG(kill_before.st_mode) or
                    kill_before.st_uid != ROOT_UID):
                raise JournalError("unsafe cgroup.kill control file")
            kill_fd = os.open(
                "cgroup.kill",
                os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise JournalError("cgroup v2 atomic kill is unavailable") from exc
        try:
            kill_opened = os.fstat(kill_fd)
            if ((kill_before.st_dev, kill_before.st_ino) !=
                    (kill_opened.st_dev, kill_opened.st_ino) or
                    not stat.S_ISREG(kill_opened.st_mode)):
                raise JournalError("cgroup.kill changed while opening")
            payload = b"1"
            offset = 0
            while offset < len(payload):
                written = os.write(kill_fd, payload[offset:])
                if written <= 0:
                    raise JournalError("short write to cgroup.kill")
                offset += written
        finally:
            os.close(kill_fd)
    finally:
        os.close(directory_fd)
    return {"unit": unit, "cgroup": cgroup, "killed": True}


def _open_checked_directory(path, uid, gid, mode, context):
    before = os.lstat(path)
    if (not stat.S_ISDIR(before.st_mode) or
            before.st_uid != uid or before.st_gid != gid or
            stat.S_IMODE(before.st_mode) != mode or
            os.path.realpath(path) != path):
        raise JournalError(f"unsafe {context}")
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
        getattr(os, "O_NOFOLLOW", 0))
    after = os.fstat(fd)
    if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
            not stat.S_ISDIR(after.st_mode)):
        os.close(fd)
        raise JournalError(f"{context} changed while opening")
    return fd


def _ensure_directory(path, uid, gid, mode, context):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        os.mkdir(path, mode)
        os.chown(path, uid, gid, follow_symlinks=False)
        os.chmod(path, mode, follow_symlinks=False)
    else:
        exact = (
            stat.S_ISDIR(info.st_mode) and info.st_uid == uid and
            info.st_gid == gid and stat.S_IMODE(info.st_mode) == mode and
            os.path.realpath(path) == path)
        if not exact:
            # Close only the helper's own mkdir -> chown/chmod crash window.
            # The candidate must be a canonical, empty, non-writable directory
            # owned either by root (pre-chown) or by its final identity.
            with os.scandir(path) as entries:
                is_empty = next(entries, None) is None
            if (not stat.S_ISDIR(info.st_mode) or
                    os.path.realpath(path) != path or
                    info.st_uid not in {ROOT_UID, uid} or
                    info.st_gid not in {ROOT_GID, gid} or
                    stat.S_IMODE(info.st_mode) & 0o022 or
                    not is_empty):
                raise JournalError(f"unsafe {context}")
            os.chown(path, uid, gid, follow_symlinks=False)
            os.chmod(path, mode, follow_symlinks=False)
    return _open_checked_directory(path, uid, gid, mode, context)


def init_runtime(release_sha, base=RUNTIME_BASE, runtime_user="ubuntu"):
    """Create the release runtime/runner-lock boundary and make it durable."""
    if os.geteuid() != ROOT_UID:
        raise JournalError("init-runtime must run as root")
    if SHA40_RE.fullmatch(release_sha or "") is None:
        raise JournalError("invalid release SHA")
    if (not os.path.isabs(base) or os.path.normpath(base) != base or
            os.path.realpath(os.path.dirname(base)) != os.path.dirname(base)):
        raise JournalError("runtime base must have a canonical real parent")
    try:
        account = pwd.getpwnam(runtime_user)
    except KeyError as exc:
        raise JournalError("runtime user does not exist") from exc

    base_parent = os.path.dirname(base)
    base_fd = target_fd = private_fd = lock_fd = None
    target = os.path.join(base, release_sha)
    private = os.path.join(target, ".runtime")
    lock = os.path.join(private, "runner.lock")
    # A canonical path can still be a bind mount. Refuse before any crash-window
    # ownership repair or runner-lock creation can mutate a mounted target.
    _reject_mounts_under_path(target)
    try:
        base_fd = _ensure_directory(
            base, ROOT_UID, ROOT_GID, 0o755, "runtime base")
        target_fd = _ensure_directory(
            target, account.pw_uid, account.pw_gid, 0o710,
            "release runtime")
        private_fd = _ensure_directory(
            private, account.pw_uid, account.pw_gid, 0o700,
            "runner-lock directory")
        _reject_mounts_under_path(target)
        try:
            before = os.lstat(lock)
        except FileNotFoundError:
            flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0))
            lock_fd = os.open(lock, flags, 0o600)
            os.fchown(lock_fd, account.pw_uid, account.pw_gid)
            os.fchmod(lock_fd, 0o600)
        else:
            exact = (
                stat.S_ISREG(before.st_mode) and
                before.st_uid == account.pw_uid and
                before.st_gid == account.pw_gid and
                stat.S_IMODE(before.st_mode) == 0o600 and
                before.st_nlink == 1 and os.path.realpath(lock) == lock)
            if not exact:
                if (not stat.S_ISREG(before.st_mode) or
                        before.st_uid not in {ROOT_UID, account.pw_uid} or
                        before.st_gid not in {ROOT_GID, account.pw_gid} or
                        stat.S_IMODE(before.st_mode) & 0o077 or
                        before.st_nlink != 1 or before.st_size != 0 or
                        os.path.realpath(lock) != lock):
                    raise JournalError("unsafe runner lock")
            lock_fd = os.open(
                lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            opened = os.fstat(lock_fd)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise JournalError("runner lock changed while opening")
            if not exact:
                os.fchown(lock_fd, account.pw_uid, account.pw_gid)
                os.fchmod(lock_fd, 0o600)
        os.fsync(lock_fd)
        for fd in (private_fd, target_fd, base_fd):
            os.fsync(fd)
        parent_fd = os.open(
            base_parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
            getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        for fd in (lock_fd, private_fd, target_fd, base_fd):
            if fd is not None:
                os.close(fd)
    return {"runtime": target, "runner_lock": lock}


def _tree_record(hasher, kind, relative, info, content_sha256=b""):
    # Directory st_size is an allocation detail and can remain expanded after
    # an otherwise fully rolled-back create/unlink. Logical tree identity is
    # its path, ownership, mode and child records; only files contribute size.
    logical_size = info.st_size if stat.S_ISREG(info.st_mode) else 0
    fields = (
        kind,
        os.fsencode(relative),
        f"{stat.S_IMODE(info.st_mode):o}".encode("ascii"),
        str(info.st_uid).encode("ascii"),
        str(info.st_gid).encode("ascii"),
        str(logical_size).encode("ascii"),
        content_sha256,
    )
    for field in fields:
        hasher.update(len(field).to_bytes(8, "big"))
        hasher.update(field)


def _is_current_abandon_control(name, attempt_id):
    if name == f"deployment_abandon_{attempt_id}.json":
        return True
    return name in {
        f".abandoned.{attempt_id}.{control}"
        for control in RUNTIME_GATE_CONTROL
    }


def _omit_current_gate_control(name, attempt_id, gate_state):
    abandoned_control = _is_current_abandon_control(name, attempt_id)
    if gate_state == "active" and abandoned_control:
        raise JournalError(
            "active runtime contains current-attempt abandon control")
    return name in RUNTIME_GATE_CONTROL or abandoned_control


def _scan_tree(root, root_info, sync, attempt_id, gate_state):
    """Hash one strict tree pass, optionally making every inode durable."""
    hasher = hashlib.sha256()
    directories = []
    for current, names, files in os.walk(root, topdown=True, followlinks=False):
        names.sort(key=os.fsencode)
        files.sort(key=os.fsencode)
        current_info = os.lstat(current)
        if (not stat.S_ISDIR(current_info.st_mode) or
                current_info.st_dev != root_info.st_dev or
                os.path.realpath(current) != current):
            raise JournalError("sync-tree contains an unsafe directory")
        relative = os.path.relpath(current, root)
        _tree_record(
            hasher, b"directory", "" if relative == "." else relative,
            current_info)
        directories.append((current, current_info))
        for name in names:
            if (current == root and
                    (name in RUNTIME_GATE_CONTROL or
                     _is_current_abandon_control(name, attempt_id))):
                raise JournalError("gate control must be a regular file")
            info = os.lstat(os.path.join(current, name))
            if (not stat.S_ISDIR(info.st_mode) or
                    info.st_dev != root_info.st_dev):
                raise JournalError("sync-tree contains a symlink or mount")
        for name in files:
            path = os.path.join(current, name)
            post_ready_evidence = (
                current == root and
                _omit_current_gate_control(name, attempt_id, gate_state))
            before = os.lstat(path)
            if (not stat.S_ISREG(before.st_mode) or
                    before.st_dev != root_info.st_dev or before.st_nlink != 1):
                raise JournalError("sync-tree contains a non-regular file")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(fd)
                if _stable_identity(before) != _stable_identity(opened):
                    raise JournalError("sync-tree file changed while opening")
                if sync:
                    os.fsync(fd)
                content = hashlib.sha256()
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    content.update(chunk)
                final = os.fstat(fd)
                if _stable_identity(opened) != _stable_identity(final):
                    raise JournalError("sync-tree file changed while reading")
                if not post_ready_evidence:
                    _tree_record(
                        hasher, b"file", os.path.relpath(path, root), final,
                        content.hexdigest().encode("ascii"))
            finally:
                os.close(fd)
    if sync:
        for path, before in reversed(directories):
            fd = os.open(
                path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(fd)
                if _stable_identity(before) != _stable_identity(opened):
                    raise JournalError("sync-tree directory changed before fsync")
                os.fsync(fd)
                if _stable_identity(opened) != _stable_identity(os.fstat(fd)):
                    raise JournalError("sync-tree directory changed during fsync")
            finally:
                os.close(fd)
    return hasher.hexdigest()


def sync_tree(root, attempt_id, gate_state):
    """Durably hash a stopped runtime's stable payload.

    The current attempt's sentinel, later baseline/completion, abandon audit and
    archives are still structurally checked and fsynced, but omitted from this
    payload digest. Their independent gate state machine validates them, while
    the attempt id keeps prior cycles inside the stable payload hash.
    """
    if os.geteuid() != ROOT_UID:
        raise JournalError("sync-tree must run as root")
    if not isinstance(attempt_id, str) or not ATTEMPT_RE.fullmatch(attempt_id):
        raise JournalError("sync-tree requires a four-digit attempt id")
    if gate_state not in {"active", "abandoned"}:
        raise JournalError("sync-tree gate state is invalid")
    if (not os.path.isabs(root) or os.path.normpath(root) != root or
            os.path.realpath(root) != root):
        raise JournalError("sync-tree root must be canonical and real")
    root_info = os.lstat(root)
    if not stat.S_ISDIR(root_info.st_mode):
        raise JournalError("sync-tree root must be a real directory")
    _reject_mounts_under_path(root)
    durable_digest = _scan_tree(
        root, root_info, sync=True, attempt_id=attempt_id,
        gate_state=gate_state)
    _reject_mounts_under_path(root)
    verified_digest = _scan_tree(
        root, root_info, sync=False, attempt_id=attempt_id,
        gate_state=gate_state)
    _reject_mounts_under_path(root)
    if durable_digest != verified_digest:
        raise JournalError("sync-tree changed between durability and verify passes")
    return {"synced_tree": root, "tree_sha256": durable_digest}


def _phase_name(sequence, phase):
    return f"phase-{sequence:02d}-{phase.lower()}.json"


def _parse_facts(values):
    result = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not FACT_RE.fullmatch(key) or key in result:
            raise JournalError("facts must be unique lower-case key=value pairs")
        if not item or len(item) > 1024 or "\x00" in item:
            raise JournalError("invalid fact value")
        result[key] = item
    return result


def _validate_entry(payload, release_sha, attempt_id, driver_sha256,
                    sequence, phase, previous_sha256):
    expected = {
        "schema_version", "release_sha", "attempt_id", "driver_sha256",
        "sequence", "phase", "previous_sha256", "facts",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise JournalError("journal schema is not exact")
    if (type(payload["schema_version"]) is not int or
            payload["schema_version"] != 1 or
            type(payload["sequence"]) is not int or
            payload["sequence"] != sequence):
        raise JournalError("journal integer fields are invalid")
    if payload != {
            **payload,
            "schema_version": 1,
            "release_sha": release_sha,
            "attempt_id": attempt_id,
            "driver_sha256": driver_sha256,
            "sequence": sequence,
            "phase": phase,
            "previous_sha256": previous_sha256}:
        raise JournalError("journal binding mismatch")
    facts = payload["facts"]
    if (not isinstance(facts, dict) or
            any(not isinstance(k, str) or not FACT_RE.fullmatch(k) or
                not isinstance(v, str) or not v or len(v) > 1024 or "\x00" in v
                for k, v in facts.items())):
        raise JournalError("invalid journal facts")


def _load_chain(dir_fd, release_sha, attempt_id, driver_sha256):
    names = os.listdir(dir_fd)
    unknown = [name for name in names if name.startswith("phase-") and
               name not in {_phase_name(i, phase)
                            for i, phase in enumerate(PHASES)}]
    if unknown:
        raise JournalError("unknown phase journal entry")
    chain = []
    previous = None
    gap = False
    for sequence, phase in enumerate(PHASES):
        name = _phase_name(sequence, phase)
        if name not in names:
            gap = True
            continue
        if gap:
            raise JournalError("phase journal has a gap")
        payload = _read_entry(dir_fd, name)
        _validate_entry(
            payload, release_sha, attempt_id, driver_sha256,
            sequence, phase, previous)
        previous = _digest(payload)
        chain.append(payload)
    if "abandoned.json" in names:
        abandoned = _read_entry(dir_fd, "abandoned.json")
        expected = {
            "schema_version", "release_sha", "attempt_id", "driver_sha256",
            "last_phase", "last_phase_sha256", "reason",
        }
        if (not isinstance(abandoned, dict) or set(abandoned) != expected or
                type(abandoned["schema_version"]) is not int or
                abandoned["schema_version"] != 1 or
                abandoned["release_sha"] != release_sha or
                abandoned["attempt_id"] != attempt_id or
                abandoned["driver_sha256"] != driver_sha256 or
                abandoned["last_phase"] != (chain[-1]["phase"] if chain else None) or
                abandoned["last_phase_sha256"] != previous or
                not isinstance(abandoned["reason"], str) or
                not abandoned["reason"] or len(abandoned["reason"]) > 1024):
            raise JournalError("invalid abandoned journal entry")
    return chain, previous, "abandoned.json" in names


def init_journal(attempt_dir, release_sha, attempt_id, driver_sha256):
    _validate_inputs(attempt_dir, release_sha, attempt_id, driver_sha256)
    dir_fd = _open_attempt_dir(attempt_dir)
    try:
        chain, _, abandoned = _load_chain(
            dir_fd, release_sha, attempt_id, driver_sha256)
        if abandoned:
            raise JournalError("attempt is abandoned")
        payload = {
            "schema_version": 1,
            "release_sha": release_sha,
            "attempt_id": attempt_id,
            "driver_sha256": driver_sha256,
            "sequence": 0,
            "phase": "PREPARED",
            "previous_sha256": None,
            "facts": {},
        }
        if chain:
            raise JournalError("attempt already started; use recover-deployment")
        _write_once(dir_fd, _phase_name(0, "PREPARED"), payload)
        return payload
    finally:
        os.close(dir_fd)


def advance_journal(attempt_dir, release_sha, attempt_id, driver_sha256,
                    expected, next_phase, facts):
    _validate_inputs(attempt_dir, release_sha, attempt_id, driver_sha256)
    if expected not in PHASES or next_phase not in PHASES:
        raise JournalError("unknown phase")
    if PHASES.index(next_phase) != PHASES.index(expected) + 1:
        raise JournalError("phase transition is not consecutive")
    dir_fd = _open_attempt_dir(attempt_dir)
    try:
        chain, previous, abandoned = _load_chain(
            dir_fd, release_sha, attempt_id, driver_sha256)
        if abandoned or not chain or chain[-1]["phase"] != expected:
            raise JournalError("current phase does not match --expect")
        sequence = PHASES.index(next_phase)
        payload = {
            "schema_version": 1,
            "release_sha": release_sha,
            "attempt_id": attempt_id,
            "driver_sha256": driver_sha256,
            "sequence": sequence,
            "phase": next_phase,
            "previous_sha256": previous,
            "facts": _parse_facts(facts),
        }
        _write_once(dir_fd, _phase_name(sequence, next_phase), payload)
        return payload
    finally:
        os.close(dir_fd)


def abandon_journal(attempt_dir, release_sha, attempt_id, driver_sha256, reason):
    _validate_inputs(attempt_dir, release_sha, attempt_id, driver_sha256)
    if not reason or len(reason) > 1024 or "\x00" in reason:
        raise JournalError("invalid abandon reason")
    dir_fd = _open_attempt_dir(attempt_dir)
    try:
        chain, previous, abandoned = _load_chain(
            dir_fd, release_sha, attempt_id, driver_sha256)
        payload = {
            "schema_version": 1,
            "release_sha": release_sha,
            "attempt_id": attempt_id,
            "driver_sha256": driver_sha256,
            "last_phase": chain[-1]["phase"] if chain else None,
            "last_phase_sha256": previous,
            "reason": reason,
        }
        if abandoned:
            if _read_entry(dir_fd, "abandoned.json") != payload:
                raise JournalError("attempt was abandoned with different evidence")
            _make_entry_durable(dir_fd, "abandoned.json")
            if _read_entry(dir_fd, "abandoned.json") != payload:
                raise JournalError("abandon evidence changed during durability retry")
            return payload
        _write_once(dir_fd, "abandoned.json", payload)
        return payload
    finally:
        os.close(dir_fd)


def read_journal_status(attempt_dir, release_sha, attempt_id, driver_sha256):
    _validate_inputs(attempt_dir, release_sha, attempt_id, driver_sha256)
    dir_fd = _open_attempt_dir(attempt_dir)
    try:
        chain, digest, abandoned_state = _load_chain(
            dir_fd, release_sha, attempt_id, driver_sha256)
    finally:
        os.close(dir_fd)
    return {
        "phase": chain[-1]["phase"] if chain else None,
        "phase_sha256": digest,
        "abandoned": abandoned_state,
        "g0_facts": next(
            (entry["facts"] for entry in chain if entry["phase"] == "G0"),
            None),
        "runtime_ready_facts": next(
            (entry["facts"] for entry in chain
             if entry["phase"] == "RUNTIME_READY"), None),
    }


def _validate_recovery_seed(payload, release_sha, attempt_id, driver_sha256):
    expected = {
        "schema_version", "release_sha", "attempt_id", "driver_sha256",
        "entry_state", "previous_attempt_id", "previous_phase",
        "previous_phase_sha256", "source",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise JournalError("recovery seed schema is not exact")
    if (type(payload["schema_version"]) is not int or
            payload["schema_version"] != 1 or
            payload["release_sha"] != release_sha or
            payload["attempt_id"] != attempt_id or
            payload["driver_sha256"] != driver_sha256 or
            payload["entry_state"] != "inactive_no_open" or
            not ATTEMPT_RE.fullmatch(payload["previous_attempt_id"]) or
            payload["previous_attempt_id"] == attempt_id or
            payload["previous_phase"] not in PHASES or
            not SHA256_RE.fullmatch(payload["previous_phase_sha256"] or "")):
        raise JournalError("recovery seed binding is invalid")
    _validate_source_contract(payload["source"])
    return payload


def create_recovery_seed(attempt_dir, release_sha, attempt_id, driver_sha256,
                         previous_attempt_id, previous_phase,
                         previous_phase_sha256, source_trading, source_data,
                         completed_schedule_slot, data_state):
    _validate_inputs(attempt_dir, release_sha, attempt_id, driver_sha256)
    payload = {
        "schema_version": 1,
        "release_sha": release_sha,
        "attempt_id": attempt_id,
        "driver_sha256": driver_sha256,
        "entry_state": "inactive_no_open",
        "previous_attempt_id": previous_attempt_id,
        "previous_phase": previous_phase,
        "previous_phase_sha256": previous_phase_sha256,
        "source": describe_source(
            source_trading, source_data, completed_schedule_slot, data_state),
    }
    _validate_recovery_seed(payload, release_sha, attempt_id, driver_sha256)
    dir_fd = _open_attempt_dir(attempt_dir)
    try:
        chain, _, abandoned = _load_chain(
            dir_fd, release_sha, attempt_id, driver_sha256)
        if chain or abandoned:
            raise JournalError("recovery seed requires an unused attempt")
        names = set(os.listdir(dir_fd))
        if names - {RECOVERY_SEED_NAME}:
            raise JournalError("recovery seed requires a clean attempt directory")
        if RECOVERY_SEED_NAME in names:
            if names != {RECOVERY_SEED_NAME}:
                raise JournalError("completed recovery seed has stray state")
            existing = _read_entry(dir_fd, RECOVERY_SEED_NAME)
            if existing != payload:
                raise JournalError("recovery seed already has different content")
            _make_entry_durable(dir_fd, RECOVERY_SEED_NAME)
            if _read_entry(dir_fd, RECOVERY_SEED_NAME) != payload:
                raise JournalError("recovery seed changed during durability retry")
            return payload
        _write_once(dir_fd, RECOVERY_SEED_NAME, payload)
        return payload
    finally:
        os.close(dir_fd)


def read_recovery_seed(attempt_dir, release_sha, attempt_id, driver_sha256):
    _validate_inputs(attempt_dir, release_sha, attempt_id, driver_sha256)
    dir_fd = _open_attempt_dir(attempt_dir)
    try:
        if RECOVERY_SEED_NAME not in os.listdir(dir_fd):
            raise JournalError("recovery seed is absent")
        return _validate_recovery_seed(
            _read_entry(dir_fd, RECOVERY_SEED_NAME),
            release_sha, attempt_id, driver_sha256)
    finally:
        os.close(dir_fd)


def _common(child):
    child.add_argument("--attempt-dir", required=True)
    child.add_argument("--release-sha", required=True)
    child.add_argument("--attempt-id", required=True)
    child.add_argument("--driver-sha256", required=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    _common(init)
    advance = commands.add_parser("advance")
    _common(advance)
    advance.add_argument("--expect", required=True, choices=PHASES)
    advance.add_argument("--next", required=True, choices=PHASES)
    advance.add_argument("--fact", action="append", default=[])
    abandon = commands.add_parser("abandon")
    _common(abandon)
    abandon.add_argument("--reason", required=True)
    status = commands.add_parser("status")
    _common(status)
    seed = commands.add_parser("seed-recovery")
    _common(seed)
    seed.add_argument("--previous-attempt-id", required=True)
    seed.add_argument("--previous-phase", required=True, choices=PHASES)
    seed.add_argument("--previous-phase-sha256", required=True)
    seed.add_argument("--source-trading", required=True)
    seed.add_argument("--source-data", required=True)
    seed.add_argument("--completed-schedule-slot", required=True)
    seed.add_argument(
        "--data-state", required=True, choices=sorted(SOURCE_DATA_STATES))
    read_seed = commands.add_parser("recovery-seed")
    _common(read_seed)
    publish = commands.add_parser("publish-artifact")
    _common(publish)
    publish.add_argument("--name", required=True, choices=sorted(PUBLISH_NAMES))
    publish.add_argument("--source", required=True)
    read_source = commands.add_parser("source-contract")
    _common(read_source)
    describe = commands.add_parser("describe-source")
    describe.add_argument("--source-trading", required=True)
    describe.add_argument("--source-data", required=True)
    describe.add_argument("--completed-schedule-slot", required=True)
    describe.add_argument(
        "--data-state", required=True, choices=sorted(SOURCE_DATA_STATES))
    sync = commands.add_parser("sync-paths")
    sync.add_argument("--path", action="append", required=True)
    switch = commands.add_parser("switch-active-attempt")
    switch.add_argument("--release-sha", required=True)
    switch.add_argument("--expect", required=True)
    switch.add_argument("--next", required=True)
    mounts = commands.add_parser("assert-no-mounts")
    mounts.add_argument("--path", action="append", required=True)
    bound_cgroup = commands.add_parser("kill-bound-cgroup")
    bound_cgroup.add_argument(
        "--unit", required=True, choices=sorted(HARD_KILL_UNITS))
    bound_cgroup.add_argument("--cgroup", required=True)
    runtime = commands.add_parser("init-runtime")
    runtime.add_argument("--release-sha", required=True)
    tree = commands.add_parser("sync-runtime-tree")
    tree.add_argument("--release-sha", required=True)
    tree.add_argument("--attempt-id", required=True)
    tree.add_argument(
        "--gate-state", required=True, choices=("active", "abandoned"))
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            payload = init_journal(
                args.attempt_dir, args.release_sha, args.attempt_id,
                args.driver_sha256)
        elif args.command == "advance":
            payload = advance_journal(
                args.attempt_dir, args.release_sha, args.attempt_id,
                args.driver_sha256, args.expect, args.next, args.fact)
        elif args.command == "abandon":
            payload = abandon_journal(
                args.attempt_dir, args.release_sha, args.attempt_id,
                args.driver_sha256, args.reason)
        elif args.command == "status":
            payload = read_journal_status(
                args.attempt_dir, args.release_sha, args.attempt_id,
                args.driver_sha256)
        elif args.command == "seed-recovery":
            payload = create_recovery_seed(
                args.attempt_dir, args.release_sha, args.attempt_id,
                args.driver_sha256, args.previous_attempt_id,
                args.previous_phase, args.previous_phase_sha256,
                args.source_trading, args.source_data,
                args.completed_schedule_slot,
                args.data_state)
        elif args.command == "recovery-seed":
            payload = read_recovery_seed(
                args.attempt_dir, args.release_sha, args.attempt_id,
                args.driver_sha256)
        elif args.command == "publish-artifact":
            payload = publish_artifact(
                args.attempt_dir, args.release_sha, args.attempt_id,
                args.driver_sha256, args.name, args.source)
        elif args.command == "source-contract":
            payload = read_source_contract(
                args.attempt_dir, args.release_sha, args.attempt_id,
                args.driver_sha256)
        elif args.command == "describe-source":
            payload = describe_source(
                args.source_trading, args.source_data,
                args.completed_schedule_slot, args.data_state)
        elif args.command == "sync-paths":
            payload = sync_paths(args.path)
        elif args.command == "switch-active-attempt":
            payload = switch_active_attempt(
                args.release_sha, args.expect, args.next)
        elif args.command == "assert-no-mounts":
            payload = assert_no_mounts(args.path)
        elif args.command == "kill-bound-cgroup":
            payload = kill_bound_cgroup(args.unit, args.cgroup)
        elif args.command == "init-runtime":
            payload = init_runtime(args.release_sha)
        elif args.command == "sync-runtime-tree":
            if SHA40_RE.fullmatch(args.release_sha or "") is None:
                raise JournalError("invalid release SHA")
            payload = sync_tree(
                os.path.join(RUNTIME_BASE, args.release_sha), args.attempt_id,
                args.gate_state)
        else:  # argparse requires a known subcommand; keep future dispatch explicit.
            raise JournalError("unsupported deployment journal command")
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    except (JournalError, OSError) as exc:
        print(f"deployment phase journal blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
