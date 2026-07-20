"""止损防线子系统（TradingSystem 的 mixin）。

真钱红线的集中地：验证式撤销确认、残留标记与阻断、止损自愈巡检、
「交易所已平」统一确认收尾、本地账本落盘失败的运行时补偿。

以 mixin 形式承载：方法仍绑定在 TradingSystem 实例上——self 语义、
测试对实例方法的桩打法、调用链与日志行为全部不变，只做物理分层。
宿主须提供：exchange_api / trade_state / notifier / config / _trade_lock /
_stop_anomalies / _get_strategy_display_name / _exchange_position_has_contracts。
"""

import logging
import math
import time
from datetime import date, datetime

from trade_state import (
    TradeStateCommitDurabilityError,
    TradeStatePersistenceError,
    positive_amounts_equal,
    position_stop_coverage_valid,
)

logger = logging.getLogger(__name__)


class StopGuardianMixin:

    # marker 在 POST 前落盘；一次 15s HTTP 超时后还会有多轮
    # 只读确认，因此 10s 会在请求尚未返回时就过期。保守跨过
    # 一个 5 分钟 guardian 周期；未知 POST 绝不在同一调用栈释放。
    STOP_RESIDUE_VISIBILITY_GRACE_SECONDS = 5 * 60

    def _defer_stop_residue_release(self, symbol, strategy_name, state):
        """未知 POST 的可见性窗内保留 residue/quarantine，不把单次快照当清净。"""
        if self._stop_residue_grace_elapsed(symbol):
            return False
        self._quarantine_position_mismatch(
            symbol,
            f'未知止损仍在可见性等待窗（当前完整清单={state}）',
            notify=False)
        self._stop_anomalies.setdefault(
            symbol, 'residue_visibility_pending')
        logger.warning(
            f'{symbol} [{strategy_name}] 止损清单当前为 {state}，但未知'
            '止损残留仍在可见性等待窗；保留 marker/quarantine，暂不释放')
        return True

    def reconcile_intraday_stop_losses(self):
        """盘中巡检：发现本地有仓而交易所端已无仓时，立即按止损/外部平仓处理并通知。"""
        if not self._trade_lock.acquire(blocking=False):
            previous = getattr(self, '_last_guardian_failure', None)
            # 正常串行冲突不能洗掉尚未被一次完整成功巡检清除的真实故障。
            # 若此前只有 busy/无故障，则保留最新冲突时间供健康快照展示。
            if (previous is None or
                    (isinstance(previous, dict) and
                     previous.get('kind') == 'trade_lock_busy')):
                self._last_guardian_failure = {
                    'at': time.time(), 'kind': 'trade_lock_busy'}
            logger.info("交易检查执行中，跳过盘中止损巡检")
            return False

        failures = []
        try:
            # 平仓后的未知止损标记跨过可见性窗口后，由同一 5 分钟巡检复验清理；
            # 不再等到次日日检，也不在 POST 超时后的瞬时空清单上过早解锁。
            try:
                if self._retry_clear_stop_residues() is False:
                    failures.append('stop_residue_recheck')
            except Exception as residue_error:
                failures.append('stop_residue_exception')
                logger.exception(
                    f'盘中止损残留复验异常；继续巡检已有仓位: {residue_error}')
            open_positions = self.trade_state.get_all_open_positions()
            # 反向核对不能依赖“本地至少有一仓”：迟到成交/人工仓恰恰可能在
            # 本地零仓时出现。每个 5 分钟巡检都做，新增孤儿持久化隔离。
            try:
                if self._reconcile_intraday_orphans(open_positions) is False:
                    failures.append('orphan_reconciliation')
            except Exception as orphan_error:
                failures.append('orphan_reconciliation_exception')
                logger.exception(f'盘中孤儿仓核对异常；继续巡检已有本地仓位: {orphan_error}')
            if open_positions:
                logger.debug(
                    f"开始盘中止损巡检，当前本地持仓数: {len(open_positions)}")
                for symbol, position in sorted(open_positions.items()):
                    # 单品种异常只跳过该品种，不得中断其余品种的巡检。
                    try:
                        if self._reconcile_symbol_intraday(
                                symbol, position) is False:
                            failures.append(f'symbol:{symbol}')
                    except Exception as sym_e:
                        failures.append(f'symbol_exception:{symbol}')
                        logger.exception(
                            f"{symbol} 盘中止损巡检单品种异常，"
                            f"跳过该品种继续: {sym_e}")
        except Exception as e:
            failures.append('guardian_exception')
            logger.exception(f"盘中止损巡检异常: {e}")
        finally:
            if failures:
                self._last_guardian_failure = {
                    'at': time.time(),
                    'kind': 'guardian_failures',
                    'failures': sorted(set(failures)),
                }
            else:
                self._last_guardian_failure = None
                self._last_successful_guardian_ts = time.time()
            self._trade_lock.release()
        return not failures

    def _reconcile_intraday_orphans(self, local_positions):
        """盘中反向核对交易所额外持仓；查询未知也按 fail-closed 隔离空仓品种。"""
        local_symbols = set(local_positions)
        try:
            exchange_symbols = set(self.exchange_api.list_position_symbols())
        except Exception as exc:
            # 无法证明空仓品种真的空仓，就不能允许它继续新开。已有本地仓仍由
            # 下方逐仓查询/止损巡检处理，不因反向清单失败而整体中断。
            for cfg in self.config.get('trading', {}).get('symbols', []):
                symbol = cfg.get('name')
                if symbol and symbol not in local_symbols:
                    self._quarantine_position_mismatch(
                        symbol, f'盘中无法完成孤儿仓核对: {exc}')
            return False

        unresolved = False
        for symbol in sorted(exchange_symbols - local_symbols):
            intent = self.trade_state.get_open_intent(symbol)
            if intent and self._resume_open_intent_position(symbol, intent):
                continue
            unresolved = True
            self._quarantine_position_mismatch(
                symbol, '盘中发现交易所有仓但本地无记录（孤儿仓）')

        quarantines = list(self.trade_state.get_position_quarantines())
        for symbol in quarantines:
            if symbol not in exchange_symbols and symbol not in local_symbols:
                self._clear_position_quarantine_after_reconcile(symbol)
        return not unresolved

    def _reconcile_symbol_intraday(self, symbol, position):
        """单品种盘中巡检：持仓核对 + 止损自愈 + 交易所端已平的记账。异常由调用方按品种隔离。"""
        lifecycle_intent = self.trade_state.get_open_intent(symbol)
        if lifecycle_intent:
            if not self._reconcile_position_open_intent(
                    symbol, lifecycle_intent, position):
                # 未终态执行必须先于 close intent、generic flat 与止损账本变化；
                # 否则会把余仓先记平，再让迟到开仓/单笔补偿订单重复改写现实。
                self._protect_unresolved_lifecycle_position(
                    symbol, lifecycle_intent, position)
                return False
            position = self.trade_state.get_open_position(symbol)
            if not position:
                return True
        close_recovery = self._resume_persisted_close_intent(
            symbol, position, '盘中巡检')
        if close_recovery == 'closed':
            return True
        if close_recovery == 'unresolved':
            return False
        if close_recovery == 'partial':
            position = self.trade_state.get_open_position(symbol)
            if not position:
                return True
        strategy_name = self._get_strategy_display_name()
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)

        try:
            exchange_position = self.exchange_api.get_position(ccxt_symbol)
        except Exception as e:
            logger.warning(f"{symbol} [{strategy_name}] 盘中止损巡检查询持仓失败: {e}")
            return False

        try:
            position_present = self._exchange_position_has_contracts(
                exchange_position)
        except Exception as exc:
            logger.warning(
                f'{symbol} [{strategy_name}] 盘中持仓响应非法: {exc}')
            return False

        if position_present:
            if not self._verify_existing_position_or_quarantine(
                    symbol, position, exchange_position, clear_on_match=False):
                return False
            protected = self._ensure_stop_order_alive(
                symbol, ccxt_symbol, position, strategy_name)
            if protected:
                self._clear_position_quarantine_after_reconcile(symbol)
            return bool(protected)

        logger.warning(f"{symbol} [{strategy_name}] 盘中巡检发现交易所端已无持仓，按止损/外部平仓处理")
        exit_price = position.get('stop_loss_price') or position.get('entry_price')
        closed_position, state_saved, _stop_cleared = self._handle_exchange_flat_close(
            symbol, ccxt_symbol, position, exit_price,
            f"{strategy_name}盘中止损巡检")
        if not closed_position:
            return False

        self.notifier.notify_stop_loss_triggered(
            symbol,
            strategy_name,
            position.get('side', ''),
            exit_price,
            source='盘中5分钟巡检确认'
        )

        if not state_saved:
            logger.warning(f"{symbol} [{strategy_name}] 盘中止损巡检已发通知，但本地状态落盘失败，跳过后续状态修正")
            return False

        logger.info(f"{symbol} [双均线] 盘中止损巡检已与平仓同事务记录 T+1 限制")
        return True

    def _protect_unresolved_lifecycle_position(self, symbol, intent, position):
        """未终态期间只维护 reduce-only 风险保护，不做财务/平仓生命周期消费。"""
        unresolved = (intent or {}).get('unresolved_execution') or {}
        if unresolved.get('kind') == 'open_attribution':
            # 归因歧义表示 fresh 净仓可能混入人工同向仓。即使
            # 数量不超过计划量，也不得把整仓当系统仓挂止损。
            logger.critical(
                f'{symbol} 未决执行属于 open_attribution；可能混入'
                '人工仓，保护管理器零写入并保留最高级隔离')
            return False
        side = unresolved.get('side')
        if side not in ('long', 'short'):
            return False
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)
        try:
            exchange_position = self.exchange_api.get_position(ccxt_symbol)
        except Exception as exc:
            logger.critical(f'{symbol} 未决执行保护前 fresh 持仓查询失败: {exc}')
            return False
        if not isinstance(exchange_position, dict):
            # flat 时禁止 generic close；blocker 继续等待确定性订单终态。
            return False
        raw_contracts = exchange_position.get('contracts')
        if isinstance(raw_contracts, bool) or raw_contracts is None:
            return False
        try:
            contracts = abs(float(raw_contracts))
            expected_contracts = float(self.exchange_api._coin_to_contracts(
                ccxt_symbol, float(unresolved['expected_position_size'])))
            local_contracts = float(self.exchange_api._coin_to_contracts(
                ccxt_symbol, float(position['position_size'])))
        except Exception as exc:
            logger.critical(f'{symbol} 未决执行保护数量换算失败: {exc}')
            return False
        tolerance = max(
            1e-12, math.ulp(max(abs(expected_contracts), 1.0)) * 8)
        if (not math.isfinite(contracts) or contracts <= tolerance or
                exchange_position.get('side') != side or
                contracts > expected_contracts + tolerance or
                not math.isfinite(local_contracts) or
                abs(contracts - local_contracts) > tolerance):
            logger.critical(
                f'{symbol} 未决执行 fresh 仓位方向/数量与当前'
                '本地受托余仓不一致，零写入并保留隔离')
            return False
        try:
            fresh_amount = float(self.exchange_api._contracts_to_coins(
                ccxt_symbol, contracts))
        except Exception as exc:
            logger.critical(f'{symbol} 未决执行 fresh 仓位换币失败: {exc}')
            return False
        provisional = dict(position)
        recorded_stop_size = provisional.get('stop_order_size')
        try:
            recorded_stop_size = float(recorded_stop_size)
        except (TypeError, ValueError, OverflowError):
            recorded_stop_size = math.nan
        provisional['stop_resize_pending'] = bool(
            provisional.get('stop_resize_pending') or
            not math.isfinite(recorded_stop_size) or
            not math.isclose(
                recorded_stop_size, fresh_amount,
                rel_tol=1e-12,
                abs_tol=max(1e-15, math.ulp(fresh_amount) * 8)))
        protected = self._ensure_stop_order_alive(
            symbol, ccxt_symbol, provisional,
            self._get_strategy_display_name())
        if protected:
            logger.warning(
                f'{symbol} 未决执行期间 fresh 余仓已有 reduce-only 保护；'
                'lifecycle blocker/quarantine 继续保留')
        return bool(protected)

    def _ensure_stop_order_alive(self, symbol, ccxt_symbol, position, strategy_name):
        """止损自愈：本地与交易所都有仓时，确认止损单仍挂在交易所；丢失则按本地止损价补挂。

        覆盖「建新止损失败 / 人工误撤 / 交易所端丢单」等任何原因的止损缺失——巡检周期内
        自动恢复保护，不再只靠告警等人工。fail-safe：查询失败、残留阻断、状态不明时一律不动。
        状态判定由适配层 find_stop_order_state 完成（方向+触发价+张数严格匹配才算 intact，
        张数换算不外泄）：intact 不动 / adoptable 收养唯一新 ID / mismatch 隔离且不补挂
        （防双止损）/ missing 补挂。
        异常状态记入 self._stop_anomalies（前端展示 + 告警只在状态首次进入时发，防止巡检轰炸）。
        """
        # 无论是否待缩量，都必须先读取 residue 并拉一份完整算法单清单。
        # 旧顺序会在 fresh marker 尚未过可见性窗时直接 POST 新止损。
        try:
            residue_present = self.trade_state.has_stop_residue(symbol)
        except Exception as exc:
            logger.warning(
                f'{symbol} [{strategy_name}] 无法读取止损残留状态，跳过本轮: {exc}')
            return False
        stop_price = position.get('stop_loss_price')
        if not stop_price:
            return False
        if not position_stop_coverage_valid(position):
            self._stop_anomalies[symbol] = 'stop_undercoverage'
            self._quarantine_position_mismatch(
                symbol, '本地止损数量未完整覆盖当前仓位', {
                    'position_size': position.get('position_size'),
                    'stop_order_size': position.get('stop_order_size'),
                    'stop_resize_pending': position.get(
                        'stop_resize_pending'),
                })
            return False
        position, recovery_ok = self._recover_pending_resize_stop(
            symbol, ccxt_symbol, position, strategy_name, residue_present)
        if not recovery_ok:
            return False
        stop_order_id = position.get('stop_order_id')
        known_extra_ids = None
        if position.get('stop_resize_pending'):
            values = position.get('extra_stop_order_ids') or []
            if values:
                known_extra_ids = list(values)
        try:
            # 常规四态裁决下沉到适配层；部分平仓仅额外开放两个严格窄态：
            # cleanup 清已知旧 ID，needed 对完整已知 oversized 集合换保护。
            inspection_amount = (
                position.get('position_size') if known_extra_ids is not None
                else (position.get('stop_order_size') or
                      position.get('position_size')))
            if known_extra_ids is None:
                state_result = self.exchange_api.find_stop_order_state(
                    ccxt_symbol, position.get('side'), inspection_amount,
                    stop_price, stop_order_id)
            else:
                state_result = self.exchange_api.find_stop_order_state(
                    ccxt_symbol, position.get('side'), inspection_amount,
                    stop_price, stop_order_id,
                    known_extra_stop_order_ids=known_extra_ids)
        except Exception as e:
            logger.warning(f"{symbol} [{strategy_name}] 止损存在性检查失败，跳过本轮: {e}")
            return False
        if isinstance(state_result, dict):
            state = state_result.get('state')
            discovered_order_id = state_result.get('order_id')
            expected_fields = (
                {'state', 'order_id'} if state == 'adoptable'
                else {'state', 'order_ids'} if state == 'resize_cleanup'
                else {'state'} if state == 'resize_needed'
                else None)
            missing_adoptable_id = (
                state == 'adoptable' and set(state_result) == {'state'})
            if (expected_fields is None or
                    (set(state_result) != expected_fields and
                     not missing_adoptable_id)):
                logger.warning(
                    f'{symbol} [{strategy_name}] 止损状态响应结构非法: '
                    f'{state_result!r}')
                return False
        else:
            state = state_result
            discovered_order_id = None
            if not isinstance(state, str):
                logger.warning(
                    f'{symbol} [{strategy_name}] 止损状态响应值非法: '
                    f'{state!r}')
                return False
            if state in {'adoptable', 'resize_cleanup', 'resize_needed'}:
                logger.warning(
                    f'{symbol} [{strategy_name}] 止损状态 {state!r} '
                    '缺少专用证据对象，拒绝裁决')
                return False
        if state not in {
                'intact', 'adoptable', 'resize_cleanup', 'resize_needed',
                'mismatch', 'missing'}:
            logger.warning(
                f'{symbol} [{strategy_name}] 止损状态响应值非法: {state!r}')
            return False
        exact_resize_already_protected = (
            position.get('stop_resize_pending') is True and
            not (position.get('extra_stop_order_ids') or []) and
            positive_amounts_equal(
                inspection_amount, position.get('position_size')))
        if state == 'resize_cleanup':
            cleanup_ids = state_result.get('order_ids')
            if residue_present and self._defer_stop_residue_release(
                    symbol, strategy_name, state):
                return True
            return self._retry_partial_stop_cleanup(
                symbol, ccxt_symbol, position, strategy_name,
                cleanup_ids)
        if state == 'resize_needed':
            if (position.get('stop_resize_pending') is not True or
                    not (position.get('extra_stop_order_ids') or [])):
                logger.critical(
                    f'{symbol} [{strategy_name}] resize_needed 与本地缩量账本矛盾')
                return False
            if residue_present:
                if self._defer_stop_residue_release(
                        symbol, strategy_name, state):
                    # 全部已知 oversized reduce-only 单仍覆盖余仓；跨可见窗前
                    # 保留 marker，不重复 POST，但继续视为受保护。
                    return True
                try:
                    self.trade_state.clear_stop_residue(symbol)
                except Exception as exc:
                    self._notify_trade_state_persistence_issue(
                        symbol, '连续部分平仓缩量前残留清除', exc)
                    return False
            return self._retry_partial_stop_resize(
                symbol, ccxt_symbol, position, strategy_name)
        if state == 'intact':
            if residue_present:
                if self._defer_stop_residue_release(
                        symbol, strategy_name, state):
                    # 已知保护单可见，当前仓位受保护；但另一张未知 POST 仍可能
                    # 迟到，故可继续托管而不能解除 residue/quarantine。
                    return True
                try:
                    # find_stop_order_state 已权威拉取全部算法单并证明只有这一张
                    # 完整保护；此时才可解除未知残留标记。
                    self.trade_state.clear_stop_residue(symbol)
                except Exception as exc:
                    self._notify_trade_state_persistence_issue(
                        symbol, '止损残留验证清除', exc)
                    return False
                residue_present = False
            if exact_resize_already_protected:
                updated, state_saved = (
                    self._update_trade_state_stop_with_runtime_fallback(
                        symbol, stop_price, stop_order_id,
                        '权威止损清单确认余仓尺寸',
                        stop_order_size=position.get('position_size'),
                        extra_stop_order_ids=[],
                        stop_resize_pending=False))
                if not updated or not state_saved:
                    return False
                self._stop_anomalies.pop(symbol, None)
                logger.warning(
                    f'{symbol} [{strategy_name}] 唯一止损已精确覆盖余仓；'
                    '仅收口本地缩量标记，未重复挂单或撤单')
                return True
            if position.get('stop_resize_pending'):
                return self._retry_partial_stop_resize(
                    symbol, ccxt_symbol, position, strategy_name)
            self._stop_anomalies.pop(symbol, None)  # 状态恢复正常，解除前端警示
            return True
        if state == 'adoptable':
            if not discovered_order_id:
                state = 'mismatch'
            else:
                residue_deferred = (
                    residue_present and self._defer_stop_residue_release(
                        symbol, strategy_name, state))
                updated, _saved = self._update_trade_state_stop_with_runtime_fallback(
                    symbol, stop_price, discovered_order_id, "巡检收养唯一止损",
                    stop_order_size=(
                        position.get('position_size')
                        if exact_resize_already_protected else
                        (position.get('stop_order_size') or
                         position.get('position_size'))),
                    extra_stop_order_ids=position.get('extra_stop_order_ids') or [],
                    stop_resize_pending=(
                        bool(position.get('stop_resize_pending')) and
                        (not exact_resize_already_protected or
                         residue_deferred)))
                if not updated:
                    msg = (f"{symbol} 发现唯一完整止损单 {discovered_order_id}，"
                           f"但本地收养失败；已暂停补挂，请立即核对")
                    logger.critical(msg)
                    if self._stop_anomalies.get(symbol) != 'adoption_failed':
                        self.notifier.notify_error(msg)
                    self._stop_anomalies[symbol] = 'adoption_failed'
                    return False
                if not _saved:
                    # 共用持久化降级路径已经告警并隔离；这里只停止
                    # 本轮，避免对同一次写盘故障重复通知。
                    return False
                if residue_present:
                    if residue_deferred:
                        logger.warning(
                            f'{symbol} [{strategy_name}] 已收养当前唯一完整止损 '
                            f'{discovered_order_id}，但未知迟到单窗口尚未结束')
                        return True
                    try:
                        self.trade_state.clear_stop_residue(symbol)
                    except Exception as exc:
                        self._notify_trade_state_persistence_issue(
                            symbol, '止损收养后残留清除', exc)
                        return False
                    residue_present = False
                position = (
                    self.trade_state.get_open_position(symbol) or updated)
                if position.get('stop_resize_pending'):
                    return self._retry_partial_stop_resize(
                        symbol, ccxt_symbol, position, strategy_name)
                self._stop_anomalies.pop(symbol, None)
                logger.warning(
                    f"{symbol} [{strategy_name}] 本地止损 ID 已失效，安全收养交易所唯一完整止损 "
                    f"{discovered_order_id}（未补挂新单）")
                self.notifier.send_message(
                    f"[{self.label}] 止损单 ID 已自动收养",
                    f"{symbol} 原记录止损 ID={stop_order_id} 已不可见；交易所存在唯一一张"
                    f"方向/价格/数量完全一致的保护单，已收养 ID={discovered_order_id}，未重复补挂")
                return True
        if state == 'mismatch':
            # ID 内容不符或存在多张保护候选：自动补挂会造成双止损，必须人工裁决。
            msg = (f"{symbol} 止损单状态异常：交易所保护单与本地记录存在内容不符、"
                   f"多张或 ID 歧义。已隔离该品种并暂停自动补挂，请立即人工核对欧易委托！")
            logger.critical(msg)
            first_mismatch = self._stop_anomalies.get(symbol) != 'mismatch'
            self._stop_anomalies[symbol] = 'mismatch'
            if first_mismatch:
                self._quarantine_position_mismatch(
                    symbol, '止损单存在多张或内容歧义，禁止自动补挂/新开仓',
                    {'recorded_stop_order_id': stop_order_id},
                    stop_residue_possible=True)
            return False

        if residue_present:
            if self._defer_stop_residue_release(
                    symbol, strategy_name, 'missing'):
                # unknown residue 往往来自“止损 POST 结果不确定”。刚写入标记
                # 后的一次完整空清单仍可能早于交易所索引可见；此时清 marker
                # 并补 POST 会制造双止损。所有状态都须跨窗复查；区别只在于
                # intact/adoptable 已能证明当前受保护，missing 必须停止托管动作。
                return False
            try:
                # 可见性窗口结束后的这一次完整算法单清单仍为 missing，才足以
                # 证明旧未知止损不存在；清掉保守标记后方可补挂唯一保护。
                self.trade_state.clear_stop_residue(symbol)
            except Exception as exc:
                self._notify_trade_state_persistence_issue(
                    symbol, '止损缺失核验后的残留清除', exc)
                return False

        if position.get('stop_resize_pending'):
            # state=missing 已证明当前完整清单没有旧保护；state=intact/adoptable
            # 已在上面返回。只有经过这份清单裁决后，才可走统一写边界补挂。
            return self._retry_partial_stop_resize(
                symbol, ccxt_symbol, position, strategy_name)

        logger.warning(f"{symbol} [{strategy_name}] 持仓缺少止损单（记录ID={stop_order_id}），按本地止损价 {stop_price} 补挂")
        stop_order = self._create_stop_with_write_intent(
            symbol, ccxt_symbol, position['side'], position['position_size'],
            stop_price)
        if not stop_order:
            msg = f"{symbol} 持仓缺少止损单且自动补挂失败，仓位暂无止损保护，请立即人工处理！"
            logger.critical(msg)
            self._stop_anomalies[symbol] = 'replant_failed'
            # 隔离入口负责首轮告警和持久化去重；这里再单独通知会让同一故障
            # 在一次巡检中连续发送两条错误消息。
            self._quarantine_position_mismatch(
                symbol, '持仓缺少止损且自动补挂失败',
                stop_residue_possible=True)
            return False
        self._stop_anomalies.pop(symbol, None)
        updated, state_saved = self._update_trade_state_stop_with_runtime_fallback(
            symbol, stop_price, stop_order.get('id'), "巡检补挂止损",
            stop_order_size=position['position_size'])
        if not updated:
            return False
        if not state_saved or not self._complete_stop_write(
                symbol, ccxt_symbol, '巡检补挂止损'):
            return False
        self.notifier.send_message(
            f"[{self.label}] 止损已自动补挂",
            f"{symbol} 检测到持仓缺少止损单，已按本地止损价 {stop_price} 补挂\n"
            f"新止损单ID: {stop_order.get('id')}\n请顺手核对欧易当前委托")
        return True

    def _recover_pending_resize_stop(
            self, symbol, ccxt_symbol, position, strategy_name,
            residue_present):
        """只读找回缩量 POST 的确定性迟到单，并把旧主单降为已知待清理单。"""
        old_stop_id = position.get('stop_order_id')
        if (not residue_present or
                position.get('stop_resize_pending') is not True or
                not old_stop_id or
                positive_amounts_equal(
                    position.get('stop_order_size'),
                    position.get('position_size'))):
            return position, True

        recovered = self._create_stop_with_write_intent(
            symbol, ccxt_symbol, position.get('side'),
            position.get('position_size'), position.get('stop_loss_price'),
            require_existing=True)
        if not recovered:
            return position, True

        recovered_id = recovered.get('id')
        prior_ids = [old_stop_id] + list(
            position.get('extra_stop_order_ids') or [])
        extra_ids = list(dict.fromkeys(
            value for value in prior_ids
            if value and str(value) != str(recovered_id)))
        updated, state_saved = (
            self._update_trade_state_stop_with_runtime_fallback(
                symbol, position.get('stop_loss_price'), recovered_id,
                '只读找回部分平仓迟到止损',
                stop_order_size=position.get('position_size'),
                extra_stop_order_ids=extra_ids,
                stop_resize_pending=True))
        if not updated or not state_saved:
            return position, False
        logger.warning(
            f'{symbol} [{strategy_name}] 已按确定性幂等 ID 只读找回迟到余仓止损 '
            f'{recovered_id}；等待完整清单证明后清理旧止损')
        return updated, True

    def _retry_partial_stop_cleanup(
            self, symbol, ccxt_symbol, position, strategy_name,
            cleanup_ids):
        """新余仓止损已严格确认时，只清理账本中精确匹配的旧止损。"""
        ledger_ids = position.get('extra_stop_order_ids') or []
        if (not isinstance(ledger_ids, list) or
                any(not isinstance(value, str) or not value
                    for value in ledger_ids) or
                len(ledger_ids) != len(set(ledger_ids)) or
                not isinstance(cleanup_ids, list) or
                any(not isinstance(value, str) or not value
                    for value in cleanup_ids) or
                len(cleanup_ids) != len(set(cleanup_ids)) or
                set(cleanup_ids) != set(ledger_ids)):
            self._quarantine_position_mismatch(
                symbol, '止损缩量清理证据与账本旧 ID 集合不一致',
                {'ledger_ids': ledger_ids, 'observed_ids': cleanup_ids},
                stop_residue_possible=True)
            return False

        uncleared = self._cancel_active_stop_ids_only(
            symbol, ccxt_symbol, cleanup_ids)
        updated, state_saved = self._update_trade_state_stop_with_runtime_fallback(
            symbol, position.get('stop_loss_price'),
            position.get('stop_order_id'), '部分平仓旧止损清理',
            stop_order_size=position.get('position_size'),
            extra_stop_order_ids=uncleared,
            stop_resize_pending=bool(uncleared))
        if not updated or not state_saved:
            return False
        if uncleared:
            self._stop_anomalies[symbol] = 'partial_stop_cleanup_pending'
            logger.critical(
                f'{symbol} [{strategy_name}] 旧止损 {uncleared} '
                '仍未确认撤销；保留缩量重试标记')
            return False

        if not self._complete_stop_write(
                symbol, ccxt_symbol, '部分平仓旧止损清理'):
            self._quarantine_position_mismatch(
                symbol, '旧止损清理后二次完整清单未证明仅剩主止损',
                stop_residue_possible=True)
            return False
        self._stop_anomalies.pop(symbol, None)
        logger.warning(
            f'{symbol} [{strategy_name}] 已验证撤清旧止损，仅保留余仓主止损')
        return True

    def _retry_partial_stop_resize(self, symbol, ccxt_symbol, position, strategy_name):
        """部分平仓后先挂余仓新止损，再撤旧大额 reduce-only 止损。"""
        remaining = position.get('position_size')
        stop_price = position.get('stop_loss_price')
        if not remaining or not stop_price:
            return False
        new_stop = self._create_stop_with_write_intent(
            symbol, ccxt_symbol, position['side'], remaining, stop_price)
        if not new_stop:
            anomaly = 'partial_stop_resize_failed'
            if self._stop_anomalies.get(symbol) != anomaly:
                self.notifier.notify_error(
                    f'{symbol} [{strategy_name}] 部分平仓余仓止损缩量重试失败；'
                    '旧 reduce-only 止损仍在，请人工核对')
            self._stop_anomalies[symbol] = anomaly
            return False

        new_stop_id = new_stop['id']
        old_stop_id = position.get('stop_order_id')
        previous_ids = [old_stop_id] + list(
            position.get('extra_stop_order_ids') or [])
        previous_ids = [
            value for value in previous_ids
            if value and str(value) != str(new_stop_id)]
        extra_ids = self._cancel_active_stop_ids_only(
            symbol, ccxt_symbol, previous_ids)
        try:
            updated = self.trade_state.update_stop_loss(
                symbol, stop_price, new_stop_id, stop_order_size=remaining,
                extra_stop_order_ids=extra_ids,
                stop_resize_pending=bool(extra_ids))
        except TradeStateCommitDurabilityError as e:
            updated = e.committed_result
            self._notify_trade_state_persistence_issue(
                symbol, '部分平仓止损缩量', e)
            # 当前磁盘与内存都是新 ID，但掉电耐久性不可证明；门闩已经禁开仓，
            # 本轮也不能清理 residue/隔离。
            return False
        except TradeStatePersistenceError as e:
            updated = self.trade_state.force_runtime_update_stop_loss(
                symbol, stop_price, new_stop_id, stop_order_size=remaining,
                extra_stop_order_ids=extra_ids,
                stop_resize_pending=bool(extra_ids))
            self._notify_trade_state_persistence_issue(symbol, '部分平仓止损缩量', e)
            # 磁盘仍是旧 ID/旧数量；本轮不能继续清隔离或残留。
            return False
        if not updated:
            return False
        if extra_ids:
            self._stop_anomalies[symbol] = 'partial_stop_cleanup_pending'
            logger.critical(
                f'{symbol} [{strategy_name}] 新余仓止损已在，但旧止损 {extra_ids} '
                '仍未确认撤销；保留重试标记')
            return False
        if not self._complete_stop_write(
                symbol, ccxt_symbol, '部分平仓止损缩量'):
            return False
        self._stop_anomalies.pop(symbol, None)
        logger.warning(f'{symbol} [{strategy_name}] 部分平仓余仓止损已缩量为 {remaining}')
        return True

    def _cancel_stop_order_confirmed(self, symbol, ccxt_symbol, stop_order_id,
                                     extra_order_ids=None,
                                     visibility_grace_required=True):
        """撤销旧止损并确认撤干净（okx 适配层为验证式撤销）。

        不可确认时：标记止损残留（持久化）、严重告警、返回 False——
        调用方此时不得创建新止损或反手开仓，残留的 reduce-only 单可能错杀未来新仓。
        确认成功时顺带解除该品种的残留标记。
        """
        try:
            residue_present = bool(self.trade_state.has_stop_residue(symbol))
        except Exception:
            residue_present = True
        if extra_order_ids is None:
            try:
                current = self.trade_state.get_open_position(symbol)
            except Exception as exc:
                logger.critical(
                    f'{symbol} 无法读取完整止损 ID 集合，拒绝宣称撤销成功: {exc}')
                return False
            extra_order_ids = (current or {}).get('extra_stop_order_ids') or []
        order_ids = []
        for value in [stop_order_id] + list(extra_order_ids or []):
            if value and str(value) not in {str(item) for item in order_ids}:
                order_ids.append(value)
        known_ids_ok = True
        for order_id in order_ids:
            try:
                known_ids_ok = bool(
                    self.exchange_api.cancel_order(
                        ccxt_symbol, order_id)) and known_ids_ok
            except Exception:
                known_ids_ok = False
        # residue 可能代表一张“创建成功但 ACK/确认丢失、ID 未知”的算法单。
        # 即便已按已知 ID 撤成功，也必须 cancel_all 后才可证明该品种保护单归零。
        if order_ids and not known_ids_ok:
            # 已知 ID 不是零成交 canceled 时，cancel-all 只能尽力清扫；
            # 绝不能用集合空清单把 effective/unknown 洗成撤销成功。
            try:
                self.exchange_api.cancel_all_orders(ccxt_symbol)
            except Exception:
                pass
            ok = False
        elif residue_present or not order_ids:
            try:
                ok = bool(self.exchange_api.cancel_all_orders(ccxt_symbol))
            except Exception:
                ok = False
        else:
            ok = True
        if ok:
            if (residue_present and visibility_grace_required and
                    not self._stop_residue_grace_elapsed(symbol)):
                logger.warning(
                    f'{symbol} 算法单清单已连续验空，但未知止损标记仍在可见性等待窗；'
                    '暂不解除开仓阻断，交由后续巡检复验')
                return False
            try:
                self.trade_state.clear_stop_residue(symbol)
                return True
            except Exception as exc:
                self._notify_trade_state_persistence_issue(
                    symbol, '止损撤净后残留标记清除', exc)
                return False
        residue_persist_error = None
        try:
            self.trade_state.mark_stop_residue(symbol)
        except Exception as exc:
            residue_persist_error = exc
            try:
                self.trade_state.force_runtime_mark_stop_residue(symbol)
            except Exception:
                logger.exception(f'{symbol} 运行时止损残留标记也失败')
        msg = (f"{symbol} 旧止损单撤销无法确认，可能残留！已阻断该品种新开仓，"
               f"系统将在每日检查时自动重试清理；请人工核对欧易当前委托")
        if residue_persist_error is not None:
            msg += f'；残留标记落盘失败，仅本进程阻断: {residue_persist_error}'
        logger.critical(msg)
        self.notifier.notify_error(msg)
        return False

    def _stop_residue_grace_elapsed(self, symbol):
        try:
            runtime_deadlines = getattr(
                self, '_stop_residue_runtime_not_before', None)
            runtime_deadline = (
                runtime_deadlines.get(symbol)
                if isinstance(runtime_deadlines, dict) else None)
            if runtime_deadline is not None:
                try:
                    if time.monotonic() < float(runtime_deadline):
                        return False
                except (TypeError, ValueError, OverflowError):
                    # 运行时 not-before 损坏时只能 fail closed。
                    return False
            marked_at = self.trade_state.get_stop_residues().get(symbol)
            if marked_at is None:
                return True
            marked = datetime.fromisoformat(str(marked_at))
            now = datetime.now(marked.tzinfo) if marked.tzinfo else datetime.now()
            return ((now - marked).total_seconds() >=
                    self.STOP_RESIDUE_VISIBILITY_GRACE_SECONDS)
        except Exception as exc:
            logger.warning(f'{symbol} 止损残留时间不可验证，继续保留阻断: {exc}')
            return False

    def _retry_clear_stop_residues(self):
        """周期重试清理残留：只处理已无持仓且越过可见性等待窗的品种。"""
        all_ok = True
        for residue_symbol in list(self.trade_state.get_stop_residues().keys()):
            if self.trade_state.get_open_position(residue_symbol):
                continue
            if not self._stop_residue_grace_elapsed(residue_symbol):
                continue
            try:
                ccxt_sym = self.exchange_api.to_ccxt_symbol(residue_symbol)
                # 交易所端有仓（可能是人工手动开的仓）时绝不盲撤：手动仓的止损单也会被一并撤掉
                exchange_position = self.exchange_api.get_position(ccxt_sym)
                if self._exchange_position_has_contracts(exchange_position):
                    all_ok = False
                    logger.warning(f"{residue_symbol} 残留清理跳过：交易所端存在持仓（疑似人工仓位），请人工处理该品种挂单")
                    continue
                if self.exchange_api.cancel_all_orders(ccxt_sym):
                    self.trade_state.clear_stop_residue(residue_symbol)
                    logger.warning(f"{residue_symbol} 止损残留已确认清理，解除开仓阻断")
                    self.notifier.send_message(
                        "止损残留已清理", f"{residue_symbol} 残留挂单已确认撤销，恢复正常开仓")
                else:
                    all_ok = False
            except Exception as e:
                all_ok = False
                logger.error(f"{residue_symbol} 止损残留清理重试失败: {e}")
        return all_ok

    def _handle_exchange_flat_close(
            self, symbol, ccxt_symbol, position, exit_price, context):
        """「本地有仓、交易所已无仓」的统一确认收尾（启动同步/巡检/日检共用）：

        记平本地仓位（落盘失败走运行时补偿）后，只要记平成功——哪怕落盘失败——
        都必须验证式确认旧止损条件单已消失：手动/外部平仓留下的残留 reduce-only 单
        会错杀未来新仓，不可确认则标记残留阻断该品种开仓。
        返回 (closed_position, state_saved, stop_cleared)；closed 为 None 时调用方直接放弃。
        """
        config = getattr(self, 'config', None)
        pool = (config.get('trading', {}).get('symbols', [])
                if isinstance(config, dict) else [])
        matched = next(
            (cfg for cfg in pool if cfg.get('name') == symbol), None)
        # 已从池删除（不在池）或在池但已禁用：均按「只平不开」处理——退出时不记 T+1，
        # 避免次日按 EMA 方向自动重入。
        retired_from_pool = bool(
            isinstance(config, dict) and
            (matched is None or not matched.get('enabled', True)))
        # 区分“平仓事务刚建的 crash-cleanup marker”与“之前已有的
        # 未知 POST 残留”。前者经 cancel-all 连续验空即可清；后者还要等满
        # 可见性窗，防止迟到算法单在释放新开仓后才浮现。
        try:
            residue_preexisting = self.trade_state.has_stop_residue(symbol)
        except Exception:
            residue_preexisting = True
        stop_loss_date = (
            date.today().strftime('%Y-%m-%d')
            if not retired_from_pool else None)
        try:
            estimated_stop = float(position.get('stop_loss_price'))
            estimated_exit = float(exit_price)
        except (TypeError, ValueError, OverflowError):
            estimated_stop = estimated_exit = None
        exit_price_source = (
            'estimated_stop'
            if (estimated_stop is not None and estimated_exit is not None and
                math.isfinite(estimated_stop) and estimated_stop > 0 and
                math.isfinite(estimated_exit) and
                math.isclose(
                    estimated_exit, estimated_stop,
                    rel_tol=1e-12, abs_tol=1e-12))
            else 'estimated_entry_fallback')
        closed_position, state_saved = self._close_trade_state_with_runtime_fallback(
            symbol, exit_price, context, stop_loss_date=stop_loss_date,
            stop_cleanup_pending=True,
            exit_price_source=exit_price_source)
        if not closed_position:
            return None, False, False
        stop_cleared = self._cancel_stop_order_confirmed(
            symbol, ccxt_symbol, position.get('stop_order_id'),
            position.get('extra_stop_order_ids'),
            visibility_grace_required=residue_preexisting)
        return closed_position, state_saved, stop_cleared

    def _close_trade_state_with_runtime_fallback(self, symbol, exit_price, context,
                                                 exit_fee=None,
                                                 exit_fee_currency=None,
                                                 exit_order_ids=None,
                                                 stop_loss_date=None,
                                                 stop_cleanup_pending=False,
                                                 close_intent_client_id=None,
                                                 exit_price_source=None):
        close_kwargs = {}
        if exit_fee is not None or exit_order_ids:
            close_kwargs.update(
                exit_fee=exit_fee,
                exit_fee_currency=exit_fee_currency,
                exit_order_ids=exit_order_ids)
        if stop_loss_date is not None:
            close_kwargs['stop_loss_date'] = stop_loss_date
        if stop_cleanup_pending:
            close_kwargs['stop_cleanup_pending'] = True
        if close_intent_client_id is not None:
            close_kwargs['close_intent_client_id'] = close_intent_client_id
        if exit_price_source is not None:
            close_kwargs['exit_price_source'] = exit_price_source
        try:
            closed_position = self.trade_state.close_position(
                symbol, exit_price, **close_kwargs)
            state_saved = True
        except TradeStateCommitDurabilityError as e:
            closed_position = e.committed_result
            self._notify_trade_state_persistence_issue(symbol, context, e)
            state_saved = False
        except TradeStatePersistenceError as e:
            closed_position = self.trade_state.force_runtime_close_position(
                symbol, exit_price, **close_kwargs)
            self._notify_trade_state_persistence_issue(symbol, context, e)
            state_saved = False
        if closed_position:
            # 仓位已结束：止损异常警示随仓位生命周期终结（异常单实体由调用方的撤单确认链路负责清理）
            self._stop_anomalies.pop(symbol, None)
        return closed_position, state_saved

    def _update_trade_state_stop_with_runtime_fallback(
            self, symbol, new_stop_loss_price, stop_order_id, context,
            stop_order_size=None, extra_stop_order_ids=None,
            stop_resize_pending=False, notify_on_failure=True):
        kwargs = {}
        if stop_order_size is not None:
            kwargs['stop_order_size'] = stop_order_size
        if extra_stop_order_ids is not None:
            kwargs['extra_stop_order_ids'] = extra_stop_order_ids
        if stop_resize_pending:
            kwargs['stop_resize_pending'] = True
        try:
            return self.trade_state.update_stop_loss(
                symbol, new_stop_loss_price, stop_order_id, **kwargs), True
        except TradeStateCommitDurabilityError as e:
            position = e.committed_result
            if notify_on_failure:
                self._notify_trade_state_persistence_issue(symbol, context, e)
            else:
                logger.critical(
                    f'{symbol} {context}主账本已替换但掉电耐久性不可证明；'
                    '保留新内存并维持运行时隔离')
            return position, False
        except TradeStatePersistenceError as e:
            position = self.trade_state.force_runtime_update_stop_loss(
                symbol, new_stop_loss_price, stop_order_id, **kwargs)
            if notify_on_failure:
                self._notify_trade_state_persistence_issue(symbol, context, e)
            else:
                logger.critical(
                    f"{symbol} {context}仍无法保存本地状态: {e}；"
                    "同一次止损切换已告警，继续维持运行时隔离状态")
            return position, False

    def _notify_trade_state_persistence_issue(self, symbol, context, exc):
        if isinstance(exc, TradeStateCommitDurabilityError):
            msg = (
                f'{symbol} {context}后主账本已替换，但掉电耐久性不可证明: '
                f'{exc}。\n已保留新内存且永久禁开仓；没有重放本次状态变更。'
                '请立即核对磁盘、交易所持仓与止损单。')
        else:
            msg = (f"{symbol} {context}后本地状态保存失败: {exc}。\n"
                   f"已执行运行时补偿，请立即核对交易所持仓、止损单和本地状态。")
        logger.critical(msg)
        self.notifier.notify_error(msg)
        self._quarantine_position_mismatch(
            symbol, f'{context}后磁盘状态未收口，仅完成运行时补偿',
            {'persistence_error': str(exc)}, notify=False)
