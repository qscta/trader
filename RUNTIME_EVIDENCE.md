# 运行态证据与限制

记录时间：2026-07-22 08:09–08:32（Asia/Shanghai）。外部审查者无法直接进入服务器，因此这些内容只作为审查输入，不替代源码证明。

## 已验证状态

- `trading.service`：active/running；07:50 备份后自动恢复，`NRestarts=0`。
- `trading-mem-monitor.service`：active/running，`NRestarts=0`。
- 本机 Web 请求返回 HTTP 200。
- 资源监控日志实际写入 `/var/log/trading-mem-monitor/mem_monitor.log`，权限为 0600。
- 提交 `17ef04fd5d52617faa7312449b4ff5c47efc926b` 的两项 P2 修复已经按 Fable5 批准范围部署。

## 首次自然备份与日检

- timer 于 2026-07-22 07:50:01 自然触发，07:50:02 成功退出；新归档 `state-20260722-075001-464338.tgz` 为 266,905 bytes，指定内容校验通过。
- `trading.service` 同一分钟自动恢复，08:00:05 开始检查 40 个品种，08:00:17 完成钉钉每日持仓汇总推送（HTTP 200）。
- 下一次 timer 触发时间为 2026-07-23 07:50。
- 根文件系统 40 GiB，已用 8.4 GiB，可用 30 GiB。

## 运维保留规则与部署验证

- `ops/logrotate/trading-mem-monitor` 新增 9 行标准规则：日志达到 10 MiB 后轮转，保留 5 份、压缩并以 `copytruncate` 维持监控进程的现有文件句柄。
- `ops/sbin/trading-state-backup` 新增 10 行：新归档完成、自校验、原子改名并 `sync` 后，按文件名倒序只保留最近 365 份 `state-*.tgz`；其它部署与人工存档名称不匹配，永不自动删除。
- 删除边界测试制造 370 份普通状态备份后仅删除最旧 5 份；同名目录、符号链接、嵌套文件和 `deploy-pre-*` 均保留，失败状态可传回现有退出清理。
- 隔离日志测试连续轮转 7 次后精确保留 5 份，压缩正常，原文件 UID/GID/0600 权限保持；生产配置的 `logrotate --debug` 也解析通过。
- 应用全量 342 项测试、Python 编译、ruff、前端语法、Bash 语法与内容哈希清单均通过。

两项规则于 2026-07-22 无停机部署：交易 PID 部署前后均为 `464363`，`NRestarts=0`；12 份现有状态备份数量未变，Web 返回 200，服务近 10 分钟无 error 日志。交易 Python、前端、systemd 单元和测试代码均未修改。
