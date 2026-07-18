#!/usr/bin/env python3
"""单策略版本部署预检与状态规范化。

默认只分析，不改文件；加 ``--apply`` 后才会先备份全部待改文件，再原子写入。
任何未结束的开仓意图、不兼容的在途持仓或损坏数据都会阻断部署，绝不猜测。
"""

import argparse
import copy
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trade_state import (TradeState, atomic_write_json, open_private_text_file,
                         private_file_exists)
from config_validation import MA_PARAMETER_FIELDS, SYMBOL_CONFIG_FIELDS

STRATEGY_ID = 'ma_cross'
SIGNAL_FIELDS = frozenset({'last_processed_candle', 'last_update'})
ARCHIVE_NAME_RE = re.compile(r'^closed_trades_archive(?:_\d{4})?\.json$')

EXIT_OK = 0
EXIT_UNSAFE = 2
EXIT_BACKUP_FAILED = 3
EXIT_WRITE_FAILED = 4
EXIT_RESTORE_FAILED = 5


def _load_json(path):
    with open_private_text_file(path) as handle:
        return json.load(
            handle,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f'不允许的 JSON 数值常量: {value}')))


def normalize_config(config):
    """移除品种级冗余分派字段；不兼容值作为部署阻断返回。"""
    cleaned = copy.deepcopy(config)
    report = []
    blockers = []
    if not isinstance(cleaned, dict):
        return cleaned, report, ['配置顶层必须是对象']
    trading = cleaned.get('trading')
    symbols = trading.get('symbols') if isinstance(trading, dict) else None
    if not isinstance(symbols, list):
        return cleaned, report, ['config.trading.symbols 必须是数组']
    params = cleaned.get('strategy')
    if not isinstance(params, dict):
        blockers.append('config.strategy 必须是对象')
    else:
        unknown_params = sorted(set(params) - MA_PARAMETER_FIELDS)
        if unknown_params:
            blockers.append(
                f'config.strategy 含未知字段: {unknown_params}；请人工核对并删除')
    for index, symbol in enumerate(symbols):
        if not isinstance(symbol, dict):
            blockers.append(f'config.trading.symbols[{index}] 必须是对象')
            continue
        unknown_fields = sorted(
            set(symbol) - SYMBOL_CONFIG_FIELDS - {'strategy'})
        if unknown_fields:
            blockers.append(
                f'config.trading.symbols[{index}] 含未知字段: {unknown_fields}')
        if 'strategy' not in symbol:
            continue
        label = symbol.get('strategy')
        name = symbol.get('name') or f'索引 {index}'
        if label not in (None, STRATEGY_ID):
            blockers.append(
                f'{name} 含不兼容策略标签；请人工确认该品种已无未结束生命周期')
            continue
        del symbol['strategy']
        report.append(f'{name}: 删除冗余 strategy 配置字段')
    return cleaned, report, blockers


def normalize_ledger(state):
    """收敛信号元数据，并拒绝自动解释任何在途真钱生命周期。"""
    cleaned = copy.deepcopy(state)
    report = []
    blockers = []
    if not isinstance(cleaned, dict):
        return cleaned, report, ['账本顶层必须是对象']

    intents = cleaned.get('open_intents', {})
    if not isinstance(intents, dict):
        blockers.append('open_intents 必须是对象')
    elif intents:
        blockers.extend(
            f'{symbol}: 存在未收口开仓意图，禁止迁移和部署'
            for symbol in sorted(intents))

    positions = cleaned.get('open_positions')
    if not isinstance(positions, dict):
        blockers.append('open_positions 必须是对象')
    else:
        for symbol, position in positions.items():
            if not isinstance(position, dict):
                blockers.append(f'{symbol}: 持仓记录必须是对象')
                continue
            label = position.get('strategy')
            if label != STRATEGY_ID:
                blockers.append(
                    f'{symbol}: 在途持仓缺少可证明的双均线归属，必须人工裁决')

    signal_states = cleaned.get('signal_states', {})
    if not isinstance(signal_states, dict):
        blockers.append('signal_states 必须是对象')
    else:
        normalized_signals = {}
        for symbol, record in signal_states.items():
            if not isinstance(record, dict):
                blockers.append(f'{symbol}: signal_states 记录必须是对象')
                continue
            label = record.get('strategy')
            if label not in (None, STRATEGY_ID):
                report.append(f'{symbol}: 丢弃不兼容信号标记并在下次日检重建基线')
                continue
            normalized = {
                key: copy.deepcopy(value)
                for key, value in record.items() if key in SIGNAL_FIELDS
            }
            if normalized != record:
                report.append(f'{symbol}: 信号元数据收敛到单策略字段集')
            if normalized:
                normalized_signals[symbol] = normalized
        cleaned['signal_states'] = normalized_signals

    closed = cleaned.get('closed_trades')
    if isinstance(closed, list):
        for index, trade in enumerate(closed):
            if isinstance(trade, dict) and trade.get('strategy') not in (
                    None, STRATEGY_ID):
                del trade['strategy']
                report.append(f'closed_trades[{index}]: 删除不兼容历史标签')
    else:
        blockers.append('closed_trades 必须是数组')

    if not blockers:
        try:
            TradeState.validate_state(cleaned)
        except Exception as exc:
            blockers.append(f'规范化后账本校验失败: {exc}')
    return cleaned, report, blockers


