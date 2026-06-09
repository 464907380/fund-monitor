"""
基金风险监控 v5.1 — 每日晚报 + 企业微信推送
"""
import json
import logging
import os
import re
import datetime
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
import smtplib
from logging.handlers import RotatingFileHandler

# ── 配置 ──────────────────────────────────────
FUND_LIST = [
    {"code": "001438"}, {"code": "180031"}, {"code": "018998"},
    {"code": "000979"}, {"code": "320007"}, {"code": "161725"},
    {"code": "001480"}, {"code": "001753"}, {"code": "001170"},
]

# 推送配置（优先级：企业微信 > 邮件）
WECHAT_WEBHOOK = os.getenv("WECHAT_WEBHOOK", "")
QQ_EMAIL = os.getenv("QQ_EMAIL", "464907380@qq.com")
QQ_AUTH_CODE = os.getenv("QQ_MAIL_AUTH", "")
if not QQ_AUTH_CODE:
    QQ_AUTH_CODE = "ivfqwtorvsnfcbch"

ALERT_DROP_1M = -10
ALERT_DROP_1M_RED = -15
ALERT_SCALE_2X = 2.0
ALERT_SCALE_1_5X = 1.5

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

_cache: dict[str, str] = {}


def fetch(url: str) -> str:
    if url in _cache:
        return _cache[url]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
        _cache[url] = resp
        return resp
    except Exception as e:
        log.warning("请求失败 %s: %s", url, e)
        raise


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
        return
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
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
        return
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
            f'<td style="padding:6px;border-bottom:1px solid #eee;font-family:Consolas,monospace;">{r["code"]}</td>'
            f'<td style="padding:6px;border-bottom:1px solid #eee;">{r["name_short"]}</td>'
            f'<td style="padding:6px;border-bottom:1px solid #eee;text-align:right;font-weight:bold;{_color(r["day"])}">{r["day"]}</td>'
            f'<td style="padding:6px;border-bottom:1px solid #eee;text-align:right;{_color(r["f5"])}">{r["f5"]}</td>'
            f'<td style="padding:6px;border-bottom:1px solid #eee;text-align:right;{_color(r["m1"])}">{r["m1"]}</td>'
            f'<td style="padding:6px;border-bottom:1px solid #eee;text-align:right;{_color(r["m3"])}">{r["m3"]}</td>'
            f'<td style="padding:6px;border-bottom:1px solid #eee;text-align:right;{_color(r["y1"])}">{r["y1"]}</td>'
            f'<td style="padding:6px;border-bottom:1px solid #eee;">{r["mgr"]}</td>'
            f'</tr>'
        )
    html = html.replace("{{ROWS}}", "\n".join(row_htmls))
    # 警报
    if alerts:
        al = '<div style="background:#fff5f5;border:1px solid #fcc;border-radius:6px;padding:12px;">'
        al += '<div style="font-weight:bold;font-size:14px;margin-bottom:8px;">🚨 警报</div>'
        for a in alerts:
            al += f'<div style="font-size:13px;margin:4px 0;padding:4px 0;border-bottom:1px solid #fee;">{re.sub(r"<[^>]+>","",a)}</div>'
        al += '</div>'
        html = html.replace("{{ALERTS}}", al)
    else:
        html = html.replace("{{ALERTS}}", "")
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = msg["To"] = QQ_EMAIL
    try:
        s = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=10)
        s.login(QQ_EMAIL, QQ_AUTH_CODE)
        s.sendmail(QQ_EMAIL, [QQ_EMAIL], msg.as_string())
        s.quit()
        log.info("邮件发送成功")
    except Exception as e:
        log.error("邮件发送失败: %s", e)


def _color(val: str) -> str:
    """数值颜色：涨红跌绿"""
    if not val:
        return ""
    if val.startswith("+"):
        return "color:#d32f2f;"
    if val.startswith("-"):
        return "color:#2e7d32;"
    return ""


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

