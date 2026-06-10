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

# ── 文件路径 ──────────────────────────────────
_HISTORY_DIR = os.path.dirname(os.path.abspath(__file__))
_RECOMMEND_RESULT_FILE = os.path.join(_HISTORY_DIR, ".fund_recommend_result.json")

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

ALERT_DROP_1M = CFG.get("fund_watch", {}).get("alert_drop_1m", -10)
ALERT_DROP_1M_RED = CFG.get("fund_watch", {}).get("alert_drop_1m_red", -15)
ALERT_SCALE_2X = CFG.get("fund_watch", {}).get("alert_scale_2x", 2.0)
ALERT_SCALE_1_5X = CFG.get("fund_watch", {}).get("alert_scale_1_5x", 1.5)

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
_CACHE_TTL = CFG.get("network", {}).get("cache_ttl_seconds", 300)
_CACHE_MAX = CFG.get("network", {}).get("cache_max_entries", 100)

# ── 重试配置 ──────────────────────────────────
_RETRY_MAX = CFG.get("network", {}).get("retry_max", 3)
_RETRY_BACKOFF = CFG.get("network", {}).get("retry_backoff_seconds", [1, 3, 8])


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
    """通过 QQ 邮箱发送邮件（HTML 模板渲染）"""
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

    # 构造警报 HTML
    extra_parts = []

    # 持仓 vs 推荐对比
    compare_lines = _compare_with_recommendations(rows)
    if compare_lines:
        compare_html = '<div style="margin-top:12px;background:#fffbe6;border:1px solid #e6c300;border-radius:8px;padding:12px;">'
        compare_html += '<div style="font-weight:bold;font-size:14px;margin-bottom:6px;">⚔️ 持仓 vs 市场优选</div>'
        for line in compare_lines:
            clean = line.strip()
            if not clean:
                continue
            # 简单 markdown→html 转换
            clean = clean.replace("**", "")
            compare_html += f'<div style="font-size:13px;margin:2px 0;">{clean}</div>'
        compare_html += '</div>'
        extra_parts.append(compare_html)

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


def _parse_syl_6y(data: str) -> float | None:
    """提取近6年收益率"""
    m = re.search(r'syl_6y="([-\d.]+)"', data)
    return float(m.group(1)) if m else None


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


def _score_rank(d: dict) -> tuple[float, float]:
    """同类排名维度 — 年化收益率评分 (权重10%)"""
    ann_ret = d.get("annual_return")
    if ann_ret is None:
        return (0.0, 0.0)
    if ann_ret >= 50:       ret_score = 100
    elif ann_ret >= 30:     ret_score = 80 + (ann_ret - 30) / 20 * 20
    elif ann_ret >= 20:     ret_score = 65 + (ann_ret - 20) / 10 * 15
    elif ann_ret >= 10:     ret_score = 45 + (ann_ret - 10) / 10 * 20
    elif ann_ret >= 5:      ret_score = 30 + (ann_ret - 5) / 5 * 15
    elif ann_ret >= 0:      ret_score = 10 + ann_ret / 5 * 20
    else:                   ret_score = 0
    return (min(100, ret_score), 0.10)


def _score_sharpe(d: dict) -> tuple[float, float]:
    """夏普比率评分 (权重15%)"""
    sharpe = d.get("sharpe")
    if sharpe is None:
        return (0.0, 0.0)
    return (max(0, min(100, sharpe * 25 + 10)), 0.15)


def _score_sortino(d: dict) -> tuple[float, float]:
    """索提诺比率评分 (权重10%)"""
    sortino = d.get("sortino")
    if sortino is None:
        return (0.0, 0.0)
    return (max(0, min(100, sortino * 17 + 10)), 0.10)


def _score_profit_ratio(d: dict) -> tuple[float, float]:
    """盈亏比评分 (权重5%)"""
    pr = d.get("profit_ratio")
    if pr is None:
        return (0.0, 0.0)
    return (max(0, min(100, (pr - 0.5) * 70)), 0.05)


def _score_recovery(d: dict) -> tuple[float, float]:
    """修复系数评分 (权重10%)"""
    rec = d.get("recovery")
    if rec is None:
        return (0.0, 0.0)
    return (max(0, min(100, rec * 4 + 5)), 0.10)


def _score_sy6(d: dict) -> tuple[float, float]:
    """近6年收益评分 (权重20%)"""
    sy6 = d.get("sy6")
    if sy6 is None:
        return (0.0, 0.0)
    if sy6 >= 100:   sy6_score = 100
    elif sy6 >= 50:  sy6_score = 80 + (sy6 - 50) / 50 * 20
    elif sy6 >= 20:  sy6_score = 60 + (sy6 - 20) / 30 * 20
    elif sy6 >= 0:   sy6_score = 20 + sy6 / 20 * 40
    else:            sy6_score = 0
    return (sy6_score, 0.20)


def _score_max_dd(d: dict) -> tuple[float, float]:
    """最大回撤评分 (权重10%)"""
    max_dd = d.get("max_dd")
    if max_dd is None:
        return (0.0, 0.0)
    return (max(0, min(90, 110 - max_dd * 2)), 0.10)


def _score_win_rate(d: dict) -> tuple[float, float]:
    """上行胜率评分 (权重5%)"""
    win_rate = d.get("win_rate")
    if win_rate is None:
        return (0.0, 0.0)
    return (max(0, min(90, (win_rate - 30) * 3)), 0.05)


