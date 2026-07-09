# 欧易（OKX）程序化交易系统 — 架构与上线说明

本系统由币安版迁移而来，现已收敛为「**欧易单所版**」：只对接欧易，状态、配置、前端均按单所组织。
**核心交易逻辑（海龟通道突破 / 双均线、以损定量、开仓后建止损、止损推进、平仓、平仓后状态记录、风控校验、每日 08:00 调度）与原币安已跑通版本保持等价**，本次迁移只替换交易所适配层、收敛交易所范围、调整统计展示与默认参数。

## 一、整体架构

```
        TradingSystem (main.py)               ← 单实例：加载配置、调度、执行
          ├── OkxApi      (适配器, okx_api.py)  ← 继承 ExchangeApi (exchange_base.py)
          ├── TradeState   trade_state.json     ← 本地持仓/止损/信号状态（恒定大小；平仓历史归档在 closed_trades_archive.json）
          └── EquityTracker *.json              ← 权益历史/峰值/日快照/求索指数
```

- **内部符号**统一 `BTCUSDT`，由 `to_ccxt_symbol()` 映射为欧易 `BTC/USDT:USDT`。
- **仓位单位**：上层与本地状态始终用「币的数量」；欧易「张数」换算只在 `okx_api.py` 下单边界内部发生，不外泄，保证风控/盈亏口径与原版一致。
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
  "strategy": { "ma_short_period": 7, "ma_long_period": 28, "channel_period": 28, ... },
  "trading":  { "symbols": [ {"name": "BTCUSDT", "enabled": true, "strategy": "turtle", ...} ] },
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
- **求索指数数据存储**：每天切日时把当天的 5 分钟采样压缩成一根日线 OHLC 永久存入 `daily_equity.json`，并清空当天之前的原始采样——`equity_ticks.json` 永远只存当天，历史十年也仅几百 KB，「全部」是完整历史的真蜡烛。`equity_tick_retention_days`（默认 30）仅作压缩失效时的兜底。
- 排行榜（交易对动态池）已彻底删除。样式内联在 `index.html`。

### 删除交易对的语义
- 删除 ≠ 平仓、≠ 停止托管。正确语义：**从品种池移除，之后不再为该交易对开新仓**；`DELETE /api/symbols/<symbol>` 只改 `config.trading.symbols`，**不动** `trade_state` 持仓、不撤止损单、不平仓。
- 如本地已有持仓，`check_and_execute_trades()` 会继续把 `trade_state` 里的持仓 symbol 加入检查集合，**按持仓记录的 `strategy` 字段**（turtle / ma_cross）继续跟踪、推进止损、处理平仓，直到仓位自然结束。
- 老仓兜底：若删除时发现该持仓缺 `strategy` 字段，会先从当前配置补写进持仓再删；配置里也没有时**拒绝删除**，提示先明确策略。

### 未创新高统计口径
- **未创新高天数**（`days_since_peak`）= 距上次权益创新高的天数；当天创新高则为 0。
- **历史最长未创新高**（`longest_drawdown_days`）= 历史上两次创新高之间的最长间隔；**若当前正处于未创新高中，会把当前这段进行中的天数一起纳入比较**（`max(历史已记录, 当前未创新高天数)`）。创新高当天会先用「旧峰值时间→本次新高」结算刚结束的那段、更新历史最长，再把未创新高天数归零。
- 前端、`/api/account_stats`、钉钉周报**统一复用** `EquityTracker.build_account_stats()` 的结果，口径完全一致。

## 四、HTTP API（单所，无 exchange 参数）

`/api/login`、`/api/logout`、`/api/check_auth`、`/api/logs`、`/api/status`、`/api/positions`、
`/api/symbols`、`/api/account_stats`、`/api/equity_ohlc`(=`/api/qiusuo_index_ohlc`)、`/api/instant_open`、
`/api/close_position`、`/api/strategy_params`、`/api/equity_sync`、`/api/trades`、`/api/channel_data`、`/api/manual_check`。

> 多所时代的 `/api/exchanges`、`/api/overview`、`/api/overview_ohlc` 已删除；其余路由不再接受 `?exchange=` 或 body 里的 `exchange` 字段。

## 五、运行 / 部署

```bash
pip install -r requirements.txt   # ccxt, pandas, flask, apscheduler, requests, gunicorn
python main.py                  # 直接运行（仅交易调度，无 Web）
# 或 Web + 交易一体（推荐，gunicorn 走 wsgi）
FLASK_SECRET_KEY=xxx TRADING_LOGIN_PASSWORD=xxx gunicorn -w 1 -b 0.0.0.0:5000 wsgi:application
```

