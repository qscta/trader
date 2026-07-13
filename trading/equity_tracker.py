"""按交易所隔离的权益 / 求索指数追踪器。

原本这些逻辑以模块级函数 + 全局文件常量的形式散落在 api_server.py 里，
只能服务单一交易所。多交易所并行后，每个交易所需要**完全独立**的权益历史、
峰值、每日快照、日内采样和求索指数——因此抽成本类，每个 TradingSystem 持有一份，
所有状态文件落在该交易所自己的 data_dir 下，互不串扰。

逻辑与原 api_server 实现保持一致，仅做了：路径实例化、全局单例改为 self.system 反向引用、
锁实例化、以及持久化失败/消息推送改为回调。
"""

import copy
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, date, timedelta, timezone

from trade_state import (atomic_write_json, open_private_text_file,
                         private_file_exists)
import config_validation as cfgv

logger = logging.getLogger(__name__)


class EquityStatePersistenceError(RuntimeError):
    """权益/指数辅助状态无法被可信读取或保存。"""


def _reject_nonfinite_json(value):
    raise ValueError(f'JSON 不允许非有限数字常量: {value}')


def _coerce_positive_float(value):
    try:
        value = cfgv.strict_float_finite(value, '正数')
    except Exception:
        return None
    return value if value > 0 else None


def _parse_equity_tick_timestamp(ts):
    try:
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is not None:
            # 内部调度/日界统一使用北京时间 naive datetime；把带 offset 的
            # 合法 ISO 先换算到 UTC+8，再去掉 tzinfo，避免 aware/naive 比较崩溃。
            parsed = parsed.astimezone(
                timezone(timedelta(hours=8))).replace(tzinfo=None)
        return parsed
    except Exception:
        return None