def _score_institutional(d: dict) -> tuple[float, float]:
    """机构持有比例评分 (权重5%)"""
    inst = d.get("inst")
    if inst is None:
        return (0.0, 0.0)
    return (max(10, min(90, inst * 1.5 + 20)), 0.05)


def _score_scale(d: dict) -> tuple[float, float]:
    """基金规模评分 (权重5%)"""
    sc = d.get("sc")
    if sc is None:
        return (0.0, 0.0)
    if 1 <= sc <= 50:       scale_score = 90
    elif 0.5 <= sc < 1:     scale_score = 60
    elif 50 < sc <= 100:    scale_score = 70
    elif sc > 100:          scale_score = 40
    else:                   scale_score = 30
    return (scale_score, 0.05)


def _score_rate(d: dict) -> tuple[float, float]:
    """费率评分 (权重10%)"""
    rate = d.get("rate")
    if rate is None:
        return (0.0, 0.0)
    return (max(20, 100 - rate * 40), 0.10)


def _calc_score(d: dict) -> float:
    """
    计算基金综合评分 (0-100)

    11 维全透明评分：
      - 年化收益率 (10%): 长期年化回报
      - 夏普比率 (15%): 每单位总波动的超额收益
      - 索提诺比率 (10%): 每单位下行波动的超额收益
      - 盈亏比 (5%): 平均盈利 / 平均亏损，赚比亏多才算好
      - 修复系数 (10%): 总收益 / 最大回撤，跌下去能涨回来
      - 近6年收益 (20%): 穿越牛熊的长期表现
      - 最大回撤 (10%): 历史最大跌幅
      - 上行胜率 (5%): 日收益率 > 0 的天数占比
      - 机构持有比例 (5%): 机构资金认可度
      - 基金规模 (5%): 1~50亿最理想
      - 费率 (10%): 申购费越低越好
    """
    sub_scores = [
        _score_rank(d), _score_sharpe(d), _score_sortino(d),
        _score_profit_ratio(d), _score_recovery(d), _score_sy6(d),
        _score_max_dd(d), _score_win_rate(d), _score_institutional(d),
        _score_scale(d), _score_rate(d),
    ]
    total = 0.0
    total_weight = 0.0
    for score, weight in sub_scores:
        if weight > 0:
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
    if sy6 := _parse_syl_6y(data):
        d["sy6"] = sy6

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

def _load_recommend_results() -> list[dict] | None:
    """加载基金推荐结果"""
    if not os.path.exists(_RECOMMEND_RESULT_FILE):
        return None
    try:
        with open(_RECOMMEND_RESULT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("results", [])  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None


def _compare_with_recommendations(held_rows: list[dict]) -> list[str]:
    """
    对比持仓基金和推荐基金，返回对比文本行。
    加载最近一次 fund_recommend.py 的结果做比较。
    """
    lines: list[str] = []
    recs = _load_recommend_results()
    if recs is None:
        lines.append("")
        lines.append("💡 **想看看你的持仓 vs 市场优选？**")
        lines.append("   运行 python fund_recommend.py（约15分钟）")
        lines.append("   之后晚报自动展示对比分析")
        return lines

    # 检查推荐结果是否过旧
    try:
        with open(_RECOMMEND_RESULT_FILE, encoding="utf-8") as f:
            meta = json.load(f)
        rec_date = meta.get("date", "")
        if rec_date:
            rec_dt = datetime.date.fromisoformat(rec_date)
            days_old = (datetime.date.today() - rec_dt).days
            if days_old > 14:
                lines.append("")
                lines.append(f"⚠️ 推荐结果已是 {days_old} 天前的，建议重新运行")
                lines.append(f"   python fund_recommend.py")
    except Exception:
        pass

    # 计算持仓平均分
    held_scores = [r.get("score", 0) for r in held_rows]
    avg_held = sum(held_scores) / len(held_scores) if held_scores else 0

    # 推荐 TOP5 平均分
    top_recs = recs[:5]
    rec_avg = sum(r.get("score", 0) for r in top_recs) / len(top_recs) if top_recs else 0

    diff = rec_avg - avg_held
    lines.append("")
    lines.append("⚔️ **持仓 vs 市场优选**")
    lines.append(f"你的持仓平均分: {avg_held:.1f}")
    lines.append(f"市场优选平均分: {rec_avg:.1f}  (差距 {diff:+.1f}分)")
    if diff > 5:
        lines.append(f"💡 市场优选明显优于你的持仓，建议关注推荐结果")
    elif diff < -5:
        lines.append(f"💪 你的持仓表现优于市场平均，继续保持")

    # 找出持仓中评分最低的，推荐替代品
    sorted_held = sorted(held_rows, key=lambda r: r.get("score", 0))
    worst = sorted_held[0]
    worst_score = worst.get("score", 0)
    if worst_score < 50 and top_recs:
        best_rec = top_recs[0]
        lines.append("")
        lines.append(f"⚠️  **{worst['name_short']}({worst['code']})** 评分仅 {worst_score:.1f}")
        lines.append(f"   同类优选 **{best_rec['name']}({best_rec['code']})** 评分 {best_rec['score']:.1f}")
        lines.append(f"   对比: 你的持仓评分偏低，可考虑替换")

    return lines


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
