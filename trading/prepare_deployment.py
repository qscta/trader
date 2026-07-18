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
from pathlib import Path


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DRIVER_FILES = (
    "deploy.sh",
    "emergency-stop.sh",
    "recover-deployment.sh",
    "deployment_evidence.py",
    "deployment_attempt.py",
)
REVIEWED_INPUTS = {
    "remove-one-confirmed-config-key.py": 0o755,
    "remove-one-confirmed-config-key.spec.json": 0o600,
    "writer-inventory.json": 0o600,
    "backup-script.patch": 0o600,
}
ROOT_UID = 0
ROOT_GID = 0


def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


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


def write_private(path, value):
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)


def copy_reviewed(source, target, mode=0o600):
    info = os.lstat(source)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError(f"reviewed input is not a single regular file: {source}")
    shutil.copyfile(source, target, follow_symlinks=False)
    os.chmod(target, mode)


def require_protected_directory(path):
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != ROOT_UID or
            info.st_gid != ROOT_GID or
            stat.S_IMODE(info.st_mode) & 0o022):
        raise RuntimeError(f"directory is not root-owned/protected: {path}")


def secure_release_tree(trading):
    """Make reviewed code/venv immutable before executing its interpreter."""
    for directory, dirnames, names in os.walk(trading, followlinks=False):
        directory_path = Path(directory)
        for name in [*dirnames, *names]:
            path = directory_path / name
            info = os.lstat(path)
            in_venv = path == trading / ".venv" or trading / ".venv" in path.parents
            if stat.S_ISLNK(info.st_mode):
                if not in_venv:
                    raise RuntimeError(f"code-tree symlink is forbidden: {path}")
                os.chown(path, 0, 0, follow_symlinks=False)
                continue
            os.chown(path, 0, 0, follow_symlinks=False)
            os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o022)
    root_info = os.lstat(trading)
    os.chown(trading, 0, 0, follow_symlinks=False)
    os.chmod(trading, stat.S_IMODE(root_info.st_mode) & ~0o022)


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


def verify_ci(path, sha, check_runs, required):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    items = payload.get("check_runs") if check_runs and isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        raise RuntimeError("CI evidence is empty")
    name_key = "name" if check_runs else "workflowName"
    head_key = "head_sha" if check_runs else "headSha"
    if not check_runs:
        latest = {}
        for item in items:
            name = item.get(name_key)
            if not isinstance(name, str) or not name:
                raise RuntimeError("workflow evidence has no name")
            if name not in latest or str(item.get("createdAt", "")) > str(
                    latest[name].get("createdAt", "")):
                latest[name] = item
        items = list(latest.values())
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
    with open(stage / "reviewed-venv.sha256", "w", encoding="utf-8") as hashes, \
            open(stage / "reviewed-venv-files.txt", "w", encoding="utf-8") as files:
        for path in entries:
            relative = path.relative_to(release_root).as_posix()
            hashes.write(f"{digest(path)}  {relative}\n")
            kind = "l" if path.is_symlink() else "f"
            files.write(f"{kind} {stat.S_IMODE(os.lstat(path).st_mode):o} {relative}\n")
    os.chmod(stage / "reviewed-venv.sha256", 0o600)
    os.chmod(stage / "reviewed-venv-files.txt", 0o600)
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


