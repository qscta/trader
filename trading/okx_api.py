import ccxt
import hashlib
import logging
import math
import re
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import config_validation as cfgv
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
                # ccxt 对 >=6h K 线会据此请求 OKX 的 *utc bar（1Dutc），
                # 不依赖库版本当前的隐藏默认值。
                'fetchOHLCV': {'timezone': 'UTC'},
            },
        })
        if config.get('sandbox', False) or config.get('demo', False):
            ex.set_sandbox_mode(True)   # OKX 模拟盘（demo trading）
            logger.info("OKX 已切换到模拟盘模式")
        return ex

    def __init__(self, config):
        cfgv.validate_and_normalize_okx_config(config)
        # 该值直接进入每笔订单的 tdMode。拼写错误若不在启动时拒绝，会让
        # 全部下单到盘中才逐笔失败。
        raw_margin_mode = config.get('margin_mode', 'cross')
        super().__init__(config)
        self.margin_mode = raw_margin_mode
        # 杠杆：默认值 + 可按内部符号覆盖，如 {"BTCUSDT": 10}
        self.default_leverage = config.get('leverage', 5)
        self.leverage_overrides = dict(config.get('leverage_overrides', {}))

        self._contract_size_cache = {}     # ccxt_symbol -> contractSize（每张多少币）
        self._amount_precision_cache = {}  # ccxt_symbol -> 张数小数位
        self._load_market_cache()

    # ===================== 符号映射 =====================

    def to_ccxt_symbol(self, symbol):
        """内部符号(BTCUSDT) -> OKX ccxt 永续符号(BTC/USDT:USDT)。"""
        if not isinstance(symbol, str):
            raise ValueError('交易品种必须是字符串')
        if re.fullmatch(r'[A-Z0-9]{1,20}/USDT:USDT', symbol):
            return symbol
        if re.fullmatch(r'[A-Z0-9]{1,20}USDT', symbol):
            return symbol[:-4] + '/USDT:USDT'
        raise ValueError(
            f'只允许内部 BASEUSDT 或 U 本位永续 BASE/USDT:USDT: {symbol!r}')

    def to_internal_symbol(self, ccxt_symbol):
        """BTC/USDT:USDT -> BTCUSDT。"""
        if (not isinstance(ccxt_symbol, str) or
                re.fullmatch(
                    r'[A-Z0-9]{1,20}/USDT:USDT', ccxt_symbol) is None):
            raise ValueError(f'非法 U 本位永续 ccxt symbol: {ccxt_symbol!r}')
        base = ccxt_symbol.split('/')[0]
        return f"{base}USDT"

    def _resolve_symbol(self, symbol):
        """允许上层传内部符号或 ccxt 符号，统一成 ccxt 符号。"""
        return self.to_ccxt_symbol(symbol)

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
                    if contract_size is not None:
                        try:
                            self._contract_size_cache[sym] = (
                                self._parse_contract_size(contract_size, sym))
                        except ContractSizeUnavailable as exc:
                            # 单个不相关市场元数据异常不拖垮全部启动，但绝不缓存；
                            # 该品种若实际被交易，_get_contract_size 会再次严格拒绝。
                            logger.warning(str(exc))
                    amount_step = (market.get('precision') or {}).get('amount')
                    if amount_step is not None:
                        self._amount_precision_cache[sym] = self._normalize_precision(amount_step)
                    count += 1
            logger.info(f"OKX 市场缓存已加载: {count} 个 USDT 永续，合约面值 {len(self._contract_size_cache)} 个")
        except Exception as e:
            logger.warning(f"加载 OKX 市场缓存失败: {e}")

    @staticmethod
    def _parse_contract_size(value, ccxt_symbol):
        if isinstance(value, bool):
            raise ContractSizeUnavailable(
                f'{ccxt_symbol} contractSize 不能是布尔值，拒绝换算/交易')
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ContractSizeUnavailable(
                f'{ccxt_symbol} contractSize 不是有效数字，拒绝换算/交易') from exc
        if not math.isfinite(parsed) or parsed <= 0:
            raise ContractSizeUnavailable(
                f'{ccxt_symbol} 市场数据缺少有效 contractSize，拒绝换算/交易')
        return parsed

    def _get_contract_size(self, ccxt_symbol):
        """获取合约面值（每张多少币）。获取失败/缺失必须抛出，由上层放弃本次交易。"""
        if ccxt_symbol in self._contract_size_cache:
            return self._parse_contract_size(
                self._contract_size_cache[ccxt_symbol], ccxt_symbol)
        try:
            market = self.exchange.market(ccxt_symbol)
            if not isinstance(market, dict):
                raise TypeError('market 响应不是对象')
            contract_size = self._parse_contract_size(
                market.get('contractSize'), ccxt_symbol)
        except ContractSizeUnavailable:
            raise
        except Exception as e:
            raise ContractSizeUnavailable(f"{ccxt_symbol} 合约面值获取失败: {e}，拒绝换算/交易") from e
        self._contract_size_cache[ccxt_symbol] = contract_size
        return contract_size

    # ===================== 张数换算 =====================

    @staticmethod
    def _finite_decimal_float(value, field, *, allow_zero=True):
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f'{field} 无法转换为有限浮点数') from exc
        if (not math.isfinite(numeric) or
                (numeric < 0 if allow_zero else numeric <= 0)):
            qualifier = '有限非负数' if allow_zero else '有限正数'
            raise ValueError(f'{field} 必须是{qualifier}')
        return numeric

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
        raw_contracts_float = self._finite_decimal_float(
            raw_contracts, f'{ccxt_symbol} 原始张数', allow_zero=False)
        try:
            # 整数张数可被 float 精确表达；非整数仍由 ccxt 按市场步长截断。
            rounded_raw = self.exchange.amount_to_precision(
                ccxt_symbol, raw_contracts_float)
        except Exception:
            precision = self._amount_precision_cache.get(ccxt_symbol, 0)
            quantum = Decimal(1).scaleb(-precision)
            try:
                rounded = raw_contracts.quantize(
                    quantum, rounding=ROUND_DOWN)
            except InvalidOperation as exc:
                raise ValueError(
                    f'{ccxt_symbol} 原始张数无法按精度对齐') from exc
        else:
            if isinstance(rounded_raw, bool):
                raise ValueError(
                    f'{ccxt_symbol} amount_to_precision 返回布尔值')
            try:
                rounded = Decimal(str(rounded_raw))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(
                    f'{ccxt_symbol} amount_to_precision 返回非法张数') from exc
        if not rounded.is_finite() or rounded < 0:
            raise ValueError(
                f'{ccxt_symbol} amount_to_precision 返回非有限或负张数')
        # amount_to_precision 在本系统必须是向下对齐。只容忍十进制/二进制
        # 往返的 1e-12 张表示噪声；真实向上取整会扩大实盘风险，必须拒绝。
        if rounded > raw_contracts:
            if rounded - raw_contracts > Decimal('1e-12'):
                raise ValueError(
                    f'{ccxt_symbol} amount_to_precision 非法向上取整')
            rounded = raw_contracts
        return self._finite_decimal_float(
            rounded, f'{ccxt_symbol} 对齐张数')

    def _contracts_to_coins(self, ccxt_symbol, contracts):
        """张数 -> 币数，保持与十进制 contractSize 的账本口径一致。"""
        if isinstance(contracts, bool):
            raise ValueError(f'{ccxt_symbol} 张数不能是布尔值')
        try:
            coins = (Decimal(str(contracts)) *
                     Decimal(str(self._get_contract_size(ccxt_symbol))))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f'{ccxt_symbol} 张数无法换算为币数') from exc
        return self._finite_decimal_float(
            coins, f'{ccxt_symbol} 换算币数')

    def round_quantity(self, symbol, quantity):
        """把上层算出的“币数”对齐到 OKX 整张，再换算回“币数”返回。

        返回的币数 = 整张数 × 合约面值，确保它与最终真实下单张数一一对应——
        这样上层用这个币数做名义价值/风控/盈亏计算时不会与实际成交错位。
        若不足一张则返回 0，上层会据此放弃开仓（无法交易小于一张的量）。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        contracts = self._coin_to_contracts(ccxt_symbol, quantity)
        return self._contracts_to_coins(ccxt_symbol, contracts)

    def get_quantity_precision(self, symbol):
        """返回“张数”的小数位（仅用于日志展示）。"""
        ccxt_symbol = self._resolve_symbol(symbol)
        return self._amount_precision_cache.get(ccxt_symbol, 0)

    # ===================== 杠杆 / 持仓模式 =====================

    def verify_one_way_mode(self):
        """只读回验单向净持仓模式；启动过程绝不隐式修改真实账户。"""
        try:
            mode = self.exchange.fetch_position_mode()
        except Exception as e:
            raise PositionModeError(f"OKX 持仓模式回读失败，拒绝启动: {e}") from e
        if not isinstance(mode, dict) or mode.get('hedged') is not False:
            raise PositionModeError(f"OKX 账户不是单向净持仓模式，拒绝启动: {mode!r}")
        info = mode.get('info') or {}
        if info.get('posMode') not in (None, 'net_mode'):
            raise PositionModeError(f"OKX posMode={info.get('posMode')}，拒绝启动")
        logger.info("OKX 单向(净)持仓模式已只读确认")

    def _leverage_for(self, ccxt_symbol):
        internal = self.to_internal_symbol(ccxt_symbol)
        return (self.leverage_overrides.get(internal)
                or self.leverage_overrides.get(ccxt_symbol)
                or self.default_leverage)

    def setup_symbol(self, ccxt_symbol):
        """每次开仓前幂等设置并确认杠杆，避免进程外修改后沿用陈旧假设。"""
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        leverage = self._leverage_for(ccxt_symbol)
        try:
            response = self.exchange.set_leverage(
                leverage, ccxt_symbol, params={'mgnMode': self.margin_mode})
            data = response.get('data') if isinstance(response, dict) else None
            item = data[0] if isinstance(data, list) and len(data) == 1 else None
            if (not isinstance(response, dict) or response.get('code') != '0' or
                    not isinstance(item, dict) or
                    item.get('instId') != self._to_inst_id(ccxt_symbol) or
                    item.get('mgnMode') != self.margin_mode or
                    item.get('posSide') not in (None, '', 'net') or
                    item.get('sCode') not in (None, '', '0')):
                raise RuntimeError('杠杆设置 ACK 身份/状态不完整')
            raw_leverage = item.get('lever')
            if isinstance(raw_leverage, bool):
                raise RuntimeError('杠杆设置 ACK lever 非法')
            try:
                observed_leverage = Decimal(str(raw_leverage))
                expected_leverage = Decimal(str(leverage))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise RuntimeError('杠杆设置 ACK lever 非法') from exc
            if (not observed_leverage.is_finite() or
                    observed_leverage != expected_leverage):
                raise RuntimeError('杠杆设置 ACK lever 不匹配')
            logger.info(f"OKX 已设置 {ccxt_symbol} 杠杆={leverage}x，保证金模式={self.margin_mode}")
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

    def _position_entry_is_nonzero(self, entry, info, ccxt_symbol):
        """统一裁决持仓条目的数量与方向；单仓查询/全仓清单共用。"""
        hedged = entry.get('hedged')
        if hedged not in (None, False, True):
            raise PositionModeError(
                f'{ccxt_symbol} 持仓 hedged 字段非法: {hedged!r}')
        pos_side = info.get('posSide')
        if pos_side not in (None, '', 'net', 'long', 'short'):
            raise PositionModeError(
                f'{ccxt_symbol} 持仓 posSide 字段非法: {pos_side!r}')
        standard_side = entry.get('side')
        if standard_side not in (None, '', 'long', 'short'):
            raise PositionModeError(
                f'{ccxt_symbol} 持仓 side 字段非法: {standard_side!r}')
        contracts_signed = self._parse_signed_size(
            entry.get('contracts'), 'contracts', ccxt_symbol)
        raw_pos = self._parse_signed_size(info.get('pos'), 'pos', ccxt_symbol)
        if contracts_signed is None:
            if raw_pos is None:
                raise PositionModeError(
                    f'{ccxt_symbol} contracts 与原始 pos 同时缺失，'
                    '无法证明空仓')
            if raw_pos == 0:
                return False
            raise PositionModeError(
                f'{ccxt_symbol} contracts 缺失但原始 pos={info.get("pos")!r} '
                '非零，拒绝当空仓')

        contracts = abs(contracts_signed)
        if contracts <= 0:
            if raw_pos is not None and raw_pos != 0:
                raise PositionModeError(
                    f'{ccxt_symbol} contracts=0 与原始 pos={info.get("pos")!r} 矛盾')
            return False
        if raw_pos is not None and raw_pos == 0:
            raise PositionModeError(
                f'{ccxt_symbol} contracts={entry.get("contracts")!r} 与原始 pos=0 矛盾')
        if (raw_pos is not None and raw_pos != 0 and
                abs(contracts - abs(raw_pos)) >
                self._contracts_tolerance(ccxt_symbol)):
            raise PositionModeError(
                f'{ccxt_symbol} 标准 contracts={entry.get("contracts")!r} '
                f'与原始 pos={info.get("pos")!r} 数量矛盾，拒绝裁决')
        if hedged is True or pos_side in ('long', 'short'):
            raise PositionModeError(
                f'{ccxt_symbol} 检测到双向持仓腿(posSide={info.get("posSide")})，'
                '拒绝裁剪为单笔订单')
        side = self._position_side(entry)
        if side not in ('long', 'short'):
            raise PositionModeError(
                f'{ccxt_symbol} 非零持仓方向不可判定，拒绝继续交易')
        if raw_pos is not None and raw_pos != 0:
            raw_side = 'long' if raw_pos > 0 else 'short'
            if raw_side != side:
                raise PositionModeError(
                    f'{ccxt_symbol} 标准 side={side} 与原始 pos={info.get("pos")!r} '
                    '符号矛盾，拒绝裁决')
        return True

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
                raise PositionModeError(
                    f'{ccxt_symbol} 持仓条目为 None，无法证明空仓')
            if not isinstance(p, dict):
                raise PositionModeError(
                    f"{ccxt_symbol} 持仓条目结构异常: {type(p).__name__}")
            info = p.get('info')
            if info is None:
                info = {}
            if not isinstance(info, dict):
                raise PositionModeError(
                    f'{ccxt_symbol} 持仓 info 结构异常: '
                    f'{type(info).__name__}')
            self._assert_position_entry_symbol(p, info, ccxt_symbol)
            if self._position_entry_is_nonzero(p, info, ccxt_symbol):
                normalized = dict(p)
                normalized['contracts'] = abs(self._parse_signed_size(
                    p.get('contracts'), 'contracts', ccxt_symbol))
                nonzero.append(normalized)
        if len(nonzero) > 1:
            raise PositionModeError(f"{ccxt_symbol} 同时存在 {len(nonzero)} 条非零持仓，拒绝隐藏任何一腿")
        return nonzero[0] if nonzero else None

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
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed >= 0 else None

    @staticmethod
    def _positive_coin_amount(value, field):
        """公开交易边界共用的币数校验；失败必须发生在任何交易所读取前。"""
        if isinstance(value, bool):
            raise ValueError(f'{field}不能是布尔值')
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f'{field}不是有效数字') from exc
        try:
            binary = float(parsed)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f'{field}超出可交易数值范围') from exc
        if (not parsed.is_finite() or not math.isfinite(binary) or
                parsed <= 0):
            raise ValueError(f'{field}必须是有限正数')
        return parsed

    @staticmethod
    def _public_order_id(value):
        """撤单公开边界只接受明确、无空白的 ASCII 订单 ID。"""
        if (not isinstance(value, str) or value != value.strip() or
                re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) is None):
            raise ValueError('OKX order_id 必须为 1-64 位 ASCII 字母数字、下划线或连字符')
        return value

    @staticmethod
    def _strict_boolean(value):
        if value is True or (
                isinstance(value, str) and value == 'true'):
            return True
        if value is False or (
                isinstance(value, str) and value == 'false'):
            return False
        return None

    @staticmethod
    def _raw_candle_number(value, field, *, positive):
        if isinstance(value, bool):
            raise ValueError(f'OKX 日 K {field} 不能是布尔值')
        try:
            parsed = Decimal(str(value))
            numeric = float(parsed)
        except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f'OKX 日 K {field} 不是有效数字') from exc
        if (not parsed.is_finite() or not math.isfinite(numeric) or
                (numeric <= 0 if positive else numeric < 0)):
            qualifier = '有限正数' if positive else '有限非负数'
            raise ValueError(f'OKX 日 K {field} 必须是{qualifier}')
        return numeric

    @retry_on_network_error(max_retries=3)
    def _fetch_confirmed_daily_ohlcv(self, ccxt_symbol, requested):
        """直接使用 OKX 原生 ``confirm`` 位，只接收已完成的 UTC 日 K。"""
        raw_limit = min(300, requested + 1)
        response = self.exchange.publicGetMarketCandles({
            'instId': self._to_inst_id(ccxt_symbol),
            'bar': '1Dutc',
            'limit': str(raw_limit),
        })
        data = response.get('data') if isinstance(response, dict) else None
        if (not isinstance(response, dict) or response.get('code') != '0' or
                not isinstance(data, list)):
            raise ValueError(
                f'{ccxt_symbol} OKX 原生日 K 响应结构/状态异常')

        rows = []
        for index, raw in enumerate(data):
            if not isinstance(raw, (list, tuple)) or len(raw) < 9:
                raise ValueError(
                    f'{ccxt_symbol} OKX 第{index}根原生日 K 结构异常')
            confirm = raw[8]
            if not isinstance(confirm, str) or confirm not in {'0', '1'}:
                raise ValueError(
                    f'{ccxt_symbol} OKX 第{index}根日 K confirm 非法')
            if confirm == '0':
                continue
            timestamp = self._raw_candle_number(
                raw[0], 'timestamp', positive=False)
            if not timestamp.is_integer():
                raise ValueError(
                    f'{ccxt_symbol} OKX 第{index}根日 K timestamp 不是整数毫秒')
            if int(timestamp) % 86_400_000 != 0:
                raise ValueError(
                    f'{ccxt_symbol} OKX 第{index}根日 K 未锚定 UTC 00:00')
            rows.append([
                int(timestamp),
                self._raw_candle_number(raw[1], 'open', positive=True),
                self._raw_candle_number(raw[2], 'high', positive=True),
                self._raw_candle_number(raw[3], 'low', positive=True),
                self._raw_candle_number(raw[4], 'close', positive=True),
                self._raw_candle_number(raw[5], 'volume', positive=False),
            ])
        rows.sort(key=lambda row: row[0])
        return self.validate_ohlcv(
            rows[-requested:], ccxt_symbol, timeframe='1d')

    def fetch_ohlcv(self, symbol, timeframe='1d', limit=100):
        """日 K 以 OKX ``confirm=1`` 为收盘证据；其余周期沿用通用读取。"""
        if isinstance(limit, bool):
            raise ValueError('K 线 limit 不能是布尔值')
        try:
            requested = int(limit)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('K 线 limit 必须是整数') from exc
        if requested <= 0:
            return []
        if requested > 300:
            raise ValueError(
                f'K 线请求 {requested} 根超过 OKX 单页上限 300；'
                '策略配置必须在最新单页内完成计算')
        ccxt_symbol = self._resolve_symbol(symbol)
        if timeframe == '1d':
            return self._fetch_confirmed_daily_ohlcv(
                ccxt_symbol, requested)
        return super().fetch_ohlcv(
            ccxt_symbol, timeframe=timeframe, limit=requested)

    @staticmethod
    def _order_reduce_only_value(order):
        """合并标准/原生 reduceOnly；缺失、非法或冲突都返回 None。"""
        if not isinstance(order, dict):
            return None
        info = order.get('info') or {}
        if not isinstance(info, dict):
            return None
        values = [
            OkxApi._strict_boolean(value)
            for value in (order.get('reduceOnly'), info.get('reduceOnly'))
            if value is not None]
        if (not values or any(value is None for value in values) or
                len(set(values)) != 1):
            return None
        return values[0]

    @staticmethod
    def _order_reduce_only(order):
        """归一 reduceOnly：优先标准字段，缺失回退原生 info；只认 True/'true'。

        读不到一律 False——不可证明 reduce-only 的订单绝不当保护性止损。
        """
        return OkxApi._order_reduce_only_value(order) is True

    @staticmethod
    def _reduce_only_unknown(order):
        """reduceOnly 在标准字段与原生 info 中都读不到 → 未知。

        未知 ≠ 否：未知订单不能证明是保护（不参与 intact 匹配），但也
        不能隐身——四态裁决必须把它当候选计入，宁可 mismatch 交人工，
        绝不因看不见而判 missing 再挂第二张止损。
        """
        return OkxApi._order_reduce_only_value(order) is None

    def _fetch_order_for_confirmation(self, ccxt_symbol, order_id, client_order_id):
        """查询订单并严格绑定所用身份；缺失/错配一律不可裁决。"""
        if client_order_id is None:
            raise ValueError('订单确认必须提供确定性 clOrdId')
        client_order_id = self._client_order_id(client_order_id)
        if order_id not in (None, ''):
            expected_id = self._public_order_id(order_id)
            order = self.exchange.fetch_order(expected_id, ccxt_symbol)
            query_kind = 'ordId'
        else:
            expected_id = client_order_id
            order = self.exchange.fetch_order(
                expected_id, ccxt_symbol, params={'clOrdId': expected_id})
            query_kind = 'clOrdId'
        if not isinstance(order, dict):
            raise RuntimeError(
                f'{ccxt_symbol} {query_kind} 查询返回非对象，拒绝归因')
        info = order.get('info')
        if info is None:
            info = {}
        if not isinstance(info, dict):
            raise RuntimeError(
                f'{ccxt_symbol} {query_kind} 查询 info 非对象，拒绝归因')

        # 标准字段与原生字段是两份独立证据，不能用 ``or`` 让一份正确值
        # 遮住另一份冲突值。只要交易所返回了某个身份字段，它就必须与
        # 查询目标（或同类字段）一致。
        try:
            observed_order_ids = [
                self._public_order_id(value)
                for value in (order.get('id'), info.get('ordId'))
                if value not in (None, '')]
            observed_client_ids = [
                self._client_order_id(value)
                for value in (
                    order.get('clientOrderId'), info.get('clOrdId'))
                if value not in (None, '')]
        except ValueError as exc:
            raise RuntimeError(
                f'{ccxt_symbol} {query_kind} 查询返回非法订单身份') from exc

        observed_symbol = order.get('symbol')
        observed_inst_id = info.get('instId')
        if observed_symbol in (None, '') and observed_inst_id in (None, ''):
            raise RuntimeError(
                f'{ccxt_symbol} {query_kind} 查询缺少品种身份')
        if (observed_symbol not in (None, '') and
                (not isinstance(observed_symbol, str) or
                 observed_symbol != ccxt_symbol)):
            raise RuntimeError(
                f'{ccxt_symbol} {query_kind} 查询返回错误 symbol')
        if (observed_inst_id not in (None, '') and
                (not isinstance(observed_inst_id, str) or
                 observed_inst_id != self._to_inst_id(ccxt_symbol))):
            raise RuntimeError(
                f'{ccxt_symbol} {query_kind} 查询返回错误 instId')
        if (info.get('instType') not in (None, '', 'SWAP')):
            raise RuntimeError(
                f'{ccxt_symbol} {query_kind} 查询返回错误 instType')

        if order_id not in (None, ''):
            if (not observed_order_ids or
                    any(value != expected_id for value in observed_order_ids)):
                raise RuntimeError(
                    f'{ccxt_symbol} ordId 查询身份不匹配: '
                    f'expected={expected_id}')
            if (client_order_id not in (None, '') and
                    (not observed_client_ids or
                     any(value != client_order_id
                         for value in observed_client_ids))):
                raise RuntimeError(
                    f'{ccxt_symbol} ordId 查询返回错误 clOrdId')
        else:
            if (not observed_client_ids or
                    any(value != expected_id for value in observed_client_ids)):
                raise RuntimeError(
                    f'{ccxt_symbol} clOrdId 查询身份不匹配: '
                    f'expected={expected_id}')
            if not observed_order_ids:
                raise RuntimeError(
                    f'{ccxt_symbol} clOrdId 查询缺少交易所 ordId')
            if len(set(observed_order_ids)) != 1:
                raise RuntimeError(
                    f'{ccxt_symbol} clOrdId 查询返回冲突 ordId')
        return order

    def _fetch_order_tristate(
            self, ccxt_symbol, client_order_id, *,
            wait_for_visibility=False):
        """订单三态裁决的唯一原语：('found', order) / ('absent', None)。

        只有交易所明确 OrderNotFound 才是「明确不存在」；{}/None/缺身份
        字段属「无法裁决」，一律抛出——把畸形响应当不存在，幂等开仓会
        重复下单、平仓恢复会漏算真实成交腿。所有按 clOrdId 的存在性
        查询都必须走这里，不得各自临时判定。恢复/崩溃裁决若要求
        ``wait_for_visibility``，必须连续经过统一确认宽限仍为
        OrderNotFound，才返回 absent；调用方不得自行复制 sleep/poll。
        """
        attempts = self.ORDER_CONFIRM_ATTEMPTS if wait_for_visibility else 1
        for attempt in range(attempts):
            try:
                order = self._fetch_order_for_confirmation(
                    ccxt_symbol, None, client_order_id)
            except ccxt.OrderNotFound:
                if attempt < attempts - 1:
                    time.sleep(self.ORDER_CONFIRM_DELAY)
                    continue
                return 'absent', None
            if not isinstance(order, dict) or not order.get('id'):
                raise RuntimeError(
                    f'{ccxt_symbol} clOrdId={client_order_id} 查询返回无法识别的'
                    f'响应，拒绝当订单不存在: {str(order)[:120]}')
            return 'found', order
        raise AssertionError('订单可见性确认循环未返回')

    def _sanitize_order_ack(self, ccxt_symbol, client_order_id, ack):
        """只保留能安全绑定到当前请求的 POST ACK。

        ACK 不是最终成交证明，但其中若出现冲突身份，也不能拿错误 ordId 去
        查询或撤单。冲突时丢弃整份 ACK，后续只按权威 clOrdId 查询/撤销。
        """
        if client_order_id is None:
            raise ValueError('ACK 裁决必须提供确定性 clOrdId')
        if ack is None:
            return None
        if not isinstance(ack, dict):
            logger.critical(f'{ccxt_symbol} 下单 ACK 非对象，改按 clOrdId 裁决')
            return None
        info = ack.get('info')
        if info is None:
            info = {}
        if not isinstance(info, dict):
            logger.critical(f'{ccxt_symbol} 下单 ACK info 非对象，改按 clOrdId 裁决')
            return None
        try:
            expected_client_id = self._client_order_id(client_order_id)
            order_ids = [
                self._public_order_id(value)
                for value in (ack.get('id'), info.get('ordId'))
                if value not in (None, '')]
            client_ids = [
                self._client_order_id(value)
                for value in (
                    ack.get('clientOrderId'), info.get('clOrdId'))
                if value not in (None, '')]
        except ValueError:
            logger.critical(
                f'{ccxt_symbol} 下单 ACK 身份非法，丢弃 ACK 并改按 clOrdId 裁决')
            return None
        invalid = (
            not client_ids or
            len(set(order_ids)) > 1 or
            any(value != expected_client_id for value in client_ids) or
            (ack.get('symbol') not in (None, '') and
             (not isinstance(ack.get('symbol'), str) or
              ack.get('symbol') != ccxt_symbol)) or
            (info.get('instId') not in (None, '') and
             (not isinstance(info.get('instId'), str) or
              info.get('instId') != self._to_inst_id(ccxt_symbol))))
        if invalid:
            logger.critical(
                f'{ccxt_symbol} 下单 ACK 身份冲突，丢弃 ACK 并改按 clOrdId 裁决')
            return None
        return dict(ack)

    @staticmethod
    def _client_order_id(value=None):
        """生成或校验 OKX clOrdId（1-32 位 ASCII 字母数字）。"""
        if value is None:
            return f"trader{uuid.uuid4().hex[:26]}"
        if not isinstance(value, str):
            raise ValueError("OKX client_order_id 必须是字符串")
        if not (1 <= len(value) <= 32 and value.isascii() and value.isalnum()):
            raise ValueError("OKX client_order_id 必须为 1-32 位 ASCII 字母数字")
        return value

    @staticmethod
    def compensation_client_order_id(open_client_order_id):
        """由持久化开仓句柄稳定派生补偿平仓基础 clOrdId。"""
        if open_client_order_id is None:
            raise ValueError('开仓 clOrdId 不能为空')
        value = OkxApi._client_order_id(open_client_order_id)
        return 'R' + hashlib.sha256(
            f'open-compensation|{value}'.encode('utf-8')
        ).hexdigest()[:31]

    @staticmethod
    def _canonical_order_status(value):
        if value in (None, ''):
            return None
        status = str(value).lower()
        if status in {'closed', 'filled'}:
            return 'filled'
        if status in {'canceled', 'cancelled', 'mmp_canceled', 'mmp_cancelled'}:
            return 'canceled'
        if status in {'rejected', 'expired'}:
            return status
        if status in {'open', 'live', 'partially_filled'}:
            return 'open'
        return None

    def _terminal_fill(self, order, amount, tolerance):
        """严格合并标准/原生终态、成交量与余量守恒证据。"""
        if not isinstance(order, dict):
            raise RuntimeError('订单终态响应非对象')
        info = order.get('info') or {}
        if not isinstance(info, dict):
            raise RuntimeError('订单终态 info 非对象')

        statuses = [
            self._canonical_order_status(value)
            for value in (order.get('status'), info.get('state'))
            if value not in (None, '')]
        if (not statuses or any(value is None for value in statuses) or
                len(set(statuses)) != 1):
            raise RuntimeError('订单标准/原生状态缺失、未知或冲突')
        status = statuses[0]

        amounts = [
            self._finite_nonnegative(value)
            for value in (order.get('amount'), info.get('sz'))
            if value not in (None, '')]
        if (not amounts or any(value is None or value <= tolerance
                               for value in amounts) or
                any(abs(value - amount) > tolerance for value in amounts)):
            raise RuntimeError('订单标准/原生委托量缺失或冲突')

        filled_values = [
            self._finite_nonnegative(value)
            for value in (order.get('filled'), info.get('accFillSz'))
            if value not in (None, '')]
        if (filled_values and
                (any(value is None for value in filled_values) or
                 max(filled_values) - min(filled_values) > tolerance)):
            raise RuntimeError('订单标准/原生成交量冲突')
        remaining_values = [
            self._finite_nonnegative(value)
            for value in (order.get('remaining'),)
            if value not in (None, '')]
        if remaining_values and any(
                value is None for value in remaining_values):
            raise RuntimeError('订单余量非法')

        filled = filled_values[0] if filled_values else None
        remaining = remaining_values[0] if remaining_values else None
        if filled is None and remaining is not None:
            filled = amount - remaining
        if filled is None:
            # 终态可以成立，但无法把当前净仓归因给这张订单。调用方必须
            # 将 open 隔离、close 按价格/费用不完整处理，不能猜成满成。
            terminal = status in {'filled', 'canceled', 'rejected', 'expired'}
            return terminal, None, status
        if (filled < -tolerance or filled > amount + tolerance or
                (remaining is not None and
                 abs(filled + remaining - amount) > tolerance)):
            raise RuntimeError('订单成交量/余量不守恒')
        filled = min(amount, max(0.0, filled))
        if status == 'filled' and abs(filled - amount) > tolerance:
            raise RuntimeError('filled/closed 状态与部分成交量矛盾')
        terminal = status in {'filled', 'canceled', 'rejected', 'expired'}
        return terminal, filled, status

    def _find_existing_close_order(
            self, ccxt_symbol, close_side, requested_contracts,
            client_order_id, *, wait_for_visibility=False):
        """只读找回单一持久化 clOrdId 的终态成交。

        每个 close intent 只允许一张市价单。若终态部分成交，上层
        先原子落余仓并消费该 intent，下次动作创建新 intent。
        恢复期只查询这一张单，绝不补发。
        """
        if client_order_id is None:
            raise ValueError('平仓恢复必须提供确定性 clOrdId')
        client_order_id = self._client_order_id(client_order_id)
        tolerance = self._contracts_tolerance(ccxt_symbol)
        last_problem = None
        for attempt in range(self.ORDER_CONFIRM_ATTEMPTS):
            presence, candidate = self._fetch_order_tristate(
                ccxt_symbol, client_order_id,
                wait_for_visibility=wait_for_visibility)
            if presence == 'absent':
                return None
            info = candidate.get('info') or {}
            observed_client_id = (
                candidate.get('clientOrderId') or info.get('clOrdId'))
            if observed_client_id in (None, ''):
                raise RuntimeError(
                    f'{ccxt_symbol} 平仓恢复单缺少 clOrdId')
            try:
                observed_client_id = self._client_order_id(observed_client_id)
            except ValueError as exc:
                raise RuntimeError(
                    f'{ccxt_symbol} 平仓恢复单 clOrdId 非法') from exc
            if observed_client_id != client_order_id:
                raise RuntimeError(
                    f'{ccxt_symbol} 平仓恢复单 clOrdId 不匹配: '
                    f'expected={client_order_id}, actual={observed_client_id}')
            observed_amount = candidate.get('amount')
            if observed_amount is None:
                observed_amount = info.get('sz')
            observed_amount = self._finite_nonnegative(observed_amount)
            if (observed_amount is None or observed_amount <= tolerance or
                    abs(observed_amount - requested_contracts) > tolerance or
                    not self._existing_order_matches_request(
                        candidate, ccxt_symbol, close_side,
                        observed_amount, reduce_only=True)):
                raise RuntimeError(
                    f'{ccxt_symbol} 平仓恢复单内容与 intent 不一致')
            terminal, filled, order_status = self._terminal_fill(
                candidate, observed_amount, tolerance)
            if terminal and filled is not None:
                order = dict(candidate)
                order.setdefault('clientOrderId', client_order_id)
                return client_order_id, order, observed_amount, filled
            last_problem = (
                f'{client_order_id}: status={order_status}, '
                f'filled={filled}, amount={observed_amount}')
            if attempt < self.ORDER_CONFIRM_ATTEMPTS - 1:
                time.sleep(self.ORDER_CONFIRM_DELAY)
        raise RuntimeError(
            f'{ccxt_symbol} 平仓恢复单未能确认终态: {last_problem}')

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
        if not isinstance(info, dict):
            return False
        observed_symbol = order.get('symbol')
        native_inst_id = info.get('instId')
        symbol_matches = False
        try:
            if observed_symbol not in (None, ''):
                symbol_matches = self._resolve_symbol(observed_symbol) == ccxt_symbol
            if native_inst_id not in (None, ''):
                native_matches = (
                    str(native_inst_id) == self._to_inst_id(ccxt_symbol))
                symbol_matches = (
                    native_matches if observed_symbol in (None, '') else
                    symbol_matches and native_matches)
        except (TypeError, ValueError):
            symbol_matches = False

        observed_types = [
            str(value) for value in (order.get('type'), info.get('ordType'))
            if value not in (None, '')]
        observed_sides = [
            str(value) for value in (order.get('side'), info.get('side'))
            if value not in (None, '')]
        raw_amounts = [
            value for value in (order.get('amount'), info.get('sz'))
            if value not in (None, '')]
        observed_amounts = [
            self._finite_nonnegative(value) for value in raw_amounts]
        tolerance = self._contracts_tolerance(ccxt_symbol)
        amount_matches = (
            bool(observed_amounts) and
            all(value is not None and value > tolerance and
                abs(value - requested_contracts) <= tolerance
                for value in observed_amounts))
        observed_reduce_only = self._order_reduce_only_value(order)
        matches = (
            symbol_matches and bool(observed_types) and
            all(value == 'market' for value in observed_types) and
            bool(observed_sides) and
            all(value == order_side for value in observed_sides) and
            amount_matches and
            observed_reduce_only is bool(reduce_only))
        if not matches:
            logger.critical(
                f"{ccxt_symbol} clOrdId 命中订单与请求不一致，拒绝复用: "
                f"symbol={observed_symbol or native_inst_id}, type={observed_types}, "
                f"side={observed_sides}, amount={observed_amounts}, "
                f"reduceOnly={observed_reduce_only}; expected side={order_side}, "
                f"amount={requested_contracts}, reduceOnly={bool(reduce_only)}")
        return matches

    def _confirmed_order_result(self, ccxt_symbol, order, requested_contracts,
                                actual_contracts, *, fully_closed=None, source='order+position'):
        """构造上层契约：amount/requested_amount 均为币数，张数不外泄。"""
        result = self._sanitize_financial_evidence(order)
        result['amount'] = self._contracts_to_coins(ccxt_symbol, actual_contracts)
        result['requested_amount'] = self._contracts_to_coins(ccxt_symbol, requested_contracts)
        result['confirmed'] = True
        result['fully_filled'] = (
            actual_contracts + self._contracts_tolerance(ccxt_symbol) >= requested_contracts)
        result['confirmation_source'] = source
        if fully_closed is not None:
            result['fully_closed'] = bool(fully_closed)
        return result

    @classmethod
    def _sanitize_financial_evidence(cls, order):
        """冲突/畸形成交财务证据一律剥离，不能污染账本与止损风险价。"""
        result = dict(order or {})
        result.pop('financial_evidence_incomplete', None)
        info = result.get('info') or {}
        invalid = not isinstance(info, dict)
        if invalid:
            info = {}

        def finite(value, *, positive=False):
            if isinstance(value, bool):
                return None
            try:
                parsed = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if not math.isfinite(parsed) or (parsed <= 0 if positive else parsed < 0):
                return None
            return parsed

        def finite_sum(values):
            total = 0.0
            for value in values:
                total += value
                if not math.isfinite(total):
                    return None
            return total

        raw_prices = [
            value for value in (result.get('average'), info.get('avgPx'))
            if value not in (None, '')]
        price_missing = not raw_prices
        prices = [finite(value, positive=True) for value in raw_prices]
        if (any(value is None for value in prices) or
                (len(prices) > 1 and
                 max(prices) - min(prices) >
                 max(1e-12, max(prices) * 1e-12))):
            invalid = True

        if result.get('cost') not in (None, '') and finite(
                result.get('cost')) is None:
            invalid = True

        top_fee_cost = None
        top_fee_currency = None
        fee = result.get('fee')
        if fee is not None:
            if not isinstance(fee, dict) or fee.get('cost') in (None, ''):
                invalid = True
            else:
                top_fee_cost = finite(fee.get('cost'))
                top_fee_currency = fee.get('currency')
                if (top_fee_cost is None or
                        (top_fee_currency is not None and
                         (not isinstance(top_fee_currency, str) or
                          not top_fee_currency))):
                    invalid = True

        fees = result.get('fees')
        fee_costs = []
        fee_currencies = set()
        if fees is not None:
            if not isinstance(fees, list):
                invalid = True
            else:
                for item in fees:
                    if (not isinstance(item, dict) or
                            item.get('cost') in (None, '') or
                            finite(item.get('cost')) is None or
                            (item.get('currency') is not None and
                             (not isinstance(item.get('currency'), str) or
                              not item.get('currency')))):
                        invalid = True
                        break
                    fee_costs.append(finite(item.get('cost')))
                    if item.get('currency') is not None:
                        fee_currencies.add(item.get('currency'))

        native_fee = None
        if info.get('fee') not in (None, ''):
            if isinstance(info.get('fee'), bool):
                invalid = True
            else:
                try:
                    native_fee = abs(float(info.get('fee')))
                except (TypeError, ValueError, OverflowError):
                    invalid = True
                else:
                    if not math.isfinite(native_fee):
                        invalid = True
        native_currency = info.get('feeCcy')
        if native_currency not in (None, '') and not isinstance(native_currency, str):
            invalid = True
        if top_fee_cost is not None and native_fee is not None:
            if abs(top_fee_cost - native_fee) > max(
                    1e-12, max(top_fee_cost, native_fee) * 1e-12):
                invalid = True
        fees_total = finite_sum(fee_costs)
        if fee_costs and fees_total is None:
            invalid = True
        if top_fee_cost is not None and fees is not None:
            if (not fee_costs or fees_total is None or
                    abs(top_fee_cost - fees_total) > max(
                        1e-12, max(top_fee_cost, fees_total) * 1e-12)):
                invalid = True
            if (top_fee_currency and fee_currencies and
                    fee_currencies != {top_fee_currency}):
                invalid = True
        if native_fee is not None and fee_costs:
            if (fees_total is None or
                    abs(native_fee - fees_total) > max(
                        1e-12, max(native_fee, fees_total) * 1e-12)):
                invalid = True
            if (native_currency not in (None, '') and fee_currencies and
                    fee_currencies != {native_currency}):
                invalid = True
        if (top_fee_currency and native_currency not in (None, '') and
                top_fee_currency != native_currency):
            invalid = True

        if invalid:
            for key in ('average', 'cost', 'fee', 'fees'):
                result.pop(key, None)
            result['execution_ambiguous'] = True
        elif price_missing:
            # 有成交的调用方必须把这视为未完成财务证据，
            # 不得用当前行情/信号价伪装成真实成交均价。
            result['financial_evidence_incomplete'] = True
        return result

    def _confirm_market_order(self, ccxt_symbol, initial_order, client_order_id, *,
                              operation, side, pre_contracts,
                              requested_contracts):
        """轮询订单终态，并以净持仓变化交叉确认实际成交张数。

        返回 ``(result, last_position_contracts)``。只有订单终态与仓位 delta 一致，
        或仓位已达到订单理论上不可能超越的完整目标时才返回 result。
        """
        order = self._sanitize_order_ack(
            ccxt_symbol, client_order_id, initial_order) or {}
        order_id = order.get('id')
        tolerance = self._contracts_tolerance(ccxt_symbol)
        last_post_contracts = pre_contracts
        expected_order_side = (
            'buy' if operation == 'open' and side == 'long' else
            'sell' if operation == 'open' else
            'sell' if side == 'long' else 'buy')
        expected_reduce_only = operation == 'close'

        for attempt in range(self.ORDER_CONFIRM_ATTEMPTS):
            order_observed_this_round = False
            terminal = False
            filled = None
            status = None
            try:
                fetched = self._fetch_order_for_confirmation(
                    ccxt_symbol, order_id, client_order_id)
                if isinstance(fetched, dict):
                    if not self._existing_order_matches_request(
                            fetched, ccxt_symbol, expected_order_side,
                            requested_contracts,
                            reduce_only=expected_reduce_only):
                        raise RuntimeError(
                            f'{ccxt_symbol} 订单终态语义与当前 intent 不一致')
                    terminal, filled, status = self._terminal_fill(
                        fetched, requested_contracts, tolerance)
                    # ACK 只提供查询句柄，不提供成交价格/费用证据。
                    order = dict(fetched)
                    order_id = order.get('id')
                    order_observed_this_round = True
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

            # 不把旧 ACK/上一轮订单状态与本轮新持仓快照拼成一份成交证明。
            # 尤其身份错配时，哪怕净仓恰好变化也不能归因给当前 intent。
            if not order_observed_this_round:
                if attempt < self.ORDER_CONFIRM_ATTEMPTS - 1:
                    time.sleep(self.ORDER_CONFIRM_DELAY)
                continue

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

            # 恢复既有 clOrdId 时没有“发单前刚确认 flat”的同轮基线；终态缺
            # filled 也不能拿当前净仓冒充本单成交。无论 delta 是否达到计划量，
            # open 的 filled 缺失/不一致都先进入归因隔离，绝不自动平整仓。
            open_attribution_ambiguous = (
                operation == 'open' and terminal and delta is not None and (
                    filled is None or
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
                filled_matches = (
                    filled is not None and abs(filled - delta) <= tolerance)
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
                    ambiguous = dict(order)
                    for key in ('average', 'cost', 'fee', 'fees'):
                        ambiguous.pop(key, None)
                    ambiguous['execution_ambiguous'] = True
                    return self._confirmed_order_result(
                        ccxt_symbol, ambiguous, requested_contracts, delta,
                        fully_closed=(
                            operation == 'close' and
                            last_post_contracts <= tolerance),
                        source='position-attribution-ambiguous'), last_post_contracts
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
            last_contracts):
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
            pre_contracts=0.0, requested_contracts=contracts)
        if retry_result:
            retry_result['clientOrderId'] = client_order_id
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
                terminal, _filled, _status = self._terminal_fill(
                    final_order, contracts, tolerance)
                final_position = self.get_position(ccxt_symbol)
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
        compensation_remaining = (
            self._finite_nonnegative(compensation.get('remaining_amount'))
            if isinstance(compensation, dict) else None)
        compensation_filled = (
            self._finite_nonnegative(compensation.get('filled'))
            if isinstance(compensation, dict) else None)
        if (compensation and compensation.get('confirmed') is True and
                compensation.get('fully_closed') is True and
                compensation_remaining == 0.0):
            # 补偿归零之后原开仓单仍可能活着并迟到成交。只有同一轮同时看见
            # 原单终态和零仓，才能消费恢复句柄；否则保留 intent + quarantine。
            try:
                final_order = self._fetch_order_for_confirmation(
                    ccxt_symbol, cancel_ref, client_order_id)
                terminal, original_filled, _status = self._terminal_fill(
                    final_order, contracts, tolerance)
                final_position = self.get_position(ccxt_symbol)
            except Exception as exc:
                logger.warning(
                    f'{ccxt_symbol} 补偿全平后原开仓终态复核失败: {exc}')
                terminal = False
                final_position = None
                original_filled = None
            quantities_conserved = (
                original_filled is not None and
                original_filled > tolerance and
                abs(original_filled - last_contracts) <= tolerance and
                compensation_filled is not None and
                compensation_filled > tolerance and
                abs(compensation_filled - last_contracts) <= tolerance and
                abs(compensation_filled - original_filled) <= tolerance)
            if terminal and not final_position and quantities_conserved:
                authoritative = self._sanitize_financial_evidence(final_order)
                result = {
                    'id': authoritative.get('id') or cancel_ref,
                    'clientOrderId': client_order_id,
                    'status': 'compensated_flat',
                    'confirmed': False,
                    'open_execution_compensated': True,
                    'amount': self._contracts_to_coins(
                        ccxt_symbol, original_filled),
                    'remaining_amount': 0.0,
                    'compensation': compensation,
                    'info': '原开仓已终态且 reduce-only 补偿后同轮确认全平',
                }
                for key in ('average', 'cost', 'fee', 'fees',
                            'execution_ambiguous'):
                    if key in authoritative:
                        result[key] = authoritative[key]
                return result
            if terminal and not final_position and not quantities_conserved:
                # flat 只证明当前净仓为零，不证明被补偿的仓全部来自本系统。
                # 原单、补偿单与补偿前净仓三者必须同量，否则可能已经误平
                # 人工同向仓；保留生命周期 blocker，绝不伪造一笔往返。
                logger.critical(
                    f'{ccxt_symbol} 补偿后虽为零仓，但原单成交、补偿成交与'
                    '补偿前净仓不守恒；按开仓归因歧义保留句柄')
                attributable = (
                    original_filled if original_filled is not None and
                    original_filled > tolerance else last_contracts)
                return {
                    'id': cancel_ref,
                    'clientOrderId': client_order_id,
                    'status': 'compensation_attribution_ambiguous',
                    'confirmed': False,
                    'open_execution_attribution_ambiguous': True,
                    'amount': self._contracts_to_coins(
                        ccxt_symbol, attributable),
                    'observed_position_amount': 0.0,
                    'remaining_amount': 0.0,
                    'compensation': compensation,
                    'info': '原开仓/补偿/净仓数量不守恒，拒绝消费执行句柄',
                }
            observed = 0.0
            if final_position and final_position.get('contracts') is not None:
                observed = self._contracts_to_coins(
                    ccxt_symbol, abs(float(final_position['contracts'])))
            logger.critical(
                f'{ccxt_symbol} 补偿曾归零，但原开仓未证明终态/同轮零仓；'
                '保留未决句柄防迟到成交')
            result = {
                'id': cancel_ref,
                'clientOrderId': client_order_id,
                'status': 'post_compensation_unresolved',
                'confirmed': False,
                'amount': self._contracts_to_coins(ccxt_symbol, last_contracts),
                'remaining_amount': observed,
                'compensation': compensation,
                'info': '补偿后原开仓终态与零仓未能同时证明',
            }
            if not terminal:
                result['open_order_may_remain_live'] = True
            elif final_position:
                result['open_execution_attribution_ambiguous'] = True
            else:
                result['open_execution_unresolved'] = True
            return result
        # 畸形补偿回包不能把 None/bool/NaN 继续泄漏给上层。无法严格证明
        # 余仓时保守沿用补偿前最后一次已观测仓位，交给隔离流程接管。
        remaining_amount = (
            compensation_remaining
            if compensation_remaining is not None else
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

    def find_existing_open_order(
            self, symbol, side, amount, client_order_id, *,
            wait_for_visibility=False):
        """只读查询确定性 clOrdId 对应的既有开仓单。

        严格核对 symbol/market/side/amount/reduceOnly。明确 OrderNotFound 返回
        None；查询不确定或字段不一致一律抛出，调用方必须 fail-closed。
        崩溃恢复可要求统一可见性宽限，只有连续查无才返回 None。本方法永不
        创建订单。
        """
        if side not in ('long', 'short'):
            raise ValueError(f'查询既有开仓单时方向非法: {side!r}')
        if client_order_id is None:
            raise ValueError('查询既有开仓单必须提供确定性 clOrdId')
        self._positive_coin_amount(amount, '查询既有开仓单币数')
        ccxt_symbol = self._resolve_symbol(symbol)
        order_side = 'buy' if side == 'long' else 'sell'
        client_order_id = self._client_order_id(client_order_id)
        contracts = self._coin_to_contracts(ccxt_symbol, amount)
        if contracts <= 0:
            raise ValueError(f"{ccxt_symbol} 查询既有开仓单时预期张数无效: {amount}")
        status, order = self._fetch_order_tristate(
            ccxt_symbol, client_order_id,
            wait_for_visibility=wait_for_visibility)
        if status == 'absent':
            return None
        if not self._existing_order_matches_request(
                order, ccxt_symbol, order_side, contracts, reduce_only=False):
            raise RuntimeError(
                f"{ccxt_symbol} clOrdId={client_order_id} 命中订单与 pending 预期不一致")
        # 这是启动 open-intent 恢复使用的公共读取边界。执行身份已在上方
        # 严格绑定；价格/手续费也必须在离开适配层前按与正常成交确认相同的
        # 财务证据契约净化，不能让 main.py 重新猜 OKX 原生字段语义。
        return self._sanitize_financial_evidence(order)

    def open_position(
            self, symbol, side, amount, client_order_id=None, *,
            require_existing=False):
        """安全开仓（市价单）。amount 单位为币数。

        开仓前必须可证明交易所为空仓；下单 ACK 后必须确认订单终态与仓位
        delta。终态部分成交会按实际币数返回，由上层按实际量挂止损/记账。
        ``require_existing`` 是 pending/孤儿仓恢复的最终写边界：只按已持久化
        clOrdId 找回旧单，经过可见性宽限仍 absent 也绝不创建新订单。
        """
        if side not in ('long', 'short'):
            logger.error(f'拒绝非法开仓方向: {side!r}')
            return None
        try:
            self._positive_coin_amount(amount, '开仓币数')
        except ValueError as exc:
            logger.error(f'拒绝非法开仓币数: {amount!r}: {exc}')
            return None
        if not isinstance(require_existing, bool):
            logger.error('require_existing 必须是严格布尔值')
            return None
        if require_existing and client_order_id is None:
            logger.error('只读恢复开仓必须提供已持久化 clOrdId')
            return None
        supplied_client_id = client_order_id is not None
        try:
            client_order_id = self._client_order_id(client_order_id)
        except ValueError as e:
            logger.error(f"拒绝非法幂等订单号: {e}")
            return None
        ccxt_symbol = self._resolve_symbol(symbol)
        order_side = 'buy' if side == 'long' else 'sell'

        contracts = self._coin_to_contracts(ccxt_symbol, amount)
        if contracts <= 0:
            logger.error(f"{ccxt_symbol} 开仓张数为0（amount={amount}币，不足一张），放弃开仓")
            return None

        # 信号层提供确定性 clOrdId 时，重试/重启先查询同一订单，绝不再次 create。
        # 三态裁决：OrderNotFound=确定不存在（可新发单）；命中且一致=复用；
        # 响应畸形/查询失败=无法裁决，一律拒绝新 POST。
        if supplied_client_id:
            try:
                status, existing = self._fetch_order_tristate(
                    ccxt_symbol, client_order_id,
                    wait_for_visibility=require_existing)
            except Exception as e:
                logger.error(f"{ccxt_symbol} 幂等开仓订单查询无法裁决，拒绝新下单: {e}")
                return None
            if status == 'found':
                if not self._existing_order_matches_request(
                        existing, ccxt_symbol, order_side, contracts, reduce_only=False):
                    return None
                result, _last = self._confirm_market_order(
                    ccxt_symbol, existing, client_order_id, operation='open', side=side,
                    pre_contracts=0.0, requested_contracts=contracts)
                if result:
                    result['clientOrderId'] = client_order_id
                    logger.info(f"{ccxt_symbol} 命中既有幂等开仓订单 {client_order_id}，未重复下单")
                    return result
                return self._resolve_unconfirmed_open(
                    ccxt_symbol, side, contracts, client_order_id,
                    existing, _last)
            if require_existing:
                logger.critical(
                    f'{ccxt_symbol} pending 恢复 clOrdId={client_order_id} '
                    '经可见性宽限仍不存在；只读恢复边界禁止重放旧信号 POST')
                return None

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
            ack = self._sanitize_order_ack(
                ccxt_symbol, client_order_id, ack)
        except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
            # clOrdId 在发单前生成；即使 HTTP ACK 丢失也能查询同一订单，禁止重下。
            logger.warning(f"开仓请求超时: {e}，按 clOrdId={client_order_id} 查询终态")
        except (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.BadRequest) as e:
            logger.error(f"开仓业务异常: {e}")
            return None
        except Exception as e:
            # create_order 的任意未知异常都可能发生在请求已到达交易所、响应
            # 尚未返回之后；已有确定性 clOrdId，必须继续查询/补偿，不能当未发单。
            logger.warning(
                f"开仓未知异常（结果不确定）: {e}，"
                f"按 clOrdId={client_order_id} 查询终态")

        result, last_contracts = self._confirm_market_order(
            ccxt_symbol, ack, client_order_id, operation='open', side=side,
            pre_contracts=0.0, requested_contracts=contracts)
        if result:
            result['clientOrderId'] = client_order_id
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

    def close_position(
            self, symbol, side, amount, client_order_id=None, *,
            require_existing=False):
        """安全平仓（市价单，reduce-only）。amount 单位为币数。

        返回的 ``amount`` 是已确认实际成交币数；``fully_closed`` 只有在
        交易所净持仓已归零时为 True。带持久化 clOrdId 的恢复还必须找到
        该确定性订单的终态证据；上层不得用一次 flat 或部分成交删除完整账本。
        """
        if not isinstance(require_existing, bool):
            logger.error('require_existing 必须是严格布尔值')
            return None
        if require_existing and client_order_id is None:
            logger.error('只读恢复平仓必须提供已持久化 clOrdId')
            return None
        if side not in ('long', 'short'):
            logger.error(f'拒绝非法平仓方向: {side!r}')
            return None
        try:
            self._positive_coin_amount(amount, '平仓币数')
        except ValueError as exc:
            logger.error(f'拒绝非法平仓币数: {amount!r}: {exc}')
            return None
        supplied_client_id = client_order_id is not None
        try:
            client_order_id = self._client_order_id(client_order_id)
        except ValueError as e:
            logger.error(f"拒绝非法幂等订单号: {e}")
            return None
        ccxt_symbol = self._resolve_symbol(symbol)
        close_side = 'sell' if side == 'long' else 'buy'

        try:
            pre_position = self.get_position(ccxt_symbol)
        except Exception as e:
            logger.error(f"{ccxt_symbol} 平仓前持仓查询失败，无法建立成交基线: {e}")
            return None

        pre_contracts = abs(float(pre_position['contracts'])) if pre_position and pre_position.get('contracts') else 0.0

        if pre_contracts == 0 and not supplied_client_id:
            logger.warning(f"{ccxt_symbol} 欧易端已无持仓，跳过平仓指令（可能已手动平仓）")
            return {
                'id': 'already_closed', 'average': None, 'status': 'closed',
                'amount': 0.0, 'requested_amount': amount, 'confirmed': True,
                'fully_filled': True, 'fully_closed': True,
                'remaining_amount': 0.0,
            }

        actual_side = self._position_side(pre_position)
        if actual_side and actual_side != side:
            logger.critical(
                f"{ccxt_symbol} 拒绝按错误方向平仓: 本地请求={side}, 交易所={actual_side}")
            return None

        requested_contracts = self._coin_to_contracts(ccxt_symbol, amount)
        if requested_contracts <= 0:
            logger.error(
                f'{ccxt_symbol} 平仓币数不足一张或无效: {amount!r}')
            return None
        tolerance = self._contracts_tolerance(ccxt_symbol)
        flat_before_recovery = pre_contracts <= tolerance
        existing_order = None
        if supplied_client_id:
            try:
                existing_order = self._find_existing_close_order(
                    ccxt_symbol, close_side, requested_contracts,
                    client_order_id,
                    # 上层只会对本调用刚创建的 intent 传 false。
                    # fresh 路径对几乎不可能碰撞的新 clOrdId 只预查
                    # 一次，不白等历史可见窗；任何已有 intent 传
                    # require_existing=true，必须跨完整可见窗只读裁决。
                    wait_for_visibility=require_existing)
            except Exception as e:
                logger.error(
                    f"{ccxt_symbol} 幂等平仓订单查询失败，拒绝新下单: {e}")
                return None
            if existing_order is None and flat_before_recovery:
                logger.warning(
                    f"{ccxt_symbol} POST 前已 flat，且持久化单一 "
                    f"clOrdId={client_order_id} 经可见性裁决查无；"
                    "本调用明确零 POST，交上层按已知保护止损只读归因")
                return {
                    'confirmed': False,
                    'definitely_no_post': True,
                    'position_flat_before_post': True,
                    'clientOrderId': client_order_id,
                    'requested_amount': amount,
                    'read_only_evidence': True,
                }
            if existing_order is None and require_existing:
                try:
                    refreshed_position = self.get_position(ccxt_symbol)
                except Exception as exc:
                    logger.error(
                        f'{ccxt_symbol} 历史 close intent 查无后无法复查持仓: '
                        f'{exc}')
                    return None
                raw_contracts = (
                    refreshed_position.get('contracts')
                    if isinstance(refreshed_position, dict) else None)
                refreshed_side = self._position_side(refreshed_position)
                if (isinstance(raw_contracts, bool) or
                        raw_contracts is None):
                    return None
                try:
                    refreshed_contracts = abs(float(raw_contracts))
                except (TypeError, ValueError, OverflowError):
                    return None
                if (not math.isfinite(refreshed_contracts) or
                        refreshed_side != side or
                        abs(refreshed_contracts - requested_contracts) >
                        tolerance):
                    logger.critical(
                        f'{ccxt_symbol} 历史 close intent 查无，但 fresh 仓位'
                        '不再与计划方向/数量精确一致；保留 intent')
                    return None
                return {
                    'confirmed': False,
                    'definitely_no_post': True,
                    'close_order_absent': True,
                    'position_unchanged': True,
                    'amount': 0.0,
                    'remaining_amount': amount,
                    'requested_amount': amount,
                    'clientOrderId': client_order_id,
                    'read_only_evidence': True,
                }
            if existing_order is not None:
                try:
                    refreshed_position = self.get_position(ccxt_symbol)
                except Exception as e:
                    logger.error(
                        f'{ccxt_symbol} 幂等订单终态找到后无法复查当前持仓: {e}')
                    return None
                refreshed_contracts = (
                    abs(float(refreshed_position['contracts']))
                    if (refreshed_position and
                        refreshed_position.get('contracts') is not None)
                    else 0.0)
                refreshed_side = self._position_side(refreshed_position)
                if refreshed_side and refreshed_side != side:
                    logger.critical(
                        f'{ccxt_symbol} 幂等订单终态找到后持仓方向反转为 '
                        f'{refreshed_side}，拒绝继续/消费 close intent')
                    return None
                if flat_before_recovery and refreshed_contracts > tolerance:
                    logger.critical(
                        f'{ccxt_symbol} 初始 flat，但幂等订单终态找到后持仓已'
                        f'重新出现 {refreshed_contracts} 张，拒绝继续/消费 close intent')
                    return None
                pre_contracts = refreshed_contracts

        if (supplied_client_id and existing_order is None and
                pre_contracts > tolerance and requested_contracts > tolerance and
                abs(pre_contracts - requested_contracts) > tolerance):
            logger.critical(
                f'{ccxt_symbol} 新 close intent 的账本计划量 '
                f'{requested_contracts} 张与交易所当前 {pre_contracts} 张不一致；'
                '拒绝猜测哪部分属于系统，先隔离对账')
            return None
        # 新请求不得超平；崩溃恢复只读唯一持久化订单的
        # 终态 filled，并与“原 intent 仓位 - 当前余仓”交叉证明。
        if existing_order is not None:
            target_contracts = requested_contracts
            recovered_filled = existing_order[3]
            observed_delta = max(0.0, requested_contracts - pre_contracts)
            if abs(recovered_filled - observed_delta) > tolerance:
                logger.critical(
                    f'{ccxt_symbol} 平仓恢复归因不一致: 唯一订单 filled='
                    f'{recovered_filled}, intent→当前仓位 delta={observed_delta}')
                # 一次 flat 也不能填补确定性订单缺失的成交量。差额可能来自
                # 止损或人工成交；无论是否仍有余仓，都保留
                # close intent/quarantine，禁止伪造 fully_closed 或继续补平。
                return None
            total_filled_contracts = recovered_filled
            last_contracts = pre_contracts
            close_execution = (
                (existing_order[1], existing_order[3])
                if existing_order[3] > tolerance else None)
            logger.info(
                f'{ccxt_symbol} 命中唯一幂等平仓单，'
                '已确认真实终态且未重复下单')
        else:
            target_contracts = (
                min(requested_contracts, pre_contracts)
                if requested_contracts > 0 else pre_contracts)
            total_filled_contracts = 0.0
            last_contracts = pre_contracts
            close_execution = None

        if require_existing:
            if (existing_order is not None and
                    total_filled_contracts <= tolerance):
                return {
                    'confirmed': True, 'zero_fill_terminal': True,
                    'fully_closed': False, 'fully_filled': False,
                    'amount': 0.0, 'requested_amount': amount,
                    'remaining_amount': amount,
                    'clientOrderId': client_order_id,
                    'status': 'no_fill', 'read_only_evidence': True,
                }
            if close_execution is None:
                logger.critical(
                    f'{ccxt_symbol} 历史 close intent 的确定性订单查无；'
                    '只读恢复禁止 POST，保留 intent 等待人工裁决')
                return None
            result = self._build_close_result(
                ccxt_symbol, close_execution[0], target_contracts,
                total_filled_contracts, last_contracts)
            result['status'] = (
                'closed' if result['fully_closed'] else 'partial')
            return result

        # 每个 intent 最多发一张单。市价单若部分成交，上层先
        # 原子落余仓/止损并消费旧 intent，下次业务动作再用
        # 新 intent。恢复命中旧单时上面已直接返回，绝不补发。
        if existing_order is None:
            remaining_target = min(
                max(0.0, target_contracts - total_filled_contracts),
                last_contracts)
            if remaining_target <= tolerance:
                return None
            # 幂等订单查询/确认可能等待数秒；每一笔新的 reduce-only POST 紧前
            # 都重新读取净仓。flat、方向变化、数量漂移或畸形响应均表示原
            # intent 已无法独占归因，必须保留 intent，不能平掉期间出现的仓位。
            try:
                fresh_position = self.get_position(ccxt_symbol)
            except Exception as exc:
                logger.error(
                    f'{ccxt_symbol} 平仓订单 POST 紧前'
                    f'持仓复核失败，拒绝下单: {exc}')
                return None
            fresh_side = self._position_side(fresh_position)
            raw_fresh_contracts = (
                fresh_position.get('contracts')
                if isinstance(fresh_position, dict) else None)
            if fresh_position is None:
                fresh_contracts = 0.0
            elif (isinstance(raw_fresh_contracts, bool) or
                    raw_fresh_contracts is None):
                logger.critical(
                    f'{ccxt_symbol} 平仓订单 POST 紧前'
                    '持仓数量缺失/非法，拒绝下单')
                return None
            else:
                try:
                    fresh_contracts = abs(float(raw_fresh_contracts))
                except (TypeError, ValueError, OverflowError):
                    logger.critical(
                        f'{ccxt_symbol} 平仓订单 POST 紧前'
                        '持仓数量不可解析，拒绝下单')
                    return None
            if not math.isfinite(fresh_contracts):
                logger.critical(
                    f'{ccxt_symbol} 平仓订单 POST 紧前'
                    '持仓数量非有限，拒绝下单')
                return None
            if (fresh_contracts <= tolerance or fresh_side != side or
                    abs(fresh_contracts - last_contracts) > tolerance):
                logger.critical(
                    f'{ccxt_symbol} 平仓订单等待期间仓位变化：'
                    f'expected={side}/{last_contracts}张, '
                    f'actual={fresh_side}/{fresh_contracts}张；拒绝 POST')
                return None
            last_contracts = fresh_contracts
            remaining_target = min(
                max(0.0, target_contracts - total_filled_contracts),
                last_contracts)
            if remaining_target <= tolerance:
                return None
            order_client_id = client_order_id
            params = self._order_params(
                reduce_only=True, extra={'clOrdId': order_client_id})
            ack = None
            try:
                ack = self.exchange.create_order(
                    ccxt_symbol, 'market', close_side, remaining_target, None, params)
                ack = self._sanitize_order_ack(
                    ccxt_symbol, order_client_id, ack)
            except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
                logger.warning(
                    f"平仓请求超时: {e}，"
                    f"按 clOrdId={order_client_id} 查询终态")
            except (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.BadRequest) as e:
                logger.error(f"平仓业务拒绝: {e}")
            except Exception as e:
                # 未知异常可能发生在 POST 已抵达、响应解析失败之后；与超时
                # 一样只能按已持久化 clOrdId 查询/撤余量，绝不能假定未下单。
                logger.warning(
                    f"平仓结果不确定: {e}，"
                    f"按 clOrdId={order_client_id} 查询终态")
            order_pre_contracts = last_contracts
            order_result, last_contracts = self._confirm_market_order(
                ccxt_symbol, ack, order_client_id, operation='close', side=side,
                pre_contracts=order_pre_contracts,
                requested_contracts=remaining_target)
            if not order_result:
                cancel_ref = ack.get('id') if isinstance(ack, dict) else None
                try:
                    if cancel_ref:
                        self.exchange.cancel_order(cancel_ref, ccxt_symbol)
                    else:
                        self.exchange.cancel_order(
                            order_client_id, ccxt_symbol,
                            params={'clOrdId': order_client_id})
                except Exception as e:
                    logger.warning(
                        f"{ccxt_symbol} 平仓订单撤余量未确认: {e}")
                order_result, last_contracts = self._confirm_market_order(
                    ccxt_symbol, ack, order_client_id, operation='close', side=side,
                    pre_contracts=order_pre_contracts,
                    requested_contracts=remaining_target)
                if not order_result:
                    logger.error(
                        f"平仓成交无法确认: {ccxt_symbol} {side}")
                    return None
            order_result['clientOrderId'] = order_client_id
            if not order_result.get('id'):
                order_result['id'] = 'timeout_confirmed'
            actual_filled_contracts = max(
                0.0, order_pre_contracts - last_contracts)
            if actual_filled_contracts <= tolerance:
                return {
                    'confirmed': True, 'zero_fill_terminal': True,
                    'fully_closed': False, 'fully_filled': False,
                    'amount': 0.0, 'requested_amount': amount,
                    'remaining_amount': amount,
                    'clientOrderId': order_client_id,
                    'status': 'no_fill',
                }
            total_filled_contracts += actual_filled_contracts
            close_execution = (order_result, actual_filled_contracts)
            if last_contracts > tolerance:
                logger.warning(
                    f"{ccxt_symbol} 单一平仓 intent 仅成交 "
                    f"{actual_filled_contracts}/{remaining_target} 张；"
                    "返回 partial，不在本调用补发第二单")

        if close_execution is None:
            logger.error(f"平仓成交无法确认: {ccxt_symbol} {side} {amount}")
            return None

        result = self._build_close_result(
            ccxt_symbol, close_execution[0], target_contracts,
            total_filled_contracts, last_contracts)

        if result['fully_closed']:
            result['status'] = 'closed'
            logger.info(
                f"平仓成交已确认且仓位归零: {ccxt_symbol} {side} "
                f"{result['amount']}币, 订单={result['ids']}")
        else:
            logger.critical(
                f"{ccxt_symbol} 单一订单终态后仍剩 {result['remaining_amount']}币；"
                f"返回实际部分成交，调用方必须原子缩减账本并重挂余仓止损")
        return result

    def _build_close_result(self, ccxt_symbol, order, target_contracts,
                            filled_contracts, remaining_contracts):
        """把唯一平仓订单与净仓守恒证据归一为上层契约。"""
        result = self._sanitize_financial_evidence(order)
        tolerance = self._contracts_tolerance(ccxt_symbol)
        public_id = self._public_order_id(result.get('id'))
        result['ids'] = [public_id] if public_id else []
        client_id = result.get('clientOrderId')
        result['clientOrderIds'] = [client_id] if client_id else []
        result['amount'] = self._contracts_to_coins(
            ccxt_symbol, filled_contracts)
        result['requested_amount'] = self._contracts_to_coins(
            ccxt_symbol, target_contracts)
        result['filled'] = filled_contracts
        result['confirmed'] = True
        result['fully_filled'] = (
            filled_contracts + tolerance >= target_contracts)
        result['fully_closed'] = remaining_contracts <= tolerance
        result['remaining_amount'] = (
            0.0 if result['fully_closed'] else
            self._contracts_to_coins(ccxt_symbol, remaining_contracts))
        result['price_complete'] = (
            result.get('financial_evidence_incomplete') is not True and
            result.get('execution_ambiguous') is not True and
            self._finite_nonnegative(result.get('average')) not in (None, 0.0))
        result['cost_complete'] = (
            self._finite_nonnegative(result.get('cost')) is not None)
        result['fees_complete'] = bool(
            isinstance(result.get('fee'), dict) or
            (isinstance(result.get('fees'), list) and result.get('fees')))
        return result

    def find_compensation_close_progress(self, symbol, side, amount,
                                         open_client_order_id):
        """只读返回唯一补偿订单进度；absent 与 terminal-partial 明确区分。"""
        if side not in ('long', 'short'):
            raise ValueError(f'查询补偿平仓进度时方向非法: {side!r}')
        self._positive_coin_amount(amount, '查询补偿平仓进度币数')
        if open_client_order_id is None:
            raise ValueError('查询补偿平仓进度必须提供开仓 clOrdId')
        ccxt_symbol = self._resolve_symbol(symbol)
        close_side = 'sell' if side == 'long' else 'buy'
        base_client_id = self.compensation_client_order_id(open_client_order_id)
        requested_contracts = self._coin_to_contracts(ccxt_symbol, amount)
        if requested_contracts <= 0:
            raise ValueError(
                f"{ccxt_symbol} 查询补偿平仓进度时预期张数无效: {amount}")
        existing_order = self._find_existing_close_order(
            ccxt_symbol, close_side, requested_contracts, base_client_id,
            wait_for_visibility=True)
        if existing_order is None:
            return {
                # 确定性 ID 的 absent 是可见性事实，不是历史零成交终态；
                # 是否仍在交易所留存证明窗内只能由持久化 lifecycle 时间裁决。
                'terminal': None, 'absent': True, 'confirmed': False,
                'filled': 0.0, 'amount': 0.0,
                'requested_amount': amount,
                'remaining_amount': amount,
                'clientOrderId': base_client_id,
                'ids': [], 'read_only_evidence': True,
                'order': None,
                'order_state': {
                    'client_order_id': base_client_id,
                    'presence': 'absent', 'terminal': None,
                    'filled': None,
                },
            }
        tolerance = self._contracts_tolerance(ccxt_symbol)
        raw_filled_contracts = existing_order[3]
        # _terminal_fill 允许的只是交易所精度噪声。必须在公共 progress
        # 契约生成点一次性钳平，否则 top/state/order 可能各自携带 0、微量
        # 或 near-full 三种值，让上层无法严格证明同一张订单的数量守恒。
        if raw_filled_contracts <= tolerance:
            filled_contracts = 0.0
        elif requested_contracts - raw_filled_contracts <= tolerance:
            filled_contracts = requested_contracts
        else:
            filled_contracts = raw_filled_contracts
        remaining_contracts = max(
            0.0, requested_contracts - filled_contracts)
        canonical_order = dict(existing_order[1])
        canonical_order.update({
            'amount': requested_contracts,
            'filled': filled_contracts,
            'remaining': remaining_contracts,
        })
        raw_info = canonical_order.get('info')
        if isinstance(raw_info, dict):
            canonical_info = dict(raw_info)
            if 'sz' in canonical_info:
                canonical_info['sz'] = requested_contracts
            if 'accFillSz' in canonical_info:
                canonical_info['accFillSz'] = filled_contracts
            canonical_order['info'] = canonical_info
        if filled_contracts > 0.0:
            result = self._build_close_result(
                ccxt_symbol, canonical_order, requested_contracts,
                filled_contracts,
                remaining_contracts)
        else:
            result = {
                'confirmed': True, 'filled': 0.0, 'amount': 0.0,
                'requested_amount': amount,
                'remaining_amount': amount, 'ids': [],
            }
        financial_order = {
            **self._sanitize_financial_evidence(canonical_order),
            'filled_contracts': filled_contracts,
            'filled_amount': self._contracts_to_coins(
                ccxt_symbol, filled_contracts),
        }
        result.update({
            'terminal': True,
            'absent': False,
            'clientOrderId': base_client_id,
            'status': (
                'closed' if filled_contracts == requested_contracts
                else 'partial'),
            'read_only_evidence': True,
            'order': financial_order,
            'order_state': {
                'client_order_id': existing_order[0],
                'presence': 'present', 'terminal': True,
                'filled': filled_contracts,
                'remaining': remaining_contracts,
            },
        })
        return result

    def find_compensation_close_evidence(self, symbol, side, amount,
                                         open_client_order_id):
        """只读找回由开仓句柄派生的唯一补偿订单；绝不发送下单请求。

        前置条件：调用方已确认交易所该品种净持仓为零。用途是判断历史补偿
        平仓是否真实发生过，以便用真实退出价补记往返——此前该场景误用可
        下单的 close_position()，极端竞态下（确认空仓后用户人工开出同方向
        同数量仓位）会把人工仓平掉。返回：
          - None — 可见性宽限后仍明确不存在补偿订单，或成交未覆盖请求量；
          - dict — 唯一订单终态且覆盖请求量的结果（average/fees/ids）。
        查询不确定或订单内容与请求不一致一律抛出。
        """
        progress = self.find_compensation_close_progress(
            symbol, side, amount, open_client_order_id)
        if progress.get('absent') or progress.get('status') != 'closed':
            return None
        return progress

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
        if expected_order_id is not None:
            try:
                observed_id = OkxApi._public_order_id(order.get('id'))
                expected_order_id = OkxApi._public_order_id(expected_order_id)
            except ValueError:
                return False
            if observed_id != expected_order_id:
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
        if any(isinstance(value, bool) for value in (
                trigger, stop_price, order.get('amount'), info.get('sz'),
                contracts)):
            return False
        try:
            trigger = float(trigger)
            stop_price = float(stop_price)
        except (TypeError, ValueError):
            return False
        if (not (math.isfinite(trigger) and math.isfinite(stop_price)) or
                trigger <= 0 or stop_price <= 0):
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
        if (not (math.isfinite(amount) and math.isfinite(contracts)) or
                amount <= 0 or contracts <= 0):
            return False
        return abs(amount - contracts) <= max(math.ulp(amount), math.ulp(contracts)) * 4

    @classmethod
    def _algo_client_order_id(cls, order):
        if not order:
            return None
        info = order.get('info') or {}
        value = (order.get('clientOrderId') or info.get('algoClOrdId')
                 or info.get('clientOrderId'))
        if value in (None, ''):
            return None
        try:
            return cls._client_order_id(value)
        except ValueError as exc:
            raise RuntimeError('算法单返回非法 algoClOrdId') from exc

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
        if client_order_id is None:
            raise ValueError('止损确认必须提供确定性 algoClOrdId')
        client_order_id = self._client_order_id(client_order_id)
        same_client = [
            order for order in algos
            if self._algo_client_order_id(order) == client_order_id]
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
        输入、精度元数据或格式化结果只要不可证明为有限正数就拒绝；止损价绝不
        以未经交易所 tick 格式化的原值 fail-open。
        """
        if isinstance(stop_price, bool):
            raise ValueError(f'{ccxt_symbol} 止损价不能是布尔值')
        try:
            numeric_price = float(stop_price)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f'{ccxt_symbol} 止损价不是有效数字') from exc
        if not math.isfinite(numeric_price) or numeric_price <= 0:
            raise ValueError(f'{ccxt_symbol} 止损价必须是有限正数')
        try:
            aligned_raw = self.exchange.price_to_precision(
                ccxt_symbol, numeric_price)
        except Exception as exc:
            raise ValueError(
                f'{ccxt_symbol} 止损价无法按交易所 tick 对齐') from exc
        if isinstance(aligned_raw, bool):
            raise ValueError(
                f'{ccxt_symbol} price_to_precision 返回布尔值')
        try:
            aligned_price = float(aligned_raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f'{ccxt_symbol} price_to_precision 返回非法价格') from exc
        if not math.isfinite(aligned_price) or aligned_price <= 0:
            raise ValueError(
                f'{ccxt_symbol} price_to_precision 返回非有限或非正价格')
        return aligned_price

    def create_stop_loss_order(self, symbol, side, amount, stop_price,
                               client_order_id=None, *,
                               require_existing=False):
        """幂等创建止损算法单（reduce-only，触发后市价平仓）。

        同一保护意图使用固定 ``algoClOrdId``，发单前先查、发单后再按该 ID
        严格确认。POST 全程最多一次：超时或查询不确定时绝不盲重发。
        amount 单位为币数。
        """
        if not isinstance(require_existing, bool):
            logger.error('require_existing 必须是布尔值')
            return None
        if side not in ('long', 'short'):
            logger.error(f'拒绝非法止损方向: {side!r}')
            return None
        try:
            self._positive_coin_amount(amount, '止损币数')
        except ValueError as exc:
            logger.error(f'拒绝非法止损币数: {amount!r}: {exc}')
            return None
        if client_order_id is not None:
            try:
                client_order_id = self._client_order_id(client_order_id)
            except ValueError as exc:
                logger.error(f'拒绝非法止损幂等 ID: {exc}')
                return None
        ccxt_symbol = self._resolve_symbol(symbol)
        stop_side = 'sell' if side == 'long' else 'buy'
        stop_price = self._align_stop_price(ccxt_symbol, stop_price)

        contracts = self._coin_to_contracts(ccxt_symbol, amount)
        if contracts <= 0:
            logger.error(
                f"{ccxt_symbol} 止损币数不足一张，拒绝猜测/回退为整仓止损")
            return None

        try:
            algo_client_id = self._stop_client_order_id(
                ccxt_symbol, stop_side, contracts, stop_price, client_order_id)
        except ValueError as e:
            logger.error(f"{ccxt_symbol} 非法止损幂等 ID，拒绝发单: {e}")
            return None

        # 重启/上轮超时恢复：先按固定 algoClOrdId 查现有单，命中即复用。
        # 只读恢复必须跨完整可见性窗口连续查无，才可返回 absent；即便如此也
        # 绝不 POST。普通首次创建只做一次权威预查，因为上层已经先落盘写入句柄。
        lookup_attempts = (
            self.STOP_CONFIRM_ATTEMPTS if require_existing else 1)
        for attempt in range(lookup_attempts):
            try:
                existing = self._find_stop_by_client_id(
                    self._fetch_algo_orders(ccxt_symbol), algo_client_id,
                    stop_side, stop_price, contracts)
            except Exception as e:
                # 查询不完整时无法证明“尚未创建”，绝不 POST。
                logger.error(
                    f"{ccxt_symbol} 止损幂等预查失败，拒绝盲发: {e}")
                return None
            if existing:
                logger.info(
                    f"复用既有止损单: {ccxt_symbol} algoClOrdId={algo_client_id}, "
                    f"订单ID={existing.get('id')}")
                return existing
            if require_existing and attempt < lookup_attempts - 1:
                time.sleep(self.STOP_CONFIRM_DELAY)
        if require_existing:
            logger.warning(
                f'{ccxt_symbol} 只读恢复跨可见性窗口未找到 '
                f'algoClOrdId={algo_client_id}；拒绝 POST')
            return None

        # 张数已由 _coin_to_contracts 唯一一次按 amount step 格式化并严格验证；
        # 触发价也已由 _align_stop_price 唯一一次按 tick 格式化并严格验证。
        # POST 必须复用这两个值，不能在预查之后第二次调用 formatter 造成
        # 幂等 ID / 预查内容与真正发单内容之间的 TOCTOU 分叉。
        size_text = str(contracts)
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
            if (isinstance(response, dict) and
                    response.get('code') not in (None, '', '0')):
                logger.error(
                    f"止损单被交易所明确拒绝: {str(response)[:240]}")
                return None
            if (isinstance(item, dict) and
                    item.get('sCode') not in (None, '', '0')):
                logger.error(
                    f"止损单被交易所明确拒绝: {str(item)[:240]}")
                return None
            if (not isinstance(response, dict) or response.get('code') != '0'
                    or not isinstance(item, dict) or
                    not (item.get('algoId') or item.get('algoClOrdId'))):
                raise RuntimeError(f"OKX 止损 ACK 异常: {str(response)[:240]}")
        except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
            logger.warning(
                f"止损 POST 超时（不会重发）: {ccxt_symbol} "
                f"algoClOrdId={algo_client_id}: {e}")
        except (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.BadRequest) as e:
            logger.error(f"止损单业务异常: {e}")
            return None
        except Exception as e:
            # 任意未知异常/畸形 ACK 都可能发生在交易所已受理之后。固定
            # algoClOrdId 足以只读裁决，绝不能把“不确定”当“未创建”。
            logger.warning(
                f"止损单创建结果不确定（不会重发），按 algoClOrdId 查询: {e}")

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
                raise PositionModeError(
                    '持仓清单包含 None 条目，完整性不确定')
            if not isinstance(p, dict):
                raise PositionModeError(
                    f"持仓清单条目结构异常: {type(p).__name__}")
            info = p.get('info')
            if info is None:
                info = {}
            if not isinstance(info, dict):
                raise PositionModeError(
                    f'持仓清单 info 结构异常: {type(info).__name__}')
            observed_symbol = p.get('symbol')
            observed_inst = info.get('instId')
            if not observed_symbol and not observed_inst:
                raise PositionModeError(
                    f"持仓清单条目缺少品种标识，拒绝裁决: {str(p)[:120]}")
            # OKX fetch_positions() 默认可混入 MARGIN/FUTURES/OPTION。系统只
            # 托管 SWAP；有权威 instType 时先排除边界外产品，避免把交割合约
            # 或期权的合法 symbol 误报成“未知永续”。
            inst_type = str(info.get('instType') or '').upper()
            if inst_type in {'MARGIN', 'FUTURES', 'OPTION', 'SPOT'}:
                # 产品类型与原生/USDT 标准化身份矛盾时不能相信任一侧并静默
                # 跳过；否则真实 U 本位永续仓可能从孤儿仓门禁中隐身。
                native_swap_identity = any(
                    isinstance(value, str) and
                    re.fullmatch(r'[A-Z0-9]{1,20}-[A-Z0-9]{2,20}-SWAP', value)
                    for value in (observed_symbol, observed_inst))
                standard_usdt_swap_identity = (
                    isinstance(observed_symbol, str) and
                    re.fullmatch(
                        r'[A-Z0-9]{1,20}/USDT:USDT', observed_symbol))
                if native_swap_identity or standard_usdt_swap_identity:
                    raise PositionModeError(
                        '持仓产品类型与 SWAP 身份矛盾，拒绝漏报: '
                        f'instType={inst_type!r}, symbol={observed_symbol!r}, '
                        f'instId={observed_inst!r}')
                continue
            if inst_type and inst_type != 'SWAP':
                raise PositionModeError(
                    f'持仓清单包含未知产品类型 instType={inst_type!r}，拒绝漏报')

            ccxt_symbol = None
            standard_usdt = (
                isinstance(observed_symbol, str) and
                re.fullmatch(r'[A-Z0-9]{1,20}/USDT:USDT', observed_symbol))
            raw_symbol_usdt = (
                isinstance(observed_symbol, str) and
                re.fullmatch(r'[A-Z0-9]{1,20}-USDT-SWAP', observed_symbol))
            raw_inst_usdt = (
                isinstance(observed_inst, str) and
                re.fullmatch(r'[A-Z0-9]{1,20}-USDT-SWAP', observed_inst))
            if standard_usdt:
                ccxt_symbol = str(observed_symbol)
                if (observed_inst is not None and
                        str(observed_inst) != self._to_inst_id(ccxt_symbol)):
                    raise PositionModeError(
                        f'{ccxt_symbol} 标准 symbol 与 instId={observed_inst!r} 矛盾')
            elif raw_inst_usdt or raw_symbol_usdt:
                raw_id = observed_inst if raw_inst_usdt else observed_symbol
                if (observed_inst is not None and observed_symbol is not None and
                        str(observed_inst) != str(observed_symbol)):
                    raise PositionModeError(
                        f'持仓清单原生标识冲突: symbol={observed_symbol!r}, '
                        f'instId={observed_inst!r}')
                base = raw_id[:-len('-USDT-SWAP')]
                ccxt_symbol = f'{base}/USDT:USDT'
            elif (str(observed_inst or '').endswith('-USDT-SWAP') or
                  str(observed_symbol or '').endswith(':USDT') or
                  str(observed_symbol or '').endswith('-USDT-SWAP')):
                raise PositionModeError(
                    f'持仓清单 U 本位条目标识不可一致解析: '
                    f'symbol={observed_symbol!r}, instId={observed_inst!r}')
            else:
                # 只有格式明确的其它结算币永续才可判为系统边界外。未知标识
                # 若数量非零，绝不能以“不像 USDT”静默跳过，否则孤儿仓会隐身。
                standard_other_swap = (
                    isinstance(observed_symbol, str) and
                    re.fullmatch(
                        r'[A-Z0-9]{1,20}/[A-Z0-9]{2,20}:[A-Z0-9]{1,20}',
                        observed_symbol) is not None)
                raw_other_swap = any(
                    isinstance(value, str) and
                    re.fullmatch(
                        r'[A-Z0-9]{1,20}-[A-Z0-9]{2,20}-SWAP', value)
                    for value in (observed_symbol, observed_inst))
                if standard_other_swap or raw_other_swap:
                    def other_identity(value):
                        if not isinstance(value, str):
                            return None
                        standard = re.fullmatch(
                            r'([A-Z0-9]{1,20})/([A-Z0-9]{2,20}):'
                            r'[A-Z0-9]{1,20}', value)
                        native = re.fullmatch(
                            r'([A-Z0-9]{1,20})-([A-Z0-9]{2,20})-SWAP',
                            value)
                        match = standard or native
                        return match.groups()[:2] if match else None

                    identities = [
                        identity for identity in (
                            other_identity(observed_symbol),
                            other_identity(observed_inst))
                        if identity is not None]
                    if len(set(identities)) > 1:
                        raise PositionModeError(
                            '持仓清单边界外永续标识互相矛盾: '
                            f'symbol={observed_symbol!r}, '
                            f'instId={observed_inst!r}')
                    continue
                identity = str(observed_symbol or observed_inst)
                if self._position_entry_is_nonzero(p, info, identity):
                    raise PositionModeError(
                        '持仓清单包含无法识别的非零仓位，拒绝漏报: '
                        f'symbol={observed_symbol!r}, instId={observed_inst!r}')
                continue

            if not self._position_entry_is_nonzero(p, info, ccxt_symbol):
                continue
            internal = self.to_internal_symbol(ccxt_symbol)
            if internal in symbols:
                raise PositionModeError(
                    f'{ccxt_symbol} 出现多条非零持仓，拒绝隐藏任何一腿')
            symbols.append(internal)
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
        if side not in ('long', 'short'):
            raise ValueError(f'止损状态查询方向非法: {side!r}')
        self._positive_coin_amount(amount, '止损状态查询币数')
        if stop_order_id is not None:
            stop_order_id = self._public_order_id(stop_order_id)
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
                if self._public_order_id(order.get('id')) == stop_order_id), None)
        if expected is not None:
            if (len(protective) == 1 and len(reduce_only_algos) == 1 and
                    self._algo_order_matches(
                        expected, stop_side, stop_price, contracts,
                        expected_order_id=stop_order_id)):
                return 'intact'
            # 记录 ID 内容错误，或同品种同时还有其它保护止损：都属歧义。
            return 'mismatch'

        if stop_order_id:
            # pending 缺席只说明“不再待触发”，不能区分 canceled、effective、
            # partially_effective 或查询漏项。只有精确详情证明为零成交取消/失败，
            # 才允许把记录 ID 当作真正失效并进入 missing/adoptable 分支。
            detail = self._fetch_algo_order_raw(ccxt_symbol, stop_order_id)
            if not self._algo_order_safely_inactive(detail):
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

    def confirm_stop_execution(
            self, symbol, side, amount, stop_price, stop_order_id):
        """按算法单与其唯一子订单证明指定止损真实、完整地触发成交。"""
        if side not in ('long', 'short'):
            raise ValueError(f'止损成交确认方向非法: {side!r}')
        self._positive_coin_amount(amount, '止损成交确认币数')
        stop_order_id = self._public_order_id(stop_order_id)
        ccxt_symbol = self._resolve_symbol(symbol)
        inst_id = self._to_inst_id(ccxt_symbol)
        stop_side = 'sell' if side == 'long' else 'buy'
        contracts = self._coin_to_contracts(ccxt_symbol, amount)
        aligned_stop = self._align_stop_price(ccxt_symbol, stop_price)
        detail = self._fetch_algo_order_raw(ccxt_symbol, stop_order_id)
        if not isinstance(detail, dict) or detail.get('state') != 'effective':
            return False
        wrapped = {
            'id': self._public_order_id(detail.get('algoId')),
            'side': detail.get('side'),
            'reduceOnly': detail.get('reduceOnly'),
            'info': detail,
        }
        if (detail.get('instType') != 'SWAP' or
                wrapped['side'] != stop_side or
                not self._order_reduce_only(wrapped) or
                detail.get('ordType') != 'conditional' or
                detail.get('slTriggerPxType') != 'last' or
                detail.get('posSide') not in (None, '', 'net') or
                detail.get('actualSide') != 'sl'):
            return False
        if any(isinstance(detail.get(field), bool) for field in (
                'slOrdPx', 'slTriggerPx', 'sz', 'actualSz', 'triggerTime')):
            return False
        try:
            if float(detail.get('slOrdPx')) != -1.0:
                return False
            trigger_price = float(detail.get('slTriggerPx'))
            requested = float(detail.get('sz'))
            actual = float(detail.get('actualSz'))
            trigger_time = int(detail.get('triggerTime'))
        except (TypeError, ValueError, OverflowError):
            return False
        if not all(math.isfinite(value) for value in (
                trigger_price, requested, actual)):
            return False
        price_tolerance = max(
            math.ulp(trigger_price), math.ulp(float(aligned_stop))) * 4
        amount_tolerance = self._contracts_tolerance(ccxt_symbol)
        if (abs(trigger_price - float(aligned_stop)) > price_tolerance or
                abs(requested - contracts) > amount_tolerance or
                abs(actual - contracts) > amount_tolerance or
                trigger_time <= 0):
            return False

        raw_ids = detail.get('ordIdList')
        if raw_ids is None:
            raw_ids = []
        if not isinstance(raw_ids, list):
            return False
        try:
            child_ids = [self._public_order_id(value) for value in raw_ids]
            if detail.get('ordId') not in (None, ''):
                child_ids.append(self._public_order_id(detail.get('ordId')))
        except ValueError:
            return False
        child_ids = list(dict.fromkeys(child_ids))
        if len(child_ids) != 1:
            return False
        child = self._fetch_normal_order_raw(ccxt_symbol, child_ids[0])
        if not isinstance(child, dict):
            return False
        try:
            child_algo_id = self._public_order_id(child.get('algoId'))
        except ValueError:
            return False
        if (child.get('instType') != 'SWAP' or
                child.get('instId') != inst_id or
                child_algo_id != stop_order_id or
                str(child.get('state') or '').lower() != 'filled' or
                child.get('side') != stop_side or
                child.get('posSide') not in (None, '', 'net') or
                self._strict_boolean(child.get('reduceOnly')) is not True):
            return False
        filled = self._finite_nonnegative(child.get('accFillSz'))
        child_size = self._finite_nonnegative(child.get('sz'))
        return bool(
            filled is not None and child_size is not None and
            abs(filled - contracts) <= amount_tolerance and
            abs(child_size - contracts) <= amount_tolerance)

    # ===================== 撤单（含算法单） =====================

    # 待触发算法单查询覆盖 OKX 该端点的 ordType 全集。系统自建止损恒为
    # conditional，但人工 reduce-only iceberg/twap 同样可能在未来新仓上执行，
    # 不能因“不像止损”就在 stale-order 清单里隐身。
    ALGO_ORDER_TYPES = (
        'conditional', 'oco', 'trigger', 'move_order_stop', 'iceberg', 'twap',
        'chase', 'smart_iceberg')
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
        if (not isinstance(ccxt_symbol, str) or
                re.fullmatch(
                    r'[A-Z0-9]{1,20}/USDT:USDT', ccxt_symbol) is None):
            raise ValueError(f'非法 U 本位永续 ccxt symbol: {ccxt_symbol!r}')
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
                if not isinstance(item, dict):
                    raise RuntimeError(
                        f'算法单分页项缺少 algoId(ordType={ord_type})')
                try:
                    algo_id = self._public_order_id(item.get('algoId'))
                    if item.get('algoClOrdId') not in (None, ''):
                        self._client_order_id(item.get('algoClOrdId'))
                except ValueError as exc:
                    raise RuntimeError(
                        f'算法单分页项身份非法(ordType={ord_type})') from exc
                if item.get('instId') != inst_id:
                    raise RuntimeError(
                        f'算法单分页项品种不匹配(ordType={ord_type})')
                if item.get('ordType') != ord_type:
                    raise RuntimeError(
                        f'算法单分页项类型不匹配(ordType={ord_type})')
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
                    'id': self._public_order_id(item.get('algoId')),
                    'clientOrderId': (
                        self._client_order_id(item.get('algoClOrdId'))
                        if item.get('algoClOrdId') not in (None, '') else ''),
                    'side': item.get('side'),
                    # 字段缺失保留 None（未知）：压成 False 会让该单在
                    # 四态裁决的 reduce-only 清单里隐身，missing 误判触发双止损。
                    'reduceOnly': self._strict_boolean(raw_reduce_only),
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
                if not isinstance(item, dict):
                    raise RuntimeError('普通挂单分页项缺少 ordId')
                try:
                    order_id = self._public_order_id(item.get('ordId'))
                    if item.get('clOrdId') not in (None, ''):
                        self._client_order_id(item.get('clOrdId'))
                except ValueError as exc:
                    raise RuntimeError('普通挂单分页项身份非法') from exc
                if item.get('instId') != inst_id:
                    raise RuntimeError('普通挂单分页项品种不匹配')
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
            'id': self._public_order_id(item.get('ordId')),
            'clientOrderId': (
                self._client_order_id(item.get('clOrdId'))
                if item.get('clOrdId') not in (None, '') else ''),
            'side': item.get('side'),
            'reduceOnly': self._strict_boolean(item.get('reduceOnly')),
            'info': item,
        } for item in records]

    @classmethod
    def _merge_orders_by_id(cls, *groups):
        merged = []
        seen = set()
        for group in groups:
            for order in group or []:
                try:
                    order_id = cls._public_order_id(order.get('id'))
                except ValueError as exc:
                    raise RuntimeError('挂单快照项缺少/含非法 ID') from exc
                if order_id not in seen:
                    seen.add(order_id)
                    merged.append(order)
        return merged

    def _fetch_pending_snapshot(self, ccxt_symbol):
        """两类非原子清单的边界复读快照。

        OKX 没有“普通+算法单”单一原子端点。按 normal→algo→normal
        复读普通单并取并集：只有三次都空才返回空。algo 端点一次
        完整快照已需按类型请求 8 次，由外层“连续空轮”复读，避免超过
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
        order_id = self._public_order_id(order_id)
        return all(
            self._public_order_id(order.get('id')) != order_id
            for order in self._fetch_normal_orders(ccxt_symbol))

    @retry_on_network_error(max_retries=3)
    def _fetch_algo_order_raw(self, ccxt_symbol, order_id):
        """按精确 algoId 读取单张算法单详情；查不到与异常严格区分。"""
        order_id = self._public_order_id(order_id)
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        try:
            resp = self.exchange.privateGetTradeOrderAlgo({
                'algoId': order_id,
            })
        except ccxt.OrderNotFound:
            return None
        if isinstance(resp, dict) and resp.get('code') == '51603':
            return None
        if (not isinstance(resp, dict) or resp.get('code') != '0' or
                not isinstance(resp.get('data'), list) or
                len(resp['data']) != 1 or not isinstance(resp['data'][0], dict)):
            raise RuntimeError(
                f'算法单详情响应异常: {str(resp)[:200]}')
        item = resp['data'][0]
        try:
            observed_id = self._public_order_id(item.get('algoId'))
            if item.get('algoClOrdId') not in (None, ''):
                self._client_order_id(item.get('algoClOrdId'))
        except ValueError as exc:
            raise RuntimeError('算法单详情身份非法') from exc
        if observed_id != order_id:
            raise RuntimeError(
                f'算法单详情 ID 不匹配: expected={order_id}, '
                f"actual={item.get('algoId')}")
        expected_inst = self._to_inst_id(ccxt_symbol)
        if item.get('instId') != expected_inst:
            raise RuntimeError(
                f'算法单详情品种不匹配: expected={expected_inst}, '
                f"actual={item.get('instId')}")
        return item

    @retry_on_network_error(max_retries=3)
    def _fetch_normal_order_raw(self, ccxt_symbol, order_id):
        """查普通订单终态；未找到返回 None，异常信封拒绝裁决。"""
        order_id = self._public_order_id(order_id)
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        try:
            resp = self.exchange.privateGetTradeOrder({
                'instId': self._to_inst_id(ccxt_symbol),
                'ordId': order_id,
            })
        except ccxt.OrderNotFound:
            return None
        if isinstance(resp, dict) and resp.get('code') == '51603':
            return None
        if (not isinstance(resp, dict) or resp.get('code') != '0' or
                not isinstance(resp.get('data'), list) or
                len(resp['data']) != 1 or not isinstance(resp['data'][0], dict)):
            raise RuntimeError(
                f'普通订单终态响应异常: {str(resp)[:200]}')
        item = resp['data'][0]
        try:
            observed_id = self._public_order_id(item.get('ordId'))
            if item.get('clOrdId') not in (None, ''):
                self._client_order_id(item.get('clOrdId'))
            if item.get('algoId') not in (None, ''):
                self._public_order_id(item.get('algoId'))
        except ValueError as exc:
            raise RuntimeError('普通订单终态身份非法') from exc
        if observed_id != order_id:
            raise RuntimeError(
                f'普通订单终态 ID 不匹配: expected={order_id}, '
                f"actual={item.get('ordId')}")
        expected_inst = self._to_inst_id(ccxt_symbol)
        if item.get('instId') != expected_inst:
            raise RuntimeError(
                f'普通订单终态品种不匹配: expected={expected_inst}, '
                f"actual={item.get('instId')}")
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

    @staticmethod
    def _algo_order_safely_cancelled(order):
        """只有明确 canceled 且 actualSz=0 才能证明撤单未触发成交。"""
        if not isinstance(order, dict) or order.get('state') != 'canceled':
            return False
        actual = OkxApi._finite_nonnegative(order.get('actualSz'))
        return actual == 0

    @staticmethod
    def _algo_order_safely_inactive(order):
        """允许重建保护的明确无成交终态；部分/触发/未知状态一律拒绝。"""
        if not isinstance(order, dict) or order.get('state') not in {
                'canceled', 'order_failed'}:
            return False
        actual = OkxApi._finite_nonnegative(order.get('actualSz'))
        return actual == 0

    def _request_cancel_normal_order(self, ccxt_symbol, order_id):
        order_id = self._public_order_id(order_id)
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        try:
            self.exchange.privatePostTradeCancelOrder({
                'instId': self._to_inst_id(ccxt_symbol),
                'ordId': order_id,
            })
        except Exception as exc:
            logger.warning(
                f'撤销普通单指令异常（成败以终态裁决）: {order_id}: {exc}')

    def _request_cancel_normal_orders(self, ccxt_symbol, order_ids):
        """批量发撤单指令（每批最多 20）；ACK 不作结论。"""
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        unique_ids = list(dict.fromkeys(
            self._public_order_id(order_id) for order_id in order_ids))
        inst_id = self._to_inst_id(ccxt_symbol)
        for start in range(0, len(unique_ids), self.NORMAL_CANCEL_BATCH_LIMIT):
            self._assert_flat_before_cancel_all_write(ccxt_symbol)
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
        order_id = self._public_order_id(order_id)
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
        order_id = self._public_order_id(order_id)
        for o in self._fetch_algo_orders(ccxt_symbol):
            if self._public_order_id(o.get('id')) == order_id:
                return False
        return True

    def _cancel_algo_order(self, ccxt_symbol, order_id):
        """撤销算法单；只有精确详情证明零成交 canceled 才算成功。

        撤销指令直调 OKX 原生 cancel-algos 端点（与查询同一权威接口族，消除
        ccxt 统一撤单参数映射漂移的最后一处依赖）。指令自身的返回不构成任何
        结论。pending 消失也可能是已触发或失败，不能作为撤销证据。
        """
        order_id = self._public_order_id(order_id)
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        try:
            self.exchange.privatePostTradeCancelAlgos(
                [{'algoId': order_id, 'instId': self._to_inst_id(ccxt_symbol)}])
        except Exception as e:
            logger.warning(f"撤销算法单指令异常（成败以清单裁决，不据此下结论）: {order_id}: {e}")
        try:
            for attempt in range(2):
                detail = self._fetch_algo_order_raw(ccxt_symbol, order_id)
                if (self._algo_order_safely_cancelled(detail) and
                        self._algo_order_absent(ccxt_symbol, order_id)):
                    return True
                if attempt == 0:
                    time.sleep(self.CANCEL_VERIFY_RECHECK_DELAY)
            return False
        except Exception as e:
            logger.warning(f"撤销后查询算法单终态失败，无法确认 {order_id} 已撤: {e}")
            return False

    def _request_cancel_algo_orders(self, ccxt_symbol, order_ids):
        """批量发算法单撤销指令（每批最多 10）；由外层完整清单复验。"""
        ccxt_symbol = self._resolve_symbol(ccxt_symbol)
        unique_ids = list(dict.fromkeys(
            self._public_order_id(order_id) for order_id in order_ids))
        inst_id = self._to_inst_id(ccxt_symbol)
        for start in range(0, len(unique_ids), self.ALGO_CANCEL_BATCH_LIMIT):
            self._assert_flat_before_cancel_all_write(ccxt_symbol)
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
        """先按精确详情区分订单类型，再验证其零成交撤销终态。"""
        try:
            order_id = self._public_order_id(order_id)
        except ValueError as exc:
            logger.error(f'拒绝非法撤单 ID: {exc}')
            return False
        ccxt_symbol = self._resolve_symbol(symbol)
        try:
            normal_seen = any(
                self._public_order_id(order.get('id')) == order_id
                for order in self._fetch_normal_orders(ccxt_symbol))
            normal_detail = self._fetch_normal_order_raw(
                ccxt_symbol, order_id)
            algo_detail = self._fetch_algo_order_raw(ccxt_symbol, order_id)
        except Exception as exc:
            logger.warning(f'按 ID 识别订单类型失败: {order_id}: {exc}')
            return False
        normal_known = normal_seen or normal_detail is not None
        algo_known = algo_detail is not None
        if normal_known == algo_known:
            logger.error(
                f'{ccxt_symbol} 订单 {order_id} 类型不存在或存在歧义，拒绝撤销裁决')
            return False
        if algo_known:
            return self._cancel_algo_order(ccxt_symbol, order_id)
        return self._cancel_normal_order(ccxt_symbol, order_id)

    def cancel_stop_order_only(self, symbol, order_id):
        """持仓仍开着时只撤指定算法止损，失败绝不退化为 cancel-all。

        make-before-break 已先挂好新保护；全撤会把新单一起删掉并造成裸仓。
        """
        try:
            order_id = self._public_order_id(order_id)
        except ValueError as exc:
            logger.error(f'拒绝非法止损撤单 ID: {exc}')
            return False
        ccxt_symbol = self._resolve_symbol(symbol)
        return bool(self._cancel_algo_order(ccxt_symbol, order_id))

    @retry_on_network_error(max_retries=3)
    def cancel_all_orders(self, symbol):
        """安全清理某交易对挂单：连续空清单 + 普通单零成交撤销 + 空仓。"""
        ccxt_symbol = self._resolve_symbol(symbol)
        try:
            self._assert_flat_before_cancel_all_write(ccxt_symbol)
            # 先拍快照再发统一撤全，否则已成交消失的 ID 将无法做终态审计。
            normal, algos = self._fetch_pending_snapshot(ccxt_symbol)
            seen_normal_ids = {
                self._public_order_id(order.get('id')) for order in normal}
            seen_algo_ids = {
                self._public_order_id(order.get('id')) for order in algos}
            self._assert_flat_before_cancel_all_write(ccxt_symbol)
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
                        self._public_order_id(order.get('id'))
                        for order in remaining_normal}
                    seen_normal_ids.update(new_normal_ids)
                    seen_algo_ids.update(
                        self._public_order_id(order.get('id'))
                        for order in remaining_algos)
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
            for algo_id in sorted(seen_algo_ids):
                terminal = self._fetch_algo_order_raw(
                    ccxt_symbol, algo_id)
                if not self._algo_order_safely_cancelled(terminal):
                    logger.critical(
                        f'{ccxt_symbol} 算法单 {algo_id} 未证明为零成交撤销，'
                        '可能在撤单竞态中已触发')
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

    def _assert_flat_before_cancel_all_write(self, ccxt_symbol):
        """cancel-all 只允许用于空仓清场；任何写请求紧前都必须再证明空仓。"""
        try:
            position = self.get_position(ccxt_symbol)
        except Exception as exc:
            raise RuntimeError(
                f'{ccxt_symbol} 撤全前无法证明空仓') from exc
        if position is not None:
            raise RuntimeError(
                f'{ccxt_symbol} 当前有持仓，禁止 cancel-all 撤掉保护单')
