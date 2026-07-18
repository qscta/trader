#!/usr/bin/python3 -I
"""Strict, root-owned approvals for the production deployment driver.

The deployment is deliberately non-interactive.  Human review points therefore
arrive as short-lived JSON approvals rather than a prompt in the deployment
shell.  Every approval is bound to the reviewed release and to caller-supplied
hash/inode facts.  This module never reads OKX credentials and never performs
network I/O.
"""

import argparse
import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone


SCHEMA_VERSION = 1
MAX_BYTES = 64 * 1024
MAX_LIFETIME_SECONDS = 24 * 60 * 60
RELEASE_RE = re.compile(r"^[0-9a-f]{40}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTITY_RE = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$")
NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
TRADING_ENV_KEYS = frozenset({
    "OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE", "OKX_PASSWORD",
    "DINGTALK_WEBHOOK", "FLASK_SECRET_KEY", "TRADING_LOGIN_PASSWORD",
    "TRADING_API_TOKEN", "TRADING_COOKIE_SECURE", "TRADING_PROXYFIX_X_FOR",
    "TRADING_BIND", "TRADING_WEB_THREADS", "TRADING_GUNICORN_TIMEOUT",
    "TRADING_GUNICORN_GRACEFUL_TIMEOUT",
})
DANGEROUS_INHERITED_ENV = (
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_DEBUG",
    "LD_DEBUG_OUTPUT", "LD_PROFILE", "GUNICORN_CMD_ARGS",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT",
    "PYTHONUSERBASE", "PYTHONWARNINGS", "PYTHONBREAKPOINT",
    "PYTHONPYCACHEPREFIX", "PYTHONPLATLIBDIR", "PYTHONEXECUTABLE",
    "PYTHONCASEOK", "PYTHONHTTPSVERIFY",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE", "AWS_CA_BUNDLE", "OPENSSL_CONF",
    "OPENSSL_MODULES", "SSLKEYLOGFILE",
    "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH",
)
UNSET_ENV_TEXT = " ".join(DANGEROUS_INHERITED_ENV)
WRITER_CONTROLS = {
    "human": "operator_attestation_and_okx_ui_order_entry_freeze",
    "host": "reviewed_process_and_unit_inventory",
    "service": "service_stopped_and_frozen",
    "container": "container_stopped_and_frozen",
    "credential_consumer": "credential_consumer_inventory_and_freeze",
}
WRITER_BOUNDARY_FIELDS = frozenset({
    "exchange_ui_manual_actions_frozen",
    "all_other_api_credentials_and_consumers_frozen",
    "host_process_inventory_complete",
    "runtime_system_api_only_acknowledged",
    "same_side_size_replacement_unattributable_acknowledged",
})
MANAGED_SCHEMAS = {
    "release_env": (0o600, re.compile(
        r"TRADING_RUNNER_LOCK_FILE=/var/lib/trading-runtime/"
        r"(?P<sha>[0-9a-f]{40})/\.runtime/runner\.lock\n"
        r"TRADING_MAINTENANCE_SENTINEL=/var/lib/trading-runtime/"
        r"(?P=sha)/\.maintenance_no_open\n"
        r"TRADING_CONFIG_FILE=/var/lib/trading-runtime/(?P=sha)/config\.json\n"
        r"TRADING_DATA_DIR=/var/lib/trading-runtime/(?P=sha)\n")),
    "release_dropin": (0o644, re.compile(
        r"\[Service\]\n"
        r"Type=simple\n"
        r"User=ubuntu\n"
        r"Group=ubuntu\n"
        r"DynamicUser=false\n"
        r"WorkingDirectory=/opt/trader-releases/(?P<sha>[0-9a-f]{40})/trading\n"
        r"ExecCondition=\n"
        r"ExecStartPre=\n"
        r"ExecStart=\n"
        r"ExecStart=/opt/trader-releases/(?P=sha)/trading/\.venv/bin/"
        r"python -B -E -m gunicorn -c gunicorn\.conf\.py wsgi:application\n"
        r"ExecStartPost=\n"
        r"ExecReload=\n"
        r"ExecStop=\n"
        r"ExecStopPost=\n"
        r"Environment=\n"
        r"EnvironmentFile=\n"
        r"EnvironmentFile=/etc/trading\.env\n"
        r"EnvironmentFile=/etc/trading-release\.env\n"
        r"PassEnvironment=\n"
        r"PAMName=\n"
        r"RootDirectory=\n"
        r"RootImage=\n"
        r"BindPaths=\n"
        r"BindReadOnlyPaths=\n"
        r"TemporaryFileSystem=\n"
        r"MountImages=\n"
        r"ExtensionImages=\n"
        r"ExtensionDirectories=\n"
        r"ExecSearchPath=/usr/sbin:/usr/bin:/sbin:/bin\n"
        r"LoadCredential=\n"
        r"LoadCredentialEncrypted=\n"
        r"SetCredential=\n"
        r"SetCredentialEncrypted=\n"
        r"UnsetEnvironment=\n"
        rf"UnsetEnvironment={re.escape(UNSET_ENV_TEXT)}\n"
        r"UMask=0077\n"
        r"KillMode=control-group\n"
        r"Restart=on-failure\n"
        r"RestartSec=10s\n"
        r"TimeoutStopSec=920s\n")),
    "monitor_dropin": (0o644, re.compile(
        r"\[Service\]\n"
        r"Type=simple\n"
        r"DynamicUser=true\n"
        r"SupplementaryGroups=ubuntu\n"
        r"WorkingDirectory=/opt/trader-releases/(?P<sha>[0-9a-f]{40})/trading\n"
        r"ExecCondition=\n"
        r"ExecStartPre=\n"
        r"ExecStart=\n"
        r"ExecStart=/opt/trader-releases/(?P=sha)/trading/\.venv/bin/python -B -E mem_monitor\.py\n"
        r"ExecStartPost=\n"
        r"ExecReload=\n"
        r"ExecStop=\n"
        r"ExecStopPost=\n"
        r"Environment=\n"
        r"EnvironmentFile=\n"
        r"EnvironmentFile=/etc/trading-mem-monitor\.env\n"
        r"Environment=TRADING_DATA_DIR=/var/lib/trading-runtime/(?P=sha)\n"
        r"Environment=TRADING_CONFIG_FILE=/var/lib/trading-runtime/(?P=sha)/config\.json\n"
        r"PassEnvironment=\n"
        r"PAMName=\n"
        r"RootDirectory=\n"
        r"RootImage=\n"
        r"BindPaths=\n"
        r"BindReadOnlyPaths=\n"
        r"TemporaryFileSystem=\n"
        r"MountImages=\n"
        r"ExtensionImages=\n"
        r"ExtensionDirectories=\n"
        r"ExecSearchPath=/usr/sbin:/usr/bin:/sbin:/bin\n"
        r"LoadCredential=\n"
        r"LoadCredentialEncrypted=\n"
        r"SetCredential=\n"
        r"SetCredentialEncrypted=\n"
        r"UnsetEnvironment=\n"
        rf"UnsetEnvironment={re.escape(UNSET_ENV_TEXT)}\n"
        r"InaccessiblePaths=\n"
        r"InaccessiblePaths=-/var/lib/trading-runtime/(?P=sha)/config\.json\n"
        r"UMask=0077\n"
        r"KillMode=control-group\n"
        r"Restart=on-failure\n"
        r"RestartSec=10s\n")),
}


