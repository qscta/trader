import importlib.util
import json
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parent
DEPLOY = ROOT / "deploy.sh"
EMERGENCY = ROOT / "emergency-stop.sh"
RECOVER = ROOT / "recover-deployment.sh"
PREPARE = ROOT / "prepare_deployment.py"
ATTEMPT = ROOT / "deployment_attempt.py"
GATE = ROOT / "deployment_no_open_gate.py"
OLD_GATE = ROOT / "deployment_old_runner_gate.py"
EVIDENCE = ROOT / "deployment_evidence.py"
NOTES = ROOT / "DEPLOY_NOTES.md"
PLACEHOLDER = "__RELEASE_SHA__"
TEST_SHA = "a" * 40


def load_evidence_module():
    spec = importlib.util.spec_from_file_location("deployment_evidence", EVIDENCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_old_gate_module():
    spec = importlib.util.spec_from_file_location(
        "deployment_old_runner_gate", OLD_GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_attempt_module():
    spec = importlib.util.spec_from_file_location(
        "deployment_attempt", ATTEMPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeploymentArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.deploy = DEPLOY.read_text(encoding="utf-8")
        cls.emergency = EMERGENCY.read_text(encoding="utf-8")
        cls.recover = RECOVER.read_text(encoding="utf-8")
        cls.prepare = PREPARE.read_text(encoding="utf-8")
        cls.attempt = ATTEMPT.read_text(encoding="utf-8")
        cls.gate = GATE.read_text(encoding="utf-8")
        cls.old_gate = OLD_GATE.read_text(encoding="utf-8")
        cls.notes = NOTES.read_text(encoding="utf-8")
        cls.evidence = load_evidence_module()
        cls.old_gate_module = load_old_gate_module()
        cls.attempt_module = load_attempt_module()

    def test_actual_lock_holder_process_is_bound(self):
        module = self.old_gate_module
        worker_pid = 4321
        service_cgroup = "/system.slice/trading.service"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            proc = root / "proc"
            cgroups = root / "cgroup"
            expected_cwd = root / "trading"
            expected_cwd.mkdir()
            lock = root / "runner.lock"
            lock.write_text(f"{worker_pid}\n", encoding="ascii")
            lock.chmod(0o600)
            info = lock.stat()
            (proc / "sys/kernel/random").mkdir(parents=True)
            (proc / "sys/kernel/random/boot_id").write_text(
                "12345678-1234-1234-1234-123456789abc\n", encoding="ascii")
            (proc / "locks").write_text(
                f"1: FLOCK ADVISORY WRITE {worker_pid} "
                f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:"
                f"{info.st_ino} 0 EOF\n",
                encoding="ascii")
            process = proc / str(worker_pid)
            (process / "fd").mkdir(parents=True)
            (process / "stat").write_text(
                f"{worker_pid} (worker) S " + "0 " * 18 + "12345\n",
                encoding="ascii")
            (process / "cgroup").write_text(
                f"0::{service_cgroup}\n", encoding="ascii")
            os.symlink(expected_cwd, process / "cwd")
            os.symlink(lock, process / "fd/7")
            cgroup_dir = cgroups / service_cgroup.lstrip("/")
            cgroup_dir.mkdir(parents=True)
            (cgroup_dir / "cgroup.procs").write_text(
                f"{worker_pid}\n", encoding="ascii")
            ubuntu = SimpleNamespace(
                pw_uid=os.getuid(), pw_gid=os.getgid())
            with mock.patch.object(module.pwd, "getpwnam", return_value=ubuntu):
                binding = module.capture_process_binding(
                    str(lock), service_cgroup, str(expected_cwd),
                    proc_root=str(proc), cgroup_root=str(cgroups))
            self.assertEqual(worker_pid, binding["worker_pid"])
            self.assertEqual(
                "12345678-1234-1234-1234-123456789abc",
                binding["boot_id"])
            for schema in (True, 1.0):
                malformed_binding = dict(binding, schema_version=schema)
                with self.subTest(
                        kind="process_binding", schema=schema), \
                        self.assertRaisesRegex(module.GateError, "字段非法"):
                    module._validate_process_binding(malformed_binding)
                malformed_boundary = {
                    "schema_version": schema,
                    "mode": "runtime_sentinel",
                    "handshake": {
                        "protocol": "trade-lock-no-open-v1",
                        "status": "maintenance_blocked",
                        "inflight_open_boundary_drained": True,
                        "sentinel": {
                            "schema_version": 1,
                            "kind": "old_runner_no_open",
                            "release_sha": TEST_SHA,
                            "nonce": "d" * 64,
                            "worker_pid": worker_pid,
                            "path": str(root / ".maintenance_no_open"),
                            "dev": 1,
                            "ino": 2,
                        },
                    },
                    "process_binding": binding,
                }
                with self.subTest(
                        kind="runtime_sentinel_boundary", schema=schema), \
                        self.assertRaisesRegex(module.GateError, "mode 非法"):
                    module._validate_handshake_boundary(malformed_boundary)
            for schema in (True, 1.0):
                sentinel = {
                    "schema_version": schema,
                    "nonce": "d" * 64,
                    "release_sha": TEST_SHA,
                }
                with mock.patch.object(
                        module, "_runtime_sentinel",
                        return_value=(sentinel, SimpleNamespace(
                            st_dev=1, st_ino=2))), \
                        self.subTest(
                            kind="release_sentinel", schema=schema), \
                        self.assertRaisesRegex(module.GateError, "schema"):
                    module.verify_release_sentinel("/unused", TEST_SHA)
            wrong_worker = {
                "schema_version": 1,
                "mode": "runtime_sentinel",
                "handshake": {
                    "protocol": "trade-lock-no-open-v1",
                    "status": "maintenance_blocked",
                    "inflight_open_boundary_drained": True,
                    "sentinel": {
                        "schema_version": 1,
                        "kind": "old_runner_no_open",
                        "release_sha": TEST_SHA,
                        "nonce": "d" * 64,
                        "worker_pid": worker_pid + 1,
                        "path": str(root / ".maintenance_no_open"),
                        "dev": 1,
                        "ino": 2,
                    },
                },
                "process_binding": binding,
            }
            with self.assertRaisesRegex(module.GateError, "FLOCK"):
                module._validate_handshake_boundary(wrong_worker)
            (proc / "locks").write_text(
                f"1: FLOCK ADVISORY WRITE 9876 "
                f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:"
                f"{info.st_ino} 0 EOF\n",
                encoding="ascii")
            with mock.patch.object(module.pwd, "getpwnam", return_value=ubuntu), \
                    self.assertRaisesRegex(module.GateError, "FLOCK"):
                module.capture_process_binding(
                    str(lock), service_cgroup, str(expected_cwd),
                    proc_root=str(proc), cgroup_root=str(cgroups))

    def test_old_runner_artifacts_are_bound_to_the_exact_release(self):
        module = self.old_gate_module
        binding = {
            "schema_version": 1,
            "boot_id": "12345678-1234-1234-1234-123456789abc",
            "worker_pid": 4321,
            "worker_start_ticks": 12345,
            "service_cgroup": "/system.slice/trading.service",
            "worker_cwd": "/opt/trader-releases/" + TEST_SHA + "/trading",
            "runner_lock": {
                "path": "/var/lib/trading-runtime/" + TEST_SHA +
                        "/.runtime/runner.lock",
                "dev": 1,
                "ino": 2,
            },
        }
        boundary = {
            "schema_version": 1,
            "mode": "runtime_sentinel",
            "handshake": {
                "protocol": "trade-lock-no-open-v1",
                "status": "maintenance_blocked",
                "inflight_open_boundary_drained": True,
                "sentinel": {
                    "schema_version": 1,
                    "kind": "old_runner_no_open",
                    "release_sha": TEST_SHA,
                    "nonce": "d" * 64,
                    "worker_pid": 4321,
                    "path": "/var/lib/trading-runtime/" + TEST_SHA +
                            "/.maintenance_no_open",
                    "dev": 3,
                    "ino": 4,
                },
            },
            "process_binding": binding,
        }
        recovery = {
            "mode": "recovery_inactive",
            "release_sha": TEST_SHA,
            "sentinel": {
                "path": "/var/lib/trading-runtime/" + TEST_SHA +
                        "/.maintenance_no_open",
                "dev": 3,
                "ino": 4,
            },
        }
        intent = {
            "schema_version": 1,
            "kind": "old_runner_no_open_arm_intent",
            "release_sha": TEST_SHA,
        }
        for artifact, expected_mode in (
                (boundary, "runtime_sentinel"),
                (recovery, "recovery_inactive")):
            with mock.patch.object(
                    module, "_protected_root_file", return_value=artifact):
                self.assertEqual(
                    expected_mode,
                    module.boundary_summary("/unused", TEST_SHA)["mode"])
                with self.assertRaises(module.GateError):
                    module.boundary_summary("/unused", "b" * 40)
        with mock.patch.object(
                module, "_protected_root_file", return_value=intent):
            self.assertEqual(
                intent, module.verify_arm_intent("/unused", TEST_SHA))
            with self.assertRaises(module.GateError):
                module.verify_arm_intent("/unused", "b" * 40)

    def test_maintenance_continuity_accepts_restart_but_binds_current_worker(self):
        module = self.old_gate_module
        current = {
            "schema_version": 1,
            "boot_id": "12345678-1234-1234-1234-123456789abc",
            "worker_pid": 9002,
            "worker_start_ticks": 67890,
            "service_cgroup": "/system.slice/trading.service",
            "worker_cwd": "/srv/trading",
            "runner_lock": {
                "path": "/srv/data/.runtime/runner.lock",
                "dev": 1,
                "ino": 2,
            },
        }
        payload = {
            "schema_version": 1,
            "kind": "old_runner_no_open",
            "release_sha": TEST_SHA,
            "nonce": "d" * 64,
            "worker_pid": 4321,
        }
        info = SimpleNamespace(
            st_dev=3, st_ino=4, st_mode=stat.S_IFREG | 0o600,
            st_uid=os.geteuid(), st_gid=os.getegid(), st_nlink=1,
            st_size=128, st_mtime_ns=123456789,
        )
        capability = {
            "protocol": "trade-lock-no-open-v1",
            "worker_pid": current["worker_pid"],
            "sentinel_path": "/srv/data/.maintenance_no_open",
            "maintenance_active": True,
        }
        intent = {
            "schema_version": 1,
            "kind": "old_runner_no_open_arm_intent",
            "release_sha": TEST_SHA,
        }
        with mock.patch.object(
                module, "capture_process_binding",
                side_effect=[current, current]), \
                mock.patch.object(
                    module, "_protected_root_file", return_value=intent), \
                mock.patch.object(
                    module, "_runtime_sentinel", return_value=(payload, info)), \
                mock.patch.object(
                    module, "_runtime_gate_object", return_value=info), \
                mock.patch.object(
                    module, "_durabilize_runtime_gate", return_value=info), \
                mock.patch.object(
                    module, "capability", return_value=capability), \
                mock.patch.object(module, "verify_http_block") as http_block:
            result = module.verify_maintenance_continuity(
                TEST_SHA, current["runner_lock"]["path"],
                current["service_cgroup"], current["worker_cwd"],
                "/srv/data", arm_intent_path="/intent")
        self.assertEqual(4321, result["creator_worker_pid"])
        self.assertEqual(9002, result["current_worker_pid"])
        self.assertTrue(result["inflight_open_boundary_drained"])
        http_block.assert_called_once_with()

    def test_final_drain_binds_trade_lock_worker_and_release_sentinel(self):
        module = self.old_gate_module
        binding = {
            "schema_version": 1,
            "boot_id": "12345678-1234-1234-1234-123456789abc",
            "worker_pid": 9002,
            "worker_start_ticks": 67890,
            "service_cgroup": "/system.slice/trading.service",
            "worker_cwd": "/srv/trading",
            "runner_lock": {
                "path": "/srv/data/.runtime/runner.lock",
                "dev": 1,
                "ino": 2,
            },
        }
        result = {
            "protocol": "trade-lock-no-open-v1",
            "status": "maintenance_blocked",
            "inflight_open_boundary_drained": True,
            "worker_pid": 9002,
            "sentinel": {
                "path": "/srv/data/.maintenance_no_open",
                "dev": 3,
                "ino": 4,
            },
        }
        payload = {
            "schema_version": 1,
            "nonce": "d" * 64,
            "release_sha": TEST_SHA,
        }
        info = SimpleNamespace(st_dev=3, st_ino=4)
        with mock.patch.object(
                module, "capture_process_binding",
                side_effect=[binding, binding]), \
                mock.patch.object(
                    module, "_request", return_value=(200, result)) as request, \
                mock.patch.object(
                    module, "_runtime_sentinel", return_value=(payload, info)), \
                mock.patch.object(module, "verify_http_block") as http_block:
            observed = module.drain_maintenance_boundary(
                TEST_SHA, binding["runner_lock"]["path"],
                binding["service_cgroup"], binding["worker_cwd"],
                "/srv/data")

        self.assertEqual(result, observed)
        request.assert_called_once_with(
            "GET", "/api/deployment/drain-no-open")
        http_block.assert_called_once_with()

    def test_maintenance_continuity_accepts_partial_v1_inode_after_lock_drain(self):
        module = self.old_gate_module
        current = {
            "schema_version": 1,
            "boot_id": "12345678-1234-1234-1234-123456789abc",
            "worker_pid": 9002,
            "worker_start_ticks": 67890,
            "service_cgroup": "/system.slice/trading.service",
            "worker_cwd": "/srv/trading",
            "runner_lock": {
                "path": "/srv/data/.runtime/runner.lock",
                "dev": 1,
                "ino": 2,
            },
        }
        info = SimpleNamespace(
            st_dev=3, st_ino=4, st_mode=stat.S_IFREG | 0o600,
            st_uid=os.geteuid(), st_gid=os.getegid(), st_nlink=1,
            st_size=2, st_mtime_ns=123,
        )
        intent = {
            "schema_version": 1,
            "kind": "old_runner_no_open_arm_intent",
            "release_sha": TEST_SHA,
        }
        capability = {
            "protocol": "trade-lock-no-open-v1",
            "worker_pid": current["worker_pid"],
            "sentinel_path": "/srv/data/.maintenance_no_open",
            "maintenance_active": True,
        }
        ubuntu = SimpleNamespace(
            pw_uid=os.geteuid(), pw_gid=os.getegid())
        with mock.patch.object(
                module, "capture_process_binding",
                side_effect=[current, current]), \
                mock.patch.object(
                    module, "_protected_root_file", return_value=intent), \
                mock.patch.object(
                    module, "_runtime_gate_object", return_value=info), \
                mock.patch.object(
                    module, "_runtime_sentinel",
                    side_effect=module.GateError("opaque")), \
                mock.patch.object(
                    module, "_durabilize_runtime_gate", return_value=info), \
                mock.patch.object(
                    module.pwd, "getpwnam", return_value=ubuntu), \
                mock.patch.object(
                    module, "capability", return_value=capability), \
                mock.patch.object(module, "verify_http_block"):
            result = module.verify_maintenance_continuity(
                TEST_SHA, current["runner_lock"]["path"],
                current["service_cgroup"], current["worker_cwd"],
                "/srv/data", arm_intent_path="/intent")
        self.assertFalse(result["strict_creator_evidence"])
        self.assertIsNone(result["creator_worker_pid"])
        self.assertTrue(result["inflight_open_boundary_drained"])

    def test_maintenance_continuity_accepts_offline_lock_armed_release_gate(self):
        module = self.old_gate_module
        current = {
            "schema_version": 1,
            "boot_id": "12345678-1234-1234-1234-123456789abc",
            "worker_pid": 9002,
            "worker_start_ticks": 67890,
            "service_cgroup": "/system.slice/trading.service",
            "worker_cwd": "/srv/trading",
            "runner_lock": {
                "path": "/srv/data/.runtime/runner.lock",
                "dev": 1,
                "ino": 2,
            },
        }
        info = SimpleNamespace(
            st_dev=3, st_ino=4, st_mode=stat.S_IFREG | 0o600,
            st_uid=os.geteuid(), st_gid=os.getegid(), st_nlink=1,
            st_size=128, st_mtime_ns=123456789,
        )
        capability = {
            "protocol": "trade-lock-no-open-v1",
            "worker_pid": current["worker_pid"],
            "sentinel_path": "/srv/data/.maintenance_no_open",
            "maintenance_active": True,
        }
        intent = {
            "schema_version": 1,
            "kind": "old_runner_no_open_arm_intent",
            "release_sha": TEST_SHA,
        }
        with mock.patch.object(
                module, "capture_process_binding",
                side_effect=[current, current]), \
                mock.patch.object(
                    module, "_protected_root_file", return_value=intent), \
                mock.patch.object(
                    module, "_runtime_sentinel", return_value=({
                        "schema_version": 1,
                        "nonce": "d" * 64,
                        "release_sha": TEST_SHA,
                    }, info)), \
                mock.patch.object(
                    module, "_runtime_gate_object", return_value=info), \
                mock.patch.object(
                    module, "_durabilize_runtime_gate", return_value=info), \
                mock.patch.object(
                    module, "capability", return_value=capability), \
                mock.patch.object(module, "verify_http_block"):
            result = module.verify_maintenance_continuity(
                TEST_SHA, current["runner_lock"]["path"],
                current["service_cgroup"], current["worker_cwd"],
                "/srv/data", arm_intent_path="/intent")
        self.assertFalse(result["strict_creator_evidence"])
        self.assertIsNone(result["creator_worker_pid"])
        self.assertTrue(result["inflight_open_boundary_drained"])

    def test_visible_runtime_gate_is_fsynced_before_recovery_accepts_it(self):
        module = self.old_gate_module
        with tempfile.TemporaryDirectory() as directory:
            directory = os.path.realpath(directory)
            path = os.path.join(directory, ".maintenance_no_open")
            with open(path, "wb") as handle:
                handle.write(b'{"schema_version":1')
            os.chmod(path, 0o600)
            observed = module._runtime_gate_object(path, "test gate")
            real_fsync = os.fsync
            with mock.patch.object(
                    module.os, "fsync", wraps=real_fsync) as fsync:
                durable = module._durabilize_runtime_gate(
                    path, observed, "test gate")
            self.assertEqual(observed.st_ino, durable.st_ino)
            self.assertEqual(2, fsync.call_count)

    def test_g0_journal_and_recovery_seed_are_write_once(self):
        module = self.attempt_module
        driver = "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            attempt = root / "0001"
            next_attempt = root / "0002"
            source = root / "trading"
            attempt.mkdir(mode=0o700)
            next_attempt.mkdir(mode=0o700)
            (source / ".runtime").mkdir(parents=True, mode=0o700)
            runner_lock = source / ".runtime/runner.lock"
            runner_lock.write_bytes(b"")
            runner_lock.chmod(0o600)
            identity = SimpleNamespace(
                pw_uid=os.geteuid(), pw_gid=os.getegid())
            with mock.patch.object(module, "ROOT_UID", os.geteuid()), \
                    mock.patch.object(module, "ROOT_GID", os.getegid()), \
                    mock.patch.object(module, "LIVE_TRADING", str(source)), \
                    mock.patch.object(module.pwd, "getpwnam",
                                      return_value=identity):
                module.init_journal(
                    str(attempt), TEST_SHA, "0001", driver)
                g0 = module.advance_journal(
                    str(attempt), TEST_SHA, "0001", driver,
                    "PREPARED", "G0", [
                        "old_gate_mode=recovery_inactive",
                        f"old_gate_evidence_sha256={'c' * 64}",
                    ])
                phase_digest = module._digest(g0)
                seed = module.create_recovery_seed(
                    str(next_attempt), TEST_SHA, "0002", driver,
                    "0001", "G0", phase_digest,
                    str(source), str(source),
                    "2026-07-19", "requires_migration")
                self.assertEqual(
                    seed,
                    module.read_recovery_seed(
                        str(next_attempt), TEST_SHA, "0002", driver))
                module.init_journal(
                    str(next_attempt), TEST_SHA, "0002", driver)
                with self.assertRaisesRegex(module.JournalError, "unused"):
                    module.create_recovery_seed(
                        str(next_attempt), TEST_SHA, "0002", driver,
                        "0001", "G0", phase_digest,
                        str(source), str(source),
                        "2026-07-19", "requires_migration")

    def test_prepared_pending_and_runtime_init_are_crash_idempotent(self):
        module = self.attempt_module
        driver = "d" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            attempt = root / "0001"
            attempt.mkdir(mode=0o700)
            real_write = module.os.write
            write_failed = False

            def fail_after_partial_write(fd, payload):
                nonlocal write_failed
                if not write_failed:
                    write_failed = True
                    real_write(fd, payload[:7])
                    raise OSError("injected partial journal write")
                return real_write(fd, payload)

            with mock.patch.object(module, "ROOT_UID", os.geteuid()), \
                    mock.patch.object(module, "ROOT_GID", os.getegid()), \
                    mock.patch.object(module.sys, "platform", "darwin"), \
                    mock.patch.object(module.os, "write",
                                      side_effect=fail_after_partial_write), \
                    self.assertRaises(OSError):
                module.init_journal(str(attempt), TEST_SHA, "0001", driver)
            self.assertEqual([], sorted(path.name for path in attempt.iterdir()))

            real_publish = module._link_named_noreplace
            failed = False

            def fail_first_publish(*args, **kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("injected publication failure")
                return real_publish(*args, **kwargs)

            identity = SimpleNamespace(
                pw_uid=os.geteuid(), pw_gid=os.getegid())
            with mock.patch.object(module, "ROOT_UID", os.geteuid()), \
                    mock.patch.object(module, "ROOT_GID", os.getegid()), \
                    mock.patch.object(module.sys, "platform", "darwin"), \
                    mock.patch.object(module, "_link_named_noreplace",
                                      side_effect=fail_first_publish), \
                    self.assertRaises(OSError):
                module.init_journal(str(attempt), TEST_SHA, "0001", driver)
            self.assertEqual([], sorted(path.name for path in attempt.iterdir()))
            with mock.patch.object(module, "ROOT_UID", os.geteuid()), \
                    mock.patch.object(module, "ROOT_GID", os.getegid()), \
                    mock.patch.object(module.sys, "platform", "darwin"):
                payload = module.init_journal(
                    str(attempt), TEST_SHA, "0001", driver)
            self.assertEqual("PREPARED", payload["phase"])
            self.assertEqual(
                ["phase-00-prepared.json"],
                sorted(path.name for path in attempt.iterdir()))

            runtime_base = root / "runtime"
            runtime_base.mkdir(mode=0o755)
            real_chown = module.os.chown
            chown_failed = False

            def fail_first_chown(*args, **kwargs):
                nonlocal chown_failed
                if not chown_failed:
                    chown_failed = True
                    raise OSError("injected chown crash")
                return real_chown(*args, **kwargs)

            previous_umask = os.umask(0o077)
            try:
                with mock.patch.object(module, "ROOT_UID", os.geteuid()), \
                        mock.patch.object(module, "ROOT_GID", os.getegid()), \
                        mock.patch.object(module.pwd, "getpwnam",
                                          return_value=identity), \
                        mock.patch.object(module.os, "chown",
                                          side_effect=fail_first_chown), \
                        self.assertRaises(OSError):
                    module.init_runtime(TEST_SHA, base=str(runtime_base))
            finally:
                os.umask(previous_umask)
            with mock.patch.object(module, "ROOT_UID", os.geteuid()), \
                    mock.patch.object(module, "ROOT_GID", os.getegid()), \
                    mock.patch.object(module.pwd, "getpwnam",
                                      return_value=identity):
                result = module.init_runtime(TEST_SHA, base=str(runtime_base))
            self.assertTrue(Path(result["runner_lock"]).is_file())

    def test_active_pointer_switch_survives_partial_pending_publication(self):
        module = self.attempt_module
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            stage = base / TEST_SHA
            stage.mkdir(mode=0o700)
            active = stage / "active-attempt"
            active.write_text("0001\n", encoding="ascii")
            active.chmod(0o600)
            stage_fd = os.open(stage, os.O_RDONLY)
            real_read = module.os.read
            try:
                with mock.patch.object(module, 'ROOT_UID', os.geteuid()), \
                        mock.patch.object(module, 'ROOT_GID', os.getegid()), \
                        mock.patch.object(module.os, 'read',
                        side_effect=lambda fd, size: real_read(fd, min(size, 2))):
                    self.assertEqual(
                        '0001', module._read_attempt_pointer(
                            stage_fd, 'active-attempt'))
            finally:
                os.close(stage_fd)
            real_write = module.os.write
            write_failed = False

            def fail_after_partial_write(fd, payload):
                nonlocal write_failed
                if not write_failed:
                    write_failed = True
                    real_write(fd, payload[:2])
                    raise OSError("injected pointer write crash")
                return real_write(fd, payload)

            common = (
                mock.patch.object(module, "ROOT_UID", os.geteuid()),
                mock.patch.object(module, "ROOT_GID", os.getegid()),
                mock.patch.object(module, "DEPLOY_STAGE_BASE", str(base)),
                mock.patch.object(module.sys, "platform", "darwin"),
            )
            with common[0], common[1], common[2], common[3], \
                    mock.patch.object(
                        module.os, "write", side_effect=fail_after_partial_write), \
                    self.assertRaises(OSError):
                module.switch_active_attempt(TEST_SHA, "0001", "0002")
            self.assertEqual("0001\n", active.read_text(encoding="ascii"))
            self.assertFalse((stage / ".active-attempt.pending").exists())

            stage_fd = os.open(stage, os.O_RDONLY)
            try:
                with mock.patch.object(module.sys, "platform", "darwin"):
                    module._write_bytes_once(
                        stage_fd, ".active-attempt.pending", b"0002\n")
            finally:
                os.close(stage_fd)
            with mock.patch.object(module, "ROOT_UID", os.geteuid()), \
                    mock.patch.object(module, "ROOT_GID", os.getegid()), \
                    mock.patch.object(module, "DEPLOY_STAGE_BASE", str(base)):
                self.assertEqual(
                    {"active_attempt": "0002"},
                    module.switch_active_attempt(TEST_SHA, "0001", "0002"),
                )
                self.assertEqual(
                    {"active_attempt": "0002"},
                    module.switch_active_attempt(TEST_SHA, "0001", "0002"),
                )
            self.assertEqual("0002\n", active.read_text(encoding="ascii"))
            self.assertFalse((stage / ".active-attempt.pending").exists())

    def test_old_runner_evidence_uses_strict_json(self):
        with self.assertRaisesRegex(
                self.old_gate_module.GateError, "duplicate|重复"):
            self.old_gate_module._loads_strict(
                b'{"mode":"x","mode":"y"}', "test evidence")
        with self.assertRaises(self.old_gate_module.GateError):
            self.old_gate_module._loads_strict(
                b'{"value":NaN}', "test evidence")

    def test_shell_syntax_and_rendered_syntax(self):
        for path in (DEPLOY, EMERGENCY, RECOVER):
            subprocess.run(["bash", "-n", str(path)], check=True)
            rendered = path.read_text(encoding="utf-8").replace(
                PLACEHOLDER, TEST_SHA)
            self.assertNotIn(PLACEHOLDER, rendered)
            with tempfile.NamedTemporaryFile("w", suffix=".sh") as handle:
                handle.write(rendered)
                handle.flush()
                subprocess.run(["bash", "-n", handle.name], check=True)

    def test_driver_entry_is_sterile_against_path_bash_env_and_functions(self):
        expected_shebang = (
            "#!/usr/bin/env -S -i PATH=/usr/sbin:/usr/bin:/sbin:/bin "
            "TRADING_STERILE_DRIVER=1 /bin/bash --noprofile --norc")
        for source in (self.deploy, self.emergency, self.recover):
            self.assertTrue(source.startswith(expected_shebang + "\n"))
            self.assertIn("exec /usr/bin/env -i PATH=", source)
            self.assertIn("readonly PATH", source)
            self.assertIn("umask 077", source)

        header = self.deploy.split("set -Eeuo pipefail", 1)[0]
        harness = header + "set -Eeuo pipefail\n" + (
            "declare -F systemctl >/dev/null && exit 91\n"
            "printf '%s|%s\\n' \"$PATH\" \"$TRADING_STERILE_DRIVER\"\n")
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "sterile-driver"
            hook = Path(directory) / "bash-env"
            marker = Path(directory) / "hook-ran"
            script.write_text(harness, encoding="utf-8")
            script.chmod(0o755)
            hook.write_text(
                f"/usr/bin/touch {marker}\n", encoding="utf-8")
            env = dict(os.environ)
            env.update({
                "PATH": directory,
                "BASH_ENV": str(hook),
                "BASH_FUNC_systemctl%%": "() { return 0; }",
            })
            result = subprocess.run(
                [str(script)], check=True, capture_output=True,
                text=True, env=env)
            self.assertEqual(
                "/usr/sbin:/usr/bin:/sbin:/bin|1\n", result.stdout)
            self.assertFalse(marker.exists())

    def test_prepare_entry_requires_absolute_isolated_system_python(self):
        self.assertTrue(self.prepare.startswith("#!/usr/bin/python3 -I\n"))
        self.assertIn("sys.flags.isolated", self.prepare)
        self.assertIn('os.path.realpath("/usr/bin/python3")', self.prepare)
        self.assertIn("os.path.realpath('/usr/bin/python3')", self.old_gate)
        self.assertIn('"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"', self.prepare)

    def test_placeholder_is_single_and_consistent(self):
        self.assertEqual(self.deploy.count(PLACEHOLDER), 1)
        self.assertEqual(self.emergency.count(PLACEHOLDER), 1)
        self.assertEqual(self.recover.count(PLACEHOLDER), 1)
        self.assertEqual(self.deploy.count("EXPECTED_SHA='__RELEASE_SHA__'"), 1)
        self.assertEqual(
            self.emergency.count("EXPECTED_SHA='__RELEASE_SHA__'"), 1)
        self.assertEqual(
            self.recover.count("EXPECTED_SHA='__RELEASE_SHA__'"), 1)
        self.assertNotIn("REPLACE_WITH", self.deploy)
        self.assertNotIn("REPLACE_WITH", self.emergency)

    def test_fail_safe_and_persistent_block_precede_first_stop(self):
        block = self.deploy.index('run_emergency --install-block-only')
        traps = self.deploy.index("trap 'fail_safe $?' ERR EXIT")
        first_stop = self.deploy.index(
            'run_emergency --graceful-stop-and-arm', traps)
        self.assertLess(traps, block)
        self.assertLess(block, first_stop)
        self.assertIn("START_AUTH='/run/trading-deploy-authorize-start'",
                      self.emergency)
        self.assertIn('"ConditionPathExists=$START_AUTH"', self.emergency)
        self.assertNotIn("touch \"$START_AUTH\"", self.deploy)

    def test_fail_safe_transfers_external_source_lease_to_emergency(self):
        start = self.deploy.index('run_fail_safe_emergency() {')
        end = self.deploy.index('\n}', start) + 2
        function = self.deploy[start:end]
        fd = function.index('source_lock_fd=${SOURCE_LOCK_FD:-}')
        path = function.index('source_lock_path=$OLD_LOCK', fd)
        emergency = function.index(
            '/bin/bash --noprofile --norc "$EMERGENCY" --stop-and-arm', path)
        self.assertLess(fd, path)
        self.assertLess(path, emergency)
        self.assertNotIn('exec {SOURCE_LOCK_FD}>&-', function)
        self.assertIn(
            'TRADING_INHERITED_SOURCE_LOCK_FD="$source_lock_fd"', function)
        self.assertIn(
            'TRADING_INHERITED_SOURCE_LOCK_PATH="$source_lock_path"', function)
        fail_safe = self.deploy[
            self.deploy.index('fail_safe() {'):
            self.deploy.index('\n}', self.deploy.index('fail_safe() {')) + 2]
        self.assertIn('run_fail_safe_emergency || true', fail_safe)
        self.assertIn('test ! -e /proc/$$/fd/8', self.deploy)
        self.assertIn('test ! -L /proc/$$/fd/8', self.deploy)

        validate = self.emergency.index('validate_inherited_source_lock() {')
        local_validate = self.emergency.index(
            'validate_local_source_lock() {', validate)
        local_hold = self.emergency.index(
            'hold_external_pre_g0_source_lock() {', local_validate)
        acquire_local = self.emergency.index(
            'exec 8<>"$OLD_RUNNER_LOCK_FILE"', local_hold)
        local_flock = self.emergency.index(
            'flock --exclusive --nonblock "$LOCAL_SOURCE_LOCK_FD"',
            acquire_local)
        capture = self.emergency.index(
            'capture_pre_g0_source_contract || fail=1', local_hold)
        invoke_hold = self.emergency.index(
            '! hold_external_pre_g0_source_lock', local_hold)
        self.assertLess(local_validate, local_hold)
        self.assertLess(acquire_local, local_flock)
        self.assertLess(invoke_hold, capture)
        final_validate = self.emergency.index(
            'validate_inherited_source_lock || fail=1', capture)
        final_local_validate = self.emergency.index(
            'validate_local_source_lock || fail=1', final_validate)
        self.assertLess(validate, capture)
        self.assertLess(capture, final_validate)
        self.assertLess(final_validate, final_local_validate)
        final_probe = self.emergency.index(
            'sudo -u ubuntu flock --nonblock "$OLD_RUNNER_LOCK_FILE"',
            final_local_validate)
        self.assertLess(final_local_validate, final_probe)
        generic = self.emergency.index('validate_source_lock_fd() {')
        generic_body = self.emergency[generic:validate]
        self.assertIn('stat -Lc \'%d:%i\' "$fd_path"', generic_body)
        self.assertIn('flock --exclusive --nonblock "$fd"', generic_body)
        capture_function = self.emergency[
            self.emergency.index('capture_pre_g0_source_contract() {'):
            self.emergency.index('gate_verify_committed_stopped()')]
        self.assertIn(
            '"$INHERITED_SOURCE_LOCK_ACTIVE" -ne 1', capture_function)
        self.assertIn(
            '"$LOCAL_SOURCE_LOCK_ACTIVE" -ne 1', capture_function)
        self.assertIn('"$CURRENT_DATA" != "$DATA_DIR"', capture_function)
        self.assertIn(
            '! test -e "$attempt_dir/source-runtime.json"', capture_function)

    def test_old_runner_boundary_is_proven_before_any_planned_stop(self):
        old_identity = self.deploy.index(
            "OLD_MAIN_PID=$(systemctl show trading.service -p MainPID")
        old_lock = self.deploy.index(
            "flock --nonblock \"$OLD_LOCK\" true", old_identity)
        probe = self.deploy.index(
            'old_gate_client probe-handshake',
            old_lock)
        intent = self.deploy.index(
            'publish_attempt_artifact old-no-open-arm-intent.json', probe)
        establish = self.deploy.index(
            'old_gate_client establish-handshake', intent)
        final_slot = self.deploy.index(
            'FINAL_LIVE_SLOT=$(completed_slot "$SOURCE_DATA")', establish)
        publish = self.deploy.index(
            'publish_attempt_artifact old-no-open-boundary.json', final_slot)
        revalidate = self.deploy.index(
            'old_gate_client verify-handshake', publish)
        traps = self.deploy.index("trap 'fail_safe $?' ERR EXIT", revalidate)
        block = self.deploy.index(
            'run_emergency --install-block-only', traps)
        first_stop = self.deploy.index(
            'run_emergency --graceful-stop-and-arm', block)
        source_lock = self.deploy.index(
            'hold_external_source_lock unbound', first_stop)
        stopped_slot = self.deploy.index(
            'test "$(completed_slot "$SOURCE_DATA")" = "$SLOT"',
            source_lock)
        source_contract = self.deploy.index(
            'bind_source_contract', stopped_slot)
        g0 = self.deploy.index("advance_phase PREPARED G0", source_contract)
        self.assertLess(old_identity, old_lock)
        self.assertLess(old_lock, probe)
        self.assertLess(probe, intent)
        self.assertLess(intent, establish)
        self.assertLess(establish, final_slot)
        self.assertLess(final_slot, publish)
        self.assertLess(publish, revalidate)
        self.assertLess(revalidate, traps)
        self.assertLess(traps, block)
        self.assertLess(revalidate, first_stop)
        self.assertLess(first_stop, source_lock)
        self.assertLess(source_lock, stopped_slot)
        self.assertLess(stopped_slot, source_contract)
        self.assertLess(source_contract, g0)
        self.assertIn("verify-handshake", self.emergency)
        self.assertNotIn("credential_mode", self.emergency)
        install = self.emergency.index("START_BLOCK_PROVEN=0")
        boundary = self.emergency.index(
            '! verify_planned_no_open_boundary', install)
        timer_stop = self.emergency.index(
            "sudo systemctl stop trading-state-backup.timer", boundary)
        self.assertLess(install, boundary)
        self.assertLess(boundary, timer_stop)

    def test_deployment_lock_and_same_lock_handshake_are_ordered(self):
        for source in (self.deploy, self.recover):
            self.assertIn(
                "GLOBAL_DEPLOY_LOCK_DIR='/run/trading-deploy-control'", source)
            self.assertIn(
                'GLOBAL_DEPLOY_LOCK="$GLOBAL_DEPLOY_LOCK_DIR/operation.lock"',
                source)
            acquire = source.index("flock --exclusive --nonblock 9")
            first_attempt = source.index("active-attempt")
            self.assertLess(acquire, first_attempt)
            self.assertIn("TRADING_DEPLOY_LOCK_HELD=1", source)
        probe = self.deploy.index('old_gate_client probe-handshake')
        intent = self.deploy.index(
            'publish_attempt_artifact old-no-open-arm-intent.json', probe)
        establish = self.deploy.index(
            'old_gate_client establish-handshake', intent)
        persist = self.deploy.index(
            'publish_attempt_artifact old-no-open-boundary.json', establish)
        revalidate = self.deploy.index(
            'old_gate_client verify-handshake', persist)
        traps = self.deploy.index("trap 'fail_safe $?' ERR EXIT", revalidate)
        block = self.deploy.index(
            'run_emergency --install-block-only', traps)
        self.assertLess(probe, intent)
        self.assertLess(intent, establish)
        self.assertLess(establish, persist)
        self.assertLess(persist, revalidate)
        self.assertLess(revalidate, traps)
        self.assertLess(traps, block)
        recovery_intent = self.recover.index(
            'elif sudo test -e "$ARM_INTENT"')
        recovery_verify = self.recover.index(
            'old_gate_observer verify-arm-intent', recovery_intent)
        recovery_reconcile = self.recover.index(
            'run_emergency --reconcile-arm-intent', recovery_verify)
        recovery_stop = self.recover.index(
            'run_emergency --stop-and-arm', recovery_reconcile)
        recovery_contain = self.recover.index(
            'normalize_blocked_inactive', recovery_stop)
        recovery_source = self.recover.index(
            'ensure_source_contract', recovery_contain)
        recovery_g0 = self.recover.index(
            'advance_recovered_g0', recovery_source)
        self.assertLess(recovery_verify, recovery_reconcile)
        self.assertLess(recovery_reconcile, recovery_stop)
        self.assertLess(recovery_stop, recovery_contain)
        self.assertLess(recovery_contain, recovery_source)
        self.assertLess(recovery_source, recovery_g0)
        reconcile_function = self.emergency[
            self.emergency.index('reconcile_arm_intent_boundary() {'):
            self.emergency.index('\n}', self.emergency.index(
                'reconcile_arm_intent_boundary() {')) + 2]
        reconcile_sentinel = reconcile_function.index(
            'verify-maintenance-continuity')
        self.assertIn("arm_inactive_old_gate || return 1", reconcile_function)
        self.assertIn('--arm-intent "$intent"', reconcile_function)
        self.assertIn(
            "arm-recovery-gate --data-dir \"$CURRENT_DATA\"",
            self.emergency)
        reconcile_establish = reconcile_function.index(
            'old_gate_client establish-handshake', reconcile_sentinel)
        reconcile_publish = reconcile_function.index(
            '--name old-no-open-boundary.json', reconcile_establish)
        self.assertLess(reconcile_sentinel, reconcile_establish)
        self.assertLess(reconcile_establish, reconcile_publish)
        for forbidden in (
                'install_start_block', 'systemctl stop',
                'freeze_kill_stop_unit'):
            self.assertNotIn(forbidden, reconcile_function)
        helper_before = self.old_gate.index(
            'before = capture_process_binding(',
            self.old_gate.index('def probe_handshake'))
        helper_after = self.old_gate.index(
            'after = capture_process_binding(', helper_before)
        self.assertLess(helper_before, helper_after)
        emergency_verify = self.emergency.index(
            "old_gate_client verify-handshake")
        service_stop = self.emergency.index(
            'graceful_stop_unit "$unit"', emergency_verify)
        self.assertLess(emergency_verify, service_stop)

    def test_deployment_never_requires_api_permission_changes(self):
        combined = self.deploy + self.emergency + self.old_gate
        for removed in (
                'credential_mode read_only', 'credential_mode trade',
                'credential_read_only', 'credential-restore.approval.json',
                'credential-trade-restored.json'):
            self.assertNotIn(removed, combined)
        self.assertIn('old_gate_client establish-handshake', self.deploy)
        self.assertIn('runtime_sentinel', self.emergency)

    def test_only_canonical_seal_commit_state_machine_remains(self):
        combined = self.deploy + self.emergency + self.gate
        for removed in ("CANDIDATE", "trading-candidate", "gate disarm",
                        "attest-completion", "def disarm(",
                        "def attest_completion("):
            self.assertNotIn(removed, combined)
        self.assertEqual(self.deploy.count(
            'gate verify --config "$CONFIG"'), 1)
        self.assertEqual(self.deploy.count(
            'gate seal --config "$CONFIG"'), 1)
        self.assertEqual(self.deploy.count(
            'gate commit --config "$CONFIG"'), 1)

    def test_freeze_kill_drains_cgroup_before_stop(self):
        freeze = self.emergency.index('sudo systemctl freeze "$unit"')
        frozen = self.emergency.index("FreezerState", freeze)
        kill = self.emergency.index(
            "systemctl kill --kill-whom=all --signal=SIGKILL", frozen)
        post_kill_probe = self.emergency.index(
            'populated=$(cgroup_populated "$cgroup")', kill)
        recursive_kill = self.emergency.index(
            'kill-bound-cgroup', post_kill_probe)
        drain = self.emergency.index('while :; do', recursive_kill)
        populated = self.emergency.index(
            'populated=$(cgroup_populated "$cgroup")', drain)
        stop = self.emergency.index('sudo systemctl stop "$unit"', drain)
        self.assertLess(freeze, frozen)
        self.assertLess(frozen, kill)
        self.assertLess(kill, recursive_kill)
        self.assertLess(recursive_kill, drain)
        self.assertLess(drain, populated)
        self.assertLess(drain, stop)
        self.assertIn("kill-bound-cgroup", self.emergency)
        self.assertNotIn("awk '{print $22}'", self.emergency)
        self.assertNotIn("--starttime", self.emergency)
        self.assertIn('"$needs_kill" -eq 1', self.emergency)

    def test_cgroup_empty_proof_is_recursive_and_missing_events_fail_closed(self):
        start = self.emergency.index('cgroup_populated() {')
        end = self.emergency.index('\n}', start) + 2
        function = self.emergency[start:end].replace(
            'path="/sys/fs/cgroup${cgroup}"',
            'path="$CGROUP_ROOT${cgroup}"')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            service = root / 'system.slice' / 'trading.service'
            service.mkdir(parents=True)
            (service / 'cgroup.procs').write_text('', encoding='ascii')
            events = service / 'cgroup.events'
            events.write_text('populated 1\nfrozen 0\n', encoding='ascii')
            script = f'''set -Eeuo pipefail
CGROUP_ROOT={shlex.quote(str(root))}
{function}
test "$(cgroup_populated /system.slice/trading.service)" = 1
rm -f -- {shlex.quote(str(events))}
if cgroup_populated /system.slice/trading.service >/dev/null; then
  exit 91
fi
'''
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-c', script],
                check=False, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        for source in (self.deploy, self.emergency, self.recover):
            self.assertIn('cgroup.events', source)
            self.assertNotIn(
                '-s "/sys/fs/cgroup${cgroup}/cgroup.procs"', source)

    def test_external_source_lock_validator_propagates_each_failed_proof(self):
        """A validator used below ``|| die`` must not inherit Bash's -e hole."""
        start = self.deploy.index('validate_external_source_lock() {')
        end = self.deploy.index('\n}\n', start) + 2
        function = self.deploy[start:end]
        script = f'''set -Eeuo pipefail
SOURCE_DATA=/source
RUNTIME_ROOT=/runtime
SOURCE_LOCK_FD=8
OLD_LOCK=/missing-runner.lock
validate_bound_source_contract() {{ return 0; }}
flock() {{ return 0; }}
realpath() {{ printf '%s\\n' "$OLD_LOCK"; }}
stat() {{
  case "$*" in
    *%U:%G:%a:%h*) printf 'ubuntu:ubuntu:600:1\\n' ;;
    *) printf '1:2\\n' ;;
  esac
}}
{function}
# /proc/$$/fd/8 is deliberately absent.  The old implementation continued
# through later successful commands and returned the status of its final false
# ``if`` condition as success when called from a conditional context.
if validate_external_source_lock unbound; then
  exit 91
fi
'''
        result = subprocess.run(
            ['bash', '--noprofile', '--norc', '-c', script],
            check=False, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_kernel_cgroup_fallback_kills_the_bound_service_subtree(self):
        module = self.attempt_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'cgroup'
            service = root / 'system.slice' / 'trading.service'
            service.mkdir(parents=True)
            kill_file = service / 'cgroup.kill'
            kill_file.write_bytes(b'')
            with mock.patch.object(module, 'ROOT_UID', os.geteuid()), \
                    mock.patch.object(module.sys, 'platform', 'linux'):
                result = module.kill_bound_cgroup(
                    'trading.service', '/system.slice/trading.service',
                    str(root))
            self.assertEqual(b'1', kill_file.read_bytes())
            self.assertEqual({
                'unit': 'trading.service',
                'cgroup': '/system.slice/trading.service',
                'killed': True,
            }, result)
            with mock.patch.object(module, 'ROOT_UID', os.geteuid()), \
                    mock.patch.object(module.sys, 'platform', 'linux'), \
                    self.assertRaises(module.JournalError):
                module.kill_bound_cgroup(
                    'trading.service', '/system.slice/cloudflared.service',
                    str(root))

    def test_completed_slot_and_two_quiescence_windows_precede_t0(self):
        schedule = self.deploy.index('SLOT=$(completed_slot "$SOURCE_DATA")')
        cleanup = self.deploy.index(
            '--config "$SOURCE_DATA/config.json"', schedule)
        migration = self.deploy.index(
            '"$RELEASE_TRADING/migrate_single_strategy.py"', cleanup)
        final_slot = self.deploy.index(
            'FINAL_LIVE_SLOT=$(completed_slot "$SOURCE_DATA")',
            migration)
        boundary = self.deploy.index(
            'publish_attempt_artifact old-no-open-boundary.json', final_slot)
        stop = self.deploy.index(
            'run_emergency --graceful-stop-and-arm', boundary)
        stopped_slot = self.deploy.index(
            'test "$(completed_slot "$SOURCE_DATA")" = "$SLOT"', stop)
        source_contract = self.deploy.index('bind_source_contract', stopped_slot)
        g0 = self.deploy.index("advance_phase PREPARED G0", source_contract)
        probes = self.deploy.index("for probe in 1 2")
        q_order = self.deploy.index(
            'quiescence-1.json")" -le', probes)
        runtime_ready = self.deploy.index(
            "advance_phase G0 RUNTIME_READY", stop)
        phase = self.deploy.index(
            "advance_phase RUNTIME_READY QUIESCED", q_order)
        baseline = self.deploy.index("gate baseline", phase)
        self.assertLess(schedule, cleanup)
        self.assertLess(cleanup, migration)
        self.assertLess(migration, final_slot)
        self.assertLess(final_slot, boundary)
        self.assertLess(boundary, stop)
        self.assertLess(stop, stopped_slot)
        self.assertLess(stopped_slot, source_contract)
        self.assertLess(source_contract, g0)
        self.assertLess(stop, runtime_ready)
        self.assertLess(g0, runtime_ready)
        self.assertLess(runtime_ready, probes)
        self.assertLess(stop, probes)
        self.assertLess(probes, q_order)
        self.assertLess(q_order, phase)
        self.assertLess(phase, baseline)
        self.assertIn('--history-start-ms "$(sudo jq -r .t2_ms', self.deploy)
        self.assertNotIn('waiting safely stopped', self.deploy)

    def test_planned_stops_drain_and_hard_kill_only_fails_closed(self):
        self.assertEqual(
            2, self.deploy.count('run_emergency --graceful-stop-and-arm'))
        self.assertEqual(1, self.deploy.count(
            'run_fail_safe_emergency || true'))
        graceful = self.emergency.index("graceful_stop_unit()")
        result = self.emergency.index('[[ "$result" = success ]]', graceful)
        fallback = self.emergency.index(
            'freeze_kill_stop_unit "$unit" || true', result)
        failure = self.emergency.index('fail=1', fallback)
        self.assertLess(graceful, result)
        self.assertLess(result, fallback)
        self.assertLess(fallback, failure)
        self.assertIn("'q0_ms': probe['t0_ms']", self.gate)
        self.assertIn("'history_verified_through_ms'", self.gate)

    def test_formal_stack_is_validated_under_sentinel_then_sealed(self):
        baseline = self.deploy.index("gate baseline")
        first_start = self.deploy.index(
            "sudo systemctl start trading.service", baseline)
        backup = self.deploy.index(
            "sudo systemctl start --wait trading-state-backup.service",
            first_start)
        tunnel = self.deploy.index(
            "sudo systemctl start cloudflared.service", backup)
        final_stop = self.deploy.index(
            'run_emergency --graceful-stop-and-arm', tunnel)
        final_verify = self.deploy.index("gate verify", final_stop)
        seal = self.deploy.index("gate seal", final_verify)
        second_start = self.deploy.index(
            "sudo systemctl start trading.service", seal)
        self.assertLess(first_start, backup)
        self.assertLess(backup, tunnel)
        self.assertLess(tunnel, final_stop)
        self.assertLess(final_stop, final_verify)
        self.assertLess(final_verify, seal)
        self.assertLess(seal, second_start)
        self.assertGreaterEqual(
            self.deploy[first_start:final_stop].count(
                "check_maintenance_http_gate"), 3)

    def test_commit_is_unique_gate_mutation_and_backup_stays_quiescent(self):
        seal = self.deploy.index("gate seal")
        second_start = self.deploy.index(
            "sudo systemctl start trading.service", seal)
        timer = self.deploy.index('settle_backup_timer', second_start)
        quiesce = self.deploy.index('quiesce_backup_timer', timer)
        revoke_auth = self.deploy.index(
            'sudo rm -- "$START_AUTH"', quiesce)
        stop_tunnel = self.deploy.index(
            'sudo systemctl stop cloudflared.service', quiesce)
        drain = self.deploy.index(
            'drain-maintenance-boundary', stop_tunnel)
        freeze = self.deploy.index(
            "sudo systemctl freeze trading.service", drain)
        frozen = self.deploy.index(
            '-p FreezerState --value)" = frozen', freeze)
        binding = self.deploy.index(
            'old_gate_observer process-binding', frozen)
        ready = self.deploy.index("advance_phase SEALED COMMIT_READY", binding)
        boundary_trap = self.deploy.index(
            "trap 'commit_boundary_fail $?' ERR EXIT", ready)
        commit = self.deploy.index("gate commit", boundary_trap)
        self.assertLess(seal, second_start)
        self.assertLess(second_start, timer)
        self.assertLess(timer, quiesce)
        self.assertLess(quiesce, revoke_auth)
        self.assertLess(revoke_auth, stop_tunnel)
        self.assertLess(stop_tunnel, drain)
        self.assertLess(drain, freeze)
        self.assertLess(freeze, frozen)
        self.assertLess(frozen, binding)
        self.assertLess(binding, ready)
        self.assertLess(ready, boundary_trap)
        self.assertLess(boundary_trap, commit)
        expected_pid = self.deploy.index(
            '--expected-runner-pid "$EXPECTED_RUNNER_PID"', commit)
        prove_frozen = self.deploy.index('prove_formal_runner', expected_pid)
        timer_inactive = self.deploy.index(
            'prove_unit_inactive trading-state-backup.timer', prove_frozen)
        service_inactive = self.deploy.index(
            'prove_unit_inactive trading-state-backup.service', timer_inactive)
        final_slot = self.deploy.index(
            'completed_slot "$DATA_DIR"', service_inactive)
        final_inactive = self.deploy.index(
            'prove_unit_inactive trading-state-backup.timer', final_slot)
        final_thaw = self.deploy.index(
            'sudo systemctl thaw trading.service', final_inactive)
        committed_verify = self.deploy.index(
            'gate verify-committed-running', final_thaw)
        unblock = self.deploy.index(
            'sudo rm -- "$START_BLOCK"', committed_verify)
        reload_ = self.deploy.index(
            'sudo systemctl daemon-reload', unblock)
        block_absent = self.deploy.index(
            'verify_start_block_absent', reload_)
        timer_resume = self.deploy.index('settle_backup_timer', block_absent)
        final_health = self.deploy.index(
            'check_local_health >/dev/null', timer_resume)
        final_cloud = self.deploy.index(
            'sudo systemctl start cloudflared.service', final_health)
        final_cloud_proof = self.deploy.index(
            'prove_running_aux_unit cloudflared.service', final_cloud)
        traps_off = self.deploy.index(
            'trap - ERR EXIT HUP INT TERM', final_cloud_proof)
        self.assertLess(commit, expected_pid)
        self.assertLess(expected_pid, prove_frozen)
        self.assertLess(prove_frozen, timer_inactive)
        self.assertLess(timer_inactive, service_inactive)
        self.assertLess(service_inactive, final_slot)
        self.assertLess(final_slot, final_inactive)
        self.assertLess(final_inactive, final_thaw)
        self.assertLess(final_thaw, committed_verify)
        self.assertLess(committed_verify, unblock)
        self.assertLess(unblock, reload_)
        self.assertLess(reload_, block_absent)
        self.assertLess(block_absent, timer_resume)
        self.assertLess(final_thaw, timer_resume)
        self.assertLess(timer_resume, final_health)
        self.assertLess(final_health, final_cloud)
        self.assertLess(final_cloud, final_cloud_proof)
        self.assertLess(final_cloud_proof, traps_off)
        frozen_tail = self.deploy[quiesce:final_thaw]
        self.assertNotIn('backup_timer_stable_sample', frozen_tail)
        self.assertNotIn('settle_backup_timer', frozen_tail)
        self.assertGreaterEqual(
            self.deploy[commit:final_thaw].count(
                'prove_unit_inactive trading-state-backup.timer'), 2)
        self.assertGreaterEqual(
            self.deploy[commit:final_thaw].count(
                'prove_unit_inactive trading-state-backup.service'), 2)
        tail = self.deploy[commit + len("gate commit"):]
        for forbidden in ("advance_phase", "gate seal", "gate commit"):
            self.assertNotIn(forbidden, tail)
        self.assertEqual(
            1, tail.count('sudo rm -- "$START_BLOCK"'))
        self.assertIn('prove_formal_runner', self.deploy[commit:])
        self.assertNotIn(
            'check_local_health', self.deploy[commit:final_thaw])
        self.assertNotIn(
            'sudo systemctl start cloudflared.service',
            self.deploy[stop_tunnel:final_health])
        self.assertIn('run_emergency --stop-only', self.deploy)
        self.assertIn('gate verify-committed-stopped', self.deploy)
        self.assertIn("_require_runner_lock_held_elsewhere(lock_fd)", self.gate)
        self.assertIn("LOCK_EX | fcntl.LOCK_NB", self.gate)

    def test_reboot_cutpoint_keeps_start_block_through_commit_and_thaw(self):
        """断电/代际替换不能在 sentinel 删除后绕过持久启动门禁。"""
        seal = self.deploy.index("gate seal")
        second_start = self.deploy.index(
            "sudo systemctl start trading.service", seal)
        revoke_auth = self.deploy.index(
            'sudo rm -- "$START_AUTH"', second_start)
        commit = self.deploy.index("gate commit", revoke_auth)
        thaw = self.deploy.index(
            'sudo systemctl thaw trading.service', commit)
        unblock = self.deploy.index(
            'sudo rm -- "$START_BLOCK"', thaw)
        reload_ = self.deploy.index(
            'sudo systemctl daemon-reload', unblock)
        timer_resume = self.deploy.index('settle_backup_timer', reload_)

        self.assertLess(second_start, revoke_auth)
        self.assertLess(revoke_auth, commit)
        self.assertLess(commit, thaw)
        self.assertLess(thaw, unblock)
        self.assertLess(unblock, reload_)
        self.assertLess(reload_, timer_resume)
        self.assertNotIn(
            'rm -- "$START_BLOCK"', self.deploy[revoke_auth:thaw])
        self.assertIn('verify_block', self.deploy[revoke_auth:commit])
        self.assertIn(
            'verify_loaded_start_block', self.deploy[revoke_auth:commit])

    def test_backup_timer_sample_rejects_mid_sample_invocation_change(self):
        function_start = self.deploy.index('backup_timer_stable_sample() {')
        function_end = self.deploy.index('\n}\n', function_start) + 2
        function = self.deploy[function_start:function_end]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            counter = root / 'invocation-count'
            systemctl = root / 'systemctl'
            systemctl.write_text('''#!/bin/sh
set -eu
if [ "$1" = list-jobs ]; then
    exit 0
fi
unit=$2
property=$4
case "$unit:$property" in
  trading-state-backup.timer:ActiveState) printf 'active\\n' ;;
  trading-state-backup.timer:SubState) printf 'waiting\\n' ;;
  trading-state-backup.timer:NextElapseUSecRealtime)
    printf '2030-01-01 00:00:00 UTC\\n'
    ;;
  trading-state-backup.service:ActiveState) printf 'inactive\\n' ;;
  trading-state-backup.service:Result) printf 'success\\n' ;;
  trading-state-backup.service:InvocationID)
    count=0
    if [ -f "$COUNTER" ]; then count=$(cat "$COUNTER"); fi
    count=$((count + 1))
    printf '%s\\n' "$count" >"$COUNTER"
    if [ "$MODE" = mutate ] && [ "$count" -gt 1 ]; then
      printf 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\\n'
    else
      printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n'
    fi
    ;;
  *) exit 41 ;;
esac
''', encoding='utf-8')
            systemctl.chmod(0o755)
            date = root / 'date'
            date.write_text('''#!/bin/sh
set -eu
case "$1" in
  --date=*) printf '2000\\n' ;;
  +%s) printf '1000\\n' ;;
  *) exit 42 ;;
esac
''', encoding='utf-8')
            date.chmod(0o755)
            script = f'''set -Eeuo pipefail
{function}
backup_timer_stable_sample
'''
            base_env = dict(os.environ)
            base_env.update({
                'PATH': f'{root}:{base_env["PATH"]}',
                'COUNTER': str(counter),
            })
            mutated = subprocess.run(
                ['bash', '--noprofile', '--norc', '-c', script],
                env={**base_env, 'MODE': 'mutate'},
                check=False, capture_output=True, text=True,
            )
            counter.unlink()
            stable = subprocess.run(
                ['bash', '--noprofile', '--norc', '-c', script],
                env={**base_env, 'MODE': 'stable'},
                check=False, capture_output=True, text=True,
            )
        self.assertNotEqual(0, mutated.returncode)
        self.assertEqual('', mutated.stdout)
        self.assertEqual(0, stable.returncode, stable.stderr)
        self.assertEqual(
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:2000\n', stable.stdout)

    def test_backup_quiesce_propagates_every_failed_proof_in_conditionals(self):
        function_start = self.deploy.index('quiesce_backup_timer() {')
        function_end = self.deploy.index('\n}', function_start) + 2
        function = self.deploy[function_start:function_end]
        script = f'''set -Eeuo pipefail
FAIL=${{FAIL:-none}}
sudo() {{ "$@"; }}
systemctl() {{ [[ "$FAIL" != stop ]]; }}
prove_unit_inactive() {{ [[ "$FAIL" != "$1" ]]; }}
{function}
if quiesce_backup_timer; then
  exit 0
fi
exit 23
'''
        for mode, expected in (
                ('none', 0),
                ('stop', 23),
                ('trading-state-backup.timer', 23),
                ('trading-state-backup.service', 23)):
            with self.subTest(mode=mode):
                result = subprocess.run(
                    ['bash', '--noprofile', '--norc', '-c', script],
                    env={**os.environ, 'FAIL': mode},
                    check=False, capture_output=True, text=True)
                self.assertEqual(expected, result.returncode, result.stderr)

    def test_committed_recovery_quiesces_backup_until_after_thaw(self):
        function_start = self.recover.index('recover_committed_stack() {')
        function_end = self.recover.index('\n}\n', function_start) + 2
        function = self.recover[function_start:function_end]
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / 'events'
            script = f'''set -Eeuo pipefail
EVENTS={shlex.quote(str(events))}
FREEZER_STATE=frozen
MEM_STATE=inactive
sudo() {{ "$@"; }}
prove_committed_runner() {{
  printf 'prove:%s\n' "$FREEZER_STATE" >>"$EVENTS"
}}
prove_unit_inactive() {{ printf 'inactive:%s\n' "$1" >>"$EVENTS"; }}
prove_running_aux_unit() {{ printf 'aux:%s\n' "$1" >>"$EVENTS"; }}
check_committed_health() {{ printf 'health\n' >>"$EVENTS"; }}
settle_backup_timer() {{ printf 'settle-timer\n' >>"$EVENTS"; }}
backup_timer_stable_sample() {{ printf 'timer-sample\n' >>"$EVENTS"; }}
quiesce_backup_timer() {{ printf 'quiesce-timer\n' >>"$EVENTS"; }}
safe_resume_slot() {{ printf 'slot:%s\n' "$1" >>"$EVENTS"; }}
systemctl() {{
  local action=$1 unit=${{2:-}} property=${{4:-}}
  if [[ "$action" = show ]]; then
    if [[ "$unit" = trading.service && "$property" = FreezerState ]]; then
      printf '%s\n' "$FREEZER_STATE"
    elif [[ "$unit" = trading-mem-monitor.service &&
            "$property" = ActiveState ]]; then
      printf '%s\n' "$MEM_STATE"
    else
      return 41
    fi
    return 0
  fi
  printf '%s:%s\n' "$action" "$unit" >>"$EVENTS"
  if [[ "$action" = thaw && "$unit" = trading.service ]]; then
    FREEZER_STATE=running
  elif [[ "$action" = freeze && "$unit" = trading.service ]]; then
    FREEZER_STATE=frozen
  elif [[ "$action" = start &&
          "$unit" = trading-mem-monitor.service ]]; then
    MEM_STATE=active
  fi
}}
{function}
recover_committed_stack 2026-07-19
'''
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-c', script],
                check=False, capture_output=True, text=True)
            recorded = events.read_text(encoding='utf-8').splitlines()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([
            'quiesce-timer',
            'stop:cloudflared.service',
            'inactive:cloudflared.service',
            'prove:frozen',
            'start:trading-mem-monitor.service',
            'aux:trading-mem-monitor.service',
            'prove:frozen',
            'inactive:trading-state-backup.timer',
            'inactive:trading-state-backup.service',
            'slot:2026-07-19',
            'slot:2026-07-19',
            'inactive:trading-state-backup.timer',
            'inactive:trading-state-backup.service',
            'prove:frozen',
            'aux:trading-mem-monitor.service',
            'thaw:trading.service',
            'settle-timer',
            'prove:running',
            'health',
            'aux:trading-mem-monitor.service',
            'start:cloudflared.service',
            'aux:cloudflared.service',
        ], recorded)
        active_branch = self.recover[self.recover.index(
            'if [[ "$COMMITTED_STATE" = active ]]; then'):]
        self.assertIn(
            'if ! recover_committed_stack "$COMMITTED_SLOT"; then',
            active_branch,
        )
        self.assertIn(
            'committed formal stack could not be safely recovered',
            active_branch,
        )
        self.assertIn(
            'committed service is stopped without a persistent deployment block',
            active_branch,
        )

    def test_committed_recovery_closes_backup_restart_race_before_tunnel_stop(self):
        function_start = self.recover.index('recover_committed_stack() {')
        function_end = self.recover.index('\n}\n', function_start) + 2
        function = self.recover[function_start:function_end]
        script = f'''set -Eeuo pipefail
FREEZER_STATE=frozen
MEM_STATE=active
BACKUP_QUIESCED=0
RESTART_RACE=0
sudo() {{ "$@"; }}
quiesce_backup_timer() {{ BACKUP_QUIESCED=1; }}
prove_committed_runner() {{ [[ "$RESTART_RACE" -eq 0 ]]; }}
prove_unit_inactive() {{ :; }}
prove_running_aux_unit() {{ :; }}
check_committed_health() {{ :; }}
settle_backup_timer() {{ :; }}
safe_resume_slot() {{ :; }}
systemctl() {{
  local action=$1 unit=${{2:-}} property=${{4:-}}
  if [[ "$action" = show ]]; then
    if [[ "$unit" = trading.service && "$property" = FreezerState ]]; then
      printf '%s\n' "$FREEZER_STATE"
    elif [[ "$unit" = trading-mem-monitor.service &&
            "$property" = ActiveState ]]; then
      printf '%s\n' "$MEM_STATE"
    else
      return 41
    fi
    return 0
  fi
  if [[ "$action" = stop && "$unit" = cloudflared.service &&
        "$BACKUP_QUIESCED" -eq 0 ]]; then
    # Models a timer firing while tunnel stop is in progress: its reviewed
    # worker is allowed to restart the frozen formal service.
    RESTART_RACE=1
  elif [[ "$action" = thaw && "$unit" = trading.service ]]; then
    FREEZER_STATE=running
  fi
}}
{function}
recover_committed_stack 2026-07-19
'''
        result = subprocess.run(
            ['bash', '--noprofile', '--norc', '-c', script],
            check=False, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_formal_runner_proofs_reject_pending_restart_jobs(self):
        for source, function_name in (
                (self.deploy, 'prove_formal_runner() {'),
                (self.recover, 'prove_committed_runner() {')):
            with self.subTest(function=function_name):
                start = source.index(function_name)
                end = source.index('\n}', start) + 2
                function = source[start:end]
                self.assertIn(
                    'unit_job_absent trading.service || return 1', function)

    def test_phase_journal_and_recovery_are_single_ordered_protocol(self):
        for phase in ("PREPARED", "G0", "RUNTIME_READY", "QUIESCED", "T0",
                      "VALIDATED",
                      "SEALED", "COMMIT_READY"):
            self.assertIn(f'"{phase}"', self.attempt)
        mark = self.recover.index("journal abandon")
        archive = self.recover.index("gate abandon", mark)
        runtime_verify = self.recover.index("sync-runtime-tree", archive)
        seed = self.recover.index("seed-recovery", archive)
        arm = self.recover.index("gate arm", seed)
        switch = self.recover.index('switch_active_attempt "$NEXT"', arm)
        self.assertLess(mark, archive)
        self.assertLess(archive, runtime_verify)
        self.assertLess(runtime_verify, seed)
        self.assertLess(archive, seed)
        self.assertLess(seed, arm)
        self.assertLess(arm, switch)
        self.assertIn("COMMIT_READY", self.recover)
        self.assertIn("--stop-only", self.recover)
        self.assertIn("TRADING_EMERGENCY_INTERNAL=1", self.recover)

    def test_g0_recovery_holds_external_source_lease_through_switch(self):
        source = self.recover.index(
            'RECOVERY_SOURCE=$(journal source-contract)')
        acquire = self.recover.index(
            'hold_external_recovery_source_lock "$RECOVERY_SOURCE"', source)
        abandon = self.recover.index(
            'journal abandon --reason operator_requested_post_g0_reset',
            acquire,
        )
        classify = self.recover.index('--classify-state', abandon)
        seed = self.recover.index('seed-recovery', classify)
        switch = self.recover.index('switch_active_attempt "$NEXT"', seed)
        self.assertLess(source, acquire)
        self.assertLess(acquire, abandon)
        self.assertLess(abandon, classify)
        self.assertLess(classify, seed)
        self.assertLess(seed, switch)

        function_start = self.recover.index(
            'hold_external_recovery_source_lock() {')
        function_end = self.recover.index('\n}', function_start) + 2
        function = self.recover[function_start:function_end]
        open_lease = function.index(
            'exec {RECOVERY_SOURCE_LOCK_FD}<>"$source_lock"')
        flock = function.index(
            'flock --exclusive --nonblock "$RECOVERY_SOURCE_LOCK_FD"',
            open_lease,
        )
        validate_call = function.index(
            'validate_external_recovery_source_lock "$contract"', flock)
        self.assertLess(open_lease, flock)
        self.assertLess(flock, validate_call)

        validate_start = self.recover.index(
            'validate_external_recovery_source_lock() {')
        validate_end = self.recover.index('\n}', validate_start) + 2
        validate = self.recover[validate_start:validate_end]
        first_contract = validate.index('journal source-contract')
        fd_identity = validate.index(
            '"/proc/$$/fd/$RECOVERY_SOURCE_LOCK_FD"', first_contract)
        path_identity = validate.index(
            'stat -c \'%d:%i\' "$source_lock"', fd_identity)
        second_contract = validate.index(
            'journal source-contract', path_identity)
        self.assertLess(first_contract, fd_identity)
        self.assertLess(fd_identity, path_identity)
        self.assertLess(path_identity, second_contract)
        self.assertNotIn('exec {RECOVERY_SOURCE_LOCK_FD}>&-', self.recover)

    def test_missing_sentinel_with_uncommitted_evidence_is_contained(self):
        status = self.recover.index('if STATUS=$(journal status); then')
        journal_damage = self.recover.index(
            "contain_damaged_writer 'attempt journal is invalid", status)
        phase_read = self.recover.index('parse_status "$STATUS"', status)
        self.assertLess(status, journal_damage)
        self.assertLess(journal_damage, phase_read)
        for path in ("$SENTINEL", "$BASELINE", "$COMPLETION",
                     "$BOUNDARY_EVIDENCE", "$SOURCE_CONTRACT",
                     "$START_BLOCK", "$START_AUTH"):
            self.assertIn(f'sudo test -L "{path}"',
                          self.recover[status:journal_damage])

        damaged = self.recover.index(
            'elif sudo test -e "$BASELINE" || sudo test -L "$BASELINE"')
        refuse = self.recover.index(
            "contain_damaged_writer 'sentinel is missing", damaged)
        phase_null = self.recover.index("CURRENT_SEED=''", refuse)
        self.assertLess(damaged, refuse)
        self.assertLess(refuse, phase_null)

        helper_start = self.recover.index('contain_damaged_writer() {')
        helper_end = self.recover.index('\n}', helper_start) + 2
        helper = self.recover[helper_start:helper_end]
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / 'events'
            script = f'''set -Eeuo pipefail
EVENTS={shlex.quote(str(events))}
run_emergency() {{ printf 'stop\n' >>"$EVENTS"; return 41; }}
verify_blocked_inactive() {{ printf 'verify:%s\n' "$1" >>"$EVENTS"; return 0; }}
die() {{ printf 'die:%s\n' "$*" >>"$EVENTS"; exit 73; }}
{helper}
contain_damaged_writer damaged
'''
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-c', script],
                check=False, capture_output=True, text=True)
            recorded = events.read_text(encoding='utf-8').splitlines()
        self.assertEqual(73, result.returncode, result.stderr)
        self.assertEqual('stop', recorded[0])
        self.assertEqual('verify:any', recorded[1])
        self.assertTrue(recorded[2].startswith('die:damaged;'))

    def test_recovery_durability_retries_precede_authority_switch(self):
        pre = self.recover.index(
            "journal abandon --reason operator_requested_pre_g0_reset")
        pre_switch = self.recover.index('switch_active_attempt "$NEXT"', pre)
        self.assertLess(pre, pre_switch)

        post = self.recover.index(
            "journal abandon --reason operator_requested_post_g0_reset")
        archive = self.recover.index("gate abandon", post)
        runtime_verify = self.recover.index("sync-runtime-tree", archive)
        seed = self.recover.index("seed-recovery", runtime_verify)
        arm = self.recover.index("gate arm", seed)
        switch = self.recover.index('switch_active_attempt "$NEXT"', arm)
        self.assertLess(post, archive)
        self.assertLess(archive, runtime_verify)
        self.assertLess(runtime_verify, seed)
        self.assertLess(seed, arm)
        self.assertLess(arm, switch)

    def test_self_source_g0_transaction_is_rolled_back_before_reseed(self):
        source = self.recover.index(
            'if [[ "$PHASE" = G0 && "$RECOVERY_SOURCE_DATA" = "$RUNTIME" &&')
        rollback = self.recover.index('--recover-only', source)
        classify = self.recover.index('--classify-state', rollback)
        seed = self.recover.index('seed-recovery', classify)
        arm = self.recover.index('gate arm', seed)
        switch = self.recover.index('switch_active_attempt "$NEXT"', arm)
        self.assertLess(source, rollback)
        self.assertLess(rollback, classify)
        self.assertLess(classify, seed)
        self.assertLess(seed, arm)
        self.assertLess(arm, switch)

    def test_backup_is_durable_before_any_candidate_state_mutation(self):
        checksum = self.deploy.index(
            "sha256sum -c SHA256SUMS >/dev/null")
        durable = self.deploy.index(
            '--path "$BACKUP_ROOT/current-data.tar"', checksum)
        cleanup = self.deploy.index(
            'if [[ "$SOURCE_DATA_STATE" = requires_migration ]]', durable)
        migration_apply = self.deploy.index(
            '"$RELEASE_TRADING/migrate_single_strategy.py" --data-dir '
            '"$DATA_DIR" --apply', cleanup)
        self.assertLess(checksum, durable)
        self.assertLess(durable, cleanup)
        self.assertLess(cleanup, migration_apply)
        managed = self.deploy[
            self.deploy.index("safe_install_managed()"):
            self.deploy.index("verify_block()")]
        self.assertIn('--path "$backup" --path "$backup.sha256"', managed)
        self.assertIn('--path "$target" --path "$parent"', managed)
        same = managed.index('if sudo cmp -s -- "$source" "$target"; then')
        same_return = managed.index('return 0', same)
        self.assertIn(
            '--path "$target" --path "$parent"',
            managed[same:same_return])

    def test_migration_originals_leave_active_runtime_before_runtime_ready(self):
        apply = self.deploy.index(
            '"$RELEASE_TRADING/migrate_single_strategy.py" --data-dir '
            '"$DATA_DIR" --apply')
        archive = self.deploy.index('archive_migration_artifacts', apply)
        runtime_ready = self.deploy.index(
            'advance_phase G0 RUNTIME_READY', archive)
        self.assertLess(apply, archive)
        self.assertLess(archive, runtime_ready)
        function_start = self.deploy.index('archive_migration_artifacts() {')
        function_end = self.deploy.index('\n}\n', function_start)
        function = self.deploy[function_start:function_end]
        self.assertIn('.single_strategy_migration_journal.json', function)
        self.assertIn('$BACKUP_ROOT/migration-originals', function)
        self.assertIn("-name '*.premigrate.*'", function)
        self.assertIn('test "$after" = "$before"', function)
        self.assertIn('--path "$DATA_DIR"', function)
        self.assertIn('$DATA_DIR/data/okx', function)
        self.assertIn('.okx_legacy_migration_complete.json', function)
        self.assertIn("--include='/data/'", self.deploy)
        self.assertIn(
            "--include='/.okx_legacy_migration_complete.json*'", self.deploy)

    def test_abandoned_active_attempt_remains_emergency_containable(self):
        gate_arm = self.emergency[
            self.emergency.index("gate_arm()"):
            self.emergency.index("active_attempt_status()")]
        self.assertNotIn("active_attempt_abandoned", gate_arm)
        capture = self.emergency[
            self.emergency.index("capture_pre_g0_source_contract()"):
            self.emergency.index("gate_verify_committed_stopped()")]
        self.assertIn('if [[ "$abandoned" = true ]]', capture)
        abandoned_retry = self.recover.index(
            'if [[ "$ABANDONED" = true && "$G0_FACTS" = null ]]')
        abandoned_switch = self.recover.index(
            'switch_active_attempt "$NEXT"', abandoned_retry)
        g0_resolution = self.recover.index(
            'if [[ "$G0_FACTS" != null ]]', abandoned_switch)
        self.assertLess(abandoned_retry, abandoned_switch)
        self.assertLess(abandoned_switch, g0_resolution)
        self.assertNotIn(
            "publish_recovery_boundary",
            self.recover[abandoned_retry:g0_resolution],
        )

        gate_dispatch = self.emergency[
            self.emergency.index('if [[ "$SHOULD_ARM" -eq 1 ]]'):
            self.emergency.index('for unit in \\', self.emergency.index(
                'if [[ "$SHOULD_ARM" -eq 1 ]]'))
        ]
        seed_boundary = gate_dispatch.index(
            'successor_recovery_seed "$gate_attempt" "$gate_phase"')
        rearm = gate_dispatch.index("gate_arm", seed_boundary)
        finish = gate_dispatch.index(
            'gate_finish_abandon "$gate_attempt"', rearm)
        self.assertLess(seed_boundary, rearm)
        self.assertLess(rearm, finish)
        self.assertIn(
            '"$gate_abandoned" = true',
            gate_dispatch[:finish],
        )
        self.assertIn('ABANDONED_CYCLE=1', gate_dispatch)

    def test_public_emergency_request_marker_is_immediately_fail_closed(self):
        create_start = self.emergency.index('create_emergency_request() {')
        create_end = self.emergency.index('\n}', create_start) + 2
        create = self.emergency[create_start:create_end]
        publish = create.index(
            '( set -o noclobber; : >"$EMERGENCY_REQUEST" )')
        lease = create.index(
            'flock --exclusive --nonblock "$EMERGENCY_REQUEST_FD"', publish)
        identity = create.index(
            'stat -Lc \'%d:%i\' "/proc/$$/fd/$EMERGENCY_REQUEST_FD"',
            lease,
        )
        durable = create.index(
            'sync -f "$GLOBAL_DEPLOY_LOCK_DIR"', identity)
        self.assertLess(publish, lease)
        self.assertLess(lease, identity)
        self.assertLess(identity, durable)
        self.assertNotIn('.tmp', create)
        self.assertNotIn('mv ', create)

        cleanup_start = self.emergency.index(
            'cleanup_emergency_requests() {')
        cleanup_end = self.emergency.index('\n}', cleanup_start) + 2
        cleanup = self.emergency[cleanup_start:cleanup_end]
        glob = cleanup.index(
            '"$GLOBAL_DEPLOY_LOCK_DIR"/emergency.request.*')
        reject_symlink = cleanup.index('[[ ! -L "$request" ]] || return 1')
        existence = cleanup.index('test -e "$request" || continue')
        validate = cleanup.index(
            '[[ "$name" =~ ^emergency\\.request\\.[0-9a-f]{32}$ ]]',
            glob,
        )
        skip_own = cleanup.index(
            '[[ "$request" != "$EMERGENCY_REQUEST" ]] || continue',
            validate,
        )
        stale_lease = cleanup.index(
            'if flock --exclusive --nonblock "$cleanup_fd"; then',
            skip_own,
        )
        stale_remove = cleanup.index(
            'rm -f -- "$request"', stale_lease)
        own_sync = cleanup.index(
            'sync -f "$GLOBAL_DEPLOY_LOCK_DIR"', stale_remove)
        own_remove = cleanup.index(
            'rm -f -- "$EMERGENCY_REQUEST"', own_sync)
        self.assertLess(glob, reject_symlink)
        self.assertLess(reject_symlink, existence)
        self.assertLess(existence, validate)
        self.assertLess(validate, skip_own)
        self.assertLess(skip_own, stale_lease)
        self.assertLess(stale_lease, stale_remove)
        self.assertLess(stale_remove, own_sync)
        self.assertLess(own_sync, own_remove)

        with tempfile.TemporaryDirectory() as directory:
            token = 'a' * 32
            dangling = Path(directory) / f'emergency.request.{token}'
            dangling.symlink_to(Path(directory) / 'missing-target')
            script = f'''set -uo pipefail
GLOBAL_DEPLOY_LOCK_DIR={shlex.quote(directory)}
EMERGENCY_REQUEST={shlex.quote(str(Path(directory) / 'own'))}
{cleanup}
if cleanup_emergency_requests; then exit 0; else exit $?; fi
'''
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-c', script],
                check=False, capture_output=True, text=True)
            self.assertEqual(1, result.returncode, result.stderr)
            self.assertTrue(dangling.is_symlink())

    def test_public_emergency_owns_one_fixed_containment_cgroup(self):
        scope = self.emergency.index(
            "GLOBAL_EMERGENCY_UNIT='trading-deployment-emergency.service'")
        reexec = self.emergency.index(
            '--unit="$GLOBAL_EMERGENCY_UNIT" --service-type=exec', scope)
        kill_mode = self.emergency.index(
            '--property=KillMode=control-group', reexec)
        request = self.emergency.index('create_emergency_request()', kill_mode)
        self.assertLess(scope, reexec)
        self.assertLess(reexec, kill_mode)
        self.assertLess(kill_mode, request)
        self.assertIn(
            'systemctl show "$GLOBAL_EMERGENCY_UNIT" -p MainPID',
            self.emergency[reexec:request])
        self.assertNotIn("EXISTING_NEXT_SEED", self.recover)

        contain_start = self.emergency.index('contain_operation_units() {')
        contain_end = self.emergency.index('\n}', contain_start) + 2
        contain = self.emergency[contain_start:contain_end]
        self.assertIn('-p After --value', contain)
        self.assertNotIn(
            '"$lock_busy" -eq 1 &&\n'
            '        ( "$main_present" -eq 1 || "$helper_present" -eq 1 )',
            contain)
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / 'events'
            script = f'''set -Eeuo pipefail
GLOBAL_DEPLOY_UNIT=trading-deployment-operation.service
GLOBAL_DEPLOY_HELPER=trading-deployment-operation-helper.service
GLOBAL_DEPLOY_LOCK=/tmp/unused-operation.lock
EVENTS={shlex.quote(str(events))}
MAIN_STOPPED=0
cgroup_populated() {{ printf '0\n'; }}
systemctl() {{
  local action=$1 unit=${{2:-}} property=${{4:-}}
  if [[ "$action" = show ]]; then
    if [[ "$unit" = "$GLOBAL_DEPLOY_HELPER" && "$property" = LoadState ]]; then
      printf 'not-found\n'; return 0
    fi
    case "$property" in
      LoadState) printf 'loaded\n' ;;
      Transient) printf 'yes\n' ;;
      KillMode) printf 'control-group\n' ;;
      ActiveState)
        if [[ "$MAIN_STOPPED" -eq 1 ]]; then printf 'inactive\n';
        else printf 'active\n'; fi ;;
      MainPID) printf '4321\n' ;;
      ControlGroup) printf '/system.slice/deploy.service\n' ;;
      *) return 1 ;;
    esac
    return 0
  fi
  printf '%s:%s\n' "$action" "${{!#}}" >>"$EVENTS"
  if [[ "$action" = stop && "${{!#}}" = "$GLOBAL_DEPLOY_UNIT" ]]; then
    MAIN_STOPPED=1
  fi
}}
sleep() {{ SECONDS=$((SECONDS + 61)); }}
{contain}
contain_operation_units 0
'''
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-c', script],
                check=False, capture_output=True, text=True)
            recorded = events.read_text(encoding='utf-8')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(
            'kill:trading-deployment-operation.service', recorded)
        self.assertIn(
            'stop:trading-deployment-operation.service', recorded)

        for source, active, read in (
                (self.deploy, '$DEPLOY_STAGE/active-attempt',
                 'ATTEMPT_ID=$(sudo sed -n \'1p\''),
                (self.recover, '$ACTIVE',
                 'CURRENT=$(sudo sed -n \'1p\''),
                (self.emergency, '$ACTIVE_ATTEMPT',
                 'current=$(sed -n \'1p\'')):
            first_read = source.index(read)
            sync = source.rfind("sync-paths", 0, first_read)
            self.assertGreaterEqual(sync, 0)
            self.assertIn(active, source[sync:first_read])

        dispatch = self.recover.index('switch-active-attempt \\')
        final_stat = self.recover.index(
            "stat -c '%U:%G:%a:%h:%s' \"$ACTIVE\"", dispatch)
        self.assertLess(dispatch, final_stat)
        replace = self.attempt.index(
            "os.replace(\n            pending_name, active_name")
        stage_sync = self.attempt.index("os.fsync(dir_fd)", replace)
        final_read = self.attempt.index(
            "_read_attempt_pointer(dir_fd, active_name)", stage_sync)
        self.assertLess(replace, stage_sync)
        self.assertLess(stage_sync, final_read)

    def test_existing_source_contract_validation_failure_is_not_swallowed(self):
        start = self.recover.index('ensure_source_contract() {')
        end = self.recover.index('\n}\n', start) + 2
        function = self.recover[start:end]
        with tempfile.TemporaryDirectory() as directory:
            contract = os.path.join(directory, 'source-runtime.json')
            Path(contract).write_text('{}\n', encoding='utf-8')
            script = f'''set -Eeuo pipefail
SOURCE_CONTRACT={shlex.quote(contract)}
CURRENT_SEED=''
sudo() {{ "$@"; }}
journal() {{ return 23; }}
{function}
if ensure_source_contract; then
  exit 91
fi
'''
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-c', script],
                check=False, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_prepare_is_atomic_idempotent_clean_and_ci_explicit(self):
        self.assertIn('"status", "--porcelain=v1", "--untracked-files=all"',
                      self.prepare)
        self.assertIn('"--required-check"', self.prepare)
        self.assertIn('"--required-workflow"', self.prepare)
        self.assertIn("validate_driver(args.driver_dir, drivers)", self.prepare)
        self.assertIn("validate_stage(args.stage, descriptor)", self.prepare)
        self.assertIn('"deployment_old_runner_gate.py"', self.prepare)
        driver_publish = self.prepare.index(
            "os.rename(driver_tmp, args.driver_dir)")
        stage_publish = self.prepare.index("os.rename(stage_tmp, args.stage)")
        self.assertLess(driver_publish, stage_publish)
        self.assertIn("fsync_tree(stage_tmp)", self.prepare)
        self.assertIn("fsync_directory(args.stage.parent)", self.prepare)

    def test_integrity_runtime_and_managed_gates_exist(self):
        self.assertGreaterEqual(self.deploy.count("verify_reviewed_materials"), 3)
        self.assertIn("safe_install_managed", self.deploy)
        self.assertIn("validate-managed", self.deploy)
        self.assertIn("managed-$kind.before", self.deploy)
        self.assertIn("sudo cmp -s", self.deploy)
        self.assertIn("sudo mv --no-target-directory", self.deploy)
        self.assertIn("ci-workflow-runs.json", self.deploy)
        self.assertIn(
            "sort_by(.workflowName,.createdAt,.databaseId)", self.deploy)
        self.assertIn(
            '($e.check_runs|type=="array" and length==$e.total_count)',
            self.deploy)
        self.assertIn('.status!="completed" or .conclusion!="success"',
                      self.deploy)
        for name in ("reviewed-venv.json", "pip-freeze.txt", "python-version.txt",
                     "package-paths.txt"):
            self.assertIn(name, self.deploy)
        self.assertIn('RUNTIME_ROOT="/var/lib/trading-runtime/$RELEASE_SHA"',
                      self.deploy)
        self.assertNotIn("chown -R", self.deploy)
        self.assertIn("verify_release_tree", self.prepare)

    def test_release_closure_is_verified_before_first_candidate_execution(self):
        init = self.deploy.index(
            '"$ATTEMPT_HELPER" init')
        boundary = self.deploy.index(
            '\nverify_release_execution_boundary\n', init)
        first_slot = self.deploy.index('SLOT=$(completed_slot', boundary)
        self.assertLess(boundary, first_slot)
        function_start = self.deploy.index(
            'verify_release_execution_boundary() {')
        function_end = self.deploy.index('\n}\n', function_start)
        function = self.deploy[function_start:function_end]
        closure = function.index('ls-files --others -z')
        tracked = function.index('reviewed-tracked.sha256')
        venv = function.index('verify-venv-manifest')
        self.assertLess(closure, tracked)
        self.assertLess(tracked, venv)
        self.assertNotIn('$PYTHON', function)

        prepare_main = self.prepare.index('def main(')
        reject = self.prepare.index(
            'reject_unreviewed_release_entries(args.release_root)',
            prepare_main,
        )
        candidate_python = self.prepare.index(
            'str(trading / "deployment_evidence.py")', reject)
        self.assertLess(reject, candidate_python)

    def test_effective_units_reject_path_virtualization_and_hidden_inputs(self):
        for directive in (
                "RootDirectory", "RootImage", "BindPaths",
                "BindReadOnlyPaths", "TemporaryFileSystem", "MountImages",
                "ExtensionImages", "ExtensionDirectories", "LoadCredential",
                "LoadCredentialEncrypted", "SetCredential",
                "SetCredentialEncrypted"):
            self.assertGreaterEqual(self.deploy.count(f"'{directive}='"), 2)
            self.assertIn(directive, self.deploy.split(
                "prove_effective_units()", 1)[1].split(
                    "prove_formal_runner()", 1)[0])
        self.assertGreaterEqual(
            self.deploy.count(
                "'ExecSearchPath=/usr/sbin:/usr/bin:/sbin:/bin'"), 2)
        self.assertIn("validate-monitor-env", self.deploy)

    def test_monitor_dropin_restores_its_fixed_log_path_after_environment_reset(self):
        expected = (
            "TRADING_MEM_MONITOR_LOG=/var/log/trading-mem-monitor/"
            "mem_monitor.log")
        start = self.deploy.index(
            '"ExecStart=$PYTHON -B -E mem_monitor.py"')
        end = self.deploy.index(
            'safe_install_managed "$DROPIN_TMP" "$MONITOR_DROPIN"', start)
        self.assertIn(f"'Environment={expected}'", self.deploy[start:end])
        effective = self.deploy[
            self.deploy.index("prove_effective_units()"):
            self.deploy.index("prove_formal_runner()")]
        self.assertIn(expected, effective)
        self.assertIn(
            "TRADING_MEM_MONITOR_LOG=/var/log/trading-mem-monitor/"
            "mem_monitor\\.log",
            self.evidence.MANAGED_SCHEMAS["monitor_dropin"][1].pattern)

    def test_effective_exec_start_requires_exact_path_and_argv_boundary(self):
        start = self.deploy.index("prove_exact_exec_start() {")
        end = self.deploy.index("\n}\n\nprove_effective_units()", start) + 2
        function = self.deploy[start:end]
        python = f"/opt/trader-releases/{TEST_SHA}/trading/.venv/bin/python"
        argv = (
            f"{python} -B -E -m gunicorn -c gunicorn.conf.py "
            "wsgi:application")

        def accepted(value):
            script = f'''set -uo pipefail
EXEC_VALUE={shlex.quote(value)}
systemctl() {{ printf '%s\n' "$EXEC_VALUE"; }}
{function}
prove_exact_exec_start trading.service {shlex.quote(python)} {shlex.quote(argv)}
'''
            return subprocess.run(
                ["bash", "--noprofile", "--norc", "-c", script],
                check=False, capture_output=True, text=True).returncode == 0

        valid = (
            f"{{ path={python} ; argv[]={argv} ; ignore_errors=no ; "
            "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; "
            "code=(null) ; status=0/0 }")
        self.assertTrue(accepted(valid))
        for malformed in (
                valid.replace(
                    " wsgi:application ; ignore_errors=",
                    " wsgi:application --pythonpath /tmp/attacker --preload "
                    "; ignore_errors="),
                valid.replace(f"path={python}", "path=/tmp/attacker/python"),
                valid + f"\n{{ path={python} ; argv[]={argv} ; "
                "ignore_errors=no ; }}",
        ):
            with self.subTest(value=malformed):
                self.assertFalse(accepted(malformed))

    def test_reviewed_venv_manifest_handles_spaces_and_directory_symlinks(self):
        module = self.evidence
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            venv = root / "trading/.venv"
            (venv / "lib").mkdir(parents=True)
            regular = venv / "lib/launcher manifest.xml"
            regular.write_text("pass\n", encoding="utf-8")
            link = venv / "lib64"
            os.symlink("lib", link)
            entries = sorted([
                module._venv_manifest_entry(root, regular),
                module._venv_manifest_entry(root, link),
            ], key=lambda item: item["path"])
            manifest_text = json.dumps({
                "schema_version": 1,
                "entries": entries,
            }, sort_keys=True, separators=(",", ":"))

            with mock.patch.object(
                    module, "validate_venv_tree", return_value=str(venv)), \
                    mock.patch.object(
                        module, "_read_root_owned", return_value=manifest_text):
                self.assertTrue(module.verify_venv_manifest(
                    str(root), "/reviewed-venv.json"))
            link.unlink()
            os.symlink("other", link)
            with mock.patch.object(
                    module, "validate_venv_tree", return_value=str(venv)), \
                    mock.patch.object(
                        module, "_read_root_owned", return_value=manifest_text), \
                    self.assertRaisesRegex(
                        module.EvidenceError, "does not match"):
                module.verify_venv_manifest(
                    str(root), "/reviewed-venv.json")

    def test_emergency_is_self_contained_and_reinstalls_block(self):
        self.assertIn("ACTION=\"${1:---stop-and-arm}\"", self.emergency)
        self.assertIn("--install-block-only", self.emergency)
        self.assertIn(
            "emergency stop: --install-block-only is deployment-internal",
            self.emergency,
        )
        usage = self.emergency.split("usage: emergency-stop.sh", 1)[1]
        self.assertNotIn("--install-block-only", usage.split("\\n", 1)[0])
        self.assertGreaterEqual(
            self.deploy.count("TRADING_EMERGENCY_INTERNAL=1"), 1)
        self.assertIn("if install_start_block; then", self.emergency)
        self.assertLess(
            self.emergency.index("if install_start_block; then"),
            self.emergency.index(
                "sudo systemctl stop --no-block trading.service"))
        self.assertIn("gate_arm", self.emergency)
        for required in (
            "LIVE_TRADING=", "RELEASE_ROOT=", "PYTHON=", "DATA_DIR=",
            "OLD_RUNNER_LOCK_FILE=", "START_BLOCK=",
        ):
            self.assertIn(required, self.emergency)

    def test_hard_emergency_kills_writer_when_start_block_is_damaged(self):
        start = self.emergency.index("START_BLOCK_PROVEN=0")
        end = self.emergency.index("if verify_old_lock_contract; then", start)
        hard_boundary = self.emergency[start:end]
        stop_job = hard_boundary.index(
            "sudo systemctl stop --no-block trading.service")
        hard_kill = hard_boundary.index(
            "freeze_kill_stop_unit trading.service", stop_job)
        final_block_check = hard_boundary.index(
            'verify_loaded_emergency_start_block || fail=1', hard_kill)
        self.assertLess(stop_job, hard_kill)
        self.assertLess(hard_kill, final_block_check)

        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events"
            script = f'''set -uo pipefail
GRACEFUL=0
FALLBACK_CONTAIN_ONLY=0
fail=0
EVENTS={shlex.quote(str(events))}
install_start_block() {{ return 1; }}
install_emergency_start_block() {{ printf 'install-fallback\n' >>"$EVENTS"; return 0; }}
verify_loaded_start_block() {{ printf 'verify-block\n' >>"$EVENTS"; return 1; }}
verify_loaded_emergency_start_block() {{ printf 'verify-fallback\n' >>"$EVENTS"; return 0; }}
freeze_kill_stop_unit() {{ printf 'kill:%s\n' "$1" >>"$EVENTS"; return 0; }}
cgroup_is_empty() {{ return 0; }}
sudo() {{ "$@"; }}
systemctl() {{
  if [[ "$1" = stop ]]; then
    printf 'stop:%s:%s\n' "$2" "$3" >>"$EVENTS"
    return 0
  fi
  [[ "$1" = show ]] || return 1
  case "$4" in
    ActiveState) printf 'inactive\n' ;;
    MainPID) printf '0\n' ;;
    ControlGroup) printf '\n' ;;
    *) return 1 ;;
  esac
}}
{hard_boundary}
printf 'fail:%s\n' "$fail" >>"$EVENTS"
'''
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-c", script],
                check=False,
                capture_output=True,
                text=True,
            )
            recorded = events.read_text(encoding="utf-8").splitlines()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [
                "install-fallback",
                "verify-fallback",
                "stop:--no-block:trading.service",
                "kill:trading.service",
                "verify-fallback",
                "fail:1",
            ],
            recorded,
        )

    def test_damaged_primary_uses_independent_permanent_false_block(self):
        self.assertIn(
            "EMERGENCY_START_BLOCK=\"$START_BLOCK_DIR/"
            "zzzz-deploy-emergency-closed.conf\"",
            self.emergency,
        )
        self.assertIn(
            "printf '%s\\n' '[Unit]' 'ConditionPathExists=' \\",
            self.emergency)
        self.assertIn("'ConditionPathExists=!/' >\"$tmp\"", self.emergency)
        self.assertIn(
            "org.freedesktop.systemd1.Unit Conditions", self.emergency)
        for exact in (
                '"$signature" = \'a(sbbsi)\'', '"$count" = 1',
                '"$trigger" = false', '"$negate" = true',
                '"$parameter" = \'"/"\''):
            self.assertIn(exact, self.emergency)
        self.assertNotIn('rm -- "$EMERGENCY_START_BLOCK"', self.emergency)
        install_start = self.emergency.index('install_exact_start_block() {')
        install_end = self.emergency.index('\n}', install_start) + 2
        install = self.emergency[install_start:install_end]
        emergency_branch = install.index('if [[ "$kind" = emergency ]]')
        reload_ = install.index('sudo systemctl daemon-reload', emergency_branch)
        cleanup = install.index('sudo rm -f -- "$START_AUTH"', reload_)
        self.assertLess(reload_, cleanup)
        self.assertNotIn('rm -f -- "$EMERGENCY_START_BLOCK"', self.emergency)

        for source, first_state_read in (
                (self.deploy,
                 'ATTEMPT_ID=$(sudo sed -n \'1p\' "$DEPLOY_STAGE/active-attempt")'),
                (self.recover, 'CURRENT=$(sudo sed -n \'1p\' "$ACTIVE")')):
            incident = source.index(
                'run_emergency --contain-fallback-only')
            self.assertLess(incident, source.index(first_state_read))
            self.assertIn('[[ "$fallback_rc" -eq 75 ]]', source)

        contain_exit = self.emergency.index(
            "if [[ \"$FALLBACK_CONTAIN_ONLY\" -eq 1 ]]")
        source_contract = self.emergency.index(
            'if verify_old_lock_contract; then', contain_exit)
        self.assertLess(contain_exit, source_contract)

    def test_start_block_proofs_use_structured_systemd_conditions(self):
        legacy_text_probe = (
            'systemctl show trading.service -p Conditions --value')
        for source in (self.deploy, self.emergency, self.recover):
            self.assertNotIn(legacy_text_probe, source)
            self.assertIn(
                'org.freedesktop.systemd1.Unit Conditions', source)
            self.assertIn('"$signature" = \'a(sbbsi)\'', source)
            self.assertIn('"$count" = 1', source)
            self.assertIn('"$negate" = false', source)
            self.assertIn('"$parameter" = "\\"$START_AUTH\\""', source)
        self.assertIn(
            '"$signature" = \'a(sbbsi)\' && "$count" = 0',
            self.deploy)

    def test_recovery_does_not_require_main_pid_from_timer_unit(self):
        self.assertIn('[[ "$unit" = *.timer ]] && continue', self.recover)

    def test_all_timer_inactive_proofs_skip_service_only_fields(self):
        self.assertIn('prove_unit_inactive "$unit" || return 1', self.deploy)
        timer_branch = 'if [[ "$unit" = *.timer ]]; then'
        for source in (self.deploy, self.recover):
            self.assertIn(timer_branch, source)
            self.assertIn('unit_job_absent "$unit" || return 1', source)

    def test_backup_script_inode_is_synced_before_atomic_replace(self):
        install = self.deploy.index(
            '"$DEPLOY_STAGE/trading-state-backup.reviewed" '
            '"$BACKUP_SCRIPT_TMP"')
        sync = self.deploy.index(
            '--path "$BACKUP_SCRIPT_TMP" >/dev/null', install)
        replace = self.deploy.index(
            'sudo mv --no-target-directory "$BACKUP_SCRIPT_TMP"', sync)
        self.assertLess(install, sync)
        self.assertLess(sync, replace)

    def test_no_secret_values_or_interactive_prompts(self):
        combined = self.deploy + self.emergency
        self.assertNotIn("read -p", combined)
        self.assertNotIn("set -x", combined)
        self.assertNotIn("OKX_API_KEY=", combined)
        self.assertNotIn("TRADING_API_TOKEN=", combined)
        self.assertIn("EnvironmentFile=/etc/trading.env", self.deploy)
        self.assertNotIn("apiSecret", self.old_gate)

    def test_evidence_exact_schema_expiry_and_bindings(self):
        module = self.evidence
        now = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
        bindings = {"report_sha256": "b" * 64,
                    "sentinel_identity": "1:2"}
        payload = {
            "schema_version": 1,
            "kind": "migration",
            "release_sha": TEST_SHA,
            "decision": "approved",
            "issued_at": "2026-07-19T00:00:00Z",
            "expires_at": "2026-07-19T00:10:00Z",
            "bindings": bindings,
        }
        self.assertEqual(module.validate_payload(
            payload, kind="migration", release_sha=TEST_SHA,
            bindings=bindings, now=now), payload)
        for schema in (True, 1.0):
            with self.subTest(schema=schema), \
                    self.assertRaisesRegex(
                        module.EvidenceError, "schema version"):
                module.validate_payload(
                    dict(payload, schema_version=schema),
                    kind="migration", release_sha=TEST_SHA,
                    bindings=bindings, now=now)
        bad = dict(payload, extra=True)
        with self.assertRaises(module.EvidenceError):
            module.validate_payload(
                bad, kind="migration", release_sha=TEST_SHA,
                bindings=bindings, now=now)
        with self.assertRaises(module.EvidenceError):
            module.validate_payload(
                payload, kind="migration", release_sha=TEST_SHA,
                bindings=bindings, now=now + timedelta(hours=1))
        with self.assertRaises(module.EvidenceError):
            module.validate_payload(
                payload, kind="migration", release_sha=TEST_SHA,
                bindings=bindings,
                now=datetime(2026, 7, 19, 0, 1))
        with self.assertRaises(module.EvidenceError):
            module.validate_payload(
                payload, kind="migration", release_sha=TEST_SHA,
                bindings=bindings,
                now=datetime(2026, 7, 19, 0, 10, tzinfo=timezone.utc))
        duplicate = '{"a":1,"a":2}'
        with self.assertRaises(module.EvidenceError):
            module.loads_strict(duplicate)
        with self.assertRaises(module.EvidenceError):
            module.loads_strict(json.dumps({"x": float("nan")}))

    def test_evidence_rejects_symlink_and_unsafe_parent(self):
        module = self.evidence
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_text("{}", encoding="utf-8")
            link = Path(directory) / "approval"
            os.symlink(target, link)
            with self.assertRaisesRegex(module.EvidenceError, "symlink"):
                module._read_root_owned(str(link))
            with self.assertRaisesRegex(module.EvidenceError, "parent"):
                module._read_root_owned(str(target.resolve()))

    def test_evidence_rejects_metadata_change_during_read(self):
        module = self.evidence
        path = "/protected/approval.json"
        regular = SimpleNamespace(
            st_dev=1, st_ino=2, st_mode=stat.S_IFREG | 0o600,
            st_uid=0, st_nlink=1, st_size=2)
        changed = SimpleNamespace(
            st_dev=1, st_ino=2, st_mode=stat.S_IFREG | 0o640,
            st_uid=0, st_nlink=1, st_size=2)
        protected_dir = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700, st_uid=0)
        with mock.patch.object(module.os.path, "realpath", return_value=path), \
                mock.patch.object(module.os, "stat", return_value=protected_dir), \
                mock.patch.object(module.os, "lstat", return_value=regular), \
                mock.patch.object(module.os, "open", return_value=7), \
                mock.patch.object(module.os, "fstat",
                                  side_effect=[regular, changed]), \
                mock.patch.object(module.os, "read", side_effect=[b"{}", b""]), \
                mock.patch.object(module.os, "close"):
            with self.assertRaisesRegex(module.EvidenceError,
                                        "changed while reading"):
                module._read_root_owned(path)

    def test_managed_schemas_allow_only_canonical_content(self):
        module = self.evidence
        old_sha = "b" * 40
        old_env = (
            f"TRADING_RUNNER_LOCK_FILE=/var/lib/trading-runtime/{old_sha}/"
            ".runtime/runner.lock\n"
            f"TRADING_MAINTENANCE_SENTINEL=/var/lib/trading-runtime/{old_sha}/"
            ".maintenance_no_open\n"
            f"TRADING_CONFIG_FILE=/var/lib/trading-runtime/{old_sha}/config.json\n"
            f"TRADING_DATA_DIR=/var/lib/trading-runtime/{old_sha}\n")
        self.assertEqual(
            old_env, module.validate_managed_text(old_env, "release_env"))
        with self.assertRaisesRegex(module.EvidenceError, "known schema"):
            module.validate_managed_text(
                old_env + "UNREVIEWED=1\n", "release_env")
        with self.assertRaises(module.EvidenceError):
            module.validate_managed_text(old_env, "unknown")
        mismatched = (
            "[Service]\n"
            f"WorkingDirectory=/opt/trader-releases/{old_sha}/trading\n"
            "ExecStart=\n"
            f"ExecStart=/opt/trader-releases/{TEST_SHA}/trading/.venv/bin/"
            "python -B -E -m gunicorn -c gunicorn.conf.py wsgi:application\n"
            "EnvironmentFile=/etc/trading-release.env\n"
            "UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH GUNICORN_CMD_ARGS "
            "PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT PYTHONUSERBASE\n")
        with self.assertRaisesRegex(module.EvidenceError, "known schema"):
            module.validate_managed_text(mismatched, "release_dropin")

    def test_environment_allowlist_and_venv_target_immutability(self):
        module = self.evidence
        valid = (
            "FLASK_SECRET_KEY=01234567890123456789012345678901\n"
            "TRADING_API_TOKEN=token\n"
            "OKX_API_KEY=key\n")
        self.assertEqual(
            ("FLASK_SECRET_KEY", "OKX_API_KEY", "TRADING_API_TOKEN"),
            module._validate_trading_env_text(valid))
        for injected in ("LD_PRELOAD=/tmp/x.so\n", "PYTHONPATH=/tmp/x\n",
                         "GUNICORN_CMD_ARGS=--pythonpath=/tmp\n",
                         "UNKNOWN_KEY=value\n"):
            with self.subTest(injected=injected), self.assertRaises(
                    module.EvidenceError):
                module._validate_trading_env_text(valid + injected)
        module._assert_immutable_target("/usr/bin/python3", "system python")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "python"
            target.write_text("mutable", encoding="utf-8")
            link = Path(directory) / "venv-python"
            os.symlink(target, link)
            with self.assertRaisesRegex(
                    module.EvidenceError, "immutable|writable"):
                module._assert_immutable_target(str(link), "venv symlink")
        root_file = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o555, st_uid=0)
        nonroot_parent = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o555, st_uid=501)
        with mock.patch.object(
                module.os.path, "realpath", return_value="/unsafe/python"), \
                mock.patch.object(module.os.path, "exists", return_value=True), \
                mock.patch.object(
                    module.os, "stat",
                    side_effect=[root_file, nonroot_parent]), \
                self.assertRaisesRegex(module.EvidenceError, "non-root"):
            module._assert_immutable_target(
                "/venv/python", "non-root chmod-capable parent")

    def test_writer_inventory_requires_concrete_single_writer_boundary(self):
        module = self.evidence
        controls = {
            "human": "operator_attestation_and_okx_ui_order_entry_freeze",
            "host": "reviewed_process_and_unit_inventory",
            "credential_consumer": "credential_consumer_inventory_and_freeze",
        }
        payload = {
            "schema_version": 2,
            "release_sha": TEST_SHA,
            "all_other_writers_frozen": True,
            "single_writer_boundary": {
                "exchange_ui_manual_actions_frozen": True,
                "all_other_api_credentials_and_consumers_frozen": True,
                "host_process_inventory_complete": True,
                "runtime_system_api_only_acknowledged": True,
                "same_side_size_replacement_unattributable_acknowledged": True,
            },
            "writers": [{
                "id": f"reviewed-{kind}",
                "type": kind,
                "scope": "managed_swap_order_writes",
                "frozen": True,
                "evidence": {
                    "control": control,
                    "observed_at": "2026-07-19T00:00:00Z",
                    "subject": f"concrete managed SWAP boundary for {kind}",
                },
            } for kind, control in controls.items()],
        }
        self.assertEqual(
            payload,
            module.validate_writer_inventory_payload(payload, TEST_SHA))
        with self.assertRaisesRegex(module.EvidenceError, "release/schema"):
            module.validate_writer_inventory_payload(
                dict(payload, schema_version=2.0), TEST_SHA)
        for mutation in ("boundary", "human", "credentials"):
            bad = json.loads(json.dumps(payload))
            if mutation == "boundary":
                del bad["single_writer_boundary"][
                    "exchange_ui_manual_actions_frozen"]
            elif mutation == "human":
                bad["writers"] = [
                    item for item in bad["writers"]
                    if item["type"] != "human"]
            else:
                bad["writers"] = [
                    item for item in bad["writers"]
                    if item["type"] != "credential_consumer"]
            with self.subTest(mutation=mutation), self.assertRaises(
                    module.EvidenceError):
                module.validate_writer_inventory_payload(bad, TEST_SHA)

    def test_writer_freeze_precedes_runtime_mutation(self):
        writer_freeze = self.deploy.index(
            'write_request writer_freeze')
        old_gate = self.deploy.index('OLD_MAIN_PID=$(', writer_freeze)
        probe = self.deploy.index('old_gate_client probe-handshake', old_gate)
        intent = self.deploy.index(
            'publish_attempt_artifact old-no-open-arm-intent.json', probe)
        establish = self.deploy.index(
            'old_gate_client establish-handshake', intent)
        publish = self.deploy.index(
            'publish_attempt_artifact old-no-open-boundary.json', establish)
        block = self.deploy.index(
            'run_emergency --install-block-only', publish)
        self.assertLess(writer_freeze, old_gate)
        self.assertLess(old_gate, probe)
        self.assertLess(probe, intent)
        self.assertLess(intent, establish)
        self.assertLess(establish, publish)
        self.assertLess(publish, block)
        self.assertNotIn('credential-exposure', self.deploy)
        self.assertNotIn('public-history-exposure', self.deploy)

    def test_all_python_boundaries_ignore_python_and_loader_injection(self):
        combined = self.deploy + self.emergency + self.recover
        self.assertNotRegex(
            combined, r'(?m)^[ \t]*"\$PYTHON" (?!-B -E)')
        self.assertIn("UnsetEnvironment=$UNSET_ENV", self.deploy)
        for dangerous in ("LD_PRELOAD", "LD_LIBRARY_PATH",
                          "GUNICORN_CMD_ARGS", "PYTHONPATH", "PYTHONHOME"):
            self.assertIn(dangerous, self.deploy)
        self.assertIn("validate-trading-env", self.deploy)
        self.assertIn("trading_env_sha256", self.deploy)
        declared = self.deploy.split("readonly UNSET_ENV='", 1)[1].split(
            "'", 1)[0]
        self.assertEqual(self.evidence.UNSET_ENV_TEXT, declared)
        for dangerous in (
                "HTTP_PROXY", "https_proxy", "SSL_CERT_FILE",
                "REQUESTS_CA_BUNDLE", "OPENSSL_CONF", "SSLKEYLOGFILE"):
            self.assertIn(dangerous, declared)

    def test_notes_delegate_commands_to_reviewed_artifacts(self):
        self.assertIn("deploy.sh", self.notes)
        self.assertIn("emergency-stop.sh", self.notes)
        self.assertIn("deployment_evidence.py", self.notes)
        self.assertLess(self.notes.count("```bash"), 3)
        self.assertNotIn("systemctl start trading.service", self.notes)


if __name__ == "__main__":
    unittest.main()
