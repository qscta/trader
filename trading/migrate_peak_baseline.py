#!/usr/bin/env python3
"""一次性把日内浮盈污染的峰值纠正为日收盘高水位。

默认只分析；加 ``--apply`` 才会在备份原文件后写入。迁移以最近一次
``equity_sync`` 重置的 ``equity_history.initial_*`` 为基准，只采用该时刻
之后的日收盘，避免把同一交易日中、资金同步之前的旧快照带入新基线。
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trade_state import atomic_write_json, open_private_text_file


ROLLOVER_HOUR = 8


def _pos_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if (
        value == value
        and value not in (float('inf'), float('-inf'))
        and value > 0
    ) else None


def _parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(
            timezone(timedelta(hours=8))).replace(tzinfo=None)
    return parsed


def _trading_day(dt):
    return (dt - timedelta(hours=ROLLOVER_HOUR)).date()


def _day_close_ts(day_str):
    return datetime.strptime(
        f'{day_str}T{ROLLOVER_HOUR:02d}:00:00',
        '%Y-%m-%dT%H:%M:%S',
    )


def _snapshot_close_ts(snapshot, day_str):
    """返回 daily 记录代表的真实收盘边界。

    刚在 08:00 建立的单点快照以当天日期标记；次日压缩后，同一记录会带
    OHLC/samples，此时 ``date=D`` 代表 D 交易日（D 08:00 至 D+1 08:00），
    收盘边界应为 D+1 08:00。
    """
    boundary = _day_close_ts(day_str)
    is_compacted = (
        _pos_float(snapshot.get('samples')) is not None
        or all(key in snapshot for key in ('open', 'high', 'low', 'close'))
    )
    return boundary + timedelta(days=1) if is_compacted else boundary


def recompute_daily_close_peak(peak_data, eq_hist, daily_snapshots):
    """按最近资金基准及其后的日收盘重算峰值；数据不足时返回 ``None``。"""
    stored_eq = _pos_float((peak_data or {}).get('peak_equity'))
    if stored_eq is None:
        return None

    candidates = []  # (equity, timestamp, source)
    baseline_eq = _pos_float((eq_hist or {}).get('initial_equity'))
    baseline_time = _parse_ts((eq_hist or {}).get('initial_time'))
    # 没有最近资金同步基准就无法区分入金前后的世代；宁可拒绝迁移，也不能
    # 仅凭日快照猜测并向下改写真实资金基线。
    if baseline_eq is None or baseline_time is None:
        return None
    candidates.append((baseline_eq, baseline_time, 'baseline'))

    observed_days = []
    for snap in daily_snapshots or []:
        if not isinstance(snap, dict):
            continue
        day_str = snap.get('date')
        eq = _pos_float(snap.get('equity'))
        if not isinstance(day_str, str) or eq is None:
            continue
        try:
            date.fromisoformat(day_str)  # 严格校验日期；真实边界由记录形态决定。
            close_time = _snapshot_close_ts(snap, day_str)
        except ValueError:
            continue
        # 关键边界：资金同步可能发生在 20:29，而同日 08:00 快照属于旧基线。
        # 只比较日期会错误保留这份同步前快照，必须按完整时间排除。
        if close_time < baseline_time:
            continue
        candidates.append((eq, close_time, f'daily:{day_str}'))
        observed_days.append(_trading_day(close_time))

    if not candidates:
        return None

    best_eq, best_time, best_source = max(candidates, key=lambda item: item[0])
    observed_day = (
        max(observed_days)
        if observed_days
        else _trading_day(baseline_time or best_time)
    )
    return {
        'stored_peak_equity': stored_eq,
        'stored_peak_time': (peak_data or {}).get('peak_time'),
        'recomputed_peak_equity': best_eq,
        'recomputed_peak_time': best_time.isoformat(timespec='seconds'),
        # 这是“最近已消费的日收盘”，不是“峰值发生日”。峰值可能来自更早的基准。
        'recomputed_peak_observed_day': observed_day.isoformat(),
        'source': best_source,
    }


def _load_json(path):
    if not os.path.lexists(path):
        return None
    with open_private_text_file(path) as handle:
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
        peak_data,
        _load_json(hist_path) or {},
        _load_json(daily_path) or [],
    )
    if analysis is None:
        print('[跳过] 数据不足以判定应有峰值（缺基准与日收盘），不做改动。')
        return 0

    stored = analysis['stored_peak_equity']
    correct = analysis['recomputed_peak_equity']
    print('—— 峰值口径迁移分析 ——')
    print(f"  落盘峰值:   {stored:.4f} @ {analysis['stored_peak_time']}")
    print(
        f"  应有峰值:   {correct:.4f} @ {analysis['recomputed_peak_time']}  "
        f"(来源 {analysis['source']})")

    tolerance = max(1e-9, abs(stored) * 1e-9)
    if correct >= stored - tolerance:
        print('  判定: 落盘峰值未高于日收盘高水位，无需向下纠正。')
        return 0

    corrected = {
        'peak_equity': correct,
        'peak_time': analysis['recomputed_peak_time'],
        'peak_observed_day': analysis['recomputed_peak_observed_day'],
    }
    print(
        '  判定: 落盘峰值被日内浮盈污染'
        f'（高于日收盘高水位 {stored - correct:.4f}），应向下纠正。')

    if not apply:
        print('  这是干跑；确认无误后加 --apply 执行（会先备份再写入）。')
        return 0

    backup = f'{peak_path}.premigrate.{datetime.now().strftime("%Y%m%d_%H%M%S")}'
    if not atomic_write_json(backup, peak_data):
        print(f'  [失败] 备份原峰值失败，未改动: {backup}')
        return 1
    if not atomic_write_json(peak_path, corrected):
        print(f'  [失败] 写入主峰值失败；请从备份恢复: {backup}')
        return 1
    if not atomic_write_json(peak_path + '.bak', corrected):
        print(f'  [失败] 主峰值已纠正但备份刷新失败；原值仍保存在: {backup}')
        return 1
    print(f'  [完成] 已备份到 {backup} 并写入纠正峰值。')
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='把被日内浮盈污染的峰值纠正回日收盘高水位口径')
    parser.add_argument(
        '--data-dir',
        default=os.path.dirname(os.path.abspath(__file__)),
        help='状态文件目录（默认脚本所在目录）',
    )
    parser.add_argument('--apply', action='store_true', help='实际写入（否则仅干跑分析）')
    args = parser.parse_args()
    return run(args.data_dir, args.apply)


if __name__ == '__main__':
    sys.exit(main())
