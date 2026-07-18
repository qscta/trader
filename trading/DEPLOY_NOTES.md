# 单策略版本部署门禁

本版本只保留双均线 EMA，部署目标是稳定、纯净、简洁。以下步骤必须在 runner
停止、交易所持仓与挂单已人工核对的维护窗口内执行。

## 1. 代码与依赖

```bash
git rev-parse HEAD
python3 --version                  # 生产要求 Python 3.12
python3 -m pip install -r requirements.lock
python3 -m pip check
```

保存当前提交号、配置、账本、账本 `.bak`、权益文件与年度平仓史书的一致性加密备份。
备份只放受控存储，不得进入 Git 或聊天工具。

## 2. 单策略预检

```bash
cd trading

# 只读分析；非零退出码即停止部署
python3 migrate_single_strategy.py --data-dir .

# 人工核对报告后才写入；脚本会先给配置、主备账本及年度史书留时间戳备份
python3 migrate_single_strategy.py --data-dir . --apply

# 幂等复验：必须返回 0 并显示已经通过
python3 migrate_single_strategy.py --data-dir .
```

以下情况脚本会 fail-closed：

- 缺少 `config.json` 或 `trade_state.json`；
- JSON 损坏、权限/符号链接不安全或规范化后 schema 非法；
- 存在未收口 `open_intent` 或旧版 `signal_execution=pending`；
- 配置带不兼容标签，或在途持仓无法证明属于双均线；平仓史书格式损坏；
- 备份、写入或失败回滚任一步骤不能确认完成。

脚本要求 `config.json` 与账本位于同一目录；`--data-dir` 最后一级目录条目不得是
符号链接或允许组/其他用户写入，敏感 JSON 必须预先为 `0600`；干跑只观察，绝不
代为修改权限。脚本不证明所有祖先目录均非符号链接，部署时必须先用 `pwd -P` 进入
受信、不可由其他用户改写的物理父目录，再传 `--data-dir .`。`--apply`
会覆盖全部运行时平仓史书（包括 `closed_trades_archive_undated.json`），并在首个正式
写入前建立 `.single_strategy_migration_journal.json`。若进程在多文件写入之间死亡，
runner 会拒绝启动；普通干跑只报告并保持现场不变，显式加 `--apply` 后迁移器才会
先从整组 `.premigrate.*` 备份恢复，再重新预检和迁移。
事务日志和备份均已被 Git 忽略且由仓库卫生测试二次阻断，但它们仍含凭据/真钱状态，
只能留在受控部署目录。

禁止手改脚本退出码或清空账本绕过阻断。先核对交易所现实，收口生命周期，再重跑。

## 3. 自动化门禁

```bash
# 纯标准库矩阵
python3 -m unittest discover -s . -p "test_*.py" -v

# 完整依赖矩阵，含真实 pandas EMA 行为
python3 -m unittest discover -s tests -p "test_*.py" -v

python3 -m compileall -q .
node --check static/app.js
```

GitHub Actions 的 Python 3.10–3.13 标准库矩阵、Python 3.12 依赖矩阵和前端语法检查
必须全部通过。任何跳过、预期失败或只在本机通过都不算部署证据。

## 4. 模拟盘门禁

当前候选提交及当前依赖必须重新执行：

```bash
OKX_DEMO=1 python verify_okx.py BTCUSDT 0.01
OKX_DEMO=1 python verify_okx.py BTCUSDT 0.01 --side long --fire
OKX_DEMO=1 python verify_okx.py BTCUSDT 0.01 --side short --fire
OKX_DEMO=1 python verify_okx.py BTCUSDT 0.01 --side long --stop-id-reuse
```

超时或结果不确定不算通过。必须人工核对币数↔张数、单向持仓模式、reduce-only
算法止损、触发平仓、撤单清单和确定性止损 ID 复用。

## 5. 启动后观察

1. 单实例锁正常，runner 与 Web 状态一致；
2. 启动对账无孤儿仓、隔离、残留或未收口意图；
3. 每个实仓稳定态只有一张数量、方向、触发价均匹配的止损；
4. 正式日检完成日期与信号 K 线标记均正常持久化；
5. 当日止损品种不重入，次日才按当前 EMA 方向重入；
6. 磁盘、日志、钉钉和账户权益无异常。

只有自动化门禁、模拟盘证据与启动后观察均通过，才允许逐步恢复真实资金流量。
