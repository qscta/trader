#!/usr/bin/env python3
"""
交易系统资源监控服务
- 每5分钟检查一次内存和磁盘使用率
- 内存超过85%或磁盘超过90%时，通过钉钉推送告警
- 同一类告警30分钟内只推送一次，避免消息轰炸
- 日志按 5MB 轮转，保留 3 个备份（总量约 20MB）
- 支持 --test 参数发送测试消息
"""

import os
import re
import sys
import json
import time
import subprocess
import requests
import logging
import logging.handlers

# 抹掉错误串里可能随 requests 连接异常带出的 webhook access_token，避免泄露到 mem_monitor.log
_ACCESS_TOKEN_RE = re.compile(r'(access_token=)[^&\s\'"]+', re.IGNORECASE)


def _redact_secrets(text):
    return _ACCESS_TOKEN_RE.sub(r'\1***', str(text))

# ====== 配置 ======
CHECK_INTERVAL = 300        # 检查间隔：5分钟（秒）
MEMORY_THRESHOLD = 85       # 内存告警阈值：85%
DISK_THRESHOLD = 90         # 磁盘告警阈值：90%
ALERT_COOLDOWN = 1800       # 告警冷却时间：30分钟（秒）
TOP_PROCESS_COUNT = 5       # 告警时展示的Top进程数量

# 日志配置
# 交易账本/权益状态所在目录：其所在盘写满比 / 写满更致命，必须一并监控。
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
# systemd 服务用动态低权限用户运行，日志目录由 LogsDirectory 创建；本地手工
# 运行仍默认写项目目录。环境值只接受绝对路径，避免工作目录变化写到意外位置。
_configured_log_file = os.environ.get('TRADING_MEM_MONITOR_LOG', '').strip()
LOG_FILE = (
    _configured_log_file if os.path.isabs(_configured_log_file)
    else os.path.join(DATA_DIR, 'mem_monitor.log'))
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3,
            encoding='utf-8', delay=True),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 从 config.json 读取钉钉 webhook
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')


def load_webhook():
    """优先从环境变量读取 webhook，回退到 config.json"""
    env_webhook = os.environ.get('DINGTALK_WEBHOOK', '').strip()
    if env_webhook:
        return env_webhook

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
        webhook = config.get('dingtalk', {}).get('webhook_url')
        return webhook.strip() if isinstance(webhook, str) else None
    except Exception as e:
        logger.error(f"读取配置文件失败: {e}")
        return None


def send_dingtalk(webhook, msg):
    """发送钉钉告警消息；只有服务端明确接受才返回 True。"""
    try:
        data = {"msgtype": "text", "text": {"content": msg}}
        resp = requests.post(webhook, json=data, timeout=5)
        if resp.status_code == 200:
            result = resp.json()
            if result.get('errcode') == 0:
                logger.info("钉钉告警发送成功")
                return True
            else:
                logger.warning(f"钉钉推送被拒: {result.get('errmsg')}")
        else:
            logger.warning(f"钉钉推送返回非200: {resp.status_code}")
    except Exception as e:
        logger.error(f"钉钉推送失败: {_redact_secrets(e)}")
    return False


def get_memory_usage():
    """获取内存使用率"""
    try:
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
        mem_info = {}
        for line in lines:
            parts = line.split(':')
            if len(parts) == 2:
                key = parts[0].strip()
                value = int(parts[1].strip().split()[0])
                mem_info[key] = value
        total = mem_info.get('MemTotal', 1)
        available = mem_info.get('MemAvailable', 0)
        used_pct = (1 - available / total) * 100
        return round(used_pct, 1), total // 1024, (total - available) // 1024, available // 1024
    except Exception as e:
        logger.error(f"获取内存信息失败: {e}")
        return None, 0, 0, 0


def worst_disk_usage():
    """在 / 与数据目录所在文件系统中取使用率更高者（同盘自动按 st_dev 去重）。

    历史实现只盯 /：若账本/权益状态所在的数据目录挂在独立卷上、该卷写满，
    则完全不告警——而那恰是最致命场景（atomic_write_json 失败、交易记不进账本）。
    这里同时覆盖两处并取更满的一个。返回 (pct, total_gb, used_gb, free_gb, mount)。
    """
    candidates = {}
    for path in ('/', DATA_DIR):
        try:
            dev = os.stat(path).st_dev
        except OSError:
            continue
        if dev in candidates:
            continue  # 与 / 同一文件系统，只报一次
        usage = get_disk_usage(path)
        if usage[0] is not None:
            candidates[dev] = (path, usage)
    if not candidates:
        return None, 0, 0, 0, '/'
    mount, usage = max(candidates.values(), key=lambda item: item[1][0])
    return usage[0], usage[1], usage[2], usage[3], mount


def get_disk_usage(path='/'):
    """获取磁盘使用率"""
    try:
        stat = os.statvfs(path)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used = total - free
        used_pct = (used / total) * 100 if total > 0 else 0
        return round(used_pct, 1), total // (1024**3), used // (1024**3), free // (1024**3)
    except Exception as e:
        logger.error(f"获取磁盘信息失败: {e}")
        return None, 0, 0, 0


