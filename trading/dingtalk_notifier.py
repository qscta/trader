import re
import requests
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# requests 的连接/超时异常字符串常带完整 URL（...robot/send?access_token=xxx）。
# 钉钉 webhook 的 access_token 是可轮换的密钥，原样进 trading.log 会经 /api/logs 面板
# 与备份泄露——记录任何含 URL 的错误串前，必须先抹掉 token。
_ACCESS_TOKEN_RE = re.compile(r'(access_token=)[^&\s\'"]+', re.IGNORECASE)


def _redact_secrets(text):
    """抹掉错误串里的钉钉 webhook access_token，供安全落日志。"""
    return _ACCESS_TOKEN_RE.sub(r'\1***', str(text))


class DingTalkNotifier:
    SEND_RETRY_DELAY_SECONDS = 1.0  # 首发失败后重试一次的间隔（网络抖动是常态，告警丢失不可接受）

    def __init__(self, webhook_url):
        self.webhook_url = webhook_url

    def send_message(self, title, content):
        """发送钉钉 markdown 消息（校验 errcode；失败重试一次后仍失败才放弃）。"""
        if not self.webhook_url:
            logger.warning(f"钉钉推送已跳过（未配置 webhook）: {title}")
            return False
        last_err = None
        for attempt in (1, 2):
            try:
                msg = {"msgtype": "markdown", "markdown": {"title": title, "text": content}}
                resp = requests.post(self.webhook_url, json=msg, timeout=10)
                try:
                    payload = resp.json()
                except ValueError:
                    payload = {}
                if resp.status_code == 200 and payload.get('errcode', 0) == 0:
                    logger.info(f"钉钉推送: {title} -> {resp.status_code}")
                    return True
                last_err = f"http={resp.status_code}, body={payload or resp.text[:200]}"
            except Exception as e:
                # 抹掉可能随异常带出的 access_token（requests 连接异常常含完整 URL）
                last_err = _redact_secrets(e)
            if attempt == 1:
                logger.warning(f"钉钉推送首次失败({last_err})，{self.SEND_RETRY_DELAY_SECONDS}s 后重试: {title}")
                time.sleep(self.SEND_RETRY_DELAY_SECONDS)
        logger.error(f"钉钉推送失败(已重试): {title} -> {last_err}")
        return False

    def notify_error(self, error_msg):
        """发送错误通知"""
        content = f"### ⚠️ 交易系统警告\n\n{error_msg}\n\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        return self.send_message("系统警告", content)

    def notify_trade_opened(self, symbol, side, price, size, stop_loss_price):
        """发送开仓通知"""
        side_cn = "做多" if side == 'long' else "做空"
        emoji = "🟢" if side == 'long' else "🔴"
        content = (
            f"### {emoji} 交易系统开仓通知 - {symbol}\n\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"交易对: {symbol}\n\n"
            f"方向: {side_cn}\n\n"
            f"入场价: {price}\n\n"
            f"头寸: {size}\n\n"
            f"止损价: {stop_loss_price}"
        )
        return self.send_message(f"[交易系统] 开仓通知 - {symbol}", content)

    def notify_trade_opened_summary(self, trades):
        """发送本轮开仓通知汇总。"""
        if not trades:
            return False

        lines = []
        for item in trades:
            side_cn = "做多" if item['side'] == 'long' else "做空"
            lines.append(
                f"- {item['symbol']}: {side_cn}, 入场{item['price']}, 头寸{item['size']}, 止损{item['stop_loss_price']}"
            )

        content = (
            f"### 交易系统开仓通知汇总\n\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"开仓数量: {len(trades)}\n\n"
            f"{chr(10).join(lines)}"
        )
        return self.send_message("[交易系统] 开仓通知汇总", content)

    def notify_trade_closed(self, symbol, side, exit_price, pnl, pnl_pct):
        """发送平仓通知"""
        side_cn = "多" if side == 'long' else "空"
        emoji = "💰" if pnl >= 0 else "💔"
        content = (
            f"### {emoji} 交易系统平仓通知 - {symbol}\n\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"交易对: {symbol}\n\n"
            f"方向: {side_cn}\n\n"
            f"平仓价: {exit_price}\n\n"
            f"盈亏: {pnl:.2f}U ({pnl_pct:.2f}%)"
        )
        return self.send_message(f"[交易系统] 平仓通知 - {symbol}", content)

    def notify_trade_closed_summary(self, trades):
        """发送本轮平仓通知汇总。"""
        if not trades:
            return False

        lines = []
        for item in trades:
            side_cn = "多" if item['side'] == 'long' else "空"
            lines.append(
                f"- {item['symbol']}: {side_cn}, 出场{item['exit_price']}, 盈亏{item['pnl']:.2f}U ({item['pnl_pct']:.2f}%)"
            )

        content = (
            f"### 交易系统平仓通知汇总\n\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"平仓数量: {len(trades)}\n\n"
            f"{chr(10).join(lines)}"
        )
        return self.send_message("[交易系统] 平仓通知汇总", content)

    def notify_stop_loss_triggered(self, symbol, strategy_name, side, exit_price, source='日检确认'):
        """发送止损触发通知。"""
        side_cn = "做多" if side == 'long' else "做空"
        content = (
            f"### ⚠️ 交易系统止损触发 - {symbol}\n\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"交易对: {symbol}\n\n"
            f"策略: {strategy_name}\n\n"
            f"方向: {side_cn}\n\n"
            f"出场价(估算): {exit_price}\n\n"
            f"确认来源: {source}"
        )
        return self.send_message(f"[交易系统] 止损触发 - {symbol}", content)

    def notify_stop_loss_updates_summary(self, updates):
        """发送本轮止损更新汇总。"""
        if not updates:
            return False

        lines = []
        for item in updates:
            lines.append(
                f"- {item['symbol']}: {item['old_stop_loss_price']} -> {item['new_stop_loss_price']}"
            )

        content = (
            f"### 交易系统止损更新汇总\n\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"更新数量: {len(updates)}\n\n"
            f"{chr(10).join(lines)}"
        )
        return self.send_message("[交易系统] 止损更新汇总", content)

    def notify_signal_missed(self, symbol, strategy_name, side, reason, signal=None):
        """标准信号已出现但最终未形成持仓时提醒人工复核。"""
        side_cn = "做多" if side == 'long' else "做空"
        content = (
            f"### ⚠️ 交易系统信号未成交提醒 - {symbol}\n\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"交易对: {symbol}\n\n"
            f"策略: {strategy_name}\n\n"
            f"方向: {side_cn}\n\n"
            f"原因: {reason}\n\n"
        )
        if signal:
            current_close = signal.get('current_close')
            upper_line = signal.get('upper_line')
            lower_line = signal.get('lower_line')
            mid_line = signal.get('mid_line')
            if current_close is not None:
                content += f"收盘价: {current_close}\n\n"
            if upper_line is not None and lower_line is not None:
                content += f"上轨/下轨: {upper_line} / {lower_line}\n\n"
            if mid_line is not None:
                content += f"中轨: {mid_line}\n\n"
        return self.send_message(f"[交易系统] 信号未成交 - {symbol}", content)

    def notify_position_summary(self, positions, symbols_config, total_equity):
        """发送每日持仓汇总"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        long_count = sum(1 for p in positions.values() if p.get('side') == 'long')
        short_count = sum(1 for p in positions.values() if p.get('side') == 'short')
        total_count = len(positions)

        total_risk = 0
        details = []
        for sym, pos in positions.items():
            side_cn = "多" if pos.get('side') == 'long' else "空"
            entry = pos.get('entry_price', 0)
            stop = pos.get('stop_loss_price', 0)
            size = pos.get('position_size', 0)
            if pos.get('side') == 'long':
                loss = abs(entry - stop) * size if stop else 0
            else:
                loss = abs(stop - entry) * size if stop else 0
            total_risk += loss
            details.append(f"    {sym}({side_cn}): 入场{entry}, 止损{stop}, 损失{loss:.2f}U")

        equity_known = total_equity is not None and total_equity > 0
        risk_pct = (total_risk / total_equity * 100) if equity_known else None
        equity_text = f"{total_equity:.2f}U" if total_equity is not None else "未知（读取失败）"
        risk_text = f"{risk_pct:.1f}%" if risk_pct is not None else "未知"
        content = (
            f"### 交易系统每日持仓汇总\n\n"
            f"时间: {now}\n\n"
            f"---持仓概览---\n\n"
            f"总持仓数: {total_count}\n\n"
            f"多单数量: {long_count}\n\n"
            f"空单数量: {short_count}\n\n"
            f"当前账户权益: {equity_text}\n\n"
            f"当前实际风险度: {risk_text}\n\n"
            f"全部止损预计亏损: {total_risk:.2f}U\n\n"
            f"---持仓明细---\n\n"
        )
        content += "\n\n".join(details)
        return self.send_message("[交易系统] 每日持仓汇总", content)
