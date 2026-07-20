#!/usr/bin/env -S -i PATH=/usr/sbin:/usr/bin:/sbin:/bin TRADING_STERILE_DRIVER=1 /bin/bash --noprofile --norc
# Render __RELEASE_SHA__ to the reviewed 40-hex commit, install this file as
# /usr/local/lib/trading-deploy/<sha>/emergency-stop.sh root:root 0555, then
# hash it before the normal deployment may stop any service.
if [[ ${TRADING_STERILE_DRIVER:-} != 1 ]]; then
  exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    TRADING_STERILE_DRIVER=1 /bin/bash --noprofile --norc "$0" "$@"
fi
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
readonly PATH
IFS=$' \t\n'
umask 077
set -uo pipefail

GLOBAL_DEPLOY_LOCK_DIR='/run/trading-deploy-control'
GLOBAL_DEPLOY_LOCK="$GLOBAL_DEPLOY_LOCK_DIR/operation.lock"
GLOBAL_DEPLOY_UNIT='trading-deployment-operation.service'
GLOBAL_DEPLOY_HELPER='trading-deployment-operation-helper.service'
GLOBAL_EMERGENCY_UNIT='trading-deployment-emergency.service'
ACTION="${1:---stop-and-arm}"
if [[ $EUID -ne 0 ]]; then
  exec sudo /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    TRADING_STERILE_DRIVER=1 /bin/bash --noprofile --norc "$0" "$@" || exit 1
fi
if test -e "$GLOBAL_DEPLOY_LOCK_DIR" || test -L "$GLOBAL_DEPLOY_LOCK_DIR"; then
  test -d "$GLOBAL_DEPLOY_LOCK_DIR" || exit 1
  test ! -L "$GLOBAL_DEPLOY_LOCK_DIR" || exit 1
  test "$(stat -c '%U:%G:%a' "$GLOBAL_DEPLOY_LOCK_DIR")" = root:root:700 || exit 1
else
  install -d -o root -g root -m 0700 "$GLOBAL_DEPLOY_LOCK_DIR" || exit 1
fi
if ! test -e "$GLOBAL_DEPLOY_LOCK" && ! test -L "$GLOBAL_DEPLOY_LOCK"; then
  lock_tmp=$(mktemp "$GLOBAL_DEPLOY_LOCK_DIR/.operation.lock.XXXXXX") || exit 1
  chmod 0600 "$lock_tmp" || exit 1
  mv --no-clobber --no-target-directory "$lock_tmp" \
    "$GLOBAL_DEPLOY_LOCK" || exit 1
  test ! -e "$lock_tmp" || rm -f -- "$lock_tmp" || exit 1
fi
test -f "$GLOBAL_DEPLOY_LOCK" || exit 1
test ! -L "$GLOBAL_DEPLOY_LOCK" || exit 1
test "$(stat -c '%U:%G:%a:%h:%s' "$GLOBAL_DEPLOY_LOCK")" = \
  root:root:600:1:0 || exit 1

# A public emergency must own a fixed cgroup before it or any descendant can
# inherit the global lease. If its shell dies, KillMode=control-group prevents
# an orphaned sudo/systemctl/Python child from retaining fd 9 indefinitely.
if [[ ${TRADING_DEPLOY_LOCK_HELD:-} != 1 &&
      ${TRADING_EMERGENCY_SCOPE:-} != 1 ]]; then
  [[ "$ACTION" = --stop-and-arm ]] || exit 1
  EMERGENCY_SELF=$(realpath -e -- "${BASH_SOURCE[0]}") || exit 1
  exec systemd-run --quiet --wait --collect --pipe \
    --unit="$GLOBAL_EMERGENCY_UNIT" --service-type=exec \
    --property=KillMode=control-group --property=TimeoutStopSec=30s \
    /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    TRADING_STERILE_DRIVER=1 TRADING_EMERGENCY_SCOPE=1 \
    /bin/bash --noprofile --norc "$EMERGENCY_SELF" "$@"
fi
if [[ ${TRADING_EMERGENCY_SCOPE:-} = 1 ]]; then
  test "$(systemctl show "$GLOBAL_EMERGENCY_UNIT" -p MainPID --value)" = \
    "$$" || exit 1
  test "$(systemctl show "$GLOBAL_EMERGENCY_UNIT" -p Transient --value)" = \
    yes || exit 1
  test "$(systemctl show "$GLOBAL_EMERGENCY_UNIT" -p KillMode --value)" = \
    control-group || exit 1
fi

PUBLIC_EMERGENCY=0
EMERGENCY_REQUEST=''
EMERGENCY_REQUEST_FD=''

create_emergency_request() {
  local token
  token=$(tr -d '-' </proc/sys/kernel/random/uuid) || return 1
  [[ "$token" =~ ^[0-9a-f]{32}$ ]] || return 1
  EMERGENCY_REQUEST="$GLOBAL_DEPLOY_LOCK_DIR/emergency.request.$token"
  # Publish the fail-closed name immediately. If this process dies before it
  # takes the lease, the next public emergency validates and removes the stale
  # unlocked marker; deployments continue to refuse while it is present.
  ( set -o noclobber; : >"$EMERGENCY_REQUEST" ) 2>/dev/null || return 1
  chmod 0600 "$EMERGENCY_REQUEST" || return 1
  exec {EMERGENCY_REQUEST_FD}<>"$EMERGENCY_REQUEST"
  flock --exclusive --nonblock "$EMERGENCY_REQUEST_FD" || return 1
  test "$(stat -c '%U:%G:%a:%h:%s' "$EMERGENCY_REQUEST")" = \
    root:root:600:1:0 || return 1
  test "$(stat -Lc '%d:%i' "/proc/$$/fd/$EMERGENCY_REQUEST_FD")" = \
    "$(stat -c '%d:%i' "$EMERGENCY_REQUEST")" || return 1
  sync -f "$GLOBAL_DEPLOY_LOCK_DIR" || return 1
}

