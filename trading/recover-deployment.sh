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
EXPECTED_SHA='__RELEASE_SHA__'
readonly RELEASE_SHA="$EXPECTED_SHA"
readonly DRIVER_DIR="/usr/local/lib/trading-deploy/$RELEASE_SHA"
readonly DEPLOY="$DRIVER_DIR/deploy.sh"
readonly EMERGENCY="$DRIVER_DIR/emergency-stop.sh"
readonly JOURNAL="$DRIVER_DIR/deployment_attempt.py"
readonly RUNTIME="/var/lib/trading-runtime/$RELEASE_SHA"
readonly PYTHON="/opt/trader-releases/$RELEASE_SHA/trading/.venv/bin/python"
readonly GATE="/opt/trader-releases/$RELEASE_SHA/trading/deployment_no_open_gate.py"
readonly STAGE="/var/lib/trading-deploy/$RELEASE_SHA"
readonly ACTIVE="$STAGE/active-attempt"
readonly SENTINEL="$RUNTIME/.maintenance_no_open"
readonly BASELINE="$RUNTIME/deployment_no_open_baseline.json"
readonly COMPLETION="$RUNTIME/deployment_no_open_completion.json"

die() { printf 'recover refused: %s\n' "$*" >&2; exit 1; }
sha256() { sha256sum -- "$1" | awk '{print $1}'; }
gate() {
  sudo -u ubuntu env -i PATH=/usr/bin:/bin \
    "TRADING_RUNNER_LOCK_FILE=$RUNTIME/.runtime/runner.lock" \
    "$PYTHON" -B -E "$GATE" "$@"
}
journal() {
  sudo /usr/bin/python3 -I -B "$JOURNAL" "$@" \
    --attempt-dir "$ATTEMPT_DIR" --release-sha "$RELEASE_SHA" \
    --attempt-id "$CURRENT" --driver-sha256 "$DRIVER_SHA"
}

[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || die 'release SHA not rendered'
test "$(stat -c '%U:%G:%a' "${BASH_SOURCE[0]}")" = root:root:555
test "$(stat -c '%U:%G:%a' "$DEPLOY")" = root:root:555
test "$(stat -c '%U:%G:%a' "$JOURNAL")" = root:root:555
DRIVER_SHA=$(sha256 "$DEPLOY")
readonly DRIVER_SHA
sudo test -f "$ACTIVE" && sudo test ! -L "$ACTIVE"
test "$(sudo stat -c '%U:%G:%a:%h' "$ACTIVE")" = root:root:600:1
CURRENT=$(sudo sed -n '1p' "$ACTIVE")
[[ "$CURRENT" =~ ^[0-9]{4}$ ]] || die 'invalid active attempt'
ATTEMPT_DIR="$STAGE/attempts/$CURRENT"
test "$(sudo stat -c '%U:%G:%a' "$ATTEMPT_DIR")" = root:root:700
ARCHIVED_SENTINEL="$RUNTIME/.abandoned.$CURRENT.$(basename "$SENTINEL")"

# Never reinterpret a completed sentinel unlink as a failed attempt. This
# check precedes emergency arm, which would otherwise create a new sentinel.
STATUS=$(journal status)
PHASE=$(jq -r '.phase // ""' <<<"$STATUS")
ABANDONED=$(jq -r '.abandoned' <<<"$STATUS")
if ! sudo test -e "$SENTINEL"; then
  if [[ "$PHASE" = COMMIT_READY ]] || sudo test -e "$BASELINE" || \
      sudo test -e "$COMPLETION"; then
    die 'sentinel missing with commit-ready/durable evidence (committed or damaged)'
  fi
fi

if sudo test -e "$ARCHIVED_SENTINEL" && ! sudo test -e "$SENTINEL"; then
  "$EMERGENCY" --stop-only
else
  "$EMERGENCY" --stop-and-arm
fi
if [[ "$ABANDONED" != true ]]; then
  journal abandon --reason operator_requested_same_sha_reset >/dev/null
fi

# Gate abandon is journal-first and retryable. It accepts either canonical
# evidence or the exact archive declared by its durable audit.
if sudo test -e "$SENTINEL" || sudo test -e "$ARCHIVED_SENTINEL"; then
  gate abandon --data-dir "$RUNTIME" --release-sha "$RELEASE_SHA" \
    --attempt-id "$CURRENT"
elif sudo test -e "$BASELINE" || sudo test -e "$COMPLETION"; then
  die 'sentinel/archive missing while baseline/completion remains'
fi

NEXT=$(printf '%04d' "$((10#$CURRENT + 1))")
[[ "$NEXT" =~ ^[0-9]{4}$ && "$NEXT" != 0000 ]] || die 'attempt id exhausted'
NEXT_DIR="$STAGE/attempts/$NEXT"
if sudo test -e "$NEXT_DIR" || sudo test -L "$NEXT_DIR"; then
  test "$(sudo stat -c '%U:%G:%a' "$NEXT_DIR")" = root:root:700
  test -z "$(sudo find "$NEXT_DIR" -mindepth 1 -maxdepth 1 -print -quit)"
else
  sudo install -d -o root -g root -m 0700 "$NEXT_DIR"
fi
ACTIVE_TMP="$STAGE/.active-attempt.$$.new"
sudo test ! -e "$ACTIVE_TMP" && sudo test ! -L "$ACTIVE_TMP"
LOCAL_TMP=$(mktemp)
printf '%s\n' "$NEXT" >"$LOCAL_TMP"
sudo install -o root -g root -m 0600 "$LOCAL_TMP" "$ACTIVE_TMP"
rm -f -- "$LOCAL_TMP"
sudo mv --no-target-directory "$ACTIVE_TMP" "$ACTIVE"
sudo /usr/bin/python3 -I -B -c \
  'import os,sys; f=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY); os.fsync(f); os.close(f)' \
  "$STAGE"
gate arm --data-dir "$RUNTIME" --release-sha "$RELEASE_SHA"
printf 'attempt %s archived; fresh attempt %s armed and blocked\n' \
  "$CURRENT" "$NEXT" >&2