- **时区**：每日 08:00 检查等 cron 任务按**服务器系统本地时区**触发。部署前确认时区符合预期（如 `timedatectl set-timezone Asia/Shanghai`），否则 UTC 服务器上 08:00 实际是北京时间 16:00。
- 务必用 **1 个 worker**（`-w 1`）：交易线程与状态需单实例，`wsgi.py` 已用文件锁防重复启动（抢不到锁直接退出）。
- **从多所版升级（自动迁移，带边界保护）**：若状态还在 `data/okx/` 下——根目录无 `trade_state.json` 时自动迁回（顺带打 `"exchange": "okx"` 归属标记）；根目录已有但**空仓**而旧文件**有持仓**时，备份空文件后迁移旧持仓（不会被空文件绕过）；**两边都有持仓时拒绝启动**等人工裁决；任一文件读取失败也拒绝启动。首次启动后请在日志确认迁移信息并**核对持仓数量无误**再继续运行。
- **止损自愈（防裸奔红线）**：盘中巡检（默认 5 分钟）在「本地与交易所都有仓」时，会确认本地记录的止损单仍挂在交易所（方向+触发价+张数严格匹配，三态判定：intact 不动 / mismatch 告警人工 / missing 补挂）。异常状态（mismatch/补挂失败）在前端顶栏常驻红色警示，钉钉告警只在状态首次进入时发（防巡检轰炸）；**丢失则按本地止损价自动补挂**并通知——覆盖建新止损失败、人工误撤、交易所端丢单等任何原因的止损缺失。fail-safe：查询失败、残留阻断、状态不明时一律不动，只告警。前端顶栏会常驻显示止损残留阻断警示。
- **止损残留护栏（防错杀红线）**：撤旧止损采用**验证式撤销**（撤完必须在算法单列表确认目标 id 不存在）。不可确认时：标记该品种「止损残留」（持久化在 trade_state.json）、严重告警、**阻断该品种新开仓/反手/止损更新**；每日检查前自动重试清理（仅对已无持仓的品种盲撤兜底），确认清干净后解除阻断并通知。
- **状态归属护栏（防串仓红线）**：启动时校验 `trade_state.json` 顶层 `exchange` 标记——
  - 标记为 `okx`：放行；标记为其它交易所（如旧币安）：**拒绝启动**；
  - 无标记且无持仓：视为全新状态，自动打上 `okx` 标记；
  - 无标记但**已有持仓**（可能是旧币安单所版遗留状态）：**拒绝启动**，要求人工确认——确属欧易则在文件顶层加 `"exchange": "okx"` 后重启，不确定则把欧易系统部署到独立目录。绝不静默把旧币安持仓当欧易仓位管理。

## 六、⚠️ 欧易上线前务必小额 / 模拟盘验证

我无法在本机连欧易实盘联调，以下几处不同 ccxt 版本行为可能有差异，**先用最小金额或模拟盘（`sandbox: true`）实测确认再放大资金**。可用 `python verify_okx.py`（带 `--side long/short/both`）做开/平/止损全链路验证：

1. **合约张数换算（最关键）**：欧易按「张」下单，每张 = `contractSize` 个币。开一小单，核对欧易实际持仓数量与日志「≈X币」是否一致——不一致会导致仓位成倍偏差。
2. **止损算法单**：系统用 ccxt 统一参数 `stopLossPrice` + `reduceOnly` 创建条件单；确认它确实挂上、是 reduce-only、触发后市价平仓。
3. **撤止损 / 撤全部**：确认 `cancel_all_orders` 能把算法止损单一并撤掉，无残留。
4. **杠杆与单向模式**：账户须为**单向（净）持仓模式**；系统每品种首次开仓前 `set_leverage`，确认杠杆/保证金模式生效（风控以损定量，仓位价值常数倍于本金，杠杆必须够）。

## 七、测试

无需第三方依赖、可本机运行并通过（共 169 用例，已验证同进程任意顺序全绿）：

```bash
python -m unittest test_startup_smoke               # 15 通过（启动装配全链冒烟：配置校验/迁移/护栏/装配）
python -m unittest test_final_judgment              # 16 通过（时间边界/并发混沌/灾难恢复/风控性质/孤儿仓告警）
python -m unittest test_equity_drawdown             # 9 通过（未创新高统计/权益采样/资金同步）
python -m unittest test_daily_summary_delivery      # 10 通过（钉钉汇总/缓冲/去重）
python -m unittest test_symbol_removal_management   # 53 通过（删除后托管 + 异常隔离 + 各护栏 + 撤单确认 + 状态事务回滚 + 止损自愈 + 调度兜底）
python -m unittest test_okx_adapter_safety          # 33 通过（面值 fail-closed + 原生端点查询/撤销/复验 + 止损严格匹配/三态判定 + tick 对齐）
python -m unittest test_closed_trades_archive       # 8 通过（账本/史书分离：归档/降级/去重/日检接线）
python -m unittest test_config_validation           # 14 通过（三入口共享校验原语边界）
python -m unittest test_trade_state_fees            # 3 通过（手续费/盈亏）
python -m unittest test_turtle_strategy_regression  # 8 通过（海龟信号回归）
```

测试桩统一走 `_test_stubs.import_main()`：桩模块只在导入 main 的瞬间存在于 `sys.modules`，导入完成立即恢复原状，因此多个测试模块同进程任意顺序运行互不污染。

> `tests/test_trading_logic_unittest.py` 需要 pandas/ccxt/flask 才能运行（本机未装）。它已**适配单所结构**：api_server 路由测试直接 patch `api_server.trading_system`（用 `_prep_system` 补 `_config_lock`/`persist_config`/`config_file`），`filter_closed_candles` 测试改用基类 `exchange_base.ExchangeApi`，并新增 `DeleteSymbolApiTests`（4 用例：删除只动配置不平仓不撤单 / 缺 strategy 从配置补写 / 无从知晓策略时拒删）。请在依赖齐全的环境运行验证：
> ```bash
> python -m unittest tests.test_trading_logic_unittest
> ```
