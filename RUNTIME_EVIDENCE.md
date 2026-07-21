# 运行态证据与限制

记录时间：2026-07-21 19:35–19:40（Asia/Shanghai）。外部审查者无法直接进入服务器，因此这些内容只作为审查输入，不替代源码证明。

## 已验证状态

- `trading.service`：active/running，主进程未因资源监控修复而重启。
- `trading-mem-monitor.service`：active/running，`NRestarts=0`。
- `cloudflared.service`：active。
- 资源监控日志实际写入 `/var/log/trading-mem-monitor/mem_monitor.log`，权限为 0600。
- 资源监控成功启动后，交易服务与监控服务均无新的 error 级 journal 记录。
- 当前应用源码与生产服务器对应源码哈希一致；新增的 `mem_monitor.py` 测试也已在服务器通过。

## 已知未修复运维问题

- `trading-state-backup.timer` 显示 enabled，但自 2026-07-20 13:40 被停止后一直 inactive/dead。
- 最后一次定时备份成功时间为 2026-07-20 07:50。
- 当前备份脚本检查 `/home/ubuntu/trader/trading/.runtime/runner.lock`。
- 当前应用实际由 `main.acquire_runner_lock()` 持有 `/tmp/trading_system_runner.okx.lock`；服务器证据显示 Gunicorn 持有后者，旧 `.runtime` 文件只是遗留普通文件。
- 因此旧备份脚本的 runner-lock 证明已经失真，不应仅执行 `systemctl start trading-state-backup.timer` 就宣称恢复。

该问题不影响现有开平仓、止损、日检、前端或资源监控；它影响的是自动备份可用性与备份一致性证明。应优先用删除旧锁依赖、接受明确停机窗口的简单方案，或改为人工备份隔离，避免重新扩张部署状态机。
