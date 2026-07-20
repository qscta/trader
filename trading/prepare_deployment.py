#!/usr/bin/python3 -I
"""Build and atomically publish a credential-free deployment stage."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
WORKFLOW_CREATED_AT_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
DRIVER_FILES = (
    "deploy.sh",
    "emergency-stop.sh",
    "recover-deployment.sh",
    "deployment_evidence.py",
    "deployment_attempt.py",
    "deployment_old_runner_gate.py",
)
REVIEWED_INPUTS = {
    "remove-one-confirmed-config-key.spec.json": 0o600,
    "writer-inventory.json": 0o600,
    "backup-script.patch": 0o600,
}
TRACKED_REVIEWED_INPUTS = {
    "remove-one-confirmed-config-key.py": 0o755,
}
DESCRIPTOR_FIELDS = {
    "schema_version", "release_sha", "prepare_tool_sha256",
    "ci_checks_sha256", "ci_runs_sha256", "required_checks",
    "required_workflows", "reviewed_inputs", "backup_original_sha256",
}
ROOT_UID = 0
ROOT_GID = 0
MAX_REVIEWED_INPUT_BYTES = 32 * 1024 * 1024


def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def digest_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def digest_venv_entry(path):
    """Hash a venv entry without following symlinks, including directory links."""
    path = Path(path)
    if path.is_symlink():
        target = os.fsencode(os.readlink(path))
        return digest_bytes(b"symlink\0" + target)
    return digest(path)


def _reject_json_constant(value):
    raise RuntimeError(f"JSON contains non-standard constant: {value}")


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError("JSON object contains a duplicate key")
        result[key] = value
    return result


def load_strict_json(payload, context):
    try:
        return json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except RuntimeError:
        raise
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{context} is not strict UTF-8 JSON") from exc


def _stable_file_identity(info):
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
        info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


def read_reviewed_snapshot(source, protected=False):
    """Read one pathname once through a no-follow, inode-bound snapshot."""
    source = Path(source)
    path = str(source)
    if (not source.is_absolute() or os.path.normpath(path) != path or
            os.path.realpath(path) != path):
        raise RuntimeError(f"reviewed input path is not canonical: {source}")
    before = os.lstat(source)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or
            before.st_size > MAX_REVIEWED_INPUT_BYTES):
        raise RuntimeError(
            f"reviewed input is not a bounded single regular file: {source}")
    if protected and (
            before.st_uid != ROOT_UID or before.st_gid != ROOT_GID or
            stat.S_IMODE(before.st_mode) & 0o022):
        raise RuntimeError(f"reviewed input is not root-protected: {source}")
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if _stable_file_identity(before) != _stable_file_identity(opened):
            raise RuntimeError(f"reviewed input changed while opening: {source}")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_REVIEWED_INPUT_BYTES:
                raise RuntimeError(f"reviewed input exceeds size limit: {source}")
            chunks.append(chunk)
        if _stable_file_identity(opened) != _stable_file_identity(os.fstat(fd)):
            raise RuntimeError(f"reviewed input changed while reading: {source}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def run(args, **kwargs):
    kwargs.setdefault("env", sterile_runtime_env())
    return subprocess.run(args, check=True, text=True, **kwargs)


def sterile_runtime_env(**extra):
    allowed = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
    for key in ("LANG", "LC_ALL"):
        if key in os.environ:
            allowed[key] = os.environ[key]
    allowed.update(extra)
    return allowed


def write_snapshot(target, payload, mode=0o600):
    """Create one private-tree file without ever chmod'ing a pathname."""
    fd = None
    created = False
    try:
        fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL |
            getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        created = True
        os.fchmod(fd, mode)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise RuntimeError(f"snapshot write made no progress: {target}")
            view = view[written:]
    except BaseException:
        if fd is not None:
            os.close(fd)
            fd = None
        if created:
            try:
                os.unlink(target)
            except FileNotFoundError:
                pass
        raise
    finally:
        if fd is not None:
            os.close(fd)


