"""OKX 止损 / 合约张数 / 撤单 验证脚本 —— 上欧易实盘前的最后一道红线。

代码层无法自证、必须真连交易所验证的部分（多轮审查都强调）。
**强烈建议先用 OKX 模拟盘（demo trading）跑**，确认无误再小额实盘。

用法：
    export OKX_API_KEY=...  OKX_API_SECRET=...  OKX_API_PASSPHRASE=...
    export OKX_DEMO=1                      # 用 OKX 模拟盘（推荐！实盘则不设）
    python verify_okx.py BTCUSDT 0.01              # 默认 long+short 两条路径都测
    python verify_okx.py BTCUSDT 0.01 --side short # 只测做空
    python verify_okx.py BTCUSDT                   # 不带币数 = 只读检查（余额/张数/单向模式）
    python verify_okx.py BTCUSDT 0.01 --side long --fire   # 实弹触发验证（见下）
    python verify_okx.py BTCUSDT 0.01 --side long --stop-id-reuse  # 止损幂等 ID 终态复用验证（见下）

它用项目里的 OkxApi **真实代码路径**，对每个方向跑：
    换张数 → 开仓 → 挂止损 → 查算法单 → 撤止损 → 平仓（finally 保证平仓清理）
逐项人工核对：
    1) 张数换算是否符合预期（合约面值 contractSize）
    2) 止损是否 reduce-only 条件单（做多=sell、做空=buy）、触发后是否市价平仓
    3) cancel_order / 算法单查询能否把止损撤干净
    4) 平仓后是否干净
    5) 账户是否单向(净)持仓模式（本系统的硬前提）

--fire 模式（实弹触发验证，需显式 --side long/short，不支持 both）：
    以上 5 项测的都是「挂着但不触发」的止损。reduce-only 标志本身已被证实
    接受存储，但「触发那一瞬间是否真的只减仓、绝不反向」此前完全依赖 OKX
    平台承诺，从未被本系统实测过。--fire 把止损挂在离市价很近处（默认
    0.15%），等真实行情自然触发，断言：触发后持仓精确归零、无反向仓、
    算法单从待触发列表消失。超时未触发（默认 300s）判「不确定」而非失败，
    清理后可用 --fire-distance / --fire-timeout 调整重试。

--stop-id-reuse 模式（止损幂等 ID 终态复用验证，需显式 --side long/short）：
    系统的止损 algoClOrdId 由（品种|方向|张数|触发价）四元组哈希确定性派生
    ——同一保护意图的崩溃重试必须命中同一 ID，这是幂等防重发的根基。它隐含
    一个从未被实测的假设：**旧单进入终态（已撤销/已触发）后，同一 algoClOrdId
    可以再次用于新的 POST**。若 OKX 对历史终态算法单也强制 ID 唯一，则
    「撤销后按相同参数补挂」会被交易所拒绝——系统会大声失败（开仓路径回滚、
    巡检路径告警+隔离），不丢钱，但会牺牲入场机会且需要人工介入。
    本模式在真实代码路径上完成：挂止损 → 验证式撤销（终态）→ 用完全相同的
    参数再挂一次（适配层派生出同一个 algoClOrdId）→ 裁决交易所接受/拒绝。
    本模式验证的是「已撤销」这一终态变体；「已触发」同为终态，OKX 的唯一性
    约束通常不区分两者，但如需完全确证，可在同参数 --fire 触发后手工加测。
"""
import os
import sys
import time
import argparse
import math

import config_validation as cfgv
from okx_api import OkxApi
from trade_state import load_strict_json, open_private_text_file


def load_cfg():
    demo_env = os.environ.get('OKX_DEMO')
    if demo_env is None:
        sandbox = False
    elif demo_env == '1':
        sandbox = True
    elif demo_env == '0':
        sandbox = False
    else:
        sandbox = cfgv.strict_bool(demo_env, 'OKX_DEMO')
    passphrase = cfgv.resolve_optional_alias(
        os.environ.get('OKX_API_PASSPHRASE'),
        os.environ.get('OKX_PASSWORD'),
        'OKX passphrase 环境变量')
    cfg = {
        'apiKey': os.environ.get('OKX_API_KEY'),
        'secret': os.environ.get('OKX_API_SECRET'),
        'password': passphrase,
        'sandbox': sandbox,
    }
    if 'OKX_MARGIN_MODE' in os.environ:
        cfg['margin_mode'] = os.environ['OKX_MARGIN_MODE']
    if 'OKX_LEVERAGE' in os.environ:
        cfg['leverage'] = os.environ['OKX_LEVERAGE']
    # config.json 补齐缺失项
    cfgfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
    if os.path.exists(cfgfile):
        try:
            with open_private_text_file(cfgfile) as f:
                raw = load_strict_json(f)
            cfgv.canonicalize_single_okx_config(raw)
            okx = raw.get('okx', {})
            cfgv.validate_and_normalize_okx_config(okx)
            for k in ('apiKey', 'secret', 'password', 'margin_mode', 'leverage', 'leverage_overrides'):
                if ((k in ('apiKey', 'secret', 'password') and not cfg.get(k)) or
                        (k not in cfg)) and k in okx:
                    cfg[k] = okx[k]
            # sandbox 跟随 config.json（环境变量 OKX_DEMO 优先）——防止 config 是模拟盘而脚本误连实盘
            if demo_env is None:
                environment = {
                    key: okx[key] for key in ('sandbox', 'demo') if key in okx
                }
                cfgv.validate_and_normalize_okx_environment(environment)
                cfg['sandbox'] = bool(
                    environment.get('sandbox', False) or
                    environment.get('demo', False))
        except Exception as e:
            print('读取 config.json 失败:', e)
            raise ValueError(f'无法安全读取验证配置: {e}') from e
    cfg.setdefault('margin_mode', 'cross')
    cfg.setdefault('leverage', 5)
    cfgv.validate_and_normalize_okx_config(cfg)
    return cfg


