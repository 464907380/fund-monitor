"""
基金风险监控 v5.2 — 每日晚报 + 企业微信推送
"""
import json
import os
import re
import datetime
import math
import csv
import html as _html
from typing import Callable
from email.header import Header
from email.mime.text import MIMEText
from config import CFG
from config import get_secret as _get_secret
from fund_utils import fetch, log, HISTORY_DIR, is_trading_day, write_heartbeat, clear_heartbeat, _fetch_fund_estimate, \
    _color_inline, _strip_html, _send_smtp, send_wechat
from fund_scoring import SCORE_DIMS, _calc_score, _rank_percentile_str
from fund_metrics import _calc_nav_metrics

# ── 基金列表 ──────────────────────────────────
_FUND_LIST_FALLBACK = [
    {"code": "001438"}, {"code": "180031"}, {"code": "018998"},
    {"code": "000979"}, {"code": "320007"}, {"code": "161725"},
    {"code": "001480"}, {"code": "001753"}, {"code": "001170"},
]
FUND_LIST: list[dict] = []  # 占位，稍后由 _load_fund_list() 或 _ensure_fund_list_loaded() 填充
_fund_list_loaded = False


def _ensure_fund_list_loaded() -> None:
    """惰性加载基金列表（替代模块级副作用）"""
    global _fund_list_loaded
    if _fund_list_loaded:
        return  # 已加载
    _fund_list_path = os.path.join(HISTORY_DIR, "fund_list.json")
    if os.path.exists(_fund_list_path):
        try:
            with open(_fund_list_path, encoding="utf-8") as _f:
                loaded = json.load(_f)
            FUND_LIST[:] = loaded
            log.info("已从 fund_list.json 加载 %d 只基金", len(FUND_LIST))
        except Exception as _e:
            log.warning("读取 fund_list.json 失败 (%s)，使用内置默认列表", _e)
            FUND_LIST[:] = _FUND_LIST_FALLBACK
    else:
        FUND_LIST[:] = _FUND_LIST_FALLBACK
    _fund_list_loaded = True

def _get_webhook() -> str | None:
    """惰性读取企业微信 Webhook（支持长进程环境变量刷新）"""
    return _get_secret("WECHAT_WEBHOOK")


def _get_email_user() -> str | None:
    """惰性读取 QQ 邮箱（支持长进程环境变量刷新）"""
    return _get_secret("QQ_EMAIL")


def _get_email_auth() -> str | None:
    """惰性读取 QQ 邮箱授权码"""
    return _get_secret("QQ_MAIL_AUTH")

ALERT_DROP_1M = CFG.get("fund_watch", {}).get("alert_drop_1m", -10)
ALERT_DROP_1M_RED = CFG.get("fund_watch", {}).get("alert_drop_1m_red", -15)
ALERT_SCALE_2X = CFG.get("fund_watch", {}).get("alert_scale_2x", 2.0)
ALERT_SCALE_1_5X = CFG.get("fund_watch", {}).get("alert_scale_1_5x", 1.5)


# ── 推荐结果文件（需要 HISTORY_DIR 定义后）──
_RECOMMEND_RESULT_FILE = os.path.join(HISTORY_DIR, ".fund_recommend_result.json")


# ── 推送 ──────────────────────────────────────

def _pipe_table_to_html(ranking_lines: list[str]) -> str:
    """将 Markdown 管道表行列表转为 HTML <table> 字符串"""
    cp = '<tr><td style="padding:12px 14px;background:#0f3460;border:1px solid #333;border-radius:6px;">'
    cp += '<p style="margin:0 0 8px;font-size:14px;font-weight:600;color:#e0e0e0;">🏆 市场优选基金 TOP 10 （12 维评分）</p>'
    in_table = False
    header_done = False
    for line in ranking_lines:
        clean = line.strip()
        if clean.startswith("🏆"):
            continue
        if not clean:
            if in_table:
                cp += '</tbody></table>'
                in_table = False
            cp += '<br>'
            continue
        if clean.startswith("|:---"):
            continue
        if clean.startswith("|"):
            if not in_table:
                in_table = True
                header_done = False
                cp += '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:4px;">'
            if not header_done:
                cp += '<thead><tr>'
                for c in clean.strip("|").split("|"):
                    cp += f'<th style="padding:4px 6px;text-align:center;border-bottom:1px solid #444;color:#888;white-space:nowrap;">{_html.escape(c.strip())}</th>'
                cp += '</tr></thead><tbody>'
                header_done = True
            else:
                cp += '<tr>'
                for c in clean.strip("|").split("|"):
                    cp += f'<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;color:#bbb;white-space:nowrap;">{_html.escape(c.strip())}</td>'
                cp += '</tr>'
            continue
        if not in_table:
            cp += f'<p style="margin:2px 0;font-size:12px;color:#888;">{_html.escape(clean)}</p>'
    if in_table:
        cp += '</tbody></table>'
    cp += '</td></tr>'
    return cp


