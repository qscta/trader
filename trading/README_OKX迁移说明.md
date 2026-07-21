# 欧易（OKX）程序化交易系统 — 架构与上线说明

本系统由币安版迁移而来，现已收敛为「**欧易单所版**」：只对接欧易，状态、配置、前端均按单所组织。
**核心交易逻辑为双均线 EMA：以损定量、开仓后挂入场时确定的前 N 日收盘高/低止损、反向交叉翻转、平仓记账与每日 08:00 调度。**

## 一、整体架构

```
        TradingSystem (main.py)               ← 单实例：加载配置、调度、执行
          ├── OkxApi      (适配器, okx_api.py)  ← 继承 ExchangeApi (exchange_base.py)
          ├── TradeState   trade_state.json     ← 本地持仓/止损/残留阻断（恒定大小；平仓历史归档在 closed_trades_archive.json）
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
  "strategy": { "ma_short_period": 7, "ma_long_period": 28, "ma_stop_period": 28, ... },
  "trading":  { "symbols": [ {"name": "BTCUSDT", "enabled": true, "strategy": "ma_cross", ...} ] },
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
- 如本地已有持仓，`check_and_execute_trades()` 会继续把 `trade_state` 里的持仓 symbol 加入检查集合，核对并补挂既定止损、处理平仓，直到仓位结束；反向信号只平旧仓，不开新仓。
- 品种被设为“禁用”且已有持仓时采用同一语义：只管理到当前仓结束，不反手、不重入。
- 老仓兜底：若删除时发现该持仓缺 `strategy` 字段，会先从当前配置补写进持仓再删；配置里也没有时**拒绝删除**，提示先明确策略。

### 未创新高统计口径
- **未创新高天数**（`days_since_peak`）= 距上次权益创新高的天数；当天创新高则为 0。
- **历史最长未创新高**（`longest_drawdown_days`）= 历史上两次创新高之间的最长间隔；**若当前正处于未创新高中，会把当前这段进行中的天数一起纳入比较**（`max(历史已记录, 当前未创新高天数)`）。创新高当天会先用「旧峰值时间→本次新高」结算刚结束的那段、更新历史最长，再把未创新高天数归零。
- 前端、`/api/account_stats`、钉钉周报**统一复用** `EquityTracker.build_account_stats()` 的结果，口径完全一致。

## 四、HTTP API（单所，无 exchange 参数）

`/api/login`、`/api/logout`、`/api/check_auth`、`/api/logs`、`/api/status`、`/api/positions`、
`/api/symbols`、`/api/account_stats`、`/api/equity_ohlc`(=`/api/qiusuo_index_ohlc`)、`/api/instant_open`、
`/api/close_position`、`/api/strategy_params`、`/api/equity_sync`、`/api/trades`、`/api/manual_check`。

> 多所时代的 `/api/exchanges`、`/api/overview`、`/api/overview_ohlc` 已删除；其余路由不再接受 `?exchange=` 或 body 里的 `exchange` 字段。

## 五、运行 / 部署

```bash
pip install -r requirements.txt   # ccxt, pandas, flask, apscheduler, requests, gunicorn
python main.py                  # 直接运行（仅交易调度，无 Web）
# 或 Web + 交易一体（推荐，gunicorn 走 wsgi）
FLASK_SECRET_KEY=xxx TRADING_LOGIN_PASSWORD=xxx gunicorn -w 1 --timeout 0 -b 0.0.0.0:5000 wsgi:application
```

- **时区**：每日 08:00 检查等 cron 任务按**服务器系统本地时区**触发。部署前确认时区符合预期（如 `timedatectl set-timezone Asia/Shanghai`），否则 UTC 服务器上 08:00 实际是北京时间 16:00。
- 务必用 **1 个 worker**（`-w 1`）、显式 `--timeout 0`，并且**禁止 `--preload`**：本系统的 Web 与交易调度同进程，固定 worker 超时可能在网络重试或订单/记账链中途杀掉唯一 worker；客户端请求超时交给外层 HTTPS 反代。预加载会在 Gunicorn master 中导入并启动交易线程，fork 后该线程不会留在 worker。`main.py`、`api_server.py` 和 `wsgi.py` 均在初始化前争用同一文件锁，重复实例会拒绝启动。
- **状态迁移只允许人工执行**：当前版本不会自动移动任何状态文件。部署前若发现状态仍在其它目录，停止旧进程，逐份备份并核对交易所实仓后再复制到项目根目录；存在两份账本时禁止自动选择。
- **账本备份只作人工候选**：主账本缺失、损坏或不可读时一律拒绝启动；`.bak` 是上一次保存前的旧版本，可能漏掉刚开仓或复活刚平仓，代码不会自动恢复。必须停机对照交易所实仓后人工裁决。
- **部署期开仓总闸**：首次启动新代码时设置 `TRADING_DISABLE_NEW_OPENS=1`。它只拦所有自动、即时、重入和反手的新开仓，不影响持仓巡检、止损自愈和平仓。完成只读核对后方可解除。
- **持仓模式硬门禁**：启动会尝试切到单向净持仓，并读取账户配置证明 `posMode=net_mode`；查询失败或模式不符均拒绝启动。
- **调度存活门禁**：主循环每分钟确认 APScheduler 线程仍存活；线程停止或交易主循环退出时，Web 进程会随即退出，由服务管理器整体重启，不允许只剩“能打开但不交易”的网页。
- **止损自愈（防裸奔红线）**：新止损只有在待触发清单中确认“唯一一张、方向/触发价/张数/reduce-only 全匹配”才算创建成功；超时只查询、不重发。盘中巡检（默认 5 分钟）在「本地与交易所都有仓」时再次做同样核验：intact 不动 / mismatch 隔离并告警人工 / missing 按本地固定止损价补挂。查询失败、残留阻断或状态不明时一律不猜。
- **止损残留护栏（防错杀红线）**：撤旧止损采用**验证式撤销**（撤完必须在算法单列表确认目标 id 不存在）。不可确认时：标记该品种「止损残留」（持久化在 trade_state.json）、严重告警、**阻断该品种新开仓和反手**；每日检查前自动重试清理，确认清干净后解除阻断并通知。
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

测试与生产代码物理分离：`tests/unit/`（零依赖）+ `tests/integration/`（需
flask/pandas/ccxt）。在 `trading/` 目录下运行：

```bash
# 零依赖套件（无需安装任何第三方库；已验证同进程正序/倒序全绿）
python -m unittest discover -s tests/unit -p "test_*.py"

