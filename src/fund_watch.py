"""
基金数据获取与评分 — 共享工具模块
"""
import json
import os
import re
import time
import threading
import datetime
from config import CFG, api_url, get_timeout
from config import get_secret as _get_secret
from fund_utils import fetch, fetch_bytes, log, HISTORY_DIR, _fetch_fund_estimate, record_estimate
from fund_scoring import SCORE_DIMS, calc_score_detail
from fund_metrics import _calc_nav_metrics

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
    _fund_list_path = os.path.join(HISTORY_DIR, "data", "fund_list.json")
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


def _parse_real_time(code: str) -> tuple[float | None, str]:
    """获取实时估算涨跌幅，返回 (涨跌幅, 数据来源)
    来源: lsjz=实际净值, holdings=持仓估算
    交易日: 优先今日实际净值，无今日净值时用持仓估算
    非交易日: 只用实际净值（最近一次净值），不做持仓估算
    收盘后(≥15:00) lsjz 实际净值是当天固定值 → 命中当天缓存直接返回，避免每只基金实时请求。
    """
    import urllib.request, re as _re, datetime
    from fund_utils import is_trading_day, _get_td_lsjz_cache, _set_td_lsjz_cache

    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    is_trading = is_trading_day(now.date())
    after_market = now.hour >= 15

    # 收盘后：实际净值固定值，命中当天缓存直接返回（避免重启后首屏每只实时请求 LSJZ）
    if after_market:
        _cached_td = _get_td_lsjz_cache(code)
        if _cached_td is not None:
            return (_cached_td, "lsjz")

    # 查 LSJZ 实际净值（今日有净值则用今日；非交易日返回最近净值）
    try:
        url = f"https://api.fund.eastmoney.com/f10/lsjz?callback=j&fundCode={code}&pageIndex=1&pageSize=1"
        req = urllib.request.Request(url, headers={"Referer": "https://fund.eastmoney.com/", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            gz_data = r.read().decode("utf-8")
        m_date = _re.search(r'FSRQ":"(\d{4}-\d{2}-\d{2})"', gz_data)
        m_val = _re.search(r'"JZZZL":"([-+\d.]+)"', gz_data)
        if m_date and m_val:
            # 今日有实际净值（收盘后）→ 净值，并缓存当天固定值
            if m_date.group(1) == today_str:
                _v = float(m_val.group(1))
                if after_market:
                    _set_td_lsjz_cache(code, _v)
                return (_v, "lsjz")
            # 非交易日 → 返回最近净值，不做估算
            if not is_trading:
                return (float(m_val.group(1)), "lsjz")
    except Exception:
        pass
    # 交易日且无今日净值 → 持仓估算（盘中实时）
    if is_trading:
        try:
            est = _estimate_from_holdings(code)
            if est is not None:
                record_estimate(code, est)  # 记录盘中估算，供收盘后对比实际净值
                return (est, "holdings")
        except Exception:
            pass
    return (None, "")


# ── 个股行情短期缓存（跨基金合并复用，减少新浪请求与域名限速排队）──
_stock_quote_cache: dict[str, tuple[float, tuple[str, float]]] = {}  # sina_code -> (ts, (name, chg))
_STOCK_QUOTE_TTL = 60  # 秒


def _fetch_stock_quotes_batch(sina_codes: list[str]) -> dict[str, tuple[str, float]]:
    """批量获取个股行情（新浪一次请求多代码，40个/块），返回 {sina_code: (name, chg)}。
    带 60s 按代码缓存：自选表多基金共享重仓股时只拉一次，且受域名限速影响小。"""
    result: dict[str, tuple[str, float]] = {}
    if not sina_codes:
        return result
    _now = time.time()
    unique = list(dict.fromkeys(sina_codes))
    # 缓存命中部分直接复用；只拉未命中的
    _miss: list[str] = []
    for _c in unique:
        _e = _stock_quote_cache.get(_c)
        if _e and _now - _e[0] < _STOCK_QUOTE_TTL:
            result[_c] = _e[1]
        else:
            _miss.append(_c)
    if not _miss:
        return result
    # 并发分块拉取（每块40只，urllib直连绕过0.3s域名限速锁；批量一次性可接受）
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _AC
    _chunks = [_miss[i:i + 40] for i in range(0, len(_miss), 40)]

    def _fetch_chunk(_chunk: list[str]) -> dict[str, tuple[str, float]]:
        _out: dict[str, tuple[str, float]] = {}
        _url = api_url("sina_hq_batch", codes=",".join(_chunk))
        try:
            _req = urllib.request.Request(_url, headers={
                "Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(_req, timeout=8) as _r:
                _text = _r.read().decode("gbk", errors="ignore")
            for _line in _text.strip().split("\n"):
                _m = re.search(r'hq_str_(\w+)="(.*?)"', _line)
                if not _m:
                    continue
                _c = _m.group(1)
                _fields = _m.group(2).split(",")
                if len(_fields) < 4 or not _fields[2]:
                    continue
                _pc = float(_fields[2])
                _cur = float(_fields[3]) if _fields[3] else 0
                if _pc:
                    _out[_c] = (_fields[0], round((_cur - _pc) / _pc * 100, 2))
        except Exception:
            pass
        return _out

    if len(_chunks) > 1:
        with _TPE(max_workers=min(20, len(_chunks))) as _ex:
            _futs = {_ex.submit(_fetch_chunk, _ch): _i for _i, _ch in enumerate(_chunks)}
            for _f in _AC(_futs):
                for _c, _item in _f.result().items():
                    _stock_quote_cache[_c] = (_now, _item)
                    result[_c] = _item
    else:
        for _c, _item in _fetch_chunk(_chunks[0]).items():
            _stock_quote_cache[_c] = (_now, _item)
            result[_c] = _item
    return result


def _estimate_from_holdings(code: str) -> float | None:
    """根据持仓股票实时行情估算基金涨跌幅（批量获取行情，避免逐只串行请求）"""
    try:
        holds = _parse_holdings(code)
        if not holds:
            return None
        # 收集所有持仓股票的 sina 代码，批量一次获取行情
        sina_codes = []
        for h in holds:
            sc = h.get("c", "")
            if not sc:
                continue
            prefix = "sh" if h.get("m") == "sh" else "sz"
            sina_codes.append(f"{prefix}{sc}")
        quotes = _fetch_stock_quotes_batch(sina_codes)
        total_w = 0.0
        weighted_chg = 0.0
        for h in holds:
            if not h.get("c") or not h.get("p"):
                continue
            sc = h["c"]
            prefix = "sh" if h.get("m") == "sh" else "sz"
            item = quotes.get(f"{prefix}{sc}")
            if not item:
                continue
            chg_pct = item[1]
            total_w += h["p"]
            weighted_chg += chg_pct * h["p"]
        if total_w >= 5:
            return round(weighted_chg / total_w, 2)
    except Exception as e:
        log.debug("持仓估算失败 %s: %s", code, e)
    return None


def _parse_holdings_meta(code: str) -> dict:
    """获取持仓数据的报告期信息（报告期+截止日期）"""
    import html as _html
    url = api_url("fund_holdings", code=code)
    try:
        jj = fetch(url, headers={"Referer": "https://fundf10.eastmoney.com/"})
        # 提取截止日期 如 2026-03-31
        dm = re.search(r'截止至：<font[^>]*>(\d{4}-\d{2}-\d{2})</font>', jj)
        # 提取报告期 如 2026年1季度
        qm = re.search(r'(\d{4}年(?:1季|2季|3季|4季|半年|年报)[度]?)', jj)
        return {
            "date": dm.group(1) if dm else "",
            "quarter": qm.group(1) if qm else ""
        }
    except Exception:
        return {"date": "", "quarter": ""}


# ── 持仓缓存（季报披露，30天TTL避免推荐/监控/自选重复拉取）──
_HOLDINGS_CACHE_PATH = os.path.join(HISTORY_DIR, "data", "fund_holdings_cache.json")
_HOLDINGS_TTL = 30 * 24 * 3600  # 30天（季报约90天更新一次）
_HOLDINGS_LOCK = threading.Lock()
_HOLDINGS_MEM: dict | None = None
_HOLDINGS_MTIME: float = -1.0
_HOLDINGS_PROC: dict[str, tuple[float, list | None]] = {}  # code -> (ts, holds) 进程内


def _load_holdings_cache() -> dict:
    """读取持仓磁盘缓存 {code: {ts, holds}}（进程内缓存+mtime检测）"""
    global _HOLDINGS_MEM, _HOLDINGS_MTIME
    try:
        if os.path.exists(_HOLDINGS_CACHE_PATH):
            _mtime = os.path.getmtime(_HOLDINGS_CACHE_PATH)
            if _HOLDINGS_MEM is not None and _mtime == _HOLDINGS_MTIME:
                return _HOLDINGS_MEM
            with open(_HOLDINGS_CACHE_PATH, encoding="utf-8") as f:
                _HOLDINGS_MEM = json.load(f)
            _HOLDINGS_MTIME = _mtime
            return _HOLDINGS_MEM
    except Exception:
        pass
    return {}


def _save_holdings_cache(cache: dict) -> None:
    """原子写入持仓缓存并同步进程内缓存"""
    global _HOLDINGS_MEM, _HOLDINGS_MTIME
    try:
        os.makedirs(os.path.dirname(_HOLDINGS_CACHE_PATH), exist_ok=True)
        _tmp = _HOLDINGS_CACHE_PATH + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(_tmp, _HOLDINGS_CACHE_PATH)
        _HOLDINGS_MEM = cache
        _HOLDINGS_MTIME = os.path.getmtime(_HOLDINGS_CACHE_PATH)
    except Exception:
        pass


def _parse_holdings(code: str) -> list[dict] | None:
    """获取前10大持仓明细（含股票名称/代码/占比）。
    持仓按季度披露（季报/半年报/年报），TTL缓存(30天)避免每次重复拉取。
    失败不缓存（避免把失败状态缓存30天导致后续一直拿不到）。"""
    import html as _html
    _now = time.time()
    # 1. 进程内缓存
    if code in _HOLDINGS_PROC and _now - _HOLDINGS_PROC[code][0] < _HOLDINGS_TTL:
        return _HOLDINGS_PROC[code][1]
    # 2. 磁盘缓存
    try:
        _entry = _load_holdings_cache().get(code)
        if _entry and _entry.get("holds") is not None and _now - _entry.get("ts", 0) < _HOLDINGS_TTL:
            _HOLDINGS_PROC[code] = (_now, _entry["holds"])
            return _entry["holds"]
    except Exception:
        pass
    # 3. 网络拉取
    url = api_url("fund_holdings", code=code)
    try:
        jj = fetch(url, headers={"Referer": "https://fundf10.eastmoney.com/"})
        cm = re.search(r'content:"(.+?)"', jj, re.DOTALL)
        if not cm:
            return None
        content = cm.group(1)
        content = content.replace('\\n', '\n').replace('\\"', '"').replace('\\/', '/')
        content = _html.unescape(content)
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', content, re.DOTALL)
        holds = []
        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            if len(cells) < 7:
                continue
            clean = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
            try:
                idx = int(clean[0])
            except (ValueError, IndexError):
                continue
            code_s = clean[1] if len(clean) > 1 else ""
            name = clean[2] if len(clean) > 2 else ""
            pct_str = clean[6] if len(clean) > 6 else "0"
            try:
                pct = float(pct_str.replace("%", ""))
            except ValueError:
                pct = 0
            # 从链接中推断市场：116→港股, 0→深市, 1→沪市
            href_match = re.search(r'href=["\'](?:[^"\']*[/.])(\d+)\.(\d+)["\']', cells[1])
            market = "sz"  # 默认深市
            if href_match:
                prefix = href_match.group(1)
                if prefix == "116":
                    market = "hk"
                elif prefix == "1":
                    market = "sh"
            holds.append({"n": name, "c": code_s, "p": pct, "m": market})
        # 只取前十大持仓：fund_holdings 接口返回两个表格（第2组列结构不同、
        # 无占比数据且与第1组重复），避免解析出占比为0的无效条目
        result = holds[:10] if holds else None
        # 4. 写缓存（仅成功时）
        if result is not None:
            _HOLDINGS_PROC[code] = (_now, result)
            try:
                with _HOLDINGS_LOCK:
                    _disk = _load_holdings_cache()
                    _disk[code] = {"ts": _now, "holds": result}
                    _save_holdings_cache(_disk)
            except Exception:
                pass
        return result
    except Exception as e:
        log.debug("拉取重仓股失败 %s: %s", code, e)
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
        d["nav"] = full_nav
        metrics = _calc_nav_metrics(full_nav)
        d.update(metrics)
        # 从净值数据计算近3年收益（自然日历口径，与排行API一致）
        d["sy3"] = _calc_period_return(full_nav, 728)  # ≈3年（自然日1095天≈728个交易日）
        d["sy2"] = _calc_period_return(full_nav, 484)  # ≈2年（自然日730天≈484个交易日）
    else:
        if nav := _parse_net_trend(data):
            d["nav"] = nav
    td, _ = _parse_real_time(code)
    if td is not None:
        d["td"] = td
    if holds := _parse_holdings(code):
        d["holds"] = holds
    if rp := _parse_rank_info(data):
        d["rank"], d["rank_total"] = rp
    if rate := _parse_fund_rate(data):
        d["rate"] = rate
    d["sy6"] = _parse_syl_6y(data)  # 近6月收益（暂未用于评分，保留供未来使用）

    return d


def _fetch_nav_from_lsjz(code: str, max_pages: int = 38) -> list[dict] | None:
    """从 LSJZ 历史净值 API 并行获取多页净值数据，兼容旧格式返回。

    返回 [{d: YYYY-MM-DD, v: nav_value}, ...] 按日期升序。
    注意: LSJZ API 每页固定返回 20 条（pageSize>20 无效），页数即 max_pages。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import urllib.request, re, json as _json

    _total_pages = max(1, max_pages)

    def _fetch_page(page: int) -> list[dict]:
        url = (f"https://api.fund.eastmoney.com/f10/lsjz"
               f"?callback=j&fundCode={code}&pageIndex={page}&pageSize=20")
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://fund.eastmoney.com/",
        })
        with urllib.request.urlopen(req, timeout=get_timeout("default", 10)) as r:
            text = r.read().decode("utf-8")
        m = re.search(r"j\((.+)\)", text)
        if not m:
            return []
        result = _json.loads(m.group(1))
        items = result.get("Data", {}).get("LSJZList", [])
        return [{"d": it["FSRQ"], "v": float(it["DWJZ"])} for it in items if it.get("DWJZ")]

    all_by_date: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(_fetch_page, p): p for p in range(1, _total_pages + 1)}
        for fut in as_completed(futs):
            try:
                page_entries = fut.result()
            except Exception:
                continue  # 单页网络失败跳过，不影响整体
            for entry in page_entries:
                if entry["d"] not in all_by_date:  # 去重，最新优先
                    all_by_date[entry["d"]] = entry["v"]

    if not all_by_date:
        return None
    # 按日期升序
    return [{"d": d, "v": all_by_date[d]} for d in sorted(all_by_date.keys())]


# 维度 → 所需最少净值天数映射（用于动态决定 LSJZ 拉取量）
_DIM_LOOKBACK: dict[str, int] = {
    "近1月收益": 22,
    "近一周收益": 5,
    "近3月收益": 66,
    "近6月收益": 125,
    "近1年收益": 250,
    "近2年收益": 500,
    "近3年收益": 750,
}


def _required_nav_pages() -> int:
    """根据当前启用的评分维度和筛选配置计算需要拉取的 LSJZ 页数。
    
    LSJZ 每页 20 条，额外加 5 页缓冲用于风险指标计算。
    最少 5 页（100 条），最多 38 页（760 条 ≈ 3 年）。
    """
    try:
        from fund_scoring import SCORE_DIMS
        max_days = 0
        for name, _, weight, _ in SCORE_DIMS:
            if weight > 0:
                days = _DIM_LOOKBACK.get(name, 0)
                if days > max_days:
                    max_days = days
        # 如果开启了"筛掉缺失收益数据"，需确保所有检查字段的数据足够
        try:
            from config import CFG
            if CFG.get("recommend", {}).get("skip_missing_perf", False):
                # skip_missing_perf 检查的字段所需最少天数
                _perf_lookback = {"m1": 22, "m3": 66, "y1": 243, "f5": 6,
                                  "sy6": 119, "sy2": 484, "sy3": 728,
                                  "annual_return": 250}
                for _need in _perf_lookback.values():
                    if _need > max_days:
                        max_days = _need
        except Exception:
            pass
    except Exception:
        max_days = 0
    # 至少有 100 条（5页）保证风险指标有意义
    pages = max(5, (max_days + 20 - 1) // 20 + 5)  # ceil + 5页缓冲
    return min(pages, 38)


def _fetch_fund_name_light(code: str) -> str:
    """获取基金名，优先全市场名称索引缓存（替代已失效的 fundgz），降级新浪/fundgz"""
    import urllib.request, re, json as _json
    # 1. 全市场名称索引缓存（懒加载，O(1)）
    try:
        from fund_utils import _get_fund_name
        nm = _get_fund_name(code)
        if nm:
            return nm
    except Exception:
        pass
    # 2. 新浪降级
    try:
        url = f"https://hq.sinajs.cn/list=of{code}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
        with urllib.request.urlopen(req, timeout=get_timeout("default", 10)) as r:
            text = r.read().decode("gbk")
        m = re.search(r'"(.*?)"', text)
        if m:
            parts = m.group(1).split(",")
            if parts[0]:
                return parts[0]
    except Exception:
        pass
    # 3. fundgz 兜底
    try:
        url = f"https://fundgz.1234567.com.cn/js/{code}.js"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=get_timeout("default", 10)) as r:
            text = r.read().decode("utf-8")
        m = re.search(r"jsonpgz\((.+)\)", text)
        if m:
            data = _json.loads(m.group(1))
            name = data.get("name", "")
            if name:
                return name
    except Exception:
        pass
    return ""


def get_scoring_data(code: str) -> dict:
    """拉取评分所需的最小数据集（LSJZ 历史净值替代 pingzhongdata）

    盘中评分数据不会变化，使用每日缓存避免重复拉取。
    """
    today = datetime.date.today().isoformat()
    if code in _scoring_cache and _scoring_cache[code][0] == today:
        return _scoring_cache[code][1]
    d: dict = {"code": code}

    # 1. 获取基金名（fundgz 轻量 API, 160B）
    name = _fetch_fund_name_light(code)
    if name:
        d["n"] = name

    # 2. 获取净值历史（LSJZ API, 根据启用维度动态决定页数）
    max_pages = _required_nav_pages()
    full_nav = None
    # 磁盘净值缓存三级策略（推荐评分 _score_one / 折线图已写入）：
    #   ① 当天缓存 → 直接命中（O(1) 本地）
    #   ② 跨天缓存 → 增量更新：只拉最新 1 页(20条)合并新增净值，免 38 页重拉
    #   ③ 无缓存/缓存不足 → 全量拉取
    try:
        from fund_utils import _load_fund_trend_cache
        _cache_all = _load_fund_trend_cache()
        _entry = _cache_all.get(code)
        if _entry and _entry.get("navs"):
            _old_navs = [{"d": _dd, "v": _vv} for _dd, _vv in _entry["navs"]]
            if _entry.get("date") == today:
                # ① 当天缓存直接命中
                if len(_old_navs) >= 250:
                    full_nav = _old_navs
            elif len(_old_navs) >= 250:
                # ② 跨天增量更新：只拉最新 1 页，合并比旧缓存更新的净值
                _old_last = _old_navs[-1]["d"]
                _new_page = _fetch_nav_from_lsjz(code, max_pages=1)
                if _new_page:
                    _added = [it for it in _new_page if it["d"] > _old_last]
                    if not _added:
                        # 无新增（今天净值未发布/非交易日）→ 旧缓存数据仍最新，直接复用
                        full_nav = _old_navs
                    elif len(_added) < 20:
                        # 正常增量合并：保持升序，滚动窗口取最近 760 条
                        full_nav = (_old_navs + _added)[-760:]
                    # len(_added)>=20 → 长假后新增超过 1 页，走下方全量回退
    except Exception:
        pass
    if full_nav is None:
        # 优先 pingzhongdata（1次请求拉全量，比 LSJZ 38页快~1.5倍且数据全），截断到800条控制计算量；失败回退 LSJZ 分页
        try:
            _pz_raw = fetch(api_url("fund_pingzhongdata", code=code))
            _pz = _parse_full_nav(_pz_raw)
            if _pz:
                full_nav = _pz[-800:]
                # 顺带补全规模/费率/经理/机构占比（评分维度用，LSJZ路径缺失）
                if _sc := _parse_scale(_pz_raw):
                    d["sc"] = _sc
                if _mgr := _parse_manager(_pz_raw):
                    d["mgr"] = _mgr
                if _inst := _parse_institutional_ratio(_pz_raw):
                    d["inst"] = _inst
                if _rate := _parse_fund_rate(_pz_raw):
                    d["rate"] = _rate
        except Exception:
            pass
        if full_nav is None:
            full_nav = _fetch_nav_from_lsjz(code, max_pages=max_pages)
    if full_nav:
        d["full_nav"] = full_nav
        d["nav"] = full_nav  # 完整净值数据
        # 计算风险指标
        metrics = _calc_nav_metrics(full_nav)
        d.update(metrics)
        # 多窗口版本（供维度按窗口评分：如 max_dd_1y / volatility_3y / max_dd_all）
        _window_dims = ["max_dd", "volatility", "max_loss_days", "sharpe",
                        "sortino", "calmar", "recovery", "win_rate",
                        "profit_ratio", "annual_return"]
        _windows = {"all": None, "1y": 250, "2y": 500, "3y": 750}
        for _lb, _days in _windows.items():
            _m = _calc_nav_metrics(full_nav, lookback=_days) if _days else metrics
            for _dk in _window_dims:
                d[f"{_dk}_{_lb}"] = _m.get(_dk)
        # 从净值数据计算各阶段收益（自然日历口径：近1月=22交易日、近3月=66、近6月=119、近1年=243、近2年=484、近3年=728，与排行API一致）
        d["m1"] = _calc_period_return(full_nav, 22)    # ≈1月
        d["m3"] = _calc_period_return(full_nav, 66)    # ≈3月
        d["y1"] = _calc_period_return(full_nav, 243)   # ≈1年（自然日365天≈243交易日）
        d["sy6"] = _calc_period_return(full_nav, 119)  # ≈6月（自然日182天≈119交易日）
        d["sy3"] = _calc_period_return(full_nav, 728)  # ≈3年（自然日1095天≈728交易日）
        d["sy2"] = _calc_period_return(full_nav, 484)  # ≈2年（自然日730天≈484交易日）
    else:
        d["nav"] = []

    _scoring_cache[code] = (today, d)
    # 限制缓存大小：推荐批量评分时内存会无限累积导致 OOM，最多保留最近 200 只
    if len(_scoring_cache) > 200:
        try:
            _scoring_cache.pop(next(iter(_scoring_cache)))
        except (StopIteration, KeyError):
            pass
    return d


# ── 评分数据每日缓存（盘中不变，避免重复拉取 pingzhongdata）──
_scoring_cache: dict[str, tuple[str, dict]] = {}  # code -> (today_date, data)

# ── 限购信息缓存（复用网络缓存TTL）────────────────
_limit_cache: dict[str, tuple[float, float | None]] = {}  # code -> (timestamp, amount_in_wan)


def _parse_purchase_limit(code: str) -> float | None:
    """获取基金单日限购金额（万元），None=无限购/获取失败"""
    import urllib.request
    now = time.time()
    if code in _limit_cache and now - _limit_cache[code][0] < 86400:
        return _limit_cache[code][1]

    result: float | None = None
    try:
        url = f"https://fund.eastmoney.com/{code}.html"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://fund.eastmoney.com/",
        })
        with urllib.request.urlopen(req, timeout=CFG.get("network",{}).get("timeout",{}).get("purchase_limit", 10)) as r:
            html = r.read().decode("utf-8", errors="ignore")
        # 提取限购金额，支持"万元"和"元"两种单位
        m = re.search(r"单日累计购买上限\s*([\d.]+)\s*万元", html)
        if m:
            result = float(m.group(1))  # 已经是万元
        else:
            m = re.search(r"单日累计购买上限\s*([\d.]+)\s*元", html)
            if m:
                result = float(m.group(1)) / 10000  # 元→万元
            # 检测"限大额"标记（有上限但未显示具体金额，视为<=2万）
            elif re.search(r"限大额", html):
                result = 2.0
        # 查找 fundBuyStatus="0" = 暂停申购
        if re.search(r'fundBuyStatus\s*=\s*"0"', html):
            result = 0.0  # 暂停申购
    except Exception:
        pass
    _limit_cache[code] = (now, result)
    return result


# ── 历史快照 ──────────────────────────────────

def _validate_fund_code(code: str) -> None:
    """校验基金代码：仅允许 6 位数字，防止路径遍历"""
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"非法基金代码: {code}")


# ── 推荐排行 ────────────────────────────────────


