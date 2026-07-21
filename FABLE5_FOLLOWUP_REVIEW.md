# Fable5 两项 P2 候选修复复审

请独立审查本分支相对首轮提交 `5acff0071280425ba40f6e71f525e30ea6a35a25` 的差异。服务器尚未部署，只有 GitHub 候选代码发生变化。

最高原则仍是：**稳定、安全、纯净、精简**。不得借此扩张状态机或顺手重构其他代码。

## 候选修复 A：备份脚本收缩

文件：`ops/sbin/trading-state-backup`

- 删除旧 `.runtime/runner.lock` 的存在性、符号链接与 `flock` 检查。
- 删除对应的无用 `exec 8>&-`。
- 预期净变化：0 行新增、11 行删除。
- 保留：备份任务自身的 `/run/trading-state-backup/backup.lock`、`systemctl stop`、`is-active` 停止复核、临时归档、`tar -tzf` 内容自校验、原子改名、失败清理与交易服务恢复。
- 禁止提出“改指 `/tmp/trading_system_runner.okx.lock`”；备份服务有 `PrivateTmp=true`。

请判断删除旧锁后，在“正式交易程序只由 systemd 托管”的既定运维边界内，是否没有降低实际安全性，并确认 Bash 清理路径不存在未定义 fd 或退出状态回归。

## 候选修复 B：资源监控 fail-loud

文件：`trading/mem_monitor.py`、`trading/tests/unit/test_mem_monitor.py`

- 生产路径取不到 webhook 时由 `return` 改为 `sys.exit(1)`。
- 新测试在独立子进程中强制 `load_webhook()` 返回 `None`，要求退出码为 1。
- `trading-mem-monitor.service` 的 `Restart=on-failure` 与 `StartLimitBurst=3` 应使误配显式失败且限制重试风暴。

请确认测试不是空壳，不会读取真实 webhook、不会进入无限监控循环，也不会改变 webhook 配置正确时的正常运行行为。

## 必须亲自执行

```bash
cd trading
python -m unittest -v tests.unit.test_mem_monitor
python -m unittest discover -s tests -p 'test_*.py'
ruff check --no-cache --select F,E9 .
node --check static/app.js
cd ..
bash -n ops/sbin/trading-state-backup
sha256sum -c SOURCE_MANIFEST.sha256
```

同时确认：运行时代码海龟零命中、无敏感信息、新改动仅限上述两个 P2 和审查文档。

## 输出要求

只需给出：

1. 两项修复分别 `APPROVE` 或 `REJECT`；
2. 若拒绝，给出精确文件、行号、可复现路径及更小的修复；
3. 是否允许 Codex 在不重启交易主服务的前提下部署监控文件与备份脚本，并只启动下一次计划时间为 07:50 的 timer。
