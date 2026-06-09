"""
基金风险监控 v5.2 — 每日晚报 + 企业微信推送
"""
import json
import logging
import os
import re
import datetime
import time
import urllib.error
import urllib.request
import csv
from email.header import Header
from email.mime.text import MIMEText
import smtplib
from logging.handlers import RotatingFileHandler
from config import CFG

# ── 配置 ──────────────────────────────────────

# 基金列表：优先从 fund_list.json 加载（可编辑），不存在时使用内置默认列表
_FUND_LIST_FALLBACK = [
    {"code": "001438"}, {"code": "180031"}, {"code": "018998"},
    {"code": "000979"}, {"code": "320007"}, {"code": "161725"},
    {"code": "001480"}, {"code": "001753"}, {"code": "001170"},
]
FUND_LIST: list[dict] = []  # 占位，稍后由 _load_fund_list() 填充

# 推送配置（优先级：企业微信 > 邮件。请通过环境变量 WECHAT_WEBHOOK / QQ_EMAIL / QQ_MAIL_AUTH 配置）
WECHAT_WEBHOOK = os.getenv("WECHAT_WEBHOOK", "")
QQ_EMAIL = os.getenv("QQ_EMAIL", "")
QQ_AUTH_CODE = os.getenv("QQ_MAIL_AUTH", "")

ALERT_DROP_1M = CFG["fund_watch"]["alert_drop_1m"]
ALERT_DROP_1M_RED = CFG["fund_watch"]["alert_drop_1m_red"]
ALERT_SCALE_2X = CFG["fund_watch"]["alert_scale_2x"]
ALERT_SCALE_1_5X = CFG["fund_watch"]["alert_scale_1_5x"]

HISTORY_DIR = os.path.dirname(os.path.abspath(__file__))

# ── 日志 ──────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(
            os.path.join(HISTORY_DIR, "fund_watch.log"),
            maxBytes=1_000_000,  # 1MB
            backupCount=3,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# 在日志就绪后加载基金列表
_fund_list_path = os.path.join(HISTORY_DIR, "fund_list.json")
if os.path.exists(_fund_list_path):
    try:
        with open(_fund_list_path, encoding="utf-8") as _f:
            FUND_LIST[:] = json.load(_f)
        log.info("已从 fund_list.json 加载 %d 只基金", len(FUND_LIST))
    except Exception as _e:
        log.warning("读取 fund_list.json 失败 (%s)，使用内置默认列表", _e)
        FUND_LIST[:] = _FUND_LIST_FALLBACK
elif not FUND_LIST:
    FUND_LIST[:] = _FUND_LIST_FALLBACK

_cache: dict[str, tuple[float, str]] = {}       # url -> (timestamp, data)
_CACHE_TTL = CFG["network"]["cache_ttl_seconds"]
_CACHE_MAX = CFG["network"]["cache_max_entries"]

# ── 重试配置 ──────────────────────────────────
_RETRY_MAX = CFG["network"]["retry_max"]
_RETRY_BACKOFF = CFG["network"]["retry_backoff_seconds"]


