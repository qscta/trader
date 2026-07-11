import copy
import json
import logging
import math
import os
import stat
import tempfile
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

_file_lock = threading.RLock()
TRADING_FEE_RATE = 0.00045


class TradeStatePersistenceError(RuntimeError):
    """交易状态持久化失败。"""


def _reject_nonfinite_json(value):
    """json.load 默认接受 NaN/Infinity；命脉状态只接受标准 JSON。"""
    raise ValueError(f'不允许的 JSON 数值常量: {value}')


def private_file_exists(filepath):
    """返回敏感文件路径是否存在（包括断链符号链接）。

    对敏感状态不能用 ``exists`` 把断链当成「全新部署」，否则会
    绕过下方的 no-follow 拒绝逻辑并以默认空状态启动。
    """
    return os.path.lexists(filepath)


def open_private_text_file(filepath):
    """安全打开已有敏感 JSON，并把权限收紧为 0600。

    lstat + O_NOFOLLOW + fstat inode 复核同时挡住符号链接与检查/打开
    窗口的替换；所有者不是当前用户时拒绝读取，不对别人的文件
    chmod。返回值是可直接用 ``with`` 管理的文本流。
    """
    path = os.path.abspath(filepath)
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f'敏感状态路径不是普通文件（拒绝符号链接）: {path}')

    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = None
    try:
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'敏感状态不是普通文件: {path}')
        if (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
            raise RuntimeError(f'敏感状态在检查与打开之间被替换: {path}')
        current_uid = os.geteuid() if hasattr(os, 'geteuid') else os.getuid()
        if info.st_uid != current_uid:
            raise PermissionError(f'敏感状态不属于当前用户，拒绝读取: {path}')
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        stream = os.fdopen(fd, 'r', encoding='utf-8')
        fd = None
        return stream
    finally:
        if fd is not None:
            os.close(fd)


def private_file_stat(filepath):
    """以与读取相同的安全规则返回文件 fstat。"""
    with open_private_text_file(filepath) as stream:
        return os.fstat(stream.fileno())


def atomic_write_json(filepath, data):
    """原子写入JSON文件：写临时文件 → fsync → rename。

    fsync 保证 rename 时数据已真正落盘——否则掉电/断电瞬间可能留下空文件或半截文件，
    对 trade_state.json 这类命脉文件不可接受。encoding 显式 utf-8，
    避免 systemd 等 C locale 环境下写中文（如品种备注）时 UnicodeEncodeError。
    """
    dir_name = os.path.dirname(os.path.abspath(filepath))
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
        # 文件 fsync 不包含 rename 目录项；再 fsync 父目录，避免掉电后“内容在、文件名丢”。
        dir_fd = None
        try:
            dir_fd = os.open(dir_name, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            os.fsync(dir_fd)
        except Exception as e:
            # rename 已经提交，绝不能返回 False 让调用方回滚内存、制造磁盘/内存分叉。
            # 目录 fsync 不可用只影响掉电耐久性，运行时真实状态仍是“写入成功”。
            logger.critical(f'目录 fsync 失败，写入已提交但掉电耐久性降级: {dir_name}: {e}')
        finally:
            if dir_fd is not None:
                try:
                    os.close(dir_fd)
                except OSError as e:
                    logger.warning(f'关闭目录 fd 失败（写入已提交）: {dir_name}: {e}')
        return True
    except Exception as e:
        logger.error(f'原子写入失败 {filepath}: {e}')
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def _normalise_optional_fee(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def calculate_closed_trade_metrics(side, entry_price, exit_price, position_size,
                                   fee_rate=TRADING_FEE_RATE,
                                   entry_fee=None, exit_fee=None):
    """返回净盈亏；交易所真实手续费优先，缺失的一侧才按费率估算。"""
    entry_price = float(entry_price or 0)
    exit_price = float(exit_price or 0)
    position_size = float(position_size or 0)

    entry_notional = entry_price * position_size
    exit_notional = exit_price * position_size

    if side == 'long':
        gross_pnl = (exit_price - entry_price) * position_size
    else:
        gross_pnl = (entry_price - exit_price) * position_size

    actual_entry_fee = _normalise_optional_fee(entry_fee)
    actual_exit_fee = _normalise_optional_fee(exit_fee)
    entry_fee = actual_entry_fee if actual_entry_fee is not None else entry_notional * fee_rate
    exit_fee = actual_exit_fee if actual_exit_fee is not None else exit_notional * fee_rate
    total_fee = entry_fee + exit_fee
    net_pnl = gross_pnl - total_fee
    pnl_percent = (net_pnl / entry_notional * 100) if entry_notional > 0 else 0

    return {
        'fee_rate': fee_rate,
        'fee_source': ('actual' if actual_entry_fee is not None and actual_exit_fee is not None
                       else 'mixed' if actual_entry_fee is not None or actual_exit_fee is not None
                       else 'estimated'),
        'entry_notional': entry_notional,
        'exit_notional': exit_notional,
        'gross_pnl': gross_pnl,
        'entry_fee': entry_fee,
        'exit_fee': exit_fee,
        'total_fee': total_fee,
        'pnl': net_pnl,
        'pnl_percent': pnl_percent,
    }


def enrich_closed_trade_with_fees(trade, fee_rate=TRADING_FEE_RATE):
    """补齐旧记录的估算手续费；绝不覆盖已持久化的真实/混合成交口径。"""
    enriched = copy.deepcopy(trade)
    if (enriched.get('fee_source') in ('actual', 'mixed') and
            all(key in enriched for key in (
                'entry_fee', 'exit_fee', 'total_fee', 'gross_pnl', 'pnl', 'pnl_percent'))):
        return enriched
    side = enriched.get('side')
    entry_price = enriched.get('entry_price')
    exit_price = enriched.get('exit_price')
    position_size = enriched.get('position_size')

    if side not in ('long', 'short') or not entry_price or not exit_price or not position_size:
        enriched.setdefault('fee_rate', fee_rate)
        enriched.setdefault('entry_fee', 0)
        enriched.setdefault('exit_fee', 0)
        enriched.setdefault('total_fee', 0)
        enriched.setdefault('gross_pnl', enriched.get('pnl', 0))
        return enriched

    enriched.update(
        calculate_closed_trade_metrics(
            side,
            entry_price,
            exit_price,
            position_size,
            fee_rate=fee_rate,
        )
    )
    return enriched


class TradeState:
    # 账本内保留的最近平仓记录条数：超出部分由 compact_closed_trades 搬进只追加的
    # 史书文件。命脉账本（持仓/止损/信号状态）从此恒定大小，每次落盘不再全量重写
    # 逐年增长的历史；史书损坏只影响历史展示，绝不阻断启动（与账本 fail-closed 相区分）。
    KEEP_RECENT_CLOSED = 200

    def __init__(self, state_file='trade_state.json', keep_recent_closed=None):
        self.state_file = state_file
        self.archive_file = os.path.join(
            os.path.dirname(os.path.abspath(state_file)), 'closed_trades_archive.json')
        self.keep_recent_closed = keep_recent_closed or self.KEEP_RECENT_CLOSED
        self.lock = _file_lock
        self._archive_cache_key = None
        self._archive_cache_records = None
        self.state = self.load_state()

    def load_state(self):
        """加载账本，fail-closed 语义：

        - 主文件与 .bak 都不存在：全新部署，返回默认空状态；
        - 主文件不存在但 .bak 仍在：疑似误删，拒绝启动（人工恢复备份或删 .bak 确认重置）；
        - 主文件可读：正常加载；
        - 主文件损坏、备份可读：从 .bak 恢复；
        - 主文件损坏、备份也不可读：抛 TradeStatePersistenceError 拒绝启动。

        账本无法确认时绝不「失忆」运行：不仅会漏管旧仓，日检还会把有真实仓位的
        品种当空仓重复开仓（单向模式下同向叠加敞口/反向误减仓）。
        """
        backup = self.state_file + '.bak'
        if not private_file_exists(self.state_file):
            if private_file_exists(backup):
                # 不自动恢复：.bak 是上次保存前的副本，可能落后于被删的主文件，
                # 静默复活等于凭空捏造持仓；也不空启动：那是失忆。留给人工显式二选一。
                raise TradeStatePersistenceError(
                    f'主账本 {self.state_file} 不存在，但备份 {backup} 仍在（疑似误删）。'
                    f'拒绝以空状态启动。请人工二选一：'
                    f'1) 恢复账本：cp {backup} {self.state_file} 后重启；'
                    f'2) 确认全新重置：删除 {backup} 后重启'
                )
            return self.get_default_state()
        try:
            with open_private_text_file(self.state_file) as f:
                state = json.load(f, parse_constant=_reject_nonfinite_json)
            self.validate_state(state)
            return state
        except Exception as e:
            logger.error(f'读取交易状态失败({self.state_file}): {e}，尝试从备份恢复')
            try:
                with open_private_text_file(backup) as f:
                    recovered = json.load(f, parse_constant=_reject_nonfinite_json)
                self.validate_state(recovered)
                # 不能只恢复到内存：下一次保存会先把仍损坏的主文件复制到 .bak，
                # 反而摧毁唯一好备份。恢复成功后必须先原子修复主文件。
                if not atomic_write_json(self.state_file, recovered):
                    raise TradeStatePersistenceError(
                        f'备份可读，但无法原子修复主账本 {self.state_file}')
                logger.warning(f'交易状态已从备份恢复并修复主文件: {backup}')
                return recovered
            except Exception as be:
                raise TradeStatePersistenceError(
                    f'交易状态主文件损坏且备份不可恢复（主: {e}；备: {be}）。'
                    f'拒绝以空状态启动，请人工修复 {self.state_file} 或其 .bak 后重启'
                ) from e

    @staticmethod
    def get_default_state():
        return {
            'open_positions': {},
            'closed_trades': [],
            'signal_states': {},
            'open_intents': {},
            'stop_loss_dates': {},
            'stop_loss_dates_migrated': False,
            'position_quarantines': {},
            'last_daily_check_date': None,
            'last_daily_summary_date': None,
        }

    @staticmethod
    def validate_state(state):
        """校验账本最小 schema。

        合法 JSON 不等于合法账本：顶层数组、缺失 open_positions，或把
        open_positions 改成数组，都会让后续以裸 TypeError/KeyError 崩溃，绕过
        fail-closed 恢复路径。扩展字段允许缺省（兼容旧账本），但一旦存在必须类型正确。
        """
        if not isinstance(state, dict):
            raise ValueError(f'账本顶层必须是对象，实际为 {type(state).__name__}')
        required = {
            'open_positions': dict,
            'closed_trades': list,
        }
        optional = {
            'signal_states': dict,
            'open_intents': dict,
            'stop_residues': dict,
            'stop_loss_dates': dict,
            'position_quarantines': dict,
        }
        for key, expected in required.items():
            if key not in state:
                raise ValueError(f'账本缺少必需字段 {key}')
            if not isinstance(state[key], expected):
                raise ValueError(
                    f'账本字段 {key} 必须是 {expected.__name__}，'
                    f'实际为 {type(state[key]).__name__}')
        for key, expected in optional.items():
            if key in state and not isinstance(state[key], expected):
                raise ValueError(
                    f'账本字段 {key} 必须是 {expected.__name__}，'
                    f'实际为 {type(state[key]).__name__}')
        if ('stop_loss_dates_migrated' in state and
                not isinstance(state['stop_loss_dates_migrated'], bool)):
            raise ValueError('账本字段 stop_loss_dates_migrated 必须是 bool')
        if ('exchange' in state and state['exchange'] is not None
                and not isinstance(state['exchange'], str)):
            raise ValueError('账本字段 exchange 必须是字符串或 null')
        last_daily = state.get('last_daily_check_date')
        if last_daily is not None:
            if not isinstance(last_daily, str):
                raise ValueError('last_daily_check_date 必须是 YYYY-MM-DD 或 null')
            try:
                datetime.strptime(last_daily, '%Y-%m-%d')
            except ValueError as exc:
                raise ValueError(f'last_daily_check_date 非法: {last_daily!r}') from exc
        last_summary = state.get('last_daily_summary_date')
        if last_summary is not None:
            if not isinstance(last_summary, str):
                raise ValueError('last_daily_summary_date 必须是 YYYY-MM-DD 或 null')
            try:
                datetime.strptime(last_summary, '%Y-%m-%d')
            except ValueError as exc:
                raise ValueError(
                    f'last_daily_summary_date 非法: {last_summary!r}') from exc

        for symbol, position in state['open_positions'].items():
            if not isinstance(symbol, str) or not symbol:
                raise ValueError('open_positions 的键必须是非空字符串')
            if not isinstance(position, dict):
                raise ValueError(f'{symbol} 持仓必须是对象')
            required_position_fields = (
                'symbol', 'side', 'entry_price', 'position_size', 'stop_loss_price')
            missing = [field for field in required_position_fields if field not in position]
            if missing:
                raise ValueError(f'{symbol} 持仓缺少必需字段: {missing}')
            if position['symbol'] != symbol:
                raise ValueError(f'{symbol} 持仓内 symbol 不一致')
            if position['side'] not in ('long', 'short'):
                raise ValueError(f'{symbol}.side 必须是 long/short')
            for field in ('entry_price', 'position_size', 'stop_loss_price'):
                value = position[field]
                if (isinstance(value, bool) or not isinstance(value, (int, float))):
                    raise ValueError(f'{symbol}.{field} 必须是正有限数')
                if not math.isfinite(value) or value <= 0:
                    raise ValueError(f'{symbol}.{field} 必须是正有限数')
            order_id = position.get('stop_order_id')
            if order_id is not None and not isinstance(order_id, str):
                raise ValueError(f'{symbol}.stop_order_id 必须是字符串或 null')
            if position.get('strategy') not in (None, 'turtle', 'ma_cross'):
                raise ValueError(f'{symbol}.strategy 必须是 turtle/ma_cross/null')
            for field in ('original_position_size', 'stop_order_size'):
                if field in position:
                    if isinstance(position[field], bool):
                        raise ValueError(f'{symbol}.{field} 必须是正有限数')
                    try:
                        number = float(position[field])
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f'{symbol}.{field} 必须是正有限数') from exc
                    if not math.isfinite(number) or number <= 0:
                        raise ValueError(f'{symbol}.{field} 必须是正有限数')
            if ('stop_resize_pending' in position and
                    not isinstance(position['stop_resize_pending'], bool)):
                raise ValueError(f'{symbol}.stop_resize_pending 必须是 bool')
            for field in ('entry_order_ids', 'extra_stop_order_ids'):
                values = position.get(field)
                if values is not None and (not isinstance(values, list) or
                                           any(not isinstance(value, str) for value in values)):
                    raise ValueError(f'{symbol}.{field} 必须是字符串数组')
            close_intent = position.get('close_intent')
            if close_intent is not None:
                if not isinstance(close_intent, dict):
                    raise ValueError(f'{symbol}.close_intent 必须是对象')
                for field in ('client_order_id', 'side', 'status', 'context',
                              'created_at', 'updated_at'):
                    if (not isinstance(close_intent.get(field), str) or
                            not close_intent[field]):
                        raise ValueError(
                            f'{symbol}.close_intent.{field} 必须是非空字符串')
                close_client_id = close_intent['client_order_id']
                if not (1 <= len(close_client_id) <= 32 and
                        close_client_id.isascii() and
                        close_client_id.isalnum()):
                    raise ValueError(
                        f'{symbol}.close_intent.client_order_id 必须是 '
                        '1-32 位 ASCII 字母数字')
                if close_intent['side'] != position['side']:
                    raise ValueError(f'{symbol}.close_intent.side 与持仓方向不一致')
                if close_intent['status'] != 'pending':
                    raise ValueError(f'{symbol}.close_intent.status 必须是 pending')
                if isinstance(close_intent.get('planned_position_size'), bool):
                    raise ValueError(
                        f'{symbol}.close_intent.planned_position_size 非法')
                try:
                    planned_close = float(close_intent['planned_position_size'])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        f'{symbol}.close_intent.planned_position_size 非法') from exc
                if (not math.isfinite(planned_close) or planned_close <= 0 or
                        not math.isclose(
                            planned_close, float(position['position_size']),
                            rel_tol=1e-12,
                            abs_tol=max(
                                1e-15,
                                math.ulp(float(position['position_size'])) * 8))):
                    raise ValueError(
                        f'{symbol}.close_intent 计划量必须与当前账本仓位一致')
                for field in ('created_at', 'updated_at'):
                    try:
                        datetime.fromisoformat(close_intent[field])
                    except ValueError as exc:
                        raise ValueError(
                            f'{symbol}.close_intent.{field} 非法') from exc
            partials = (
                position['partial_closes']
                if 'partial_closes' in position else [])
            if not isinstance(partials, list) or any(
                    not isinstance(item, dict) for item in partials):
                raise ValueError(f'{symbol}.partial_closes 必须是对象数组')
            for item in partials:
                for field in ('position_size', 'exit_price', 'exit_notional',
                              'gross_pnl', 'exit_fee'):
                    if field not in item:
                        raise ValueError(f'{symbol}.partial_closes 缺少 {field}')
                    if isinstance(item[field], bool):
                        raise ValueError(f'{symbol}.partial_closes.{field} 不能是 bool')
                    try:
                        number = float(item[field])
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f'{symbol}.partial_closes.{field} 必须是有限数') from exc
                    if not math.isfinite(number) or (
                            field in ('position_size', 'exit_price', 'exit_notional') and number <= 0
                    ) or (field == 'exit_fee' and number < 0):
                        raise ValueError(f'{symbol}.partial_closes.{field} 非法')
        if any(not isinstance(trade, dict) for trade in state['closed_trades']):
            raise ValueError('closed_trades 的每一项都必须是对象')
        for symbol, record in (state.get('signal_states') or {}).items():
            if not isinstance(symbol, str) or not isinstance(record, dict):
                raise ValueError('signal_states 必须是 品种→对象')
            if ('mid_line_crossed' in record
                    and not isinstance(record['mid_line_crossed'], bool)):
                raise ValueError(f'{symbol}.mid_line_crossed 必须是 bool')
            execution = record.get('signal_execution')
            if execution is not None:
                if not isinstance(execution, dict):
                    raise ValueError(f'{symbol}.signal_execution 必须是对象')
                for field in ('strategy', 'signal_id', 'client_order_id', 'status'):
                    if not isinstance(execution.get(field), str) or not execution[field]:
                        raise ValueError(
                            f'{symbol}.signal_execution.{field} 必须是非空字符串')
                if execution['strategy'] not in ('turtle', 'ma_cross'):
                    raise ValueError(
                        f'{symbol}.signal_execution.strategy 必须是 turtle/ma_cross')
                if execution['status'] not in ('pending', 'confirmed'):
                    raise ValueError(
                        f'{symbol}.signal_execution.status 必须是 pending/confirmed')
                payload = execution.get('payload')
                if payload is not None and not isinstance(payload, dict):
                    raise ValueError(f'{symbol}.signal_execution.payload 必须是对象或 null')
                if 'planned_position_size' in execution:
                    if isinstance(execution['planned_position_size'], bool):
                        raise ValueError(
                            f'{symbol}.signal_execution.planned_position_size 必须是正有限数')
                    try:
                        planned = float(execution['planned_position_size'])
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f'{symbol}.signal_execution.planned_position_size 必须是正有限数') from exc
                    if not math.isfinite(planned) or planned <= 0:
                        raise ValueError(
                            f'{symbol}.signal_execution.planned_position_size 必须是正有限数')
        for symbol, intent in (state.get('open_intents') or {}).items():
            if not isinstance(symbol, str) or not isinstance(intent, dict):
                raise ValueError('open_intents 必须是 品种→对象')
            for field in ('strategy', 'side', 'client_order_id', 'status',
                          'created_at', 'updated_at'):
                if not isinstance(intent.get(field), str) or not intent[field]:
                    raise ValueError(f'{symbol}.open_intent.{field} 必须是非空字符串')
            if intent['strategy'] not in ('turtle', 'ma_cross'):
                raise ValueError(f'{symbol}.open_intent.strategy 非法')
            if intent['side'] not in ('long', 'short'):
                raise ValueError(f'{symbol}.open_intent.side 非法')
            if intent['status'] != 'pending':
                raise ValueError(f'{symbol}.open_intent.status 必须是 pending')
            for field in ('created_at', 'updated_at'):
                try:
                    datetime.fromisoformat(intent[field])
                except ValueError as exc:
                    raise ValueError(
                        f'{symbol}.open_intent.{field} 非法') from exc
            payload = intent.get('payload')
            if not isinstance(payload, dict):
                raise ValueError(f'{symbol}.open_intent.payload 必须是对象')
            if 'planned_position_size' in intent:
                if isinstance(intent['planned_position_size'], bool):
                    raise ValueError(
                        f'{symbol}.open_intent.planned_position_size 非法')
                try:
                    planned = float(intent['planned_position_size'])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f'{symbol}.open_intent.planned_position_size 非法') from exc
                if not math.isfinite(planned) or planned <= 0:
                    raise ValueError(
                        f'{symbol}.open_intent.planned_position_size 非法')
        for symbol, day in (state.get('stop_loss_dates') or {}).items():
            if not isinstance(symbol, str) or not isinstance(day, str):
                raise ValueError('stop_loss_dates 必须是 品种→日期字符串')
            try:
                datetime.strptime(day, '%Y-%m-%d')
            except ValueError as exc:
                raise ValueError(f'{symbol} T+1 日期非法: {day!r}') from exc
        for symbol, marked_at in (state.get('stop_residues') or {}).items():
            if not isinstance(symbol, str) or not isinstance(marked_at, str):
                raise ValueError('stop_residues 必须是 品种→时间字符串')
        for symbol, quarantine in (state.get('position_quarantines') or {}).items():
            if not isinstance(symbol, str) or not isinstance(quarantine, dict):
                raise ValueError('position_quarantines 必须是 品种→对象')
        return True

    def _snapshot_locked(self):
        return copy.deepcopy(self.state)

    def _ensure_signal_states_locked(self):
        if 'signal_states' not in self.state:
            self.state['signal_states'] = {}

    def _set_signal_state_locked(self, symbol, mid_line_crossed):
        self._ensure_signal_states_locked()
        # 保留 last_processed_candle / last_consumed_signal_id：它们是防止停损后
        # 重放同一根突破 K 线的持久化幂等键，不能被单纯布尔更新覆盖。
        record = self.state['signal_states'].setdefault(symbol, {})
        record.update({
            'mid_line_crossed': bool(mid_line_crossed),
            'last_update': datetime.now().isoformat()
        })

    def save_state(self):
        with self.lock:
            snapshot = self._snapshot_locked()
            try:
                # 写端与启动读端共用同一 schema；任何内部入口若构造出坏状态，
                # 当前事务立即回滚，不能“本次保存成功、下次重启才拒绝”。
                self.validate_state(snapshot)
            except Exception as exc:
                raise TradeStatePersistenceError(
                    f'拒绝保存 schema 非法的交易状态: {exc}') from exc
            if private_file_exists(self.state_file):
                # 备份也必须是已解析、schema 合法、原子落盘的上一版本。静默忽略 copy
                # 失败会让“有 .bak 即可恢复”的承诺失效，且 copy2 中断可留下半截备份。
                try:
                    with open_private_text_file(self.state_file) as f:
                        previous = json.load(f, parse_constant=_reject_nonfinite_json)
                    self.validate_state(previous)
                except Exception as e:
                    raise TradeStatePersistenceError(
                        f'当前主账本无法验证，拒绝覆盖并保存: {self.state_file}: {e}') from e
                if not atomic_write_json(self.state_file + '.bak', previous):
                    raise TradeStatePersistenceError(
                        f'保存备份失败，主账本未提交: {self.state_file}.bak')
            if not atomic_write_json(self.state_file, snapshot):
                raise TradeStatePersistenceError(f'保存状态失败: {self.state_file}')
            return True

    def _save_or_rollback_locked(self, snapshot):
        """落盘失败时把内存回滚到修改前再抛出（事务语义）。

        否则内存会留下与磁盘、交易所都不一致的状态（如开仓保存失败后的「假仓」：
        交易所侧已回滚平仓，内存却还有 position——前端显示假持仓，巡检还会把它
        当「交易所无仓」再记一笔假平仓）。需要「交易所动作已发生、内存必须强制
        反映现实」的场景，由调用方在捕获异常后使用 force_runtime_* 系列方法。
        """
        try:
            self.save_state()
        except TradeStatePersistenceError:
            self.state = snapshot
            raise

    def add_open_position(self, symbol, side, entry_price, position_size,
                          stop_loss_price, stop_order_id=None, strategy=None,
                          entry_fee=None, entry_fee_currency=None,
                          entry_order_ids=None,
                          open_intent_client_id=None):
        with self.lock:
            snapshot = self._snapshot_locked()
            if open_intent_client_id is not None:
                intent = (self.state.get('open_intents') or {}).get(symbol) or {}
                if (intent.get('status') != 'pending' or
                        intent.get('client_order_id') != str(open_intent_client_id)):
                    raise TradeStatePersistenceError(
                        f'{symbol} 开仓落账与 pending open intent 不匹配')
            position = {
                'symbol': symbol,
                'side': side,
                'entry_price': entry_price,
                'position_size': position_size,
                'original_position_size': position_size,
                'stop_loss_price': stop_loss_price,
                'stop_order_id': stop_order_id,
                'stop_order_size': position_size,
                'strategy': strategy,
                'open_time': datetime.now().isoformat()
            }
            actual_entry_fee = _normalise_optional_fee(entry_fee)
            if actual_entry_fee is not None:
                position['entry_fee'] = actual_entry_fee
                position['entry_fee_currency'] = entry_fee_currency
                position['entry_fee_source'] = 'exchange'
            if entry_order_ids:
                position['entry_order_ids'] = [str(value) for value in entry_order_ids if value]
            if open_intent_client_id is not None:
                position['client_order_id'] = str(open_intent_client_id)
            self.state['open_positions'][symbol] = position
            # T+1 只描述“当前仍空仓、等待重入”。真实持仓一旦与账本同事务建立，
            # 陈旧标记必须同时消失，否则人工平仓后会被误判为待重入再开一笔。
            self.state.setdefault('stop_loss_dates', {}).pop(symbol, None)
            self._set_signal_state_locked(symbol, False)
            if open_intent_client_id is not None:
                del self.state['open_intents'][symbol]
            self._save_or_rollback_locked(snapshot)
            return copy.deepcopy(position)

    def get_open_position(self, symbol):
        with self.lock:
            position = self.state['open_positions'].get(symbol)
            return copy.deepcopy(position) if position is not None else None

    def prepare_close_intent(self, symbol, client_order_id, context):
        """在主动平仓 POST 前原子固化幂等句柄和原始仓位。

        同一持仓已有 pending 时返回旧句柄，HTTP 重试、日检重跑和重启恢复
        都只能继续原交易，不能生成另一张无法归因的平仓单。
        """
        if (not isinstance(client_order_id, str) or
                not (1 <= len(client_order_id) <= 32) or
                not client_order_id.isascii() or
                not client_order_id.isalnum()):
            raise ValueError(
                'close intent client_order_id 必须是 1-32 位 ASCII 字母数字')
        if not isinstance(context, str) or not context:
            raise ValueError('close intent context 不能为空')
        with self.lock:
            position = self.state['open_positions'].get(symbol)
            if position is None:
                raise TradeStatePersistenceError(
                    f'{symbol} 无本地持仓，拒绝建立 close intent')
            existing = position.get('close_intent')
            if existing is not None:
                if not isinstance(existing, dict) or existing.get('status') != 'pending':
                    raise TradeStatePersistenceError(
                        f'{symbol} 现有 close intent 结构非法')
                return copy.deepcopy(existing)
            snapshot = self._snapshot_locked()
            now = datetime.now().isoformat()
            intent = {
                'client_order_id': client_order_id,
                'side': position['side'],
                'planned_position_size': float(position['position_size']),
                'status': 'pending',
                'context': context,
                'created_at': now,
                'updated_at': now,
            }
            position['close_intent'] = intent
            self._save_or_rollback_locked(snapshot)
            return copy.deepcopy(intent)

    def get_close_intent(self, symbol):
        with self.lock:
            position = self.state['open_positions'].get(symbol) or {}
            intent = position.get('close_intent')
            return copy.deepcopy(intent) if intent is not None else None

    @staticmethod
    def _consume_close_intent_locked(position, client_order_id):
        intent = position.get('close_intent')
        if client_order_id is None:
            if intent is not None:
                raise TradeStatePersistenceError(
                    '持仓存在 pending close intent，拒绝用无句柄结果修改账本')
            return None
        if (not isinstance(intent, dict) or intent.get('status') != 'pending' or
                intent.get('client_order_id') != str(client_order_id)):
            raise TradeStatePersistenceError(
                '平仓结果与 pending close intent 不匹配')
        consumed = position.pop('close_intent')
        position['last_close_client_order_id'] = str(client_order_id)
        return consumed

    def update_stop_loss(self, symbol, new_stop_price, new_stop_order_id,
                         stop_order_size=None, extra_stop_order_ids=None,
                         stop_resize_pending=False):
        with self.lock:
            if symbol not in self.state['open_positions']:
                return None
            snapshot = self._snapshot_locked()
            position = self.state['open_positions'][symbol]
            position['stop_loss_price'] = new_stop_price
            position['stop_order_id'] = new_stop_order_id
            position['stop_order_size'] = (
                position['position_size'] if stop_order_size is None else stop_order_size)
            position['extra_stop_order_ids'] = [
                str(value) for value in (extra_stop_order_ids or []) if value]
            position['stop_resize_pending'] = bool(stop_resize_pending)
            position['last_stop_update'] = datetime.now().isoformat()
            self._save_or_rollback_locked(snapshot)
            return copy.deepcopy(position)

    def force_runtime_update_stop_loss(self, symbol, new_stop_price, new_stop_order_id,
                                       stop_order_size=None,
                                       extra_stop_order_ids=None,
                                       stop_resize_pending=False):
        with self.lock:
            if symbol not in self.state['open_positions']:
                return None
            position = self.state['open_positions'][symbol]
            position['stop_loss_price'] = new_stop_price
            position['stop_order_id'] = new_stop_order_id
            position['stop_order_size'] = (
                position['position_size'] if stop_order_size is None else stop_order_size)
            position['extra_stop_order_ids'] = [
                str(value) for value in (extra_stop_order_ids or []) if value]
            position['stop_resize_pending'] = bool(stop_resize_pending)
            position['last_stop_update'] = datetime.now().isoformat()
            return copy.deepcopy(position)

    @staticmethod
    def _gross_pnl(side, entry_price, exit_price, size):
        return ((exit_price - entry_price) if side == 'long'
                else (entry_price - exit_price)) * size

    def _apply_partial_close_locked(self, symbol, closed_size, exit_price,
                                    exit_fee=None, exit_fee_currency=None,
                                    exit_order_ids=None, new_stop_order_id=None,
                                    remaining_size=None,
                                    stop_order_size=None,
                                    extra_stop_order_ids=None,
                                    stop_resize_pending=False,
                                    close_intent_client_id=None):
        if symbol not in self.state['open_positions']:
            return None
        position = self.state['open_positions'][symbol]
        current_size = float(position['position_size'])
        closed_size = float(closed_size)
        exit_price = float(exit_price)
        if (not math.isfinite(closed_size) or closed_size <= 0 or
                not math.isfinite(exit_price) or exit_price <= 0):
            raise ValueError('部分平仓数量和成交价必须是正有限数')
        tolerance = max(1e-15, math.ulp(current_size) * 8)
        if closed_size >= current_size - tolerance:
            raise ValueError('部分平仓数量已覆盖全部仓位，应走 close_position')

        if remaining_size is None:
            remaining = current_size - closed_size
        else:
            remaining = float(remaining_size)
            if (not math.isfinite(remaining) or remaining <= 0 or
                    remaining >= current_size):
                raise ValueError('交易所余仓数量必须介于 0 与当前仓位之间')
            if not math.isclose(
                    closed_size + remaining, current_size,
                    rel_tol=1e-12, abs_tol=tolerance):
                raise ValueError(
                    f'部分成交不守恒: current={current_size}, '
                    f'closed={closed_size}, remaining={remaining}')
        exit_notional = exit_price * closed_size
        actual_fee = _normalise_optional_fee(exit_fee)
        fee_value = actual_fee if actual_fee is not None else exit_notional * TRADING_FEE_RATE
        partial = {
            'position_size': closed_size,
            'exit_price': exit_price,
            'exit_notional': exit_notional,
            'gross_pnl': self._gross_pnl(
                position['side'], float(position['entry_price']), exit_price, closed_size),
            'exit_fee': fee_value,
            'fee_source': 'exchange' if actual_fee is not None else 'estimated',
            'close_time': datetime.now().isoformat(),
        }
        if exit_fee_currency is not None:
            partial['exit_fee_currency'] = exit_fee_currency
        if exit_order_ids:
            partial['exit_order_ids'] = [str(value) for value in exit_order_ids if value]

        position.setdefault('partial_closes', []).append(partial)
        position['position_size'] = remaining
        position['stop_order_id'] = new_stop_order_id
        position['stop_order_size'] = (
            remaining if stop_order_size is None else float(stop_order_size))
        position['extra_stop_order_ids'] = [
            str(value) for value in (extra_stop_order_ids or []) if value]
        position['stop_resize_pending'] = bool(stop_resize_pending)
        position['last_partial_close'] = partial['close_time']
        self._consume_close_intent_locked(
            position, close_intent_client_id)
        return copy.deepcopy(position)

    def apply_partial_close(self, symbol, closed_size, exit_price, exit_fee=None,
                            exit_fee_currency=None, exit_order_ids=None,
                            new_stop_order_id=None, remaining_size=None,
                            stop_order_size=None,
                            extra_stop_order_ids=None,
                            stop_resize_pending=False,
                            close_intent_client_id=None):
        """原子缩减余仓并累计分段成交；不会把部分成交伪装成完整平仓。"""
        with self.lock:
            snapshot = self._snapshot_locked()
            position = self._apply_partial_close_locked(
                symbol, closed_size, exit_price, exit_fee, exit_fee_currency,
                exit_order_ids, new_stop_order_id, remaining_size, stop_order_size,
                extra_stop_order_ids, stop_resize_pending,
                close_intent_client_id)
            if position is None:
                return None
            self._save_or_rollback_locked(snapshot)
            return position

    def force_runtime_apply_partial_close(self, symbol, closed_size, exit_price,
                                          exit_fee=None, exit_fee_currency=None,
                                          exit_order_ids=None,
                                          new_stop_order_id=None,
                                          remaining_size=None,
                                          stop_order_size=None,
                                          extra_stop_order_ids=None,
                                          stop_resize_pending=False,
                                          close_intent_client_id=None):
        with self.lock:
            return self._apply_partial_close_locked(
                symbol, closed_size, exit_price, exit_fee, exit_fee_currency,
                exit_order_ids, new_stop_order_id, remaining_size, stop_order_size,
                extra_stop_order_ids, stop_resize_pending,
                close_intent_client_id)

    def _add_open_after_partial_rollback_locked(
            self, symbol, side, entry_price, original_size, remaining_size,
            stop_loss_price, partial_exit_price, stop_order_id=None,
            stop_order_size=None, strategy=None,
            entry_fee=None, entry_fee_currency=None, entry_order_ids=None,
            exit_fee=None, exit_fee_currency=None, exit_order_ids=None,
            extra_stop_order_ids=None, stop_resize_pending=False,
            quarantine_reason=None, quarantine_details=None,
            stop_residue_possible=False, open_intent_client_id=None):
        if symbol in self.state['open_positions']:
            raise ValueError(f'{symbol} 已有本地持仓，拒绝覆盖')
        if open_intent_client_id is not None:
            intent = (self.state.get('open_intents') or {}).get(symbol) or {}
            if (intent.get('status') != 'pending' or
                    intent.get('client_order_id') != str(open_intent_client_id)):
                raise TradeStatePersistenceError(
                    f'{symbol} 部分回滚余仓与 pending open intent 不匹配')
        if side not in ('long', 'short'):
            raise ValueError('side 必须是 long/short')
        try:
            entry_price = float(entry_price)
            original_size = float(original_size)
            remaining_size = float(remaining_size)
            stop_loss_price = float(stop_loss_price)
            partial_exit_price = float(partial_exit_price)
            closed_size = float(
                Decimal(str(original_size)) - Decimal(str(remaining_size)))
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise ValueError('部分回滚恢复的价格/数量非法') from exc
        if any(not math.isfinite(value) or value <= 0 for value in (
                entry_price, original_size, remaining_size,
                stop_loss_price, partial_exit_price, closed_size)):
            raise ValueError('部分回滚恢复的价格/数量必须是正有限数')
        if remaining_size >= original_size:
            raise ValueError('部分回滚余仓必须小于原始仓位')

        now = datetime.now().isoformat()
        position = {
            'symbol': symbol,
            'side': side,
            'entry_price': entry_price,
            'position_size': original_size,
            'original_position_size': original_size,
            'stop_loss_price': stop_loss_price,
            'stop_order_id': stop_order_id,
            'stop_order_size': (
                remaining_size if stop_order_size is None else float(stop_order_size)),
            'strategy': strategy,
            'open_time': now,
            'recovered_partial_rollback': True,
        }
        actual_entry_fee = _normalise_optional_fee(entry_fee)
        if actual_entry_fee is not None:
            position['entry_fee'] = actual_entry_fee
            position['entry_fee_currency'] = entry_fee_currency
            position['entry_fee_source'] = 'exchange'
        if entry_order_ids:
            position['entry_order_ids'] = [
                str(value) for value in entry_order_ids if value]
        if open_intent_client_id is not None:
            position['client_order_id'] = str(open_intent_client_id)
        self.state['open_positions'][symbol] = position
        self.state.setdefault('stop_loss_dates', {}).pop(symbol, None)
        self._set_signal_state_locked(symbol, False)
        updated = self._apply_partial_close_locked(
            symbol, closed_size, partial_exit_price,
            exit_fee, exit_fee_currency, exit_order_ids,
            stop_order_id, remaining_size, stop_order_size,
            extra_stop_order_ids, stop_resize_pending)
        if updated is None:
            raise ValueError(f'{symbol} 无法建立部分回滚余仓账本')
        if quarantine_reason:
            quarantines = self.state.setdefault('position_quarantines', {})
            previous = quarantines.get(symbol) or {}
            quarantines[symbol] = {
                'reason': str(quarantine_reason),
                'details': copy.deepcopy(quarantine_details)
                if quarantine_details is not None else None,
                'first_seen': previous.get('first_seen') or now,
                'last_seen': now,
            }
        if stop_residue_possible:
            # 止损创建 ACK/确认不确定时，可能还有一张无法得知 ID 的旧算法单。
            # 与余仓账本同一事务标记，未来撤保护必须 cancel_all 验净。
            self.state.setdefault('stop_residues', {})[symbol] = now
        if open_intent_client_id is not None:
            del self.state['open_intents'][symbol]
        return updated

    def add_open_after_partial_rollback(self, *args, **kwargs):
        """把未曾落盘的开仓及其部分补偿平仓一次性写成受保护余仓。"""
        with self.lock:
            snapshot = self._snapshot_locked()
            try:
                position = self._add_open_after_partial_rollback_locked(
                    *args, **kwargs)
                self._save_or_rollback_locked(snapshot)
                return position
            except Exception:
                self.state = snapshot
                raise

    def force_runtime_add_open_after_partial_rollback(self, *args, **kwargs):
        """磁盘失效时仍让本进程内账本反映交易所余仓；重启前必须人工修复磁盘。"""
        with self.lock:
            return self._add_open_after_partial_rollback_locked(*args, **kwargs)

    def _add_untracked_open_position_locked(
            self, symbol, side, entry_price, position_size, stop_loss_price,
            stop_order_id=None, stop_order_size=None, strategy=None,
            entry_fee=None, entry_fee_currency=None, entry_order_ids=None,
            stop_resize_pending=False, quarantine_reason=None,
            quarantine_details=None, stop_residue_possible=False,
            open_intent_client_id=None):
        """建立“补偿零成交/不可确认”后的完整余仓；调用方必须同时隔离。"""
        if symbol in self.state['open_positions']:
            raise ValueError(f'{symbol} 已有本地持仓，拒绝覆盖')
        if open_intent_client_id is not None:
            intent = (self.state.get('open_intents') or {}).get(symbol) or {}
            if (intent.get('status') != 'pending' or
                    intent.get('client_order_id') != str(open_intent_client_id)):
                raise TradeStatePersistenceError(
                    f'{symbol} 完整余仓与 pending open intent 不匹配')
        if side not in ('long', 'short'):
            raise ValueError('side 必须是 long/short')
        try:
            entry_price = float(entry_price)
            position_size = float(position_size)
            stop_loss_price = float(stop_loss_price)
            stop_order_size = (
                position_size if stop_order_size is None else float(stop_order_size))
        except (TypeError, ValueError) as exc:
            raise ValueError('未决开仓的价格/数量非法') from exc
        if any(not math.isfinite(value) or value <= 0 for value in (
                entry_price, position_size, stop_loss_price, stop_order_size)):
            raise ValueError('未决开仓的价格/数量必须是正有限数')
        now = datetime.now().isoformat()
        position = {
            'symbol': symbol, 'side': side, 'entry_price': entry_price,
            'position_size': position_size,
            'original_position_size': position_size,
            'stop_loss_price': stop_loss_price,
            'stop_order_id': stop_order_id,
            'stop_order_size': stop_order_size,
            'stop_resize_pending': bool(stop_resize_pending),
            'strategy': strategy, 'open_time': now,
            'recovered_unresolved_open': True,
        }
        actual_entry_fee = _normalise_optional_fee(entry_fee)
        if actual_entry_fee is not None:
            position['entry_fee'] = actual_entry_fee
            position['entry_fee_currency'] = entry_fee_currency
            position['entry_fee_source'] = 'exchange'
        if entry_order_ids:
            position['entry_order_ids'] = [
                str(value) for value in entry_order_ids if value]
        if open_intent_client_id is not None:
            position['client_order_id'] = str(open_intent_client_id)
        self.state['open_positions'][symbol] = position
        self.state.setdefault('stop_loss_dates', {}).pop(symbol, None)
        self._set_signal_state_locked(symbol, False)
        quarantines = self.state.setdefault('position_quarantines', {})
        previous = quarantines.get(symbol) or {}
        quarantines[symbol] = {
            'reason': str(quarantine_reason or '未决开仓余仓等待复核'),
            'details': copy.deepcopy(quarantine_details)
            if quarantine_details is not None else None,
            'first_seen': previous.get('first_seen') or now,
            'last_seen': now,
        }
        if stop_residue_possible:
            self.state.setdefault('stop_residues', {})[symbol] = now
        if open_intent_client_id is not None:
            del self.state['open_intents'][symbol]
        return copy.deepcopy(position)

    def add_untracked_open_position(self, *args, **kwargs):
        """原子建立未决完整余仓、隔离与未知止损残留标记。"""
        with self.lock:
            snapshot = self._snapshot_locked()
            try:
                position = self._add_untracked_open_position_locked(*args, **kwargs)
                self._save_or_rollback_locked(snapshot)
                return position
            except Exception:
                self.state = snapshot
                raise

    def force_runtime_add_untracked_open_position(self, *args, **kwargs):
        with self.lock:
            return self._add_untracked_open_position_locked(*args, **kwargs)

    def _close_position_locked(
            self, symbol, exit_price, exit_fee=None,
            exit_fee_currency=None, exit_order_ids=None,
            stop_loss_date=None, stop_cleanup_pending=False,
            reset_turtle_signal=False, close_intent_client_id=None):
        if symbol not in self.state['open_positions']:
            return None
        if stop_loss_date is not None:
            if not isinstance(stop_loss_date, str):
                raise ValueError('stop_loss_date 必须是 YYYY-MM-DD 字符串')
            datetime.strptime(stop_loss_date, '%Y-%m-%d')

        position = self.state['open_positions'][symbol]
        self._consume_close_intent_locked(
            position, close_intent_client_id)
        if not exit_price or exit_price <= 0:
            exit_price = position['entry_price']

        final_size = float(position['position_size'])
        partials = list(position.get('partial_closes') or [])
        original_size = final_size + sum(float(item['position_size']) for item in partials)
        final_exit_notional = float(exit_price) * final_size
        total_exit_notional = final_exit_notional + sum(
            float(item.get('exit_notional') or
                  float(item['exit_price']) * float(item['position_size']))
            for item in partials)
        gross_pnl = self._gross_pnl(
            position['side'], float(position['entry_price']), float(exit_price), final_size)
        gross_pnl += sum(float(item.get('gross_pnl') or 0) for item in partials)

        entry_notional = float(position['entry_price']) * original_size
        actual_entry_fee = (_normalise_optional_fee(position.get('entry_fee'))
                            if position.get('entry_fee_source') == 'exchange' else None)
        entry_fee_value = (actual_entry_fee if actual_entry_fee is not None
                           else entry_notional * TRADING_FEE_RATE)
        actual_final_exit_fee = _normalise_optional_fee(exit_fee)
        final_exit_fee = (actual_final_exit_fee if actual_final_exit_fee is not None
                          else final_exit_notional * TRADING_FEE_RATE)
        exit_fee_value = final_exit_fee + sum(float(item.get('exit_fee') or 0) for item in partials)
        total_fee = entry_fee_value + exit_fee_value

        all_exit_actual = (actual_final_exit_fee is not None and
                           all(item.get('fee_source') == 'exchange' for item in partials))
        any_actual = (actual_entry_fee is not None or actual_final_exit_fee is not None or
                      any(item.get('fee_source') == 'exchange' for item in partials))

        position['final_exit_price'] = exit_price
        position['exit_price'] = total_exit_notional / original_size
        position['close_time'] = datetime.now().isoformat()
        position['position_size'] = original_size
        position['entry_notional'] = entry_notional
        position['exit_notional'] = total_exit_notional
        position['gross_pnl'] = gross_pnl
        position['entry_fee'] = entry_fee_value
        position['exit_fee'] = exit_fee_value
        position['total_fee'] = total_fee
        position['fee_rate'] = TRADING_FEE_RATE
        position['fee_source'] = ('actual' if actual_entry_fee is not None and all_exit_actual
                                  else 'mixed' if any_actual else 'estimated')
        position['pnl'] = gross_pnl - total_fee
        position['pnl_percent'] = (
            position['pnl'] / entry_notional * 100 if entry_notional > 0 else 0)
        if exit_fee_currency is not None:
            position['exit_fee_currency'] = exit_fee_currency
        combined_order_ids = []
        for item in partials:
            combined_order_ids.extend(item.get('exit_order_ids') or [])
        combined_order_ids.extend(str(value) for value in (exit_order_ids or []) if value)
        if combined_order_ids:
            position['exit_order_ids'] = combined_order_ids

        self.state['closed_trades'].append(position)
        del self.state['open_positions'][symbol]
        if stop_loss_date is not None:
            # 与删仓/记成交同一事务，消除 MA 止损后崩溃导致 T+1 丢失的窗口。
            self.state.setdefault('stop_loss_dates', {})[symbol] = stop_loss_date
            self.state['stop_loss_dates_migrated'] = True
        if stop_cleanup_pending:
            # 交易所已空仓时，旧 reduce-only 条件单仍可能存在。先与记平同一
            # 事务持久化清理标记；验证式撤净后再单独清除。进程在两步间崩溃
            # 也只会保持 fail-closed，不会让未知旧止损错杀未来新仓。
            # 若此前已有未知 POST 残留，保留其原始时间，不得因记平
            # 再把可见性等待窗从零计时；普通平仓则在此原子创建 marker。
            self.state.setdefault('stop_residues', {}).setdefault(
                symbol, datetime.now().isoformat())
        if reset_turtle_signal:
            self._set_signal_state_locked(symbol, False)
        return copy.deepcopy(position)

    def close_position(self, symbol, exit_price, exit_fee=None,
                       exit_fee_currency=None, exit_order_ids=None,
                       stop_loss_date=None, stop_cleanup_pending=False,
                       reset_turtle_signal=False,
                       close_intent_client_id=None):
        with self.lock:
            snapshot = self._snapshot_locked()
            position = self._close_position_locked(
                symbol, exit_price, exit_fee, exit_fee_currency, exit_order_ids,
                stop_loss_date, stop_cleanup_pending, reset_turtle_signal,
                close_intent_client_id)
            if position is None:
                return None
            self._save_or_rollback_locked(snapshot)
            return position

    def force_runtime_close_position(self, symbol, exit_price, exit_fee=None,
                                     exit_fee_currency=None, exit_order_ids=None,
                                     stop_loss_date=None,
                                     stop_cleanup_pending=False,
                                     reset_turtle_signal=False,
                                     close_intent_client_id=None):
        with self.lock:
            return self._close_position_locked(
                symbol, exit_price, exit_fee, exit_fee_currency, exit_order_ids,
                stop_loss_date, stop_cleanup_pending, reset_turtle_signal,
                close_intent_client_id)

    def get_all_open_positions(self):
        with self.lock:
            return self._snapshot_locked()['open_positions']

    def _read_archive(self, copy_records=True):
        """读平仓历史史书。返回 (records, ok)：文件不存在视为空史书（ok=True）；
        损坏时 ok=False——调用方 fail-safe（展示只出近期、归档跳过本轮），绝不静默清空。"""
        with self.lock:
            if not private_file_exists(self.archive_file):
                self._archive_cache_key = None
                self._archive_cache_records = []
                return [], True
            try:
                info = private_file_stat(self.archive_file)
                cache_key = (info.st_mtime_ns, info.st_size)
                if (self._archive_cache_key == cache_key
                        and self._archive_cache_records is not None):
                    records = self._archive_cache_records
                    return (copy.deepcopy(records) if copy_records else records), True
                with open_private_text_file(self.archive_file) as f:
                    records = json.load(f, parse_constant=_reject_nonfinite_json)
                if not isinstance(records, list) or any(
                        not isinstance(record, dict) for record in records):
                    raise ValueError('史书必须是交易对象数组')
                self._archive_cache_key = cache_key
                self._archive_cache_records = copy.deepcopy(records)
                cached = self._archive_cache_records
                return (copy.deepcopy(cached) if copy_records else cached), True
            except Exception as e:
                # 损坏后不能继续返回旧缓存，避免面板伪装成仍在展示最新史书。
                self._archive_cache_key = None
                self._archive_cache_records = None
                logger.error(f'读取平仓历史史书失败（历史展示降级为近期记录，归档暂停）: {self.archive_file}: {e}')
                return [], False

    def get_closed_trades(self):
        """全部平仓历史 = 史书（旧）+ 账本近期（新），按时间先后拼接。"""
        with self.lock:
            recent = self._snapshot_locked()['closed_trades']
            # 与 compact_closed_trades 共享同一 RLock，避免“先拍到未收缩账本、
            # 后读到已追加史书”把窄窗口内同一批成交返回两遍。
            archive, _ok = self._read_archive()
            return archive + recent

    def get_closed_trades_page(self, page, page_size):
        """按最新在前返回一页，不为每个 HTTP 请求复制/反转整部史书。"""
        if page < 1 or page_size < 1:
            raise ValueError('page/page_size 必须为正整数')
        with self.lock:
            archive, _ok = self._read_archive(copy_records=False)
            recent = self.state['closed_trades']
            total = len(archive) + len(recent)
            start = (page - 1) * page_size
            if start >= total:
                return [], total
            remaining = page_size
            selected = []

            # 最近账本最多 KEEP_RECENT_CLOSED 条，优先从尾部倒序取。
            if start < len(recent):
                recent_start = start
                recent_take = min(remaining, len(recent) - recent_start)
                hi = len(recent) - recent_start
                lo = hi - recent_take
                selected.extend(reversed(recent[lo:hi]))
                remaining -= recent_take
                archive_start = 0
            else:
                archive_start = start - len(recent)

            if remaining and archive_start < len(archive):
                archive_take = min(remaining, len(archive) - archive_start)
                hi = len(archive) - archive_start
                lo = hi - archive_take
                selected.extend(reversed(archive[lo:hi]))
            return copy.deepcopy(selected), total

    def get_closed_trades_revision(self):
        """供只读统计缓存使用；不解析史书内容即可识别归档/近期记录变化。"""
        with self.lock:
            try:
                info = private_file_stat(self.archive_file)
                archive_key = (info.st_mtime_ns, info.st_size)
            except FileNotFoundError:
                archive_key = None
            recent = self.state['closed_trades']
            last = recent[-1] if recent else {}
            recent_key = (
                len(recent), last.get('close_time'), last.get('symbol'),
                last.get('pnl'), last.get('position_size'))
        return archive_key, recent_key

    def compact_closed_trades(self):
        """把账本中超出保留窗口的最旧平仓记录搬进只追加的史书文件，返回搬移条数。

        fail-safe 顺序：先写史书、成功后才收缩账本（任一失败都不动账本，绝不丢史料）。
        账本落盘失败走既有回滚——此时史书里可能多出一批「已写入但账本未收缩」的记录，
        下一轮用内容级去重消除（同一批记录 deepcopy 后内容完全相等）。
        """
        with self.lock:
            closed = self.state['closed_trades']
            overflow_count = len(closed) - self.keep_recent_closed
            if overflow_count <= 0:
                return 0
            archive, ok = self._read_archive()
            if not ok:
                return 0  # 史书损坏：保留账本全部记录等人工修复，_read_archive 已记日志
            overflow = closed[:overflow_count]
            # 上轮可能崩溃在“史书已追加、账本尚未收缩”。只能跳过
            # archive 后缀与 overflow 前缀的最大有序重叠；集合式 `t not in tail`
            # 会把两笔内容恰好相同的真实成交误删掉。
            overlap = self._ordered_archive_overlap(archive, overflow)
            to_append = overflow[overlap:]
            if to_append and not atomic_write_json(self.archive_file, archive + to_append):
                logger.error(f'平仓历史归档写入失败，本轮跳过（账本保留全部记录）: {self.archive_file}')
                return 0
            snapshot = self._snapshot_locked()
            self.state['closed_trades'] = closed[overflow_count:]
            self._save_or_rollback_locked(snapshot)
            logger.info(f'已把 {overflow_count} 条最旧平仓记录归档到史书（账本保留最近 {self.keep_recent_closed} 条）')
            return overflow_count

    @staticmethod
    def _ordered_archive_overlap(archive, overflow):
        """KMP 求 archive 后缀与 overflow 前缀最大重叠，保留重复次数与顺序。"""
        if not archive or not overflow:
            return 0
        pattern = [
            json.dumps(item, sort_keys=True, ensure_ascii=False, allow_nan=False)
            for item in overflow]
        prefix = [0] * len(pattern)
        for index in range(1, len(pattern)):
            matched = prefix[index - 1]
            while matched and pattern[index] != pattern[matched]:
                matched = prefix[matched - 1]
            if pattern[index] == pattern[matched]:
                matched += 1
            prefix[index] = matched

        matched = 0
        for item in archive[-len(pattern):]:
            token = json.dumps(
                item, sort_keys=True, ensure_ascii=False, allow_nan=False)
            while matched and token != pattern[matched]:
                matched = prefix[matched - 1]
            if token == pattern[matched]:
                matched += 1
            if matched == len(pattern):
                # 完整模式若恰好落在 archive 尾部，就是最大重叠；若后面仍有
                # token，则退回前缀继续匹配。
                continue
        return matched

    def set_signal_state(self, symbol, mid_line_crossed):
        """设置品种的中轨穿越状态。"""
        with self.lock:
            snapshot = self._snapshot_locked()
            self._set_signal_state_locked(symbol, mid_line_crossed)
            self._save_or_rollback_locked(snapshot)

    def get_signal_state(self, symbol):
        """获取品种的中轨穿越状态。"""
        with self.lock:
            self._ensure_signal_states_locked()
            if symbol not in self.state['signal_states']:
                snapshot = self._snapshot_locked()
                self._set_signal_state_locked(symbol, False)
                self._save_or_rollback_locked(snapshot)
            return self.state['signal_states'][symbol].get('mid_line_crossed', False)

    def remove_symbol_metadata(self, symbol, clear_quarantine=False):
        """清除已退池且无持仓/止损残留品种的辅助状态。

        quarantine 默认保留：它可能代表交易所孤儿仓；只有调用方已重新确认交易所
        也为空时才可显式清除，避免“删除配置”顺手解除真钱隔离。
        """
        with self.lock:
            if symbol in self.state['open_positions']:
                return False
            if symbol in (self.state.get('open_intents') or {}):
                return False
            if symbol in (self.state.get('stop_residues') or {}):
                return False
            snapshot = self._snapshot_locked()
            changed = False
            for key in ('signal_states', 'stop_loss_dates'):
                mapping = self.state.get(key) or {}
                if symbol in mapping:
                    del mapping[symbol]
                    changed = True
            if clear_quarantine:
                quarantines = self.state.get('position_quarantines') or {}
                if symbol in quarantines:
                    del quarantines[symbol]
                    changed = True
            if changed:
                self._save_or_rollback_locked(snapshot)
            return changed

    def prune_inactive_symbol_metadata(self, active_symbols):
        """批量清理已退池且生命周期结束的信号/T+1 元数据。

        持仓、止损残留或仓位隔离仍存在的品种一律保留；因此删除时有仓的品种
        会在最终平仓后的下一轮自动清理，而不会永久留在 signal_states。
        """
        active = {str(symbol) for symbol in active_symbols}
        with self.lock:
            protected = set(self.state['open_positions'])
            protected.update((self.state.get('open_intents') or {}).keys())
            protected.update((self.state.get('stop_residues') or {}).keys())
            protected.update((self.state.get('position_quarantines') or {}).keys())
            protected.update(
                symbol for symbol, record in (self.state.get('signal_states') or {}).items()
                if isinstance(record, dict) and
                isinstance(record.get('signal_execution'), dict) and
                record['signal_execution'].get('status') == 'pending')
            candidates = (
                set((self.state.get('signal_states') or {}).keys()) |
                set((self.state.get('stop_loss_dates') or {}).keys())
            ) - active - protected
            if not candidates:
                return []
            snapshot = self._snapshot_locked()
            for key in ('signal_states', 'stop_loss_dates'):
                mapping = self.state.get(key) or {}
                for symbol in candidates:
                    mapping.pop(symbol, None)
            self._save_or_rollback_locked(snapshot)
            return sorted(candidates)

    def get_signal_metadata(self, symbol):
        """返回品种信号幂等元数据的快照。"""
        with self.lock:
            self._ensure_signal_states_locked()
            return copy.deepcopy(self.state['signal_states'].get(symbol) or {})

    def mark_candle_processed(self, symbol, strategy, candle_id):
        """持久化某策略最后成功处理的已收盘 K 线 ID。"""
        if not candle_id:
            raise ValueError('candle_id 不能为空')
        with self.lock:
            snapshot = self._snapshot_locked()
            self._ensure_signal_states_locked()
            record = self.state['signal_states'].setdefault(symbol, {})
            record['strategy'] = strategy
            record['last_processed_candle'] = str(candle_id)
            record['last_update'] = datetime.now().isoformat()
            self._save_or_rollback_locked(snapshot)

    # ---- 所有非海龟开仓入口共用的两阶段意图 ----

    def prepare_open_intent(
            self, symbol, strategy, side, client_order_id, payload,
            planned_position_size=None):
        if strategy not in ('turtle', 'ma_cross'):
            raise ValueError('open intent strategy 必须是 turtle/ma_cross')
        if side not in ('long', 'short'):
            raise ValueError('open intent side 必须是 long/short')
        if not client_order_id or not isinstance(payload, dict):
            raise ValueError('open intent client_order_id/payload 非法')
        planned = None
        if planned_position_size is not None:
            if isinstance(planned_position_size, bool):
                raise ValueError('open intent 数量不能是 bool')
            try:
                planned = float(planned_position_size)
            except (TypeError, ValueError) as exc:
                raise ValueError('open intent 数量非法') from exc
            if not math.isfinite(planned) or planned <= 0:
                raise ValueError('open intent 数量必须是正有限数')
        with self.lock:
            intents = self.state.setdefault('open_intents', {})
            existing = intents.get(symbol) or {}
            if existing.get('status') == 'pending':
                if existing.get('client_order_id') == str(client_order_id):
                    existing_planned = existing.get('planned_position_size')
                    if (planned is not None and existing_planned is not None and
                            abs(float(existing_planned) - planned) >
                            max(1e-15, math.ulp(planned) * 8)):
                        raise TradeStatePersistenceError(
                            f'{symbol} 同一 open intent 的计划量不一致')
                    return copy.deepcopy(existing)
                raise TradeStatePersistenceError(
                    f'{symbol} 仍有未收口 open intent '
                    f"{existing.get('client_order_id')}，拒绝覆盖")
            snapshot = self._snapshot_locked()
            now = datetime.now().isoformat()
            intent = {
                'strategy': strategy, 'side': side,
                'client_order_id': str(client_order_id), 'status': 'pending',
                'payload': copy.deepcopy(payload),
                'created_at': now, 'updated_at': now,
            }
            if planned is not None:
                intent['planned_position_size'] = planned
            intents[symbol] = intent
            self._save_or_rollback_locked(snapshot)
            return copy.deepcopy(intent)

    def get_open_intent(self, symbol, client_order_id=None):
        with self.lock:
            intent = copy.deepcopy(
                (self.state.get('open_intents') or {}).get(symbol))
            if not intent or intent.get('status') != 'pending':
                return None
            if (client_order_id is not None and
                    intent.get('client_order_id') != str(client_order_id)):
                return None
            return intent

    def get_open_intents(self):
        with self.lock:
            return copy.deepcopy(self.state.get('open_intents') or {})

    def set_open_intent_amount(self, symbol, client_order_id, position_size):
        if isinstance(position_size, bool):
            raise ValueError('open intent 数量不能是 bool')
        try:
            amount = float(position_size)
        except (TypeError, ValueError) as exc:
            raise ValueError('open intent 数量非法') from exc
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError('open intent 数量必须是正有限数')
        with self.lock:
            intent = (self.state.get('open_intents') or {}).get(symbol) or {}
            if (intent.get('status') != 'pending' or
                    intent.get('client_order_id') != str(client_order_id)):
                raise TradeStatePersistenceError(
                    f'{symbol} 不存在匹配 open intent')
            existing = intent.get('planned_position_size')
            if existing is not None:
                return float(existing)
            snapshot = self._snapshot_locked()
            intent['planned_position_size'] = amount
            intent['updated_at'] = datetime.now().isoformat()
            self._save_or_rollback_locked(snapshot)
            return amount

    def resolve_open_intent(self, symbol, client_order_id):
        with self.lock:
            intent = (self.state.get('open_intents') or {}).get(symbol) or {}
            if intent.get('client_order_id') != str(client_order_id):
                raise TradeStatePersistenceError(
                    f'{symbol} open intent 收口句柄不匹配')
            snapshot = self._snapshot_locked()
            del self.state['open_intents'][symbol]
            self._save_or_rollback_locked(snapshot)
            return copy.deepcopy(intent)

    def finalize_open_intent_round_trip(
            self, symbol, client_order_id, entry_price, exit_price,
            position_size, entry_order_ids=None, exit_order_ids=None,
            entry_fee=None, exit_fee=None, reason='open intent 恢复补记'):
        try:
            entry_price = float(entry_price)
            exit_price = float(exit_price)
            position_size = float(position_size)
        except (TypeError, ValueError) as exc:
            raise ValueError('open intent 往返价格/数量非法') from exc
        if any(not math.isfinite(value) or value <= 0 for value in (
                entry_price, exit_price, position_size)):
            raise ValueError('open intent 往返价格/数量必须是正有限数')
        with self.lock:
            intent = (self.state.get('open_intents') or {}).get(symbol) or {}
            if intent.get('client_order_id') != str(client_order_id):
                raise TradeStatePersistenceError(
                    f'{symbol} open intent 往返与当前句柄不匹配')
            existing = next((
                item for item in reversed(self.state['closed_trades'])
                if item.get('symbol') == symbol and
                item.get('client_order_id') == str(client_order_id)), None)
            if existing is None:
                archive, ok = self._read_archive(copy_records=False)
                if not ok:
                    raise TradeStatePersistenceError(
                        '平仓史书不可验证，拒绝追加可能重复的 open intent 往返')
                existing = next((
                    item for item in reversed(archive)
                    if item.get('symbol') == symbol and
                    item.get('client_order_id') == str(client_order_id)), None)
            snapshot = self._snapshot_locked()
            if existing is None:
                side = intent.get('side')
                strategy = intent.get('strategy')
                trade = {
                    'symbol': symbol, 'side': side, 'strategy': strategy,
                    'entry_price': entry_price, 'exit_price': exit_price,
                    'final_exit_price': exit_price,
                    'position_size': position_size,
                    'original_position_size': position_size,
                    'open_time': intent.get('created_at'),
                    'close_time': datetime.now().isoformat(),
                    'recovered_round_trip': True,
                    'recovery_reason': str(reason),
                    'client_order_id': str(client_order_id),
                }
                trade.update(calculate_closed_trade_metrics(
                    side, entry_price, exit_price, position_size,
                    entry_fee=entry_fee, exit_fee=exit_fee))
                if entry_order_ids:
                    trade['entry_order_ids'] = [
                        str(value) for value in entry_order_ids if value]
                if exit_order_ids:
                    trade['exit_order_ids'] = [
                        str(value) for value in exit_order_ids if value]
                self.state['closed_trades'].append(trade)
                existing = trade
            del self.state['open_intents'][symbol]
            self._save_or_rollback_locked(snapshot)
            return copy.deepcopy(existing)

    def prepare_signal_execution(self, symbol, strategy, signal_id, client_order_id, payload=None):
        """在外部交易前持久化 pending，但不提前宣称信号已消费。

        同一 signal_id 重启/重试时返回原确定性 client_order_id，适配层会先按
        clOrdId 查找旧订单，因此同时消除“落盘后下单前崩溃吞信号”与
        “下单后记账前崩溃重复下单”两个窗口。
        """
        if not signal_id or not client_order_id:
            raise ValueError('signal_id/client_order_id 不能为空')
        with self.lock:
            self._ensure_signal_states_locked()
            record = self.state['signal_states'].setdefault(symbol, {})
            execution = record.get('signal_execution') or {}
            if (execution.get('strategy') == strategy and
                    execution.get('signal_id') == str(signal_id)):
                return copy.deepcopy(execution)
            if execution.get('status') == 'pending':
                raise TradeStatePersistenceError(
                    f'{symbol} 仍有未收口信号 '
                    f"{execution.get('signal_id')} / {execution.get('client_order_id')}，"
                    f'拒绝用新信号 {signal_id} 覆盖唯一恢复句柄')
            snapshot = self._snapshot_locked()
            execution = {
                'strategy': strategy,
                'signal_id': str(signal_id),
                'client_order_id': str(client_order_id),
                'status': 'pending',
                'payload': copy.deepcopy(payload) if payload is not None else None,
                'updated_at': datetime.now().isoformat(),
            }
            record['strategy'] = strategy
            record['signal_execution'] = execution
            record['last_update'] = datetime.now().isoformat()
            self._save_or_rollback_locked(snapshot)
            return copy.deepcopy(execution)

    def get_pending_signal_execution(self, symbol, client_order_id=None):
        with self.lock:
            self._ensure_signal_states_locked()
            execution = copy.deepcopy(
                (self.state['signal_states'].get(symbol) or {}).get('signal_execution') or {})
            if execution.get('status') != 'pending':
                return None
            if (client_order_id is not None and
                    execution.get('client_order_id') != str(client_order_id)):
                return None
            return execution

    def get_pending_signal_executions(self):
        """返回全部未收口信号，供启动/日检主动裁决。"""
        with self.lock:
            self._ensure_signal_states_locked()
            pending = {}
            for symbol, record in self.state['signal_states'].items():
                if not isinstance(record, dict):
                    continue
                execution = record.get('signal_execution') or {}
                if execution.get('status') == 'pending':
                    pending[symbol] = copy.deepcopy(execution)
            return pending

    def set_pending_signal_order_amount(self, symbol, client_order_id, position_size):
        """在首次发单前固化计划币数，重启不因行情/权益变化改变同 clOrdId 的数量。"""
        if isinstance(position_size, bool):
            raise ValueError('计划仓位数量不能是 bool')
        try:
            amount = float(position_size)
        except (TypeError, ValueError) as exc:
            raise ValueError('计划仓位数量非法') from exc
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError('计划仓位数量必须是有限正数')
        with self.lock:
            self._ensure_signal_states_locked()
            record = self.state['signal_states'].get(symbol) or {}
            execution = record.get('signal_execution') or {}
            if (execution.get('status') != 'pending' or
                    execution.get('client_order_id') != str(client_order_id)):
                raise TradeStatePersistenceError(
                    f'{symbol} 不存在匹配的 pending 信号，拒绝固化下单量')
            existing = execution.get('planned_position_size')
            if existing is not None:
                return float(existing)
            snapshot = self._snapshot_locked()
            execution['planned_position_size'] = amount
            execution['updated_at'] = datetime.now().isoformat()
            self._save_or_rollback_locked(snapshot)
            return amount

    def confirm_signal_execution(self, symbol, strategy, signal_id):
        """仅在交易结果已经与主账本对齐后，把 pending 改为 confirmed。"""
        with self.lock:
            self._ensure_signal_states_locked()
            record = self.state['signal_states'].get(symbol) or {}
            execution = record.get('signal_execution') or {}
            if (execution.get('strategy') != strategy or
                    execution.get('signal_id') != str(signal_id)):
                raise TradeStatePersistenceError(
                    f'{symbol} 信号确认与 pending 不匹配，拒绝错记幂等状态')
            if execution.get('status') == 'confirmed':
                return copy.deepcopy(execution)
            snapshot = self._snapshot_locked()
            execution['status'] = 'confirmed'
            execution['updated_at'] = datetime.now().isoformat()
            record['last_consumed_signal_id'] = str(signal_id)
            if strategy == 'turtle':
                record['mid_line_crossed'] = False
            record['last_update'] = datetime.now().isoformat()
            self._save_or_rollback_locked(snapshot)
            return copy.deepcopy(execution)

    def finalize_signal_without_trade(
            self, symbol, strategy, signal_id, candle_id=None,
            resolution='unsubmitted_expired', reason=None, order=None):
        """原子消费已明确不会产生持仓的 pending 信号。

        仅用于“从未发单且原止损已失效”或“旧订单已终态且零成交”。
        signal_execution 与 candle marker 同一次落盘，不留“已消费但重启又重放”窗口。
        """
        with self.lock:
            self._ensure_signal_states_locked()
            record = self.state['signal_states'].get(symbol) or {}
            execution = record.get('signal_execution') or {}
            if (execution.get('strategy') != strategy or
                    execution.get('signal_id') != str(signal_id) or
                    execution.get('status') != 'pending'):
                raise TradeStatePersistenceError(
                    f'{symbol} 无成交收口与当前 pending 不匹配')
            snapshot = self._snapshot_locked()
            now = datetime.now().isoformat()
            execution['status'] = 'confirmed'
            execution['resolution'] = str(resolution)
            execution['resolution_reason'] = str(reason or resolution)
            execution['updated_at'] = now
            if isinstance(order, dict):
                execution['resolution_order'] = {
                    'id': str(order.get('id')) if order.get('id') is not None else None,
                    'status': str(order.get('status')) if order.get('status') is not None else None,
                    'filled': order.get('filled'),
                }
            record['last_consumed_signal_id'] = str(signal_id)
            if strategy == 'turtle':
                record['mid_line_crossed'] = False
            if candle_id:
                record['last_processed_candle'] = str(candle_id)
            record['strategy'] = strategy
            record['last_update'] = now
            self._save_or_rollback_locked(snapshot)
            return copy.deepcopy(execution)

    def finalize_recovered_signal_round_trip(
            self, symbol, strategy, signal_id, side, entry_price, exit_price,
            position_size, stop_loss_price, candle_id=None,
            entry_fee=None, entry_fee_currency=None, entry_order_ids=None,
            exit_fee=None, exit_fee_currency=None, exit_order_ids=None,
            reason='pending 崩溃恢复时止损已失效，补偿平仓'):
        """原子记录 pending 已成交后又全量回滚的完整往返，并消费信号。

        不能先 append 成交史、再另存 confirmed：任一步崩溃都会造成重复回滚或
        丢交易。该方法把成交史、signal_execution 和 candle marker 合并为一次
        主账本事务；调用方只能在交易所已明确 ``fully_closed=True`` 后使用。
        """
        if side not in ('long', 'short'):
            raise ValueError('side 必须是 long/short')
        try:
            entry_price = float(entry_price)
            exit_price = float(exit_price)
            position_size = float(position_size)
            stop_loss_price = float(stop_loss_price)
        except (TypeError, ValueError) as exc:
            raise ValueError('恢复往返的价格/数量必须是正有限数') from exc
        if any(not math.isfinite(value) or value <= 0 for value in (
                entry_price, exit_price, position_size, stop_loss_price)):
            raise ValueError('恢复往返的价格/数量必须是正有限数')

        with self.lock:
            self._ensure_signal_states_locked()
            signal_record = self.state['signal_states'].get(symbol) or {}
            execution = signal_record.get('signal_execution') or {}
            if (execution.get('strategy') != strategy or
                    execution.get('signal_id') != str(signal_id) or
                    execution.get('status') != 'pending'):
                raise TradeStatePersistenceError(
                    f'{symbol} 恢复往返与当前 pending 不匹配，拒绝错记成交史')

            snapshot = self._snapshot_locked()
            now = datetime.now().isoformat()
            actual_entry_fee = _normalise_optional_fee(entry_fee)
            actual_exit_fee = _normalise_optional_fee(exit_fee)
            incoming_order_ids = {
                str(value) for value in (entry_order_ids or []) if value}
            existing_trade = None
            def matches_existing(candidate):
                # signal_id 只在“单个 symbol 的 signal_states”内唯一；不同品种
                # 同一根日线、同一方向会得到相同 candle|side。缺少品种约束会把
                # ETH 的历史误当 BTC 已记账，确认 pending 却吞掉真实成交史。
                if candidate.get('symbol') != symbol:
                    return False
                candidate_ids = {
                    str(value) for value in (candidate.get('entry_order_ids') or []) if value}
                return bool(
                    candidate.get('signal_id') == str(signal_id) or
                    candidate.get('client_order_id') == execution.get('client_order_id') or
                    (incoming_order_ids and incoming_order_ids & candidate_ids))

            for candidate in reversed(self.state['closed_trades']):
                if matches_existing(candidate):
                    existing_trade = candidate
                    break
            if existing_trade is None:
                archive, archive_ok = self._read_archive(copy_records=False)
                if not archive_ok:
                    raise TradeStatePersistenceError(
                        '平仓史书不可验证，拒绝追加可能重复的 pending 恢复往返')
                for candidate in reversed(archive):
                    if matches_existing(candidate):
                        existing_trade = candidate
                        break
            if existing_trade is not None:
                # 可能崩溃在“真实平仓已记入 closed_trades”后、pending 确认前。
                # 只补幂等元数据，绝不追加第二条往返成交。
                execution['status'] = 'confirmed'
                execution['resolution'] = 'existing_closed_trade'
                execution['updated_at'] = now
                signal_record['last_consumed_signal_id'] = str(signal_id)
                if strategy == 'turtle':
                    signal_record['mid_line_crossed'] = False
                if candle_id:
                    signal_record['last_processed_candle'] = str(candle_id)
                signal_record['strategy'] = strategy
                signal_record['last_update'] = now
                self._save_or_rollback_locked(snapshot)
                return copy.deepcopy(existing_trade)
            trade = {
                'symbol': symbol,
                'side': side,
                'strategy': strategy,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'final_exit_price': exit_price,
                'position_size': position_size,
                'original_position_size': position_size,
                'stop_loss_price': stop_loss_price,
                'stop_order_id': None,
                'open_time': execution.get('updated_at') or now,
                'close_time': now,
                'recovered_round_trip': True,
                'recovery_reason': str(reason),
                'signal_id': str(signal_id),
                'client_order_id': execution.get('client_order_id'),
            }
            trade.update(calculate_closed_trade_metrics(
                side, entry_price, exit_price, position_size,
                entry_fee=actual_entry_fee, exit_fee=actual_exit_fee))
            if entry_fee_currency is not None:
                trade['entry_fee_currency'] = entry_fee_currency
            if exit_fee_currency is not None:
                trade['exit_fee_currency'] = exit_fee_currency
            if entry_order_ids:
                trade['entry_order_ids'] = [
                    str(value) for value in entry_order_ids if value]
            if exit_order_ids:
                trade['exit_order_ids'] = [
                    str(value) for value in exit_order_ids if value]
            self.state['closed_trades'].append(trade)

            execution['status'] = 'confirmed'
            execution['updated_at'] = now
            signal_record['last_consumed_signal_id'] = str(signal_id)
            if strategy == 'turtle':
                signal_record['mid_line_crossed'] = False
            if candle_id:
                signal_record['last_processed_candle'] = str(candle_id)
            signal_record['strategy'] = strategy
            signal_record['last_update'] = now
            self._save_or_rollback_locked(snapshot)
            return copy.deepcopy(trade)

    # ---- 双均线 T+1：与持仓/止损/信号共用同一账本事务 ----

    def get_stop_loss_dates(self):
        with self.lock:
            return copy.deepcopy(self.state.get('stop_loss_dates') or {})

    def replace_stop_loss_dates(self, dates):
        if not isinstance(dates, dict) or any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in dates.items()):
            raise ValueError('stop_loss_dates 必须是 {symbol: YYYY-MM-DD} 对象')
        with self.lock:
            snapshot = self._snapshot_locked()
            self.state['stop_loss_dates'] = copy.deepcopy(dates)
            self.state['stop_loss_dates_migrated'] = True
            self._save_or_rollback_locked(snapshot)
            return self.get_stop_loss_dates()

    def stop_loss_dates_migrated(self):
        with self.lock:
            return bool(self.state.get('stop_loss_dates_migrated', False))

    # ---- 仓位现实隔离：不一致时阻断该品种任何新建仓 ----

    def mark_position_quarantine(
            self, symbol, reason, details=None, stop_residue_possible=False):
        with self.lock:
            snapshot = self._snapshot_locked()
            quarantines = self.state.setdefault('position_quarantines', {})
            previous = quarantines.get(symbol) or {}
            quarantines[symbol] = {
                'reason': str(reason),
                'details': copy.deepcopy(details) if details is not None else None,
                'first_seen': previous.get('first_seen') or datetime.now().isoformat(),
                'last_seen': datetime.now().isoformat(),
            }
            if stop_residue_possible:
                self.state.setdefault('stop_residues', {})[symbol] = (
                    datetime.now().isoformat())
            self._save_or_rollback_locked(snapshot)
            return copy.deepcopy(quarantines[symbol])

    def force_runtime_mark_position_quarantine(
            self, symbol, reason, details=None, stop_residue_possible=False):
        """磁盘故障时仍在本进程阻断交易；只作运行时最后防线，不冒充已持久化。"""
        with self.lock:
            quarantines = self.state.setdefault('position_quarantines', {})
            previous = quarantines.get(symbol) or {}
            quarantines[symbol] = {
                'reason': str(reason),
                'details': copy.deepcopy(details) if details is not None else None,
                'first_seen': previous.get('first_seen') or datetime.now().isoformat(),
                'last_seen': datetime.now().isoformat(),
            }
            if stop_residue_possible:
                self.state.setdefault('stop_residues', {})[symbol] = (
                    datetime.now().isoformat())
            return copy.deepcopy(quarantines[symbol])

    def clear_position_quarantine(self, symbol):
        with self.lock:
            quarantines = self.state.get('position_quarantines') or {}
            if symbol not in quarantines:
                return False
            snapshot = self._snapshot_locked()
            del quarantines[symbol]
            self._save_or_rollback_locked(snapshot)
            return True

    def is_position_quarantined(self, symbol):
        with self.lock:
            return symbol in (self.state.get('position_quarantines') or {})

    def get_position_quarantines(self):
        with self.lock:
            return copy.deepcopy(self.state.get('position_quarantines') or {})

    # ---- 日检去重：跨进程/跨零点仍保留调度日语义 ----

    def get_last_daily_check_date(self):
        with self.lock:
            return self.state.get('last_daily_check_date')

    def set_last_daily_check_date(self, check_date):
        if check_date is not None:
            if not isinstance(check_date, str):
                raise ValueError('check_date 必须是 YYYY-MM-DD 或 null')
            datetime.strptime(check_date, '%Y-%m-%d')
        with self.lock:
            snapshot = self._snapshot_locked()
            self.state['last_daily_check_date'] = check_date
            self._save_or_rollback_locked(snapshot)
            return check_date

    def get_last_daily_summary_date(self):
        with self.lock:
            return self.state.get('last_daily_summary_date')

    def set_last_daily_summary_date(self, summary_date):
        if summary_date is not None:
            if not isinstance(summary_date, str):
                raise ValueError('summary_date 必须是 YYYY-MM-DD 或 null')
            datetime.strptime(summary_date, '%Y-%m-%d')
        with self.lock:
            snapshot = self._snapshot_locked()
            self.state['last_daily_summary_date'] = summary_date
            self._save_or_rollback_locked(snapshot)
            return summary_date

    def set_position_strategy(self, symbol, strategy):
        """为已有持仓补写策略字段（老仓缺 strategy 时兜底，避免删除后误按默认策略托管）。"""
        with self.lock:
            if symbol not in self.state['open_positions']:
                return None
            snapshot = self._snapshot_locked()
            self.state['open_positions'][symbol]['strategy'] = strategy
            self._save_or_rollback_locked(snapshot)
            return copy.deepcopy(self.state['open_positions'][symbol])

    # ---- 止损残留标记：旧止损单撤销无法确认时阻断该品种新开仓，直到确认清理 ----

    def mark_stop_residue(self, symbol):
        """标记该品种可能残留未撤销的止损单（撤销不可确认），持久化。"""
        with self.lock:
            snapshot = self._snapshot_locked()
            self.state.setdefault('stop_residues', {})[symbol] = datetime.now().isoformat()
            self._save_or_rollback_locked(snapshot)

    def force_runtime_mark_stop_residue(self, symbol):
        """磁盘故障时仍在本进程阻断未来开仓；不冒充已经持久化。"""
        with self.lock:
            self.state.setdefault('stop_residues', {})[symbol] = datetime.now().isoformat()

    def clear_stop_residue(self, symbol):
        with self.lock:
            residues = self.state.get('stop_residues') or {}
            if symbol in residues:
                snapshot = self._snapshot_locked()
                del residues[symbol]
                self._save_or_rollback_locked(snapshot)

    def has_stop_residue(self, symbol):
        with self.lock:
            return symbol in (self.state.get('stop_residues') or {})

    def get_stop_residues(self):
        with self.lock:
            return dict(self.state.get('stop_residues') or {})

    def get_owner_exchange(self):
        """读取状态文件归属的交易所标记（None 表示尚未标记）。"""
        with self.lock:
            return self.state.get('exchange')

    def claim_owner_exchange(self, exchange_id):
        """把当前状态文件标记为某交易所所有（仅应在安全情形下调用：空状态或已确认归属）。"""
        with self.lock:
            snapshot = self._snapshot_locked()
            self.state['exchange'] = exchange_id
            self._save_or_rollback_locked(snapshot)
            return exchange_id
