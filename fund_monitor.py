"""
基金盘中监控 — 定时轮询 + 急跌报警

依赖 fund_watch.py 中的数据获取和推送函数。
交易日 9:30-15:00 每 10 分钟检查一次实时估算，
单次跌超阈值或累计跌超时立即企业微信推送。
"""
import datetime
import time
import re
import json
from fund_watch import fetch, send_wechat, log, clear_cache, FUND_LIST, \
    send_mail, WECHAT_WEBHOOK, QQ_EMAIL, QQ_AUTH_CODE

# ── 急涨急跌阈值 ──────────────────────────────
ALERT_DROP_ONCE = -3       # 单次估值跌幅超 -3% → 🚩 推送
ALERT_DROP_ONCE_YELLOW = -2    # 单次跌幅超 -2% → 🟡 推送
ALERT_JUMP_ONCE = 3        # 单次估值涨幅超 +3% → 🚩 推送
ALERT_JUMP_ONCE_YELLOW = 2     # 单次涨幅超 +2% → 🟡 推送
ALERT_ACCUM_DROP = -7      # 当日累计跌幅超 -7% → 🚩 推送
ALERT_ACCUM_DROP_YELLOW = -5   # 当日累计跌幅超 -5% → 🟡 推送
ALERT_ACCUM_JUMP = 7       # 当日累计涨幅超 +7% → 🚩 推送
ALERT_ACCUM_JUMP_YELLOW = 5    # 当日累计涨幅超 +5% → 🟡 推送

# ── 轮询间隔（秒） ────────────────────────────
POLL_INTERVAL = 10 * 60  # 10 分钟

# 简单节假日列表（农历/调休不处理，只标记固定节日）
# 如果当天请求发现无实时数据则自动跳过
# 简单节假日列表（仅标注固定日期，浮动节假日由智能检测补充）
# 春节/清明/端午/中秋等农历节日每年变化，不在此列出
FIXED_HOLIDAYS = {
    (1, 1),   # 元旦
    (5, 1), (5, 2), (5, 3),   # 劳动节
    (10, 1), (10, 2), (10, 3), (10, 4), (10, 5), (10, 6), (10, 7),  # 国庆
}

# 连续几次无数据则判定为节假日
MAX_EMPTY_ROUNDS = 2


# ── 辅助函数 ──────────────────────────────────

def is_trading_day(d: datetime.date) -> bool:
    """判断是否为交易日（周一到周五，非固定假日）"""
    if d.weekday() >= 5:
        return False
    if (d.month, d.day) in FIXED_HOLIDAYS:
        return False
    return True


def is_trading_time(dt: datetime.datetime) -> bool:
    """判断是否在交易时段内（9:30-11:30, 13:00-15:00）"""
    t = dt.time()
    if t < datetime.time(9, 30) or t >= datetime.time(15, 0):
        return False
    if datetime.time(11, 30) <= t < datetime.time(13, 0):
        return False  # 午休
    return True


def wait_until_next_trading() -> None:
    """等到下一个交易日开盘（9:30）"""
    now = datetime.datetime.now()
    next_day = now.date()

    while True:
        # 先找下一个交易日
        while not is_trading_day(next_day):
            next_day += datetime.timedelta(days=1)

        # 如果是今天，且还没收盘，等到 9:30 即可
        if next_day == now.date():
            if now.time() < datetime.time(9, 30):
                target = datetime.datetime.combine(next_day, datetime.time(9, 30))
                wait = (target - now).total_seconds()
                log.info("距开盘还有 %.0f 分钟，等待中...", wait / 60)
                time.sleep(wait)
                return
            elif now.time() < datetime.time(15, 0):
                return  # 已经在交易中
            else:
                # 今天已收盘，看下一个交易日
                next_day += datetime.timedelta(days=1)
        else:
            # 不是今天，等到那天 9:30
            target = datetime.datetime.combine(next_day, datetime.time(9, 30))
            wait = (target - now).total_seconds()
            log.info("距下一个交易日还有 %.1f 小时，等待中...", wait / 3600)
            time.sleep(min(wait, 3600))  # 最多等 1 小时再重新判断


