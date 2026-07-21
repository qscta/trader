# 生产运维定义（脱敏只读快照）

这些文件从生产服务器只读导出，供外部审查者检查服务边界。它们不包含 `/etc/trading.env`、`/etc/trading-mem-monitor.env`、Cloudflare 隧道 token 或任何应用数据。

- `systemd/trading.service` 与 drop-in：当前交易进程定义。
- `systemd/trading-mem-monitor.service`：当前资源监控定义。
- `systemd/trading-state-backup.service`、`.timer`：当前但已停用的生产定义。
- `sbin/trading-state-backup`：删除旧 runner lock 空证明后的候选修复，尚未部署。

请勿把本目录直接当成部署包安装。先按 `FABLE5_FOLLOWUP_REVIEW.md` 复审，批准后再部署并恢复 timer。
