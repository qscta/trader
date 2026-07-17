"""止损防线子系统（TradingSystem 的 mixin）。

真钱红线的集中地：验证式撤销确认、残留标记与阻断、止损自愈巡检、
「交易所已平」统一确认收尾、本地账本落盘失败的运行时补偿。

以 mixin 形式承载：方法仍绑定在 TradingSystem 实例上——self 语义、
测试对实例方法的桩打法、调用链与日志行为全部不变，只做物理分层。
宿主须提供：exchange_api / trade_state / notifier / config / _trade_lock /
_stop_anomalies / record_stop_loss / get_strategy_for_symbol / _get_strategy_display_name。
"""

import logging
from datetime import date, datetime

from trade_state import TradeStatePersistenceError

logger = logging.getLogger(__name__)


class StopGuardianMixin:

    STOP_RESIDUE_VISIBILITY_GRACE_SECONDS = 10

    def reconcile_intraday_stop_losses(self):
        """盘中巡检：发现本地有仓而交易所端已无仓时，立即按止损/外部平仓处理并通知。"""
        if not self._trade_lock.acquire(blocking=False):
            logger.info("交易检查执行中，跳过盘中止损巡检")
            return

        try:
            # 平仓后的未知止损标记跨过可见性窗口后，由同一 5 分钟巡检复验清理；
            # 不再等到次日日检，也不在 POST 超时后的瞬时空清单上过早解锁。
            try:
                self._retry_clear_stop_residues()
            except Exception as residue_error:
                logger.exception(
                    f'盘中止损残留复验异常；继续巡检已有仓位: {residue_error}')
            open_positions = self.trade_state.get_all_open_positions()
            # 反向核对不能依赖“本地至少有一仓”：迟到成交/人工仓恰恰可能在
            # 本地零仓时出现。每个 5 分钟巡检都做，新增孤儿持久化隔离。
            try:
                self._reconcile_intraday_orphans(open_positions)
            except Exception as orphan_error:
                logger.exception(f'盘中孤儿仓核对异常；继续巡检已有本地仓位: {orphan_error}')
            if not open_positions:
                return

            logger.debug(f"开始盘中止损巡检，当前本地持仓数: {len(open_positions)}")
            symbol_configs = {s['name']: s for s in self.config['trading']['symbols']}

            for symbol, position in sorted(open_positions.items()):
                # 单品种异常只跳过该品种，不得中断其余品种的巡检（与日检同一隔离标准）：
                # 止损自愈补挂/撤单确认都可能抛出（如面值不可得、撤单重试耗尽）
                try:
                    self._reconcile_symbol_intraday(symbol, position, symbol_configs)
                except Exception as sym_e:
                    logger.exception(f"{symbol} 盘中止损巡检单品种异常，跳过该品种继续: {sym_e}")
        except Exception as e:
            logger.exception(f"盘中止损巡检异常: {e}")
        finally:
            self._trade_lock.release()

    def _reconcile_intraday_orphans(self, local_positions):
        """盘中反向核对交易所额外持仓；查询未知也按 fail-closed 隔离空仓品种。"""
        list_symbols = getattr(self.exchange_api, 'list_position_symbols', None)
        if not callable(list_symbols):
            return
        local_symbols = set(local_positions)
        try:
            exchange_symbols = set(list_symbols())
        except Exception as exc:
            # 无法证明空仓品种真的空仓，就不能允许它继续新开。已有本地仓仍由
            # 下方逐仓查询/止损巡检处理，不因反向清单失败而整体中断。
            for cfg in self.config.get('trading', {}).get('symbols', []):
                symbol = cfg.get('name')
                if symbol and symbol not in local_symbols:
                    self._quarantine_position_mismatch(
                        symbol, f'盘中无法完成孤儿仓核对: {exc}')
            return

        for symbol in sorted(exchange_symbols - local_symbols):
            intent_getter = getattr(self.trade_state, 'get_open_intent', None)
            intent = intent_getter(symbol) if callable(intent_getter) else None
            resume_intent = getattr(self, '_resume_open_intent_position', None)
            if intent and callable(resume_intent) and resume_intent(symbol, intent):
                continue
            self._quarantine_position_mismatch(
                symbol, '盘中发现交易所有仓但本地无记录（孤儿仓）')

        get_quarantines = getattr(self.trade_state, 'get_position_quarantines', None)
        quarantines = list(get_quarantines()) if callable(get_quarantines) else []
        for symbol in quarantines:
            if symbol not in exchange_symbols and symbol not in local_symbols:
                self._clear_position_quarantine_after_reconcile(symbol)

    def _reconcile_symbol_intraday(self, symbol, position, symbol_configs):
        """单品种盘中巡检：持仓核对 + 止损自愈 + 交易所端已平的记账。异常由调用方按品种隔离。"""
        close_recovery = self._resume_persisted_close_intent(
            symbol, position, '盘中巡检')
        if close_recovery in ('closed', 'unresolved'):
            return
        if close_recovery == 'partial':
            position = self.trade_state.get_open_position(symbol)
            if not position:
                return
        symbol_config = symbol_configs.get(symbol, {
            'name': symbol,
            'enabled': True,
            'risk_per_trade': self.config['strategy']['default_risk_per_trade'],
            # 品种已删但仍有持仓时，与日线主检查保持一致：用持仓记录的策略托管
            'strategy': position.get('strategy') or 'ma_cross'
        })
        _strategy, strategy_type = self.get_strategy_for_symbol(symbol_config)
        strategy_name = self._get_strategy_display_name(strategy_type)
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)

        try:
            exchange_position = self.exchange_api.get_position(ccxt_symbol)
        except Exception as e:
            logger.warning(f"{symbol} [{strategy_name}] 盘中止损巡检查询持仓失败: {e}")
            return

        if exchange_position is not None and exchange_position.get('contracts', 0) > 0:
            verifier = getattr(self, '_verify_existing_position_or_quarantine', None)
            if callable(verifier) and not verifier(
                    symbol, position, exchange_position, clear_on_match=False):
                return
            protected = self._ensure_stop_order_alive(
                symbol, ccxt_symbol, position, strategy_name)
            if protected:
                clear = getattr(self, '_clear_position_quarantine_after_reconcile', None)
                if callable(clear):
                    clear(symbol)
            return

        logger.warning(f"{symbol} [{strategy_name}] 盘中巡检发现交易所端已无持仓，按止损/外部平仓处理")
        exit_price = position.get('stop_loss_price') or position.get('entry_price')
        closed_position, state_saved, _stop_cleared = self._handle_exchange_flat_close(
            symbol, ccxt_symbol, position, exit_price,
            f"{strategy_name}盘中止损巡检", strategy_type=strategy_type)
        if not closed_position:
            return

        self.notifier.notify_stop_loss_triggered(
            symbol,
            strategy_name,
            position.get('side', ''),
            exit_price,
            source='盘中5分钟巡检确认'
        )

        if not state_saved:
            logger.warning(f"{symbol} [{strategy_name}] 盘中止损巡检已发通知，但本地状态落盘失败，跳过后续状态修正")
            return

        logger.info(f"{symbol} [双均线] 盘中止损巡检已与平仓同事务记录 T+1 限制")

    def _ensure_stop_order_alive(self, symbol, ccxt_symbol, position, strategy_name):
        """止损自愈：本地与交易所都有仓时，确认止损单仍挂在交易所；丢失则按本地止损价补挂。

        覆盖「建新止损失败 / 人工误撤 / 交易所端丢单」等任何原因的止损缺失——巡检周期内
        自动恢复保护，不再只靠告警等人工。fail-safe：查询失败、残留阻断、状态不明时一律不动。
        状态判定由适配层 find_stop_order_state 完成（方向+触发价+张数严格匹配才算 intact，
        张数换算不外泄）：intact 不动 / adoptable 收养唯一新 ID / mismatch 隔离且不补挂
        （防双止损）/ missing 补挂。
        异常状态记入 self._stop_anomalies（前端展示 + 告警只在状态首次进入时发，防止巡检轰炸）。
        """
        if position.get('stop_resize_pending'):
            if not self._retry_partial_stop_resize(
                    symbol, ccxt_symbol, position, strategy_name):
                return False
            position = self.trade_state.get_open_position(symbol) or position
        residue_present = self.trade_state.has_stop_residue(symbol)
        stop_price = position.get('stop_loss_price')
        if not stop_price:
            return False
        stop_order_id = position.get('stop_order_id')
        try:
            # 四态裁决下沉到适配层：intact / adoptable / mismatch / missing。
            state_result = self.exchange_api.find_stop_order_state(
                ccxt_symbol, position.get('side'),
                position.get('stop_order_size') or position.get('position_size'),
                stop_price, stop_order_id)
        except Exception as e:
            logger.warning(f"{symbol} [{strategy_name}] 止损存在性检查失败，跳过本轮: {e}")
            return False
        if isinstance(state_result, dict):
            state = state_result.get('state')
            discovered_order_id = state_result.get('order_id')
        else:
            state = state_result
            discovered_order_id = None
        if state == 'intact':
            if residue_present:
                try:
                    # find_stop_order_state 已权威拉取全部算法单并证明只有这一张
                    # 完整保护；此时才可解除未知残留标记。
                    self.trade_state.clear_stop_residue(symbol)
                except Exception as exc:
                    self._notify_trade_state_persistence_issue(
                        symbol, '止损残留验证清除', exc)
                    return False
            self._stop_anomalies.pop(symbol, None)  # 状态恢复正常，解除前端警示
            return True
        if state == 'adoptable':
            if not discovered_order_id:
                state = 'mismatch'
            else:
                updated, _saved = self._update_trade_state_stop_with_runtime_fallback(
                    symbol, stop_price, discovered_order_id, "巡检收养唯一止损",
                    stop_order_size=(position.get('stop_order_size')
                                     or position.get('position_size')),
                    extra_stop_order_ids=position.get('extra_stop_order_ids') or [],
                    stop_resize_pending=bool(position.get('stop_resize_pending')))
                if not updated:
                    msg = (f"{symbol} 发现唯一完整止损单 {discovered_order_id}，"
                           f"但本地收养失败；已暂停补挂，请立即核对")
                    logger.critical(msg)
                    if self._stop_anomalies.get(symbol) != 'adoption_failed':
                        self.notifier.notify_error(msg)
                    self._stop_anomalies[symbol] = 'adoption_failed'
                    return False
                if residue_present:
                    try:
                        self.trade_state.clear_stop_residue(symbol)
                    except Exception as exc:
                        self._notify_trade_state_persistence_issue(
                            symbol, '止损收养后残留清除', exc)
                        return False
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
            quarantine = getattr(self, '_quarantine_position_mismatch', None)
            if first_mismatch and callable(quarantine):
                quarantine(
                    symbol, '止损单存在多张或内容歧义，禁止自动补挂/新开仓',
                    {'recorded_stop_order_id': stop_order_id},
                    stop_residue_possible=True)
            elif first_mismatch:
                self.notifier.notify_error(msg)
            return False

        if residue_present:
            try:
                # “missing”来自完整算法单清单，足以证明旧未知止损已不存在；
                # 清掉保守标记后方可补挂当前仓位唯一保护。
                self.trade_state.clear_stop_residue(symbol)
            except Exception as exc:
                self._notify_trade_state_persistence_issue(
                    symbol, '止损缺失核验后的残留清除', exc)
                return False

        logger.warning(f"{symbol} [{strategy_name}] 持仓缺少止损单（记录ID={stop_order_id}），按本地止损价 {stop_price} 补挂")
        stop_order = self.exchange_api.create_stop_loss_order(
            ccxt_symbol, position['side'], position['position_size'], stop_price)
        if not stop_order:
            msg = f"{symbol} 持仓缺少止损单且自动补挂失败，仓位暂无止损保护，请立即人工处理！"
            logger.critical(msg)
            self._stop_anomalies[symbol] = 'replant_failed'
            quarantine = getattr(self, '_quarantine_position_mismatch', None)
            if callable(quarantine):
                # 隔离入口负责首轮告警和持久化去重；这里再单独通知会让同一故障
                # 在一次巡检中连续发送两条错误消息。
                quarantine(
                    symbol, '持仓缺少止损且自动补挂失败',
                    stop_residue_possible=True)
            else:
                try:
                    self.trade_state.mark_stop_residue(symbol)
                except Exception as exc:
                    logger.critical(f'{symbol} 补挂失败后的未知止损标记失败: {exc}')
                self.notifier.notify_error(msg)
            return False
        self._stop_anomalies.pop(symbol, None)
        updated, _saved = self._update_trade_state_stop_with_runtime_fallback(
            symbol, stop_price, stop_order.get('id'), "巡检补挂止损")
        if not updated:
            return False
        self.notifier.send_message(
            f"[{self.label}] 止损已自动补挂",
            f"{symbol} 检测到持仓缺少止损单，已按本地止损价 {stop_price} 补挂\n"
            f"新止损单ID: {stop_order.get('id')}\n请顺手核对欧易当前委托")
        return True

    def _retry_partial_stop_resize(self, symbol, ccxt_symbol, position, strategy_name):
        """部分平仓后先挂余仓新止损，再撤旧大额 reduce-only 止损。"""
        remaining = position.get('position_size')
        stop_price = position.get('stop_loss_price')
        if not remaining or not stop_price:
            return False
        new_stop = self.exchange_api.create_stop_loss_order(
            ccxt_symbol, position['side'], remaining, stop_price)
        if not new_stop or not new_stop.get('id'):
            marker = getattr(self, '_mark_possible_unknown_stop_residue', None)
            if callable(marker):
                marker(symbol)
            else:
                try:
                    self.trade_state.mark_stop_residue(symbol)
                except Exception as exc:
                    logger.critical(f'{symbol} 止损缩量失败后的未知单标记失败: {exc}')
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
        # 本地 extra_ids 为空不等于交易所没有 POST 超时留下的未知单。
        # 调用方紧接着用完整算法单清单确认，只有 intact/adoptable 才清 residue。
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
        residue_present = False
        residue_check = getattr(self.trade_state, 'has_stop_residue', None)
        if callable(residue_check):
            try:
                residue_present = bool(residue_check(symbol))
            except Exception:
                residue_present = True
        if extra_order_ids is None:
            getter = getattr(self.trade_state, 'get_open_position', None)
            current = getter(symbol) if callable(getter) else None
            extra_order_ids = (current or {}).get('extra_stop_order_ids') or []
        order_ids = []
        for value in [stop_order_id] + list(extra_order_ids or []):
            if value and str(value) not in {str(item) for item in order_ids}:
                order_ids.append(value)
        ok = bool(order_ids)
        for order_id in order_ids:
            try:
                ok = bool(self.exchange_api.cancel_order(ccxt_symbol, order_id)) and ok
            except Exception:
                ok = False
        # residue 可能代表一张“创建成功但 ACK/确认丢失、ID 未知”的算法单。
        # 即便已按已知 ID 撤成功，也必须 cancel_all 后才可证明该品种保护单归零。
        if not ok or residue_present:
            try:
                ok = bool(self.exchange_api.cancel_all_orders(ccxt_symbol))
            except Exception:
                ok = False
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
            force_mark = getattr(
                self.trade_state, 'force_runtime_mark_stop_residue', None)
            if callable(force_mark):
                try:
                    force_mark(symbol)
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
            marked_at = self.trade_state.get_stop_residues().get(symbol)
            if marked_at is None:
                return True
            marked = datetime.fromisoformat(str(marked_at))
            return ((datetime.now() - marked).total_seconds() >=
                    self.STOP_RESIDUE_VISIBILITY_GRACE_SECONDS)
        except Exception as exc:
            logger.warning(f'{symbol} 止损残留时间不可验证，继续保留阻断: {exc}')
            return False

    def _retry_clear_stop_residues(self):
        """周期重试清理残留：只处理已无持仓且越过可见性等待窗的品种。"""
        for residue_symbol in list(self.trade_state.get_stop_residues().keys()):
            if self.trade_state.get_open_position(residue_symbol):
                continue
            if not self._stop_residue_grace_elapsed(residue_symbol):
                continue
            try:
                ccxt_sym = self.exchange_api.to_ccxt_symbol(residue_symbol)
                # 交易所端有仓（可能是人工手动开的仓）时绝不盲撤：手动仓的止损单也会被一并撤掉
                if self.exchange_api.get_position(ccxt_sym):
                    logger.warning(f"{residue_symbol} 残留清理跳过：交易所端存在持仓（疑似人工仓位），请人工处理该品种挂单")
                    continue
                if self.exchange_api.cancel_all_orders(ccxt_sym):
                    self.trade_state.clear_stop_residue(residue_symbol)
                    logger.warning(f"{residue_symbol} 止损残留已确认清理，解除开仓阻断")
                    self.notifier.send_message(
                        "止损残留已清理", f"{residue_symbol} 残留挂单已确认撤销，恢复正常开仓")
            except Exception as e:
                logger.error(f"{residue_symbol} 止损残留清理重试失败: {e}")

    def _handle_exchange_flat_close(
            self, symbol, ccxt_symbol, position, exit_price, context,
            strategy_type=None):
        """「本地有仓、交易所已无仓」的统一确认收尾（启动同步/盘中巡检/两策略日检共用）：

        记平本地仓位（落盘失败走运行时补偿）后，只要记平成功——哪怕落盘失败——
        都必须验证式确认旧止损条件单已消失：手动/外部平仓留下的残留 reduce-only 单
        会错杀未来新仓，不可确认则标记残留阻断该品种开仓。
        返回 (closed_position, state_saved, stop_cleared)；closed 为 None 时调用方直接放弃。
        """
        effective_strategy = strategy_type or position.get('strategy') or 'ma_cross'
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
            if effective_strategy == 'ma_cross' and not retired_from_pool else None)
        closed_position, state_saved = self._close_trade_state_with_runtime_fallback(
            symbol, exit_price, context, stop_loss_date=stop_loss_date,
            stop_cleanup_pending=True,
            reset_turtle_signal=(effective_strategy == 'turtle'))
        if not closed_position:
            return None, False, False
        if stop_loss_date is not None:
            # stop_loss_dates 是主账本的只读镜像；磁盘事务（或 force-runtime
            # 补偿）已经先完成，这里只同步当前进程视图，不再二次落盘。
            in_memory_dates = getattr(self, 'stop_loss_dates', None)
            if isinstance(in_memory_dates, dict):
                in_memory_dates[symbol] = stop_loss_date
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
                                                 reset_turtle_signal=False,
                                                 close_intent_client_id=None):
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
        if reset_turtle_signal:
            close_kwargs['reset_turtle_signal'] = True
        if close_intent_client_id is not None:
            close_kwargs['close_intent_client_id'] = close_intent_client_id
        try:
            closed_position = self.trade_state.close_position(
                symbol, exit_price, **close_kwargs)
            state_saved = True
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
        msg = (f"{symbol} {context}后本地状态保存失败: {exc}。\n"
               f"已执行运行时补偿，请立即核对交易所持仓、止损单和本地状态。")
        logger.critical(msg)
        self.notifier.notify_error(msg)
        quarantine = getattr(self, '_quarantine_position_mismatch', None)
        if callable(quarantine):
            quarantine(
                symbol, f'{context}后磁盘状态未收口，仅完成运行时补偿',
                {'persistence_error': str(exc)}, notify=False)
