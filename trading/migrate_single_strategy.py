#!/usr/bin/env python3
"""单策略版本部署预检与状态规范化。

默认只分析，不改文件；加 ``--apply`` 后才会先备份全部待改文件，再原子写入。
任何未结束的开仓意图、不兼容的在途持仓或损坏数据都会阻断部署，绝不猜测。
"""

import argparse
import copy
import importlib.util
import os
import stat
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trade_state import (AtomicWriteCommitDurabilityError, TradeState,
                         atomic_write_json,
                         is_closed_trade_archive_candidate,
                         is_closed_trade_archive_name, load_strict_json,
                         open_private_text_file, owner_auxiliary_state_paths,
                         private_file_exists, state_has_lifecycle_data,
                         validate_closed_trade_record,
                         validate_okx_owner_manifest)
import config_validation as cfgv

STRATEGY_ID = 'ma_cross'
SIGNAL_FIELDS = frozenset({'last_processed_candle', 'last_update'})

EXIT_OK = 0
EXIT_UNSAFE = 2
EXIT_BACKUP_FAILED = 3
EXIT_WRITE_FAILED = 4
EXIT_RESTORE_FAILED = 5


def _load_json(path):
    # 部署预检必须是观察者：权限异常直接阻断，不得在“干跑”中偷偷 chmod。
    with open_private_text_file(path, adjust_permissions=False) as handle:
        return load_strict_json(handle)


def normalize_config(config):
    """移除品种级冗余分派字段；不兼容值作为部署阻断返回。"""
    cleaned = copy.deepcopy(config)
    report = []
    blockers = []
    if not isinstance(cleaned, dict):
        return cleaned, report, ['配置顶层必须是对象']
    try:
        legacy_layout = cfgv.canonicalize_single_okx_config(cleaned)
    except (TypeError, ValueError) as exc:
        return cleaned, report, [str(exc)]
    if legacy_layout:
        report.append('配置布局由 exchanges.okx 收敛为顶层 okx')
    trading = cleaned.get('trading')
    symbols = trading.get('symbols') if isinstance(trading, dict) else None
    if not isinstance(symbols, list):
        return cleaned, report, ['config.trading.symbols 必须是数组']
    for index, symbol in enumerate(symbols):
        if not isinstance(symbol, dict):
            blockers.append(f'config.trading.symbols[{index}] 必须是对象')
            continue
        if 'strategy' not in symbol:
            continue
        label = symbol.get('strategy')
        name = symbol.get('name') or f'索引 {index}'
        if label not in (None, STRATEGY_ID):
            blockers.append(
                f'{name} 含不兼容策略标签；请人工确认该品种已无未结束生命周期')
        del symbol['strategy']
        report.append(f'{name}: 删除冗余 strategy 配置字段')
    try:
        cfgv.validate_and_normalize_execution_config(cleaned)
    except (TypeError, ValueError) as exc:
        blockers.append(str(exc))
    return cleaned, report, blockers


