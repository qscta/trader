# MA-only production deployment handoff

Production deployment has one canonical state path:

`PREPARED -> G0 -> RUNTIME_READY -> QUIESCED -> T0 -> VALIDATED -> SEALED -> COMMIT_READY -> COMMITTED`

`COMMITTED` is derived, not written after the fact: the root phase journal is
`COMMIT_READY`, the sealed baseline/completion pair is valid, and the sentinel
is absent. The sentinel unlink performed by `gate commit` is the only commit.

## Reviewed artifacts

- `prepare_deployment.py` builds the credential-free stage and installed driver.
- `deploy.sh` is the non-interactive one-way driver.
- `emergency-stop.sh` synchronously blocks and stops every known writer.
- `recover-deployment.sh` abandons one failed attempt and creates a fresh ID.
- `deployment_attempt.py` owns the write-once root phase journal.
- `deployment_no_open_gate.py` owns read-only exchange snapshots and release
  sentinel evidence.
- `deployment_old_runner_gate.py` binds the old runner's same-trade-lock
  sentinel handshake to the actual kernel-reported FLOCK holder.
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
All rendered deploy and recovery drivers, regardless of release SHA, run in the
same fixed transient operation unit and hold the same kernel `flock` for their
complete lifetime before touching attempt state. A public emergency writes a
boot-scoped, fsynced request marker, contains that unit and its bound helper
cgroup, and only then takes the lease; an incomplete same-boot emergency leaves
its marker fail-closed. Reboot safety after G0 comes from the old runner's
fsynced runtime sentinel, then the persistent start block and release
sentinel—not from `/run` or an API permission change.

If the primary `00-deploy-closed.conf` is present but damaged, hard emergency
containment preserves it as evidence and atomically installs
`zzzz-deploy-emergency-closed.conf`. That fallback clears prior systemd
conditions and installs the permanently false `ConditionPathExists=!/`; its
loaded D-Bus condition tuple and an empty writer cgroup must both be proven.
Deploy and recovery then contain the writer and refuse to advance any attempt.
Repair is deliberately manual: preserve the damaged primary, atomically restore
and reload the exact normal primary block, prove it loaded, and only then remove
the fallback, fsync the systemd directories and reload again. No driver removes
the fallback automatically.

The reviewed workflow is `.github/workflows/tests.yml`, whose workflow name is
`tests`.  Its required check-run names are:

- `stdlib tests (no deps) (3.10)`
- `stdlib tests (no deps) (3.11)`
- `stdlib tests (no deps) (3.12)`
- `stdlib tests (no deps) (3.13)`
- `dependency tests (flask/pandas/ccxt)`
- `frontend syntax (app.js)`

After the frozen release SHA has those exact successful checks, collect and
prepare the immutable stage with the following canonical command shape. The
release root, including `.git` and the reviewed `.venv`, must be a newly created,
independent checkout already owned by `root:root`, free of mounts and hardlinks,
and not group/world writable. Preparation only verifies that boundary; it never
recursively repairs a mutable checkout. Every untracked or Git-ignored release
entry is rejected except `trading/.venv/**`, whose complete file/symlink set is
hash-bound separately; this also prevents ignored bytecode or `config.json`
credentials from executing outside the reviewed manifest. `deploy.sh` repeats
that closure proof before its first release-Python execution. The protected input directory must
contain the three files described in `deployment_templates/README.md`.

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
3. Before the first deployment mutation, prove that the real worker actually
   holding `runner.lock` exposes `trade-lock-no-open-v1` on loopback. Install the
   root-owned, fsynced, write-once `arm-intent`, then call the authenticated
   endpoint. The endpoint first acquires the same in-process trade lock used by
   every open path, thereby draining admitted operations. While still holding
   that lock it creates and fsyncs the runner's actual sentinel, verifies the
   engine gate, and only then releases the lock. Publish and
   revalidate the returned root evidence, and only then install fail-safe traps
   and the persistent `ConditionPathExists` start block. The response worker
   must remain the same kernel FLOCK owner across probe, arm, and revalidation.
   Deployment never asks the operator to change the live Key's permissions; an
   incompatible runner fails before the intent, sentinel, or start block.
4. With the same-lock sentinel proven, cut the external tunnel and gracefully
   drain backup/monitor/trading units. Gunicorn
   stops accepting requests and waits for the scheduler/active trade thread.
   A timeout or failed drain is hard-contained with cgroup freeze/SIGKILL but
   returns failure and enters recovery; it never continues into migration.
5. Arm the release sentinel, take stable inactive/PID/cgroup/lock samples, fsync
   the complete migrated runtime tree, and bind its stable payload digest in
   `RUNTIME_READY`. The current attempt's sentinel, later baseline/completion
   and abandon audit/archive are validated by their own gate state machine and
   excluded from that attempt's payload digest; prior attempt evidence remains
   hashed. Run two complete
   Q0-to-Q2 read-only visibility probes. The
   second Q0 must not precede the first Q2. Baseline then proves history
   continuously from the second Q2 through authoritative T0, takes its snapshot
   after T0, and persists the T0 evidence. There is no Q2-to-T0 or
   snapshot-to-T0 blind interval. Successful migration originals, plus the
   retired `data/okx` snapshot and its completion marker, are moved out of the
   active runtime into the existing root-only rollback directory only after the
   migration journal is absent and file digests are preserved. The stopped
   source snapshot remains in `current-data.tar`; MA runtime never consumes
   these retired bytes.