def check_position_mode(api):
    """明确检查 OKX 单向(净)持仓模式。返回 True/False/None。"""
    print("\n--- 持仓模式检查（本系统硬前提：单向/净持仓）---")
    try:
        acc = api.exchange.privateGetAccountConfig()
        pos_mode = ((acc.get('data') or [{}])[0]).get('posMode')
        print(f"  账户 posMode = {pos_mode}（期望 net_mode）")
        if pos_mode == 'net_mode':
            print("  ✓ 单向持仓模式")
            return True
        print("  ⚠️⚠️ 不是单向模式！本系统假设单向，上线前必须到欧易把合约持仓模式改为「单向」。")
        return False
    except Exception as e:
        print(f"  ⚠️ 无法自动确认持仓模式: {e}")
        print("  ⚠️ 上线前必须人工到欧易确认账户为「单向(净)持仓模式」——这是硬条件。")
        return None


def confirm_open_order_contract(order, tag):
    """只接受适配层已完整确认且无任何未决/歧义标记的开仓契约。"""
    if not isinstance(order, dict):
        print(f'[{tag}] ❌ 开仓未返回结构化成交契约')
        return False
    if order.get('confirmed') is not True:
        print(f'[{tag}] ❌ 开仓订单未获终态确认')
        return False
    if order.get('fully_filled') is not True:
        print(f'[{tag}] ❌ 开仓未完整成交，本次验证不构成证据')
        return False
    unsafe_flags = (
        'open_execution_unresolved', 'open_order_may_remain_live',
        'open_execution_attribution_ambiguous',
        'open_execution_compensated', 'execution_ambiguous',
    )
    present = [key for key in unsafe_flags if order.get(key)]
    if present:
        print(f'[{tag}] ❌ 开仓契约含未决/歧义标记: {present}')
        return False
    amount_value = order.get('amount')
    if isinstance(amount_value, bool):
        print(f'[{tag}] ❌ 开仓成交量非法: {amount_value!r}')
        return False
    try:
        amount = float(amount_value)
    except (TypeError, ValueError):
        amount = None
    if amount is None or not math.isfinite(amount) or amount <= 0:
        print(f'[{tag}] ❌ 开仓成交量不可确认: {amount_value!r}')
        return False
    return True


def confirm_open_position(api, ccxt_symbol, expected_side, tag,
                          expected_contracts=None, require_entry=False):
    """ACK 后必须由真实仓位快照证明开仓成功；未知绝不算验证证据。"""
    try:
        position = api.get_position(ccxt_symbol)
    except Exception as exc:
        print(f'[{tag}] ❌ 开仓 ACK 后无法回读真实持仓: {exc}')
        return None
    if not isinstance(position, dict):
        print(f'[{tag}] ❌ 开仓 ACK 后未形成可确认持仓')
        return None
    contracts_value = position.get('contracts')
    if isinstance(contracts_value, bool):
        print(f'[{tag}] ❌ 持仓张数字段非法: {contracts_value!r}')
        return None
    try:
        contracts = abs(float(contracts_value))
    except (TypeError, ValueError):
        contracts = None
    if contracts is None or not math.isfinite(contracts) or contracts <= 0:
        print(f'[{tag}] ❌ 开仓 ACK 后持仓张数不可确认: {contracts_value!r}')
        return None
    if expected_contracts is not None:
        try:
            expected = abs(float(expected_contracts))
        except (TypeError, ValueError):
            expected = None
        if (expected is None or not math.isfinite(expected) or expected <= 0 or
                not math.isclose(
                    contracts, expected, rel_tol=1e-9, abs_tol=1e-9)):
            print(f'[{tag}] ❌ 真实持仓张数 {contracts} 与本次请求 '
                  f'{expected_contracts!r} 不一致')
            return None
    if position.get('side') != expected_side:
        print(f'[{tag}] ❌ 开仓方向不符: '
              f'{position.get("side")!r}（期望 {expected_side}）')
        return None
    if require_entry:
        entry_value = position.get('entryPrice')
        try:
            entry = float(entry_value)
        except (TypeError, ValueError):
            entry = None
        if entry is None or not math.isfinite(entry) or entry <= 0:
            print(f'[{tag}] ❌ 入场价不可确认: {entry_value!r}')
            return None
    return position


