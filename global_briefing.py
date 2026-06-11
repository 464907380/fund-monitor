"""
全球股市简报 — 每天早上 09:30 推送

数据来源：
  - A股：新浪财经（主）→ 东方财富（备）
  - 全球：新浪财经（支持美股/日韩/欧洲等主要指数）
"""
import json
import re
import datetime
import os
import urllib.request
from fund_utils import send_wechat, log, HISTORY_DIR, fetch_bytes, send_mail, send_mail_html, parse_sina_csv
from config import get_secret as _get_secret

# ── 成交额历史（用于动态百分位阈值） ──────────
_VOLUME_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".volume_history.json")
_VOLUME_BREADTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".breadth_history.json")
_VOLUME_HISTORY_DAYS = 60  # 取近60个交易日做百分位计算


# ── 指数列表 ──────────────────────────────────
A_INDICES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399300", "沪深300"),
]

GLOBAL_INDICES = [
    ("gb_$dji",   "道琼斯"),
    ("gb_$ixic",  "纳斯达克"),
    ("gb_$inx",   "标普500"),
    ("gb_$hsi",   "恒生指数"),
    ("gb_$n225",  "日经225"),
    ("gb_$ks11",  "韩国KOSPI"),
    ("gb_$ftse",  "英国富时100"),
    ("gb_$gdaxi", "德国DAX"),
    ("gb_$fchi",  "法国CAC40"),
    ("gb_$ssmi",  "瑞士SMI"),
]


# WECHAT_WEBHOOK 在 main() 中惰性读取，支持环境变量刷新


def fetch_sina(code: str) -> dict | None:
    """从新浪财经获取A股指数"""
    url = f"https://hq.sinajs.cn/list={code}"
    data = fetch_bytes(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn",
    })
    if data is None:
        return None
    try:
        parts = parse_sina_csv(data, encoding="gbk")
        if parts is None or len(parts) < 6:
            return None
        name = parts[0]
        prev_close = float(parts[2]) if parts[2] else 0
        current = float(parts[3]) if parts[3] else 0
        change_pct = round((current - prev_close) / prev_close * 100, 2) if prev_close else 0
        return {"current": current, "change": change_pct}
    except Exception as e:
        log.warning("新浪获取 %s 失败: %s", code, e)
        return None


def fetch_eastmoney_a(code: str) -> dict | None:
    """备选：从东方财富获取A股指数"""
    mapping = {"sh000001": "1.000001", "sz399001": "0.399001", "sz399300": "1.000300"}
    secid = mapping.get(code)
    if not secid:
        return None
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f170"
    data = fetch_bytes(url)
    if data is None:
        return None
    try:
        j = json.loads(data.decode("utf-8"))
        d = j.get("data", {})
        current = d.get("f43", 0)
        change_pct = d.get("f170", 0)
        if current:
            return {"current": current, "change": round(change_pct, 2)}
    except Exception as e:
        log.warning("东方财富获取 %s 失败: %s", code, e)
    return None


def get_a_share() -> list[dict]:
    """获取A股三大指数（新浪→东方财富备选）"""
    results = []
    for code, name in A_INDICES:
        data = fetch_sina(code) or fetch_eastmoney_a(code)
        if data:
            data["code"] = name
            results.append(data)
    return results


def fetch_sina_global() -> list[dict]:
    """从新浪财经获取全球主要指数（批量请求，国内可访问）

    新浪全球指数返回格式（不同于A股）：
    var hq_str_gb_$dji="名称,当前价,涨跌幅%,日期时间,..."
    字段索引: 0=名称, 1=当前价, 2=涨跌幅%, 3=日期
    """
    codes = ",".join(code for code, _ in GLOBAL_INDICES)
    url = f"https://hq.sinajs.cn/list={codes}"
    data = fetch_bytes(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn",
    })
    if data is None:
        return []
    name_map = dict(GLOBAL_INDICES)
    results = []
    try:
        text = data.decode("gbk")
        for line in text.strip().split("\n"):
            m = re.search(r'var hq_str_gb_\$(\w+)="(.+?)"', line)
            if not m:
                continue
            raw_code = m.group(1)
            parts = m.group(2).split(",")
            if len(parts) < 6:
                continue
            # 全球指数格式: 0=名称, 1=最新价, 2=涨跌幅%
            current = float(parts[1]) if parts[1] else 0
            change_pct = float(parts[2]) if parts[2] else 0
            if current:
                full_code = f"gb_${raw_code}"
                display_name = name_map.get(full_code, raw_code)
                results.append({
                    "code": display_name,
                    "current": current,
                    "change": round(change_pct, 2),
                })
        return results
    except Exception as e:
        log.warning("新浪全球指数获取失败: %s", e)
        return []


