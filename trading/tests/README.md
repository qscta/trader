# 交易逻辑测试（需完整依赖环境）

本目录的 `test_trading_logic_unittest.py`（113 用例）需要 pandas / ccxt / flask / apscheduler 环境运行，覆盖：

- 收盘 K 线时间戳过滤（`filter_closed_candles`）
- 海龟止损确认分支（含撤销不可确认时不反手）
- 双均线 T+1 重入
- `instant_open` / `delete_symbol` / 输入校验 / 策略参数校验等 API 路由行为
- 开仓与主动平仓两阶段意图、确定性多腿幂等恢复、归因隔离、部分回滚余仓建账
- make-before-break 止损更新、持久化失败补偿、调度/进程生命周期

## 运行

```bash
# 在项目根目录（或用 TRADING_SYSTEM_DIR 指定）
python3 -m unittest tests.test_trading_logic_unittest -v
```

> 项目根目录另有 **496 个无第三方依赖**的测试（`test_*.py`，统一走 `_test_stubs.import_main()` 桩环境），
> `python3 -m unittest discover -s . -p "test_*.py"` 即可本机运行，详见根目录 README。
