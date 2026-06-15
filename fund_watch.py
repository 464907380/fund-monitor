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
from config import CFG, api_url
from config import get_secret as _get_secret
from fund_utils import fetch, log, HISTORY_DIR, write_heartbeat, update_heartbeat, clear_heartbeat, _fetch_fund_estimate, \
    _color_inline, _strip_html, _send_smtp, send_wechat
from fund_scoring import SCORE_DIMS, calc_score_detail, _rank_percentile_str
from fund_metrics import _calc_nav_metrics
from fund_alerts import check_stagnation, check_consecutive_drop, check_dividend, \
    STAGNATION_THRESHOLD, STAGNATION_DAYS, CONSECUTIVE_DROP_DAYS, CONSECUTIVE_DROP_TOTAL, DIVIDEND_DROP
from fund_render import _get_webhook, _get_email_user, _get_email_auth, _pipe_table_to_html, send_mail_html, push, md_content, _load_recommend_data, _format_recommend_rankings

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




def _calc_period_return(full_nav: list[dict] | None, lookback_days: int) -> float | None:
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
        d["sy2"] = _calc_period_return(full_nav, 500)  # ≈2年
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


def get_scoring_data(code: str) -> dict:
    """拉取评分所需的最小数据集（跳过实时估值和持仓，减少网络请求）"""
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
    if full_nav := _parse_full_nav(data):
        d["full_nav"] = full_nav
        d["nav"] = _parse_net_trend(data, full_nav)
        metrics = _calc_nav_metrics(full_nav)
        d.update(metrics)
        d["sy3"] = _calc_period_return(full_nav, 750)
        d["sy2"] = _calc_period_return(full_nav, 500)
    else:
        if nav := _parse_net_trend(data):
            d["nav"] = nav
    if rp := _parse_rank_info(data):
        d["rank"], d["rank_total"] = rp
    if rate := _parse_fund_rate(data):
        d["rate"] = rate
    d["sy6"] = _parse_syl_6y(data)
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
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False)
        os.replace(tmp_path, path)
    except OSError as e:
        log.warning("保存历史数据失败 %s: %s", code, e)


# ── 主检查逻辑 ────────────────────────────────

ALERT_DROP_1M = CFG.get("fund_watch", {}).get("alert_drop_1m", -10)
ALERT_DROP_1M_RED = CFG.get("fund_watch", {}).get("alert_drop_1m_red", -15)
ALERT_SCALE_2X = CFG.get("fund_watch", {}).get("alert_scale_2x", 2.0)
ALERT_SCALE_1_5X = CFG.get("fund_watch", {}).get("alert_scale_1_5x", 1.5)

STAGNATION_THRESHOLD = CFG.get("fund_watch", {}).get("stagnation_threshold", 0.05)
def _check_manager_and_scale(d: dict, h: dict, name: str, code: str) -> list[str]:
    """检查经理变更和规模变化，返回警报列表"""
    alerts: list[str] = []
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
    return alerts


def _check_monthly_drop(d: dict, name: str, code: str) -> list[str]:
    """检查近一月跌幅是否触发警报"""
    alerts: list[str] = []
    m1 = d.get("m1")
    if m1 is not None:
        if m1 < ALERT_DROP_1M_RED:
            alerts.append(f"🚩 <font color=\"warning\">{name}({code}) 近一月亏 {m1:.1f}%</font>")
        elif m1 < ALERT_DROP_1M:
            alerts.append(f"🟡 {name}({code}) 近一月亏 {m1:.1f}%")
    return alerts


def _calc_day_change(navs: list[dict], td: float | None) -> tuple[str, str]:
    """计算当日涨跌幅和近5日涨跌幅"""
    day_s = ""
    f5 = ""
    if td is None and len(navs) >= 2:
        last_date = navs[-1].get("d", "")
        if last_date == datetime.date.today().isoformat():
            td = (navs[-1]["v"] - navs[-2]["v"]) / navs[-2]["v"] * 100
    day_s = f"{td:+.2f}%" if td is not None else ""
    if len(navs) >= 5:
        f5 = f"{(navs[-1]['v'] - navs[-5]['v']) / navs[-5]['v'] * 100:+.1f}%"
    return day_s, f5



# ── 公告类型名称映射 ────────────────────────────
_ANNOUNCE_TYPE_NAMES = {
    "1": "发行运作", "2": "分红送配", "3": "定期报告",
    "4": "人事调整", "5": "基金销售", "6": "其他公告",
}

_ANNOUNCE_ICONS = {
    "1": "📋", "2": "💰", "3": "📊",
    "4": "👤", "5": "🏷️", "6": "📄",
}

# 公告标题中不值得单独推送的关键词（跳过）
_ANNOUNCE_SKIP_KEYWORDS = [
    "系统维护", "隐私政策", "网上交易", "节日", "假期",
]