def write_private(path, value):
    write_snapshot(path, value.encode("utf-8"), 0o600)


def require_protected_directory(path):
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != ROOT_UID or
            info.st_gid != ROOT_GID or
            stat.S_IMODE(info.st_mode) & 0o022):
        raise RuntimeError(f"directory is not root-owned/protected: {path}")


def mount_points():
    if not sys.platform.startswith("linux"):
        return []
    try:
        lines = Path("/proc/self/mountinfo").read_text(
            encoding="ascii").splitlines()
    except OSError as exc:
        raise RuntimeError("cannot inspect Linux mount table") from exc

    def unescape(value):
        return re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            value,
        )

    result = []
    for line in lines:
        fields = line.split(" ")
        if len(fields) < 6:
            raise RuntimeError("malformed Linux mount table")
        result.append(unescape(fields[4]))
    return result


def reject_mounts_under(path):
    root = str(path)
    for mount_point in mount_points():
        if mount_point == root or mount_point.startswith(root + os.sep):
            raise RuntimeError(
                f"release tree contains a mount point: {mount_point}")


def verify_release_tree(release_root, trading):
    """Require an already immutable release; preparation never repairs code."""
    reject_mounts_under(release_root)
    root_info = os.lstat(release_root)
    if (not stat.S_ISDIR(root_info.st_mode) or
            root_info.st_uid != ROOT_UID or root_info.st_gid != ROOT_GID or
            stat.S_IMODE(root_info.st_mode) & 0o022):
        raise RuntimeError("release root must already be root-owned/protected")
    for directory, dirnames, names in os.walk(
            release_root, followlinks=False):
        directory_path = Path(directory)
        directory_info = os.lstat(directory_path)
        if (not stat.S_ISDIR(directory_info.st_mode) or
                directory_info.st_dev != root_info.st_dev or
                directory_info.st_uid != ROOT_UID or
                directory_info.st_gid != ROOT_GID or
                stat.S_IMODE(directory_info.st_mode) & 0o022):
            raise RuntimeError(
                f"release directory is not immutable: {directory_path}")
        for name in [*dirnames, *names]:
            path = directory_path / name
            info = os.lstat(path)
            in_venv = path == trading / ".venv" or trading / ".venv" in path.parents
            if stat.S_ISLNK(info.st_mode):
                if (not in_venv or info.st_uid != ROOT_UID or
                        info.st_gid != ROOT_GID):
                    raise RuntimeError(f"code-tree symlink is forbidden: {path}")
                continue
            if (info.st_uid != ROOT_UID or info.st_gid != ROOT_GID or
                    stat.S_IMODE(info.st_mode) & 0o022):
                raise RuntimeError(f"release entry is not immutable: {path}")
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise RuntimeError(f"code-tree hardlink is forbidden: {path}")
            elif not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(f"code-tree special file is forbidden: {path}")
    reject_mounts_under(release_root)


def reject_unreviewed_release_entries(release_root):
    """Reject every untracked/ignored release entry except the bound venv."""
    listed = run([
        "git", "-c", f"safe.directory={release_root}", "-C",
        str(release_root), "ls-files", "--others", "-z",
    ], capture_output=True).stdout
    unexpected = []
    for name in listed.split("\0"):
        if not name or name.startswith("trading/.venv/"):
            continue
        unexpected.append(name)
    if unexpected:
        raise RuntimeError(
            "release contains unreviewed untracked/ignored entries: " +
            ", ".join(sorted(unexpected)))