6. Keep the sentinel active while the exact formal service, memory monitor,
   explicit backup (including its formal-service restart), backup timer, and
   external tunnel are exercised. Every HTTP write returns 503; authenticated
   GET status remains available. The runner's formal and manual daily-check
   entry returns before any side effect while the sentinel exists; health and
   guardian duties remain live. External approval binds the sentinel, baseline,
   local health, nonce, config and exact environment hashes.
7. Gracefully drain the whole stack again. With the runner lock proven free,
   require migration dry-run and the same completed slot again, run the sole
   final `verify`, recheck reviewed code/venv/CI/writer approval, and `seal`.
   Seal writes completion but retains the sentinel.
8. Restart the already-validated formal stack under the sentinel. Prove the exact
   unit is active/running, its MainPID is in its cgroup, its working directory is
   the immutable release, and the actual worker in that cgroup holds the exact
   runner-lock inode.
9. While the sentinel still exists, remove both start block and temporary `/run`
   authorization, daemon-reload, and settle any backup catch-up. Then stop and
   prove inactive both the backup timer and its worker; a future timer timestamp
   is not used as an exclusion lock because the backup is allowed to restart the
   formal service. Stop the tunnel, obtain a final authenticated same-trade-lock
   drain, then freeze the exact healthy worker. `gate commit` re-evaluates the
   current scheduler slot inside the unique unlink operation and remains covered
   by a boundary-aware trap: pre-unlink failure preserves/rearms the sentinel;
   post-unlink failure preserves committed absence and installs stop-only
   containment. After unlink, the exact runner remains frozen and backup stays
   inactive while the monitor, tunnel and scheduler slot are re-proved. Final
   thaw is the sole post-commit live/open boundary; only afterward is the normal
   backup timer restored and runner health re-proved under the same failure trap.
   Recovery first quiesces the backup restart trigger, before even stopping the
   tunnel, then applies the same freeze/thaw/restore ordering. Both deployment
   and recovery reject a queued `trading.service` job as an unstable runner
   proof, and contain every failed committed proof with `stop-only ->
   verify-committed-stopped -> absent-sentinel proof`.

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

Never restart `deploy.sh` for the same attempt ID. Before the runtime boundary is
published, a failure either leaves production untouched or leaves the newly
fsynced sentinel in place; it does not install a start block first. After that
boundary, ordinary failures invoke the emergency path and leave the system
stopped, persistently blocked and sentinel-protected. Run the rendered
`recover-deployment.sh` instead.

Recovery recognizes `COMMIT_READY + missing sentinel` only after validating the
durable baseline/completion pair and exact runner lock; any other missing-sentinel
evidence remains damaged and fail-closed. A PREPARED failure with no G0,
`arm-intent`, boundary, or start-block evidence is metadata-only recovery:
production remains active and no block, stop, or sentinel is introduced. A
write-once `arm-intent` means the crash may straddle sentinel creation or the
response. The v1 sentinel inode is created only after the old runner acquires
its trade lock, so a lost response—or a crash that leaves its JSON incomplete—
does not erase the drain proof. Recovery binds a complete sentinel, or a partial
ubuntu-owned v1 inode plus the root intent, to the current FLOCK worker and HTTP
gate without inventing creator fields. Before accepting a lost-response or
partial object, recovery reopens the exact same inode without following links,
fsyncs it and its canonical parent directory, and rechecks stable metadata; it
does not infer crash durability merely from visibility. If the service died
before creating any sentinel, recovery creates a release sentinel while holding the exact runner
FLOCK and then proves the service stably inactive. Both paths avoid systemd
mutation until a durable gate plus an in-flight-drain or empty-cgroup proof is
present. Recovery never fabricates a lost creator response as
new evidence. Only after that proof may it hard-contain the old writer and
normalize to the inactive release boundary. At or after G0,
recovery must revalidate the durable
runtime-sentinel/release-sentinel boundary before stopping an active writer. It then
abandons the old journal and gate evidence, writes a fresh inactive recovery
seed, arms and verifies the new sentinel plus stopped/blocked/lock-free state,
and only then atomically switches `active-attempt`. A retry consumes that seed
instead of requiring a fictional active old runner. Human approvals and reports
remain attempt-local.

A public pre-G0 hard emergency also takes the exact external source
`runner.lock` before publishing a new write-once source contract and retains that
lease until exit. Failure to acquire or revalidate the lease blocks publication;
an end-of-run free-lock probe is not treated as a substitute.

Deployment is authorized only after the required three full adversarial reviews,
with the final two consecutive reviews reporting no issue, plus the final strongest
model judgment. Deployment does not require or perform any live Key permission
change. The kernel-reported legacy FLOCK owner must instead complete and retain
the same-trade-lock runtime-sentinel handshake.
Every stage after G0 and before the unique commit is mechanically no-open, and
all HTTP writes are denied while the reviewed release is exercised.
