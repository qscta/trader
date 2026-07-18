from flask import Flask, jsonify, request, send_from_directory, session
from contextlib import contextmanager
from functools import wraps
from werkzeug.middleware.proxy_fix import ProxyFix
import json
import logging
import threading
import time
import os
import secrets
from datetime import datetime
from trade_executor import safe_fill_price
from trade_state import enrich_closed_trade_with_fees

# 品种写接口的输入校验（脏数据会进 config、前端渲染和真实下单路径，必须挡在门口）。
# 校验口径全部取自 config_validation——与手写 config.json 的启动校验同一事实源，
# 三入口（前端/API/文件）由构造保证一致，不再靠人工同步两份常量。
from config_validation import (MA_PARAMETER_FIELDS, PERIOD_MAX, PERIOD_MIN,
                               SYMBOL_CONFIG_FIELDS, ohlcv_fetch_limit,
                               required_closed_candles, strict_int,
                               strict_risk_per_trade, strict_bool,
                               normalize_symbol_name, strict_float_finite,
                               validate_ohlcv_capacity)


def _validate_symbol_input(name, risk_per_trade=None, enabled=None):
    """校验并**规范化**品种写入字段（与启动校验 main._validate_symbol_configs 同源）。

    返回 (clean, error)：error 非 None 时 clean 为 None；否则 clean 仅含传入（非 None）
    字段的规范化值——name 恒有；risk_per_trade→float、enabled→bool。
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


def _validate_flask_secret_key(value):
    """校验 Flask 签名密钥。所有 Session 认证都依赖它，「非空」不够：
    短密钥可被离线猜中并伪造 authenticated=True 绕过全部写接口。
    """
    if not value:
        raise RuntimeError('未配置 FLASK_SECRET_KEY，拒绝启动')
    if len(str(value).encode('utf-8')) < 32:
        raise RuntimeError('FLASK_SECRET_KEY 至少需要 32 字节的随机值，拒绝弱密钥启动')
    return str(value)


# 反代跳数由部署方声明（代码无法安全地自动探测——盲信 X-Forwarded-For 本身就是漏洞）：
# 0 = 无反代直连（默认，完全不信 XFF）；1 = 单反代 / Cloudflare Tunnel（真实客户端 IP
# 在链尾）；2 = CDN→nginx 双层。登录防爆破按还原后的 remote_addr 计数——跳数配错时
# 计数键要么可被伪造（绕过锁定）、要么全体访客共享（互相连坐锁定）。
_PROXYFIX_HOPS = _parse_proxyfix_hops(os.environ.get('TRADING_PROXYFIX_X_FOR', '0'))
if _PROXYFIX_HOPS > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_PROXYFIX_HOPS, x_proto=1)
app.config['JSON_AS_ASCII'] = False   # Flask < 2.3
try:
    app.json.ensure_ascii = False     # Flask >= 2.2（JSON_AS_ASCII 已废弃）
except AttributeError:
    pass
app.secret_key = _validate_flask_secret_key(os.environ.get('FLASK_SECRET_KEY'))

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
_manual_check_thread = None
_manual_check_guard = threading.Lock()

# 登录防爆破：内存级按 IP 退避（连续 5 次失败锁 60 秒，成功即清零；进程重启即重置，无需持久化）
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60
LOGIN_FAILURE_CACHE_MAX = 4096
_login_failures = {}   # ip -> (连续失败次数, 锁定截止时间戳)
_login_guard = threading.Lock()

API_TOKEN = os.environ.get('TRADING_API_TOKEN')

# 由 wsgi / __main__ 保存真实 runner 线程状态。不能仅凭 trading_system 非空就宣称
# “运行中”：调度线程若在 register_jobs/start 中异常退出，Web 仍可能完全正常。
_runner_thread = None
_runner_started_at = None
_runner_failure = None
_runner_guard = threading.Lock()


def _prune_login_failures(now):
    """清除过期项并对攻击者可控的 IP 字典设置硬上限。必须在 _login_guard 内调用。"""
    expired = [ip for ip, (_fails, locked_until) in _login_failures.items()
               if locked_until and locked_until <= now]
    for ip in expired:
        _login_failures.pop(ip, None)
    while len(_login_failures) >= LOGIN_FAILURE_CACHE_MAX:
        _login_failures.pop(next(iter(_login_failures)), None)


def start_runner_thread(system):
    """启动并登记交易 runner；异常被记录，供 /api/status fail-loud。"""
    global _runner_thread, _runner_started_at, _runner_failure

    def run():
        global _runner_failure
        try:
            system.start()
        except BaseException as exc:
            with _runner_guard:
                _runner_failure = f'{type(exc).__name__}: {exc}'
            logger.critical('交易 runner 已退出', exc_info=True)

    with _runner_guard:
        if _runner_thread is not None and _runner_thread.is_alive():
            return _runner_thread
        stop_event = getattr(system, '_stop_event', None)
        if stop_event is not None:
            # 必须在线程启动前清；若让 main.start 在线程内清，紧随 start 的 stop()
            # 可能先 set、后被后台线程 clear，丢失退出请求并卡住 worker shutdown。
            stop_event.clear()
        _runner_failure = None
        _runner_started_at = datetime.now().isoformat()
        _runner_thread = threading.Thread(
            target=run, name='trading-system-runner', daemon=True)
        _runner_thread.start()
        return _runner_thread


def stop_runner_thread(timeout=895):
    """请求 runner 优雅停止并等待交易 job/调度器收尾；供 Gunicorn hooks 调用。"""
    deadline = time.monotonic() + max(0, timeout)
    with _runner_guard:
        thread = _runner_thread
    system = trading_system
    if system is not None and hasattr(system, 'stop'):
        try:
            system.stop()
        except Exception:
            logger.exception('请求交易 runner 停止失败')
    if thread is not None and thread.is_alive():
        thread.join(timeout=max(0, deadline - time.monotonic()))
    runner_alive = bool(thread and thread.is_alive())
    with _manual_check_guard:
        manual_thread = _manual_check_thread
    if manual_thread is not None and manual_thread.is_alive():
        manual_thread.join(timeout=max(0, deadline - time.monotonic()))
    manual_alive = bool(manual_thread and manual_thread.is_alive())
    if runner_alive or manual_alive:
        logger.critical(f'后台交易线程在 {timeout}s 内未能优雅停止；worker 不应强制退出')
    return not runner_alive and not manual_alive


def _runner_health(system):
    """返回 runner/scheduler/heartbeat 的真实健康快照。"""
    with _runner_guard:
        thread = _runner_thread
        started_at = _runner_started_at
        failure = _runner_failure
    issues = []
    if thread is None:
        issues.append('runner_thread_unregistered')
        thread_alive = False
    else:
        thread_alive = thread.is_alive()
        if not thread_alive:
            issues.append('runner_thread_stopped')

    # TradingSystem.health_snapshot 同时检查 APScheduler 内部线程。单看
    # scheduler.running 会在调度线程异常退出后仍保留 RUNNING 状态；
    # runner 主循环又会继续更新 heartbeat，从而把「所有 job 都停了」
    # 误报为健康。测试桩没有 health_snapshot 时才走兼容回退。
    system_health = {}
    snapshot_reader = getattr(system, 'health_snapshot', None)
    if callable(snapshot_reader):
        try:
            system_health = snapshot_reader()
            if not isinstance(system_health, dict):
                raise TypeError('health_snapshot 必须返回对象')
        except Exception as exc:
            logger.error(f'读取交易 runner 健康快照失败: {exc}')
            issues.append('system_health_unavailable')
            system_health = {}

    scheduler = getattr(system, 'scheduler', None)
    scheduler_running = bool(system_health.get(
        'scheduler_running', getattr(scheduler, 'running', False)))
    scheduler_thread_alive = system_health.get('scheduler_thread_alive')
    if scheduler_thread_alive is None:
        scheduler_thread = getattr(scheduler, '_thread', None)
        if scheduler_thread is None:
            scheduler_thread_alive = scheduler_running
        else:
            try:
                scheduler_thread_alive = bool(scheduler_thread.is_alive())
            except Exception:
                scheduler_thread_alive = False
    if not scheduler_running:
        issues.append('scheduler_not_running')
    elif not scheduler_thread_alive:
        issues.append('scheduler_thread_stopped')

    # main.start 会更新该 epoch；兼容尚未带 heartbeat 的测试桩，但真实部署一旦存在
    # 心跳，超过阈值就明确降级。阈值需覆盖主循环 60 秒睡眠及短暂调度抖动。
    heartbeat = system_health.get(
        'runner_heartbeat_ts', getattr(system, '_runner_heartbeat_ts', None))
    heartbeat_age = None
    if heartbeat is not None:
        try:
            heartbeat_age = max(0.0, time.time() - float(heartbeat))
            if heartbeat_age > 150:
                issues.append('runner_heartbeat_stale')
        except (TypeError, ValueError):
            issues.append('runner_heartbeat_invalid')
    elif hasattr(system, '_runner_heartbeat_ts'):
        issues.append('runner_heartbeat_missing')
    stop_event = getattr(system, '_stop_event', None)
    stopping = system_health.get('stopping')
    if stopping is None and stop_event is not None:
        stopping = stop_event.is_set()
    if stopping:
        issues.append('runner_stopping')
    if failure:
        issues.append('runner_failed')
    return {
        'healthy': not issues,
        'runner_thread_alive': thread_alive,
        'scheduler_running': scheduler_running,
        'scheduler_thread_alive': bool(scheduler_thread_alive),
        'runner_started_at': started_at,
        'heartbeat_age_seconds': round(heartbeat_age, 1) if heartbeat_age is not None else None,
        'failure': failure,
        'issues': issues,
    }


@contextmanager
def _trade_then_config(system):
    """所有影响交易的配置提交统一按 trade→config 顺序加锁。

    非阻塞获取可避免 HTTP 请求在一次长交易重试后方继续提交用户早已放弃的修改。
    """
    acquired = system._trade_lock.acquire(blocking=False)
    if not acquired:
        yield False
        return
    try:
        with system._config_lock:
            yield True
    finally:
        system._trade_lock.release()


def _explicit_null_error(data, fields):
    for field in fields:
        if field in data and data[field] is None:
            return f'{field} 不允许为 null；请提供有效值或省略该字段'
    return None


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


def _load_closed_daily_df(system, symbol, fetch_limit):
    """统一的日 K 加载：内部符号 → fetch → DataFrame → 过滤未收盘。

    新增品种回溯与即时开仓两处路由共用同一流程；K 线为空返回
    (ccxt_symbol, None)，由调用方按各自路由语义处理。行情边界校验
    （NaN/乱序/区间矛盾整批拒绝）已在适配层 fetch_ohlcv 出口统一执行，
    异常按各路由自身的 try/except 语义向上抛。
    """
    ccxt_symbol = system.exchange_api.to_ccxt_symbol(symbol)
    ohlcv = system.exchange_api.fetch_ohlcv(ccxt_symbol, '1d', limit=fetch_limit)
    if not ohlcv:
        return ccxt_symbol, None
    df = system.exchange_api.ohlcv_to_dataframe(ohlcv)
    return ccxt_symbol, system.exchange_api.filter_closed_candles(
        df, timeframe='1d')


def _persist_config():
    """把整份 config 写回磁盘。"""
    if trading_system and hasattr(trading_system, 'persist_config'):
        return trading_system.persist_config()
    return False


def _commit_config_or_rollback(system, section_key, sub_key, backup, fail_message):
    """配置提交：持久化整份 config；写盘失败把 config[section][sub]（sub 为 None 则整段）
    回滚为 backup，并返回 500 错误响应；成功则 reload 策略并返回 None。
    必须在固定顺序的 system._trade_lock → system._config_lock 内调用
    （写路由的统一收口，替代五处重复样板）。"""
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
        _prune_login_failures(now)
        _fails, locked_until = _login_failures.get(ip, (0, 0.0))
        if now < locked_until:
            return jsonify({'success': False,
                            'message': f'登录失败次数过多，请 {int(locked_until - now) + 1} 秒后再试'}), 429
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
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
        if ip not in _login_failures:
            _prune_login_failures(now)
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
        # 从文件尾向前按块读取；正常请求不再为 80 行日志整读整个 10MB 文件。
        chunks = b''
        result = []
        with open(log_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            while pos > 0 and len(result) < lines:
                size = min(8192, pos)
                pos -= size
                f.seek(pos)
                chunks = f.read(size) + chunks
                raw_lines = chunks.splitlines(keepends=True)
                # 仍未读到文件头时，首段可能只是某一行的后半截，不能返回乱码半行。
                candidates = raw_lines if pos == 0 else raw_lines[1:]
                decoded = [line.decode('utf-8', errors='replace') for line in candidates]
                result = [line for line in decoded
                          if not (' "' in line and 'HTTP/1.1' in line)
                          and 'code 400, message Bad request' not in line]
        result = result[-lines:]
        result.reverse()
        return jsonify({'logs': result})
    except FileNotFoundError:
        return jsonify({'logs': []})
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
        try:
            position_quarantines = system.trade_state.get_position_quarantines()
        except Exception:
            position_quarantines = {}
        stop_anomalies = dict(getattr(system, '_stop_anomalies', {}) or {})
        health = _runner_health(system)
        payload = {
            'status': 'running' if health['healthy'] else 'degraded',
            'health': health,
            'exchange': system.exchange_id,
            'label': system.label,
            'open_positions_count': len(open_positions),
            'open_positions': open_positions,
            'enabled_symbols': manual_symbols,
            'manual_pool_count': len(manual_symbols),
            'stop_residues': stop_residues,
            'stop_anomalies': stop_anomalies,
            'position_quarantines': position_quarantines,
            'last_symbol_update': last_symbol_update
        }
        return jsonify(payload), (200 if health['healthy'] else 503)
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
        if not isinstance(data, dict) or 'name' not in data:
            return jsonify({'error': '缺少必要参数'}), 400
        unknown = sorted(set(data) - SYMBOL_CONFIG_FIELDS)
        if unknown:
            return jsonify({'error': f'未知字段: {unknown}'}), 400
        null_error = _explicit_null_error(
            data, ('name', 'risk_per_trade', 'enabled'))
        if null_error:
            return jsonify({'error': null_error}), 400

        clean, invalid = _validate_symbol_input(
            data.get('name'),
            data['risk_per_trade'] if 'risk_per_trade' in data else 0.01,
            data['enabled'] if 'enabled' in data else True)
        if invalid:
            return jsonify({'error': invalid}), 400
        new_symbol = {  # 全部用规范化值：name 大写、risk float、enabled 真 bool
            'name': clean['name'], 'enabled': clean['enabled'],
            'risk_per_trade': clean['risk_per_trade'],
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

        with _trade_then_config(system) as locked:
            if not locked:
                return jsonify({'error': '交易检查/巡检正在执行中，请稍后再修改配置'}), 409
            if any(s['name'] == new_symbol['name'] for s in system.config['trading']['symbols']):
                return jsonify({'error': '交易对已存在'}), 400
            intent_getter = getattr(system.trade_state, 'get_open_intent', None)
            if callable(intent_getter) and intent_getter(new_symbol['name']):
                return jsonify({'error': f"{new_symbol['name']} 存在未收口开仓意图，禁止重新加入/改配"}), 409
            backup_symbols = json.loads(json.dumps(system.config['trading']['symbols']))
            system.config['trading']['symbols'].append(new_symbol)
            err_resp = _commit_config_or_rollback(system, 'trading', 'symbols', backup_symbols, '配置写入失败，交易对未添加')
            if err_resp:
                return err_resp

        strategy_text = '双均线EMA'
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
        if not isinstance(data, dict) or not data:
            return jsonify({'error': '缺少参数'}), 400
        unknown = sorted(set(data) - (SYMBOL_CONFIG_FIELDS - {'name'}))
        if unknown:
            return jsonify({'error': f'未知字段: {unknown}'}), 400
        null_error = _explicit_null_error(data, ('risk_per_trade', 'enabled'))
        if null_error:
            return jsonify({'error': null_error}), 400
        clean, invalid = _validate_symbol_input(
            symbol, data.get('risk_per_trade'), data.get('enabled'))
        if invalid:
            return jsonify({'error': invalid}), 400
        if not any(k in clean for k in ('risk_per_trade', 'enabled')):
            return jsonify({'error': '缺少可更新字段（risk_per_trade / enabled）'}), 400
        symbol_u = clean['name']
        changes = None
        with _trade_then_config(system) as locked:
            if not locked:
                return jsonify({'error': '交易检查/巡检正在执行中，请稍后再修改配置'}), 409
            # 未收口生命周期存在时，只允许“单独禁用”这一紧急收口动作；
            # 风险修改或重新启用可能让旧意图被另一套配置接管。
            disabling_only = (
                clean.get('enabled') is False and
                'risk_per_trade' not in clean)
            intent_getter = getattr(system.trade_state, 'get_open_intent', None)
            open_intent = intent_getter(symbol_u) if callable(intent_getter) else None
            if open_intent and not disabling_only:
                # 紧急“禁用”必须允许：恢复裁决会在确认从未发单后消费意图；
                # 若交易所已有真钱仓则仍补账/补止损，并按退池仓只平不开托管。
                return jsonify({'error': f'{symbol_u} 存在未收口开仓意图，'
                                         '仅允许紧急禁用，禁止改风险或重新启用'}), 409
            backup_symbols = json.loads(json.dumps(system.config['trading']['symbols']))
            for s in system.config['trading']['symbols']:
                if s['name'] == symbol_u:
                    if 'enabled' in clean:
                        s['enabled'] = clean['enabled']          # 规范化真 bool（挡 "false"）
                    if 'risk_per_trade' in clean:
                        s['risk_per_trade'] = clean['risk_per_trade']  # 规范化 float
                    err_resp = _commit_config_or_rollback(system, 'trading', 'symbols', backup_symbols, '配置写入失败，更新已回滚')
                    if err_resp:
                        return err_resp

                    changes = []
                    if 'enabled' in clean:
                        changes.append(f'状态: {"启用" if clean["enabled"] else "禁用"}')
                    if 'risk_per_trade' in clean:
                        changes.append(f'风险度: {clean["risk_per_trade"]*100:.1f}%')
                    break
        if changes is None:
            return jsonify({'error': '交易对不存在'}), 404
        # 外部网络通知不得占用 trade→config 双锁：失败重试最长可阻塞
        # 数十秒，会延迟日检/盘中巡检。
        send_dingtalk(f'[{system.label}] 更新交易对: {symbol_u}, {", ".join(changes)}')
        return jsonify({'status': 'success', 'message': f'交易对 {symbol_u} 已更新'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/symbols/<symbol>', methods=['DELETE'])
@require_auth
def delete_symbol(symbol):
    system, err = _require_system()
    if err:
        return err
    try:
        try:
            symbol_u = normalize_symbol_name(symbol)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        held = None
        with _trade_then_config(system) as locked:
            if not locked:
                return jsonify({'error': '交易检查/巡检正在执行中，请稍后再修改配置'}), 409
            # 品种不在池中：直接 404，与 update_symbol 的语义一致。否则「删不存在的品种」
            # 会走完过滤（空操作）后返回 200 已删除，还误发一条删除钉钉，掩盖前端/调用方的错。
            if not any(s['name'] == symbol_u for s in system.config['trading']['symbols']):
                return jsonify({'error': '交易对不存在'}), 404
            held = system.trade_state.get_open_position(symbol_u)
            backup_symbols = json.loads(json.dumps(system.config['trading']['symbols']))
            system.config['trading']['symbols'] = [
                s for s in system.config['trading']['symbols'] if s['name'] != symbol_u
            ]
            err_resp = _commit_config_or_rollback(system, 'trading', 'symbols', backup_symbols, '配置写入失败，删除已回滚')
            if err_resp:
                return err_resp
            # 清理也必须留在同一 trade lock 内；否则锁释放后即时开仓可插入，清理线程
            # 还拿旧的 exchange-flat 结论删除新仓的信号/T+1 元数据。
            if not held and hasattr(system.trade_state, 'remove_symbol_metadata'):
                try:
                    clear_quarantine = False
                    if hasattr(system.exchange_api, 'get_position'):
                        ccxt_symbol = system.exchange_api.to_ccxt_symbol(symbol_u)
                        exchange_position = system.exchange_api.get_position(ccxt_symbol)
                        clear_quarantine = not exchange_position or float(
                            exchange_position.get('contracts') or 0) == 0
                    system.trade_state.remove_symbol_metadata(
                        symbol_u, clear_quarantine=clear_quarantine)
                except Exception as e:
                    # 查询失败时 fail-closed：不清 quarantine；配置删除本身已成功。
                    logger.warning(f'删除 {symbol_u} 后清理辅助状态失败（隔离记录保留）: {e}')
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
        try:
            page = int(request.args.get('page', 1))
            page_size = int(request.args.get('page_size', 100))
        except (TypeError, ValueError):
            return jsonify({'error': 'page/page_size 必须是整数'}), 400
        if page < 1 or not (1 <= page_size <= 200):
            return jsonify({'error': 'page 必须 >= 1，page_size 必须在 1-200'}), 400
        page_reader = getattr(system.trade_state, 'get_closed_trades_page', None)
        if callable(page_reader):
            selected, total = page_reader(page, page_size)
        else:
            all_trades = system.trade_state.get_closed_trades()
            total = len(all_trades)
            start = (page - 1) * page_size
            selected = list(reversed(all_trades))[start:start + page_size]
        # 接口按最新在前分页，响应和常态内存工作量都限制在 page_size。
        trades = [enrich_closed_trade_with_fees(t) for t in selected]
        total_pages = (total + page_size - 1) // page_size
        return jsonify({
            'trades': trades,
            'page': page,
            'page_size': page_size,
            'total': total,
            'total_pages': total_pages,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/trades_summary', methods=['GET'])
@require_auth
def get_trades_summary():
    system, err = _require_system()
    if err:
        return err
    try:
        revision_fn = getattr(system.trade_state, 'get_closed_trades_revision', None)
        revision = revision_fn() if callable(revision_fn) else None
        cached = getattr(system, '_trades_summary_cache', None)
        if revision is not None and cached and cached.get('revision') == revision:
            return jsonify(cached['payload'])
        trades = [enrich_closed_trade_with_fees(t) for t in system.trade_state.get_closed_trades()]
        if not trades:
            payload = {'total': 0}
            if revision is not None:
                system._trades_summary_cache = {'revision': revision, 'payload': payload}
            return jsonify(payload)
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
        payload = {
            'total': total, 'win_count': win_count, 'loss_count': loss_count,
            'win_rate': round(win_rate, 1), 'total_pnl': round(total_pnl, 2),
            'avg_win': round(avg_win, 2), 'avg_loss': round(avg_loss, 2),
            'profit_factor': round(profit_factor, 2) if profit_factor else None,
            'avg_pnl_pct': round(avg_pnl_pct, 2),
            'max_win': round(max_win, 2), 'max_loss': round(max_loss, 2)
        }
        if revision is not None:
            system._trades_summary_cache = {'revision': revision, 'payload': payload}
        return jsonify(payload)
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
        # 即使无 flow_amount 也必须发送 JSON 对象 {}：阻断跨站表单 POST，且不把
        # text/plain / HTML 错误请求静默解释为一次真实的资金基准重置。
        data = request.get_json(silent=True)
        if not request.is_json or not isinstance(data, dict):
            return jsonify({'error': '请求必须是 JSON 对象（无参数时发送 {}）'}), 400
        unknown = sorted(set(data) - {'flow_amount'})
        if unknown:
            return jsonify({'error': f'未知字段: {unknown}；只允许 flow_amount'}), 400
        if 'flow_amount' in data and data['flow_amount'] is None:
            return jsonify({'error': 'flow_amount 不允许为 null；请提供有效值或省略该字段'}), 400
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
        if not isinstance(data, dict) or not data:
            return jsonify({'error': '缺少参数'}), 400
        unknown = sorted(set(data) - MA_PARAMETER_FIELDS)
        if unknown:
            return jsonify({'error': f'未知策略参数: {unknown}'}), 400
        null_error = _explicit_null_error(
            data, ('ma_short_period', 'ma_long_period',
                   'ma_stop_period', 'default_risk_per_trade'))
        if null_error:
            return jsonify({'error': null_error}), 400
        # 范围硬校验：非法值 400，不写配置、不 reload（周期严格整数；短期必须小于长期；风险度限幅）
        # strict_int 与启动校验同源：28.9 拒绝而非截断，三入口口径完全一致
        try:
            parsed = {}
            for key in ('ma_short_period', 'ma_long_period', 'ma_stop_period'):
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
        if not parsed:
            return jsonify({'error': '缺少可更新策略参数'}), 400
        changed = []
        params_snapshot = None
        with _trade_then_config(system) as locked:
            if not locked:
                return jsonify({'error': '交易检查/巡检正在执行中，请稍后再修改配置'}), 409
            cur = system.config.get('strategy', {})
            eff_short = parsed.get('ma_short_period', cur.get('ma_short_period', 7))
            eff_long = parsed.get('ma_long_period', cur.get('ma_long_period', 28))
            if eff_short >= eff_long:
                return jsonify({'error': f'EMA 短期({eff_short})必须小于长期({eff_long})'}), 400
            proposed = dict(cur)
            proposed.update(parsed)
            try:
                validate_ohlcv_capacity(proposed)
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 400
            backup = json.loads(json.dumps(system.config.get('strategy', {})))
            sp = system.config.setdefault('strategy', {})

            label_map = {'ma_short_period': 'EMA短期周期',
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
            params_snapshot = json.loads(json.dumps(system.config['strategy']))
        send_dingtalk(f'[{system.label}] 策略参数更新: {", ".join(changed)}')
        return jsonify({'status': 'success', 'message': '策略参数已更新', 'params': params_snapshot})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/instant_open', methods=['POST'])
@require_auth
def instant_open():
    """按当前双均线方向即时开仓。"""
    system, err = _require_system()
    if err:
        return err
    try:
        # 与 08:00 日检 / 盘中巡检互斥，防止并发下单（拿不到锁直接拒绝，不排队）
        if not system._trade_lock.acquire(blocking=False):
            return jsonify({'error': '交易检查/巡检正在执行中，请稍后再试'}), 409
        try:
            data = request.get_json(silent=True)
            if not isinstance(data, dict) or 'name' not in data:
                return jsonify({'error': '缺少交易对名称'}), 400
            unknown = sorted(set(data) - (SYMBOL_CONFIG_FIELDS - {'enabled'}))
            if unknown:
                return jsonify({'error': f'未知字段: {unknown}'}), 400
            null_error = _explicit_null_error(data, ('name', 'risk_per_trade'))
            if null_error:
                return jsonify({'error': null_error}), 400
            clean, invalid = _validate_symbol_input(
                data.get('name'),
                data['risk_per_trade'] if 'risk_per_trade' in data else 0.01)
            if invalid:
                return jsonify({'error': invalid}), 400
            symbol_name = clean['name']
            risk_per_trade = clean['risk_per_trade']  # 规范化 float，杜绝字符串进仓位计算

            if system.trade_state.get_open_position(symbol_name):
                return jsonify({'error': f'{symbol_name} 已有持仓，无法重复开仓'}), 400

            fetch_limit = ohlcv_fetch_limit(system.config.get('strategy', {}))
            required_closed = required_closed_candles(system.config.get('strategy', {}))
            ccxt_symbol, df = _load_closed_daily_df(
                system, symbol_name, fetch_limit)
            if df is None:
                return jsonify({'error': f'{symbol_name} 获取K线数据失败'}), 500

            if len(df) < required_closed:
                return jsonify({'error': f'{symbol_name} K线数据不足：双均线至少需要 '
                                         f'{required_closed} 根已收盘K线，当前仅 {len(df)} 根'}), 400

            fresh, latest_candle_date, minimum_candle_date = (
                system._daily_candle_is_fresh(
                    df, datetime.now().date().isoformat()))
            if not fresh:
                logger.critical(
                    f'{symbol_name} 即时开仓拒绝陈旧日 K: latest={latest_candle_date}, '
                    f'minimum={minimum_candle_date}')
                return jsonify({
                    'error': f'{symbol_name} 最新已收盘日K陈旧，禁止即时开仓',
                    'latest_candle_date': str(latest_candle_date),
                    'minimum_candle_date': str(minimum_candle_date),
                }), 409

            try:
                current_price = system.exchange_api.get_last_price(ccxt_symbol)
            except Exception as e:
                logger.error(f"{symbol_name} 获取实时市价失败，拒绝即时开仓: {e}")
                return jsonify({'error': f'{symbol_name} 无法取得实时市价，禁止即时开仓'}), 503

            signal = system.ma_cross_strategy.check_current_state(df)
            if not signal:
                return jsonify({'error': f'{symbol_name} K线数据不足，无法计算EMA信号'}), 400
            signal_side = signal.get('action')
            if signal_side not in ('long', 'short'):
                return jsonify({'error': f'{symbol_name} 当前EMA无明确方向（短期EMA≈长期EMA）',
                                'info': {'ema_short': float(signal.get('ema_short', 0)),
                                         'ema_long': float(signal.get('ema_long', 0)),
                                         'current_price': current_price}}), 400
            stop_loss_price = signal['lower_stop'] if signal_side == 'long' else signal['upper_stop']
            signal_info = {'ema_short': float(signal.get('ema_short', 0)),
                           'ema_long': float(signal.get('ema_long', 0)),
                           'upper_stop': float(signal.get('upper_stop', 0)),
                           'lower_stop': float(signal.get('lower_stop', 0))}

            symbol_config = {'name': symbol_name, 'enabled': True,
                             'risk_per_trade': risk_per_trade}
            # buffer_notification=False：本路由下方自发专属钉钉，不进日检汇总缓冲
            outcome = system._execute_open(
                symbol_name, signal_side, current_price, stop_loss_price,
                symbol_config, buffer_notification=False)

            new_position = system.trade_state.get_open_position(symbol_name)
            outcome_status = outcome.get('status') if isinstance(outcome, dict) else None

            # rollback_incomplete 也会建立 quarantined 余仓账本。它不是“开仓成功”，
            # 但仍须加入配置托管，不能因 HTTP 失败响应让真钱余仓退出日常管理。
            if new_position:
                with system._config_lock:
                    exists = any(
                        s['name'] == symbol_name
                        for s in system.config['trading']['symbols'])
                    if not exists:
                        backup_symbols = json.loads(json.dumps(
                            system.config['trading']['symbols']))
                        system.config['trading']['symbols'].append(symbol_config)
                        err_resp = _commit_config_or_rollback(
                            system, 'trading', 'symbols', backup_symbols,
                            f'{symbol_name} 已产生交易所余仓，但写入交易对配置失败，'
                            '请立即检查磁盘和配置文件')
                        if err_resp:
                            return err_resp

            if outcome_status != 'opened':
                if new_position:
                    return jsonify({
                        'status': 'quarantined',
                        'error': (f'{symbol_name} 开仓未能完整收口，交易所余仓已建账并'
                                  '隔离托管；这不是开仓成功，请立即人工核对持仓与止损'),
                        'outcome_status': outcome_status or 'unknown',
                        'position': {
                            'symbol': symbol_name,
                            'side': new_position.get('side'),
                            'position_size': new_position.get('position_size'),
                            'stop_order_id': new_position.get('stop_order_id'),
                        },
                    }), 409
                status_code = 409 if outcome_status in {
                    'rolled_back', 'order_unresolved', 'attribution_unresolved'} else 500
                return jsonify({
                    'error': f'{symbol_name} 开仓未成功，请查看日志',
                    'outcome_status': outcome_status or 'unknown',
                }), status_code

            if not new_position:
                return jsonify({
                    'error': f'{symbol_name} 返回 opened 但本地无持仓，状态不一致',
                }), 500

            direction_text = '做多' if signal_side == 'long' else '做空'
            strategy_text = '双均线EMA'
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
                                      'risk_per_trade': risk_per_trade,
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
            if not isinstance(data, dict) or 'name' not in data:
                return jsonify({'error': '缺少交易对名称'}), 400

            try:
                symbol_name = normalize_symbol_name(data['name'])
            except ValueError as e:
                return jsonify({'error': str(e)}), 400
            position = system.trade_state.get_open_position(symbol_name)
            if not position:
                return jsonify({'error': f'{symbol_name} 没有持仓记录'}), 400

            ccxt_symbol = system.exchange_api.to_ccxt_symbol(symbol_name)
            submit_close = getattr(system, '_submit_persisted_close', None)
            if callable(submit_close):
                close_order = submit_close(
                    symbol_name, ccxt_symbol, position, 'API 手动平仓')
            else:
                # 兼容旧测试桩；真实 TradingSystem 必须走持久化 close intent。
                close_order = system.exchange_api.close_position(
                    ccxt_symbol, position['side'], position['position_size'])
            if not close_order:
                return jsonify({'error': f'{symbol_name} 平仓失败'}), 500

            reject_partial = getattr(system, '_reject_partial_close', None)
            if close_order.get('fully_closed') is False:
                handle_partial = getattr(system, '_handle_partial_close', None)
                safely_reconciled = False
                if callable(handle_partial):
                    safely_reconciled = bool(handle_partial(
                        symbol_name, close_order, position, '手动平仓'))
                elif callable(reject_partial):
                    reject_partial(symbol_name, close_order, '手动平仓')
                else:
                    logger.critical(f'{symbol_name} 手动平仓仅部分成交，保留账本和止损')
                # 绝不能继续撤掉保护余仓的止损或删除完整本地账本。
                return jsonify({
                    'status': 'partial',
                    'error': (f'{symbol_name} 仅部分平仓，交易所仍有余仓；'
                              f'{"账本已按余仓缩减且止损已收口" if safely_reconciled else "安全收口失败，请立即人工复核"}'),
                    'closed_amount': close_order.get('amount'),
                    'remaining_amount': close_order.get('remaining_amount'),
                    'safely_reconciled': safely_reconciled,
                }), 409

            warn_ambiguous = getattr(system, '_warn_ambiguous_close_execution', None)
            if callable(warn_ambiguous):
                warn_ambiguous(symbol_name, close_order, '手动平仓')

            # 仓位变化与订单成交量不一致时，订单 VWAP 不能代表完整平仓，使用保守行情回退。
            actual_price = None if close_order.get('execution_ambiguous') \
                else safe_fill_price(close_order, None)
            if not actual_price:
                try:
                    actual_price = system.exchange_api.get_last_price(ccxt_symbol) or position['entry_price']
                except Exception:
                    actual_price = position['entry_price']
            if close_order.get('fee') is not None or close_order.get('fees'):
                logger.info(
                    f"{symbol_name} 手动平仓真实手续费: fee={close_order.get('fee')}, "
                    f"fees={close_order.get('fees')}")

            if not system._cancel_stop_order_confirmed(symbol_name, ccxt_symbol, position.get('stop_order_id')):
                logger.error(f"[{system.label}] {symbol_name} 手动平仓后止损撤销不可确认，已标记残留并阻断该品种新开仓")

            # 记平 + 落盘失败的运行时补偿 + 告警 + 止损异常警示清理，统一复用主系统的收口方法
            extract_fee = getattr(system, '_extract_usdt_fee', None)
            exit_fee, exit_fee_currency = (
                extract_fee(close_order) if callable(extract_fee) else (None, None))
            order_ids_getter = getattr(system, '_order_ids', None)
            exit_order_ids = (
                order_ids_getter(close_order) if callable(order_ids_getter) else [])
            closed_position, state_saved = system._close_trade_state_with_runtime_fallback(
                symbol_name, actual_price, "手动平仓",
                exit_fee=exit_fee, exit_fee_currency=exit_fee_currency,
                exit_order_ids=exit_order_ids,
                close_intent_client_id=close_order.get(
                    'close_intent_client_id'))
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
    global _manual_check_thread
    system, err = _require_system()
    if err:
        return err
    # 要求 JSON body（空 {} 即可）：其余写接口因解析 JSON 天然免疫跨站表单，
    # 本接口原本无参数，补上同等门槛（防 CSRF 触发手动检查）
    if not isinstance(request.get_json(silent=True), dict):
        return jsonify({'error': '请求须携带 JSON body（空对象 {} 即可），'
                                 '例如: curl -X POST -H "Content-Type: application/json" -d "{}"'}), 400
    if not _set_manual_check_running(True):
        return jsonify({'status': 'busy', 'message': '已有手动检查在执行中，请稍后再试'}), 409
    try:
        logger.info(f"[{system.label}] 手动触发交易检查")

        def run_check():
            global _manual_check_thread, _manual_check_running
            try:
                system.check_and_execute_trades(manual_run=True)
                logger.info(f"[{system.label}] 手动触发的交易检查执行完毕")
            except Exception as e:
                logger.error(f"[{system.label}] 手动触发的交易检查失败: {e}")
            finally:
                with _manual_check_guard:
                    _manual_check_running = False
                    _manual_check_thread = None

        thread = threading.Thread(target=run_check, name='manual-trade-check', daemon=True)
        with _manual_check_guard:
            _manual_check_thread = thread
        thread.start()
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
    from runtime_guard import acquire_runner_lock
    acquire_runner_lock()
    _bootstrap()
    start_runner_thread(trading_system)
    logger.info("HTTP API 服务器启动在 127.0.0.1:5000")
    try:
        app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)
    finally:
        stop_runner_thread()
