#!/usr/bin/env -S -i PATH=/usr/sbin:/usr/bin:/sbin:/bin TRADING_STERILE_DRIVER=1 /bin/bash --noprofile --norc
# Fail-closed same-SHA abandon/reset. Render once and install root:root 0555.
if [[ ${TRADING_STERILE_DRIVER:-} != 1 ]]; then
  exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    TRADING_STERILE_DRIVER=1 /bin/bash --noprofile --norc "$0" "$@"
fi
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
readonly PATH
IFS=$' \t\n'
umask 077
set -Eeuo pipefail

readonly GLOBAL_DEPLOY_LOCK_DIR='/run/trading-deploy-control'
readonly GLOBAL_DEPLOY_LOCK="$GLOBAL_DEPLOY_LOCK_DIR/operation.lock"
readonly GLOBAL_DEPLOY_UNIT='trading-deployment-operation.service'
readonly GLOBAL_DEPLOY_HELPER='trading-deployment-operation-helper.service'
if [[ $EUID -ne 0 ]]; then
  exec sudo /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    TRADING_STERILE_DRIVER=1 /bin/bash --noprofile --norc "$0" "$@"
fi
if [[ ${TRADING_DEPLOY_SCOPE:-} != 1 ]]; then
  exec systemd-run --quiet --wait --collect --pipe \
    --unit="$GLOBAL_DEPLOY_UNIT" --service-type=exec \
    --property=KillMode=control-group --property=TimeoutStopSec=30s \
    /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    TRADING_STERILE_DRIVER=1 TRADING_DEPLOY_SCOPE=1 \
    /bin/bash --noprofile --norc "$0" "$@"
fi
export TRADING_DEPLOY_SCOPE=1
test "$(systemctl show "$GLOBAL_DEPLOY_UNIT" -p MainPID --value)" = "$$"
test "$(systemctl show "$GLOBAL_DEPLOY_UNIT" -p Transient --value)" = yes
test "$(systemctl show "$GLOBAL_DEPLOY_UNIT" -p KillMode --value)" = control-group
if test -e "$GLOBAL_DEPLOY_LOCK_DIR" || test -L "$GLOBAL_DEPLOY_LOCK_DIR"; then
  test -d "$GLOBAL_DEPLOY_LOCK_DIR"
  test ! -L "$GLOBAL_DEPLOY_LOCK_DIR"
  test "$(stat -c '%U:%G:%a' "$GLOBAL_DEPLOY_LOCK_DIR")" = root:root:700
else
  install -d -o root -g root -m 0700 "$GLOBAL_DEPLOY_LOCK_DIR"
fi
if ! test -e "$GLOBAL_DEPLOY_LOCK" && ! test -L "$GLOBAL_DEPLOY_LOCK"; then
  lock_tmp=$(mktemp "$GLOBAL_DEPLOY_LOCK_DIR/.operation.lock.XXXXXX")
  chmod 0600 "$lock_tmp"
  mv --no-clobber --no-target-directory "$lock_tmp" "$GLOBAL_DEPLOY_LOCK"
  test ! -e "$lock_tmp" || rm -f -- "$lock_tmp"
fi
test -f "$GLOBAL_DEPLOY_LOCK"
test ! -L "$GLOBAL_DEPLOY_LOCK"
test "$(stat -c '%U:%G:%a:%h:%s' "$GLOBAL_DEPLOY_LOCK")" = root:root:600:1:0
test -z "$(find "$GLOBAL_DEPLOY_LOCK_DIR" -maxdepth 1 \
  -name 'emergency.request.*' -print -quit)"
exec 9<>"$GLOBAL_DEPLOY_LOCK"
flock --exclusive --nonblock 9
test -z "$(find "$GLOBAL_DEPLOY_LOCK_DIR" -maxdepth 1 \
  -name 'emergency.request.*' -print -quit)"
export TRADING_DEPLOY_LOCK_HELD=1 TRADING_DEPLOY_LOCK_FD=9
EXPECTED_SHA='__RELEASE_SHA__'
readonly RELEASE_SHA="$EXPECTED_SHA"
readonly DRIVER_DIR="/usr/local/lib/trading-deploy/$RELEASE_SHA"
readonly DEPLOY="$DRIVER_DIR/deploy.sh"
readonly EMERGENCY="$DRIVER_DIR/emergency-stop.sh"
readonly JOURNAL="$DRIVER_DIR/deployment_attempt.py"
readonly OLD_GATE="$DRIVER_DIR/deployment_old_runner_gate.py"
readonly EVIDENCE_PYTHON='/usr/bin/python3'
readonly RUNTIME="/var/lib/trading-runtime/$RELEASE_SHA"
readonly RELEASE_TRADING="/opt/trader-releases/$RELEASE_SHA/trading"
readonly PYTHON="$RELEASE_TRADING/.venv/bin/python"
readonly GATE="$RELEASE_TRADING/deployment_no_open_gate.py"
readonly MIGRATE="$RELEASE_TRADING/migrate_single_strategy.py"
readonly RUNNER_LOCK="$RUNTIME/.runtime/runner.lock"
readonly STAGE="/var/lib/trading-deploy/$RELEASE_SHA"
readonly CLEANUP="$STAGE/remove-one-confirmed-config-key.py"
readonly SPEC="$STAGE/remove-one-confirmed-config-key.spec.json"
readonly ACTIVE="$STAGE/active-attempt"
readonly SENTINEL="$RUNTIME/.maintenance_no_open"
readonly BASELINE="$RUNTIME/deployment_no_open_baseline.json"
readonly COMPLETION="$RUNTIME/deployment_no_open_completion.json"
readonly START_BLOCK='/etc/systemd/system/trading.service.d/00-deploy-closed.conf'
readonly EMERGENCY_START_BLOCK='/etc/systemd/system/trading.service.d/zzzz-deploy-emergency-closed.conf'
readonly START_AUTH='/run/trading-deploy-authorize-start'
readonly UNSET_ENV='LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT LD_DEBUG LD_DEBUG_OUTPUT LD_PROFILE GUNICORN_CMD_ARGS PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT PYTHONUSERBASE PYTHONWARNINGS PYTHONBREAKPOINT PYTHONPYCACHEPREFIX PYTHONPLATLIBDIR PYTHONEXECUTABLE PYTHONCASEOK PYTHONHTTPSVERIFY HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE AWS_CA_BUNDLE OPENSSL_CONF OPENSSL_MODULES SSLKEYLOGFILE GRPC_DEFAULT_SSL_ROOTS_FILE_PATH'
RECOVERY_SOURCE_LOCK_FD=''

