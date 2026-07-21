# OKX 双均线系统 — Fable5 最终审查快照

这是一个与历史分支隔离的审查快照，内容来自 2026-07-21 生产部署后的“神话终局”版本。
策略仅保留双均线 EMA；止损仍使用前 N 根已收盘日线收盘价的最高/最低值，这是明确保留的设计，不是海龟残留。

## 审查范围

- `trading/`：当前应用源码、前端、示例配置和 341 项测试。
- `ops/systemd/`：从服务器只读导出的、不含密钥的 systemd 单元。
- `ops/sbin/trading-state-backup`：服务器当前安装的备份脚本。
- `FABLE5_FINAL_REVIEW_GUIDE.md`：最终审查目标、门禁和输出要求。
- `RUNTIME_EVIDENCE.md`：无法由外部审查者直接复验的运行态事实及已知运维问题。
- `SOURCE_MANIFEST.sha256`：受审文件内容清单。

本分支不包含 `config.json`、环境文件、API Key、Webhook、账本、持仓明细、日志或备份包。

## 本地门禁

在 `trading/` 中执行：

```bash
python -m unittest discover -s tests -p 'test_*.py'
python -m compileall -q .
ruff check --no-cache --select F,E9 .
node --check static/app.js
```

当前受审快照的完整测试结果：`Ran 341 tests`，`OK`。

## 重要边界

- 不允许把交易所人工仓位自动认领为系统仓位；证据不完整时必须隔离、告警、禁止新开仓。
- 从品种池删除或禁用但仍有持仓的品种，只管理到当前仓结束，不反手、不重入。
- 生产环境必须保持单一 Gunicorn worker。
- `TRADING_DISABLE_NEW_OPENS=1` 是维护期新开仓总闸；平仓、止损巡检和止损自愈不受它影响。
- `ops/` 是审查证据，不是可以未经复核直接安装的部署包；已知备份链路问题见 `RUNTIME_EVIDENCE.md`。