def parse_position_snapshot(position, tag):
    """把 None 解释为空仓；其它响应必须完整证明有限张数与有效方向。"""
    if position is None:
        return 0.0, None
    if not isinstance(position, dict):
        print(f'[{tag}] ❌ 持仓快照结构非法: {type(position).__name__}')
        return None
    value = position.get('contracts')
    if isinstance(value, bool):
        print(f'[{tag}] ❌ 持仓快照张数非法: {value!r}')
        return None
    try:
        contracts = abs(float(value))
    except (TypeError, ValueError):
        contracts = None
    if contracts is None or not math.isfinite(contracts):
        print(f'[{tag}] ❌ 持仓快照张数不可确认: {value!r}')
        return None
    side = position.get('side')
    if contracts > 0 and side not in ('long', 'short'):
        print(f'[{tag}] ❌ 非零持仓方向不可确认: {side!r}')
        return None
    if contracts == 0 and side not in (None, 'long', 'short'):
        print(f'[{tag}] ❌ 零仓快照方向字段非法: {side!r}')
        return None
    return contracts, side


def require_flat_before_live_test(api, ccxt_symbol, tag):
    """实弹写入前必须证明目标品种空仓，清理才不会误平既有仓位。"""
    try:
        snapshot = parse_position_snapshot(
            api.get_position(ccxt_symbol), tag)
    except Exception as exc:
        print(f'[{tag}] ❌ 实弹前无法确认目标品种空仓: {exc}')
        return False
    if snapshot is None:
        return False
    contracts, side = snapshot
    if contracts != 0:
        print(f'[{tag}] ❌ 实弹前已有 {contracts} 张 {side} 仓位，拒绝测试和盲目清理')
        return False
    return True


def cleanup_live_position(api, ccxt_symbol, expected_side, expected_coin, tag):
    """按交易所当前真实方向/数量清理验证仓位；撤单失败也必须继续平仓。

    清理期间止损仍可能成交并让仓位状态发生变化。因此首次平仓后若仍有仓位，
    必须重新读取交易所方向/数量并限次补平，不能沿用清理开始时的快照。
    """
    cleanup_ok = True

    def coin_for_contracts(contracts, fallback):
        """尽量按最新真实张数换算币数；换算失败时仍保留原清理机会。"""
        converter = getattr(api, '_contracts_to_coins', None)
        if contracts > 0 and callable(converter):
            try:
                converted = float(converter(ccxt_symbol, contracts))
                if math.isfinite(converted) and converted > 0:
                    return converted
            except Exception as e:
                # 验证脚本的首要目标是尽力把实弹测试仓清干净。合约面值缓存
                # 异常（ContractSizeUnavailable 是 RuntimeError）也不能在进入
                # reduce-only 平仓前打断 finally；回退调用者原始币数继续尝试。
                print(f"[{tag}] ⚠️ 真实张数换算币数失败，回退原数量清理:", e)
        return fallback

    try:
        if api.cancel_all_orders(ccxt_symbol) is not True:
            # 适配器的完整撤净契约包含“最终空仓”。此刻测试仓仍在，False
            # 只表示尚未完成；平仓后会再次严格撤净，不能永久锁存假失败。
            print(f"[{tag}] ⚠️ 平仓前撤单尚不能证明完整清理，继续平仓后复核")
    except Exception as e:
        # 最终裁决只看平仓后的严格撤净与空仓复核。
        print(f"[{tag}] ⚠️ 平仓前撤单异常，继续平仓后复核:", e)

    actual_position = None
    try:
        actual_position = api.get_position(ccxt_symbol)
    except Exception as e:
        cleanup_ok = False
        print(f"[{tag}] ⚠️ 清理前无法读取真实仓位，回退原方向/数量尝试平仓:", e)

    actual_snapshot = parse_position_snapshot(actual_position, tag)
    if actual_snapshot is None:
        cleanup_ok = False
        actual_side = expected_side
        contracts = 0.0
    else:
        contracts, actual_side = actual_snapshot
        actual_side = actual_side or expected_side
    close_coin = coin_for_contracts(contracts, expected_coin)
    if actual_side != expected_side:
        print(f"[{tag}] 🔴 清理检测到方向已变化: {expected_side} → {actual_side}，"
              "改按交易所真实方向 reduce-only 平仓")

    try:
        close_order = api.close_position(
            ccxt_symbol, actual_side, close_coin)
        if close_order:
            print(f"[{tag}] 清理平仓返回:", close_order.get('id'))
    except Exception as e:
        cleanup_ok = False
        print(f"[{tag}] ❌ 清理平仓请求失败:", e)

    # 平仓后再撤一次：首次撤单可能只是瞬时失败；若残留 reduce-only 止损
    # 一直挂着，未来同品种新仓仍可能被旧单干扰。
    try:
        if api.cancel_all_orders(ccxt_symbol) is not True:
            cleanup_ok = False
            print(f"[{tag}] ⚠️ 平仓后残留撤单返回失败")
    except Exception as e:
        cleanup_ok = False
        print(f"[{tag}] ⚠️ 平仓后残留撤单仍失败:", e)

    time.sleep(2)
    try:
        after = api.get_position(ccxt_symbol)
        after_snapshot = parse_position_snapshot(after, tag)
        if after_snapshot is None:
            cleanup_ok = False
            remaining = 0.0
            retry_side = None
        else:
            remaining, retry_side = after_snapshot
        print(f"[{tag}] 清理后剩余持仓（应 0）: {remaining}")
        if remaining:
            # 极窄但真实的竞态：首次撤单失败后，原止损可能在首次平仓调用前
            # 触发，使适配层因方向快照已过期而拒绝平仓。只按旧方向重试会继续
            # 失败，所以必须以此刻交易所返回的方向/数量补做一次有界清理。
            if retry_side in ('long', 'short'):
                retry_coin = coin_for_contracts(remaining, expected_coin)
                print(f"[{tag}] ⚠️ 检测到清理竞态，按最新仓位补平: "
                      f"{remaining} 张 {retry_side}")
                try:
                    retry_order = api.close_position(
                        ccxt_symbol, retry_side, retry_coin)
                    if retry_order:
                        print(f"[{tag}] 补平返回:", retry_order.get('id'))
                except Exception as e:
                    cleanup_ok = False
                    print(f"[{tag}] ❌ 补平请求失败:", e)

                # 补平后再清一次算法单，防止第二次快照期间又出现已失效残单。
                try:
                    if api.cancel_all_orders(ccxt_symbol) is not True:
                        cleanup_ok = False
                        print(f"[{tag}] ⚠️ 补平后残留撤单返回失败")
                except Exception as e:
                    cleanup_ok = False
                    print(f"[{tag}] ⚠️ 补平后残留撤单失败:", e)

                time.sleep(2)
                after = api.get_position(ccxt_symbol)
                final_snapshot = parse_position_snapshot(after, tag)
                if final_snapshot is None:
                    cleanup_ok = False
                    remaining = 0.0
                else:
                    remaining, _final_side = final_snapshot
                print(f"[{tag}] 补平后剩余持仓（应 0）: {remaining}")

            if remaining:
                cleanup_ok = False
                print(f"[{tag}] ⚠️⚠️ 仍有残留持仓，请立即手动到欧易平掉！")
    except Exception as e:
        cleanup_ok = False
        print(f"[{tag}] ❌ 清理后仓位无法确认，请立即人工核查:", e)
    return cleanup_ok


