import copy
import json
import logging
import math
import os
import shutil
import tempfile
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

_file_lock = threading.RLock()
TRADING_FEE_RATE = 0.00045


class TradeStatePersistenceError(RuntimeError):
    """交易状态持久化失败。"""


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
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
        return True
    except Exception as e:
        logger.error(f'原子写入失败 {filepath}: {e}')
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def calculate_closed_trade_metrics(side, entry_price, exit_price, position_size, fee_rate=TRADING_FEE_RATE):
    """按合约价值计算开/平仓手续费，并返回净盈亏口径。"""
    entry_price = float(entry_price or 0)
    exit_price = float(exit_price or 0)
    position_size = float(position_size or 0)

    entry_notional = entry_price * position_size
    exit_notional = exit_price * position_size

    if side == 'long':
        gross_pnl = (exit_price - entry_price) * position_size
    else:
        gross_pnl = (entry_price - exit_price) * position_size

    entry_fee = entry_notional * fee_rate
    exit_fee = exit_notional * fee_rate
    total_fee = entry_fee + exit_fee
    net_pnl = gross_pnl - total_fee
    pnl_percent = (net_pnl / entry_notional * 100) if entry_notional > 0 else 0

    return {
        'fee_rate': fee_rate,
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
    """对历史已平仓记录统一按当前手续费口径重算，避免新旧数据不一致。"""
    enriched = copy.deepcopy(trade)
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
        self.state = self.load_state()

    def load_state(self):
        """加载账本，fail-closed 语义：

        - 主文件与 .bak 都不存在：全新部署，返回默认空状态；
        - 主文件不存在但 .bak 仍在：疑似误删，拒绝启动（人工恢复备份或删 .bak 确认重置）；
        - 主文件可读：正常加载；
        - 主文件损坏：无论 .bak 是否可读都拒绝启动；备份只供停机后人工核对。

        账本无法确认时绝不「失忆」运行：不仅会漏管旧仓，日检还会把有真实仓位的
        品种当空仓重复开仓（单向模式下同向叠加敞口/反向误减仓）。
        """
        backup = self.state_file + '.bak'
        if not os.path.exists(self.state_file):
            if os.path.exists(backup):
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
            with open(self.state_file, 'r', encoding='utf-8') as f:
                return self._validate_loaded_state(json.load(f), self.state_file)
        except Exception as e:
            backup_hint = f'；备份候选位于 {backup}' if os.path.exists(backup) else ''
            raise TradeStatePersistenceError(
                f'交易状态主文件损坏或无法读取: {self.state_file}: {e}{backup_hint}。'
                f'.bak 是上一版本，可能漏掉刚开仓或复活刚平仓，禁止自动恢复。'
                f'请停机核对交易所实仓、主文件与备份后人工修复再启动'
            ) from e

    @staticmethod
    def _validate_loaded_state(state, source):
        """命脉账本的最小结构校验：类型损坏不得被当成空状态继续交易。"""
        if not isinstance(state, dict):
            raise ValueError(f'{source} 顶层必须是对象')
        if not isinstance(state.get('open_positions'), dict):
            raise ValueError(f'{source} open_positions 必须是对象')
        if not isinstance(state.get('closed_trades'), list):
            raise ValueError(f'{source} closed_trades 必须是数组')
        if 'stop_residues' in state and not isinstance(state['stop_residues'], dict):
            raise ValueError(f'{source} stop_residues 必须是对象')
        if 'exchange' in state and state['exchange'] is not None and not isinstance(state['exchange'], str):
            raise ValueError(f'{source} exchange 必须是字符串')
        for symbol, position in state['open_positions'].items():
            if not isinstance(symbol, str) or not isinstance(position, dict):
                raise ValueError(f'{source} open_positions 含非法持仓条目: {symbol!r}')
            if position.get('symbol') not in (None, symbol):
                raise ValueError(f'{source} {symbol} 持仓内 symbol 与键不一致')
            side = position.get('side')
            if side not in ('long', 'short'):
                raise ValueError(f'{source} {symbol} side 非法: {side!r}')
            numbers = {}
            for field in ('entry_price', 'position_size', 'stop_loss_price'):
                value = position.get(field)
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value <= 0):
                    raise ValueError(f'{source} {symbol} {field} 必须是正有限数: {value!r}')
                numbers[field] = float(value)
            if side == 'long' and numbers['stop_loss_price'] >= numbers['entry_price']:
                raise ValueError(f'{source} {symbol} 多仓止损价必须低于入场价')
            if side == 'short' and numbers['stop_loss_price'] <= numbers['entry_price']:
                raise ValueError(f'{source} {symbol} 空仓止损价必须高于入场价')
            if position.get('strategy') not in (None, 'ma_cross'):
                raise ValueError(f"{source} {symbol} strategy 非法: {position.get('strategy')!r}")
        return state

    def get_default_state(self):
        return {
            'open_positions': {},
            'closed_trades': []
        }

    def _snapshot_locked(self):
        return copy.deepcopy(self.state)

    def save_state(self):
        with self.lock:
            snapshot = self._snapshot_locked()
            try:
                if os.path.exists(self.state_file):
                    shutil.copy2(self.state_file, self.state_file + '.bak')
            except Exception:
                pass
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

    @staticmethod
    def _new_open_position(symbol, side, entry_price, position_size, stop_loss_price,
                           stop_order_id=None, strategy=None):
        return {
            'symbol': symbol,
            'side': side,
            'entry_price': entry_price,
            'position_size': position_size,
            'stop_loss_price': stop_loss_price,
            'stop_order_id': stop_order_id,
            'strategy': strategy,
            'open_time': datetime.now().isoformat()
        }

    def add_open_position(self, symbol, side, entry_price, position_size, stop_loss_price, stop_order_id=None, strategy=None):
        with self.lock:
            snapshot = self._snapshot_locked()
            position = self._new_open_position(
                symbol, side, entry_price, position_size, stop_loss_price,
                stop_order_id, strategy)
            self.state['open_positions'][symbol] = position
            self._save_or_rollback_locked(snapshot)
            return copy.deepcopy(position)

    def force_runtime_add_open_position(self, symbol, side, entry_price, position_size,
                                        stop_loss_price, stop_order_id=None, strategy=None):
        """交易所回滚失败且磁盘不可写时，至少让当前进程继续托管真实仓位。"""
        with self.lock:
            position = self._new_open_position(
                symbol, side, entry_price, position_size, stop_loss_price,
                stop_order_id, strategy)
            self.state['open_positions'][symbol] = position
            return copy.deepcopy(position)

    def get_open_position(self, symbol):
        with self.lock:
            position = self.state['open_positions'].get(symbol)
            return copy.deepcopy(position) if position is not None else None

    def update_stop_loss(self, symbol, new_stop_price, new_stop_order_id):
        with self.lock:
            if symbol not in self.state['open_positions']:
                return None
            snapshot = self._snapshot_locked()
            position = self.state['open_positions'][symbol]
            position['stop_loss_price'] = new_stop_price
            position['stop_order_id'] = new_stop_order_id
            position['last_stop_update'] = datetime.now().isoformat()
            self._save_or_rollback_locked(snapshot)
            return copy.deepcopy(position)

    def force_runtime_update_stop_loss(self, symbol, new_stop_price, new_stop_order_id):
        with self.lock:
            if symbol not in self.state['open_positions']:
                return None
            position = self.state['open_positions'][symbol]
            position['stop_loss_price'] = new_stop_price
            position['stop_order_id'] = new_stop_order_id
            position['last_stop_update'] = datetime.now().isoformat()
            return copy.deepcopy(position)

    def _close_position_locked(self, symbol, exit_price):
        if symbol not in self.state['open_positions']:
            return None

        position = self.state['open_positions'][symbol]
        try:
            exit_price = float(exit_price)
        except (TypeError, ValueError):
            exit_price = float('nan')
        if not math.isfinite(exit_price) or exit_price <= 0:
            exit_price = position['entry_price']

        position['exit_price'] = exit_price
        position['close_time'] = datetime.now().isoformat()
        position.update(
            calculate_closed_trade_metrics(
                position['side'],
                position['entry_price'],
                exit_price,
                position['position_size'],
            )
        )

        self.state['closed_trades'].append(position)
        del self.state['open_positions'][symbol]
        return copy.deepcopy(position)

    def close_position(self, symbol, exit_price):
        with self.lock:
            snapshot = self._snapshot_locked()
            position = self._close_position_locked(symbol, exit_price)
            if position is None:
                return None
            self._save_or_rollback_locked(snapshot)
            return position

    def force_runtime_close_position(self, symbol, exit_price):
        with self.lock:
            return self._close_position_locked(symbol, exit_price)

    def get_all_open_positions(self):
        with self.lock:
            return self._snapshot_locked()['open_positions']

    def _read_archive(self):
        """读平仓历史史书。返回 (records, ok)：文件不存在视为空史书（ok=True）；
        损坏时 ok=False——调用方 fail-safe（展示只出近期、归档跳过本轮），绝不静默清空。"""
        if not os.path.exists(self.archive_file):
            return [], True
        try:
            with open(self.archive_file, 'r', encoding='utf-8') as f:
                records = json.load(f)
            if not isinstance(records, list):
                raise ValueError(f'史书顶层不是数组: {type(records).__name__}')
            return records, True
        except Exception as e:
            logger.error(f'读取平仓历史史书失败（历史展示降级为近期记录，归档暂停）: {self.archive_file}: {e}')
            return [], False

    def get_closed_trades(self):
        """全部平仓历史 = 史书（旧）+ 账本近期（新），按时间先后拼接。"""
        with self.lock:
            recent = self._snapshot_locked()['closed_trades']
        archive, _ok = self._read_archive()
        return archive + recent

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
            # 内容级去重：只需比对史书尾部（重复只可能来自上一轮的窄窗口）
            tail = archive[-(overflow_count + self.keep_recent_closed):]
            to_append = [t for t in overflow if t not in tail]
            if to_append and not atomic_write_json(self.archive_file, archive + to_append):
                logger.error(f'平仓历史归档写入失败，本轮跳过（账本保留全部记录）: {self.archive_file}')
                return 0
            snapshot = self._snapshot_locked()
            self.state['closed_trades'] = closed[overflow_count:]
            self._save_or_rollback_locked(snapshot)
            logger.info(f'已把 {overflow_count} 条最旧平仓记录归档到史书（账本保留最近 {self.keep_recent_closed} 条）')
            return overflow_count

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
        """残留标记落盘失败时的运行时阻断兜底（不再尝试写盘）。"""
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