def get_global() -> list[dict]:
    """获取全球主要指数"""
    return fetch_sina_global()


def build_briefing_md() -> str:
    """构造简报 Markdown（企业微信推送用）"""
    today = datetime.date.today().isoformat()
    a_shares = get_a_share()
    globals_ = get_global()

    lines = [f"🌏 **全球股市简报 {today}**", ""]

    if a_shares:
        lines.append("**🇨🇳 A股**")
        lines.append("|指数|最新|涨跌幅|")
        lines.append("|---|---|---:|")
        for s in a_shares:
            c = s["change"]
            emoji = "🔴" if c > 0 else ("🟢" if c < 0 else "⚪")
            lines.append(f"|{s['code']}|{s['current']:.2f}|{emoji}{c:+.2f}%|")

    # 成交额及对比
    senti = _fetch_sentiment()
    breadth = _fetch_market_breadth()
    if senti or breadth:
        if a_shares:
            lines.append("")
        lines.append("**📊 市场情绪**")

        if senti:
            recent = sorted(senti["recent"].items())
            lines.append("**成交额**")
            lines.append("|日期|成交额|较前一日|")
            lines.append("|---|---|---:|")
            for i, (d, v) in enumerate(recent):
                if i == 0:
                    lines.append(f"|{d[-5:]}|{v:.0f}亿|—|")
                else:
                    prev_v = recent[i-1][1]
                    diff = v - prev_v
                    lines.append(f"|{d[-5:]}|{v:.0f}亿|{'↑' if diff>=0 else '↓'}{abs(diff):.0f}亿|")
            if senti.get("rank_str"):
                lines.append(f"> {senti['rank_str']}")

        if breadth:
            up, down = breadth["up"], breadth["down"]
            lines.append(f"**涨跌** 📈涨{up}家 📉跌{down}家")

    if globals_:
        if a_shares or senti:
            lines.append("")
        lines.append("**🌍 全球**")
        lines.append("|指数|最新|涨跌幅|")
        lines.append("|---|---|---:|")
        for s in globals_:
            c = s["change"]
            emoji = "🔴" if c > 0 else ("🟢" if c < 0 else "⚪")
            lines.append(f"|{s['code']}|{s['current']:.2f}|{emoji}{c:+.2f}%|")

    if not a_shares and not globals_:
        lines.append("❌ 所有数据源均不可用，请检查网络连接")

    lines.append("")
    lines.append("⏰ 美股/欧股为上一交易日收盘")

    return "\n".join(lines)


def build_briefing_text() -> str:
    """构造简报文本版（终端/邮件用，对齐格式参考晚报）"""
    today = datetime.date.today().isoformat()
    a_shares = get_a_share()
    globals_ = get_global()

    lines = [f"🌏 全球股市简报 {today}", ""]

    if a_shares:
        lines.append("🇨🇳 A股")
        lines.append(f"{'指数':<12} {'最新':<10} {'涨跌幅':<10}")
        lines.append("-" * 36)
        for s in a_shares:
            c = s["change"]
            emoji = "🔴" if c > 0 else ("🟢" if c < 0 else "⚪")
            lines.append(f"{s['code']:<12} {s['current']:<10.2f} {emoji}{c:+.2f}%")

    # 成交额及对比
    senti = _fetch_sentiment()
    breadth = _fetch_market_breadth()
    if senti or breadth:
        lines.append("")
        lines.append("📊 市场情绪")

        if senti:
            lines.append("成交额")
            lines.append(f"{'日期':<12} {'成交额':<12} {'较前一日':<12}")
            lines.append("-" * 36)
            for i, (d, v) in enumerate(sorted(senti["recent"].items())):
                if i == 0:
                    lines.append(f"{d[-5:]:<12} {v:.0f}亿{'':<9} —")
                else:
                    prev_v = sorted(senti["recent"].items())[i-1][1]
                    diff = v - prev_v
                    arrow = "↑" if diff >= 0 else "↓"
                    lines.append(f"{d[-5:]:<12} {v:.0f}亿{'':<9} {arrow}{abs(diff):.0f}亿")
            if senti.get("rank_str"):
                lines.append(f"  {senti['rank_str']}")

        if breadth:
            up, down = breadth["up"], breadth["down"]
            lines.append(f"涨跌  📈涨{up}家 📉跌{down}家")

    if globals_:
        lines.append("")
        lines.append("🌍 全球")
        lines.append(f"{'指数':<12} {'最新':<10} {'涨跌幅':<10}")
        lines.append("-" * 36)
        for s in globals_:
            c = s["change"]
            emoji = "🔴" if c > 0 else ("🟢" if c < 0 else "⚪")
            lines.append(f"{s['code']:<12} {s['current']:<10.2f} {emoji}{c:+.2f}%")

    if not a_shares and not globals_:
        lines.append("❌ 所有数据源均不可用，请检查网络连接")

    lines.append("")
    lines.append("⏰ 美股/欧股为上一交易日收盘")

    return "\n".join(lines)