def _run_side_exercise(api, ccxt_symbol, coin, side, order, contracts):
    """基础实弹验证主体；开仓尝试与最终清理由外层统一包住。"""
    ok = confirm_open_order_contract(order, side)
    if ok:
        time.sleep(2)
        pos = confirm_open_position(
            api, ccxt_symbol, side, side,
            expected_contracts=contracts, require_entry=True)
        print(f"[{side}] 持仓:", {k: (pos or {}).get(k) for k in ('contracts', 'side', 'entryPrice')} if pos else None)
        if pos is None:
            ok = False
    if ok:
        entry = float(pos['entryPrice'])
        # 多单止损在下方(-10%)、空单止损在上方(+10%)，远离市价不期望触发
        stop_px = round(entry * (0.9 if side == 'long' else 1.1), 6)
        close_dir = 'sell' if side == 'long' else 'buy'
        print(f"[{side}] 挂止损 @ {stop_px}（应为 {close_dir} reduceOnly 条件单）")
        stop = api.create_stop_loss_order(ccxt_symbol, side, coin, stop_px)
        print(f"[{side}] 止损返回:", (stop or {}).get('id'))
        if not stop:
            print(f"[{side}] ⚠️ 止损创建失败 —— OKX 适配最关键点，请检查 create_stop_loss_order")
            ok = False
        time.sleep(2)

        algos = api._fetch_algo_orders(ccxt_symbol)
        print(f"[{side}] 查到算法/条件单 {len(algos)} 个（原生 orders-algo-pending 端点）:")
        for algo in algos:
            info = algo.get('info') or {}
            print("    -", {'id': algo.get('id'), 'side': algo.get('side'),
                            'reduceOnly': algo.get('reduceOnly'),
                            'ordType': info.get('ordType'),
                            'slTriggerPx': info.get('slTriggerPx')})
        if not algos:
            print(f"[{side}] ⚠️ 没查到算法止损单！可能没挂上或查询路径异常。")
            ok = False

        # 四态裁决全链路：原生查询 + 方向/触发价/张数严格匹配（止损自愈巡检同一路径）
        try:
            state = api.find_stop_order_state(
                ccxt_symbol, side, coin, stop_px, (stop or {}).get('id'))
            print(f"[{side}] find_stop_order_state = {state}（应为 intact）")
            if state != 'intact':
                print(f"[{side}] ⚠️ 四态裁决未返回 intact！严格匹配（触发价 tick 取整/张数）需人工核对。")
                ok = False
        except Exception as exc:
            print(f"[{side}] ⚠️ 四态裁决异常: {exc}")
            ok = False

        print(f"[{side}] 撤止损 ...")
        if stop and stop.get('id'):
            cancelled = api.cancel_order(ccxt_symbol, stop['id'])
            print(f"[{side}] cancel_order:", cancelled)
        else:
            cancelled = api.cancel_all_orders(ccxt_symbol)
            print(f"[{side}] cancel_all_orders:", cancelled)
        if cancelled is not True:
            print(f"[{side}] ⚠️ 撤止损未获确认")
            ok = False
        time.sleep(2)
        left = api._fetch_algo_orders(ccxt_symbol)
        print(f"[{side}] 撤后算法单（应 0）: {len(left)}")
        if left:
            print(f"[{side}] ⚠️ 仍有残留算法单！")
            ok = False
    return ok