def fsync_tree(root):
    for directory, _, names in os.walk(root, topdown=False):
        for name in names:
            path = Path(directory) / name
            if path.is_symlink():
                raise RuntimeError(f"published evidence may not be a symlink: {path}")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_ci(payload_raw, sha, check_runs, required):
    payload = load_strict_json(payload_raw, "CI evidence")
    if check_runs:
        if not isinstance(payload, dict):
            raise RuntimeError("check-run evidence must be an object")
        items = payload.get("check_runs")
        total_count = payload.get("total_count")
        if (type(total_count) is not int or total_count <= 0 or
                not isinstance(items, list) or total_count != len(items)):
            raise RuntimeError(
                "check-run total_count is invalid or evidence is truncated")
    else:
        items = payload
    if not isinstance(items, list) or not items:
        raise RuntimeError("CI evidence is empty")
    name_key = "name" if check_runs else "workflowName"
    head_key = "head_sha" if check_runs else "headSha"
    if not check_runs:
        if len(items) >= 100:
            raise RuntimeError("workflow evidence may be truncated at the limit")
        latest = {}
        seen_run_ids = set()
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError("invalid workflow evidence item")
            name = item.get(name_key)
            if not isinstance(name, str) or not name:
                raise RuntimeError("workflow evidence has no name")
            if item.get(head_key) != sha:
                raise RuntimeError(
                    "workflow evidence contains a different release SHA")
            run_id = item.get("databaseId")
            created_at = item.get("createdAt")
            if (type(run_id) is not int or run_id <= 0 or
                    run_id in seen_run_ids):
                raise RuntimeError("workflow evidence databaseId is invalid")
            if (not isinstance(created_at, str) or
                    WORKFLOW_CREATED_AT_RE.fullmatch(created_at) is None):
                raise RuntimeError("workflow evidence createdAt is invalid")
            try:
                created_key = datetime.strptime(
                    created_at, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError as exc:
                raise RuntimeError(
                    "workflow evidence createdAt is invalid") from exc
            seen_run_ids.add(run_id)
            key = (created_key, run_id)
            if name not in latest or key > latest[name][0]:
                latest[name] = (key, item)
        items = [value[1] for value in latest.values()]
    names = set()
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("invalid CI evidence item")
        name = item.get(name_key)
        if not isinstance(name, str) or not name:
            raise RuntimeError("CI evidence item has no name")
        names.add(name)
        if (item.get(head_key) != sha or item.get("status") != "completed" or
                item.get("conclusion") != "success"):
            raise RuntimeError("CI evidence is not successful for the release SHA")
    missing = sorted(set(required) - names)
    if missing:
        raise RuntimeError(f"required CI evidence missing: {', '.join(missing)}")


def manifest_tree(release_root, output):
    listed = run(
        ["git", "-c", f"safe.directory={release_root}", "-C", str(release_root),
         "ls-files", "-z", "--", "trading"], capture_output=True).stdout
    names = sorted(item for item in listed.split("\0") if item)
    with open(output, "w", encoding="utf-8") as handle:
        for name in names:
            handle.write(f"{digest(release_root / name)}  {name}\n")
    os.chmod(output, 0o600)


def manifest_venv(release_root, trading, stage):
    venv = trading / ".venv"
    entries = sorted(
        path for path in venv.rglob("*") if path.is_file() or path.is_symlink())
    manifest_entries = []
    for path in entries:
        info = os.lstat(path)
        manifest_entries.append({
            "path": path.relative_to(release_root).as_posix(),
            "kind": "symlink" if stat.S_ISLNK(info.st_mode) else "file",
            "mode": stat.S_IMODE(info.st_mode),
            "sha256": digest_venv_entry(path),
        })
    manifest_entries.sort(key=lambda item: item["path"])
    write_private(
        stage / "reviewed-venv.json",
        json.dumps({
            "schema_version": 1,
            "entries": manifest_entries,
        }, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
    python = trading / ".venv/bin/python"
    env = sterile_runtime_env(PYTHONDONTWRITEBYTECODE="1")
    version = run(
        [str(python), "-B", "-E", "-c",
         "import sys; print('.'.join(map(str,sys.version_info[:3])))"],
        capture_output=True, env=env).stdout
    write_private(stage / "python-version.txt", version)
    freeze = run(
        [str(python), "-B", "-E", "-m", "pip", "freeze", "--all"],
        capture_output=True, env=env).stdout
    if re.search(r"(?m)(^-e| @ (?:file|git)?:)", freeze):
        raise RuntimeError("editable/VCS/file dependency is forbidden")
    write_private(stage / "pip-freeze.txt", freeze)
    code = (
        "import importlib,os; v=os.path.realpath(os.environ['EXPECTED_VENV']); "
        "out=[f'{n}={os.path.realpath(importlib.import_module(n).__file__)}' "
        "for n in ('ccxt','flask','gunicorn')]; "
        "assert all(os.path.commonpath((x.split('=',1)[1],v))==v for x in out); "
        "print('\\n'.join(out))")
    paths = run(
        [str(python), "-B", "-E", "-c", code], capture_output=True,
        env=dict(env, EXPECTED_VENV=str(venv))).stdout
    write_private(stage / "package-paths.txt", paths)


def write_asset_manifest(stage):
    files = sorted(path for path in stage.iterdir()
                   if path.is_file() and path.name != "reviewed-assets.sha256")
    lines = [f"{digest(path)}  {path.name}\n" for path in files]
    write_private(stage / "reviewed-assets.sha256", "".join(lines))


def expected_drivers(trading, sha):
    result = {}
    for name in DRIVER_FILES:
        source = trading / name
        if name in {"deploy.sh", "emergency-stop.sh", "recover-deployment.sh"}:
            text = source.read_text(encoding="utf-8")
            if text.count("__RELEASE_SHA__") != 1:
                raise RuntimeError(f"unexpected release placeholder count in {name}")
            result[name] = text.replace("__RELEASE_SHA__", sha).encode("utf-8")
        else:
            result[name] = source.read_bytes()
    return result


def validate_driver(path, expected):
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != ROOT_UID or
            info.st_gid != ROOT_GID or
            stat.S_IMODE(info.st_mode) != 0o755):
        raise RuntimeError("published driver directory is unsafe")
    if set(os.listdir(path)) != set(expected):
        raise RuntimeError("published driver file set mismatch")
    for name, payload in expected.items():
        target = path / name
        item = os.lstat(target)
        if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 or
                item.st_uid != ROOT_UID or item.st_gid != ROOT_GID or
                stat.S_IMODE(item.st_mode) != 0o555 or target.read_bytes() != payload):
            raise RuntimeError(f"published driver mismatch: {name}")


def validate_descriptor(descriptor):
    if (not isinstance(descriptor, dict) or
            set(descriptor) != DESCRIPTOR_FIELDS or
            type(descriptor.get("schema_version")) is not int or
            descriptor["schema_version"] != 1 or
            not isinstance(descriptor.get("release_sha"), str) or
            SHA_RE.fullmatch(descriptor["release_sha"]) is None):
        raise RuntimeError("published stage descriptor schema is invalid")
    digest_fields = (
        "prepare_tool_sha256", "ci_checks_sha256", "ci_runs_sha256",
        "backup_original_sha256",
    )
    if any(
            not isinstance(descriptor.get(name), str) or
            re.fullmatch(r"[0-9a-f]{64}", descriptor[name]) is None
            for name in digest_fields):
        raise RuntimeError("published stage descriptor digest is invalid")
    reviewed = descriptor.get("reviewed_inputs")
    expected_reviewed = set(REVIEWED_INPUTS) | set(TRACKED_REVIEWED_INPUTS)
    if (not isinstance(reviewed, dict) or set(reviewed) != expected_reviewed or
            any(not isinstance(value, str) or
                re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in reviewed.values())):
        raise RuntimeError("published reviewed-input binding is invalid")
    for name in ("required_checks", "required_workflows"):
        values = descriptor.get(name)
        if (not isinstance(values, list) or not values or
                len(values) != len(set(values)) or
                any(not isinstance(value, str) or not value
                    for value in values)):
            raise RuntimeError(f"published {name} binding is invalid")


def validate_stage(path, descriptor):
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != ROOT_UID or
            info.st_gid != ROOT_GID or
            stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("published stage directory is unsafe")
    validate_descriptor(descriptor)
    request_raw = read_reviewed_snapshot(
        path / "prepare-request.json", protected=True)
    if load_strict_json(request_raw, "published stage request") != descriptor:
        raise RuntimeError("published stage request binding mismatch")
    manifest = {}
    manifest_raw = read_reviewed_snapshot(
        path / "reviewed-assets.sha256", protected=True)
    try:
        manifest_lines = manifest_raw.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise RuntimeError("invalid published asset manifest encoding") from exc
    for line in manifest_lines:
        value, separator, name = line.partition("  ")
        if (not separator or not re.fullmatch(r"[0-9a-f]{64}", value) or
                not name or "/" in name or name in manifest):
            raise RuntimeError("invalid published asset manifest")
        manifest[name] = value
    root_files = set()
    root_dirs = set()
    for item in path.iterdir():
        item_info = os.lstat(item)
        if stat.S_ISREG(item_info.st_mode):
            root_files.add(item.name)
        elif stat.S_ISDIR(item_info.st_mode):
            root_dirs.add(item.name)
        else:
            raise RuntimeError(f"published stage contains unsafe entry: {item.name}")
    if root_files - {"reviewed-assets.sha256", "active-attempt"} != set(manifest):
        raise RuntimeError("published asset file set mismatch")
    if root_dirs != {"attempts"} or set(os.listdir(path / "attempts")) != {"0001"}:
        raise RuntimeError("published stage directory set mismatch")
    for name, value in manifest.items():
        item = os.lstat(path / name)
        raw = read_reviewed_snapshot(path / name, protected=True)
        if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 or
                item.st_uid != ROOT_UID or item.st_gid != ROOT_GID or
                digest_bytes(raw) != value):
            raise RuntimeError(f"published asset hash mismatch: {name}")
    checks_raw = read_reviewed_snapshot(
        path / "ci-check-runs.json", protected=True)
    runs_raw = read_reviewed_snapshot(
        path / "ci-workflow-runs.json", protected=True)
    if digest_bytes(checks_raw) != descriptor["ci_checks_sha256"]:
        raise RuntimeError("published CI checks descriptor binding mismatch")
    if digest_bytes(runs_raw) != descriptor["ci_runs_sha256"]:
        raise RuntimeError("published CI runs descriptor binding mismatch")
    verify_ci(
        checks_raw, descriptor["release_sha"], True,
        descriptor["required_checks"])
    verify_ci(
        runs_raw, descriptor["release_sha"], False,
        descriptor["required_workflows"])
    for name, expected_sha in descriptor["reviewed_inputs"].items():
        raw = read_reviewed_snapshot(path / name, protected=True)
        if digest_bytes(raw) != expected_sha:
            raise RuntimeError(
                f"published reviewed input descriptor mismatch: {name}")
    backup_raw = read_reviewed_snapshot(
        path / "trading-state-backup.original", protected=True)
    if digest_bytes(backup_raw) != descriptor["backup_original_sha256"]:
        raise RuntimeError("published backup descriptor binding mismatch")
    active = path / "active-attempt"
    active_info = os.lstat(active)
    active_raw = read_reviewed_snapshot(active, protected=True)
    attempt = path / "attempts/0001"
    attempt_info = os.lstat(attempt)
    if (active_raw != b"0001\n" or
            not stat.S_ISREG(active_info.st_mode) or
            active_info.st_uid != ROOT_UID or active_info.st_gid != ROOT_GID or
            stat.S_IMODE(active_info.st_mode) != 0o600 or
            not stat.S_ISDIR(attempt_info.st_mode) or
            attempt_info.st_uid != ROOT_UID or attempt_info.st_gid != ROOT_GID or
            stat.S_IMODE(attempt_info.st_mode) != 0o700 or
            os.listdir(attempt)):
        raise RuntimeError("published initial attempt is not pristine")