def _cache_evict() -> None:
    """清除过期缓存；超出上限时清除最旧的条目"""
    now = time.time()
    expired = [k for k, (t, _) in _cache.items() if now - t > _CACHE_TTL]
    for k in expired:
        del _cache[k]
    # 超过最大条数时，按时间戳排序移除最旧的一半
    if len(_cache) > _CACHE_MAX:
        sorted_items = sorted(_cache.items(), key=lambda kv: kv[1][0])
        for k, _ in sorted_items[:len(sorted_items) // 2]:
            del _cache[k]
    log.debug("缓存清理: 过期 %d, 当前 %d 条", len(expired), len(_cache))


def _retry_fetch(url: str) -> str:
    """带指数退避的 HTTP GET 请求"""
    _cache_evict()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for attempt in range(1, _RETRY_MAX + 1):
        try:
            resp = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
            return resp  # type: ignore[no-any-return]
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            last_err = e
            if attempt < _RETRY_MAX:
                wait = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
                log.debug("请求失败，第 %d 次重试 (等待 %ds): %s", attempt, wait, url)
                time.sleep(wait)
    log.warning("请求失败 %s (已重试 %d 次): %s", url, _RETRY_MAX, last_err)
    raise urllib.error.URLError(str(last_err)) if last_err else urllib.error.URLError(f"Request failed: {url}")


def fetch(url: str) -> str:
    entry = _cache.get(url)
    if entry:
        ts, data = entry
        if time.time() - ts <= _CACHE_TTL:
            return data
        del _cache[url]
    resp = _retry_fetch(url)
    _cache[url] = (time.time(), resp)
    return resp


def clear_cache() -> None:
    _cache.clear()


# ── 推送 ──────────────────────────────────────

def send_wechat(content: str, markdown: bool = True) -> bool:
    if not WECHAT_WEBHOOK:
        return False
    msgtype = "markdown" if markdown else "text"
    payload = json.dumps({msgtype: {msgtype: content}}).encode("utf-8")
    req = urllib.request.Request(
        WECHAT_WEBHOOK, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
        log.info("企业微信推送成功")
        return True
    except Exception as e:
        log.error("企业微信推送失败: %s", e)
        return False


def send_mail(subject: str, text: str) -> None:
    """通过 QQ 邮箱发送纯文本邮件（供 fund_monitor.py 使用）"""
    if not QQ_EMAIL or not QQ_AUTH_CODE:
        log.warning("QQ_EMAIL 或 QQ_MAIL_AUTH 未配置，邮件推送跳过")
        return
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")  # type: ignore[assignment]
    msg["From"] = msg["To"] = QQ_EMAIL
    try:
        s = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=10)
        s.login(QQ_EMAIL, QQ_AUTH_CODE)
        s.sendmail(QQ_EMAIL, [QQ_EMAIL], msg.as_string())
        s.quit()
        log.info("邮件发送成功")
    except Exception as e:
        log.error("邮件发送失败: %s", e)


def send_mail_html(subject: str, rows: list[dict], alerts: list[str], today: str) -> None:
    """通过 QQ 邮箱发送邮件（HTML 模板渲染）"""
    if not QQ_EMAIL or not QQ_AUTH_CODE:
        log.warning("QQ_EMAIL 或 QQ_MAIL_AUTH 未配置，邮件推送跳过")
        return
    def _color_cls(val: str) -> str:
        """数值颜色 class"""
        if not val:
            return ""
        if val.startswith("+"):
            return ' class="red"'
        if val.startswith("-"):
            return ' class="green"'
        return ""

    def _strip_html(text: str) -> str:
        return re.sub(r"<[^>]+>", "", text)

    tpl_path = os.path.join(HISTORY_DIR, "email_template.html")
    if not os.path.exists(tpl_path):
        log.warning("email_template.html 不存在，跳过邮件")
        return
    html = open(tpl_path, encoding="utf-8").read()
    html = html.replace("{{DATE}}", today)
    # 表格行
    row_htmls = []
    for r in rows:
        row_htmls.append(
            f'<tr>'
            f'<td>{r["code"]}</td>'
            f'<td>{r["name_short"]}</td>'
            f'<td class="num"{_color_cls(r["day"])}>{r["day"]}</td>'
            f'<td class="num"{_color_cls(r["f5"])}>{r["f5"]}</td>'
            f'<td class="num"{_color_cls(r["m1"])}>{r["m1"]}</td>'
            f'<td class="num"{_color_cls(r["m3"])}>{r["m3"]}</td>'
            f'<td class="num"{_color_cls(r["y1"])}>{r["y1"]}</td>'
            f'<td>{r["mgr"]}</td>'
            f'</tr>'
        )
    html = html.replace("{{ROWS}}", "\n".join(row_htmls))
    # 警报
    if alerts:
        al = '<div class="alerts">'
        al += '<div class="alerts-title">🚨 警报</div>'
        for a in alerts:
            al += f'<div class="alerts-item">{_strip_html(a)}</div>'
        al += '</div>'
        html = html.replace("{{ALERTS}}", al)
    else:
        html = html.replace("{{ALERTS}}", "")
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")  # type: ignore[assignment]
    msg["From"] = msg["To"] = QQ_EMAIL
    try:
        s = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=10)
        s.login(QQ_EMAIL, QQ_AUTH_CODE)
        s.sendmail(QQ_EMAIL, [QQ_EMAIL], msg.as_string())
        s.quit()
        log.info("邮件发送成功")
    except Exception as e:
        log.error("邮件发送失败: %s", e)


def push(subject: str, rows: list[dict], alerts: list[str], today: str) -> None:
    sent = send_wechat(md_content(rows, alerts, today))
    if not sent:
        send_mail_html(subject, rows, alerts, today)


