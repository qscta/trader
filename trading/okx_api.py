import ccxt
import hashlib
import logging
import math
import time
import uuid
from datetime import datetime, timezone
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
    STOP_FILL_RECOVERY_ATTEMPTS = 3
    STOP_FILL_RECOVERY_DELAY = 0.25

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

    @retry_on_network_error(max_retries=3)
    def get_position(self, symbol):
        """获取特定交易对的持仓（单向模式下只有一条）。

        无实仓时返回 None。OKX 可能返回 contracts=None/0 的空仓条目，
        若原样外泄，上层（币安时代写下的）`contracts == 0` / `contracts > 0`
        判断会因 None 误判甚至 TypeError——统一在适配层归一化掉。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        positions = self.exchange.fetch_positions([ccxt_symbol])
        nonzero = []
        for p in positions or []:
            if not p or p.get('contracts') is None:
                continue
            try:
                contracts = abs(float(p['contracts']))
            except (TypeError, ValueError) as e:
                raise PositionModeError(f"{ccxt_symbol} 持仓张数字段异常: {p.get('contracts')!r}") from e
            if contracts <= 0:
                continue
            info = p.get('info') or {}
            if p.get('hedged') is True or info.get('posSide') in ('long', 'short'):
                raise PositionModeError(f"{ccxt_symbol} 检测到双向持仓腿(posSide={info.get('posSide')})，拒绝裁剪为单腿")
            if self._position_side(p) not in ('long', 'short'):
                raise PositionModeError(f"{ccxt_symbol} 非零持仓方向不可判定，拒绝继续交易")
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

    def _fetch_order_for_confirmation(self, ccxt_symbol, order_id, client_order_id):
        """按交易所订单 id；ACK 丢失时按预先生成的 clOrdId 查询。"""
        if order_id:
            return self.exchange.fetch_order(str(order_id), ccxt_symbol)
        return self.exchange.fetch_order(
            client_order_id, ccxt_symbol, params={'clOrdId': client_order_id})

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

    def _collect_existing_close_legs(
            self, ccxt_symbol, close_side, requested_contracts,
            base_client_order_id):
        """只读找回基础腿及 r1/r2，全部终态后返回真实逐腿成交。

        close intent 只需持久化基础 clOrdId；后续腿 ID 由固定后缀派生。
        恢复若只看首腿，会把 r1/r2 的仓位变化误算到首腿并丢掉 VWAP/fee。
        """
        tolerance = self._contracts_tolerance(ccxt_symbol)
        terminal_statuses = {
            'closed', 'filled', 'canceled', 'cancelled', 'rejected', 'expired',
            'mmp_canceled', 'mmp_cancelled'}
        last_problem = None
        for attempt in range(self.ORDER_CONFIRM_ATTEMPTS):
            found = {}
            known_filled = 0.0
            for leg_index in range(self.MAX_CLOSE_LEGS):
                leg_client_id = self._close_leg_client_order_id(
                    base_client_order_id, leg_index)
                try:
                    candidate = self._fetch_order_for_confirmation(
                        ccxt_symbol, None, leg_client_id)
                except ccxt.OrderNotFound:
                    candidate = None
                if not isinstance(candidate, dict) or not (
                        candidate.get('id') or candidate.get('status')):
                    # 后续腿只可能在前一腿已经存在之后创建；首个缺口之后
                    # 不再查询更后缀，既减少限频压力也避免把异常响应拼成腿。
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
                preliminary_status = str(
                    candidate.get('status') or info.get('state') or '').lower()
                preliminary_filled = self._finite_nonnegative(
                    candidate.get('filled'))
                preliminary_remaining = self._finite_nonnegative(
                    candidate.get('remaining'))
                if preliminary_filled is None and preliminary_remaining is not None:
                    preliminary_filled = max(
                        0.0, observed_amount - preliminary_remaining)
                if (preliminary_filled is None and
                        preliminary_status in {'closed', 'filled'} and
                        (preliminary_remaining is None or
                         preliminary_remaining <= tolerance)):
                    preliminary_filled = observed_amount
                if (preliminary_status not in terminal_statuses or
                        preliminary_filled is None):
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
                info = order.get('info') or {}
                status = str(
                    order.get('status') or info.get('state') or '').lower()
                terminal = status in terminal_statuses
                filled = self._finite_nonnegative(order.get('filled'))
                remaining = self._finite_nonnegative(order.get('remaining'))
                if filled is None and remaining is not None:
                    filled = max(0.0, amount - remaining)
                if (filled is None and status in {'closed', 'filled'} and
                        (remaining is None or remaining <= tolerance)):
                    filled = amount
                if (not terminal or filled is None or
                        filled > amount + tolerance):
                    all_terminal = False
                    last_problem = (
                        f'{leg_client_id}: status={status}, filled={filled}, '
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
        observed_reduce_only = order.get('reduceOnly')
        if observed_reduce_only is None:
            observed_reduce_only = info.get('reduceOnly')
        observed_reduce_only = observed_reduce_only in (True, 'true')
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
        try:
            order = self._fetch_order_for_confirmation(
                ccxt_symbol, None, client_order_id)
        except ccxt.OrderNotFound:
            return None
        if not isinstance(order, dict) or not (order.get('id') or order.get('status')):
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
        if supplied_client_id:
            try:
                existing = self._fetch_order_for_confirmation(
                    ccxt_symbol, None, client_order_id)
            except ccxt.OrderNotFound:
                existing = None
            except Exception as e:
                logger.error(f"{ccxt_symbol} 幂等开仓订单查询失败，拒绝新下单: {e}")
                return None
            if isinstance(existing, dict) and (existing.get('id') or existing.get('status')):
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

        # 聚合多腿真实执行：币数、VWAP、cost、fee/fees 都不丢失。
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

    @staticmethod
    def _algo_order_matches(order, stop_side, stop_price, contracts, expected_order_id=None):
        """严格判断算法单是否为本地记录的保护性止损。

        必须同时满足：记录 ID（若有）、conditional 类型、reduceOnly、触发后市价
        (slOrdPx=-1)、方向、触发价、张数。任何字段读不到一律视为不匹配。
        """
        if not order or order.get('side') != stop_side:
            return False
        if expected_order_id is not None and str(order.get('id')) != str(expected_order_id):
            return False
        info = order.get('info') or {}
        reduce_only = order.get('reduceOnly')
        if reduce_only is None:
            reduce_only = info.get('reduceOnly')
        if reduce_only not in (True, 'true'):
            return False
        if info.get('ordType') != 'conditional':
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
        reduce_only = order.get('reduceOnly')
        if reduce_only is None:
            reduce_only = info.get('reduceOnly')
        return (
            reduce_only in (True, 'true') and
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
        """列出 U 本位永续真实持仓；币本位合约不属于本系统边界。"""
        symbols = []
        for p in self.exchange.fetch_positions() or []:
            if not p or not p.get('contracts'):
                continue
            ccxt_symbol = p.get('symbol') or ''
            # BTC/USD:BTC 若被 to_internal_symbol 会错映成 BTCUSDT，导致把
            # 人工币本位仓误报/漏报为本系统的 U 本位孤儿仓。
            if abs(float(p['contracts'])) > 0 and ccxt_symbol.endswith(':USDT'):
                symbols.append(self.to_internal_symbol(ccxt_symbol))
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
        reduce_only_algos = []
        for order in algos:
            info = order.get('info') or {}
            reduce_only = order.get('reduceOnly')
            if reduce_only is None:
                reduce_only = info.get('reduceOnly')
            if reduce_only in (True, 'true'):
                reduce_only_algos.append(order)
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
                if algo_id not in seen_ids:
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
                orders.append({
                    'id': str(item.get('algoId') or ''),
                    'clientOrderId': str(item.get('algoClOrdId') or ''),
                    'side': item.get('side'),
                    'reduceOnly': item.get('reduceOnly') in (True, 'true'),
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
                if order_id not in seen_ids:
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
            'reduceOnly': item.get('reduceOnly') in (True, 'true'),
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

    @retry_on_network_error(max_retries=3)
    def _fetch_algo_order_raw(self, algo_order_id):
        """按 algoId 查算法单详情；未找到返回 None，异常信封拒绝裁决。"""
        try:
            resp = self.exchange.privateGetTradeOrderAlgo({
                'algoId': str(algo_order_id),
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
                f'算法订单详情响应异常: {str(resp)[:200]}')
        item = resp['data'][0]
        if str(item.get('algoId') or '') != str(algo_order_id):
            raise RuntimeError(
                f'算法订单详情 ID 不匹配: expected={algo_order_id}, '
                f"actual={item.get('algoId')}")
        return item

    @staticmethod
    def _exchange_timestamp_iso(value):
        """OKX 毫秒时间戳 -> UTC ISO；缺失/非法时不伪造时间。"""
        if value in (None, '') or isinstance(value, bool):
            return None
        try:
            milliseconds = int(value)
            if milliseconds <= 0:
                return None
            return datetime.fromtimestamp(
                milliseconds / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, TypeError, ValueError):
            return None

    @staticmethod
    def _algo_child_order_ids(algo_order):
        """严格提取算法单关联的普通子订单 ID（兼容已废弃 ordId 字段）。"""
        values = algo_order.get('ordIdList')
        if values is None:
            values = []
        if not isinstance(values, list):
            raise RuntimeError('算法订单 ordIdList 不是数组')
        order_ids = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise RuntimeError('算法订单 ordIdList 含非法订单 ID')
            order_id = str(value)
            if not order_id:
                raise RuntimeError('算法订单 ordIdList 含空订单 ID')
            if order_id not in order_ids:
                order_ids.append(order_id)
        deprecated_id = algo_order.get('ordId')
        if deprecated_id not in (None, ''):
            if isinstance(deprecated_id, bool) or not isinstance(
                    deprecated_id, (str, int)):
                raise RuntimeError('算法订单 ordId 非法')
            deprecated_id = str(deprecated_id)
            if order_ids and deprecated_id not in order_ids:
                raise RuntimeError('算法订单 ordId 与 ordIdList 不一致')
            if not order_ids:
                order_ids.append(deprecated_id)
        return order_ids

    def recover_stop_fill_evidence(
            self, symbol, position_side, amount, stop_order_ids):
        """回查已触发保护止损的真实成交证据；无已触发止损返回 ``None``。

        只有算法单能严格证明为该仓位的 ``SL``，且其全部普通子订单均为同品种、
        同平仓方向、reduce-only、完整成交，累计张数又与账本剩余仓位精确一致时，
        才返回真实 VWAP。任一字段缺失/矛盾都会抛出，由上层降级到明确标记的
        保守估值；本方法只读，不会下单或撤单。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        if position_side not in ('long', 'short'):
            raise ValueError('position_side 必须是 long/short')
        if not isinstance(stop_order_ids, (list, tuple)):
            raise ValueError('stop_order_ids 必须是数组')
        unique_algo_ids = []
        for value in stop_order_ids:
            if value in (None, ''):
                continue
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError('stop_order_ids 含非法 ID')
            value = str(value)
            if value not in unique_algo_ids:
                unique_algo_ids.append(value)
        if not unique_algo_ids:
            return None

        expected_contracts = self._coin_to_contracts(ccxt_symbol, amount)
        tolerance = self._contracts_tolerance(ccxt_symbol)
        if expected_contracts <= tolerance:
            raise RuntimeError(
                f'{ccxt_symbol} 账本仓位不足一个可验证张数，拒绝归因止损成交')
        expected_inst_id = self._to_inst_id(ccxt_symbol)
        expected_side = 'sell' if position_side == 'long' else 'buy'

        effective = []
        for attempt in range(self.STOP_FILL_RECOVERY_ATTEMPTS):
            effective = []
            for algo_id in unique_algo_ids:
                detail = self._fetch_algo_order_raw(algo_id)
                if detail is None:
                    continue
                state = str(detail.get('state') or '').lower()
                actual_side = str(detail.get('actualSide') or '').lower()
                if state == 'effective' and actual_side == 'sl':
                    effective.append((algo_id, detail))
            if effective:
                break
            if attempt < self.STOP_FILL_RECOVERY_ATTEMPTS - 1:
                time.sleep(self.STOP_FILL_RECOVERY_DELAY)
        if not effective:
            return None
        if len(effective) != 1:
            raise RuntimeError(
                f'{ccxt_symbol} 有 {len(effective)} 张止损算法单声称已触发，'
                '成交归因不唯一')

        algo_id, algo = effective[0]
        if (algo.get('instId') != expected_inst_id or
                algo.get('ordType') != 'conditional' or
                algo.get('side') != expected_side or
                algo.get('reduceOnly') not in (True, 'true') or
                algo.get('posSide') not in (None, '', 'net')):
            raise RuntimeError(
                f'{ccxt_symbol} 已触发算法单内容与保护止损不一致: algoId={algo_id}')
        algo_size = self._finite_nonnegative(algo.get('sz'))
        if (algo_size is None or algo_size <= tolerance or
                abs(algo_size - expected_contracts) > tolerance):
            raise RuntimeError(
                f'{ccxt_symbol} 已触发算法单张数与账本仓位不一致: '
                f'algo={algo_size}, expected={expected_contracts}')

        child_ids = self._algo_child_order_ids(algo)
        if not child_ids:
            raise RuntimeError(
                f'{ccxt_symbol} 已触发止损没有关联普通子订单: algoId={algo_id}')

        total_contracts = 0.0
        total_notional = 0.0
        total_fee = 0.0
        fee_exact = True
        fill_times = []
        for child_id in child_ids:
            child = None
            last_child_state = 'not-visible'
            for attempt in range(self.STOP_FILL_RECOVERY_ATTEMPTS):
                candidate = self._fetch_normal_order_raw(
                    ccxt_symbol, child_id)
                if candidate is not None:
                    if (candidate.get('instId') != expected_inst_id or
                            candidate.get('side') != expected_side or
                            candidate.get('ordType') != 'market' or
                            candidate.get('reduceOnly') not in (True, 'true') or
                            candidate.get('posSide') not in (None, '', 'net')):
                        raise RuntimeError(
                            f'{ccxt_symbol} 止损子订单内容不一致: '
                            f'orderId={child_id}')
                    last_child_state = str(
                        candidate.get('state') or '').lower()
                    if last_child_state == 'filled':
                        child = candidate
                        break
                    if last_child_state not in {
                            'live', 'partially_filled', 'partially-filled'}:
                        raise RuntimeError(
                            f'{ccxt_symbol} 止损子订单终态不是 filled: '
                            f'orderId={child_id}, state={last_child_state}')
                if attempt < self.STOP_FILL_RECOVERY_ATTEMPTS - 1:
                    time.sleep(self.STOP_FILL_RECOVERY_DELAY)
            if child is None:
                raise RuntimeError(
                    f'{ccxt_symbol} 止损子订单未确认完整成交: '
                    f'orderId={child_id}, state={last_child_state}')
            filled = self._finite_nonnegative(child.get('accFillSz'))
            average = self._finite_nonnegative(child.get('avgPx'))
            child_size = self._finite_nonnegative(child.get('sz'))
            if (filled is None or filled <= tolerance or
                    child_size is None or
                    abs(child_size - filled) > tolerance or
                    average is None or average <= 0):
                raise RuntimeError(
                    f'{ccxt_symbol} 止损子订单缺少完整成交量/均价: orderId={child_id}')
            total_contracts += filled
            total_notional += filled * average
            if total_contracts > expected_contracts + tolerance:
                raise RuntimeError(
                    f'{ccxt_symbol} 止损子订单累计成交超过账本仓位')

            # OKX 的 fee<0 表示付费，fee>0 表示返佣；当前账本只接受非负成本。
            # 返佣或非 USDT 费用不影响真实 VWAP，但不能冒充可精确入账的费用。
            try:
                raw_fee = float(child.get('fee'))
            except (TypeError, ValueError):
                raw_fee = None
            if (raw_fee is None or not math.isfinite(raw_fee) or raw_fee > 0 or
                    child.get('feeCcy') != 'USDT'):
                fee_exact = False
            else:
                total_fee += -raw_fee
            fill_time = self._exchange_timestamp_iso(child.get('fillTime'))
            if fill_time:
                fill_times.append(fill_time)

        if abs(total_contracts - expected_contracts) > tolerance:
            raise RuntimeError(
                f'{ccxt_symbol} 止损子订单累计成交与账本仓位不一致: '
                f'filled={total_contracts}, expected={expected_contracts}')
        raw_actual_size = algo.get('actualSz')
        if raw_actual_size not in (None, ''):
            actual_size = self._finite_nonnegative(raw_actual_size)
            if (actual_size is None or
                    abs(actual_size - total_contracts) > tolerance):
                raise RuntimeError(
                    f'{ccxt_symbol} 算法单 actualSz 与子订单累计成交不一致')

        return {
            'source': 'okx_stop_fill',
            'average': total_notional / total_contracts,
            'filled_contracts': total_contracts,
            'filled_amount': self._contracts_to_coins(
                ccxt_symbol, total_contracts),
            'fee': total_fee if fee_exact else None,
            'fee_currency': 'USDT' if fee_exact else None,
            'order_ids': child_ids,
            'algo_order_ids': [algo_id],
            'fill_time': (max(fill_times) if fill_times else
                          self._exchange_timestamp_iso(algo.get('triggerTime'))),
        }

    @staticmethod
    def _normal_order_safely_cancelled(order):
        """只有非成交终态且累计成交为零，才能证明“撤单未改变仓位”。"""
        if not isinstance(order, dict):
            return False
        if str(order.get('state') or '').lower() not in {
                'canceled', 'cancelled', 'mmp_canceled', 'rejected', 'expired'}:
            return False
        try:
            filled = float(order.get('accFillSz') or 0)
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