def validate_stage(path, descriptor):
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != ROOT_UID or
            info.st_gid != ROOT_GID or
            stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("published stage directory is unsafe")
    with open(path / "prepare-request.json", encoding="utf-8") as handle:
        if json.load(handle) != descriptor:
            raise RuntimeError("published stage request binding mismatch")
    manifest = {}
    for line in (path / "reviewed-assets.sha256").read_text(encoding="ascii").splitlines():
        value, separator, name = line.partition("  ")
        if (not separator or not re.fullmatch(r"[0-9a-f]{64}", value) or
                not name or "/" in name or name in manifest):
            raise RuntimeError("invalid published asset manifest")
        manifest[name] = value
    root_files = {item.name for item in path.iterdir() if item.is_file()}
    root_dirs = {item.name for item in path.iterdir() if item.is_dir()}
    if root_files - {"reviewed-assets.sha256", "active-attempt"} != set(manifest):
        raise RuntimeError("published asset file set mismatch")
    if root_dirs != {"attempts"} or set(os.listdir(path / "attempts")) != {"0001"}:
        raise RuntimeError("published stage directory set mismatch")
    for name, value in manifest.items():
        item = os.lstat(path / name)
        if (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 or
                item.st_uid != ROOT_UID or item.st_gid != ROOT_GID or
                digest(path / name) != value):
            raise RuntimeError(f"published asset hash mismatch: {name}")
    active = path / "active-attempt"
    active_info = os.lstat(active)
    attempt = path / "attempts/0001"
    attempt_info = os.lstat(attempt)
    if (active.read_text(encoding="ascii") != "0001\n" or
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
    if (not SHA_RE.fullmatch(args.release_sha) or
            not args.release_root.is_absolute() or
            args.release_root.resolve() != args.release_root or
            not args.stage.is_absolute() or not args.driver_dir.is_absolute()):
        raise SystemExit("invalid SHA or non-absolute destination")
    if (len(set(args.required_check)) != len(args.required_check) or
            len(set(args.required_workflow)) != len(args.required_workflow)):
        raise SystemExit("required CI names must be unique")
    require_protected_directory(args.stage.parent)
    require_protected_directory(args.driver_dir.parent)
    require_protected_directory(args.release_root.parent)
    release_info = os.lstat(args.release_root)
    if not stat.S_ISDIR(release_info.st_mode):
        raise SystemExit("release root must be a real directory")
    os.chown(args.release_root, 0, 0, follow_symlinks=False)
    os.chmod(args.release_root, stat.S_IMODE(release_info.st_mode) & ~0o022)
    trading = args.release_root / "trading"
    git = ["git", "-c", f"safe.directory={args.release_root}",
           "-C", str(args.release_root)]
    head = run([*git, "rev-parse", "HEAD"], capture_output=True).stdout.strip()
    if head != args.release_sha:
        raise SystemExit("release HEAD mismatch")
    if run([*git, "status", "--porcelain=v1", "--untracked-files=all"],
           capture_output=True).stdout:
        raise SystemExit("release worktree/index is not clean")
    secure_release_tree(trading)
    run([
        "/usr/bin/python3", "-I", "-B",
        str(trading / "deployment_evidence.py"),
        "validate-venv", "--venv", str(trading / ".venv"),
    ], env=sterile_runtime_env())
    verify_ci(args.ci_checks, args.release_sha, True, args.required_check)
    verify_ci(args.ci_runs, args.release_sha, False, args.required_workflow)
    original = Path("/usr/local/sbin/trading-state-backup")
    descriptor = {
        "schema_version": 1,
        "release_sha": args.release_sha,
        "prepare_tool_sha256": digest(Path(__file__)),
        "ci_checks_sha256": digest(args.ci_checks),
        "ci_runs_sha256": digest(args.ci_runs),
        "required_checks": sorted(args.required_check),
        "required_workflows": sorted(args.required_workflow),
        "reviewed_inputs": {
            name: digest(args.reviewed_input / name) for name in REVIEWED_INPUTS},
        "backup_original_sha256": digest(original),
    }
    drivers = expected_drivers(trading, args.release_sha)
    state = published_state(args.stage, args.driver_dir, drivers, descriptor)
    if state == "complete":
        return 0
    driver_published = state == "driver_only"

    stage_tmp = Path(tempfile.mkdtemp(
        prefix=f".{args.stage.name}.tmp.", dir=args.stage.parent))
    driver_tmp = None
    os.chmod(stage_tmp, 0o700)
    try:
        copy_reviewed(args.ci_checks, stage_tmp / "ci-check-runs.json")
        copy_reviewed(args.ci_runs, stage_tmp / "ci-workflow-runs.json")
        for name, mode in REVIEWED_INPUTS.items():
            copy_reviewed(args.reviewed_input / name, stage_tmp / name, mode)
        write_private(
            stage_tmp / "prepare-request.json",
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n")
        manifest_tree(args.release_root, stage_tmp / "reviewed-tracked.sha256")
        manifest_venv(args.release_root, trading, stage_tmp)
        copy_reviewed(original, stage_tmp / "trading-state-backup.original")
        shutil.copyfile(original, stage_tmp / "trading-state-backup.reviewed")
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
        fsync_tree(stage_tmp)

        if not driver_published:
            driver_tmp = Path(tempfile.mkdtemp(
                prefix=f".{args.driver_dir.name}.tmp.", dir=args.driver_dir.parent))
            os.chmod(driver_tmp, 0o755)
            for name, payload in drivers.items():
                target = driver_tmp / name
                target.write_bytes(payload)
                os.chmod(target, 0o555)
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