def md_content(rows: list[dict], alerts: list[str], today: str) -> str:
    """构造 Markdown 内容（企业微信推送用）"""
    md_lines = [
        f"📊 **基金晚报 {today}**",
        "",
        "|代码|基金名|涨跌|近5日|近1月|近3月|近1年|经理|",
        "|:---|:---|---:|----:|----:|----:|----:|:---|",
    ]
    for r in rows:
        md_lines.append(
            f"|{r['code']}|{r['name_short']}|{r['day']}|{r['f5']}|{r['m1']}|{r['m3']}|{r['y1']}|{r['mgr']}|"
        )
    if alerts:
        md_lines.append("")
        md_lines.append("**🚨 警报:**")
        for a in alerts:
            md_lines.append(f"> {a}")
    return "\n".join(md_lines)

# ── 数据获取 ──────────────────────────────────

def _parse_name(data: str) -> str | None:
    """从 pingzhongdata JS 中提取基金名称"""
    m = re.search(r'var fS_name\s*=\s*"([^"]+)"', data)
    return m.group(1) if m else None


def _parse_scale(data: str) -> float | None:
    """提取基金规模（亿元）"""
    m = re.findall(r'"y":([\d.]+),"mom":"[\d.-]+%"', data)
    return float(m[-1]) if m else None


def _parse_period_returns(data: str) -> dict:
    """提取阶段收益：近1月/近3月/近1年"""
    result = {}
    for key, js_var in [("m1", "syl_1y"), ("m3", "syl_3y"), ("y1", "syl_1n")]:
        m = re.search(rf'var {js_var}\s*=\s*"([-\d.]+)"', data)
        if m:
            result[key] = float(m.group(1))
    return result


def _parse_price_info(data: str) -> int | None:
    """提取基金价格/净值"""
    m = re.search(r'"data":\[([\d.]+),([\d.]+),([\d.]+),([\d.]+),([\d.]+)\]', data)
    return int(float(m.group(1))) if m else None


def _parse_manager(data: str) -> str | None:
    """提取基金经理"""
    m = re.search(r'Data_currentFundManager.*?"name":"([^"]+)"', data, re.DOTALL)
    return m.group(1) if m else None


def _parse_institutional_ratio(data: str) -> float | None:
    """提取机构持有比例"""
    m = re.search(r'"机构持有比例","data":\[([^\]]+)\]', data)
    if not m:
        return None
    vs = m.group(1).split(",")
    return float(vs[-1].strip()) if vs else None


