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
from fund_utils import send_wechat, log, fetch_bytes, send_mail_html, parse_sina_csv, write_heartbeat, clear_heartbeat
from config import get_secret as _get_secret, api_url

# ── 成交额历史（用于动态百分位阈值） ──────────
_VOLUME_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".volume_history.json")
_VOLUME_BREADTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".breadth_history.json")
_VOLUME_HISTORY_DAYS = 60  # 取近60个交易日做百分位计算


# 新浪全球指数代码映射（不同市场的代码格式不同）
_GLOBAL_CODE_MAP = {
    "gb_$dji":    ["gb_$dji"],
    "gb_$ixic":   ["gb_$ixic"],
    "gb_$inx":    ["gb_$inx"],
    "rt_hkHSI":   ["rt_hkHSI"],
    "int_nikkei": ["int_nikkei"],
    "gb_$ks11":   ["gb_$ks11", "int_kospi"],
    "int_ftse":   ["int_ftse"],
    "gb_$gdaxi":  ["gb_$gdaxi", "int_dax"],
    "gb_$fchi":   ["gb_$fchi", "int_cac"],
    "gb_$ssmi":   ["gb_$ssmi", "int_smi"],
}

_GLOBAL_DISPLAY_NAMES = {
    "gb_$dji":    "道琼斯",
    "gb_$ixic":   "纳斯达克",
    "gb_$inx":    "标普500",
    "rt_hkHSI":   "恒生指数",
    "int_nikkei": "日经225",
    "gb_$ks11":   "韩国KOSPI",
    "int_ftse":   "英国富时100",
    "gb_$gdaxi":  "德国DAX",
    "gb_$fchi":   "法国CAC40",
    "gb_$ssmi":   "瑞士SMI",
}
A_INDICES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
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

_GLOBAL_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".global_cache.json")


# WECHAT_WEBHOOK 在 main() 中惰性读取，支持环境变量刷新


def fetch_sina(code: str) -> dict | None:
    """从新浪财经获取A股指数"""
    url = api_url("sina_hq", code=code)
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
    mapping = {"sh000001": "1.000001", "sz399001": "0.399001", "sz399006": "0.399006", "sz399300": "1.000300"}
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


def _parse_global_data(text: str, raw_code: str) -> dict | None:
    """解析新浪全球指数数据（支持多种格式），返回 {current, change, date}"""
    try:
        # gb_$ 格式: "名称,最新价,涨跌幅%,日期时间,涨跌额,..."
        if raw_code.startswith("gb_"):
            parts = text.split(",")
            if len(parts) >= 4 and parts[1]:
                current = float(parts[1])
                change_pct = float(parts[2]) if parts[2] else 0
                date_raw = parts[3].strip() if len(parts) > 3 else ""
                date = date_raw[:10] if date_raw else ""
                if current:
                    return {"current": current, "change": round(change_pct, 2), "date": date}
            return None

        # int_ 格式: "名称,最新价,涨跌额,涨跌幅%" — 无日期字段
        if raw_code.startswith("int_"):
            parts = text.split(",")
            if len(parts) >= 4 and parts[1]:
                current = float(parts[1])
                change_pct = float(parts[3])
                if current:
                    return {"current": current, "change": round(change_pct, 2), "date": ""}
            return None

        # rt_ 格式: "代码,名称,最新价,开盘,最高,最低,昨收,涨跌额,涨跌幅%,...,日期,..."
        if raw_code.startswith("rt_"):
            parts = text.split(",")
            if len(parts) >= 18 and parts[2]:
                current = float(parts[2])
                change_pct = float(parts[8]) if parts[8] else 0
                date_raw = parts[17].strip() if len(parts) > 17 and parts[17] else ""
                date = date_raw[:10].replace("/", "-") if date_raw else ""
                if current:
                    return {"current": current, "change": round(change_pct, 2), "date": date}
            return None

        return None
    except (ValueError, IndexError):
        return None


