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
from fund_utils import send_wechat, log, HISTORY_DIR, fetch_bytes, send_mail, parse_sina_csv
from config import get_secret as _get_secret

# ── 成交额历史（用于动态百分位阈值） ──────────
_VOLUME_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".volume_history.json")
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
            if senti.get("pct") is not None:
                lines.append(f"> 高于{senti['pct']:.0f}%的交易日（近{senti['history_days']}天）")

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
            if senti.get("pct") is not None:
                lines.append(f"  （高于{senti['pct']:.0f}%的交易日，近{senti['history_days']}天）")

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
    """获取市场成交额（含历史对比数据）"""
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

        # 最近几天成交额（用于显示趋势）
        recent = {}
        for d in dates[-7:]:
            recent[d] = history[d]

        # 历史百分位
        pct = None
        if len(dates) >= 10:
            vals = sorted(history.values())
            n = len(vals)
            pct = sum(1 for v in vals if v <= amount_yi) / n * 100

        return {
            "amount": round(amount_yi, 0),
            "recent": recent,
            "history_days": len(dates),
            "pct": round(pct, 0) if pct is not None else None,
        }
    except Exception as e:
        log.debug("获取成交额失败: %s", e)
        return None


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


def _fetch_market_breadth() -> dict | None:
    """获取涨跌家数（沪深两市合计）"""
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
        up = int(float(parts[28])) if len(parts) > 28 and parts[28] else 0
        down = int(float(parts[29])) if len(parts) > 29 and parts[29] else 0
        if up == 0 and down == 0:
            return None  # 收盘后数据清零，不展示
        return {"up": up, "down": down}
    except Exception as e:
        log.debug("获取涨跌家数失败: %s", e)
        return None


def main() -> None:
    log.info("====== 全球股市简报 开始 ======")
    brief_md = build_briefing_md()
    brief_text = build_briefing_text()
    print(brief_text)

    webhook = _get_secret("WECHAT_WEBHOOK")
    if webhook:
        send_wechat(brief_md)
    else:
        send_mail("🌏 全球股市简报", brief_text)

    log.info("====== 全球股市简报 完成 ======")


if __name__ == "__main__":
    main()
