"""
全球股市简报 — 每天早上推送

数据来源：
  - A股：新浪财经（主）→ 东方财富（备）
  - 全球：Yahoo Finance → 若失败跳过，不阻塞推送
"""
import time
import urllib.error
import urllib.request
import json
import re
import datetime
import os
import logging
from config import CFG
from fund_watch import send_wechat, send_mail, log, HISTORY_DIR

# ── 重试配置 ──────────────────────────────────
_RETRY_MAX = CFG["global_briefing"]["retry_max"]
_RETRY_BACKOFF = CFG["global_briefing"]["retry_backoff_seconds"]


def _retry_fetch_url(url: str, headers: dict | None = None) -> bytes | None:
    """带指数退避的 HTTP GET 请求，全部失败返回 None（返回原始字节，由调用方决定编码）"""
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    for attempt in range(1, _RETRY_MAX + 1):
        try:
            return urllib.request.urlopen(req, timeout=10).read()  # type: ignore[no-any-return]
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            if attempt < _RETRY_MAX:
                wait = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
                time.sleep(wait)
    return None

# ── 指数列表 ──────────────────────────────────
A_INDICES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399300", "沪深300"),
]

GLOBAL_INDICES = [
    ("^DJI",  "道琼斯"),
    ("^GSPC", "标普500"),
    ("^IXIC", "纳斯达克"),
    ("^HSI",  "恒生指数"),
    ("^N225", "日经225"),
    ("^FTSE", "英国富时100"),
    ("^GDAXI","德国DAX"),
]

WECHAT_WEBHOOK = os.getenv("WECHAT_WEBHOOK", "")


def fetch_sina(code: str) -> dict | None:
    """从新浪财经获取A股指数"""
    url = f"https://hq.sinajs.cn/list={code}"
    data = _retry_fetch_url(url, {
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
    data = _retry_fetch_url(url)
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


def fetch_yahoo(symbol: str) -> dict | None:
    """从Yahoo Finance获取全球指数"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    data = _retry_fetch_url(url)
    if data is None:
        return None
    try:
        j = json.loads(data.decode("utf-8"))
        meta = j.get("chart", {}).get("result", [{}])[0].get("meta", {})
        reg_price = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose")
        if reg_price is None or prev_close is None:
            return None
        change_pct = round((reg_price - prev_close) / prev_close * 100, 2)
        return {"current": reg_price, "change": change_pct}
    except Exception as e:
        log.warning("Yahoo获取 %s 失败: %s", symbol, e)
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


def get_global() -> list[dict]:
    """获取全球主要指数"""
    results = []
    for symbol, name in GLOBAL_INDICES:
        data = fetch_yahoo(symbol)
        if data:
            data["code"] = name
            results.append(data)
    return results


def build_briefing() -> str:
    """构造简报 Markdown"""
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

    if globals_:
        if a_shares:
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
