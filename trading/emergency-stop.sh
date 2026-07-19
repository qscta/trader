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
START_AUTH='/run/trading-deploy-authorize-start'
RELEASE_ENV='/etc/trading-release.env'
EVIDENCE_HELPER="$(dirname -- "${BASH_SOURCE[0]}")/deployment_evidence.py"
OLD_GATE_HELPER="$(dirname -- "${BASH_SOURCE[0]}")/deployment_old_runner_gate.py"
EVIDENCE_PYTHON='/usr/bin/python3'
ACTION="${1:---stop-and-arm}"
BOUNDARY_MODE="${2:-}"
BOUNDARY_EVIDENCE="${3:-}"
SHOULD_ARM=1
GRACEFUL=0
UNSET_ENV='LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT LD_DEBUG LD_DEBUG_OUTPUT LD_PROFILE GUNICORN_CMD_ARGS PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT PYTHONUSERBASE PYTHONWARNINGS PYTHONBREAKPOINT PYTHONPYCACHEPREFIX PYTHONPLATLIBDIR PYTHONEXECUTABLE PYTHONCASEOK PYTHONHTTPSVERIFY HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE AWS_CA_BUNDLE OPENSSL_CONF OPENSSL_MODULES SSLKEYLOGFILE GRPC_DEFAULT_SSL_ROOTS_FILE_PATH'

