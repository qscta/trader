#!/usr/bin/env -S -i PATH=/usr/sbin:/usr/bin:/sbin:/bin TRADING_STERILE_DRIVER=1 /bin/bash --noprofile --norc
# Render __RELEASE_SHA__ once, review the rendered SHA-256, and install this
# file plus emergency-stop.sh under /usr/local/lib/trading-deploy/<sha>/.
# This driver is intentionally non-interactive.  Human decisions are supplied
# as short-lived root-owned deployment_evidence.py files.
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

# Every release SHA mutates the same service, drop-ins and authorization path.
# The shell itself owns one inherited kernel lease, allowing an internal
# fail-safe emergency to run under the same lease and a public emergency to
# identify/cancel a waiting deployment instead of being locked out for an hour.
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
[[ $- != *i* ]]

EXPECTED_SHA='__RELEASE_SHA__'
readonly RELEASE_SHA="$EXPECTED_SHA"
readonly SHORT_SHA="${RELEASE_SHA:0:12}"
readonly LIVE_REPO='/home/ubuntu/trader'
readonly LIVE_TRADING="$LIVE_REPO/trading"
readonly RELEASE_ROOT="/opt/trader-releases/$RELEASE_SHA"
readonly RELEASE_TRADING="$RELEASE_ROOT/trading"
readonly PYTHON="$RELEASE_TRADING/.venv/bin/python"
readonly RUNTIME_ROOT="/var/lib/trading-runtime/$RELEASE_SHA"
readonly CONFIG="$RUNTIME_ROOT/config.json"
readonly STATE="$RUNTIME_ROOT/trade_state.json"
readonly DATA_DIR="$RUNTIME_ROOT"
readonly DEPLOY_STAGE="/var/lib/trading-deploy/$RELEASE_SHA"
readonly DRIVER_DIR="/usr/local/lib/trading-deploy/$RELEASE_SHA"
readonly EMERGENCY="$DRIVER_DIR/emergency-stop.sh"
readonly EVIDENCE_HELPER="$DRIVER_DIR/deployment_evidence.py"
readonly ATTEMPT_HELPER="$DRIVER_DIR/deployment_attempt.py"
readonly OLD_GATE_HELPER="$DRIVER_DIR/deployment_old_runner_gate.py"
readonly EVIDENCE_PYTHON='/usr/bin/python3'
SOURCE_TRADING=''
SOURCE_DATA=''
SOURCE_DATA_STATE=''
SLOT=''
OLD_LOCK=''
OLD_GATE_MODE=''
OLD_GATE_EVIDENCE=''
OLD_GATE_EVIDENCE_SHA=''
RECOVERY_SEED=''
SOURCE_LOCK_FD=''
SOURCE_CONTRACT_SHA=''
readonly RELEASE_LOCK="$RUNTIME_ROOT/.runtime/runner.lock"
readonly START_BLOCK='/etc/systemd/system/trading.service.d/00-deploy-closed.conf'
readonly EMERGENCY_START_BLOCK='/etc/systemd/system/trading.service.d/zzzz-deploy-emergency-closed.conf'
readonly START_AUTH='/run/trading-deploy-authorize-start'
readonly RELEASE_DROPIN='/etc/systemd/system/trading.service.d/20-release.conf'
readonly MONITOR_DROPIN='/etc/systemd/system/trading-mem-monitor.service.d/20-release.conf'
readonly RELEASE_ENV='/etc/trading-release.env'
readonly SENTINEL="$RUNTIME_ROOT/.maintenance_no_open"
readonly BASELINE="$RUNTIME_ROOT/deployment_no_open_baseline.json"
readonly COMPLETION="$RUNTIME_ROOT/deployment_no_open_completion.json"
readonly APPROVAL_TIMEOUT_SECONDS=3600
readonly UNSET_ENV='LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT LD_DEBUG LD_DEBUG_OUTPUT LD_PROFILE GUNICORN_CMD_ARGS PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT PYTHONUSERBASE PYTHONWARNINGS PYTHONBREAKPOINT PYTHONPYCACHEPREFIX PYTHONPLATLIBDIR PYTHONEXECUTABLE PYTHONCASEOK PYTHONHTTPSVERIFY HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE AWS_CA_BUNDLE OPENSSL_CONF OPENSSL_MODULES SSLKEYLOGFILE GRPC_DEFAULT_SSL_ROOTS_FILE_PATH'

FAIL_SAFE_ACTIVE=0
ATTEMPT_ID=''
ATTEMPT_STAGE=''

die() { printf 'deploy: %s\n' "$*" >&2; exit 1; }
sha256() { sha256sum -- "$1" | awk '{print $1}'; }
identity() { sudo stat -c '%d:%i' -- "$1"; }
run_emergency() {
  TRADING_EMERGENCY_INTERNAL=1 \
    /bin/bash --noprofile --norc "$EMERGENCY" "$@"
}

run_fail_safe_emergency() {
  local source_lock_fd=${SOURCE_LOCK_FD:-} source_lock_path=''
  if [[ -n "$source_lock_fd" ]]; then
    source_lock_path=$OLD_LOCK
  fi
  TRADING_EMERGENCY_INTERNAL=1 \
    TRADING_INHERITED_SOURCE_LOCK_FD="$source_lock_fd" \
    TRADING_INHERITED_SOURCE_LOCK_PATH="$source_lock_path" \
    /bin/bash --noprofile --norc "$EMERGENCY" --stop-and-arm
}

validate_bound_source_contract() {
  sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" source-contract \
    --attempt-dir "$ATTEMPT_STAGE" --release-sha "$RELEASE_SHA" \
    --attempt-id "$ATTEMPT_ID" --driver-sha256 "$(sha256 "$SELF")" \
    >/dev/null || return 1
  if [[ -n "${SOURCE_CONTRACT_SHA:-}" ]]; then
    test "$(sudo sha256sum "$SOURCE_CONTRACT" | awk '{print $1}')" = \
      "$SOURCE_CONTRACT_SHA" || return 1
  fi
  return 0
}

validate_external_source_lock() {
  local contract_state=${1:-bound} fd_identity path_identity
  [[ "$contract_state" = bound || "$contract_state" = unbound ]] || return 1
  [[ "$SOURCE_DATA" != "$RUNTIME_ROOT" ]] || {
    [[ -z "$SOURCE_LOCK_FD" ]] || return 1
    return 0
  }
  [[ "$SOURCE_LOCK_FD" = 8 ]] || return 1
  if [[ "$contract_state" = bound ]]; then
    validate_bound_source_contract || return 1
  fi
  test -e "/proc/$$/fd/$SOURCE_LOCK_FD" || return 1
  flock --exclusive --nonblock "$SOURCE_LOCK_FD" || return 1
  test -f "$OLD_LOCK" || return 1
  test ! -L "$OLD_LOCK" || return 1
  test "$(realpath -e -- "$OLD_LOCK")" = "$OLD_LOCK" || return 1
  test "$(stat -c '%U:%G:%a:%h' "$OLD_LOCK")" = \
    ubuntu:ubuntu:600:1 || return 1
  fd_identity=$(stat -Lc '%d:%i' "/proc/$$/fd/$SOURCE_LOCK_FD") || return 1
  path_identity=$(stat -c '%d:%i' "$OLD_LOCK") || return 1
  test "$fd_identity" = "$path_identity" || return 1
  if [[ "$contract_state" = bound ]]; then
    validate_bound_source_contract || return 1
  fi
  return 0
}

hold_external_source_lock() {
  local contract_state=${1:-bound}
  [[ "$contract_state" = bound || "$contract_state" = unbound ]] || return 1
  [[ "$SOURCE_DATA" != "$RUNTIME_ROOT" ]] || return 0
  if [[ -n "$SOURCE_LOCK_FD" ]]; then
    validate_external_source_lock "$contract_state" || return 1
    return 0
  fi
  test ! -e /proc/$$/fd/8 || return 1
  test ! -L /proc/$$/fd/8 || return 1
  exec 8<>"$OLD_LOCK" || return 1
  SOURCE_LOCK_FD=8
  flock --exclusive --nonblock "$SOURCE_LOCK_FD" || \
    die 'external source runner lock is active'
  validate_external_source_lock "$contract_state" || \
    die 'external source runner lock changed while acquiring the lease'
  return 0
}

assert_protected_file() {
  local path=$1 expected_mode=$2
  test -f "$path" || return 1
  test ! -L "$path" || return 1
  test "$(stat -c '%U:%G:%a' "$path")" = \
    "root:root:$expected_mode" || return 1
}

assert_protected_dir() {
  local path=$1 mode
  sudo test -d "$path" || return 1
  sudo test ! -L "$path" || return 1
  test "$(sudo stat -c '%U:%G' "$path")" = root:root || return 1
  mode="$(sudo stat -c '%a' "$path")" || return 1
  test "$(( 8#$mode & 8#022 ))" -eq 0 || return 1
}

safe_install_managed() {
  local source=$1 target=$2 mode=$3 kind=$4 parent tmp old_mode backup
  parent=$(dirname -- "$target")
  assert_protected_dir "$parent"
  if sudo test -e "$target" || sudo test -L "$target"; then
    sudo test -f "$target"
    sudo test ! -L "$target"
    test "$(sudo stat -c '%U:%G' "$target")" = root:root
    test "$(sudo stat -c '%h' "$target")" = 1
    old_mode=$(sudo stat -c '%a' "$target")
    test "$(( 8#$old_mode & 8#022 ))" -eq 0
    sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" validate-managed \
      --file "$target" --kind "$kind"
    if sudo cmp -s -- "$source" "$target"; then
      test "$old_mode" = "${mode#0}"
      sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-paths \
        --path "$target" --path "$parent" >/dev/null
      return 0
    fi
    backup="$BACKUP_ROOT/managed-$kind.before"
    sudo test ! -e "$backup"
    sudo test ! -L "$backup"
    sudo cp --archive --no-dereference "$target" "$backup"
    sudo sh -eu -c 'sha256sum "$1" >"$1.sha256" && sha256sum -c "$1.sha256" >/dev/null' \
      sh "$backup"
    sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-paths \
      --path "$backup" --path "$backup.sha256" \
      --path "$BACKUP_ROOT" >/dev/null
  fi
  tmp=$(sudo mktemp "$parent/.trading-deploy-$kind.XXXXXX")
  sudo install -o root -g root -m "$mode" "$source" "$tmp"
  test "$(sudo sha256sum "$source"|awk '{print $1}')" = \
       "$(sudo sha256sum "$tmp"|awk '{print $1}')"
  sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-paths \
    --path "$tmp" >/dev/null
  sudo mv --no-target-directory "$tmp" "$target"
  sudo test -f "$target"
  sudo test ! -L "$target"
  test "$(sudo stat -c '%U:%G:%a' "$target")" = "root:root:${mode#0}"
  test "$(sudo sha256sum "$source"|awk '{print $1}')" = \
       "$(sudo sha256sum "$target"|awk '{print $1}')"
  sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" validate-managed \
    --file "$target" --kind "$kind"
  sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-paths \
    --path "$target" --path "$parent" >/dev/null
}

verify_block() {
  sudo test -f "$START_BLOCK" || return 1
  sudo test ! -L "$START_BLOCK" || return 1
  test "$(sudo stat -c '%U:%G:%a:%h' "$START_BLOCK")" = \
    root:root:644:1 || return 1
  sudo grep -Fxq '[Unit]' "$START_BLOCK" || return 1
  sudo grep -Fxq "ConditionPathExists=$START_AUTH" "$START_BLOCK" || return 1
  test "$(sudo wc -l <"$START_BLOCK")" -eq 2 || return 1
  sudo test ! -e "$START_AUTH" || return 1
  sudo test ! -L "$START_AUTH" || return 1
  return 0
}

verify_loaded_start_block() {
  local conditions
  sudo systemd-analyze verify trading.service >/dev/null || return 1
  conditions=$(systemctl show trading.service -p Conditions --value) || return 1
  grep -Fq "ConditionPathExists=$START_AUTH" <<<"$conditions" || return 1
  return 0
}

