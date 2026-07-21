# 运行态证据与限制

记录时间：2026-07-21 19:35–20:26（Asia/Shanghai）。外部审查者无法直接进入服务器，因此这些内容只作为审查输入，不替代源码证明。

## 已验证状态

- `trading.service`：active/running，主进程未因资源监控修复而重启。
- `trading-mem-monitor.service`：active/running，`NRestarts=0`。
- `cloudflared.service`：active。
- 资源监控日志实际写入 `/var/log/trading-mem-monitor/mem_monitor.log`，权限为 0600。
- 资源监控成功启动后，交易服务与监控服务均无新的 error 级 journal 记录。
- 提交 `5acff0071280425ba40f6e71f525e30ea6a35a25` 的应用源码曾与生产服务器对应源码哈希一致；本次候选修复尚未部署。

## 生产服务器当前状态（候选修复尚未部署）

- `trading-state-backup.timer` 显示 enabled，但自 2026-07-20 13:40 被停止后一直 inactive/dead。
- 最后一次定时备份成功时间为 2026-07-20 07:50。
- 当前备份脚本检查 `/home/ubuntu/trader/trading/.runtime/runner.lock`。
- 当前应用实际由 `main.acquire_runner_lock()` 持有 `/tmp/trading_system_runner.okx.lock`；服务器证据显示 Gunicorn 持有后者，旧 `.runtime` 文件只是遗留普通文件。
- 因此旧备份脚本的 runner-lock 证明已经失真，不应仅执行 `systemctl start trading-state-backup.timer` 就宣称恢复。

服务器预检确认：交易主进程仍为 PID `272701`，资源监控仍为原版本，备份 timer 仍为 inactive/dead。本次只读预检后按用户要求暂停部署，没有上传、替换、重启或启动 timer。

## 当前分支的待复审候选修复

- `ops/sbin/trading-state-backup` 删除旧 runner lock 检查和无用 fd 清理，共净减 11 行；保留备份自身并发锁、`systemctl stop`、停止复核、归档自校验、原子改名与服务恢复。
- `trading/mem_monitor.py` 在生产路径取不到 webhook 时改为退出码 1。
- `trading/tests/unit/test_mem_monitor.py` 新增真实子进程退出码测试；完整测试由 341 增至 342 项并全部通过。

候选修复得到 Fable5 明确批准前不得部署。
