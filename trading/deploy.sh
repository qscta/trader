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
OLD_LOCK=''
OLD_GATE_MODE=''
OLD_GATE_EVIDENCE=''
OLD_GATE_EVIDENCE_SHA=''
OLD_GATE_FINGERPRINT=''
readonly RELEASE_LOCK="$RUNTIME_ROOT/.runtime/runner.lock"
readonly START_BLOCK='/etc/systemd/system/trading.service.d/00-deploy-closed.conf'
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
DEPLOY_COMPLETE=0
ATTEMPT_ID=''
ATTEMPT_STAGE=''

die() { printf 'deploy: %s\n' "$*" >&2; exit 1; }
sha256() { sha256sum -- "$1" | awk '{print $1}'; }
identity() { sudo stat -c '%d:%i' -- "$1"; }

assert_protected_file() {
  local path=$1 expected_mode=$2
  test -f "$path" && test ! -L "$path"
  test "$(stat -c '%U:%G:%a' "$path")" = "root:root:$expected_mode"
}

assert_protected_dir() {
  local path=$1 mode
  sudo test -d "$path" && sudo test ! -L "$path"
  test "$(sudo stat -c '%U:%G' "$path")" = root:root
  mode="$(sudo stat -c '%a' "$path")"
  test "$(( 8#$mode & 8#022 ))" -eq 0
}

safe_install_managed() {
  local source=$1 target=$2 mode=$3 kind=$4 parent tmp old_mode backup
  parent=$(dirname -- "$target")
  assert_protected_dir "$parent"
  if sudo test -e "$target" || sudo test -L "$target"; then
    sudo test -f "$target" && sudo test ! -L "$target"
    test "$(sudo stat -c '%U:%G' "$target")" = root:root
    test "$(sudo stat -c '%h' "$target")" = 1
    old_mode=$(sudo stat -c '%a' "$target")
    test "$(( 8#$old_mode & 8#022 ))" -eq 0
    sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" validate-managed \
      --file "$target" --kind "$kind"
    if sudo cmp -s -- "$source" "$target"; then
      test "$old_mode" = "${mode#0}"
      return 0
    fi
    backup="$BACKUP_ROOT/managed-$kind.before"
    sudo test ! -e "$backup" && sudo test ! -L "$backup"
    sudo cp --archive --no-dereference "$target" "$backup"
    sudo sh -eu -c 'sha256sum "$1" >"$1.sha256" && sha256sum -c "$1.sha256" >/dev/null' \
      sh "$backup"
  fi
  tmp=$(sudo mktemp "$parent/.trading-deploy-$kind.XXXXXX")
  sudo install -o root -g root -m "$mode" "$source" "$tmp"
  test "$(sudo sha256sum "$source"|awk '{print $1}')" = \
       "$(sudo sha256sum "$tmp"|awk '{print $1}')"
  sudo mv --no-target-directory "$tmp" "$target"
  sudo test -f "$target" && sudo test ! -L "$target"
  test "$(sudo stat -c '%U:%G:%a' "$target")" = "root:root:${mode#0}"
  test "$(sudo sha256sum "$source"|awk '{print $1}')" = \
       "$(sudo sha256sum "$target"|awk '{print $1}')"
  sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" validate-managed \
    --file "$target" --kind "$kind"
}

verify_block() {
  sudo test -f "$START_BLOCK"
  sudo test ! -L "$START_BLOCK"
  sudo grep -Fxq '[Unit]' "$START_BLOCK"
  sudo grep -Fxq "ConditionPathExists=$START_AUTH" "$START_BLOCK"
  test "$(sudo wc -l <"$START_BLOCK")" -eq 2
  sudo test ! -e "$START_AUTH"
}

gate() {
  sudo systemd-run --quiet --wait --collect --pipe \
    --uid=ubuntu --gid=ubuntu \
    --property="WorkingDirectory=$RELEASE_TRADING" \
    --property=EnvironmentFile=/etc/trading.env \
    --property="UnsetEnvironment=$UNSET_ENV" \
    /usr/bin/env \
    "TRADING_RUNNER_LOCK_FILE=$RELEASE_LOCK" \
    "$PYTHON" -B -E "$RELEASE_TRADING/deployment_no_open_gate.py" "$@"
}

