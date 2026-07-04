# 欧易（OKX）程序化合约交易系统

单交易所（欧易 U 本位永续）程序化交易系统，**真实资金运行**。
海龟通道突破 + 双均线 EMA 两策略，以损定量（默认单笔风险 1%），止损为交易所侧
reduce-only 算法单；每日 08:00 日检、5 分钟盘中止损巡检；Flask 管理台（亮/暗双主题）+ 钉钉通知。

> ⚠️ 本仓库只存放**代码**。凭据（`config.json`）、交易账本（`trade_state.json`）、权益数据、
> 日志与备份都属于部署机，已被 `.gitignore` 排除，任何时候不得提交。

## 目录结构

```
trading/
├── main.py                 # 装配与调度核心：配置、状态护栏、日检、两策略信号/开平仓执行
├── stop_guardian.py        # 止损防线 mixin：验证式撤单、残留阻断、止损自愈、交易所已平收尾
├── reporting.py            # 通知报表 mixin：开平仓汇总、每日持仓汇总、周报、权益采样告警
├── okx_api.py              # 欧易适配器：币数↔张数换算、算法止损单、杠杆/单向模式
├── exchange_base.py        # 交易所适配层抽象基类（K线读取、收盘过滤、网络重试）
├── turtle_strategy.py      # 海龟通道突破策略
├── ma_cross_strategy.py    # 双均线 EMA 策略（含 T+1 重入）
├── risk_manager.py         # 以损定量仓位计算
├── trade_state.py          # 本地账本（fail-closed、原子写入+fsync、事务回滚）
├── equity_tracker.py       # 权益/峰值/回撤/求索指数追踪
├── dingtalk_notifier.py    # 钉钉通知（errcode 校验 + 失败重试）
├── api_server.py           # Flask 管理台 API
├── wsgi.py                 # gunicorn 入口（文件锁强制单实例）
├── verify_okx.py           # 上实盘前的模拟盘全链路验证脚本（必跑）
├── mem_monitor.py          # 服务器资源监控（独立进程）
├── index.html / static/    # 前端管理台（lightweight-charts 本地化，无外网依赖）
├── test_*.py + _test_stubs.py   # 116 个纯标准库测试（无需第三方依赖）
├── tests/                  # 50 个依赖版测试（需 flask/pandas/ccxt）
├── config.example.json     # 配置模板（复制为 config.json 后填入凭据）
├── README_OKX迁移说明.md    # 部署与架构说明（完整版）
└── 审查说明.md              # 全部审查轮次与决策记录
```

## 快速开始

```bash
cd trading
pip install -r requirements.txt
cp config.example.json config.json   # 填入 OKX 凭据与钉钉 webhook（或用环境变量）
python verify_okx.py BTCUSDT         # 只读检查；上实盘前必须用模拟盘跑完整验证
gunicorn -w 1 -b 0.0.0.0:5000 wsgi:application
```

凭据也可用环境变量注入（优先于 config.json）：
`OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSPHRASE` / `DINGTALK_WEBHOOK`。

## 部署硬性要求

| 要求 | 原因 |
|---|---|
| 服务器时区 `Asia/Shanghai` | 日检 08:00 对齐 OKX 日线收盘（00:00 UTC） |
| `gunicorn -w 1`（单 worker） | 多 worker 会重复初始化交易系统（wsgi.py 有文件锁兜底，多余 worker 直接拒启） |
| 环境变量 `FLASK_SECRET_KEY`、`TRADING_LOGIN_PASSWORD` | 管理台会话与登录；缺 SECRET_KEY 拒绝启动 |
| 环境变量 `TRADING_COOKIE_SECURE=1`（HTTPS 部署时设置） | 会话 cookie 加 Secure 标志，仅经 HTTPS 传输；内网纯 HTTP 部署不要设置，否则登录态无法保持 |
| 公网访问须经 HTTPS 反向代理 | 登录密码与会话 cookie 不得明文传输 |
| 上实盘前跑通 `python verify_okx.py`（sandbox） | 张数换算/止损算法单/撤单/单向模式只能真连交易所自证，代码审查不能替代 |

> **依赖锁定**：上线机器验证通过后，在**该机器上** `pip freeze > requirements.lock` 固化并入库——
> 锁文件必须来自实际运行环境（ccxt 行为随版本漂移，正是适配层反复警示的风险），不要在 CI/开发机生成。

## 测试

```bash
cd trading
# 116 用例，纯标准库，无需安装任何依赖（CI 跑的就是这套）
python3 -m unittest discover -s . -p "test_*.py"

# 50 用例，需 flask/pandas/ccxt 环境
python3 -m unittest tests.test_trading_logic_unittest -v
```

## 安全须知

- **任何形式的凭据都不要提交**：`config.json`、状态文件、日志、备份 tarball（备份里打包了 config.json）均已被 `.gitignore` 排除。
- 若凭据曾经出现在 git 历史中，视同泄露处理：到欧易删除并重建 API Key、更换钉钉机器人 webhook，必要时重建仓库。
