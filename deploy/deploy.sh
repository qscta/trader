#!/usr/bin/env bash
# 生产部署脚本（服务器端执行）。
#
# 由 .github/workflows/deploy.yml 经 SSH 以 `bash -s` 方式送入执行，
# 版本与所部署 SHA 严格一致；也可在服务器上人工运行（导出同名环境变量）。
#
# 安全顺序（测试先行，全绿前不碰在跑代码）：
#   1. 校验：工作树必须干净、目标 SHA 必须可达、venv 必须已存在；
#   2. 临时 git worktree 内先跑全量测试（485 stdlib + 113 依赖版）；
#      requirements.lock 有变更时改用一次性 venv 测试，绝不先污染生产 venv；
#   3. 全绿后才切换生产代码（钉死到确切 SHA）、安装锁定依赖、重启服务；
#   4. 健康检查（systemctl is-active + 可选自定义命令）；
#   5. 失败自动回退上一 SHA 并重启复检：回退成功 exit 2，回退仍不健康 exit 3。
#
# 必需环境变量：
#   TARGET_SHA   要部署的完整 commit SHA
#   APP_DIR      服务器上的仓库检出目录（如 /opt/trader）
#   SERVICES     空格分隔的 systemd 服务单元（如 "trading.service trading-api.service"）
# 可选：
#   VENV         生产虚拟环境目录（默认 $APP_DIR/venv）
#   HEALTH_CMD   额外健康检查命令（bash -c 执行，exit 0 = 健康）
#   HEALTH_WAIT  重启后等待秒数再做健康检查（默认 8）
#
# sudo 面：仅需要 `systemctl restart <各单元>`（sudoers 配置见 deploy/README.md）。
set -euo pipefail

log() { printf '[deploy %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }
fail() { log "ERROR: $*"; exit 1; }

TARGET_SHA=${TARGET_SHA:-}
APP_DIR=${APP_DIR:-}
SERVICES=${SERVICES:-}
VENV=${VENV:-}
HEALTH_CMD=${HEALTH_CMD:-}
HEALTH_WAIT=${HEALTH_WAIT:-8}

[[ -n $TARGET_SHA && -n $APP_DIR && -n $SERVICES ]] || \
    fail '缺少必需环境变量 TARGET_SHA / APP_DIR / SERVICES'
cd "$APP_DIR" || fail "无法进入 APP_DIR: $APP_DIR"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || \
    fail "$APP_DIR 不是 git 仓库"
VENV=${VENV:-$APP_DIR/venv}
[[ -x $VENV/bin/python ]] || \
    fail "生产 venv 不存在: $VENV（首次部署请先人工创建并安装 requirements.lock）"

# 服务器上的任何本地改动都必须先人工裁决，自动流程绝不覆盖。
[[ -z $(git status --porcelain) ]] || \
    fail '服务器工作树不干净，拒绝部署（先人工处理本地改动/未跟踪文件）'

PREV_SHA=$(git rev-parse HEAD)
log "当前生产 SHA: $PREV_SHA"
log "目标部署 SHA: $TARGET_SHA"
if [[ $PREV_SHA == "$TARGET_SHA" ]]; then
    log '目标与当前一致；仍将重装锁定依赖、重启并健康复检（幂等重部署）'
fi

git fetch origin --quiet
git cat-file -e "$TARGET_SHA^{commit}" 2>/dev/null || \
    fail "目标 SHA 在 fetch 后仍不可达: $TARGET_SHA"

# ---------- 阶段一：临时 worktree 内验证，全绿前不碰在跑代码 ----------
TEST_DIR=$(mktemp -d /tmp/trader-deploy-test.XXXXXX)
TEST_VENV=''
cleanup() {
    git worktree remove --force "$TEST_DIR" >/dev/null 2>&1 || true
    rm -rf "$TEST_DIR"
    [[ -n $TEST_VENV ]] && rm -rf "$TEST_VENV"
}
trap cleanup EXIT

git worktree add --detach --quiet "$TEST_DIR" "$TARGET_SHA"

TEST_PY="$VENV/bin/python"
if ! git diff --quiet "$PREV_SHA" "$TARGET_SHA" -- trading/requirements.lock; then
    # 锁文件有变更：用一次性 venv 验证新依赖，生产 venv 在测试全绿前保持原样。
    log 'requirements.lock 有变更，构建一次性测试 venv（利用 pip 缓存）'
    TEST_VENV=$(mktemp -d /tmp/trader-deploy-venv.XXXXXX)
    "$VENV/bin/python" -m venv "$TEST_VENV/venv"
    TEST_PY="$TEST_VENV/venv/bin/python"
    "$TEST_PY" -m pip install --quiet --upgrade pip
    "$TEST_PY" -m pip install --quiet -r "$TEST_DIR/trading/requirements.lock"
fi

log '阶段一：worktree 内跑全量测试（stdlib + 依赖版）'
( cd "$TEST_DIR/trading" && "$TEST_PY" -m unittest discover -s . -p 'test_*.py' ) || \
    fail '目标 SHA 的 stdlib 测试未通过，生产代码未被触碰'
( cd "$TEST_DIR/trading" && "$TEST_PY" -m unittest tests.test_trading_logic_unittest ) || \
    fail '目标 SHA 的依赖版测试未通过，生产代码未被触碰'
log '阶段一通过：目标 SHA 测试全绿'

restart_services() {
    local unit
    for unit in $SERVICES; do
        sudo -n systemctl restart "$unit" || return 1
    done
}

health_check() {
    local unit
    sleep "$HEALTH_WAIT"
    for unit in $SERVICES; do
        if ! systemctl is-active --quiet "$unit"; then
            log "健康检查失败: $unit 非 active"
            return 1
        fi
    done
    if [[ -n $HEALTH_CMD ]]; then
        if ! bash -c "$HEALTH_CMD"; then
            log "健康检查失败: 自定义命令非零退出: $HEALTH_CMD"
            return 1
        fi
    fi
    return 0
}

activate_sha() {
    # 钉死到确切 SHA（detached HEAD）：生产运行的永远是被批准的那个提交，
    # 不随分支后续提交漂移。
    git -c advice.detachedHead=false checkout --quiet --detach "$1"
    "$VENV/bin/python" -m pip install --quiet -r trading/requirements.lock
    restart_services
}

# ---------- 阶段二：切换生产代码 + 重启 + 健康检查 ----------
log '阶段二：切换生产代码并重启服务'
activate_sha "$TARGET_SHA" || fail '切换/安装/重启过程失败——立即人工检查服务状态'

if health_check; then
    log "部署成功：生产现运行 $TARGET_SHA"
    exit 0
fi

# ---------- 回退 ----------
log "健康检查失败，自动回退到 $PREV_SHA"
if activate_sha "$PREV_SHA" && health_check; then
    log "回退成功：生产已恢复运行 $PREV_SHA；本次部署失败，请检查目标版本"
    exit 2
fi
log '回退后仍不健康——生产可能处于故障状态，立即人工介入！'
exit 3