def build_briefing() -> str:
    """兼容旧接口：返回 Markdown（企业微信推送用）"""
    return build_briefing_md()


def _fetch_sentiment() -> dict | None:
    """获取市场成交额（含历史对比数据），不足7天时从腾讯回填"""
    try:
        url = "https://hq.sinajs.cn/list=sh000001,sz399001"
        data = fetch_bytes(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        if data is None:
            return None
        text = data.decode("gbk")
        total_amount = 0.0
        for line in text.strip().split("\n"):
            m = re.search(r'"(.*?)"', line)
            if m:
                parts = m.group(1).split(",")
                if len(parts) >= 10 and parts[9]:
                    total_amount += float(parts[9])

        amount_yi = total_amount / 1e8
        today = datetime.date.today().isoformat()

        # 保存当日成交额到历史
        history = _load_volume_history()
        history[today] = round(amount_yi, 0)
        dates = sorted(history.keys())
        if len(dates) > _VOLUME_HISTORY_DAYS:
            for d in dates[:-_VOLUME_HISTORY_DAYS]:
                del history[d]
        _save_volume_history(history)

        # 如果不足7天，从腾讯回填（用成交量估算成交额）
        if len(dates) < 7:
            _backfill_volume_history(history, amount_yi)
            dates = sorted(history.keys())

        # 全部历史数据
        recent = dict(sorted(history.items()))

        # 排名（比历史多少天高/低）
        rank_str = None
        if len(dates) >= 5:
            vals = sorted(history.values())
            n = len(vals)
            below = sum(1 for v in vals if v <= amount_yi)  # ≤今天的（含自己）
            rank = n - below + 1  # 排名，1=最高，n=最低
            higher_than = below - 1  # 今天比多少天高
            rank_str = f"近{n}天中排第{rank}（高于{higher_than}天"
            if higher_than == 0:
                rank_str += "，最低）"
            elif higher_than == n - 1:
                rank_str += "，最高）"
            else:
                rank_str += "）"

        return {
            "amount": round(amount_yi, 0),
            "recent": recent,
            "rank_str": rank_str,
        }
    except Exception as e:
        log.debug("获取成交额失败: %s", e)
        return None


def _backfill_volume_history(history: dict, today_amount: float) -> None:
    """从腾讯K线API回填历史成交额（用成交量估算）"""
    try:
        url = "http://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,10,qfq"
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=10).read()
        j = json.loads(resp)
        klines = j["data"]["sh000001"]["day"]

        # 找今天的K线算换算比例
        ratio = None
        for k in klines:
            if k[0] == datetime.date.today().isoformat() and today_amount > 0:
                vol = float(k[5])
                close = float(k[2])
                if vol > 0:
                    ratio = today_amount / (vol * close / 1e8)
                break

        if not ratio:
            return

        filled = 0
        for k in klines:
            date = k[0]
            if date in history:
                continue
            vol = float(k[5])
            close = float(k[2])
            estimated = round(vol * close / 1e8 * ratio, 0)
            if estimated > 0:
                history[date] = estimated
                filled += 1

        if filled:
            _save_volume_history(history)
            log.info("成交量回填: 新增 %d 天", filled)
    except Exception as e:
        log.debug("成交量回填失败: %s", e)


