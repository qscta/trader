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
- `deployment_old_runner_gate.py` proves the old process's same-lock handoff.
- `deployment_evidence.py` validates short-lived root approvals and managed files.
- `remove-one-confirmed-config-key.py` is the only executable config cleanup;
  host input supplies hashes and a path, never code.

Render the release SHA only through `prepare_deployment.py`. Preparation must run
as root against a clean tracked/staged/unstaged worktree. Its invocation must list
every required CI check and workflow by its exact reviewed name; a merely nonempty
set of successful CI records is insufficient. Preparation reads no trading
credential. It constructs stage and driver directories in their respective
protected parents, fsyncs them, publishes driver first and stage second by atomic
rename, and validates an already-published identical pair on retry.

The reviewed workflow is `.github/workflows/tests.yml`, whose workflow name is
`tests`.  Its required check-run names are:

- `stdlib tests (no deps) (3.10)`
- `stdlib tests (no deps) (3.11)`
- `stdlib tests (no deps) (3.12)`
- `stdlib tests (no deps) (3.13)`
- `dependency tests (flask/pandas/ccxt)`
- `frontend syntax (app.js)`

After the frozen release SHA has those exact successful checks, collect and
prepare the immutable stage with the following canonical command shape.  The
release root must already be a clean checkout at that SHA with its reviewed
`.venv`; the protected input directory must contain the three files described
in `deployment_templates/README.md`.

```bash
RELEASE_SHA='<40-hex-frozen-sha>'
RELEASE_ROOT="/opt/trader-releases/$RELEASE_SHA"
INPUT_DIR="/var/lib/trading-review-inputs/$RELEASE_SHA"
CHECKS_JSON="/tmp/trading-check-runs-$RELEASE_SHA.json"
RUNS_JSON="/tmp/trading-workflow-runs-$RELEASE_SHA.json"

gh api -H 'Accept: application/vnd.github+json' \
  "/repos/qscta/trader/commits/$RELEASE_SHA/check-runs?per_page=100" \
  >"$CHECKS_JSON"
gh run list --repo qscta/trader --commit "$RELEASE_SHA" --limit 100 \
  --json databaseId,workflowName,headSha,status,conclusion,createdAt \
  >"$RUNS_JSON"

sudo /usr/bin/python3 -I -B "$RELEASE_ROOT/trading/prepare_deployment.py" \
  --release-sha "$RELEASE_SHA" --release-root "$RELEASE_ROOT" \
  --stage "/var/lib/trading-deploy/$RELEASE_SHA" \
  --driver-dir "/usr/local/lib/trading-deploy/$RELEASE_SHA" \
  --ci-checks "$CHECKS_JSON" --ci-runs "$RUNS_JSON" \
  --reviewed-input "$INPUT_DIR" \
  --required-workflow 'tests' \
  --required-check 'stdlib tests (no deps) (3.10)' \
  --required-check 'stdlib tests (no deps) (3.11)' \
  --required-check 'stdlib tests (no deps) (3.12)' \
  --required-check 'stdlib tests (no deps) (3.13)' \
  --required-check 'dependency tests (flask/pandas/ccxt)' \
  --required-check 'frontend syntax (app.js)'
```

## Public-history credential incident gate

Commit `38ac63646d2e18ba9d238856b124594b4691f252` and its archived state
snapshots exposed two OKX API Key identities and two DingTalk webhook
identities. The current tree retains only one-way SHA-256 commitments. Before
creating a valid `writer-inventory.json`, an operator must revoke both exposed
OKX Keys, audit the live account's orders/fills/API-Key activity for the public
window, rotate both webhook identities, and record the concrete review subjects
in the inventory. Setting the two history-remediation booleans without doing
that work is a false production attestation.

`deploy.sh` independently hashes the effective current OKX Key and DingTalk
webhook (environment overrides included) before any runtime mutation. An exact
match with either exposed identity blocks deployment without printing a value
or fingerprint. This mechanical mismatch check complements—but cannot replace—
the human proof that the old identities were actually revoked and audited.

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
3. Before the first stop, an already-upgraded runner must create and fsync its
   actual sentinel while holding the same `_trade_lock` as every open path;
   the driver rechecks its inode, worker cgroup, and HTTP 503.  A legacy runner
   without that endpoint may proceed only after OKX reports this exact live Key
   as `read_only` and the emergency driver repeats that proof immediately
   before stopping.  Missing or ambiguous proof leaves production running.
4. Install and reload a persistent `ConditionPathExists` start block, cut the
   external tunnel, and gracefully drain backup/monitor/trading units. Gunicorn
   stops accepting requests and waits for the scheduler/active trade thread.
   A timeout or failed drain is hard-contained with cgroup freeze/SIGKILL but
   returns failure and enters recovery; it never continues into migration.
5. Arm the release sentinel and take stable inactive/PID/cgroup/lock samples.
   Run two complete Q0-to-Q2 read-only visibility probes. The second Q0 must not
   precede the first Q2. Baseline then proves history continuously from the
   second Q2 through authoritative T0, takes its snapshot after T0, and persists
   the T0 evidence. There is no Q2-to-T0 or snapshot-to-T0 blind interval.
6. Keep the sentinel active while the exact formal service, memory monitor,
   explicit backup (including its formal-service restart), backup timer, and
   external tunnel are exercised. Every HTTP write returns 503; authenticated
   GET status remains available. The runner's formal and manual daily-check
   entry returns before any side effect while the sentinel exists; health and
   guardian duties remain live. External approval binds the sentinel, baseline,
   local health, nonce, config and exact environment hashes.
7. For a legacy read-only bootstrap only, the driver now requests a bound human
   approval to restore this same Key to exactly `read_only,trade`; `withdraw`
   is forbidden.  The reviewed release is already running behind its durable
   sentinel, all HTTP writes still return 503, and the permission is queried
   again before commit.  Then gracefully drain the whole stack again. With the
   runner lock proven free,
   require migration dry-run and the same completed slot again, run the sole
   final `verify`, recheck reviewed code/venv/CI/writer approval, and `seal`.
   Seal writes completion but retains the sentinel.
8. Restart the already-validated formal stack under the sentinel. Prove the exact
   unit is active/running, its MainPID is in its cgroup, its working directory is
   the immutable release, and that PID holds the exact runner-lock inode.
9. While the sentinel still exists, remove both start block and temporary `/run`
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