class EvidenceError(RuntimeError):
    """An approval cannot be trusted or does not match this deployment."""


def _assert_immutable_target(path, context):
    target = os.path.realpath(path)
    if not os.path.isabs(target) or not os.path.exists(target):
        raise EvidenceError(f"{context} target does not exist")
    target_info = os.stat(target, follow_symlinks=False)
    if (not (stat.S_ISREG(target_info.st_mode) or
             stat.S_ISDIR(target_info.st_mode)) or
            target_info.st_uid != 0 or stat.S_IMODE(target_info.st_mode) & 0o022):
        raise EvidenceError(f"{context} target is not root-owned immutable content")
    current = os.path.dirname(target)
    while True:
        info = os.stat(current, follow_symlinks=False)
        mode = stat.S_IMODE(info.st_mode)
        # A non-root owner can chmod an apparently read-only directory and
        # then replace a descendant after review.  Every resolved ancestor,
        # including ``/``, must therefore be root-controlled as well as
        # protected from group/world writes.
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or
                mode & 0o022):
            raise EvidenceError(
                f"{context} target path has a non-root or writable parent")
        if current == os.path.sep:
            break
        current = os.path.dirname(current)


def validate_venv_tree(venv):
    if (not isinstance(venv, str) or not os.path.isabs(venv) or
            os.path.normpath(venv) != venv or os.path.realpath(venv) != venv):
        raise EvidenceError("venv path must be canonical and symlink-free")
    root = os.lstat(venv)
    if (not stat.S_ISDIR(root.st_mode) or root.st_uid != 0 or
            stat.S_IMODE(root.st_mode) & 0o022):
        raise EvidenceError("venv root must be root-owned and protected")
    for directory, dirnames, names in os.walk(venv, followlinks=False):
        for name in [*dirnames, *names]:
            path = os.path.join(directory, name)
            info = os.lstat(path)
            if info.st_uid != 0:
                raise EvidenceError("venv entry is not root-owned")
            if stat.S_ISLNK(info.st_mode):
                _assert_immutable_target(path, "venv symlink")
            elif (not (stat.S_ISREG(info.st_mode) or
                       stat.S_ISDIR(info.st_mode)) or
                  stat.S_IMODE(info.st_mode) & 0o022):
                raise EvidenceError("venv entry is writable or has an unsafe type")
    return venv


