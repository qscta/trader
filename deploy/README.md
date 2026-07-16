# 生产部署管道（Actions + SSH + 人工审批门）

流程：手动触发 workflow → CI 跑全量 stdlib 测试并钉死待部署 SHA →
**你在 GitHub 上一键批准** → Actions 经 SSH 把 `deploy/deploy.sh` 送到
服务器执行（测试先行、全绿才切换、健康检查失败自动回退）。

服务器私钥只存放在 GitHub Secrets；批准前部署不会开始——README 主文档
「模拟盘实弹验证后才上生产」的纪律由你在批准这一步把关。

## 一次性配置

### 1. 服务器侧（root 执行一次）

```bash
# 专用部署用户（无密码登录，仅限密钥）
useradd -m -s /bin/bash deploy

# sudo 面收窄到「重启交易服务」这一件事（按实际单元名替换）
cat > /etc/sudoers.d/deploy-trader <<'SUDO'
deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart <主服务>.service, \
    /usr/bin/systemctl restart <API 服务>.service, \
    /usr/bin/systemctl restart trading-mem-monitor.service
SUDO
chmod 440 /etc/sudoers.d/deploy-trader

# 部署密钥（在本地生成，公钥上服务器，私钥进 GitHub Secrets 后删除本地副本）
ssh-keygen -t ed25519 -f trader_deploy_key -N '' -C 'github-actions-deploy'
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
install -m 600 -o deploy -g deploy /dev/null /home/deploy/.ssh/authorized_keys
cat trader_deploy_key.pub >> /home/deploy/.ssh/authorized_keys

# deploy 用户需要对应用目录可写（git checkout / pip install 到 venv）
chown -R deploy:deploy <APP_DIR>
```

采集主机公钥（在**本地**执行并人工核对指纹，防中间人）：

```bash
ssh-keyscan -p <端口> <服务器IP或域名> 2>/dev/null
```

### 2. GitHub 仓库配置

Settings → Secrets and variables → Actions：

| Secret | 内容 |
|---|---|
| `DEPLOY_HOST` | 服务器 IP / 域名 |
| `DEPLOY_USER` | `deploy` |
| `DEPLOY_SSH_KEY` | `trader_deploy_key` 私钥全文 |
| `DEPLOY_KNOWN_HOSTS` | 上面 ssh-keyscan 的输出（核对过指纹） |
| `DEPLOY_PORT`（可选） | SSH 端口，默认 22 |

| Variable | 内容 |
|---|---|
| `DEPLOY_APP_DIR` | 服务器上的仓库目录，如 `/opt/trader` |
| `DEPLOY_SERVICES` | 空格分隔的 systemd 单元，如 `trading.service trading-api.service` |
| `DEPLOY_VENV`（可选） | 生产 venv 路径，默认 `$APP_DIR/venv` |
| `DEPLOY_HEALTH_CMD`（可选） | 额外健康检查命令，exit 0 = 健康 |
| `DEPLOY_HEALTH_WAIT`（可选） | 重启后等待秒数，默认 8 |

Settings → Environments → 新建 `production` → 勾选 **Required reviewers**
→ 加上你自己。这就是审批门：每次部署 workflow 会暂停等你批准。

### 3. 首次演练

先在 Actions 页手动跑一次 `deploy`，`ref` 填当前生产已在跑的 SHA——
等价于幂等重部署，用它验证整条链路（SSH、sudo、健康检查）而不改变
生产版本。

## 语义说明

- **测试先行**：`deploy.sh` 在临时 git worktree 里先跑全量测试
  （492 stdlib + 113 依赖版），全绿前不碰在跑代码；`requirements.lock`
  有变更时用一次性 venv 验证，绝不先污染生产 venv。
- **钉死 SHA**：批准的是哪个提交，部署的就是哪个提交（服务器检出
  detached HEAD），分支在审批等待期间的新提交不会被夹带。
- **自动回退**：重启后健康检查失败 → 自动回到上一 SHA 并重启复检；
  回退成功 exit 2，回退仍不健康 exit 3（立即人工介入）。
- **拒绝脏树**：服务器工作树有本地改动时部署直接中止，绝不覆盖。
