# 生产运维定义（脱敏审查快照）

这些文件从生产服务器只读导出，供外部审查者检查服务边界。它们不包含 `/etc/trading.env`、`/etc/trading-mem-monitor.env`、Cloudflare 隧道 token 或任何应用数据。

- `systemd/trading.service` 与 drop-in：当前交易进程定义。
- `systemd/trading-mem-monitor.service`：当前资源监控定义。
- `systemd/trading-state-backup.service`、`.timer`：当前启用的每日 07:50 冷备份定义。
- `sbin/trading-state-backup`：当前脚本；只保留最近 365 份普通 `state-*.tgz` 文件。
- `logrotate/trading-mem-monitor`：当前资源监控日志轮转规则；10 MiB 触发，保留 5 份并压缩。

2026-07-22 的首次自然定时备份已经成功并在约 1 秒内恢复交易服务。上述两项保留规则随后无停机部署：交易 PID 未变、现有备份未删、Web 返回 200；交易应用代码零改动。
