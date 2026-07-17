"""交易所适配层抽象基类。

设计目标：上层（main.py / api_server.py / 策略）只依赖本接口，不感知具体交易所。
每个交易所（币安、欧易……）实现一个子类，把"符号格式、合约张数、止损/算法单、
杠杆与保证金模式"等差异全部封装在子类内部。

内部符号约定：统一用 `BTCUSDT`（无斜杠）。各子类负责把它映射成自己的 ccxt 永续符号：
  - 币安：BTC/USDT      （defaultType=future 下即为 U 本位永续）
  - 欧易：BTC/USDT:USDT （现货才是 BTC/USDT）

仓位单位约定：上层与本地状态始终以"币的数量"为单位（如 0.5 BTC）。
张数（合约面值）的换算只发生在子类下单边界内部，绝不外泄给上层，
从而保证风控/盈亏/名义价值的计算口径在所有交易所下保持一致。
"""

import ccxt
import logging
import math
import time
from functools import wraps

import pandas as pd

logger = logging.getLogger(__name__)


# ====== 安全重试装饰器（仅用于读操作和幂等操作） ======
def retry_on_network_error(max_retries=3, backoff_seconds=(1, 2, 4)):
    """对网络超时、频率限制等可恢复异常自动重试。"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable,
                        ccxt.DDoSProtection, ccxt.RateLimitExceeded) as e:
                    last_exception = e
                    wait_time = backoff_seconds[attempt] if attempt < len(backoff_seconds) else backoff_seconds[-1]
                    logger.warning(f"[重试 {attempt+1}/{max_retries}] {func.__name__} 网络异常: {e}, {wait_time}秒后重试...")
                    time.sleep(wait_time)
                except (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.BadRequest,
                        ccxt.AuthenticationError, ccxt.PermissionDenied, ccxt.BadSymbol) as e:
                    logger.error(f"{func.__name__} 业务异常（不可重试）: {e}")
                    raise
                except Exception as e:
                    logger.error(f"{func.__name__} 未知异常: {e}")
                    raise
            logger.error(f"{func.__name__} 重试{max_retries}次后仍失败: {last_exception}")
            raise last_exception
        return wrapper
    return decorator


class ExchangeApi:
    """交易所适配层抽象基类。

    子类必须实现：_create_exchange、to_ccxt_symbol、get_position、open_position、
    close_position、create_stop_loss_order、cancel_order、cancel_all_orders、
    round_quantity、get_quantity_precision、find_stop_order_state、
    list_position_symbols、find_existing_open_order、
    find_compensation_close_evidence。

    可选重写：setup_symbol（开仓前设置杠杆/保证金模式等）。

    通用且与交易所无关的实现（K 线读取、收盘过滤、余额/持仓/挂单读取）在基类提供。
    """

    name = 'base'

    def __init__(self, config):
        self.config = config
        self.exchange = self._create_exchange(config)

    # ===================== 必须由子类实现 =====================

    def _create_exchange(self, config):
        """创建并返回已配置好的 ccxt 交易所实例。"""
        raise NotImplementedError

    def to_ccxt_symbol(self, symbol):
        """内部符号(BTCUSDT) -> 该交易所的 ccxt 永续符号。"""
        raise NotImplementedError

    def get_position(self, symbol):
        """获取单个交易对的持仓（ccxt 统一结构，无持仓返回 None 或 contracts=0）。"""
        raise NotImplementedError

    def open_position(self, symbol, side, amount):
        """市价开仓。amount 单位为‘币数’。"""
        raise NotImplementedError

    def close_position(self, symbol, side, amount, client_order_id=None):
        """市价平仓。amount 单位为‘币数’。"""
        raise NotImplementedError

    def create_stop_loss_order(self, symbol, side, amount, stop_price):
        """创建止损单（触发后市价平仓）。amount 单位为‘币数’。"""
        raise NotImplementedError

    def cancel_order(self, symbol, order_id):
        raise NotImplementedError

    def cancel_stop_order_only(self, symbol, order_id):
        """持仓仍在时仅撤指定保护单；不得退化为 cancel-all。"""
        raise NotImplementedError

    def cancel_all_orders(self, symbol):
        raise NotImplementedError

    def round_quantity(self, symbol, quantity):
        """把‘币数’对齐到交易所允许的下单步长，返回对齐后的‘币数’。"""
        raise NotImplementedError

    def get_quantity_precision(self, symbol):
        """返回下单数量的小数位数（仅用于日志展示）。"""
        raise NotImplementedError

    def setup_symbol(self, ccxt_symbol):
        """开仓前的一次性准备（如设置杠杆/保证金模式）。默认无操作。"""
        return None

    def find_stop_order_state(self, symbol, side, amount, stop_price, stop_order_id=None):
        """检查止损：intact/adoptable/mismatch/missing 四种结果。"""
        raise NotImplementedError

    def list_position_symbols(self):
        """交易所端当前有实际持仓的内部符号列表（启动孤儿仓核对用）。"""
        raise NotImplementedError

    def find_existing_open_order(self, symbol, side, amount, client_order_id):
        """只读查询确定性 clOrdId 的既有开仓单（幂等恢复用）。

        契约：明确不存在返回 None；命中且与请求一致返回订单；查询不确定或
        内容不一致必须抛出。本方法永不创建订单。主系统的 pending 恢复链
        依赖该能力，适配器必须实现而不是留到运行时才暴露缺方法。
        """
        raise NotImplementedError

    def find_compensation_close_evidence(self, symbol, side, amount,
                                         open_client_order_id):
        """已确认空仓后，只读找回由开仓句柄派生的补偿平仓聚合成交。

        契约：明确无补偿腿或证据不完整返回 None；完整证据返回聚合结果；
        查询不确定必须抛出。本方法永不创建订单——「查询」绝不允许用可
        下单的 close_position 模拟。
        """
        raise NotImplementedError

    # ===================== 交易所无关的通用实现 =====================

    @staticmethod
    def _normalize_precision(value):
        """把 ccxt 合约 precision.amount 的**步长**转换成小数位数（整数）。
        OKX（本项目唯一对接的交易所）的 precision.amount 恒为步长格式：
        - 1.0 / 1 -> 0 位（只能整数）
        - 0.1 -> 1 位, 0.01 -> 2 位, 1e-05 -> 5 位
        注意：>=1 一律按步长语义判 0 位。若未来接入以「小数位数整数」表达精度的
        交易所（如 precision.amount=5 表示 5 位），此函数会误判为 0 位——那种语义
        与步长无法从单个数值区分，须在对应适配层另行处理，不要指望这里兼容。
        本值仅用于日志展示与换算兜底，实际下单走 ccxt amount_to_precision。
        """
        if value is None:
            return 3
        value = float(value)
        if value >= 1.0:
            return 0
        elif value > 0:
            return max(0, round(-math.log10(value)))
        return 3

    @staticmethod
    def validate_ohlcv(ohlcv, symbol=''):
        """统一 K 线边界校验：任何一根坏蜡烛都整批拒绝（抛 ValueError）。

        服务所有行情入口（日检、即时开仓、API 展示）：时间戳必须严格递增
        不得重复；开高低收必须是有限正数（拒绝 bool/NaN/无穷）；蜡烛内部
        关系必须成立（low<=open/close<=high）；成交量必须是有限非负数。
        坏数据进入 EMA/突破计算仍可能算出「有效」信号并进入开仓链路，
        因此只能拒绝，不能静默丢弃或修补。
        """
        if ohlcv is None:
            raise ValueError(f'{symbol} K 线响应为 None，拒绝当空数据')
        if not isinstance(ohlcv, (list, tuple)):
            raise ValueError(
                f'{symbol} K 线响应类型异常: {type(ohlcv).__name__}')
        prev_ts = None
        for index, row in enumerate(ohlcv):
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                raise ValueError(f'{symbol} 第{index}根 K 线结构异常: {row!r}')
            ts, open_, high, low, close, volume = row[:6]
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                raise ValueError(f'{symbol} 第{index}根 K 线时间戳非法: {ts!r}')
            ts = float(ts)
            if not math.isfinite(ts):
                raise ValueError(f'{symbol} 第{index}根 K 线时间戳非有限数')
            if prev_ts is not None and ts <= prev_ts:
                raise ValueError(
                    f'{symbol} 第{index}根 K 线时间戳未严格递增: '
                    f'{prev_ts} -> {ts}（重复或乱序）')
            prev_ts = ts
            prices = {}
            for name, value in (('open', open_), ('high', high),
                                ('low', low), ('close', close)):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(
                        f'{symbol} 第{index}根 K 线 {name} 非法: {value!r}')
                value = float(value)
                if not math.isfinite(value) or value <= 0:
                    raise ValueError(
                        f'{symbol} 第{index}根 K 线 {name} 必须是有限正数: {value!r}')
                prices[name] = value
            if (prices['low'] > min(prices['open'], prices['close']) or
                    prices['high'] < max(prices['open'], prices['close'])):
                raise ValueError(
                    f'{symbol} 第{index}根 K 线高低区间与开收盘矛盾: '
                    f"O={prices['open']} H={prices['high']} "
                    f"L={prices['low']} C={prices['close']}")
            if volume is not None:
                if isinstance(volume, bool) or not isinstance(volume, (int, float)):
                    raise ValueError(
                        f'{symbol} 第{index}根 K 线成交量非法: {volume!r}')
                if not math.isfinite(float(volume)) or float(volume) < 0:
                    raise ValueError(
                        f'{symbol} 第{index}根 K 线成交量必须是有限非负数: {volume!r}')
        return ohlcv

    def ohlcv_to_dataframe(self, ohlcv):
        """将 OHLCV 数据转换为 DataFrame。"""
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df

    def filter_closed_candles(self, df, timeframe='1d', grace_seconds=2):
        """按时间戳过滤未收盘 K 线，避免固定去尾造成边界误差。"""
        if df is None or len(df) == 0:
            return df

        tf_ms_map = {
            '1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000, '30m': 1_800_000,
            '1h': 3_600_000, '2h': 7_200_000, '4h': 14_400_000, '6h': 21_600_000, '8h': 28_800_000, '12h': 43_200_000,
            '1d': 86_400_000, '3d': 259_200_000, '1w': 604_800_000
        }
        tf_ms = tf_ms_map.get(timeframe)
        if tf_ms is None:
            return df.iloc[:-1] if len(df) > 1 else df.iloc[0:0]

        now_ms = int(time.time() * 1000) - int(grace_seconds * 1000)
        last_open_ms = int(pd.Timestamp(df.iloc[-1]['timestamp']).timestamp() * 1000)

        if last_open_ms + tf_ms <= now_ms:
            return df
        return df.iloc[:-1]

    # ---- 读操作：ccxt 统一接口，可安全重试 ----

    @retry_on_network_error(max_retries=3)
    def fetch_ohlcv(self, symbol, timeframe='1d', limit=100):
        """读取最近 ``limit`` 根 K 线；策略行情严格限制为 OKX 单页上限。

        所有真钱调用都只依赖最新滚动窗口。超过 300 根时明确拒绝，避免调用者
        误以为 ccxt 会完整返回，也不再保留曾导致陈旧行情事故的分页路径。
        """
        requested = int(limit)
        if requested <= 0:
            return []
        if requested > 300:
            raise ValueError(
                f'K 线请求 {requested} 根超过 OKX 单页上限 300；'
                '策略配置必须在最新单页内完成计算')
        # 所有行情入口共用同一边界校验：坏蜡烛（NaN/重复/乱序/区间矛盾）
        # 一律在适配层拒绝，绝不让 EMA/突破在污染数据上算出“有效”信号。
        return self.validate_ohlcv(
            self.exchange.fetch_ohlcv(symbol, timeframe, limit=requested),
            symbol)

    @retry_on_network_error(max_retries=3)
    def get_balance(self):
        return self.exchange.fetch_balance()

    @retry_on_network_error(max_retries=3)
    def get_last_price(self, symbol):
        """最新成交价（float）。symbol 可传内部符号或 ccxt 符号，内部统一归一。

        上层读市价的唯一入口（收口此前散落各处的 exchange.fetch_ticker 直调，
        顺带获得网络重试保护）。失败/无价向上抛出，回退逻辑由调用方自持。
        """
        ccxt_symbol = symbol if '/' in symbol else self.to_ccxt_symbol(symbol)
        ticker = self.exchange.fetch_ticker(ccxt_symbol)
        return float(ticker['last'])
