"""OKX 止损 / 合约张数 / 撤单 验证脚本 —— 上欧易实盘前的最后一道红线。

代码层无法自证、必须真连交易所验证的部分（多轮审查都强调）。
**强烈建议先用 OKX 模拟盘（demo trading）跑**，确认无误再小额实盘。

用法：
    export OKX_API_KEY=...  OKX_API_SECRET=...  OKX_API_PASSPHRASE=...
    export OKX_DEMO=1                      # 用 OKX 模拟盘（推荐！实盘则不设）
    python verify_okx.py BTCUSDT 0.01              # 默认 long+short 两条路径都测
    python verify_okx.py BTCUSDT 0.01 --side short # 只测做空
    python verify_okx.py BTCUSDT                   # 不带币数 = 只读检查（余额/张数/单向模式）

它用项目里的 OkxApi **真实代码路径**，对每个方向跑：
    换张数 → 开仓 → 挂止损 → 查算法单 → 撤止损 → 平仓（finally 保证平仓清理）
逐项人工核对：
    1) 张数换算是否符合预期（合约面值 contractSize）
    2) 止损是否 reduce-only 条件单（做多=sell、做空=buy）、触发后是否市价平仓
    3) cancel_order / 算法单查询能否把止损撤干净
    4) 平仓后是否干净
    5) 账户是否单向(净)持仓模式（本系统的硬前提）
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

            # 三态判定全链路：原生查询 + 方向/触发价/张数严格匹配（止损自愈巡检同一路径）
            try:
                state = api.find_stop_order_state(
                    ccxt_symbol, side, coin, stop_px, (stop or {}).get('id'))
                print(f"[{side}] find_stop_order_state = {state}（应为 intact）")
                if state != 'intact':
                    print(f"[{side}] ⚠️ 三态判定未返回 intact！严格匹配（触发价 tick 取整/张数）需人工核对。")
                    ok = False
            except Exception as e:
                print(f"[{side}] ⚠️ 三态判定异常: {e}")
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('symbol', nargs='?', default='BTCUSDT', help='内部符号，如 BTCUSDT')
    ap.add_argument('coin', nargs='?', type=float, default=0.0, help='开仓币数（务必很小）；不填=只读检查')
    ap.add_argument('--side', choices=['long', 'short', 'both'], default='both', help='验证方向（默认两条都测）')
    args = ap.parse_args()

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

    check_position_mode(api)

    bal = api.get_balance()
    print("USDT 总额:", (bal or {}).get('total', {}).get('USDT'))

    if args.coin <= 0:
        print("\n未指定开仓币数，仅做只读检查，结束。")
        return

    sides = ['long', 'short'] if args.side == 'both' else [args.side]
    results = {}
    for s in sides:
        results[s] = run_side(api, ccxt_symbol, args.coin, s)
        time.sleep(2)

    print(f"\n{'=' * 60}\n汇总：")
    for s, r in results.items():
        print(f"  {s}: {'✓ 通过' if r else '⚠️ 有问题，回看上面日志'}")
    print("「触发后是否只减仓不反向」仍需手动验证：把止损价设在离市价很近处让它真触发，再看持仓是否归零、没被反向开新仓。")


if __name__ == '__main__':
    main()
