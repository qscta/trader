"""止损防线子系统（TradingSystem 的 mixin）。

真钱红线的集中地：验证式撤销确认、残留标记与阻断、止损自愈巡检、
「交易所已平」统一确认收尾、本地账本落盘失败的运行时补偿。

以 mixin 形式承载：方法仍绑定在 TradingSystem 实例上——self 语义、
测试对实例方法的桩打法、调用链与日志行为全部不变，只做物理分层。
宿主须提供：exchange_api / trade_state / notifier / config / _trade_lock /
_stop_anomalies / record_stop_loss / get_strategy_for_symbol / _get_strategy_display_name。
"""

import logging

from trade_state import TradeStatePersistenceError

logger = logging.getLogger(__name__)


class StopGuardianMixin:

    def reconcile_intraday_stop_losses(self):
        """盘中巡检：发现本地有仓而交易所端已无仓时，立即按止损/外部平仓处理并通知。"""
        if not self._trade_lock.acquire(blocking=False):
            logger.info("交易检查执行中，跳过盘中止损巡检")
            return

        try:
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

    def _reconcile_symbol_intraday(self, symbol, position, symbol_configs):
        """单品种盘中巡检：持仓核对 + 止损自愈 + 交易所端已平的记账。异常由调用方按品种隔离。"""
        symbol_config = symbol_configs.get(symbol, {
            'name': symbol,
            'enabled': True,
            'risk_per_trade': self.config['strategy']['default_risk_per_trade'],
            # 品种已删但仍有持仓时，与日线主检查保持一致：优先用持仓记录的策略，
            # 否则 ma_cross 持仓会被错当 turtle，漏记 T+1 止损限制
            'strategy': position.get('strategy') or 'turtle'
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
            # 两边都有仓：顺带确认止损单仍然挂着（止损自愈，丢失则按本地止损价补挂）
            self._ensure_stop_order_alive(symbol, ccxt_symbol, position, strategy_name)
            return

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

        if strategy_type == 'turtle':
            self.trade_state.set_signal_state(symbol, False)
            logger.info(f"{symbol} [海龟] 盘中止损巡检已将开仓资格重置，等待下次日检按已收盘日线重新判定")
        else:
            self.record_stop_loss(symbol)
            logger.info(f"{symbol} [双均线] 盘中止损巡检已记录 T+1 限制")

    def _ensure_stop_order_alive(self, symbol, ccxt_symbol, position, strategy_name):
        """止损自愈：本地与交易所都有仓时，确认止损单仍挂在交易所；丢失则按本地止损价补挂。

        覆盖「建新止损失败 / 人工误撤 / 交易所端丢单」等任何原因的止损缺失——巡检周期内
        自动恢复保护，不再只靠告警等人工。fail-safe：查询失败、残留阻断、状态不明时一律不动。
        三态判定由适配层 find_stop_order_state 完成（方向+触发价+张数严格匹配才算 intact，
        张数换算不外泄）：intact 不动 / mismatch 告警人工不补挂（防双止损）/ missing 补挂。
        异常状态记入 self._stop_anomalies（前端展示 + 告警只在状态首次进入时发，防止巡检轰炸）。
        """
        if self.trade_state.has_stop_residue(symbol):
            return  # 残留阻断品种状态不明，交由日检的止损更新流程处理
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
            # id 还在但内容不符（可能被人工改挂）：自动补挂会造成双止损，必须人工裁决
            msg = (f"{symbol} 止损单状态异常：本地记录的止损单仍在交易所，但方向/触发价/张数与本地不符，"
                   f"可能被人工修改过。已暂停该品种自动补挂，请立即人工核对欧易委托！")
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
        if not ok:
            ok = bool(self.exchange_api.cancel_all_orders(ccxt_symbol))
        if ok:
            self.trade_state.clear_stop_residue(symbol)
            return True
        self.trade_state.mark_stop_residue(symbol)
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

    def _handle_exchange_flat_close(self, symbol, ccxt_symbol, position, exit_price, context):
        """「本地有仓、交易所已无仓」的统一确认收尾（启动同步/盘中巡检/两策略日检共用）：

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