def _fetch_announcements(code: str) -> list[dict]:
    """获取基金最新公告列表，返回 [{id, title, type, date}]，失败返回 []"""
    import urllib.request, re, json
    url = api_url("fund_announce", code=code)
    req = urllib.request.Request(url, headers={
        "Referer": f"https://fundf10.eastmoney.com/jjgg_{code}.html",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read().decode("utf-8")
        m = re.search(r'j\((.+)\)$', data)
        if not m:
            return []
        obj = json.loads(m.group(1))
        if obj.get("ErrCode") != 0:
            return []
        raw = obj.get("Data") or []
        result = []
        for item in raw:
            tid = item.get("ID", "")
            title = item.get("TITLE", "")
            cat = item.get("NEWCATEGORY", "0")
            date = item.get("PUBLISHDATEDesc", "")
            if tid and title:
                result.append({"id": tid, "title": title, "type": cat, "date": date})
        return result
    except Exception as e:
        log.debug("获取公告失败 %s: %s", code, e)
        return []


def _check_announcements(d: dict, h: dict, name: str, code: str) -> list[str]:
    """检查是否有新公告，返回警报列表"""
    alerts: list[str] = []
    items = _fetch_announcements(code)
    if not items:
        return alerts

    last_id = h.get("last_ann_id", "")
    latest_id = items[0]["id"]

    if not last_id:
        # 首次运行：只记录最新 ID，不报警
        h["last_ann_id"] = latest_id
        return alerts

    if latest_id == last_id:
        # 无新公告
        return alerts

    # 有新公告：找出比 last_id 更新的
    found = False
    for item in items:
        if item["id"] == last_id:
            break
        # 跳过不重要的公告
        title = item["title"]
        if any(kw in title for kw in _ANNOUNCE_SKIP_KEYWORDS):
            continue
        cat = item.get("type", "0")
        cat_name = _ANNOUNCE_TYPE_NAMES.get(cat, "")
        icon = _ANNOUNCE_ICONS.get(cat, "📋")
        date = item.get("date", "")
        if not found:
            alerts.append(f"📋 <font color=\"info\">{name}({code}) 新公告</font>")
            found = True
        alerts.append(f"   {icon} [{date}] {cat_name} {title}")

    h["last_ann_id"] = latest_id
    return alerts

def check(code: str) -> tuple[dict, list[str]]:
    d = get(code)
    h = load_hist(code)
    alerts: list[str] = []
    name = d.get("n", code)
    data_ok = "n" in d  # 是否成功获取到网络数据

    alerts += _check_manager_and_scale(d, h, name, code)
    alerts += _check_monthly_drop(d, name, code)
    alerts += _check_announcements(d, h, name, code)
    save_hist(code, h)

    td = d.get("td")
    navs = d.get("nav", [])
    day_s, f5 = _calc_day_change(navs, td)

    # ── 净值异常停滞 ──
    w = check_stagnation(navs)
    if w:
        alerts.append(f"{w} {name}({code})")

    # ── 连跌趋势 ──
    w = check_consecutive_drop(navs)
    if w:
        alerts.append(f"{w} {name}({code})")

    # ── 分红/拆分 ──
    w = check_dividend(navs)
    if w:
        alerts.append(f"{w}")

    row = {
        "code": code,
        "name": name,
        "name_short": name[:12],
        "_nodata": not data_ok,
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
        "_sy2": d.get("sy2"),
        "_volatility": d.get("volatility"),
        "_calmar": d.get("calmar"),
        "_max_loss_days": d.get("max_loss_days"),
        "_sy6": d.get("sy6"),
    }
    return row, alerts


# ── 推荐排行 ────────────────────────────────────

def main() -> None:
    _ensure_fund_list_loaded()
    today = datetime.date.today()
    today_str = today.isoformat()
    log.info("====== 基金晚报 %s 开始 ======", today_str)
    write_heartbeat("fund_watch")
    update_heartbeat("fund_briefing", progress=0, total=0, status="启动中...")
    try:

        # 第一遍：拉取所有基金原始数据
        raw_rows: list[dict] = []
        all_alerts: list[str] = []
        total_steps = len(FUND_LIST) * 2 + 2  # 取数据N + 评分N + 市场优选 + 推送
        for f in FUND_LIST:
            name = "?"
            try:
                r, a = check(f["code"])
                raw_rows.append(r)
                all_alerts.extend(a)
                name = r.get("name", "?")
                log.info("  %s(%s) %s | 近1月%s | 近3月%s | 近1年%s", name, r["code"], r["day"], r["m1"], r["m3"], r["y1"])
            except Exception as e:
                log.error("❌ %s: %s", f["code"], e)
            update_heartbeat("fund_briefing", progress=len(raw_rows), total=total_steps,
                             status=f"取数据 {name}({f['code']})")
    
        # 计算评分（供展示使用）
        for i, r in enumerate(raw_rows, 1):
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
                "m1": r.get("m1"),      # 近1月收益（字符串 "+3.45%"）
                "m3": r.get("m3"),      # 近3月收益（字符串）
                "sy6": r.get("_sy6"),   # 近6月收益
                "f5": r.get("f5"),      # 近一周收益（字符串 "+2.1%"）
                "sy2": r.get("_sy2"),   # 近2年收益
                "volatility": r.get("_volatility"),
                "calmar": r.get("_calmar"),
                "max_loss_days": r.get("_max_loss_days"),
            }
            r["score"], r["_score_detail"], r["_skipped_weight"] = calc_score_detail(d)
            update_heartbeat("fund_briefing", progress=len(FUND_LIST) + i, total=total_steps,
                             status=f"评分 {r.get('name_short','?')}({r['code']})")
        update_heartbeat("fund_briefing", progress=len(FUND_LIST) + len(raw_rows) + 1, total=total_steps, status="获取市场优选")
        rows = raw_rows

        # 推送（两条通道共用推荐排行数据）
        ranking_lines = _format_recommend_rankings() if rows else None
        update_heartbeat("fund_briefing", progress=total_steps, total=total_steps, status="推送中")
        push("📊 基金晚报", rows, all_alerts, today_str, ranking_lines)
        log.info("====== 基金晚报 %s 完成 ======", today_str)
    finally:
        clear_heartbeat("fund_watch")


if __name__ == "__main__":
    main()
