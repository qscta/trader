# 测试

测试与生产代码物理分离，本目录下分两套（均在 `trading/` 目录运行）：

## `tests/unit/` — 零依赖套件

纯标准库，无需安装任何第三方库；统一走 `_test_stubs.import_main()` 桩环境
（桩只在导入 main 的瞬间存在于 `sys.modules`，任意顺序运行互不污染）。

```bash
python3 -m unittest discover -s tests/unit -p "test_*.py"
```

覆盖：启动装配全链、配置校验三入口、账本事务与 fail-closed、止损三防线、
未创新高统计、并发混沌 / 灾难恢复 / 变异测试、删除后托管、钉钉送达证据与各护栏等。

## `tests/integration/` — 依赖版套件

需 pandas / ccxt / flask 环境，覆盖：

- 收盘 K 线时间戳过滤（`filter_closed_candles`）
- 双均线止损确认与翻转分支（含撤销不可确认时不反手）
- 双均线 T+1 重入
- `instant_open` / `delete_symbol` / 输入校验 / 策略参数校验等 API 路由行为
- 开仓风险护栏、止损核验与自愈、持久化失败补偿等

```bash
pip install -r requirements.txt
python3 -m unittest discover -s tests/integration -p "test_*.py" -v
```

## 根目录统一门禁

`tests/` 及两个子目录均为 Python 包。从 `trading/` 根目录执行下列
标准命令会递归运行两套全部测试，不再出现 `Ran 0 tests / OK` 假绿灯：

```bash
python3 -m unittest discover -p "test_*.py"
```

> 单独跑某个零依赖模块：`PYTHONPATH=tests/unit python3 -m unittest test_startup_smoke`
> （`tests/unit` 入 path 供 `_test_stubs`，仓库根由 CWD 提供生产模块）。