verify_start_block_absent() {
  local conditions
  sudo test ! -e "$START_BLOCK" || return 1
  sudo test ! -L "$START_BLOCK" || return 1
  sudo test ! -e "$START_AUTH" || return 1
  sudo test ! -L "$START_AUTH" || return 1
  sudo systemd-analyze verify trading.service >/dev/null || return 1
  conditions=$(systemctl show trading.service -p Conditions --value) || return 1
  if grep -Fq "ConditionPathExists=$START_AUTH" <<<"$conditions"; then
    return 1
  fi
  return 0
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
  run_release_helper \
    /usr/bin/env \
    "TRADING_RUNNER_LOCK_FILE=$RELEASE_LOCK" \
    "$PYTHON" -B -E "$RELEASE_TRADING/deployment_no_open_gate.py" "$@"
}

fail_safe() {
  local rc=${1:-1}
  trap - ERR EXIT HUP INT TERM
  if [[ "$FAIL_SAFE_ACTIVE" -eq 0 ]]; then
    FAIL_SAFE_ACTIVE=1
    # Keep the exact external source lease across synchronous containment.
    # The emergency child independently verifies the inherited fd/path inode
    # and exclusive flock before it captures a recovery source contract.
    run_fail_safe_emergency || true
  fi
  exit "$rc"
}

post_commit_fail() {
  local rc=${1:-1}
  trap - ERR EXIT HUP INT TERM
  if run_emergency --stop-only &&
      gate verify-committed-stopped --data-dir "$DATA_DIR" \
        --release-sha "$RELEASE_SHA" >/dev/null &&
      assert_inactive_boundaries && verify_block &&
      sudo test ! -e "$SENTINEL" && sudo test ! -L "$SENTINEL" &&
      sudo -u ubuntu flock --nonblock "$RELEASE_LOCK" true; then
    printf 'deploy: committed deployment failed; writer is persistently contained\n' >&2
    exit "$rc"
  fi
  printf 'deploy: CRITICAL committed deployment failed and containment is unproven\n' >&2
  exit 70
}

commit_boundary_fail() {
  local rc=${1:-1}
  trap - ERR EXIT HUP INT TERM
  if sudo test ! -e "$SENTINEL" && sudo test ! -L "$SENTINEL"; then
    post_commit_fail "$rc"
  fi
  fail_safe "$rc"
}

wait_approval() {
  local kind=$1 file=$2; shift 2
  local deadline=$((SECONDS + APPROVAL_TIMEOUT_SECONDS)) binding
  printf 'deploy: waiting for root-owned %s approval at %s\n' "$kind" "$file" >&2
  while ! sudo test -e "$file"; do
    (( SECONDS < deadline )) || die "$kind approval timed out"
    sleep 5
  done
  local args=()
  for binding in "$@"; do args+=(--binding "$binding"); done
  sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" verify \
    --file "$file" --kind "$kind" --release-sha "$RELEASE_SHA" "${args[@]}"
}

write_request() {
  local kind=$1 file=$2; shift 2
  local tmp binding
  tmp=$(mktemp)
  {
    printf 'schema_version=1\nkind=%s\nrelease_sha=%s\n' "$kind" "$RELEASE_SHA"
    for binding in "$@"; do printf 'binding=%s\n' "$binding"; done
    printf 'approval_path=%s\nexpires_in_max=3600\n' "$file"
  } >"$tmp"
  sudo install -o root -g root -m 0600 "$tmp" "$ATTEMPT_STAGE/$kind.request"
  rm -f -- "$tmp"
}

assert_inactive_boundaries() {
  local unit cgroup populated
  for unit in trading-state-backup.timer trading-state-backup.service \
      cloudflared.service trading-mem-monitor.service trading.service; do
    test "$(systemctl show "$unit" -p ActiveState --value)" = inactive || \
      return 1
    test "$(systemctl show "$unit" -p MainPID --value)" = 0 || return 1
    cgroup=$(systemctl show "$unit" -p ControlGroup --value) || return 1
    populated=$(cgroup_populated "$cgroup") || return 1
    test "$populated" = 0 || return 1
  done
  return 0
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

http_socket_idle_once() {
  local rows state recvq _rest listen_count=0
  rows=$(sudo ss -H -tan 'sport = :5000') || return 1
  [[ -n "$rows" ]] || return 1
  while read -r state recvq _rest; do
    case "$state" in
      LISTEN)
        [[ "$recvq" =~ ^[0-9]+$ && "$recvq" -eq 0 ]] || return 1
        listen_count=$((listen_count + 1))
        ;;
      TIME-WAIT) ;;
      *) return 1 ;;
    esac
  done <<<"$rows"
  [[ "$listen_count" -eq 1 ]]
}

wait_local_http_idle() {
  local deadline=$((SECONDS + 30)) stable=0
  while (( SECONDS < deadline )); do
    if http_socket_idle_once; then
      stable=$((stable + 1))
      [[ "$stable" -ge 2 ]] && return 0
    else
      stable=0
    fi
    sleep 1
  done
  return 1
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
  # Observation cannot prevent a scheduled backup from restarting a frozen
  # trading.service.  Stop both trigger and worker before the unique commit
  # boundary; every command propagates failure even when this helper is called
  # from a conditional/trap context.
  sudo systemctl stop trading-state-backup.timer \
    trading-state-backup.service || return 1
  prove_unit_inactive trading-state-backup.timer || return 1
  prove_unit_inactive trading-state-backup.service || return 1
  return 0
}

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

prove_exact_exec_start() {
  local unit=$1 expected_path=$2 expected_argv=$3 value prefix
  value=$(systemctl show "$unit" -p ExecStart --value) || return 1
  [[ "$value" != *$'\n'* ]] || return 1
  prefix="{ path=$expected_path ; argv[]=$expected_argv ; ignore_errors="
  [[ "${value:0:${#prefix}}" = "$prefix" ]] || return 1
  test "$(grep -o 'argv\[\]=' <<<"$value" | wc -l | tr -d ' ')" = 1
}

prove_effective_units() {
  local unit property value envfiles expected_exec
  for unit in trading.service trading-mem-monitor.service; do
    for property in ExecCondition ExecStartPre ExecStartPost ExecReload \
        ExecStop ExecStopPost PassEnvironment PAMName RootDirectory RootImage \
        BindPaths BindReadOnlyPaths TemporaryFileSystem MountImages \
        ExtensionImages ExtensionDirectories LoadCredential \
        LoadCredentialEncrypted SetCredential SetCredentialEncrypted; do
      test -z "$(systemctl show "$unit" -p "$property" --value)"
    done
    test "$(systemctl show "$unit" -p WorkingDirectory --value)" = \
      "$RELEASE_TRADING"
    test "$(systemctl show "$unit" -p KillMode --value)" = control-group
    test "$(systemctl show "$unit" -p Delegate --value)" = no
    test "$(systemctl show "$unit" -p UMask --value)" = 0077
    test "$(systemctl show "$unit" -p ExecSearchPath --value)" = \
      /usr/sbin:/usr/bin:/sbin:/bin
    test "$(systemctl show "$unit" -p UnsetEnvironment --value)" = \
      "$UNSET_ENV"
  done

  test "$(systemctl show trading.service -p User --value)" = ubuntu
  test "$(systemctl show trading.service -p Group --value)" = ubuntu
  test "$(systemctl show trading.service -p DynamicUser --value)" = no
  test -z "$(systemctl show trading.service -p Environment --value)"
  envfiles=$(systemctl show trading.service -p EnvironmentFiles --value | \
    grep -oE '/[^ ;)]+' | sort)
  test "$envfiles" = $'/etc/trading-release.env\n/etc/trading.env'
  expected_exec="$PYTHON -B -E -m gunicorn -c gunicorn.conf.py wsgi:application"
  prove_exact_exec_start trading.service "$PYTHON" "$expected_exec"

  test "$(systemctl show trading-mem-monitor.service -p DynamicUser --value)" = yes
  test "$(systemctl show trading-mem-monitor.service -p SupplementaryGroups --value)" = ubuntu
  test "$(systemctl show trading-mem-monitor.service -p Environment --value)" = \
    "TRADING_DATA_DIR=$DATA_DIR TRADING_CONFIG_FILE=$CONFIG "\
"TRADING_MEM_MONITOR_LOG=/var/log/trading-mem-monitor/mem_monitor.log"
  envfiles=$(systemctl show trading-mem-monitor.service \
    -p EnvironmentFiles --value | grep -oE '/[^ ;)]+' | sort)
  test "$envfiles" = /etc/trading-mem-monitor.env
  expected_exec="$PYTHON -B -E mem_monitor.py"
  prove_exact_exec_start trading-mem-monitor.service "$PYTHON" "$expected_exec"
}

