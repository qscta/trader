#!/usr/bin/env python3
"""一次性状态迁移：把被日内浮盈污染的 peak_equity.json 纠正回「日收盘高水位」口径。

背景
----
历史实现每 5 分钟按市值总权益（含未实现盈亏）推进峰值，日内浮盈冒一个高点
就把 `peak_time` 刷成当时刻——`days_since_peak`（未创新高天数）因此被反复清零，
`peak_drawdown` 也被虚高的峰值放大。新代码已把峰值改为「每交易日按收盘推进
一次」，但**已经落盘的错误峰值不会被新代码自动纠正**，需要本脚本做一次迁移。

口径
----
应有峰值 = 「回撤基准」与「基准之后的各日收盘」的最高者：
  - 回撤基准 = equity_history.initial_equity @ initial_time
    （资金同步 equity_sync 会把它重置为同步时权益，并清零回撤统计）；
  - 各日收盘 = daily_equity.json 中日期 ≥ 基准交易日的 equity。
本脚本**只向下纠正**（当落盘峰值高于上述高水位时才改），绝不抬高峰值——
抬高会掩盖真实回撤。向下纠正只会让未创新高天数/回撤更保守、更真实。

用法
----
  python3 migrate_peak_baseline.py            # 干跑：只分析并打印，不改任何文件
  python3 migrate_peak_baseline.py --apply     # 备份后写入纠正值
  python3 migrate_peak_baseline.py --data-dir /path/to/state --apply
默认 data-dir 为本脚本所在目录（状态文件通常与代码同目录的项目根）。
幂等：已纠正过再跑一次会判定「无需纠正」。
"""

import argparse
import json
import os
import sys
from datetime import datetime, date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trade_state import atomic_write_json  # 复用命脉级原子写（含 0600 + fsync）

ROLLOVER_HOUR = 8  # 与 EquityTracker.QIUSUO_INDEX_ROLLOVER_HOUR 一致


def _pos_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if (value == value and value not in (float('inf'), float('-inf')) and value > 0) else None


def _parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _trading_day(dt):
    return (dt - timedelta(hours=ROLLOVER_HOUR)).date()


def _day_close_ts(day_str):
    return datetime.strptime(f'{day_str}T{ROLLOVER_HOUR:02d}:00:00', '%Y-%m-%dT%H:%M:%S')


def recompute_daily_close_peak(peak_data, eq_hist, daily_snapshots):
    """按日收盘高水位口径重算应有峰值。返回分析 dict，或 None（无法判定）。

    纯函数，便于单测：不读文件、不写文件。
    """
    stored_eq = _pos_float((peak_data or {}).get('peak_equity'))
    if stored_eq is None:
        return None

    candidates = []  # (equity, timestamp, source)
    baseline_eq = _pos_float((eq_hist or {}).get('initial_equity'))
    baseline_time = _parse_ts((eq_hist or {}).get('initial_time'))
    baseline_day = _trading_day(baseline_time) if baseline_time else None
    if baseline_eq is not None and baseline_time is not None:
        candidates.append((baseline_eq, baseline_time, 'baseline'))

    for snap in daily_snapshots or []:
        if not isinstance(snap, dict):
            continue
        day_str = snap.get('date')
        eq = _pos_float(snap.get('equity'))
        if not isinstance(day_str, str) or eq is None:
            continue
        try:
            snap_day = date.fromisoformat(day_str)
        except ValueError:
            continue
        # 同步/基准之前的旧收盘不计入（equity_sync 已重置回撤基准）。
        if baseline_day is not None and snap_day < baseline_day:
            continue
        candidates.append((eq, _day_close_ts(day_str), f'daily:{day_str}'))

    if not candidates:
        return None

    best_eq, best_time, best_source = max(candidates, key=lambda c: c[0])
    return {
        'stored_peak_equity': stored_eq,
        'stored_peak_time': (peak_data or {}).get('peak_time'),
        'recomputed_peak_equity': best_eq,
        'recomputed_peak_time': best_time.isoformat(timespec='seconds'),
        'recomputed_peak_advanced_day': _trading_day(best_time).isoformat(),
        'source': best_source,
    }


def _load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def run(data_dir, apply):
    peak_path = os.path.join(data_dir, 'peak_equity.json')
    hist_path = os.path.join(data_dir, 'equity_history.json')
    daily_path = os.path.join(data_dir, 'daily_equity.json')

    peak_data = _load_json(peak_path)
    if not isinstance(peak_data, dict):
        print(f'[跳过] 未找到有效 peak_equity.json: {peak_path}')
        return 0

    analysis = recompute_daily_close_peak(
        peak_data, _load_json(hist_path) or {}, _load_json(daily_path) or [])
    if analysis is None:
        print('[跳过] 数据不足以判定应有峰值（缺基准与日收盘），不做改动。')
        return 0

    stored = analysis['stored_peak_equity']
    correct = analysis['recomputed_peak_equity']
    print('—— 峰值口径迁移分析 ——')
    print(f"  落盘峰值:   {stored:.4f} @ {analysis['stored_peak_time']}")
    print(f"  应有峰值:   {correct:.4f} @ {analysis['recomputed_peak_time']}  (来源 {analysis['source']})")

    if correct >= stored - max(1e-9, abs(stored) * 1e-9):
        print('  判定: 落盘峰值未高于日收盘高水位，无需纠正（或将由日检自然推进）。')
        return 0

    corrected = {
        'peak_equity': correct,
        'peak_time': analysis['recomputed_peak_time'],
        'peak_advanced_day': analysis['recomputed_peak_advanced_day'],
    }
    print(f'  判定: 落盘峰值被日内浮盈污染（高于日收盘高水位 {stored - correct:.4f}），应向下纠正。')

    if not apply:
        print('  这是干跑；确认无误后加 --apply 执行（会先备份再写入）。')
        return 0

    backup = f'{peak_path}.premigrate.{datetime.now().strftime("%Y%m%d_%H%M%S")}'
    if not atomic_write_json(backup, peak_data):
        print(f'  [失败] 备份原峰值失败，未改动: {backup}')
        return 1
    if not (atomic_write_json(peak_path, corrected) and
            atomic_write_json(peak_path + '.bak', corrected)):
        print('  [失败] 写入纠正峰值失败；原文件请从备份恢复:', backup)
        return 1
    print(f'  [完成] 已备份到 {backup} 并写入纠正峰值。')
    return 0


def main():
    parser = argparse.ArgumentParser(description='把被日内浮盈污染的峰值纠正回日收盘高水位口径')
    parser.add_argument('--data-dir', default=os.path.dirname(os.path.abspath(__file__)),
                        help='状态文件目录（默认脚本所在目录）')
    parser.add_argument('--apply', action='store_true', help='实际写入（否则仅干跑分析）')
    args = parser.parse_args()
    return run(args.data_dir, args.apply)


if __name__ == '__main__':
    sys.exit(main())