def run_side(api, ccxt_symbol, coin, side):
    """对单个方向跑一轮；任何开仓结果都进入最终清理。"""
    print(f"\n{'#' * 60}\n方向: {side.upper()}（{'做多' if side == 'long' else '做空'}）\n{'#' * 60}")
    if not require_flat_before_live_test(api, ccxt_symbol, side):
        return False
    contracts = api._coin_to_contracts(ccxt_symbol, coin)
    print(f"换算：{coin} 币 → {contracts} 张")
    ok = False
    try:
        try:
            order = api.open_position(ccxt_symbol, side, coin)
            print(f"[{side}] 开仓返回:", (order or {}).get('id'))
            if not order:
                print(f"[{side}] ❌ 开仓失败或结果无法确认")
            else:
                ok = _run_side_exercise(
                    api, ccxt_symbol, coin, side, order, contracts)
        except Exception as exc:
            print(f"[{side}] ❌ 开仓/验证异常，转入强制清理: {exc}")
    finally:
        print(f"[{side}] --- 平仓 / 清理（保证执行）---")
        if not cleanup_live_position(
                api, ccxt_symbol, side, coin, side):
            ok = False
    return ok


def _run_fire_exercise(api, ccxt_symbol, coin, side, order,
                       expected_contracts, distance_pct, timeout_seconds,
                       poll_interval):
    """执行触发试验主体；清理统一由外层裁决，避免 return 绕过清理结果。"""
    tag = f'fire-{side}'
    if not confirm_open_order_contract(order, tag):
        return None
    time.sleep(2)
    if confirm_open_position(
            api, ccxt_symbol, side, tag,
            expected_contracts=expected_contracts) is None:
        return None
    last_price = api.get_last_price(ccxt_symbol)
    stop_px = (
        last_price * (1 - distance_pct / 100)
        if side == 'long' else last_price * (1 + distance_pct / 100))
    print(f"[{tag}] 市价={last_price}, 挂止损 @ {stop_px:.6f}")

    stop = api.create_stop_loss_order(ccxt_symbol, side, coin, stop_px)
    stop_id = (stop or {}).get('id')
    print(f"[{tag}] 止损返回:", stop_id)
    if not isinstance(stop, dict) or not stop_id:
        print(f"[{tag}] ❌ 止损创建失败或缺少可追踪 ID，无法继续验证")
        return None
    state = api.find_stop_order_state(
        ccxt_symbol, side, coin, stop_px, stop_id)
    if state != 'intact':
        print(f"[{tag}] ❌ 止损内容未获 intact 确认: {state!r}")
        return None

    print(f"[{tag}] 等待自然行情触发...")
    start = time.time()
    triggered = False
    reverse_observed = False
    partial_fill_observed = False
    initial_contracts = float(expected_contracts)
    while time.time() - start < timeout_seconds:
        pos = api.get_position(ccxt_symbol)
        snapshot = parse_position_snapshot(pos, tag)
        if snapshot is None:
            return None
        contracts, observed_side = snapshot
        if contracts == 0:
            triggered = True
            break
        if observed_side and observed_side != side:
            reverse_observed = True
            triggered = True
            print(f"[{tag}] 🔴 轮询直接观察到反向仓: {contracts} 张 {observed_side}")
            break
        if contracts > initial_contracts and not math.isclose(
                contracts, initial_contracts, rel_tol=1e-9, abs_tol=1e-9):
            print(f"[{tag}] ❌ 试验期间仓位由 {initial_contracts} 增至 "
                  f"{contracts} 张，无法唯一归因")
            return False
        if contracts < initial_contracts:
            partial_fill_observed = True
        print(f"[{tag}] 未触发（已等待 {int(time.time() - start)}s，持仓仍在）...")
        time.sleep(poll_interval)

    if not triggered:
        if partial_fill_observed:
            print(f"[{tag}] ❌ 观察到止损部分成交但未在超时前归零，按失败处理。")
            return False
        print(f"[{tag}] ⏱️ 超时未触发（{timeout_seconds}s 内价格未走到止损位），"
              '本次不构成证据，非失败。清理后可调整参数重试。')
        return None

    if reverse_observed:
        print(f"[{tag}] ❌ 检测到止损执行，但仓位直接反向")
    else:
        print(f"[{tag}] 观察到持仓已归零，继续按 algoId 核验止损成交归因")
    time.sleep(2)
    pos_after = api.get_position(ccxt_symbol)
    after_snapshot = parse_position_snapshot(pos_after, tag)
    if after_snapshot is None:
        return None
    contracts_after, side_after = after_snapshot

    ok = not reverse_observed
    if contracts_after > 0:
        print(f"[{tag}] ⚠️⚠️ 触发后仍有持仓 {contracts_after} 张，方向={side_after}！")
        if side_after and side_after != side:
            print(f"[{tag}] 🔴🔴 严重：检测到反向持仓！请立即人工核查交易所！")
        ok = False
    else:
        print(f"[{tag}] ✓ 归零后复核未见反向持仓")

    execution_confirmed = False
    for attempt in range(5):
        try:
            execution_confirmed = api.confirm_stop_execution(
                ccxt_symbol, side, coin, stop_px, stop_id) is True
        except Exception as exc:
            print(f"[{tag}] 止损成交归因第 {attempt + 1} 次查询异常: {exc}")
        if execution_confirmed:
            break
        if attempt < 4:
            time.sleep(2)
    if not execution_confirmed:
        print(f"[{tag}] ❌ 未能证明该 algoId 以止损子订单完整成交；"
              "归零可能来自手动平仓、清算或其它并发动作")
        ok = False
    else:
        print(f"[{tag}] ✓ 算法单 effective 且唯一子订单完整成交，止损归因成立")

    algos = api._fetch_algo_orders(ccxt_symbol)
    still_listed = any(str(item.get('id')) == str(stop_id) for item in algos)
    if still_listed:
        time.sleep(2)
        algos = api._fetch_algo_orders(ccxt_symbol)
        still_listed = any(
            str(item.get('id')) == str(stop_id) for item in algos)
    print(f"[{tag}] 触发后该止损单是否仍在待触发列表（应否）: {still_listed}")
    if still_listed:
        print(f"[{tag}] ⚠️ 触发单未从待触发列表消失，需人工核对状态语义")
        ok = False
    return ok


