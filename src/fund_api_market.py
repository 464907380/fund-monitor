"""
市场类 API 处理（大盘指数/分时/日K）—— 从 fund_server.Handler 拆分
纯数据获取 + 解析 + 返回，不依赖 Handler 私有状态，便于单独测试与复用。
"""
import json
import time
import datetime

from config import get_timeout
from fund_utils import fetch, fetch_bytes, is_trading_day, log


def api_market_indices() -> dict:
    """大盘指数实时行情（新浪）"""
    try:
        today = datetime.date.today()
        now = datetime.datetime.now()
        is_trading = (is_trading_day(today)
                      and 9 <= now.hour < 15)
        url = "http://hq.sinajs.cn/list=sh000001,sz399001,sz399006"
        raw = fetch_bytes(url, {"Referer": "https://finance.sina.com.cn/"})
        indices = []
        if raw:
            text = raw.decode("gbk", errors="ignore")
            for line in text.strip().split("\n"):
                if "hq_str_" not in line:
                    continue
                parts = line.split('"')
                if len(parts) < 2:
                    continue
                fields = parts[1].split(",")
                if len(fields) < 30:
                    continue
                name = fields[0]
                prev_close = float(fields[2]) if fields[2] else 0
                price = float(fields[3]) if fields[3] else 0
                chg_pts = price - prev_close
                chg_pct = (chg_pts / prev_close * 100) if prev_close else 0
                indices.append({
                    "name": name, "price": price,
                    "change_points": round(chg_pts, 2),
                    "change_pct": round(chg_pct, 2),
                })
        return {"ok": True, "indices": indices, "is_trading": is_trading}
    except Exception as e:
        log.error("/api/market-indices 异常", exc_info=True)
        return {"ok": False, "error": str(e)}


def _trading_offset(day_str: str) -> int:
    """计算交易偏移量（分钟），09:30→0, 11:30→120, 13:00→120, 15:00→240"""
    try:
        dt = datetime.datetime.strptime(day_str, "%Y-%m-%d %H:%M:%S")
        mins = dt.hour * 60 + dt.minute
        if mins < 570:  # 09:30 之前
            return 0
        if mins <= 690:  # 09:30-11:30
            return mins - 570
        if mins < 780:  # 11:30-13:00 午休
            return 120
        return 120 + (mins - 780)  # 13:00-15:00
    except Exception:
        return 0


def _fetch_pre_close(sym: str, ref_day: str) -> float | None:
    """从日K线获取 ref_day 前一交易日的收盘价（昨收）"""
    try:
        daily_url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                     f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen=10")
        raw_daily = fetch(daily_url)
        daily_data = json.loads(raw_daily)
        if daily_data:
            for p in reversed(daily_data):
                if p.get("day", "")[:10] < ref_day and p.get("close"):
                    return float(p["close"])
    except Exception:
        pass
    return None


def api_market_trends() -> dict:
    """大盘指数当日5分钟K线数据（用于画分时折线图）"""
    try:
        now = datetime.datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        symbols = [
            ("sh000001", "上证指数"),
            ("sz399001", "深证成指"),
            ("sz399006", "创业板指"),
        ]
        result = []
        for sym, name in symbols:
            url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   f"CN_MarketData.getKLineData?symbol={sym}&scale=5&ma=no&datalen=48")
            raw = fetch(url)
            points = json.loads(raw)
            today_points = [p for p in points if p.get("day", "").startswith(today_str)]
            if today_points:
                display_day = today_str
            elif points:
                display_day = points[-1].get("day", "")[:10]
            else:
                display_day = today_str
            pre_close = _fetch_pre_close(sym, display_day)
            if pre_close is None:
                for p in reversed(points):
                    if p.get("day", "")[:10] < display_day and p.get("close"):
                        pre_close = float(p["close"])
                        break
            if not today_points and points:
                today_points = [p for p in points if p.get("day", "").startswith(display_day)]
            pt_list = []
            for p in today_points:
                day_str = p.get("day", "")
                close = float(p.get("close", 0))
                off = _trading_offset(day_str)
                pt_list.append({"t": day_str, "close": close, "offset": off})
            closes = [pt["close"] for pt in pt_list]
            result.append({
                "name": name,
                "symbol": sym,
                "closes": closes,
                "points": pt_list,
                "pre_close": pre_close,
            })
        return {"ok": True, "trends": result}
    except Exception as e:
        log.error("/api/market-trends 异常", exc_info=True)
        return {"ok": False, "error": str(e)}


def api_market_kline() -> dict:
    """大盘指数30日K线（含成交量，用于画日K蜡烛图）。
    新浪日K接口(scale=240)当日/盘中不含未收盘的今日日K → 用今日5分钟K合成一根
    "今日日K"(O/H/L/C/成交量聚合)追加，使图上出现今天。非交易日无今日5分钟K则不加。"""
    try:
        symbols = [
            ("sh000001", "上证指数"),
            ("sz399001", "深证成指"),
            ("sz399006", "创业板指"),
        ]
        result = []
        for sym, name in symbols:
            url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen=30")
            raw = fetch(url)
            data = json.loads(raw)
            klines = []
            for p in data:
                klines.append({
                    "d": p.get("day", "")[:10],
                    "o": float(p.get("open", 0)),
                    "h": float(p.get("high", 0)),
                    "l": float(p.get("low", 0)),
                    "c": float(p.get("close", 0)),
                    "v": float(p.get("volume", 0)),
                })
            # 今日日K缺失 → 用今日5分钟K合成今日日K追加
            _today = datetime.date.today().isoformat()
            if not klines or klines[-1]["d"] != _today:
                try:
                    url5 = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                            f"CN_MarketData.getKLineData?symbol={sym}&scale=5&ma=no&datalen=120")
                    raw5 = fetch(url5)
                    d5 = json.loads(raw5)
                    _bars = [p for p in d5 if p.get("day", "").startswith(_today) and p.get("open")]
                    if _bars:
                        klines.append({
                            "d": _today,
                            "o": float(_bars[0]["open"]),
                            "h": max(float(p["high"]) for p in _bars),
                            "l": min(float(p["low"]) for p in _bars),
                            "c": float(_bars[-1]["close"]),
                            "v": sum(float(p.get("volume", 0) or 0) for p in _bars),
                        })
                except Exception:
                    pass  # 5分钟K失败则维持原状(无今日)
            result.append({"name": name, "symbol": sym, "klines": klines})
        return {"ok": True, "klines": result}
    except Exception as e:
        log.error("/api/market-kline 异常", exc_info=True)
        return {"ok": False, "error": str(e)}


# 路由表：path -> 处理函数（返回 dict，由调用方统一转 JSON 响应）
MARKET_GET_ROUTES: dict[str, object] = {
    "/api/market-indices": api_market_indices,
    "/api/market-trends": api_market_trends,
    "/api/market-kline": api_market_kline,
}
