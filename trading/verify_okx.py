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
import time
import argparse

from okx_api import OkxApi


def load_cfg():
    cfg = {
        'apiKey': os.environ.get('OKX_API_KEY'),
        'secret': os.environ.get('OKX_API_SECRET'),
        'password': os.environ.get('OKX_API_PASSPHRASE') or os.environ.get('OKX_PASSWORD'),
        'margin_mode': os.environ.get('OKX_MARGIN_MODE', 'cross'),
        'leverage': float(os.environ.get('OKX_LEVERAGE', '3')),
        'sandbox': os.environ.get('OKX_DEMO') == '1',
    }
    # config.json 补齐缺失项
    cfgfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
    if os.path.exists(cfgfile):
        try:
            import json
            with open(cfgfile, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            okx = (raw.get('exchanges', {}) or {}).get('okx', {}) or raw.get('okx', {}) or {}
            for k in ('apiKey', 'secret', 'password', 'margin_mode', 'leverage', 'leverage_overrides'):
                if not cfg.get(k) and okx.get(k) is not None:
                    cfg[k] = okx[k]
            # sandbox 跟随 config.json（环境变量 OKX_DEMO 优先）——防止 config 是模拟盘而脚本误连实盘
            if os.environ.get('OKX_DEMO') is None and okx.get('sandbox') is not None:
                cfg['sandbox'] = bool(okx['sandbox'])
        except Exception as e:
            print('读取 config.json 失败:', e)
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


def run_side(api, ccxt_symbol, coin, side):
    """对单个方向跑一轮，finally 保证平仓清理。返回该方向是否全部检查通过。"""
    print(f"\n{'#' * 60}\n方向: {side.upper()}（{'做多' if side == 'long' else '做空'}）\n{'#' * 60}")
    contracts = api._coin_to_contracts(ccxt_symbol, coin)
    print(f"换算：{coin} 币 → {contracts} 张")

    order = api.open_position(ccxt_symbol, side, coin)
    print(f"[{side}] 开仓返回:", (order or {}).get('id'))
    if not order:
        print(f"[{side}] ❌ 开仓失败")
        return False

    ok = True
    try:
        time.sleep(2)
        pos = api.get_position(ccxt_symbol)
        print(f"[{side}] 持仓:", {k: (pos or {}).get(k) for k in ('contracts', 'side', 'entryPrice')} if pos else None)
        entry = float((pos or {}).get('entryPrice') or 0)

        if entry <= 0:
            print(f"[{side}] 未取到入场价，跳过止损测试，直接进入清理。")
        else:
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
            for o in algos:
                info = o.get('info') or {}
                print("    -", {'id': o.get('id'), 'side': o.get('side'),
                                'reduceOnly': o.get('reduceOnly'),
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
            except Exception as e:
                print(f"[{side}] ⚠️ 四态裁决异常: {e}")
                ok = False

            print(f"[{side}] 撤止损 ...")
            if stop and stop.get('id'):
                print(f"[{side}] cancel_order:", api.cancel_order(ccxt_symbol, stop['id']))
            else:
                print(f"[{side}] cancel_all_orders:", api.cancel_all_orders(ccxt_symbol))
            time.sleep(2)
            left = api._fetch_algo_orders(ccxt_symbol)
            print(f"[{side}] 撤后算法单（应 0）: {len(left)}")
            if left:
                print(f"[{side}] ⚠️ 仍有残留算法单！")
                ok = False
    finally:
        print(f"[{side}] --- 平仓 / 清理（保证执行）---")
        try:
            api.cancel_all_orders(ccxt_symbol)
            print(f"[{side}] 平仓返回:", (api.close_position(ccxt_symbol, side, coin) or {}).get('id'))
            time.sleep(2)
            after = api.get_position(ccxt_symbol)
            rem = abs(float((after or {}).get('contracts') or 0))
            print(f"[{side}] 平仓后剩余持仓（应 0）: {rem}")
            if rem:
                print(f"[{side}] ⚠️⚠️ 仍有残留持仓，请立即手动到欧易平掉！")
                ok = False
        except Exception as e:
            print(f"[{side}] ❌ 清理失败，请立即手动到欧易检查并平仓！", e)
            ok = False
    return ok


def run_fire_test(api, ccxt_symbol, coin, side, distance_pct=0.15, timeout_seconds=300, poll_interval=5):
    """实弹触发验证：止损挂在离市价很近处，等待真实行情自然触发。

    本系统止损防线唯一从未被实测过的一环——reduce-only 标志本身已在非触发
    场景（run_side）实证被交易所接受存储，但「触发瞬间是否真的只减仓、
    绝不反向开出反向仓」此前完全依赖 OKX 平台承诺，代码从未亲眼验证过。

    返回 True=触发且验证通过 / False=触发但发现问题 / None=超时未触发
    （价格在窗口内未走到止损位，不构成证据，非失败，可调参数重试）。
    finally 保证清理（撤单+平仓），不留残留仓位。
    """
    print(f"\n{'#' * 60}\n实弹触发验证: {side.upper()}（{'做多' if side == 'long' else '做空'}）\n{'#' * 60}")
    print(f"止损距市价 {distance_pct}%，最长等待 {timeout_seconds}s（每 {poll_interval}s 轮询一次）")

    order = api.open_position(ccxt_symbol, side, coin)
    print(f"[fire-{side}] 开仓返回:", (order or {}).get('id'))
    if not order:
        print(f"[fire-{side}] ❌ 开仓失败")
        return None

    stop_id = None
    try:
        time.sleep(2)
        last_price = api.get_last_price(ccxt_symbol)
        stop_px = last_price * (1 - distance_pct / 100) if side == 'long' else last_price * (1 + distance_pct / 100)
        print(f"[fire-{side}] 市价={last_price}, 挂止损 @ {stop_px:.6f}")

        stop = api.create_stop_loss_order(ccxt_symbol, side, coin, stop_px)
        print(f"[fire-{side}] 止损返回:", (stop or {}).get('id'))
        if not stop:
            print(f"[fire-{side}] ❌ 止损创建失败，无法继续验证")
            return None
        stop_id = stop.get('id')

        print(f"[fire-{side}] 等待自然行情触发...")
        start = time.time()
        triggered = False
        reverse_observed = False
        partial_fill_observed = False
        initial_contracts = None
        while time.time() - start < timeout_seconds:
            pos = api.get_position(ccxt_symbol)
            contracts = abs(float((pos or {}).get('contracts') or 0))
            observed_side = (pos or {}).get('side')
            if pos is None or contracts == 0:
                triggered = True
                break
            # 不要求必须先采样到“空仓”。如果错误的非 reduce-only 止损让仓位
            # 在两个轮询点之间由 long 直接翻成 short（或反之），旧逻辑只会一路
            # 等到超时并给出“不确定”，恰好漏掉最危险的证据。
            if observed_side and observed_side != side:
                reverse_observed = True
                triggered = True
                print(f"[fire-{side}] 🔴 轮询直接观察到反向仓: {contracts} 张 {observed_side}")
                break
            if initial_contracts is None:
                initial_contracts = contracts
            elif contracts < initial_contracts:
                partial_fill_observed = True
            print(f"[fire-{side}] 未触发（已等待 {int(time.time() - start)}s，持仓仍在）...")
            time.sleep(poll_interval)

        if not triggered:
            if partial_fill_observed:
                print(f"[fire-{side}] ❌ 观察到止损部分成交但未在超时前归零，按失败处理。")
                return False
            print(f"[fire-{side}] ⏱️ 超时未触发（{timeout_seconds}s 内价格未走到止损位），"
                  f"本次不构成证据，非失败。清理后可加大 --fire-distance 或 --fire-timeout 重试。")
            return None

        if reverse_observed:
            print(f"[fire-{side}] ❌ 检测到止损执行，但仓位直接反向")
        else:
            print(f"[fire-{side}] ✓ 检测到持仓已归零，止损已触发")
        time.sleep(2)  # 留出成交回报与列表更新的短暂窗口
        pos_after = api.get_position(ccxt_symbol)
        contracts_after = abs(float((pos_after or {}).get('contracts') or 0))
        side_after = (pos_after or {}).get('side')

        ok = not reverse_observed
        if pos_after is not None and contracts_after > 0:
            print(f"[fire-{side}] ⚠️⚠️ 触发后仍有持仓 {contracts_after} 张，方向={side_after}！")
            if side_after and side_after != side:
                print(f"[fire-{side}] 🔴🔴 严重：检测到反向持仓！reduce-only 未生效，请立即人工核查交易所！")
            ok = False
        else:
            print(f"[fire-{side}] ✓ 触发后持仓精确归零，未反向")

        algos = api._fetch_algo_orders(ccxt_symbol)
        still_listed = any(str(o.get('id')) == str(stop_id) for o in algos)
        if still_listed:
            time.sleep(2)  # 列表可能滞后于触发生效，复查一次再裁决（与验证式撤单同一容忍）
            algos = api._fetch_algo_orders(ccxt_symbol)
            still_listed = any(str(o.get('id')) == str(stop_id) for o in algos)
        print(f"[fire-{side}] 触发后该止损单是否仍在待触发列表（应否）: {still_listed}")
        if still_listed:
            print(f"[fire-{side}] ⚠️ 触发单未从待触发列表消失，需人工核对状态语义")
            ok = False

        return ok
    finally:
        print(f"[fire-{side}] --- 清理（保证执行）---")
        try:
            api.cancel_all_orders(ccxt_symbol)
            close_order = api.close_position(ccxt_symbol, side, coin)
            if close_order:
                print(f"[fire-{side}] 清理平仓返回:", close_order.get('id'))
            time.sleep(2)
            after = api.get_position(ccxt_symbol)
            rem = abs(float((after or {}).get('contracts') or 0))
            print(f"[fire-{side}] 清理后剩余持仓（应 0）: {rem}")
            if rem:
                print(f"[fire-{side}] ⚠️⚠️ 仍有残留持仓，请立即手动到欧易平掉！")
        except Exception as e:
            print(f"[fire-{side}] ❌ 清理失败，请立即手动到欧易检查并平仓！", e)


def run_stop_id_reuse_test(api, ccxt_symbol, coin, side):
    """止损幂等 ID 终态复用验证：撤销后按完全相同参数再挂，同一 algoClOrdId 是否被接受。

    流程（全部走项目真实代码路径，不绕过适配层）：
      1. 开小仓，按远离市价的固定止损价挂第一张止损（适配层由四元组派生 algoClOrdId）；
      2. 验证式撤销该止损（两类清单验净 = 已进入终态）；
      3. 用**完全相同**的 (方向, 币数, 触发价) 再调一次 create_stop_loss_order——
         适配层必然派生出同一个 algoClOrdId，且预查在待触发清单中找不到旧单，会真实 POST；
      4. 裁决：
         - 第二次返回新算法单（同 algoClOrdId、不同 algoId）→ OKX 允许终态后复用 ✓
         - 第二次返回 None 且该 ID 不在待触发清单 → 极可能被交易所以重复 ID 拒绝，
           回看 stderr 中「止损单业务异常 / 止损 ACK 异常」日志里的 sCode 确认 🔴
         - 第二次返回 None 但该 ID 已出现在待触发清单 → 复用实际成功、仅确认查询滞后 ✓(带警告)
    返回 True=复用被接受 / False=复用被拒绝 / None=前置步骤未走通（不构成证据）。
    finally 保证清理（撤单+平仓），不留残留仓位。
    """
    print(f"\n{'#' * 60}\n止损幂等 ID 终态复用验证: {side.upper()}（{'做多' if side == 'long' else '做空'}）\n{'#' * 60}")

    order = api.open_position(ccxt_symbol, side, coin)
    print(f"[reuse-{side}] 开仓返回:", (order or {}).get('id'))
    if not order:
        print(f"[reuse-{side}] ❌ 开仓失败，无法继续")
        return None

    try:
        time.sleep(2)
        last_price = api.get_last_price(ccxt_symbol)
        # 与 run_side 同取向：远离市价（±10%），试验期间不期望被行情触发
        stop_px = round(last_price * (0.9 if side == 'long' else 1.1), 6)
        print(f"[reuse-{side}] 市价={last_price}, 止损价={stop_px}（两次挂单使用同一价）")

        first = api.create_stop_loss_order(ccxt_symbol, side, coin, stop_px)
        first_id = (first or {}).get('id')
        first_client_id = (first or {}).get('clientOrderId')
        print(f"[reuse-{side}] 第一张止损: algoId={first_id}, algoClOrdId={first_client_id}")
        if not first or not first_id or not first_client_id:
            print(f"[reuse-{side}] ❌ 第一张止损创建失败或缺少可追踪 ID，无法继续")
            return None

        print(f"[reuse-{side}] 验证式撤销第一张止损（撤销确认 = 终态证明）...")
        cancelled = api.cancel_order(ccxt_symbol, first_id)
        print(f"[reuse-{side}] 撤销确认: {cancelled}")
        if not cancelled:
            print(f"[reuse-{side}] ❌ 撤销未能确认，无法证明旧单已终态，本次不构成证据")
            return None
        time.sleep(2)

        print(f"[reuse-{side}] 用完全相同参数再挂一次（适配层将派生同一个 algoClOrdId）...")
        second = api.create_stop_loss_order(ccxt_symbol, side, coin, stop_px)

        if second:
            second_id = second.get('id')
            second_client_id = second.get('clientOrderId')
            print(f"[reuse-{side}] 第二张止损: algoId={second_id}, algoClOrdId={second_client_id}")
            if second_client_id != first_client_id:
                print(f"[reuse-{side}] ⚠️ 两次派生的 algoClOrdId 不一致"
                      f"（{first_client_id} vs {second_client_id}），前提被破坏，本次不构成证据")
                return None
            if str(second_id) == str(first_id):
                print(f"[reuse-{side}] ⚠️ 第二次返回了与已撤销单相同的 algoId——撤销可能未真正生效"
                      f"（清单滞后），未产生新 POST，本次不构成证据")
                return None
            print(f"[reuse-{side}] ✓ OKX 接受了终态后复用的 algoClOrdId（新 algoId={second_id}）")
            return True

        # 第二次返回 None：区分「被拒绝」与「已创建但确认滞后」
        time.sleep(2)
        algos = api._fetch_algo_orders(ccxt_symbol)
        listed = [o for o in algos if o.get('clientOrderId') == first_client_id]
        if listed:
            print(f"[reuse-{side}] ⚠️ 第二次调用返回 None，但该 algoClOrdId 已出现在待触发清单"
                  f"（algoId={listed[0].get('id')}）——复用实际成功，仅确认查询滞后")
            return True
        print(f"[reuse-{side}] 🔴 第二次 POST 未产生任何算法单：同参数在数秒前刚成功过、账户状态未变，"
              f"极可能是交易所拒绝了终态后复用的 algoClOrdId。")
        print(f"[reuse-{side}]    请回看上方 stderr 日志「止损单业务异常 / OKX 止损 ACK 异常」中的 sCode 确认。")
        print(f"[reuse-{side}]    运营含义：撤销后按完全相同参数补挂止损会失败——系统会大声失败"
              f"（开仓路径自动回滚 / 巡检路径告警+隔离），不丢钱，但需人工介入且损失入场机会。")
        return False
    finally:
        print(f"[reuse-{side}] --- 清理（保证执行）---")
        try:
            api.cancel_all_orders(ccxt_symbol)
            close_order = api.close_position(ccxt_symbol, side, coin)
            if close_order:
                print(f"[reuse-{side}] 清理平仓返回:", close_order.get('id'))
            time.sleep(2)
            after = api.get_position(ccxt_symbol)
            rem = abs(float((after or {}).get('contracts') or 0))
            print(f"[reuse-{side}] 清理后剩余持仓（应 0）: {rem}")
            if rem:
                print(f"[reuse-{side}] ⚠️⚠️ 仍有残留持仓，请立即手动到欧易平掉！")
        except Exception as e:
            print(f"[reuse-{side}] ❌ 清理失败，请立即手动到欧易检查并平仓！", e)


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
        return
    if args.fire and args.side == 'both':
        print('❌ --fire 模式需要显式指定单一方向：--side long 或 --side short')
        return
    if args.stop_id_reuse and args.side == 'both':
        print('❌ --stop-id-reuse 模式需要显式指定单一方向：--side long 或 --side short')
        return

    cfg = load_cfg()
    if not (cfg['apiKey'] and cfg['secret'] and cfg['password']):
        print('请先设置 OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE（或写进 config.json）')
        return

    print(f"== OKX 验证 {'[模拟盘 DEMO]' if cfg['sandbox'] else '[!! 实盘 LIVE !!]'} "
          f"symbol={args.symbol} coin={args.coin} side={args.side} "
          f"margin={cfg['margin_mode']} lev={cfg['leverage']} ==")
    if not cfg['sandbox'] and input("当前是实盘，确认继续？输入 yes：").strip() != 'yes':
        return

    api = OkxApi(cfg)
    ccxt_symbol = api.to_ccxt_symbol(args.symbol)
    cs = api._get_contract_size(ccxt_symbol)
    print(f"ccxt 符号: {ccxt_symbol} | 合约面值 contractSize: {cs}")

    mode_ok = check_position_mode(api)
    if mode_ok is not True:
        print("❌ 无法确认账户处于 net_mode，验证脚本拒绝继续。请先修正/确认持仓模式。")
        return

    bal = api.get_balance()
    print("USDT 总额:", (bal or {}).get('total', {}).get('USDT'))

    if args.coin <= 0:
        print("\n未指定开仓币数，仅做只读检查，结束。")
        return

    if args.fire:
        result = run_fire_test(api, ccxt_symbol, args.coin, args.side,
                               distance_pct=args.fire_distance, timeout_seconds=args.fire_timeout)
        print(f"\n{'=' * 60}")
        if result is True:
            print("✓ 实弹触发验证通过：触发后持仓归零、未反向、算法单已从待触发列表消失")
        elif result is False:
            print("⚠️ 实弹触发验证发现问题，回看上面日志，请立即人工核查交易所仓位")
        else:
            print("⏱️ 本次未获得触发证据（超时或前置步骤失败），非失败，可调参数重试")
        return

    if args.stop_id_reuse:
        result = run_stop_id_reuse_test(api, ccxt_symbol, args.coin, args.side)
        print(f"\n{'=' * 60}")
        if result is True:
            print("✓ 复用验证通过：OKX 接受终态后复用同一 algoClOrdId——确定性止损 ID 的幂等假设成立")
        elif result is False:
            print("🔴 复用验证未通过：OKX 拒绝终态后复用同一 algoClOrdId。系统在该场景会大声失败"
                  "（回滚/告警+隔离，不丢钱），但请知悉：撤销后按相同参数补挂会需要人工介入")
        else:
            print("⏱️ 本次未获得复用证据（前置步骤未走通），非失败，可重试")
        return

    sides = ['long', 'short'] if args.side == 'both' else [args.side]
    results = {}
    for s in sides:
        results[s] = run_side(api, ccxt_symbol, args.coin, s)
        time.sleep(2)

    print(f"\n{'=' * 60}\n汇总：")
    for s, r in results.items():
        print(f"  {s}: {'✓ 通过' if r else '⚠️ 有问题，回看上面日志'}")
    print("「触发后是否只减仓不反向」可用 --fire 模式实测（见文件顶部说明）。")


if __name__ == '__main__':
    main()
