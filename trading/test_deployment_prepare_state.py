"""Dynamic tests for deployment journal and atomic prepare publication."""

import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parent
SHA = "a" * 40
DRIVER_SHA = "b" * 64


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AttemptJournalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load("deployment_attempt")
        cls.old_uid = cls.module.ROOT_UID
        cls.old_gid = cls.module.ROOT_GID
        cls.module.ROOT_UID = os.geteuid()
        cls.module.ROOT_GID = os.getegid()

    @classmethod
    def tearDownClass(cls):
        cls.module.ROOT_UID = cls.old_uid
        cls.module.ROOT_GID = cls.old_gid

    def attempt(self, root, attempt_id="0001"):
        path = Path(root) / attempt_id
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
        return path

    def args(self, path, attempt_id="0001"):
        return str(path), SHA, attempt_id, DRIVER_SHA

    def test_init_adjacent_advances_and_commit_ready_chain(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.attempt(root)
            self.module.init_journal(*self.args(path))
            for previous, following in zip(
                    self.module.PHASES, self.module.PHASES[1:]):
                payload = self.module.advance_journal(
                    *self.args(path), previous, following,
                    [f"proof={following.lower()}"])
                self.assertEqual(following, payload["phase"])
            fd = self.module._open_attempt_dir(str(path))
            try:
                chain, last_digest, abandoned = self.module._load_chain(
                    fd, SHA, "0001", DRIVER_SHA)
            finally:
                os.close(fd)
            self.assertEqual("COMMIT_READY", chain[-1]["phase"])
            self.assertRegex(last_digest, r"^[0-9a-f]{64}$")
            self.assertFalse(abandoned)

    def test_duplicate_skip_and_reused_expect_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.attempt(root)
            self.module.init_journal(*self.args(path))
            with self.assertRaisesRegex(self.module.JournalError, "already started"):
                self.module.init_journal(*self.args(path))
            with self.assertRaisesRegex(self.module.JournalError, "not consecutive"):
                self.module.advance_journal(
                    *self.args(path), "PREPARED", "T0", [])
            self.module.advance_journal(
                *self.args(path), "PREPARED", "G0", ["proof=g0"])
            with self.assertRaisesRegex(self.module.JournalError, "current phase"):
                self.module.advance_journal(
                    *self.args(path), "PREPARED", "G0", ["proof=g0"])

    def test_gap_and_content_tamper_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.attempt(root)
            self.module.init_journal(*self.args(path))
            os.rename(
                path / "phase-00-prepared.json",
                path / "phase-01-g0.json")
            fd = self.module._open_attempt_dir(str(path))
            try:
                with self.assertRaisesRegex(self.module.JournalError, "gap"):
                    self.module._load_chain(fd, SHA, "0001", DRIVER_SHA)
            finally:
                os.close(fd)

        for invalid in (
                b'{"schema_version":1,"schema_version":1}\n',
                b'{"schema_version":NaN}\n'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as root:
                path = self.attempt(root)
                entry = path / "phase-00-prepared.json"
                entry.write_bytes(invalid)
                os.chmod(entry, 0o600)
                fd = self.module._open_attempt_dir(str(path))
                try:
                    with self.assertRaises(self.module.JournalError):
                        self.module._load_chain(
                            fd, SHA, "0001", DRIVER_SHA)
                finally:
                    os.close(fd)
        with tempfile.TemporaryDirectory() as root:
            path = self.attempt(root)
            self.module.init_journal(*self.args(path))
            (path / "phase-00-prepared.json").write_text("{}\n", encoding="ascii")
            os.chmod(path / "phase-00-prepared.json", 0o600)
            fd = self.module._open_attempt_dir(str(path))
            try:
                with self.assertRaisesRegex(self.module.JournalError, "schema"):
                    self.module._load_chain(fd, SHA, "0001", DRIVER_SHA)
            finally:
                os.close(fd)

    def test_journal_and_recovery_schemas_require_exact_integers(self):
        for field, invalid in (
                ("schema_version", True),
                ("schema_version", 1.0),
                ("sequence", False),
                ("sequence", 0.0)):
            with self.subTest(field=field, invalid=invalid), \
                    tempfile.TemporaryDirectory() as root:
                path = self.attempt(root)
                self.module.init_journal(*self.args(path))
                entry = path / "phase-00-prepared.json"
                payload = json.loads(entry.read_text(encoding="utf-8"))
                payload[field] = invalid
                entry.write_text(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")) +
                    "\n", encoding="utf-8")
                os.chmod(entry, 0o600)
                fd = self.module._open_attempt_dir(str(path))
                try:
                    with self.assertRaisesRegex(
                            self.module.JournalError, "integer fields"):
                        self.module._load_chain(
                            fd, SHA, "0001", DRIVER_SHA)
                finally:
                    os.close(fd)

        with tempfile.TemporaryDirectory() as root:
            path = self.attempt(root)
            self.module.init_journal(*self.args(path))
            self.module.abandon_journal(*self.args(path), "operator_reset")
            abandoned = path / "abandoned.json"
            payload = json.loads(abandoned.read_text(encoding="utf-8"))
            payload["schema_version"] = True
            abandoned.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) +
                "\n", encoding="utf-8")
            os.chmod(abandoned, 0o600)
            fd = self.module._open_attempt_dir(str(path))
            try:
                with self.assertRaisesRegex(
                        self.module.JournalError, "abandoned"):
                    self.module._load_chain(fd, SHA, "0001", DRIVER_SHA)
            finally:
                os.close(fd)

        source = {
            "schema_version": True,
            "source_trading": "/tmp/trading",
            "source_data": "/tmp/data",
            "source_trading_identity": {"dev": 1, "ino": 2},
            "source_data_identity": {"dev": 1, "ino": 3},
            "runner_lock": {
                "path": "/tmp/data/.runtime/runner.lock",
                "dev": 1,
                "ino": 4,
            },
            "completed_schedule_slot": "2026-07-19",
            "data_state": "migration_complete",
        }
        with mock.patch.object(
                self.module, "describe_source", return_value=source), \
                self.assertRaisesRegex(
                    self.module.JournalError, "schema version"):
            self.module._validate_source_contract(source)

        seed = {
            "schema_version": True,
            "release_sha": SHA,
            "attempt_id": "0002",
            "driver_sha256": DRIVER_SHA,
            "entry_state": "inactive_no_open",
            "previous_attempt_id": "0001",
            "previous_phase": "G0",
            "previous_phase_sha256": "c" * 64,
            "source": source,
        }
        with mock.patch.object(self.module, "_validate_source_contract"), \
                self.assertRaisesRegex(
                    self.module.JournalError, "seed binding"):
            self.module._validate_recovery_seed(
                seed, SHA, "0002", DRIVER_SHA)

    def test_abandon_is_idempotent_but_conflicting_reason_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.attempt(root)
            self.module.init_journal(*self.args(path))
            first = self.module.abandon_journal(
                *self.args(path), "operator_reset")
            second = self.module.abandon_journal(
                *self.args(path), "operator_reset")
            self.assertEqual(first, second)
            with self.assertRaisesRegex(self.module.JournalError, "different"):
                self.module.abandon_journal(
                    *self.args(path), "different_reason")

    def test_runtime_tree_digest_detects_content_mounts_and_unsafe_inodes(self):
        with tempfile.TemporaryDirectory() as root:
            tree = Path(root).resolve()
            state_dir = tree / "data"
            state_dir.mkdir()
            state = state_dir / "state.json"
            state.write_bytes(b"AAAA")
            os.chmod(state, 0o600)
            before = state.stat()
            first = self.module.sync_tree(str(tree), "0001", "active")
            second = self.module.sync_tree(str(tree), "0001", "active")
            self.assertEqual(first["tree_sha256"], second["tree_sha256"])

            state.write_bytes(b"BBBB")
            os.utime(
                state, ns=(before.st_atime_ns, before.st_mtime_ns))
            changed = self.module.sync_tree(str(tree), "0001", "active")
            self.assertNotEqual(first["tree_sha256"], changed["tree_sha256"])

            # The gate creates this write-once artifact after RUNTIME_READY but
            # just before the T0 journal transition. It is independently
            # validated by gate recovery and must not invalidate the stable
            # migrated-payload digest in that crash window.
            state.write_bytes(b"AAAA")
            baseline = tree / "deployment_no_open_baseline.json"
            baseline.write_bytes(b"strict-gate-evidence")
            os.chmod(baseline, 0o600)
            with_baseline = self.module.sync_tree(str(tree), "0001", "active")
            self.assertEqual(first["tree_sha256"], with_baseline["tree_sha256"])
            baseline.write_bytes(b"changed-gate-evidence")
            changed_baseline = self.module.sync_tree(
                str(tree), "0001", "active")
            self.assertEqual(
                first["tree_sha256"], changed_baseline["tree_sha256"])
            baseline.unlink()

            # The current attempt's gate archive is a separately validated
            # control plane. Its sentinel rename and write-once audit must not
            # alter the stable payload digest across a recovery retry.
            sentinel = tree / ".maintenance_no_open"
            sentinel.write_bytes(b"sentinel")
            os.chmod(sentinel, 0o600)
            with_sentinel = self.module.sync_tree(str(tree), "0001", "active")
            self.assertEqual(first["tree_sha256"], with_sentinel["tree_sha256"])
            sentinel.rename(tree / ".abandoned.0001..maintenance_no_open")
            audit = tree / "deployment_abandon_0001.json"
            audit.write_bytes(b"audit")
            os.chmod(audit, 0o600)
            with self.assertRaisesRegex(
                    self.module.JournalError, "active runtime"):
                self.module.sync_tree(str(tree), "0001", "active")
            with_archive = self.module.sync_tree(
                str(tree), "0001", "abandoned")
            self.assertEqual(first["tree_sha256"], with_archive["tree_sha256"])

            prior_archive = tree / ".abandoned.0000..maintenance_no_open"
            prior_archive.write_bytes(b"prior")
            os.chmod(prior_archive, 0o600)
            with_prior_archive = self.module.sync_tree(
                str(tree), "0001", "abandoned")
            self.assertNotEqual(
                first["tree_sha256"], with_prior_archive["tree_sha256"])
            prior_archive.unlink()
            audit.unlink()
            (tree / ".abandoned.0001..maintenance_no_open").unlink()

            reserved_directory = tree / "deployment_abandon_0001.json"
            reserved_directory.mkdir()
            with self.assertRaisesRegex(
                    self.module.JournalError, "regular file"):
                self.module.sync_tree(str(tree), "0001", "active")
            with self.assertRaisesRegex(
                    self.module.JournalError, "regular file"):
                self.module.sync_tree(str(tree), "0001", "abandoned")
            reserved_directory.rmdir()

            unexpected = tree / "unexpected.json"
            unexpected.write_bytes(b"unexpected")
            os.chmod(unexpected, 0o600)
            with_unexpected = self.module.sync_tree(
                str(tree), "0001", "active")
            self.assertNotEqual(
                first["tree_sha256"], with_unexpected["tree_sha256"])
            unexpected.unlink()

            hardlink = state_dir / "hardlink.json"
            os.link(state, hardlink)
            with self.assertRaisesRegex(
                    self.module.JournalError, "non-regular"):
                self.module.sync_tree(str(tree), "0001", "active")
            hardlink.unlink()

            symlink = state_dir / "link.json"
            os.symlink(state, symlink)
            with self.assertRaisesRegex(
                    self.module.JournalError, "non-regular"):
                self.module.sync_tree(str(tree), "0001", "active")
            symlink.unlink()

            with mock.patch.object(
                    self.module, "_mount_points",
                    return_value=[str(state_dir)]), \
                    self.assertRaisesRegex(
                        self.module.JournalError, "mount point"):
                self.module.sync_tree(str(tree), "0001", "active")

    def test_visible_recovery_seed_retries_file_and_directory_durability(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            attempt = self.attempt(root, "0002")
            source = root / "trading"
            (source / ".runtime").mkdir(parents=True, mode=0o700)
            lock = source / ".runtime/runner.lock"
            lock.write_bytes(b"")
            lock.chmod(0o600)
            identity = SimpleNamespace(
                pw_uid=os.geteuid(), pw_gid=os.getegid())
            seed_path = attempt / self.module.RECOVERY_SEED_NAME
            real_fsync = self.module.os.fsync
            failed = False

            def fail_directory_fsync_after_publish(fd):
                nonlocal failed
                if (not failed and seed_path.exists() and
                        stat.S_ISDIR(os.fstat(fd).st_mode)):
                    failed = True
                    raise OSError("injected directory fsync failure")
                return real_fsync(fd)

            common = (
                str(attempt), SHA, "0002", DRIVER_SHA,
                "0001", "G0", "c" * 64,
                str(source), str(source),
                "2026-07-19", "requires_migration",
            )
            with mock.patch.object(
                    self.module, "LIVE_TRADING", str(source)), \
                    mock.patch.object(
                        self.module.pwd, "getpwnam", return_value=identity), \
                    mock.patch.object(
                        self.module.os, "fsync",
                        side_effect=fail_directory_fsync_after_publish), \
                    self.assertRaises(OSError):
                self.module.create_recovery_seed(*common)
            self.assertTrue(seed_path.is_file())

            fsynced_kinds = []

            def record_fsync(fd):
                fsynced_kinds.append(stat.S_IFMT(os.fstat(fd).st_mode))
                return real_fsync(fd)

            with mock.patch.object(
                    self.module, "LIVE_TRADING", str(source)), \
                    mock.patch.object(
                        self.module.pwd, "getpwnam", return_value=identity), \
                    mock.patch.object(
                        self.module.os, "fsync", side_effect=record_fsync):
                recovered = self.module.create_recovery_seed(*common)
            self.assertEqual("0002", recovered["attempt_id"])
            self.assertIn(stat.S_IFREG, fsynced_kinds)
            self.assertIn(stat.S_IFDIR, fsynced_kinds)


class PreparePublicationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load("prepare_deployment")
        cls.old_uid = cls.module.ROOT_UID
        cls.old_gid = cls.module.ROOT_GID
        cls.module.ROOT_UID = os.geteuid()
        cls.module.ROOT_GID = os.getegid()

    @classmethod
    def tearDownClass(cls):
        cls.module.ROOT_UID = cls.old_uid
        cls.module.ROOT_GID = cls.old_gid

    def make_driver(self, path, expected):
        path.mkdir(mode=0o755)
        os.chmod(path, 0o755)
        for name, payload in expected.items():
            target = path / name
            target.write_bytes(payload)
            os.chmod(target, 0o555)

    def test_venv_manifest_hashes_directory_symlink_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "lib"
            target.mkdir()
            link = root / "lib64"
            os.symlink("lib", link)
            expected = self.module.digest_bytes(b"symlink\0lib")
            self.assertEqual(expected, self.module.digest_venv_entry(link))
            (target / "module.py").write_text("pass\n", encoding="utf-8")
            self.assertEqual(expected, self.module.digest_venv_entry(link))
            link.unlink()
            os.symlink("other", link)
            self.assertNotEqual(expected, self.module.digest_venv_entry(link))

    def test_untracked_release_closure_allows_only_bound_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            trading = root / "trading"
            trading.mkdir()
            (root / ".gitignore").write_text(
                "trading/.venv/\ntrading/config.json\n__pycache__/\n",
                encoding="utf-8",
            )
            (trading / "main.py").write_text("pass\n", encoding="utf-8")
            self.module.run(["git", "init", str(root)])
            self.module.run([
                "git", "-C", str(root), "add", ".gitignore",
                "trading/main.py",
            ])
            venv_file = trading / ".venv/lib/python/site.py"
            venv_file.parent.mkdir(parents=True)
            venv_file.write_text("pass\n", encoding="utf-8")

            self.module.reject_unreviewed_release_entries(root)

            config = trading / "config.json"
            config.write_text('{"secret":"must-not-ship"}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "config.json"):
                self.module.reject_unreviewed_release_entries(root)
            config.unlink()

            pyc = trading / "__pycache__/main with space.cpython-312.pyc"
            pyc.parent.mkdir()
            pyc.write_bytes(b"unreviewed")
            with self.assertRaisesRegex(RuntimeError, "main with space"):
                self.module.reject_unreviewed_release_entries(root)

    def stage_payloads(self):
        checks = json.dumps({"total_count": 1, "check_runs": [{
            "name": "required-check", "head_sha": SHA,
            "status": "completed", "conclusion": "success",
        }]}, sort_keys=True, separators=(",", ":")).encode()
        runs = json.dumps([{
            "databaseId": 1,
            "workflowName": "required-workflow", "headSha": SHA,
            "status": "completed", "conclusion": "success",
            "createdAt": "2026-07-19T00:00:00Z",
        }], sort_keys=True, separators=(",", ":")).encode()
        reviewed = {
            name: f"reviewed {name}\n".encode()
            for name in (
                set(self.module.REVIEWED_INPUTS) |
                set(self.module.TRACKED_REVIEWED_INPUTS))
        }
        return checks, runs, reviewed, b"#!/bin/sh\nexit 0\n"

    def descriptor(self):
        checks, runs, reviewed, backup = self.stage_payloads()
        return {
            "schema_version": 1,
            "release_sha": SHA,
            "prepare_tool_sha256": "b" * 64,
            "ci_checks_sha256": self.module.digest_bytes(checks),
            "ci_runs_sha256": self.module.digest_bytes(runs),
            "required_checks": ["required-check"],
            "required_workflows": ["required-workflow"],
            "reviewed_inputs": {
                name: self.module.digest_bytes(payload)
                for name, payload in reviewed.items()
            },
            "backup_original_sha256": self.module.digest_bytes(backup),
        }

    def make_stage(self, path, descriptor):
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
        checks, runs, reviewed, backup = self.stage_payloads()
        self.module.write_snapshot(path / "ci-check-runs.json", checks)
        self.module.write_snapshot(path / "ci-workflow-runs.json", runs)
        for name, payload in reviewed.items():
            self.module.write_snapshot(path / name, payload)
        self.module.write_snapshot(
            path / "trading-state-backup.original", backup)
        self.module.write_private(
            path / "prepare-request.json",
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n")
        self.module.write_private(path / "asset.txt", "reviewed\n")
        self.module.write_asset_manifest(path)
        attempts = path / "attempts"
        attempts.mkdir(mode=0o700)
        os.chmod(attempts, 0o700)
        attempt = attempts / "0001"
        attempt.mkdir(mode=0o700)
        os.chmod(attempt, 0o700)
        self.module.write_private(path / "active-attempt", "0001\n")

    def test_driver_only_crash_state_resumes_then_complete_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            driver = root / "driver"
            stage = root / "stage"
            expected = {"deploy.sh": b"reviewed driver\n"}
            descriptor = self.descriptor()
            self.make_driver(driver, expected)
            self.assertEqual(
                "driver_only",
                self.module.published_state(stage, driver, expected, descriptor))
            self.make_stage(stage, descriptor)
            self.assertEqual(
                "complete",
                self.module.published_state(stage, driver, expected, descriptor))
            self.assertEqual(
                "complete",
                self.module.published_state(stage, driver, expected, descriptor))

    def test_stage_without_driver_and_published_tamper_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            driver = root / "driver"
            stage = root / "stage"
            expected = {"deploy.sh": b"reviewed driver\n"}
            descriptor = self.descriptor()
            self.make_stage(stage, descriptor)
            with self.assertRaisesRegex(RuntimeError, "without"):
                self.module.published_state(stage, driver, expected, descriptor)
            self.make_driver(driver, expected)
            os.chmod(driver / "deploy.sh", 0o755)
            (driver / "deploy.sh").write_bytes(b"tampered\n")
            os.chmod(driver / "deploy.sh", 0o555)
            with self.assertRaisesRegex(RuntimeError, "mismatch"):
                self.module.published_state(stage, driver, expected, descriptor)

    def test_snapshot_replacement_never_follows_symlink_or_chmods_victim(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            source = root / "source.json"
            victim = root / "victim"
            source.write_bytes(b"{}")
            victim.write_bytes(b"safe")
            os.chmod(victim, 0o644)
            real_open = self.module.os.open
            replaced = False

            def replace_before_open(path, flags, *args, **kwargs):
                nonlocal replaced
                if not replaced and Path(path) == source:
                    replaced = True
                    source.unlink()
                    os.symlink(victim, source)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(
                    self.module.os, "open", side_effect=replace_before_open), \
                    self.assertRaises(OSError):
                self.module.read_reviewed_snapshot(source)
            self.assertEqual(0o644, stat.S_IMODE(victim.stat().st_mode))
            self.assertEqual(b"safe", victim.read_bytes())

    def test_strict_ci_and_descriptor_binding_reject_split_views(self):
        duplicate = (
            b'{"check_runs":[{"name":"required-check",'
            b'"head_sha":"' + SHA.encode() +
            b'","status":"failure","status":"completed",'
            b'"conclusion":"success"}]}')
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            self.module.verify_ci(
                duplicate, SHA, True, ["required-check"])

        inconsistent_checks = json.dumps({
            "total_count": 0,
            "check_runs": [{
                "name": "required-check",
                "head_sha": SHA,
                "status": "completed",
                "conclusion": "success",
            }],
        }).encode()
        with self.assertRaisesRegex(RuntimeError, "total_count"):
            self.module.verify_ci(
                inconsistent_checks, SHA, True, ["required-check"])
        truncated_checks = json.dumps({
            "total_count": 2,
            "check_runs": [{
                "name": "required-check",
                "head_sha": SHA,
                "status": "completed",
                "conclusion": "success",
            }],
        }).encode()
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            self.module.verify_ci(
                truncated_checks, SHA, True, ["required-check"])

        invalid_created_at = json.dumps([
            {
                "databaseId": 2,
                "workflowName": "required-workflow",
                "headSha": SHA,
                "status": "completed",
                "conclusion": "failure",
                "createdAt": "2026-07-19T01:00:00Z",
            },
            {
                "databaseId": 1,
                "workflowName": "required-workflow",
                "headSha": SHA,
                "status": "completed",
                "conclusion": "success",
                "createdAt": True,
            },
        ]).encode()
        with self.assertRaisesRegex(RuntimeError, "createdAt"):
            self.module.verify_ci(
                invalid_created_at, SHA, False, ["required-workflow"])

        wrong_old_sha = json.dumps([
            {
                "databaseId": 1,
                "workflowName": "required-workflow",
                "headSha": "b" * 40,
                "status": "completed",
                "conclusion": "success",
                "createdAt": "2026-07-19T00:00:00Z",
            },
            {
                "databaseId": 2,
                "workflowName": "required-workflow",
                "headSha": SHA,
                "status": "completed",
                "conclusion": "success",
                "createdAt": "2026-07-19T01:00:00Z",
            },
        ]).encode()
        with self.assertRaisesRegex(RuntimeError, "different release SHA"):
            self.module.verify_ci(
                wrong_old_sha, SHA, False, ["required-workflow"])

        workflow_limit = json.dumps([{
            "databaseId": index + 1,
            "workflowName": "required-workflow",
            "headSha": SHA,
            "status": "completed",
            "conclusion": "success",
            "createdAt": "2026-07-19T01:00:00Z",
        } for index in range(100)]).encode()
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            self.module.verify_ci(
                workflow_limit, SHA, False, ["required-workflow"])

        same_second_runs = json.dumps([
            {
                "databaseId": 1,
                "workflowName": "required-workflow",
                "headSha": SHA,
                "status": "completed",
                "conclusion": "success",
                "createdAt": "2026-07-19T01:00:00Z",
            },
            {
                "databaseId": 2,
                "workflowName": "required-workflow",
                "headSha": SHA,
                "status": "completed",
                "conclusion": "failure",
                "createdAt": "2026-07-19T01:00:00Z",
            },
        ]).encode()
        with self.assertRaisesRegex(RuntimeError, "not successful"):
            self.module.verify_ci(
                same_second_runs, SHA, False, ["required-workflow"])

        for schema, release_sha in ((True, SHA), (1, int(SHA, 16))):
            with self.subTest(schema=schema, release_sha=release_sha):
                descriptor = self.descriptor()
                descriptor["schema_version"] = schema
                descriptor["release_sha"] = release_sha
                with self.assertRaisesRegex(RuntimeError, "descriptor schema"):
                    self.module.validate_descriptor(descriptor)

        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            stage = root / "stage"
            descriptor = self.descriptor()
            self.make_stage(stage, descriptor)
            (stage / "writer-inventory.json").write_bytes(b"changed\n")
            manifest_path = stage / "reviewed-assets.sha256"
            lines = manifest_path.read_text(encoding="ascii").splitlines()
            lines = [
                (f"{self.module.digest(stage / 'writer-inventory.json')}  "
                 "writer-inventory.json")
                if line.endswith("  writer-inventory.json") else line
                for line in lines
            ]
            manifest_path.write_text("\n".join(lines) + "\n", encoding="ascii")
            os.chmod(manifest_path, 0o600)
            with self.assertRaisesRegex(RuntimeError, "descriptor mismatch"):
                self.module.validate_stage(stage, descriptor)

    def test_visible_publication_retry_replays_tree_and_parent_fsync(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            driver = root / "driver"
            stage = root / "stage"
            expected = {"deploy.sh": b"reviewed driver\n"}
            descriptor = self.descriptor()
            self.make_driver(driver, expected)
            self.make_stage(stage, descriptor)
            trees = []
            parents = []
            with mock.patch.object(
                    self.module, "fsync_tree",
                    side_effect=lambda path: trees.append(Path(path))), \
                    mock.patch.object(
                        self.module, "fsync_directory",
                        side_effect=lambda path: parents.append(Path(path))):
                self.module.make_published_state_durable(
                    "complete", stage, driver, expected, descriptor)
            self.assertEqual([driver, stage], trees)
            self.assertEqual([driver.parent, stage.parent], parents)

    def test_release_tree_is_verified_without_recursive_repair(self):
        with tempfile.TemporaryDirectory() as root:
            release = Path(root).resolve()
            trading = release / "trading"
            venv = trading / ".venv/bin"
            (release / ".git/objects").mkdir(parents=True)
            venv.mkdir(parents=True)
            source = trading / "main.py"
            source.write_text("pass\n", encoding="utf-8")
            target = venv / "python3"
            target.write_text("python\n", encoding="utf-8")
            os.symlink("python3", venv / "python")
            for directory, _, names in os.walk(release):
                os.chmod(directory, 0o755)
                for name in names:
                    path = Path(directory) / name
                    if not path.is_symlink():
                        os.chmod(path, 0o644)

            with mock.patch.object(
                    self.module.os, "chown",
                    side_effect=AssertionError("verifier must not chown")), \
                    mock.patch.object(
                        self.module.os, "chmod",
                        side_effect=AssertionError("verifier must not chmod")):
                self.module.verify_release_tree(release, trading)

            outside_link = trading / "outside-link"
            os.symlink(source, outside_link)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                self.module.verify_release_tree(release, trading)
            outside_link.unlink()

            second = trading / "hardlink.py"
            os.link(source, second)
            with self.assertRaisesRegex(RuntimeError, "hardlink"):
                self.module.verify_release_tree(release, trading)
            second.unlink()

            os.chmod(source, 0o666)
            with self.assertRaisesRegex(RuntimeError, "immutable"):
                self.module.verify_release_tree(release, trading)
            os.chmod(source, 0o644)

            with mock.patch.object(
                    self.module, "mount_points",
                    return_value=[str(trading)]), \
                    self.assertRaisesRegex(RuntimeError, "mount point"):
                self.module.verify_release_tree(release, trading)


if __name__ == "__main__":
    unittest.main()