if [[ ! "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'emergency stop: release SHA was not rendered' >&2
  exit 2
fi

fail=0
second_arm_rc=125

# This persistent Condition is the crash/reboot boundary.  It is installed and
# daemon-reloaded before the first stop request.  The authorization path is
# created only for maintenance-mode validation and final sealed restart.
# Removing this drop-in remains a pre-commit operation while the sentinel is
# still present, so any crash stays fail closed at the HTTP and engine layers.
install_start_block() {
  local tmp dir_mode conditions
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
  if ! printf '%s\n' \
      '[Unit]' \
      "ConditionPathExists=$START_AUTH" >"$tmp"; then
    rm -f -- "$tmp"
    return 1
  fi
  if sudo test -e "$START_BLOCK" || sudo test -L "$START_BLOCK"; then
    # Unknown existing content is incident evidence.  Never overwrite it.
    sudo test -f "$START_BLOCK" || { rm -f -- "$tmp"; return 1; }
    sudo test ! -L "$START_BLOCK" || { rm -f -- "$tmp"; return 1; }
    sudo cmp -s -- "$tmp" "$START_BLOCK" || { rm -f -- "$tmp"; return 1; }
  else
    if ! sudo install -o root -g root -m 0644 "$tmp" "$START_BLOCK"; then
      rm -f -- "$tmp"
      return 1
    fi
  fi
  rm -f -- "$tmp"
  sudo test -f "$START_BLOCK" || return 1
  sudo test ! -L "$START_BLOCK" || return 1
  test "$(sudo stat -c '%U:%G:%a' "$START_BLOCK")" = 'root:root:644' || return 1
  sudo grep -Fxq '[Unit]' "$START_BLOCK" || return 1
  sudo grep -Fxq "ConditionPathExists=$START_AUTH" "$START_BLOCK" || return 1
  test "$(sudo wc -l <"$START_BLOCK")" -eq 2 || return 1
  sudo rm -f -- "$START_AUTH" || return 1
  sudo test ! -e "$START_AUTH" || return 1
  sudo systemctl daemon-reload || return 1
  sudo systemd-analyze verify trading.service >/dev/null || return 1
  conditions="$(systemctl show trading.service -p Conditions --value)" || return 1
  grep -Fq "ConditionPathExists=$START_AUTH" <<<"$conditions" || return 1
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

old_gate_helper() {
  sudo systemd-run --quiet --wait --collect --pipe \
    --property=EnvironmentFile=/etc/trading.env \
    --property="UnsetEnvironment=$UNSET_ENV" \
    /usr/bin/python3 -I -B "$OLD_GATE_HELPER" "$@"
}

credential_mode() {
  local required=$1
  sudo systemd-run --quiet --wait --collect --pipe \
    --uid=ubuntu --gid=ubuntu \
    --property="WorkingDirectory=$RELEASE_TRADING" \
    --property=EnvironmentFile=/etc/trading.env \
    --property="UnsetEnvironment=$UNSET_ENV" \
    "$PYTHON" -B -E "$RELEASE_TRADING/deployment_no_open_gate.py" \
    credential-mode --config "$CURRENT_CONFIG" --require-mode "$required"
}

verify_planned_no_open_boundary() {
  local state pid cgroup proof_tmp current_tmp rc
  state="$(systemctl show trading.service -p ActiveState --value)" || return 1
  case "$state" in
    inactive|failed) return 0 ;;
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
  # persistent boundary for later drains and recovery.  The first legacy drain
  # may use only the same-lock HTTP handshake or an exact read-only API key.
  if [[ "$CURRENT_TRADING" = "$RELEASE_TRADING" ]]; then
    old_gate_helper verify-release-sentinel \
      --path "$CURRENT_SENTINEL" --release-sha "$RELEASE_SHA" >/dev/null
    return
  fi
  case "$BOUNDARY_MODE" in
    handshake)
      [[ -n "$BOUNDARY_EVIDENCE" ]] || return 1
      old_gate_helper verify-handshake --evidence "$BOUNDARY_EVIDENCE" \
        --current-data "$CURRENT_DATA" --service-cgroup "$cgroup" >/dev/null
      ;;
    credential_read_only)
      [[ -n "$BOUNDARY_EVIDENCE" ]] || return 1
      proof_tmp="$(mktemp)" || return 1
      current_tmp="$(mktemp)" || { rm -f -- "$proof_tmp"; return 1; }
      if ! old_gate_helper credential-evidence \
          --evidence "$BOUNDARY_EVIDENCE" >"$proof_tmp"; then
        rm -f -- "$proof_tmp" "$current_tmp"
        return 1
      fi
      if ! credential_mode read_only >"$current_tmp"; then
        rm -f -- "$proof_tmp" "$current_tmp"
        return 1
      fi
      if ! cmp -s -- "$proof_tmp" "$current_tmp"; then
        rm -f -- "$proof_tmp" "$current_tmp"
        return 1
      fi
      rm -f -- "$proof_tmp" "$current_tmp"
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
  local unit=$1 state cgroup deadline version
  unit_exists "$unit" || return 1
  state="$(systemctl show "$unit" -p ActiveState --value)" || return 1
  if [[ "$state" = active || "$state" = activating || "$state" = deactivating ]]; then
    version="$(systemctl --version | awk 'NR==1{print $2}')" || return 1
    [[ "$version" =~ ^[0-9]+$ ]] && (( version >= 255 )) || return 1
    sudo systemctl freeze "$unit" || return 1
    test "$(systemctl show "$unit" -p FreezerState --value)" = frozen || return 1
    cgroup="$(systemctl show "$unit" -p ControlGroup --value)" || return 1
    [[ -n "$cgroup" && -e "/sys/fs/cgroup${cgroup}/cgroup.procs" ]] || return 1
    sudo systemctl kill --kill-whom=all --signal=SIGKILL "$unit" || return 1
    deadline=$((SECONDS + 60))
    while [[ -s "/sys/fs/cgroup${cgroup}/cgroup.procs" ]]; do
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
  if [[ -n "$cgroup" && -e "/sys/fs/cgroup${cgroup}/cgroup.procs" ]]; then
    [[ ! -s "/sys/fs/cgroup${cgroup}/cgroup.procs" ]] || return 1
  fi
}

gate_arm() {
  [[ -x "$PYTHON" && -f "$RELEASE_TRADING/deployment_no_open_gate.py" ]] || return 1
  [[ -d "$DATA_DIR" && ! -L "$DATA_DIR" ]] || return 1
  sudo -u ubuntu env -i PATH=/usr/bin:/bin \
    "TRADING_RUNNER_LOCK_FILE=$DATA_DIR/.runtime/runner.lock" \
    "$PYTHON" -B -E "$RELEASE_TRADING/deployment_no_open_gate.py" \
    arm --data-dir "$DATA_DIR" --release-sha "$RELEASE_SHA"
}

