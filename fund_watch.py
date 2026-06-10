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
from typing import Callable
from email.header import Header
from email.mime.text import MIMEText
from config import CFG
from config import get_secret as _get_secret
from fund_utils import fetch, clear_cache, fetch_bytes, log, HISTORY_DIR, \
    _color_cls, _color_inline, _strip_html, _send_smtp, send_wechat, send_mail

# ── 基金列表 ──────────────────────────────────
_FUND_LIST_FALLBACK = [
    {"code": "001438"}, {"code": "180031"}, {"code": "018998"},
    {"code": "000979"}, {"code": "320007"}, {"code": "161725"},
    {"code": "001480"}, {"code": "001753"}, {"code": "001170"},
]
FUND_LIST: list[dict] = []  # 占位，稍后由 _load_fund_list() 或 _ensure_fund_list_loaded() 填充


def _ensure_fund_list_loaded() -> None:
    """惰性加载基金列表（替代模块级副作用）"""
    if FUND_LIST:
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

WECHAT_WEBHOOK = _get_secret("WECHAT_WEBHOOK")
QQ_EMAIL = _get_secret("QQ_EMAIL")
QQ_AUTH_CODE = _get_secret("QQ_MAIL_AUTH")

ALERT_DROP_1M = CFG.get("fund_watch", {}).get("alert_drop_1m", -10)
ALERT_DROP_1M_RED = CFG.get("fund_watch", {}).get("alert_drop_1m_red", -15)
ALERT_SCALE_2X = CFG.get("fund_watch", {}).get("alert_scale_2x", 2.0)
ALERT_SCALE_1_5X = CFG.get("fund_watch", {}).get("alert_scale_1_5x", 1.5)


# ── 推荐结果文件（需要 HISTORY_DIR 定义后）──
_RECOMMEND_RESULT_FILE = os.path.join(HISTORY_DIR, ".fund_recommend_result.json")


# ── 推送 ──────────────────────────────────────

