"""止损防线子系统（TradingSystem 的 mixin）。

真钱红线的集中地：验证式撤销确认、残留标记与阻断、止损自愈巡检、
「交易所已平」统一确认收尾、本地账本落盘失败的运行时补偿。

以 mixin 形式承载：方法仍绑定在 TradingSystem 实例上——self 语义、
测试对实例方法的桩打法、调用链与日志行为全部不变，只做物理分层。
宿主须提供：exchange_api / trade_state / notifier / config / _trade_lock /
_stop_anomalies / _known_orphans / record_stop_loss / get_strategy_for_symbol /
_get_strategy_display_name。
"""

import logging

from trade_state import TradeStatePersistenceError

logger = logging.getLogger(__name__)


class StopGuardianMixin:

    def _persist_exchange_flat_policy(self, symbol, exit_only):
        """交易所已确认空仓后，先持久化后续开仓策略，再允许本地记平。

        enabled 品种先落 T+1，删除/禁用品种先清掉旧标记。这样进程即使在
        “账本记平”附近崩溃，重启也不会把刚结束的仓位误当成可立即新开。
        """
        if exit_only:
            self.clear_stop_loss(symbol)
            return
        self.record_stop_loss(symbol)

    def _confirm_exchange_flat(self, symbol, ccxt_symbol):
        """本地记平和撤止损前，必须用多次查询证明交易所确已归零。"""
        try:
            confirmed = self.exchange_api.confirm_position_flat(ccxt_symbol)
            detail = '多次复核仍未确认归零'
        except Exception as e:
            confirmed = False
            detail = f'复核异常: {e}'
        if confirmed:
            return True

        reason = 'flat_unconfirmed'
        msg = (f"{symbol} 交易所首次返回空仓，但{detail}。为防瞬时空响应导致本地误记平、"
               f"撤掉真实仓位止损，已保留本地持仓并隔离，请等待下轮巡检或人工核对。")
        logger.critical(msg)
        if self._stop_anomalies.get(symbol) != reason:
            self.notifier.notify_error(msg)
        self._stop_anomalies[symbol] = reason
        return False

    def reconcile_intraday_stop_losses(self):
        """盘中巡检：发现本地有仓而交易所端已无仓时，立即按止损/外部平仓处理并通知。"""
        if not self._trade_lock.acquire(blocking=False):
            logger.info("交易检查执行中，跳过盘中止损巡检")
            return

        try:
            # 孤儿仓核对：下方主循环只遍历本地持仓，交易所侧多出的仓（开仓请求超时且
            # 确认查询在同一场网络故障中全部失败、订单其后迟到成交 / 人工开仓）不在
            # 遍历范围——此前只在启动时核对一次，运行期出现的孤儿裸仓（无止损、无托管）
            # 要等到下次重启才可见。每轮巡检顺带核对，新增才告警（集合节流，不轰炸）。
            try:
                self._check_orphan_positions('盘中巡检')
            except Exception as e:
                logger.warning(f"盘中巡检孤儿仓核对失败（不影响本轮对本地持仓的巡检）: {e}")

            open_positions = self.trade_state.get_all_open_positions()
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

    def _check_orphan_positions(self, context):
        """孤儿仓核对：交易所有仓、本地无记录（启动同步与盘中巡检共用）。

        孤儿仓不被系统托管（不推止损、不检查平仓），静默存在比报错更危险。产生路径
        除人工开仓/本地状态丢失外，还有一条系统自身的窄窗口：开仓请求超时、且随后的
        持仓确认查询在同一场网络故障中全部失败（故障相关联，并非独立小概率）→ 本地
        按开仓失败处理不记账，订单其后在交易所迟到成交 → 无止损的裸仓。反向情形
        （本地有、交易所无）巡检 5 分钟即记平，孤儿侧必须有对称防线。
        告警按「集合新增」节流（与 _stop_anomalies 同一模式）：新孤儿出现告警一次，
        消失即从已知集合移除（再次出现会再次告警）。查询失败向上抛出，
        由调用方 fail-safe（不阻断启动/巡检）。
        """
        exchange_symbols = set(self.exchange_api.list_position_symbols())
        local_symbols = set(self.trade_state.get_all_open_positions().keys())
        orphans = exchange_symbols - local_symbols
        vanished = self._known_orphans - orphans
        if vanished:
            self._known_orphans -= vanished
            logger.info(f"[{context}] 此前的孤儿仓已消失（人工处理完成）: {', '.join(sorted(vanished))}")
        new_orphans = orphans - self._known_orphans
        if new_orphans:
            msg = (f"[{context}] 发现交易所端存在、但本地无记录的持仓: {', '.join(sorted(new_orphans))}。"
                   f"系统不会自动接管（可能是人工仓位、开仓超时后的迟到成交或本地状态丢失），"
                   f"也不会为其推进止损/平仓，请立即人工确认处理！")
            logger.critical(msg)
            if self.notifier.notify_error(msg):
                self._known_orphans |= new_orphans
            else:
                logger.error(f"[{context}] 孤儿仓告警未送达，下轮巡检将继续重试")
        return orphans

    def _reconcile_symbol_intraday(self, symbol, position, symbol_configs):
        """单品种盘中巡检：持仓核对 + 止损自愈 + 交易所端已平的记账。异常由调用方按品种隔离。"""
        pool_config = symbol_configs.get(symbol)
        exit_only = pool_config is None or not pool_config.get('enabled', True)
        symbol_config = pool_config or {
            'name': symbol,
            'enabled': True,
            'risk_per_trade': self.config['strategy']['default_risk_per_trade'],
            # 品种已删但仍有持仓时，与日线主检查保持一致：优先用持仓记录的策略（当前仅 ma_cross）
            'strategy': position.get('strategy') or 'ma_cross',
            'exit_only': True
        }
        if exit_only and pool_config is not None:
            symbol_config = dict(pool_config)
            symbol_config['exit_only'] = True
        _strategy, strategy_type = self.get_strategy_for_symbol(symbol_config)
        strategy_name = self._get_strategy_display_name(strategy_type)
        ccxt_symbol = self.exchange_api.to_ccxt_symbol(symbol)

        try:
            exchange_position = self.exchange_api.get_position(ccxt_symbol)
        except Exception as e:
            logger.warning(f"{symbol} [{strategy_name}] 盘中止损巡检查询持仓失败: {e}")
            return

        if exchange_position is not None and exchange_position.get('contracts', 0) > 0:
            if not self._managed_position_is_consistent(
                    symbol, ccxt_symbol, position, exchange_position):
                return
            # 两边都有仓：顺带确认止损单仍然挂着（止损自愈，丢失则按本地止损价补挂）
            self._ensure_stop_order_alive(symbol, ccxt_symbol, position, strategy_name)
            return

        if not self._confirm_exchange_flat(symbol, ccxt_symbol):
            return

        self._persist_exchange_flat_policy(symbol, exit_only)
        logger.warning(f"{symbol} [{strategy_name}] 盘中巡检发现交易所端已无持仓，按止损/外部平仓处理")
        exit_price = position.get('stop_loss_price') or position.get('entry_price')
        closed_position, state_saved, _stop_cleared = self._handle_exchange_flat_close(
            symbol, ccxt_symbol, position, exit_price, f"{strategy_name}盘中止损巡检")
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

        if exit_only:
            logger.info(f"{symbol} [退出模式] 当前仓已结束，不记录 T+1，后续不再开仓")
        else:
            logger.info(f"{symbol} [双均线] 盘中止损巡检已记录 T+1 限制")

    def _managed_position_is_consistent(self, symbol, ccxt_symbol, position, exchange_position):
        """本地托管记录与交易所实仓方向、数量一致才允许自动管理。"""
        try:
            matches = self.exchange_api.managed_position_matches(
                ccxt_symbol, exchange_position, position.get('side'), position.get('position_size'))
        except Exception as e:
            reason = 'position_unverifiable'
            msg = (f"{symbol} 托管持仓一致性校验失败({e})，已隔离该品种："
                   f"不自动平仓、翻转或补挂止损，请人工核对。")
        else:
            if matches:
                if self._stop_anomalies.get(symbol) in (
                        'position_mismatch', 'position_unverifiable', 'flat_unconfirmed'):
                    self._stop_anomalies.pop(symbol, None)
                    logger.info(f"{symbol} 交易所持仓已恢复与本地托管记录一致")
                return True
            reason = 'position_mismatch'
            msg = (f"{symbol} 交易所实际持仓与本地托管记录的方向或数量不一致，"
                   f"可能存在人工加减仓/反向操作。已隔离该品种："
                   f"不自动平仓、翻转或补挂止损，请人工核对。")

        logger.critical(msg)
        if self._stop_anomalies.get(symbol) != reason:
            self.notifier.notify_error(msg)
        self._stop_anomalies[symbol] = reason
        return False

    def _ensure_stop_order_alive(self, symbol, ccxt_symbol, position, strategy_name):
        """止损自愈：本地与交易所都有仓时，确认止损单仍挂在交易所；丢失则按本地止损价补挂。

        覆盖「建新止损失败 / 人工误撤 / 交易所端丢单」等任何原因的止损缺失——巡检周期内
        自动恢复保护，不再只靠告警等人工。fail-safe：查询失败、残留阻断、状态不明时一律不动。
        三态判定由适配层 find_stop_order_state 完成（方向+触发价+张数严格匹配才算 intact，
        张数换算不外泄）：intact 不动 / mismatch 告警人工不补挂（防双止损）/ missing 补挂。
        异常状态记入 self._stop_anomalies（前端展示 + 告警只在状态首次进入时发，防止巡检轰炸）。
        """
        if self.trade_state.has_stop_residue(symbol):
            return  # 残留阻断品种状态不明，交由无仓后的清理流程处理
        stop_price = position.get('stop_loss_price')
        if not stop_price:
            return
        stop_order_id = position.get('stop_order_id')
        try:
            # 三态判定下沉到适配层：方向+触发价+张数严格匹配才算 intact（张数换算不外泄）
            state = self.exchange_api.find_stop_order_state(
                ccxt_symbol, position.get('side'), position.get('position_size'),
                stop_price, stop_order_id)
        except Exception as e:
            logger.warning(f"{symbol} [{strategy_name}] 止损存在性检查失败，跳过本轮: {e}")
            return
        if state == 'intact':
            self._stop_anomalies.pop(symbol, None)  # 状态恢复正常，解除前端警示
            return
        if state == 'mismatch':
            # 内容不符或存在额外/重复单：自动补挂会扩大冲突，必须人工裁决
            msg = (f"{symbol} 止损单状态异常：交易所算法单不是唯一一张与本地记录完全一致的 "
                   f"reduce-only 止损（可能内容不符、人工额外挂单或重复单）。"
                   f"已暂停该品种自动补挂，请立即人工核对欧易委托！")
            logger.critical(msg)
            if self._stop_anomalies.get(symbol) != 'mismatch':
                self.notifier.notify_error(msg)  # 只在状态首次进入时发钉钉，巡检每轮重复判定不轰炸
            self._stop_anomalies[symbol] = 'mismatch'
            return

        logger.warning(f"{symbol} [{strategy_name}] 持仓缺少止损单（记录ID={stop_order_id}），按本地止损价 {stop_price} 补挂")
        stop_order = self.exchange_api.create_stop_loss_order(
            ccxt_symbol, position['side'], position['position_size'], stop_price)
        if not stop_order:
            msg = f"{symbol} 持仓缺少止损单且自动补挂失败，仓位暂无止损保护，请立即人工处理！"
            logger.critical(msg)
            if self._stop_anomalies.get(symbol) != 'replant_failed':
                self.notifier.notify_error(msg)
            self._stop_anomalies[symbol] = 'replant_failed'
            return
        self._stop_anomalies.pop(symbol, None)
        self._update_trade_state_stop_with_runtime_fallback(
            symbol, stop_price, stop_order.get('id'), "巡检补挂止损")
        self.notifier.send_message(
            f"[{self.label}] 止损已自动补挂",
            f"{symbol} 检测到持仓缺少止损单，已按本地止损价 {stop_price} 补挂\n"
            f"新止损单ID: {stop_order.get('id')}\n请顺手核对欧易当前委托")

    def _cancel_stop_order_confirmed(self, symbol, ccxt_symbol, stop_order_id):
        """撤销旧止损并确认撤干净（okx 适配层为验证式撤销）。

        不可确认时：标记止损残留（持久化）、严重告警、返回 False——
        调用方此时不得创建新止损或反手开仓，残留的 reduce-only 单可能错杀未来新仓。
        确认成功时顺带解除该品种的残留标记。
        """
        ok = False
        if stop_order_id:
            ok = bool(self.exchange_api.cancel_order(ccxt_symbol, stop_order_id))
        # 按 ID 撤掉账本止损后仍要清扫全部挂单：交易所可能还有超时重发造成的
        # 重复止损或人工额外算法单。仓位已结束/正在回滚时留下它们会误伤未来仓位。
        # cancel_all_orders 自身会同时验证普通单与算法单都已清空，其结果是最终裁决。
        ok = bool(self.exchange_api.cancel_all_orders(ccxt_symbol))
        if ok:
            try:
                self.trade_state.clear_stop_residue(symbol)
            except TradeStatePersistenceError as e:
                # 交易所端已确认撤干净；磁盘中保留旧残留标记只会继续阻断开仓，
                # 属于安全失败。不得因此中断调用方正在执行的紧急回滚平仓。
                msg = (f"{symbol} 止损已确认撤销，但清除残留标记落盘失败: {e}。"
                       f"当前继续保持开仓阻断，请修复磁盘后重试。")
                logger.critical(msg)
                self.notifier.notify_error(msg)
            return True
        try:
            self.trade_state.mark_stop_residue(symbol)
        except TradeStatePersistenceError as e:
            # 不可确认的止损必须至少在当前进程内阻断再开仓；同时让调用方
            # 继续紧急平仓，不让一次落盘失败把真实仓位留在交易所。
            self.trade_state.force_runtime_mark_stop_residue(symbol)
            msg = (f"{symbol} 止损撤销不可确认，且残留标记落盘失败: {e}。"
                   f"已在当前进程强制阻断再开仓，请立即修复磁盘并核对欧易委托。")
            logger.critical(msg)
            self.notifier.notify_error(msg)
        msg = (f"{symbol} 旧止损单撤销无法确认，可能残留！已阻断该品种新开仓，"
               f"系统将在每日检查时自动重试清理；请人工核对欧易当前委托")
        logger.critical(msg)
        self.notifier.notify_error(msg)
        return False

    def _retry_clear_stop_residues(self):
        """每日检查前重试清理止损残留：只处理已无持仓的品种（有持仓时“残留”即本地记录的现行止损，不能盲撤）。"""
        for residue_symbol in list(self.trade_state.get_stop_residues().keys()):
            if self.trade_state.get_open_position(residue_symbol):
                continue
            try:
                ccxt_sym = self.exchange_api.to_ccxt_symbol(residue_symbol)
                # 必须连续证明无仓后才清单；单次瞬时空响应不能作为撤掉人工仓保护单的依据。
                if not self.exchange_api.confirm_position_flat(ccxt_sym):
                    logger.warning(
                        f"{residue_symbol} 残留清理跳过：无法连续证明交易所无仓，"
                        "可能存在人工持仓，请人工核对")
                    continue
                if self.exchange_api.cancel_all_orders(ccxt_sym):
                    self.trade_state.clear_stop_residue(residue_symbol)
                    logger.warning(f"{residue_symbol} 止损残留已确认清理，解除开仓阻断")
                    self.notifier.send_message(
                        "止损残留已清理", f"{residue_symbol} 残留挂单已确认撤销，恢复正常开仓")
            except Exception as e:
                logger.error(f"{residue_symbol} 止损残留清理重试失败: {e}")

    def _handle_exchange_flat_close(self, symbol, ccxt_symbol, position, exit_price, context):
        """「本地有仓、交易所已无仓」的统一确认收尾（启动同步/盘中巡检/日检共用）：

        记平本地仓位（落盘失败走运行时补偿）后，只要记平成功——哪怕落盘失败——
        都必须验证式确认旧止损条件单已消失：手动/外部平仓留下的残留 reduce-only 单
        会错杀未来新仓，不可确认则标记残留阻断该品种开仓。
        返回 (closed_position, state_saved, stop_cleared)；closed 为 None 时调用方直接放弃。
        """
        closed_position, state_saved = self._close_trade_state_with_runtime_fallback(symbol, exit_price, context)
        if not closed_position:
            return None, False, False
        stop_cleared = self._cancel_stop_order_confirmed(symbol, ccxt_symbol, position.get('stop_order_id'))
        return closed_position, state_saved, stop_cleared

    def _close_trade_state_with_runtime_fallback(self, symbol, exit_price, context):
        try:
            closed_position = self.trade_state.close_position(symbol, exit_price)
            state_saved = True
        except TradeStatePersistenceError as e:
            closed_position = self.trade_state.force_runtime_close_position(symbol, exit_price)
            self._notify_trade_state_persistence_issue(symbol, context, e)
            state_saved = False
        if closed_position:
            # 仓位已结束：止损异常警示随仓位生命周期终结（异常单实体由调用方的撤单确认链路负责清理）
            self._stop_anomalies.pop(symbol, None)
        return closed_position, state_saved

    def _update_trade_state_stop_with_runtime_fallback(self, symbol, new_stop_loss_price, stop_order_id, context):
        try:
            return self.trade_state.update_stop_loss(symbol, new_stop_loss_price, stop_order_id), True
        except TradeStatePersistenceError as e:
            position = self.trade_state.force_runtime_update_stop_loss(symbol, new_stop_loss_price, stop_order_id)
            self._notify_trade_state_persistence_issue(symbol, context, e)
            return position, False

    def _notify_trade_state_persistence_issue(self, symbol, context, exc):
        msg = (f"{symbol} {context}后本地状态保存失败: {exc}。\n"
               f"已执行运行时补偿，请立即核对交易所持仓、止损单和本地状态。")
        logger.critical(msg)
        self.notifier.notify_error(msg)