def run_fire_test(api, ccxt_symbol, coin, side, distance_pct=0.15,
                  timeout_seconds=300, poll_interval=5):
    """开小仓执行真实止损触发试验；主体与清理必须同时成功才算通过。"""
    print(f"\n{'#' * 60}\n实弹触发验证: {side.upper()}（{'做多' if side == 'long' else '做空'}）\n{'#' * 60}")
    print(f"止损距市价 {distance_pct}%，最长等待 {timeout_seconds}s（每 {poll_interval}s 轮询一次）")
    tag = f'fire-{side}'
    if not require_flat_before_live_test(api, ccxt_symbol, tag):
        return False
    expected_contracts = api._coin_to_contracts(ccxt_symbol, coin)
    result = None
    try:
        try:
            order = api.open_position(ccxt_symbol, side, coin)
            print(f"[{tag}] 开仓返回:", (order or {}).get('id'))
            if not order:
                print(f"[{tag}] ❌ 开仓失败或结果无法确认")
            else:
                result = _run_fire_exercise(
                    api, ccxt_symbol, coin, side, order, expected_contracts,
                    distance_pct, timeout_seconds, poll_interval)
        except Exception as exc:
            print(f"[{tag}] ❌ 开仓/验证异常，转入强制清理: {exc}")
    finally:
        print(f"[{tag}] --- 清理（保证执行）---")
        cleanup_ok = cleanup_live_position(
            api, ccxt_symbol, side, coin, tag)
    if not cleanup_ok:
        print(f"[{tag}] ❌ 清理未确认完成，验证强制失败")
        return False
    return result


def _algo_client_order_id(order):
    info = order.get('info') if isinstance(order, dict) else None
    info = info if isinstance(info, dict) else {}
    value = ((order or {}).get('clientOrderId') if isinstance(order, dict)
             else None) or info.get('algoClOrdId') or info.get('clientOrderId')
    return str(value) if value else None