case "$ACTION" in
  --install-block-only)
    install_start_block || exit 1
    echo 'formal trading service is persistently blocked' >&2
    exit 0
    ;;
  --stop-and-arm)
    ;;
  --graceful-stop-and-arm)
    GRACEFUL=1
    ;;
  --stop-only)
    SHOULD_ARM=0
    ;;
  *)
    echo 'usage: emergency-stop.sh [--install-block-only|--graceful-stop-and-arm [handshake|credential_read_only evidence]|--stop-and-arm|--stop-only]' >&2
    exit 2
    ;;
esac

if ! verify_old_lock_contract; then
  echo 'emergency stop refused: old runner lock path is overridden or unknown' >&2
  exit 1
fi
if [[ "$GRACEFUL" -eq 1 ]] && ! verify_planned_no_open_boundary; then
  echo 'emergency stop refused: planned no-open boundary is not proven' >&2
  exit 1
fi
if ! install_start_block; then
  echo 'emergency stop refused: persistent formal-service block is not proven' >&2
  exit 1
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
  for unit in trading-state-backup.service cloudflared.service \
      trading-mem-monitor.service trading.service; do
    freeze_kill_stop_unit "$unit" || fail=1
  done
fi

# Arm only after old user code is synchronously impossible.  T0 is established
# later by deploy.sh, after an additional stable-empty observation window.
if [[ "$SHOULD_ARM" -eq 1 ]]; then
  gate_arm
  second_arm_rc=$?
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
  [[ "$state" = inactive || "$state" = failed ]] || fail=1
done

for _stable_sample in 1 2 3; do
  for unit in trading-state-backup.service cloudflared.service \
      trading-mem-monitor.service trading.service; do
    if unit_exists "$unit"; then
      test "$(systemctl show "$unit" -p MainPID --value)" = 0 || fail=1
      cgroup="$(systemctl show "$unit" -p ControlGroup --value)" || fail=1
      if [[ -n "$cgroup" && -e "/sys/fs/cgroup${cgroup}/cgroup.procs" ]]; then
        [[ ! -s "/sys/fs/cgroup${cgroup}/cgroup.procs" ]] || fail=1
      fi
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
  if [[ -n "$cgroup" && -e "/sys/fs/cgroup${cgroup}/cgroup.procs" ]]; then
    [[ -z "$(sudo sed -n '1p' "/sys/fs/cgroup${cgroup}/cgroup.procs")" ]] || fail=1
  fi
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
if pgrep -u ubuntu -f \
    '(/home/ubuntu/trader|/opt/trader-releases)/.*/(gunicorn|wsgi:application|main.py)' \
    >/dev/null; then
  fail=1
fi

[[ -f "$OLD_RUNNER_LOCK_FILE" && ! -L "$OLD_RUNNER_LOCK_FILE" ]] || fail=1
[[ -f "$DATA_DIR/.runtime/runner.lock" && \
   ! -L "$DATA_DIR/.runtime/runner.lock" ]] || fail=1
if [[ -f "$OLD_RUNNER_LOCK_FILE" ]]; then
  sudo -u ubuntu flock --nonblock "$OLD_RUNNER_LOCK_FILE" true || fail=1
fi
if [[ -f "$DATA_DIR/.runtime/runner.lock" ]]; then
  sudo -u ubuntu flock --nonblock \
    "$DATA_DIR/.runtime/runner.lock" true || fail=1
fi

sudo test -f "$START_BLOCK" || fail=1
sudo test ! -e "$START_AUTH" || fail=1
if [[ "$SHOULD_ARM" -eq 1 && "$second_arm_rc" -eq 0 ]]; then
  sudo test -f "$DATA_DIR/.maintenance_no_open" || fail=1
  sudo test ! -L "$DATA_DIR/.maintenance_no_open" || fail=1
fi

if [[ "$fail" -ne 0 ]]; then
  echo "emergency stop incomplete: services remain blocked; arm rc=$second_arm_rc" >&2
  exit 1
fi

if [[ "$SHOULD_ARM" -eq 1 ]]; then
  echo "emergency stop complete: formal service blocked and no-open sentinel armed" >&2
else
  echo "emergency stop complete: formal service blocked; sentinel intentionally preserved" >&2
fi
exit 0
