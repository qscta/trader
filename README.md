<div align="center">

# 欧易（OKX）程序化合约交易系统

**海龟通道突破 + 双均线 EMA · 以损定量 · 交易所侧算法止损 · Flask 管理台**

[![tests](https://github.com/qscta/trader/actions/workflows/tests.yml/badge.svg)](https://github.com/qscta/trader/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![tests count](https://img.shields.io/badge/tests-166%20stdlib%20%2B%2074%20deps-brightgreen.svg)](trading/tests)

</div>

单交易所（欧易 U 本位永续）程序化交易系统。两套策略并行，以损定量（默认单笔风险 1%），
止损为交易所侧 reduce-only 算法单；每日 08:00 日检、5 分钟盘中止损巡检；
Flask 管理台（亮/暗双主题）+ 钉钉通知。

> [!WARNING]
> **真实资金风险自负。** 本项目按 MIT 许可开源，仅供学习与参考，**不构成投资建议**，
> 作者不对任何盈亏承担责任。程序化合约交易可能导致本金全部损失。任何人在投入真实资金前，
> 必须先用 OKX 模拟盘（sandbox）跑通 `verify_okx.py` 全链路，并自行完整审计代码与风控。
>
> **本仓库只存放代码。** 凭据（`config.json`）、交易账本（`trade_state.json`）、权益数据、
> 日志与备份都属于部署机，已被 `.gitignore` 排除，任何时候不得提交。

## ✨ 设计要点

- **以损定量**——无论标的波动大小，单笔止损打掉的都是账户的固定比例（默认 1%）。开仓用实时市价而非信号收盘价计算仓位，成交后二次风险校验。
- **止损即交易所侧算法单**——reduce-only 条件单，触发后市价平仓；进程宕机期间止损依然生效。
- **三条防线保证「止损永远在、账本永远真」**——账本 fail-closed（损坏/误删拒绝启动，绝不失忆运行）、验证式撤单（以「查不到该单」为撤销成功标准）、止损自愈巡检（每 5 分钟三态校验，缺失自动补挂）。
- **单一事实源配置校验**——前端表单 / HTTP API / 手写 config.json 三入口由同一套 `config_validation` 原语把关，杜绝字符串混入下单路径、非法参数带病启动。
- **物理分层的清晰架构**——装配核心 + 四个 mixin（止损防线 / 通知报表 / 信号分派 / 下单执行），真钱编排集中一处便于审查。
- **240 个测试**——166 个纯标准库用例（零依赖即可跑，含并发混沌 / 灾难恢复 / 变异测试）+ 74 个依赖版集成用例。

## 🏗️ 架构

```
        TradingSystem (main.py)                装配 · 配置校验 · 状态护栏 · 日检总指挥 · 调度
          │
          ├── StopGuardianMixin    (stop_guardian.py)    止损防线：验证式撤单 / 残留阻断 / 止损自愈 / 交易所已平收尾
          ├── ReportingMixin       (reporting.py)        通知报表：开平仓汇总 / 每日持仓汇总 / 周报 / 权益采样告警
          ├── SignalHandlersMixin  (signal_handlers.py)  信号分派：无仓开仓判定 / 有仓平仓翻转 / 止损推进 / T+1 重入
          ├── TradeExecutorMixin   (trade_executor.py)   下单执行：开仓校验回滚 / 止损更新 / 平仓 / 翻转
          │
          ├── OkxApi          (okx_api.py)        欧易适配器：币数↔张数换算 / 算法止损单 / 杠杆 / 单向模式
          │     └── ExchangeApi (exchange_base.py) 适配层抽象基类：K线读取 / 收盘过滤 / 网络重试
          ├── TradeState      (trade_state.py)    本地账本：fail-closed / 原子写+fsync / 事务回滚
          ├── EquityTracker   (equity_tracker.py) 权益 / 峰值 / 回撤 / 求索指数
          ├── RiskManager     (risk_manager.py)   以损定量仓位计算
          └── DingTalkNotifier(dingtalk_notifier.py) 钉钉通知：errcode 校验 + 失败重试

        api_server.py  Flask 管理台 API（登录防爆破 / Cookie 加固 / 单一事实源输入校验）
        wsgi.py        gunicorn 入口（文件锁强制单实例）
        verify_okx.py  上实盘前的模拟盘全链路验证脚本（必跑）
        config_validation.py  三入口共享的配置校验原语（零依赖）
```

- **内部符号**统一 `BTCUSDT`，适配层映射为欧易永续 `BTC/USDT:USDT`。
- **仓位单位**上层始终用「币数」，张数换算只在下单边界内部发生、绝不外泄，保证风控/盈亏口径一致。

## 📈 两个策略

| 策略 | 开仓 | 止损 | 出场 |
|---|---|---|---|
| **海龟通道突破** `turtle` | 收盘价穿越中轨「武装」后，该方向**首次**轨道突破（默认 28 日通道） | 反向轨道，随通道单向推进（多单只上移） | 价格反向穿越中轨 |
| **双均线 EMA** `ma_cross` | EMA 短(默认 7)上穿/下穿长(默认 28)金叉做多/死叉做空，永远在市 | 前 N 日收盘价高低点（默认 N=28） | 反向交叉时先平旧仓再反手 |

双均线含 **T+1 限制**：当天止损过的品种当天不重入，次日 EMA 方向仍成立则按当时方向重入。
海龟含**新币启动期直通**：K 线不足以形成通道的新标的，首次能算出通道时允许直接突破开仓。

## 🚀 快速开始

```bash
cd trading
pip install -r requirements.txt
cp config.example.json config.json        # 填入 OKX 凭据与钉钉 webhook（或用环境变量）

python verify_okx.py BTCUSDT               # 只读检查；上实盘前必须用模拟盘跑完整验证
gunicorn -w 1 -b 0.0.0.0:5000 wsgi:application
```

凭据也可用环境变量注入（优先于 config.json）：
`OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSPHRASE` / `DINGTALK_WEBHOOK`。

## ⚙️ 部署硬性要求

| 要求 | 原因 |
|---|---|
| 服务器时区 `Asia/Shanghai` | 日检 08:00 对齐 OKX 日线收盘（00:00 UTC）；系统启动时校验 UTC+8，不符告警 |
| `gunicorn -w 1`（单 worker） | 多 worker 会重复初始化交易系统；`wsgi.py` 文件锁兜底，多余 worker 直接拒启 |
| 环境变量 `FLASK_SECRET_KEY`、`TRADING_LOGIN_PASSWORD` | 管理台会话与登录；缺 `FLASK_SECRET_KEY` 拒绝启动 |
| 环境变量 `TRADING_COOKIE_SECURE=1`（HTTPS 部署时） | 会话 cookie 加 Secure 标志；内网纯 HTTP 部署不要设置，否则登录态无法保持 |
| 公网访问须经 HTTPS 反向代理 | 登录密码与会话 cookie 不得明文传输 |
| 上实盘前跑通 `python verify_okx.py`（sandbox） | 张数换算 / 止损算法单 / 撤单 / 单向模式只能真连交易所自证，代码审查不能替代 |

> **依赖锁定**：上线机器验证通过后，在**该机器上** `pip freeze > requirements.lock` 固化并入库——
> 锁文件必须来自实际运行环境（ccxt 行为随版本漂移，正是适配层反复警示的风险），不要在 CI/开发机生成。

## 🧪 测试

```bash
cd trading

# 166 用例，纯标准库，无需安装任何依赖（含并发混沌 / 灾难恢复 / 变异测试）
python3 -m unittest discover -s . -p "test_*.py"

# 74 用例，需 flask/pandas/ccxt 环境（交易逻辑 / 路由集成）
pip install -r requirements.txt
python3 -m unittest tests.test_trading_logic_unittest -v
```

CI（`.github/workflows/tests.yml`）在 Python 3.10–3.13 上跑标准库套件，在 3.11 上跑依赖版套件，并对前端 `app.js` 做 `node --check` 语法检查。

## 🔒 安全须知

- **任何形式的凭据都不要提交**：`config.json`、状态文件、日志、备份 tarball（备份里打包了 config.json）均已被 `.gitignore` 排除。
- 若凭据曾出现在 git 历史中，视同泄露处理：到欧易删除并重建 API Key、更换钉钉机器人 webhook。
- 管理台已加登录防爆破（按 IP 连续 5 次失败锁 60 秒）与会话 Cookie 加固（SameSite=Lax，Secure 可选）。

## 📚 更多文档

- [`trading/README_OKX迁移说明.md`](trading/README_OKX迁移说明.md) — 部署与架构完整说明
- [`trading/运维检查清单.md`](trading/运维检查清单.md) — 部署/上实盘/巡检/未来演进的可执行清单
- [`trading/审查说明.md`](trading/审查说明.md) — 全部审查轮次与决策记录

## 📄 许可证

[MIT](LICENSE) © 2026 qscta
