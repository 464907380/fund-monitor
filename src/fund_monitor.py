"""
基金盘中监控 — 定时轮询 + 急跌报警

依赖 fund_watch.py 中的数据获取函数。
交易日 9:30-15:00 每 10 分钟检查一次实时估算，
单次跌超阈值或累计跌超时立即企业微信推送。
同时监控基金持仓个股的急涨急跌。
"""
import datetime
import json
import os
import time
import re
from config import CFG, api_url, get_secret
from fund_utils import fetch, log, is_trading_day, write_heartbeat, clear_heartbeat, _fetch_fund_estimate, send_wechat, send_mail_html, parse_sina_csv, _strip_html, setup_log

setup_log("monitor.log")
from fund_watch import FUND_LIST, _parse_holdings, _ensure_fund_list_loaded

# ── 基金急涨急跌阈值 ──────────────────────────
ALERT_DROP_ONCE = CFG.get("fund_monitor", {}).get("alert_drop_once", -3)
ALERT_JUMP_ONCE = CFG.get("fund_monitor", {}).get("alert_jump_once", 5)
ALERT_ACCUM_DROP = CFG.get("fund_monitor", {}).get("alert_accum_drop", -7)
ALERT_ACCUM_JUMP = CFG.get("fund_monitor", {}).get("accum_jump", 10)

# ── 个股急涨急跌阈值（持仓监控） ──────────────
STOCK_DROP_RED = CFG.get("fund_monitor", {}).get("stock_alert_drop_red", -5)
STOCK_JUMP_RED = CFG.get("fund_monitor", {}).get("stock_alert_jump_red", 7)
STOCK_ACCUM_DROP_RED = CFG.get("fund_monitor", {}).get("stock_alert_accum_drop_red", -10)
STOCK_ACCUM_JUMP_RED = CFG.get("fund_monitor", {}).get("stock_alert_accum_jump_red", 12)

# ── 轮询间隔（秒） ────────────────────────────
POLL_INTERVAL = CFG.get("fund_monitor", {}).get("poll_interval_seconds", 600)

# ── 节假日检测 ────────────────────────────────
# 连续几次无数据则判定为节假日
MAX_EMPTY_ROUNDS = CFG.get("fund_monitor", {}).get("max_empty_rounds", 2)

# ── 个股监控缓存（持仓一日内不变） ────────────
_holdings_cache: dict[str, list[dict]] = {}

# ── 状态快照文件（进程重启恢复用） ────────────
_STATE_SNAPSHOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".monitor_state.json")

# ── 辅助函数 ──────────────────────────────────

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
            # 长假期间（>3小时）直接睡到目标时间，避免频繁唤醒
            if wait > 10800:
                time.sleep(wait)
            else:
                time.sleep(min(wait, 3600))  # 短等待最多 1 小时再重新判断


# ── 状态快照持久化（进程崩溃恢复用） ──────────