prove_formal_runner() {
  local pid cgroup
  test "$(systemctl show trading.service -p ActiveState --value)" = active
  test "$(systemctl show trading.service -p SubState --value)" = running
  test "$(systemctl show trading.service -p WorkingDirectory --value)" = \
    "$RELEASE_TRADING"
  test "$(systemctl show trading.service -p User --value)" = ubuntu
  pid=$(systemctl show trading.service -p MainPID --value)
  [[ "$pid" =~ ^[1-9][0-9]*$ ]]
  cgroup=$(systemctl show trading.service -p ControlGroup --value)
  [[ "$cgroup" = /* && -f "/sys/fs/cgroup$cgroup/cgroup.procs" ]] || return 1
  grep -Fxq "$pid" "/sys/fs/cgroup$cgroup/cgroup.procs"
  if sudo -u ubuntu flock --nonblock "$RELEASE_LOCK" true; then
    die 'formal runner is active but does not hold the exact runner lock'
  else
    test "$?" -eq 1
  fi
  old_gate_observer process-binding --runner-lock "$RELEASE_LOCK" \
    --service-cgroup "$cgroup" --expected-cwd "$RELEASE_TRADING" \
    >/dev/null || return 1
  # A backup worker may legitimately request a formal-service restart.  An
  # active PID/lock proof is therefore incomplete while such a job is queued:
  # it could replace the proved generation immediately after this function.
  unit_job_absent trading.service || return 1
  return 0
}

advance_phase() {
  local expected=$1 next=$2; shift 2
  local args=() fact
  for fact in "$@"; do args+=(--fact "$fact"); done
  sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" advance \
    --attempt-dir "$ATTEMPT_STAGE" --release-sha "$RELEASE_SHA" \
    --attempt-id "$ATTEMPT_ID" --driver-sha256 "$REVIEWED_DEPLOY_SHA" \
    --expect "$expected" --next "$next" "${args[@]}" >/dev/null
}

publish_attempt_artifact() {
  local name=$1 source=$2
  sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" publish-artifact \
    --attempt-dir "$ATTEMPT_STAGE" --release-sha "$RELEASE_SHA" \
    --attempt-id "$ATTEMPT_ID" --driver-sha256 "$REVIEWED_DEPLOY_SHA" \
    --name "$name" --source "$source" >/dev/null
}

check_local_health() {
  run_release_helper \
    "$PYTHON" -B -E -c \
    'import json,os,urllib.request; from trade_state import load_strict_json; q=urllib.request.Request("http://127.0.0.1:5000/api/status",headers={"X-API-Token":os.environ["TRADING_API_TOKEN"]}); r=urllib.request.urlopen(q,timeout=15); d=load_strict_json(r); blockers=d.get("health",{}).get("safety_blockers"); ok=(r.status==200 and d.get("status")=="running" and d.get("health",{}).get("healthy") is True and blockers=={"open_intents":0,"close_intents":0,"position_quarantines":0,"stop_residues":0} and d.get("open_intents_count")==0 and d.get("stop_residues")==[] and d.get("stop_anomalies")=={} and d.get("position_quarantines")=={}); print(json.dumps({k:d.get(k) for k in ("status","open_positions_count","open_intents_count","last_daily_check_date")},sort_keys=True)); raise SystemExit(0 if ok else 2)'
}

check_maintenance_http_gate() {
  run_release_helper \
    "$PYTHON" -B -E -c \
    'import os,urllib.error,urllib.request; q=urllib.request.Request("http://127.0.0.1:5000/api/instant_open",data=b"{}",headers={"Content-Type":"application/json","X-API-Token":os.environ["TRADING_API_TOKEN"]},method="POST");
try: urllib.request.urlopen(q,timeout=15); raise SystemExit(2)
except urllib.error.HTTPError as e: raise SystemExit(0 if e.code==503 else 2)'
}

verify_release_execution_boundary() {
  local scan relative failed=0
  test "$(git -c safe.directory="$RELEASE_ROOT" -C "$RELEASE_ROOT" rev-parse HEAD)" = "$RELEASE_SHA"
  git -c safe.directory="$RELEASE_ROOT" -C "$RELEASE_ROOT" diff --quiet
  git -c safe.directory="$RELEASE_ROOT" -C "$RELEASE_ROOT" diff --cached --quiet
  test "$(stat -c '%U:%G' "$RELEASE_TRADING")" = root:root
  test "$(( 8#$(stat -c '%a' "$RELEASE_TRADING") & 8#022 ))" -eq 0
  test "$(stat -c '%U:%G' "$RELEASE_TRADING/deployment_no_open_gate.py")" = root:root
  test -d "$RELEASE_TRADING/.venv" || return 1
  test ! -L "$RELEASE_TRADING/.venv" || return 1
  test "$(realpath -e -- "$RELEASE_TRADING/.venv")" = "$RELEASE_TRADING/.venv"
  test -z "$(find "$RELEASE_TRADING/.venv" -xdev ! -user root -print -quit)"
  scan=$(mktemp)
  git -c safe.directory="$RELEASE_ROOT" -C "$RELEASE_ROOT" \
    ls-files --others -z >"$scan" || { rm -f -- "$scan"; return 1; }
  while IFS= read -r -d '' relative; do
    case "$relative" in
      trading/.venv/*) ;;
      *)
        printf 'deploy: unreviewed release entry: %s\n' "$relative" >&2
        failed=1
        ;;
    esac
  done <"$scan"
  rm -f -- "$scan"
  test "$failed" -eq 0 || return 1
  sudo sh -eu -c 'cd "$1" && sha256sum -c reviewed-assets.sha256 >/dev/null' \
    sh "$DEPLOY_STAGE" || return 1
  sudo sh -eu -c 'cd "$1" && sha256sum -c "$2" >/dev/null' \
    sh "$RELEASE_ROOT" "$DEPLOY_STAGE/reviewed-tracked.sha256" || return 1
  sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" verify-venv-manifest \
    --release-root "$RELEASE_ROOT" \
    --manifest "$DEPLOY_STAGE/reviewed-venv.json" || return 1
  return 0
}

verify_reviewed_materials() {
  local vtmp ptmp
  verify_release_execution_boundary
  test "$(sha256 "$SELF")" = "$REVIEWED_DEPLOY_SHA"
  test "$(sha256 "$EMERGENCY")" = "$REVIEWED_EMERGENCY_SHA"
  test "$(sha256 "$EVIDENCE_HELPER")" = "$REVIEWED_EVIDENCE_SHA"
  test "$(sha256 "$ATTEMPT_HELPER")" = "$REVIEWED_ATTEMPT_SHA"
  test "$(sha256 "$OLD_GATE_HELPER")" = "$REVIEWED_OLD_GATE_SHA"
  sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" validate-trading-env \
    --file /etc/trading.env
  sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" validate-monitor-env \
    --file /etc/trading-mem-monitor.env
  vtmp=$(mktemp)
  env -i PATH=/usr/bin:/bin "$PYTHON" -B -E -c \
    'import sys; print(".".join(map(str,sys.version_info[:3])))' >"$vtmp"
  sudo cmp -s -- "$vtmp" "$DEPLOY_STAGE/python-version.txt"
  env -i PATH=/usr/bin:/bin "$PYTHON" -B -E -m pip freeze --all >"$vtmp"
  if grep -Eq '(^-e| @ (file|git)?:)' "$vtmp"; then return 1; fi
  sudo cmp -s -- "$vtmp" "$DEPLOY_STAGE/pip-freeze.txt"
  rm -f -- "$vtmp"
  ptmp=$(mktemp)
  env -i PATH=/usr/bin:/bin EXPECTED_VENV="$RELEASE_TRADING/.venv" \
    "$PYTHON" -B -E -c \
    'import importlib,os; v=os.path.realpath(os.environ["EXPECTED_VENV"]); out=[]; [out.append(f"{n}={os.path.realpath(importlib.import_module(n).__file__)}") for n in ("ccxt","flask","gunicorn")]; assert all(os.path.commonpath((p.split("=",1)[1],v))==v for p in out); print("\n".join(out))' >"$ptmp"
  sudo cmp -s -- "$ptmp" "$DEPLOY_STAGE/package-paths.txt"
  rm -f -- "$ptmp"
  sudo sh -eu -c 'cd "$1" && sha256sum -c "$2" >/dev/null' \
    sh "$RELEASE_ROOT" "$DEPLOY_STAGE/reviewed-tracked.sha256"
}

completed_slot() {
  local data_dir=$1 expected=${2:-} args=()
  if [[ -n "$expected" ]]; then args+=(--expected-slot "$expected"); fi
  sudo -u ubuntu env -i PATH=/usr/bin:/bin "$PYTHON" -B -E \
    "$RELEASE_TRADING/deployment_no_open_gate.py" completed-slot \
    --config "$data_dir/config.json" --data-dir "$data_dir" "${args[@]}"
}

bind_source_contract() {
  local temporary existing
  temporary=$(mktemp)
  sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" describe-source \
    --source-trading "$SOURCE_TRADING" --source-data "$SOURCE_DATA" \
    --completed-schedule-slot "$SLOT" --data-state "$SOURCE_DATA_STATE" \
    >"$temporary"
  if sudo test -e "$SOURCE_CONTRACT" || \
      sudo test -L "$SOURCE_CONTRACT"; then
    existing=$(mktemp)
    sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" source-contract \
      --attempt-dir "$ATTEMPT_STAGE" --release-sha "$RELEASE_SHA" \
      --attempt-id "$ATTEMPT_ID" --driver-sha256 "$(sha256 "$SELF")" \
      >"$existing"
    cmp -s -- "$temporary" "$existing"
    rm -f -- "$existing"
  else
    sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" publish-artifact \
      --attempt-dir "$ATTEMPT_STAGE" --release-sha "$RELEASE_SHA" \
      --attempt-id "$ATTEMPT_ID" --driver-sha256 "$(sha256 "$SELF")" \
      --name source-runtime.json --source "$temporary" >/dev/null
  fi
  rm -f -- "$temporary"
  SOURCE_CONTRACT_SHA=$(sudo sha256sum "$SOURCE_CONTRACT" | awk '{print $1}')
}

archive_migration_artifacts() {
  local archive="$BACKUP_ROOT/migration-originals" scan source destination
  local legacy_parent="$DATA_DIR/data" legacy="$DATA_DIR/data/okx"
  local marker="$DATA_DIR/.okx_legacy_migration_complete.json"
  local before after moved=0 sync_args=()
  sudo test ! -e "$DATA_DIR/.single_strategy_migration_journal.json"
  sudo test ! -L "$DATA_DIR/.single_strategy_migration_journal.json"
  sudo install -d -o root -g root -m 0700 "$archive"
  scan=$(mktemp)
  sudo find "$DATA_DIR" -mindepth 1 -maxdepth 1 \
    -name '*.premigrate.*' -print0 >"$scan"
  while IFS= read -r -d '' source; do
    [[ "$source" = "$DATA_DIR/"* && "$source" != *$'\n'* ]] || return 1
    sudo test -f "$source" || return 1
    sudo test ! -L "$source" || return 1
    test "$(sudo stat -c '%U:%G:%a:%h' "$source")" = \
      ubuntu:ubuntu:600:1 || return 1
    destination="$archive/${source##*/}"
    sudo test ! -e "$destination" || return 1
    sudo test ! -L "$destination" || return 1
    before=$(sudo sha256sum -- "$source" | awk '{print $1}') || return 1
    sudo mv --no-clobber --no-target-directory "$source" "$destination" || \
      return 1
    after=$(sudo sha256sum -- "$destination" | awk '{print $1}') || return 1
    test "$after" = "$before" || return 1
    sync_args+=(--path "$destination")
    moved=$((moved + 1))
  done <"$scan"
  rm -f -- "$scan"
  test -z "$(sudo find "$DATA_DIR" -mindepth 1 -maxdepth 1 \
    -name '*.premigrate.*' -print -quit)" || return 1

  # The old data/okx snapshot and its completion marker are upgrade evidence,
  # never MA runtime inputs.  Keep them under the root-only rollback tree, not
  # in the active data directory.  A partially completed prior move is safe to
  # resume because each source and destination is accepted only in one place.
  if sudo test -e "$legacy" || sudo test -L "$legacy"; then
    sudo test -d "$legacy" || return 1
    sudo test ! -L "$legacy" || return 1
    test "$(sudo stat -c '%U:%G' "$legacy")" = ubuntu:ubuntu || return 1
    sudo test -d "$legacy_parent" || return 1
    sudo test ! -L "$legacy_parent" || return 1
    test -z "$(sudo find "$legacy_parent" -mindepth 1 -maxdepth 1 \
      ! -name okx -print -quit)" || return 1
    test -z "$(sudo find "$legacy" -xdev \
      \( ! -user ubuntu -o -perm /022 -o \
      ! \( -type d -o -type f \) \) -print -quit)" || return 1
    destination="$archive/legacy-okx"
    sudo test ! -e "$destination" || return 1
    sudo test ! -L "$destination" || return 1
    sudo mv --no-clobber --no-target-directory "$legacy" "$destination" || \
      return 1
    sudo test -d "$destination" || return 1
    sudo test ! -L "$destination" || return 1
    sudo rmdir "$legacy_parent" || return 1
    sync_args+=(--path "$destination")
    moved=$((moved + 1))
  fi
  if sudo test -e "$marker" || sudo test -L "$marker"; then
    sudo test -f "$marker" || return 1
    sudo test ! -L "$marker" || return 1
    test "$(sudo stat -c '%U:%G:%a:%h' "$marker")" = \
      ubuntu:ubuntu:600:1 || return 1
    destination="$archive/.okx_legacy_migration_complete.json"
    sudo test ! -e "$destination" || return 1
    sudo test ! -L "$destination" || return 1
    before=$(sudo sha256sum -- "$marker" | awk '{print $1}') || return 1
    sudo mv --no-clobber --no-target-directory "$marker" "$destination" || \
      return 1
    after=$(sudo sha256sum -- "$destination" | awk '{print $1}') || return 1
    test "$after" = "$before" || return 1
    sync_args+=(--path "$destination")
    moved=$((moved + 1))
  fi
  sudo test ! -e "$legacy"
  sudo test ! -L "$legacy"
  sudo test ! -e "$marker"
  sudo test ! -L "$marker"
  if [[ "$moved" -gt 0 ]]; then
    sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-paths \
      "${sync_args[@]}" --path "$archive" --path "$BACKUP_ROOT" \
      --path "$DATA_DIR" >/dev/null || return 1
  fi
  return 0
}

old_gate_observer() {
  sudo env -i PATH=/usr/bin:/bin \
    /usr/bin/python3 -I -B "$OLD_GATE_HELPER" "$@"
}

old_gate_client() {
  run_helper \
    --property=EnvironmentFile=/etc/trading.env \
    --property="UnsetEnvironment=$UNSET_ENV" \
    /usr/bin/python3 -I -B "$OLD_GATE_HELPER" "$@"
}

credential_exposure() {
  local config_path=$1
  run_release_helper \
    "$PYTHON" -B -E "$RELEASE_TRADING/deployment_no_open_gate.py" \
    credential-exposure --config "$config_path"
}

[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || die 'release SHA placeholder not rendered'
for command in awk bash busctl date flock git grep jq openssl realpath rsync sha256sum \
    ss stat sudo systemctl systemd-analyze systemd-run tar; do
  command -v "$command" >/dev/null || die "required command missing: $command"
done
SELF=$(realpath -e -- "${BASH_SOURCE[0]}")
test "$SELF" = "$DRIVER_DIR/deploy.sh"
assert_protected_file "$SELF" 555
assert_protected_file "$EMERGENCY" 555
assert_protected_file "$EVIDENCE_HELPER" 555
assert_protected_file "$ATTEMPT_HELPER" 555
assert_protected_file "$OLD_GATE_HELPER" 555
if sudo test -e "$EMERGENCY_START_BLOCK" || \
    sudo test -L "$EMERGENCY_START_BLOCK"; then
  fallback_rc=0
  run_emergency --contain-fallback-only || fallback_rc=$?
  [[ "$fallback_rc" -eq 75 ]] || \
    die 'emergency fallback exists and writer containment is not proven'
  die 'emergency fallback exists; primary block requires manual adjudication'
fi
sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" assert-no-mounts \
  --path "$RELEASE_TRADING" >/dev/null
test -x "$EVIDENCE_PYTHON"
test "$(stat -c '%U:%G' "$DRIVER_DIR")" = root:root
test "$(( 8#$(stat -c '%a' "$DRIVER_DIR") & 8#022 ))" -eq 0
assert_protected_dir /opt/trader-releases
assert_protected_dir "$RELEASE_ROOT"
test -x "$PYTHON"
test "$(realpath -e -- "$RELEASE_TRADING")" = "$RELEASE_TRADING"
test -f "$EVIDENCE_HELPER"
test -f "$ATTEMPT_HELPER"
test -f "$OLD_GATE_HELPER"
cd "$RELEASE_TRADING"
sudo test -d "$DEPLOY_STAGE"
test "$(sudo stat -c '%U:%G:%a' "$DEPLOY_STAGE")" = root:root:700
sudo test -f "$DEPLOY_STAGE/active-attempt"
sudo test ! -L "$DEPLOY_STAGE/active-attempt"
test "$(sudo stat -c '%U:%G:%a:%h:%s' "$DEPLOY_STAGE/active-attempt")" = \
  root:root:600:1:5
sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-paths \
  --path "$DEPLOY_STAGE/active-attempt" --path "$DEPLOY_STAGE" >/dev/null
ATTEMPT_ID=$(sudo sed -n '1p' "$DEPLOY_STAGE/active-attempt")
[[ "$ATTEMPT_ID" =~ ^[0-9]{4}$ ]]
ATTEMPT_STAGE="$DEPLOY_STAGE/attempts/$ATTEMPT_ID"
test "$(sudo stat -c '%U:%G:%a' "$ATTEMPT_STAGE")" = root:root:700
readonly ATTEMPT_ID ATTEMPT_STAGE
readonly CLEANUP="$DEPLOY_STAGE/remove-one-confirmed-config-key.py"
readonly SPEC="$DEPLOY_STAGE/remove-one-confirmed-config-key.spec.json"
sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" init \
  --attempt-dir "$ATTEMPT_STAGE" --release-sha "$RELEASE_SHA" \
  --attempt-id "$ATTEMPT_ID" --driver-sha256 "$(sha256 "$SELF")" >/dev/null
# No release interpreter or module may run until tracked code, the complete
# venv, and the untracked/ignored closure have all been proved.
verify_release_execution_boundary
if sudo test -e "$ATTEMPT_STAGE/recovery-seed.json" || \
    sudo test -L "$ATTEMPT_STAGE/recovery-seed.json"; then
  RECOVERY_SEED=$(sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" \
    recovery-seed --attempt-dir "$ATTEMPT_STAGE" \
    --release-sha "$RELEASE_SHA" --attempt-id "$ATTEMPT_ID" \
    --driver-sha256 "$(sha256 "$SELF")")
fi
if [[ -n "$RECOVERY_SEED" ]]; then
  # The root recovery seed, release sentinel and stopped lock are the source of
  # truth.  A half-installed old unit/drop-in is deliberately not re-parsed.
  SOURCE_TRADING=$(jq -r .source.source_trading <<<"$RECOVERY_SEED")
  SOURCE_DATA=$(jq -r .source.source_data <<<"$RECOVERY_SEED")
  SOURCE_DATA_STATE=$(jq -r .source.data_state <<<"$RECOVERY_SEED")
  SLOT=$(jq -r .source.completed_schedule_slot <<<"$RECOVERY_SEED")
  OLD_LOCK=$(jq -r .source.runner_lock.path <<<"$RECOVERY_SEED")
  test "$(realpath -e -- "$SOURCE_TRADING")" = "$SOURCE_TRADING"
  test "$(sudo realpath -e -- "$SOURCE_DATA")" = "$SOURCE_DATA"
else
  test "$(systemctl show trading.service -p User --value)" = ubuntu
  SOURCE_DIRECT_ENV=$(systemctl show trading.service -p Environment --value)
  case "$SOURCE_DIRECT_ENV" in
    *TRADING_RUNNER_LOCK_FILE=*|*TRADING_DATA_DIR=*|*TRADING_CONFIG_FILE=*|\
    *TRADING_MAINTENANCE_SENTINEL=*)
      die 'old trading.service directly overrides a deployment-owned runtime path'
      ;;
  esac
  SOURCE_TRADING=$(systemctl show trading.service -p WorkingDirectory --value)
  [[ "$SOURCE_TRADING" = "$LIVE_TRADING" ||
     "$SOURCE_TRADING" =~ ^/opt/trader-releases/[0-9a-f]{40}/trading$ ]]
  test "$(realpath -e -- "$SOURCE_TRADING")" = "$SOURCE_TRADING"
  test -d "$SOURCE_TRADING"
  test ! -L "$SOURCE_TRADING"
  SOURCE_DATA="$SOURCE_TRADING"
  source_env_count=0
  release_env_attached=0
  while IFS= read -r source_envfile; do
    [[ -n "$source_envfile" ]] || continue
    case "$source_envfile" in
      /etc/trading.env) source_env_count=$((source_env_count + 1)) ;;
      "$RELEASE_ENV") release_env_attached=$((release_env_attached + 1)) ;;
      *) die "unreviewed trading.service EnvironmentFile: $source_envfile" ;;
    esac
  done < <(systemctl show trading.service -p EnvironmentFiles --value | \
    grep -oE '/[^ ;)]+' || true)
  test "$source_env_count" -eq 1
  test "$release_env_attached" -le 1
  if [[ "$release_env_attached" -eq 1 ]]; then
    sudo test -f "$RELEASE_ENV"
    sudo test ! -L "$RELEASE_ENV"
    sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" validate-managed \
      --file "$RELEASE_ENV" --kind release_env
    configured_data=$(sudo sed -n 's/^TRADING_DATA_DIR=//p' "$RELEASE_ENV")
    if [[ -n "$configured_data" ]]; then SOURCE_DATA="$configured_data"; fi
  fi
  [[ "$SOURCE_DATA" = "$SOURCE_TRADING" ||
     "$SOURCE_DATA" =~ ^/var/lib/trading-runtime/[0-9a-f]{40}$ ]]
  test "$(realpath -e -- "$SOURCE_DATA")" = "$SOURCE_DATA"
  OLD_LOCK="$SOURCE_DATA/.runtime/runner.lock"
  SOURCE_DATA_STATE='requires_migration'
  SLOT=$(completed_slot "$SOURCE_DATA") ||
    die 'current schedule day is not complete; production was left running'
