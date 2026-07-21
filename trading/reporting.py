"""通知与报表子系统（TradingSystem 的 mixin）。

开/平仓通知缓冲与汇总、信号未成交提醒、每日持仓汇总（去重）、周报、
权益采样告警——所有「向人汇报」的出口集中在这里，交易逻辑零依赖它的内部实现。

以 mixin 形式承载：方法仍绑定在 TradingSystem 实例上——self 语义、
测试对实例方法的桩打法、调用链与日志行为全部不变，只做物理分层。
宿主须提供：notifier / trade_state / exchange_api / config / equity_tracker /
label / _pending_* 缓冲 / _summary_lock / _last_summary_date / _equity_tick_* 计数。
"""

import logging
from datetime import datetime, date

from equity_tracker import EquityTracker

logger = logging.getLogger(__name__)


class ReportingMixin:

    def _get_strategy_display_name(self, strategy_type):
        return '双均线 EMA'

    def _notify_missing_position_after_signal(self, symbol, strategy_type, side, signal, reason):
        logger.warning(f"{symbol} [{strategy_type}] {reason}")
        self.notifier.notify_signal_missed(
            symbol,
            self._get_strategy_display_name(strategy_type),
            side,
            reason,
            signal=signal
        )

    def _notify_persistence_failure(self, label, filepath):
        """持久化失败告警（供 EquityTracker 回调）。"""
        try:
            self.notifier.send_message(
                f"[{self.label}] 持久化失败告警",
                f"{label}\n文件: {filepath}\n请立即检查磁盘、权限和服务状态"
            )
        except Exception as e:
            logger.error(f"发送持久化失败告警异常: {e}")

    def _buffer_trade_open_notification(self, symbol, side, price, size, stop_loss_price):
        self._pending_trade_open_notifications.append({
            'symbol': symbol,
            'side': side,
            'price': price,
            'size': size,
            'stop_loss_price': stop_loss_price,
        })

    def _buffer_trade_close_notification(self, symbol, side, exit_price, pnl, pnl_pct):
        self._pending_trade_close_notifications.append({
            'symbol': symbol,
            'side': side,
            'exit_price': exit_price,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
        })

    def _flush_pending_trade_notifications(self):
        if self._pending_trade_close_notifications:
            logger.info(
                f"信号检查完毕，推送平仓通知汇总({len(self._pending_trade_close_notifications)}条)..."
            )
            self.notifier.notify_trade_closed_summary(self._pending_trade_close_notifications)

        if self._pending_trade_open_notifications:
            logger.info(
                f"信号检查完毕，推送开仓通知汇总({len(self._pending_trade_open_notifications)}条)..."
            )
            self.notifier.notify_trade_opened_summary(self._pending_trade_open_notifications)

    def send_daily_position_summary(self):
        """每天早上推送持仓汇总"""
        try:
            positions = self.trade_state.get_all_open_positions()
            symbols_config = self.config['trading']['symbols']
            try:
                balance = self.exchange_api.get_balance()
                total_equity = float(balance['total']['USDT']) if balance else None
            except Exception as e:
                logger.warning(f"获取权益失败: {e}")
                total_equity = None
            pushed = self.notifier.notify_position_summary(positions, symbols_config, total_equity)
            if pushed:
                logger.info("每日持仓汇总已推送")
                return True
            logger.error("每日持仓汇总推送失败")
            return False
        except Exception as e:
            logger.error(f"推送持仓汇总失败: {e}")
            return False

    def send_daily_position_summary_if_due(self, force=False, mark_sent=True):
        """每天最多推送一次持仓汇总；失败时允许后续重试。

        加锁保证「查重→推送→标记」原子：独立兜底调度(08:00:50/08:01:20)与
        日检结束后的调用可能并发，不加锁存在双发窗口。
        """
        with self._summary_lock:
            today = date.today().isoformat()
            if self._last_summary_date == today and not force:
                logger.info(f"今日({today})每日持仓汇总已推送，跳过重复推送")
                return False

            if self.send_daily_position_summary():
                if mark_sent:
                    self._last_summary_date = today
                else:
                    logger.info(f"今日({today})每日持仓汇总已推送，本次不标记去重日期")
                return True

            logger.warning(f"今日({today})每日持仓汇总推送未成功，等待后续重试")
            return False

    def send_weekly_report(self):
        """周报：直接复用 equity_tracker.build_account_stats（与前端/API/钉钉同一套统计口径，不再手写）。"""
        try:
            stats = self.equity_tracker.build_account_stats(persist=False)
        except Exception as e:
            logger.error(f"周报: 获取账户统计失败: {e}")
            return
        try:
            open_positions = self.trade_state.get_all_open_positions()
            equity = stats['current_equity']
            peak = stats['peak_equity']
            ytd = stats['ytd_return']
            total_ret = stats['total_return']
            peak_dd = (stats.get('peak_drawdown', 0) or 0) * 100
            mdd = (stats.get('max_drawdown', 0) or 0) * 100
            pmd = (stats.get('potential_max_drawdown', 0) or 0) * 100
            worst = stats['worst_case_equity']
            dsp = stats['days_since_peak']                 # 未创新高天数
            ldd = stats['longest_drawdown_days']           # 历史最长未创新高天数

            enabled_symbols = [s['name'] for s in self.config['trading']['symbols'] if s.get('enabled', True)]
            long_count = sum(1 for p in open_positions.values() if p.get('side') == 'long')
            short_count = sum(1 for p in open_positions.values() if p.get('side') == 'short')
            now = datetime.now().strftime('%Y-%m-%d %H:%M')

            ytd_s = f"{'+' if ytd >= 0 else ''}{ytd:.2f}%"
            tot_s = f"{'+' if total_ret >= 0 else ''}{total_ret:.2f}%"
            # 钉钉为 markdown 消息：每行须以空行(\n\n)分隔，否则会被合并成一段。
            # 标题含「交易系统」、监控行含「交易对」——满足钉钉机器人关键词安全校验，缺则被拒收。
            msg = (
                f"### 📊 交易系统周报 · {now}\n\n"
                f"**收益**　本年 {ytd_s} · 累计 {tot_s}\n\n"
                f"**当前权益**　{equity:.2f} USDT（峰值 {peak:.2f}）\n\n"
                f"**回撤**　峰值 -{peak_dd:.2f}% · 历史最大 -{mdd:.2f}% · 潜在 -{pmd:.2f}%\n\n"
                f"**最低权益**　{worst:.2f} USDT\n\n"
                f"**持仓**　{len(open_positions)} 个 · 监控 {len(enabled_symbols)} 个交易对（多{long_count} 空{short_count}）\n\n"
                f"**未创新高**　{dsp} 天（历史最长 {ldd} 天）"
            )

            self.notifier.send_message('[周报]', msg)
            logger.info("每周报告已推送")
        except Exception as e:
            logger.error(f"推送周报失败: {e}")

    def _record_equity_tick_with_alert(self):
        """记录权益采样，连续失败时告警、恢复时通知（各所独立计数）。"""
        try:
            ok = bool(self.equity_tracker.record_equity_tick())
            if ok:
                if self._equity_tick_alert_sent:
                    self.notifier.send_message(
                        f"[{self.label}] 权益采样恢复",
                        f"权益采样已恢复正常（恢复时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）"
                    )
                self._equity_tick_fail_streak = 0
                self._equity_tick_alert_sent = False
                return
            raise RuntimeError("record_equity_tick returned False")
        except Exception as e:
            self._equity_tick_fail_streak += 1
            logger.warning(f"[{self.label}] 记录权益采样失败（连续{self._equity_tick_fail_streak}次）: {e}")
            if self._equity_tick_fail_streak >= 3 and not self._equity_tick_alert_sent:
                streak_minutes = self._equity_tick_fail_streak * EquityTracker.EQUITY_TICK_INTERVAL_MINUTES
                self.notifier.send_message(
                    f"[{self.label}] 权益采样告警",
                    f"权益采样已连续失败 {self._equity_tick_fail_streak} 次（约 {streak_minutes} 分钟），"
                    f"最近错误: {e}\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self._equity_tick_alert_sent = True