die() { printf 'recover refused: %s\n' "$*" >&2; exit 1; }
sha256() { sha256sum -- "$1" | awk '{print $1}'; }
cgroup_populated() {
  local cgroup=$1 path events value
  if [[ -z "$cgroup" ]]; then
    printf '0\n'
    return 0
  fi
  [[ "$cgroup" = /* && "$cgroup" != *$'\n'* ]] || return 1
  path="/sys/fs/cgroup${cgroup}"
  events="$path/cgroup.events"
  if [[ ! -e "$path" ]]; then
    printf '0\n'
    return 0
  fi
  [[ -d "$path" && ! -L "$path" ]] || return 1
  if [[ ! -e "$events" ]]; then
    [[ ! -e "$path" ]] && { printf '0\n'; return 0; }
    return 1
  fi
  [[ -f "$events" && ! -L "$events" ]] || return 1
  value=$(awk '$1=="populated" {print $2}' "$events") || return 1
  [[ "$value" = 0 || "$value" = 1 ]] || return 1
  printf '%s\n' "$value"
}
cgroup_is_empty() {
  local populated
  populated=$(cgroup_populated "$1") || return 1
  [[ "$populated" = 0 ]]
}
unit_job_absent() {
  local unit=$1 jobs
  jobs=$(systemctl list-jobs --no-legend --no-pager) || return 1
  ! grep -Eq "^[[:space:]]*[0-9]+[[:space:]]+$unit[[:space:]]" \
    <<<"$jobs"
}
prove_unit_inactive() {
  local unit=$1 cgroup
  test "$(systemctl show "$unit" -p ActiveState --value)" = inactive || \
    return 1
  if [[ "$unit" = *.timer ]]; then
    unit_job_absent "$unit" || return 1
    return 0
  fi
  test "$(systemctl show "$unit" -p MainPID --value)" = 0 || return 1
  cgroup=$(systemctl show "$unit" -p ControlGroup --value) || return 1
  test "$(cgroup_populated "$cgroup")" = 0 || return 1
  unit_job_absent "$unit" || return 1
  return 0
}
prove_running_aux_unit() {
  local unit=$1 pid cgroup
  test "$(systemctl show "$unit" -p ActiveState --value)" = active || \
    return 1
  test "$(systemctl show "$unit" -p SubState --value)" = running || \
    return 1
  pid=$(systemctl show "$unit" -p MainPID --value) || return 1
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  cgroup=$(systemctl show "$unit" -p ControlGroup --value) || return 1
  test "$(cgroup_populated "$cgroup")" = 1 || return 1
  unit_job_absent "$unit" || return 1
  return 0
}
backup_timer_stable_sample() {
  local timer_state timer_sub service_state invocation result jobs next next_epoch
  timer_state=$(systemctl show trading-state-backup.timer \
    -p ActiveState --value) || return 1
  timer_sub=$(systemctl show trading-state-backup.timer \
    -p SubState --value) || return 1
  service_state=$(systemctl show trading-state-backup.service \
    -p ActiveState --value) || return 1
  invocation=$(systemctl show trading-state-backup.service \
    -p InvocationID --value) || return 1
  result=$(systemctl show trading-state-backup.service \
    -p Result --value) || return 1
  next=$(systemctl show trading-state-backup.timer \
    -p NextElapseUSecRealtime --value) || return 1
  next_epoch=$(date --date="$next" +%s) || return 1
  (( next_epoch >= $(date +%s) + 300 )) || return 1
  jobs=$(systemctl list-jobs --no-legend --no-pager) || return 1
  test "$timer_state:$timer_sub:$service_state:$result" = \
    active:waiting:inactive:success || return 1
  [[ "$invocation" =~ ^[0-9a-f]{32}$ ]] || return 1
  ! grep -Eq \
    '^[[:space:]]*[0-9]+[[:space:]]+trading-state-backup\.(service|timer)[[:space:]]' \
    <<<"$jobs" || return 1
  test "$(systemctl show trading-state-backup.service \
    -p ActiveState --value)" = "$service_state" || return 1
  test "$(systemctl show trading-state-backup.service \
    -p InvocationID --value)" = "$invocation" || return 1
  test "$(systemctl show trading-state-backup.service \
    -p Result --value)" = "$result" || return 1
  test "$(systemctl show trading-state-backup.timer \
    -p ActiveState --value)" = "$timer_state" || return 1
  test "$(systemctl show trading-state-backup.timer \
    -p SubState --value)" = "$timer_sub" || return 1
  test "$(systemctl show trading-state-backup.timer \
    -p NextElapseUSecRealtime --value)" = "$next" || return 1
  printf '%s:%s\n' "$invocation" "$next_epoch"
}

settle_backup_timer() {
  local sample previous='' deadline=$((SECONDS + 300)) stable=0
  sudo systemctl start trading-state-backup.timer || return 1
  while (( SECONDS < deadline )); do
    if sample=$(backup_timer_stable_sample); then
      if [[ "$sample" = "$previous" ]]; then
        stable=$((stable + 1))
      else
        previous=$sample
        stable=1
      fi
      [[ "$stable" -ge 2 ]] && return 0
    else
      previous=''
      stable=0
    fi
    sleep 1
  done
  return 1
}

quiesce_backup_timer() {
  sudo systemctl stop trading-state-backup.timer \
    trading-state-backup.service || return 1
  prove_unit_inactive trading-state-backup.timer || return 1
  prove_unit_inactive trading-state-backup.service || return 1
  return 0
}
run_emergency() {
  TRADING_EMERGENCY_INTERNAL=1 \
    /bin/bash --noprofile --norc "$EMERGENCY" "$@"
}
wait_helper_absent() {
  local deadline=$((SECONDS + 30)) load
  while :; do
    load=$(systemctl show "$GLOBAL_DEPLOY_HELPER" \
      -p LoadState --value 2>/dev/null) || return 1
    [[ "$load" = not-found ]] && return 0
    (( SECONDS < deadline )) || return 1
    sleep 1
  done
}
run_helper() {
  local rc
  wait_helper_absent || return 1
  if sudo systemd-run --quiet --wait --collect --pipe \
      --unit="$GLOBAL_DEPLOY_HELPER" --service-type=exec \
      --property="BindsTo=$GLOBAL_DEPLOY_UNIT" \
      --property="After=$GLOBAL_DEPLOY_UNIT" \
      --property=KillMode=control-group "$@"; then
    rc=0
  else
    rc=$?
  fi
  wait_helper_absent || return 1
  return "$rc"
}
run_release_helper() {
  run_helper \
    --uid=ubuntu --gid=ubuntu \
    --property="WorkingDirectory=$RELEASE_TRADING" \
    --property=EnvironmentFile=/etc/trading.env \
    --property="UnsetEnvironment=$UNSET_ENV" \
    "$@"
}
gate() {
  sudo -u ubuntu env -i PATH=/usr/bin:/bin \
    "TRADING_RUNNER_LOCK_FILE=$RUNNER_LOCK" \
    "$PYTHON" -B -E "$GATE" "$@"
}
check_committed_health() {
  run_release_helper \
    "$PYTHON" -B -E -c \
    'import os,urllib.request; from trade_state import load_strict_json; q=urllib.request.Request("http://127.0.0.1:5000/api/status",headers={"X-API-Token":os.environ["TRADING_API_TOKEN"]}); r=urllib.request.urlopen(q,timeout=15); d=load_strict_json(r); blockers=d.get("health",{}).get("safety_blockers"); ok=(r.status==200 and d.get("status")=="running" and d.get("health",{}).get("healthy") is True and blockers=={"open_intents":0,"close_intents":0,"position_quarantines":0,"stop_residues":0} and d.get("open_intents_count")==0 and d.get("stop_residues")==[] and d.get("stop_anomalies")=={} and d.get("position_quarantines")=={}); raise SystemExit(0 if ok else 2)'
}
safe_resume_slot() {
  local expected_slot=$1
  gate resume-slot --config "$RUNTIME/config.json" --data-dir "$RUNTIME" \
    --expected-slot "$expected_slot" >/dev/null
}
journal() {
  sudo /usr/bin/python3 -I -B "$JOURNAL" "$@" \
    --attempt-dir "$ATTEMPT_DIR" --release-sha "$RELEASE_SHA" \
    --attempt-id "$CURRENT" --driver-sha256 "$DRIVER_SHA"
}
validate_external_recovery_source_lock() {
  local contract=$1 source_data source_lock expected_identity fd_identity
  source_data=$(jq -r .source_data <<<"$contract")
  if [[ "$source_data" = "$RUNTIME" ]]; then
    [[ -z "$RECOVERY_SOURCE_LOCK_FD" ]]
    return
  fi
  [[ "$RECOVERY_SOURCE_LOCK_FD" =~ ^[0-9]+$ ]]
  source_lock=$(jq -r .runner_lock.path <<<"$contract")
  test "$source_lock" = "$source_data/.runtime/runner.lock"
  expected_identity=$(jq -r \
    '.runner_lock | "\(.dev):\(.ino)"' <<<"$contract")
  [[ "$expected_identity" =~ ^[0-9]+:[0-9]+$ ]]
  test "$(journal source-contract)" = "$contract"
  test -e "/proc/$$/fd/$RECOVERY_SOURCE_LOCK_FD"
  flock --exclusive --nonblock "$RECOVERY_SOURCE_LOCK_FD"
  test -f "$source_lock"
  test ! -L "$source_lock"
  test "$(realpath -e -- "$source_lock")" = "$source_lock"
  test "$(stat -c '%U:%G:%a:%h' "$source_lock")" = \
    ubuntu:ubuntu:600:1
  fd_identity=$(stat -Lc '%d:%i' \
    "/proc/$$/fd/$RECOVERY_SOURCE_LOCK_FD")
  test "$fd_identity" = "$expected_identity"
  test "$(stat -c '%d:%i' "$source_lock")" = "$expected_identity"
  test "$(journal source-contract)" = "$contract"
}
hold_external_recovery_source_lock() {
  local contract=$1 source_data source_lock expected_identity
  source_data=$(jq -r .source_data <<<"$contract")
  [[ "$source_data" != "$RUNTIME" ]] || return 0
  [[ -z "$RECOVERY_SOURCE_LOCK_FD" ]] || \
    die 'external recovery source lease was already initialized'
  source_lock=$(jq -r .runner_lock.path <<<"$contract")
  test "$source_lock" = "$source_data/.runtime/runner.lock"
  expected_identity=$(jq -r \
    '.runner_lock | "\(.dev):\(.ino)"' <<<"$contract")
  [[ "$expected_identity" =~ ^[0-9]+:[0-9]+$ ]]
  test -f "$source_lock"
  test ! -L "$source_lock"
  test "$(realpath -e -- "$source_lock")" = "$source_lock"
  test "$(stat -c '%U:%G:%a:%h' "$source_lock")" = \
    ubuntu:ubuntu:600:1
  exec {RECOVERY_SOURCE_LOCK_FD}<>"$source_lock"
  flock --exclusive --nonblock "$RECOVERY_SOURCE_LOCK_FD" || \
    die 'external recovery source runner lock is active'
  validate_external_recovery_source_lock "$contract"
}
parse_status() {
  local status=$1
  PHASE=$(jq -r '.phase // ""' <<<"$status")
  PHASE_SHA=$(jq -r '.phase_sha256 // ""' <<<"$status")
  ABANDONED=$(jq -r '.abandoned' <<<"$status")
  G0_FACTS=$(jq -c '.g0_facts' <<<"$status")
  RUNTIME_READY_FACTS=$(jq -c '.runtime_ready_facts' <<<"$status")
}
old_gate_observer() {
  sudo env -i PATH=/usr/bin:/bin /usr/bin/python3 -I -B "$OLD_GATE" "$@"
}
verify_blocked_units() {
  local unit state cgroup raw signature count name trigger negate parameter
  local result extra jobs
  # These predicates are intentionally used from `if`; Bash disables errexit
  # for the whole function in that context.  Every proof step must therefore
  # return explicitly instead of relying on `set -e`.
  sudo test -f "$START_BLOCK" || return 1
  sudo test ! -L "$START_BLOCK" || return 1
  test "$(sudo stat -c '%U:%G:%a:%h' "$START_BLOCK")" = \
    root:root:644:1 || return 1
  sudo grep -Fxq '[Unit]' "$START_BLOCK" || return 1
  sudo grep -Fxq "ConditionPathExists=$START_AUTH" "$START_BLOCK" || return 1
  test "$(sudo wc -l <"$START_BLOCK")" -eq 2 || return 1
  sudo test ! -e "$START_AUTH" || return 1
  sudo test ! -L "$START_AUTH" || return 1
  sudo systemd-analyze verify trading.service >/dev/null || return 1
  raw=$(busctl get-property org.freedesktop.systemd1 \
    /org/freedesktop/systemd1/unit/trading_2eservice \
    org.freedesktop.systemd1.Unit Conditions) || return 1
  read -r signature count name trigger negate parameter result extra <<<"$raw"
  [[ "$signature" = 'a(sbbsi)' && "$count" = 1 && \
     "$name" = '"ConditionPathExists"' && "$trigger" = false && \
     "$negate" = false && "$parameter" = "\"$START_AUTH\"" && \
     "$result" =~ ^-?[0-9]+$ && -z "$extra" ]] || return 1
  jobs="$(systemctl list-jobs --no-legend --no-pager)" || return 1
  for unit in trading-state-backup.timer trading-state-backup.service \
      cloudflared.service trading-mem-monitor.service trading.service; do
    if grep -Eq "^[[:space:]]*[0-9]+[[:space:]]+$unit[[:space:]]" \
        <<<"$jobs"; then
      return 1
    fi
  done
  for unit in trading-state-backup.timer trading-state-backup.service \
      cloudflared.service trading-mem-monitor.service trading.service; do
    state="$(systemctl show "$unit" -p ActiveState --value)" || return 1
    [[ "$state" = inactive ]] || return 1
    [[ "$unit" = *.timer ]] && continue
    test "$(systemctl show "$unit" -p MainPID --value)" = 0 || return 1
    cgroup="$(systemctl show "$unit" -p ControlGroup --value)" || return 1
    cgroup_is_empty "$cgroup" || return 1
  done
  return 0
}
verify_blocked_inactive() {
  local sentinel_mode=${1:-sentinel} lock
  verify_blocked_units || return 1
  case "$sentinel_mode" in
    sentinel)
      old_gate_observer verify-release-sentinel \
        --path "$SENTINEL" --release-sha "$RELEASE_SHA" >/dev/null || return 1
      ;;
    absent)
      sudo test ! -e "$SENTINEL" || return 1
      sudo test ! -L "$SENTINEL" || return 1
      ;;
    any) ;;
    *) return 1 ;;
  esac
  lock="$RUNNER_LOCK"
  sudo test -f "$lock" || return 1
  sudo test ! -L "$lock" || return 1
  test "$(sudo stat -c '%U:%G:%a:%h' "$lock")" = \
    ubuntu:ubuntu:600:1 || return 1
  sudo -u ubuntu flock --nonblock "$lock" true || return 1
  return 0
}
normalize_blocked_inactive() {
  local sentinel_mode=${1:-sentinel}
  if verify_blocked_inactive "$sentinel_mode"; then
    return 0
  fi
  run_emergency --stop-and-arm || return 1
  verify_blocked_inactive "$sentinel_mode"
}
contain_damaged_writer() {
  local reason=$1
  # stop-only installs the persistent block and kills the writer before it
  # parses the potentially damaged attempt/gate evidence.  Its later failure
  # is therefore not accepted as proof; verify the closed units and free exact
  # runtime lock independently, while preserving every existing gate artifact.
  if run_emergency --stop-only; then :; fi
  verify_blocked_inactive any || \
    die 'damaged deployment state could not be proven contained'
  die "$reason; writer was contained without rewriting gate evidence"
}
contain_committed_writer() {
  local reason=$1
  if run_emergency --stop-only; then :; fi
  if gate verify-committed-stopped --data-dir "$RUNTIME" \
      --release-sha "$RELEASE_SHA" >/dev/null && \
      verify_blocked_inactive absent; then
    die "$reason; committed writer was persistently contained"
  fi
  die "$reason; CRITICAL committed writer containment is unproven"
}
stop_and_prove_committed() {
  run_emergency --stop-only || return 1
  gate verify-committed-stopped --data-dir "$RUNTIME" \
    --release-sha "$RELEASE_SHA" >/dev/null || return 1
  verify_blocked_inactive absent || return 1
  return 0
}
publish_recovery_boundary() {
  local temporary
  temporary=$(mktemp)
  old_gate_observer verify-release-sentinel \
    --path "$SENTINEL" --release-sha "$RELEASE_SHA" >"$temporary"
  journal publish-artifact --name old-no-open-boundary.json \
    --source "$temporary" >/dev/null
  rm -f -- "$temporary"
}
switch_active_attempt() {
  local next=$1
  sudo "$EVIDENCE_PYTHON" -I -B "$JOURNAL" switch-active-attempt \
    --release-sha "$RELEASE_SHA" --expect "$CURRENT" --next "$next" \
    >/dev/null
  test "$(sudo stat -c '%U:%G:%a:%h:%s' "$ACTIVE")" = root:root:600:1:5
  test "$(sudo sed -n '1p' "$ACTIVE")" = "$next"
}

[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || die 'release SHA not rendered'
for artifact in "${BASH_SOURCE[0]}" "$DEPLOY" "$EMERGENCY" "$JOURNAL" \
    "$OLD_GATE"; do
  test -f "$artifact"
  test ! -L "$artifact"
  test "$(stat -c '%U:%G:%a:%h' "$artifact")" = root:root:555:1
done
if sudo test -e "$EMERGENCY_START_BLOCK" || \
    sudo test -L "$EMERGENCY_START_BLOCK"; then
  fallback_rc=0
  run_emergency --contain-fallback-only || fallback_rc=$?
  [[ "$fallback_rc" -eq 75 ]] || \
    die 'emergency fallback exists and writer containment is not proven'
  die 'emergency fallback exists; primary block requires manual adjudication'
fi
for artifact in "$STAGE/reviewed-assets.sha256" "$CLEANUP" "$SPEC"; do
  sudo test -f "$artifact"
  sudo test ! -L "$artifact"
  test "$(sudo stat -c '%U:%G:%h' "$artifact")" = root:root:1
done
sudo sh -eu -c 'cd "$1" && sha256sum -c reviewed-assets.sha256 >/dev/null' \
  sh "$STAGE"
DRIVER_SHA=$(sha256 "$DEPLOY")
readonly DRIVER_SHA
sudo test -f "$ACTIVE"
sudo test ! -L "$ACTIVE"
test "$(sudo stat -c '%U:%G:%a:%h:%s' "$ACTIVE")" = root:root:600:1:5
sudo "$EVIDENCE_PYTHON" -I -B "$JOURNAL" sync-paths \
  --path "$ACTIVE" --path "$STAGE" >/dev/null
CURRENT=$(sudo sed -n '1p' "$ACTIVE")
[[ "$CURRENT" =~ ^[0-9]{4}$ ]] || die 'invalid active attempt'
ATTEMPT_DIR="$STAGE/attempts/$CURRENT"
test "$(sudo stat -c '%U:%G:%a' "$ATTEMPT_DIR")" = root:root:700
BOUNDARY_EVIDENCE="$ATTEMPT_DIR/old-no-open-boundary.json"
ARM_INTENT="$ATTEMPT_DIR/old-no-open-arm-intent.json"
SOURCE_CONTRACT="$ATTEMPT_DIR/source-runtime.json"

ensure_source_contract() {
  local temporary source_trading source_data completed_slot data_state
  if sudo test -e "$SOURCE_CONTRACT" || sudo test -L "$SOURCE_CONTRACT"; then
    # This function is called from ``if ! ensure_source_contract`` below.
    # Bash disables errexit throughout a function used as a condition, so the
    # validation result must be propagated explicitly instead of being hidden
    # by the following successful ``return``.
    journal source-contract >/dev/null || return 1
    return 0
  fi
  [[ -n "${CURRENT_SEED:-}" ]] || return 1
  source_trading=$(jq -r .source.source_trading <<<"$CURRENT_SEED") || return 1
  source_data=$(jq -r .source.source_data <<<"$CURRENT_SEED") || return 1
  completed_slot=$(jq -r .source.completed_schedule_slot \
    <<<"$CURRENT_SEED") || return 1
  data_state=$(jq -r .source.data_state <<<"$CURRENT_SEED") || return 1
  temporary=$(mktemp) || return 1
  if ! sudo "$EVIDENCE_PYTHON" -I -B "$JOURNAL" describe-source \
      --source-trading "$source_trading" --source-data "$source_data" \
      --completed-schedule-slot "$completed_slot" --data-state "$data_state" \
      >"$temporary" || \
      ! journal publish-artifact --name source-runtime.json \
        --source "$temporary" >/dev/null; then
    rm -f -- "$temporary"
    return 1
  fi
  rm -f -- "$temporary"
  journal source-contract >/dev/null
}

advance_recovered_g0() {
  local boundary_sha source_contract_sha
  boundary_sha=$(sudo sha256sum "$BOUNDARY_EVIDENCE" | awk '{print $1}')
  source_contract_sha=$(sudo sha256sum "$SOURCE_CONTRACT" | awk '{print $1}')
  journal advance --expect PREPARED --next G0 \
    --fact "old_gate_mode=$BOUNDARY_MODE" \
    --fact "old_gate_evidence_sha256=$boundary_sha" \
    --fact "source_contract_sha256=$source_contract_sha" >/dev/null
  STATUS=$(journal status)
  parse_status "$STATUS"
  test "$PHASE" = G0
}

prove_committed_runner() {
  local state pid cgroup binding
  state=$(systemctl show trading.service -p ActiveState --value) || return 1
  test "$state" = active || return 1
  test "$(systemctl show trading.service -p SubState --value)" = running || \
    return 1
  test "$(systemctl show trading.service -p User --value)" = ubuntu || \
    return 1
  test "$(systemctl show trading.service -p WorkingDirectory --value)" = \
    "$RELEASE_TRADING" || return 1
  pid=$(systemctl show trading.service -p MainPID --value) || return 1
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  cgroup=$(systemctl show trading.service -p ControlGroup --value) || return 1
  [[ "$cgroup" = /* ]] || return 1
  grep -Fxq "$pid" "/sys/fs/cgroup$cgroup/cgroup.procs" || return 1
  binding=$(old_gate_observer process-binding \
    --runner-lock "$RUNNER_LOCK" --service-cgroup "$cgroup" \
    --expected-cwd "$RELEASE_TRADING") || return 1
  jq -e --arg cgroup "$cgroup" \
    '.service_cgroup==$cgroup and (.worker_pid|type=="number" and .>0)' \
    <<<"$binding" >/dev/null || return 1
  gate verify-committed-running --data-dir "$RUNTIME" \
    --release-sha "$RELEASE_SHA" || return 1
  sudo test ! -e "$START_BLOCK" || return 1
  sudo test ! -L "$START_BLOCK" || return 1
  sudo test ! -e "$START_AUTH" || return 1
  sudo test ! -L "$START_AUTH" || return 1
  unit_job_absent trading.service || return 1
  return 0
}

recover_committed_stack() {
  local expected_slot=$1 freezer_state mem_state
  # Close the only reviewed restart trigger before any potentially blocking
  # auxiliary operation.  Otherwise the backup can replace a frozen committed
  # worker while the sentinel is already absent.
  quiesce_backup_timer || return 1
  sudo systemctl stop cloudflared.service || return 1
  prove_unit_inactive cloudflared.service || return 1
  prove_committed_runner || return 1
  freezer_state=$(systemctl show trading.service \
    -p FreezerState --value) || return 1
  case "$freezer_state" in
    running|frozen) ;;
    *) return 1 ;;
  esac
  mem_state=$(systemctl show trading-mem-monitor.service \
    -p ActiveState --value) || return 1
  case "$mem_state" in
    active) ;;
    inactive|failed)
      if [[ "$mem_state" = failed ]]; then
        sudo systemctl reset-failed trading-mem-monitor.service || return 1
      fi
      sudo systemctl start trading-mem-monitor.service || return 1
      ;;
    *) return 1 ;;
  esac
  prove_running_aux_unit trading-mem-monitor.service || return 1
  if [[ "$freezer_state" = running ]]; then
    safe_resume_slot "$expected_slot" || return 1
    check_committed_health || return 1
    prove_committed_runner || return 1
    check_committed_health || return 1
    prove_running_aux_unit trading-mem-monitor.service || return 1
    sudo systemctl freeze trading.service || return 1
  fi
  test "$(systemctl show trading.service \
    -p FreezerState --value)" = frozen || return 1
  prove_committed_runner || return 1
  prove_unit_inactive trading-state-backup.timer || return 1
  prove_unit_inactive trading-state-backup.service || return 1
  safe_resume_slot "$expected_slot" || return 1
  safe_resume_slot "$expected_slot" || return 1
  prove_unit_inactive trading-state-backup.timer || return 1
  prove_unit_inactive trading-state-backup.service || return 1
  prove_committed_runner || return 1
  prove_running_aux_unit trading-mem-monitor.service || return 1
  sudo systemctl thaw trading.service || return 1
  test "$(systemctl show trading.service \
    -p FreezerState --value)" = running || return 1
  settle_backup_timer || return 1
  prove_committed_runner || return 1
  check_committed_health || return 1
  prove_running_aux_unit trading-mem-monitor.service || return 1
  # Recovery is still a deployment boundary: do not expose instant-open until
  # the runner is thawed, the backup restart source is settled and local
  # health has been re-proved.
  sudo systemctl start cloudflared.service || return 1
  prove_running_aux_unit cloudflared.service || return 1
  return 0
}

# Never reinterpret a completed sentinel unlink as a failed attempt. This
# check precedes emergency arm, which would otherwise create a new sentinel.
if STATUS=$(journal status); then
  :
elif sudo test -e "$SENTINEL" || sudo test -L "$SENTINEL" || \
    sudo test -e "$BASELINE" || sudo test -L "$BASELINE" || \
    sudo test -e "$COMPLETION" || sudo test -L "$COMPLETION" || \
    sudo test -e "$BOUNDARY_EVIDENCE" || \
    sudo test -L "$BOUNDARY_EVIDENCE" || \
    sudo test -e "$ARM_INTENT" || sudo test -L "$ARM_INTENT" || \
    sudo test -e "$SOURCE_CONTRACT" || sudo test -L "$SOURCE_CONTRACT" || \
    sudo test -e "$START_BLOCK" || sudo test -L "$START_BLOCK" || \
    sudo test -e "$START_AUTH" || sudo test -L "$START_AUTH"; then
  contain_damaged_writer 'attempt journal is invalid beside a deployment boundary'
else
  die 'attempt journal is invalid before any deployment boundary'
fi
parse_status "$STATUS"
if sudo test -e "$SENTINEL" || sudo test -L "$SENTINEL"; then
  old_gate_observer verify-release-sentinel \
    --path "$SENTINEL" --release-sha "$RELEASE_SHA" >/dev/null || \
    contain_damaged_writer 'deployment sentinel is invalid'
else
  if [[ "$PHASE" = COMMIT_READY && "$ABANDONED" = false ]]; then
    if ! COMMITTED_SOURCE=$(journal source-contract) || \
        ! COMMITTED_SLOT=$(jq -er '.completed_schedule_slot |
          select(type=="string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}$"))' \
          <<<"$COMMITTED_SOURCE"); then
      contain_committed_writer 'committed schedule slot could not be verified'
    fi
    if sudo test -e "$START_BLOCK" || sudo test -L "$START_BLOCK" || \
        sudo test -e "$START_AUTH" || sudo test -L "$START_AUTH"; then
      # Any managed block/auth path marks an interrupted emergency, regardless
      # of the transient systemd ActiveState at the crash point.
      if ! stop_and_prove_committed; then
        contain_committed_writer \
          'interrupted committed emergency could not be normalized'
      fi
      printf 'attempt %s is committed and emergency-stopped after completing an interrupted block\n' \
        "$CURRENT" >&2
      exit 0
    fi
    if ! COMMITTED_STATE=$(systemctl show \
        trading.service -p ActiveState --value); then
      contain_committed_writer 'committed service state could not be read'
    fi
    if [[ "$COMMITTED_STATE" = active ]]; then
      if ! recover_committed_stack "$COMMITTED_SLOT"; then
        contain_committed_writer \
          'committed formal stack could not be safely recovered'
      fi
      printf 'attempt %s is already durably committed and running\n' "$CURRENT" >&2
      exit 0
    elif [[ "$COMMITTED_STATE" = inactive || "$COMMITTED_STATE" = failed ]]; then
      if sudo test -e "$START_BLOCK" || sudo test -L "$START_BLOCK"; then
        if ! stop_and_prove_committed; then
          contain_committed_writer \
            'committed stopped boundary could not be verified'
        fi
        printf 'attempt %s is committed and intentionally emergency-stopped; no deployment recovery is required\n' \
          "$CURRENT" >&2
      else
        contain_committed_writer \
          'committed service is stopped without a persistent deployment block'
      fi
      exit 0
    fi
    contain_committed_writer 'committed service state is ambiguous'
  elif sudo test -e "$BASELINE" || sudo test -L "$BASELINE" || \
      sudo test -e "$COMPLETION" || sudo test -L "$COMPLETION"; then
    # Outside COMMIT_READY, a missing sentinel beside durable gate evidence is
    # damage, never proof of a legal commit.  Contain the possibly trade-capable
    # writer without creating a replacement sentinel, then refuse for manual
    # adjudication.  The hard stop may report unrelated evidence damage after
    # killing the writer, so independently prove the fail-closed boundary.
    contain_damaged_writer 'sentinel is missing beside durable gate evidence'
  fi
fi

CURRENT_SEED=''
if sudo test -e "$ATTEMPT_DIR/recovery-seed.json" || \
    sudo test -L "$ATTEMPT_DIR/recovery-seed.json"; then
  CURRENT_SEED=$(journal recovery-seed)
fi
if [[ -z "$PHASE" ]]; then
  test "$ABANDONED" = false
  ATTEMPT_ENTRIES=$(sudo find "$ATTEMPT_DIR" -mindepth 1 -maxdepth 1 \
    -printf '%f\n' | sort)
  if [[ -n "$CURRENT_SEED" && "$ATTEMPT_ENTRIES" = recovery-seed.json ]]; then
    normalize_blocked_inactive
    printf 'recovery attempt %s is already blocked and ready\n' "$CURRENT" >&2
    exit 0
  elif [[ -z "$ATTEMPT_ENTRIES" ]]; then
    if [[ "$(systemctl show trading.service -p ActiveState --value)" = active ]] && \
        ! sudo test -e "$START_BLOCK" && ! sudo test -L "$START_BLOCK" && \
        ! sudo test -e "$SENTINEL" && ! sudo test -e "$BASELINE" && \
        ! sudo test -e "$COMPLETION"; then
      if sudo test -e "$START_AUTH" || sudo test -L "$START_AUTH"; then
        die 'orphan start authorization exists before G0; production was left running'
      fi
      printf 'fresh pre-G0 attempt %s already needs no recovery\n' "$CURRENT" >&2
      exit 0
    fi
    # A standalone hard emergency may have stopped and blocked the service
    # before this attempt wrote PREPARED.  Finish its release sentinel first,
    # then enter the same recovery state machine as an ordinary post-G0 crash.
    normalize_blocked_inactive
    journal init >/dev/null
    STATUS=$(journal status)
    parse_status "$STATUS"
    test "$PHASE" = PREPARED
  else
    die 'phase-null attempt contains unrecognized state'
  fi
fi

NEXT=$(printf '%04d' "$((10#$CURRENT + 1))")
[[ "$NEXT" =~ ^[0-9]{4}$ && "$NEXT" != 0000 ]] || die 'attempt id exhausted'
NEXT_DIR="$STAGE/attempts/$NEXT"
if sudo test -e "$NEXT_DIR" || sudo test -L "$NEXT_DIR"; then
  sudo test -d "$NEXT_DIR"
  sudo test ! -L "$NEXT_DIR"
  test "$(sudo realpath -e -- "$NEXT_DIR")" = "$NEXT_DIR"
  test "$(sudo stat -c '%U:%G:%a' "$NEXT_DIR")" = root:root:700
else
  sudo install -d -o root -g root -m 0700 "$NEXT_DIR"
fi
sudo "$EVIDENCE_PYTHON" -I -B "$JOURNAL" sync-paths \
  --path "$NEXT_DIR" --path "$STAGE/attempts" >/dev/null

# Close the journal-first pre-G0 abandon window before interpreting any block,
# sentinel, seed or boundary artifact.  A concurrent public emergency may have
# created those safety artifacts after the abandon entry became durable.  The
# abandoned attempt cannot legally publish/advance again; switch authority to
# the already-durable empty successor and let the next recovery invocation
# normalize any emergency boundary there.
if [[ "$ABANDONED" = true && "$G0_FACTS" = null ]]; then
  test "$PHASE" = PREPARED
  test "$RUNTIME_READY_FACTS" = null
  test -z "$(sudo find "$NEXT_DIR" -mindepth 1 -maxdepth 1 -print -quit)"
  switch_active_attempt "$NEXT"
  printf 'abandoned pre-G0 attempt %s closed; fresh attempt %s is active; rerun recovery if an emergency boundary remains\n' \
    "$CURRENT" "$NEXT" >&2
  exit 0
fi

BOUNDARY_MODE=''

if [[ "$G0_FACTS" != null ]]; then
  jq -e '
    type=="object" and
    keys==["old_gate_evidence_sha256","old_gate_mode",
      "source_contract_sha256"] and
    (.old_gate_mode=="runtime_sentinel" or
     .old_gate_mode=="recovery_inactive") and
    (.old_gate_evidence_sha256|test("^[0-9a-f]{64}$")) and
    (.source_contract_sha256|test("^[0-9a-f]{64}$"))
  ' <<<"$G0_FACTS" >/dev/null
  sudo test -f "$BOUNDARY_EVIDENCE"
  sudo test ! -L "$BOUNDARY_EVIDENCE"
  test "$(sudo sha256sum "$BOUNDARY_EVIDENCE"|awk '{print $1}')" = \
    "$(jq -r .old_gate_evidence_sha256 <<<"$G0_FACTS")"
  ensure_source_contract
  test "$(sudo sha256sum "$SOURCE_CONTRACT"|awk '{print $1}')" = \
    "$(jq -r .source_contract_sha256 <<<"$G0_FACTS")"
  BOUNDARY_SUMMARY=$(old_gate_observer boundary-summary \
    --evidence "$BOUNDARY_EVIDENCE" --release-sha "$RELEASE_SHA")
  BOUNDARY_MODE=$(jq -r .old_gate_mode <<<"$G0_FACTS")
  test "$(jq -r .mode <<<"$BOUNDARY_SUMMARY")" = "$BOUNDARY_MODE"
  if [[ "$BOUNDARY_MODE" = runtime_sentinel ]]; then
    sudo test -f "$ARM_INTENT"
    sudo test ! -L "$ARM_INTENT"
  fi
  if sudo test -e "$ARM_INTENT" || sudo test -L "$ARM_INTENT"; then
    old_gate_observer verify-arm-intent \
      --evidence "$ARM_INTENT" --release-sha "$RELEASE_SHA" >/dev/null
  fi
elif sudo test -e "$BOUNDARY_EVIDENCE" || sudo test -L "$BOUNDARY_EVIDENCE"; then
  # Crash closure: the root evidence was durable but the consecutive G0 phase
  # write did not happen yet.  Validate it, then finish that single transition.
  test "$PHASE" = PREPARED
  BOUNDARY_SUMMARY=$(old_gate_observer boundary-summary \
    --evidence "$BOUNDARY_EVIDENCE" --release-sha "$RELEASE_SHA")
  BOUNDARY_MODE=$(jq -r .mode <<<"$BOUNDARY_SUMMARY")
  if [[ "$BOUNDARY_MODE" = runtime_sentinel ]]; then
    sudo test -f "$ARM_INTENT"
    sudo test ! -L "$ARM_INTENT"
    # Before touching systemd, re-prove the live old sentinel.  This also
    # closes a crash immediately after its root boundary was published.
    run_emergency --reconcile-arm-intent
  fi
  if sudo test -e "$ARM_INTENT" || sudo test -L "$ARM_INTENT"; then
    old_gate_observer verify-arm-intent \
      --evidence "$ARM_INTENT" --release-sha "$RELEASE_SHA" >/dev/null
  fi
  if ! ensure_source_contract; then
    run_emergency --stop-and-arm
    normalize_blocked_inactive
    ensure_source_contract
  fi
  advance_recovered_g0
elif sudo test -e "$ARM_INTENT" || sudo test -L "$ARM_INTENT"; then
  # The intent is durable immediately before the old runner sentinel call.
  # It therefore covers both SIGKILL sides of that call: either production is
  # unchanged, or its own sentinel is already no-open.  Recovery uses one
  # conservative hard containment and then publishes the normal inactive
  # release boundary; it never tries to resume an ambiguous live runner.
  test "$PHASE" = PREPARED
  if ! old_gate_observer verify-arm-intent \
      --evidence "$ARM_INTENT" --release-sha "$RELEASE_SHA" >/dev/null; then
    die 'old runner arm intent is invalid; production was not mutated'
  fi
  # This internal action performs no service-graph mutation.  It either arms
  # the live old runner through its same-lock endpoint, proves a restarted
  # worker still obeys the already-fsynced sentinel, or proves the service is
  # stably inactive.  Only after success may hard containment install the
  # start block.  A lost creator response is not fabricated as new evidence.
  run_emergency --reconcile-arm-intent
  run_emergency --stop-and-arm
  normalize_blocked_inactive
  if sudo test -e "$BOUNDARY_EVIDENCE" || \
      sudo test -L "$BOUNDARY_EVIDENCE"; then
    BOUNDARY_SUMMARY=$(old_gate_observer boundary-summary \
      --evidence "$BOUNDARY_EVIDENCE" --release-sha "$RELEASE_SHA")
    BOUNDARY_MODE=$(jq -r .mode <<<"$BOUNDARY_SUMMARY")
    test "$BOUNDARY_MODE" = runtime_sentinel
  else
    publish_recovery_boundary
    BOUNDARY_MODE='recovery_inactive'
  fi
  ensure_source_contract
  advance_recovered_g0
elif [[ -n "$CURRENT_SEED" ]] || sudo test -e "$START_BLOCK" || \
    sudo test -L "$START_BLOCK"; then
  test "$PHASE" = PREPARED
  normalize_blocked_inactive
  publish_recovery_boundary
  if ! ensure_source_contract; then
    run_emergency --stop-and-arm
    normalize_blocked_inactive
    ensure_source_contract
  fi
  BOUNDARY_MODE='recovery_inactive'
  advance_recovered_g0
else
  test "$PHASE" = PREPARED
fi

if [[ -z "$BOUNDARY_MODE" ]]; then
  # Before G0, recovery is metadata-only.  The live writer must not be stopped
  # merely because a preparation or approval step failed.
  test "$ABANDONED" = false
  test "$(systemctl show trading.service -p ActiveState --value)" = active
  sudo test ! -e "$START_BLOCK"
  sudo test ! -L "$START_BLOCK"
  if sudo test -e "$START_AUTH" || sudo test -L "$START_AUTH"; then
    die 'orphan start authorization exists before G0; production was left running'
  fi
  sudo test ! -e "$SENTINEL"
  sudo test ! -e "$BASELINE"
  sudo test ! -e "$COMPLETION"
  journal abandon --reason operator_requested_pre_g0_reset >/dev/null
  test -z "$(sudo find "$NEXT_DIR" -mindepth 1 -maxdepth 1 -print -quit)"
  switch_active_attempt "$NEXT"
  printf 'pre-G0 attempt %s archived; production left running; fresh attempt %s ready\n' \
    "$CURRENT" "$NEXT" >&2
  exit 0
fi

# At/after G0 the boundary has already been validated above.  Recovery is an
# abnormal path: use one idempotent hard containment instead of replaying a
# stale PID/boot binding after a reboot or service replacement.
if [[ "$ABANDONED" != true ]]; then
  [[ -f "$BOUNDARY_EVIDENCE" && ! -L "$BOUNDARY_EVIDENCE" ]] || \
    die 'post-G0 attempt lacks durable no-open evidence'
  normalize_blocked_inactive
else
  # The journal was already abandoned, so gate abandon may be midway through
  # its per-file archive.  Preserve that exact layout and let the idempotent
  # gate journal finish it before any fresh sentinel is created.
  if verify_blocked_units && \
      sudo -u ubuntu flock --nonblock "$RUNNER_LOCK" true; then
    :
  else
    run_emergency --stop-only
  fi
fi
RECOVERY_SOURCE=$(journal source-contract)
RECOVERY_SLOT=$(jq -r .completed_schedule_slot <<<"$RECOVERY_SOURCE")
RECOVERY_DATA_STATE=$(jq -r .data_state <<<"$RECOVERY_SOURCE")
[[ "$RECOVERY_SLOT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]
[[ "$RECOVERY_DATA_STATE" = requires_migration ||
   "$RECOVERY_DATA_STATE" = migration_complete ]]
if [[ "$PHASE" = G0 ]]; then
  # G0 still reads the frozen pre-migration source.  The original deploy held
  # this exact lease, so recovery must reacquire it before archiving authority
  # or deriving a successor seed and retain it through the ACTIVE switch.
  hold_external_recovery_source_lock "$RECOVERY_SOURCE"
fi
journal abandon --reason operator_requested_post_g0_reset >/dev/null
# Finish the gate archive before deriving a recovery source.  This operation is
# idempotent and validates every current/archived gate artifact against its
# write-once audit.  The stable runtime digest below excludes only this active
# attempt's validated gate control plane, so a crash after archive publication
# cannot make a legitimate retry look like payload drift.
gate abandon --data-dir "$RUNTIME" --release-sha "$RELEASE_SHA" \
  --attempt-id "$CURRENT"
if [[ "$PHASE" = G0 ]]; then
  validate_external_recovery_source_lock "$RECOVERY_SOURCE"
fi

case "$PHASE" in
  G0)
    RECOVERY_SOURCE_TRADING=$(jq -r .source_trading <<<"$RECOVERY_SOURCE")
    RECOVERY_SOURCE_DATA=$(jq -r .source_data <<<"$RECOVERY_SOURCE")
    ;;
  RUNTIME_READY|QUIESCED)
    jq -e '
      type=="object" and keys==["runtime_tree_sha256","slot"] and
      (.runtime_tree_sha256|test("^[0-9a-f]{64}$")) and
      (.slot|test("^[0-9]{4}-[0-9]{2}-[0-9]{2}$"))
    ' <<<"$RUNTIME_READY_FACTS" >/dev/null
    RECOVERY_RUNTIME_RESULT=$(sudo "$EVIDENCE_PYTHON" -I -B "$JOURNAL" \
      sync-runtime-tree --release-sha "$RELEASE_SHA" \
      --attempt-id "$CURRENT" --gate-state abandoned)
    test "$(jq -r .tree_sha256 <<<"$RECOVERY_RUNTIME_RESULT")" = \
      "$(jq -r .runtime_tree_sha256 <<<"$RUNTIME_READY_FACTS")"
    RECOVERY_SOURCE_TRADING="$RELEASE_TRADING"
    RECOVERY_SOURCE_DATA="$RUNTIME"
    RECOVERY_DATA_STATE='migration_complete'
    test "$(jq -r .slot <<<"$RUNTIME_READY_FACTS")" = "$RECOVERY_SLOT"
    ;;
  T0|VALIDATED|SEALED|COMMIT_READY)
    RECOVERY_SOURCE_TRADING="$RELEASE_TRADING"
    RECOVERY_SOURCE_DATA="$RUNTIME"
    RECOVERY_DATA_STATE='migration_complete'
    ;;
  *) die 'post-G0 recovery source phase is invalid' ;;
esac
sudo "$EVIDENCE_PYTHON" -I -B "$JOURNAL" describe-source \
  --source-trading "$RECOVERY_SOURCE_TRADING" \
  --source-data "$RECOVERY_SOURCE_DATA" \
  --completed-schedule-slot "$RECOVERY_SLOT" \
  --data-state "$RECOVERY_DATA_STATE" >/dev/null

# A G0 recovery attempt can legitimately use this same stopped runtime as its
# source.  If deploy was killed during the migration write transaction, seeding
# that mixed tree would make the next read-only preflight reject forever.  Only
# roll the reviewed transaction back here; never continue or approve migration.
if [[ "$PHASE" = G0 && "$RECOVERY_SOURCE_DATA" = "$RUNTIME" &&
      "$RECOVERY_DATA_STATE" = requires_migration ]]; then
  sudo -u ubuntu env -i PATH=/usr/bin:/bin "$PYTHON" -B -E \
    "$MIGRATE" --data-dir "$RUNTIME" --recover-only
fi
if RECOVERY_CLASSIFICATION=$(sudo -u ubuntu env -i PATH=/usr/bin:/bin \
    "$PYTHON" -B -E "$MIGRATE" \
    --data-dir "$RECOVERY_SOURCE_DATA" --classify-state); then
  :
elif [[ "$RECOVERY_DATA_STATE" = requires_migration ]]; then
  # A pristine source can contain the one exact reviewed key that the normal
  # classifier intentionally rejects.  Prove the same cleanup preview as
  # deploy without changing the source; it remains requires_migration.
  sudo env -i PATH=/usr/bin:/bin "$PYTHON" -B -E "$CLEANUP" \
    --check --release-sha "$RELEASE_SHA" \
    --config "$RECOVERY_SOURCE_DATA/config.json" --spec "$SPEC"
  (
    RECOVERY_PREVIEW_SPEC=$(sudo mktemp /run/trading-cleanup-preview.XXXXXX)
    trap 'sudo rm -f -- "$RECOVERY_PREVIEW_SPEC"' EXIT
    sudo install -o root -g root -m 0644 "$SPEC" "$RECOVERY_PREVIEW_SPEC"
    sudo -u ubuntu env -i PATH=/usr/bin:/bin "$PYTHON" -B -E \
      "$MIGRATE" --data-dir "$RECOVERY_SOURCE_DATA" \
      --cleanup-spec "$RECOVERY_PREVIEW_SPEC" \
      --release-sha "$RELEASE_SHA" >/dev/null
  )
  RECOVERY_CLASSIFICATION='requires_migration'
else
  die 'migration-complete recovery source no longer validates'
fi
[[ "$RECOVERY_CLASSIFICATION" = requires_migration ||
   "$RECOVERY_CLASSIFICATION" = migration_complete ]]
if [[ "$PHASE" = G0 ]]; then
  validate_external_recovery_source_lock "$RECOVERY_SOURCE"
fi
if [[ "$RECOVERY_DATA_STATE" = migration_complete ]]; then
  test "$RECOVERY_CLASSIFICATION" = migration_complete
else
  # This also closes the crash window after migration committed but before
  # the RUNTIME_READY phase write: promotion is allowed only by the strict,
  # read-only classifier after any unfinished transaction was rolled back.
  RECOVERY_DATA_STATE="$RECOVERY_CLASSIFICATION"
fi

# The seed is written only after the old gate cycle is durably archived.  A
# crash can therefore require only an idempotent archive verify, seed retry,
# fresh arm and ACTIVE switch.
if [[ "$PHASE" = G0 ]]; then
  validate_external_recovery_source_lock "$RECOVERY_SOURCE"
fi
sudo "$EVIDENCE_PYTHON" -I -B "$JOURNAL" seed-recovery \
  --attempt-dir "$NEXT_DIR" --release-sha "$RELEASE_SHA" \
  --attempt-id "$NEXT" --driver-sha256 "$DRIVER_SHA" \
  --previous-attempt-id "$CURRENT" --previous-phase "$PHASE" \
  --previous-phase-sha256 "$PHASE_SHA" \
  --source-trading "$RECOVERY_SOURCE_TRADING" \
  --source-data "$RECOVERY_SOURCE_DATA" \
  --completed-schedule-slot "$RECOVERY_SLOT" \
  --data-state "$RECOVERY_DATA_STATE" >/dev/null

gate arm --data-dir "$RUNTIME" --release-sha "$RELEASE_SHA"
verify_blocked_inactive
if [[ "$PHASE" = G0 ]]; then
  validate_external_recovery_source_lock "$RECOVERY_SOURCE"
fi
switch_active_attempt "$NEXT"
printf 'post-G0 attempt %s archived; fresh blocked attempt %s is recoverable\n' \
  "$CURRENT" "$NEXT" >&2
