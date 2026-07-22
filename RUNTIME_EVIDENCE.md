# 运行态证据与限制

记录时间：2026-07-22 08:09–08:12（Asia/Shanghai）。外部审查者无法直接进入服务器，因此这些内容只作为审查输入，不替代源码证明。

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

## 当前分支的待复审候选差异

- `ops/logrotate/trading-mem-monitor` 新增 9 行标准规则：日志达到 10 MiB 后轮转，保留 5 份、压缩并以 `copytruncate` 维持监控进程的现有文件句柄。
- `ops/sbin/trading-state-backup` 新增 10 行：新归档完成、自校验、原子改名并 `sync` 后，按文件名倒序只保留最近 365 份 `state-*.tgz`；其它部署与人工存档名称不匹配，永不自动删除。
- 隔离测试制造 370 份状态备份后仅删除最旧 5 份，最新 365 份和 `deploy-pre-*` 均保留。
- 生产服务器上的 `logrotate --debug` 成功解析候选配置，识别 10 MiB 阈值和 5 份保留；debug 模式未执行轮转。
- 应用全量 342 项测试、Python 编译、ruff、前端语法、Bash 语法与内容哈希清单均通过。

候选差异合计 19 行生产运维内容，交易 Python、前端、systemd 单元和测试代码均未修改；复审批准前不部署。