def _validate_trading_env_text(text):
    keys = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or
                key not in TRADING_ENV_KEYS or key in keys or not value or
                value.endswith("\\") or any(ord(char) < 32 for char in value)):
            raise EvidenceError(f"trading environment line {number} is not allowed")
        keys.add(key)
    if not {"FLASK_SECRET_KEY", "TRADING_API_TOKEN"}.issubset(keys):
        raise EvidenceError("trading environment lacks required web secrets")
    return tuple(sorted(keys))


def _read_protected_env(path, expected_path, context):
    if path != expected_path:
        raise EvidenceError(f"{context} path must be {expected_path}")
    _assert_immutable_target(os.path.dirname(path), f"{context} parent")
    before = os.lstat(path)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or
            stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1):
        raise EvidenceError(f"{context} metadata is invalid")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if _metadata_identity(opened) != _metadata_identity(before):
            raise EvidenceError(f"{context} changed while opening")
        data = os.read(fd, MAX_BYTES + 1)
        if len(data) > MAX_BYTES or os.read(fd, 1):
            raise EvidenceError(f"{context} is too large")
        if _metadata_identity(os.fstat(fd)) != _metadata_identity(opened):
            raise EvidenceError(f"{context} changed while reading")
    finally:
        os.close(fd)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError(f"{context} is not UTF-8") from exc
    return text


def validate_trading_env(path):
    return _validate_trading_env_text(_read_protected_env(
        path, "/etc/trading.env", "trading environment"))


def validate_monitor_env(path):
    text = _read_protected_env(
        path, "/etc/trading-mem-monitor.env", "monitor environment")
    lines = [line for line in text.splitlines()
             if line and not line.startswith("#")]
    if len(lines) != 1:
        raise EvidenceError("monitor environment must contain exactly one value")
    key, separator, value = lines[0].partition("=")
    if (key != "DINGTALK_WEBHOOK" or not separator or not value or
            value.endswith("\\") or any(ord(char) < 32 for char in value)):
        raise EvidenceError("monitor environment may contain only DINGTALK_WEBHOOK")
    return key