def _save_snapshot(states: dict, stock_states: dict, today: str,
                   empty_rounds: int, hold_loaded: bool) -> None:
    """保存盘中监控状态快照"""
    try:
        snapshot = {
            "today": today,
            "ts": time.time(),
            "states": states,
            "stock_states": stock_states,
            "empty_rounds": empty_rounds,
            "hold_loaded": hold_loaded,
        }
        with open(_STATE_SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
    except Exception as e:
        log.debug("保存状态快照失败: %s", e)


def _load_snapshot(today: str) -> tuple[dict, dict, int, bool] | None:
    """加载当日快照，返回 (states, stock_states, empty_rounds, hold_loaded) 或 None"""
    if not os.path.exists(_STATE_SNAPSHOT):
        return None
    try:
        with open(_STATE_SNAPSHOT, encoding="utf-8") as f:
            snap = json.load(f)
        if snap.get("today") != today:
            _clear_snapshot()  # 旧日期快照，清理掉
            return None
        log.info("已从快照恢复监控状态（%d 只基金 + %d 只个股）",
                 len(snap.get("states", {})), len(snap.get("stock_states", {})))
        return (
            snap.get("states", {}),
            snap.get("stock_states", {}),
            snap.get("empty_rounds", 0),
            snap.get("hold_loaded", False),
        )
    except Exception as e:
        log.debug("读取状态快照失败: %s", e)
        return None


def _clear_snapshot() -> None:
    """删除状态快照"""
    try:
        if os.path.exists(_STATE_SNAPSHOT):
            os.remove(_STATE_SNAPSHOT)
    except Exception as e:
        log.debug("删除状态快照失败: %s", e)


# ── 持仓个股监控 ──────────────────────────────

def _sina_stock_code(code: str) -> str:
    """将基金持仓股票代码转为新浪格式：sh600519 / sz000333 / hk00700 / sh688981"""
    if code.startswith("6") or code.startswith("8"):
        return f"sh{code}"
    # 港股：5 位纯数字（如 00700）或带 hk 前缀
    if code.startswith("hk") or code.startswith("HK"):
        raw = code[2:]
        return f"hk{raw}"
    if code.isdigit() and len(code) == 5:
        return f"hk{code}"
    return f"sz{code}"


def _fetch_stock_change(sina_code: str) -> tuple[str, float] | None:
    """
    从新浪获取个股实时涨跌幅。
    返回 (股票名称, 涨跌幅%)，失败返回 None。
    """
    # 主数据源：新浪
    url = api_url("sina_hq", code=sina_code)
    try:
        data = fetch(url)
        parts = parse_sina_csv(data)
        if parts is not None:
            name = parts[0]
            prev_close = float(parts[2]) if parts[2] else 0
            current = float(parts[3]) if parts[3] else 0
            if prev_close:
                chg = round((current - prev_close) / prev_close * 100, 2)
                return name, chg
    except Exception as e:
        log.debug("新浪获取个股 %s 失败: %s", sina_code, e)

    # 备选：腾讯财经
    try:
        data = fetch(api_url("tencent_realtime", code=sina_code))
        parts = data.split("~")
        if len(parts) > 32 and parts[3]:
            name = parts[1]
            price = float(parts[3])
            prev_close = float(parts[4]) if parts[4] else 0
            if prev_close:
                chg = round((price - prev_close) / prev_close * 100, 2)
                return name, chg
    except Exception as e:
        log.debug("腾讯获取个股 %s 失败: %s", sina_code, e)

    return None


def _get_fund_holdings(code: str) -> list[dict]:
    """获取基金持仓（缓存，盘中不重复请求）"""
    if code not in _holdings_cache:
        holds = _parse_holdings(code)
        if holds:
            log.info("已加载 %s 持仓 %d 只个股", code, len(holds))
            _holdings_cache[code] = holds
        else:
            log.info("%s 无持仓数据", code)
            _holdings_cache[code] = []
    return _holdings_cache.get(code, [])


def _chg_text(chg: float) -> str:
    """涨跌幅文案：涨/跌"""
    return f"当前{'涨' if chg >= 0 else '跌'}{abs(chg):.2f}%"


def check_holdings_intraday(fund_code: str, fund_name: str,
                            stock_states: dict[str, dict]) -> list[str]:
    """
    盘中检查基金持仓个股的涨跌，返回警报列表。
    stock_states: 个股状态字典（key=f"{fund_code}:{stock_code}"）
    """
    now = datetime.datetime.now().strftime("%H:%M")
    alerts: list[str] = []
    holds = _get_fund_holdings(fund_code)
    if not holds:
        return alerts

    checked = 0
    for h in holds:
        stock_code = h.get("c", "")
        stock_name = h.get("n", "")
        ratio = h.get("p", 0)
        if not stock_code:
            continue

        checked += 1
        sina_code = _sina_stock_code(stock_code)
        result = _fetch_stock_change(sina_code)
        if result is None:
            continue
        _, chg = result  # chg = 当前涨跌幅%（相对昨收）

        state_key = f"{fund_code}:{stock_code}"
        if state_key not in stock_states:
            stock_states[state_key] = {
                "first_chg": chg, "last_chg": chg,
                "name": stock_name,
                "chg": chg, "max_chg": chg, "min_chg": chg,
            }
            continue

        prev = stock_states[state_key]["last_chg"]
        state = stock_states[state_key]

        # ── 单次急涨急跌检测（与上次检查的差值） ──
        diff = chg - prev
        if diff <= STOCK_DROP_RED:
            alerts.append(
                f"🔴 <font color=\"warning\">[{now}] {fund_name}持仓{stock_name}({stock_code})"
                f"急跌 {diff:+.1f}%（{_chg_text(chg)}，占比{ratio:.1f}%）</font>"
            )
        elif diff >= STOCK_JUMP_RED:
            alerts.append(
                f"🟢 <font color=\"info\">[{now}] {fund_name}持仓{stock_name}({stock_code})"
                f"急涨 {diff:+.1f}%（{_chg_text(chg)}，占比{ratio:.1f}%）</font>"
            )

        # ── 累计涨跌幅检测（从当天首次检查到现在的总变动） ──
        first_chg = state["first_chg"]
        accum = chg - first_chg
        if accum <= STOCK_ACCUM_DROP_RED:
            alerts.append(
                f"🔴 <font color=\"warning\">[{now}] {fund_name}持仓{stock_name}({stock_code})"
                f"当日累计急跌 {accum:.1f}%（{first_chg:+.2f}%→{chg:+.2f}%，占比{ratio:.1f}%）</font>"
            )
        elif accum >= STOCK_ACCUM_JUMP_RED:
            alerts.append(
                f"🟢 <font color=\"info\">[{now}] {fund_name}持仓{stock_name}({stock_code})"
                f"当日累计急涨 {accum:.1f}%（{first_chg:+.2f}%→{chg:+.2f}%，占比{ratio:.1f}%）</font>"
            )

        # 更新个股状态
        state["last_chg"] = chg
        state["chg"] = chg
        state["max_chg"] = max(state.get("max_chg", chg), chg)
        state["min_chg"] = min(state.get("min_chg", chg), chg)

    if checked:
        log.debug("%s 个股检查: %d 只", fund_name, checked)
    return alerts


# ── 基金盘中检查 ──────────────────────────────

def check_intraday(code: str, state: dict) -> list[str]:
    """
    盘中检查一只基金，返回警报列表
    state: 当日状态（内存中维护）
    """
    now = datetime.datetime.now().strftime("%H:%M")
    alerts: list[str] = []


    try:
        result = _fetch_fund_estimate(code)
        if not result:
            return []
        name, gszzl = result

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
                f"🔴 <font color=\"warning\">[{now}] {name}({code}) 急跌 {diff_once:+.1f}%"
                f"（当前{gszzl:+.2f}%）</font>"
            )
        elif diff_once >= ALERT_JUMP_ONCE:
            alerts.append(
                f"🟢 <font color=\"info\">[{now}] {name}({code}) 急涨 {diff_once:+.1f}%"
                f"（当前{gszzl:+.2f}%）</font>"
            )

        # ── 累计涨跌幅检测 ──
        accum = gszzl - state["first_td"]  # 从当天第一次检查到现在的总变动
        if accum <= ALERT_ACCUM_DROP:
            alerts.append(
                f"🔴 <font color=\"warning\">[{now}] {name}({code}) 当日累计跌 {accum:.1f}%"
                f"（{state['first_td']:+.2f}%→{gszzl:+.2f}%）</font>"
            )
        elif accum >= ALERT_ACCUM_JUMP:
            alerts.append(
                f"🟢 <font color=\"info\">[{now}] {name}({code}) 当日累计涨 {accum:.1f}%"
                f"（{state['first_td']:+.2f}%→{gszzl:+.2f}%）</font>"
            )

        # 更新状态
        state["last_td"] = gszzl
        state["min_td"] = min(state.get("min_td", gszzl), gszzl)
        state["max_td"] = max(state.get("max_td", gszzl), gszzl)

    except Exception as e:
        log.warning("盘中检查 %s 失败: %s", code, e)

    return alerts