def get(code: str) -> dict:
    d: dict = {"code": code}
    data = fetch(f"https://fund.eastmoney.com/pingzhongdata/{code}.js")

    m = re.search(r'var fS_name\s*=\s*"([^"]+)"', data)
    if m:
        d["n"] = m.group(1)

    m = re.findall(r'"y":([\d.]+),"mom":"[\d.-]+%"', data)
    if m:
        d["sc"] = float(m[-1])

    for key, js_var in [("m1", "syl_1y"), ("m3", "syl_3y"), ("y1", "syl_1n")]:
        m = re.search(rf'var {js_var}\s*=\s*"([-\d.]+)"', data)
        if m:
            d[key] = float(m.group(1))

    m = re.search(r'"data":\[([\d.]+),([\d.]+),([\d.]+),([\d.]+),([\d.]+)\]', data)
    if m:
        d["sp"] = int(float(m.group(1)))

    m = re.search(r'Data_currentFundManager.*?"name":"([^"]+)"', data, re.DOTALL)
    if m:
        d["mgr"] = m.group(1)

    m = re.search(r'"机构持有比例","data":\[([^\]]+)\]', data)
    if m:
        vs = m.group(1).split(",")
        if vs:
            d["inst"] = float(vs[-1].strip())

    ts = data.find("var Data_netWorthTrend")
    if ts >= 0:
        as_ = data.find("[{", ts)
        if as_ >= 0:
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
            if ms:
                d["nav"] = []
                for t, v in ms[-6:]:
                    dt = datetime.datetime.fromtimestamp(int(t) // 1000)
                    d["nav"].append({"d": dt.strftime("%m-%d"), "v": float(v), "ts": int(t)})

    try:
        gz = fetch(f"https://fundgz.1234567.com.cn/js/{code}.js")
        m = re.search(r'"gszzl":"([-\d.]+)"', gz)
        if m:
            d["td"] = float(m.group(1))
    except Exception as e:
        log.debug("拉取实时估算失败 %s: %s", code, e)

    try:
        jj = fetch(
            f"https://fund.eastmoney.com/f10/FundArchivesDatas.aspx"
            f"?type=jjcc&code={code}&topline=5&year=&month=&rt=0.1"
        )
        cm = re.search(r'content:"([^"]+)"', jj, re.DOTALL)
        if cm:
            d["holds"] = []
            for line in cm.group(1).split("\\n"):
                parts = line.split(",")
                if len(parts) >= 6:
                    try:
                        int(parts[0])
                        d["holds"].append({"n": parts[2], "p": float(parts[5]) if parts[5] else 0})
                    except Exception:
                        pass
    except Exception as e:
        log.debug("拉取持仓失败 %s: %s", code, e)

    return d


# ── 历史快照 ──────────────────────────────────

def load_hist(code: str) -> dict:
    p = os.path.join(HISTORY_DIR, f".fw_{code}.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("读取历史文件失败 %s: %s", code, e)
    return {}


def save_hist(code: str, h: dict) -> None:
    with open(os.path.join(HISTORY_DIR, f".fw_{code}.json"), "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False)


# ── 主检查逻辑 ────────────────────────────────

STAGNATION_THRESHOLD = 0.05    # 日涨跌幅 < 0.05% 视为停滞
STAGNATION_DAYS = 3            # 连续 3 天
CONSECUTIVE_DROP_DAYS = 3      # 连跌 3 天起
CONSECUTIVE_DROP_TOTAL = -3    # 累计跌幅超 -3%
DIVIDEND_DROP = -4             # 单日净值跌超 -4% 提示分红/拆分


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
        for a in all_alerts:
            lines.append(f"  {a}")
    full_text = "\n".join(lines)

    print(full_text)
    push("📊 基金晚报", rows, all_alerts, today)
    log.info("====== 基金晚报 %s 完成 ======", today)


if __name__ == "__main__":
    main()