def published_state(stage, driver_dir, drivers, descriptor):
    stage_exists = stage.exists() or stage.is_symlink()
    driver_exists = driver_dir.exists() or driver_dir.is_symlink()
    if stage_exists:
        if not driver_exists:
            raise RuntimeError("stage exists without its atomically published driver")
        validate_driver(driver_dir, drivers)
        validate_stage(stage, descriptor)
        return "complete"
    if driver_exists:
        validate_driver(driver_dir, drivers)
        return "driver_only"
    return "empty"


def make_published_state_durable(
        state, stage, driver_dir, drivers, descriptor):
    """Finish a visible rename's durability before publishing its successor."""
    if state not in {"driver_only", "complete"}:
        raise RuntimeError("published durability state is invalid")
    fsync_tree(driver_dir)
    fsync_directory(driver_dir.parent)
    validate_driver(driver_dir, drivers)
    if state == "complete":
        fsync_tree(stage)
        fsync_directory(stage.parent)
        validate_stage(stage, descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--release-root", required=True, type=Path)
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--driver-dir", required=True, type=Path)
    parser.add_argument("--ci-checks", required=True, type=Path)
    parser.add_argument("--ci-runs", required=True, type=Path)
    parser.add_argument("--required-check", action="append", required=True)
    parser.add_argument("--required-workflow", action="append", required=True)
    parser.add_argument("--reviewed-input", required=True, type=Path)
    args = parser.parse_args(argv)
    if os.geteuid() != ROOT_UID:
        raise SystemExit("prepare_deployment.py must run as root")
    if (not sys.flags.isolated or not sys.flags.ignore_environment or
            not sys.flags.no_user_site or
            os.path.realpath(sys.executable) != os.path.realpath("/usr/bin/python3")):
        raise SystemExit(
            "prepare_deployment.py must run as /usr/bin/python3 -I")
    expected_release = Path(f"/opt/trader-releases/{args.release_sha}")
    expected_stage = Path(f"/var/lib/trading-deploy/{args.release_sha}")
    expected_driver = Path(f"/usr/local/lib/trading-deploy/{args.release_sha}")
    canonical_inputs = (args.ci_checks, args.ci_runs, args.reviewed_input)
    if (not SHA_RE.fullmatch(args.release_sha) or
            args.release_root != expected_release or
            args.stage != expected_stage or args.driver_dir != expected_driver or
            any(not path.is_absolute() or path.resolve() != path
                for path in canonical_inputs)):
        raise SystemExit("prepare paths do not match the canonical deployment roots")
    if (len(set(args.required_check)) != len(args.required_check) or
            len(set(args.required_workflow)) != len(args.required_workflow)):
        raise SystemExit("required CI names must be unique")
    require_protected_directory(args.stage.parent)
    require_protected_directory(args.driver_dir.parent)
    require_protected_directory(args.release_root.parent)
    require_protected_directory(args.reviewed_input)
    trading = args.release_root / "trading"
    verify_release_tree(args.release_root, trading)
    git = ["git", "-c", f"safe.directory={args.release_root}",
           "-C", str(args.release_root)]
    head = run([*git, "rev-parse", "HEAD"], capture_output=True).stdout.strip()
    if head != args.release_sha:
        raise SystemExit("release HEAD mismatch")
    reject_unreviewed_release_entries(args.release_root)
    if run([*git, "status", "--porcelain=v1", "--untracked-files=all"],
           capture_output=True).stdout:
        raise SystemExit("release worktree/index is not clean")
    run([
        "/usr/bin/python3", "-I", "-B",
        str(trading / "deployment_evidence.py"),
        "validate-venv", "--venv", str(trading / ".venv"),
    ], env=sterile_runtime_env())
    original = Path("/usr/local/sbin/trading-state-backup")
    checks_snapshot = read_reviewed_snapshot(args.ci_checks)
    runs_snapshot = read_reviewed_snapshot(args.ci_runs)
    reviewed_snapshots = {
        name: read_reviewed_snapshot(
            args.reviewed_input / name, protected=True)
        for name in REVIEWED_INPUTS
    }
    tracked_snapshots = {
        name: read_reviewed_snapshot(trading / name, protected=True)
        for name in TRACKED_REVIEWED_INPUTS
    }
    original_snapshot = read_reviewed_snapshot(original, protected=True)
    verify_ci(
        checks_snapshot, args.release_sha, True, args.required_check)
    verify_ci(
        runs_snapshot, args.release_sha, False, args.required_workflow)
    descriptor = {
        "schema_version": 1,
        "release_sha": args.release_sha,
        "prepare_tool_sha256": digest(Path(__file__)),
        "ci_checks_sha256": digest_bytes(checks_snapshot),
        "ci_runs_sha256": digest_bytes(runs_snapshot),
        "required_checks": sorted(args.required_check),
        "required_workflows": sorted(args.required_workflow),
        "reviewed_inputs": {
            **{
                name: digest_bytes(payload)
                for name, payload in reviewed_snapshots.items()
            },
            **{
                name: digest_bytes(payload)
                for name, payload in tracked_snapshots.items()
            },
        },
        "backup_original_sha256": digest_bytes(original_snapshot),
    }
    drivers = expected_drivers(trading, args.release_sha)
    state = published_state(args.stage, args.driver_dir, drivers, descriptor)
    if state != "empty":
        make_published_state_durable(
            state, args.stage, args.driver_dir, drivers, descriptor)
    if state == "complete":
        return 0
    driver_published = state == "driver_only"

    stage_tmp = Path(tempfile.mkdtemp(
        prefix=f".{args.stage.name}.tmp.", dir=args.stage.parent))
    driver_tmp = None
    os.chmod(stage_tmp, 0o700)
    try:
        write_snapshot(
            stage_tmp / "ci-check-runs.json", checks_snapshot, 0o600)
        write_snapshot(
            stage_tmp / "ci-workflow-runs.json", runs_snapshot, 0o600)
        for name, mode in REVIEWED_INPUTS.items():
            write_snapshot(
                stage_tmp / name, reviewed_snapshots[name], mode)
        for name, mode in TRACKED_REVIEWED_INPUTS.items():
            write_snapshot(stage_tmp / name, tracked_snapshots[name], mode)
        write_private(
            stage_tmp / "prepare-request.json",
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n")
        manifest_tree(args.release_root, stage_tmp / "reviewed-tracked.sha256")
        manifest_venv(args.release_root, trading, stage_tmp)
        write_snapshot(
            stage_tmp / "trading-state-backup.original",
            original_snapshot, 0o600)
        write_snapshot(
            stage_tmp / "trading-state-backup.reviewed",
            original_snapshot, 0o600)
        run(["patch", "--batch", "--fuzz=0",
             str(stage_tmp / "trading-state-backup.reviewed"),
             str(stage_tmp / "backup-script.patch")])
        os.chmod(stage_tmp / "trading-state-backup.reviewed", 0o755)
        run(["bash", "-n", str(stage_tmp / "trading-state-backup.reviewed")])
        write_asset_manifest(stage_tmp)
        attempts = stage_tmp / "attempts"
        attempts.mkdir(mode=0o700)
        (attempts / "0001").mkdir(mode=0o700)
        write_private(stage_tmp / "active-attempt", "0001\n")
        validate_stage(stage_tmp, descriptor)
        fsync_tree(stage_tmp)

        if not driver_published:
            driver_tmp = Path(tempfile.mkdtemp(
                prefix=f".{args.driver_dir.name}.tmp.", dir=args.driver_dir.parent))
            os.chmod(driver_tmp, 0o755)
            for name, payload in drivers.items():
                write_snapshot(driver_tmp / name, payload, 0o555)
            fsync_tree(driver_tmp)
            os.rename(driver_tmp, args.driver_dir)
            driver_tmp = None
            fsync_directory(args.driver_dir.parent)
            validate_driver(args.driver_dir, drivers)

        os.rename(stage_tmp, args.stage)
        fsync_directory(args.stage.parent)
        validate_stage(args.stage, descriptor)
    finally:
        if stage_tmp.exists():
            shutil.rmtree(stage_tmp)
        if driver_tmp is not None and driver_tmp.exists():
            shutil.rmtree(driver_tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