def send_mail_html(subject: str, rows: list[dict], alerts: list[str], today: str) -> None:
    """通过 QQ 邮箱发送邮件（MJML 编译渲染）"""
    if not QQ_EMAIL or not QQ_AUTH_CODE:
        log.warning("QQ_EMAIL 或 QQ_MAIL_AUTH 未配置，邮件推送跳过")
        return
    tpl_path = os.path.join(HISTORY_DIR, "email_template.html")
    if not os.path.exists(tpl_path):
        log.warning("email_template.html 不存在，跳过邮件")
        return
    with open(tpl_path, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("{{DATE}}", today)

    # 表格行（white-space:nowrap 自动撑宽）
    row_htmls = []
    for r in rows:
        row_htmls.append("<tr>"
            + f'<td style="padding:6px 4px;border-bottom:1px solid #f0f0f0;font-family:Consolas;font-size:11px;color:#888;white-space:nowrap;">{r["code"]}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #f0f0f0;font-size:13px;white-space:nowrap;">{r["name_short"]}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #f0f0f0;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["day"])}">{r["day"]}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #f0f0f0;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["m1"])}">{r["m1"]}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #f0f0f0;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["m3"])}">{r["m3"]}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #f0f0f0;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["y1"])}">{r["y1"]}</td>'
            + "</tr>"
        )
    html = html.replace("{{ROWS}}", "\n".join(row_htmls))

    # 警报区块
    extra_parts = []

    # 持仓对比
    compare_lines = _compare_with_recommendations(rows)
    if compare_lines:
        cp = '<tr><td style="padding:12px 14px;background:#f0f8ff;border:1px solid #bcd;border-radius:6px;">'
        cp += '<p style="margin:0 0 8px;font-size:14px;font-weight:600;color:#1a1a2e;">🏆 市场优选基金 TOP 10 （11 维评分）</p>'
        in_table = False
        header_done = False
        for line in compare_lines:
            clean = line.strip()
            # 跳过函数自带的标题（HTML 已有）
            if clean.startswith("🏆"):
                continue
            # 空行 — 结束表格（如果有的话）
            if not clean:
                if in_table:
                    cp += '</tbody></table>'
                    in_table = False
                cp += '<br>'
                continue
            # 管道表分隔行 (|:---|:---|...)
            if clean.startswith("|:---"):
                continue
            # 管道表行
            if clean.startswith("|"):
                if not in_table:
                    in_table = True
                    header_done = False
                    cp += '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:4px;">'
                if not header_done:
                    cp += '<thead><tr>'
                    for c in clean.strip("|").split("|"):
                        cp += f'<th style="padding:4px 6px;text-align:center;border-bottom:1px solid #ccc;color:#555;white-space:nowrap;">{c.strip()}</th>'
                    cp += '</tr></thead><tbody>'
                    header_done = True
                else:
                    cp += '<tr>'
                    for c in clean.strip("|").split("|"):
                        cp += f'<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #eee;color:#444;white-space:nowrap;">{c.strip()}</td>'
                    cp += '</tr>'
                continue
            # 非表行（评分说明、提示等）
            if not in_table:
                cp += f'<p style="margin:2px 0;font-size:12px;color:#666;">{clean}</p>'
        if in_table:
            cp += '</tbody></table>'
        cp += '</td></tr>'
        extra_parts.append(cp)

    # 警报
    if alerts:
        al = '<tr><td style="padding:12px 14px;"><p style="margin:0 0 8px;font-size:14px;font-weight:600;color:#c62828;">🚨 警报</p>'
        for a in alerts:
            al += f'<p style="margin:3px 0;padding:4px 0;font-size:12px;color:#555;">{_strip_html(a)}</p>'
        al += '</td></tr>'
        extra_parts.append(al)

    html = html.replace("{{ALERTS}}", "\n".join(extra_parts) if extra_parts else "")

    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")  # type: ignore[assignment]
    msg["From"] = msg["To"] = QQ_EMAIL
    _send_smtp(msg)


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

    # 持仓 vs 推荐对比
    compare_lines = _compare_with_recommendations(rows)
    if compare_lines:
        md_lines.append("")
        md_lines.extend(compare_lines)

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
        m = re.search(rf'var {js_var}\s*=\s*["\']([-\d.]+)["\']', data)
        if m:
            result[key] = float(m.group(1))
    return result


def _parse_price_info(data: str) -> int | None:
    """提取基金价格/净值"""
    m = re.search(r'"data":\[([\d.]+),([\d.]+),([\d.]+),([\d.]+),([\d.]+)\]', data)
    return int(float(m.group(1))) if m else None


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
    dep, end = 0, as_
    for i in range(as_, min(as_ + 500000, len(data))):
        if data[i] == "[":
            dep += 1
        elif data[i] == "]":
            dep -= 1
            if dep == 0:
                end = i
                break
    try:
        import json as _json
        full = _json.loads(data[as_:end + 1])
        return [{"d": datetime.datetime.fromtimestamp(int(n["x"]) // 1000).strftime("%Y-%m-%d"),
                 "v": float(n["y"]), "ts": int(n["x"])} for n in full]
    except (ValueError, KeyError, TypeError, IndexError):
        return None


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


def _score_annual_return(d: dict) -> float:
    """年化收益率评分"""
    ann_ret = d.get("annual_return")
    if ann_ret is None:
        return 0.0
    if ann_ret >= 50:       ret_score = 100
    elif ann_ret >= 30:     ret_score = 80 + (ann_ret - 30) / 20 * 20
    elif ann_ret >= 20:     ret_score = 65 + (ann_ret - 20) / 10 * 15
    elif ann_ret >= 10:     ret_score = 45 + (ann_ret - 10) / 10 * 20
    elif ann_ret >= 5:      ret_score = 30 + (ann_ret - 5) / 5 * 15
    elif ann_ret >= 0:      ret_score = 10 + ann_ret / 5 * 20
    else:                   ret_score = 0
    return min(100, ret_score)


def _score_y1(d: dict) -> float:
    """近1年收益评分"""
    y1 = d.get("y1")
    if y1 is None:
        return 0.0
    if y1 >= 100:       y1_score = 100
    elif y1 >= 50:      y1_score = 80 + (y1 - 50) / 50 * 20
    elif y1 >= 30:      y1_score = 65 + (y1 - 30) / 20 * 15
    elif y1 >= 10:      y1_score = 45 + (y1 - 10) / 20 * 20
    elif y1 >= 0:       y1_score = 10 + y1 / 10 * 35
    else:               y1_score = 0
    return min(100, y1_score)


def _score_sharpe(d: dict) -> float:
    """夏普比率评分"""
    sharpe = d.get("sharpe")
    if sharpe is None:
        return 0.0
    return float(max(0, min(100, sharpe * 25 + 10)))


def _score_sortino(d: dict) -> float:
    """索提诺比率评分"""
    sortino = d.get("sortino")
    if sortino is None:
        return 0.0
    return float(max(0, min(100, sortino * 17 + 10)))


def _score_profit_ratio(d: dict) -> float:
    """盈亏比评分"""
    pr = d.get("profit_ratio")
    if pr is None:
        return 0.0
    return float(max(0, min(100, (pr - 0.5) * 70)))


def _score_recovery(d: dict) -> float:
    """修复系数评分"""
    rec = d.get("recovery")
    if rec is None:
        return 0.0
    return float(max(0, min(100, rec * 4 + 5)))


def _score_sy3(d: dict) -> float:
    """近3年收益评分 — 从净值数据计算"""
    sy3 = d.get("sy3")
    if sy3 is None:
        return 0.0
    if sy3 >= 100:   sy3_score = 100
    elif sy3 >= 50:  sy3_score = 80 + (sy3 - 50) / 50 * 20
    elif sy3 >= 20:  sy3_score = 60 + (sy3 - 20) / 30 * 20
    elif sy3 >= 0:   sy3_score = 20 + sy3 / 20 * 40
    else:            sy3_score = 0
    return sy3_score


def _score_max_dd(d: dict) -> float:
    """最大回撤评分"""
    max_dd = d.get("max_dd")
    if max_dd is None:
        return 0.0
    return float(max(0, min(90, 110 - max_dd * 1.2)))


def _score_win_rate(d: dict) -> float:
    """上行胜率评分"""
    win_rate = d.get("win_rate")
    if win_rate is None:
        return 0.0
    return float(max(0, min(90, (win_rate - 30) * 3)))


def _score_institutional(d: dict) -> float:
    """机构持有比例评分"""
    inst = d.get("inst")
    if inst is None:
        return 0.0
    return float(max(10, min(90, inst * 1.5 + 20)))


def _score_scale(d: dict) -> float:
    """基金规模评分"""
    sc = d.get("sc")
    if sc is None:
        return 0.0
    if 1 <= sc <= 50:       scale_score = 90
    elif 0.5 <= sc < 1:     scale_score = 60
    elif 50 < sc <= 100:    scale_score = 70
    elif sc > 100:          scale_score = 40
    else:                   scale_score = 30
    return scale_score


def _score_rate(d: dict) -> float:
    """费率评分"""
    rate = d.get("rate")
    if rate is None:
        return 0.0
    return float(max(20, 100 - rate * 40))


# ── 评分维度注册表 ────────────────────────────
# 格式: (显示名, 函数, 权重, 说明)
# 增/删/改权重都在这里，_calc_score 和晚报说明自动同步
SCORE_DIMS: list[tuple[str, Callable, float, str]] = [
    ("近1年收益",    _score_y1,             0.10, "最近一年的表现，反映基金近期赚钱能力"),
    ("年化收益率",    _score_annual_return,  0.05, "基金成立以来年化回报"),
    ("夏普比率",     _score_sharpe,         0.15, "每承受 1 份波动能换来多少超额收益"),
    ("索提诺比率",   _score_sortino,        0.10, "只考虑下跌波动，更贴近真实风险感受"),
    ("最大回撤",     _score_max_dd,         0.10, "历史最大跌幅，公式：max(0, min(90, 110-回撤×1.2))"),
    ("上行胜率",     _score_win_rate,       0.05, "赚钱天数占总交易天数的比例"),
    ("盈亏比",       _score_profit_ratio,   0.05, "平均盈利÷平均亏损，>1说明赚比亏多"),
    ("修复系数",     _score_recovery,       0.10, "总收益÷最大回撤，衡量跌下去能不能涨回来"),
    ("近3年收益",    _score_sy3,            0.15, "从净值数据取约750个交易日精确计算，看穿越牛熊能力"),
    ("机构持有比例", _score_institutional,  0.02, "专业机构认可度，小幅参考"),
    ("基金规模",     _score_scale,          0.03, "1~50亿最理想，太小不灵活、太大难操作"),
    ("费率",         _score_rate,           0.10, "申购费越低越好"),
]


def _calc_score(d: dict) -> float:
    """
    计算基金综合评分 (0-100)

    从 SCORE_DIMS 注册表动态读取维度和权重。
    增删改维度只需编辑 SCORE_DIMS，无需改此函数。
    """
    total = 0.0
    total_weight = 0.0
    for name, fn, weight, desc in SCORE_DIMS:
        if weight > 0:
            score = fn(d)
            total += score * weight
            total_weight += weight

    if total_weight == 0:
        return 0.0
    return round(min(100, total / total_weight), 1)


def _calc_nav_metrics(full_nav: list[dict]) -> dict:
    """
    从完整净值列表计算风险指标。

    返回:
      annual_return: 年化收益率(%)
      volatility: 年化波动率(%)
      max_dd: 最大回撤(%)
      calmar: 卡玛比率
      sharpe: 夏普比率
      sortino: 索提诺比率
      win_rate: 上行胜率(%)
      profit_ratio: 盈亏比 — 平均盈利 / 平均亏损
      recovery: 修复系数 — 总收益 / 最大回撤
      max_loss_days: 最长连续下跌天数
    """
    if not full_nav or len(full_nav) < 30:
        return {}
    prices = [n["v"] for n in full_nav]
    days = len(prices)

    # 日收益率（小数形式，非百分比）
    daily_r = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, days)]
    n = len(daily_r)

    # 年化收益率
    total_return = (prices[-1] - prices[0]) / prices[0]
    annual_return = ((1 + total_return) ** (250 / days) - 1) * 100

    # 年化波动率
    mean_r = sum(daily_r) / n
    variance = sum((r - mean_r) ** 2 for r in daily_r) / n
    import math
    volatility = math.sqrt(variance * 250) * 100

    # 最大回撤
    peak = prices[0]
    max_dd = 0.0
    for p in prices:
        if p > peak:
            peak = p
        dd = (peak - p) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # 卡玛比率
    calmar = annual_return / max_dd if max_dd > 0 else 0

    # 下行波动率（只算负收益）
    neg_r = [r for r in daily_r if r < 0]
    if len(neg_r) > 1:
        down_var = sum((r - mean_r) ** 2 for r in neg_r) / len(neg_r)
        down_dev = math.sqrt(down_var * 250) * 100
    else:
        down_dev = volatility  # fallback

    # 夏普比率 & 索提诺比率（无风险利率 2.5%）
    rf = 2.5
    sharpe = (annual_return - rf) / volatility if volatility > 0 else 0
    sortino = (annual_return - rf) / down_dev if down_dev > 0 else 0

    # 上行胜率
    win_rate = sum(1 for r in daily_r if r > 0) / n * 100

    # 盈亏比
    avg_win = sum(r for r in daily_r if r > 0) / max(sum(1 for r in daily_r if r > 0), 1)
    avg_loss = abs(sum(r for r in daily_r if r < 0) / max(sum(1 for r in daily_r if r < 0), 1))
    profit_ratio = avg_win / avg_loss if avg_loss > 0 else 0

    # 修复系数 = 总收益 / 最大回撤
    total_return_pct = total_return * 100
    recovery = abs(total_return_pct / max_dd) if max_dd > 0 else 0

    # 最长连续下跌天数
    max_loss_days = 0
    cur = 0
    for r in daily_r:
        if r < 0:
            cur += 1
            max_loss_days = max(max_loss_days, cur)
        else:
            cur = 0

    return {
        "annual_return": round(annual_return, 2),
        "volatility": round(volatility, 2),
        "max_dd": round(max_dd, 2),
        "calmar": round(calmar, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "win_rate": round(win_rate, 1),
        "profit_ratio": round(profit_ratio, 2),
        "recovery": round(recovery, 2),
        "max_loss_days": max_loss_days,
    }


def _rank_percentile_str(d: dict) -> str:
    """返回排名百分位字符串，如 'top 1.2%'"""
    rk = d.get("rank")
    total = d.get("rank_total")
    if rk is not None and total:
        pct = rk / total * 100
        if pct <= 5:
            return f"top {pct:.1f}%🌟"
        elif pct <= 20:
            return f"top {pct:.1f}%"
        else:
            return f"{pct:.1f}%"
    return ""


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
    d["sy6"] = _parse_syl_6y(data)

    return d


# ── 历史快照 ──────────────────────────────────

def load_hist(code: str) -> dict:
    p = os.path.join(HISTORY_DIR, f".fw_{code}.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError) as _e:
            log.warning("读取历史文件失败 %s: %s", code, _e)
    return {}


def save_hist(code: str, h: dict) -> None:
    with open(os.path.join(HISTORY_DIR, f".fw_{code}.json"), "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False)


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

    if h.get("s") and d.get("sc") is not None:
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
        "_sy6": d.get("sy6"),
    }
    return row, alerts


# ── 持仓 vs 推荐对比 ──────────────────────────

def _load_recommend_data() -> dict | None:
    """加载推荐结果完整数据（含日期和结果列表），合并文件读取"""
    if not os.path.exists(_RECOMMEND_RESULT_FILE):
        return None
    try:
        with open(_RECOMMEND_RESULT_FILE, encoding="utf-8") as f:
            return json.load(f)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None


def _load_recommend_results() -> list[dict] | None:
    """加载基金推荐结果列表"""
    data = _load_recommend_data()
    if data is None:
        return None
    return data.get("results", [])  # type: ignore[no-any-return]


def _compare_with_recommendations(held_rows: list[dict]) -> list[str]:
    """
    展示市场优选基金排行（来自上次推荐结果）。
    """
    lines: list[str] = []
    recs = _load_recommend_results()
    if recs is None:
        lines.append("")
        lines.append("💡 **想看看市场上有哪些优秀基金？**")
        lines.append("   运行 python fund_recommend.py（约4分钟）")
        lines.append("   之后晚报自动展示推荐排行")
        return lines

    # 检查推荐结果是否过旧（复用已加载的数据）
    data = _load_recommend_data()
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
        except Exception:
            pass

    lines.append("")
    lines.append("🏆 **市场优选基金 TOP 10**  （11 维评分）")
    lines.append("")
    lines.append(f"|{'排名':<4}|{'基金名':<14}|{'年化%':<6}|{'近1月':<7}|{'近3月':<7}|{'近1年':<7}|{'夏普':<5}|{'回撤':<5}|{'近3年':<6}|")
    lines.append(f"|:---:|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(recs[:10], 1):
        badge = medals[i - 1] if i <= 3 else f" {i}."
        name = r.get("name", "")[:14]
        ar = r.get("annual_return", 0)
        m1 = r.get("m1", "")
        m3 = r.get("m3", "")
        y1 = r.get("y1", "")
        sharpe = r.get("sharpe", 0)
        dd = r.get("max_dd", 0)
        sy3 = r.get("sy3") or r.get("sy6", 0)
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
    today = datetime.date.today().isoformat()
    log.info("====== 基金晚报 %s 开始 ======", today)

    if not WECHAT_WEBHOOK:
        log.info("WECHAT_WEBHOOK 未设置，走邮件推送")

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

    # 计算评分（供持仓对比使用）
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
            "sy6": r.get("_sy6"),
        }
        r["score"] = _calc_score(d)

    rows = raw_rows

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

    # 持仓 vs 推荐对比
    if rows:
        compare_lines = _compare_with_recommendations(rows)
        if compare_lines:
            lines.extend(compare_lines)

    full_text = "\n".join(lines)

    print(full_text)
    push("📊 基金晚报", rows, all_alerts, today)
    log.info("====== 基金晚报 %s 完成 ======", today)


if __name__ == "__main__":
    main()