def _load_global_cache() -> dict:
    """加载全球指数缓存"""
    if not os.path.exists(_GLOBAL_CACHE_FILE):
        return {}
    try:
        with open(_GLOBAL_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return {}


def _save_global_cache(cache: dict) -> None:
    """保存全球指数缓存"""
    try:
        with open(_GLOBAL_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except OSError:
        pass


def fetch_all_global() -> list[dict]:
    """获取全球主要指数（多数据源，无实时数据的走缓存）"""
    today_str = datetime.date.today().isoformat()
    cache = _load_global_cache()

    # 收集所有需要查询的代码（去重）
    all_codes = set()
    for codes in _GLOBAL_CODE_MAP.values():
        all_codes.update(codes)
    all_codes.discard("")  # 移除空字符串

    # 批量请求
    url = f"https://hq.sinajs.cn/list={','.join(all_codes)}"
    data = fetch_bytes(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn",
    })

    # 解析返回数据 keyed by code
    live: dict[str, dict] = {}
    if data:
        text = data.decode("gbk")
        for line in text.strip().split("\n"):
            m = re.search(r'var hq_str_([^=]+)="(.*?)"', line)
            if m:
                code = m.group(1)
                val = m.group(2).strip()
                if val:
                    parsed = _parse_global_data(val, code)
                    if parsed:
                        live[code] = parsed

    # 按显示顺序组装结果
    results = []
    for key, display_name in _GLOBAL_DISPLAY_NAMES.items():
        codes = _GLOBAL_CODE_MAP.get(key, [])
        found = None
        used_code = None
        for c in codes:
            if c in live:
                found = live[c]
                used_code = c
                break

        if found:
            # 用API返回的日期（无日期时用 today_str）
            data_date = found.get("date", "")
            if not data_date:
                data_date = today_str
            date_label = data_date[-5:]
            results.append({
                "code": f"{display_name}（{date_label}）",
                "current": found["current"],
                "change": found["change"],
            })
            # 更新缓存
            raw_key = key.replace("gb_$", "").replace("rt_", "").replace("int_", "")
            cache[raw_key] = {"current": found["current"], "change": found["change"], "date": data_date}
        elif cache:
            # 无实时数据，走缓存
            raw_key = key.replace("gb_$", "").replace("rt_", "").replace("int_", "")
            cached = cache.get(raw_key)
            if cached:
                date_note = cached.get("date", "")
                suffix = f"（{date_note[-5:]}）" if date_note else ""
                results.append({
                    "code": f"{display_name}{suffix}",
                    "current": cached["current"],
                    "change": cached["change"],
                })

    if results:
        _save_global_cache(cache)
    return results


def get_global() -> list[dict]:
    """获取全球主要指数"""
    return fetch_all_global()


def build_briefing_md(a_shares: list[dict] | None = None,
                      globals_: list[dict] | None = None,
                      senti: dict | None = None,
                      breadth: dict | None = None) -> str:
    """构造简报 Markdown（企业微信推送用）"""
    today = datetime.date.today().isoformat()
    if a_shares is None:
        a_shares = get_a_share()
    if globals_ is None:
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
    if senti is None:
        senti = _fetch_sentiment()
    if breadth is None:
        breadth = _fetch_market_breadth()
    if senti or breadth:
        if a_shares:
            lines.append("")
        lines.append("**📊 市场情绪**")

        if senti:
            recent = sorted(senti["recent"].items(), reverse=True)
            lines.append("**成交额**")
            lines.append("|日期|成交额|较前一日|")
            lines.append("|---|---|---:|")
            for i, (d, v) in enumerate(recent):
                if i == len(recent) - 1:
                    lines.append(f"|{d[-5:]}|{v:.0f}亿|—|")
                else:
                    next_v = recent[i+1][1]
                    diff = v - next_v
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


def build_briefing_text(a_shares: list[dict] | None = None,
                       globals_: list[dict] | None = None,
                       senti: dict | None = None,
                       breadth: dict | None = None) -> str:
    """构造简报文本版（终端/邮件用，对齐格式参考晚报）"""
    today = datetime.date.today().isoformat()
    if a_shares is None:
        a_shares = get_a_share()
    if globals_ is None:
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
    if senti is None:
        senti = _fetch_sentiment()
    if breadth is None:
        breadth = _fetch_market_breadth()
    if senti or breadth:
        lines.append("")
        lines.append("📊 市场情绪")

        if senti:
            lines.append("成交额")
            lines.append(f"{'日期':<12} {'成交额':<12} {'较前一日':<12}")
            lines.append("-" * 36)
            sorted_recent = sorted(senti["recent"].items(), reverse=True)
            for i, (d, v) in enumerate(sorted_recent):
                if i == len(sorted_recent) - 1:
                    lines.append(f"{d[-5:]:<12} {v:.0f}亿{'':<9} —")
                else:
                    next_v = sorted_recent[i+1][1]
                    diff = v - next_v
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
        url = api_url("sina_volume")
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
        url = api_url("tencent_kline")
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



def _html_a_share_section(a_shares: list[dict] | None) -> list[str]:
    """渲染 A 股指数 HTML 行"""
    rows: list[str] = []
    if not a_shares:
        return rows
    rows.append('<tr style="background:#2a2a2a;"><td style="padding:8px 12px;font-size:13px;font-weight:600;color:#ccc;" colspan="3">\U0001f1e8\U0001f1f3 A股</td></tr>')
    rows.append('<tr style="background:#222;"><td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;border-bottom:1px solid #333;">指数</td>'
                '<td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;text-align:right;border-bottom:1px solid #333;">最新</td>'
                '<td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;text-align:right;border-bottom:1px solid #333;">涨跌幅</td></tr>')
    for s in a_shares:
        c_val = s["change"]
        color = "#ef5350" if c_val > 0 else ("#66bb6a" if c_val < 0 else "#ccc")
        arrow = "\U0001f534" if c_val > 0 else ("\U0001f7e2" if c_val < 0 else "\u26aa")
        rows.append(f'<tr><td style="padding:6px 12px;border-bottom:1px solid #333;color:#ccc;">{s["code"]}</td>'
                    f'<td style="padding:6px 12px;text-align:right;font-family:Consolas;border-bottom:1px solid #333;color:#ccc;">{s["current"]:.2f}</td>'
                    f'<td style="padding:6px 12px;text-align:right;font-weight:600;font-family:Consolas;border-bottom:1px solid #333;color:{color};">{arrow}{c_val:+.2f}%</td></tr>')
    return rows


def _html_volume_section(senti: dict | None) -> list[str]:
    """渲染成交额 HTML 行"""
    rows: list[str] = []
    if not senti:
        return rows
    rows.append('<tr><td style="padding:10px 12px 4px;" colspan="3"><p style="margin:0;font-size:13px;font-weight:600;color:#ccc;">\U0001f4ca 成交额</p></td></tr>')
    rows.append('<tr style="background:#222;"><td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;border-bottom:1px solid #333;">日期</td>'
                '<td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;text-align:right;border-bottom:1px solid #333;">成交额</td>'
                '<td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;text-align:right;border-bottom:1px solid #333;">较前一日</td></tr>')
    sorted_recent = sorted(senti["recent"].items(), reverse=True)
    for i, (d, v) in enumerate(sorted_recent):
        if i == len(sorted_recent) - 1:
            diff_str = "\u2014"
        else:
            next_v = sorted_recent[i+1][1]
            diff = v - next_v
            diff_str = f'<span style="color:#ef5350;">\u2191{abs(diff):.0f}亿</span>' if diff >= 0 else f'<span style="color:#66bb6a;">\u2193{abs(diff):.0f}亿</span>'
        rows.append(f'<tr><td style="padding:4px 12px;border-bottom:1px solid #333;color:#ccc;">{d[-5:]}</td>'
                    f'<td style="padding:4px 12px;text-align:right;font-family:Consolas;border-bottom:1px solid #333;color:#ccc;">{v:.0f}亿</td>'
                    f'<td style="padding:4px 12px;text-align:right;font-family:Consolas;border-bottom:1px solid #333;">{diff_str}</td></tr>')
    if senti.get("rank_str"):
        rows.append(f'<tr><td style="padding:6px 12px;font-size:11px;color:#888;" colspan="3">\U0001f4cc {senti["rank_str"]}</td></tr>')
    return rows


def _html_breadth_section(breadth: dict | None) -> list[str]:
    """渲染涨跌家数 HTML 行"""
    if not breadth:
        return []
    up, down = breadth["up"], breadth["down"]
    return [f'<tr><td style="padding:8px 12px;font-size:12px;color:#ccc;" colspan="3">\U0001f4c8涨{up}家  \U0001f4c9跌{down}家</td></tr>']


def _html_global_section(globals_: list[dict] | None) -> list[str]:
    """渲染全球指数 HTML 行"""
    rows: list[str] = []
    if not globals_:
        return rows
    rows.append('<tr style="background:#2a2a2a;"><td style="padding:8px 12px;font-size:13px;font-weight:600;color:#ccc;" colspan="3">\U0001f30d 全球</td></tr>')
    rows.append('<tr style="background:#222;"><td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;border-bottom:1px solid #333;">指数</td>'
                '<td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;text-align:right;border-bottom:1px solid #333;">最新</td>'
                '<td style="padding:6px 12px;font-size:11px;color:#888;font-weight:600;text-align:right;border-bottom:1px solid #333;">涨跌幅</td></tr>')
    for s in globals_:
        c_val = s["change"]
        color = "#ef5350" if c_val > 0 else ("#66bb6a" if c_val < 0 else "#ccc")
        arrow = "\U0001f534" if c_val > 0 else ("\U0001f7e2" if c_val < 0 else "\u26aa")
        rows.append(f'<tr><td style="padding:6px 12px;border-bottom:1px solid #333;color:#ccc;">{s["code"]}</td>'
                    f'<td style="padding:6px 12px;text-align:right;font-family:Consolas;border-bottom:1px solid #333;color:#ccc;">{s["current"]:.2f}</td>'
                    f'<td style="padding:6px 12px;text-align:right;font-weight:600;font-family:Consolas;border-bottom:1px solid #333;color:{color};">{arrow}{c_val:+.2f}%</td></tr>')
    return rows




def _fetch_market_breadth() -> dict | None:
    """获取涨跌家数（沪深两市合计），收盘后展示上次缓存值"""
    # 先试新浪fields 28/29（交易时段有效）
    try:
        url = api_url("sina_hq", code="sh000001")
        data = fetch_bytes(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        if data:
            text = data.decode("gbk")
            m = re.search(r'"(.*?)"', text)
            if m:
                parts = m.group(1).split(",")
                up = int(float(parts[28])) if len(parts) > 28 and parts[28] else 0
                down = int(float(parts[29])) if len(parts) > 29 and parts[29] else 0
                if up > 0 or down > 0:
                    result = {"up": up, "down": down}
                    _save_breadth_history(result)
                    return result
    except Exception:
        pass

    # 收盘后清零了 → 用缓存
    cached = _load_breadth_history()
    if cached.get("up") and cached.get("down"):
        return cached

    # 缓存也没有 → 遍历新浪hs_a全市场（仅首次）
    try:
        import json as _json, urllib.request as _ur
        total_up, total_down = 0, 0
        for _pg in range(1, 101):
            url = f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={_pg}&num=100&sort=changePercent&asc=0&node=hs_a"
            req = _ur.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.sina.com.cn"})
            resp = _ur.urlopen(req, timeout=10).read().decode("gbk")
            items = _json.loads(resp)
            if not items:
                break
            for i in items:
                chg = float(i.get("changepercent", 0))
                if chg > 0: total_up += 1
                elif chg < 0: total_down += 1
            if len(items) < 100:
                break
        if total_up or total_down:
            result = {"up": total_up, "down": total_down}
            _save_breadth_history(result)
            log.info("涨跌家数: 涨%d家 跌%d家（从新浪全市场遍历）", total_up, total_down)
            return result
    except Exception as e:
        log.debug("涨跌家数遍历失败: %s", e)

    return None


def build_briefing_html(a_shares: list[dict] | None = None,
                       globals_: list[dict] | None = None,
                       senti: dict | None = None,
                       breadth: dict | None = None) -> str:
    """构造简报 HTML（邮件推送用，深色主题同晚报）"""
    today = datetime.date.today().isoformat()
    if a_shares is None:
        a_shares = get_a_share()
    if globals_ is None:
        globals_ = get_global()
    if senti is None:
        senti = _fetch_sentiment()
    if breadth is None:
        breadth = _fetch_market_breadth()

    rows: list[str] = []
    rows.extend(_html_a_share_section(a_shares))
    rows.extend(_html_volume_section(senti))
    rows.extend(_html_breadth_section(breadth))
    rows.extend(_html_global_section(globals_))

    if not a_shares and not globals_:
        rows.append('<tr><td style="padding:20px 12px;text-align:center;color:#888;" colspan="3">\u274c \u6240\u6709\u6570\u636e\u6e90\u5747\u4e0d\u53ef\u7528</td></tr>')

    if globals_ or a_shares:
        rows.append('<tr><td style="padding:8px 12px;font-size:11px;color:#555;text-align:center;" colspan="3">\u23f0 \u7f8e\u80a1/\u6b27\u80a1\u4e3a\u4e0a\u4e00\u4e2a\u4ea4\u6613\u65e5\u6536\u76d8</td></tr>')

    body_rows = "\\n".join(rows)

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
<h1 style="margin:0;font-size:20px;color:#e0e0e0;">\U0001f30f \u5168\u7403\u80a1\u5e02\u7b80\u62a5</h1>
<p style="margin:4px 0 0;font-size:12px;color:#666;">{today}</p>
</td></tr>

<tr><td style="padding:0 10px 10px;">
<table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-size:13px;">
{body_rows}
</table>
</td></tr>

<tr><td style="text-align:center;padding:16px 10px;font-size:11px;color:#555;border-top:1px solid #333;">Fund Monitor \u00b7 \u5929\u5929\u57fa\u91d1</td></tr>
</table>
</td></tr></table>
</body>
</html>"""

def main() -> None:
    write_heartbeat("global_briefing")
    try:
        log.info("====== 全球股市简报 开始 ======")
        a_shares = get_a_share()
        globals_ = get_global()
        senti = _fetch_sentiment()
        breadth = _fetch_market_breadth()
        brief_md = build_briefing_md(a_shares, globals_, senti, breadth)
        brief_html = build_briefing_html(a_shares, globals_, senti, breadth)

        webhook = _get_secret("WECHAT_WEBHOOK")
        if webhook:
            send_wechat(brief_md)
        # 邮件也发（可同时走双通道）
        try:
            send_mail_html("🌏 全球股市简报", brief_html)
        except Exception as _e:
            log.warning("简报邮件发送失败: %s", _e)

        log.info("====== 全球股市简报 完成 ======")
    finally:
        clear_heartbeat("global_briefing")


if __name__ == "__main__":
    main()