def _run_stop_id_reuse_exercise(
        api, ccxt_symbol, coin, side, order, expected_contracts):
    """执行 ID 复用试验主体；每个成功结论都复用严格四态止损裁决。"""
    tag = f'reuse-{side}'
    if not confirm_open_order_contract(order, tag):
        return None
    time.sleep(2)
    if confirm_open_position(
            api, ccxt_symbol, side, tag,
            expected_contracts=expected_contracts) is None:
        return None
    last_price = api.get_last_price(ccxt_symbol)
    stop_px = round(last_price * (0.9 if side == 'long' else 1.1), 6)
    print(f"[{tag}] 市价={last_price}, 止损价={stop_px}（两次挂单使用同一价）")

    first = api.create_stop_loss_order(ccxt_symbol, side, coin, stop_px)
    first_id = (first or {}).get('id')
    first_client_id = _algo_client_order_id(first)
    print(f"[{tag}] 第一张止损: algoId={first_id}, algoClOrdId={first_client_id}")
    if not isinstance(first, dict) or not first_id or not first_client_id:
        print(f"[{tag}] ❌ 第一张止损创建失败或缺少可追踪 ID，无法继续")
        return None
    first_state = api.find_stop_order_state(
        ccxt_symbol, side, coin, stop_px, first_id)
    if first_state != 'intact':
        print(f"[{tag}] ❌ 第一张止损未获 intact 确认: {first_state!r}")
        return None

    print(f"[{tag}] 验证式撤销第一张止损（撤销确认 = 终态证明）...")
    cancelled = api.cancel_order(ccxt_symbol, first_id)
    print(f"[{tag}] 撤销确认: {cancelled}")
    if cancelled is not True:
        print(f"[{tag}] ❌ 撤销未能确认，无法证明旧单已终态，本次不构成证据")
        return None
    time.sleep(2)

    print(f"[{tag}] 用完全相同参数再挂一次（适配层将派生同一个 algoClOrdId）...")
    second = api.create_stop_loss_order(ccxt_symbol, side, coin, stop_px)
    if second:
        second_id = second.get('id')
        second_client_id = _algo_client_order_id(second)
        print(f"[{tag}] 第二张止损: algoId={second_id}, algoClOrdId={second_client_id}")
        if not second_id or second_client_id != first_client_id:
            print(f"[{tag}] ⚠️ 第二次止损 ID 不完整或幂等 ID 不一致，本次不构成证据")
            return None
        if str(second_id) == str(first_id):
            print(f"[{tag}] ⚠️ 第二次仍是已撤销 algoId，本次不构成证据")
            return None
        second_state = api.find_stop_order_state(
            ccxt_symbol, side, coin, stop_px, second_id)
        if second_state != 'intact':
            print(f"[{tag}] ❌ 第二张止损内容未获 intact 确认: {second_state!r}")
            return False
        print(f"[{tag}] ✓ OKX 接受终态后复用幂等 ID（新 algoId={second_id}）")
        return True

    time.sleep(2)
    algos = api._fetch_algo_orders(ccxt_symbol)
    listed = [item for item in algos
              if _algo_client_order_id(item) == first_client_id]
    if not listed:
        print(f"[{tag}] 🔴 第二次 POST 未产生任何可确认算法单")
        return False
    if len(listed) != 1:
        print(f"[{tag}] 🔴 同一幂等 ID 出现 {len(listed)} 张算法单，拒绝判成功")
        return False
    second_id = listed[0].get('id')
    if not second_id or str(second_id) == str(first_id):
        print(f"[{tag}] ⚠️ 清单只看到旧单或无有效新 algoId，本次不构成证据")
        return None
    second_state = api.find_stop_order_state(
        ccxt_symbol, side, coin, stop_px, second_id)
    if second_state != 'intact':
        print(f"[{tag}] 🔴 同幂等 ID 的待触发单内容不匹配: {second_state!r}")
        return False
    print(f"[{tag}] ⚠️ POST 返回不确定，但清单严格确认新止损完整（algoId={second_id}）")
    return True


