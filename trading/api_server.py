from flask import Flask, jsonify, request, send_from_directory, session
from functools import wraps
from werkzeug.middleware.proxy_fix import ProxyFix
import json
import logging
import threading
import time
import os
import secrets
from datetime import datetime
from trade_state import enrich_closed_trade_with_fees

# 品种写接口的输入校验（脏数据会进 config、前端渲染和真实下单路径，必须挡在门口）。
# 校验口径全部取自 config_validation——与手写 config.json 的启动校验同一事实源，
# 三入口（前端/API/文件）由构造保证一致，不再靠人工同步两份常量。
from config_validation import (PERIOD_MAX, PERIOD_MIN, STRATEGY_WHITELIST,
                               ohlcv_fetch_limit_for_strategy,
                               required_closed_candles_for_strategy, strict_int,
                               strict_risk_per_trade, strict_bool,
                               normalize_symbol_name, strict_float_finite)


def _validate_symbol_input(name, risk_per_trade=None, strategy=None, enabled=None):
    """校验并**规范化**品种写入字段（与启动校验 main._validate_symbol_configs 同源）。

    返回 (clean, error)：error 非 None 时 clean 为 None；否则 clean 仅含传入（非 None）
    字段的规范化值——name 恒有；risk_per_trade→float、enabled→bool、strategy 原样（已白名单）。
    路由必须用 clean 的值写入 config，杜绝 "0.01"/"false" 等字符串混入下单/开仓资格路径。
    """
    clean = {}
    try:
        clean['name'] = normalize_symbol_name(name)
        if risk_per_trade is not None:
            clean['risk_per_trade'] = strict_risk_per_trade(risk_per_trade)
        if enabled is not None:
            clean['enabled'] = strict_bool(enabled)
    except ValueError as e:
        return None, str(e)
    if strategy is not None:
        if strategy not in STRATEGY_WHITELIST:
            return None, f'未知策略: {strategy!r}（只支持 turtle / ma_cross）'
        clean['strategy'] = strategy
    return clean, None

app = Flask(__name__, static_folder='static', static_url_path='/static')


def _parse_proxyfix_hops(value):
    """解析 TRADING_PROXYFIX_X_FOR（信任的反代跳数）。非法值拒绝启动——
    登录防爆破按它从 X-Forwarded-For 还原真实客户端 IP，配错即整个功能失效
    （与 FLASK_SECRET_KEY 缺失同标准 fail-loud）。"""
    try:
        hops = int(str(value).strip())
    except (TypeError, ValueError):
        raise RuntimeError(f'TRADING_PROXYFIX_X_FOR 必须是 0-10 的整数（反代跳数）: {value!r}')
    if not (0 <= hops <= 10):
        raise RuntimeError(f'TRADING_PROXYFIX_X_FOR 必须是 0-10 的整数（反代跳数）: {value!r}')
    return hops


# 反代跳数由部署方声明（代码无法安全地自动探测——盲信 X-Forwarded-For 本身就是漏洞）：
# 0 = 无反代直连（完全不信 XFF）；1 = 单反代 / Cloudflare Tunnel（默认，真实客户端 IP
# 在链尾）；2 = CDN→nginx 双层。登录防爆破按还原后的 remote_addr 计数——跳数配错时
# 计数键要么可被伪造（绕过锁定）、要么全体访客共享（互相连坐锁定）。
_PROXYFIX_HOPS = _parse_proxyfix_hops(os.environ.get('TRADING_PROXYFIX_X_FOR', '1'))
if _PROXYFIX_HOPS > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_PROXYFIX_HOPS, x_proto=1)
app.config['JSON_AS_ASCII'] = False   # Flask < 2.3
try:
    app.json.ensure_ascii = False     # Flask >= 2.2（JSON_AS_ASCII 已废弃）
except AttributeError:
    pass
app.secret_key = os.environ.get('FLASK_SECRET_KEY')
if not app.secret_key:
    raise RuntimeError('未配置 FLASK_SECRET_KEY，拒绝启动')

# 会话 Cookie 加固：SameSite 显式 Lax；Secure 由部署方按是否走 HTTPS 决定——
# 设 TRADING_COOKIE_SECURE=1 开启，不无条件写死（内网纯 HTTP 部署会被弄坏）
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('TRADING_COOKIE_SECURE') == '1'

LOGIN_PASSWORD = os.environ.get('TRADING_LOGIN_PASSWORD')

# 全局单交易所系统（由 wsgi / __main__ 注入）
trading_system = None

logger = logging.getLogger(__name__)

# 手动检查防重入标记
_manual_check_running = False
_manual_check_guard = threading.Lock()

# 登录防爆破：内存级按 IP 退避（连续 5 次失败锁 60 秒，成功即清零；进程重启即重置，无需持久化）
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60
_login_failures = {}   # ip -> (连续失败次数, 锁定截止时间戳)
_login_guard = threading.Lock()

API_TOKEN = os.environ.get('TRADING_API_TOKEN')