def send_mail_html(subject: str, rows: list[dict], alerts: list[str], today: str,
                   ranking_lines: list[str] | None = None) -> None:
    """通过 QQ 邮箱发送邮件（MJML 编译渲染）"""
    qq_email = _get_email_user()
    qq_auth = _get_email_auth()
    if not qq_email or not qq_auth:
        log.debug("QQ_EMAIL 或 QQ_MAIL_AUTH 未配置，邮件推送跳过")
        return
    tpl_path = os.path.join(HISTORY_DIR, "email_template.html")
    if not os.path.exists(tpl_path):
        log.warning("email_template.html 不存在，跳过邮件")
        return
    with open(tpl_path, encoding="utf-8") as f:
        tpl_html = f.read()
    tpl_html = tpl_html.replace("{{DATE}}", today)

    # 表格行（white-space:nowrap 自动撑宽）
    row_htmls = []
    for r in rows:
        _code = _html.escape(str(r.get("code", "")))
        _name = _html.escape(str(r.get("name_short", "")))
        _day = _html.escape(str(r.get("day", "")))
        _m1 = _html.escape(str(r.get("m1", "")))
        _m3 = _html.escape(str(r.get("m3", "")))
        _y1 = _html.escape(str(r.get("y1", "")))
        row_htmls.append("<tr>"
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;font-family:Consolas;font-size:11px;color:#888;white-space:nowrap;">{_code}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;font-size:13px;color:#ccc;white-space:nowrap;">{_name}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["day"])}">{_day}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["m1"])}">{_m1}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["m3"])}">{_m3}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["y1"])}">{_y1}</td>'
            + "</tr>"
        )
    html = tpl_html.replace("{{ROWS}}", "\n".join(row_htmls))

    # 警报区块
    extra_parts = []

    # 推荐排行（将管道表转为 HTML）
    if ranking_lines is None:
        ranking_lines = _format_recommend_rankings()
    if ranking_lines:
        cp = _pipe_table_to_html(ranking_lines)
        extra_parts.append(cp)

    # 警报
    if alerts:
        al = '<tr><td style="padding:12px 14px;"><p style="margin:0 0 8px;font-size:14px;font-weight:600;color:#ef5350;">🚨 警报</p>'
        for a in alerts:
            al += f'<p style="margin:3px 0;padding:4px 0;font-size:12px;color:#aaa;">{_strip_html(a)}</p>'
        al += '</td></tr>'
        extra_parts.append(al)

    html = html.replace("{{ALERTS}}", "\n".join(extra_parts) if extra_parts else "")

    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")  # type: ignore[assignment]
    msg["From"] = msg["To"] = qq_email
    _send_smtp(msg)


def push(subject: str, rows: list[dict], alerts: list[str], today: str,
         ranking_lines: list[str] | None = None) -> None:
    # 预计算推荐排行，两个推送通道共用
    if ranking_lines is None:
        ranking_lines = _format_recommend_rankings()
    sent = send_wechat(md_content(rows, alerts, today, ranking_lines))
    if not sent:
        send_mail_html(subject, rows, alerts, today, ranking_lines)


def md_content(rows: list[dict], alerts: list[str], today: str,
               ranking_lines: list[str] | None = None) -> str:
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

    # 推荐排行
    if ranking_lines is None:
        ranking_lines = _format_recommend_rankings()
    if ranking_lines:
        md_lines.append("")
        md_lines.extend(ranking_lines)

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
    """提取阶段收益：近1月/近3月/近1年

    注意：天天基金 JS 变量命名容易误解：
    syl_1y = 近1月 (1y=1月), syl_3y = 近3月, syl_1n = 近1年
    """
    result = {}
    for key, js_var in [("m1", "syl_1y"), ("m3", "syl_3y"), ("y1", "syl_1n")]:
        m = re.search(rf'var {js_var}\s*=\s*["\']([-\d.]+)["\']', data)
        if m:
            result[key] = float(m.group(1))
    return result




