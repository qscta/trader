# 欧易（OKX）程序化交易系统 — 架构与上线说明

本系统由币安版迁移而来，现已收敛为「**欧易单所版**」：只对接欧易，状态、配置、前端均按单所组织。
当前只运行双均线 EMA；同时具备下单幂等、订单终态归因、补偿回滚、原子账本、崩溃恢复、止损残留隔离与三入口配置校验。

## 一、整体架构

```
        TradingSystem (main.py)               ← 单实例：加载配置、调度、执行
          ├── OkxApi      (适配器, okx_api.py)  ← 继承 ExchangeApi (exchange_base.py)
          ├── TradeState   trade_state.json     ← 本地持仓/止损/信号状态（保留最近 200 笔平仓）
          │                                      旧平仓历史按年归档到 closed_trades_archive_YYYY.json
          └── EquityTracker *.json              ← 权益历史/峰值/日快照/求索指数
```

- **内部符号**统一 `BTCUSDT`，由 `to_ccxt_symbol()` 映射为欧易 `BTC/USDT:USDT`。
- **仓位单位**：上层与本地状态始终用「币的数量」；欧易「张数」换算只在 `okx_api.py` 下单边界内部发生，不外泄，保证风控/盈亏口径与原版一致。
- **双向事务句柄**：开仓用 `open_intent`，已有仓位的主动平仓用持仓内 `close_intent`；两者都在首次 POST 前固化基础 `clOrdId` 和计划量。平仓恢复会查询基础腿及确定性 `r1/r2`，把真实 VWAP、手续费和订单 ID 与完整/部分账本更新一次性收口。
- 状态文件直接存在**项目根目录**（不再有 `data/<交易所>/` 子目录）。

## 二、配置（config.json）

见 `config.example.json`。结构（单所、顶层即欧易）：

```json
{
  "okx": {
    "label": "欧易", "apiKey": "...", "secret": "...", "password": "...",
    "margin_mode": "cross", "leverage": 5, "leverage_overrides": {"BTCUSDT": 10},
    "sandbox": false
  },
  "strategy": { "ma_short_period": 7, "ma_long_period": 28, "ma_stop_period": 28, ... },
  "trading":  { "symbols": [ {"name": "BTCUSDT", "enabled": true, "risk_per_trade": 0.01} ] },
  "equity_tick_retention_days": 30,
  "scheduler": {...},
  "dingtalk": {"webhook_url": "..."}
}
```

- 凭据也可用环境变量：`OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSPHRASE`、`DINGTALK_WEBHOOK`。
- **向后兼容**：旧的 `{"exchanges": {"okx": {...}}}` 嵌套结构会被 `load_config()` 自动拍平为顶层 `okx`，无需手改即可运行。
- **双均线默认参数**：`ma_short_period` 默认 **7**、`ma_long_period` 默认 **28**（仅改默认值；若你的 config.json 已显式写了短周期，则按你的配置走，不强制覆盖）。

## 三、前端（单所）

