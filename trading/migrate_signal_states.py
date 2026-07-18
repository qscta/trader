#!/usr/bin/env python3
"""一次性清除账本 signal_states 里的海龟时代死字段。

海龟策略下线后，代码不再读写 ``mid_line_crossed`` / ``signal_execution``，
但 ``mark_candle_processed`` 用 setdefault 逐键更新，旧账本里已经写入的这两个
字段会永久滞留。本脚本把它们从 ``signal_states`` 的每个品种记录中删除，
其余字段（``last_processed_candle`` / ``strategy`` / ``last_update``）原样保留。

默认只干跑分析；加 ``--apply`` 才会在备份原文件后写入。主账本与 ``.bak``
副本都会清理（各自留一份带时间戳的 ``.premigrate`` 备份），避免主文件损坏
后从仍带死字段的备份恢复。清理是纯删键、幂等——第二次运行不再改动。
"""

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trade_state import atomic_write_json, open_private_text_file

# 海龟时代遗留、当前代码零读写的死字段（见 trade_state 校验注释）。
DEAD_FIELDS = ('mid_line_crossed', 'signal_execution')


def strip_dead_fields(state):
    """从 signal_states 每个品种记录里删除死字段。

    返回 (清理后的 state, 每品种删除了哪些字段的报告 dict)。原对象不被修改。
    """
    report = {}
    signal_states = state.get('signal_states')
    if not isinstance(signal_states, dict):
        return state, report
    new_signal_states = {}
    for symbol, record in signal_states.items():
        if not isinstance(record, dict):
            new_signal_states[symbol] = record  # 非对象记录不属本脚本职责，原样保留
            continue
        removed = [field for field in DEAD_FIELDS if field in record]
        if removed:
            record = {k: v for k, v in record.items() if k not in DEAD_FIELDS}
            report[symbol] = removed
        new_signal_states[symbol] = record
    new_state = dict(state)
    new_state['signal_states'] = new_signal_states
    return new_state, report


def _load_json(path):
    if not os.path.lexists(path):
        return None
    with open_private_text_file(path) as handle:
        return json.load(handle)


def _migrate_file(path, apply):
    """清理单个账本文件。返回 (改动了吗, 遇到需处理的记录数)。"""
    data = _load_json(path)
    if not isinstance(data, dict):
        print(f'[跳过] 未找到有效账本: {path}')
        return False, 0

    cleaned, report = strip_dead_fields(data)
    if not report:
        print(f'[干净] {path}：signal_states 无死字段，无需改动。')
        return False, 0

    print(f'—— {path} ——')
    for symbol in sorted(report):
        print(f'  {symbol}: 将删除 {", ".join(report[symbol])}')

    if not apply:
        print('  这是干跑；确认无误后加 --apply 执行（会先备份再写入）。')
        return False, len(report)

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = f'{path}.premigrate.{stamp}'
    if not atomic_write_json(backup, data):
        print(f'  [失败] 备份原账本失败，未改动: {backup}')
        return False, len(report)
    if not atomic_write_json(path, cleaned):
        print(f'  [失败] 写入清理后账本失败；请从备份恢复: {backup}')
        return False, len(report)
    print(f'  [完成] 已备份到 {backup} 并写入清理后账本。')
    return True, len(report)


def run(data_dir, apply):
    main_path = os.path.join(data_dir, 'trade_state.json')
    bak_path = main_path + '.bak'

    if not os.path.lexists(main_path) and not os.path.lexists(bak_path):
        print(f'[跳过] 未找到 trade_state.json 或其 .bak: {data_dir}')
        return 0

    # 主账本与 .bak 各自独立清理：.bak 是上次保存前的副本，可能仍带死字段，
    # 若只清主文件，主文件一旦损坏就会从带死字段的 .bak 恢复回来。
    for path in (main_path, bak_path):
        if os.path.lexists(path):
            _migrate_file(path, apply)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='清除账本 signal_states 里的海龟死字段 '
                    '(mid_line_crossed / signal_execution)')
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