def validate_writer_inventory_payload(payload, release_sha):
    expected = {
        "schema_version", "release_sha", "all_other_writers_frozen",
        "single_writer_boundary", "writers",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise EvidenceError("writer inventory top-level schema is not exact")
    if payload["schema_version"] != 2 or payload["release_sha"] != release_sha:
        raise EvidenceError("writer inventory release/schema does not match")
    if payload["all_other_writers_frozen"] is not True:
        raise EvidenceError("writer inventory has unfrozen external writers")
    boundary = payload["single_writer_boundary"]
    if (not isinstance(boundary, dict) or
            set(boundary) != WRITER_BOUNDARY_FIELDS or
            any(value is not True for value in boundary.values())):
        raise EvidenceError("single-writer boundary declarations are incomplete")
    writers = payload["writers"]
    if not isinstance(writers, list) or not writers or len(writers) > 128:
        raise EvidenceError("writer inventory must be a bounded non-empty list")
    seen = set()
    types = set()
    for writer in writers:
        if not isinstance(writer, dict) or set(writer) != {
                "id", "type", "scope", "frozen", "evidence"}:
            raise EvidenceError("writer inventory entry schema is not exact")
        writer_id = writer["id"]
        writer_type = writer["type"]
        if (not isinstance(writer_id, str) or not writer_id.strip() or
                len(writer_id) > 128 or writer_id in seen or
                writer_type not in WRITER_CONTROLS or
                writer["scope"] != "managed_swap_order_writes" or
                writer["frozen"] is not True):
            raise EvidenceError("writer inventory entry is invalid or duplicated")
        evidence = writer["evidence"]
        if not isinstance(evidence, dict) or set(evidence) != {
                "control", "observed_at", "subject"}:
            raise EvidenceError("writer evidence schema is not exact")
        if evidence["control"] != WRITER_CONTROLS[writer_type]:
            raise EvidenceError("writer evidence control does not match its type")
        if (not isinstance(evidence["subject"], str) or
                len(evidence["subject"].strip()) < 8 or
                len(evidence["subject"]) > 512):
            raise EvidenceError("writer evidence subject is not concrete")
        _utc_timestamp(evidence["observed_at"], "writer observed_at")
        seen.add(writer_id)
        types.add(writer_type)
    required_types = {"human", "host", "credential_consumer"}
    if not required_types.issubset(types):
        raise EvidenceError(
            "writer inventory must explicitly cover human, host and credentials")
    return payload


def validate_writer_inventory(path, release_sha):
    if not isinstance(release_sha, str) or RELEASE_RE.fullmatch(release_sha) is None:
        raise EvidenceError("writer inventory release SHA is invalid")
    return validate_writer_inventory_payload(
        loads_strict(_read_root_owned(path)), release_sha)


def _metadata_identity(info):
    """Fields that must stay invariant across lstat/open/read."""
    return (
        info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_uid,
        stat.S_IMODE(info.st_mode), info.st_nlink, info.st_size,
    )


def _reject_constant(value):
    raise EvidenceError(f"JSON contains non-finite constant {value!r}")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def loads_strict(text):
    try:
        return json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except EvidenceError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceError("approval is not strict JSON") from exc


def _utc_timestamp(value, field):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvidenceError(f"{field} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EvidenceError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise EvidenceError(f"{field} must be UTC")
    return parsed


def _validate_binding(key, value):
    if not isinstance(key, str) or KEY_RE.fullmatch(key) is None:
        raise EvidenceError(f"invalid binding key {key!r}")
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise EvidenceError(f"binding {key} must be a non-empty string")
    if key.endswith("_sha256") and SHA256_RE.fullmatch(value) is None:
        raise EvidenceError(f"binding {key} must be a SHA-256 digest")
    if key.endswith("_identity") and IDENTITY_RE.fullmatch(value) is None:
        raise EvidenceError(f"binding {key} must be dev:ino")
    if key.endswith("_nonce") and NONCE_RE.fullmatch(value) is None:
        raise EvidenceError(f"binding {key} must be a 256-bit hex nonce")


def parse_bindings(values):
    result = {}
    for item in values:
        if not isinstance(item, str) or "=" not in item:
            raise EvidenceError("--binding must be KEY=VALUE")
        key, value = item.split("=", 1)
        _validate_binding(key, value)
        if key in result:
            raise EvidenceError(f"duplicate expected binding {key}")
        result[key] = value
    return result


def validate_payload(payload, *, kind, release_sha, bindings, now=None):
    expected_fields = {
        "schema_version", "kind", "release_sha", "decision",
        "issued_at", "expires_at", "bindings",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise EvidenceError("approval schema fields are not exact")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise EvidenceError("approval schema version is unsupported")
    if not isinstance(kind, str) or KIND_RE.fullmatch(kind) is None:
        raise EvidenceError("expected approval kind is invalid")
    if not isinstance(release_sha, str) or RELEASE_RE.fullmatch(release_sha) is None:
        raise EvidenceError("expected release SHA is invalid")
    if payload["kind"] != kind:
        raise EvidenceError("approval kind does not match")
    if payload["release_sha"] != release_sha:
        raise EvidenceError("approval release SHA does not match")
    if payload["decision"] != "approved":
        raise EvidenceError("approval decision is not approved")
    if not isinstance(payload["bindings"], dict):
        raise EvidenceError("approval bindings must be an object")
    for key, value in payload["bindings"].items():
        _validate_binding(key, value)
    if payload["bindings"] != bindings:
        raise EvidenceError("approval bindings do not match exactly")

    issued = _utc_timestamp(payload["issued_at"], "issued_at")
    expires = _utc_timestamp(payload["expires_at"], "expires_at")
    if expires <= issued:
        raise EvidenceError("approval expiry must follow issue time")
    if (expires - issued).total_seconds() > MAX_LIFETIME_SECONDS:
        raise EvidenceError("approval lifetime exceeds 24 hours")
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime):
        raise EvidenceError("current time must be a datetime")
    if current.tzinfo is None or current.utcoffset() is None:
        raise EvidenceError("current time must be timezone-aware")
    current = current.astimezone(timezone.utc)
    if issued > current + timedelta(seconds=60):
        raise EvidenceError("approval issue time is in the future")
    if current >= expires:
        raise EvidenceError("approval has expired")
    return payload


def _read_root_owned(path):
    if not isinstance(path, str) or not os.path.isabs(path):
        raise EvidenceError("approval path must be absolute")
    if os.path.normpath(path) != path:
        raise EvidenceError("approval path must be canonical")
    if os.path.realpath(path) != path:
        raise EvidenceError("approval path must not traverse symlinks")
    parent = os.path.dirname(path)
    try:
        parent_info = os.stat(parent, follow_symlinks=False)
    except OSError as exc:
        raise EvidenceError("approval parent cannot be inspected") from exc
    if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != 0 or
            stat.S_IMODE(parent_info.st_mode) & 0o022):
        raise EvidenceError("approval parent must be root-owned and protected")
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise EvidenceError("approval cannot be inspected") from exc
    if not stat.S_ISREG(before.st_mode):
        raise EvidenceError("approval must be a regular file")
    if before.st_uid != 0:
        raise EvidenceError("approval must be owned by root")
    if stat.S_IMODE(before.st_mode) != 0o600:
        raise EvidenceError("approval mode must be 0600")
    if before.st_nlink != 1:
        raise EvidenceError("approval must have exactly one hard link")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise EvidenceError("approval cannot be opened safely") from exc
    try:
        after = os.fstat(fd)
        before_identity = _metadata_identity(before)
        after_identity = _metadata_identity(after)
        if before_identity != after_identity:
            raise EvidenceError("approval changed while opening")
        if after.st_size > MAX_BYTES:
            raise EvidenceError("approval is too large")
        chunks = []
        remaining = MAX_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if sum(len(chunk) for chunk in chunks) > MAX_BYTES:
            raise EvidenceError("approval is too large")
        try:
            text = b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvidenceError("approval is not UTF-8") from exc
        final = os.fstat(fd)
        final_identity = _metadata_identity(final)
        if final_identity != after_identity:
            raise EvidenceError("approval changed while reading")
        return text
    finally:
        os.close(fd)


def verify_file(path, *, kind, release_sha, bindings, now=None):
    payload = loads_strict(_read_root_owned(path))
    return validate_payload(
        payload, kind=kind, release_sha=release_sha,
        bindings=bindings, now=now)


def validate_managed_text(text, kind):
    if kind not in MANAGED_SCHEMAS:
        raise EvidenceError("unknown managed file kind")
    _, pattern = MANAGED_SCHEMAS[kind]
    if not isinstance(text, str) or pattern.fullmatch(text) is None:
        raise EvidenceError("managed file content is not a known schema")
    return text


def validate_managed_file(path, kind):
    if kind not in MANAGED_SCHEMAS:
        raise EvidenceError("unknown managed file kind")
    expected_mode, _ = MANAGED_SCHEMAS[kind]
    if not isinstance(path, str) or not os.path.isabs(path):
        raise EvidenceError("managed path must be absolute")
    if os.path.normpath(path) != path or os.path.realpath(path) != path:
        raise EvidenceError("managed path must be canonical and symlink-free")
    parent = os.path.dirname(path)
    parent_info = os.stat(parent, follow_symlinks=False)
    if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != 0 or
            stat.S_IMODE(parent_info.st_mode) & 0o022):
        raise EvidenceError("managed parent must be root-owned and protected")
    before = os.lstat(path)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or
            stat.S_IMODE(before.st_mode) != expected_mode or before.st_nlink != 1):
        raise EvidenceError("managed file metadata is invalid")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if _metadata_identity(opened) != _metadata_identity(before):
            raise EvidenceError("managed file changed while opening")
        if opened.st_size > MAX_BYTES:
            raise EvidenceError("managed file is too large")
        chunks = []
        remaining = MAX_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_BYTES:
            raise EvidenceError("managed file is too large")
        final = os.fstat(fd)
        if _metadata_identity(final) != _metadata_identity(opened):
            raise EvidenceError("managed file changed while reading")
    finally:
        os.close(fd)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError("managed file is not UTF-8") from exc
    return validate_managed_text(text, kind)