fail_safe() {
  local rc=${1:-1}
  trap - ERR EXIT HUP INT TERM
  if [[ "$DEPLOY_COMPLETE" -eq 0 && "$FAIL_SAFE_ACTIVE" -eq 0 ]]; then
    FAIL_SAFE_ACTIVE=1
    "$EMERGENCY" --stop-and-arm "$OLD_GATE_MODE" "$OLD_GATE_EVIDENCE" || true
  fi
  exit "$rc"
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
  local unit
  for unit in trading-state-backup.timer trading-state-backup.service \
      cloudflared.service trading-mem-monitor.service trading.service; do
    test "$(systemctl show "$unit" -p ActiveState --value)" = inactive
  done
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
  expected_exec="argv[]=$PYTHON -B -E -m gunicorn -c gunicorn.conf.py wsgi:application"
  value=$(systemctl show trading.service -p ExecStart --value)
  [[ "$value" = *"$expected_exec"* ]]
  test "$(grep -o 'argv\[\]=' <<<"$value" | wc -l | tr -d ' ')" = 1

  test "$(systemctl show trading-mem-monitor.service -p DynamicUser --value)" = yes
  test "$(systemctl show trading-mem-monitor.service -p SupplementaryGroups --value)" = ubuntu
  test "$(systemctl show trading-mem-monitor.service -p Environment --value)" = \
    "TRADING_DATA_DIR=$DATA_DIR TRADING_CONFIG_FILE=$CONFIG"
  envfiles=$(systemctl show trading-mem-monitor.service \
    -p EnvironmentFiles --value | grep -oE '/[^ ;)]+' | sort)
  test "$envfiles" = /etc/trading-mem-monitor.env
  expected_exec="argv[]=$PYTHON -B -E mem_monitor.py"
  value=$(systemctl show trading-mem-monitor.service -p ExecStart --value)
  [[ "$value" = *"$expected_exec"* ]]
  test "$(grep -o 'argv\[\]=' <<<"$value" | wc -l | tr -d ' ')" = 1
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
  [[ "$cgroup" = /* && -f "/sys/fs/cgroup$cgroup/cgroup.procs" ]]
  grep -Fxq "$pid" "/sys/fs/cgroup$cgroup/cgroup.procs"
  if sudo -u ubuntu flock --nonblock "$RELEASE_LOCK" true; then
    die 'formal runner is active but does not hold the exact runner lock'
  else
    test "$?" -eq 1
  fi
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

check_local_health() {
  sudo systemd-run --quiet --wait --collect --pipe \
    --uid=ubuntu --gid=ubuntu \
    --property="WorkingDirectory=$RELEASE_TRADING" \
    --property=EnvironmentFile=/etc/trading.env \
    --property="UnsetEnvironment=$UNSET_ENV" \
    "$PYTHON" -B -E -c \
    'import json,os,urllib.request; from trade_state import load_strict_json; q=urllib.request.Request("http://127.0.0.1:5000/api/status",headers={"X-API-Token":os.environ["TRADING_API_TOKEN"]}); r=urllib.request.urlopen(q,timeout=15); d=load_strict_json(r); blockers=d.get("health",{}).get("safety_blockers"); ok=(r.status==200 and d.get("status")=="running" and d.get("health",{}).get("healthy") is True and blockers=={"open_intents":0,"close_intents":0,"position_quarantines":0,"stop_residues":0} and d.get("open_intents_count")==0 and d.get("stop_residues")==[] and d.get("stop_anomalies")=={} and d.get("position_quarantines")=={}); print(json.dumps({k:d.get(k) for k in ("status","open_positions_count","open_intents_count","last_daily_check_date")},sort_keys=True)); raise SystemExit(0 if ok else 2)'
}

check_maintenance_http_gate() {
  sudo systemd-run --quiet --wait --collect --pipe \
    --uid=ubuntu --gid=ubuntu \
    --property="WorkingDirectory=$RELEASE_TRADING" \
    --property=EnvironmentFile=/etc/trading.env \
    --property="UnsetEnvironment=$UNSET_ENV" \
    "$PYTHON" -B -E -c \
    'import os,urllib.error,urllib.request; q=urllib.request.Request("http://127.0.0.1:5000/api/instant_open",data=b"{}",headers={"Content-Type":"application/json","X-API-Token":os.environ["TRADING_API_TOKEN"]},method="POST");
try: urllib.request.urlopen(q,timeout=15); raise SystemExit(2)
except urllib.error.HTTPError as e: raise SystemExit(0 if e.code==503 else 2)'
}

verify_reviewed_materials() {
  local scan source relative failed=0 vtmp ftmp ptmp mtmp
  test "$(sha256 "$SELF")" = "$REVIEWED_DEPLOY_SHA"
  test "$(sha256 "$EMERGENCY")" = "$REVIEWED_EMERGENCY_SHA"
  test "$(sha256 "$EVIDENCE_HELPER")" = "$REVIEWED_EVIDENCE_SHA"
  test "$(sha256 "$ATTEMPT_HELPER")" = "$REVIEWED_ATTEMPT_SHA"
  test "$(sha256 "$OLD_GATE_HELPER")" = "$REVIEWED_OLD_GATE_SHA"
  test "$(git -c safe.directory="$RELEASE_ROOT" -C "$RELEASE_ROOT" rev-parse HEAD)" = "$RELEASE_SHA"
  git -c safe.directory="$RELEASE_ROOT" -C "$RELEASE_ROOT" diff --quiet
  git -c safe.directory="$RELEASE_ROOT" -C "$RELEASE_ROOT" diff --cached --quiet
  test "$(stat -c '%U:%G' "$RELEASE_TRADING")" = root:root
  test "$(( 8#$(stat -c '%a' "$RELEASE_TRADING") & 8#022 ))" -eq 0
  test "$(stat -c '%U:%G' "$RELEASE_TRADING/deployment_no_open_gate.py")" = root:root
  test -d "$RELEASE_TRADING/.venv" && test ! -L "$RELEASE_TRADING/.venv"
  test "$(realpath -e -- "$RELEASE_TRADING/.venv")" = "$RELEASE_TRADING/.venv"
  test -z "$(find "$RELEASE_TRADING/.venv" -xdev ! -user root -print -quit)"
  sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" validate-venv \
    --venv "$RELEASE_TRADING/.venv"
  sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" validate-trading-env \
    --file /etc/trading.env
  sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" validate-monitor-env \
    --file /etc/trading-mem-monitor.env
  sudo sh -eu -c 'cd "$1" && sha256sum -c reviewed-assets.sha256 >/dev/null' \
    sh "$DEPLOY_STAGE"
  sudo sh -eu -c 'cd "$1" && sha256sum -c "$2" >/dev/null' \
    sh "$RELEASE_ROOT" "$DEPLOY_STAGE/reviewed-tracked.sha256"
  sudo sh -eu -c 'cd "$1" && sha256sum -c "$2" >/dev/null' \
    sh "$RELEASE_ROOT" "$DEPLOY_STAGE/reviewed-venv.sha256"
  sudo awk 'NF!=2 || $1 !~ /^[0-9a-f]{64}$/ || $2 !~ /^trading\/\.venv\// || $2 ~ /(^|\/)\.\.($|\/)/ {exit 1}' \
    "$DEPLOY_STAGE/reviewed-venv.sha256"
  ftmp=$(mktemp)
  (cd "$RELEASE_ROOT" && find trading/.venv -xdev \( -type f -o -type l \) \
    -printf '%y %m %p\n' | sort) >"$ftmp"
  sudo cmp -s -- "$ftmp" "$DEPLOY_STAGE/reviewed-venv-files.txt"
  mtmp=$(mktemp)
  sudo awk '{print $2}' "$DEPLOY_STAGE/reviewed-venv.sha256" | sort >"$mtmp"
  sudo awk '{print $3}' "$DEPLOY_STAGE/reviewed-venv-files.txt" | sort | \
    cmp -s -- "$mtmp" -
  rm -f -- "$mtmp"
  rm -f -- "$ftmp"
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
  scan=$(mktemp)
  find "$RELEASE_TRADING" -xdev \
    -path "$RELEASE_TRADING/.venv" -type d -prune -o \
    \( -type l -o -type f \( -name '*.py' -o -name '*.pyw' -o \
       -name '*.pyc' -o -name '*.pyo' -o -name '*.so' -o -name '*.pth' \) \) \
    -print0 >"$scan"
  while IFS= read -r -d '' source; do
    relative="${source#"$RELEASE_ROOT"/}"
    git -c safe.directory="$RELEASE_ROOT" -C "$RELEASE_ROOT" \
      ls-files --error-unmatch -- "$relative" \
      >/dev/null || failed=1
  done <"$scan"
  rm -f -- "$scan"
  test "$failed" -eq 0
  sudo sh -eu -c 'cd "$1" && sha256sum -c "$2" >/dev/null' \
    sh "$RELEASE_ROOT" "$DEPLOY_STAGE/reviewed-tracked.sha256"
}

completed_slot() {
  local data_dir=$1
  sudo -u ubuntu env -i PATH=/usr/bin:/bin "$PYTHON" -B -E \
    "$RELEASE_TRADING/deployment_no_open_gate.py" completed-slot \
    --config "$data_dir/config.json" --data-dir "$data_dir"
}

old_gate_client() {
  sudo systemd-run --quiet --wait --collect --pipe \
    --property=EnvironmentFile=/etc/trading.env \
    --property="UnsetEnvironment=$UNSET_ENV" \
    /usr/bin/python3 -I -B "$OLD_GATE_HELPER" "$@"
}

credential_mode() {
  local required=$1 config_path=$2
  sudo systemd-run --quiet --wait --collect --pipe \
    --uid=ubuntu --gid=ubuntu \
    --property="WorkingDirectory=$RELEASE_TRADING" \
    --property=EnvironmentFile=/etc/trading.env \
    --property="UnsetEnvironment=$UNSET_ENV" \
    "$PYTHON" -B -E "$RELEASE_TRADING/deployment_no_open_gate.py" \
    credential-mode --config "$config_path" --require-mode "$required"
}

credential_exposure() {
  local config_path=$1
  sudo systemd-run --quiet --wait --collect --pipe \
    --uid=ubuntu --gid=ubuntu \
    --property="WorkingDirectory=$RELEASE_TRADING" \
    --property=EnvironmentFile=/etc/trading.env \
    --property="UnsetEnvironment=$UNSET_ENV" \
    "$PYTHON" -B -E "$RELEASE_TRADING/deployment_no_open_gate.py" \
    credential-exposure --config "$config_path"
}

[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || die 'release SHA placeholder not rendered'
for command in awk bash flock git grep jq openssl realpath rsync sha256sum \
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
test -x "$EVIDENCE_PYTHON"
test "$(stat -c '%U:%G' "$DRIVER_DIR")" = root:root
test "$(( 8#$(stat -c '%a' "$DRIVER_DIR") & 8#022 ))" -eq 0
assert_protected_dir /opt/trader-releases
assert_protected_dir "$RELEASE_ROOT"
test -x "$PYTHON"
test "$(realpath -e -- "$RELEASE_TRADING")" = "$RELEASE_TRADING"
test -f "$EVIDENCE_HELPER" && test -f "$ATTEMPT_HELPER" && \
  test -f "$OLD_GATE_HELPER"
cd "$RELEASE_TRADING"
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
test -d "$SOURCE_TRADING" && test ! -L "$SOURCE_TRADING"
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
  sudo test -f "$RELEASE_ENV" && sudo test ! -L "$RELEASE_ENV"
  sudo "$EVIDENCE_PYTHON" -I -B "$EVIDENCE_HELPER" validate-managed \
    --file "$RELEASE_ENV" --kind release_env
  configured_data=$(sudo sed -n 's/^TRADING_DATA_DIR=//p' "$RELEASE_ENV")
  if [[ -n "$configured_data" ]]; then SOURCE_DATA="$configured_data"; fi
fi
[[ "$SOURCE_DATA" = "$SOURCE_TRADING" ||
   "$SOURCE_DATA" =~ ^/var/lib/trading-runtime/[0-9a-f]{40}$ ]]
test "$(realpath -e -- "$SOURCE_DATA")" = "$SOURCE_DATA"
OLD_LOCK="$SOURCE_DATA/.runtime/runner.lock"
readonly SOURCE_TRADING SOURCE_DATA OLD_LOCK
sudo test -d "$DEPLOY_STAGE"
test "$(sudo stat -c '%U:%G:%a' "$DEPLOY_STAGE")" = root:root:700
sudo test -f "$DEPLOY_STAGE/active-attempt"
sudo test ! -L "$DEPLOY_STAGE/active-attempt"
test "$(sudo stat -c '%U:%G:%a:%h' "$DEPLOY_STAGE/active-attempt")" = \
  root:root:600:1
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

# All network/dependency/CI work is an explicit pre-stop prerequisite.  The
# root-only stage is produced by the reviewed preparation job and is immutable
# during this invocation.
  for item in reviewed-tracked.sha256 reviewed-assets.sha256 ci-check-runs.json \
    ci-workflow-runs.json trading-state-backup.original \
    trading-state-backup.reviewed \
    reviewed-venv.sha256 reviewed-venv-files.txt pip-freeze.txt \
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
  '.total_count > 0 and ([.check_runs[].head_sha]|all(.==$sha)) and
   ([.check_runs[]|select(.status!="completed" or .conclusion!="success")]|length==0) and
   ($request[0].required_checks - [.check_runs[].name] | length==0)' \
  "$DEPLOY_STAGE/ci-check-runs.json" >/dev/null
sudo jq -e --arg sha "$RELEASE_SHA" \
  --slurpfile request "$DEPLOY_STAGE/prepare-request.json" '
  type=="array" and length>0 and length<100 and
  ([.[].headSha]|all(.==$sha)) and
  ((sort_by(.workflowName,.createdAt)|group_by(.workflowName)|map(last)) as $latest |
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
sudo test ! -e "$EXPOSURE_EVIDENCE" && sudo test ! -L "$EXPOSURE_EVIDENCE"
sudo install -o root -g root -m 0600 "$EXPOSURE_TMP" "$EXPOSURE_EVIDENCE"
rm -f -- "$EXPOSURE_TMP"
EXPOSURE_EVIDENCE_SHA=$(sudo sha256sum "$EXPOSURE_EVIDENCE" | awk '{print $1}')
readonly EXPOSURE_EVIDENCE_SHA
SLOT=$(completed_slot "$SOURCE_DATA") ||
  die 'current schedule day is not complete; production was left running'
[[ "$SLOT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]
readonly SLOT
# Reject every already-known migration/config blocker while the old runner is
# still untouched.  The same checks run again against the stopped copy below
# so this early screen cannot hide a race.
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

# G0 is established while the old service is still the only writer.  A
# handshake-capable runner takes the same _trade_lock as every open path,
# creates its actual sentinel, and releases the lock only after fsync.  The
# one-time bootstrap for older releases is allowed only when OKX itself reports
# this exact key as read_only; a missing endpoint never falls through to stop.
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

CAPABILITY_TMP=$(mktemp)
if old_gate_client capability >"$CAPABILITY_TMP"; then
  sudo jq -e --arg path "$SOURCE_DATA/.maintenance_no_open" '
    type=="object" and
    keys==["maintenance_active","protocol","sentinel_path","worker_pid"] and
    .protocol=="trade-lock-no-open-v1" and
    .maintenance_active==false and .sentinel_path==$path and
    (.worker_pid|type=="number" and .>0 and floor==.)
  ' "$CAPABILITY_TMP" >/dev/null
  OLD_WORKER_PID=$(jq -r .worker_pid "$CAPABILITY_TMP")
  grep -Fxq "$OLD_WORKER_PID" "/sys/fs/cgroup$OLD_CGROUP/cgroup.procs"
  OLD_GATE_NONCE=$(openssl rand -hex 32)
  ARM_TMP=$(mktemp)
  old_gate_client arm --release-sha "$RELEASE_SHA" \
    --nonce "$OLD_GATE_NONCE" >"$ARM_TMP"
  old_gate_client verify-http >/dev/null
  test "$(jq -r .sentinel.path "$ARM_TMP")" = \
    "$SOURCE_DATA/.maintenance_no_open"
  test "$(sudo stat -c '%d:%i' -- "$SOURCE_DATA/.maintenance_no_open")" = \
    "$(jq -r '.sentinel.dev|tostring' "$ARM_TMP"):$(jq -r '.sentinel.ino|tostring' "$ARM_TMP")"
  test "$(sudo stat -c '%U:%a' -- "$SOURCE_DATA/.maintenance_no_open")" = \
    ubuntu:600
  OLD_GATE_EVIDENCE="$ATTEMPT_STAGE/old-no-open-boundary.json"
  sudo test ! -e "$OLD_GATE_EVIDENCE" && sudo test ! -L "$OLD_GATE_EVIDENCE"
  sudo install -o root -g root -m 0600 "$ARM_TMP" "$OLD_GATE_EVIDENCE"
  rm -f -- "$ARM_TMP"
  OLD_GATE_MODE='handshake'
else
  CAPABILITY_RC=$?
  test "$CAPABILITY_RC" -eq 3 || \
    die 'old runner handshake probe failed ambiguously; production left running'
  PERMISSION_TMP=$(mktemp)
  credential_mode read_only "$SOURCE_DATA/config.json" >"$PERMISSION_TMP" || \
    die 'old release has no handshake and current OKX key is not proven read_only'
  sudo jq -e '
    type=="object" and
    keys==["account_domain","api_fingerprint","mode","permissions"] and
    .account_domain=="live" and .mode=="read_only" and
    .permissions==["read_only"] and
    (.api_fingerprint|test("^[0-9a-f]{64}$"))
  ' "$PERMISSION_TMP" >/dev/null
  OLD_GATE_EVIDENCE="$ATTEMPT_STAGE/old-no-open-boundary.json"
  sudo test ! -e "$OLD_GATE_EVIDENCE" && sudo test ! -L "$OLD_GATE_EVIDENCE"
  sudo install -o root -g root -m 0600 "$PERMISSION_TMP" "$OLD_GATE_EVIDENCE"
  OLD_GATE_FINGERPRINT=$(jq -r .api_fingerprint "$PERMISSION_TMP")
  rm -f -- "$PERMISSION_TMP"
  OLD_GATE_MODE='credential_read_only'
fi
rm -f -- "$CAPABILITY_TMP"
OLD_GATE_EVIDENCE_SHA=$(sudo sha256sum "$OLD_GATE_EVIDENCE" | awk '{print $1}')
readonly OLD_GATE_MODE OLD_GATE_EVIDENCE OLD_GATE_EVIDENCE_SHA
readonly OLD_GATE_FINGERPRINT
test "$(completed_slot "$SOURCE_DATA")" = "$SLOT" ||
  die 'schedule completion changed after old no-open boundary'

# The fail-safe can arm an otherwise code-only release before production state
# is copied.  Initialize its private lock while production is still untouched.
if ! sudo test -e /var/lib/trading-runtime; then
  sudo install -d -o root -g root -m 0755 /var/lib/trading-runtime
fi
assert_protected_dir /var/lib/trading-runtime
if ! sudo test -e "$RUNTIME_ROOT"; then
  sudo install -d -o ubuntu -g ubuntu -m 0710 "$RUNTIME_ROOT"
fi
test "$(sudo stat -c '%U:%G:%a' "$RUNTIME_ROOT")" = ubuntu:ubuntu:710
sudo -u ubuntu env -i PATH=/usr/bin:/bin \
  "TRADING_RUNNER_LOCK_FILE=$RELEASE_LOCK" "$PYTHON" -B -E -c \
  'from runtime_guard import acquire_runner_lock; acquire_runner_lock()'
test "$(stat -c '%U:%G:%a' "$RELEASE_LOCK")" = ubuntu:ubuntu:600

# P1 crash/reboot invariant: persistent formal-service block is in force and
# daemon-reloaded before the first stop and before the fail-safe traps exist.
test "$(completed_slot "$SOURCE_DATA")" = "$SLOT" ||
  die 'schedule completion changed before stop; production was left running'
"$EMERGENCY" --install-block-only
verify_block
trap 'fail_safe $?' ERR EXIT
trap 'fail_safe 129' HUP
trap 'fail_safe 130' INT TERM
"$EMERGENCY" --graceful-stop-and-arm "$OLD_GATE_MODE" "$OLD_GATE_EVIDENCE"
assert_inactive_boundaries

BACKUP_ROOT="/var/backups/trading/$(date -u +%Y%m%dT%H%M%SZ)-$SHORT_SHA-$ATTEMPT_ID"
sudo test ! -e "$BACKUP_ROOT" && sudo test ! -L "$BACKUP_ROOT"
sudo install -d -o root -g root -m 0700 "$BACKUP_ROOT"
sudo tar --acls --xattrs --numeric-owner -C /home/ubuntu -cpf \
  "$BACKUP_ROOT/live-repo.tar" trader
sudo tar --acls --xattrs --numeric-owner -C "$(dirname -- "$SOURCE_TRADING")" \
  -cpf "$BACKUP_ROOT/current-trading.tar" "$(basename -- "$SOURCE_TRADING")"
sudo tar --acls --xattrs --numeric-owner -C "$(dirname -- "$SOURCE_DATA")" \
  -cpf "$BACKUP_ROOT/current-data.tar" "$(basename -- "$SOURCE_DATA")"
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
sudo sh -eu -c 'cd "$1" && sha256sum live-repo.tar current-trading.tar current-data.tar live-head.txt live-status.txt trading.service.txt trading-mem-monitor.service.txt trading-state-backup.units.txt cloudflared.service.txt trading-state-backup > SHA256SUMS' \
  sh "$BACKUP_ROOT"
sudo sh -eu -c 'cd "$1" && sha256sum -c SHA256SUMS >/dev/null' sh "$BACKUP_ROOT"

if [[ "$SOURCE_DATA" != "$RUNTIME_ROOT" ]]; then
sudo rsync -a --numeric-ids \
  --include='/config.json*' --include='/trade_state.json*' \
  --include='/closed_trades_archive*.json*' --include='/stop_loss_dates.json*' \
  --include='/daily_equity.json*' --include='/equity_history.json*' \
  --include='/equity_ticks.json*' --include='/peak_equity.json*' \
  --include='/qiusuo_index.json*' --include='/.trading_data_owner.json*' \
  --include='/.okx_legacy_migration_complete.json*' \
  --include='/.equity_sync_journal.json*' \
  --include='/.single_strategy_migration_journal.json*' \
  --include='/data/' --include='/data/***' --exclude='*' \
  "$SOURCE_DATA/" "$RUNTIME_ROOT/"
fi
test "$(sudo stat -c '%U:%G:%a' "$CONFIG")" = ubuntu:ubuntu:600
test "$(sudo stat -c '%U:%G:%a' "$STATE")" = ubuntu:ubuntu:600

sudo env -i PATH=/usr/bin:/bin "$PYTHON" -B -E "$CLEANUP" \
  --check --release-sha "$RELEASE_SHA" --config "$CONFIG" --spec "$SPEC"
sudo env -i PATH=/usr/bin:/bin "$PYTHON" -B -E "$CLEANUP" \
  --apply --release-sha "$RELEASE_SHA" --config "$CONFIG" --spec "$SPEC" \
  --audit "$ATTEMPT_STAGE/confirmed-config-cleanup.audit.json"
sudo env -i PATH=/usr/bin:/bin "$PYTHON" -B -E "$CLEANUP" \
  --verify-applied --release-sha "$RELEASE_SHA" --config "$CONFIG" \
  --spec "$SPEC" --audit "$ATTEMPT_STAGE/confirmed-config-cleanup.audit.json"

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
test "$(completed_slot "$DATA_DIR")" = "$SLOT" ||
  die 'migration changed or crossed the completed schedule slot'

sudo test -f /usr/local/sbin/trading-state-backup
sudo test ! -L /usr/local/sbin/trading-state-backup
test "$(sudo stat -c '%U:%G' /usr/local/sbin/trading-state-backup)" = root:root
test "$(sudo sha256sum /usr/local/sbin/trading-state-backup|awk '{print $1}')" = \
     "$(sudo sha256sum "$DEPLOY_STAGE/trading-state-backup.original"|awk '{print $1}')"
sudo install -o root -g root -m 0755 \
  "$DEPLOY_STAGE/trading-state-backup.reviewed" /usr/local/sbin/trading-state-backup
test "$(sudo sha256sum /usr/local/sbin/trading-state-backup|awk '{print $1}')" = \
     "$(sudo sha256sum "$DEPLOY_STAGE/trading-state-backup.reviewed"|awk '{print $1}')"
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
# Code and interpreter are immutable to the ubuntu runtime identity.  Runtime
# JSON, logs, locks, sentinel and evidence live only under RUNTIME_ROOT.
sudo chown -R root:root "$RELEASE_ROOT"
sudo chmod -R go-w "$RELEASE_ROOT"
test -z "$(sudo find "$RELEASE_ROOT" -xdev ! -user root -print -quit)"
test -z "$(sudo find "$RELEASE_ROOT" -xdev -perm /022 -print -quit)"
test "$(sudo stat -c '%U:%G:%a' "$RELEASE_TRADING")" = root:root:755
test "$(sudo stat -c '%U:%G:%a' "$RELEASE_TRADING/main.py")" = root:root:644
test "$(sudo stat -c '%U:%G' "$RELEASE_TRADING/.venv/bin/python")" = root:root
sudo systemctl daemon-reload
sudo systemd-analyze verify trading.service trading-mem-monitor.service >/dev/null
prove_effective_units
verify_block

assert_inactive_boundaries
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
advance_phase PREPARED QUIESCED \
  "slot=$SLOT" "quiescence_sha256=$QUIESCENCE_SHA" \
  "old_gate_mode=$OLD_GATE_MODE" \
  "old_gate_evidence_sha256=$OLD_GATE_EVIDENCE_SHA"
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
if [[ "$OLD_GATE_MODE" = credential_read_only ]]; then
  # The operator restores Trade only after the reviewed release is running
  # behind its durable sentinel.  The approval binds that external change;
  # the following read-only OKX query proves the exact no-withdraw permission.
  RESTORE_NONCE=$(openssl rand -hex 32)
  CREDENTIAL_RESTORE=(
    "bootstrap_evidence_sha256=$OLD_GATE_EVIDENCE_SHA"
    "api_fingerprint=$OLD_GATE_FINGERPRINT"
    "sentinel_identity=$SENTINEL_ID"
    "request_nonce=$RESTORE_NONCE"
    "required_permissions=read_only,trade"
  )
  write_request credential_restore \
    "$ATTEMPT_STAGE/credential-restore.approval.json" \
    "${CREDENTIAL_RESTORE[@]}"
  wait_approval credential_restore \
    "$ATTEMPT_STAGE/credential-restore.approval.json" \
    "${CREDENTIAL_RESTORE[@]}"
  RESTORED_PERMISSION_TMP=$(mktemp)
  credential_mode trade "$CONFIG" >"$RESTORED_PERMISSION_TMP"
  jq -e --arg fingerprint "$OLD_GATE_FINGERPRINT" '
    type=="object" and
    keys==["account_domain","api_fingerprint","mode","permissions"] and
    .account_domain=="live" and .api_fingerprint==$fingerprint and
    .mode=="trade" and .permissions==["read_only","trade"]
  ' "$RESTORED_PERMISSION_TMP" >/dev/null
  sudo install -o root -g root -m 0600 "$RESTORED_PERMISSION_TMP" \
    "$ATTEMPT_STAGE/credential-trade-restored.json"
  rm -f -- "$RESTORED_PERMISSION_TMP"
  check_maintenance_http_gate
fi
# Reinstalling the block removes authorization before the second graceful
# drain. Formal and every auxiliary writer are silent before final verify.
"$EMERGENCY" --graceful-stop-and-arm
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
sudo systemctl start trading-state-backup.timer
sudo systemctl start cloudflared.service
test "$(systemctl show cloudflared.service -p ActiveState --value)" = active
check_maintenance_http_gate
test "$(sudo sha256sum "$CONFIG"|awk '{print $1}')" = "$CONFIG_EXTERNAL_SHA"
test "$(sudo sha256sum /etc/trading.env|awk '{print $1}')" = \
  "$TRADING_ENV_EXTERNAL_SHA"
test "$(identity "$COMPLETION")" = "$COMPLETION_ID"
if [[ "$OLD_GATE_MODE" = credential_read_only ]]; then
  RESTORED_PERMISSION_TMP=$(mktemp)
  credential_mode trade "$CONFIG" >"$RESTORED_PERMISSION_TMP"
  sudo cmp -s -- "$RESTORED_PERMISSION_TMP" \
    "$ATTEMPT_STAGE/credential-trade-restored.json"
  rm -f -- "$RESTORED_PERMISSION_TMP"
fi

# Remove the persistent start block and its temporary authorization while the
# sentinel is still present. Every fallible filesystem/systemd/health action is
# complete before COMMIT_READY and the unique sentinel-unlink commit.
sudo rm -- "$START_BLOCK" "$START_AUTH"
sudo systemctl daemon-reload
prove_effective_units
sudo test ! -e "$START_BLOCK" && sudo test ! -L "$START_BLOCK"
sudo test ! -e "$START_AUTH" && sudo test ! -L "$START_AUTH"
prove_formal_runner
check_local_health >/dev/null
check_maintenance_http_gate
test "$(identity "$SENTINEL")" = "$SENTINEL_ID"
test "$(identity "$BASELINE")" = "$BASELINE_ID"
test "$(identity "$COMPLETION")" = "$COMPLETION_ID"
test "$(sudo sha256sum "$CONFIG"|awk '{print $1}')" = "$CONFIG_EXTERNAL_SHA"
test "$(sudo sha256sum /etc/trading.env|awk '{print $1}')" = \
  "$TRADING_ENV_EXTERNAL_SHA"
FORMAL_PID=$(systemctl show trading.service -p MainPID --value)
advance_phase SEALED COMMIT_READY \
  "formal_pid=$FORMAL_PID" "config_sha256=$CONFIG_EXTERNAL_SHA" \
  "trading_env_sha256=$TRADING_ENV_EXTERNAL_SHA"

# Derived COMMITTED state is COMMIT_READY + a valid completion + missing
# sentinel. Disable fail-safe traps first: before unlink, any failure leaves the
# sentinel active; after unlink, re-arming would corrupt an already committed
# cycle. This command's durable unlink is the sole final state mutation.
trap - ERR EXIT HUP INT TERM
gate commit --config "$CONFIG" --data-dir "$DATA_DIR" --release-sha "$RELEASE_SHA"
DEPLOY_COMPLETE=1
printf 'deploy complete for %s\n' "$RELEASE_SHA" >&2