def get_top_processes(count=5):
    """获取内存占用最高的 Top N 个进程，只取可执行文件名。

    不得读取 ``ps aux`` 的完整命令行：服务参数可能带 tunnel token、密码或
    其他凭据，把它们拼进钉钉告警会造成二次泄露。
    """
    try:
        result = subprocess.run(
            ['/usr/bin/ps', '-eo', 'comm=,%mem=,%cpu=', '--sort=-rss'],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split('\n')
        processes = []
        for line in lines:
            parts = line.split(None, 2)
            if len(parts) == 3:
                cmd, mem_pct, cpu_pct = parts
                cmd_short = os.path.basename(cmd)
                processes.append({
                    'cmd': cmd_short,
                    'mem': mem_pct,
                    'cpu': cpu_pct
                })
        return processes[:count]
    except Exception as e:
        logger.error(f"获取进程信息失败: {e}")
        return []


def build_memory_alert(mem_pct, mem_total, mem_used, mem_avail, is_test=False):
    """构建内存告警消息"""
    test_tag = "（测试）" if is_test else ""
    top_procs = get_top_processes(TOP_PROCESS_COUNT)

    msg = f"交易服务器内存告警{test_tag}\n"
    msg += f"• 当前使用率: {mem_pct}%（阈值: {MEMORY_THRESHOLD}%）\n"
    msg += f"• 总内存: {mem_total}MB\n"
    msg += f"• 已用: {mem_used}MB\n"
    msg += f"• 可用: {mem_avail}MB\n"

    if top_procs:
        msg += f"Top {len(top_procs)} 进程\n"
        for p in top_procs:
            msg += f"• {p['cmd']} | 内存: {p['mem']}% | CPU: {p['cpu']}%\n"

    return msg


def build_disk_alert(disk_pct, disk_total, disk_used, disk_free, is_test=False, mount='/'):
    """构建磁盘告警消息"""
    test_tag = "（测试）" if is_test else ""

    msg = f"交易服务器磁盘告警{test_tag}\n"
    msg += f"• 挂载点: {mount}\n"
    msg += f"• 当前使用率: {disk_pct}%（阈值: {DISK_THRESHOLD}%）\n"
    msg += f"• 总容量: {disk_total}GB\n"
    msg += f"• 已用: {disk_used}GB\n"
    msg += f"• 可用: {disk_free}GB\n"

    return msg


def send_test_message(webhook):
    """发送测试消息"""
    mem_pct, mem_total, mem_used, mem_avail = get_memory_usage()
    disk_pct, disk_total, disk_used, disk_free, disk_mount = worst_disk_usage()

    msg = build_memory_alert(mem_pct, mem_total, mem_used, mem_avail, is_test=True)
    msg += "\n"
    msg += f"磁盘使用率({disk_mount}): {disk_pct}%（{disk_used}GB/{disk_total}GB）\n"
    msg += "这是一条测试消息，交易系统运行正常。"

    print(f"发送测试消息:\n{msg}")
    return send_dingtalk(webhook, msg)


def main():
    # 支持 --test 参数
    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        webhook = load_webhook()
        if webhook:
            return 0 if send_test_message(webhook) else 1
        else:
            print("无法获取钉钉 webhook")
            return 1

    logger.info("资源监控服务启动")
    logger.info(f"检查间隔: {CHECK_INTERVAL}秒, 内存阈值: {MEMORY_THRESHOLD}%, 磁盘阈值: {DISK_THRESHOLD}%, 告警冷却: {ALERT_COOLDOWN}秒")

    webhook = load_webhook()
    if not webhook:
        logger.error("无法获取钉钉 webhook，退出")
        return 1

    last_alert_time = {
        'memory': 0,
        'disk': 0
    }

    while True:
        try:
            now = time.time()

            # 检查内存
            mem_pct, mem_total, mem_used, mem_avail = get_memory_usage()
            if mem_pct is not None:
                if mem_pct >= MEMORY_THRESHOLD:
                    if now - last_alert_time['memory'] >= ALERT_COOLDOWN:
                        msg = build_memory_alert(mem_pct, mem_total, mem_used, mem_avail)
                        if send_dingtalk(webhook, msg):
                            last_alert_time['memory'] = now
                    else:
                        logger.info(f"内存 {mem_pct}% 超阈值，但在冷却期内，跳过告警")
                else:
                    logger.info(f"内存正常: {mem_pct}% ({mem_used}MB/{mem_total}MB)")

            # 检查磁盘（/ 与数据目录所在盘取更满者）
            disk_pct, disk_total, disk_used, disk_free, disk_mount = worst_disk_usage()
            if disk_pct is not None:
                if disk_pct >= DISK_THRESHOLD:
                    if now - last_alert_time['disk'] >= ALERT_COOLDOWN:
                        msg = build_disk_alert(disk_pct, disk_total, disk_used, disk_free, mount=disk_mount)
                        if send_dingtalk(webhook, msg):
                            last_alert_time['disk'] = now
                    else:
                        logger.info(f"磁盘({disk_mount}) {disk_pct}% 超阈值，但在冷却期内，跳过告警")
                else:
                    logger.info(f"磁盘正常({disk_mount}): {disk_pct}% ({disk_used}GB/{disk_total}GB)")

        except Exception as e:
            logger.error(f"监控循环异常: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    sys.exit(main())