def normalize_ledger(state):
    """收敛信号元数据，并拒绝自动解释任何在途真钱生命周期。"""
    cleaned = copy.deepcopy(state)
    report = []
    blockers = []
    if not isinstance(cleaned, dict):
        return cleaned, report, ['账本顶层必须是对象']
    if 'last_check_time' in cleaned:
        del cleaned['last_check_time']
        report.append('删除 runtime 已不使用的 last_check_time')

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
            if position.get('close_intent') is not None:
                blockers.append(
                    f'{symbol}: 存在未收口 close_intent（包括未知/'
                    'single_order_v1），部署前必须人工收口')
            recovery_flags = any(
                position.get(field) is True for field in (
                    'recovered_partial_rollback',
                    'recovered_unresolved_open',
                    'recovered_open_overfill'))
            recovery_finalized = position.get(
                'execution_recovery_finalized')
            if recovery_finalized is False:
                blockers.append(
                    f'{symbol}: execution_recovery_finalized=false，'
                    '未决执行中间态禁止部署')
            elif recovery_flags and recovery_finalized is not True:
                blockers.append(
                    f'{symbol}: recovery 持仓缺少严格权威终态凭证，'
                    '禁止部署')

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
            execution = record.get('signal_execution')
            preserve_record = False
            if execution is not None:
                if not isinstance(execution, dict):
                    blockers.append(
                        f'{symbol}: signal_execution 结构异常，禁止自动删除')
                    preserve_record = True
                elif execution.get('status') == 'pending':
                    blockers.append(
                        f'{symbol}: 存在未收口 signal_execution pending，'
                        '禁止迁移和部署')
                    preserve_record = True
                elif execution.get('status') != 'confirmed':
                    blockers.append(
                        f'{symbol}: signal_execution 状态 '
                        f'{execution.get("status")!r} 无法证明已收口，禁止自动删除')
                    preserve_record = True
            if preserve_record:
                normalized_signals[symbol] = copy.deepcopy(record)
                continue
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
            if not isinstance(trade, dict):
                blockers.append(f'closed_trades[{index}] 必须是对象')
                continue
            if trade.get('strategy') not in (None, STRATEGY_ID):
                del trade['strategy']
                report.append(f'closed_trades[{index}]: 删除不兼容历史标签')
            try:
                if validate_closed_trade_record(
                        trade, f'closed_trades[{index}]', normalize=True):
                    report.append(
                        f'closed_trades[{index}]: 历史数值字段规范化')
            except ValueError as exc:
                blockers.append(str(exc))
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
        try:
            if validate_closed_trade_record(
                    trade, f'平仓史书第 {index} 项', normalize=True):
                report.append(f'第 {index} 项: 历史数值字段规范化')
        except ValueError as exc:
            blockers.append(str(exc))
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
            if is_closed_trade_archive_name(name):
                paths.append(('archive', os.path.join(data_dir, name)))
    return paths


def _validate_archive_names(data_dir):
    try:
        names = os.listdir(data_dir)
    except OSError as exc:
        return f'无法枚举平仓史书: {exc}'
    invalid = sorted(
        name for name in names
        if (is_closed_trade_archive_candidate(name) and
            not is_closed_trade_archive_name(name)))
    if invalid:
        return f'发现不受支持的平仓史书文件名: {invalid}'
    return None


def _fsync_directory(path):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        os.fsync(fd)
    finally:
        if fd is not None:
            os.close(fd)


def _remove_journal(path):
    os.unlink(path)
    _fsync_directory(os.path.dirname(path))


def _validate_data_dir(data_dir, config_path):
    try:
        info = os.lstat(data_dir)
    except OSError as exc:
        return f'数据目录不可访问: {data_dir}: {exc}'
    if not stat.S_ISDIR(info.st_mode):
        return f'数据目录必须是真实目录（拒绝符号链接）: {data_dir}'
    current_uid = os.geteuid() if hasattr(os, 'geteuid') else os.getuid()
    if info.st_uid != current_uid:
        return f'数据目录不属于当前用户，拒绝迁移: {data_dir}'
    if stat.S_IMODE(info.st_mode) & 0o022:
        return f'数据目录不得允许组/其他用户写入: {data_dir}'
    if os.path.dirname(config_path) != data_dir:
        return 'config.json 必须与 trade_state.json 位于同一真实数据目录'
    return None