def _icon_text(raw: str) -> tuple[str, str]:
    """从原始警报中提取图标(🔴/🟢)和纯文本内容（不含图标）"""
    icon = "🔴" if raw.startswith("🔴") else "🟢"
    text = _strip_html(raw)
    if text.startswith("🔴") or text.startswith("🟢"):
        text = text[1:].strip()
    return icon, text


def _push_html(fund_alerts: list[str],
               stock_groups: dict[str, tuple[str, list[str]]] | None) -> None:
    """推送盘中警报 HTML 邮件"""
    rows = []
    for fund_name, (fund_code, s_alerts) in sorted(stock_groups.items() if stock_groups else []):
        matched_fa = [a for a in fund_alerts if fund_name in a]
        if not matched_fa and not s_alerts:
            continue
        rows.append(_render_fund_section(fund_name, fund_code, matched_fa, s_alerts))

    remaining = [a for a in fund_alerts if not any(
        fn in a for fn in (list(stock_groups.keys()) if stock_groups else [])
    )]
    if remaining:
        html_remaining = ''.join(
            f'<p style="margin:2px 0;font-size:12px;color:{"#ef5350" if a.startswith("🔴") else "#66bb6a"};">{_icon_text(a)[0]} {_icon_text(a)[1]}</p>'
            for a in remaining
        )
        rows.append(f'<tr><td style="padding:6px 12px;"><p style="margin:0 0 4px;font-size:13px;font-weight:600;color:#ccc;">其他</p>{html_remaining}</td></tr>')

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body bgcolor="#000000" style="margin:0;padding:0;background:#000;font-family:'Helvetica Neue','PingFang SC','Microsoft YaHei',Arial,sans-serif;font-size:13px;color:#ccc;">
<table border="0" cellpadding="0" cellspacing="0" width="100%" bgcolor="#000000" style="background:#000;"><tr><td bgcolor="#000000" align="center" style="padding:20px 10px;">
<table border="0" cellpadding="0" cellspacing="0" width="100%" bgcolor="#1a1a1a" style="max-width:600px;background:#1a1a1a;border-radius:8px;overflow:hidden;">
<tr><td bgcolor="#1a1a1a" style="text-align:center;padding:20px 12px 8px;">
<h1 style="margin:0;font-size:18px;color:#e0e0e0;">🚨 盘中警报</h1>
</td></tr>
{''.join(rows)}
</table>
</td></tr></table>
</body>
</html>"""
    send_mail_html("🚨 基金盘中警报", html)


def _render_fund_section(fund_name: str, fund_code: str,
                          matched_fa: list[str], s_alerts: list[str]) -> str:
    """渲染单只基金的警报 HTML 区块"""
    parts = [f'<tr><td style="padding:10px 12px;"><div style="background:#1a1a1a;border:1px solid #333;border-radius:6px;padding:10px;">'
             f'<p style="margin:0 0 6px;font-size:14px;font-weight:600;color:#e0e0e0;">{fund_name}（{fund_code}）</p>']
    for a in matched_fa:
        icon, text = _icon_text(a)
        clean_fa = re.sub(r'^.+?\d{6}\)\s*', '', text)
        color = "#ef5350" if icon == "🔴" else "#66bb6a"
        parts.append(f'<p style="margin:2px 0;font-size:12px;color:{color};">{icon} 基金：{clean_fa}</p>')
    for a in s_alerts:
        icon, text = _icon_text(a)
        clean = text.split("持仓", 1)[-1] if "持仓" in a else text
        color = "#ef5350" if icon == "🔴" else "#66bb6a"
        parts.append(f'<p style="margin:2px 0;font-size:12px;color:{color};">{icon} 持股·{clean}</p>')
    parts.append('</div></td></tr>')
    return '\n'.join(parts)


def push_alert(fund_alerts: list[str], stock_alerts: list[str],
               stock_groups: dict[str, tuple[str, list[str]]] | None = None) -> None:
    """推送盘中警报——按基金分组，涨跌一目了然"""
    if not fund_alerts and not stock_alerts:
        return

    # Markdown 推送（企业微信）
    lines: list[str] = []
    if stock_groups:
        for fund_name, (fund_code, s_alerts) in sorted(stock_groups.items()):
            matched_fa = [a for a in fund_alerts if fund_name in a]
            if not matched_fa and not s_alerts:
                continue
            lines.append("")
            lines.append(f"**{fund_name}（{fund_code}）**")
            for a in matched_fa:
                icon, text = _icon_text(a)
                clean_fa = re.sub(r'^.+?\d{6}\)\s*', '', text)
                lines.append(f"  {icon} 基金：{clean_fa}")
            for a in s_alerts:
                icon, text = _icon_text(a)
                clean = text.split("持仓", 1)[-1] if "持仓" in a else text
                lines.append(f"  {icon} 持股·{clean}")

    remaining = [a for a in fund_alerts if not any(
        fn in a for fn in (list(stock_groups.keys()) if stock_groups else [])
    )]
    if remaining:
        if lines:
            lines.append("")
        lines.append("**其他**")
        for a in remaining:
            icon, text = _icon_text(a)
            lines.append(f"  {icon} {text}")

    content = "\n".join(lines)
    if get_secret("WECHAT_WEBHOOK"):
        send_wechat(content)
    else:
        _push_html(fund_alerts, stock_groups)




# ── 监控主循环 ────────────────────────────────

def monitor() -> None:
    """盘中监控主循环"""
    _ensure_fund_list_loaded()
    write_heartbeat("fund_monitor")
    log.info("====== 盘中监控启动 ======")
    log.info("推送方式: %s", "企业微信" if get_secret("WECHAT_WEBHOOK") else "邮件")
    log.info("监控基金: %d 只", len(FUND_LIST))
    log.info("轮询间隔: %d 分钟", POLL_INTERVAL // 60)
    log.info("基金阈值: 单次超过%+.0f%%, 累计超过%+.0f%%",
             ALERT_JUMP_ONCE,
             ALERT_ACCUM_JUMP)
    log.info("个股阈值: 单次超过%+.0f%%, 累计超过%+.0f%%",
             STOCK_JUMP_RED,
             STOCK_ACCUM_JUMP_RED)

    today = datetime.date.today().isoformat()
    states: dict[str, dict] = {}
    stock_states: dict[str, dict] = {}
    empty_rounds = 0  # 连续无数据轮次，用于判定休市日
    hold_loaded = False  # 当日是否已加载持仓

    # 尝试从快照恢复（进程重启时保留当日累计数据）
    recovered = _load_snapshot(today)
    if recovered:
        states, stock_states, empty_rounds, hold_loaded = recovered

    while True:
        now = datetime.datetime.now()

        # 每次轮询重新读取配置，支持运行中自动更新
        _mc = CFG.get("fund_monitor", {})
        globals()["ALERT_DROP_ONCE"] = _mc.get("alert_drop_once", -3)
        globals()["ALERT_JUMP_ONCE"] = _mc.get("alert_jump_once", 5)
        globals()["ALERT_ACCUM_DROP"] = _mc.get("alert_accum_drop", -7)
        globals()["ALERT_ACCUM_JUMP"] = _mc.get("accum_jump", 10)
        globals()["STOCK_DROP_RED"] = _mc.get("stock_alert_drop_red", -5)
        globals()["STOCK_JUMP_RED"] = _mc.get("stock_alert_jump_red", 7)
        globals()["STOCK_ACCUM_DROP_RED"] = _mc.get("stock_alert_accum_drop_red", -10)
        globals()["STOCK_ACCUM_JUMP_RED"] = _mc.get("stock_alert_accum_jump_red", 12)
        globals()["POLL_INTERVAL"] = _mc.get("poll_interval_seconds", 600)
        globals()["MAX_EMPTY_ROUNDS"] = _mc.get("max_empty_rounds", 2)

        # 当天已收盘 → 清空状态，等明天
        if now.time() >= datetime.time(15, 5):
            states.clear()
            stock_states.clear()
            _holdings_cache.clear()
            hold_loaded = False
            _clear_snapshot()
            wait_until_next_trading()
            today = datetime.date.today().isoformat()
            continue

        # 非交易时段 → 长休眠到下一个交易日开盘
        if not is_trading_time(now):
            wait_until_next_trading()
            continue

        # 交易中：首次检查时预加载所有基金持仓
        if not hold_loaded:
            for f in FUND_LIST:
                _get_fund_holdings(f["code"])
            hold_loaded = True
            log.info("持仓数据加载完毕")

        # 轮询检查每只基金 + 持仓个股
        fund_alerts: list[str] = []
        stock_alerts: list[str] = []
        stock_groups: dict[str, tuple[str, list[str]]] = {}
        got_data = False
        for f in FUND_LIST:
            code = f["code"]
            if code not in states:
                states[code] = {}
            fa = check_intraday(code, states[code])
            fund_alerts.extend(fa)
            if states[code].get("last_td") is not None:
                got_data = True

            # 检查该基金的持仓个股
            fund_name = states[code].get("name", code)
            sa = check_holdings_intraday(code, fund_name, stock_states)
            if sa:
                stock_alerts.extend(sa)
                stock_groups[fund_name] = (code, sa)

        # 智能节假日检测：所有基金都无实时数据 → 可能是休市日
        if not got_data:
            empty_rounds += 1
            if empty_rounds >= MAX_EMPTY_ROUNDS:
                log.info("连续 %d 轮无实时数据，判定为非交易日，等待下一个交易日...", empty_rounds)
                states.clear()
                stock_states.clear()
                _holdings_cache.clear()
                hold_loaded = False
                _clear_snapshot()
                wait_until_next_trading()
                today = datetime.date.today().isoformat()
                empty_rounds = 0
                continue
        else:
            empty_rounds = 0

        # 推送本周期警报
        if fund_alerts or stock_alerts:
            push_alert(fund_alerts, stock_alerts, stock_groups)
            log.info("推送 %d 条盘中警报（基金 %d 条, 个股 %d 条）",
                     len(fund_alerts) + len(stock_alerts),
                     len(fund_alerts), len(stock_alerts))
        else:
            log.debug("本轮检查无警报")

        # 持久化状态快照（进程崩溃恢复用）
        _save_snapshot(states, stock_states, today, empty_rounds, hold_loaded)

        # 等待到下一轮
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        monitor()
    except KeyboardInterrupt:
        pass
    finally:
        clear_heartbeat("fund_monitor")
