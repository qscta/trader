import ccxt
import hashlib
import logging
import math
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from exchange_base import ExchangeApi, retry_on_network_error

logger = logging.getLogger(__name__)


class ContractSizeUnavailable(RuntimeError):
    """合约面值不可得。面值是张数换算与风控的根基，拿不到必须拒绝交易（fail closed），绝不允许猜默认值。"""


class PositionModeError(RuntimeError):
    """账户不是可证明的单向净持仓模式，或同一品种出现多条持仓腿。"""


class OkxApi(ExchangeApi):
    """欧易（OKX）U 本位永续适配器。

    与币安的关键差异，全部封装在本类内部：
      1. 凭据多一个 passphrase（config['password']）。
      2. defaultType = 'swap'；永续 ccxt 符号是 BASE/USDT:USDT（BASE/USDT 是现货）。
      3. 下单单位是“张”。每张 = contractSize 个币。上层传进来的是“币数”，
         本类在下单边界用 _coin_to_contracts 换算成张数，对外永远以币数为口径。
      4. 止损是“算法单/策略委托”，用 stopLossPrice + reduceOnly 创建，撤销/查询走算法单接口。
      5. 单向(净)持仓模式 + 每品种显式 set_leverage + 每单带 tdMode（全仓/逐仓）。

    ⚠️ 上线前务必在小额/模拟盘验证的三件事（不同 ccxt 版本行为可能有差异）：
       a) 下单张数与预期币数是否一致（contractSize 换算）；
       b) 止损单是否为 reduce-only 的算法单、触发后能否市价平仓；
       c) cancel_all_orders 是否能把算法止损单一并撤掉。
    """

    name = 'okx'

    # 验证式撤单：首次复核仍见该单时，等待片刻再复查一次的间隔（秒）。
    # 交易所列表更新可能滞后于撤单生效，立即判死会把「实际已撤」误报成残留，
    # 白白触发残留阻断（该品种停开仓直到日检重试清理）。
    CANCEL_VERIFY_RECHECK_DELAY = 2.0
    # 市价单 ACK 只代表交易所受理；必须等待订单终态并与仓位变化交叉确认。
    ORDER_CONFIRM_ATTEMPTS = 6
    ORDER_CONFIRM_DELAY = 1.0
    MAX_CLOSE_LEGS = 3
    STOP_CONFIRM_ATTEMPTS = 3
    STOP_CONFIRM_DELAY = 1.0

    def _create_exchange(self, config):
        ex = ccxt.okx({
            'apiKey': config['apiKey'],
            'secret': config['secret'],
            'password': config['password'],  # OKX passphrase（资金/接口密码）
            'enableRateLimit': True,
            'timeout': 15000,
            'options': {
                'defaultType': 'swap',
            },
        })
        if config.get('sandbox') or config.get('demo'):
            ex.set_sandbox_mode(True)   # OKX 模拟盘（demo trading）
            logger.info("OKX 已切换到模拟盘模式")
        return ex

    def __init__(self, config):
        # 该值直接进入每笔订单的 tdMode。拼写错误若不在启动时拒绝，会让
        # 全部下单到盘中才逐笔失败。
        raw_margin_mode = config.get('margin_mode') or 'cross'
        if (not isinstance(raw_margin_mode, str) or
                raw_margin_mode.strip().lower() not in ('cross', 'isolated')):
            raise ValueError(
                f"okx.margin_mode 非法: {config.get('margin_mode')!r}"
                "（只支持 cross / isolated）")
        super().__init__(config)
        self.margin_mode = raw_margin_mode.strip().lower()
        # 杠杆：默认值 + 可按内部符号覆盖，如 {"BTCUSDT": 10}
        self.default_leverage = self._validate_leverage(
            config.get('leverage', 5), 'okx.leverage')
        raw_overrides = config.get('leverage_overrides', {}) or {}
        if not isinstance(raw_overrides, dict):
            raise ValueError('okx.leverage_overrides 必须是对象')
        self.leverage_overrides = {
            str(symbol): self._validate_leverage(
                value, f'okx.leverage_overrides.{symbol}')
            for symbol, value in raw_overrides.items()
        }

        self._contract_size_cache = {}     # ccxt_symbol -> contractSize（每张多少币）
        self._amount_precision_cache = {}  # ccxt_symbol -> 张数小数位
        self._leverage_done = set()        # 本进程已设置过杠杆的 ccxt_symbol

        self._load_market_cache()
        self._ensure_one_way_mode()

    # ===================== 符号映射 =====================

    def to_ccxt_symbol(self, symbol):
        """内部符号(BTCUSDT) -> OKX ccxt 永续符号(BTC/USDT:USDT)。"""
        if symbol.endswith('USDT'):
            return symbol[:-4] + '/USDT:USDT'
        return symbol

    def to_internal_symbol(self, ccxt_symbol):
        """BTC/USDT:USDT -> BTCUSDT。"""
        base = ccxt_symbol.split('/')[0]
        return f"{base}USDT"

    def _resolve_symbol(self, symbol):
        """允许上层传内部符号或 ccxt 符号，统一成 ccxt 符号。"""
        return symbol if '/' in symbol else self.to_ccxt_symbol(symbol)

    # ===================== 市场/合约面值缓存 =====================

    def _load_market_cache(self):
        """启动时加载所有 USDT 本位永续的合约面值与张数精度。"""
        try:
            markets = self.exchange.load_markets(True)
            count = 0
            for sym, market in markets.items():
                if (market.get('type') == 'swap'
                        and market.get('quote') == 'USDT'
                        and market.get('settle') == 'USDT'):
                    contract_size = market.get('contractSize')
                    if contract_size:
                        self._contract_size_cache[sym] = float(contract_size)
                    amount_step = (market.get('precision') or {}).get('amount')
                    if amount_step is not None:
                        self._amount_precision_cache[sym] = self._normalize_precision(amount_step)
                    count += 1
            logger.info(f"OKX 市场缓存已加载: {count} 个 USDT 永续，合约面值 {len(self._contract_size_cache)} 个")
        except Exception as e:
            logger.warning(f"加载 OKX 市场缓存失败: {e}")

    def _get_contract_size(self, ccxt_symbol):
        """获取合约面值（每张多少币）。获取失败/缺失必须抛出，由上层放弃本次交易。"""
        if ccxt_symbol in self._contract_size_cache:
            return self._contract_size_cache[ccxt_symbol]
        try:
            market = self.exchange.market(ccxt_symbol)
            contract_size = float(market.get('contractSize') or 0)
        except Exception as e:
            raise ContractSizeUnavailable(f"{ccxt_symbol} 合约面值获取失败: {e}，拒绝换算/交易") from e
        if contract_size <= 0:
            raise ContractSizeUnavailable(f"{ccxt_symbol} 市场数据缺少有效 contractSize，拒绝换算/交易")
        self._contract_size_cache[ccxt_symbol] = contract_size
        return contract_size

    # ===================== 张数换算 =====================

    def _coin_to_contracts(self, ccxt_symbol, coin_amount):
        """币数 -> 张数（按交易所张数步长截断）。

        用十进制字符串完成币数/合约面值的除法，避免 ``(n * cs) / cs`` 的
        二次二进制浮点误差把本来精确的 n 张变成 n-1 张。这里没有使用宽泛
        epsilon：真实不足一个步长的数量仍会严格向下取整。
        """
        contract_size = self._get_contract_size(ccxt_symbol)
        try:
            raw_contracts = Decimal(str(coin_amount)) / Decimal(str(contract_size))
        except (InvalidOperation, TypeError, ValueError, ZeroDivisionError) as e:
            raise ValueError(f"{ccxt_symbol} 非法币数/合约面值: amount={coin_amount}, contractSize={contract_size}") from e
        if not raw_contracts.is_finite() or raw_contracts <= 0:
            return 0.0
        try:
            # 整数张数可被 float 精确表达；非整数仍由 ccxt 按市场步长截断。
            return float(self.exchange.amount_to_precision(ccxt_symbol, float(raw_contracts)))
        except Exception:
            precision = self._amount_precision_cache.get(ccxt_symbol, 0)
            quantum = Decimal(1).scaleb(-precision)
            return float(raw_contracts.quantize(quantum, rounding=ROUND_DOWN))

    def _contracts_to_coins(self, ccxt_symbol, contracts):
        """张数 -> 币数，保持与十进制 contractSize 的账本口径一致。"""
        return float(Decimal(str(contracts)) * Decimal(str(self._get_contract_size(ccxt_symbol))))

    def round_quantity(self, symbol, quantity):
        """把上层算出的“币数”对齐到 OKX 整张，再换算回“币数”返回。

        返回的币数 = 整张数 × 合约面值，确保它与最终真实下单张数一一对应——
        这样上层用这个币数做名义价值/风控/盈亏计算时不会与实际成交错位。
        若不足一张则返回 0，上层会据此放弃开仓（无法交易小于一张的量）。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        contract_size = self._get_contract_size(ccxt_symbol)
        contracts = self._coin_to_contracts(ccxt_symbol, quantity)
        return float(Decimal(str(contracts)) * Decimal(str(contract_size)))

    def get_quantity_precision(self, symbol):
        """返回“张数”的小数位（仅用于日志展示）。"""
        ccxt_symbol = self._resolve_symbol(symbol)
        return self._amount_precision_cache.get(ccxt_symbol, 0)

    # ===================== 杠杆 / 持仓模式 =====================

    def _ensure_one_way_mode(self):
        """切换并回读验证单向(净)持仓模式；无法证明时拒绝启动。"""
        try:
            self.exchange.set_position_mode(False)  # False = 单向(net)
        except Exception as e:
            # 已经是 net_mode、或账户有仓位/挂单时，OKX 可能拒绝重复切换；
            # 指令失败本身不裁决模式，下面只信任权威回读。
            logger.info(f"OKX 设置单向持仓模式未执行（将回读裁决）: {e}")
        try:
            mode = self.exchange.fetch_position_mode()
        except Exception as e:
            raise PositionModeError(f"OKX 持仓模式回读失败，拒绝启动: {e}") from e
        if not isinstance(mode, dict) or mode.get('hedged') is not False:
            raise PositionModeError(f"OKX 账户不是单向净持仓模式，拒绝启动: {mode!r}")
        info = mode.get('info') or {}
        if info.get('posMode') not in (None, 'net_mode'):
            raise PositionModeError(f"OKX posMode={info.get('posMode')}，拒绝启动")
        logger.info("OKX 单向(净)持仓模式已回读确认")

    def _leverage_for(self, ccxt_symbol):
        internal = self.to_internal_symbol(ccxt_symbol)
        return (self.leverage_overrides.get(internal)
                or self.leverage_overrides.get(ccxt_symbol)
                or self.default_leverage)

    @staticmethod
    def _validate_leverage(value, field):
        if isinstance(value, bool):
            raise ValueError(f'{field} 不能是 bool')
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'{field} 必须是数值') from exc
        if not math.isfinite(parsed) or not (0 < parsed <= 125):
            raise ValueError(f'{field} 必须在 (0, 125] 内')
        return int(parsed) if parsed.is_integer() else parsed

    def setup_symbol(self, ccxt_symbol):
        """开仓前确保该品种杠杆已设置（每进程每品种一次）。"""
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        if ccxt_symbol in self._leverage_done:
            return
        leverage = self._leverage_for(ccxt_symbol)
        try:
            self.exchange.set_leverage(leverage, ccxt_symbol, params={'mgnMode': self.margin_mode})
            logger.info(f"OKX 已设置 {ccxt_symbol} 杠杆={leverage}x，保证金模式={self.margin_mode}")
            self._leverage_done.add(ccxt_symbol)
        except Exception as e:
            # 沿用账户遗留高杠杆会让止损风险模型失真，尤其 isolated 下可能
            # 在止损前先强平。无法证明配置已生效就禁止开仓。
            raise RuntimeError(
                f"OKX 设置 {ccxt_symbol} 杠杆={leverage}x / "
                f"{self.margin_mode} 失败，拒绝开仓: {e}") from e

    def _order_params(self, reduce_only=False, extra=None):
        params = {'tdMode': self.margin_mode}
        if reduce_only:
            params['reduceOnly'] = True
        if extra:
            params.update(extra)
        return params

    # ===================== 读操作 =====================

    def _assert_position_entry_symbol(self, entry, info, ccxt_symbol):
        """持仓条目必须可归属到所查品种；归属错误/无法归属一律拒绝。

        没有这道核验时，`fetch_positions([BTC])` 若因参数映射漂移返回
        ETH 的持仓（或空字典 {}），会被直接当成 BTC 的仓/空仓——随后的
        方向校验、止损巡检、重复开仓判断全部建立在错误品种之上。
        """
        observed_symbol = entry.get('symbol')
        observed_inst = info.get('instId')
        if not observed_symbol and not observed_inst:
            raise PositionModeError(
                f"{ccxt_symbol} 持仓条目缺少品种标识，拒绝裁决: "
                f"{str(entry)[:120]}")
        if observed_symbol is not None and (
                self._resolve_symbol(str(observed_symbol)) != ccxt_symbol):
            raise PositionModeError(
                f"{ccxt_symbol} 持仓查询返回错误品种 symbol={observed_symbol!r}，"
                "拒绝采用")
        if observed_inst is not None and (
                str(observed_inst) != self._to_inst_id(ccxt_symbol)):
            raise PositionModeError(
                f"{ccxt_symbol} 持仓查询返回错误品种 instId={observed_inst!r}，"
                "拒绝采用")

    @staticmethod
    def _parse_signed_size(value, field, ccxt_symbol):
        """持仓数量字段的严格解析：None/空串返回 None，其余必须是有限数。"""
        if value is None or value == '':
            return None
        if isinstance(value, bool):
            raise PositionModeError(
                f"{ccxt_symbol} 持仓 {field} 字段是 bool，拒绝裁决: {value!r}")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as e:
            raise PositionModeError(
                f"{ccxt_symbol} 持仓 {field} 字段异常: {value!r}") from e
        if not math.isfinite(parsed):
            raise PositionModeError(
                f"{ccxt_symbol} 持仓 {field} 字段非有限数: {value!r}")
        return parsed

    @retry_on_network_error(max_retries=3)
    def get_position(self, symbol):
        """获取特定交易对的持仓（单向模式下只有一条）。

        无实仓时返回 None。OKX 可能返回 contracts=None/0 的空仓条目，
        若原样外泄，上层（币安时代写下的）`contracts == 0` / `contracts > 0`
        判断会因 None 误判甚至 TypeError——统一在适配层归一化掉。

        「无法确定」绝不当「空仓」：响应 None/非列表、contracts 缺失但原始
        pos 非零、NaN/无穷/bool、标准 side 与原始 pos 符号矛盾，一律抛
        PositionModeError，交由上层 fail-safe（跳过本轮/隔离），否则孤儿仓
        漏检与重复开仓都会落在真钱仓位上。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        positions = self.exchange.fetch_positions([ccxt_symbol])
        if positions is None or not isinstance(positions, (list, tuple)):
            raise PositionModeError(
                f"{ccxt_symbol} 持仓查询返回 {type(positions).__name__}，"
                "不确定不得当空仓")
        nonzero = []
        for p in positions:
            if p is None:
                continue
            if not isinstance(p, dict):
                raise PositionModeError(
                    f"{ccxt_symbol} 持仓条目结构异常: {type(p).__name__}")
            info = p.get('info') or {}
            self._assert_position_entry_symbol(p, info, ccxt_symbol)
            contracts_signed = self._parse_signed_size(
                p.get('contracts'), 'contracts', ccxt_symbol)
            raw_pos = self._parse_signed_size(info.get('pos'), 'pos', ccxt_symbol)
            if contracts_signed is None:
                if raw_pos is None:
                    # 两个来源都明确为空才是可证明的空仓条目。
                    continue
                if raw_pos != 0:
                    raise PositionModeError(
                        f"{ccxt_symbol} contracts 缺失但原始 pos={info.get('pos')!r} "
                        "非零，拒绝当空仓")
                continue
            contracts = abs(contracts_signed)
            if contracts <= 0:
                if raw_pos is not None and raw_pos != 0:
                    raise PositionModeError(
                        f"{ccxt_symbol} contracts=0 与原始 pos={info.get('pos')!r} 矛盾")
                continue
            if raw_pos is not None and raw_pos == 0:
                raise PositionModeError(
                    f"{ccxt_symbol} contracts={p.get('contracts')!r} 与原始 pos=0 矛盾")
            if p.get('hedged') is True or info.get('posSide') in ('long', 'short'):
                raise PositionModeError(f"{ccxt_symbol} 检测到双向持仓腿(posSide={info.get('posSide')})，拒绝裁剪为单腿")
            side = self._position_side(p)
            if side not in ('long', 'short'):
                raise PositionModeError(f"{ccxt_symbol} 非零持仓方向不可判定，拒绝继续交易")
            if raw_pos is not None and raw_pos != 0:
                raw_side = 'long' if raw_pos > 0 else 'short'
                if raw_side != side:
                    raise PositionModeError(
                        f"{ccxt_symbol} 标准 side={side} 与原始 pos={info.get('pos')!r} "
                        "符号矛盾，拒绝裁决")
            nonzero.append(p)
        if len(nonzero) > 1:
            raise PositionModeError(f"{ccxt_symbol} 同时存在 {len(nonzero)} 条非零持仓，拒绝隐藏任何一腿")
        return nonzero[0] if nonzero else None

    def _position_contracts(self, ccxt_symbol):
        """查询当前持仓张数；查询失败时向上抛出（保留与币安一致的错误语义）。"""
        position = self.get_position(ccxt_symbol)
        if position and position.get('contracts'):
            return abs(float(position['contracts']))
        return 0.0

    @staticmethod
    def _position_side(position):
        """从 ccxt/OKX 持仓结构读取净持仓方向。"""
        if not position:
            return None
        side = position.get('side')
        if side in ('long', 'short'):
            return side
        info = position.get('info') or {}
        try:
            signed = float(info.get('pos'))
        except (TypeError, ValueError):
            return None
        return 'long' if signed > 0 else ('short' if signed < 0 else None)

    def _contracts_tolerance(self, ccxt_symbol):
        precision = self._amount_precision_cache.get(ccxt_symbol, 0)
        # 仅容忍浮点表示噪声，绝不把“少半个步长”当作完整成交。
        return max(1e-12, (10 ** (-precision)) * 1e-9)

    @staticmethod
    def _finite_nonnegative(value):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed >= 0 else None

    @staticmethod
    def _order_reduce_only(order):
        """归一 reduceOnly：优先标准字段，缺失回退原生 info；只认 True/'true'。

        读不到一律 False——不可证明 reduce-only 的订单绝不当保护性止损。
        """
        if not isinstance(order, dict):
            return False
        value = order.get('reduceOnly')
        if value is None:
            value = (order.get('info') or {}).get('reduceOnly')
        return value in (True, 'true')

    @staticmethod
    def _reduce_only_unknown(order):
        """reduceOnly 在标准字段与原生 info 中都读不到 → 未知。

        未知 ≠ 否：未知订单不能证明是保护（不参与 intact 匹配），但也
        不能隐身——四态裁决必须把它当候选计入，宁可 mismatch 交人工，
        绝不因看不见而判 missing 再挂第二张止损。
        """
        if not isinstance(order, dict):
            return False
        if order.get('reduceOnly') is not None:
            return False
        return (order.get('info') or {}).get('reduceOnly') is None

    def _fetch_order_for_confirmation(self, ccxt_symbol, order_id, client_order_id):
        """按交易所订单 id；ACK 丢失时按预先生成的 clOrdId 查询。"""
        if order_id:
            return self.exchange.fetch_order(str(order_id), ccxt_symbol)
        return self.exchange.fetch_order(
            client_order_id, ccxt_symbol, params={'clOrdId': client_order_id})

    def _fetch_order_tristate(self, ccxt_symbol, client_order_id):
        """订单三态裁决的唯一原语：('found', order) / ('absent', None)。

        只有交易所明确 OrderNotFound 才是「明确不存在」；{}/None/缺身份
        字段属「无法裁决」，一律抛出——把畸形响应当不存在，幂等开仓会
        重复下单、平仓恢复会漏算真实成交腿。所有按 clOrdId 的存在性
        查询都必须走这里，不得各自临时判定。
        """
        try:
            order = self._fetch_order_for_confirmation(
                ccxt_symbol, None, client_order_id)
        except ccxt.OrderNotFound:
            return 'absent', None
        if not isinstance(order, dict) or not (
                order.get('id') or order.get('status')):
            raise RuntimeError(
                f'{ccxt_symbol} clOrdId={client_order_id} 查询返回无法识别的'
                f'响应，拒绝当订单不存在: {str(order)[:120]}')
        return 'found', order

    @staticmethod
    def _client_order_id(value=None):
        """生成或校验 OKX clOrdId（1-32 位 ASCII 字母数字）。"""
        if value is None:
            return f"trader{uuid.uuid4().hex[:26]}"
        value = str(value)
        if not (1 <= len(value) <= 32 and value.isascii() and value.isalnum()):
            raise ValueError("OKX client_order_id 必须为 1-32 位 ASCII 字母数字")
        return value

    @staticmethod
    def compensation_client_order_id(open_client_order_id):
        """由持久化开仓句柄稳定派生补偿平仓基础 clOrdId。"""
        value = str(open_client_order_id or '')
        if not value:
            raise ValueError('开仓 clOrdId 不能为空')
        return 'R' + hashlib.sha256(
            f'open-compensation|{value}'.encode('utf-8')
        ).hexdigest()[:31]

    @staticmethod
    def _close_leg_client_order_id(base_client_order_id, leg_index):
        suffix = '' if leg_index == 0 else f'r{leg_index}'
        return (base_client_order_id if not suffix else
                base_client_order_id[:32 - len(suffix)] + suffix)

    CLOSE_LEG_TERMINAL_STATUSES = frozenset({
        'closed', 'filled', 'canceled', 'cancelled', 'rejected', 'expired',
        'mmp_canceled', 'mmp_cancelled'})

    def _terminal_fill(self, order, amount, tolerance):
        """读取订单终态与可归因成交量：返回 (terminal, filled, status)。

        filled 缺失时用 amount-remaining 推导；closed/filled 终态且无余量
        视为按委托量全额成交；仍推不出则 filled=None，由调用方拒绝裁决。
        """
        info = order.get('info') or {}
        status = str(order.get('status') or info.get('state') or '').lower()
        terminal = status in self.CLOSE_LEG_TERMINAL_STATUSES
        filled = self._finite_nonnegative(order.get('filled'))
        remaining = self._finite_nonnegative(order.get('remaining'))
        if filled is None and remaining is not None:
            filled = max(0.0, amount - remaining)
        if (filled is None and status in {'closed', 'filled'} and
                (remaining is None or remaining <= tolerance)):
            filled = amount
        return terminal, filled, status

    def _collect_existing_close_legs(
            self, ccxt_symbol, close_side, requested_contracts,
            base_client_order_id):
        """只读找回基础腿及 r1/r2，全部终态后返回真实逐腿成交。

        close intent 只需持久化基础 clOrdId；后续腿 ID 由固定后缀派生。
        恢复若只看首腿，会把 r1/r2 的仓位变化误算到首腿并丢掉 VWAP/fee。
        """
        tolerance = self._contracts_tolerance(ccxt_symbol)
        last_problem = None
        for attempt in range(self.ORDER_CONFIRM_ATTEMPTS):
            found = {}
            known_filled = 0.0
            for leg_index in range(self.MAX_CLOSE_LEGS):
                leg_client_id = self._close_leg_client_order_id(
                    base_client_order_id, leg_index)
                presence, candidate = self._fetch_order_tristate(
                    ccxt_symbol, leg_client_id)
                if presence == 'absent':
                    # 后续腿只可能在前一腿已经存在之后创建；首个明确缺口
                    # 之后不再查询更后缀，减少限频压力。
                    break
                info = candidate.get('info') or {}
                observed_client_id = (
                    candidate.get('clientOrderId') or info.get('clOrdId'))
                if (observed_client_id is not None and
                        str(observed_client_id) != leg_client_id):
                    raise RuntimeError(
                        f'{ccxt_symbol} 平仓恢复腿 clOrdId 不匹配: '
                        f'expected={leg_client_id}, actual={observed_client_id}')
                observed_amount = candidate.get('amount')
                if observed_amount is None:
                    observed_amount = info.get('sz')
                observed_amount = self._finite_nonnegative(observed_amount)
                if (observed_amount is None or observed_amount <= tolerance or
                        observed_amount > requested_contracts + tolerance or
                        not self._existing_order_matches_request(
                            candidate, ccxt_symbol, close_side,
                            observed_amount, reduce_only=True)):
                    raise RuntimeError(
                        f'{ccxt_symbol} 平仓恢复腿内容与 intent 不一致: '
                        f'{leg_client_id}')
                found[leg_index] = [
                    leg_client_id, dict(candidate), observed_amount, None]
                terminal, preliminary_filled, _status = self._terminal_fill(
                    candidate, observed_amount, tolerance)
                if not terminal or preliminary_filled is None:
                    break
                known_filled += preliminary_filled
                if known_filled + tolerance >= requested_contracts:
                    break

            if not found:
                return []
            indices = sorted(found)
            if indices != list(range(indices[-1] + 1)):
                raise RuntimeError(
                    f'{ccxt_symbol} 平仓恢复腿不连续: {indices}')

            all_terminal = True
            total_filled = 0.0
            for leg_index in indices:
                leg_client_id, order, amount, _filled = found[leg_index]
                terminal, filled, order_status = self._terminal_fill(
                    order, amount, tolerance)
                if (not terminal or filled is None or
                        filled > amount + tolerance):
                    all_terminal = False
                    last_problem = (
                        f'{leg_client_id}: status={order_status}, filled={filled}, '
                        f'amount={amount}')
                    break
                order.setdefault('clientOrderId', leg_client_id)
                found[leg_index][3] = filled
                total_filled += filled
            if (all_terminal and
                    total_filled <= requested_contracts + tolerance):
                return [tuple(found[index]) for index in indices]
            if all_terminal:
                raise RuntimeError(
                    f'{ccxt_symbol} 平仓恢复腿累计成交超过 intent: '
                    f'{total_filled}>{requested_contracts}')
            if attempt < self.ORDER_CONFIRM_ATTEMPTS - 1:
                time.sleep(self.ORDER_CONFIRM_DELAY)
        raise RuntimeError(
            f'{ccxt_symbol} 平仓恢复腿未能全部确认终态: {last_problem}')

    def _stop_client_order_id(self, ccxt_symbol, stop_side, contracts,
                              stop_price, value=None):
        """固定算法单幂等键；同一保护意图的重试/重启复用同一个 algoClOrdId。"""
        if value is not None:
            return self._client_order_id(value)
        payload = (
            f"stop|{self._to_inst_id(ccxt_symbol)}|{stop_side}|"
            f"{Decimal(str(contracts)).normalize()}|{Decimal(str(stop_price)).normalize()}")
        return 'S' + hashlib.sha256(payload.encode('utf-8')).hexdigest()[:31]

    def _existing_order_matches_request(self, order, ccxt_symbol, order_side,
                                        requested_contracts, *, reduce_only):
        """幂等恢复前严格证明 clOrdId 命中的正是本次请求。"""
        if not isinstance(order, dict):
            return False
        info = order.get('info') or {}
        observed_symbol = order.get('symbol')
        native_inst_id = info.get('instId')
        if observed_symbol is not None:
            symbol_matches = self._resolve_symbol(observed_symbol) == ccxt_symbol
        else:
            symbol_matches = native_inst_id == self._to_inst_id(ccxt_symbol)
        observed_type = order.get('type') or info.get('ordType')
        observed_side = order.get('side') or info.get('side')
        observed_amount = order.get('amount')
        if observed_amount is None:
            observed_amount = info.get('sz')
        observed_amount = self._finite_nonnegative(observed_amount)
        observed_reduce_only = self._order_reduce_only(order)
        amount_matches = (
            observed_amount is not None and
            abs(observed_amount - requested_contracts) <= self._contracts_tolerance(ccxt_symbol))
        matches = (
            symbol_matches and observed_type == 'market' and
            observed_side == order_side and amount_matches and
            observed_reduce_only is bool(reduce_only))
        if not matches:
            logger.critical(
                f"{ccxt_symbol} clOrdId 命中订单与请求不一致，拒绝复用: "
                f"symbol={observed_symbol or native_inst_id}, type={observed_type}, "
                f"side={observed_side}, amount={observed_amount}, "
                f"reduceOnly={observed_reduce_only}; expected side={order_side}, "
                f"amount={requested_contracts}, reduceOnly={bool(reduce_only)}")
        return matches

    def _confirmed_order_result(self, ccxt_symbol, order, requested_contracts,
                                actual_contracts, *, fully_closed=None, source='order+position'):
        """构造上层契约：amount/requested_amount 均为币数，张数不外泄。"""
        result = dict(order or {})
        result['amount'] = self._contracts_to_coins(ccxt_symbol, actual_contracts)
        result['requested_amount'] = self._contracts_to_coins(ccxt_symbol, requested_contracts)
        result['confirmed'] = True
        result['fully_filled'] = (
            actual_contracts + self._contracts_tolerance(ccxt_symbol) >= requested_contracts)
        result['confirmation_source'] = source
        if fully_closed is not None:
            result['fully_closed'] = bool(fully_closed)
        return result

    def _confirm_market_order(self, ccxt_symbol, initial_order, client_order_id, *,
                              operation, side, pre_contracts, requested_contracts,
                              require_filled_attribution=False):
        """轮询订单终态，并以净持仓变化交叉确认实际成交张数。

        返回 ``(result, last_position_contracts)``。只有订单终态与仓位 delta 一致，
        或仓位已达到订单理论上不可能超越的完整目标时才返回 result。
        """
        order = dict(initial_order or {})
        order_id = order.get('id')
        tolerance = self._contracts_tolerance(ccxt_symbol)
        last_post_contracts = pre_contracts
        terminal_statuses = {'closed', 'canceled', 'cancelled', 'rejected', 'expired'}

        for attempt in range(self.ORDER_CONFIRM_ATTEMPTS):
            try:
                fetched = self._fetch_order_for_confirmation(
                    ccxt_symbol, order_id, client_order_id)
                if isinstance(fetched, dict):
                    order.update(fetched)
                    order_id = order.get('id') or order_id
            except Exception as e:
                logger.warning(f"{ccxt_symbol} 第{attempt + 1}次订单终态查询失败: {e}")

            position = None
            position_known = False
            try:
                position = self.get_position(ccxt_symbol)
                position_known = True
                last_post_contracts = (
                    abs(float(position['contracts']))
                    if position and position.get('contracts') is not None else 0.0)
            except Exception as e:
                logger.warning(f"{ccxt_symbol} 第{attempt + 1}次成交后持仓查询失败: {e}")
                # 旧仓位快照只能用于最终诊断/补偿量，不能与本轮新取得的订单
                # 终态拼成 confirmed；订单与仓位证明必须来自同一轮成功观测。
                position_known = False
                position = None

            if position_known and position:
                actual_side = self._position_side(position)
                if actual_side and actual_side != side:
                    logger.critical(
                        f"{ccxt_symbol} 成交确认发现方向异常: 预期={side}, 实际={actual_side}")
                    return None, last_post_contracts

            if operation == 'open':
                delta = max(0.0, last_post_contracts - pre_contracts) if position_known else None
                fully_resolved = delta is not None and delta + tolerance >= requested_contracts
            else:
                delta = max(0.0, pre_contracts - last_post_contracts) if position_known else None
                fully_resolved = position_known and last_post_contracts <= tolerance

            status = str(order.get('status') or '').lower()
            filled = self._finite_nonnegative(order.get('filled'))
            terminal = status in terminal_statuses

            # 恢复既有 clOrdId 时没有“发单前刚确认 flat”的同轮基线；终态缺
            # filled 也不能拿当前净仓冒充本单成交。无论 delta 是否达到计划量，
            # open 的 filled 缺失/不一致都先进入归因隔离，绝不自动平整仓。
            open_attribution_ambiguous = (
                operation == 'open' and terminal and delta is not None and (
                    (require_filled_attribution and filled is None) or
                    (filled is not None and abs(filled - delta) > tolerance)))
            if open_attribution_ambiguous:
                ambiguous = dict(order)
                for key in ('average', 'cost', 'fee', 'fees'):
                    ambiguous.pop(key, None)
                ambiguous['execution_ambiguous'] = True
                attributable = filled if filled is not None else 0.0
                result = self._confirmed_order_result(
                    ccxt_symbol, ambiguous, requested_contracts, attributable,
                    fully_closed=False, source='position-attribution-ambiguous')
                result['open_execution_attribution_ambiguous'] = True
                result['observed_position_amount'] = self._contracts_to_coins(
                    ccxt_symbol, delta)
                logger.critical(
                    f'{ccxt_symbol} 开仓归因不确定: status={status}, filled={filled}, '
                    f'delta={delta}；拒绝自动处置整仓')
                return result, last_post_contracts

            # 完整目标已由仓位证明，但仍尽量等到订单终态以取得真实 VWAP/fee。
            # 若终态 filled 与仓位 delta 不同，说明保护止损/人工交易并发介入；
            # 仓位现实仍可收口，但该订单的价格/手续费不能冒充全部成交。
            if fully_resolved and delta is not None:
                filled_matches = filled is None or abs(filled - delta) <= tolerance
                if terminal and not filled_matches:
                    ambiguous = dict(order)
                    for key in ('average', 'cost', 'fee', 'fees'):
                        ambiguous.pop(key, None)
                    ambiguous['execution_ambiguous'] = True
                    logger.critical(
                        f"{ccxt_symbol} 仓位已达目标但订单 filled={filled} 与 "
                        f"delta={delta} 不一致，疑似止损/人工成交并发介入")
                    return self._confirmed_order_result(
                        ccxt_symbol, ambiguous, requested_contracts, delta,
                        fully_closed=(operation == 'close'),
                        source='position-ambiguous'), last_post_contracts
                if terminal:
                    return self._confirmed_order_result(
                        ccxt_symbol, order, requested_contracts, delta,
                        fully_closed=(operation == 'close'),
                        source='terminal+position'), last_post_contracts

            # 部分成交只有在订单已终态，且订单 filled 与仓位变化一致时才安全上报。
            if terminal and delta is not None:
                if filled is None:
                    # 某些 OKX/ccxt 响应缺 filled；终态下持仓 delta 是唯一实值。
                    filled = delta
                if abs(filled - delta) <= tolerance:
                    if delta <= tolerance:
                        return None, last_post_contracts
                    return self._confirmed_order_result(
                        ccxt_symbol, order, requested_contracts, delta,
                        fully_closed=(operation == 'close' and last_post_contracts <= tolerance),
                        source='terminal+position'), last_post_contracts
                logger.warning(
                    f"{ccxt_symbol} 订单终态与仓位变化暂不一致: status={status}, "
                    f"filled={filled}, delta={delta}")

            if attempt < self.ORDER_CONFIRM_ATTEMPTS - 1:
                time.sleep(self.ORDER_CONFIRM_DELAY)

        logger.error(
            f"{ccxt_symbol} 市价单无法确认: operation={operation}, order_id={order_id}, "
            f"pre={pre_contracts}, last={last_post_contracts}, requested={requested_contracts}")
        return None, last_post_contracts

    def _resolve_unconfirmed_open(
            self, ccxt_symbol, side, contracts, client_order_id, ack,
            last_contracts, require_filled_attribution=False):
        """撤销未决开仓余量并裁决迟到成交；当前零仓不等于订单已死。"""
        cancel_ref = ack.get('id') if isinstance(ack, dict) else None
        try:
            if cancel_ref:
                self.exchange.cancel_order(str(cancel_ref), ccxt_symbol)
            else:
                self.exchange.cancel_order(
                    client_order_id, ccxt_symbol,
                    params={'clOrdId': client_order_id})
        except Exception as e:
            logger.warning(f"{ccxt_symbol} 不可确认开仓的剩余量撤销未确认: {e}")

        retry_result, last_contracts = self._confirm_market_order(
            ccxt_symbol, ack, client_order_id, operation='open', side=side,
            pre_contracts=0.0, requested_contracts=contracts,
            require_filled_attribution=require_filled_attribution)
        if retry_result:
            retry_result.setdefault('clientOrderId', client_order_id)
            if not retry_result.get('id'):
                retry_result['id'] = 'timeout_confirmed'
            logger.info(
                f"开仓撤余量后成交已确认: {ccxt_symbol} {side} "
                f"{retry_result['amount']}币, 完整成交={retry_result['fully_filled']}")
            return retry_result

        tolerance = self._contracts_tolerance(ccxt_symbol)
        if last_contracts <= tolerance:
            # 只有“订单已终态 + 同一轮交易所持仓为零”才能证明不会迟到成交。
            try:
                final_order = self._fetch_order_for_confirmation(
                    ccxt_symbol, cancel_ref, client_order_id)
                final_status = str((final_order or {}).get('status') or '').lower()
                final_position = self.get_position(ccxt_symbol)
                terminal = final_status in {
                    'closed', 'canceled', 'cancelled', 'rejected', 'expired'}
                if terminal and not final_position:
                    logger.warning(
                        f'{ccxt_symbol} 未决开仓已确认终态且零持仓，不会迟到成交')
                    return None
            except Exception as e:
                logger.warning(
                    f'{ccxt_symbol} 未决开仓零持仓但订单终态仍不可证明: {e}')

            logger.critical(
                f'{ccxt_symbol} 当前虽为零仓，但开仓订单未证明终态；'
                '返回未决契约，禁止遗忘可能的迟到成交')
            return {
                'id': cancel_ref,
                'clientOrderId': client_order_id,
                'status': 'order_may_remain_live',
                'confirmed': False,
                'open_execution_unresolved': True,
                'open_order_may_remain_live': True,
                'amount': self._contracts_to_coins(ccxt_symbol, contracts),
                'remaining_amount': 0.0,
                'compensation': None,
                'info': '当前零仓但未决开仓订单未证明终态',
            }

        # 撤余量后仍不可确认但确有仓位，以当前实际量执行 reduce-only 补偿。
        logger.critical(
            f"{ccxt_symbol} 开仓无法确认但检测到 {last_contracts} 张，执行紧急补偿平仓")
        compensation = self.close_position(
            ccxt_symbol, side,
            self._contracts_to_coins(ccxt_symbol, last_contracts),
            client_order_id=self.compensation_client_order_id(
                client_order_id))
        if compensation and compensation.get('fully_closed') is True:
            # 补偿归零之后原开仓单仍可能活着并迟到成交。只有同一轮同时看见
            # 原单终态和零仓，才能消费恢复句柄；否则保留 intent + quarantine。
            try:
                final_order = self._fetch_order_for_confirmation(
                    ccxt_symbol, cancel_ref, client_order_id)
                final_status = str((final_order or {}).get('status') or '').lower()
                final_position = self.get_position(ccxt_symbol)
                terminal = final_status in {
                    'closed', 'canceled', 'cancelled', 'rejected', 'expired'}
            except Exception as exc:
                logger.warning(
                    f'{ccxt_symbol} 补偿全平后原开仓终态复核失败: {exc}')
                terminal = False
                final_position = None
            if terminal and not final_position:
                return {
                    'id': cancel_ref,
                    'clientOrderId': client_order_id,
                    'status': 'compensated_flat',
                    'confirmed': False,
                    'open_execution_compensated': True,
                    'amount': self._contracts_to_coins(ccxt_symbol, last_contracts),
                    'remaining_amount': 0.0,
                    'average': (ack.get('average') if isinstance(ack, dict) else None),
                    'compensation': compensation,
                    'info': '原开仓已终态且 reduce-only 补偿后同轮确认全平',
                }
            observed = 0.0
            if final_position and final_position.get('contracts') is not None:
                observed = self._contracts_to_coins(
                    ccxt_symbol, abs(float(final_position['contracts'])))
            logger.critical(
                f'{ccxt_symbol} 补偿曾归零，但原开仓未证明终态/同轮零仓；'
                '保留未决句柄防迟到成交')
            return {
                'id': cancel_ref,
                'clientOrderId': client_order_id,
                'status': 'post_compensation_unresolved',
                'confirmed': False,
                'open_execution_unresolved': True,
                'open_order_may_remain_live': not terminal,
                'open_execution_attribution_ambiguous': bool(final_position),
                'amount': self._contracts_to_coins(ccxt_symbol, last_contracts),
                'remaining_amount': observed,
                'compensation': compensation,
                'info': '补偿后原开仓终态与零仓未能同时证明',
            }
        remaining_amount = (
            compensation.get('remaining_amount')
            if isinstance(compensation, dict) else
            self._contracts_to_coins(ccxt_symbol, last_contracts))
        logger.critical(
            f"{ccxt_symbol} 不可确认开仓的补偿平仓仍有余仓 "
            f"{remaining_amount}币；返回 unresolved 契约，禁止上层遗忘！")
        return {
            'id': cancel_ref,
            'clientOrderId': client_order_id,
            'status': 'compensation_incomplete',
            'confirmed': False,
            'open_execution_unresolved': True,
            'amount': self._contracts_to_coins(ccxt_symbol, last_contracts),
            'remaining_amount': remaining_amount,
            'compensation': compensation,
            'info': '开仓无法确认且 reduce-only 补偿未能全平，必须隔离接管',
        }

    # ===================== 写操作：超时后查询确认 =====================

    def find_existing_open_order(self, symbol, side, amount, client_order_id):
        """只读查询确定性 clOrdId 对应的既有开仓单。

        严格核对 symbol/market/side/amount/reduceOnly。明确 OrderNotFound 返回
        None；查询不确定或字段不一致一律抛出，调用方必须 fail-closed。本方法
        永不创建订单。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        order_side = 'buy' if side == 'long' else 'sell'
        client_order_id = self._client_order_id(client_order_id)
        contracts = self._coin_to_contracts(ccxt_symbol, amount)
        if contracts <= 0:
            raise ValueError(f"{ccxt_symbol} 查询既有开仓单时预期张数无效: {amount}")
        status, order = self._fetch_order_tristate(ccxt_symbol, client_order_id)
        if status == 'absent':
            return None
        if not self._existing_order_matches_request(
                order, ccxt_symbol, order_side, contracts, reduce_only=False):
            raise RuntimeError(
                f"{ccxt_symbol} clOrdId={client_order_id} 命中订单与 pending 预期不一致")
        return order

    def open_position(self, symbol, side, amount, client_order_id=None):
        """安全开仓（市价单）。amount 单位为币数。

        开仓前必须可证明交易所为空仓；下单 ACK 后必须确认订单终态与仓位
        delta。终态部分成交会按实际币数返回，由上层按实际量挂止损/记账。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        order_side = 'buy' if side == 'long' else 'sell'

        contracts = self._coin_to_contracts(ccxt_symbol, amount)
        if contracts <= 0:
            logger.error(f"{ccxt_symbol} 开仓张数为0（amount={amount}币，不足一张），放弃开仓")
            return None

        supplied_client_id = client_order_id is not None
        try:
            client_order_id = self._client_order_id(client_order_id)
        except ValueError as e:
            logger.error(f"{ccxt_symbol} 拒绝非法幂等订单号: {e}")
            return None

        # 信号层提供确定性 clOrdId 时，重试/重启先查询同一订单，绝不再次 create。
        # 三态裁决：OrderNotFound=确定不存在（可新发单）；命中且一致=复用；
        # 响应畸形/查询失败=无法裁决，一律拒绝新 POST。
        if supplied_client_id:
            try:
                status, existing = self._fetch_order_tristate(
                    ccxt_symbol, client_order_id)
            except Exception as e:
                logger.error(f"{ccxt_symbol} 幂等开仓订单查询无法裁决，拒绝新下单: {e}")
                return None
            if status == 'found':
                if not self._existing_order_matches_request(
                        existing, ccxt_symbol, order_side, contracts, reduce_only=False):
                    return None
                result, _last = self._confirm_market_order(
                    ccxt_symbol, existing, client_order_id, operation='open', side=side,
                    pre_contracts=0.0, requested_contracts=contracts,
                    require_filled_attribution=True)
                if result:
                    result['clientOrderId'] = client_order_id
                    logger.info(f"{ccxt_symbol} 命中既有幂等开仓订单 {client_order_id}，未重复下单")
                    return result
                return self._resolve_unconfirmed_open(
                    ccxt_symbol, side, contracts, client_order_id,
                    existing, _last, require_filled_attribution=True)

        try:
            pre_position = self.get_position(ccxt_symbol)
        except Exception as e:
            logger.error(f"{ccxt_symbol} 开仓前持仓查询失败，拒绝开仓: {e}")
            return None
        if pre_position is not None:
            logger.error(
                f"{ccxt_symbol} 交易所已有 {pre_position.get('side')} "
                f"{pre_position.get('contracts')} 张，拒绝叠加/对冲开仓")
            return None

        # 设置保证金模式/杠杆可能经历网络等待，必须放在最终空仓+空单
        # 快照之前，否则旧挂单可在 setup 期间成交后再被叠加新仓。
        try:
            self.setup_symbol(ccxt_symbol)
        except Exception as e:
            logger.error(f"{ccxt_symbol} 开仓前交易参数设置失败，拒绝开仓: {e}")
            return None

        try:
            self.assert_no_stale_protective_orders(ccxt_symbol)
        except Exception as e:
            # 空仓时任何普通/算法挂单都可能与新开仓并发成交。
            # 适配层不擅自撤人工单，只拒绝开仓交由上层隔离/人工裁决。
            logger.critical(f"{ccxt_symbol} 开仓前遗留挂单核验失败，拒绝开仓: {e}")
            return None

        # pending 中的限价/算法单可在清单快照期间成交并随即消失。
        # create_order 紧前再读持仓，不让“挂单消失=安全”的 TOCTOU 叠仓。
        try:
            final_pre_position = self.get_position(ccxt_symbol)
        except Exception as e:
            logger.error(f"{ccxt_symbol} 发单紧前持仓复核失败，拒绝开仓: {e}")
            return None
        if final_pre_position is not None:
            logger.critical(
                f"{ccxt_symbol} 挂单预检期间出现 "
                f"{final_pre_position.get('side')} {final_pre_position.get('contracts')} 张，"
                '拒绝叠加/对冲开仓')
            return None

        params = self._order_params(extra={'clOrdId': client_order_id})
        ack = None

        try:
            ack = self.exchange.create_order(
                ccxt_symbol, 'market', order_side, contracts, None, params
            )
        except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
            # clOrdId 在发单前生成；即使 HTTP ACK 丢失也能查询同一订单，禁止重下。
            logger.warning(f"开仓请求超时: {e}，按 clOrdId={client_order_id} 查询终态")
        except (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.BadRequest) as e:
            logger.error(f"开仓业务异常: {e}")
            return None
        except Exception as e:
            logger.error(f"开仓未知异常: {e}")
            return None

        result, last_contracts = self._confirm_market_order(
            ccxt_symbol, ack, client_order_id, operation='open', side=side,
            pre_contracts=0.0, requested_contracts=contracts)
        if result:
            result.setdefault('clientOrderId', client_order_id)
            if not result.get('id'):
                result['id'] = 'timeout_confirmed'
                result['clientOrderId'] = client_order_id
            logger.info(
                f"开仓成交已确认: {ccxt_symbol} {side} "
                f"{result['amount']}币, 订单ID={result.get('id') or client_order_id}, "
                f"完整成交={result['fully_filled']}")
            return result

        return self._resolve_unconfirmed_open(
            ccxt_symbol, side, contracts, client_order_id, ack,
            last_contracts)

    def close_position(self, symbol, side, amount, client_order_id=None):
        """安全平仓（市价单，reduce-only）。amount 单位为币数。

        返回的 ``amount`` 是已确认实际成交币数；``fully_closed`` 只有在
        交易所净持仓已归零时为 True。上层不得用部分成交结果删除完整账本。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        close_side = 'sell' if side == 'long' else 'buy'

        try:
            pre_position = self.get_position(ccxt_symbol)
        except Exception as e:
            logger.error(f"{ccxt_symbol} 平仓前持仓查询失败，无法建立成交基线: {e}")
            return None

        pre_contracts = abs(float(pre_position['contracts'])) if pre_position and pre_position.get('contracts') else 0.0

        if pre_contracts == 0 and client_order_id is None:
            logger.warning(f"{ccxt_symbol} 欧易端已无持仓，跳过平仓指令（可能已手动平仓）")
            return {
                'id': 'already_closed', 'average': None, 'status': 'closed',
                'amount': 0.0, 'requested_amount': amount, 'confirmed': True,
                'fully_filled': True, 'fully_closed': True,
            }

        actual_side = self._position_side(pre_position)
        if actual_side and actual_side != side:
            logger.critical(
                f"{ccxt_symbol} 拒绝按错误方向平仓: 本地请求={side}, 交易所={actual_side}")
            return None

        requested_contracts = self._coin_to_contracts(ccxt_symbol, amount)
        supplied_client_id = client_order_id is not None
        try:
            client_order_id = self._client_order_id(client_order_id)
        except ValueError as e:
            logger.error(f"{ccxt_symbol} 拒绝非法幂等订单号: {e}")
            return None
        existing_legs = []
        if supplied_client_id:
            try:
                existing_legs = self._collect_existing_close_legs(
                    ccxt_symbol, close_side, requested_contracts,
                    client_order_id)
            except Exception as e:
                logger.error(
                    f"{ccxt_symbol} 幂等平仓全部腿查询失败，拒绝新下单: {e}")
                return None
            if not existing_legs and pre_contracts == 0:
                logger.warning(
                    f"{ccxt_symbol} 已无持仓且未查到幂等平仓各腿 "
                    f"{client_order_id}，按外部已平处理且不再发单")
                return {
                    'id': 'already_closed', 'average': None, 'status': 'closed',
                    'amount': 0.0, 'requested_amount': amount, 'confirmed': True,
                    'fully_filled': True, 'fully_closed': True,
                    'clientOrderId': client_order_id,
                    'execution_ambiguous': True,
                }

        tolerance = self._contracts_tolerance(ccxt_symbol)
        if (supplied_client_id and not existing_legs and
                pre_contracts > tolerance and requested_contracts > tolerance and
                abs(pre_contracts - requested_contracts) > tolerance):
            logger.critical(
                f'{ccxt_symbol} 新 close intent 的账本计划量 '
                f'{requested_contracts} 张与交易所当前 {pre_contracts} 张不一致；'
                '拒绝猜测哪部分属于系统，先隔离对账')
            return None
        # 新请求不得超平；崩溃恢复则把基础腿+r1/r2 的终态 filled 汇总后，
        # 与“原 intent 仓位 - 当前余仓”交叉证明。只看第一腿会丢掉后续腿。
        if existing_legs:
            target_contracts = requested_contracts
            recovered_filled = sum(leg[3] for leg in existing_legs)
            observed_delta = max(0.0, requested_contracts - pre_contracts)
            if abs(recovered_filled - observed_delta) > tolerance:
                logger.critical(
                    f'{ccxt_symbol} 平仓恢复归因不一致: 全部腿 filled='
                    f'{recovered_filled}, intent→当前仓位 delta={observed_delta}')
                if pre_contracts <= tolerance and observed_delta > tolerance:
                    # 仓位现实已经归零，账本必须收口；但订单价格/费用不可冒充
                    # 全部退出成交，交给上层按保守价记账并告警。
                    return {
                        'id': existing_legs[-1][1].get('id'),
                        'ids': [str(leg[1].get('id')) for leg in existing_legs],
                        'clientOrderId': client_order_id,
                        'clientOrderIds': [leg[0] for leg in existing_legs],
                        'status': 'closed',
                        'amount': self._contracts_to_coins(
                            ccxt_symbol, observed_delta),
                        'requested_amount': self._contracts_to_coins(
                            ccxt_symbol, requested_contracts),
                        'filled': observed_delta, 'confirmed': True,
                        'fully_filled': True, 'fully_closed': True,
                        'remaining_amount': 0.0,
                        'execution_ambiguous': True,
                        'confirmation_source': 'all-legs-position-ambiguous',
                    }
                # 仍有真钱余仓时不能把混入止损/人工成交的 delta 强行归给
                # 本 intent，更不能继续补平可能属于人工的同向仓。
                return None
            total_filled_contracts = recovered_filled
            last_contracts = pre_contracts
            legs = [
                (leg[1], leg[3]) for leg in existing_legs
                if leg[3] > tolerance]
            next_leg_index = len(existing_legs)
            logger.info(
                f'{ccxt_symbol} 命中 {len(existing_legs)} 条幂等平仓腿，'
                '已汇总真实终态且未重复下单')
        else:
            target_contracts = (
                min(requested_contracts, pre_contracts)
                if requested_contracts > 0 else pre_contracts)
            total_filled_contracts = 0.0
            last_contracts = pre_contracts
            legs = []
            next_leg_index = 0

        for leg_index in range(next_leg_index, self.MAX_CLOSE_LEGS):
            remaining_target = min(
                max(0.0, target_contracts - total_filled_contracts),
                last_contracts)
            if remaining_target <= tolerance:
                break
            leg_client_id = self._close_leg_client_order_id(
                client_order_id, leg_index)
            params = self._order_params(
                reduce_only=True, extra={'clOrdId': leg_client_id})
            ack = None
            try:
                ack = self.exchange.create_order(
                    ccxt_symbol, 'market', close_side, remaining_target, None, params)
            except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
                logger.warning(
                    f"平仓第{leg_index + 1}腿请求超时: {e}，"
                    f"按 clOrdId={leg_client_id} 查询终态")
            except Exception as e:
                logger.error(f"平仓第{leg_index + 1}腿异常: {e}")
                break
            leg_pre_contracts = last_contracts
            leg_result, last_contracts = self._confirm_market_order(
                ccxt_symbol, ack, leg_client_id, operation='close', side=side,
                pre_contracts=leg_pre_contracts,
                requested_contracts=remaining_target)
            if not leg_result:
                cancel_ref = ack.get('id') if isinstance(ack, dict) else None
                try:
                    if cancel_ref:
                        self.exchange.cancel_order(str(cancel_ref), ccxt_symbol)
                    else:
                        self.exchange.cancel_order(
                            leg_client_id, ccxt_symbol,
                            params={'clOrdId': leg_client_id})
                except Exception as e:
                    logger.warning(
                        f"{ccxt_symbol} 平仓第{leg_index + 1}腿撤余量未确认: {e}")
                leg_result, last_contracts = self._confirm_market_order(
                    ccxt_symbol, ack, leg_client_id, operation='close', side=side,
                    pre_contracts=leg_pre_contracts,
                    requested_contracts=remaining_target)
                if not leg_result:
                    logger.error(
                        f"平仓第{leg_index + 1}腿成交无法确认: {ccxt_symbol} {side}")
                    break
            leg_result.setdefault('clientOrderId', leg_client_id)
            if not leg_result.get('id'):
                leg_result['id'] = 'timeout_confirmed'
            actual_leg_contracts = max(0.0, leg_pre_contracts - last_contracts)
            if actual_leg_contracts <= tolerance:
                break
            total_filled_contracts += actual_leg_contracts
            legs.append((leg_result, actual_leg_contracts))
            if last_contracts <= tolerance:
                break
            logger.warning(
                f"{ccxt_symbol} 平仓第{leg_index + 1}腿仅成交 "
                f"{actual_leg_contracts}/{remaining_target} 张，继续 reduce-only 补平余量")

        if not legs:
            logger.error(f"平仓成交无法确认: {ccxt_symbol} {side} {amount}")
            return None

        aggregate = self._aggregate_close_legs(
            ccxt_symbol, legs, target_contracts,
            total_filled_contracts, last_contracts)

        if aggregate['fully_closed']:
            aggregate['status'] = 'closed'
            logger.info(
                f"平仓成交已确认且仓位归零: {ccxt_symbol} {side} "
                f"{aggregate['amount']}币, 订单={aggregate['ids']}")
        else:
            logger.critical(
                f"{ccxt_symbol} {len(legs)}腿补平后仍剩 {aggregate['remaining_amount']}币；"
                f"返回实际部分成交，调用方必须原子缩减账本并重挂余仓止损")
        return aggregate

    def _aggregate_close_legs(self, ccxt_symbol, legs, target_contracts,
                              total_filled_contracts, last_contracts):
        """聚合多腿真实执行：币数、VWAP、cost、fee/fees 都不丢失。"""
        tolerance = self._contracts_tolerance(ccxt_symbol)
        aggregate = dict(legs[-1][0])
        aggregate['ids'] = [str(leg.get('id')) for leg, _qty in legs]
        aggregate['clientOrderIds'] = [leg.get('clientOrderId') for leg, _qty in legs]
        aggregate['amount'] = self._contracts_to_coins(ccxt_symbol, total_filled_contracts)
        aggregate['requested_amount'] = self._contracts_to_coins(ccxt_symbol, target_contracts)
        aggregate['filled'] = total_filled_contracts
        aggregate['confirmed'] = True
        aggregate['fully_filled'] = (
            total_filled_contracts + tolerance >= target_contracts)
        aggregate['fully_closed'] = last_contracts <= tolerance
        aggregate['remaining_amount'] = self._contracts_to_coins(ccxt_symbol, last_contracts)

        weighted_value = 0.0
        weighted_qty = 0.0
        total_cost = 0.0
        cost_known = False
        fees = []
        for leg, qty in legs:
            average = self._finite_nonnegative(leg.get('average'))
            if average is not None and average > 0:
                weighted_value += average * qty
                weighted_qty += qty
            cost = self._finite_nonnegative(leg.get('cost'))
            if cost is not None:
                total_cost += cost
                cost_known = True
            if isinstance(leg.get('fees'), list):
                fees.extend(leg['fees'])
            elif isinstance(leg.get('fee'), dict):
                fees.append(leg['fee'])
        if weighted_qty > 0:
            aggregate['average'] = weighted_value / weighted_qty
        if cost_known:
            aggregate['cost'] = total_cost
        if fees:
            aggregate['fees'] = fees
            currencies = {f.get('currency') for f in fees if isinstance(f, dict)}
            costs = [self._finite_nonnegative(f.get('cost')) for f in fees if isinstance(f, dict)]
            if len(currencies) == 1 and None not in currencies and all(v is not None for v in costs):
                aggregate['fee'] = {'currency': next(iter(currencies)), 'cost': sum(costs)}
        return aggregate

    def find_compensation_close_evidence(self, symbol, side, amount,
                                         open_client_order_id):
        """只读找回由开仓句柄派生的补偿平仓腿聚合成交；绝不发送下单请求。

        前置条件：调用方已确认交易所该品种净持仓为零。用途是判断历史补偿
        平仓是否真实发生过，以便用真实退出价补记往返——此前该场景误用可
        下单的 close_position()，极端竞态下（确认空仓后用户人工开出同方向
        同数量仓位）会把人工仓平掉。返回：
          - None — 明确不存在补偿腿（OrderNotFound），或成交未覆盖请求量
                   （证据不完整，调用方按保守价兜底）；
          - dict — 全部腿终态且覆盖请求量的聚合结果（average/fees/ids）。
        查询不确定或腿内容与请求不一致一律抛出。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        close_side = 'sell' if side == 'long' else 'buy'
        base_client_id = self.compensation_client_order_id(open_client_order_id)
        requested_contracts = self._coin_to_contracts(ccxt_symbol, amount)
        if requested_contracts <= 0:
            raise ValueError(
                f"{ccxt_symbol} 查询补偿平仓腿时预期张数无效: {amount}")
        existing_legs = self._collect_existing_close_legs(
            ccxt_symbol, close_side, requested_contracts, base_client_id)
        if not existing_legs:
            return None
        tolerance = self._contracts_tolerance(ccxt_symbol)
        total_filled = sum(leg[3] for leg in existing_legs)
        if total_filled + tolerance < requested_contracts:
            logger.warning(
                f'{ccxt_symbol} 补偿腿累计成交 {total_filled} 张未覆盖请求 '
                f'{requested_contracts} 张，证据不完整，不当完整补偿')
            return None
        legs = [(leg[1], leg[3]) for leg in existing_legs if leg[3] > tolerance]
        if not legs:
            return None
        aggregate = self._aggregate_close_legs(
            ccxt_symbol, legs, requested_contracts, total_filled, 0.0)
        aggregate.setdefault('clientOrderId', base_client_id)
        aggregate['status'] = 'closed'
        aggregate['read_only_evidence'] = True
        return aggregate

    @staticmethod
    def _algo_order_matches(order, stop_side, stop_price, contracts, expected_order_id=None):
        """严格判断算法单是否为本地记录的保护性止损。

        必须同时满足：记录 ID（若有）、conditional 类型、reduceOnly、触发后市价
        (slOrdPx=-1)、state=live、slTriggerPxType=last（与本系统创建口径一致）、
        非对冲 posSide、方向、触发价、张数。任何字段读不到一律视为不匹配——
        已暂停/已触发/已撤销或触发价类型不同的算法单都不是完整保护。
        """
        if not order or order.get('side') != stop_side:
            return False
        if expected_order_id is not None and str(order.get('id')) != str(expected_order_id):
            return False
        info = order.get('info') or {}
        if not OkxApi._order_reduce_only(order):
            return False
        if info.get('ordType') != 'conditional':
            return False
        if info.get('state') != 'live':
            return False
        if info.get('slTriggerPxType') != 'last':
            return False
        if info.get('posSide') in ('long', 'short'):
            return False
        try:
            if float(info.get('slOrdPx')) != -1.0:
                return False
        except (TypeError, ValueError):
            return False
        trigger = (order.get('stopLossPrice') or order.get('triggerPrice') or order.get('stopPrice')
                   or info.get('slTriggerPx') or info.get('triggerPx'))
        try:
            trigger = float(trigger)
            stop_price = float(stop_price)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(trigger) and math.isfinite(stop_price)):
            return False
        # 两边都已按同一 tick/step 对齐，只容忍浮点表示的数个 ULP；相差一个
        # 真实价格 tick 或数量 step 必须判 mismatch，不能用随数值放大的 ppm。
        if abs(trigger - stop_price) > max(math.ulp(trigger), math.ulp(stop_price)) * 4:
            return False
        amount = order.get('amount')
        if amount is None:
            amount = info.get('sz')
        try:
            amount = float(amount)
            contracts = float(contracts)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(amount) and math.isfinite(contracts)):
            return False
        return abs(amount - contracts) <= max(math.ulp(amount), math.ulp(contracts)) * 4

    @staticmethod
    def _algo_client_order_id(order):
        if not order:
            return None
        info = order.get('info') or {}
        value = (order.get('clientOrderId') or info.get('algoClOrdId')
                 or info.get('clientOrderId'))
        return str(value) if value else None

    @staticmethod
    def _is_protective_stop_candidate(order, stop_side):
        """识别任何可能平掉当前/未来同方向净仓的算法单。

        系统自己的精确保护仍由 ``_algo_order_matches`` 限定为 conditional
        市价止损；这里故意更宽：人工/旧版本留下的 trigger、OCO、移动止损
        同样可能在未来仓位上触发，必须把“唯一正常止损 + 另一张未知算法单”
        判成 mismatch，而不能因类型不同就隐身。
        """
        if not order or order.get('side') != stop_side:
            return False
        info = order.get('info') or {}
        return (
            OkxApi._order_reduce_only(order) and
            info.get('ordType') in {'conditional', 'oco', 'trigger', 'move_order_stop'})

    def assert_no_stale_protective_orders(self, symbol):
        """新开仓前证明该品种没有任何遗留普通/算法挂单。"""
        ccxt_symbol = self._resolve_symbol(symbol)
        normal, algos = self._fetch_pending_snapshot(ccxt_symbol)
        if normal or algos:
            stale = [
                {'id': order.get('id'), 'kind': 'normal',
                 'side': order.get('side'),
                 'ordType': (order.get('info') or {}).get('ordType')}
                for order in normal
            ]
            stale.extend(
                {'id': order.get('id'), 'kind': 'algo',
                 'side': order.get('side'),
                 'ordType': (order.get('info') or {}).get('ordType')}
                for order in algos)
            raise RuntimeError(
                f'{ccxt_symbol} 空仓开仓前发现 {len(stale)} 张遗留挂单，'
                f'拒绝开仓: {stale[:5]}')
        return True

    def _find_stop_by_client_id(self, algos, client_order_id, stop_side,
                                stop_price, contracts):
        """按 algoClOrdId 严格确认唯一且内容一致的止损；歧义直接抛出。"""
        same_client = [
            order for order in algos
            if self._algo_client_order_id(order) == str(client_order_id)]
        if not same_client:
            return None
        if len(same_client) != 1:
            raise RuntimeError(
                f"algoClOrdId={client_order_id} 对应 {len(same_client)} 张算法单，拒绝裁决")
        order = same_client[0]
        if not self._algo_order_matches(order, stop_side, stop_price, contracts):
            raise RuntimeError(
                f"algoClOrdId={client_order_id} 命中订单但保护内容不一致，拒绝收养")
        return order

    def _align_stop_price(self, ccxt_symbol, stop_price):
        """把止损触发价对齐到交易所价格步长（tick）。

        实盘验证实证：OKX 会把非对齐触发价按 tick 取整后存储（39.384→39.38），
        本地原始价与交易所存储价的差会让严格匹配（创建超时确认/四态裁决）误判
        不匹配/mismatch。发单前先用交易所元数据对齐——发送值与存储值必然一致；
        比对侧用同一函数对齐本地记录，历史留存的非对齐价也能正确匹配。
        精度元数据不可得时按原价返回（匹配退化为对齐前行为，fail-safe 不阻断）。
        """
        try:
            return float(self.exchange.price_to_precision(ccxt_symbol, stop_price))
        except Exception:
            return stop_price

    def create_stop_loss_order(self, symbol, side, amount, stop_price,
                               client_order_id=None):
        """幂等创建止损算法单（reduce-only，触发后市价平仓）。

        同一保护意图使用固定 ``algoClOrdId``，发单前先查、发单后再按该 ID
        严格确认。POST 全程最多一次：超时或查询不确定时绝不盲重发。
        amount 单位为币数。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        stop_side = 'sell' if side == 'long' else 'buy'
        stop_price = self._align_stop_price(ccxt_symbol, stop_price)

        contracts = self._coin_to_contracts(ccxt_symbol, amount)
        if contracts <= 0:
            # 用实际持仓张数兜底，避免因换算误差漏挂止损
            try:
                contracts = self._position_contracts(ccxt_symbol)
            except Exception:
                contracts = 0.0
        if contracts <= 0:
            logger.error(f"{ccxt_symbol} 止损单张数为0，放弃创建止损单")
            return None

        try:
            algo_client_id = self._stop_client_order_id(
                ccxt_symbol, stop_side, contracts, stop_price, client_order_id)
        except ValueError as e:
            logger.error(f"{ccxt_symbol} 非法止损幂等 ID，拒绝发单: {e}")
            return None

        # 重启/上轮超时恢复：先按固定 algoClOrdId 查现有单，命中即复用。
        try:
            existing = self._find_stop_by_client_id(
                self._fetch_algo_orders(ccxt_symbol), algo_client_id,
                stop_side, stop_price, contracts)
        except Exception as e:
            # 查询不完整时无法证明“尚未创建”，绝不 POST。
            logger.error(f"{ccxt_symbol} 止损幂等预查失败，拒绝盲发: {e}")
            return None
        if existing:
            logger.info(
                f"复用既有止损单: {ccxt_symbol} algoClOrdId={algo_client_id}, "
                f"订单ID={existing.get('id')}")
            return existing

        try:
            size_text = self.exchange.amount_to_precision(ccxt_symbol, contracts)
        except Exception:
            size_text = str(contracts)
        try:
            price_text = self.exchange.price_to_precision(ccxt_symbol, stop_price)
        except Exception:
            price_text = str(stop_price)
        request = {
            'instId': self._to_inst_id(ccxt_symbol),
            'tdMode': self.margin_mode,
            'side': stop_side,
            'ordType': 'conditional',
            'sz': str(size_text),
            'slTriggerPx': str(price_text),
            'slTriggerPxType': 'last',
            'slOrdPx': '-1',
            'reduceOnly': 'true',
            'algoClOrdId': algo_client_id,
        }
        try:
            response = self.exchange.privatePostTradeOrderAlgo(request)
            data = response.get('data') if isinstance(response, dict) else None
            item = data[0] if isinstance(data, list) and data else None
            if (not isinstance(response, dict) or response.get('code') != '0'
                    or not isinstance(item, dict)
                    or item.get('sCode') not in (None, '', '0')
                    or not (item.get('algoId') or item.get('algoClOrdId'))):
                raise RuntimeError(f"OKX 止损 ACK 异常: {str(response)[:240]}")
        except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
            logger.warning(
                f"止损 POST 超时（不会重发）: {ccxt_symbol} "
                f"algoClOrdId={algo_client_id}: {e}")
        except (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.BadRequest) as e:
            logger.error(f"止损单业务异常: {e}")
            return None
        except Exception as e:
            logger.error(f"止损单创建响应异常: {e}")
            return None

        # ACK 或超时都不直接下结论，只按同一个 algoClOrdId 查询确认；绝不二次 POST。
        for attempt in range(self.STOP_CONFIRM_ATTEMPTS):
            try:
                confirmed = self._find_stop_by_client_id(
                    self._fetch_algo_orders(ccxt_symbol), algo_client_id,
                    stop_side, stop_price, contracts)
                if confirmed:
                    logger.info(
                        f"止损单创建已确认: {ccxt_symbol} {stop_side} {contracts}张 "
                        f"@ {stop_price}, algoClOrdId={algo_client_id}, "
                        f"订单ID={confirmed.get('id')}")
                    return confirmed
            except Exception as e:
                logger.warning(
                    f"止损单第{attempt + 1}次确认失败（不会重发）: {e}")
            if attempt < self.STOP_CONFIRM_ATTEMPTS - 1:
                time.sleep(self.STOP_CONFIRM_DELAY)

        logger.error(
            f"止损单 POST 后无法按 algoClOrdId 确认，拒绝重发: "
            f"{ccxt_symbol} @ {stop_price}, algoClOrdId={algo_client_id}")
        return None

    @retry_on_network_error(max_retries=3)
    def list_position_symbols(self):
        """列出 U 本位永续真实持仓；币本位合约不属于本系统边界。

        孤儿仓核对依赖本清单完整：响应 None/非列表、张数字段不可解析或
        contracts 缺失但原始 pos 非零，都必须抛出——静默跳过等于让守护
        程序漏检真钱仓位。
        """
        positions = self.exchange.fetch_positions()
        if positions is None or not isinstance(positions, (list, tuple)):
            raise PositionModeError(
                f"持仓清单查询返回 {type(positions).__name__}，不确定不得当无持仓")
        symbols = []
        for p in positions:
            if p is None:
                continue
            if not isinstance(p, dict):
                raise PositionModeError(
                    f"持仓清单条目结构异常: {type(p).__name__}")
            info = p.get('info') or {}
            ccxt_symbol = p.get('symbol') or info.get('instId') or ''
            if not ccxt_symbol:
                raise PositionModeError(
                    f"持仓清单条目缺少品种标识，拒绝裁决: {str(p)[:120]}")
            contracts = self._parse_signed_size(
                p.get('contracts'), 'contracts', ccxt_symbol)
            raw_pos = self._parse_signed_size(info.get('pos'), 'pos', ccxt_symbol)
            if contracts is None:
                if raw_pos is not None and raw_pos != 0:
                    raise PositionModeError(
                        f"{ccxt_symbol} contracts 缺失但原始 pos={info.get('pos')!r} "
                        "非零，孤儿仓核对拒绝跳过")
                continue
            # BTC/USD:BTC 若被 to_internal_symbol 会错映成 BTCUSDT，导致把
            # 人工币本位仓误报/漏报为本系统的 U 本位孤儿仓。
            if abs(contracts) > 0 and str(p.get('symbol') or '').endswith(':USDT'):
                symbols.append(self.to_internal_symbol(p['symbol']))
        return symbols

    def find_stop_order_state(self, symbol, side, amount, stop_price, stop_order_id=None):
        """检查与「本地持仓记录」对应的止损算法单状态（供主层止损自愈巡检使用）。

        amount 为币数，张数换算在本方法内部完成（张数不外泄）。返回：
          'intact'   — 存在方向+触发价+张数与本地记录严格一致的算法单（保护完整）；
          'mismatch' — 本地记录的 stop_order_id 还在列表里，但内容与本地记录不符
                       或出现多张/内容歧义（自动补挂会造成双止损，须人工核对）；
          'missing'  — 列表中不存在匹配的止损单（需要补挂）。
          {'state': 'adoptable', 'order_id': ...} — 原 ID 不在，但交易所仅有一张
                       内容完全匹配的新保护单；调用方应原子收养 ID，绝不补挂。
        查询/换算失败向上抛出，调用方按 fail-safe 跳过本轮。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        stop_side = 'sell' if side == 'long' else 'buy'
        contracts = self._coin_to_contracts(ccxt_symbol, amount)
        # 本地记录价与交易所存储价须同一口径：交易所按 tick 取整存储，
        # 比对前用同一对齐函数归一本地价（详见 _align_stop_price）
        stop_price = self._align_stop_price(ccxt_symbol, stop_price)
        algos = self._fetch_algo_orders(ccxt_symbol)
        # reduceOnly 未知的算法单必须可见：它可能就是真实保护单（字段暂缺），
        # 计入候选会把裁决推向 mismatch（fail-safe），绝不会推向补挂双止损。
        reduce_only_algos = [
            order for order in algos
            if self._order_reduce_only(order) or self._reduce_only_unknown(order)]
        protective = [
            order for order in reduce_only_algos
            if self._is_protective_stop_candidate(order, stop_side)]
        expected = None
        if stop_order_id:
            expected = next((
                order for order in algos
                if str(order.get('id')) == str(stop_order_id)), None)
        if expected is not None:
            if (len(protective) == 1 and len(reduce_only_algos) == 1 and
                    self._algo_order_matches(
                        expected, stop_side, stop_price, contracts,
                        expected_order_id=stop_order_id)):
                return 'intact'
            # 记录 ID 内容错误，或同品种同时还有其它保护止损：都属歧义。
            return 'mismatch'

        content_matches = [
            order for order in protective
            if self._algo_order_matches(
                order, stop_side, stop_price, contracts)]
        if (len(content_matches) == 1 and len(protective) == 1 and
                len(reduce_only_algos) == 1):
            # 原 ID 已不可见，但唯一一张新单完整覆盖当前仓位：安全收养其 ID，
            # 不得把它当 missing 再补挂第二张。
            return {'state': 'adoptable', 'order_id': content_matches[0].get('id')}
        if reduce_only_algos:
            # 一张内容错误、多张保护候选，或另一方向/类型的 reduce-only
            # 算法单都不能自动裁决/补挂。
            return 'mismatch'
        return 'missing'

    # ===================== 撤单（含算法单） =====================

    # 待触发算法单查询覆盖 OKX 该端点的 ordType 全集。系统自建止损恒为
    # conditional，但人工 reduce-only iceberg/twap 同样可能在未来新仓上执行，
    # 不能因“不像止损”就在 stale-order 清单里隐身。
    ALGO_ORDER_TYPES = (
        'conditional', 'oco', 'trigger', 'move_order_stop', 'iceberg', 'twap',
        'chase')
    ALGO_PAGE_LIMIT = 100
    ALGO_MAX_PAGES = 100
    ALGO_CANCEL_BATCH_LIMIT = 10
    NORMAL_PAGE_LIMIT = 100
    NORMAL_MAX_PAGES = 100
    NORMAL_CANCEL_BATCH_LIMIT = 20
    CANCEL_ALL_VERIFY_ROUNDS = 5
    CANCEL_ALL_EMPTY_CONFIRMATIONS = 2

    @staticmethod
    def _to_inst_id(ccxt_symbol):
        """BTC/USDT:USDT -> BTC-USDT-SWAP（OKX U 本位永续 instId 命名规则）。

        确定性字符串变换，不依赖 load_markets 缓存是否加载成功——本适配器只服务
        U 本位永续，ccxt 符号与 OKX instId 本就按这同一条规则互相推导。
        """
        return f"{ccxt_symbol.split('/')[0]}-USDT-SWAP"

    @retry_on_network_error(max_retries=3)
    def _fetch_algo_pending_raw(self, inst_id, ord_type):
        """单一 ordType 的原生待触发算法单查询（带网络重试）。

        响应信封（code=='0' 且 data 为数组）由交易所自证请求已被正确理解——
        这是对「成功但答非所问」的结构性防护；信封异常一律抛出（fail-loud）。
        OKX 单页最多 100 条；必须沿 after 游标读到短页，才能把“查不到”当结论。
        游标缺失/不前进或超过安全页数都抛出，绝不返回可能截断的清单。
        """
        records = []
        seen_ids = set()
        seen_cursors = set()
        after = None
        for _page in range(self.ALGO_MAX_PAGES):
            params = {
                'ordType': ord_type, 'instId': inst_id,
                'limit': str(self.ALGO_PAGE_LIMIT),
            }
            if after is not None:
                params['after'] = after
            resp = self.exchange.privateGetTradeOrdersAlgoPending(params)
            if (not isinstance(resp, dict) or resp.get('code') != '0' or
                    not isinstance(resp.get('data'), list)):
                raise RuntimeError(
                    f'算法单查询响应异常(ordType={ord_type}): {str(resp)[:200]}')
            page = resp['data']
            page_ids = []
            for item in page:
                if not isinstance(item, dict) or not item.get('algoId'):
                    raise RuntimeError(
                        f'算法单分页项缺少 algoId(ordType={ord_type})')
                algo_id = str(item['algoId'])
                page_ids.append(algo_id)
                if algo_id in seen_ids:
                    # OKX 分页语义：after 返回边界 ID 之前的记录，同一 ID 不应
                    # 再次出现。重复 ID 说明分页异常，静默去重会把可能截断/
                    # 错乱的清单宣布为完整快照。
                    raise RuntimeError(
                        f'算法单分页重复 ID(ordType={ord_type}): {algo_id}')
                seen_ids.add(algo_id)
                records.append(item)
            if len(page) < self.ALGO_PAGE_LIMIT:
                return records
            next_after = page_ids[-1]
            if next_after == after or next_after in seen_cursors:
                raise RuntimeError(
                    f'算法单分页游标未前进(ordType={ord_type}, after={next_after})')
            seen_cursors.add(next_after)
            after = next_after
        raise RuntimeError(
            f'算法单分页超过 {self.ALGO_MAX_PAGES} 页(ordType={ord_type})')

    def _fetch_algo_orders(self, ccxt_symbol):
        """查询未触发的算法/条件单——直调 OKX 原生 orders-algo-pending 端点。

        历史实现经 ccxt fetch_open_orders 的三种参数组合猜谜并合并：某组合可能因
        统一接口跨版本参数映射漂移「成功但答非所问返回空」，合并清单不完整会让
        验证式撤单误判「已撤干净」（历轮审查的保留观察项）。原生端点是该数据的
        唯一权威来源：问题只有一种问法，不存在映射漂移。任一 ordType 查询失败
        （重试后）即整体抛出——绝不基于可能不完整的清单下「不存在」的结论
        （调用方对异常一律 fail-safe：跳过本轮 / 标记残留 / 阻断开仓）。
        返回结构与 _algo_order_matches / 调用方约定一致：id/side/reduceOnly + 原生 info。
        """
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        inst_id = self._to_inst_id(ccxt_symbol)
        orders = []
        for ord_type in self.ALGO_ORDER_TYPES:
            for item in self._fetch_algo_pending_raw(inst_id, ord_type):
                raw_reduce_only = item.get('reduceOnly')
                orders.append({
                    'id': str(item.get('algoId') or ''),
                    'clientOrderId': str(item.get('algoClOrdId') or ''),
                    'side': item.get('side'),
                    # 字段缺失保留 None（未知）：压成 False 会让该单在
                    # 四态裁决的 reduce-only 清单里隐身，missing 误判触发双止损。
                    'reduceOnly': (raw_reduce_only in (True, 'true')
                                   if raw_reduce_only is not None else None),
                    'info': item,
                })
        return orders

    @retry_on_network_error(max_retries=3)
    def _fetch_normal_pending_raw(self, inst_id):
        """读完某品种全部未完成普通订单；不完整分页绝不当空清单。"""
        records = []
        seen_ids = set()
        seen_cursors = set()
        after = None
        for _page in range(self.NORMAL_MAX_PAGES):
            params = {
                'instId': inst_id, 'limit': str(self.NORMAL_PAGE_LIMIT),
            }
            if after is not None:
                params['after'] = after
            resp = self.exchange.privateGetTradeOrdersPending(params)
            if (not isinstance(resp, dict) or resp.get('code') != '0' or
                    not isinstance(resp.get('data'), list)):
                raise RuntimeError(
                    f'普通挂单查询响应异常: {str(resp)[:200]}')
            page = resp['data']
            page_ids = []
            for item in page:
                if not isinstance(item, dict) or not item.get('ordId'):
                    raise RuntimeError('普通挂单分页项缺少 ordId')
                order_id = str(item['ordId'])
                page_ids.append(order_id)
                if order_id in seen_ids:
                    # 同算法单分页：边界 ID 不应重复出现，重复即分页异常。
                    raise RuntimeError(f'普通挂单分页重复 ID: {order_id}')
                seen_ids.add(order_id)
                records.append(item)
            if len(page) < self.NORMAL_PAGE_LIMIT:
                return records
            next_after = page_ids[-1]
            if next_after == after or next_after in seen_cursors:
                raise RuntimeError(
                    f'普通挂单分页游标未前进(after={next_after})')
            seen_cursors.add(next_after)
            after = next_after
        raise RuntimeError(
            f'普通挂单分页超过 {self.NORMAL_MAX_PAGES} 页')

    def _fetch_normal_orders(self, ccxt_symbol):
        """OKX ``orders-pending`` 权威清单（live + partially_filled）。"""
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        records = self._fetch_normal_pending_raw(self._to_inst_id(ccxt_symbol))
        return [{
            'id': str(item['ordId']),
            'clientOrderId': str(item.get('clOrdId') or ''),
            'side': item.get('side'),
            'reduceOnly': (item.get('reduceOnly') in (True, 'true')
                           if item.get('reduceOnly') is not None else None),
            'info': item,
        } for item in records]

    @staticmethod
    def _merge_orders_by_id(*groups):
        merged = []
        seen = set()
        for group in groups:
            for order in group or []:
                order_id = str(order.get('id') or '')
                if not order_id:
                    raise RuntimeError('挂单快照项缺少 ID')
                if order_id not in seen:
                    seen.add(order_id)
                    merged.append(order)
        return merged

    def _fetch_pending_snapshot(self, ccxt_symbol):
        """两类非原子清单的边界复读快照。

        OKX 没有“普通+算法单”单一原子端点。按 normal→algo→normal
        复读普通单并取并集：只有三次都空才返回空。algo 端点一次
        完整快照已需按类型请求 7 次，由外层“连续空轮”复读，避免超过
        OKX 20 次/2s 的算法单查询限额。这不替代账户单写者约束。
        """
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        normal_first = self._fetch_normal_orders(ccxt_symbol)
        algo_first = self._fetch_algo_orders(ccxt_symbol)
        normal_second = self._fetch_normal_orders(ccxt_symbol)
        return (
            self._merge_orders_by_id(normal_first, normal_second),
            algo_first,
        )

    def _normal_order_absent(self, ccxt_symbol, order_id):
        return all(
            str(order.get('id')) != str(order_id)
            for order in self._fetch_normal_orders(ccxt_symbol))

    @retry_on_network_error(max_retries=3)
    def _fetch_normal_order_raw(self, ccxt_symbol, order_id):
        """查普通订单终态；未找到返回 None，异常信封拒绝裁决。"""
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        try:
            resp = self.exchange.privateGetTradeOrder({
                'instId': self._to_inst_id(ccxt_symbol),
                'ordId': str(order_id),
            })
        except ccxt.OrderNotFound:
            return None
        if (isinstance(resp, dict) and str(resp.get('code')) in
                {'51603', '51604'}):
            return None
        if (not isinstance(resp, dict) or resp.get('code') != '0' or
                not isinstance(resp.get('data'), list) or
                len(resp['data']) != 1 or not isinstance(resp['data'][0], dict)):
            raise RuntimeError(
                f'普通订单终态响应异常: {str(resp)[:200]}')
        item = resp['data'][0]
        if str(item.get('ordId') or '') != str(order_id):
            raise RuntimeError(
                f'普通订单终态 ID 不匹配: expected={order_id}, '
                f"actual={item.get('ordId')}")
        return item

    @staticmethod
    def _normal_order_safely_cancelled(order):
        """只有非成交终态且累计成交为零，才能证明“撤单未改变仓位”。

        accFillSz 缺失/空串是「不知道成交了多少」，不是「明确零成交」；
        压成 0 会把可能已部分成交的撤单误报为未动仓位。
        """
        if not isinstance(order, dict):
            return False
        if str(order.get('state') or '').lower() not in {
                'canceled', 'cancelled', 'mmp_canceled', 'rejected', 'expired'}:
            return False
        raw_filled = order.get('accFillSz')
        if raw_filled is None or raw_filled == '' or isinstance(raw_filled, bool):
            return False
        try:
            filled = float(raw_filled)
        except (TypeError, ValueError):
            return False
        return math.isfinite(filled) and filled == 0

    def _request_cancel_normal_order(self, ccxt_symbol, order_id):
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        try:
            self.exchange.privatePostTradeCancelOrder({
                'instId': self._to_inst_id(ccxt_symbol),
                'ordId': str(order_id),
            })
        except Exception as exc:
            logger.warning(
                f'撤销普通单指令异常（成败以终态裁决）: {order_id}: {exc}')

    def _request_cancel_normal_orders(self, ccxt_symbol, order_ids):
        """批量发撤单指令（每批最多 20）；ACK 不作结论。"""
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        unique_ids = list(dict.fromkeys(
            str(order_id) for order_id in order_ids if order_id))
        inst_id = self._to_inst_id(ccxt_symbol)
        for start in range(0, len(unique_ids), self.NORMAL_CANCEL_BATCH_LIMIT):
            batch = [
                {'instId': inst_id, 'ordId': order_id}
                for order_id in unique_ids[
                    start:start + self.NORMAL_CANCEL_BATCH_LIMIT]
            ]
            try:
                self.exchange.privatePostTradeCancelBatchOrders(batch)
            except Exception as exc:
                logger.warning(
                    f'批量撤普通单指令异常（继续以终态裁决）: {exc}')

    def _cancel_normal_order(self, ccxt_symbol, order_id):
        """单张普通撤单：pending 消失还不够，必须证明终态为零成交撤销。"""
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        self._request_cancel_normal_order(ccxt_symbol, order_id)
        try:
            for attempt in range(2):
                if self._normal_order_absent(ccxt_symbol, order_id):
                    terminal = self._fetch_normal_order_raw(
                        ccxt_symbol, order_id)
                    if self._normal_order_safely_cancelled(terminal):
                        return True
                    if terminal and str(terminal.get('state')).lower() == 'filled':
                        logger.critical(
                            f'普通单 {order_id} 在撤单竞态中已成交，不得报撤单成功')
                if attempt == 0:
                    time.sleep(self.CANCEL_VERIFY_RECHECK_DELAY)
            return False
        except Exception as exc:
            logger.warning(
                f'撤销后查询普通单终态失败，无法确认 {order_id} 已撤: {exc}')
            return False

    def _algo_order_absent(self, ccxt_symbol, order_id):
        """查询算法单列表，确认目标 id 已不存在。查询失败时向上抛出（不可确认 ≠ 已撤干净）。"""
        for o in self._fetch_algo_orders(ccxt_symbol):
            if str(o.get('id')) == str(order_id):
                return False
        return True

    def _cancel_algo_order(self, ccxt_symbol, order_id):
        """撤销算法单，并以「列表里查不到该 id」为成功标准。

        撤销指令直调 OKX 原生 cancel-algos 端点（与查询同一权威接口族，消除
        ccxt 统一撤单参数映射漂移的最后一处依赖）。指令自身的返回不构成任何
        结论——成败一律以原生查询清单裁决：首次复核仍在列表时，等待片刻复查
        一次再裁决（交易所列表可能滞后于撤单生效）。
        """
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        try:
            self.exchange.privatePostTradeCancelAlgos(
                [{'algoId': str(order_id), 'instId': self._to_inst_id(ccxt_symbol)}])
        except Exception as e:
            logger.warning(f"撤销算法单指令异常（成败以清单裁决，不据此下结论）: {order_id}: {e}")
        try:
            if self._algo_order_absent(ccxt_symbol, order_id):
                return True
            time.sleep(self.CANCEL_VERIFY_RECHECK_DELAY)
            return self._algo_order_absent(ccxt_symbol, order_id)
        except Exception as e:
            logger.warning(f"撤销后查询算法单失败，无法确认 {order_id} 已撤: {e}")
            return False

    def _request_cancel_algo_orders(self, ccxt_symbol, order_ids):
        """批量发算法单撤销指令（每批最多 10）；由外层完整清单复验。"""
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        unique_ids = list(dict.fromkeys(
            str(order_id) for order_id in order_ids if order_id))
        inst_id = self._to_inst_id(ccxt_symbol)
        for start in range(0, len(unique_ids), self.ALGO_CANCEL_BATCH_LIMIT):
            batch = [
                {'algoId': order_id, 'instId': inst_id}
                for order_id in unique_ids[
                    start:start + self.ALGO_CANCEL_BATCH_LIMIT]
            ]
            try:
                self.exchange.privatePostTradeCancelAlgos(batch)
            except Exception as exc:
                logger.warning(
                    f'批量撤算法单指令异常（继续以清单裁决）: {exc}')

    def cancel_order(self, symbol, order_id):
        """撤销未知类型订单；普通单还必须证明不是在撤单竞态中成交。"""
        ccxt_symbol = self._resolve_symbol(symbol)
        try:
            # 单 ID 撤单不先扫两轮全部算法类型：否则 7 类预查+撤后
            # 复验会超过 OKX 20 次/2s 限额。普通单可按 ID 查终态，算法单
            # 直接发验证式撤单，最后各自再查一次。
            normal_seen = any(
                str(order.get('id')) == str(order_id)
                for order in self._fetch_normal_orders(ccxt_symbol))
            normal_detail = self._fetch_normal_order_raw(
                ccxt_symbol, order_id)
            if normal_seen or (normal_detail and str(
                    normal_detail.get('state')).lower() in {'live', 'partially_filled'}):
                normal_ok = self._cancel_normal_order(ccxt_symbol, order_id)
            elif normal_detail is None:
                normal_ok = True
            else:
                normal_ok = self._normal_order_safely_cancelled(normal_detail)

            algo_ok = self._cancel_algo_order(ccxt_symbol, order_id)
            normal_ok = normal_ok and self._normal_order_absent(
                ccxt_symbol, order_id)
            algo_ok = self._algo_order_absent(ccxt_symbol, order_id)
        except Exception as exc:
            logger.warning(f'按 ID 验证式撤单异常: {order_id}: {exc}')
            return False
        if algo_ok and normal_ok:
            logger.info(
                f'撤单成功(两类清单已验净): {ccxt_symbol} 订单ID={order_id}')
            return True
        logger.error(
            f'{ccxt_symbol} 订单 {order_id} 撤销不可确认: '
            f'algo_ok={algo_ok}, normal_ok={normal_ok}')
        return False

    def cancel_stop_order_only(self, symbol, order_id):
        """持仓仍开着时只撤指定算法止损，失败绝不退化为 cancel-all。

        make-before-break 已先挂好新保护；全撤会把新单一起删掉并造成裸仓。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        if not order_id:
            return False
        return bool(self._cancel_algo_order(ccxt_symbol, order_id))

    @retry_on_network_error(max_retries=3)
    def cancel_all_orders(self, symbol):
        """安全清理某交易对挂单：连续空清单 + 普通单零成交撤销 + 空仓。"""
        ccxt_symbol = self._resolve_symbol(symbol)
        try:
            # 先拍快照再发统一撤全，否则已成交消失的 ID 将无法做终态审计。
            normal, algos = self._fetch_pending_snapshot(ccxt_symbol)
            seen_normal_ids = {
                str(order.get('id')) for order in normal if order.get('id')}
            try:
                self.exchange.cancel_all_orders(ccxt_symbol)
            except Exception as exc:
                logger.warning(
                    f'{ccxt_symbol} 统一撤普通挂单指令异常，'
                    f'继续按原生清单裁决: {exc}')
            self._request_cancel_normal_orders(ccxt_symbol, seen_normal_ids)
            self._request_cancel_algo_orders(
                ccxt_symbol, [order.get('id') for order in algos])

            # 超时 POST 的普通/算法单都可能延迟浮现。一次空快照不是
            # 不存在证明；只有两类清单同时连续为空才能释放残留标记。
            consecutive_empty = 0
            for verify_round in range(self.CANCEL_ALL_VERIFY_ROUNDS):
                remaining_normal, remaining_algos = (
                    self._fetch_pending_snapshot(ccxt_symbol))
                if remaining_normal or remaining_algos:
                    consecutive_empty = 0
                    new_normal_ids = {
                        str(order.get('id')) for order in remaining_normal
                        if order.get('id')}
                    seen_normal_ids.update(new_normal_ids)
                    self._request_cancel_normal_orders(
                        ccxt_symbol, new_normal_ids)
                    self._request_cancel_algo_orders(
                        ccxt_symbol,
                        [order.get('id') for order in remaining_algos])
                else:
                    consecutive_empty += 1
                    if consecutive_empty >= self.CANCEL_ALL_EMPTY_CONFIRMATIONS:
                        break
                if verify_round < self.CANCEL_ALL_VERIFY_ROUNDS - 1:
                    time.sleep(self.CANCEL_VERIFY_RECHECK_DELAY)
            if consecutive_empty < self.CANCEL_ALL_EMPTY_CONFIRMATIONS:
                logger.error(
                    f'{ccxt_symbol} 未获得连续 '
                    f'{self.CANCEL_ALL_EMPTY_CONFIRMATIONS} 次普通+算法单空清单确认')
                return False
            for normal_id in sorted(seen_normal_ids):
                terminal = self._fetch_normal_order_raw(
                    ccxt_symbol, normal_id)
                if not self._normal_order_safely_cancelled(terminal):
                    logger.critical(
                        f'{ccxt_symbol} 普通单 {normal_id} 未证明为零成交撤销，'
                        '可能在撤单竞态中改变仓位')
                    return False
            try:
                if self.get_position(ccxt_symbol) is not None:
                    logger.critical(
                        f'{ccxt_symbol} 撤挂单期间持仓发生变化，拒绝报清理成功')
                    return False
            except Exception as exc:
                logger.error(
                    f'{ccxt_symbol} 撤挂单后持仓无法复核: {exc}')
                return False
            return True
        except Exception as exc:
            logger.error(
                f'{ccxt_symbol} 查询/撤销完整挂单清单失败，按残留处理: {exc}')
            return False
