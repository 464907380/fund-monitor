"""
基金数据获取与评分 — 共享工具模块
"""
import json
import os
import re
import time
import datetime
from config import CFG, api_url, get_timeout
from config import get_secret as _get_secret
from fund_utils import fetch, log, HISTORY_DIR, _fetch_fund_estimate
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


def _parse_real_time(code: str) -> float | None:
    """获取实时估算涨跌幅"""
    result = _fetch_fund_estimate(code)
    if result:
        _, gszzl = result
        return gszzl
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


def _parse_holdings(code: str) -> list[dict] | None:
    """获取前10大持仓明细（含股票名称/代码/占比），同时返回实时涨跌幅"""
    import html as _html
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
        return holds if holds else None
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
        # 从净值数据计算近3年收益
        d["sy3"] = _calc_period_return(full_nav, 750)  # ≈3年（约250个交易日/年 × 3）
        d["sy2"] = _calc_period_return(full_nav, 500)  # ≈2年
    else:
        if nav := _parse_net_trend(data):
            d["nav"] = nav
    td = _parse_real_time(code)
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
    LSJZ API 每页 20 条，max_pages=38 约 760 条（~3 年数据）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import urllib.request, re, json as _json

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
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_fetch_page, p): p for p in range(1, max_pages + 1)}
        for fut in as_completed(futs):
            for entry in fut.result():
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
                _perf_lookback = {"m1": 22, "m3": 66, "y1": 250, "f5": 5,
                                  "sy6": 125, "sy2": 500, "sy3": 750,
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
    """从 fundgz 实时估值 API 获取基金名（160B 请求）"""
    import urllib.request, re, json as _json
    try:
        url = f"https://fundgz.1234567.com.cn/js/{code}.js"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=get_timeout("default", 10)) as r:
            text = r.read().decode("utf-8")
        m = re.search(r"jsonpgz\((.+)\)", text)
        if m:
            data = _json.loads(m.group(1))
            return data.get("name", "")
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
    full_nav = _fetch_nav_from_lsjz(code, max_pages=max_pages)
    if full_nav:
        d["full_nav"] = full_nav
        d["nav"] = full_nav  # 完整净值数据
        # 计算风险指标
        metrics = _calc_nav_metrics(full_nav)
        d.update(metrics)
        # 从净值数据计算各阶段收益
        d["m1"] = _calc_period_return(full_nav, 22)    # ≈1月
        d["m3"] = _calc_period_return(full_nav, 66)    # ≈3月
        d["y1"] = _calc_period_return(full_nav, 250)   # ≈1年
        d["sy6"] = _calc_period_return(full_nav, 125)  # ≈6月
        d["sy3"] = _calc_period_return(full_nav, 750)  # ≈3年（可能不够数据）
        d["sy2"] = _calc_period_return(full_nav, 500)  # ≈2年
    else:
        d["nav"] = []

    _scoring_cache[code] = (today, d)
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


