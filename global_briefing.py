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
from fund_watch import send_wechat, send_mail, log, HISTORY_DIR, fetch_bytes


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
]

WECHAT_WEBHOOK = os.getenv("WECHAT_WEBHOOK", "")


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
        text = data.decode("gbk")
        m = re.search(r'"(.*?)"', text)
        if not m:
            return None
        parts = m.group(1).split(",")
        if len(parts) < 6:
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


def build_briefing() -> str:
    """构造简报 Markdown（含市场情绪指标）"""
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

    # 市场情绪指标
    senti = _fetch_sentiment()
    breadth = _fetch_market_breadth()
    if senti or breadth:
        if a_shares:
            lines.append("")
        lines.append("**📊 市场情绪**")
        parts = []
        if senti:
            parts.append(f"成交额 {senti['amount']:.0f}亿 | {senti['mood']}")
        if breadth:
            parts.append(f"涨跌方向 {breadth}")
        lines.append("  ".join(parts))

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


def _fetch_sentiment() -> dict | None:
    """获取市场情绪指标：成交额、涨跌家数"""
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
        if amount_yi > 20000:
            mood = "🔥🔥 极端放量（>20000亿）"
        elif amount_yi > 14000:
            mood = "🔥 成交火爆（14000-20000亿）"
        elif amount_yi > 10000:
            mood = "🟡 成交活跃（10000-14000亿）"
        elif amount_yi > 7000:
            mood = "🟢 成交正常（7000-10000亿）"
        else:
            mood = "🔵 成交低迷（<7000亿）"

        return {"amount": round(amount_yi, 0), "mood": mood}
    except Exception as e:
        log.debug("获取情绪指标失败: %s", e)
        return None


def _fetch_market_breadth() -> str | None:
    """获取涨跌家数（沪深两市合计）

    尝试从 sh000001 字段[28][29]获取涨跌家数；
    若数据不可用，则根据上证指数涨跌方向做简单判断。
    """
    try:
        url = "https://hq.sinajs.cn/list=sh000001"
        data = fetch_bytes(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        if data is None:
            return None
        text = data.decode("gbk")
        m = re.search(r'"(.*?)"', text)
        if not m:
            return None
        parts = m.group(1).split(",")

        # 尝试获取精确涨跌家数（字段28=涨家数，29=跌家数）
        up = parts[28] if len(parts) > 28 and parts[28] else None
        down = parts[29] if len(parts) > 29 and parts[29] else None
        if up and down and up != "0" and down != "0":
            up_int = int(float(up))
            down_int = int(float(down))
            if up_int > down_int:
                emoji = "📈"
            elif up_int < down_int:
                emoji = "📉"
            else:
                emoji = "➖"
            return f"{emoji} 涨{up_int}家 / 跌{down_int}家"

        # 备选：根据上证指数涨跌方向判断
        prev_close = float(parts[2]) if parts[2] else 0
        current = float(parts[3]) if parts[3] else 0
        if current > prev_close:
            return "📈 涨多跌少"
        elif current < prev_close:
            return "📉 跌多涨少"
        else:
            return "➖ 涨跌持平"
    except Exception as e:
        log.debug("获取涨跌家数失败: %s", e)
        return None


def main() -> None:
    log.info("====== 全球股市简报 开始 ======")
    briefing = build_briefing()
    print(briefing)

    if WECHAT_WEBHOOK:
        send_wechat(briefing)
    else:
        text = briefing.replace("**", "").replace("|", " ")
        send_mail("🌏 全球股市简报", text)

    log.info("====== 全球股市简报 完成 ======")


if __name__ == "__main__":
    main()