cleanup_emergency_requests() {
  local request name cleanup_fd
  for request in "$GLOBAL_DEPLOY_LOCK_DIR"/emergency.request.*; do
    [[ ! -L "$request" ]] || return 1
    test -e "$request" || continue
    name=${request##*/}
    [[ "$name" =~ ^emergency\.request\.[0-9a-f]{32}$ ]] || return 1
    test -f "$request" || return 1
    test ! -L "$request" || return 1
    test "$(stat -c '%U:%G:%a:%h:%s' "$request")" = \
      root:root:600:1:0 || return 1
    [[ "$request" != "$EMERGENCY_REQUEST" ]] || continue
    exec {cleanup_fd}<>"$request"
    test "$(stat -Lc '%d:%i' "/proc/$$/fd/$cleanup_fd")" = \
      "$(stat -c '%d:%i' "$request")" || return 1
    if flock --exclusive --nonblock "$cleanup_fd"; then
      test "$(stat -Lc '%d:%i' "/proc/$$/fd/$cleanup_fd")" = \
        "$(stat -c '%d:%i' "$request")" || return 1
      rm -f -- "$request" || return 1
    fi
    exec {cleanup_fd}>&-
  done
  # Our request is the fail-closed token for this invocation.  Remove it only
  # after every stale request was validated/cleaned and every other live
  # request was left locked in place.
  test -n "$EMERGENCY_REQUEST" || return 1
  test "$(stat -Lc '%d:%i' "/proc/$$/fd/$EMERGENCY_REQUEST_FD")" = \
    "$(stat -c '%d:%i' "$EMERGENCY_REQUEST")" || return 1
  sync -f "$GLOBAL_DEPLOY_LOCK_DIR" || return 1
  rm -f -- "$EMERGENCY_REQUEST" || return 1
  sync -f "$GLOBAL_DEPLOY_LOCK_DIR" || return 1
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

cgroup_is_empty() {
  local populated
  populated=$(cgroup_populated "$1") || return 1
  [[ "$populated" = 0 ]]
}

contain_operation_units() {
  local lock_busy=$1 main_load main_state main_pid main_cgroup=''
  local helper_load helper_state helper_cgroup='' deadline populated
  local current_populated
  local main_populated helper_populated main_present helper_present
  main_load=$(systemctl show "$GLOBAL_DEPLOY_UNIT" \
    -p LoadState --value 2>/dev/null) || return 1
  helper_load=$(systemctl show "$GLOBAL_DEPLOY_HELPER" \
    -p LoadState --value 2>/dev/null) || return 1
  if [[ "$main_load" != not-found ]]; then
    test "$(systemctl show "$GLOBAL_DEPLOY_UNIT" -p Transient --value)" = yes || \
      return 1
    test "$(systemctl show "$GLOBAL_DEPLOY_UNIT" -p KillMode --value)" = \
      control-group || return 1
    main_state=$(systemctl show "$GLOBAL_DEPLOY_UNIT" \
      -p ActiveState --value) || return 1
    main_pid=$(systemctl show "$GLOBAL_DEPLOY_UNIT" -p MainPID --value) || \
      return 1
    main_cgroup=$(systemctl show "$GLOBAL_DEPLOY_UNIT" \
      -p ControlGroup --value) || return 1
  else
    main_state=inactive
    main_pid=0
  fi
  if [[ "$helper_load" != not-found ]]; then
    test "$(systemctl show "$GLOBAL_DEPLOY_HELPER" -p Transient --value)" = yes || \
      return 1
    test "$(systemctl show "$GLOBAL_DEPLOY_HELPER" -p KillMode --value)" = \
      control-group || return 1
    tr ' ' '\n' <<<"$(systemctl show "$GLOBAL_DEPLOY_HELPER" \
      -p BindsTo --value)" | grep -Fxq "$GLOBAL_DEPLOY_UNIT" || return 1
    tr ' ' '\n' <<<"$(systemctl show "$GLOBAL_DEPLOY_HELPER" \
      -p After --value)" | grep -Fxq "$GLOBAL_DEPLOY_UNIT" || return 1
    helper_state=$(systemctl show "$GLOBAL_DEPLOY_HELPER" \
      -p ActiveState --value) || return 1
    helper_cgroup=$(systemctl show "$GLOBAL_DEPLOY_HELPER" \
      -p ControlGroup --value) || return 1
  else
    helper_state=inactive
  fi

  main_populated=$(cgroup_populated "$main_cgroup") || return 1
  helper_populated=$(cgroup_populated "$helper_cgroup") || return 1
  main_present=0
  helper_present=0
  [[ "$main_state" = active || "$main_state" = activating ||
     "$main_state" = deactivating || "$main_populated" -eq 1 ]] && \
    main_present=1
  [[ "$helper_state" = active || "$helper_state" = activating ||
     "$helper_state" = deactivating || "$helper_populated" -eq 1 ]] && \
    helper_present=1

  if [[ "$lock_busy" -eq 1 &&
        ( "$main_state" = active || "$main_state" = activating ) ]]; then
    [[ "$main_pid" =~ ^[1-9][0-9]*$ ]] || return 1
    test "$(stat -Lc '%d:%i' "/proc/$main_pid/fd/9")" = \
      "$(stat -c '%d:%i' "$GLOBAL_DEPLOY_LOCK")" || return 1
  fi
  if [[ "$main_present" -eq 1 || "$helper_present" -eq 1 ]]; then
    if [[ "$helper_present" -eq 1 ]]; then
      systemctl freeze "$GLOBAL_DEPLOY_HELPER" >/dev/null 2>&1 || true
    fi
    if [[ "$main_present" -eq 1 ]]; then
      systemctl freeze "$GLOBAL_DEPLOY_UNIT" >/dev/null 2>&1 || true
    fi
    if [[ "$helper_present" -eq 1 ]]; then
      systemctl kill --kill-whom=all --signal=SIGKILL \
        "$GLOBAL_DEPLOY_HELPER" >/dev/null 2>&1 || true
    fi
    if [[ "$main_present" -eq 1 ]]; then
      systemctl kill --kill-whom=all --signal=SIGKILL \
        "$GLOBAL_DEPLOY_UNIT" >/dev/null 2>&1 || true
    fi
    if [[ "$helper_load" != not-found ]]; then
      systemctl stop "$GLOBAL_DEPLOY_HELPER" >/dev/null 2>&1 || true
    fi
    if [[ "$main_load" != not-found ]]; then
      systemctl stop "$GLOBAL_DEPLOY_UNIT" >/dev/null 2>&1 || true
    fi
  fi

  deadline=$((SECONDS + 60))
  while :; do
    main_state=$(systemctl show "$GLOBAL_DEPLOY_UNIT" \
      -p ActiveState --value 2>/dev/null || printf 'inactive')
    helper_state=$(systemctl show "$GLOBAL_DEPLOY_HELPER" \
      -p ActiveState --value 2>/dev/null || printf 'inactive')
    populated=0
    for cgroup in "$main_cgroup" "$helper_cgroup"; do
      current_populated=$(cgroup_populated "$cgroup") || return 1
      if [[ "$current_populated" = 1 ]]; then
        populated=1
      fi
    done
    if [[ "$main_state" != active && "$main_state" != activating &&
          "$main_state" != deactivating && "$helper_state" != active &&
          "$helper_state" != activating && "$helper_state" != deactivating &&
          "$populated" -eq 0 ]]; then
      return 0
    fi
    (( SECONDS < deadline )) || return 1
    sleep 1
  done
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

run_bound_helper() {
  local rc
  wait_helper_absent || return 1
  if systemd-run --quiet --wait --collect --pipe \
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

if [[ ${TRADING_DEPLOY_LOCK_HELD:-} = 1 ]]; then
  test "${TRADING_DEPLOY_LOCK_FD:-}" = 9 || exit 1
  test "$(stat -Lc '%d:%i' /proc/$$/fd/9)" = \
    "$(stat -c '%d:%i' "$GLOBAL_DEPLOY_LOCK")" || exit 1
  flock --exclusive --nonblock 9 || exit 1
else
  [[ "$ACTION" = --stop-and-arm ]] || exit 1
  PUBLIC_EMERGENCY=1
  create_emergency_request || exit 1
  exec 9<>"$GLOBAL_DEPLOY_LOCK"
  lock_was_busy=0
  if ! flock --exclusive --nonblock 9; then
    lock_was_busy=1
    contain_operation_units 1 || exit 1
    flock --exclusive --timeout 360 9 || exit 1
  fi
  contain_operation_units "$lock_was_busy" || exit 1
  export TRADING_DEPLOY_LOCK_HELD=1 TRADING_DEPLOY_LOCK_FD=9
fi

EXPECTED_SHA='__RELEASE_SHA__'
RELEASE_SHA="$EXPECTED_SHA"
LIVE_TRADING='/home/ubuntu/trader/trading'
RELEASE_ROOT="/opt/trader-releases/$RELEASE_SHA"
RELEASE_TRADING="$RELEASE_ROOT/trading"
PYTHON="$RELEASE_TRADING/.venv/bin/python"
DATA_DIR="/var/lib/trading-runtime/$RELEASE_SHA"
CURRENT_TRADING=''
CURRENT_DATA=''
CURRENT_CONFIG=''
CURRENT_SENTINEL=''
OLD_RUNNER_LOCK_FILE=''
START_BLOCK_DIR='/etc/systemd/system/trading.service.d'
START_BLOCK="$START_BLOCK_DIR/00-deploy-closed.conf"
EMERGENCY_START_BLOCK="$START_BLOCK_DIR/zzzz-deploy-emergency-closed.conf"
START_AUTH='/run/trading-deploy-authorize-start'
RELEASE_ENV='/etc/trading-release.env'
DRIVER_DIR=$(dirname -- "${BASH_SOURCE[0]}")
DEPLOY_DRIVER="$DRIVER_DIR/deploy.sh"
EVIDENCE_HELPER="$DRIVER_DIR/deployment_evidence.py"
ATTEMPT_HELPER="$DRIVER_DIR/deployment_attempt.py"
OLD_GATE_HELPER="$DRIVER_DIR/deployment_old_runner_gate.py"
DEPLOY_STAGE="/var/lib/trading-deploy/$RELEASE_SHA"
ACTIVE_ATTEMPT="$DEPLOY_STAGE/active-attempt"
EVIDENCE_PYTHON='/usr/bin/python3'
BOUNDARY_MODE="${2:-}"
BOUNDARY_EVIDENCE="${3:-}"
INHERITED_SOURCE_LOCK_FD="${TRADING_INHERITED_SOURCE_LOCK_FD:-}"
INHERITED_SOURCE_LOCK_PATH="${TRADING_INHERITED_SOURCE_LOCK_PATH:-}"
INHERITED_SOURCE_LOCK_ACTIVE=0
LOCAL_SOURCE_LOCK_FD=''
LOCAL_SOURCE_LOCK_ACTIVE=0
SHOULD_ARM=1
GRACEFUL=0
FALLBACK_CONTAIN_ONLY=0
LOCK_CONTRACT_OK=0
RUNTIME_READY=0
COMMITTED_CYCLE=0
ABANDONED_CYCLE=0
UNSET_ENV='LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT LD_DEBUG LD_DEBUG_OUTPUT LD_PROFILE GUNICORN_CMD_ARGS PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT PYTHONUSERBASE PYTHONWARNINGS PYTHONBREAKPOINT PYTHONPYCACHEPREFIX PYTHONPLATLIBDIR PYTHONEXECUTABLE PYTHONCASEOK PYTHONHTTPSVERIFY HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE AWS_CA_BUNDLE OPENSSL_CONF OPENSSL_MODULES SSLKEYLOGFILE GRPC_DEFAULT_SSL_ROOTS_FILE_PATH'

if [[ ! "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'emergency stop: release SHA was not rendered' >&2
  exit 2
fi
if [[ -n "$INHERITED_SOURCE_LOCK_FD" ||
      -n "$INHERITED_SOURCE_LOCK_PATH" ]]; then
  [[ ${TRADING_EMERGENCY_INTERNAL:-} = 1 &&
     ${TRADING_DEPLOY_LOCK_HELD:-} = 1 &&
     "$ACTION" = --stop-and-arm ]] || exit 2
  [[ "$INHERITED_SOURCE_LOCK_FD" = 8 ]] || exit 2
  [[ -n "$INHERITED_SOURCE_LOCK_PATH" &&
     "$INHERITED_SOURCE_LOCK_PATH" = /* &&
     "$INHERITED_SOURCE_LOCK_PATH" != *$'\n'* ]] || exit 2
fi

fail=0
second_arm_rc=125

# This persistent Condition is the crash/reboot boundary.  It is installed and
# daemon-reloaded before the first stop request.  The authorization path is
# created only for maintenance-mode validation and final sealed restart.
# Removing this drop-in remains a pre-commit operation while the sentinel is
# still present, so any crash stays fail closed at the HTTP and engine layers.
install_exact_start_block() {
  local target=$1 kind=$2 tmp block_tmp dir_mode
  sudo test ! -L "$START_BLOCK_DIR" || return 1
  if sudo test -e "$START_BLOCK_DIR"; then
    sudo test -d "$START_BLOCK_DIR" || return 1
    test "$(sudo stat -c '%U:%G' "$START_BLOCK_DIR")" = root:root || return 1
    dir_mode="$(sudo stat -c '%a' "$START_BLOCK_DIR")" || return 1
    test "$(( 8#$dir_mode & 8#022 ))" -eq 0 || return 1
  else
    sudo install -d -o root -g root -m 0755 "$START_BLOCK_DIR" || return 1
  fi
  sudo test -d "$START_BLOCK_DIR" || return 1
  sudo test ! -L "$START_BLOCK_DIR" || return 1
  test "$(sudo stat -c '%U:%G' "$START_BLOCK_DIR")" = root:root || return 1
  dir_mode="$(sudo stat -c '%a' "$START_BLOCK_DIR")" || return 1
  test "$(( 8#$dir_mode & 8#022 ))" -eq 0 || return 1
  tmp="$(mktemp)" || return 1
  if [[ "$kind" = normal ]]; then
    printf '%s\n' '[Unit]' \
      "ConditionPathExists=$START_AUTH" >"$tmp" || {
        rm -f -- "$tmp"; return 1;
      }
  elif [[ "$kind" = emergency ]]; then
    # Empty assignment clears every earlier Condition; !/ is a permanent
    # false condition because a bootable Linux host always has a root path.
    # It cannot be unlocked by the normal deployment authorization file.
    printf '%s\n' '[Unit]' 'ConditionPathExists=' \
      'ConditionPathExists=!/' >"$tmp" || {
        rm -f -- "$tmp"; return 1;
      }
  else
    rm -f -- "$tmp"
    return 1
  fi
  test -f "$ATTEMPT_HELPER" && test ! -L "$ATTEMPT_HELPER" || {
    rm -f -- "$tmp"; return 1;
  }
  test "$(stat -c '%U:%G:%a:%h' "$ATTEMPT_HELPER")" = root:root:555:1 || {
    rm -f -- "$tmp"; return 1;
  }
  if sudo test -e "$target" || sudo test -L "$target"; then
    # Unknown existing content is incident evidence.  Never overwrite it.
    sudo test -f "$target" || { rm -f -- "$tmp"; return 1; }
    sudo test ! -L "$target" || { rm -f -- "$tmp"; return 1; }
    sudo cmp -s -- "$tmp" "$target" || { rm -f -- "$tmp"; return 1; }
  else
    block_tmp="$(sudo mktemp "$START_BLOCK_DIR/.trading-deploy-block.XXXXXX")" || {
      rm -f -- "$tmp"
      return 1
    }
    if ! sudo install -o root -g root -m 0644 "$tmp" "$block_tmp" ||
        ! sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-paths \
          --path "$block_tmp" >/dev/null ||
        ! sudo mv --no-target-directory "$block_tmp" "$target"; then
      sudo rm -f -- "$block_tmp"
      rm -f -- "$tmp"
      return 1
    fi
  fi
  if ! sudo test -f "$target" || ! sudo test ! -L "$target" || \
      [[ "$(sudo stat -c '%U:%G:%a:%h' "$target")" != \
        'root:root:644:1' ]] || ! sudo cmp -s -- "$tmp" "$target" || \
      ! sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-paths \
        --path "$target" --path "$START_BLOCK_DIR" \
        --path /etc/systemd/system >/dev/null; then
    rm -f -- "$tmp"
    return 1
  fi
  rm -f -- "$tmp"
  if [[ "$kind" = emergency ]]; then
    # The permanent false condition is independent of START_AUTH.  Load and
    # prove it even when that path is a damaged directory which cannot be
    # removed; otherwise systemd could retain an older, authorizable block.
    sudo systemctl daemon-reload || return 1
    if ! sudo rm -f -- "$START_AUTH" || \
        ! { sudo test ! -e "$START_AUTH" && \
            sudo test ! -L "$START_AUTH"; }; then
      echo 'emergency stop warning: start authorization path is damaged; permanent fallback remains loaded' >&2
    fi
    return 0
  fi
  sudo rm -f -- "$START_AUTH" || return 1
  sudo test ! -e "$START_AUTH" && sudo test ! -L "$START_AUTH" || return 1
  sudo systemctl daemon-reload || return 1
}

install_start_block() {
  # An earlier fallback is durable incident evidence, not a normal deployment
  # artifact.  Never let a planned run proceed through it.
  ! sudo test -e "$EMERGENCY_START_BLOCK" && \
    ! sudo test -L "$EMERGENCY_START_BLOCK" || return 1
  install_exact_start_block "$START_BLOCK" normal
}

install_emergency_start_block() {
  install_exact_start_block "$EMERGENCY_START_BLOCK" emergency
}

verify_loaded_start_block() {
  local conditions
  sudo systemd-analyze verify trading.service >/dev/null || return 1
  conditions="$(systemctl show trading.service -p Conditions --value)" || return 1
  grep -Fq "ConditionPathExists=$START_AUTH" <<<"$conditions" || return 1
}

verify_loaded_emergency_start_block() {
  local raw signature count name trigger negate parameter result extra
  sudo test -f "$EMERGENCY_START_BLOCK" || return 1
  sudo test ! -L "$EMERGENCY_START_BLOCK" || return 1
  test "$(sudo stat -c '%U:%G:%a:%h' "$EMERGENCY_START_BLOCK")" = \
    root:root:644:1 || return 1
  test "$(sudo sed -n '1p' "$EMERGENCY_START_BLOCK")" = '[Unit]' || return 1
  test "$(sudo sed -n '2p' "$EMERGENCY_START_BLOCK")" = \
    'ConditionPathExists=' || return 1
  test "$(sudo sed -n '3p' "$EMERGENCY_START_BLOCK")" = \
    'ConditionPathExists=!/' || return 1
  test "$(sudo wc -l <"$EMERGENCY_START_BLOCK")" -eq 3 || return 1
  sudo systemd-analyze verify trading.service >/dev/null || return 1
  raw=$(busctl get-property org.freedesktop.systemd1 \
    /org/freedesktop/systemd1/unit/trading_2eservice \
    org.freedesktop.systemd1.Unit Conditions) || return 1
  read -r signature count name trigger negate parameter result extra <<<"$raw"
  [[ "$signature" = 'a(sbbsi)' && "$count" = 1 && \
     "$name" = '"ConditionPathExists"' && "$trigger" = false && \
     "$negate" = true && "$parameter" = '"/"' && \
     "$result" =~ ^-?[0-9]+$ && -z "$extra" ]] || return 1
}

verify_old_lock_contract() {
  local direct envfiles envfile env_count=0 env_mode lock_line data_line
  local config_line sentinel_line
  sudo test -f /etc/trading.env || return 1
  test -f "$EVIDENCE_HELPER" && test ! -L "$EVIDENCE_HELPER" || return 1
  test "$(stat -c '%U:%G:%a' "$EVIDENCE_HELPER")" = root:root:555 || return 1
  sudo test ! -L /etc/trading.env || return 1
  test "$(sudo stat -c '%U:%G' /etc/trading.env)" = root:root || return 1
  env_mode="$(sudo stat -c '%a' /etc/trading.env)" || return 1
  test "$(( 8#$env_mode & 8#022 ))" -eq 0 || return 1
  sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" \
    validate-trading-env --file /etc/trading.env || return 1
  if sudo awk '
      /^[[:space:]]*(#|$)/ { next }
      /^[[:space:]]*(export[[:space:]]+)?TRADING_RUNNER_LOCK_FILE[[:space:]]*=/ { found=1 }
      END { exit found ? 0 : 1 }
    ' /etc/trading.env; then
    return 1
  else
    test "$?" -eq 1 || return 1
  fi
  direct="$(systemctl show trading.service -p Environment --value)" || return 1
  case "$direct" in
    *TRADING_RUNNER_LOCK_FILE=*|*TRADING_DATA_DIR=*|*TRADING_CONFIG_FILE=*|\
    *TRADING_MAINTENANCE_SENTINEL=*) return 1 ;;
  esac
  CURRENT_TRADING="$(systemctl show trading.service -p WorkingDirectory --value)" || return 1
  [[ "$CURRENT_TRADING" = "$LIVE_TRADING" ||
     "$CURRENT_TRADING" =~ ^/opt/trader-releases/[0-9a-f]{40}/trading$ ]] || return 1
  test "$(realpath -e -- "$CURRENT_TRADING")" = "$CURRENT_TRADING" || return 1
  test -d "$CURRENT_TRADING" && test ! -L "$CURRENT_TRADING" || return 1
  CURRENT_DATA="$CURRENT_TRADING"
  CURRENT_CONFIG="$CURRENT_TRADING/config.json"
  CURRENT_SENTINEL="$CURRENT_TRADING/.maintenance_no_open"
  OLD_RUNNER_LOCK_FILE="$CURRENT_TRADING/.runtime/runner.lock"
  envfiles="$(systemctl show trading.service -p EnvironmentFiles --value)" || return 1
  while IFS= read -r envfile; do
    [[ -n "$envfile" ]] || continue
    case "$envfile" in
      /etc/trading.env) env_count=$((env_count + 1));;
      "$RELEASE_ENV")
        sudo test -f "$RELEASE_ENV" || return 1
        sudo test ! -L "$RELEASE_ENV" || return 1
        test "$(sudo stat -c '%U:%G:%a' "$RELEASE_ENV")" = root:root:600 || return 1
        sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" validate-managed \
          --file "$RELEASE_ENV" --kind release_env || return 1
        lock_line="$(sudo sed -n 's/^TRADING_RUNNER_LOCK_FILE=//p' "$RELEASE_ENV")" || return 1
        data_line="$(sudo sed -n 's/^TRADING_DATA_DIR=//p' "$RELEASE_ENV")" || return 1
        config_line="$(sudo sed -n 's/^TRADING_CONFIG_FILE=//p' "$RELEASE_ENV")" || return 1
        sentinel_line="$(sudo sed -n 's/^TRADING_MAINTENANCE_SENTINEL=//p' "$RELEASE_ENV")" || return 1
        [[ -n "$lock_line" && -n "$data_line" && -n "$config_line" &&
           -n "$sentinel_line" ]] || return 1
        OLD_RUNNER_LOCK_FILE="$lock_line"
        CURRENT_DATA="$data_line"
        CURRENT_CONFIG="$config_line"
        CURRENT_SENTINEL="$sentinel_line"
        ;;
      *) return 1;;
    esac
  done < <(grep -oE '/[^ ;)]+' <<<"$envfiles")
  test "$env_count" -eq 1 || return 1
  [[ "$CURRENT_DATA" = "$CURRENT_TRADING" ||
     "$CURRENT_DATA" =~ ^/var/lib/trading-runtime/[0-9a-f]{40}$ ]] || return 1
  test "$(realpath -e -- "$CURRENT_DATA")" = "$CURRENT_DATA" || return 1
  test "$OLD_RUNNER_LOCK_FILE" = "$CURRENT_DATA/.runtime/runner.lock" || return 1
  test "$CURRENT_CONFIG" = "$CURRENT_DATA/config.json" || return 1
  test "$CURRENT_SENTINEL" = "$CURRENT_DATA/.maintenance_no_open" || return 1
  test -f "$CURRENT_CONFIG" && test ! -L "$CURRENT_CONFIG"
}

validate_source_lock_fd() {
  local fd=$1 path=$2 fd_path
  [[ "$fd" = 8 && -n "$path" ]] || return 1
  test -f "$path" || return 1
  test ! -L "$path" || return 1
  test "$(realpath -e -- "$path")" = "$path" || return 1
  test "$(stat -c '%U:%G:%a:%h' "$path")" = \
    ubuntu:ubuntu:600:1 || return 1
  fd_path="/proc/$$/fd/$fd"
  test -e "$fd_path" || return 1
  test "$(stat -Lc '%d:%i' "$fd_path")" = \
    "$(stat -c '%d:%i' "$path")" || return 1
  flock --exclusive --nonblock "$fd" || return 1
}

validate_inherited_source_lock() {
  [[ -n "$INHERITED_SOURCE_LOCK_FD" &&
     -n "$INHERITED_SOURCE_LOCK_PATH" ]] || return 1
  validate_source_lock_fd \
    "$INHERITED_SOURCE_LOCK_FD" "$INHERITED_SOURCE_LOCK_PATH" || return 1
  INHERITED_SOURCE_LOCK_ACTIVE=0
  if [[ "$OLD_RUNNER_LOCK_FILE" = "$INHERITED_SOURCE_LOCK_PATH" ]]; then
    INHERITED_SOURCE_LOCK_ACTIVE=1
  fi
  return 0
}

validate_local_source_lock() {
  [[ "$LOCAL_SOURCE_LOCK_FD" = 8 ]] || return 1
  validate_source_lock_fd \
    "$LOCAL_SOURCE_LOCK_FD" "$OLD_RUNNER_LOCK_FILE" || return 1
  LOCAL_SOURCE_LOCK_ACTIVE=1
}

hold_external_pre_g0_source_lock() {
  [[ "$CURRENT_DATA" != "$DATA_DIR" ]] || return 0
  if [[ -n "$INHERITED_SOURCE_LOCK_FD" ]]; then
    validate_inherited_source_lock || return 1
    [[ "$INHERITED_SOURCE_LOCK_ACTIVE" -eq 1 ]]
    return
  fi
  [[ -z "$LOCAL_SOURCE_LOCK_FD" ]] || {
    validate_local_source_lock
    return
  }
  test ! -e /proc/$$/fd/8 && test ! -L /proc/$$/fd/8 || return 1
  exec 8<>"$OLD_RUNNER_LOCK_FILE" || return 1
  LOCAL_SOURCE_LOCK_FD=8
  if ! flock --exclusive --nonblock "$LOCAL_SOURCE_LOCK_FD" || \
      ! validate_local_source_lock; then
    exec 8>&-
    LOCAL_SOURCE_LOCK_FD=''
    LOCAL_SOURCE_LOCK_ACTIVE=0
    return 1
  fi
  return 0
}

old_gate_observer() {
  sudo env -i PATH=/usr/bin:/bin \
    /usr/bin/python3 -I -B "$OLD_GATE_HELPER" "$@"
}

old_gate_client() {
  run_bound_helper \
    --property=EnvironmentFile=/etc/trading.env \
    --property="UnsetEnvironment=$UNSET_ENV" \
    /usr/bin/python3 -I -B "$OLD_GATE_HELPER" "$@"
}

verify_planned_no_open_boundary() {
  local state pid cgroup rc jobs sample
  state="$(systemctl show trading.service -p ActiveState --value)" || return 1
  case "$state" in
    inactive|failed)
      for sample in 1 2; do
        state="$(systemctl show trading.service -p ActiveState --value)" || return 1
        [[ "$state" = inactive || "$state" = failed ]] || return 1
        test "$(systemctl show trading.service -p MainPID --value)" = 0 || return 1
        cgroup="$(systemctl show trading.service -p ControlGroup --value)" || return 1
        cgroup_is_empty "$cgroup" || return 1
        jobs="$(systemctl list-jobs --no-legend --no-pager)" || return 1
        grep -Eq '^[[:space:]]*[0-9]+[[:space:]]+trading\.service[[:space:]]' \
          <<<"$jobs" && return 1
        sudo -u ubuntu flock --nonblock "$OLD_RUNNER_LOCK_FILE" true || return 1
      done
      return 0
      ;;
    active|activating|deactivating) ;;
    *) return 1 ;;
  esac
  test -f "$OLD_GATE_HELPER" && test ! -L "$OLD_GATE_HELPER" || return 1
  test "$(stat -c '%U:%G:%a' "$OLD_GATE_HELPER")" = root:root:555 || return 1
  pid="$(systemctl show trading.service -p MainPID --value)" || return 1
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  test "$(sudo realpath -e -- "/proc/$pid/cwd")" = "$CURRENT_TRADING" || return 1
  cgroup="$(systemctl show trading.service -p ControlGroup --value)" || return 1
  [[ "$cgroup" = /* && -f "/sys/fs/cgroup$cgroup/cgroup.procs" ]] || return 1
  grep -Fxq "$pid" "/sys/fs/cgroup$cgroup/cgroup.procs" || return 1
  if sudo -u ubuntu flock --nonblock "$OLD_RUNNER_LOCK_FILE" true; then
    return 1
  else
    rc=$?
    test "$rc" -eq 1 || return 1
  fi

  # Once the reviewed release itself is active, its exact sentinel is the
  # persistent boundary for later drains and recovery.  The legacy drain has
  # exactly one alternative: its same-lock runtime sentinel handshake.
  if [[ "$CURRENT_TRADING" = "$RELEASE_TRADING" ]]; then
    old_gate_observer verify-release-sentinel \
      --path "$CURRENT_SENTINEL" --release-sha "$RELEASE_SHA" >/dev/null
    return
  fi
  case "$BOUNDARY_MODE" in
    runtime_sentinel)
      [[ -n "$BOUNDARY_EVIDENCE" ]] || return 1
      old_gate_client verify-handshake --evidence "$BOUNDARY_EVIDENCE" \
        --runner-lock "$OLD_RUNNER_LOCK_FILE" \
        --service-cgroup "$cgroup" --expected-cwd "$CURRENT_TRADING" \
        --current-data "$CURRENT_DATA" >/dev/null || return 1
      ;;
    *) return 1 ;;
  esac
  # Refuse if the service identity changed during the final proof call.
  test "$(systemctl show trading.service -p MainPID --value)" = "$pid" || return 1
  grep -Fxq "$pid" "/sys/fs/cgroup$cgroup/cgroup.procs"
}

unit_exists() {
  [[ "$(systemctl show "$1" -p LoadState --value 2>/dev/null)" != not-found ]]
}

freeze_kill_stop_unit() {
  local unit=$1 state cgroup='' deadline kill_ok=0 populated
  local needs_kill=0
  unit_exists "$unit" || return 1
  state="$(systemctl show "$unit" -p ActiveState --value)" || return 1
  cgroup="$(systemctl show "$unit" -p ControlGroup --value 2>/dev/null || true)"
  if [[ "$state" = active || "$state" = activating || "$state" = deactivating ]]; then
    needs_kill=1
  fi
  populated=$(cgroup_populated "$cgroup") || return 1
  if [[ "$populated" = 1 ]]; then
    needs_kill=1
  fi
  if [[ "$needs_kill" -eq 1 ]]; then
    # Freeze is an extra fork/cleanup barrier when supported. SIGKILL plus an
    # observed empty cgroup remains the kernel-backed containment proof.
    if sudo systemctl freeze "$unit"; then
      # Read the state for diagnostics/serialization, but never let an
      # inconclusive freezer report delay the unconditional SIGKILL below.
      systemctl show "$unit" -p FreezerState --value >/dev/null || true
    fi
    # Observation must never precede the kill attempt: a damaged unit may no
    # longer expose ControlGroup even though its writer is still executable.
    if sudo systemctl kill --kill-whom=all --signal=SIGKILL "$unit"; then
      kill_ok=1
    elif sudo systemctl kill --signal=SIGKILL "$unit"; then
      kill_ok=1
    elif [[ "$cgroup" = /* ]] && \
        sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" kill-bound-cgroup \
          --unit "$unit" --cgroup "$cgroup"; then
      # cgroup.kill is a kernel-level subtree operation: unlike a MainPID
      # fallback it also kills every gunicorn worker and nested descendant.
      kill_ok=1
    fi
    [[ -n "$cgroup" ]] || return 1
    populated=$(cgroup_populated "$cgroup") || return 1
    if [[ "$populated" = 1 ]]; then
      # systemctl kill may report success while a nested delegated cgroup
      # remains populated.  cgroup.kill is the recursive kernel boundary.
      [[ "$cgroup" = /* ]] && \
        sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" kill-bound-cgroup \
          --unit "$unit" --cgroup "$cgroup" || return 1
      kill_ok=1
    fi
    [[ "$kill_ok" -eq 1 ]] || return 1
    deadline=$((SECONDS + 60))
    while :; do
      populated=$(cgroup_populated "$cgroup") || return 1
      [[ "$populated" = 0 ]] && break
      (( SECONDS < deadline )) || return 1
      sleep 1
    done
    # Only now may stop thaw/remove the frozen cgroup: no executable user task
    # remains to run deferred cleanup after the future T0 boundary.
  fi
  sudo systemctl stop "$unit" || return 1
}

graceful_stop_unit() {
  local unit=$1 state result cgroup
  unit_exists "$unit" || return 1
  state="$(systemctl show "$unit" -p ActiveState --value)" || return 1
  if [[ "$state" = active || "$state" = activating || "$state" = deactivating ]]; then
    sudo systemctl stop "$unit" || return 1
    result="$(systemctl show "$unit" -p Result --value)" || return 1
    [[ "$result" = success ]] || return 1
  fi
  state="$(systemctl show "$unit" -p ActiveState --value)" || return 1
  [[ "$state" = inactive ]] || return 1
  cgroup="$(systemctl show "$unit" -p ControlGroup --value)" || return 1
  cgroup_is_empty "$cgroup" || return 1
}

gate_arm() {
  [[ -x "$PYTHON" && -f "$RELEASE_TRADING/deployment_no_open_gate.py" ]] || return 1
  [[ -d "$DATA_DIR" && ! -L "$DATA_DIR" ]] || return 1
  sudo -u ubuntu env -i PATH=/usr/bin:/bin \
    "TRADING_RUNNER_LOCK_FILE=$DATA_DIR/.runtime/runner.lock" \
    "$PYTHON" -B -E "$RELEASE_TRADING/deployment_no_open_gate.py" \
    arm --data-dir "$DATA_DIR" --release-sha "$RELEASE_SHA"
}

arm_inactive_old_gate() {
  [[ -x "$PYTHON" && -f "$RELEASE_TRADING/deployment_no_open_gate.py" ]] || return 1
  [[ -d "$CURRENT_DATA" && ! -L "$CURRENT_DATA" ]] || return 1
  [[ "$OLD_RUNNER_LOCK_FILE" = "$CURRENT_DATA/.runtime/runner.lock" ]] || return 1
  sudo -u ubuntu env -i PATH=/usr/bin:/bin \
    "TRADING_RUNNER_LOCK_FILE=$OLD_RUNNER_LOCK_FILE" \
    "$PYTHON" -B -E "$RELEASE_TRADING/deployment_no_open_gate.py" \
    arm-recovery-gate --data-dir "$CURRENT_DATA" --release-sha "$RELEASE_SHA"
}

gate_finish_abandon() {
  local attempt_id=$1
  [[ "$attempt_id" =~ ^[0-9]{4}$ ]] || return 1
  [[ -x "$PYTHON" && -f "$RELEASE_TRADING/deployment_no_open_gate.py" ]] || return 1
  [[ -d "$DATA_DIR" && ! -L "$DATA_DIR" ]] || return 1
  sudo -u ubuntu env -i PATH=/usr/bin:/bin \
    "TRADING_RUNNER_LOCK_FILE=$DATA_DIR/.runtime/runner.lock" \
    "$PYTHON" -B -E "$RELEASE_TRADING/deployment_no_open_gate.py" \
    abandon --data-dir "$DATA_DIR" --release-sha "$RELEASE_SHA" \
    --attempt-id "$attempt_id"
}

successor_recovery_seed() {
  local previous_attempt=$1 previous_phase=$2 next_attempt next_dir seed driver_sha
  next_attempt=$(printf '%04d' "$((10#$previous_attempt + 1))") || return 2
  [[ "$next_attempt" =~ ^[0-9]{4}$ && "$next_attempt" != 0000 ]] || return 1
  next_dir="$DEPLOY_STAGE/attempts/$next_attempt"
  seed="$next_dir/recovery-seed.json"
  if ! test -e "$seed" && ! test -L "$seed"; then
    return 1
  fi
  test -f "$seed" && test ! -L "$seed" || return 2
  driver_sha=$(sha256sum -- "$DEPLOY_DRIVER" | awk '{print $1}') || return 2
  seed=$(
    "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" recovery-seed \
      --attempt-dir "$next_dir" --release-sha "$RELEASE_SHA" \
      --attempt-id "$next_attempt" --driver-sha256 "$driver_sha"
  ) || return 2
  jq -e --arg previous "$previous_attempt" --arg phase "$previous_phase" '
    .previous_attempt_id==$previous and .previous_phase==$phase
  ' <<<"$seed" >/dev/null || return 2
  return 0
}

active_attempt_status() {
  local current attempt_dir driver_sha status
  test -f "$DEPLOY_DRIVER" && test ! -L "$DEPLOY_DRIVER" || return 2
  test -f "$ATTEMPT_HELPER" && test ! -L "$ATTEMPT_HELPER" || return 2
  test "$(stat -c '%U:%G:%a:%h' "$ATTEMPT_HELPER")" = root:root:555:1 || \
    return 2
  test -f "$ACTIVE_ATTEMPT" && test ! -L "$ACTIVE_ATTEMPT" || return 2
  test "$(stat -c '%U:%G:%a:%h:%s' "$ACTIVE_ATTEMPT")" = \
    root:root:600:1:5 || return 2
  "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" sync-paths \
    --path "$ACTIVE_ATTEMPT" --path "$DEPLOY_STAGE" >/dev/null || return 2
  current=$(sed -n '1p' "$ACTIVE_ATTEMPT") || return 2
  [[ "$current" =~ ^[0-9]{4}$ ]] || return 2
  attempt_dir="$DEPLOY_STAGE/attempts/$current"
  test "$(stat -c '%U:%G:%a' "$attempt_dir")" = root:root:700 || return 2
  driver_sha=$(sha256sum -- "$DEPLOY_DRIVER" | awk '{print $1}') || return 2
  status=$("$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" status \
    --attempt-dir "$attempt_dir" --release-sha "$RELEASE_SHA" \
    --attempt-id "$current" --driver-sha256 "$driver_sha") || return 2
  jq -c --arg active_attempt "$current" \
    '. + {active_attempt:$active_attempt}' <<<"$status" || return 2
}

capture_pre_g0_source_contract() {
  local status phase abandoned current attempt_dir driver_sha temporary seed
  local completed_slot data_state
  status=$(active_attempt_status) || return 1
  phase=$(jq -r '.phase // ""' <<<"$status") || return 1
  abandoned=$(jq -r .abandoned <<<"$status") || return 1
  [[ "$abandoned" = true || "$abandoned" = false ]] || return 1
  current=$(jq -r .active_attempt <<<"$status") || return 1
  [[ "$current" =~ ^[0-9]{4}$ ]] || return 1
  attempt_dir="$DEPLOY_STAGE/attempts/$current"
  driver_sha=$(sha256sum -- "$DEPLOY_DRIVER" | awk '{print $1}') || return 1
  if [[ "$CURRENT_DATA" != "$DATA_DIR" &&
        "$INHERITED_SOURCE_LOCK_ACTIVE" -ne 1 &&
        "$LOCAL_SOURCE_LOCK_ACTIVE" -ne 1 &&
        ( -z "$phase" || "$phase" = PREPARED ) ]] && \
      ! test -e "$attempt_dir/source-runtime.json" && \
      ! test -L "$attempt_dir/source-runtime.json"; then
    # Before G0, a held source lease must protect the exact service source from
    # which a new authority contract would be captured. A later phase already
    # has a durable source contract and may legitimately have switched systemd
    # to the candidate runtime while retaining that lease.
    return 1
  fi
  if [[ "$abandoned" = true ]]; then
    # Recovery may be interrupted after its journal-first abandon but before
    # ACTIVE switches. Containment must remain idempotent in that window. An
    # existing source contract is still validated; an absent one is no longer
    # publishable and is unnecessary for the hard-stop boundary itself.
    if test -e "$attempt_dir/source-runtime.json" || \
        test -L "$attempt_dir/source-runtime.json"; then
      "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" source-contract \
        --attempt-dir "$attempt_dir" --release-sha "$RELEASE_SHA" \
        --attempt-id "$current" --driver-sha256 "$driver_sha" >/dev/null || \
        return 1
    fi
    return 0
  fi
  case "$phase" in
    '')
      "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" init \
        --attempt-dir "$attempt_dir" --release-sha "$RELEASE_SHA" \
        --attempt-id "$current" --driver-sha256 "$driver_sha" >/dev/null || \
        return 1
      ;;
    PREPARED) ;;
    *) return 0 ;;
  esac
  if test -e "$attempt_dir/source-runtime.json" || \
      test -L "$attempt_dir/source-runtime.json"; then
    "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" source-contract \
      --attempt-dir "$attempt_dir" --release-sha "$RELEASE_SHA" \
      --attempt-id "$current" --driver-sha256 "$driver_sha" >/dev/null
    return
  fi
  temporary=$(mktemp) || return 1
  if test -e "$attempt_dir/recovery-seed.json" || \
      test -L "$attempt_dir/recovery-seed.json"; then
    seed=$("$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" recovery-seed \
      --attempt-dir "$attempt_dir" --release-sha "$RELEASE_SHA" \
      --attempt-id "$current" --driver-sha256 "$driver_sha") || {
        rm -f -- "$temporary"; return 1;
      }
    if ! "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" describe-source \
        --source-trading "$(jq -r .source.source_trading <<<"$seed")" \
        --source-data "$(jq -r .source.source_data <<<"$seed")" \
        --completed-schedule-slot \
          "$(jq -r .source.completed_schedule_slot <<<"$seed")" \
        --data-state "$(jq -r .source.data_state <<<"$seed")" \
        >"$temporary"; then
      rm -f -- "$temporary"
      return 1
    fi
  else
    if [[ "$LOCK_CONTRACT_OK" -ne 1 ]]; then
      rm -f -- "$temporary"
      return 1
    fi
    completed_slot=$(sudo -u ubuntu env -i PATH=/usr/bin:/bin \
      "$PYTHON" -B -E "$RELEASE_TRADING/deployment_no_open_gate.py" \
      recorded-slot --config "$CURRENT_DATA/config.json" \
      --data-dir "$CURRENT_DATA") || {
        rm -f -- "$temporary"; return 1;
      }
    data_state='requires_migration'
    if ! "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" describe-source \
        --source-trading "$CURRENT_TRADING" --source-data "$CURRENT_DATA" \
        --completed-schedule-slot "$completed_slot" \
        --data-state "$data_state" >"$temporary"; then
      rm -f -- "$temporary"
      return 1
    fi
  fi
  if ! "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" publish-artifact \
      --attempt-dir "$attempt_dir" --release-sha "$RELEASE_SHA" \
      --attempt-id "$current" --driver-sha256 "$driver_sha" \
      --name source-runtime.json --source "$temporary" >/dev/null; then
    rm -f -- "$temporary"
    return 1
  fi
  rm -f -- "$temporary"
  return 0
}

gate_verify_committed_stopped() {
  [[ -x "$PYTHON" && -f "$RELEASE_TRADING/deployment_no_open_gate.py" ]] || return 1
  sudo -u ubuntu env -i PATH=/usr/bin:/bin \
    "TRADING_RUNNER_LOCK_FILE=$DATA_DIR/.runtime/runner.lock" \
    "$PYTHON" -B -E "$RELEASE_TRADING/deployment_no_open_gate.py" \
    verify-committed-stopped --data-dir "$DATA_DIR" \
    --release-sha "$RELEASE_SHA"
}

init_release_runtime() {
  sudo "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" init-runtime \
    --release-sha "$RELEASE_SHA" >/dev/null
}

reconcile_arm_intent_boundary() {
  local status phase abandoned current attempt_dir driver_sha
  local intent boundary state cgroup nonce temporary jobs sample
  local continuity_args
  status=$(active_attempt_status) || return 1
  phase=$(jq -r '.phase // ""' <<<"$status") || return 1
  abandoned=$(jq -r .abandoned <<<"$status") || return 1
  current=$(jq -r .active_attempt <<<"$status") || return 1
  [[ "$phase" = PREPARED && "$abandoned" = false &&
     "$current" =~ ^[0-9]{4}$ ]] || return 1
  attempt_dir="$DEPLOY_STAGE/attempts/$current"
  intent="$attempt_dir/old-no-open-arm-intent.json"
  boundary="$attempt_dir/old-no-open-boundary.json"
  driver_sha=$(sha256sum -- "$DEPLOY_DRIVER" | awk '{print $1}') || return 1
  old_gate_observer verify-arm-intent \
    --evidence "$intent" --release-sha "$RELEASE_SHA" >/dev/null || return 1
  verify_old_lock_contract || return 1
  state=$(systemctl show trading.service -p ActiveState --value) || return 1
  if [[ "$state" = inactive || "$state" = failed ]]; then
    # If the process died before its HTTP arm, create the same fail-closed gate
    # while holding the exact runner FLOCK.  A concurrent restart either wins
    # the lock (this attempt fails without mutation) or starts under the gate.
    if ! test -e "$CURRENT_SENTINEL" && ! test -L "$CURRENT_SENTINEL"; then
      ! test -e "$boundary" && ! test -L "$boundary" || return 1
      arm_inactive_old_gate || return 1
    fi
    if test -e "$boundary" || test -L "$boundary"; then
      old_gate_observer verify-persistent-old-sentinel \
        --path "$CURRENT_SENTINEL" --release-sha "$RELEASE_SHA" \
        --evidence "$boundary" >/dev/null || return 1
    else
      old_gate_observer verify-persistent-gate-object \
        --path "$CURRENT_SENTINEL" >/dev/null || return 1
    fi
    # Two stable samples, an empty cgroup, no pending job and the free exact
    # runner lock prove that no pre-sentinel operation remains in flight.
    for sample in 1 2; do
      state=$(systemctl show trading.service -p ActiveState --value) || return 1
      [[ "$state" = inactive || "$state" = failed ]] || return 1
      test "$(systemctl show trading.service -p MainPID --value)" = 0 || return 1
      cgroup=$(systemctl show trading.service -p ControlGroup --value) || return 1
      cgroup_is_empty "$cgroup" || return 1
      jobs=$(systemctl list-jobs --no-legend --no-pager) || return 1
      grep -Eq '^[[:space:]]*[0-9]+[[:space:]]+trading\.service[[:space:]]' \
        <<<"$jobs" && return 1
      sudo -u ubuntu flock --nonblock "$OLD_RUNNER_LOCK_FILE" true || return 1
      [[ "$sample" -eq 2 ]] || sleep 1
    done
    return 0
  fi
  [[ "$state" = active ]] || return 1
  cgroup=$(systemctl show trading.service -p ControlGroup --value) || return 1
  [[ "$cgroup" = /* && -f "/sys/fs/cgroup$cgroup/cgroup.procs" ]] || return 1

  if test -e "$CURRENT_SENTINEL" || test -L "$CURRENT_SENTINEL"; then
    continuity_args=(
      verify-maintenance-continuity --release-sha "$RELEASE_SHA"
      --runner-lock "$OLD_RUNNER_LOCK_FILE" --service-cgroup "$cgroup"
      --expected-cwd "$CURRENT_TRADING" --current-data "$CURRENT_DATA"
      --arm-intent "$intent"
    )
    if test -e "$boundary" || test -L "$boundary"; then
      continuity_args+=(--evidence "$boundary")
    fi
    old_gate_client "${continuity_args[@]}" >/dev/null
    return
  fi
  # A durable historical boundary cannot coexist with an absent sentinel.
  # Refuse without touching systemd; do not manufacture replacement evidence.
  ! test -e "$boundary" && ! test -L "$boundary" || return 1

  temporary=$(mktemp) || return 1
  nonce=$(openssl rand -hex 32) || { rm -f -- "$temporary"; return 1; }
  [[ "$nonce" =~ ^[0-9a-f]{64}$ ]] || {
    rm -f -- "$temporary"
    return 1
  }
  if ! old_gate_client establish-handshake \
      --release-sha "$RELEASE_SHA" --nonce "$nonce" \
      --runner-lock "$OLD_RUNNER_LOCK_FILE" --service-cgroup "$cgroup" \
      --expected-cwd "$CURRENT_TRADING" --current-data "$CURRENT_DATA" \
      >"$temporary"; then
      rm -f -- "$temporary"
      return 1
  fi
  if ! "$EVIDENCE_PYTHON" -I -B "$ATTEMPT_HELPER" publish-artifact \
      --attempt-dir "$attempt_dir" --release-sha "$RELEASE_SHA" \
      --attempt-id "$current" --driver-sha256 "$driver_sha" \
      --name old-no-open-boundary.json --source "$temporary" >/dev/null; then
    rm -f -- "$temporary"
    return 1
  fi
  rm -f -- "$temporary"
  old_gate_client verify-handshake --evidence "$boundary" \
    --runner-lock "$OLD_RUNNER_LOCK_FILE" --service-cgroup "$cgroup" \
    --expected-cwd "$CURRENT_TRADING" --current-data "$CURRENT_DATA" \
    >/dev/null
}

case "$ACTION" in
  --install-block-only)
    [[ ${TRADING_EMERGENCY_INTERNAL:-} = 1 ]] || {
      echo 'emergency stop: --install-block-only is deployment-internal' >&2
      exit 2
    }
    install_start_block || exit 1
    verify_loaded_start_block || exit 1
    echo 'formal trading service is persistently blocked' >&2
    exit 0
    ;;
  --stop-and-arm)
    ;;
  --reconcile-arm-intent)
    [[ ${TRADING_EMERGENCY_INTERNAL:-} = 1 ]] || {
      echo 'emergency stop: --reconcile-arm-intent is recovery-internal' >&2
      exit 2
    }
    reconcile_arm_intent_boundary || exit 1
    echo 'old runner runtime sentinel boundary is durable and verified' >&2
    exit 0
    ;;
  --graceful-stop-and-arm)
    [[ ${TRADING_EMERGENCY_INTERNAL:-} = 1 ]] || {
      echo 'emergency stop: --graceful-stop-and-arm is deployment-internal' >&2
      exit 2
    }
    GRACEFUL=1
    ;;
  --stop-only)
    [[ ${TRADING_EMERGENCY_INTERNAL:-} = 1 ]] || {
      echo 'emergency stop: --stop-only is recovery-internal' >&2
      exit 2
    }
    SHOULD_ARM=0
    ;;
  --contain-fallback-only)
    [[ ${TRADING_EMERGENCY_INTERNAL:-} = 1 ]] || {
      echo 'emergency stop: --contain-fallback-only is deployment-internal' >&2
      exit 2
    }
    SHOULD_ARM=0
    FALLBACK_CONTAIN_ONLY=1
    ;;
  *)
    echo 'usage: emergency-stop.sh [--stop-and-arm]' >&2
    exit 2
    ;;
esac

# Install the persistent boundary first when possible.  Planned deployment may
# not stop without it.  A hard emergency must still kill the current writer if
# unknown block content is preserved as incident evidence; it returns failure
# after containment instead of falsely claiming a durable restart boundary.
START_BLOCK_PROVEN=0
EMERGENCY_BLOCK_PROVEN=0
if install_start_block; then
  START_BLOCK_PROVEN=1
elif [[ "$GRACEFUL" -eq 1 ]]; then
  echo 'emergency stop refused: persistent formal-service block is not proven' >&2
  exit 1
else
  if install_emergency_start_block && \
      verify_loaded_emergency_start_block; then
    EMERGENCY_BLOCK_PROVEN=1
    echo 'emergency stop warning: primary block is damaged; independent persistent fallback installed' >&2
  else
    echo 'emergency stop warning: persistent blocks are damaged; containing current writer' >&2
  fi
  fail=1
fi
# Planned stop depends on a fully valid service graph.  A hard stop instead
# kills first: a malformed unit/drop-in must not leave the current writer live.
if [[ "$GRACEFUL" -eq 1 ]] && ! verify_loaded_start_block; then
  echo 'emergency stop refused: formal-service block is not loaded' >&2
  exit 1
fi
# A hard emergency has no credential/sentinel boundary for the current writer.
# Contain that writer before runtime initialization or auxiliary-unit cleanup
# can delay the kill.  Queueing the stop job first suppresses Restart= while the
# hard kill drains the current cgroup, even when a damaged block was preserved.
if [[ "$GRACEFUL" -ne 1 ]]; then
  sudo systemctl stop --no-block trading.service || fail=1
  freeze_kill_stop_unit trading.service || fail=1
  writer_state="$(systemctl show trading.service -p ActiveState --value)" || exit 1
  writer_pid="$(systemctl show trading.service -p MainPID --value)" || exit 1
  writer_cgroup="$(systemctl show trading.service -p ControlGroup --value)" || exit 1
  if [[ "$writer_state" != inactive && "$writer_state" != failed ]] || \
      [[ "$writer_pid" != 0 ]] || \
      ! cgroup_is_empty "$writer_cgroup"; then
    echo 'emergency stop incomplete: current trading writer is not contained' >&2
    exit 1
  fi
  if [[ "$START_BLOCK_PROVEN" -eq 1 ]]; then
    verify_loaded_start_block || fail=1
  elif [[ "$EMERGENCY_BLOCK_PROVEN" -eq 1 ]]; then
    verify_loaded_emergency_start_block || fail=1
  else
    fail=1
  fi
  if [[ "$FALLBACK_CONTAIN_ONLY" -eq 1 ]]; then
    [[ "$EMERGENCY_BLOCK_PROVEN" -eq 1 ]] || exit 1
    verify_loaded_emergency_start_block || exit 1
    echo 'emergency fallback is loaded and the current writer is contained; manual repair is required' >&2
    exit 75
  fi
fi
if verify_old_lock_contract; then
  LOCK_CONTRACT_OK=1
fi
if [[ -n "$INHERITED_SOURCE_LOCK_FD" ]]; then
  if [[ "$LOCK_CONTRACT_OK" -ne 1 ]] || \
      ! validate_inherited_source_lock; then
    LOCK_CONTRACT_OK=0
    fail=1
  fi
fi
if [[ "$GRACEFUL" -ne 1 ]]; then
  if [[ "$LOCK_CONTRACT_OK" -eq 1 ]] && \
      ! hold_external_pre_g0_source_lock; then
    LOCK_CONTRACT_OK=0
    fail=1
  fi
  if [[ "$LOCK_CONTRACT_OK" -eq 1 ]]; then
    capture_pre_g0_source_contract || fail=1
  else
    fail=1
  fi
fi
if [[ "$GRACEFUL" -eq 1 && "$LOCK_CONTRACT_OK" -ne 1 ]]; then
  echo 'emergency stop refused: planned runner lock contract is not proven' >&2
  exit 1
fi
if [[ "$GRACEFUL" -eq 1 ]] && ! verify_planned_no_open_boundary; then
  echo 'emergency stop refused: planned no-open boundary is not proven' >&2
  exit 1
fi
if init_release_runtime; then
  RUNTIME_READY=1
elif [[ "$GRACEFUL" -eq 1 ]]; then
  echo 'emergency stop refused: release runtime boundary cannot be initialized' >&2
  exit 1
else
  fail=1
fi

# Timers contain no user code; stop the trigger first.  Planned deployment
# stops drain the external tunnel, backup and runner through their normal TERM
# hooks so an accepted order lifecycle reaches a durable terminal state.  A
# failed/timeout drain is then hard-contained but returns failure: deployment
# must enter recovery and must never migrate that crash window automatically.
sudo systemctl stop trading-state-backup.timer || fail=1
if [[ "$GRACEFUL" -eq 1 && "$fail" -eq 0 ]]; then
  graceful_failed=0
  for unit in cloudflared.service trading-state-backup.service \
      trading-mem-monitor.service trading.service; do
    graceful_stop_unit "$unit" || graceful_failed=1
  done
  if [[ "$graceful_failed" -ne 0 ]]; then
    for unit in trading-state-backup.service cloudflared.service \
        trading-mem-monitor.service trading.service; do
      freeze_kill_stop_unit "$unit" || true
    done
    fail=1
  fi
else
  hard_units=(trading-state-backup.service cloudflared.service \
    trading-mem-monitor.service)
  if [[ "$GRACEFUL" -eq 1 ]]; then
    # A timer-stop failure bypassed the normal graceful loop, so the writer
    # has not yet been contained by the hard-writer-first branch above.
    hard_units+=(trading.service)
  fi
  for unit in "${hard_units[@]}"; do
    freeze_kill_stop_unit "$unit" || fail=1
  done
fi

# Arm only after old user code is synchronously impossible.  T0 is established
# later by deploy.sh, after an additional stable-empty observation window.
if [[ "$SHOULD_ARM" -eq 1 ]]; then
  if [[ "$RUNTIME_READY" -eq 1 ]]; then
    if gate_attempt_status=$(active_attempt_status) && \
        gate_attempt=$(jq -er \
          '.active_attempt | select(type=="string" and test("^[0-9]{4}$"))' \
          <<<"$gate_attempt_status") && \
        gate_phase=$(jq -er \
          '(.phase // "") | select(type=="string")' \
          <<<"$gate_attempt_status") && \
        gate_abandoned=$(jq -er \
          '.abandoned | select(type=="boolean") | tostring' \
          <<<"$gate_attempt_status"); then
      if [[ "$gate_phase" = COMMIT_READY && "$gate_abandoned" = false ]] && \
          ! sudo test -e "$DATA_DIR/.maintenance_no_open"; then
        gate_verify_committed_stopped
        second_arm_rc=$?
        [[ "$second_arm_rc" -eq 0 ]] && COMMITTED_CYCLE=1
      elif [[ "$gate_abandoned" = true && -n "$gate_phase" && \
              "$gate_phase" != PREPARED ]]; then
        # A durable successor seed is the authority boundary between the old
        # archived cycle and the new sentinel. Before that seed, finish the old
        # archive. At/after it, preserve or create the successor sentinel. This
        # closes both crash windows without ever placing a fresh sentinel beside
        # an old archive and then trying to abandon the old attempt again.
        successor_recovery_seed "$gate_attempt" "$gate_phase"
        successor_seed_rc=$?
        if [[ "$successor_seed_rc" -eq 0 ]]; then
          gate_arm
          second_arm_rc=$?
        elif [[ "$successor_seed_rc" -eq 1 ]]; then
          gate_finish_abandon "$gate_attempt"
          second_arm_rc=$?
          [[ "$second_arm_rc" -eq 0 ]] && ABANDONED_CYCLE=1
        else
          second_arm_rc=125
        fi
      else
        gate_arm
        second_arm_rc=$?
      fi
    else
      second_arm_rc=125
    fi
  else
    second_arm_rc=125
  fi
  [[ "$second_arm_rc" -eq 0 ]] || fail=1
else
  second_arm_rc=0
fi

for unit in \
    trading-state-backup.timer trading-state-backup.service \
    cloudflared.service trading-mem-monitor.service \
    trading.service; do
  unit_exists "$unit" || { fail=1; continue; }
  state="$(systemctl show "$unit" -p ActiveState --value 2>/dev/null)" || fail=1
  if [[ "$state" = failed ]]; then
    sudo systemctl reset-failed "$unit" || fail=1
    state="$(systemctl show "$unit" -p ActiveState --value 2>/dev/null)" || fail=1
  fi
  [[ "$state" = inactive ]] || fail=1
done

for _stable_sample in 1 2 3; do
  for unit in trading-state-backup.service cloudflared.service \
      trading-mem-monitor.service trading.service; do
    if unit_exists "$unit"; then
      test "$(systemctl show "$unit" -p MainPID --value)" = 0 || fail=1
      cgroup="$(systemctl show "$unit" -p ControlGroup --value)" || fail=1
      cgroup_is_empty "$cgroup" || fail=1
    fi
  done
  sleep 1
done

for unit in \
    trading-state-backup.service cloudflared.service \
    trading-mem-monitor.service trading.service; do
  unit_exists "$unit" || { fail=1; continue; }
  pid="$(systemctl show "$unit" -p MainPID --value 2>/dev/null)" || fail=1
  [[ "$pid" = 0 ]] || fail=1
done

for unit in trading-state-backup.service cloudflared.service \
    trading-mem-monitor.service trading.service; do
  unit_exists "$unit" || { fail=1; continue; }
  cgroup="$(systemctl show "$unit" -p ControlGroup --value 2>/dev/null)" || fail=1
  cgroup_is_empty "$cgroup" || fail=1
done

pending_jobs="$(systemctl list-jobs --no-legend --no-pager 2>/dev/null)" || fail=1
for unit in \
    trading-state-backup.timer trading-state-backup.service \
    cloudflared.service trading-mem-monitor.service \
    trading.service; do
  if grep -Eq "^[[:space:]]*[0-9]+[[:space:]]+$unit[[:space:]]" \
      <<<"$pending_jobs"; then
    fail=1
  fi
done

if pgrep -f '/usr/local/sbin/trading-state-backup' >/dev/null; then
  fail=1
fi
if [[ "$LOCK_CONTRACT_OK" -eq 1 ]]; then
  [[ -f "$OLD_RUNNER_LOCK_FILE" && ! -L "$OLD_RUNNER_LOCK_FILE" ]] || fail=1
fi
[[ -f "$DATA_DIR/.runtime/runner.lock" && \
   ! -L "$DATA_DIR/.runtime/runner.lock" ]] || fail=1
if [[ "$LOCK_CONTRACT_OK" -eq 1 &&
      -n "$INHERITED_SOURCE_LOCK_FD" ]]; then
  validate_inherited_source_lock || fail=1
fi
if [[ "$LOCK_CONTRACT_OK" -eq 1 &&
      -n "$LOCAL_SOURCE_LOCK_FD" ]]; then
  validate_local_source_lock || fail=1
fi
if [[ "$LOCK_CONTRACT_OK" -eq 1 &&
      "$INHERITED_SOURCE_LOCK_ACTIVE" -ne 1 &&
      "$LOCAL_SOURCE_LOCK_ACTIVE" -ne 1 &&
      -f "$OLD_RUNNER_LOCK_FILE" ]]; then
  sudo -u ubuntu flock --nonblock "$OLD_RUNNER_LOCK_FILE" true || fail=1
fi
if [[ -f "$DATA_DIR/.runtime/runner.lock" ]]; then
  sudo -u ubuntu flock --nonblock \
    "$DATA_DIR/.runtime/runner.lock" true || fail=1
fi

sudo test -f "$START_BLOCK" || fail=1
sudo test ! -e "$START_AUTH" || fail=1
if [[ "$SHOULD_ARM" -eq 1 && "$second_arm_rc" -eq 0 && \
      "$COMMITTED_CYCLE" -eq 0 && "$ABANDONED_CYCLE" -eq 0 ]]; then
  sudo test -f "$DATA_DIR/.maintenance_no_open" || fail=1
  sudo test ! -L "$DATA_DIR/.maintenance_no_open" || fail=1
fi
if [[ "$PUBLIC_EMERGENCY" -eq 1 && "$fail" -eq 0 ]]; then
  cleanup_emergency_requests || fail=1
fi

if [[ "$fail" -ne 0 ]]; then
  echo "emergency stop incomplete: writer containment and/or persistent block is not fully proven; arm rc=$second_arm_rc" >&2
  exit 1
fi

if [[ "$SHOULD_ARM" -eq 1 ]]; then
  if [[ "$COMMITTED_CYCLE" -eq 1 ]]; then
    echo "emergency stop complete: committed formal service is blocked and stopped" >&2
  elif [[ "$ABANDONED_CYCLE" -eq 1 ]]; then
    echo "emergency stop complete: abandoned gate cycle is archived and formal service is blocked" >&2
  else
    echo "emergency stop complete: formal service blocked and no-open sentinel armed" >&2
  fi
else
  echo "emergency stop complete: formal service blocked; gate state intentionally unchanged" >&2
fi
exit 0
