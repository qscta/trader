import ccxt
import logging
import math
import time

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
        super().__init__(config)
        # 保证金模式：cross（全仓）/ isolated（逐仓）
        self.margin_mode = (config.get('margin_mode') or 'cross').lower()
        # 杠杆：默认值 + 可按内部符号覆盖，如 {"BTCUSDT": 10}
        self.default_leverage = config.get('leverage', 5)
        self.leverage_overrides = config.get('leverage_overrides', {}) or {}

        self._contract_size_cache = {}     # ccxt_symbol -> contractSize（每张多少币）
        self._amount_precision_cache = {}  # ccxt_symbol -> 张数小数位
        self._price_tick_cache = {}        # ccxt_symbol -> 价格最小变动单位（tick）
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
                    price_step = (market.get('precision') or {}).get('price')
                    if price_step:
                        self._price_tick_cache[sym] = float(price_step)
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
        """币数 -> 张数（按交易所张数步长取整）。面值不可得时向上抛出（拒绝交易）。"""
        contract_size = self._get_contract_size(ccxt_symbol)
        raw_contracts = coin_amount / contract_size
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

    def _get_price_tick(self, ccxt_symbol):
        """价格最小变动单位（tick）。取不到返回 None——止损单匹配退回原严格容差（fail-safe）。"""
        if ccxt_symbol in self._price_tick_cache:
            return self._price_tick_cache[ccxt_symbol]
        try:
            step = (self.exchange.market(ccxt_symbol).get('precision') or {}).get('price')
            step = float(step) if step else None
        except Exception:
            return None
        if step and step > 0:
            self._price_tick_cache[ccxt_symbol] = step
            return step
        return None

    # ===================== 杠杆 / 持仓模式 =====================

    def _ensure_one_way_mode(self):
        """切换为单向(净)持仓模式。若账户已有持仓/挂单或已是该模式会报错，记录即可。"""
        try:
            self.exchange.set_position_mode(False)  # False = 单向(net)
            logger.info("OKX 已设置为单向(净)持仓模式")
        except Exception as e:
            logger.info(f"OKX 设置单向持仓模式跳过/失败（可能已是该模式或已有持仓）: {e}")

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
        若原样外泄，上层（币安时代写下的）`contracts == 0` / `contracts > 0`
        判断会因 None 误判甚至 TypeError——统一在适配层归一化掉。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        positions = self.exchange.fetch_positions([ccxt_symbol])
        for p in positions or []:
            if p and p.get('contracts') and abs(float(p['contracts'])) > 0:
                return p
        return None

    def _position_contracts(self, ccxt_symbol):
        """查询当前持仓张数；查询失败时向上抛出（保留与币安一致的错误语义）。"""
        position = self.get_position(ccxt_symbol)
        if position and position.get('contracts'):
            return abs(float(position['contracts']))
        return 0.0

    # ===================== 写操作：超时后查询确认 =====================

    def open_position(self, symbol, side, amount):
        """安全开仓（市价单）。amount 单位为币数。"""
        ccxt_symbol = self._resolve_symbol(symbol)
        order_side = 'buy' if side == 'long' else 'sell'

        self.setup_symbol(ccxt_symbol)

        contracts = self._coin_to_contracts(ccxt_symbol, amount)
        if contracts <= 0:
            logger.error(f"{ccxt_symbol} 开仓张数为0（amount={amount}币，不足一张），放弃开仓")
            return None

        pre_position = None
        try:
            pre_position = self.get_position(ccxt_symbol)
        except Exception as e:
            logger.warning(f"开仓前查询持仓失败: {e}，继续开仓...")
        pre_contracts = abs(float(pre_position['contracts'])) if pre_position and pre_position.get('contracts') else 0.0

        try:
            order = self.exchange.create_order(
                ccxt_symbol, 'market', order_side, contracts, None, self._order_params()
            )
            logger.info(f"开仓成功: {ccxt_symbol} {side} {contracts}张(≈{amount}币), 订单ID={order.get('id')}")
            return order

        except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
            logger.warning(f"开仓请求超时: {e}，查询持仓确认是否已成交...")
            time.sleep(2)

            for check in range(3):
                try:
                    post_contracts = self._position_contracts(ccxt_symbol)
                    if post_contracts > pre_contracts:
                        logger.info(f"确认开仓已成交: 持仓从 {pre_contracts} 变为 {post_contracts} 张")
                        return {
                            'id': 'timeout_confirmed',
                            'average': None,
                            # 对外契约是币数：张数差换算回币，张数不外泄
                            'amount': (post_contracts - pre_contracts) * self._get_contract_size(ccxt_symbol),
                            'status': 'closed',
                            'info': '超时后通过持仓查询确认已成交'
                        }
                    else:
                        if check < 2:
                            logger.info(f"持仓未变化，等待2秒后第{check+2}次确认...")
                            time.sleep(2)
                except Exception as check_e:
                    logger.warning(f"确认查询失败: {check_e}")
                    time.sleep(2)

            logger.error(f"开仓超时且确认未成交: {ccxt_symbol} {side} {amount}")
            return None

        except (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.BadRequest) as e:
            logger.error(f"开仓业务异常: {e}")
            return None
        except Exception as e:
            logger.error(f"开仓未知异常: {e}")
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
            logger.warning(f"{ccxt_symbol} 欧易端已无持仓，跳过平仓指令（可能已手动平仓）")
            return {'id': 'already_closed', 'average': None, 'status': 'closed'}

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
            logger.info(f"平仓成功: {ccxt_symbol} {side} {contracts}张, 订单ID={order.get('id')}")
            return order

        except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
            logger.warning(f"平仓请求超时: {e}，查询持仓确认...")
            time.sleep(2)

            for check in range(3):
                try:
                    post_contracts = self._position_contracts(ccxt_symbol)
                    if post_contracts < pre_contracts:
                        logger.info(f"确认平仓已成交: 持仓从 {pre_contracts} 变为 {post_contracts} 张")
                        return {
                            'id': 'timeout_confirmed',
                            'average': None,
                            # 对外契约是币数：张数差换算回币，张数不外泄
                            'amount': (pre_contracts - post_contracts) * self._get_contract_size(ccxt_symbol),
                            'status': 'closed',
                            'info': '超时后通过持仓查询确认已平仓'
                        }
                    else:
                        if check < 2:
                            logger.info(f"持仓未变化，等待2秒后第{check+2}次确认...")
                            time.sleep(2)
                except Exception as check_e:
                    logger.warning(f"确认查询失败: {check_e}")
                    time.sleep(2)

            logger.error(f"平仓超时且确认未成交: {ccxt_symbol} {side} {amount}")
            return None

        except Exception as e:
            logger.error(f"平仓异常: {e}")
            return None

    @staticmethod
    def _algo_order_matches(order, stop_side, stop_price, contracts, price_tick=None):
        """判断一张算法单是否就是「我们刚下的那张止损单」：方向 + 触发价 + 张数全部吻合。

        超时确认只按方向匹配会把残留的旧止损误认成新单，导致本地记录的止损价
        与交易所实际不一致。任何字段读不到一律视为不匹配（宁可重试创建，也不误认）。

        price_tick 提供时，触发价容差放宽到一个 tick（×1.001 抗浮点）：交易所可能把
        触发价按 tick 取整，严格 1ppm 匹配会把「实际已落单」误判成未落单——超时重试
        路径因此再建一张，留下双止损/孤儿单。一个 tick 内不可能同时存在两张我方止损
        （撤旧都是验证式确认后才建新，残留则直接阻断建新），放宽不引入误认。
        """
        if not order or order.get('side') != stop_side:
            return False
        info = order.get('info') or {}
        trigger = (order.get('stopLossPrice') or order.get('triggerPrice') or order.get('stopPrice')
                   or info.get('slTriggerPx') or info.get('triggerPx'))
        try:
            trigger = float(trigger)
            stop_price = float(stop_price)
        except (TypeError, ValueError):
            return False
        tolerance = max(1e-8, abs(stop_price) * 1e-6)
        if price_tick:
            tolerance = max(tolerance, float(price_tick) * 1.001)
        if abs(trigger - stop_price) > tolerance:
            return False
        amount = order.get('amount')
        if amount is None:
            amount = info.get('sz')
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return False
        return abs(amount - float(contracts)) <= max(1e-8, abs(float(contracts)) * 1e-6)

    def create_stop_loss_order(self, symbol, side, amount, stop_price):
        """创建止损算法单（reduce-only，触发后市价平仓）。amount 单位为币数。"""
        ccxt_symbol = self._resolve_symbol(symbol)
        stop_side = 'sell' if side == 'long' else 'buy'

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
        price_tick = self._get_price_tick(ccxt_symbol)  # 超时确认按 tick 容差匹配，防交易所取整致重复建单

        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                order = self.exchange.create_order(ccxt_symbol, 'market', stop_side, contracts, None, params)
                logger.info(f"止损单创建成功: {ccxt_symbol} {stop_side} {contracts}张 @ {stop_price}, 订单ID={order.get('id')}")
                return order

            except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
                logger.warning(f"[止损单 尝试{attempt+1}/{max_attempts}] 超时: {e}，查询确认...")
                time.sleep(2)

                try:
                    for o in self._fetch_algo_orders(ccxt_symbol):
                        # 必须方向+触发价+张数全匹配，防止把残留旧止损误认成本次新单
                        if self._algo_order_matches(o, stop_side, stop_price, contracts, price_tick=price_tick):
                            logger.info(f"确认本次止损单已存在: 订单ID={o.get('id')}")
                            return o
                except Exception as check_e:
                    logger.warning(f"查询算法单失败: {check_e}")

                if attempt < max_attempts - 1:
                    logger.info("止损单未确认，2秒后重试...")
                    time.sleep(2)

            except (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.BadRequest) as e:
                logger.error(f"止损单业务异常: {e}")
                return None
            except Exception as e:
                logger.error(f"止损单未知异常: {e}")
                return None

        logger.error(f"止损单创建失败（{max_attempts}次尝试）: {ccxt_symbol} @ {stop_price}")
        return None

    @retry_on_network_error(max_retries=3)
    def list_position_symbols(self):
        """交易所端当前有实际持仓的内部符号列表（启动时反向核对孤儿仓用）。"""
        symbols = []
        for p in self.exchange.fetch_positions() or []:
            if p and p.get('contracts') and abs(float(p['contracts'])) > 0 and p.get('symbol'):
                symbols.append(self.to_internal_symbol(p['symbol']))
        return symbols

    def find_stop_order_state(self, symbol, side, amount, stop_price, stop_order_id=None):
        """检查与「本地持仓记录」对应的止损算法单状态（供主层止损自愈巡检使用）。

        amount 为币数，张数换算在本方法内部完成（张数不外泄）。返回三态：
          'intact'   — 存在方向+触发价+张数与本地记录一致的算法单（触发价按一个 tick
                       容差比对，抗交易所取整；方向与张数严格），保护完整；
          'mismatch' — 本地记录的 stop_order_id 还在列表里，但内容与本地记录不符
                       （异常状态：可能被人工改挂过，自动补挂会造成双止损，须人工核对）；
          'missing'  — 列表中不存在匹配的止损单（需要补挂）。
        查询/换算失败向上抛出，调用方按 fail-safe 跳过本轮。
        """
        ccxt_symbol = self._resolve_symbol(symbol)
        stop_side = 'sell' if side == 'long' else 'buy'
        contracts = self._coin_to_contracts(ccxt_symbol, amount)
        price_tick = self._get_price_tick(ccxt_symbol)  # 与创建路径同一容差口径（tick 取整不算 mismatch）
        algos = self._fetch_algo_orders(ccxt_symbol)
        for o in algos:
            if self._algo_order_matches(o, stop_side, stop_price, contracts, price_tick=price_tick):
                return 'intact'
        if stop_order_id and any(str(o.get('id')) == str(stop_order_id) for o in algos):
            return 'mismatch'
        return 'missing'

    # ===================== 撤单（含算法单） =====================

    def _fetch_algo_orders(self, ccxt_symbol):
        """查询未触发的算法/条件单（止损单）。不同 ccxt 版本参数名不同。

        三种参数组合**全部尝试并按订单 id 合并**：某个组合可能"成功但返回空"，
        真正能查到算法单的是另一个组合，只取第一个成功结果会误判「无算法单」。
        只有全部组合都失败才抛出（返回空列表会被当成"已撤干净"，绝不允许）。
        """
        merged = {}
        any_success = False
        last_err = None
        for params in ({'ordType': 'conditional'}, {'trigger': True}, {'stop': True}):
            try:
                for o in self.exchange.fetch_open_orders(ccxt_symbol, params=params):
                    merged.setdefault(str(o.get('id')), o)
                any_success = True
            except Exception as e:
                last_err = e
        if not any_success:
            raise last_err if last_err else RuntimeError('查询算法单失败')
        return list(merged.values())

    def _algo_order_absent(self, ccxt_symbol, order_id):
        """查询算法单列表，确认目标 id 已不存在。查询失败时向上抛出（不可确认 ≠ 已撤干净）。"""
        for o in self._fetch_algo_orders(ccxt_symbol):
            if str(o.get('id')) == str(order_id):
                return False
        return True

    def _cancel_algo_order(self, ccxt_symbol, order_id):
        """撤销算法单，并以「列表里查不到该 id」为成功标准。

        不能把 OrderNotFound 直接当成功：某种参数组合下交易所把该 id 当普通单
        查找也会报 OrderNotFound，实际算法单可能还活着。撤销必须可验证。
        首次复核仍在列表时，等待片刻复查一次再裁决——列表可能滞后于撤单生效。
        """
        for params in ({'trigger': True}, {'stop': True}, {'algo': True}):
            try:
                self.exchange.cancel_order(order_id, ccxt_symbol, params)
                break
            except Exception:
                continue
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

        # 普通挂单
        try:
            self.exchange.cancel_all_orders(ccxt_symbol)
            logger.info(f"已撤销 {ccxt_symbol} 普通挂单")
        except ccxt.NotSupported:
            try:
                for o in self.exchange.fetch_open_orders(ccxt_symbol):
                    self.exchange.cancel_order(o['id'], ccxt_symbol)
                logger.info(f"已逐个撤销 {ccxt_symbol} 普通挂单")
            except Exception as e:
                logger.warning(f"逐个撤销普通挂单失败: {e}")
                normal_ok = False
        except Exception as e:
            if 'no open orders' in str(e).lower() or '51603' in str(e):
                logger.info(f"{ccxt_symbol} 无普通挂单需要撤销")
            else:
                logger.warning(f"撤销普通挂单失败: {e}")
                normal_ok = False

        # 算法止损单
        try:
            algos = self._fetch_algo_orders(ccxt_symbol)
            for o in algos:
                if not self._cancel_algo_order(ccxt_symbol, o.get('id')):
                    logger.warning(f"撤销算法单 {o.get('id')} 失败")
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