def _calc_period_return(full_nav: list[dict], lookback_days: int) -> float | None:
    """从净值数据计算指定区间收益(%)，lookback_days≈交易日数"""
    if not full_nav or len(full_nav) < 2:
        return None
    prices: list[float] = [float(n["v"]) for n in full_nav]
    if len(prices) < lookback_days:
        return None  # 数据不够
    start = prices[-lookback_days]
    end = prices[-1]
    return (end - start) / start * 100


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


def _parse_syl_6y(data: str) -> float | None:
    """提取近6月收益率"""
    m = re.search(r'syl_6y="([-\d.]+)"', data)
    return float(m.group(1)) if m else None

def _parse_net_trend(data: str, full_nav: list[dict] | None = None) -> list[dict] | None:
    """提取净值趋势（最近6条，供日报表使用）

    可传入已解析的 full_nav 复用，避免重复解析大 JSON。
    """
    nav = full_nav if full_nav is not None else _parse_full_nav(data)
    if not nav:
        return None
    return nav[-6:]


def _parse_full_nav(data: str) -> list[dict] | None:
    """提取完整净值趋势（全部历史数据，供评分计算使用）"""
    ts = data.find("var Data_netWorthTrend")
    if ts < 0:
        return None
    as_ = data.find("[{", ts)
    if as_ < 0:
        return None
    dep, end = 0, -1
    for i in range(as_, len(data)):
        if data[i] == "[":
            dep += 1
        elif data[i] == "]":
            dep -= 1
            if dep == 0:
                end = i
                break
    if end < 0:
        return None
    try:
        full = json.loads(data[as_:end + 1])
        return [{"d": datetime.datetime.fromtimestamp(int(n["x"]) // 1000).strftime("%Y-%m-%d"),
                 "v": float(n["y"]), "ts": int(n["x"])} for n in full]
    except (ValueError, KeyError, TypeError, IndexError):
        return None


def _parse_real_time(code: str) -> float | None:
    """获取实时估算涨跌幅"""
    result = _fetch_fund_estimate(code)
    if result:
        _, gszzl = result
        return gszzl
    return None


def _parse_holdings(code: str) -> list[dict] | None:
    """获取前5大持仓明细（使用 csv.reader 处理名称含逗号的情况）"""
    urls = [
        api_url("fund_holdings", code=code),
        api_url("fund_holdings", code=code),
    ]
    last_err = None
    for url in urls:
        try:
            jj = fetch(url)
            cm = re.search(r'content:"([^"]+)"', jj, re.DOTALL)
            if not cm:
                continue
            holds = []
            for line in cm.group(1).split("\\n"):
                reader = csv.reader([line])
                for parts in reader:
                    if len(parts) < 6:
                        continue
                    try:
                        int(parts[0])
                        holds.append({"n": parts[2], "c": parts[1], "p": float(parts[5]) if parts[5] else 0})
                    except (ValueError, IndexError):
                        pass
            if holds:
                return holds
        except Exception as e:
            last_err = e
            continue
    if last_err:
        log.debug("拉取重仓股失败 %s: %s", code, last_err)
    return None


# ── 评分相关解析 ──────────────────────────────

def _parse_rank_info(data: str) -> tuple[int, int] | None:
    """提取同类排名 (当前排名, 同类总数)"""
    m = re.search(r'var Data_rateInSimilarType = (\[.*?\]);', data, re.DOTALL)
    if not m:
        return None
    try:
        ranks = json.loads(m.group(1))
        if not ranks:
            return None
        last = ranks[-1]
        return int(last["y"]), int(last.get("sc", 1))
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _parse_fund_rate(data: str) -> float | None:
    """提取基金现费率（%）"""
    m = re.search(r'fund_Rate="([^"]+)"', data)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None




def get(code: str) -> dict:
    """拉取一只基金的全量数据并组装返回"""
    d: dict = {"code": code}
    data = fetch(api_url("fund_pingzhongdata", code=code))

    if name := _parse_name(data):
        d["n"] = name
    if sc := _parse_scale(data):
        d["sc"] = sc
    d.update(_parse_period_returns(data))
    if mgr := _parse_manager(data):
        d["mgr"] = mgr
    if inst := _parse_institutional_ratio(data):
        d["inst"] = inst
    # 完整净值（用于计算回撤/波动率/卡玛比率）
    if full_nav := _parse_full_nav(data):
        d["full_nav"] = full_nav
        d["nav"] = _parse_net_trend(data, full_nav)
        metrics = _calc_nav_metrics(full_nav)
        d.update(metrics)
        # 从净值数据计算近3年收益
        d["sy3"] = _calc_period_return(full_nav, 750)  # ≈3年（约250个交易日/年 × 3）
    else:
        if nav := _parse_net_trend(data):
            d["nav"] = nav
    if td := _parse_real_time(code):
        d["td"] = td
    if holds := _parse_holdings(code):
        d["holds"] = holds
    if rp := _parse_rank_info(data):
        d["rank"], d["rank_total"] = rp
    if rate := _parse_fund_rate(data):
        d["rate"] = rate
    d["sy6"] = _parse_syl_6y(data)  # 近6月收益（暂未用于评分，保留供未来使用）

    return d


# ── 历史快照 ──────────────────────────────────

def _validate_fund_code(code: str) -> None:
    """校验基金代码：仅允许 6 位数字，防止路径遍历"""
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"非法基金代码: {code}")


