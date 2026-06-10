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


def fetch_bytes(url: str, headers: dict | None = None) -> bytes | None:
    """带指数退避的 HTTP GET，返回原始 bytes（不缓存，供新浪等非标准编码使用）"""
    _cache_evict()
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    for attempt in range(1, _RETRY_MAX + 1):
        try:
            return urllib.request.urlopen(req, timeout=15).read()  # type: ignore[no-any-return]
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            if attempt < _RETRY_MAX:
                wait = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
                time.sleep(wait)
    log.warning("请求失败 %s (已重试 %d 次)", url, _RETRY_MAX)
    return None


# ── 推送 ──────────────────────────────────────

def _color_cls(val: str) -> str:
    """数值颜色 class：涨红跌绿"""
    if not val:
        return ""
    if val.startswith("+"):
        return ' class="red"'
    if val.startswith("-"):
        return ' class="green"'
    return ""


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _send_smtp(msg: MIMEText) -> None:
    """发送 SMTP 邮件（QQ 邮箱）"""
    try:
        s = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=10)
        s.login(QQ_EMAIL, QQ_AUTH_CODE)
        s.sendmail(QQ_EMAIL, [QQ_EMAIL], msg.as_string())
        s.quit()
        log.info("邮件发送成功")
    except Exception as e:
        log.error("邮件发送失败: %s", e)

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
    _send_smtp(msg)


def send_mail_html(subject: str, rows: list[dict], alerts: list[str], today: str) -> None:
    """通过 QQ 邮箱发送邮件（HTML 模板渲染，含评分排名）"""
    if not QQ_EMAIL or not QQ_AUTH_CODE:
        log.warning("QQ_EMAIL 或 QQ_MAIL_AUTH 未配置，邮件推送跳过")
        return
    tpl_path = os.path.join(HISTORY_DIR, "email_template.html")
    if not os.path.exists(tpl_path):
        log.warning("email_template.html 不存在，跳过邮件")
        return
    html = open(tpl_path, encoding="utf-8").read()
    html = html.replace("{{DATE}}", today)

    # 按评分排序
    rows_sorted = sorted(rows, key=lambda r: r.get("score", 0), reverse=True)

    # 表格行（含评分列）
    row_htmls = []
    for r in rows_sorted:
        score_str = f"{r.get('score', 0):.1f}"
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
            f'<td class="num">{score_str}</td>'
            f'</tr>'
        )
    html = html.replace("{{ROWS}}", "\n".join(row_htmls))

    # 构造排名 + 警报 HTML
    extra_parts = []

    # 评分排名榜
    medals = ["🥇", "🥈", "🥉"]
    rank_items = []
    for i, r in enumerate(rows_sorted[:5], 1):
        badge = medals[i - 1] if i <= 3 else f"{i}."
        rp = r.get("rank_pct", "")
        rank_items.append(
            f'<div style="font-size:13px;margin:4px 0;padding:4px 0;'
            f'border-bottom:1px solid #eee;">'
            f'{badge} <b>{r["name_short"]}</b> — {r.get("score", 0):.1f}分 '
            f'{rp} {r["y1"]}</div>'
        )
    if rank_items:
        extra_parts.append(
            '<div style="margin-top:16px;background:#f0f8ff;border:1px solid #bcd;'
            'border-radius:8px;padding:12px;">'
            '<div style="font-weight:bold;font-size:14px;margin-bottom:8px;">'
            '🏆 基金综合评分排名</div>'
            + "\n".join(rank_items) + '</div>'
        )

    # 警报
    if alerts:
        al = '<div style="margin-top:12px;background:#fff5f5;border:1px solid #fcc;border-radius:8px;padding:12px;">'
        al += '<div style="font-weight:bold;font-size:14px;margin-bottom:8px;color:#c62828;">🚨 警报</div>'
        for a in alerts:
            al += f'<div style="font-size:13px;margin:4px 0;padding:4px 0;border-bottom:1px solid #fee;">{_strip_html(a)}</div>'
        al += '</div>'
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
    """构造 Markdown 内容（含评分排名，企业微信推送用）"""
    md_lines = [
        f"📊 **基金晚报 {today}**",
        "",
        "|代码|基金名|涨跌|近5日|近1月|近3月|近1年|经理|评分|",
        "|:---|:---|---:|----:|----:|----:|----:|:---|:---|",
    ]
    rows_sorted = sorted(rows, key=lambda r: r.get("score", 0), reverse=True)
    for r in rows_sorted:
        score_str = f"{r.get('score', 0):.1f}"
        md_lines.append(
            f"|{r['code']}|{r['name_short']}|{r['day']}|{r['f5']}|{r['m1']}|{r['m3']}|{r['y1']}|{r['mgr']}|{score_str}|"
        )
    # 评分排名榜
    medals = ["🥇", "🥈", "🥉"]
    rank_lines = ["", "**🏆 基金综合评分排名**"]
    for i, r in enumerate(rows_sorted[:5], 1):
        badge = medals[i - 1] if i <= 3 else f"{i}."
        rp = r.get("rank_pct", "")
        rank_lines.append(f"{badge} **{r['name_short']}** — {r.get('score', 0):.1f}分  {rp}  {r['y1']}")
    md_lines.append("\n".join(rank_lines))
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
    """提取净值趋势（最近6条，供日报表使用）"""
    nav = _parse_full_nav(data)
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
    except Exception:
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


