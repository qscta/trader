# MA-only production deployment handoff

Production deployment has one canonical state path:

`PREPARED -> QUIESCED -> T0 -> VALIDATED -> SEALED -> COMMIT_READY -> COMMITTED`

`COMMITTED` is derived, not written after the fact: the root phase journal is
`COMMIT_READY`, the sealed baseline/completion pair is valid, and the sentinel
is absent. The sentinel unlink performed by `gate commit` is the only commit.

## Reviewed artifacts

- `prepare_deployment.py` builds the credential-free stage and installed driver.
- `deploy.sh` is the non-interactive one-way driver.
- `emergency-stop.sh` synchronously blocks and stops every known writer.
- `recover-deployment.sh` abandons one failed attempt and creates a fresh ID.
- `deployment_attempt.py` owns the write-once root phase journal.
- `deployment_no_open_gate.py` owns OKX read-only proof and sentinel evidence.
- `deployment_evidence.py` validates short-lived root approvals and managed files.

Render the release SHA only through `prepare_deployment.py`. Preparation must run
as root against a clean tracked/staged/unstaged worktree. Its invocation must list
every required CI check and workflow by its exact reviewed name; a merely nonempty
set of successful CI records is insufficient. Preparation reads no trading
credential. It constructs stage and driver directories in their respective
protected parents, fsyncs them, publishes driver first and stage second by atomic
rename, and validates an already-published identical pair on retry.

## Safety sequence

1. Before creating a runtime root, installing a start block, or stopping any
   unit, compute the current scheduler slot with the same pure resolver used by
   the runner and require `last_daily_check_date` to equal it. Before the daily
   window or while that slot is incomplete, exit with production untouched.
   Run the migration dry-run and reviewed config-cleanup check against the live
   source at this same pre-stop boundary; repeat all checks after the snapshot.
2. The root approval bound to `writer-inventory.json` must explicitly freeze
   OKX UI/manual order entry, every other API credential consumer, and the
   reviewed host process/unit inventory. During deployment nobody and no other
   program may open, close, amend, or cancel managed SWAP orders. During normal
   runtime, managed instruments may be changed only through the system API.
3. Install and reload a persistent `ConditionPathExists` start block, cut the
   external tunnel, and gracefully drain backup/monitor/trading units. Gunicorn
   stops accepting requests and waits for the scheduler/active trade thread.
   A timeout or failed drain is hard-contained with cgroup freeze/SIGKILL but
   returns failure and enters recovery; it never continues into migration.
4. Arm the release sentinel and take stable inactive/PID/cgroup/lock samples.
   Run two complete Q0-to-Q2 read-only visibility probes. The second Q0 must not
   precede the first Q2. Baseline then proves history continuously from the
   second Q2 through authoritative T0, takes its snapshot after T0, and persists
   the T0 evidence. There is no Q2-to-T0 or snapshot-to-T0 blind interval.
5. Keep the sentinel active while the exact formal service, memory monitor,
   explicit backup (including its formal-service restart), backup timer, and
   external tunnel are exercised. Every HTTP write returns 503; authenticated
   GET status remains available. The runner's formal and manual daily-check
   entry returns before any side effect while the sentinel exists; health and
   guardian duties remain live. External approval binds the sentinel, baseline,
   local health, nonce, config and exact environment hashes.
6. Gracefully drain the whole stack again. With the runner lock proven free,
   require migration dry-run and the same completed slot again, run the sole
   final `verify`, recheck reviewed code/venv/CI/writer approval, and `seal`.
   Seal writes completion but retains the sentinel.
7. Restart the already-validated formal stack under the sentinel. Prove the exact
   unit is active/running, its MainPID is in its cgroup, its working directory is
   the immutable release, and that PID holds the exact runner-lock inode.
8. While the sentinel still exists, remove both start block and temporary `/run`
   authorization, daemon-reload, and repeat health, HTTP-gate, identity, and hash
   checks. Write `COMMIT_READY`, disable fail-safe traps, then execute `gate commit`.
   No fallible filesystem, service, evidence, health, or network action follows.

Runtime JSON, locks, sentinel, baseline and completion live under
`/var/lib/trading-runtime/<sha>`. Reviewed code and its complete virtual
environment live under `/opt/trader-releases/<sha>` and are root-owned,
non-group/world-writable through every resolved interpreter target and ancestor.
`trading.log` also lives in the runtime root so rotation never needs to write the
release tree. The runtime identity can write only runtime data; config is
inaccessible to the memory monitor.

`/etc/trading.env` has an exact key allowlist and is hash-bound/rechecked.
Every Python boundary uses `-B -E`; direct tools use `env -i`, while formal,
gate, health and monitor units reset inherited environment sources and remove
all loader, Python, proxy and TLS override variables. Effective unit properties
must prove the exact executable, environment files, empty pre/post hooks and
the reviewed `UnsetEnvironment` set before a service can start.

OKX net positions do not provide a reliable identity that distinguishes an
external same-side/same-size replacement. The no-open proof therefore depends
on the single-writer boundary above and must not be described as attributing
such a replacement to a particular actor.

## Failure and same-SHA retry

Never restart `deploy.sh` for the same attempt ID. Before commit, any ordinary
failure invokes the emergency boundary and leaves the system stopped, persistently
blocked and sentinel-protected. Run the rendered `recover-deployment.sh` instead.

Recovery first refuses `COMMIT_READY + missing sentinel` and any ambiguous
sentinel-missing durable evidence, so it cannot reinterpret a committed release as
failed. It then writes `abandoned.json` in the old root attempt journal, completes
the gate's journal-first evidence archive (retryable after interruption), atomically
switches `active-attempt` to a fresh four-digit ID, fsyncs it, and arms a fresh
sentinel. Human approvals and reports are attempt-local and cannot be reused.

Deployment is authorized only after the required three full adversarial reviews,
with the final two consecutive reviews reporting no issue, plus the final strongest
model judgment. The live API key may remain configured because every stage before
the unique commit is mechanically no-open and all HTTP writes are denied.
