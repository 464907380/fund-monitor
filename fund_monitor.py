"""
基金盘中监控 — 定时轮询 + 急跌报警

依赖 fund_watch.py 中的数据获取和推送函数。
交易日 9:30-15:00 每 10 分钟检查一次实时估算，
单次跌超阈值或累计跌超时立即企业微信推送。
同时监控基金持仓个股的急涨急跌。
"""
import datetime
import json
import os
import time
import re
from config import CFG
from fund_watch import fetch, send_wechat, log, clear_cache, FUND_LIST, \
    send_mail, _parse_holdings, WECHAT_WEBHOOK, QQ_EMAIL, QQ_AUTH_CODE

# ── 基金急涨急跌阈值 ──────────────────────────
ALERT_DROP_ONCE = CFG.get("fund_monitor", {}).get("alert_drop_once", -3)
ALERT_DROP_ONCE_YELLOW = CFG.get("fund_monitor", {}).get("alert_drop_once_yellow", -2)
ALERT_JUMP_ONCE = CFG.get("fund_monitor", {}).get("alert_jump_once", 3)
ALERT_JUMP_ONCE_YELLOW = CFG.get("fund_monitor", {}).get("alert_jump_once_yellow", 2)
ALERT_ACCUM_DROP = CFG.get("fund_monitor", {}).get("alert_accum_drop", -7)
ALERT_ACCUM_DROP_YELLOW = CFG.get("fund_monitor", {}).get("alert_accum_drop_yellow", -5)
ALERT_ACCUM_JUMP = CFG.get("fund_monitor", {}).get("accum_jump", 7)
ALERT_ACCUM_JUMP_YELLOW = CFG.get("fund_monitor", {}).get("accum_jump_yellow", 5)

# ── 个股急涨急跌阈值（持仓监控） ──────────────
STOCK_DROP_RED = CFG.get("fund_monitor", {}).get("stock_alert_drop_red", -5)
STOCK_DROP_YELLOW = CFG.get("fund_monitor", {}).get("stock_alert_drop_yellow", -3)
STOCK_JUMP_RED = CFG.get("fund_monitor", {}).get("stock_alert_jump_red", 5)
STOCK_JUMP_YELLOW = CFG.get("fund_monitor", {}).get("stock_alert_jump_yellow", 3)
STOCK_ACCUM_DROP_RED = CFG.get("fund_monitor", {}).get("stock_alert_accum_drop_red", -10)
STOCK_ACCUM_DROP_YELLOW = CFG.get("fund_monitor", {}).get("stock_alert_accum_drop_yellow", -7)
STOCK_ACCUM_JUMP_RED = CFG.get("fund_monitor", {}).get("stock_alert_accum_jump_red", 10)
STOCK_ACCUM_JUMP_YELLOW = CFG.get("fund_monitor", {}).get("stock_alert_accum_jump_yellow", 7)

# ── 轮询间隔（秒） ────────────────────────────
POLL_INTERVAL = CFG.get("fund_monitor", {}).get("poll_interval_seconds", 600)

# ── 节假日检测 ────────────────────────────────
# 固定日期节假日（公历）
FIXED_HOLIDAYS = {
    (1, 1),   # 元旦
    (5, 1), (5, 2), (5, 3),   # 劳动节
    (10, 1), (10, 2), (10, 3), (10, 4), (10, 5), (10, 6), (10, 7),  # 国庆
}

_HOLIDAY_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".holiday_cache.json")
_HOLIDAY_CACHE_TTL = CFG.get("fund_monitor", {}).get("holiday_cache_ttl", 86400)

# 连续几次无数据则判定为节假日
MAX_EMPTY_ROUNDS = CFG.get("fund_monitor", {}).get("max_empty_rounds", 2)

# ── 个股监控缓存（持仓一日内不变） ────────────
_holdings_cache: dict[str, list[dict]] = {}

# ── 状态快照文件（进程重启恢复用） ────────────
_STATE_SNAPSHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".monitor_state.json")

# ── 辅助函数 ──────────────────────────────────

def _load_holiday_cache() -> dict:
    """加载已缓存的节假日数据"""
    if os.path.exists(_HOLIDAY_CACHE_FILE):
        try:
            with open(_HOLIDAY_CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)  # type: ignore[no-any-return]
        except Exception:
            pass  # 缓存文件损坏或格式不对，重新获取
    return {}


