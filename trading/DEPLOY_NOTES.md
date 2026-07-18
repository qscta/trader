# 部署说明：海龟下线收尾 + 账本死字段迁移（给 Codex）

分支：`claude/turtle-code-review-tbddqk`

本文覆盖两件独立的事：**(1) 代码改动**（部署分支）与 **(2) 账本数据迁移**（清除
海龟遗留死字段）。两者相互独立、无强制先后——迁移是清数据，代码不依赖数据已清。

---

## 一、本轮代码改动清单（可安全部署，除一处通知展示外全部行为保持）

| 提交 | 内容 | 行为影响 |
|------|------|---------|
| `9751da5` | 通知层 `notify_signal_missed` 不再读海龟唐奇安通道字段（上轨/下轨/中轨——对双均线信号恒为空的死代码），改展示双 EMA + N 日高低止损参考；`exchange_base` 两处注释去掉海龟"突破"词 | **唯一行为变化**：仅"信号未成交"钉钉通知的展示字段。非交易逻辑 |
| `7c738a0` | 文档：`README_OKX迁移说明.md` 去残留"突破"词；`审查说明.md` 追加"2026-07-18 海龟彻底下线"当前口径节 | 纯文档 |
| `5ffafb8` | 删除零调用的死方法 `main.is_symbol_quarantined`（自引入起从未接线） | 行为保持 |
| `5cda1f9` | 删除两个死参数：`notify_position_summary(symbols_config)`、`handle_open_position_ma_cross(df)`（体内均未用），同步更新测试桩 | 行为保持 |
| `34646d2` | 新增 `migrate_signal_states.py` + `test_signal_states_migration.py`（迁移工具，不参与运行时） | 无（新文件） |

> 分支同时携带更早未合并到 `main` 的海龟移除系列与加固历史；具体部署基线（prod
> 当前在哪个提交、要不要整分支上线）由你/Codex 决定，本文不替你判断。

**部署前验证**（两条命令须全绿）：
```bash
cd trading
python3 -m unittest discover -s . -p "test_*.py"     # 期望：481 OK（纯标准库）
.venv/bin/python -m unittest tests.test_trading_logic_unittest   # 期望：103 OK（需 flask/pandas/ccxt）
```

---

## 二、账本死字段迁移 runbook

**背景**：海龟下线后代码不再读写 `signal_states` 里的 `mid_line_crossed` /
`signal_execution`，但 `mark_candle_processed` 用 `setdefault` 逐键更新，旧账本
已写入的死字段会永久滞留。迁移脚本纯删这两个键，保留 `last_processed_candle` /
`strategy` / `last_update` 等 live 字段，幂等。生产现状：活账本 38 品种均带
`mid_line_crossed`（无 `signal_execution`）。

**只迁移活账本这一对**：`/home/ubuntu/trader/trading/trade_state.json` 及其
`.bak`。`trader_deploy_backups/`、`trader_backups/`、`deploy_backups/` 里的是冻结
的历史回滚点，**不要动**。

**安全前提**：迁移读-改-写对交易服务不是原子的，须在写入方停下时做。停 app 期间
仓位仍受 **OKX 交易所侧 reduce-only 止损单**保护，app 巡检只是二次兜底。

```bash
# 1) 停交易服务（写入方）
sudo systemctl stop trading.service
systemctl is-active trading.service                       # 期望 inactive
ps -eo pid,cmd | grep -Ei 'gunicorn|wsgi' | grep -v grep  # 期望无输出

# 2) 手动全量备份活账本（脚本自身还会再留一份 .premigrate 备份，这是额外保险）
cd /home/ubuntu/trader/trading
ts=$(date +%Y%m%d_%H%M%S)
cp -av trade_state.json      trade_state.json.manualbak.$ts
cp -av trade_state.json.bak  trade_state.json.bak.manualbak.$ts

# 3) 干跑（只分析，不写盘）——核对：两个文件各列出 ~38 品种「将删除 mid_line_crossed」
.venv/bin/python3 migrate_signal_states.py --data-dir "$PWD"

# 4) 确认干跑无误后再执行写入（写前自动备份 + 原子写，主文件与 .bak 各清各的）
.venv/bin/python3 migrate_signal_states.py --data-dir "$PWD" --apply

# 5) 启服务并确认健康
sudo systemctl start trading.service
systemctl is-active trading.service                       # 期望 active
journalctl -u trading.service -n 50 --no-pager            # 期望正常启动、无账本校验报错
```

**迁移后自检**（应显示两个活账本文件均「无（账本已干净）」）：
```bash
for L in /home/ubuntu/trader/trading/trade_state.json /home/ubuntu/trader/trading/trade_state.json.bak; do
  echo "== $L =="
  python3 - "$L" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); ss=d.get('signal_states') or {}
hits={s:[k for k in ('mid_line_crossed','signal_execution') if isinstance(r,dict) and k in r] for s,r in ss.items()}
print("  含死字段的品种:", {s:v for s,v in hits.items() if v} or "无（账本已干净）")
PY
done
```

---

## 三、明确**不做**的事：保留 trade_state 的遗留字段校验

`trade_state.py` 中 `_validate_state` 对 `mid_line_crossed` / `signal_execution`
的约 38 行类型校验**建议保留，不要删**。理由：

- 它不是防当前崩溃（未知键本就被容忍加载），而是 **fail-closed 防线**——若日后
  回滚到某个仍带这些字段的历史备份（`trader_deploy_backups/` 等目录里都有），
  这段校验保证"要么干净加载、要么大声拒绝畸形值"。
- 你手里保留着多份带旧字段的部署备份，这道防线便宜且真实相关。
- 删它还会连带需要改一个断言"拒绝畸形 signal_execution"的 fail-closed 测试
  （`test_api_process_persistence_safety.test_schema_rejects_bool_for_optional_numeric_state`）。

净收益是 38 行只在加载时跑一次的代码，代价是放松一条 fail-closed 不变量——在真钱
系统 + 保留旧备份的现实下不划算。

---

## 四、回滚

- **代码**：`git revert` 对应提交，或部署基线切回上一个 tag/commit。
- **账本迁移**：从 step 2 的 `trade_state.json.manualbak.<ts>` / `.bak.manualbak.<ts>`
  复制回原名即可；脚本自动留的 `trade_state.json.premigrate.<ts>` 是同一份原始态。
  回滚账本前同样先 `systemctl stop trading.service`，回滚后再 `start`。