def check_intraday(code: str, state: dict) -> list[str]:
    """
    盘中检查一只基金，返回警报列表
    state: 当日状态（内存中维护）
    """
    alerts: list[str] = []

    # 不缓存实时数据（每次都最新）
    clear_cache()

    try:
        gz = fetch(f"https://fundgz.1234567.com.cn/js/{code}.js")
        m = re.search(r'"fundcode":"([^"]+)","name":"([^"]*)","gszzl":"([-\d.]+)"', gz)
        if not m:
            return []

        code_r = m.group(1)
        name = m.group(2) or code
        gszzl = float(m.group(3))  # 实时估算涨跌幅

        # 初始化当日状态
        if "first_td" not in state:
            state["first_td"] = gszzl
            state["last_td"] = gszzl
            state["min_td"] = gszzl
            state["max_td"] = gszzl
            state["name"] = name
            return []

        prev = state["last_td"]

        # ── 单次急涨急跌检测 ──
        diff_once = gszzl - prev  # 与上次检查的差值
        if diff_once <= ALERT_DROP_ONCE:
            alerts.append(
                f"🚩 <font color=\"warning\">{name}({code}) 急跌 {diff_once:+.1f}%"
                f"（当前{gszzl:+.2f}%）</font>"
            )
        elif diff_once <= ALERT_DROP_ONCE_YELLOW:
            alerts.append(
                f"🟡 {name}({code}) 下跌 {diff_once:+.1f}%（当前{gszzl:+.2f}%）"
            )
        elif diff_once >= ALERT_JUMP_ONCE:
            alerts.append(
                f"🚩 <font color=\"info\">{name}({code}) 急涨 {diff_once:+.1f}%"
                f"（当前{gszzl:+.2f}%）</font>"
            )
        elif diff_once >= ALERT_JUMP_ONCE_YELLOW:
            alerts.append(
                f"🟢 {name}({code}) 上涨 {diff_once:+.1f}%（当前{gszzl:+.2f}%）"
            )

        # ── 累计涨跌幅检测 ──
        accum = gszzl - state["first_td"]  # 从当天第一次检查到现在的总变动
        if accum <= ALERT_ACCUM_DROP:
            alerts.append(
                f"🚩 <font color=\"warning\">{name}({code}) 当日累计跌 {accum:.1f}%"
                f"（{state['first_td']:+.2f}%→{gszzl:+.2f}%）</font>"
            )
        elif accum <= ALERT_ACCUM_DROP_YELLOW:
            alerts.append(
                f"🟡 {name}({code}) 当日累计跌 {accum:.1f}%"
                f"（{state['first_td']:+.2f}%→{gszzl:+.2f}%）"
            )
        elif accum >= ALERT_ACCUM_JUMP:
            alerts.append(
                f"🚩 <font color=\"info\">{name}({code}) 当日累计涨 {accum:.1f}%"
                f"（{state['first_td']:+.2f}%→{gszzl:+.2f}%）</font>"
            )
        elif accum >= ALERT_ACCUM_JUMP_YELLOW:
            alerts.append(
                f"🟢 {name}({code}) 当日累计涨 {accum:.1f}%"
                f"（{state['first_td']:+.2f}%→{gszzl:+.2f}%）"
            )

        # 更新状态
        state["last_td"] = gszzl
        state["min_td"] = min(state["min_td"], gszzl)
        state["max_td"] = max(state["max_td"], gszzl)

    except Exception as e:
        log.debug("盘中检查 %s 失败: %s", code, e)

    return alerts