def run_stop_id_reuse_test(api, ccxt_symbol, coin, side):
    """验证终态后复用止损幂等 ID；主体与最终清理同时成功才通过。"""
    print(f"\n{'#' * 60}\n止损幂等 ID 终态复用验证: {side.upper()}（{'做多' if side == 'long' else '做空'}）\n{'#' * 60}")
    tag = f'reuse-{side}'
    if not require_flat_before_live_test(api, ccxt_symbol, tag):
        return False
    expected_contracts = api._coin_to_contracts(ccxt_symbol, coin)
    result = None
    try:
        try:
            order = api.open_position(ccxt_symbol, side, coin)
            print(f"[{tag}] 开仓返回:", (order or {}).get('id'))
            if not order:
                print(f"[{tag}] ❌ 开仓失败或结果无法确认")
            else:
                result = _run_stop_id_reuse_exercise(
                    api, ccxt_symbol, coin, side, order,
                    expected_contracts)
        except Exception as exc:
            print(f"[{tag}] ❌ 开仓/验证异常，转入强制清理: {exc}")
    finally:
        print(f"[{tag}] --- 清理（保证执行）---")
        cleanup_ok = cleanup_live_position(
            api, ccxt_symbol, side, coin, tag)
    if not cleanup_ok:
        print(f"[{tag}] ❌ 清理未确认完成，验证强制失败")
        return False
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('symbol', nargs='?', default='BTCUSDT', help='内部符号，如 BTCUSDT')
    ap.add_argument('coin', nargs='?', type=float, default=0.0, help='开仓币数（务必很小）；不填=只读检查')
    ap.add_argument('--side', choices=['long', 'short', 'both'], default='both', help='验证方向（默认两条都测）')
    ap.add_argument('--fire', action='store_true',
                    help='实弹触发验证：止损挂近市价等真实行情触发（需 --side long 或 short，不支持 both）')
    ap.add_argument('--fire-distance', type=float, default=0.15, help='触发验证止损距市价百分比（默认 0.15）')
    ap.add_argument('--fire-timeout', type=int, default=300, help='触发验证最长等待秒数（默认 300）')
    ap.add_argument('--stop-id-reuse', action='store_true',
                    help='止损幂等 ID 终态复用验证：撤销后按相同参数再挂，同一 algoClOrdId 是否被接受'
                         '（需 --side long 或 short，不支持 both）')
    args = ap.parse_args()

    if args.fire and args.stop_id_reuse:
        print('❌ --fire 与 --stop-id-reuse 是两个独立试验，一次只能跑一个')
        return 2
    if args.fire and args.side == 'both':
        print('❌ --fire 模式需要显式指定单一方向：--side long 或 --side short')
        return 2
    if args.stop_id_reuse and args.side == 'both':
        print('❌ --stop-id-reuse 模式需要显式指定单一方向：--side long 或 --side short')
        return 2
    write_mode = args.fire or args.stop_id_reuse
    if (not math.isfinite(args.coin) or args.coin < 0 or
            (write_mode and args.coin <= 0)):
        print('❌ 写验证模式必须显式提供有限正数开仓币数；只读模式仅允许省略或使用 0')
        return 2

    try:
        cfg = load_cfg()
    except (OSError, TypeError, ValueError) as exc:
        print(f'❌ 验证配置非法: {exc}')
        return 2
    if not (cfg['apiKey'] and cfg['secret'] and cfg['password']):
        print('请先设置 OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE（或写进 config.json）')
        return 2
    # 这是验证工具，不是生产下单入口。实盘凭据下任何 coin>0
    # 都会进入开/平/止损写路径，不得依赖可误触的交互式 yes。
    if not cfg['sandbox'] and args.coin > 0:
        print('❌ 实盘凭据只允许 coin=0 的只读检查；所有写验证必须使用明确的模拟盘账户')
        return 2

    print(f"== OKX 验证 {'[模拟盘 DEMO]' if cfg['sandbox'] else '[!! 实盘 LIVE !!]'} "
          f"symbol={args.symbol} coin={args.coin} side={args.side} "
          f"margin={cfg['margin_mode']} lev={cfg['leverage']} ==")

    api = OkxApi(cfg)
    ccxt_symbol = api.to_ccxt_symbol(args.symbol)
    cs = api._get_contract_size(ccxt_symbol)
    print(f"ccxt 符号: {ccxt_symbol} | 合约面值 contractSize: {cs}")

    mode_ok = check_position_mode(api)
    if mode_ok is not True:
        print("❌ 无法确认账户处于 net_mode，验证脚本拒绝继续。请先修正/确认持仓模式。")
        return 1

    bal = api.get_balance()
    total = (bal or {}).get('total')
    usdt_total = total.get('USDT') if isinstance(total, dict) else None
    try:
        usdt_total = float(usdt_total)
    except (TypeError, ValueError):
        usdt_total = None
    if (usdt_total is None or not math.isfinite(usdt_total) or
            usdt_total < 0):
        print('❌ 无法确认有限非负的 USDT 余额，验证门禁拒绝继续')
        return 1
    print("USDT 总额:", usdt_total)

    if args.coin == 0:
        print("\n未指定开仓币数，仅做只读检查，结束。")
        return 0

    if args.fire:
        result = run_fire_test(api, ccxt_symbol, args.coin, args.side,
                               distance_pct=args.fire_distance, timeout_seconds=args.fire_timeout)
        print(f"\n{'=' * 60}")
        if result is True:
            print("✓ 实弹触发验证通过：触发后持仓归零、未反向、算法单已从待触发列表消失")
        elif result is False:
            print("⚠️ 实弹触发验证发现问题，回看上面日志，请立即人工核查交易所仓位")
        else:
            print("⏱️ 本次未获得触发证据（超时或前置步骤失败），门禁未通过，可调参数重试")
        return 0 if result is True else 1

    if args.stop_id_reuse:
        result = run_stop_id_reuse_test(api, ccxt_symbol, args.coin, args.side)
        print(f"\n{'=' * 60}")
        if result is True:
            print("✓ 复用验证通过：OKX 接受终态后复用同一 algoClOrdId——确定性止损 ID 的幂等假设成立")
        elif result is False:
            print("🔴 复用验证未通过：OKX 拒绝终态后复用同一 algoClOrdId。系统在该场景会大声失败"
                  "（回滚/告警+隔离，不丢钱），但请知悉：撤销后按相同参数补挂会需要人工介入")
        else:
            print("⏱️ 本次未获得复用证据（前置步骤未走通），门禁未通过，可重试")
        return 0 if result is True else 1

    sides = ['long', 'short'] if args.side == 'both' else [args.side]
    results = {}
    for s in sides:
        results[s] = run_side(api, ccxt_symbol, args.coin, s)
        time.sleep(2)

    print(f"\n{'=' * 60}\n汇总：")
    for s, r in results.items():
        print(f"  {s}: {'✓ 通过' if r else '⚠️ 有问题，回看上面日志'}")
    print("「触发后是否只减仓不反向」可用 --fire 模式实测（见文件顶部说明）。")
    return 0 if all(result is True for result in results.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