def _format_utc(value):
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _write_exclusive_root(path, payload):
    if os.geteuid() != 0:
        raise EvidenceError("issuing an approval requires root")
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise EvidenceError("approval output must be a canonical absolute path")
    parent = os.path.dirname(path)
    if os.path.realpath(parent) != parent:
        raise EvidenceError("approval parent must not traverse symlinks")
    parent_info = os.stat(parent, follow_symlinks=False)
    if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != 0 or
            stat.S_IMODE(parent_info.st_mode) & 0o022):
        raise EvidenceError("approval parent must be a root-owned protected directory")
    encoded = (json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = None
    try:
        fd = os.open(path, flags, 0o600)
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(encoded):
            written = os.write(fd, encoded[offset:])
            if written <= 0:
                raise EvidenceError("approval write did not progress")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = None
        parent_fd = os.open(
            parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except FileExistsError as exc:
        raise EvidenceError("approval already exists; never overwrite it") from exc
    finally:
        if fd is not None:
            os.close(fd)


def issue_file(path, *, kind, release_sha, bindings, expires_in, now=None):
    if not isinstance(expires_in, int) or isinstance(expires_in, bool):
        raise EvidenceError("expiry must be an integer number of seconds")
    if not (1 <= expires_in <= MAX_LIFETIME_SECONDS):
        raise EvidenceError("expiry must be between 1 second and 24 hours")
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime):
        raise EvidenceError("current time must be a datetime")
    if current.tzinfo is None or current.utcoffset() is None:
        raise EvidenceError("current time must be timezone-aware")
    current = current.astimezone(timezone.utc)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "release_sha": release_sha,
        "decision": "approved",
        "issued_at": _format_utc(current),
        "expires_at": _format_utc(current + timedelta(seconds=expires_in)),
        "bindings": dict(bindings),
    }
    validate_payload(
        payload, kind=kind, release_sha=release_sha,
        bindings=bindings, now=current)
    _write_exclusive_root(path, payload)
    return payload