# 依赖版套件（交易逻辑 / 路由集成）
pip install -r requirements.txt
python -m unittest discover -s tests/integration -p "test_*.py"
```

单独跑某个零依赖模块（`tests/unit` 入 path 供 `_test_stubs`，仓库根由 CWD 提供生产模块）：

```bash
PYTHONPATH=tests/unit python -m unittest test_startup_smoke   # 启动装配全链冒烟：配置校验/护栏/装配
PYTHONPATH=tests/unit python -m unittest test_final_judgment  # 时间边界/并发混沌/灾难恢复/风控性质/孤儿仓告警
# 其余：test_equity_drawdown / test_daily_summary_delivery / test_symbol_removal_management /
#       test_okx_adapter_safety / test_closed_trades_archive / test_config_validation /
#       test_trade_state_fees / test_verify_fire_logic / test_dingtalk_notifier
```

测试桩统一走 `_test_stubs.import_main()`：桩模块只在导入 main 的瞬间存在于 `sys.modules`，导入完成立即恢复原状，因此多个测试模块同进程任意顺序运行互不污染。

> `tests/integration/test_trading_logic_unittest.py` 需要 pandas/ccxt/flask：api_server 路由测试直接
> patch `api_server.trading_system`（用 `_prep_system` 补 `_config_lock`/`persist_config`/`config_file`），
> `filter_closed_candles` 测试改用基类 `exchange_base.ExchangeApi`，含 `DeleteSymbolApiTests`
> （删除只动配置不平仓不撤单 / 缺 strategy 从配置补写 / 无从知晓策略时拒删）。