def _calc_score(d: dict) -> float:
    """
    计算基金综合评分 (0-100)

    8 维透明评分（所有数据均可追溯）：
      - 同类排名百分位 (20%): 在同类基金中的排位
      - 夏普比率 (20%): 每单位总波动的超额收益（经典风险调整指标）
      - 索提诺比率 (15%): 每单位下行波动的超额收益（只惩罚下跌）
      - 最大回撤 (10%): 历史最大跌幅，越小越好
      - 上行胜率 (10%): 日收益率 > 0 的天数占比
      - 机构持有比例 (10%): 机构资金认可度
      - 基金规模 (5%): 1~50亿最理想
      - 费率 (5%): 申购费越低越好
    """
    scores: list[float] = []
    total_weight = 0.0

    # 1. 同类排名评分 (20%)
    rk = d.get("rank")
    rk_total = d.get("rank_total")
    if rk is not None and rk_total and rk_total > 0:
        pct = rk / rk_total * 100
        if pct <= 1:        rank_score = 100
        elif pct <= 5:      rank_score = 95
        elif pct <= 10:     rank_score = 85
        elif pct <= 20:     rank_score = 70
        elif pct <= 30:     rank_score = 55
        elif pct <= 50:     rank_score = 35
        else:               rank_score = max(0, 25 - (pct - 50) * 0.5)
        scores.append(rank_score * 0.20)
        total_weight += 0.20

    # 2. 夏普比率 (20%) — 每单位总波动的超额收益
    sharpe = d.get("sharpe")
    if sharpe is not None:
        # >3 → 100, 2→80, 1.5→65, 1→50, 0.5→30, 0→10, <0→0
        s = max(0, min(100, sharpe * 25 + 10))
        scores.append(s * 0.20)
        total_weight += 0.20

    # 3. 索提诺比率 (15%) — 每单位下行波动的超额收益
    sortino = d.get("sortino")
    if sortino is not None:
        # >4 → 100, 3→80, 2→60, 1→35, 0→10, <0→0
        s = max(0, min(100, sortino * 17 + 10))
        scores.append(s * 0.15)
        total_weight += 0.15

    # 4. 最大回撤 (10%)
    max_dd = d.get("max_dd")
    if max_dd is not None:
        # <10% → 90分, 20%→70, 30%→50, 40%→30, 50%→10
        dd_score = max(0, min(90, 110 - max_dd * 2))
        scores.append(dd_score * 0.10)
        total_weight += 0.10

    # 5. 上行胜率 (10%)
    win_rate = d.get("win_rate")
    if win_rate is not None:
        # >55% → 90, 50%→70, 45%→50, 40%→30, <35%→10
        wr_score = max(0, min(90, (win_rate - 30) * 3))
        scores.append(wr_score * 0.10)
        total_weight += 0.10

    # 6. 机构持有比例 (10%)
    inst = d.get("inst")
    if inst is not None:
        # >50% → 90, 30%→70, 10%→50, 5%→30, <1%→10
        inst_score = max(10, min(90, inst * 1.5 + 20))
        scores.append(inst_score * 0.10)
        total_weight += 0.10

    # 7. 基金规模 (5%)
    sc = d.get("sc")
    if sc is not None:
        if 1 <= sc <= 50:
            scale_score = 90
        elif 0.5 <= sc < 1:
            scale_score = 60
        elif 50 < sc <= 100:
            scale_score = 70
        elif sc > 100:
            scale_score = 40
        else:
            scale_score = 30  # < 0.5亿
        scores.append(scale_score * 0.05)
        total_weight += 0.05

    # 8. 费率 (5%)
    rate = d.get("rate")
    if rate is not None:
        rate_score = max(20, 100 - rate * 40)
        scores.append(rate_score * 0.05)
        total_weight += 0.05

    if not scores:
        return 0.0

    return round(min(100, sum(scores) / total_weight), 1)