def push_alert(alerts: list[str]) -> None:
    """推送盘中警报"""
    if not alerts:
        return

    content = "**🚨 盘中警报**\n\n" + "\n\n".join(alerts)

    if WECHAT_WEBHOOK:
        send_wechat(content)
    else:
        # 邮件格式（纯文本）
        text = "🚨 盘中警报\n\n" + "\n".join(
            re.sub(r'<[^>]+>', '', a) for a in alerts
        )
        send_mail("🚨 基金盘中警报", text)


def push_summary(states: dict[str, dict]) -> None:
    """
    收盘后发送盘中汇总
    """
    if not states:
        return

    lines = ["📊 **盘中监控汇总**", ""]
    for code, s in states.items():
        name = s.get("name", code)
        first = s.get("first_td", 0)
        last = s.get("last_td", 0)
        low = s.get("min_td", 0)
        high = s.get("max_td", 0)
        lines.append(
            f"**{name}({code})** "
            f"波动 {high:+.1f}%~{low:+.1f}% "
            f"| 收盘估算 {last:+.2f}%"
        )

    content = "\n".join(lines)
    if WECHAT_WEBHOOK:
        send_wechat(content)
    else:
        text = re.sub(r'\*\*', '', content)
        send_mail("📊 盘中监控汇总", text)


# ── 监控主循环 ────────────────────────────────

def monitor() -> None:
    """盘中监控主循环"""
    log.info("====== 盘中监控启动 ======")
    log.info("推送方式: %s", "企业微信" if WECHAT_WEBHOOK else "邮件")
    log.info("监控基金: %d 只", len(FUND_LIST))
    log.info("轮询间隔: %d 分钟", POLL_INTERVAL // 60)
    log.info("涨跌阈值: 单次超过%+.0f%%(黄)/%+.0f%%(红), 累计超过%+.0f%%(黄)/%+.0f%%(红)",
             ALERT_JUMP_ONCE_YELLOW, ALERT_JUMP_ONCE,
             ALERT_ACCUM_JUMP_YELLOW, ALERT_ACCUM_JUMP)
    log.info("跌阈值: 单次低于%+.0f%%(黄)/%+.0f%%(红), 累计低于%+.0f%%(黄)/%+.0f%%(红)",
             ALERT_DROP_ONCE_YELLOW, ALERT_DROP_ONCE,
             ALERT_ACCUM_DROP_YELLOW, ALERT_ACCUM_DROP)

    today = datetime.date.today()
    states: dict[str, dict] = {}
    empty_rounds = 0  # 连续无数据轮次，用于判定休市日

    while True:
        now = datetime.datetime.now()

        # 当天已收盘 → 推送汇总，等明天
        if now.time() >= datetime.time(15, 5):
            if states:
                push_summary(states)
                log.info("收盘汇总已推送")
            states.clear()
            wait_until_next_trading()
            today = datetime.date.today()
            continue

        # 非交易时段 → 等
        if not is_trading_time(now):
            time.sleep(60)
            continue

        # 交易中：轮询检查每只基金
        all_alerts = []
        got_data = False
        for f in FUND_LIST:
            code = f["code"]
            if code not in states:
                states[code] = {}
            alerts = check_intraday(code, states[code])
            all_alerts.extend(alerts)
            if states[code].get("last_td") is not None:
                got_data = True

        # 智能节假日检测：所有基金都无实时数据 → 可能是休市日
        if not got_data:
            empty_rounds += 1
            if empty_rounds >= MAX_EMPTY_ROUNDS:
                log.info("连续 %d 轮无实时数据，判定为非交易日，等待下一个交易日...", empty_rounds)
                states.clear()
                wait_until_next_trading()
                today = datetime.date.today()
                empty_rounds = 0
                continue
        else:
            empty_rounds = 0

        # 推送本周期警报
        if all_alerts:
            push_alert(all_alerts)
            log.info("推送 %d 条盘中警报", len(all_alerts))
        else:
            log.debug("本轮检查无警报")

        # 等待到下一轮
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    monitor()