def normalize_archive(records):
    """清除平仓史书中已不属于当前账本模型的历史分类标签。"""
    cleaned = copy.deepcopy(records)
    report = []
    blockers = []
    if not isinstance(cleaned, list):
        return cleaned, report, ['平仓史书顶层必须是数组']
    for index, trade in enumerate(cleaned):
        if not isinstance(trade, dict):
            blockers.append(f'平仓史书第 {index} 项必须是对象')
            continue
        if trade.get('strategy') not in (None, STRATEGY_ID):
            del trade['strategy']
            report.append(f'第 {index} 项: 删除不兼容历史标签')
    return cleaned, report, blockers


def _targets(data_dir, config_path):
    state_path = os.path.join(data_dir, 'trade_state.json')
    paths = [('config', config_path or os.path.join(data_dir, 'config.json')),
             ('ledger', state_path)]
    backup_state = state_path + '.bak'
    if private_file_exists(backup_state):
        paths.append(('ledger', backup_state))
    if os.path.isdir(data_dir):
        for name in sorted(os.listdir(data_dir)):
            if ARCHIVE_NAME_RE.fullmatch(name):
                paths.append(('archive', os.path.join(data_dir, name)))
    return paths


def run(data_dir, apply=False, config_path=None):
    targets = _targets(os.path.abspath(data_dir), config_path)
    required_missing = [path for _kind, path in targets[:2]
                        if not private_file_exists(path)]
    if required_missing:
        for path in required_missing:
            print(f'[阻断] 缺少部署必需文件: {path}')
        return EXIT_UNSAFE

    originals = {}
    normalized = {}
    reports = {}
    blockers = []
    for kind, path in targets:
        try:
            original = _load_json(path)
            if kind == 'config':
                result = normalize_config(original)
            elif kind == 'archive':
                result = normalize_archive(original)
            else:
                result = normalize_ledger(original)
            cleaned, report, target_blockers = result
            originals[path] = original
            normalized[path] = cleaned
            reports[path] = report
            blockers.extend(f'{path}: {item}' for item in target_blockers)
        except Exception as exc:
            blockers.append(f'{path}: 读取或解析失败: {exc}')

    if blockers:
        for item in blockers:
            print(f'[阻断] {item}')
        return EXIT_UNSAFE

    changed = [path for _kind, path in targets
               if originals[path] != normalized[path]]
    for path in changed:
        print(f'—— {path} ——')
        for item in reports[path]:
            print(f'  {item}')
    if not changed:
        print('[通过] 配置与账本已符合单策略模式。')
        return EXIT_OK
    if not apply:
        print('[干跑] 检查通过；加 --apply 才会备份并写入。')
        return EXIT_OK

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    for path in changed:
        backup = f'{path}.premigrate.{stamp}'
        if not atomic_write_json(backup, originals[path]):
            print(f'[失败] 备份失败，所有原文件均未改动: {backup}')
            return EXIT_BACKUP_FAILED

    written = []
    for path in changed:
        if atomic_write_json(path, normalized[path]):
            written.append(path)
            continue
        print(f'[失败] 写入失败，开始恢复已写文件: {path}')
        restore_ok = True
        for written_path in reversed(written):
            restore_ok = atomic_write_json(
                written_path, originals[written_path]) and restore_ok
        return EXIT_WRITE_FAILED if restore_ok else EXIT_RESTORE_FAILED

    print(f'[完成] 已原子规范化 {len(changed)} 个文件；再次运行应显示已通过。')
    return EXIT_OK


def main():
    parser = argparse.ArgumentParser(description='单策略部署预检与状态规范化')
    parser.add_argument('--data-dir', required=True, help='trade_state.json 所在目录')
    parser.add_argument('--config', help='config.json 路径；默认与账本同目录')
    parser.add_argument('--apply', action='store_true', help='备份后实际写入')
    args = parser.parse_args()
    return run(args.data_dir, apply=args.apply, config_path=args.config)


if __name__ == '__main__':
    sys.exit(main())