def _calc_nav_metrics(full_nav: list[dict]) -> dict:
    """
    从完整净值列表计算风险指标。

    返回:
      annual_return: 年化收益率(%)
      volatility: 年化波动率(%)
      max_dd: 最大回撤(%)
      calmar: 卡玛比率
      sharpe: 夏普比率 — (年化收益 - 无风险) / 年化波动率
      sortino: 索提诺比率 — (年化收益 - 无风险) / 下行波动率
      win_rate: 上行胜率(%) — 日收益率 > 0 的天数占比
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

    return {
        "annual_return": round(annual_return, 2),
        "volatility": round(volatility, 2),
        "max_dd": round(max_dd, 2),
        "calmar": round(calmar, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "win_rate": round(win_rate, 1),
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
    if nav := _parse_net_trend(data):
        d["nav"] = nav
    # 完整净值（用于计算回撤/波动率/卡玛比率）
    if full_nav := _parse_full_nav(data):
        d["full_nav"] = full_nav
        metrics = _calc_nav_metrics(full_nav)
        d.update(metrics)
    if td := _parse_real_time(code):
        d["td"] = td
    if holds := _parse_holdings(code):
        d["holds"] = holds
    if rp := _parse_rank_info(data):
        d["rank"], d["rank_total"] = rp
    if rate := _parse_fund_rate(data):
        d["rate"] = rate

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
    }
    return row, alerts


# ── 主程序 ────────────────────────────────────

def main() -> None:
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

    # 第二遍：计算评分（不需要 peer_returns，全透明指标）
    for r in raw_rows:
        d = {
            "rank": r.get("_rank"),
            "rank_total": r.get("_rank_total"),
            "sharpe": r.get("_sharpe"),
            "sortino": r.get("_sortino"),
            "max_dd": r.get("_max_dd"),
            "win_rate": r.get("_win_rate"),
            "inst": r.get("_inst"),
            "sc": r.get("_sc"),
            "rate": r.get("_rate"),
        }
        r["score"] = _calc_score(d)
        r["rank_pct"] = _rank_percentile_str(d)

    # 按综合评分排序
    rows = sorted(raw_rows, key=lambda r: r.get("score", 0), reverse=True)

    # 纯文本（终端用）
    lines = [
        f"📊 基金晚报 {today}",
        "",
        f"{'代码':<6} {'基金名':<14} {'涨跌':<8} {'近5日':<8} {'近1月':<8} {'近3月':<8} {'近1年':<8} {'经理':<6} {'评分':<6}",
        "-" * 80,
    ]
    for i, r in enumerate(rows, 1):
        score_str = f"{r.get('score', 0):.1f}"
        lines.append(f"{r['code']:<6} {r['name_short']:<14} {r['day']:<8} {r['f5']:<8} {r['m1']:<8} {r['m3']:<8} {r['y1']:<8} {r['mgr']:<6} {score_str:<6}")
    if all_alerts:
        lines.append("")
        lines.append("🚨 警报:")
        for a in all_alerts:  # type: ignore[assignment]
            lines.append(f"  {a}")

    # 综合评分排名
    if rows:
        lines.append("")
        lines.append("🏆 基金综合评分排名")
        lines.append(f"{'排名':<4} {'基金名':<14} {'评分':<6} {'同类排名':<12} {'近1年':<8} {'经理':<6}")
        lines.append("-" * 56)
        medals = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(rows[:5], 1):
            badge = medals[i - 1] if i <= 3 else f"{i:>2d}."
            rp = r.get("rank_pct", "")
            lines.append(f"{badge:<4} {r['name_short']:<14} {r.get('score', 0):<6.1f} {rp:<12} {r['y1']:<8} {r['mgr']:<6}")
    full_text = "\n".join(lines)

    print(full_text)
    push("📊 基金晚报", rows, all_alerts, today)
    log.info("====== 基金晚报 %s 完成 ======", today)


if __name__ == "__main__":
    main()