def load_hist(code: str) -> dict:
    _validate_fund_code(code)
    p = os.path.join(HISTORY_DIR, f".fw_{code}.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError) as _e:
            log.warning("读取历史文件失败 %s: %s", code, _e)
    return {}


def save_hist(code: str, h: dict) -> None:
    _validate_fund_code(code)
    path = os.path.join(HISTORY_DIR, f".fw_{code}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False)
    except OSError as e:
        log.warning("保存历史数据失败 %s: %s", code, e)


# ── 主检查逻辑 ────────────────────────────────

STAGNATION_THRESHOLD = CFG.get("fund_watch", {}).get("stagnation_threshold", 0.05)
STAGNATION_DAYS = CFG.get("fund_watch", {}).get("stagnation_days", 3)
CONSECUTIVE_DROP_DAYS = CFG.get("fund_watch", {}).get("consecutive_drop_days", 3)
CONSECUTIVE_DROP_TOTAL = CFG.get("fund_watch", {}).get("consecutive_drop_total", -3)
DIVIDEND_DROP = CFG.get("fund_watch", {}).get("dividend_drop", -4)


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

    if h.get("s") is not None and h["s"] > 0 and d.get("sc") is not None:
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
        last_date = navs[-1].get("d", "")
        # 仅在最新净值日期为今明两天时才显示（避免非交易日冒充当日涨跌）
        _td = datetime.date.today()
        if last_date in (_td.isoformat(), (_td - datetime.timedelta(days=1)).isoformat()):
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
        "m1": f"{_v:+.1f}%" if (_v := d.get("m1")) is not None else "",
        "m3": f"{_v:+.1f}%" if (_v := d.get("m3")) is not None else "",
        "y1": f"{_v:+.1f}%" if (_v := d.get("y1")) is not None else "",
        "mgr": d.get("mgr", "")[:6],
        "holds": d.get("holds", []),
        "_y1_raw": d.get("y1"),
        "_rank": d.get("rank"),
        "_rank_total": d.get("rank_total"),
        "_sharpe": d.get("sharpe"),
        "_sortino": d.get("sortino"),
        "_max_dd": d.get("max_dd"),
        "_win_rate": d.get("win_rate"),
        "_inst": d.get("inst"),
        "_sc": d.get("sc"),
        "_rate": d.get("rate"),
        "_annual_return": d.get("annual_return"),
        "_profit_ratio": d.get("profit_ratio"),
        "_recovery": d.get("recovery"),
        "_sy3": d.get("sy3"),
        "_sy6": d.get("sy6"),
    }
    return row, alerts


# ── 推荐排行 ────────────────────────────────────

def _load_recommend_data() -> dict | None:
    """加载推荐结果完整数据（含日期和结果列表），合并文件读取"""
    if not os.path.exists(_RECOMMEND_RESULT_FILE):
        return None
    try:
        with open(_RECOMMEND_RESULT_FILE, encoding="utf-8") as f:
            return json.load(f)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None




def _format_recommend_rankings() -> list[str]:
    """展示市场优选基金排行（来自上次推荐结果）"""
    data = _load_recommend_data()
    recs = data.get("results", []) if data else None
    lines: list[str] = []
    if not recs:
        lines.append("")
        lines.append("💡 **想看看市场上有哪些优秀基金？**")
        lines.append("   运行 python fund_recommend.py（约4分钟）")
        lines.append("   之后晚报自动展示推荐排行")
        return lines

    # 检查推荐结果是否过旧
    if data:
        try:
            rec_date = data.get("date", "")
            if rec_date:
                rec_dt = datetime.date.fromisoformat(rec_date)
                days_old = (datetime.date.today() - rec_dt).days
                if days_old > 14:
                    lines.append("")
                    lines.append(f"⚠️ 推荐结果已是 {days_old} 天前的，建议重新运行")
                    lines.append(f"   python fund_recommend.py")
        except (ValueError, KeyError, TypeError):
            pass

    lines.append("")
    lines.append("🏆 **市场优选基金 TOP 10**  （12 维评分）")
    lines.append("")
    lines.append(f"|{'排名':<4}|{'基金名':<14}|{'年化%':<6}|{'近1月':<7}|{'近3月':<7}|{'近1年':<7}|{'夏普':<5}|{'回撤':<5}|{'近3年':<6}|")
    lines.append(f"|:---:|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(recs[:10], 1):
        badge = medals[i - 1] if i <= 3 else f" {i}."
        name = r.get("name", "")[:14]
        ar = r.get("annual_return", 0)
        m1 = str(r.get("m1", ""))
        m3 = str(r.get("m3", ""))
        y1 = str(r.get("y1", ""))
        sharpe = r.get("sharpe", 0)
        dd = r.get("max_dd", 0)
        sy3 = 0 if r.get("sy3") is None else r["sy3"]
        lines.append(f"|{badge:<4}|{name:<14}|{ar:<6.1f}%|{m1:<7s}|{m3:<7s}|{y1:<7s}|{sharpe:<5.2f}|{dd:<5.1f}%|{sy3:<5.1f}%|")

    lines.append("")
    lines.append("  ── 排名依据：从全市场 200 只基金中精选 TOP 10 ──")
    lines.append("  📡 数据源：天天基金排行 API（https://fund.eastmoney.com）")
    lines.append("     拉取全市场近 1 年收益排行前 200 名（不限类型），")
    lines.append("     再剔除近 1 年收益为负的基金，其余全部进入深度评分。")
    lines.append("     每只基金独立拉取净值数据，从净值数组真实计算各项指标。")
    num = len(SCORE_DIMS)
    lines.append(f"  🧮 评分方式：{num} 个维度加权打分（0-100 分），权重合计 100%")
    lines.append("")
    medals_cn = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟","1️⃣1️⃣","1️⃣2️⃣","1️⃣3️⃣","1️⃣4️⃣","1️⃣5️⃣"]
    for i, (name, fn, weight, desc) in enumerate(SCORE_DIMS):
        badge = medals_cn[i] if i < len(medals_cn) else f"  {i+1}."
        lines.append(f"  {badge} {name}（权重 {int(weight*100)}%）")
        lines.append(f"      → {desc}")

    return lines


# ── 主程序 ────────────────────────────────────

def main() -> None:
    _ensure_fund_list_loaded()
    today = datetime.date.today()
    if not is_trading_day(today):
        log.info("今天非交易日，跳过晚报")
        return
    write_heartbeat("fund_watch")
    try:
        today_str = today.isoformat()
        log.info("====== 基金晚报 %s 开始 ======", today_str)

        # 第一遍：拉取所有基金原始数据
        raw_rows: list[dict] = []
        all_alerts: list[str] = []
        for f in FUND_LIST:
            try:
                r, a = check(f["code"])
                raw_rows.append(r)
                all_alerts.extend(a)
                log.info("  %s(%s) %s | 近1月%s | 近3月%s | 近1年%s", r["name"], r["code"], r["day"], r["m1"], r["m3"], r["y1"])
            except Exception as e:
                log.error("❌ %s: %s", f["code"], e)
    
        # 计算评分（供展示使用）
        for r in raw_rows:
            d = {
                "annual_return": r.get("_annual_return"),
                "sharpe": r.get("_sharpe"),
                "sortino": r.get("_sortino"),
                "max_dd": r.get("_max_dd"),
                "win_rate": r.get("_win_rate"),
                "inst": r.get("_inst"),
                "sc": r.get("_sc"),
                "rate": r.get("_rate"),
                "profit_ratio": r.get("_profit_ratio"),
                "recovery": r.get("_recovery"),
                "y1": r.get("_y1_raw"),
                "sy3": r.get("_sy3"),  # 无近3年数据不评分（不回退到近6月）
            }
            r["score"] = _calc_score(d)
    
        rows = raw_rows
    
        # 推送（两条通道共用推荐排行数据）
        ranking_lines = _format_recommend_rankings() if rows else None
        push("📊 基金晚报", rows, all_alerts, today_str, ranking_lines)
        log.info("====== 基金晚报 %s 完成 ======", today_str)
    finally:
        clear_heartbeat("fund_watch")


if __name__ == "__main__":
    main()