class EquityTracker:
    QIUSUO_INDEX_BASE = 1853.0
    QIUSUO_INDEX_ROLLOVER_HOUR = 8
    EQUITY_TICK_INTERVAL_MINUTES = 5
    STATS_CACHE_SECONDS = 5   # 只读统计的短缓存：前端 30s 轮询 + 多面板并发时不重复打交易所
    EQUITY_TICK_RETENTION_DAYS = 30   # 安全网：每日切日会把采样压成日线OHLC，正常只剩当天；此值仅在压缩失效时兜底，可由 config 覆盖
    EQUITY_OHLC_DEFAULT_DAYS = 120

    def __init__(self, data_dir, system, notify_failure=None, retention_days=None):
        """
        data_dir: 本交易所的数据目录（状态文件落在这里）
        system:   反向引用所属 TradingSystem，用其 exchange_api / trade_state
        notify_failure(label, filepath): 持久化失败告警回调
        retention_days: 日内采样保留天数（覆盖默认 EQUITY_TICK_RETENTION_DAYS）
        """
        self.data_dir = data_dir
        self.system = system
        self.notify_failure = notify_failure or (lambda label, filepath: None)
        if retention_days:
            try:
                self.EQUITY_TICK_RETENTION_DAYS = max(7, int(retention_days))
            except Exception as exc:
                # 主配置入口已 fail-loud 校验；此处只是构造器的兼容二道防线。
                logger.debug('retention_days 解析失败，沿用默认保留天数: %s', exc)
        self._lock = threading.RLock()
        self._stats_cache = None
        self._stats_cache_ts = 0.0

        os.makedirs(data_dir, exist_ok=True)
        self.PEAK_EQUITY_FILE = os.path.join(data_dir, 'peak_equity.json')
        self.EQUITY_HISTORY_FILE = os.path.join(data_dir, 'equity_history.json')
        self.DAILY_EQUITY_FILE = os.path.join(data_dir, 'daily_equity.json')
        self.EQUITY_TICKS_FILE = os.path.join(data_dir, 'equity_ticks.json')
        self.QIUSUO_INDEX_FILE = os.path.join(data_dir, 'qiusuo_index.json')
        self.EQUITY_SYNC_JOURNAL_FILE = os.path.join(
            data_dir, '.equity_sync_journal.json')
        self._recover_equity_sync_journal()

    def _equity_sync_targets(self):
        return {
            'peak': (self.PEAK_EQUITY_FILE, dict),
            'history': (self.EQUITY_HISTORY_FILE, dict),
            'qiusuo': (self.QIUSUO_INDEX_FILE, dict),
        }

    def _remove_sync_journal(self):
        if not private_file_exists(self.EQUITY_SYNC_JOURNAL_FILE):
            return
        os.unlink(self.EQUITY_SYNC_JOURNAL_FILE)
        directory_fd = None
        try:
            directory_fd = os.open(
                self.data_dir,
                os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            os.fsync(directory_fd)
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

    def _validate_sync_generation(self, generation, field):
        if not isinstance(generation, dict):
            raise ValueError(f'权益同步 journal.{field} 必须是对象')
        for key, (path, expected) in self._equity_sync_targets().items():
            if key not in generation:
                raise ValueError(f'权益同步 journal.{field} 缺少 {key}')
            self._validate_json_shape(generation[key], expected, path)

    def _recover_equity_sync_journal(self):
        """崩溃后按 journal 整代前滚，绝不加载半旧半新的权益基准。"""
        if not private_file_exists(self.EQUITY_SYNC_JOURNAL_FILE):
            return
        try:
            with open_private_text_file(self.EQUITY_SYNC_JOURNAL_FILE) as handle:
                journal = json.load(
                    handle, parse_constant=_reject_nonfinite_json)
            if not isinstance(journal, dict) or journal.get('version') != 1:
                raise ValueError('权益同步 journal 版本非法')
            old_generation = journal.get('old')
            new_generation = journal.get('new')
            self._validate_sync_generation(old_generation, 'old')
            self._validate_sync_generation(new_generation, 'new')
            for key, (path, _expected) in self._equity_sync_targets().items():
                if not atomic_write_json(path + '.bak', old_generation[key]):
                    raise OSError(f'恢复权益同步时写备份失败: {path}.bak')
                if not atomic_write_json(path, new_generation[key]):
                    raise OSError(f'恢复权益同步时写新一代失败: {path}')
            # journal 删除后，单文件损坏会走普通 .bak 恢复；备份也必须属于
            # 同一新世代，否则会把三文件重新拆成半旧半新。
            for key, (path, _expected) in self._equity_sync_targets().items():
                if not atomic_write_json(path + '.bak', new_generation[key]):
                    raise OSError(f'恢复权益同步时刷新新世代备份失败: {path}.bak')
            self._remove_sync_journal()
            logger.warning('检测到中断的权益同步，已按 journal 完整前滚同一代状态')
        except Exception as exc:
            raise EquityStatePersistenceError(
                f'权益同步 journal 无法恢复，拒绝加载半事务状态: {exc}') from exc

    def _commit_equity_sync_generation(self, old_generation, new_generation):
        """跨 peak/history/qiusuo 的可恢复多文件事务。"""
        self._validate_sync_generation(old_generation, 'old')
        self._validate_sync_generation(new_generation, 'new')
        journal = {
            'version': 1, 'created_at': datetime.now().isoformat(),
            'old': old_generation, 'new': new_generation,
        }
        if not atomic_write_json(self.EQUITY_SYNC_JOURNAL_FILE, journal):
            raise EquityStatePersistenceError('无法写权益同步预提交 journal')
        try:
            for key, (path, _expected) in self._equity_sync_targets().items():
                if not atomic_write_json(path + '.bak', old_generation[key]):
                    raise OSError(f'写权益同步备份失败: {path}.bak')
                if not atomic_write_json(path, new_generation[key]):
                    raise OSError(f'写权益同步新状态失败: {path}')
            for key, (path, _expected) in self._equity_sync_targets().items():
                if not atomic_write_json(path + '.bak', new_generation[key]):
                    raise OSError(f'刷新权益同步新世代备份失败: {path}.bak')
            self._remove_sync_journal()
            return True
        except Exception as commit_error:
            rollback_ok = True
            for key, (path, _expected) in self._equity_sync_targets().items():
                if not atomic_write_json(path, old_generation[key]):
                    rollback_ok = False
                if not atomic_write_json(path + '.bak', old_generation[key]):
                    rollback_ok = False
            if rollback_ok:
                try:
                    self._remove_sync_journal()
                except Exception:
                    rollback_ok = False
            if rollback_ok:
                raise EquityStatePersistenceError(
                    f'权益同步提交失败，旧一代已完整恢复: {commit_error}') from commit_error
            raise EquityStatePersistenceError(
                f'权益同步提交且同步回滚均失败；journal 已保留，'
                f'下次启动将前滚恢复: {commit_error}') from commit_error

    @staticmethod
    def _validate_json_shape(data, expected_type, filepath):
        if not isinstance(data, expected_type):
            raise ValueError(
                f'{filepath} 顶层应为 {expected_type.__name__}，实际为 {type(data).__name__}'
            )
        filename = os.path.basename(filepath)
        if filename.endswith('.bak'):
            filename = filename[:-4]

        def finite(value, field, *, positive=False, nonnegative=False,
                   allow_none=False):
            if value is None and allow_none:
                return
            if isinstance(value, bool):
                raise ValueError(f'{filepath}:{field} 不能是 bool')
            try:
                parsed = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f'{filepath}:{field} 必须是有限数') from exc
            if not math.isfinite(parsed):
                raise ValueError(f'{filepath}:{field} 必须是有限数')
            if positive and parsed <= 0:
                raise ValueError(f'{filepath}:{field} 必须为正')
            if nonnegative and parsed < 0:
                raise ValueError(f'{filepath}:{field} 不能为负')

        def iso_time(value, field, *, allow_none=True):
            if value is None and allow_none:
                return
            if not isinstance(value, str) or _parse_equity_tick_timestamp(value) is None:
                raise ValueError(f'{filepath}:{field} 必须是 ISO 时间或 null')

        if filename == 'peak_equity.json':
            finite(data.get('peak_equity', 0), 'peak_equity', nonnegative=True)
            iso_time(data.get('peak_time'), 'peak_time')
        elif filename == 'equity_history.json':
            for field in ('max_drawdown', 'longest_drawdown_days'):
                finite(data.get(field, 0), field, nonnegative=True)
            for field in ('initial_equity', 'year_start_equity'):
                finite(data.get(field), field, positive=True, allow_none=True)
            for field in ('max_dd_time', 'initial_time', 'year_start_time'):
                iso_time(data.get(field), field)
        elif filename == 'daily_equity.json':
            for index, item in enumerate(data):
                if not isinstance(item, dict):
                    raise ValueError(f'{filepath}[{index}] 必须是对象')
                day = item.get('date')
                if not isinstance(day, str):
                    raise ValueError(f'{filepath}[{index}].date 必须是 YYYY-MM-DD')
                datetime.strptime(day, '%Y-%m-%d')
                for field in ('equity', 'qiusuo_index', 'open', 'high', 'low', 'close'):
                    if field in item:
                        finite(item[field], f'[{index}].{field}', positive=True)
                if 'samples' in item:
                    finite(item['samples'], f'[{index}].samples', nonnegative=True)
        elif filename == 'equity_ticks.json':
            for index, item in enumerate(data):
                if not isinstance(item, dict):
                    raise ValueError(f'{filepath}[{index}] 必须是对象')
                iso_time(item.get('timestamp'), f'[{index}].timestamp', allow_none=False)
                iso_time(item.get('recorded_at'), f'[{index}].recorded_at')
                finite(item.get('equity'), f'[{index}].equity', positive=True)
                if 'qiusuo_index' in item or 'index' in item:
                    finite(
                        item.get('qiusuo_index', item.get('index')),
                        f'[{index}].qiusuo_index', positive=True)
        elif filename == 'qiusuo_index.json':
            for field in ('base_index', 'current_divisor', 'anchor_equity', 'anchor_index'):
                if field in data:
                    finite(data.get(field), field, positive=True, allow_none=True)
            iso_time(data.get('anchor_time'), 'anchor_time')
            history = data.get('history', [])
            if not isinstance(history, list):
                raise ValueError(f'{filepath}:history 必须是数组')
            for index, item in enumerate(history):
                if not isinstance(item, dict):
                    raise ValueError(f'{filepath}:history[{index}] 必须是对象')
                iso_time(
                    item.get('effective_from'),
                    f'history[{index}].effective_from', allow_none=False)
                finite(item.get('divisor'), f'history[{index}].divisor', positive=True)
                for field in ('anchor_equity', 'anchor_index'):
                    if field in item:
                        finite(
                            item.get(field), f'history[{index}].{field}',
                            positive=True, allow_none=True)
        return data

    def _load_json_state(self, filepath, expected_type, default):
        """读取辅助状态；主文件损坏时仅从合法备份恢复，绝不静默清空。"""
        backup = filepath + '.bak'
        if not private_file_exists(filepath):
            if not private_file_exists(backup):
                return default() if callable(default) else default.copy()
            # 主文件被误删但备份仍在，不能当成全新部署。权益基准/
            # 求索除数若静默回默认，下一两次保存还会覆盖唯一好备份。
            try:
                with open_private_text_file(backup) as f:
                    recovered = self._validate_json_shape(
                        json.load(f, parse_constant=_reject_nonfinite_json), expected_type, backup)
                if not atomic_write_json(filepath, recovered):
                    raise OSError('备份可读但无法恢复主文件')
                logger.warning('辅助状态主文件缺失，已从备份恢复: %s', backup)
                return recovered
            except Exception as backup_error:
                raise EquityStatePersistenceError(
                    f'辅助状态主文件缺失且备份不可恢复: {filepath} '
                    f'（备: {backup_error}）'
                ) from backup_error

        try:
            with open_private_text_file(filepath) as f:
                return self._validate_json_shape(
                    json.load(f, parse_constant=_reject_nonfinite_json), expected_type, filepath)
        except Exception as main_error:
            try:
                with open_private_text_file(backup) as f:
                    recovered = self._validate_json_shape(
                        json.load(f, parse_constant=_reject_nonfinite_json), expected_type, backup)
                if not atomic_write_json(filepath, recovered):
                    raise OSError('恢复后的状态无法写回主文件')
                logger.warning('辅助状态已从备份恢复: %s', backup)
                return recovered
            except Exception as backup_error:
                raise EquityStatePersistenceError(
                    f'辅助状态损坏且无可用备份，拒绝按空状态覆盖：{filepath} '
                    f'（主: {main_error}；备: {backup_error}）'
                ) from main_error

    def _save_json_state(self, filepath, data, expected_type, label):
        """保存辅助状态并保留最后一个已验证版本；现有文件损坏时拒绝覆盖。"""
        try:
            self._validate_json_shape(data, expected_type, filepath)
            if private_file_exists(filepath):
                with open_private_text_file(filepath) as f:
                    current = self._validate_json_shape(
                        json.load(f, parse_constant=_reject_nonfinite_json), expected_type, filepath)
                if not atomic_write_json(filepath + '.bak', current):
                    raise OSError('无法写入状态备份')
            if not atomic_write_json(filepath, data):
                raise OSError('原子写入失败')
            return True
        except Exception as e:
            logger.error('%s: %s: %s', label, filepath, e)
            self.notify_failure(label, filepath)
            return False

    # ====== 权益历史 / 峰值 ======

    def load_equity_history(self):
        return self._load_json_state(self.EQUITY_HISTORY_FILE, dict, lambda: {
            'max_drawdown': 0, 'max_dd_time': None,
            'initial_equity': None, 'initial_time': None,
            'year_start_equity': None, 'year_start_time': None,
            'longest_drawdown_days': 0
        })

    def save_equity_history(self, data):
        return self._save_json_state(
            self.EQUITY_HISTORY_FILE, data, dict, '保存权益历史失败')

    def load_peak_equity(self):
        return self._load_json_state(
            self.PEAK_EQUITY_FILE, dict, lambda: {'peak_equity': 0, 'peak_time': None})

    def save_peak_equity(self, peak_data):
        return self._save_json_state(
            self.PEAK_EQUITY_FILE, peak_data, dict, '保存峰值权益失败')

    def reconcile_peak_equity(self, current_equity, persist=False, now=None,
                              daily_close=False):
        """按日收盘口径协调峰值。

        普通统计调用只返回「当前权益若创新高」的临时展示值，绝不落盘；只有
        ``record_daily_equity_snapshot`` 明确传入 ``daily_close=True`` 时才可
        推进持久化峰值。``peak_observed_day`` 无论当天是否创新高都会写入，
        使日检失败重试也只能消费当天第一份已保存的收盘快照。
        """
        now = now or datetime.now()
        peak_data = self.load_peak_equity()
        stored_peak = float(peak_data.get('peak_equity', 0) or 0)
        peak_equity = stored_peak
        peak_time = peak_data.get('peak_time')

        if persist and not daily_close:
            raise ValueError('持久化峰值只能由每日收盘快照触发')

        if persist and daily_close:
            trading_day = self._qiusuo_trading_day(now)
            if peak_data.get('peak_observed_day') == trading_day:
                return stored_peak, peak_time

            if current_equity > stored_peak:
                # 结算刚结束的未创新高周期必须与落盘新峰值同一路径完成，否则
                # 「刚创新高」的信息会在下次统计刷新前丢失、历史最长未创新高漏记。
                self._settle_drawdown_streak(peak_time, now)
                peak_data['peak_equity'] = current_equity
                peak_data['peak_time'] = now.isoformat()

            # 即便今日没有创新高也必须记录「已观察」；否则下午第一次越过旧峰值
            # 的手动/重试统计仍会把日内浮盈误当成今日收盘。
            peak_data['peak_observed_day'] = trading_day
            peak_data.pop('peak_advanced_day', None)  # 清理 Claude 试验版字段
            if not self.save_peak_equity(peak_data):
                raise RuntimeError('保存峰值权益失败')
            return float(peak_data.get('peak_equity', 0) or 0), peak_data.get('peak_time')

        if current_equity > stored_peak:
            # 前端/周报可展示「当下创新高为 0 天」，但不改变日收盘基准。
            peak_equity = current_equity
            peak_time = now.isoformat()

        return peak_equity, peak_time

    def _settle_drawdown_streak(self, old_peak_time, now):
        """新峰值确立时，把「旧峰值时间 → 现在」这段刚结束的未创新高周期结算进历史最长。"""
        if not old_peak_time:
            return
        try:
            closed_gap = max(0, (now - datetime.fromisoformat(old_peak_time)).days)
        except Exception as exc:
            logger.debug('结算未创新高周期时跳过坏峰值时间 %r: %s', old_peak_time, exc)
            return
        if closed_gap <= 0:
            return
        with self._lock:
            eq_hist = self.load_equity_history()
            if closed_gap > (eq_hist.get('longest_drawdown_days', 0) or 0):
                eq_hist['longest_drawdown_days'] = closed_gap
                if not self.save_equity_history(eq_hist):
                    raise RuntimeError('保存刚结束的最长未创新高周期失败')

    def build_account_stats(self, persist=False):
        # 只读调用短缓存直出（persist=True 的统计刷新必须完整执行，且会刷新缓存）
        if not persist:
            with self._lock:
                if self._stats_cache is not None and (time.time() - self._stats_cache_ts) < self.STATS_CACHE_SECONDS:
                    return dict(self._stats_cache)

        balance = self.system.exchange_api.get_balance()
        if not balance:
            raise RuntimeError('获取账户余额失败')

        current_equity = balance['total'].get('USDT', 0)
        free_balance = balance['free'].get('USDT', 0)
        open_positions = self.system.trade_state.get_all_open_positions()
        total_unrealized_pnl = 0
        total_stop_loss_amount = 0

        for symbol, pos in open_positions.items():
            entry_price = pos.get('entry_price', 0)
            stop_loss_price = pos.get('stop_loss_price', 0)
            position_size = pos.get('position_size', 0)
            side = pos.get('side', 'long')
            try:
                ccxt_symbol = self.system.exchange_api.to_ccxt_symbol(symbol)
                current_price = self.system.exchange_api.get_last_price(ccxt_symbol)
                if side == 'long':
                    unrealized = (current_price - entry_price) * position_size
                    stop_loss_loss = (entry_price - stop_loss_price) * position_size
                else:
                    unrealized = (entry_price - current_price) * position_size
                    stop_loss_loss = (stop_loss_price - entry_price) * position_size
                total_unrealized_pnl += unrealized
                total_stop_loss_amount += stop_loss_loss
            except Exception as e:
                logger.warning(f"获取 {symbol} 市价失败: {e}")
                if side == 'long':
                    stop_loss_loss = (entry_price - stop_loss_price) * position_size
                else:
                    stop_loss_loss = (stop_loss_price - entry_price) * position_size
                total_stop_loss_amount += stop_loss_loss

        now = datetime.now()
        with self._lock:
            # 普通统计只读日收盘峰值；当下若越过峰值，仅返回 provisional 展示值。
            _old_peak = self.load_peak_equity()
            old_peak_equity = float(_old_peak.get('peak_equity', 0) or 0)
            old_peak_time = _old_peak.get('peak_time')
            peak_equity, peak_time = self.reconcile_peak_equity(
                current_equity, persist=False, now=now)
            made_new_high = current_equity > old_peak_equity

            peak_drawdown = 0
            if peak_equity > 0:
                peak_drawdown = 1 - current_equity / peak_equity
                if peak_drawdown < 0:
                    peak_drawdown = 0

            worst_case_equity = current_equity - total_unrealized_pnl - total_stop_loss_amount
            potential_max_drawdown = 0
            if peak_equity > 0:
                potential_max_drawdown = 1 - worst_case_equity / peak_equity
                if potential_max_drawdown < 0:
                    potential_max_drawdown = 0

            eq_hist = self.load_equity_history()
            if eq_hist.get('initial_equity') is None:
                eq_hist['initial_equity'] = current_equity
                eq_hist['initial_time'] = now.isoformat()

            year_key = str(now.year)
            if eq_hist.get('year_start_time') is None or not eq_hist['year_start_time'].startswith(year_key):
                eq_hist['year_start_equity'] = current_equity
                eq_hist['year_start_time'] = now.isoformat()

            if peak_equity > 0 and peak_drawdown > eq_hist.get('max_drawdown', 0):
                eq_hist['max_drawdown'] = peak_drawdown
                eq_hist['max_dd_time'] = now.isoformat()

            # 未创新高天数：当前时间距最近一次权益新高
            days_since_peak = 0
            if peak_time:
                try:
                    days_since_peak = max(0, (now - datetime.fromisoformat(peak_time)).days)
                except Exception:
                    days_since_peak = 0

            # 当前读数若临时创新高，只影响本次展示；真正的历史结算由每日收盘
            # reconcile 完成，不能让下午浮盈通过 persist=True 污染 durable 历史。
            provisional_closed_gap = 0
            if made_new_high and old_peak_time:
                try:
                    provisional_closed_gap = max(
                        0, (now - datetime.fromisoformat(old_peak_time)).days)
                except Exception as exc:
                    logger.debug('展示用未创新高周期结算跳过: %s', exc)

            if persist:
                if not self.save_equity_history(eq_hist):
                    raise RuntimeError('保存权益历史失败')

        year_start_eq = eq_hist.get('year_start_equity')
        ytd_return = ((current_equity - year_start_eq) / year_start_eq * 100) if year_start_eq and year_start_eq > 0 else 0
        initial_eq = eq_hist.get('initial_equity')
        total_return = ((current_equity - initial_eq) / initial_eq * 100) if initial_eq and initial_eq > 0 else 0

        # 展示用历史最长未创新高 = max(已结算最长, 当前仍未结束的这段)
        longest_dd = max(
            eq_hist.get('longest_drawdown_days', 0) or 0,
            days_since_peak,
            provisional_closed_gap,
        )

        stats = {
            'current_equity': current_equity,
            'free_balance': free_balance,
            'unrealized_pnl': total_unrealized_pnl,
            'peak_equity': peak_equity,
            'peak_time': peak_time,
            'peak_drawdown': peak_drawdown,
            'worst_case_equity': worst_case_equity,
            'total_stop_loss_amount': total_stop_loss_amount,
            'potential_max_drawdown': potential_max_drawdown,
            'position_count': len(open_positions),
            'ytd_return': ytd_return,
            'total_return': total_return,
            'max_drawdown': eq_hist.get('max_drawdown', 0),
            'max_dd_time': eq_hist.get('max_dd_time'),
            'initial_equity': initial_eq,
            'year_start_equity': year_start_eq,
            'days_since_peak': days_since_peak,
            'longest_drawdown_days': longest_dd
        }
        with self._lock:
            self._stats_cache = dict(stats)
            self._stats_cache_ts = time.time()
        return stats

    def refresh_account_stats_state(self):
        try:
            self.build_account_stats(persist=True)
        except Exception as e:
            logger.warning(f"刷新账户统计状态失败: {e}")

    # ====== 每日快照 / 日内采样 / 求索指数 ======

    def load_daily_equity(self):
        return self._load_json_state(self.DAILY_EQUITY_FILE, list, list)

    def save_daily_equity(self, data):
        return self._save_json_state(
            self.DAILY_EQUITY_FILE, data, list, '保存每日权益快照失败')

    def load_equity_ticks(self):
        return self._load_json_state(self.EQUITY_TICKS_FILE, list, list)

    def save_equity_ticks(self, data):
        return self._save_json_state(
            self.EQUITY_TICKS_FILE, data, list, '保存权益采样失败')

    def load_qiusuo_index_state(self):
        return self._load_json_state(self.QIUSUO_INDEX_FILE, dict, lambda: {
            'base_index': self.QIUSUO_INDEX_BASE,
            'current_divisor': None,
            'anchor_equity': None,
            'anchor_index': self.QIUSUO_INDEX_BASE,
            'anchor_time': None,
            'history': []
        })

    def save_qiusuo_index_state(self, data):
        return self._save_json_state(
            self.QIUSUO_INDEX_FILE, data, dict, '保存求索指数状态失败')

    def _equity_tick_bucket(self, now=None):
        now = now or datetime.now()
        minute = (now.minute // self.EQUITY_TICK_INTERVAL_MINUTES) * self.EQUITY_TICK_INTERVAL_MINUTES
        return now.replace(minute=minute, second=0, microsecond=0)

    def _qiusuo_trading_day(self, ts):
        return (ts - timedelta(hours=self.QIUSUO_INDEX_ROLLOVER_HOUR)).date().isoformat()

    def _qiusuo_day_timestamp(self, day_key):
        return _parse_equity_tick_timestamp(f"{day_key}T{self.QIUSUO_INDEX_ROLLOVER_HOUR:02d}:00:00")

    def _normalize_qiusuo_index_state(self, state):
        state = state if isinstance(state, dict) else {}
        base_index = _coerce_positive_float(state.get('base_index')) or self.QIUSUO_INDEX_BASE
        normalized = {
            'base_index': base_index,
            'current_divisor': _coerce_positive_float(state.get('current_divisor')),
            'anchor_equity': _coerce_positive_float(state.get('anchor_equity')),
            'anchor_index': _coerce_positive_float(state.get('anchor_index')) or base_index,
            'anchor_time': None,
            'history': []
        }

        anchor_time = _parse_equity_tick_timestamp(state.get('anchor_time'))
        if anchor_time:
            normalized['anchor_time'] = anchor_time.isoformat(timespec='seconds')

        history = []
        for item in state.get('history', []):
            if not isinstance(item, dict):
                continue
            effective_from = _parse_equity_tick_timestamp(item.get('effective_from') or item.get('anchor_time'))
            divisor = _coerce_positive_float(item.get('divisor'))
            if not effective_from or not divisor:
                continue
            history.append({
                'effective_from': effective_from.isoformat(timespec='seconds'),
                'divisor': divisor,
                'anchor_equity': _coerce_positive_float(item.get('anchor_equity')),
                'anchor_index': _coerce_positive_float(item.get('anchor_index')) or base_index,
                'reason': item.get('reason') or 'manual'
            })

        history.sort(key=lambda item: item['effective_from'])

        if not history and normalized['current_divisor']:
            effective_from = normalized['anchor_time'] or datetime.now().isoformat(timespec='seconds')
            history.append({
                'effective_from': effective_from,
                'divisor': normalized['current_divisor'],
                'anchor_equity': normalized['anchor_equity'],
                'anchor_index': normalized['anchor_index'],
                'reason': 'legacy'
            })

        if history:
            latest = history[-1]
            normalized['history'] = history
            normalized['current_divisor'] = latest['divisor']
            normalized['anchor_equity'] = latest.get('anchor_equity')
            normalized['anchor_index'] = latest.get('anchor_index') or base_index
            normalized['anchor_time'] = latest['effective_from']

        return normalized

    def ensure_qiusuo_index_state(self, current_equity=None, now=None, persist=False):
        now = now or datetime.now()
        state = self._normalize_qiusuo_index_state(self.load_qiusuo_index_state())
        if state.get('current_divisor'):
            if persist and not self.save_qiusuo_index_state(state):
                raise RuntimeError('保存求索指数状态失败')
            return state

        eq_hist = self.load_equity_history()
        anchor_equity = _coerce_positive_float(eq_hist.get('initial_equity'))
        anchor_time = _parse_equity_tick_timestamp(eq_hist.get('initial_time'))

        if anchor_equity is None:
            for snapshot in sorted(self.load_daily_equity(), key=lambda item: item.get('date', '')):
                anchor_equity = _coerce_positive_float(snapshot.get('equity'))
                if anchor_equity:
                    anchor_time = self._qiusuo_day_timestamp(snapshot.get('date'))
                    break

        if anchor_equity is None:
            anchor_equity = _coerce_positive_float(current_equity)
            anchor_time = now

        if anchor_equity is None:
            raise RuntimeError('无法初始化求索指数基点')

        base_index = state.get('base_index') or self.QIUSUO_INDEX_BASE
        anchor_time = anchor_time or now
        divisor = anchor_equity / base_index
        state = {
            'base_index': base_index,
            'current_divisor': divisor,
            'anchor_equity': anchor_equity,
            'anchor_index': base_index,
            'anchor_time': anchor_time.isoformat(timespec='seconds'),
            'history': [{
                'effective_from': anchor_time.isoformat(timespec='seconds'),
                'divisor': divisor,
                'anchor_equity': anchor_equity,
                'anchor_index': base_index,
                'reason': 'initial'
            }]
        }

        if persist and not self.save_qiusuo_index_state(state):
            raise RuntimeError('保存求索指数状态失败')
        return state

    def _resolve_qiusuo_history_entry(self, ts, state):
        state = self._normalize_qiusuo_index_state(state)
        history = state.get('history', [])
        if not history:
            return None

        chosen = history[0]
        for item in history:
            effective_from = _parse_equity_tick_timestamp(item.get('effective_from'))
            if effective_from and effective_from <= ts:
                chosen = item
            elif effective_from and effective_from > ts:
                break
        return chosen

    def calculate_qiusuo_index(self, equity, ts=None, state=None):
        equity = _coerce_positive_float(equity)
        if equity is None:
            return None

        ts = ts or datetime.now()
        state = self._normalize_qiusuo_index_state(state or self.load_qiusuo_index_state())
        if not state.get('current_divisor'):
            state = self.ensure_qiusuo_index_state(current_equity=equity, now=ts, persist=False)

        history_entry = self._resolve_qiusuo_history_entry(ts, state) or {}
        divisor = _coerce_positive_float(history_entry.get('divisor')) or _coerce_positive_float(state.get('current_divisor'))
        if divisor is None:
            return None
        return round(equity / divisor, 8)

    def _latest_qiusuo_index_anchor(self, state=None):
        state = self._normalize_qiusuo_index_state(state or self.load_qiusuo_index_state())
        ticks = self._trim_equity_ticks(self.load_equity_ticks())
        if ticks:
            latest_tick = ticks[-1]
            recorded_at = _parse_equity_tick_timestamp(latest_tick.get('recorded_at')) or _parse_equity_tick_timestamp(latest_tick.get('timestamp')) or datetime.now()
            latest_index = _coerce_positive_float(latest_tick.get('qiusuo_index')) or self.calculate_qiusuo_index(latest_tick.get('equity'), ts=recorded_at, state=state)
            if latest_index:
                return latest_index, recorded_at.isoformat(timespec='seconds')

        snapshots = sorted(self.load_daily_equity(), key=lambda item: item.get('date', ''))
        if snapshots:
            latest_snapshot = snapshots[-1]
            snapshot_ts = self._qiusuo_day_timestamp(latest_snapshot.get('date')) or datetime.now()
            latest_index = _coerce_positive_float(latest_snapshot.get('qiusuo_index') or latest_snapshot.get('index')) or self.calculate_qiusuo_index(latest_snapshot.get('equity'), ts=snapshot_ts, state=state)
            if latest_index:
                return latest_index, snapshot_ts.isoformat(timespec='seconds')

        return state.get('anchor_index') or state.get('base_index') or self.QIUSUO_INDEX_BASE, state.get('anchor_time')

    def _trim_equity_ticks(self, ticks):
        cutoff = datetime.now() - timedelta(days=self.EQUITY_TICK_RETENTION_DAYS)
        trimmed = []
        for item in ticks:
            ts = _parse_equity_tick_timestamp(item.get('timestamp'))
            recorded_at = _parse_equity_tick_timestamp(item.get('recorded_at')) or ts
            if ts is None or ts < cutoff:
                continue
            equity = _coerce_positive_float(item.get('equity'))
            if equity is None:
                continue
            normalized = {
                'timestamp': ts.isoformat(timespec='minutes'),
                'recorded_at': recorded_at.isoformat(timespec='seconds'),
                'equity': equity
            }
            qiusuo_index = _coerce_positive_float(item.get('qiusuo_index') or item.get('index'))
            if qiusuo_index is not None:
                normalized['qiusuo_index'] = qiusuo_index
            trimmed.append(normalized)
        trimmed.sort(key=lambda item: (item['timestamp'], item.get('recorded_at') or item['timestamp']))
        return trimmed

    def record_equity_tick(self, equity=None, now=None):
        """记录日内权益采样（默认 5 分钟一个桶）。"""
        try:
            now = now or datetime.now()
            if equity is None:
                balance = self.system.exchange_api.get_balance()
                if not balance:
                    return False
                equity = _coerce_positive_float(balance['total'].get('USDT', 0))
                if equity is None:
                    return False

            with self._lock:
                # 5 分钟按市值采样只维护求索指数，绝不推进「未创新高/回撤」峰值：
                # 否则日内浮盈冒一个高点就把 days_since_peak 永久清零（回撤时长指标失效）。
                # 峰值改由每日收盘快照按日推进（见 record_daily_equity_snapshot）。
                state = self.ensure_qiusuo_index_state(current_equity=equity, now=now, persist=True)
                qiusuo_index = self.calculate_qiusuo_index(equity, ts=now, state=state)
                if qiusuo_index is None:
                    raise RuntimeError('计算求索指数失败')

                bucket_ts = self._equity_tick_bucket(now).isoformat(timespec='minutes')
                recorded_at = now.isoformat(timespec='seconds')
                ticks = self._trim_equity_ticks(self.load_equity_ticks())

                if ticks and ticks[-1]['timestamp'] == bucket_ts:
                    ticks[-1]['equity'] = equity
                    ticks[-1]['recorded_at'] = recorded_at
                    ticks[-1]['qiusuo_index'] = qiusuo_index
                else:
                    ticks.append({
                        'timestamp': bucket_ts,
                        'recorded_at': recorded_at,
                        'equity': equity,
                        'qiusuo_index': qiusuo_index
                    })

                if not self.save_equity_ticks(ticks):
                    raise RuntimeError('保存权益采样失败')
            return True
        except Exception as e:
            logger.error(f"记录权益采样失败: {e}")
            return False

    def build_qiusuo_index_ohlc(self, days=None):
        """聚合求索指数日线。days<=0 表示「全部」（完整历史）。

        历史交易日：每日切日时已把 5 分钟采样压缩成日线 OHLC（带高低影线）永久保存；
        当前交易日：用当天的 5 分钟采样实时合成。两者拼成完整历史。
        """
        if days is None:
            days = self.EQUITY_OHLC_DEFAULT_DAYS
        show_all = days <= 0
        state = self.ensure_qiusuo_index_state(persist=False)
        ticks = self._trim_equity_ticks(self.load_equity_ticks())
        candles_by_day = {}

        # 1) 每日快照铺底（永久保留）：已压缩的历史日带真 OHLC，旧的单点记录则退化为一字线
        for snap in self.load_daily_equity():
            day_key = snap.get('date')
            if not day_key:
                continue
            close = _coerce_positive_float(snap.get('close') or snap.get('qiusuo_index') or snap.get('index'))
            if close is None:
                snap_ts = self._qiusuo_day_timestamp(day_key) or datetime.now()
                close = self.calculate_qiusuo_index(snap.get('equity'), ts=snap_ts, state=state)
            if close is None:
                continue
            o = _coerce_positive_float(snap.get('open')) or close
            hi = _coerce_positive_float(snap.get('high')) or max(o, close)
            lo = _coerce_positive_float(snap.get('low')) or min(o, close)
            candles_by_day[day_key] = {
                'date': day_key, 'open': o, 'high': hi, 'low': lo, 'close': close,
                'samples': snap.get('samples', 1), 'source': 'daily'
            }

        # 2) 日内采样覆盖/细化最近 N 天（有日内高低，更准）
        for item in ticks:
            ts = _parse_equity_tick_timestamp(item['timestamp'])
            if ts is None:
                continue
            recorded_at = _parse_equity_tick_timestamp(item.get('recorded_at')) or ts
            index_value = _coerce_positive_float(item.get('qiusuo_index')) or self.calculate_qiusuo_index(item.get('equity'), ts=recorded_at, state=state)
            if index_value is None:
                continue
            day_key = self._qiusuo_trading_day(ts)
            candle = candles_by_day.get(day_key)
            if candle is None or candle.get('source') == 'daily':
                # 该日首个采样：用采样重置（覆盖每日快照单点）
                candles_by_day[day_key] = {
                    'date': day_key, 'open': index_value, 'high': index_value,
                    'low': index_value, 'close': index_value, 'samples': 1, 'source': 'ticks'
                }
                continue
            candle['high'] = max(candle['high'], index_value)
            candle['low'] = min(candle['low'], index_value)
            candle['close'] = index_value
            candle['samples'] += 1

        candles = sorted(candles_by_day.values(), key=lambda item: item['date'])

        if not show_all:
            cutoff_day = date.fromisoformat(self._qiusuo_trading_day(datetime.now() - timedelta(days=max(days - 1, 0))))
            kept = []
            for item in candles:
                try:
                    if date.fromisoformat(item['date']) >= cutoff_day:
                        kept.append(item)
                except Exception as exc:
                    logger.debug(
                        '求索指数 OHLC 跳过坏日期项 %r: %s',
                        item.get('date'), exc)
                    continue
            candles = kept

        latest_tick_time = ticks[-1].get('recorded_at') or ticks[-1]['timestamp'] if ticks else None
        latest_bucket_time = ticks[-1]['timestamp'] if ticks else None
        tick_start_date = candles[0]['date'] if candles else None

        return {
            'candles': candles,
            'base_index': state.get('base_index') or self.QIUSUO_INDEX_BASE,
            'current_divisor': state.get('current_divisor'),
            'rollover_hour': self.QIUSUO_INDEX_ROLLOVER_HOUR,
            'sample_interval_minutes': self.EQUITY_TICK_INTERVAL_MINUTES,
            'latest_tick_time': latest_tick_time,
            'latest_bucket_time': latest_bucket_time,
            'raw_retention_days': self.EQUITY_TICK_RETENTION_DAYS,
            'tick_start_date': tick_start_date,
            'show_all': show_all,
        }

    def record_daily_equity_snapshot(self):
        """记录每日收盘权益；同一交易日首写后不可被重试/手动检查覆盖。"""
        try:
            balance = self.system.exchange_api.get_balance()
            if not balance:
                return
            equity = _coerce_positive_float(balance['total'].get('USDT', 0))
            if equity is None:
                return
            now = datetime.now()
            day_key = self._qiusuo_trading_day(now)
            with self._lock:
                snapshots = self.load_daily_equity()
                existing = next((s for s in snapshots if s.get('date') == day_key), None)
                if existing:
                    close_equity = _coerce_positive_float(existing.get('equity'))
                    if close_equity is None:
                        close_equity = equity
                    close_time = self._qiusuo_day_timestamp(day_key) or now
                    state = self.ensure_qiusuo_index_state(
                        current_equity=close_equity, now=close_time, persist=True)
                    qiusuo_index = _coerce_positive_float(
                        existing.get('qiusuo_index') or existing.get('index'))
                    if qiusuo_index is None:
                        qiusuo_index = self.calculate_qiusuo_index(
                            close_equity, ts=close_time, state=state)
                    # 兼容缺字段的旧快照，但绝不覆盖已有有效收盘数值。
                    existing['equity'] = close_equity
                    existing['qiusuo_index'] = qiusuo_index
                else:
                    close_equity = equity
                    close_time = self._qiusuo_day_timestamp(day_key) or now
                    state = self.ensure_qiusuo_index_state(
                        current_equity=close_equity, now=close_time, persist=True)
                    qiusuo_index = self.calculate_qiusuo_index(
                        close_equity, ts=close_time, state=state)
                    snapshots.append({
                        'date': day_key,
                        'equity': close_equity,
                        'qiusuo_index': qiusuo_index,
                    })
                if not self.save_daily_equity(snapshots):
                    raise RuntimeError('保存每日权益快照失败')
                # 「未创新高/回撤」峰值按日收盘高水位推进的唯一节拍：用当日收盘权益
                # 结算并推进峰值。日快照已落盘，峰值/最长回撤周期推进失败不回滚快照，
                # 只告警并等下一次日检重试（避免把日内浮盈噪声 ratchet 成新高）。
                try:
                    self.reconcile_peak_equity(
                        close_equity,
                        persist=True,
                        now=close_time,
                        daily_close=True,
                    )
                except Exception as peak_exc:
                    logger.warning(
                        f"每日收盘高水位推进失败（日快照已存，下次重试）: {peak_exc}")
            logger.info(
                f"每日权益快照已记录: {day_key} = {close_equity:.2f} USDT / "
                f"求索指数 {qiusuo_index:.2f}")
            self._compact_closed_ticks(now)
        except Exception as e:
            logger.error(f"记录权益快照失败: {e}")

    def _compact_closed_ticks(self, now=None):
        """把已收盘交易日的 5 分钟采样压缩成日线 OHLC 写入 daily，并从 ticks 中删除，只保留当前交易日。

        每日切日时调用：历史从此都是带影线的真 K 线，equity_ticks.json 永远只存当天，空间极小。
        """
        try:
            now = now or datetime.now()
            current_day = self._qiusuo_trading_day(now)
            with self._lock:
                # 锁必须覆盖“首次读取 ticks → 两个文件提交”。否则并行的 5 分钟
                # 采样可在旧快照读取后追加，随后被 kept_ticks 的旧列表覆盖删除。
                ticks = self.load_equity_ticks()
                if not ticks:
                    return

                by_day = {}
                for item in ticks:
                    ts = _parse_equity_tick_timestamp(item.get('timestamp'))
                    if ts is None:
                        continue
                    by_day.setdefault(self._qiusuo_trading_day(ts), []).append(item)

                closed_days = [d for d in by_day if d < current_day]
                if not closed_days:
                    return

                daily = self.load_daily_equity()
                daily_by_date = {s['date']: s for s in daily if s.get('date')}
                for d in sorted(closed_days):
                    idxs = [_coerce_positive_float(it.get('qiusuo_index')) for it in by_day[d]]
                    idxs = [v for v in idxs if v is not None]
                    if not idxs:
                        continue
                    rec = daily_by_date.get(d) or {'date': d}
                    rec.update({
                        'equity': by_day[d][-1].get('equity'),
                        'qiusuo_index': idxs[-1],
                        'open': idxs[0], 'high': max(idxs), 'low': min(idxs), 'close': idxs[-1],
                        'samples': len(idxs),
                    })
                    daily_by_date[d] = rec

                new_daily = sorted(daily_by_date.values(), key=lambda x: x['date'])
                kept_ticks = []
                for item in ticks:
                    ts = _parse_equity_tick_timestamp(item.get('timestamp'))
                    if ts is not None and self._qiusuo_trading_day(ts) >= current_day:
                        kept_ticks.append(item)

                # 先持久化 OHLC，成功后再删原始采样，避免丢数据
                if self.save_daily_equity(new_daily):
                    self.save_equity_ticks(kept_ticks)
                    logger.info(f"已压缩 {len(closed_days)} 个收盘交易日为日线 OHLC，日内采样保留 {len(kept_ticks)} 条")
        except Exception as e:
            logger.warning(f"压缩日内采样失败（不影响交易）: {e}")

    def equity_sync(self, flow_amount=None):
        """入金/出金后同步权益基准，尽量保持求索指数连续。返回结果 dict。

        flow_amount（净变动金额：入金为正、出金为负）提供时，锚点指数按
        「(当前权益 − 净流入) ÷ 旧除数」精确反推——即使点击时距资金到账已隔了
        若干个采样周期（期间 5 分钟 tick 已用旧除数把入金误记成一根"盈利"），
        指数水平也会被校正回真实盈亏轨迹，同步不再有时效压力。
        不提供时维持旧行为：以最近一次已记录的指数值为锚——须在资金到账后的
        第一个采样周期内（5 分钟）尽快点击，否则被污染的采样点会成为锚。
        """
        balance = self.system.exchange_api.get_balance()
        if not balance:
            raise RuntimeError('获取账户余额失败')
        current_equity = _coerce_positive_float(balance['total'].get('USDT', 0))
        if current_equity is None:
            raise ValueError('当前权益为0，无法同步')

        now = datetime.now()
        with self._lock:
            old_peak = self.load_peak_equity()
            old_history = self.load_equity_history()
            old_qiusuo = self.load_qiusuo_index_state()
            qiusuo_state = self.ensure_qiusuo_index_state(
                current_equity=current_equity, now=now, persist=False)
            old_divisor = qiusuo_state.get('current_divisor') or (current_equity / (qiusuo_state.get('base_index') or self.QIUSUO_INDEX_BASE))
            if flow_amount is not None:
                # 状态边界 fail-closed：nan/inf 会写出 nan/0.0 的除数污染求索指数，
                # 不依赖调用方（API 已挡一层，这里是最后防线）
                flow_amount = cfgv.strict_float_finite(flow_amount, '净变动金额')
                pre_flow_equity = current_equity - flow_amount
                if pre_flow_equity <= 0:
                    raise ValueError(
                        f'净变动金额不合理：当前权益 {current_equity:.2f}，净变动 {float(flow_amount):.2f}，'
                        f'反推的变动前权益不为正，请核对金额与正负号')
                qiusuo_anchor = pre_flow_equity / old_divisor
                anchor_time = now.isoformat(timespec='seconds')
            else:
                qiusuo_anchor, anchor_time = self._latest_qiusuo_index_anchor(qiusuo_state)
                qiusuo_anchor = _coerce_positive_float(qiusuo_anchor) or (qiusuo_state.get('base_index') or self.QIUSUO_INDEX_BASE)
            new_divisor = current_equity / qiusuo_anchor

            peak_data = {'peak_equity': current_equity, 'peak_time': now.isoformat()}

            eq_hist = copy.deepcopy(old_history)
            # or 0：全新系统 initial_equity 为 None（从未跑过统计刷新），
            # 原实现在下方 :.2f 格式化处直接抛 TypeError → 路由 500
            old_initial = eq_hist.get('initial_equity') or 0
            eq_hist['initial_equity'] = current_equity
            eq_hist['initial_time'] = now.isoformat()
            eq_hist['year_start_equity'] = current_equity
            eq_hist['year_start_time'] = now.isoformat()
            eq_hist['max_drawdown'] = 0
            eq_hist['max_dd_time'] = None
            eq_hist['longest_drawdown_days'] = 0
            eq_hist.pop('current_drawdown_start', None)  # 旧版遗留字段，顺带清掉
            qiusuo_history = list(qiusuo_state.get('history', []))
            qiusuo_history.append({
                'effective_from': now.isoformat(timespec='seconds'),
                'divisor': new_divisor,
                'anchor_equity': current_equity,
                'anchor_index': qiusuo_anchor,
                'reason': 'equity_sync'
            })
            qiusuo_state.update({
                'current_divisor': new_divisor,
                'anchor_equity': current_equity,
                'anchor_index': qiusuo_anchor,
                'anchor_time': now.isoformat(timespec='seconds'),
                'history': qiusuo_history
            })
            qiusuo_state = self._normalize_qiusuo_index_state(qiusuo_state)
            self._commit_equity_sync_generation(
                {
                    'peak': old_peak,
                    'history': old_history,
                    'qiusuo': old_qiusuo,
                },
                {
                    'peak': peak_data,
                    'history': eq_hist,
                    'qiusuo': qiusuo_state,
                })

            self.record_equity_tick(equity=current_equity, now=now)

        logger.info(f"权益基准已同步: {old_initial:.2f} -> {current_equity:.2f}"
                    f"{f'（净变动 {float(flow_amount):+.2f}，精确锚定）' if flow_amount is not None else '（按最近指数锚定）'}")
        return {
            'old_initial': old_initial,
            'new_initial': current_equity,
            'flow_amount': float(flow_amount) if flow_amount is not None else None,
            'qiusuo_index': qiusuo_anchor,
            'old_divisor': old_divisor,
            'new_divisor': new_divisor,
            'anchor_time': anchor_time,
            'sync_time': now.isoformat()
        }