def _load_volume_history() -> dict:
    """加载成交额历史"""
    if not os.path.exists(_VOLUME_HISTORY_FILE):
        return {}
    try:
        with open(_VOLUME_HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return {}


def _save_volume_history(history: dict) -> None:
    """保存成交额历史"""
    try:
        with open(_VOLUME_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
    except OSError as e:
        log.debug("保存成交额历史失败: %s", e)


def _load_breadth_history() -> dict:
    """加载上次的涨跌家数"""
    if not os.path.exists(_VOLUME_BREADTH_FILE):
        return {}
    try:
        with open(_VOLUME_BREADTH_FILE, encoding="utf-8") as f:
            return json.load(f)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return {}


def _save_breadth_history(data: dict) -> None:
    """保存涨跌家数"""
    try:
        with open(_VOLUME_BREADTH_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass


def _fetch_market_breadth() -> dict | None:
    """获取涨跌家数（沪深两市合计），收盘后展示上次最后值"""
    try:
        url = "https://hq.sinajs.cn/list=sh000001"
        data = fetch_bytes(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        if data is None:
            return _load_breadth_history() or None
        text = data.decode("gbk")
        m = re.search(r'"(.*?)"', text)
        if not m:
            return _load_breadth_history() or None
        parts = m.group(1).split(",")
        up = int(float(parts[28])) if len(parts) > 28 and parts[28] else 0
        down = int(float(parts[29])) if len(parts) > 29 and parts[29] else 0
        if up > 0 or down > 0:
            result = {"up": up, "down": down}
            _save_breadth_history(result)
            return result
        # 收盘已清零，返回上次保存的值
        return _load_breadth_history() or None
    except Exception as e:
        log.debug("获取涨跌家数失败: %s", e)
        return _load_breadth_history() or None


def build_briefing_html() -> str:
    """构造简报 HTML（邮件推送用，深色主题同晚报）"""
    today = datetime.date.today().isoformat()
    a_shares = get_a_share()
    globals_ = get_global()
    senti = _fetch_sentiment()
    breadth = _fetch_market_breadth()

    rows = []

    # A股
    if a_shares:
        rows.append('<tr style="background:#2a2a2a;"><td style="padding:8px 12px;font-size:13px;font-weight:600;color:#ccc;" colspan="3">🇨🇳 A股</td></tr>')
        rows.append('<tr style="background:#222;"><td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;border-bottom:1px solid #333;">指数</td>'
                    '<td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;text-align:right;border-bottom:1px solid #333;">最新</td>'
                    '<td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;text-align:right;border-bottom:1px solid #333;">涨跌幅</td></tr>')
        for s in a_shares:
            c = s["change"]
            color = "#ef5350" if c > 0 else ("#66bb6a" if c < 0 else "#ccc")
            rows.append(f'<tr><td style="padding:6px 12px;border-bottom:1px solid #333;color:#ccc;">{s["code"]}</td>'
                        f'<td style="padding:6px 12px;text-align:right;font-family:Consolas;border-bottom:1px solid #333;color:#ccc;">{s["current"]:.2f}</td>'
                        f'<td style="padding:6px 12px;text-align:right;font-weight:600;font-family:Consolas;border-bottom:1px solid #333;color:{color};">{"🔴" if c>0 else "🟢" if c<0 else "⚪"}{c:+.2f}%</td></tr>')

    # 成交额
    if senti:
        rows.append('<tr><td style="padding:10px 12px 4px;" colspan="3"><p style="margin:0;font-size:13px;font-weight:600;color:#ccc;">📊 成交额</p></td></tr>')
        rows.append('<tr style="background:#222;"><td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;border-bottom:1px solid #333;">日期</td>'
                    '<td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;text-align:right;border-bottom:1px solid #333;">成交额</td>'
                    '<td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;text-align:right;border-bottom:1px solid #333;">较前一日</td></tr>')
        for i, (d, v) in enumerate(sorted(senti["recent"].items())):
            if i == 0:
                diff_str = "—"
            else:
                prev_v = sorted(senti["recent"].items())[i-1][1]
                diff = v - prev_v
                diff_str = f'<span style="color:#ef5350;">↑{abs(diff):.0f}亿</span>' if diff >= 0 else f'<span style="color:#66bb6a;">↓{abs(diff):.0f}亿</span>'
            rows.append(f'<tr><td style="padding:4px 12px;border-bottom:1px solid #333;color:#ccc;">{d[-5:]}</td>'
                        f'<td style="padding:4px 12px;text-align:right;font-family:Consolas;border-bottom:1px solid #333;color:#ccc;">{v:.0f}亿</td>'
                        f'<td style="padding:4px 12px;text-align:right;font-family:Consolas;border-bottom:1px solid #333;">{diff_str}</td></tr>')
        if senti.get("rank_str"):
            rows.append(f'<tr><td style="padding:6px 12px;font-size:11px;color:#888;" colspan="3">📌 {senti["rank_str"]}</td></tr>')

    # 涨跌家数
    if breadth:
        up, down = breadth["up"], breadth["down"]
        rows.append(f'<tr><td style="padding:8px 12px;font-size:12px;color:#ccc;" colspan="3">📈涨{up}家  📉跌{down}家</td></tr>')

    # 全球
    if globals_:
        rows.append('<tr style="background:#2a2a2a;"><td style="padding:8px 12px;font-size:13px;font-weight:600;color:#ccc;" colspan="3">🌍 全球</td></tr>')
        rows.append('<tr style="background:#222;"><td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;border-bottom:1px solid #333;">指数</td>'
                    '<td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;text-align:right;border-bottom:1px solid #333;">最新</td>'
                    '<td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;text-align:right;border-bottom:1px solid #333;">涨跌幅</td></tr>')
        for s in globals_:
            c = s["change"]
            color = "#ef5350" if c > 0 else ("#66bb6a" if c < 0 else "#ccc")
            rows.append(f'<tr><td style="padding:6px 12px;border-bottom:1px solid #333;color:#ccc;">{s["code"]}</td>'
                        f'<td style="padding:6px 12px;text-align:right;font-family:Consolas;border-bottom:1px solid #333;color:#ccc;">{s["current"]:.2f}</td>'
                        f'<td style="padding:6px 12px;text-align:right;font-weight:600;font-family:Consolas;border-bottom:1px solid #333;color:{color};">{"🔴" if c>0 else "🟢" if c<0 else "⚪"}{c:+.2f}%</td></tr>')

    if not a_shares and not globals_:
        rows.append('<tr><td style="padding:20px 12px;text-align:center;color:#888;" colspan="3">❌ 所有数据源均不可用</td></tr>')

    if globals_ or a_shares:
        rows.append('<tr><td style="padding:8px 12px;font-size:11px;color:#555;text-align:center;" colspan="3">⏰ 美股/欧股为上一交易日收盘</td></tr>')

    body_rows = "\n".join(rows)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background:#000;font-family:'Helvetica Neue','PingFang SC','Microsoft YaHei',Arial,sans-serif;font-size:13px;color:#ccc;">
<table border="0" cellpadding="0" cellspacing="0" width="100%" style="background:#000;"><tr><td align="center" style="padding:20px 10px;">
<table border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width:600px;background:#1a1a1a;border-radius:8px;overflow:hidden;">

<tr><td style="text-align:center;padding:24px 16px 8px;">
<h1 style="margin:0;font-size:20px;color:#e0e0e0;">🌏 全球股市简报</h1>
<p style="margin:4px 0 0;font-size:12px;color:#666;">{today}</p>
</td></tr>

<tr><td style="padding:0 10px 10px;">
<table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-size:13px;">
{body_rows}
</table>
</td></tr>

<tr><td style="text-align:center;padding:16px 10px;font-size:11px;color:#555;border-top:1px solid #333;">Fund Monitor · 天天基金</td></tr>
</table>
</td></tr></table>
</body>
</html>"""


def main() -> None:
    log.info("====== 全球股市简报 开始 ======")
    brief_md = build_briefing_md()
    brief_text = build_briefing_text()
    brief_html = build_briefing_html()
    print(brief_text)

    webhook = _get_secret("WECHAT_WEBHOOK")
    if webhook:
        send_wechat(brief_md)
    else:
        send_mail_html("🌏 全球股市简报", brief_html)

    log.info("====== 全球股市简报 完成 ======")


if __name__ == "__main__":
    main()