def require_auth(f):
    """API认证装饰器：支持Session或Token认证。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('authenticated'):
            return f(*args, **kwargs)
        token = request.headers.get('X-API-Token')
        # 编码成 bytes 再比：compare_digest 对 str 仅支持 ASCII，攻击者发一个非 ASCII 的
        # X-API-Token 头会抛 TypeError（装饰器内无捕获）→ 500 而非干净 401，还能借此探测
        # token 是否启用。bytes 无此限制。（与 api_login 密码比较同一口径）
        if API_TOKEN and token and secrets.compare_digest(token.encode('utf-8'), API_TOKEN.encode('utf-8')):
            return f(*args, **kwargs)
        return jsonify({'error': '认证失败，请登录或提供有效的API Token'}), 401
    return decorated


# ============ 钉钉推送 ============

def send_dingtalk(msg):
    """业务操作通知，统一走 DingTalkNotifier 发送：
    - notifier 会校验响应 errcode——钉钉被关键词拦截时返回 HTTP 200 + errcode!=0，
      此前直接 post 只看状态码，被拒收也会记成推送成功；
    - 标题固定带「交易系统」满足机器人关键词安全校验（手动平仓/参数更新/资金同步
      等消息原文本不含关键词，text 直发会被静默拒收）。
    """
    try:
        notifier = getattr(trading_system, 'notifier', None) if trading_system else None
        if not notifier:
            logger.warning(f"钉钉推送跳过: 系统尚未就绪: {msg[:40]}")
            return
        # 钉钉 markdown 中单个换行不生效，转成空行分隔以保持原有多行排版
        notifier.send_message('[交易系统] 操作通知', msg.replace('\n', '\n\n'))
    except Exception as e:
        logger.error(f"钉钉推送失败: {e}")


# ============ 系统获取 ============

def _require_system():
    """返回 (trading_system, error_response)。欧易单交易所版，无需 exchange 参数。"""
    if trading_system is None:
        return None, (jsonify({'error': '系统尚未就绪'}), 503)
    return trading_system, None


def _persist_config():
    """把整份 config 写回磁盘。"""
    if trading_system and hasattr(trading_system, 'persist_config'):
        return trading_system.persist_config()
    return False


def _commit_config_or_rollback(system, section_key, sub_key, backup, fail_message):
    """配置提交：持久化整份 config；写盘失败把 config[section][sub]（sub 为 None 则整段）
    回滚为 backup，并返回 500 错误响应；成功则 reload 策略并返回 None。
    必须在 system._config_lock 内调用（写路由的统一收口，替代五处重复样板）。"""
    if not _persist_config():
        if sub_key is None:
            system.config[section_key] = backup
        else:
            system.config[section_key][sub_key] = backup
        return jsonify({'error': fail_message}), 500
    system.reload_strategies()
    return None


# ============ 全局路由 ============

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/api/login', methods=['POST'])
def api_login():
    if not LOGIN_PASSWORD:
        return jsonify({'success': False, 'message': '服务器未配置登录密码，请使用API Token'}), 503
    ip = request.remote_addr or 'unknown'
    now = time.time()
    with _login_guard:
        _fails, locked_until = _login_failures.get(ip, (0, 0.0))
        if now < locked_until:
            return jsonify({'success': False,
                            'message': f'登录失败次数过多，请 {int(locked_until - now) + 1} 秒后再试'}), 429
    data = request.get_json() or {}
    # 恒定时间比较，消除密码校验的计时侧信道（与 API Token 的 compare_digest 口径一致）。
    # 必须编码成 bytes 再比：compare_digest 对 str 仅支持 ASCII，含中文等非 ASCII 密码
    # 会抛 TypeError 令登录彻底失效；bytes 无此限制。str() 兜底防 password 传非字符串。
    if secrets.compare_digest(str(data.get('password', '')).encode('utf-8'),
                              LOGIN_PASSWORD.encode('utf-8')):
        with _login_guard:
            _login_failures.pop(ip, None)
        session['authenticated'] = True
        return jsonify({'success': True, 'message': '登录成功'})
    with _login_guard:
        fails, _ = _login_failures.get(ip, (0, 0.0))
        fails += 1
        locked_until = now + LOGIN_LOCKOUT_SECONDS if fails >= LOGIN_MAX_FAILURES else 0.0
        _login_failures[ip] = (fails, locked_until)
        if locked_until:
            logger.warning(f"登录连续失败 {fails} 次，已锁定 {ip} {LOGIN_LOCKOUT_SECONDS} 秒")
    return jsonify({'success': False, 'message': '密码错误'}), 401


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'success': True, 'message': '已登出'})


@app.route('/api/check_auth', methods=['GET'])
def check_auth():
    if session.get('authenticated'):
        return jsonify({'authenticated': True})
    return jsonify({'authenticated': False}), 401


# 单所版已移除多所聚合接口（/api/exchanges、/api/overview、/api/overview_ohlc）


@app.route('/api/logs', methods=['GET'])
@require_auth
def get_logs():
    try:
        lines = max(1, min(request.args.get('lines', 50, type=int), 1000))
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trading.log')
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
        filtered = [l for l in all_lines if not (' "' in l and 'HTTP/1.1' in l) and 'code 400, message Bad request' not in l]
        result = filtered[-lines:]
        result.reverse()
        return jsonify({'logs': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============ 业务路由（单所版，无 exchange 参数） ============

@app.route('/api/status', methods=['GET'])
@require_auth
def get_status():
    system, err = _require_system()
    if err:
        return err
    try:
        open_positions = system.trade_state.get_all_open_positions()
        last_symbol_update = None
        try:
            mtime = os.path.getmtime(system.config_file)
            last_symbol_update = datetime.fromtimestamp(mtime).isoformat()
        except Exception as e:
            logger.warning(f"忽略异常: {e}")
        manual_symbols = [s['name'] for s in system.config['trading']['symbols'] if s.get('enabled', True)]
        try:
            stop_residues = list(system.trade_state.get_stop_residues().keys())
        except Exception:
            stop_residues = []
        stop_anomalies = dict(getattr(system, '_stop_anomalies', {}) or {})
        return jsonify({
            'status': 'running',
            'exchange': system.exchange_id,
            'label': system.label,
            'open_positions_count': len(open_positions),
            'open_positions': open_positions,
            'enabled_symbols': manual_symbols,
            'manual_pool_count': len(manual_symbols),
            'stop_residues': stop_residues,
            'stop_anomalies': stop_anomalies,
            'last_symbol_update': last_symbol_update
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/symbols', methods=['GET'])
@require_auth
def get_symbols():
    system, err = _require_system()
    if err:
        return err
    try:
        symbols = json.loads(json.dumps(system.config['trading']['symbols']))
        open_position_symbols = set(system.trade_state.get_all_open_positions().keys())
        for symbol in symbols:
            symbol['has_open_position'] = symbol.get('name') in open_position_symbols
        return jsonify(symbols)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/symbols', methods=['POST'])
@require_auth
def add_symbol():
    system, err = _require_system()
    if err:
        return err
    try:
        data = request.get_json(silent=True)
        if not data or 'name' not in data:
            return jsonify({'error': '缺少必要参数'}), 400

        # 显式 null 与缺省同义：.get 的默认值只覆盖「键不存在」，覆盖不了 "risk_per_trade": null——
        # None 会穿过校验（视为未提供），随后 clean[键] 抛 KeyError 变 500 而非干净 400/默认值
        risk_in = data.get('risk_per_trade')
        strategy_in = data.get('strategy')
        enabled_in = data.get('enabled')
        clean, invalid = _validate_symbol_input(
            data.get('name'),
            0.01 if risk_in is None else risk_in,
            'turtle' if strategy_in is None else strategy_in,
            True if enabled_in is None else enabled_in)
        if invalid:
            return jsonify({'error': invalid}), 400
        new_symbol = {  # 全部用规范化值：name 大写、risk float、enabled 真 bool
            'name': clean['name'], 'enabled': clean['enabled'],
            'risk_per_trade': clean['risk_per_trade'], 'strategy': clean['strategy'],
        }
        for s in system.config['trading']['symbols']:
            if s['name'] == new_symbol['name']:
                return jsonify({'error': '交易对已存在'}), 400

        # 验证交易对在交易所是否真实存在
        try:
            ccxt_symbol = system.exchange_api.to_ccxt_symbol(new_symbol['name'])
            test_ohlcv = system.exchange_api.fetch_ohlcv(ccxt_symbol, '1d', limit=5)
            if not test_ohlcv or len(test_ohlcv) == 0:
                return jsonify({'error': f"交易所不存在永续合约 {new_symbol['name']}，请检查名称是否正确"}), 400
        except Exception:
            return jsonify({'error': f"交易所验证失败: {new_symbol['name']} 不是有效的永续合约交易对"}), 400

        with system._config_lock:
            if any(s['name'] == new_symbol['name'] for s in system.config['trading']['symbols']):
                return jsonify({'error': '交易对已存在'}), 400
            backup_symbols = json.loads(json.dumps(system.config['trading']['symbols']))
            system.config['trading']['symbols'].append(new_symbol)
            err_resp = _commit_config_or_rollback(system, 'trading', 'symbols', backup_symbols, '配置写入失败，交易对未添加')
            if err_resp:
                return err_resp

        # 海龟策略：回溯历史，中轨后尚未突破则激活可开仓状态
        if new_symbol['strategy'] == 'turtle':
            try:
                symbol_name = new_symbol['name']
                ccxt_symbol = system.exchange_api.to_ccxt_symbol(symbol_name)
                fetch_limit = ohlcv_fetch_limit_for_strategy('turtle', system.config.get('strategy', {}))
                required_closed = required_closed_candles_for_strategy('turtle', system.config.get('strategy', {}))
                ohlcv = system.exchange_api.fetch_ohlcv(ccxt_symbol, '1d', limit=fetch_limit)
                if ohlcv:
                    df = system.exchange_api.ohlcv_to_dataframe(ohlcv)
                    df = system.exchange_api.filter_closed_candles(df, timeframe='1d')
                    if len(df) < required_closed:
                        logger.warning(
                            f"[{system.label}] {symbol_name} 历史回溯K线不足：海龟策略配置至少需要 "
                            f"{required_closed} 根已收盘K线，本轮仅取得 {len(df)} 根"
                            f"（请求 {fetch_limit} 根）")
                    else:
                        strategy = system.get_strategy_for_symbol({'strategy': 'turtle'})[0]
                        armed = strategy.is_first_breakout_armed(df)
                        if armed:
                            system.trade_state.set_signal_state(symbol_name, True)
                            logger.info(f"[{system.label}] {symbol_name} 历史回溯判定为可开仓状态")
                        else:
                            logger.info(f"[{system.label}] {symbol_name} 历史回溯判定为未激活状态")
            except Exception as e:
                logger.warning(f"初始化 {new_symbol['name']} 状态失败: {e}")

        strategy_text = '海龟通道' if new_symbol['strategy'] == 'turtle' else '双均线EMA'
        send_dingtalk(f'[{system.label}] 新增交易对: {new_symbol["name"]}, 策略: {strategy_text}, '
                      f'风险度: {new_symbol["risk_per_trade"]*100:.1f}%, 状态: {"启用" if new_symbol["enabled"] else "禁用"}')
        return jsonify({'status': 'success', 'message': f'交易对 {new_symbol["name"]} 已添加', 'symbol': new_symbol})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/symbols/<symbol>', methods=['PUT'])
@require_auth
def update_symbol(symbol):
    system, err = _require_system()
    if err:
        return err
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': '缺少参数'}), 400
        clean, invalid = _validate_symbol_input(
            symbol, data.get('risk_per_trade'), data.get('strategy'), data.get('enabled'))
        if invalid:
            return jsonify({'error': invalid}), 400
        # 以 clean 为准判断「提供了哪些字段」：显式 null 会穿过校验（视为未提供），
        # 按 data 判断会在 clean[键] 处抛 KeyError 变 500；null 与缺省统一按未提供处理
        if not any(k in clean for k in ('risk_per_trade', 'strategy', 'enabled')):
            return jsonify({'error': '缺少可更新字段（risk_per_trade / strategy / enabled）'}), 400
        symbol_u = clean['name']
        # 改策略必须与交易执行互斥（非阻塞取锁，与 instant_open/close_position 同一标准）：
        # 否则「查持仓→写配置」之间日检/即时开仓恰好为该品种建仓（TOCTOU），护栏被穿过——
        # 仓位按旧策略建立、池子已是新策略，次日被错误策略接管。改 enabled/risk 无此风险，不加锁。
        holds_trade_lock = False
        if 'strategy' in clean:
            if not system._trade_lock.acquire(blocking=False):
                return jsonify({'error': '交易检查/巡检正在执行中，请稍后再修改策略'}), 409
            holds_trade_lock = True
        try:
            # 有持仓时禁止改策略：主循环对在池品种按配置策略托管，改了会让现有仓位换策略出场
            if 'strategy' in clean and system.trade_state.get_open_position(symbol_u):
                return jsonify({'error': f'{symbol_u} 当前有持仓，禁止修改策略（会改变现有仓位的止损/出场逻辑）。'
                                         '请等待平仓后再改，或删除该品种让持仓按原策略托管到结束。'}), 400
            with system._config_lock:
                backup_symbols = json.loads(json.dumps(system.config['trading']['symbols']))
                for s in system.config['trading']['symbols']:
                    if s['name'] == symbol_u:
                        if 'enabled' in clean:
                            s['enabled'] = clean['enabled']          # 规范化真 bool（挡 "false"）
                        if 'risk_per_trade' in clean:
                            s['risk_per_trade'] = clean['risk_per_trade']  # 规范化 float
                        if 'strategy' in clean:
                            s['strategy'] = clean['strategy']

                        err_resp = _commit_config_or_rollback(system, 'trading', 'symbols', backup_symbols, '配置写入失败，更新已回滚')
                        if err_resp:
                            return err_resp

                        changes = []
                        if 'enabled' in clean:
                            changes.append(f'状态: {"启用" if clean["enabled"] else "禁用"}')
                        if 'risk_per_trade' in clean:
                            changes.append(f'风险度: {clean["risk_per_trade"]*100:.1f}%')
                        if 'strategy' in clean:
                            strategy_text = '海龟通道' if clean['strategy'] == 'turtle' else '双均线EMA'
                            changes.append(f'策略: {strategy_text}')
                        send_dingtalk(f'[{system.label}] 更新交易对: {symbol_u}, {", ".join(changes)}')
                        return jsonify({'status': 'success', 'message': f'交易对 {symbol_u} 已更新'})
            return jsonify({'error': '交易对不存在'}), 404
        finally:
            if holds_trade_lock:
                system._trade_lock.release()
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/symbols/<symbol>', methods=['DELETE'])
@require_auth
def delete_symbol(symbol):
    system, err = _require_system()
    if err:
        return err
    try:
        symbol_u = symbol.upper()
        with system._config_lock:
            # 品种不在池中：直接 404，与 update_symbol 的语义一致。否则「删不存在的品种」
            # 会走完过滤（空操作）后返回 200 已删除，还误发一条删除钉钉，掩盖前端/调用方的错。
            if not any(s['name'] == symbol_u for s in system.config['trading']['symbols']):
                return jsonify({'error': '交易对不存在'}), 404
            # 删除前兜底：若该品种本地仍有持仓但持仓缺 strategy 字段(老仓)，必须先把策略固化进持仓，
            # 否则删除后 check_and_execute 会按默认 turtle 托管，双均线仓位会被错误管理。
            held = system.trade_state.get_open_position(symbol_u)
            if held and not held.get('strategy'):
                cfg = next((s for s in system.config['trading']['symbols'] if s['name'] == symbol_u), None)
                cfg_strategy = (cfg or {}).get('strategy')
                if cfg_strategy:
                    system.trade_state.set_position_strategy(symbol_u, cfg_strategy)
                else:
                    return jsonify({'error': '该持仓缺少策略信息，无法保证删除后继续按原策略托管，已阻止删除。'
                                             '请先在品种配置中明确该交易对的策略后再删除。'}), 400
            backup_symbols = json.loads(json.dumps(system.config['trading']['symbols']))
            system.config['trading']['symbols'] = [
                s for s in system.config['trading']['symbols'] if s['name'] != symbol_u
            ]
            err_resp = _commit_config_or_rollback(system, 'trading', 'symbols', backup_symbols, '配置写入失败，删除已回滚')
            if err_resp:
                return err_resp
        send_dingtalk(f'[{system.label}] 删除交易对: {symbol}')
        return jsonify({'status': 'success', 'message': f'交易对 {symbol} 已删除'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/positions', methods=['GET'])
@require_auth
def get_positions():
    system, err = _require_system()
    if err:
        return err
    try:
        positions = json.loads(json.dumps(system.trade_state.get_all_open_positions()))
        try:
            for symbol, pos in positions.items():
                try:
                    ccxt_symbol = system.exchange_api.to_ccxt_symbol(symbol)
                    current_price = system.exchange_api.get_last_price(ccxt_symbol)
                    pos['current_price'] = current_price
                    entry = pos['entry_price']
                    size = pos['position_size']
                    if pos['side'] == 'long':
                        pnl = (current_price - entry) * size
                        pnl_pct = (current_price - entry) / entry * 100
                    else:
                        pnl = (entry - current_price) * size
                        pnl_pct = (entry - current_price) / entry * 100
                    pos['unrealized_pnl'] = round(pnl, 2)
                    pos['unrealized_pnl_pct'] = round(pnl_pct, 2)
                except Exception:
                    pos['current_price'] = None
                    pos['unrealized_pnl'] = None
                    pos['unrealized_pnl_pct'] = None
                try:
                    open_time = pos.get('open_time')
                    if open_time:
                        if isinstance(open_time, str):
                            try:
                                open_dt = datetime.fromisoformat(open_time.replace('Z', '+00:00')).replace(tzinfo=None)
                            except Exception:
                                open_dt = datetime.strptime(open_time[:19], '%Y-%m-%dT%H:%M:%S')
                        else:
                            open_dt = datetime.fromtimestamp(open_time / 1000)
                        pos['holding_days'] = (datetime.now() - open_dt).days
                    else:
                        pos['holding_days'] = None
                except Exception:
                    pos['holding_days'] = None
        except Exception as e:
            logger.warning(f"忽略异常: {e}")
        return jsonify(positions)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/trades', methods=['GET'])
@require_auth
def get_trades():
    system, err = _require_system()
    if err:
        return err
    try:
        trades = [enrich_closed_trade_with_fees(t) for t in system.trade_state.get_closed_trades()]
        return jsonify(trades)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/trades_summary', methods=['GET'])
@require_auth
def get_trades_summary():
    system, err = _require_system()
    if err:
        return err
    try:
        trades = [enrich_closed_trade_with_fees(t) for t in system.trade_state.get_closed_trades()]
        if not trades:
            return jsonify({'total': 0})
        total = len(trades)
        wins = [t for t in trades if t.get('pnl', 0) > 0]
        losses = [t for t in trades if t.get('pnl', 0) <= 0]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = win_count / total * 100 if total > 0 else 0
        total_pnl = sum(t.get('pnl', 0) for t in trades)
        avg_win = sum(t.get('pnl', 0) for t in wins) / win_count if win_count > 0 else 0
        avg_loss = sum(t.get('pnl', 0) for t in losses) / loss_count if loss_count > 0 else 0
        loss_sum = sum(t.get('pnl', 0) for t in losses)
        win_sum = sum(t.get('pnl', 0) for t in wins)
        profit_factor = abs(win_sum / loss_sum) if loss_sum != 0 else None
        avg_pnl_pct = sum(t.get('pnl_percent', 0) for t in trades) / total
        max_win = max((t.get('pnl', 0) for t in trades), default=0)
        max_loss = min((t.get('pnl', 0) for t in trades), default=0)
        return jsonify({
            'total': total, 'win_count': win_count, 'loss_count': loss_count,
            'win_rate': round(win_rate, 1), 'total_pnl': round(total_pnl, 2),
            'avg_win': round(avg_win, 2), 'avg_loss': round(avg_loss, 2),
            'profit_factor': round(profit_factor, 2) if profit_factor else None,
            'avg_pnl_pct': round(avg_pnl_pct, 2),
            'max_win': round(max_win, 2), 'max_loss': round(max_loss, 2)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config', methods=['GET'])
@require_auth
def get_config():
    system, err = _require_system()
    if err:
        return err
    try:
        return jsonify({
            'strategy': system.config.get('strategy', {}),
            'trading': system.config.get('trading', {}),
            'scheduler': system.config.get('scheduler', {})
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/account_stats', methods=['GET'])
@require_auth
def get_account_stats():
    system, err = _require_system()
    if err:
        return err
    try:
        return jsonify(system.equity_tracker.build_account_stats(persist=False))
    except Exception as e:
        logger.error(f"获取账户统计异常: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/equity_sync', methods=['POST'])
@require_auth
def equity_sync():
    system, err = _require_system()
    if err:
        return err
    try:
        data = request.get_json(silent=True) or {}
        flow_amount = data.get('flow_amount')
        if flow_amount is not None:
            try:
                # 有限数校验：nan/inf/-inf 会写出 nan/0.0 的求索指数除数，污染资金曲线
                flow_amount = strict_float_finite(flow_amount, '净变动金额')
            except ValueError as e:
                return jsonify({'error': str(e)}), 400
        result = system.equity_tracker.equity_sync(flow_amount=flow_amount)
        flow_line = (f'净变动: {result["flow_amount"]:+.2f} USDT（精确锚定）\n'
                     if result.get('flow_amount') is not None else '锚定方式: 最近指数值\n')
        send_dingtalk(f'[{system.label}] 资金变动同步\n'
                      f'原基准: {result["old_initial"]:.2f} USDT\n'
                      f'新基准: {result["new_initial"]:.2f} USDT\n'
                      f'{flow_line}'
                      f'求索指数锚点: {result["qiusuo_index"]:.2f}\n'
                      f'除数: {result["old_divisor"]:.6f} -> {result["new_divisor"]:.6f}\n'
                      f'时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
        return jsonify({'status': 'success',
                        'message': f'权益基准已同步为 {result["new_initial"]:.2f} USDT',
                        **result})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"权益同步异常: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/equity_history', methods=['GET'])
@require_auth
def get_equity_history():
    system, err = _require_system()
    if err:
        return err
    try:
        snapshots = system.equity_tracker.load_daily_equity()
        eq_hist = system.equity_tracker.load_equity_history()
        return jsonify({'daily_snapshots': snapshots, 'initial_equity': eq_hist.get('initial_equity', 0)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/qiusuo_index_ohlc', methods=['GET'])
@app.route('/api/equity_ohlc', methods=['GET'])
@require_auth
def get_qiusuo_index_ohlc():
    system, err = _require_system()
    if err:
        return err
    try:
        days = int(request.args.get('days', system.equity_tracker.EQUITY_OHLC_DEFAULT_DAYS))
    except (TypeError, ValueError):
        return jsonify({'error': 'days 参数无效'}), 400
    if days > 0:
        # 下限 7；上限 100000 天（≈274 年，对任何真实数据集等价于「全部」）——
        # 不设上限时 days=1000万 会让 datetime.now()-timedelta(days) 抛 OverflowError → 500
        days = max(7, min(days, 100000))
    # days <= 0 表示「全部」，由 EquityTracker 返回完整历史
    try:
        return jsonify(system.equity_tracker.build_qiusuo_index_ohlc(days=days))
    except Exception as e:
        logger.error(f"获取求索指数OHLC失败: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/strategy_params', methods=['GET'])
@require_auth
def get_strategy_params():
    system, err = _require_system()
    if err:
        return err
    try:
        return jsonify(system.config.get('strategy', {}))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/strategy_params', methods=['PUT'])
@require_auth
def update_strategy_params():
    system, err = _require_system()
    if err:
        return err
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': '缺少参数'}), 400
        # 范围硬校验：非法值 400，不写配置、不 reload（周期严格整数；短期必须小于长期；风险度限幅）
        # strict_int 与启动校验同源：28.9 拒绝而非截断，三入口口径完全一致
        try:
            parsed = {}
            for key in ('channel_period', 'ma_short_period', 'ma_long_period', 'ma_stop_period'):
                if key in data:
                    v = strict_int(data[key], key)
                    if not (PERIOD_MIN <= v <= PERIOD_MAX):
                        return jsonify({'error': f'{key} 超出允许范围 [{PERIOD_MIN}, {PERIOD_MAX}]: {v}'}), 400
                    parsed[key] = v
            if 'default_risk_per_trade' in data:
                parsed['default_risk_per_trade'] = strict_risk_per_trade(
                    data['default_risk_per_trade'], '默认风险度'
                )
        except (TypeError, ValueError) as e:
            return jsonify({'error': f'参数不是有效数字: {e}'}), 400
        cur = system.config.get('strategy', {})
        eff_short = parsed.get('ma_short_period', cur.get('ma_short_period', 7))
        eff_long = parsed.get('ma_long_period', cur.get('ma_long_period', 28))
        if eff_short >= eff_long:
            return jsonify({'error': f'EMA 短期({eff_short})必须小于长期({eff_long})'}), 400

        changed = []
        with system._config_lock:
            backup = json.loads(json.dumps(system.config.get('strategy', {})))
            sp = system.config.setdefault('strategy', {})

            label_map = {'channel_period': '海龟通道周期', 'ma_short_period': 'EMA短期周期',
                         'ma_long_period': 'EMA长期周期', 'ma_stop_period': 'EMA止损周期'}
            for key, v in parsed.items():
                sp[key] = v
                if key == 'default_risk_per_trade':
                    changed.append(f"默认风险度: {v*100:.1f}%")
                else:
                    changed.append(f"{label_map[key]}: {v}")

            err_resp = _commit_config_or_rollback(system, 'strategy', None, backup, '配置写入失败，策略参数更新已回滚')
            if err_resp:
                return err_resp
            send_dingtalk(f'[{system.label}] 策略参数更新: {", ".join(changed)}')
            return jsonify({'status': 'success', 'message': '策略参数已更新', 'params': system.config['strategy']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/instant_open', methods=['POST'])
@require_auth
def instant_open():
    """即时开仓 - 根据交易对策略类型使用对应信号检测。"""
    system, err = _require_system()
    if err:
        return err
    try:
        # 与 08:00 日检 / 盘中巡检互斥，防止并发下单（拿不到锁直接拒绝，不排队）
        if not system._trade_lock.acquire(blocking=False):
            return jsonify({'error': '交易检查/巡检正在执行中，请稍后再试'}), 409
        try:
            data = request.get_json(silent=True)
            if not data or 'name' not in data:
                return jsonify({'error': '缺少交易对名称'}), 400

            # 显式 null 与缺省同义（同 add_symbol：防 None 穿过校验后 clean[键] KeyError → 500）
            risk_in = data.get('risk_per_trade')
            strategy_in = data.get('strategy')
            clean, invalid = _validate_symbol_input(
                data.get('name'),
                0.01 if risk_in is None else risk_in,
                'turtle' if strategy_in is None else strategy_in)
            if invalid:
                return jsonify({'error': invalid}), 400
            symbol_name = clean['name']
            risk_per_trade = clean['risk_per_trade']  # 规范化 float，杜绝字符串进仓位计算
            strategy_type = clean['strategy']

            if system.trade_state.get_open_position(symbol_name):
                return jsonify({'error': f'{symbol_name} 已有持仓，无法重复开仓'}), 400

            # 品种池已有该交易对且策略不同：拒绝。日检对在池品种一律按池内策略托管，
            # 按请求策略开出的仓会立刻被另一套止损/出场逻辑接管（与「有持仓禁改策略」同一护栏）
            pool_cfg = next((s for s in system.config['trading']['symbols'] if s['name'] == symbol_name), None)
            if pool_cfg and pool_cfg.get('strategy', 'turtle') != strategy_type:
                pool_strategy = pool_cfg.get('strategy', 'turtle')
                return jsonify({'error': f'{symbol_name} 已在品种池中且策略为 {pool_strategy}，'
                                         f'与本次请求的 {strategy_type} 不一致——开仓后将被日检按池内策略托管。'
                                         '请改按池内策略开仓，或先在品种池中调整该交易对的策略'}), 400

            ccxt_symbol = system.exchange_api.to_ccxt_symbol(symbol_name)
            fetch_limit = ohlcv_fetch_limit_for_strategy(strategy_type, system.config.get('strategy', {}))
            required_closed = required_closed_candles_for_strategy(strategy_type, system.config.get('strategy', {}))
            ohlcv = system.exchange_api.fetch_ohlcv(ccxt_symbol, '1d', limit=fetch_limit)
            if not ohlcv:
                return jsonify({'error': f'{symbol_name} 获取K线数据失败'}), 500

            df = system.exchange_api.ohlcv_to_dataframe(ohlcv)
            df = system.exchange_api.filter_closed_candles(df, '1d')
            if len(df) < required_closed:
                return jsonify({'error': f'{symbol_name} K线数据不足：{strategy_type} 策略至少需要 '
                                         f'{required_closed} 根已收盘K线，当前仅 {len(df)} 根'}), 400

            try:
                current_price = system.exchange_api.get_last_price(ccxt_symbol)
            except Exception as e:
                current_price = float(df['close'].iloc[-1])
                logger.warning(f"{symbol_name} 获取实时市价失败({e})，回退收盘价: {current_price}")

            signal_side = None
            stop_loss_price = None
            signal_info = {}

            if strategy_type == 'ma_cross':
                signal = system.ma_cross_strategy.check_current_state(df)
                if not signal:
                    return jsonify({'error': f'{symbol_name} K线数据不足，无法计算EMA信号'}), 400
                signal_side = signal.get('action')
                if signal_side not in ('long', 'short'):
                    return jsonify({'error': f'{symbol_name} 当前EMA无明确方向（短期EMA≈长期EMA）',
                                    'info': {'ema_short': round(signal.get('ema_short', 0), 2),
                                             'ema_long': round(signal.get('ema_long', 0), 2),
                                             'current_price': current_price}}), 400
                stop_loss_price = signal['lower_stop'] if signal_side == 'long' else signal['upper_stop']
                signal_info = {'ema_short': round(signal.get('ema_short', 0), 2),
                               'ema_long': round(signal.get('ema_long', 0), 2),
                               'upper_stop': round(signal.get('upper_stop', 0), 2),
                               'lower_stop': round(signal.get('lower_stop', 0), 2),
                               'strategy': 'ma_cross'}
            else:
                signal = system.turtle_strategy.check_current_state(df)
                if not signal:
                    return jsonify({'error': f'{symbol_name} K线数据不足，无法计算信号'}), 400
                signal_side = signal.get('action')
                if signal_side not in ('long', 'short'):
                    return jsonify({'error': f'{symbol_name} 当前无海龟通道突破信号',
                                    'info': {'upper': signal.get('upper_line'), 'lower': signal.get('lower_line'),
                                             'mid': signal.get('mid_line'), 'current_price': current_price}}), 400
                stop_loss_price = signal['lower_line'] if signal_side == 'long' else signal['upper_line']
                signal_info = {'upper': signal.get('upper_line'), 'lower': signal.get('lower_line'),
                               'mid': signal.get('mid_line'), 'strategy': 'turtle'}

            symbol_config = {'name': symbol_name, 'enabled': True,
                             'risk_per_trade': risk_per_trade, 'strategy': strategy_type}
            # buffer_notification=False：本路由下方自发专属钉钉，不进日检汇总缓冲
            system._execute_open(symbol_name, signal_side, current_price, stop_loss_price, symbol_config,
                                 buffer_notification=False)

            new_position = system.trade_state.get_open_position(symbol_name)
            if not new_position:
                return jsonify({'error': f'{symbol_name} 开仓执行失败，请查看日志'}), 500

            with system._config_lock:
                exists = any(s['name'] == symbol_name for s in system.config['trading']['symbols'])
                if not exists:
                    backup_symbols = json.loads(json.dumps(system.config['trading']['symbols']))
                    system.config['trading']['symbols'].append(symbol_config)
                    err_resp = _commit_config_or_rollback(
                        system, 'trading', 'symbols', backup_symbols,
                        f'{symbol_name} 已开仓，但写入交易对配置失败，请检查磁盘和配置文件')
                    if err_resp:
                        return err_resp

            direction_text = '做多' if signal_side == 'long' else '做空'
            strategy_text = '海龟通道' if strategy_type == 'turtle' else '双均线EMA'
            send_dingtalk(f'[{system.label}-即时开仓] {symbol_name} {direction_text} ({strategy_text})\n'
                          f'入场价: {new_position["entry_price"]}\n'
                          f'数量: {new_position["position_size"]}\n'
                          f'止损价: {new_position["stop_loss_price"]}\n'
                          f'风险度: {risk_per_trade*100:.1f}%\n已自动加入标准交易对管理')
            return jsonify({'status': 'success', 'message': f'{symbol_name} 即时开仓成功，已加入标准交易对',
                            'trade': {'symbol': symbol_name, 'side': signal_side,
                                      'entry_price': new_position['entry_price'],
                                      'position_size': new_position['position_size'],
                                      'stop_loss_price': new_position['stop_loss_price'],
                                      'stop_order_id': new_position.get('stop_order_id'),
                                      'risk_per_trade': risk_per_trade, 'strategy': strategy_type,
                                      'signal_info': signal_info}})
        finally:
            system._trade_lock.release()
    except Exception as e:
        logger.error(f"即时开仓异常: {e}")
        return jsonify({'error': f'即时开仓异常: {str(e)}'}), 500


@app.route('/api/close_position', methods=['POST'])
@require_auth
def close_position():
    """手动平仓指定交易对。"""
    system, err = _require_system()
    if err:
        return err
    try:
        # 与 08:00 日检 / 盘中巡检互斥，防止并发下单（拿不到锁直接拒绝，不排队）
        if not system._trade_lock.acquire(blocking=False):
            return jsonify({'error': '交易检查/巡检正在执行中，请稍后再试'}), 409
        try:
            data = request.get_json(silent=True)
            if not data or 'name' not in data:
                return jsonify({'error': '缺少交易对名称'}), 400

            try:
                symbol_name = normalize_symbol_name(data['name'])
            except ValueError as e:
                return jsonify({'error': str(e)}), 400
            position = system.trade_state.get_open_position(symbol_name)
            if not position:
                return jsonify({'error': f'{symbol_name} 没有持仓记录'}), 400

            ccxt_symbol = system.exchange_api.to_ccxt_symbol(symbol_name)
            close_order = system.exchange_api.close_position(ccxt_symbol, position['side'], position['position_size'])
            if not close_order:
                return jsonify({'error': f'{symbol_name} 平仓失败'}), 500

            actual_price = close_order.get('average', None)
            if actual_price and isinstance(actual_price, str):
                actual_price = float(actual_price)
            if not actual_price:
                try:
                    actual_price = system.exchange_api.get_last_price(ccxt_symbol) or position['entry_price']
                except Exception:
                    actual_price = position['entry_price']

            if not system._cancel_stop_order_confirmed(symbol_name, ccxt_symbol, position.get('stop_order_id')):
                logger.error(f"[{system.label}] {symbol_name} 手动平仓后止损撤销不可确认，已标记残留并阻断该品种新开仓")

            # 记平 + 落盘失败的运行时补偿 + 告警 + 止损异常警示清理，统一复用主系统的收口方法
            closed_position, state_saved = system._close_trade_state_with_runtime_fallback(
                symbol_name, actual_price, "手动平仓")
            if not state_saved:
                return jsonify({'error': f'{symbol_name} 已在交易所平仓，但本地状态保存失败，请立即检查'}), 500

            direction_text = '做多' if position['side'] == 'long' else '做空'
            send_dingtalk(f'[{system.label}-手动平仓] {symbol_name} {direction_text}\n'
                          f'出场价: {actual_price}\n'
                          f'盈亏: {closed_position.get("pnl", 0):.2f} USDT\n'
                          f'盈亏率: {closed_position.get("pnl_percent", 0):.2f}%')
            return jsonify({'status': 'success', 'message': f'{symbol_name} 平仓成功', 'trade': closed_position})
        finally:
            system._trade_lock.release()
    except Exception as e:
        logger.error(f"手动平仓异常: {e}")
        return jsonify({'error': f'手动平仓异常: {str(e)}'}), 500


@app.route('/api/channel_data', methods=['GET'])
@require_auth
def get_channel_data():
    """获取某个持仓的K线和通道数据。"""
    system, err = _require_system()
    if err:
        return err
    try:
        symbol = request.args.get('symbol', '')
        if not symbol:
            return jsonify({'error': '缺少symbol参数'}), 400

        strategy_config = system.config.get('strategy', {})
        period = strategy_config.get('channel_period', 28)  # 兜底与 TurtleStrategy 默认一致
        required_closed = required_closed_candles_for_strategy('turtle', strategy_config)
        fetch_limit = max(60, required_closed + 1)

        ccxt_symbol = system.exchange_api.to_ccxt_symbol(symbol)
        ohlcv = system.exchange_api.fetch_ohlcv(ccxt_symbol, '1d', limit=fetch_limit)
        df = system.exchange_api.ohlcv_to_dataframe(ohlcv)
        df = system.exchange_api.filter_closed_candles(df, timeframe='1d')
        if len(df) < period + 1:
            return jsonify({'error': f'{symbol} K线数据不足：海龟通道周期 {period} 至少需要 '
                                     f'{period + 1} 根已收盘K线，当前仅 {len(df)} 根'}), 400

        closes = df['close'].values
        upper_list, lower_list = [], []
        for i in range(len(closes)):
            if i < period:
                upper_list.append(None)
                lower_list.append(None)
            else:
                upper_list.append(max(closes[i-period:i]))
                lower_list.append(min(closes[i-period:i]))
        df['upper'] = upper_list
        df['lower'] = lower_list
        df = df.dropna(subset=['upper', 'lower']).copy()
        df['middle'] = (df['upper'] + df['lower']) / 2

        positions = system.trade_state.get_all_open_positions()
        pos = positions.get(symbol, {})
        dates = df['timestamp'].dt.strftime('%m-%d').tolist()
        result = {
            'dates': dates, 'closes': df['close'].tolist(),
            'upper': df['upper'].tolist(), 'lower': df['lower'].tolist(), 'middle': df['middle'].tolist(),
            'entry_price': pos.get('entry_price'), 'stop_loss': pos.get('stop_loss_price'),
            'current_price': df['close'].iloc[-1] if len(df) > 0 else None,
            'side': pos.get('side', ''), 'unrealized_pnl': None, 'unrealized_pnl_pct': None
        }
        if result['entry_price'] and result['current_price']:
            ep = result['entry_price']
            cp = result['current_price']
            size = pos.get('position_size', 0)
            if pos.get('side') == 'long':
                result['unrealized_pnl'] = (cp - ep) * size
                result['unrealized_pnl_pct'] = (cp - ep) / ep * 100
            else:
                result['unrealized_pnl'] = (ep - cp) * size
                result['unrealized_pnl_pct'] = (ep - cp) / ep * 100
        return jsonify(result)
    except Exception as e:
        logger.error(f"获取通道数据失败: {e}")
        return jsonify({'error': str(e)}), 500


def _set_manual_check_running(value):
    global _manual_check_running
    with _manual_check_guard:
        if value and _manual_check_running:
            return False  # 已在执行，拒绝重入
        _manual_check_running = value
        return True


@app.route('/api/manual_check', methods=['POST'])
@require_auth
def manual_check():
    """手动触发交易信号检查（后台线程执行，防重入）。"""
    system, err = _require_system()
    if err:
        return err
    # 要求 JSON body（空 {} 即可）：其余写接口因解析 JSON 天然免疫跨站表单，
    # 本接口原本无参数，补上同等门槛（防 CSRF 触发手动检查）
    if request.get_json(silent=True) is None:
        return jsonify({'error': '请求须携带 JSON body（空对象 {} 即可），'
                                 '例如: curl -X POST -H "Content-Type: application/json" -d "{}"'}), 400
    if not _set_manual_check_running(True):
        return jsonify({'status': 'busy', 'message': '已有手动检查在执行中，请稍后再试'}), 409
    try:
        logger.info(f"[{system.label}] 手动触发交易检查")

        def run_check():
            try:
                system.check_and_execute_trades(manual_run=True)
                logger.info(f"[{system.label}] 手动触发的交易检查执行完毕")
            except Exception as e:
                logger.error(f"[{system.label}] 手动触发的交易检查失败: {e}")
            finally:
                _set_manual_check_running(False)

        threading.Thread(target=run_check, daemon=True).start()
        return jsonify({'status': 'running', 'message': '交易检查已触发，正在后台执行...'})
    except Exception as e:
        _set_manual_check_running(False)
        logger.error(f"手动触发失败: {e}")
        return jsonify({'error': str(e)}), 500


def _bootstrap():
    """创建欧易单交易所系统（供 __main__ / wsgi 调用）。"""
    global trading_system
    if trading_system is not None:
        return trading_system
    from main import TradingSystem
    trading_system = TradingSystem()
    return trading_system


if __name__ == '__main__':
    _bootstrap()
    threading.Thread(target=trading_system.start, daemon=True).start()
    logger.info("HTTP API 服务器启动在 0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