fi
[[ "$SOURCE_DATA_STATE" = requires_migration ||
   "$SOURCE_DATA_STATE" = migration_complete ]]
[[ "$SLOT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]
if [[ -n "$RECOVERY_SEED" ]]; then
  completed_slot "$SOURCE_DATA" "$SLOT" >/dev/null ||
    die 'recovery source changed its frozen completed schedule slot'
fi
readonly SOURCE_TRADING SOURCE_DATA SOURCE_DATA_STATE SLOT OLD_LOCK

readonly SOURCE_CONTRACT="$ATTEMPT_STAGE/source-runtime.json"
if [[ -n "$RECOVERY_SEED" ]]; then
  # The seed is the sole source authority.  Publish it before emergency
  # normalization so a half-installed unit can never replace its provenance.
  bind_source_contract
  run_emergency --stop-and-arm
  verify_block
  assert_inactive_boundaries
  old_gate_observer verify-release-sentinel --path "$SENTINEL" \
    --release-sha "$RELEASE_SHA" >/dev/null
  sudo -u ubuntu flock --nonblock "$RELEASE_LOCK" true
  hold_external_source_lock bound
fi

# All network/dependency/CI work is an explicit pre-stop prerequisite.  The
# root-only stage is produced by the reviewed preparation job and is immutable
# during this invocation.
  for item in reviewed-tracked.sha256 reviewed-assets.sha256 ci-check-runs.json \
    ci-workflow-runs.json trading-state-backup.original \
    trading-state-backup.reviewed \
    reviewed-venv.json pip-freeze.txt \
    python-version.txt package-paths.txt \
    remove-one-confirmed-config-key.py remove-one-confirmed-config-key.spec.json \
    writer-inventory.json; do
  sudo test -f "$DEPLOY_STAGE/$item"
  sudo test ! -L "$DEPLOY_STAGE/$item"
done
sudo sh -eu -c 'cd "$1" && sha256sum -c reviewed-assets.sha256 >/dev/null' \
  sh "$DEPLOY_STAGE"
sudo jq -e --arg sha "$RELEASE_SHA" '
  type=="object" and keys==["backup_original_sha256","ci_checks_sha256",
    "ci_runs_sha256","prepare_tool_sha256","release_sha","required_checks",
    "required_workflows","reviewed_inputs","schema_version"] and
  .schema_version==1 and .release_sha==$sha and
  ([.backup_original_sha256,.ci_checks_sha256,.ci_runs_sha256,
    .prepare_tool_sha256,.reviewed_inputs[]]|
    all(type=="string" and test("^[0-9a-f]{64}$"))) and
  (.required_checks|type=="array" and length>0 and
    (length==(unique|length))) and
  (.required_workflows|type=="array" and length>0 and
    (length==(unique|length))) and
  (.required_checks+.required_workflows|all(type=="string" and length>0)) and
  (.reviewed_inputs|keys==["backup-script.patch",
    "remove-one-confirmed-config-key.py",
    "remove-one-confirmed-config-key.spec.json","writer-inventory.json"])
' "$DEPLOY_STAGE/prepare-request.json" >/dev/null
test "$(sudo jq -r .prepare_tool_sha256 "$DEPLOY_STAGE/prepare-request.json")" = \
  "$(sha256 "$RELEASE_TRADING/prepare_deployment.py")"
test "$(sudo jq -r .ci_checks_sha256 "$DEPLOY_STAGE/prepare-request.json")" = \
  "$(sudo sha256sum "$DEPLOY_STAGE/ci-check-runs.json"|awk '{print $1}')"
test "$(sudo jq -r .ci_runs_sha256 "$DEPLOY_STAGE/prepare-request.json")" = \
  "$(sudo sha256sum "$DEPLOY_STAGE/ci-workflow-runs.json"|awk '{print $1}')"
for prepared_input in backup-script.patch remove-one-confirmed-config-key.py \
    remove-one-confirmed-config-key.spec.json writer-inventory.json; do
  test "$(sudo jq -r --arg name "$prepared_input" \
    '.reviewed_inputs[$name]' "$DEPLOY_STAGE/prepare-request.json")" = \
    "$(sudo sha256sum "$DEPLOY_STAGE/$prepared_input"|awk '{print $1}')"
done
test "$(sudo jq -r .backup_original_sha256 "$DEPLOY_STAGE/prepare-request.json")" = \
  "$(sudo sha256sum "$DEPLOY_STAGE/trading-state-backup.original"|awk '{print $1}')"
sudo sh -eu -c 'cd "$1" && sha256sum -c "$2" >/dev/null' \
  sh "$RELEASE_ROOT" "$DEPLOY_STAGE/reviewed-tracked.sha256"
sudo jq -e --arg sha "$RELEASE_SHA" \
  --slurpfile request "$DEPLOY_STAGE/prepare-request.json" \
  '. as $e |
   type=="object" and
   ($e.total_count|type=="number" and floor==. and .>0) and
   ($e.check_runs|type=="array" and length==$e.total_count) and
   ([.check_runs[].head_sha]|all(.==$sha)) and
   ([.check_runs[]|select(.status!="completed" or .conclusion!="success")]|length==0) and
   ($request[0].required_checks - [.check_runs[].name] | length==0)' \
  "$DEPLOY_STAGE/ci-check-runs.json" >/dev/null
sudo jq -e --arg sha "$RELEASE_SHA" \
  --slurpfile request "$DEPLOY_STAGE/prepare-request.json" '
  type=="array" and length>0 and length<100 and
  ([.[].headSha]|all(.==$sha)) and
  ((sort_by(.workflowName,.createdAt,.databaseId)|group_by(.workflowName)|map(last)) as $latest |
   ([$latest[]|select(.status!="completed" or .conclusion!="success")]|length==0) and
   ($request[0].required_workflows - [$latest[].workflowName] | length==0))
' "$DEPLOY_STAGE/ci-workflow-runs.json" >/dev/null
sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" \
  validate-writer-inventory --file "$DEPLOY_STAGE/writer-inventory.json" \
  --release-sha "$RELEASE_SHA"

PREFLIGHT=(
  "deploy_sha256=$(sha256 "$SELF")"
  "emergency_sha256=$(sha256 "$EMERGENCY")"
  "evidence_helper_sha256=$(sha256 "$EVIDENCE_HELPER")"
  "attempt_helper_sha256=$(sha256 "$ATTEMPT_HELPER")"
  "old_gate_helper_sha256=$(sha256 "$OLD_GATE_HELPER")"
  "tracked_manifest_sha256=$(sudo sha256sum "$DEPLOY_STAGE/reviewed-tracked.sha256"|awk '{print $1}')"
  "assets_manifest_sha256=$(sudo sha256sum "$DEPLOY_STAGE/reviewed-assets.sha256"|awk '{print $1}')"
  "ci_checks_sha256=$(sudo sha256sum "$DEPLOY_STAGE/ci-check-runs.json"|awk '{print $1}')"
  "ci_runs_sha256=$(sudo sha256sum "$DEPLOY_STAGE/ci-workflow-runs.json"|awk '{print $1}')"
)
write_request preflight "$ATTEMPT_STAGE/preflight.approval.json" "${PREFLIGHT[@]}"
wait_approval preflight "$ATTEMPT_STAGE/preflight.approval.json" "${PREFLIGHT[@]}"
readonly REVIEWED_DEPLOY_SHA="${PREFLIGHT[0]#*=}"
readonly REVIEWED_EMERGENCY_SHA="${PREFLIGHT[1]#*=}"
readonly REVIEWED_EVIDENCE_SHA="${PREFLIGHT[2]#*=}"
readonly REVIEWED_ATTEMPT_SHA="${PREFLIGHT[3]#*=}"
readonly REVIEWED_OLD_GATE_SHA="${PREFLIGHT[4]#*=}"
verify_reviewed_materials
EXPOSURE_TMP=$(mktemp)
credential_exposure "$SOURCE_DATA/config.json" >"$EXPOSURE_TMP"
jq -e '
  type=="object" and
  keys==["account_domain","current_dingtalk_matches_exposed_history",
    "current_okx_key_matches_exposed_history","dingtalk_configured",
    "incident_commit"] and
  .account_domain=="live" and
  .incident_commit=="38ac63646d2e18ba9d238856b124594b4691f252" and
  .current_okx_key_matches_exposed_history==false and
  .current_dingtalk_matches_exposed_history==false and
  (.dingtalk_configured|type=="boolean")
' "$EXPOSURE_TMP" >/dev/null
readonly EXPOSURE_EVIDENCE="$ATTEMPT_STAGE/public-history-exposure.json"
sudo test ! -e "$EXPOSURE_EVIDENCE"
sudo test ! -L "$EXPOSURE_EVIDENCE"
sudo install -o root -g root -m 0600 "$EXPOSURE_TMP" "$EXPOSURE_EVIDENCE"
rm -f -- "$EXPOSURE_TMP"
EXPOSURE_EVIDENCE_SHA=$(sudo sha256sum "$EXPOSURE_EVIDENCE" | awk '{print $1}')
readonly EXPOSURE_EVIDENCE_SHA
# Reject every already-known migration/config blocker while the old runner is
# still untouched.  The same checks run again against the stopped copy below
# so this early screen cannot hide a race.
if [[ "$SOURCE_DATA_STATE" = requires_migration ]]; then
  sudo env -i PATH=/usr/bin:/bin "$PYTHON" -B -E "$CLEANUP" \
    --check --release-sha "$RELEASE_SHA" \
    --config "$SOURCE_DATA/config.json" --spec "$SPEC"
  (
    # The reviewed spec contains only hashes/key path, never credentials.  Its
    # root-owned 0644 /run copy is readable but not writable by the live runner;
    # the subshell EXIT trap removes it on both success and failure.
    PREVIEW_SPEC=$(sudo mktemp /run/trading-cleanup-preview.XXXXXX)
    trap 'sudo rm -f -- "$PREVIEW_SPEC"' EXIT
    sudo install -o root -g root -m 0644 "$SPEC" "$PREVIEW_SPEC"
    test "$(sudo stat -c '%U:%G:%a:%h' "$PREVIEW_SPEC")" = root:root:644:1
    test "$(sudo sha256sum "$PREVIEW_SPEC" | awk '{print $1}')" = \
      "$(sudo sha256sum "$SPEC" | awk '{print $1}')"
    sudo -u ubuntu env -i PATH=/usr/bin:/bin "$PYTHON" -B -E \
      "$RELEASE_TRADING/migrate_single_strategy.py" \
      --data-dir "$SOURCE_DATA" --cleanup-spec "$PREVIEW_SPEC" \
      --release-sha "$RELEASE_SHA" >/dev/null
  )
else
  sudo -u ubuntu env -i PATH=/usr/bin:/bin "$PYTHON" -B -E \
    "$RELEASE_TRADING/migrate_single_strategy.py" \
    --data-dir "$SOURCE_DATA" >/dev/null
fi
WRITER_FREEZE=(
  "inventory_sha256=$(sudo sha256sum "$DEPLOY_STAGE/writer-inventory.json"|awk '{print $1}')"
  "deploy_sha256=$REVIEWED_DEPLOY_SHA"
  "emergency_sha256=$REVIEWED_EMERGENCY_SHA"
  "completed_schedule_slot=$SLOT"
  "exchange_ui_manual_actions_frozen=true"
  "all_other_api_credentials_and_consumers_frozen=true"
  "host_process_inventory_complete=true"
  "runtime_system_api_only_acknowledged=true"
  "replacement_identity_limitation_acknowledged=true"
  "history_okx_keys_revoked_and_activity_audited=true"
  "history_dingtalk_webhooks_rotated=true"
  "credential_exposure_sha256=$EXPOSURE_EVIDENCE_SHA"
)
write_request writer_freeze "$ATTEMPT_STAGE/writer-freeze.approval.json" \
  "${WRITER_FREEZE[@]}"
wait_approval writer_freeze "$ATTEMPT_STAGE/writer-freeze.approval.json" \
  "${WRITER_FREEZE[@]}"

# G0 has one active-runner boundary: the exact kernel FLOCK holder must expose
# the reviewed trade-lock-no-open-v1 endpoint.  That endpoint takes the same
# in-process trade lock as every open path, drains accepted opens, fsyncs the
# runner's actual sentinel, and only then releases the lock.  API Key
# permissions are never changed as part of deployment.
if [[ -n "$RECOVERY_SEED" ]]; then
  sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" source-contract \
    --attempt-dir "$ATTEMPT_STAGE" --release-sha "$RELEASE_SHA" \
    --attempt-id "$ATTEMPT_ID" --driver-sha256 "$REVIEWED_DEPLOY_SHA" \
    >/dev/null
  test "$(sudo sha256sum "$SOURCE_CONTRACT" | awk '{print $1}')" = \
    "$SOURCE_CONTRACT_SHA"
fi
OLD_GATE_EVIDENCE="$ATTEMPT_STAGE/old-no-open-boundary.json"
OLD_GATE_ARM_INTENT="$ATTEMPT_STAGE/old-no-open-arm-intent.json"
sudo test ! -e "$OLD_GATE_EVIDENCE"
sudo test ! -L "$OLD_GATE_EVIDENCE"
sudo test ! -e "$OLD_GATE_ARM_INTENT"
sudo test ! -L "$OLD_GATE_ARM_INTENT"
if [[ -n "$RECOVERY_SEED" ]]; then
  # A reviewed recovery seed is the only inactive entry.  Re-prove every live
  # invariant instead of pretending that an old active runner still exists.
  test "$(systemctl show trading.service -p ActiveState --value)" = inactive
  verify_block
  assert_inactive_boundaries
  sudo test -f "$RELEASE_LOCK"
  sudo test ! -L "$RELEASE_LOCK"
  sudo -u ubuntu flock --nonblock "$RELEASE_LOCK" true || \
    die 'recovery seed exists but the runner lock is still held'
else
  OLD_MAIN_PID=$(systemctl show trading.service -p MainPID --value)
  [[ "$OLD_MAIN_PID" =~ ^[1-9][0-9]*$ ]]
  test "$(systemctl show trading.service -p ActiveState --value)" = active
  test "$(sudo realpath -e -- "/proc/$OLD_MAIN_PID/cwd")" = "$SOURCE_TRADING"
  OLD_CGROUP=$(systemctl show trading.service -p ControlGroup --value)
  [[ "$OLD_CGROUP" = /* && -f "/sys/fs/cgroup$OLD_CGROUP/cgroup.procs" ]]
  grep -Fxq "$OLD_MAIN_PID" "/sys/fs/cgroup$OLD_CGROUP/cgroup.procs"
  if sudo -u ubuntu flock --nonblock "$OLD_LOCK" true; then
    die 'old runner is active but does not hold its attested runner lock'
  else
    test "$?" -eq 1
  fi

  # Compatibility and identity are proven before the first deployment
  # mutation.  No read-only credential fallback is permitted.
  old_gate_client probe-handshake \
    --runner-lock "$OLD_LOCK" --service-cgroup "$OLD_CGROUP" \
    --expected-cwd "$SOURCE_TRADING" --current-data "$SOURCE_DATA" \
    >/dev/null || \
    die 'old runner lacks the reviewed same-lock no-open handshake; production left running'
fi

if [[ -n "$RECOVERY_SEED" ]]; then
  BOUNDARY_TMP=$(mktemp)
  old_gate_observer verify-release-sentinel --path "$SENTINEL" \
    --release-sha "$RELEASE_SHA" >"$BOUNDARY_TMP"
  publish_attempt_artifact old-no-open-boundary.json "$BOUNDARY_TMP"
  rm -f -- "$BOUNDARY_TMP"
  OLD_GATE_MODE='recovery_inactive'
  test "$(completed_slot "$SOURCE_DATA" "$SLOT")" = "$SLOT" || \
    die 'recovery source changed its frozen completed schedule slot'
else
  ARM_INTENT_TMP=$(mktemp)
  printf '%s\n' \
    "{\"kind\":\"old_runner_no_open_arm_intent\",\"release_sha\":\"$RELEASE_SHA\",\"schema_version\":1}" \
    >"$ARM_INTENT_TMP"
  publish_attempt_artifact old-no-open-arm-intent.json "$ARM_INTENT_TMP"
  rm -f -- "$ARM_INTENT_TMP"
  old_gate_observer verify-arm-intent \
    --evidence "$OLD_GATE_ARM_INTENT" --release-sha "$RELEASE_SHA" \
    >/dev/null

  OLD_GATE_NONCE=$(openssl rand -hex 32)
  BOUNDARY_TMP=$(mktemp)
  if ! old_gate_client establish-handshake \
      --release-sha "$RELEASE_SHA" --nonce "$OLD_GATE_NONCE" \
      --runner-lock "$OLD_LOCK" --service-cgroup "$OLD_CGROUP" \
      --expected-cwd "$SOURCE_TRADING" --current-data "$SOURCE_DATA" \
      >"$BOUNDARY_TMP"; then
    rm -f -- "$BOUNDARY_TMP"
    die 'old runner same-lock no-open handshake failed; recovery is required'
  fi
  OLD_GATE_MODE='runtime_sentinel'

  # The network handshake can cross a scheduler boundary.  Re-evaluate the
  # slot only after opens are durably blocked; any mismatch now contains the
  # runner instead of resuming normal trading under an ambiguous schedule.
  FINAL_LIVE_SLOT=$(completed_slot "$SOURCE_DATA") || \
    die 'completed schedule slot advanced during no-open handshake'
  test "$FINAL_LIVE_SLOT" = "$SLOT" || \
    die 'completed schedule slot advanced during no-open handshake'
  publish_attempt_artifact old-no-open-boundary.json "$BOUNDARY_TMP"
  rm -f -- "$BOUNDARY_TMP"
  old_gate_client verify-handshake --evidence "$OLD_GATE_EVIDENCE" \
    --runner-lock "$OLD_LOCK" --service-cgroup "$OLD_CGROUP" \
    --expected-cwd "$SOURCE_TRADING" --current-data "$SOURCE_DATA" \
    >/dev/null
fi

# The old runner is now durably no-open (or this is an already-inactive
# recovery entry).  Install fail-safe handling only at this boundary: before
# it, a failed intent/handshake must leave normal production untouched or its
# newly fsynced sentinel in place, never enter a block-before-stop emergency.
# From here onward every catchable failure may safely hard-contain, and SIGKILL
# leaves an independently durable no-open boundary.
trap 'fail_safe $?' ERR EXIT
trap 'fail_safe 129' HUP
trap 'fail_safe 130' INT TERM
run_emergency --install-block-only
verify_block
OLD_GATE_EVIDENCE_SHA=$(sudo sha256sum "$OLD_GATE_EVIDENCE" | awk '{print $1}')
readonly OLD_GATE_MODE OLD_GATE_EVIDENCE OLD_GATE_EVIDENCE_SHA

if [[ -n "$RECOVERY_SEED" ]]; then
  # Recovery entered with the reviewed release already stopped, blocked and
  # sentinel-protected.  Recheck that state; do not depend on damaged old unit
  # metadata or invent a second stop transition.
  assert_inactive_boundaries
  old_gate_observer verify-release-sentinel --path "$SENTINEL" \
    --release-sha "$RELEASE_SHA" >/dev/null
  sudo -u ubuntu flock --nonblock "$RELEASE_LOCK" true
else
  run_emergency --graceful-stop-and-arm "$OLD_GATE_MODE" "$OLD_GATE_EVIDENCE"
fi
if [[ -n "$RECOVERY_SEED" ]]; then
  hold_external_source_lock bound
else
  # No immutable source contract is published from a live mutable tree.  The
  # persistent block and stopped runner make this lease acquisition stable;
  # the contract below then records the exact held runner-lock inode.
  hold_external_source_lock unbound
fi
assert_inactive_boundaries
if [[ -z "$RECOVERY_SEED" ]]; then
  test "$(completed_slot "$SOURCE_DATA")" = "$SLOT" ||
    die 'current completed schedule slot changed while stopping; recovery is required'
fi

# Bind the exact stopped and, when external, exclusively locked source.  A
# crash before this write is recovered by emergency-stop's read-only source
# capture; a stale live-tree contract can therefore never deadlock recovery.
bind_source_contract
readonly SOURCE_CONTRACT_SHA
validate_external_source_lock bound
advance_phase PREPARED G0 \
  "old_gate_mode=$OLD_GATE_MODE" \
  "old_gate_evidence_sha256=$OLD_GATE_EVIDENCE_SHA" \
  "source_contract_sha256=$SOURCE_CONTRACT_SHA"

BACKUP_ROOT="/var/backups/trading/$(date -u +%Y%m%dT%H%M%SZ)-$SHORT_SHA-$ATTEMPT_ID"
sudo test ! -e "$BACKUP_ROOT"
sudo test ! -L "$BACKUP_ROOT"
sudo install -d -o root -g root -m 0700 "$BACKUP_ROOT"
sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" assert-no-mounts \
  --path "$SOURCE_DATA" >/dev/null
validate_external_source_lock bound
# Deployment never mutates the live checkout or stopped source code, so one
# state backup is the complete rollback payload; duplicate code/repo archives
# only increase outage time and disk pressure.
sudo tar --acls --xattrs --numeric-owner --one-file-system \
  -C "$(dirname -- "$SOURCE_DATA")" \
  -cpf "$BACKUP_ROOT/current-data.tar" "$(basename -- "$SOURCE_DATA")"
validate_external_source_lock bound
git -C "$LIVE_REPO" rev-parse HEAD | sudo tee "$BACKUP_ROOT/live-head.txt" >/dev/null
git -C "$LIVE_REPO" status --porcelain=v1 --untracked-files=all |
  sudo tee "$BACKUP_ROOT/live-status.txt" >/dev/null
sudo systemctl cat trading.service |
  sudo tee "$BACKUP_ROOT/trading.service.txt" >/dev/null
sudo systemctl cat trading-mem-monitor.service |
  sudo tee "$BACKUP_ROOT/trading-mem-monitor.service.txt" >/dev/null
sudo systemctl cat trading-state-backup.timer trading-state-backup.service |
  sudo tee "$BACKUP_ROOT/trading-state-backup.units.txt" >/dev/null
sudo systemctl cat cloudflared.service |
  sudo tee "$BACKUP_ROOT/cloudflared.service.txt" >/dev/null
sudo cp --archive --no-dereference /usr/local/sbin/trading-state-backup \
  "$BACKUP_ROOT/trading-state-backup"
sudo sh -eu -c 'cd "$1" && sha256sum current-data.tar live-head.txt live-status.txt trading.service.txt trading-mem-monitor.service.txt trading-state-backup.units.txt cloudflared.service.txt trading-state-backup > SHA256SUMS' \
  sh "$BACKUP_ROOT"
sudo sh -eu -c 'cd "$1" && sha256sum -c SHA256SUMS >/dev/null' sh "$BACKUP_ROOT"
sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-paths \
  --path "$BACKUP_ROOT/current-data.tar" \
  --path "$BACKUP_ROOT/live-head.txt" \
  --path "$BACKUP_ROOT/live-status.txt" \
  --path "$BACKUP_ROOT/trading.service.txt" \
  --path "$BACKUP_ROOT/trading-mem-monitor.service.txt" \
  --path "$BACKUP_ROOT/trading-state-backup.units.txt" \
  --path "$BACKUP_ROOT/cloudflared.service.txt" \
  --path "$BACKUP_ROOT/trading-state-backup" \
  --path "$BACKUP_ROOT/SHA256SUMS" --path "$BACKUP_ROOT" \
  --path /var/backups/trading --path /var/backups >/dev/null

if [[ "$SOURCE_DATA" != "$RUNTIME_ROOT" ]]; then
validate_external_source_lock bound
test "$(sudo stat -c '%d:%i' "$SOURCE_DATA")" != \
  "$(sudo stat -c '%d:%i' "$RUNTIME_ROOT")" || \
  die 'source and candidate runtime are the same filesystem object'
sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" assert-no-mounts \
  --path "$SOURCE_DATA" --path "$RUNTIME_ROOT" >/dev/null
test "$(sudo stat -c '%U:%G:%a' "$RUNTIME_ROOT")" = ubuntu:ubuntu:710
STATE_RSYNC_ARGS=(--archive --checksum --delete --delete-delay \
  --one-file-system --no-owner --no-group --no-perms --omit-dir-times \
  --include='/config.json*' --include='/trade_state.json*' \
  --include='/closed_trades_archive*.json*' --include='/stop_loss_dates.json*' \
  --include='/daily_equity.json*' --include='/equity_history.json*' \
  --include='/equity_ticks.json*' --include='/peak_equity.json*' \
  --include='/qiusuo_index.json*' --include='/.trading_data_owner.json*' \
  --include='/.okx_legacy_migration_complete.json*' \
  --include='/.equity_sync_journal.json*' \
  --include='/.single_strategy_migration_journal.json*' \
  --include='/data/' --include='/data/***' --exclude='*')
# The stopped source is authoritative.  --checksum defeats rsync's
# size+mtime quick check; --delete removes only included candidate residue,
# including a crashed prior migration journal/backups.  Excluded deployment
# sentinel, lock and gate evidence are deliberately preserved.
sudo -u ubuntu env -i PATH=/usr/bin:/bin rsync \
  "${STATE_RSYNC_ARGS[@]}" "$SOURCE_DATA/" "$RUNTIME_ROOT/"
validate_external_source_lock bound
test "$(sudo stat -c '%U:%G:%a' "$RUNTIME_ROOT")" = ubuntu:ubuntu:710
RSYNC_DRIFT=$(sudo -u ubuntu env -i PATH=/usr/bin:/bin rsync \
  --dry-run --itemize-changes \
  "${STATE_RSYNC_ARGS[@]}" "$SOURCE_DATA/" "$RUNTIME_ROOT/")
[[ -z "$RSYNC_DRIFT" ]] || die 'candidate state is not an exact source snapshot'
validate_external_source_lock bound
sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-runtime-tree \
  --release-sha "$RELEASE_SHA" --attempt-id "$ATTEMPT_ID" \
  --gate-state active >/dev/null
fi
test "$(sudo stat -c '%U:%G:%a' "$CONFIG")" = ubuntu:ubuntu:600
test "$(sudo stat -c '%U:%G:%a' "$STATE")" = ubuntu:ubuntu:600

if [[ "$SOURCE_DATA_STATE" = requires_migration ]]; then
  sudo env -i PATH=/usr/bin:/bin "$PYTHON" -B -E "$CLEANUP" \
    --check --release-sha "$RELEASE_SHA" --config "$CONFIG" --spec "$SPEC"
  sudo env -i PATH=/usr/bin:/bin "$PYTHON" -B -E "$CLEANUP" \
    --apply --release-sha "$RELEASE_SHA" --config "$CONFIG" --spec "$SPEC" \
    --audit "$ATTEMPT_STAGE/confirmed-config-cleanup.audit.json"
  sudo env -i PATH=/usr/bin:/bin "$PYTHON" -B -E "$CLEANUP" \
    --verify-applied --release-sha "$RELEASE_SHA" --config "$CONFIG" \
    --spec "$SPEC" --audit "$ATTEMPT_STAGE/confirmed-config-cleanup.audit.json"
fi

MIGRATION_REPORT="$ATTEMPT_STAGE/migration-dry-run.txt"
MIGRATION_TMP=$(mktemp)
sudo -u ubuntu env -i PATH=/usr/bin:/bin "$PYTHON" -B -E \
  "$RELEASE_TRADING/migrate_single_strategy.py" --data-dir "$DATA_DIR" >"$MIGRATION_TMP"
sudo install -o root -g root -m 0600 "$MIGRATION_TMP" "$MIGRATION_REPORT"
rm -f -- "$MIGRATION_TMP"
MIGRATION_NONCE=$(openssl rand -hex 32)
MIGRATION_REPORT_SHA=$(sudo sha256sum "$MIGRATION_REPORT"|awk '{print $1}')
MIGRATION_CONFIG_SHA=$(sudo sha256sum "$CONFIG"|awk '{print $1}')
MIGRATION_STATE_SHA=$(sudo sha256sum "$STATE"|awk '{print $1}')
MIGRATION_SENTINEL_ID=$(identity "$SENTINEL")
MIGRATION=(
  "report_sha256=$MIGRATION_REPORT_SHA"
  "config_sha256=$MIGRATION_CONFIG_SHA"
  "state_sha256=$MIGRATION_STATE_SHA"
  "sentinel_identity=$MIGRATION_SENTINEL_ID"
  "request_nonce=$MIGRATION_NONCE"
)
write_request migration "$ATTEMPT_STAGE/migration.approval.json" "${MIGRATION[@]}"
wait_approval migration "$ATTEMPT_STAGE/migration.approval.json" "${MIGRATION[@]}"
test "$(sudo sha256sum "$MIGRATION_REPORT"|awk '{print $1}')" = "$MIGRATION_REPORT_SHA"
test "$(sudo sha256sum "$CONFIG"|awk '{print $1}')" = "$MIGRATION_CONFIG_SHA"
test "$(sudo sha256sum "$STATE"|awk '{print $1}')" = "$MIGRATION_STATE_SHA"
test "$(identity "$SENTINEL")" = "$MIGRATION_SENTINEL_ID"
sudo -u ubuntu env -i PATH=/usr/bin:/bin "$PYTHON" -B -E \
  "$RELEASE_TRADING/migrate_single_strategy.py" --data-dir "$DATA_DIR" --apply
sudo -u ubuntu env -i PATH=/usr/bin:/bin "$PYTHON" -B -E \
  "$RELEASE_TRADING/migrate_single_strategy.py" --data-dir "$DATA_DIR" >/dev/null
archive_migration_artifacts
test "$(completed_slot "$DATA_DIR" "$SLOT")" = "$SLOT" ||
  die 'migration changed or crossed the completed schedule slot'

sudo test -f /usr/local/sbin/trading-state-backup
sudo test ! -L /usr/local/sbin/trading-state-backup
test "$(sudo stat -c '%U:%G' /usr/local/sbin/trading-state-backup)" = root:root
CURRENT_BACKUP_SHA=$(sudo sha256sum /usr/local/sbin/trading-state-backup|awk '{print $1}')
ORIGINAL_BACKUP_SHA=$(sudo sha256sum \
  "$DEPLOY_STAGE/trading-state-backup.original"|awk '{print $1}')
REVIEWED_BACKUP_SHA=$(sudo sha256sum \
  "$DEPLOY_STAGE/trading-state-backup.reviewed"|awk '{print $1}')
[[ "$CURRENT_BACKUP_SHA" = "$ORIGINAL_BACKUP_SHA" ||
   "$CURRENT_BACKUP_SHA" = "$REVIEWED_BACKUP_SHA" ]]
BACKUP_SCRIPT_TMP=$(sudo mktemp /usr/local/sbin/.trading-state-backup.XXXXXX)
sudo install -o root -g root -m 0755 \
  "$DEPLOY_STAGE/trading-state-backup.reviewed" "$BACKUP_SCRIPT_TMP"
sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-paths \
  --path "$BACKUP_SCRIPT_TMP" >/dev/null
sudo mv --no-target-directory "$BACKUP_SCRIPT_TMP" \
  /usr/local/sbin/trading-state-backup
test "$(sudo stat -c '%U:%G:%a:%h' /usr/local/sbin/trading-state-backup)" = \
  root:root:755:1
test "$(sudo sha256sum /usr/local/sbin/trading-state-backup|awk '{print $1}')" = \
     "$REVIEWED_BACKUP_SHA"
sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-paths \
  --path /usr/local/sbin/trading-state-backup --path /usr/local/sbin >/dev/null
assert_protected_dir /etc/systemd/system/trading.service.d
ENV_TMP=$(mktemp)
printf '%s\n' "TRADING_RUNNER_LOCK_FILE=$RELEASE_LOCK" \
  "TRADING_MAINTENANCE_SENTINEL=$SENTINEL" \
  "TRADING_CONFIG_FILE=$CONFIG" \
  "TRADING_DATA_DIR=$DATA_DIR" >"$ENV_TMP"
safe_install_managed "$ENV_TMP" "$RELEASE_ENV" 0600 release_env
rm -f -- "$ENV_TMP"
DROPIN_TMP=$(mktemp)
printf '%s\n' '[Service]' 'Type=simple' 'User=ubuntu' 'Group=ubuntu' \
  'DynamicUser=false' "WorkingDirectory=$RELEASE_TRADING" \
  'ExecCondition=' 'ExecStartPre=' 'ExecStart=' \
  "ExecStart=$PYTHON -B -E -m gunicorn -c gunicorn.conf.py wsgi:application" \
  'ExecStartPost=' 'ExecReload=' 'ExecStop=' 'ExecStopPost=' \
  'Environment=' 'EnvironmentFile=' 'EnvironmentFile=/etc/trading.env' \
  "EnvironmentFile=$RELEASE_ENV" 'PassEnvironment=' 'PAMName=' \
  'RootDirectory=' 'RootImage=' 'BindPaths=' 'BindReadOnlyPaths=' \
  'TemporaryFileSystem=' 'MountImages=' 'ExtensionImages=' \
  'ExtensionDirectories=' \
  'ExecSearchPath=/usr/sbin:/usr/bin:/sbin:/bin' \
  'LoadCredential=' 'LoadCredentialEncrypted=' \
  'SetCredential=' 'SetCredentialEncrypted=' \
  'UnsetEnvironment=' "UnsetEnvironment=$UNSET_ENV" \
  'UMask=0077' 'KillMode=control-group' 'Restart=on-failure' \
  'RestartSec=10s' 'TimeoutStopSec=920s' >"$DROPIN_TMP"
safe_install_managed "$DROPIN_TMP" "$RELEASE_DROPIN" 0644 release_dropin
printf '%s\n' '[Service]' 'Type=simple' 'DynamicUser=true' \
  'SupplementaryGroups=ubuntu' "WorkingDirectory=$RELEASE_TRADING" \
  'ExecCondition=' 'ExecStartPre=' 'ExecStart=' \
  "ExecStart=$PYTHON -B -E mem_monitor.py" \
  'ExecStartPost=' 'ExecReload=' 'ExecStop=' 'ExecStopPost=' \
  'Environment=' 'EnvironmentFile=' \
  'EnvironmentFile=/etc/trading-mem-monitor.env' \
  "Environment=TRADING_DATA_DIR=$DATA_DIR" \
  "Environment=TRADING_CONFIG_FILE=$CONFIG" \
  'Environment=TRADING_MEM_MONITOR_LOG=/var/log/trading-mem-monitor/mem_monitor.log' \
  'PassEnvironment=' 'PAMName=' \
  'RootDirectory=' 'RootImage=' 'BindPaths=' 'BindReadOnlyPaths=' \
  'TemporaryFileSystem=' 'MountImages=' 'ExtensionImages=' \
  'ExtensionDirectories=' \
  'ExecSearchPath=/usr/sbin:/usr/bin:/sbin:/bin' \
  'LoadCredential=' 'LoadCredentialEncrypted=' \
  'SetCredential=' 'SetCredentialEncrypted=' \
  'UnsetEnvironment=' "UnsetEnvironment=$UNSET_ENV" \
  'InaccessiblePaths=' "InaccessiblePaths=-$CONFIG" \
  'UMask=0077' 'KillMode=control-group' 'Restart=on-failure' \
  'RestartSec=10s' >"$DROPIN_TMP"
if ! sudo test -e /etc/systemd/system/trading-mem-monitor.service.d; then
  sudo install -d -o root -g root -m 0755 \
    /etc/systemd/system/trading-mem-monitor.service.d
fi
safe_install_managed "$DROPIN_TMP" "$MONITOR_DROPIN" 0644 monitor_dropin
rm -f -- "$DROPIN_TMP"
# Preparation already rejected a mutable release tree. Deployment re-proves the
# executable subtree and never recursively mutates .git or a shared inode.
sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" assert-no-mounts \
  --path "$RELEASE_TRADING" >/dev/null
test -z "$(sudo find "$RELEASE_TRADING" -xdev ! -user root -print -quit)"
test -z "$(sudo find "$RELEASE_TRADING" -xdev -perm /022 -print -quit)"
test -z "$(sudo find "$RELEASE_TRADING" -xdev -type f ! -links 1 -print -quit)"
test -z "$(sudo find "$RELEASE_TRADING" -xdev \
  ! -type d ! -type f ! -type l -print -quit)"
test "$(sudo stat -c '%U:%G:%a' "$RELEASE_TRADING")" = root:root:755
test "$(sudo stat -c '%U:%G:%a' "$RELEASE_TRADING/main.py")" = root:root:644
test "$(sudo stat -c '%U:%G' "$RELEASE_TRADING/.venv/bin/python")" = root:root
sudo systemctl daemon-reload
sudo systemd-analyze verify trading.service trading-mem-monitor.service >/dev/null
prove_effective_units
verify_block

assert_inactive_boundaries
RUNTIME_READY_RESULT=$(sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" \
  sync-runtime-tree --release-sha "$RELEASE_SHA" --attempt-id "$ATTEMPT_ID" \
  --gate-state active)
RUNTIME_READY_TREE_SHA=$(jq -r .tree_sha256 <<<"$RUNTIME_READY_RESULT")
[[ "$RUNTIME_READY_TREE_SHA" =~ ^[0-9a-f]{64}$ ]]
advance_phase G0 RUNTIME_READY \
  "slot=$SLOT" "runtime_tree_sha256=$RUNTIME_READY_TREE_SHA"
for _visibility_sample in 1 2 3; do
  assert_inactive_boundaries
  sleep 1
done
# Two complete read-only Q0..Q2 probes drain requests already accepted by the
# killed legacy writer.  Only the second stable interval may precede durable T0.
for probe in 1 2; do
  PROBE_TMP=$(mktemp)
  gate quiesce --config "$CONFIG" --data-dir "$DATA_DIR" \
    --release-sha "$RELEASE_SHA" >"$PROBE_TMP"
  sudo install -o root -g root -m 0600 "$PROBE_TMP" \
    "$ATTEMPT_STAGE/quiescence-$probe.json"
  rm -f -- "$PROBE_TMP"
  sudo jq -e '.q0_ms>0 and .t1_ms>=.q0_ms and .t2_ms>=.t1_ms and
    .history_verified_through_ms==.t2_ms' \
    "$ATTEMPT_STAGE/quiescence-$probe.json" >/dev/null
  if [[ "$probe" = 1 ]]; then sleep 5; fi
done
test "$(sudo jq -r .t2_ms "$ATTEMPT_STAGE/quiescence-1.json")" -le \
     "$(sudo jq -r .q0_ms "$ATTEMPT_STAGE/quiescence-2.json")"
QUIESCENCE_SHA=$(sudo sha256sum "$ATTEMPT_STAGE/quiescence-2.json" | awk '{print $1}')
advance_phase RUNTIME_READY QUIESCED \
  "slot=$SLOT" "quiescence_sha256=$QUIESCENCE_SHA"
# T0 is created only after the already-completed pre-stop slot and a second
# stable visibility window.  The stopped deployment never waits for or runs a
# formal daily-check slot.
gate baseline --config "$CONFIG" --data-dir "$DATA_DIR" \
  --release-sha "$RELEASE_SHA" \
  --history-start-ms "$(sudo jq -r .t2_ms "$ATTEMPT_STAGE/quiescence-2.json")"
SENTINEL_ID=$(identity "$SENTINEL")
BASELINE_ID=$(identity "$BASELINE")
advance_phase QUIESCED T0 \
  "sentinel_identity=$SENTINEL_ID" "baseline_identity=$BASELINE_ID"
if sudo ss -H -ltn 'sport = :5000' | grep -q .; then
  die 'formal port 5000 is already occupied'
fi
verify_reviewed_materials
prove_effective_units
verify_block
sudo install -o root -g root -m 0400 /dev/null "$START_AUTH"
sudo systemctl start trading.service
test "$(identity "$SENTINEL")" = "$SENTINEL_ID"
test "$(identity "$BASELINE")" = "$BASELINE_ID"
prove_formal_runner
check_local_health >/dev/null
check_maintenance_http_gate
sudo systemctl start trading-mem-monitor.service
sudo systemctl start --wait trading-state-backup.service
test "$(systemctl show trading-state-backup.service -p Result --value)" = success
sudo systemctl start trading-state-backup.timer
test "$(systemctl show trading-state-backup.timer -p ActiveState --value)" = active
test "$(systemctl show trading-mem-monitor.service -p ActiveState --value)" = active
prove_formal_runner
check_local_health >/dev/null
check_maintenance_http_gate

sudo systemctl start cloudflared.service
test "$(systemctl show cloudflared.service -p ActiveState --value)" = active
check_local_health >"/tmp/trading-health-$SHORT_SHA"
HEALTH_SHA=$(sha256 "/tmp/trading-health-$SHORT_SHA")
CONFIG_EXTERNAL_SHA=$(sudo sha256sum "$CONFIG" | awk '{print $1}')
TRADING_ENV_EXTERNAL_SHA=$(sudo sha256sum /etc/trading.env | awk '{print $1}')
sudo install -o root -g root -m 0600 "/tmp/trading-health-$SHORT_SHA" \
  "$ATTEMPT_STAGE/local-health.json"
rm -f -- "/tmp/trading-health-$SHORT_SHA"
EXTERNAL_NONCE=$(openssl rand -hex 32)
EXTERNAL=(
  "local_health_sha256=$HEALTH_SHA"
  "sentinel_identity=$SENTINEL_ID"
  "baseline_identity=$BASELINE_ID"
  "config_sha256=$CONFIG_EXTERNAL_SHA"
  "trading_env_sha256=$TRADING_ENV_EXTERNAL_SHA"
  "request_nonce=$EXTERNAL_NONCE"
)
write_request external_tunnel "$ATTEMPT_STAGE/external_tunnel.approval.json" \
  "${EXTERNAL[@]}"
wait_approval external_tunnel "$ATTEMPT_STAGE/external_tunnel.approval.json" \
  "${EXTERNAL[@]}"
test "$(sudo sha256sum "$ATTEMPT_STAGE/local-health.json"|awk '{print $1}')" = \
  "$HEALTH_SHA"
test "$(identity "$SENTINEL")" = "$SENTINEL_ID"
test "$(identity "$BASELINE")" = "$BASELINE_ID"
test "$(sudo sha256sum "$CONFIG"|awk '{print $1}')" = "$CONFIG_EXTERNAL_SHA"
test "$(sudo sha256sum /etc/trading.env|awk '{print $1}')" = \
  "$TRADING_ENV_EXTERNAL_SHA"
prove_formal_runner
check_local_health >/dev/null
check_maintenance_http_gate
# Reinstalling the block removes authorization before the second graceful
# drain. Formal and every auxiliary writer are silent before final verify.
run_emergency --graceful-stop-and-arm
sudo test ! -e "$START_AUTH"

# Final boundary: every writer is stopped while the formal-service block and
# no-open sentinel remain. Completion is verified before the formal restart.
assert_inactive_boundaries
sudo -u ubuntu env -i PATH=/usr/bin:/bin "$PYTHON" -B -E \
  "$RELEASE_TRADING/migrate_single_strategy.py" --data-dir "$DATA_DIR" >/dev/null
test "$(identity "$SENTINEL")" = "$SENTINEL_ID"
test "$(identity "$BASELINE")" = "$BASELINE_ID"
gate verify --config "$CONFIG" --data-dir "$DATA_DIR" --release-sha "$RELEASE_SHA"
verify_reviewed_materials
prove_effective_units
wait_approval writer_freeze "$ATTEMPT_STAGE/writer-freeze.approval.json" \
  "${WRITER_FREEZE[@]}"
FINAL_SLOT=$(completed_slot "$DATA_DIR")
test "$FINAL_SLOT" = "$SLOT"
verify_block
sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-runtime-tree \
  --release-sha "$RELEASE_SHA" --attempt-id "$ATTEMPT_ID" \
  --gate-state active >/dev/null
advance_phase T0 VALIDATED \
  "slot=$SLOT" "config_sha256=$CONFIG_EXTERNAL_SHA" \
  "trading_env_sha256=$TRADING_ENV_EXTERNAL_SHA"
gate seal --config "$CONFIG" --data-dir "$DATA_DIR" --release-sha "$RELEASE_SHA"
verify_block
sudo test -f "$SENTINEL"
sudo test -f "$COMPLETION"
test "$(identity "$BASELINE")" = "$BASELINE_ID"
COMPLETION_ID=$(identity "$COMPLETION")
COMPLETION_SHA=$(sudo sha256sum "$COMPLETION" | awk '{print $1}')
advance_phase VALIDATED SEALED \
  "completion_identity=$COMPLETION_ID" "completion_sha256=$COMPLETION_SHA"

# Restart the exact already-validated formal stack while sentinel and HTTP
# maintenance gate still denies every HTTP write.
sudo install -o root -g root -m 0400 /dev/null "$START_AUTH"
sudo systemctl start trading.service
prove_formal_runner
check_local_health >/dev/null
sudo systemctl start trading-mem-monitor.service
test "$(systemctl show trading-mem-monitor.service -p ActiveState --value)" = active
sudo systemctl start cloudflared.service
test "$(systemctl show cloudflared.service -p ActiveState --value)" = active
check_maintenance_http_gate
test "$(sudo sha256sum "$CONFIG"|awk '{print $1}')" = "$CONFIG_EXTERNAL_SHA"
test "$(sudo sha256sum /etc/trading.env|awk '{print $1}')" = \
  "$TRADING_ENV_EXTERNAL_SHA"
test "$(identity "$COMPLETION")" = "$COMPLETION_ID"

# Settle Persistent catch-up while the no-open sentinel is still authoritative.
# Keep start authorization until the timer and worker are quiescent: the backup
# worker is allowed to restart the formal service, and must not encounter the
# durable block without the matching authorization.
settle_backup_timer
quiesce_backup_timer

# Revoke the volatile authorization only after every backup restart source is
# quiet.  The already-running generation continues under the sentinel; any
# reboot, OOM restart or replacement generation is blocked through commit.
sudo rm -- "$START_AUTH"
verify_block
verify_loaded_start_block
prove_formal_runner
check_local_health >/dev/null
check_maintenance_http_gate
test "$(identity "$SENTINEL")" = "$SENTINEL_ID"
test "$(identity "$BASELINE")" = "$BASELINE_ID"
test "$(identity "$COMPLETION")" = "$COMPLETION_ID"
test "$(sudo sha256sum "$CONFIG"|awk '{print $1}')" = "$CONFIG_EXTERNAL_SHA"
test "$(sudo sha256sum /etc/trading.env|awk '{print $1}')" = \
  "$TRADING_ENV_EXTERNAL_SHA"
# Remove remote ingress before the final same-lock drain.  From this point to
# commit, only the reviewed local probes below can reach the formal runner.
sudo systemctl stop cloudflared.service
prove_unit_inactive cloudflared.service
prove_running_aux_unit trading-mem-monitor.service
wait_local_http_idle
FINAL_DRAIN=$(old_gate_client drain-maintenance-boundary \
  --release-sha "$RELEASE_SHA" --runner-lock "$RELEASE_LOCK" \
  --service-cgroup "$(systemctl show trading.service \
    -p ControlGroup --value)" \
  --expected-cwd "$RELEASE_TRADING" --current-data "$DATA_DIR")
FINAL_DRAIN_PID=$(jq -er \
  '.worker_pid | select(type=="number" and .>0)' <<<"$FINAL_DRAIN")
FINAL_DRAIN_SHA=$(printf '%s' "$FINAL_DRAIN" | sha256sum | awk '{print $1}')
prove_formal_runner
check_local_health >/dev/null
check_maintenance_http_gate
wait_local_http_idle
test "$(identity "$SENTINEL")" = "$SENTINEL_ID"
test "$(identity "$BASELINE")" = "$BASELINE_ID"
test "$(identity "$COMPLETION")" = "$COMPLETION_ID"
# Freeze the exact healthy service generation across the sentinel unlink.
# A frozen cgroup cannot run a replacement worker or submit an order; commit
# additionally binds the kernel FLOCK owner PID captured under that freeze.
sudo systemctl freeze trading.service
test "$(systemctl show trading.service -p FreezerState --value)" = frozen
FINAL_RUNNER_BINDING=$(old_gate_observer process-binding \
  --runner-lock "$RELEASE_LOCK" \
  --service-cgroup "$(systemctl show trading.service -p ControlGroup --value)" \
  --expected-cwd "$RELEASE_TRADING")
EXPECTED_RUNNER_PID=$(jq -er \
  '.worker_pid | select(type=="number" and .>0)' <<<"$FINAL_RUNNER_BINDING")
test "$EXPECTED_RUNNER_PID" = "$FINAL_DRAIN_PID"
EXPECTED_RUNNER_START=$(jq -er \
  '.worker_start_ticks | select(type=="number" and .>0)' \
  <<<"$FINAL_RUNNER_BINDING")
FINAL_RUNNER_BINDING_SHA=$(printf '%s' "$FINAL_RUNNER_BINDING" | \
  sha256sum | awk '{print $1}')
FORMAL_PID=$(systemctl show trading.service -p MainPID --value)
advance_phase SEALED COMMIT_READY \
  "formal_pid=$FORMAL_PID" "config_sha256=$CONFIG_EXTERNAL_SHA" \
  "trading_env_sha256=$TRADING_ENV_EXTERNAL_SHA" \
  "runner_worker_pid=$EXPECTED_RUNNER_PID" \
  "runner_worker_start_ticks=$EXPECTED_RUNNER_START" \
  "runner_binding_sha256=$FINAL_RUNNER_BINDING_SHA" \
  "runner_drain_sha256=$FINAL_DRAIN_SHA"

# Derived COMMITTED state is COMMIT_READY + a valid completion + missing
# sentinel. Switch atomically to a boundary-aware failure handler: before
# unlink it uses the normal armed fail-safe; after unlink it preserves the
# committed absence and contains the writer with stop-only recovery.
trap 'commit_boundary_fail $?' ERR EXIT
trap 'commit_boundary_fail 129' HUP
trap 'commit_boundary_fail 130' INT TERM
gate commit --config "$CONFIG" --data-dir "$DATA_DIR" \
  --release-sha "$RELEASE_SHA" \
  --expected-runner-pid "$EXPECTED_RUNNER_PID" --expected-slot "$SLOT"
# The deployment is now committed.  Timer catch-up and health were completed
# under the sentinel, and timer+worker are stopped, so the exact runner remains
# frozen until every auxiliary boundary is ready.  The persistent block and
# absent authorization also contain reboot/restart replacement generations.
prove_formal_runner
test "$(systemctl show trading.service -p MainPID --value)" = "$FORMAL_PID"
test "$(systemctl show trading.service -p FreezerState --value)" = frozen
prove_running_aux_unit trading-mem-monitor.service
prove_unit_inactive trading-state-backup.timer
prove_unit_inactive trading-state-backup.service
  test "$(completed_slot "$DATA_DIR")" = "$SLOT"
prove_unit_inactive trading-state-backup.timer
prove_unit_inactive trading-state-backup.service
prove_formal_runner
test "$(systemctl show trading.service -p MainPID --value)" = "$FORMAL_PID"
test "$(systemctl show trading.service -p FreezerState --value)" = frozen
prove_running_aux_unit trading-mem-monitor.service
sudo systemctl thaw trading.service
test "$(systemctl show trading.service -p FreezerState --value)" = running
prove_formal_runner
check_local_health >/dev/null
prove_running_aux_unit trading-mem-monitor.service
  gate verify-committed-running --data-dir "$DATA_DIR" \
  --release-sha "$RELEASE_SHA" >/dev/null

# Only the healthy committed generation may unlock future service starts.
# Durably remove the block, reload and prove that systemd no longer retains it;
# the normal backup timer resumes only after this restart boundary is complete.
sudo rm -- "$START_BLOCK"
sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-paths \
  --path /etc/systemd/system/trading.service.d \
  --path /etc/systemd/system --path /etc >/dev/null
sudo systemctl daemon-reload
prove_effective_units
verify_start_block_absent
prove_formal_runner
check_local_health >/dev/null
settle_backup_timer
prove_formal_runner
check_local_health >/dev/null
# Public ingress is the final deployment action.  Until every local
# post-commit boundary above is complete, no remote instant-open request can
# reach the now-unfrozen runner after the maintenance sentinel is removed.
sudo systemctl start cloudflared.service
prove_running_aux_unit cloudflared.service
trap - ERR EXIT HUP INT TERM
printf 'deploy complete for %s\n' "$RELEASE_SHA" >&2
