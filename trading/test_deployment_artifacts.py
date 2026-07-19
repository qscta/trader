import importlib.util
import json
import os
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

    def test_old_runner_http_uses_strict_json(self):
        response = SimpleNamespace(
            status=200,
            read=lambda _limit: b'{"protocol":"x","protocol":"y"}',
        )
        with mock.patch.dict(os.environ, {
                "TRADING_API_TOKEN": "test-token"}), \
                mock.patch.object(
                    self.old_gate_module.urllib.request, "urlopen",
                    return_value=response):
            with self.assertRaisesRegex(
                    self.old_gate_module.GateError, "duplicate|重复"):
                self.old_gate_module._request("GET", "/test")

        nonfinite = SimpleNamespace(
            status=200, read=lambda _limit: b'{"value":NaN}')
        with mock.patch.dict(os.environ, {
                "TRADING_API_TOKEN": "test-token"}), \
                mock.patch.object(
                    self.old_gate_module.urllib.request, "urlopen",
                    return_value=nonfinite):
            with self.assertRaises(self.old_gate_module.GateError):
                self.old_gate_module._request("GET", "/test")

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
        self.assertEqual(self.deploy.count("EXPECTED_SHA='__RELEASE_SHA__'"), 1)
        self.assertEqual(
            self.emergency.count("EXPECTED_SHA='__RELEASE_SHA__'"), 1)
        self.assertEqual(
            self.recover.count("EXPECTED_SHA='__RELEASE_SHA__'"), 1)
        self.assertNotIn("REPLACE_WITH", self.deploy)
        self.assertNotIn("REPLACE_WITH", self.emergency)

    def test_persistent_block_precedes_first_stop(self):
        block = self.deploy.index('"$EMERGENCY" --install-block-only')
        traps = self.deploy.index("trap 'fail_safe $?' ERR EXIT")
        first_stop = self.deploy.index(
            '"$EMERGENCY" --graceful-stop-and-arm', traps)
        self.assertLess(block, traps)
        self.assertLess(traps, first_stop)
        self.assertIn("START_AUTH='/run/trading-deploy-authorize-start'",
                      self.emergency)
        self.assertIn('"ConditionPathExists=$START_AUTH"', self.emergency)
        self.assertNotIn("touch \"$START_AUTH\"", self.deploy)

    def test_old_runner_boundary_is_proven_before_any_planned_stop(self):
        old_identity = self.deploy.index(
            "OLD_MAIN_PID=$(systemctl show trading.service -p MainPID")
        old_lock = self.deploy.index(
            "flock --nonblock \"$OLD_LOCK\" true", old_identity)
        capability = self.deploy.index(
            "if old_gate_client capability", old_lock)
        runtime_root = self.deploy.index(
            "if ! sudo test -e /var/lib/trading-runtime", capability)
        first_stop = self.deploy.index(
            '"$EMERGENCY" --graceful-stop-and-arm', runtime_root)
        self.assertLess(old_identity, old_lock)
        self.assertLess(old_lock, capability)
        self.assertLess(capability, runtime_root)
        self.assertLess(runtime_root, first_stop)
        self.assertIn("verify-handshake", self.emergency)
        self.assertIn("credential_mode read_only", self.emergency)
        boundary = self.emergency.index(
            'if [[ "$GRACEFUL" -eq 1 ]] && '
            '! verify_planned_no_open_boundary')
        install = self.emergency.index("if ! install_start_block; then", boundary)
        timer_stop = self.emergency.index(
            "sudo systemctl stop trading-state-backup.timer", install)
        self.assertLess(boundary, install)
        self.assertLess(install, timer_stop)

    def test_bootstrap_trade_permission_is_restored_only_under_sentinel(self):
        first_gate_check = self.deploy.index(
            "check_maintenance_http_gate\nif [[ \"$OLD_GATE_MODE\" = "
            "credential_read_only")
        restore = self.deploy.index(
            'credential_mode trade "$CONFIG"', first_gate_check)
        final_stop = self.deploy.index(
            '"$EMERGENCY" --graceful-stop-and-arm', restore)
        seal = self.deploy.index("gate seal", final_stop)
        second_start = self.deploy.index(
            "sudo systemctl start trading.service", seal)
        final_permission = self.deploy.index(
            'credential_mode trade "$CONFIG"', second_start)
        unblock = self.deploy.index('sudo rm -- "$START_BLOCK"', final_permission)
        self.assertLess(first_gate_check, restore)
        self.assertLess(restore, final_stop)
        self.assertLess(final_permission, unblock)
        self.assertIn('.permissions==["read_only","trade"]', self.deploy)
        self.assertIn("禁止 withdraw", self.gate)

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
        drain = self.emergency.index(
            'while [[ -s "/sys/fs/cgroup${cgroup}/cgroup.procs" ]]', kill)
        stop = self.emergency.index('sudo systemctl stop "$unit"', drain)
        self.assertLess(freeze, frozen)
        self.assertLess(frozen, kill)
        self.assertLess(kill, drain)
        self.assertLess(drain, stop)
        self.assertIn("version >= 255", self.emergency)

    def test_completed_slot_and_two_quiescence_windows_precede_t0(self):
        schedule = self.deploy.index('SLOT=$(completed_slot "$SOURCE_DATA")')
        cleanup = self.deploy.index(
            '--config "$SOURCE_DATA/config.json"', schedule)
        migration = self.deploy.index(
            '"$RELEASE_TRADING/migrate_single_strategy.py"', cleanup)
        runtime = self.deploy.index(
            'if ! sudo test -e /var/lib/trading-runtime')
        stop = self.deploy.index('"$EMERGENCY" --graceful-stop-and-arm')
        probes = self.deploy.index("for probe in 1 2")
        q_order = self.deploy.index(
            'quiescence-1.json")" -le', probes)
        phase = self.deploy.index("advance_phase PREPARED QUIESCED", q_order)
        baseline = self.deploy.index("gate baseline", phase)
        self.assertLess(schedule, runtime)
        self.assertLess(schedule, cleanup)
        self.assertLess(cleanup, migration)
        self.assertLess(migration, runtime)
        self.assertLess(runtime, stop)
        self.assertLess(stop, probes)
        self.assertLess(probes, q_order)
        self.assertLess(q_order, phase)
        self.assertLess(phase, baseline)
        self.assertIn('--history-start-ms "$(sudo jq -r .t2_ms', self.deploy)
        self.assertNotIn('waiting safely stopped', self.deploy)

    def test_planned_stops_drain_and_hard_kill_only_fails_closed(self):
        self.assertEqual(
            2, self.deploy.count('"$EMERGENCY" --graceful-stop-and-arm'))
        self.assertEqual(1, self.deploy.count(
            '"$EMERGENCY" --stop-and-arm "$OLD_GATE_MODE" '
            '"$OLD_GATE_EVIDENCE" || true'))
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
            '"$EMERGENCY" --graceful-stop-and-arm', tunnel)
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

    def test_commit_is_unique_last_state_mutation(self):
        seal = self.deploy.index("gate seal")
        second_start = self.deploy.index(
            "sudo systemctl start trading.service", seal)
        unblock = self.deploy.index('sudo rm -- "$START_BLOCK"', second_start)
        reload_ = self.deploy.index("sudo systemctl daemon-reload", unblock)
        ready = self.deploy.index("advance_phase SEALED COMMIT_READY", reload_)
        traps_off = self.deploy.index("trap - ERR EXIT HUP INT TERM", ready)
        commit = self.deploy.index("gate commit", traps_off)
        self.assertLess(seal, second_start)
        self.assertLess(second_start, unblock)
        self.assertLess(unblock, reload_)
        self.assertLess(reload_, ready)
        self.assertLess(ready, traps_off)
        self.assertLess(traps_off, commit)
        tail = self.deploy[commit + len("gate commit"):]
        for forbidden in ("sudo ", "systemctl", "advance_phase", " rm "):
            self.assertNotIn(forbidden, tail)
        self.assertIn("_require_runner_lock_held_elsewhere(lock_fd)", self.gate)
        self.assertIn("LOCK_EX | fcntl.LOCK_NB", self.gate)

    def test_phase_journal_and_recovery_are_single_ordered_protocol(self):
        for phase in ("PREPARED", "QUIESCED", "T0", "VALIDATED",
                      "SEALED", "COMMIT_READY"):
            self.assertIn(f'"{phase}"', self.attempt)
        mark = self.recover.index("journal abandon")
        archive = self.recover.index("gate abandon", mark)
        switch = self.recover.index(
            'sudo mv --no-target-directory "$ACTIVE_TMP" "$ACTIVE"', archive)
        arm = self.recover.index("gate arm", switch)
        self.assertLess(mark, archive)
        self.assertLess(archive, switch)
        self.assertLess(switch, arm)
        self.assertIn("COMMIT_READY", self.recover)
        self.assertIn("--stop-only", self.recover)

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
        self.assertIn('.status!="completed" or .conclusion!="success"',
                      self.deploy)
        for name in ("reviewed-venv.sha256", "reviewed-venv-files.txt",
                     "pip-freeze.txt", "python-version.txt",
                     "package-paths.txt"):
            self.assertIn(name, self.deploy)
        self.assertIn('RUNTIME_ROOT="/var/lib/trading-runtime/$RELEASE_SHA"',
                      self.deploy)
        self.assertIn("sudo chown -R root:root \"$RELEASE_ROOT\"", self.deploy)

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

    def test_emergency_is_self_contained_and_reinstalls_block(self):
        self.assertIn("ACTION=\"${1:---stop-and-arm}\"", self.emergency)
        self.assertIn("--install-block-only", self.emergency)
        self.assertIn("if ! install_start_block; then", self.emergency)
        self.assertLess(
            self.emergency.index("if ! install_start_block; then"),
            self.emergency.index("freeze_kill_stop_unit \"$unit\""))
        self.assertIn("gate_arm", self.emergency)
        for required in (
            "LIVE_TRADING=", "RELEASE_ROOT=", "PYTHON=", "DATA_DIR=",
            "OLD_RUNNER_LOCK_FILE=", "START_BLOCK=",
        ):
            self.assertIn(required, self.emergency)

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
                "public_history_okx_keys_revoked_and_activity_audited": True,
                "public_history_dingtalk_webhooks_rotated": True,
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

    def test_public_history_credential_gate_precedes_runtime_mutation(self):
        exposure = self.deploy.index(
            'credential_exposure "$SOURCE_DATA/config.json"')
        writer_freeze = self.deploy.index(
            'write_request writer_freeze', exposure)
        old_gate = self.deploy.index('OLD_MAIN_PID=$(', writer_freeze)
        runtime = self.deploy.index(
            'if ! sudo test -e /var/lib/trading-runtime', old_gate)
        self.assertLess(exposure, writer_freeze)
        self.assertLess(writer_freeze, old_gate)
        self.assertLess(old_gate, runtime)
        self.assertIn(
            'history_okx_keys_revoked_and_activity_audited=true',
            self.deploy)
        self.assertIn('history_dingtalk_webhooks_rotated=true', self.deploy)

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