def _validate_legacy_state_gate(data_dir):
    """旧状态只能由已完成标记证明收口；禁止在部署后由 runtime 再导入。"""
    legacy_dir = os.path.join(data_dir, 'data', 'okx')
    if not os.path.lexists(legacy_dir):
        return None
    try:
        info = os.lstat(legacy_dir)
    except OSError as exc:
        return f'无法检查旧状态目录 {legacy_dir}: {exc}'
    if not stat.S_ISDIR(info.st_mode):
        return f'旧状态目录必须是真实目录（拒绝符号链接）: {legacy_dir}'
    current_uid = os.geteuid() if hasattr(os, 'geteuid') else os.getuid()
    if info.st_uid != current_uid:
        return f'旧状态目录不属于当前用户: {legacy_dir}'
    marker = os.path.join(data_dir, '.okx_legacy_migration_complete.json')
    if private_file_exists(marker):
        try:
            payload = _load_json(marker)
        except Exception as exc:
            return f'旧状态迁移标记非法: {exc}'
        try:
            cfgv.validate_okx_legacy_migration_marker(payload)
        except ValueError as exc:
            return f'旧状态迁移标记内容非法: {exc}'
        return None
    try:
        entries = os.listdir(legacy_dir)
    except OSError as exc:
        return f'无法枚举旧状态目录 {legacy_dir}: {exc}'
    if entries:
        return (
            '检测到未收口的旧 data/okx 状态且缺少完成标记；'
            '禁止让 MA-only runtime 在部署后自动导入，请先人工裁决')
    return None


def _validate_owner_manifest(data_dir):
    """部署预检必须先证明 runtime 将读取的目录归属标记可安全启动。"""
    path = os.path.join(data_dir, '.trading_data_owner.json')
    if not private_file_exists(path):
        return None
    try:
        payload = _load_json(path)
    except Exception as exc:
        return f'数据目录归属标记非法: {exc}'
    try:
        validate_okx_owner_manifest(payload)
    except ValueError as exc:
        return f'{exc}: {path}'
    return None


def _directory_has_unowned_state(data_dir):
    """复用 startup 的路径集合与非空口径检查无 owner 的辅助状态。"""
    for path in owner_auxiliary_state_paths(data_dir):
        if not private_file_exists(path):
            continue
        payload = _load_json(path)
        if payload not in ({}, [], None):
            return True
    backup = os.path.join(data_dir, 'trade_state.json.bak')
    if private_file_exists(backup):
        backup_state = _load_json(backup)
        TradeState.validate_state(backup_state)
        if state_has_lifecycle_data(backup_state):
            return True
    return False


def _validate_journal_items(data_dir, journal):
    if (not isinstance(journal, dict) or
            type(journal.get('version')) is not int or
            journal['version'] != 1):
        raise ValueError('迁移事务日志版本或结构无效')
    items = journal.get('items')
    if not isinstance(items, list) or not items:
        raise ValueError('迁移事务日志缺少恢复条目')
    validated = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f'迁移事务日志第 {index} 项不是对象')
        target = os.path.abspath(str(item.get('target') or ''))
        backup = os.path.abspath(str(item.get('backup') or ''))
        if (os.path.dirname(target) != data_dir or
                os.path.dirname(backup) != data_dir or
                not backup.startswith(target + '.premigrate.')):
            raise ValueError(f'迁移事务日志第 {index} 项路径越界')
        validated.append((target, backup))
    return validated


def _recover_interrupted_migration(data_dir):
    """存在事务日志时从全套备份幂等恢复；成功后才删除日志。"""
    journal_path = os.path.join(
        data_dir, cfgv.SINGLE_STRATEGY_MIGRATION_JOURNAL)
    if not private_file_exists(journal_path):
        return True
    try:
        journal = _load_json(journal_path)
        items = _validate_journal_items(data_dir, journal)
        # 先读完全部备份，避免读到一半才发现备份损坏并开始制造新混合态。
        restore_payloads = [
            (target, _load_json(backup)) for target, backup in items]
        for target, payload in restore_payloads:
            if not atomic_write_json(target, payload):
                raise RuntimeError(f'恢复写入失败: {target}')
        _remove_journal(journal_path)
        print(f'[恢复] 已从 {len(items)} 份备份恢复上次中断的迁移事务。')
        return True
    except BaseException as exc:
        # KeyboardInterrupt/SystemExit 同样不得删除恢复凭据；下次运行仍可重试。
        print(f'[失败] 未完成迁移事务恢复失败，日志已保留: {exc}')
        return False


