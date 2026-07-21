# 生产运维定义（脱敏只读快照）

这些文件从生产服务器只读导出，供外部审查者检查服务边界。它们不包含 `/etc/trading.env`、`/etc/trading-mem-monitor.env`、Cloudflare 隧道 token 或任何应用数据。

- `systemd/trading.service` 与 drop-in：当前交易进程定义。
- `systemd/trading-mem-monitor.service`：当前资源监控定义。
- `systemd/trading-state-backup.service`、`.timer` 与 `sbin/trading-state-backup`：当前但已停用的自动备份链路。

请勿把本目录直接当成部署包安装。尤其是备份链路存在 `RUNTIME_EVIDENCE.md` 所述的旧 runner lock 不匹配问题。