def _parser():
    parser = argparse.ArgumentParser(description="strict deployment approval evidence")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify")
    issue = sub.add_parser("issue")
    managed = sub.add_parser("validate-managed")
    trading_env = sub.add_parser("validate-trading-env")
    monitor_env = sub.add_parser("validate-monitor-env")
    venv = sub.add_parser("validate-venv")
    writers = sub.add_parser("validate-writer-inventory")
    for item in (verify, issue):
        item.add_argument("--file", required=True)
        item.add_argument("--kind", required=True)
        item.add_argument("--release-sha", required=True)
        item.add_argument("--binding", action="append", default=[])
    issue.add_argument("--expires-in", type=int, default=1800)
    managed.add_argument("--file", required=True)
    managed.add_argument("--kind", required=True, choices=sorted(MANAGED_SCHEMAS))
    trading_env.add_argument("--file", required=True)
    monitor_env.add_argument("--file", required=True)
    venv.add_argument("--venv", required=True)
    writers.add_argument("--file", required=True)
    writers.add_argument("--release-sha", required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-managed":
            validate_managed_file(args.file, args.kind)
            return 0
        if args.command == "validate-trading-env":
            validate_trading_env(args.file)
            return 0
        if args.command == "validate-monitor-env":
            validate_monitor_env(args.file)
            return 0
        if args.command == "validate-venv":
            validate_venv_tree(args.venv)
            return 0
        if args.command == "validate-writer-inventory":
            validate_writer_inventory(args.file, args.release_sha)
            return 0
        bindings = parse_bindings(args.binding)
        if args.command == "verify":
            verify_file(
                args.file, kind=args.kind,
                release_sha=args.release_sha, bindings=bindings)
        else:
            issue_file(
                args.file, kind=args.kind,
                release_sha=args.release_sha, bindings=bindings,
                expires_in=args.expires_in)
    except EvidenceError as exc:
        print(f"deployment evidence rejected: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