def _preview_confirmed_config_cleanup(config_path, cleanup_spec, release_sha):
    """只加载 release 内固定的受审清理实现，不接受外部可执行文件。"""
    helper_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        'remove-one-confirmed-config-key.py',
    )
    module_spec = importlib.util.spec_from_file_location(
        '_reviewed_confirmed_config_cleanup', helper_path)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError('无法加载 release 内的受审配置清理工具')
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module.preview_config(config_path, cleanup_spec, release_sha)


def run(data_dir, apply=False, config_path=None, cleanup_spec=None,
        release_sha=None, recover_only=False, classify_state=False):
    data_dir = os.path.abspath(data_dir)
    config_path = os.path.abspath(
        config_path or os.path.join(data_dir, 'config.json'))
    if recover_only and (apply or cleanup_spec or release_sha):
        print('[阻断] recover-only 只能单独用于回滚未完成迁移事务')
        return EXIT_UNSAFE
    if classify_state and (apply or cleanup_spec or release_sha or recover_only):
        print('[阻断] classify-state 只能单独用于只读数据状态分类')
        return EXIT_UNSAFE
    if bool(cleanup_spec) != bool(release_sha):
        print('[阻断] cleanup spec 预览必须同时提供 spec 与 release SHA')
        return EXIT_UNSAFE
    if apply and cleanup_spec:
        print('[阻断] cleanup spec 只允许用于只读干跑；apply 必须读已清理的实文件')
        return EXIT_UNSAFE
    if cleanup_spec:
        cleanup_spec = os.path.abspath(cleanup_spec)
    directory_error = _validate_data_dir(data_dir, config_path)
    if directory_error:
        print(f'[阻断] {directory_error}')
        return EXIT_UNSAFE
    journal_path = os.path.join(
        data_dir, cfgv.SINGLE_STRATEGY_MIGRATION_JOURNAL)
    if recover_only:
        if not private_file_exists(journal_path):
            print('[通过] 没有未完成的单策略迁移事务。')
            return EXIT_OK
        return (EXIT_OK if _recover_interrupted_migration(data_dir)
                else EXIT_RESTORE_FAILED)
    legacy_error = _validate_legacy_state_gate(data_dir)
    if legacy_error:
        print(f'[阻断] {legacy_error}')
        return EXIT_UNSAFE
    archive_name_error = _validate_archive_names(data_dir)
    if archive_name_error:
        print(f'[阻断] {archive_name_error}')
        return EXIT_UNSAFE
    owner_error = _validate_owner_manifest(data_dir)
    if owner_error:
        print(f'[阻断] {owner_error}')
        return EXIT_UNSAFE
    if private_file_exists(journal_path):
        if not apply:
            print(
                f'[阻断] 检测到未完成迁移事务: {journal_path}；'
                '干跑保持只读；请先单独运行 --recover-only 回滚事务，'
                '再重新 dry-run 审核，确需迁移时另行 --apply')
            return EXIT_UNSAFE
        if not _recover_interrupted_migration(data_dir):
            return EXIT_RESTORE_FAILED

    targets = _targets(data_dir, config_path)
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
            if kind == 'config':
                if cleanup_spec:
                    original = _preview_confirmed_config_cleanup(
                        path, cleanup_spec, release_sha)
                else:
                    original = _load_json(path)
                result = normalize_config(original)
            elif kind == 'archive':
                original = _load_json(path)
                result = normalize_archive(original)
            else:
                original = _load_json(path)
                result = normalize_ledger(original)
            cleaned, report, target_blockers = result
            originals[path] = original
            normalized[path] = cleaned
            reports[path] = report
            blockers.extend(f'{path}: {item}' for item in target_blockers)
        except Exception as exc:
            blockers.append(f'{path}: 读取或解析失败: {exc}')

    ledger_path = os.path.join(data_dir, 'trade_state.json')
    ledger = normalized.get(ledger_path)
    owner_manifest = os.path.join(data_dir, '.trading_data_owner.json')
    if isinstance(ledger, dict) and ledger.get('exchange') is None:
        try:
            unowned = (
                state_has_lifecycle_data(ledger) or
                _directory_has_unowned_state(data_dir))
        except Exception as exc:
            blockers.append(f'无归属辅助状态无法验证: {exc}')
        else:
            if unowned and not private_file_exists(owner_manifest):
                blockers.append(
                    '账本与目录均无 OKX 归属标记，但已存在生命周期/历史/权益状态；'
                    '拒绝由 startup 自动认领')

    if blockers:
        for item in blockers:
            print(f'[阻断] {item}')
        return EXIT_UNSAFE

    changed = [path for _kind, path in targets
               if originals[path] != normalized[path]]
    if classify_state:
        print('requires_migration' if changed else 'migration_complete')
        return EXIT_OK
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
    backups = {}
    for path in changed:
        backup = f'{path}.premigrate.{stamp}'
        try:
            backup_saved = atomic_write_json(backup, originals[path])
        except AtomicWriteCommitDurabilityError as exc:
            print(f'[失败] 备份已替换但目录耐久性不可证明；原文件未改动: {exc}')
            return EXIT_BACKUP_FAILED
        if not backup_saved:
            print(f'[失败] 备份失败，所有原文件均未改动: {backup}')
            return EXIT_BACKUP_FAILED
        backups[path] = backup

    journal = {
        'version': 1,
        'created_at': datetime.now().isoformat(),
        'items': [
            {'target': path, 'backup': backups[path]} for path in changed],
    }
    try:
        journal_saved = atomic_write_json(journal_path, journal)
    except AtomicWriteCommitDurabilityError as exc:
        print(f'[失败] 迁移 journal 已替换但目录耐久性不可证明: {exc}')
        return EXIT_BACKUP_FAILED
    if not journal_saved:
        print('[失败] 无法建立迁移事务日志；所有原文件均未改动。')
        return EXIT_BACKUP_FAILED

    for path in changed:
        try:
            target_saved = atomic_write_json(path, normalized[path])
        except AtomicWriteCommitDurabilityError as exc:
            print(f'[失败] 目标已替换但目录耐久性不可证明，开始整组恢复: {exc}')
            restore_ok = _recover_interrupted_migration(data_dir)
            return EXIT_WRITE_FAILED if restore_ok else EXIT_RESTORE_FAILED
        if target_saved:
            continue
        print(f'[失败] 写入失败，开始恢复已写文件: {path}')
        restore_ok = _recover_interrupted_migration(data_dir)
        return EXIT_WRITE_FAILED if restore_ok else EXIT_RESTORE_FAILED

    try:
        _remove_journal(journal_path)
    except OSError as exc:
        print(f'[失败] 迁移内容已写入但无法提交事务日志: {exc}')
        return EXIT_WRITE_FAILED

    print(f'[完成] 已原子规范化 {len(changed)} 个文件；再次运行应显示已通过。')
    return EXIT_OK


def main():
    parser = argparse.ArgumentParser(description='单策略部署预检与状态规范化')
    parser.add_argument('--data-dir', required=True, help='trade_state.json 所在目录')
    parser.add_argument('--config', help='config.json 路径；默认与账本同目录')
    parser.add_argument('--apply', action='store_true', help='备份后实际写入')
    parser.add_argument(
        '--cleanup-spec', help='只读干跑时预览受审单键清理 spec')
    parser.add_argument(
        '--release-sha', help='cleanup spec 所绑定的完整 release SHA')
    parser.add_argument(
        '--recover-only', action='store_true',
        help='只回滚未完成事务，不执行规范化或迁移')
    parser.add_argument(
        '--classify-state', action='store_true',
        help='严格只读分类为 requires_migration 或 migration_complete')
    args = parser.parse_args()
    return run(
        args.data_dir,
        apply=args.apply,
        config_path=args.config,
        cleanup_spec=args.cleanup_spec,
        release_sha=args.release_sha,
        recover_only=args.recover_only,
        classify_state=args.classify_state,
    )


if __name__ == '__main__':
    sys.exit(main())
