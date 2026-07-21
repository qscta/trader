import ccxt
import logging
import math
import os
import time
from decimal import Decimal

from exchange_base import ExchangeApi, retry_on_network_error

logger = logging.getLogger(__name__)


class ContractSizeUnavailable(RuntimeError):
    """合约面值不可得。面值是张数换算与风控的根基，拿不到必须拒绝交易（fail closed），绝不允许猜默认值。"""


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
        # 保证金模式：cross（全仓）/ isolated（逐仓）。启动即校验（fail-loud）：该值直接
        # 进入每一笔订单的 tdMode，写错（如手滑 "corss"）不会在启动时暴露，而是让全部
        # 下单在盘中被交易所逐笔拒绝——与 strategy/scheduler 配置同标准，坏配置拒绝启动。
        raw_margin_mode = config.get('margin_mode') or 'cross'
        if not isinstance(raw_margin_mode, str) or raw_margin_mode.strip().lower() not in ('cross', 'isolated'):
            raise ValueError(
                f"okx.margin_mode 非法: {config.get('margin_mode')!r}（只支持 cross / isolated）")
        super().__init__(config)
        self.margin_mode = raw_margin_mode.strip().lower()
        # 杠杆：默认值 + 可按内部符号覆盖，如 {"BTCUSDT": 10}
        self.default_leverage = config.get('leverage', 5)
        self.leverage_overrides = config.get('leverage_overrides', {}) or {}

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
                        contract_size = float(contract_size)
                        if math.isfinite(contract_size) and contract_size > 0:
                            self._contract_size_cache[sym] = contract_size
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
        if not math.isfinite(contract_size) or contract_size <= 0:
            raise ContractSizeUnavailable(f"{ccxt_symbol} 市场数据缺少有效 contractSize，拒绝换算/交易")
        self._contract_size_cache[ccxt_symbol] = contract_size
        return contract_size

    # ===================== 张数换算 =====================

    def _coin_to_contracts(self, ccxt_symbol, coin_amount):
        """币数 -> 张数（按交易所张数步长取整）。面值不可得时向上抛出（拒绝交易）。"""
        contract_size = self._get_contract_size(ccxt_symbol)
        # 仓位币数本来就是「张数 × contractSize」写入账本；若再用二进制
        # float 反除，10.1 / 0.1 可能变成 100.99999999999999，随后
        # amount_to_precision 按步长截断成 100，令真实 101 张被误判为人工加仓。
        # 用十进制字符串还原交易所数量语义，只消除浮点表示误差，不会把真正
        # 低于下一步长的数量向上取整。
        # coin_amount 也可能来自「实仓张数 × float 面值」：例如 10 × 1e-6
        # 会先形成 9.999999999999999e-06。它若位于精确值下方，IEEE 乘法误差
        # 不超过半个 ULP；反除前只把币数向 +∞ 移动一个 ULP，再用十进制运算，
        # 恰好覆盖这层表示残差。真实的 10.099 币离下一张远大于一个 ULP，
        # 仍会按交易所规则截断，绝不会普通四舍五入扩大仓位。
        coin_amount = math.nextafter(float(coin_amount), math.inf)
        raw_contracts = float(Decimal(str(coin_amount)) / Decimal(str(contract_size)))
        try:
            return float(self.exchange.amount_to_precision(ccxt_symbol, raw_contracts))
        except Exception:
            precision = self._amount_precision_cache.get(ccxt_symbol, 0)
            factor = 10 ** precision
            return math.floor(raw_contracts * factor) / factor

    def round_quantity(self, symbol, quantity):
        """把上层算出的“币数”对齐到 OKX 整张，再换算回“币数”返回。

        返回的币数 = 整张数 × 合约面值，确保它与最终真实下单张数一一对应——
        这样上层用这个币数做名义价值/风控/盈亏计算时不会与实际成交错位。
        若不足一张则返回 0，上层会据此放弃开仓（无法交易小于一张的量）。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        contract_size = self._get_contract_size(ccxt_symbol)
        contracts = self._coin_to_contracts(ccxt_symbol, quantity)
        return contracts * contract_size

    def get_quantity_precision(self, symbol):
        """返回“张数”的小数位（仅用于日志展示）。"""
        ccxt_symbol = self._resolve_symbol(symbol)
        return self._amount_precision_cache.get(ccxt_symbol, 0)

    # ===================== 杠杆 / 持仓模式 =====================

    def _ensure_one_way_mode(self):
        """尝试切换并验证单向(净)持仓模式；无法证明时拒绝启动。"""
        try:
            self.exchange.set_position_mode(False)  # False = 单向(net)
        except Exception as e:
            # 已有持仓/挂单或本来就是净持仓时，设置请求可能被拒绝；最终只以
            # 账户配置查询为准，不能把“设置失败”猜成“已经正确”。
            logger.info(f"OKX 设置单向持仓模式未完成，将查询账户配置确认: {e}")

        try:
            response = self.exchange.privateGetAccountConfig()
            data = response.get('data') if isinstance(response, dict) else None
            position_mode = data[0].get('posMode') if data and isinstance(data[0], dict) else None
        except Exception as e:
            raise RuntimeError(f"无法验证 OKX 持仓模式，拒绝启动: {e}") from e

        if position_mode != 'net_mode':
            raise RuntimeError(
                f"OKX 持仓模式为 {position_mode!r}，本系统只支持单向净持仓(net_mode)，拒绝启动")
        logger.info("OKX 已确认单向(净)持仓模式")

    def _leverage_for(self, ccxt_symbol):
        internal = self.to_internal_symbol(ccxt_symbol)
        return (self.leverage_overrides.get(internal)
                or self.leverage_overrides.get(ccxt_symbol)
                or self.default_leverage)

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
            logger.warning(f"OKX 设置 {ccxt_symbol} 杠杆失败: {e}（将沿用账户现有杠杆）")

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
        若原样外泄，上层的 `contracts == 0` / `contracts > 0` 判断会因 None
        误判甚至 TypeError——统一在适配层归一化掉。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        positions = self.exchange.fetch_positions([ccxt_symbol])
        for p in positions or []:
            if not p or p.get('contracts') in (None, 0, 0.0, ''):
                continue
            contracts = float(p['contracts'])
            if not math.isfinite(contracts):
                raise RuntimeError(f'{ccxt_symbol} 持仓张数不是有限数: {p.get("contracts")!r}')
            if abs(contracts) > 0:
                return p
        return None

    def managed_position_matches(self, symbol, exchange_position, side, amount):
        """交易所实际持仓是否与本地托管记录一致。

        上层始终使用币数，张数换算仍封装在 OKX 适配层内。方向或数量
        任一不符即返回 False，由上层隔离该品种，不猜测人工加减仓的意图。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        actual_side = str((exchange_position or {}).get('side') or '').lower()
        actual_contracts = abs(float((exchange_position or {}).get('contracts') or 0))
        expected_contracts = self._coin_to_contracts(ccxt_symbol, amount)
        return (actual_side == side
                and math.isclose(actual_contracts, expected_contracts,
                                 rel_tol=1e-9, abs_tol=1e-9))

    def _position_contracts(self, ccxt_symbol):
        """查询当前持仓张数；查询失败时向上抛出。"""
        position = self.get_position(ccxt_symbol)
        if position and position.get('contracts'):
            return abs(float(position['contracts']))
        return 0.0

    def confirm_position_flat(self, ccxt_symbol, attempts=3, required_consecutive=2):
        """至少连续两次查询为空，才确认交易所持仓归零。"""
        consecutive_flat = 0
        for check in range(attempts):
            try:
                if self._position_contracts(ccxt_symbol) == 0:
                    consecutive_flat += 1
                    if consecutive_flat >= required_consecutive:
                        return True
                else:
                    consecutive_flat = 0
            except Exception as e:
                consecutive_flat = 0
                logger.warning(f"第{check + 1}/{attempts}次平仓确认查询失败: {e}")
            if check < attempts - 1:
                time.sleep(2)
        return False

    def _confirm_open_position(self, ccxt_symbol, side, contracts, attempts=3):
        """下单回执后，以实仓方向和完整张数确认开仓已成交。

        OKX 的下单成功回执只表示请求被受理；永续市价单仍可能尚未成交或
        部分成交。只有查询到方向正确且张数完整的实仓，才允许上层挂止损、
        写账本。返回确认后的币数和实仓，证据不足返回 ``(None, None)``。
        """
        for check in range(attempts):
            try:
                position = self.get_position(ccxt_symbol)
                actual_side = str((position or {}).get('side') or '').lower()
                actual_contracts = abs(float((position or {}).get('contracts') or 0))
                if (actual_side == side
                        and math.isclose(actual_contracts, contracts,
                                         rel_tol=1e-9, abs_tol=1e-9)):
                    coin_amount = actual_contracts * self._get_contract_size(ccxt_symbol)
                    return coin_amount, position
            except Exception as e:
                logger.warning(f"第{check + 1}/{attempts}次开仓成交确认失败: {e}")
            if check < attempts - 1:
                time.sleep(2)
        return None, None

    def _cleanup_unconfirmed_open(self, ccxt_symbol, side, amount):
        """开仓成交无法确认时，先终止未完成订单，再平掉任何已成交部分。"""
        try:
            orders_cleared = bool(self.cancel_all_orders(ccxt_symbol))
        except Exception as e:
            orders_cleared = False
            logger.critical(f"{ccxt_symbol} 未确认开仓的挂单清理异常: {e}")
        close_order = self.close_position(ccxt_symbol, side, amount)
        if orders_cleared and close_order:
            logger.warning(f"{ccxt_symbol} 未确认开仓已撤单并确认回到空仓")
            return True
        logger.critical(
            f"{ccxt_symbol} 开仓成交无法确认，且撤单/回滚未能完整确认；"
            "禁止自动归因，请立即人工核对持仓和委托")
        return False

    # ===================== 写操作：超时后查询确认 =====================

    def open_position(self, symbol, side, amount):
        """安全开仓（市价单）。amount 单位为币数。"""
        # 最终交易所写边界再守一次部署总闸：即使未来新增调用点绕过
        # TradingSystem._execute_open，也不能在维护期发出开仓单。
        if os.environ.get('TRADING_DISABLE_NEW_OPENS') == '1':
            logger.warning(f"{symbol} 开仓被 TRADING_DISABLE_NEW_OPENS 总闸阻断")
            return None
        ccxt_symbol = self._resolve_symbol(symbol)
        order_side = 'buy' if side == 'long' else 'sell'

        self.setup_symbol(ccxt_symbol)

        contracts = self._coin_to_contracts(ccxt_symbol, amount)
        if contracts <= 0:
            logger.error(f"{ccxt_symbol} 开仓张数为0（amount={amount}币，不足一张），放弃开仓")
            return None

        try:
            pre_position = self.get_position(ccxt_symbol)
        except Exception as e:
            logger.error(f"开仓前查询持仓失败: {e}，为防止叠加未托管仓位，拒绝开仓")
            return None
        if pre_position is not None:
            logger.error(f"{ccxt_symbol} 最终发单前已存在持仓，为防止叠加人工/未托管仓位，拒绝开仓")
            return None
        pre_contracts = 0.0

        try:
            order = self.exchange.create_order(
                ccxt_symbol, 'market', order_side, contracts, None, self._order_params()
            )
            confirmed_coin_amount, position = self._confirm_open_position(
                ccxt_symbol, side, contracts)
            if confirmed_coin_amount is None:
                logger.error(
                    f"开仓请求已受理但未确认完整成交: {ccxt_symbol} {side} {contracts}张，"
                    "进入撤单/回滚")
                self._cleanup_unconfirmed_open(ccxt_symbol, side, amount)
                return None
            # 成交事实只由实仓确认决定。若 ccxt/交易所返回畸形回执（None、数组等），
            # 不能在这里给非字典写字段后异常退出，把已经完整成交的真实仓位遗忘成
            # 无账本、无止损的孤儿仓。改用空字典继续向上托管；不伪造订单号。
            if not isinstance(order, dict):
                logger.warning(
                    f"{ccxt_symbol} 开仓回执不是对象({type(order).__name__})，"
                    "但实仓已确认完整成交；将以实仓证据继续托管")
                order = {}
            order['confirmed_coin_amount'] = confirmed_coin_amount
            entry_price = (position or {}).get('entryPrice')
            if entry_price not in (None, ''):
                order['average'] = entry_price
            logger.info(
                f"开仓成功(已确认完整成交): {ccxt_symbol} {side} {contracts}张"
                f"(≈{confirmed_coin_amount}币), 订单ID={order.get('id')}")
            return order

        except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
            logger.warning(f"开仓请求超时: {e}，查询持仓确认是否已成交...")
            time.sleep(2)

            confirmed_coin_amount, position = self._confirm_open_position(
                ccxt_symbol, side, contracts)
            if confirmed_coin_amount is not None:
                logger.info(f"确认开仓已完整成交: 持仓从 {pre_contracts} 变为 {contracts} 张")
                return {
                    'id': 'timeout_confirmed',
                    'average': (position or {}).get('entryPrice'),
                    # 对外契约是币数：张数换算回币，张数不外泄
                    'amount': confirmed_coin_amount,
                    'confirmed_coin_amount': confirmed_coin_amount,
                    'status': 'closed',
                    'info': '超时后通过持仓查询确认已完整成交'
                }

            logger.error(
                f"开仓超时且未确认完整成交: {ccxt_symbol} {side} {amount}，进入撤单/回滚")
            self._cleanup_unconfirmed_open(ccxt_symbol, side, amount)
            return None

        except (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.BadRequest) as e:
            logger.error(f"开仓业务异常: {e}")
            return None
        except Exception as e:
            # create_order 是非幂等写；未分类异常也可能发生在请求已到达交易所、
            # 但客户端解析回执失败之后。不能把它当“肯定未成交”直接遗忘。
            logger.error(f"开仓未知异常: {e}，按成交不确定执行撤单/回滚")
            self._cleanup_unconfirmed_open(ccxt_symbol, side, amount)
            return None

    def close_position(self, symbol, side, amount):
        """安全平仓（市价单，reduce-only）。amount 单位为币数。"""
        ccxt_symbol = self._resolve_symbol(symbol)
        close_side = 'sell' if side == 'long' else 'buy'

        pre_position = None
        query_success = False
        try:
            pre_position = self.get_position(ccxt_symbol)
            query_success = True
        except Exception as e:
            logger.warning(f"平仓前查询持仓失败: {e}，继续平仓...")

        pre_contracts = abs(float(pre_position['contracts'])) if pre_position and pre_position.get('contracts') else 0.0

        # 查询成功且确认无持仓时才跳过；查询失败不拦截（宁可重复平仓也不漏平）
        if query_success and pre_contracts == 0:
            if self.confirm_position_flat(ccxt_symbol):
                logger.warning(f"{ccxt_symbol} 欧易端已确认无持仓，跳过平仓指令（可能已手动平仓）")
                return {'id': 'already_closed', 'average': None, 'status': 'closed'}
            logger.warning(f"{ccxt_symbol} 首次查询为空但复核未确认归零，继续发送 reduce-only 平仓")

        contracts = self._coin_to_contracts(ccxt_symbol, amount)
        if query_success and pre_contracts > 0:
            # 不超平：以实际持仓张数为上限；换算为 0 时直接全平
            contracts = min(contracts, pre_contracts) if contracts > 0 else pre_contracts
        elif contracts <= 0:
            logger.error(f"{ccxt_symbol} 平仓张数换算为0且持仓查询失败，放弃平仓")
            return None

        try:
            order = self.exchange.create_order(
                ccxt_symbol, 'market', close_side, contracts, None, self._order_params(reduce_only=True)
            )
            if self.confirm_position_flat(ccxt_symbol):
                logger.info(f"平仓成功(已确认归零): {ccxt_symbol} {side} {contracts}张, 订单ID={order.get('id')}")
                return order
            logger.error(f"平仓指令已返回但持仓未确认归零: {ccxt_symbol} {side} {contracts}张")
            return None

        except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
            logger.warning(f"平仓请求超时: {e}，查询持仓确认...")
            time.sleep(2)

            if self.confirm_position_flat(ccxt_symbol):
                logger.info(f"确认平仓已成交: 原持仓 {pre_contracts} 张，当前已归零")
                return {
                    'id': 'timeout_confirmed',
                    'average': None,
                    'amount': pre_contracts * self._get_contract_size(ccxt_symbol),
                    'status': 'closed',
                    'info': '超时后通过持仓查询确认已平仓'
                }

            logger.error(f"平仓超时且确认未成交: {ccxt_symbol} {side} {amount}")
            return None

        except Exception as e:
            logger.error(f"平仓异常: {e}")
            return None

    @staticmethod
    def _algo_order_matches(order, stop_side, stop_price, contracts):
        """判断一张算法单是否就是「我们刚下的那张止损单」：方向 + 触发价 + 张数全部吻合。

        超时确认只按方向匹配会把残留的旧止损误认成新单，导致本地记录的止损价
        与交易所实际不一致。任何字段读不到一律视为不匹配（宁可重试创建，也不误认）。
        """
        if (not order or order.get('side') != stop_side
                or order.get('reduceOnly') is not True):
            return False
        info = order.get('info') or {}
        trigger = (order.get('stopLossPrice') or order.get('triggerPrice') or order.get('stopPrice')
                   or info.get('slTriggerPx') or info.get('triggerPx'))
        try:
            trigger = float(trigger)
            stop_price = float(stop_price)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(trigger) or not math.isfinite(stop_price):
            return False
        if abs(trigger - stop_price) > max(1e-8, abs(stop_price) * 1e-6):
            return False
        amount = order.get('amount')
        if amount is None:
            amount = info.get('sz')
        try:
            amount = float(amount)
            contracts = float(contracts)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(amount) or not math.isfinite(contracts):
            return False
        return abs(amount - contracts) <= max(1e-8, abs(contracts) * 1e-6)

    def _align_stop_price(self, ccxt_symbol, stop_price):
        """把止损触发价对齐到交易所价格步长（tick）。

        实盘验证实证：OKX 会把非对齐触发价按 tick 取整后存储（39.384→39.38），
        本地原始价与交易所存储价的差会让严格匹配（创建超时确认/三态判定）误判
        不匹配/mismatch。发单前先用交易所元数据对齐——发送值与存储值必然一致；
        比对侧用同一函数对齐本地记录，历史留存的非对齐价也能正确匹配。
        精度元数据不可得时按原价返回（匹配退化为对齐前行为，fail-safe 不阻断）。
        """
        try:
            return float(self.exchange.price_to_precision(ccxt_symbol, stop_price))
        except Exception:
            return stop_price

    def _confirm_new_stop_order(self, ccxt_symbol, stop_side, stop_price,
                                contracts, attempts=3):
        """只以待触发清单中唯一且完全匹配的算法单作为止损创建成功证据。"""
        for check in range(attempts):
            try:
                algos = self._fetch_algo_orders(ccxt_symbol)
                matches = [
                    order for order in algos
                    if self._algo_order_matches(
                        order, stop_side, stop_price, contracts)
                ]
                if len(algos) == 1 and len(matches) == 1:
                    return matches[0]
            except Exception as e:
                logger.warning(f"第{check + 1}/{attempts}次确认止损单失败: {e}")
            if check < attempts - 1:
                time.sleep(2)
        return None

    def create_stop_loss_order(self, symbol, side, amount, stop_price):
        """创建止损算法单（reduce-only，触发后市价平仓）。amount 单位为币数。"""
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

        # stopLossPrice 为 ccxt 统一参数：OKX 下会映射为条件单 slTriggerPx + 市价(slOrdPx=-1)
        params = self._order_params(reduce_only=True, extra={'stopLossPrice': stop_price})

        try:
            order = self.exchange.create_order(
                ccxt_symbol, 'market', stop_side, contracts, None, params)
            confirmed = self._confirm_new_stop_order(
                ccxt_symbol, stop_side, stop_price, contracts)
            if confirmed:
                logger.info(
                    f"止损单创建成功(已验证): {ccxt_symbol} {stop_side} {contracts}张 @ {stop_price}, "
                    f"订单ID={confirmed.get('id') or order.get('id')}")
                return confirmed
            logger.error(
                f"止损创建指令已返回但待触发清单未确认唯一匹配单: {ccxt_symbol} @ {stop_price}")
            return None
        except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
            # 算法单是非幂等写操作：首次请求可能已到达交易所。此时重发会
            # 生成多张同价止损，平仓时只撤账本记录的一张，残留单会误伤未来仓位。
            # 因此只复查、绝不再次发单；不可确认则返回失败，由上层撤单并回滚。
            logger.warning(f"止损单请求超时: {e}，只查询确认，不重复发单")
            confirmed = self._confirm_new_stop_order(
                ccxt_symbol, stop_side, stop_price, contracts)
            if confirmed:
                logger.info(f"确认本次止损单已存在: 订单ID={confirmed.get('id')}")
                return confirmed
            logger.error(f"止损单超时且三次复查均未确认: {ccxt_symbol} @ {stop_price}")
            return None
        except (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.BadRequest) as e:
            logger.error(f"止损单业务异常: {e}")
            return None
        except Exception as e:
            logger.error(f"止损单未知异常: {e}")
            return None

    @retry_on_network_error(max_retries=3)
    def list_position_symbols(self):
        """交易所端当前有实际持仓的内部符号列表（孤儿仓核对用：启动同步 + 盘中巡检）。

        只统计 U 本位永续（ccxt 符号以 :USDT 结尾）：OKX 的 SWAP 持仓查询会一并
        返回币本位合约（如 BTC/USD:BTC），to_internal_symbol 会把它错映射成
        BTCUSDT——人工持有的币本位仓会被误报成孤儿、或恰有同名 U 本位托管仓时
        把币本位仓错当已接管。币本位不在本系统边界内，在数据源处过滤。
        """
        symbols = []
        for p in self.exchange.fetch_positions() or []:
            if not p or p.get('contracts') in (None, 0, 0.0, ''):
                continue
            contracts = float(p['contracts'])
            if not math.isfinite(contracts):
                raise RuntimeError(f'持仓张数不是有限数: {p.get("contracts")!r}')
            ccxt_symbol = p.get('symbol') or ''
            if abs(contracts) > 0 and ccxt_symbol.endswith(':USDT'):
                symbols.append(self.to_internal_symbol(ccxt_symbol))
        return symbols

    def find_stop_order_state(self, symbol, side, amount, stop_price, stop_order_id=None):
        """检查与「本地持仓记录」对应的止损算法单状态（供主层止损自愈巡检使用）。

        amount 为币数，张数换算在本方法内部完成（张数不外泄）。返回三态：
          'intact'   — 存在方向+触发价+张数与本地记录严格一致的算法单（保护完整）；
          'mismatch' — 存在内容不符、非 reduce-only、额外或重复算法单
                       （自动补挂会造成双止损，须人工核对）；
          'missing'  — 列表为空（需要补挂）。
        查询/换算失败向上抛出，调用方按 fail-safe 跳过本轮。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        stop_side = 'sell' if side == 'long' else 'buy'
        contracts = self._coin_to_contracts(ccxt_symbol, amount)
        # 本地记录价与交易所存储价须同一口径：交易所按 tick 取整存储，
        # 比对前用同一对齐函数归一本地价（详见 _align_stop_price）
        stop_price = self._align_stop_price(ccxt_symbol, stop_price)
        algos = self._fetch_algo_orders(ccxt_symbol)
        matches = [
            o for o in algos
            if self._algo_order_matches(o, stop_side, stop_price, contracts)
        ]
        # 只有「唯一一张且内容完全匹配」才算保护完整。额外算法单（包括
        # 旧版超时重发生成的重复止损）可在未来仓位中触发，必须按 mismatch
        # 隔离给人工裁决，不能因其中恰有一张匹配就忽略其余挂单。
        if len(algos) == 1 and len(matches) == 1:
            return 'intact'
        return 'mismatch' if algos else 'missing'

    # ===================== 撤单（含算法单） =====================

    # 待触发算法单查询覆盖的 ordType 全集：系统自建止损恒为 conditional；
    # oco/trigger/move_order_stop 覆盖人工挂单，让 cancel_all_orders 清扫路径可见
    # （iceberg/twap 等高级委托不属止损语义，历史实现同样不可见，不纳入）。
    ALGO_ORDER_TYPES = ('conditional', 'oco', 'trigger', 'move_order_stop')

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
        instId 过滤下单品种的算法单数远小于单页上限(100)，无需分页。
        """
        resp = self.exchange.privateGetTradeOrdersAlgoPending(
            {'ordType': ord_type, 'instId': inst_id})
        if not isinstance(resp, dict) or resp.get('code') != '0' or not isinstance(resp.get('data'), list):
            raise RuntimeError(f'算法单查询响应异常(ordType={ord_type}): {str(resp)[:200]}')
        return resp['data']

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
                    'side': item.get('side'),
                    'reduceOnly': item.get('reduceOnly') in (True, 'true'),
                    'info': item,
                })
        return orders

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

    def cancel_order(self, symbol, order_id):
        """撤销订单。止损单是算法单，优先按算法单撤销（验证式），失败再退化为撤全部。"""
        ccxt_symbol = self._resolve_symbol(symbol)
        try:
            if self._cancel_algo_order(ccxt_symbol, order_id):
                logger.info(f"撤单成功(已验证): {ccxt_symbol} 订单ID={order_id}")
                return True
            # 算法单路径未确认撤掉，再按普通单试一次；这是止损撤销路径，
            # 普通撤单返回成功后仍须以算法单列表复验，否则不得返回 True
            self.exchange.cancel_order(order_id, ccxt_symbol)
            try:
                if self._algo_order_absent(ccxt_symbol, order_id):
                    logger.info(f"撤单成功(普通单，已复验): {ccxt_symbol} 订单ID={order_id}")
                    return True
            except Exception:
                pass
            logger.error(f"{ccxt_symbol} 订单 {order_id} 普通撤单已发出，但算法单列表仍存在/不可确认")
            return False
        except ccxt.OrderNotFound:
            # 普通单也查不到：最后以算法单列表为准做一次裁决
            try:
                if self._algo_order_absent(ccxt_symbol, order_id):
                    logger.info(f"{ccxt_symbol} 订单 {order_id} 确认不存在（已触发/已撤）")
                    return True
            except Exception:
                pass
            logger.error(f"{ccxt_symbol} 订单 {order_id} 撤销不可确认（算法单列表仍存在或查询失败）")
            return False
        except Exception as e:
            logger.warning(f"按ID撤单失败: {e}，尝试撤销所有挂单...")
            return self.cancel_all_orders(ccxt_symbol)

    @retry_on_network_error(max_retries=3)
    def cancel_all_orders(self, symbol):
        """撤销某交易对的所有挂单：普通单 + 算法止损单都要撤干净。"""
        ccxt_symbol = self._resolve_symbol(symbol)
        normal_ok = True
        algo_ok = True

        # 普通挂单：撤单指令的返回不作结论，最后以 open-orders 清单为空裁决。
        try:
            try:
                self.exchange.cancel_all_orders(ccxt_symbol)
            except ccxt.NotSupported:
                logger.info(f"{ccxt_symbol} 不支持批量撤普通单，改为逐个撤销")
            except Exception as e:
                if 'no open orders' not in str(e).lower() and '51603' not in str(e):
                    raise

            remaining = self.exchange.fetch_open_orders(ccxt_symbol) or []
            if not isinstance(remaining, list):
                raise RuntimeError(f'普通挂单查询返回非数组: {type(remaining).__name__}')
            for o in remaining:
                self.exchange.cancel_order(o['id'], ccxt_symbol)
            if remaining:
                remaining = self.exchange.fetch_open_orders(ccxt_symbol) or []
                if not isinstance(remaining, list):
                    raise RuntimeError(f'普通挂单复查返回非数组: {type(remaining).__name__}')
            if remaining:
                logger.error(f"{ccxt_symbol} 撤单后仍有 {len(remaining)} 张普通挂单")
                normal_ok = False
            else:
                logger.info(f"{ccxt_symbol} 普通挂单已确认清空")
        except Exception as e:
            logger.warning(f"撤销/确认普通挂单失败: {e}")
            normal_ok = False

        # 算法止损单
        try:
            algos = self._fetch_algo_orders(ccxt_symbol)
            for o in algos:
                if not self._cancel_algo_order(ccxt_symbol, o.get('id')):
                    logger.warning(f"撤销算法单 {o.get('id')} 失败")
                    algo_ok = False
            remaining_algos = self._fetch_algo_orders(ccxt_symbol)
            if remaining_algos:
                logger.error(f"{ccxt_symbol} 撤单后仍有 {len(remaining_algos)} 张算法单")
                algo_ok = False
            if algos:
                logger.info(f"已处理 {ccxt_symbol} 算法单 {len(algos)} 个")
            else:
                logger.info(f"{ccxt_symbol} 无算法单需要撤销")
        except Exception as e:
            logger.warning(f"查询/撤销算法单失败: {e}")
            algo_ok = False

        if normal_ok and algo_ok:
            return True
        if not normal_ok:
            logger.error(f"{ccxt_symbol} 撤销普通挂单失败，可能存在残留订单！")
        if not algo_ok:
            logger.error(f"{ccxt_symbol} 撤销算法单失败，可能存在残留止损单！")
        return False