def _parse_net_trend(data: str) -> list[dict] | None:
    """提取净值趋势（最近6条）"""
    ts = data.find("var Data_netWorthTrend")
    if ts < 0:
        return None
    as_ = data.find("[{", ts)
    if as_ < 0:
        return None
    dep, end = 0, as_
    for i in range(as_, min(as_ + 500000, len(data))):
        if data[i] == "[":
            dep += 1
        elif data[i] == "]":
            dep -= 1
            if dep == 0:
                end = i
                break
    tail = data[max(as_, end - 3000):end + 1]
    ms = re.findall(r'\{"x":(\d+),"y":([\d.]+),"equityReturn"', tail)
    if not ms:
        return None
    nav = []
    for t, v in ms[-6:]:
        dt = datetime.datetime.fromtimestamp(int(t) // 1000)
        nav.append({"d": dt.strftime("%m-%d"), "v": float(v), "ts": int(t)})
    return nav


def _parse_real_time(code: str) -> float | None:
    """获取实时估算涨跌幅"""
    try:
        gz = fetch(f"https://fundgz.1234567.com.cn/js/{code}.js")
        m = re.search(r'"gszzl":"([-\d.]+)"', gz)
        return float(m.group(1)) if m else None
    except Exception as e:
        log.debug("拉取实时估算失败 %s: %s", code, e)
        return None


def _parse_holdings(code: str) -> list[dict] | None:
    """获取前5大持仓明细（使用 csv.reader 处理名称含逗号的情况）"""
    try:
        jj = fetch(
            f"https://fund.eastmoney.com/f10/FundArchivesDatas.aspx"
            f"?type=jjcc&code={code}&topline=5&year=&month=&rt=0.1"
        )
        cm = re.search(r'content:"([^"]+)"', jj, re.DOTALL)
        if not cm:
            return None
        holds = []
        for line in cm.group(1).split("\\n"):
            # 使用 csv.reader 解析，正确处理带引号内逗号的情况
            # 格式: 序号,股票代码,股票名称,占净值比例%,持仓市值,占净值比例
            reader = csv.reader([line])
            for parts in reader:
                if len(parts) < 6:
                    continue
                try:
                    int(parts[0])
                    holds.append({"n": parts[2], "c": parts[1], "p": float(parts[5]) if parts[5] else 0})
                except (ValueError, IndexError):
                    pass
        return holds if holds else None
    except Exception as e:
        log.debug("拉取持仓失败 %s: %s", code, e)
        return None


def get(code: str) -> dict:
    """拉取一只基金的全量数据并组装返回"""
    d: dict = {"code": code}
    data = fetch(f"https://fund.eastmoney.com/pingzhongdata/{code}.js")

    if name := _parse_name(data):
        d["n"] = name
    if sc := _parse_scale(data):
        d["sc"] = sc
    d.update(_parse_period_returns(data))
    if sp := _parse_price_info(data):
        d["sp"] = sp
    if mgr := _parse_manager(data):
        d["mgr"] = mgr
    if inst := _parse_institutional_ratio(data):
        d["inst"] = inst
    if nav := _parse_net_trend(data):
        d["nav"] = nav
    if td := _parse_real_time(code):
        d["td"] = td
    if holds := _parse_holdings(code):
        d["holds"] = holds

    return d


# ── 历史快照 ──────────────────────────────────

def load_hist(code: str) -> dict:
    p = os.path.join(HISTORY_DIR, f".fw_{code}.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)  # type: ignore[no-any-return]
        except Exception as e:
            log.warning("读取历史文件失败 %s: %s", code, e)
    return {}


def save_hist(code: str, h: dict) -> None:
    with open(os.path.join(HISTORY_DIR, f".fw_{code}.json"), "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False)


# ── 主检查逻辑 ────────────────────────────────

STAGNATION_THRESHOLD = CFG["fund_watch"]["stagnation_threshold"]
STAGNATION_DAYS = CFG["fund_watch"]["stagnation_days"]
CONSECUTIVE_DROP_DAYS = CFG["fund_watch"]["consecutive_drop_days"]
CONSECUTIVE_DROP_TOTAL = CFG["fund_watch"]["consecutive_drop_total"]
DIVIDEND_DROP = CFG["fund_watch"]["dividend_drop"]


def check_stagnation(navs: list[dict]) -> str | None:
    """净值异常停滞：连续 N 个交易日涨跌幅极小"""
    if len(navs) < STAGNATION_DAYS:
        return None
    for i in range(-STAGNATION_DAYS, 0):
        prev = navs[i - 1]["v"]
        cur = navs[i]["v"]
        chg = abs((cur - prev) / prev * 100)
        if chg >= STAGNATION_THRESHOLD:
            return None
    return f"🟡 净值连续{STAGNATION_DAYS}日几乎不动（<{STAGNATION_THRESHOLD}%），可能流动性异常"


def check_consecutive_drop(navs: list[dict]) -> str | None:
    """连续多日下跌趋势"""
    if len(navs) < CONSECUTIVE_DROP_DAYS:
        return None
    # 从后往前数连续下跌天数
    drop_count = 0
    for i in range(-1, -len(navs), -1):
        prev = navs[i - 1]["v"]
        cur = navs[i]["v"]
        if cur < prev:
            drop_count += 1
        else:
            break
    if drop_count < CONSECUTIVE_DROP_DAYS:
        return None
    from_i = -drop_count - 1
    start_v = navs[from_i]["v"]
    total_chg = (navs[-1]["v"] - start_v) / start_v * 100
    start_date = navs[-drop_count]["d"]
    end_date = navs[-1]["d"]
    if total_chg <= CONSECUTIVE_DROP_TOTAL:
        return f"🚩 <font color=\"warning\">连跌{drop_count}天 ({start_date}→{end_date}) 累计{total_chg:.1f}%</font>"
    return f"🟡 连跌{drop_count}天 ({start_date}→{end_date}) 累计{total_chg:.1f}%"


def check_dividend(navs: list[dict]) -> str | None:
    """分红/份额拆分检查：单日净值异常大跌"""
    if len(navs) < 2:
        return None
    prev = navs[-2]["v"]
    cur = navs[-1]["v"]
    chg = (cur - prev) / prev * 100
    if chg <= DIVIDEND_DROP:
        return f"🟡 <font color=\"comment\">{navs[-1]['d']} 净值跌 {chg:.1f}%，可能为分红除权/份额拆分</font>"
    return None

def check(code: str) -> tuple[dict, list[str]]:
    d = get(code)
    h = load_hist(code)
    alerts: list[str] = []
    name = d.get("n", code)

    if h.get("m") and d.get("mgr") and h["m"] != d["mgr"]:
        alerts.append(f"🚩 <font color=\"warning\">{name}({code}) 经理: {h['m']}→{d['mgr']}</font>")
    h["m"] = d.get("mgr", "")

    if h.get("s") is not None and d.get("sc") is not None:
        r = d["sc"] / h["s"]
        if r >= ALERT_SCALE_2X:
            alerts.append(f"🚩 <font color=\"warning\">{name}({code}) 规模翻倍 {h['s']:.1f}亿→{d['sc']:.1f}亿</font>")
        elif r >= ALERT_SCALE_1_5X:
            alerts.append(f"🟡 {name}({code}) 规模增长 {h['s']:.1f}亿→{d['sc']:.1f}亿")
    h["s"] = d.get("sc", 0)

    m1 = d.get("m1")
    if m1 is not None:
        if m1 < ALERT_DROP_1M_RED:
            alerts.append(f"🚩 <font color=\"warning\">{name}({code}) 近一月亏 {m1:.1f}%</font>")
        elif m1 < ALERT_DROP_1M:
            alerts.append(f"🟡 {name}({code}) 近一月亏 {m1:.1f}%")

    save_hist(code, h)

    td = d.get("td")
    navs = d.get("nav", [])
    if td is None and len(navs) >= 2:
        td = (navs[-1]["v"] - navs[-2]["v"]) / navs[-2]["v"] * 100
    day_s = f"{td:+.2f}%" if td is not None else ""

    f5 = ""
    if len(navs) >= 5:
        f5 = f"{(navs[-1]['v'] - navs[-5]['v']) / navs[-5]['v'] * 100:+.1f}%"

    # ── 净值异常停滞 ──
    w = check_stagnation(navs)
    if w:
        alerts.append(f"{w}({code})")

    # ── 连跌趋势 ──
    w = check_consecutive_drop(navs)
    if w:
        alerts.append(f"{w}({code})")

    # ── 分红/拆分 ──
    w = check_dividend(navs)
    if w:
        alerts.append(f"{w}")

    row = {
        "code": code,
        "name": name,
        "name_short": name[:12],
        "day": day_s,
        "f5": f5,
        "m1": f"{d['m1']:+.1f}%" if d.get("m1") is not None else "",
        "m3": f"{d['m3']:+.1f}%" if d.get("m3") is not None else "",
        "y1": f"{d['y1']:+.1f}%" if d.get("y1") is not None else "",
        "mgr": d.get("mgr", "")[:6],
        "holds": d.get("holds", []),
    }
    return row, alerts


# ── 主程序 ────────────────────────────────────

def main() -> None:
    today = datetime.date.today().isoformat()
    log.info("====== 基金晚报 %s 开始 ======", today)

    if not WECHAT_WEBHOOK:
        log.info("WECHAT_WEBHOOK 未设置，走邮件推送")

    rows, all_alerts = [], []
    for f in FUND_LIST:
        try:
            r, a = check(f["code"])
            rows.append(r)
            all_alerts.extend(a)
            log.info("  %s(%s) %s | 近1月%s | 近3月%s | 近1年%s", r["name"], r["code"], r["day"], r["m1"], r["m3"], r["y1"])
        except Exception as e:
            log.error("❌ %s: %s", f["code"], e)

    # 纯文本（终端用）
    lines = [
        f"📊 基金晚报 {today}",
        "",
        f"{'代码':<6} {'基金名':<14} {'涨跌':<8} {'近5日':<8} {'近1月':<8} {'近3月':<8} {'近1年':<8} {'经理':<6}",
        "-" * 68,
    ]
    for r in rows:
        lines.append(f"{r['code']:<6} {r['name_short']:<14} {r['day']:<8} {r['f5']:<8} {r['m1']:<8} {r['m3']:<8} {r['y1']:<8} {r['mgr']:<6}")
    if all_alerts:
        lines.append("")
        lines.append("🚨 警报:")
        for a in all_alerts:  # type: ignore[assignment]
            lines.append(f"  {a}")
    full_text = "\n".join(lines)

    print(full_text)
    push("📊 基金晚报", rows, all_alerts, today)
    log.info("====== 基金晚报 %s 完成 ======", today)


if __name__ == "__main__":
    main()