def _save_holiday_cache(data: dict) -> None:
    """持久化节假日数据"""
    try:
        with open(_HOLIDAY_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log.debug("保存节假日缓存失败: %s", e)


def is_holiday_api(date_str: str) -> bool | None:
    """
    调用节假日 API 判断是否为非交易日。
    返回 True=非交易日, False=交易日, None=API 不可用。
    """
    cache = _load_holiday_cache()
    now_ts = time.time()

    # 缓存命中且未过期
    if date_str in cache:
        entry = cache[date_str]
        if now_ts - entry.get("ts", 0) < _HOLIDAY_CACHE_TTL:
            return entry["holiday"]  # type: ignore[no-any-return]

    # 调用公开节假日 API (https://timor.tech/api/holiday)
    try:
        data = fetch(f"https://timor.tech/api/holiday/info/{date_str}")
        j = json.loads(data)
        if j.get("code") == 0 and "type" in j.get("type", {}):
            holiday = j["type"]["type"] != 0  # 0=工作日, 1=节假日, 2=调休日
            log.debug("节假日 API: %s -> %s", date_str, "非交易日" if holiday else "交易日")
            # 写入缓存
            cache[date_str] = {"holiday": holiday, "ts": now_ts}
            _save_holiday_cache(cache)
            return holiday  # type: ignore[no-any-return]
    except Exception as e:
        log.debug("节假日 API 请求失败: %s", e)

    return None  # API 不可用


def is_trading_day(d: datetime.date) -> bool:
    """
    判断是否为交易日：
    1. API 检测（优先）
    2. 周末判断
    3. 固定假日列表
    """
    date_str = d.isoformat()

    # 优先使用 API（覆盖春节、清明等农历节日及调休）
    api_result = is_holiday_api(date_str)
    if api_result is not None:
        return not api_result  # API 返回 True=非交易日

    # API 不可用时的后备逻辑
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
    url = f"https://hq.sinajs.cn/list={sina_code}"
    try:
        data = fetch(url)
        m = re.search(r'"(.*?)"', data)
        if not m:
            return None
        parts = m.group(1).split(",")
        if len(parts) < 4:
            return None
        name = parts[0]
        prev_close = float(parts[2]) if parts[2] else 0
        current = float(parts[3]) if parts[3] else 0
        if prev_close:
            chg = round((current - prev_close) / prev_close * 100, 2)
            return name, chg
        return None
    except Exception as e:
        log.debug("获取个股 %s 失败: %s", sina_code, e)
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


def check_holdings_intraday(fund_code: str, fund_name: str,
                            stock_states: dict[str, dict]) -> list[str]:
    """
    盘中检查基金持仓个股的涨跌，返回警报列表。
    stock_states: 个股状态字典（key=f"{fund_code}:{stock_code}"）
    """
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
                f"🚩 <font color=\"warning\">{fund_name}持仓{stock_name}({stock_code})"
                f"急跌 {diff:+.1f}%（当前涨{chg:+.2f}%，占比{ratio:.1f}%）</font>"
            )
        elif diff <= STOCK_DROP_YELLOW:
            alerts.append(
                f"🟡 {fund_name}持仓{stock_name}({stock_code})"
                f"下跌 {diff:+.1f}%（当前涨{chg:+.2f}%，占比{ratio:.1f}%）"
            )
        elif diff >= STOCK_JUMP_RED:
            alerts.append(
                f"🚩 <font color=\"info\">{fund_name}持仓{stock_name}({stock_code})"
                f"急涨 {diff:+.1f}%（当前涨{chg:+.2f}%，占比{ratio:.1f}%）</font>"
            )
        elif diff >= STOCK_JUMP_YELLOW:
            alerts.append(
                f"🟢 {fund_name}持仓{stock_name}({stock_code})"
                f"上涨 {diff:+.1f}%（当前涨{chg:+.2f}%，占比{ratio:.1f}%）"
            )

        # ── 累计涨跌幅检测（从当天首次检查到现在的总变动） ──
        first_chg = state["first_chg"]
        accum = chg - first_chg
        if accum <= STOCK_ACCUM_DROP_RED:
            alerts.append(
                f"🚩 <font color=\"warning\">{fund_name}持仓{stock_name}({stock_code})"
                f"当日累计急跌 {accum:.1f}%（{first_chg:+.2f}%→{chg:+.2f}%，占比{ratio:.1f}%）</font>"
            )
        elif accum <= STOCK_ACCUM_DROP_YELLOW:
            alerts.append(
                f"🟡 {fund_name}持仓{stock_name}({stock_code})"
                f"当日累计下跌 {accum:.1f}%（{first_chg:+.2f}%→{chg:+.2f}%，占比{ratio:.1f}%）"
            )
        elif accum >= STOCK_ACCUM_JUMP_RED:
            alerts.append(
                f"🚩 <font color=\"info\">{fund_name}持仓{stock_name}({stock_code})"
                f"当日累计急涨 {accum:.1f}%（{first_chg:+.2f}%→{chg:+.2f}%，占比{ratio:.1f}%）</font>"
            )
        elif accum >= STOCK_ACCUM_JUMP_YELLOW:
            alerts.append(
                f"🟢 {fund_name}持仓{stock_name}({stock_code})"
                f"当日累计上涨 {accum:.1f}%（{first_chg:+.2f}%→{chg:+.2f}%，占比{ratio:.1f}%）"
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

    # 区分基金警报和持仓个股警报
    fund_alerts = [a for a in alerts if "持仓" not in a]
    stock_alerts = [a for a in alerts if "持仓" in a]

    parts = []
    if fund_alerts:
        parts.append("**📈 基金警报**\n\n" + "\n\n".join(fund_alerts))
    if stock_alerts:
        parts.append("**📋 持仓个股警报**\n\n" + "\n\n".join(stock_alerts))

    content = "\n\n".join(parts)

    if WECHAT_WEBHOOK:
        send_wechat(content)
    else:
        # 邮件格式（纯文本）
        text = "🚨 盘中警报\n\n" + "\n".join(
            re.sub(r'<[^>]+>', '', a) for a in alerts
        )
        send_mail("🚨 基金盘中警报", text)


def push_summary(states: dict[str, dict], stock_info: dict[str, dict] | None = None) -> None:
    """
    收盘后发送盘中汇总
    stock_info: 个股汇总文本（可选）
    """
    if not states and not stock_info:
        return

    lines = ["📊 **盘中监控汇总**", ""]

    if states:
        lines.append("**📈 基金**")
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

    if stock_info:
        if states:
            lines.append("")
        lines.append("**📋 个股监控**")
        for key, s in sorted(stock_info.items()):
            lines.append(f"**{s['name']}({key.split(':')[1]})** "
                         f"涨跌 {s['chg']:+.2f}% "
                         f"| 最大涨 {s['max_chg']:+.2f}% 最大跌 {s['min_chg']:+.2f}%")

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
    log.info("基金阈值: 单次超过%+.0f%%(黄)/%+.0f%%(红), 累计超过%+.0f%%(黄)/%+.0f%%(红)",
             ALERT_JUMP_ONCE_YELLOW, ALERT_JUMP_ONCE,
             ALERT_ACCUM_JUMP_YELLOW, ALERT_ACCUM_JUMP)
    log.info("个股阈值: 单次超过%+.0f%%(黄)/%+.0f%%(红), 累计超过%+.0f%%(黄)/%+.0f%%(红)",
             STOCK_JUMP_YELLOW, STOCK_JUMP_RED,
             STOCK_ACCUM_JUMP_YELLOW, STOCK_ACCUM_JUMP_RED)

    today = datetime.date.today().isoformat()
    states: dict[str, dict] = {}
    stock_states: dict[str, dict] = {}
    empty_rounds = 0  # 连续无数据轮次，用于判定休市日
    hold_loaded = False  # 当日是否已加载持仓
    fund_failures: dict[str, int] = {}  # 基金连续失败计数

    # 尝试从快照恢复（进程重启时保留当日累计数据）
    recovered = _load_snapshot(today)
    if recovered:
        states, stock_states, empty_rounds, hold_loaded = recovered

    while True:
        now = datetime.datetime.now()

        # 当天已收盘 → 推送汇总，等明天
        if now.time() >= datetime.time(15, 5):
            if states or stock_states:
                push_summary(states, stock_states)
                log.info("收盘汇总已推送（含 %d 只基金 + %d 只个股）",
                         len(states), len(stock_states))
            states.clear()
            stock_states.clear()
            _holdings_cache.clear()
            hold_loaded = False
            fund_failures.clear()
            _clear_snapshot()
            wait_until_next_trading()
            today = datetime.date.today().isoformat()
            continue

        # 非交易时段 → 等
        if not is_trading_time(now):
            time.sleep(60)
            continue

        # 交易中：首次检查时预加载所有基金持仓
        if not hold_loaded:
            for f in FUND_LIST:
                _get_fund_holdings(f["code"])
            hold_loaded = True
            log.info("持仓数据加载完毕")

        # 轮询检查每只基金 + 持仓个股
        all_alerts = []
        got_data = False
        for f in FUND_LIST:
            code = f["code"]
            if code not in states:
                states[code] = {}
            alerts = check_intraday(code, states[code])
            all_alerts.extend(alerts)
            has_data = states[code].get("last_td") is not None
            if has_data:
                got_data = True
                fund_failures[code] = 0  # 成功则清零
            else:
                fund_failures[code] = fund_failures.get(code, 0) + 1
                if fund_failures[code] == 3:
                    log.warning("%s 连续 3 次检查无数据", code)
                elif fund_failures[code] == 10:
                    log.warning("%s 连续 10 次检查无数据，可能该基金数据源异常", code)

            # 检查该基金的持仓个股
            fund_name = states[code].get("name", code)
            stock_alerts = check_holdings_intraday(code, fund_name, stock_states)
            all_alerts.extend(stock_alerts)

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
        if all_alerts:
            push_alert(all_alerts)
            log.info("推送 %d 条盘中警报（基金 %d 条, 个股 %d 条）",
                     len(all_alerts),
                     sum(1 for a in all_alerts if "持仓" not in a),
                     sum(1 for a in all_alerts if "持仓" in a))
        else:
            log.debug("本轮检查无警报")

        # 持久化状态快照（进程崩溃恢复用）
        _save_snapshot(states, stock_states, today, empty_rounds, hold_loaded)

        # 等待到下一轮
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    monitor()
