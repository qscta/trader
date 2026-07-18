"""Dynamic tests for deployment journal and atomic prepare publication."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


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
                *self.args(path), "PREPARED", "QUIESCED", ["proof=q2"])
            with self.assertRaisesRegex(self.module.JournalError, "current phase"):
                self.module.advance_journal(
                    *self.args(path), "PREPARED", "QUIESCED", ["proof=q2"])

    def test_gap_and_content_tamper_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.attempt(root)
            self.module.init_journal(*self.args(path))
            os.rename(
                path / "phase-00-prepared.json",
                path / "phase-01-quiesced.json")
            fd = self.module._open_attempt_dir(str(path))
            try:
                with self.assertRaisesRegex(self.module.JournalError, "gap"):
                    self.module._load_chain(fd, SHA, "0001", DRIVER_SHA)
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

    def make_stage(self, path, descriptor):
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
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
            root = Path(root)
            driver = root / "driver"
            stage = root / "stage"
            expected = {"deploy.sh": b"reviewed driver\n"}
            descriptor = {"schema_version": 1, "release_sha": SHA}
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
            root = Path(root)
            driver = root / "driver"
            stage = root / "stage"
            expected = {"deploy.sh": b"reviewed driver\n"}
            descriptor = {"schema_version": 1, "release_sha": SHA}
            self.make_stage(stage, descriptor)
            with self.assertRaisesRegex(RuntimeError, "without"):
                self.module.published_state(stage, driver, expected, descriptor)
            self.make_driver(driver, expected)
            os.chmod(driver / "deploy.sh", 0o755)
            (driver / "deploy.sh").write_bytes(b"tampered\n")
            os.chmod(driver / "deploy.sh", 0o555)
            with self.assertRaisesRegex(RuntimeError, "mismatch"):
                self.module.published_state(stage, driver, expected, descriptor)


if __name__ == "__main__":
    unittest.main()