- 顶部状态条直接显示「运行中 · 欧易」，无交易所切换、无汇总总览页。
- 完整管理区：KPI、求索指数图、品种池、持仓、即时开仓、策略参数、资金同步、系统日志、历史交易。
- **求索指数图**用 TradingView 官方开源库 **lightweight-charts**：蜡烛图、拖动平移、滚轮缩放、十字光标悬停显示**开/高/低/收/涨跌/涨幅/振幅**；区间可选 30/60/120/365/**全部**。库已下载到本地 `static/lwc.js`，**不依赖外网 CDN**。
- **求索指数数据存储**：切日压缩事务成功后，才把旧 5 分钟采样从 `equity_ticks.json` 裁掉，平常只保留当天；任一主文件/备份写入失败时整个交易回滚，原始 ticks 继续保留等下轮恢复。`daily_equity.json` 保留日线 OHLC，`equity_tick_retention_days` 是压缩失效时的兜底上限。
- 排行榜（交易对动态池）已彻底删除。样式内联在 `index.html`。

### 删除交易对的语义
- 删除 ≠ 立即平仓、≠ 立即停止保护。正确语义：**从品种池移除，只托管当前仓位到下一次平仓**；`DELETE /api/symbols/<symbol>` 不动当前持仓、不撤保护止损，但退池后禁止反手、止损后重入或任何新腿；当前仓一旦结束即彻底停止监控和交易。
- 如本地已有持仓，`check_and_execute_trades()` 会继续把账本中的 symbol 加入检查集合，按双均线语义继续保护和处理平仓，直到仓位自然结束。

### 未创新高统计口径
- **峰值口径（按日收盘高水位）**：`peak_equity` / `peak_time` 是「未创新高/回撤」家族的高水位线，**只允许正式日检最前面的每日收盘快照（`record_daily_equity_snapshot`）推进**；同一交易日首份快照写入后锁定，失败重试、08:01 去重触发、日检末尾统计刷新和手动「立即检查」都不能覆盖它。5 分钟按市值权益采样只维护求索指数，**绝不推进峰值**——否则持仓的日内浮盈冒一个高点就会把峰值时间刷成「现在」，`days_since_peak` 被反复清零、回撤时长指标失效。
- **未创新高天数**（`days_since_peak`）= 距上次「日收盘权益创新高」的天数；当天（当前按市值权益）已高于日收盘高水位则视为当日创新高，显示 0（仅展示、不落盘）。
- **历史最长未创新高**（`longest_drawdown_days`）= 历史上两次创新高之间的最长间隔；**若当前正处于未创新高中，会把当前这段进行中的天数一起纳入比较**（`max(历史已记录, 当前未创新高天数)`）。创新高当天会先用「旧峰值时间→本次新高」结算刚结束的那段、更新历史最长，再把未创新高天数归零。
- **峰值回撤/潜在回撤**（`peak_drawdown` / `potential_max_drawdown`）同样以该「日收盘高水位」为基准，不再被日内浮盈尖峰放大。
- 从旧版首次升级时先停 runner，执行 `python3 migrate_peak_baseline.py` 干跑核对，再执行 `python3 migrate_peak_baseline.py --apply`；脚本会按最近一次资金同步基准及其后的日收盘向下纠正旧峰值，并保留带时间戳备份。
- 前端、`/api/account_stats`、钉钉周报**统一复用** `EquityTracker.build_account_stats()` 的结果，口径完全一致。

## 四、主要 HTTP API（单所，无 exchange 参数）

`/api/login`、`/api/logout`、`/api/check_auth`、`/api/logs`、`/api/status`、`/api/positions`、
`/api/symbols`、`/api/account_stats`、`/api/equity_ohlc`(=`/api/qiusuo_index_ohlc`)、`/api/instant_open`、
`/api/close_position`、`/api/strategy_params`、`/api/equity_sync`、`/api/trades`、`/api/manual_check`。

> 多所时代的 `/api/exchanges`、`/api/overview`、`/api/overview_ohlc` 已删除；前端通道图下线后残留的
> `/api/channel_data` 孤儿路由也已删除。其余路由不再接受 `?exchange=` 或 body 里的 `exchange` 字段。

## 五、运行 / 部署

```bash
pip install -r requirements.lock  # 生产精确版本及完整传递依赖
python main.py                  # 直接运行（仅交易调度，无 Web；与 Web 共用单实例锁）
# 或 Web + 交易一体（推荐，gunicorn 走 wsgi）
export FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
TRADING_LOGIN_PASSWORD=xxx gunicorn -c gunicorn.conf.py wsgi:application
```

- **时区**：每日 08:00 检查等 cron 任务按**服务器系统本地时区**触发。部署前确认时区符合预期（如 `timedatectl set-timezone Asia/Shanghai`），否则 UTC 服务器上 08:00 实际是北京时间 16:00。
- 务必使用仓库的 `gunicorn.conf.py`：单 worker + gthread + 120 秒请求超时 + 900 秒优雅退出；`wsgi.py` 与
  `main.py`/`api_server.py` 共用项目内文件锁防重复 runner（抢不到锁直接退出）。默认仅监听
  `127.0.0.1:5000`，公网必须经 HTTPS 反代；一层可信反代需显式设置 `TRADING_PROXYFIX_X_FOR=1`。
- `FLASK_SECRET_KEY` 少于 32 字节会拒绝启动；不要使用文档占位符、密码或可猜字符串。
  若用 `TRADING_RUNNER_LOCK_FILE` 改锁路径，锁必须位于当前用户所有的专用 0700 目录，
  不能直接放在 `/tmp` 等共享目录。
- **一次性 legacy 迁移**：`data/okx/` 只在无 `.okx_legacy_migration_complete.json` 时参与裁决；迁移成功后原子写 marker，后续重启绝不再用永久旧快照复活主账本。根主账本/.bak 缺失冲突、两边存在不同生命周期数据、任一 schema/权限/符号链接异常均拒启；只在内容同源或安全空状态下迁移。
- **单策略部署预检**：停 runner 后执行 `python3 migrate_single_strategy.py --data-dir .`；确认报告后加 `--apply`，再干跑一次必须显示通过。未收口开仓意图、旧版 `signal_execution=pending`、无法证明终态的旧执行状态或不兼容在途持仓都会返回非零并阻断部署。
- **目录归属护栏**：`.trading_data_owner.json` 在加载权益/信号等辅助文件前标记整个数据目录为 `okx`。它与 `trade_state.json.exchange` 冲突、无归属但存在生命周期数据时都拒启；只有全新空目录才自动认领。
- **止损自愈（防裸奔红线）**：盘中巡检用四态裁决——`intact` 不动、`adoptable` 原子收养唯一完整新 ID、`mismatch` 隔离等人工、`missing` 补挂。部分平仓后的止损缩量采用 make-before-break：先建余仓新保护，再只撤已知旧 ID，绝不在持仓期间退化为撤全。
- **止损残留护栏（防错杀红线）**：不可确认时持久化 marker 并阻断新开仓/反手。自动清理先同时确认**本地空仓 + 交易所空仓**，再撤净普通单与全部算法类型；两类完整分页清单连续为空、普通单终态证明零成交且交易所仍空仓才解除。未知 POST 还有 10 秒可见性等待窗。
- **OKX 原生止损单**：直调 `POST /api/v5/trade/order-algo`，发送 `ordType=conditional`、`slTriggerPx`、`slOrdPx=-1`、`reduceOnly=true` 和确定性 `algoClOrdId`；每个意图最多一次 POST，ACK 或超时都只按同 ID 查询，绝不盲重发。
- **状态归属护栏（防串仓红线）**：启动时校验 `trade_state.json` 顶层 `exchange` 标记——
  - 标记为 `okx`：放行；标记为其它交易所（如旧币安）：**拒绝启动**；
  - 无标记且**整个目录无任何持仓/意图/历史/权益生命周期数据**：视为全新，自动打 `okx` 标记；
  - 无标记但存在任何生命周期数据：**拒绝启动**。人工核对全部文件确属 OKX 后再同时建立目录 owner manifest/账本归属，否则使用独立目录。

## 六、⚠️ 欧易上线前务必小额 / 模拟盘验证

代码审查与历史验证日志都不能替代**当前提交 + 当前 ccxt + 当前 OKX 账户**的重跑。模拟盘至少执行：

```bash
OKX_DEMO=1 python verify_okx.py BTCUSDT 0.01
OKX_DEMO=1 python verify_okx.py BTCUSDT 0.01 --side long --fire
OKX_DEMO=1 python verify_okx.py BTCUSDT 0.01 --side short --fire
```

`--fire` 超时未触发是“未获得证据”，不是通过；调整距离/超时后重跑。需确认：

1. **合约张数换算（最关键）**：欧易按「张」下单，每张 = `contractSize` 个币。开一小单，核对欧易实际持仓数量与日志「≈X币」是否一致——不一致会导致仓位成倍偏差。
2. **止损算法单**：确认原生 conditional 单确实挂上、是 reduce-only、触发后市价平仓且不反向开仓。
3. **撤止损 / 撤全部**：确认 `cancel_all_orders` 能把算法止损单一并撤掉，无残留。
4. **杠杆与单向模式**：账户须为**单向（净）持仓模式**；系统每品种首次开仓前 `set_leverage`，确认杠杆/保证金模式生效（风控以损定量，仓位价值常数倍于本金，杠杆必须够）。

## 七、测试

当前聚合测试矩阵以仓库内两条 unittest 命令的实时结果为准。策略行情固定读取
最新单页 300 根；配置若无法在过滤未收盘 K 线后满足最低窗口，启动/API 修改时即拒绝。
日 K 必须逐日连续，且最新已收盘 K 必须严格等于调度日 D-1；任何内部缺日、D-2
陈旧或未来错标都会 fail-closed。通过完整窗口校验后，策略仍只用最新两根判断本次
双均线交叉，不回放旧交易。
以聚合命令为准，避免模块新增后文档逐项计数漂移：

```bash
python3 -m unittest discover -s . -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

测试桩统一走 `_test_stubs.import_main()`：桩模块只在导入 main 的瞬间存在于 `sys.modules`，导入完成立即恢复原状，因此多个测试模块同进程任意顺序运行互不污染。

> 依赖版使用与生产同源的 requirements.lock（Python 3.12）。
