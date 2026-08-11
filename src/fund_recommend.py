"""
基金推荐工具 — 从全市场筛选候选基金并评分

流程：
  1. 拉取全市场排行
  2. 按 y1 > min_y1_return 筛选
  3. 筛掉缺失收益数据（可选）
  4. 并行评分 → 保存结果到文件
  5. 前端展示时直接读取保存的评分结果

用法：
  python fund_recommend.py                    # 运行推荐
  python fund_recommend.py --load             # 查看上次结果
  python fund_recommend.py --add 基金代码     # 将基金加入 fund_list.json
"""
import sys
import json
import re
import urllib.request
import datetime
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from fund_utils import update_heartbeat, clear_heartbeat, _fetch_fund_estimate, setup_log, is_trading_day

setup_log("recommend.log")

try:
    from fund_watch import log, fetch
    from fund_scoring import _calc_score, SCORE_DIMS
    from config import api_url, CFG, get_timeout, get_config
except ImportError:
    print("请先在 fund_watch.py 同一目录运行")
    sys.exit(1)
    sys.exit(1)


# ── 批量实时估值（新浪行情接口，支持多只基金一次查询）───────

def _estimate_td_from_holdings(code: str) -> float | None:
    """从持仓股票实时涨跌估算基金当日净值变化
    
    基金实时估值 API（fundgz）已下线，此方法用最新季报持仓的
    股票实时行情做加权估算，仅供盘中参考。
    """
    try:
        from fund_watch import _parse_holdings
        import urllib.request, re as _re
        holds = _parse_holdings(code)
        if not holds:
            return None
        total_w = 0.0
        weighted_chg = 0.0
        for h in holds:
            if not h.get("c") or not h.get("p"):
                continue
            sc = h["c"]
            # 确定市场前缀
            prefix = "sh" if h.get("m") == "sh" else "sz"
            try:
                url = f"https://hq.sinajs.cn/list={prefix}{sc}"
                req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.sina.com.cn"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    raw = resp.read().decode("gbk")
                m = _re.search(r'"[^"]*"', raw)
                if not m:
                    continue
                parts = m.group(0).strip('"').split(",")
                if len(parts) < 4:
                    continue
                prev_close = float(parts[2]) if parts[2] else 0
                current = float(parts[3]) if parts[3] else 0
                if prev_close and prev_close > 0:
                    chg_pct = (current - prev_close) / prev_close * 100
                    w = h["p"]
                    total_w += w
                    weighted_chg += chg_pct * w
            except Exception:
                continue
        if total_w > 0:
            # 总仓位不足5%时不可靠
            if total_w < 5:
                return None
            return round(weighted_chg / total_w, 2)
    except Exception:
        pass
    return None


def _batch_fetch_estimates(codes: list[str], pct_base: int | None = None) -> dict[str, tuple[float, str]]:
    """批量获取基金当日涨跌幅，返回 {code: (涨跌幅%, 来源), ...}

    来源: lsjz=今日实际净值, holdings=持仓估算, fallback=降级
    pct_base: 非 None 时整体进度固定在该百分比（全量评分预取阶段用，避免进度条
    从 100 掉回评分阶段的低值）；None 时按 0-100 正常推进（缓存刷新涨跌用）。
    """
    result: dict[str, tuple[float, str]] = {}
    if not codes:
        return result
    now = datetime.datetime.now()
    is_after_market = now.hour > 15 or (now.hour == 15 and now.minute >= 0)

    # 涨跌来源：收盘后=LSJZ当日净值，盘中(9:30-15:00)=持仓估算，其余=新浪昨日
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")

    # 收盘后：探测今日净值是否已普遍发布（晚上才陆续出）。未发布则跳过每只的 LSJZ 查询，
    # 直接走新浪昨日（1 个轻量请求），避免 refresh 模式对 1733 只逐只发 LSJZ(空等)+新浪+fundgz 三个请求。
    _today_nav_ready = False
    if is_after_market:
        try:
            from fund_utils import _probe_latest_nav_date
            _probe_codes = list(dict.fromkeys(codes))[:3]
            # 并行探测 3 只（各 1 个 LSJZ 请求），替代串行省 ~4s
            if _probe_codes:
                with ThreadPoolExecutor(max_workers=3) as _pe:
                    _pfuts = {_pe.submit(_probe_latest_nav_date, _pc): _pc for _pc in _probe_codes}
                    for _pf in as_completed(_pfuts):
                        try:
                            _ld = _pf.result()
                        except Exception:
                            _ld = None
                        if _ld and _ld >= today_str:
                            _today_nav_ready = True
                            break
        except Exception:
            pass

    def _fetch_one_td(code: str) -> tuple[str, float | None, str]:
        """返回 (code, 涨跌幅, 来源)

        来源优先级（当日涨跌维度开启时）:
          收盘后 → LSJZ 当日实际净值 "lsjz"（先查当天缓存；净值未发布则直接新浪昨日）
          盘中(9:30-15:00) → 持仓估算 "holdings"
          其余/失败 → 新浪昨日 "fallback"
        """
        # 收盘后：先查当天 td 缓存（实际净值 lsjz，或批量预取的昨日 fallback），
        # 命中直接返回——避免 refresh 模式对 1733 只候选逐只发多个请求。
        if is_after_market:
            try:
                from fund_utils import _get_td_lsjz_cache, _TD_PROC
                _cached_td = _get_td_lsjz_cache(code)
                if _cached_td is not None:
                    return (code, _cached_td, "lsjz")
                # 批量预取的 fallback=昨日，仅当今日净值普遍未发布时才可命中；
                # 若探测到今日已发布（_today_nav_ready），该基金可能也有今日净值，
                # 不能用昨日 fallback 顶替（否则市场优选会显示"昨日"涨跌）——
                # 下方统一走 LSJZ 逐只查询今日，查不到今日再回退 fallback。
                if not _today_nav_ready:
                    _pe = _TD_PROC.get(code)
                    if _pe and _pe.get("date") == today_str and _pe.get("src") == "fallback" and _pe.get("td") is not None:
                        return (code, _pe["td"], "fallback")
            except Exception:
                pass
            # 今日净值普遍未发布 → 跳过 LSJZ 逐只查询，直接走新浪昨日（下方统一处理）
            if not _today_nav_ready:
                pass
            else:
                try:
                    _url_lsjz = f"https://api.fund.eastmoney.com/f10/lsjz?callback=j&fundCode={code}&pageIndex=1&pageSize=1"
                    _req_lsjz = urllib.request.Request(_url_lsjz, headers={"Referer": "https://fund.eastmoney.com/", "User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(_req_lsjz, timeout=get_timeout("default", 6)) as _r:
                        _lsjz_data = _r.read().decode("utf-8")
                    _m_date = re.search(r'FSRQ":"(\d{4}-\d{2}-\d{2})"', _lsjz_data)
                    _m_val = re.search(r'"JZZZL":"([-+\d.]+)"', _lsjz_data)
                    if _m_date and _m_val:
                        # 今日有实际净值 → 当日净值，写入缓存供同天复用
                        if _m_date.group(1) == today_str:
                            try:
                                from fund_utils import _set_td_lsjz_cache
                                _set_td_lsjz_cache(code, float(_m_val.group(1)))
                            except Exception:
                                pass
                            return (code, float(_m_val.group(1)), "lsjz")
                        # 今日无净值（非交易日/节假日）→ 返回最近净值，与自选表一致（不降级到新浪昨日）
                        if not is_trading_day(datetime.date.today()):
                            return (code, float(_m_val.group(1)), "lsjz")
                except Exception:
                    pass
        # 盘中(9:30-15:00)：优先持仓估算（与自选表"估算"一致）
        _now2 = datetime.datetime.now()
        if (9, 30) <= (_now2.hour, _now2.minute) < (15, 0):
            try:
                from fund_watch import _estimate_from_holdings
                from fund_utils import record_estimate
                _est = _estimate_from_holdings(code)
                if _est is not None:
                    record_estimate(code, _est)  # 记录盘中估算，供收盘后对比实际净值
                    return (code, _est, "holdings")
            except Exception:
                pass
        try:
            # 1. 新浪财经（轻量，速度快）
            _url = f"https://hq.sinajs.cn/list=of{code}"
            _req = urllib.request.Request(_url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.sina.com.cn"})
            with urllib.request.urlopen(_req, timeout=get_timeout("default", 6)) as _r:
                _text = _r.read().decode("gbk")
            _m = re.search(r'"[^"]*"', _text)
            if _m:
                _parts = _m.group(0).strip('"').split(",")
                if len(_parts) >= 5 and _parts[4]:
                    return (code, float(_parts[4]), "fallback")
        except Exception:
            pass
        # 2. fundgz（实时估算）
        try:
            _url2 = f"https://fundgz.1234567.com.cn/js/{code}.js"
            _req2 = urllib.request.Request(_url2, headers={"User-Agent":"Mozilla/5.0"})
            with urllib.request.urlopen(_req2, timeout=get_timeout("default", 6)) as _r2:
                _raw2 = _r2.read().decode("utf-8")
            _m2 = re.search(r'"gszzl":"([-+\d.]+)"', _raw2)
            if _m2 and _m2.group(1):
                return (code, float(_m2.group(1)), "holdings")
        except Exception:
            pass
        return (code, None, "fallback")

    replaced_gz = 0
    _failed_codes: list[str] = []
    _total_gz = len(codes)
    _start_gz = time.time()
    _last_hb_pct = -1
    # 盘中预取合并行情：并行收集所有候选持仓，合并股票一次性批量拉行情填缓存，
    # 之后各基金 _estimate_from_holdings 命中缓存（_fetch_stock_quotes_batch 60s缓存），
    # 避免每只候选单独重复拉持仓/行情
    _is_intraday = (9, 30) <= (now.hour, now.minute) < (15, 0)
    if _is_intraday and codes:
        try:
            from fund_watch import _parse_holdings, _fetch_stock_quotes_batch
            _all_sina: list[str] = []
            _total_h = len(codes)
            _done_h = 0
            _last_hb_h = -1
            with ThreadPoolExecutor(max_workers=30) as _pe:
                _hfs = {_pe.submit(_parse_holdings, _c): _c for _c in codes}
                for _hf in as_completed(_hfs):
                    try:
                        _hhs = _hf.result() or []
                    except Exception:
                        _hhs = []
                    for _hh in _hhs:
                        if _hh.get("c"):
                            _all_sina.append(("sh" if _hh.get("m") == "sh" else "sz") + _hh["c"])
                    _done_h += 1
                    _pct_h = int(_done_h / _total_h * 100) if _total_h else 0
                    if (_pct_h != _last_hb_h and _done_h % 50 == 0) or _done_h == _total_h:
                        _last_hb_h = _pct_h
                        update_heartbeat("fund_recommend", progress=_done_h, total=_total_h,
                                         overall_pct=(pct_base if pct_base is not None else 15),
                                         phase="预取持仓",
                                         detail=f"预取持仓 {_done_h}/{_total_h} ({_pct_h}%)",
                                         elapsed=round(time.time() - _start_gz, 1))
            if _all_sina:
                update_heartbeat("fund_recommend", progress=0, total=0,
                                 overall_pct=(pct_base if pct_base is not None else 15),
                                 phase="预取行情",
                                 detail=f"合并拉取 {len(_all_sina)} 只重仓股行情",
                                 elapsed=round(time.time() - _start_gz, 1))
                _fetch_stock_quotes_batch(_all_sina)
        except Exception:
            pass
    # 收盘后净值未发布 → 新浪基金批量预取昨日涨跌（并发分块拉取，替代 1733 只逐只请求）
    if is_after_market and not _today_nav_ready and codes:
        try:
            import urllib.request as _ur
            from fund_utils import _TD_PROC

            def _fetch_chunk(_chunk: list[str]) -> int:
                """拉取一块新浪基金行情，写入 _TD_PROC(fallback=昨日)。返回成功条数"""
                _url = f"https://hq.sinajs.cn/list=" + ",".join(f"of{c}" for c in _chunk)
                try:
                    _req = _ur.Request(_url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
                    with _ur.urlopen(_req, timeout=8) as _r:
                        _text = _r.read().decode("gbk", errors="ignore")
                    for _line in _text.strip().split("\n"):
                        _mm = re.search(r'hq_str_of(\d{6})="(.*?)"', _line)
                        if not _mm:
                            continue
                        _fcode = _mm.group(1)
                        _parts = _mm.group(2).split(",")
                        if len(_parts) >= 5 and _parts[4]:
                            # 写入进程内 td 缓存（fallback=昨日），供 _fetch_one_td 命中
                            _TD_PROC[_fcode] = {"date": today_str, "td": float(_parts[4]), "src": "fallback"}
                    return len(_chunk)
                except Exception:
                    return 0

            _chunks = [codes[i:i + 40] for i in range(0, len(codes), 40)]
            _done_batch = 0
            # 并发拉取各块（新浪行情接口支持多只一次查询，网络 IO 密集可并行），
            # 替代 44 块串行 ~60s+ → 8 worker 并发 ~10s
            with ThreadPoolExecutor(max_workers=8) as _be:
                _bfuts = {_be.submit(_fetch_chunk, _c): _c for _c in _chunks}
                for _bf in as_completed(_bfuts):
                    try:
                        _done_batch += _bf.result()
                    except Exception:
                        pass
                    if _done_batch % 200 == 0 or _done_batch >= len(codes):
                        update_heartbeat("fund_recommend", progress=_done_batch, total=_total_gz,
                                         overall_pct=(pct_base if pct_base is not None else 15),
                                         phase="刷新涨跌",
                                         detail=f"批量获取昨日涨跌 {_done_batch}/{_total_gz}",
                                         elapsed=round(time.time() - _start_gz, 1))
        except Exception:
            pass
    with ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "recommend_net_value", default=50)) as _ge:
        _gfuts = {_ge.submit(_fetch_one_td, c): c for c in codes}
        for _gf in as_completed(_gfuts):
            code, gz_val, gz_src = _gf.result()
            if gz_val is not None:
                result[code] = (gz_val, gz_src)
                replaced_gz += 1
            else:
                _failed_codes.append(code)
            _done = replaced_gz + len(_failed_codes)
            _pct = int(_done / _total_gz * 100) if _total_gz else 0
            if (_pct != _last_hb_pct and _done % 20 == 0) or _done == _total_gz:
                _last_hb_pct = _pct
                update_heartbeat("fund_recommend", progress=_done, total=_total_gz,
                                 overall_pct=(_pct if pct_base is None else pct_base),
                                 phase="刷新涨跌",
                                 detail=f"获取当日涨跌 {_done}/{_total_gz} ({_pct}%)",
                                 elapsed=round(time.time() - _start_gz, 1))
    if _failed_codes:
        # 并发重试失败基金（_fetch_fund_estimate 有多层降级）；串行重试在接口限频时会
        # 卡在"获取当日涨跌 100%"很久不动，并发化大幅缩短重试耗时，心跳每5只更新一次
        from fund_utils import _fetch_fund_estimate
        _retry_max_dur = get_config("recommend", "td_retry_timeout", default=120)
        _retry_start = time.time()
        _retry_total = len(_failed_codes)
        _retried = 0

        def _retry_one(_c: str) -> tuple[str, float | None, str]:
            try:
                _td = _fetch_fund_estimate(_c)
                if _td and _td[1] is not None:
                    return (_c, round(_td[1], 2), _td[2])
            except Exception:
                pass
            return (_c, None, "")

        _re = ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "recommend_net_value", default=30))
        try:
            _rfuts = {_re.submit(_retry_one, _c): _c for _c in _failed_codes}
            for _rf in as_completed(_rfuts):
                if time.time() - _retry_start > _retry_max_dur:
                    log.warning("刷新涨跌重试阶段超时(%ds)，跳过剩余 %d 只", _retry_max_dur, _retry_total - _retried)
                    break
                _rc, _rv, _rsrc = _rf.result()
                _retried += 1
                if _rv is not None:
                    result[_rc] = (_rv, _rsrc)
                    replaced_gz += 1
                _done2 = replaced_gz + (_retry_total - _retried)
                if _retried % 5 == 0 or _retried == _retry_total:
                    update_heartbeat("fund_recommend", progress=_done2, total=_total_gz,
                                     overall_pct=(int(_done2 / _total_gz * 100) if pct_base is None else pct_base),
                                     phase="刷新涨跌",
                                     detail=f"重试失败基金 {_retried}/{_retry_total}",
                                     elapsed=round(time.time() - _start_gz, 1))
        finally:
            _re.shutdown(wait=False)

    # 收盘后尝试用实际净值替换估算值
    if is_after_market and result:
        today_str = now.strftime("%Y-%m-%d")

        def _fetch_actual(code: str) -> tuple[str, float | None]:
            try:
                url = f"https://api.fund.eastmoney.com/f10/lsjz?callback=j&fundCode={code}&pageIndex=1&pageSize=1"
                _req2 = urllib.request.Request(url, headers={
                    "Referer": "https://fund.eastmoney.com/",
                    "User-Agent": "Mozilla/5.0",
                })
                with urllib.request.urlopen(_req2, timeout=get_timeout("default", 10)) as _r2:
                    raw2 = _r2.read().decode("utf-8")
                m_date = re.search(r'FSRQ":"(\d{4}-\d{2}-\d{2})"', raw2)
                m_val = re.search(r'"JZZZL":"([-+\d.]+)"', raw2)
                if m_date and m_val and m_date.group(1) == today_str:
                    return (code, float(m_val.group(1)))
            except Exception:
                pass
            return (code, None)

        codes_list = list(result.keys())
        replaced = 0
        # 限制实际净值替换总时间不超过10秒，超时后保留剩余基金的新浪估算值
        _start = time.time()
        _max_dur = get_config("recommend", "net_value_timeout", default=10)
        _ae = ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "recommend_net_value", default=50))
        _afuts = {_ae.submit(_fetch_actual, c): c for c in codes_list}
        try:
            for _af in as_completed(_afuts):
                code, actual_val = _af.result()
                if actual_val is not None:
                    result[code] = (actual_val, "lsjz")
                    replaced += 1
                if time.time() - _start > _max_dur:
                    break
        finally:
            _ae.shutdown(wait=False)
        if replaced:
            log.info("收盘后实际净值替换: %d/%d 只基金(%.1fs)", replaced, len(codes_list), time.time()-_start)

    return result

# ── 配置 ──────────────────────────────────────
_TOP = CFG.get("recommend", {}).get("top_n", 200)
SHOW_TOP = CFG.get("recommend", {}).get("show_top", 20)
_SKIP_MISSING_PERF = CFG.get("recommend", {}).get("skip_missing_perf", False)
_SKIP_LIMITED = CFG.get("recommend", {}).get("skip_limited", False)
_HAS_TD = any(dim_name == "\u5f53\u65e5\u6da8\u8dcc" for dim_name, _, _, _ in SCORE_DIMS)
"""当日涨跌维度是否开启：开启时缓存命中后仍需刷新当日涨跌值重新评分"""
_RANK_SORT = CFG.get("recommend", {}).get("rank_sort", "1n")
"""排行排序方式：1n=近1年收益, 6n=近6月收益, 3y=近3月收益, 1y=近1月收益"""
# 筛选条件（多条件组合）
_FILTER_CONDITIONS = CFG.get("recommend", {}).get("filter_conditions", [])
"""筛选条件列表：[{field, op, value}, ...]  field: y1/sy6/m3/m1/sy2/sy3"""

# 运行时重载配置（由 _reload_config 调用时更新）
def _reload_config() -> None:
    """从文件重新加载 config.json，更新筛选条件等运行时变量"""
    global _TOP, SHOW_TOP, _SKIP_MISSING_PERF, _SKIP_LIMITED, _RANK_SORT, _FILTER_CONDITIONS
    import json as _json
    import logging
    _path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "config.json")
    try:
        with open(_path, encoding="utf-8") as _f:
            raw = _f.read()
        if not raw.strip():
            logging.warning("config.json 为空文件，跳过重载")
            return
        _cfg = _json.loads(raw)
        _rec = _cfg.get("recommend", {})
        _TOP = int(_rec.get("top_n", 200))
        SHOW_TOP = int(_rec.get("show_top", 20))
        _SKIP_MISSING_PERF = bool(_rec.get("skip_missing_perf", False))
        _SKIP_LIMITED = bool(_rec.get("skip_limited", False))
        _RANK_SORT = str(_rec.get("rank_sort", "1n"))
        _FILTER_CONDITIONS = _rec.get("filter_conditions", [])
    except Exception as e:
        logging.warning("_reload_config 失败: %s，使用模块级默认值", e)
# 排行API字段映射
_RANK_FIELD_MAP = {
    "y1":  {"idx": 11, "name": "近1年收益"},
    "sy6": {"idx": 10, "name": "近6月收益"},
    "m3":  {"idx": 9,  "name": "近3月收益"},
    "m1":  {"idx": 8,  "name": "近1月收益"},
    "sy2": {"idx": 12, "name": "近2年收益"},
    "sy3": {"idx": 13, "name": "近3年收益"},
    "f5":  {"idx": 7,  "name": "近一周收益"},
    "sc":  {"idx": 18, "name": "规模(亿)"},
}
_RECOMMEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RESULT_FILE = os.path.join(_RECOMMEND_DIR, ".fund_recommend_result.json")

# 净值走势磁盘缓存批量预热缓冲（评分线程填充，main 结束统一落盘一次）
_trend_flush_buf: dict[str, list] = {}
_trend_flush_lock = threading.Lock()


def _flush_trend_cache() -> None:
    """把评分过程中拉取的净值走势批量写入共享磁盘缓存（供前端折线图复用）"""
    with _trend_flush_lock:
        _buf = dict(_trend_flush_buf)
        _trend_flush_buf.clear()
    if not _buf:
        return
    try:
        from fund_utils import _load_fund_trend_cache, _save_fund_trend_cache
        today = datetime.date.today().isoformat()
        cache = _load_fund_trend_cache()
        for code, navs in _buf.items():
            cache[code] = {"date": today, "navs": navs}
        _save_fund_trend_cache(cache)
        print(f"   💾 已预热 {len(_buf)} 只基金净值走势到共享缓存", flush=True)
    except Exception:
        pass
_FUND_LIST_FILE = os.path.join(_RECOMMEND_DIR, "data", "fund_list.json")

# ── 超时统计（供推荐任务结束时展示）──
_timeout_count = 0
_timeout_details: list[str] = []


def _increment_timeout(url: str = "") -> None:
    """增加超时计数并记录详情"""
    global _timeout_count, _timeout_details
    _timeout_count += 1
    _timeout_details.append(url[:80])
    if len(_timeout_details) > 50:
        _timeout_details.pop(0)


def _safe_fetch(url: str, headers: dict | None = None) -> str:
    """带超时统计的 fetch"""
    result = fetch(url, headers)
    if not result:
        _increment_timeout(url)
    return result

# 启动时打印配置，方便排查缓存问题
print(f"[CFG] top_n={_TOP}, show_top={SHOW_TOP}, skip_missing={_SKIP_MISSING_PERF}, skip_limited={_SKIP_LIMITED}, rank_sort={_RANK_SORT}", file=sys.stderr)


def _parse_rank_response(data: str) -> list[list[str]] | None:
    """解析天天基金排行 API 的 JSONP 响应"""
    try:
        raw = data.replace("var rankData = ", "", 1).rstrip(";")
        raw_clean = re.sub(r'(\{|,)\s*(\w+)\s*:', lambda m: m.group(1) + '"' + m.group(2) + '":', raw)
        result = json.loads(raw_clean)
        rows = [row.split(",") for row in result.get("datas", [])]
        return rows if rows else None
    except (json.JSONDecodeError, KeyError, IndexError):
        return None


# ── 排行当日缓存（盘中净值不变，同一天多次运行复用；跨日自动失效）──
_RANK_CACHE_PATH = os.path.join(_RECOMMEND_DIR, "data", "fund_rank_cache.json")
_RANK_CACHE_MEM: dict | None = None


def _load_rank_cache() -> dict | None:
    global _RANK_CACHE_MEM
    try:
        if os.path.exists(_RANK_CACHE_PATH):
            with open(_RANK_CACHE_PATH, encoding="utf-8") as f:
                _RANK_CACHE_MEM = json.load(f)
            if _RANK_CACHE_MEM.get("date") == datetime.date.today().isoformat():
                return _RANK_CACHE_MEM
    except Exception:
        pass
    return None


def _save_rank_cache(pn: int, rows: list) -> None:
    global _RANK_CACHE_MEM
    try:
        _RANK_CACHE_MEM = {"date": datetime.date.today().isoformat(), "pn": pn,
                           "sort": _RANK_SORT, "rows": rows}
        _tmp = _RANK_CACHE_PATH + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as f:
            json.dump(_RANK_CACHE_MEM, f, ensure_ascii=False)
        os.replace(_tmp, _RANK_CACHE_PATH)
    except Exception:
        pass


def _fetch_rank_list(pn: int) -> list[list[str]]:
    """从天天基金排行 API 获取全市场基金排行（并发多URL + 当日磁盘缓存）"""
    # 当日排行缓存：盘中净值不变，同一天多次运行(全量/重筛/刷新)复用，跨日自动失效
    _c = _load_rank_cache()
    if _c and _c.get("pn") == pn and _c.get("sort") == _RANK_SORT:
        print(f"   💾 排行当日缓存命中: {len(_c['rows'])} 只")
        return _c["rows"]
    # 根据排序方式决定日期范围
    sort_days = {"1n": 365, "6n": 180, "3y": 90, "1y": 30, "2n": 730, "3n": 1095}
    if _RANK_SORT == "zn":
        sd = datetime.date(datetime.date.today().year, 1, 1).isoformat()
    else:
        days = sort_days.get(_RANK_SORT, 365)
        sd = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    ed = datetime.date.today().isoformat()
    sc = _RANK_SORT
    urls = [
        api_url("fund_rank") + f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc={sc}&st=desc"
                              f"&sd={sd}&ed={ed}&pi=1&pn={pn}&dx=1",
        api_url("fund_rank") + f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc={sc}&st=desc"
                              f"&sd={sd}&ed={ed}&pi=1&pn={pn}",
        "http://fund.eastmoney.com/data/rankhandler.aspx" + f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc={sc}&st=desc"
                              f"&sd={sd}&ed={ed}&pi=1&pn={pn}&dx=1",
        "http://fund.eastmoney.com/data/rankhandler.aspx" + f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc={sc}&st=desc"
                              f"&sd={sd}&ed={ed}&pi=1&pn={pn}",
    ]

    def _try_one(url: str) -> list[list[str]] | None:
        try:
            data = _safe_fetch(url, {"Referer": "https://fund.eastmoney.com/"})
            return _parse_rank_response(data)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(_try_one, url): url for url in urls[:2]}
        for f in as_completed(futs):
            rows = f.result()
            if rows:
                _save_rank_cache(pn, rows)
                return rows

    for url in urls[2:]:
        rows = _try_one(url)
        if rows:
            _save_rank_cache(pn, rows)
            return rows
    return []


def _filter_candidates(rows: list) -> list[dict]:
    """根据多条件筛选候选基金，返回 [{code, name, y1}, ...]"""
    candidates = []
    for r in rows:
        try:
            code = r[0]
            name = r[1]
            y1 = float(r[11]) if len(r) > 11 and r[11] else 0
            # 筛掉缺失收益数据（在初筛阶段就执行，避免进入评分后失败）
            if _SKIP_MISSING_PERF:
                _perf_idxs = {"m1": 8, "m3": 9, "sy6": 10, "y1": 11, "sy2": 12, "sy3": 13}
                _missing = False
                for _key, _idx in _perf_idxs.items():
                    if len(r) <= _idx or not r[_idx]:
                        _missing = True
                        break
                if _missing:
                    continue
            # 多条件组合筛选
            passed = True
            for cond in _FILTER_CONDITIONS:
                field = cond.get("field", "")
                op = cond.get("op", "gte")
                val = cond.get("value")
                if val is None or field not in _RANK_FIELD_MAP:
                    continue
                fidx = _RANK_FIELD_MAP[field]["idx"]
                raw = float(r[fidx]) if len(r) > fidx and r[fidx] else 0
                if op == "gte" and not (raw >= val):
                    passed = False
                    break
                elif op == "lte" and not (raw <= val):
                    passed = False
                    break
                elif op == "eq" and not (abs(raw - val) < 0.01):
                    passed = False
                    break
            if not passed:
                continue
            candidates.append({"code": code, "name": name, "y1": y1})
        except (ValueError, IndexError):
            continue
    return candidates


_CONFIG_VERSION = "2"
"""配置版本号，修改解析逻辑或配置结构时递增，使旧缓存失效"""


def _filter_hash() -> str:
    """计算筛选条件哈希，仅包含影响数据筛选的参数（不含权重）"""
    import hashlib
    parts = [
        _CONFIG_VERSION,
        str(_TOP), str(_SKIP_MISSING_PERF), str(_SKIP_LIMITED), _RANK_SORT, json.dumps(_FILTER_CONDITIONS, sort_keys=True),
    ]
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _config_hash() -> str:
    """计算当前配置的哈希值，用于检测评分/筛选参数是否变化"""
    import hashlib
    from fund_scoring import SCORE_DIMS
    parts = [
        _CONFIG_VERSION,
        str(_TOP), str(SHOW_TOP), str(_SKIP_MISSING_PERF), str(_SKIP_LIMITED), json.dumps(_FILTER_CONDITIONS, sort_keys=True),
    ]
    for name, fn, weight, desc in SCORE_DIMS:
        parts.append(f"{name}|{weight}|{desc}")
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _score_hash() -> str:
    """计算评分配置哈希（仅权重+维度，不含筛选条件）。
    筛选条件变化不影响评分指标，故 score_hash 相同时可复用已评分结果重新过滤。"""
    import hashlib
    from fund_scoring import SCORE_DIMS
    parts = [_CONFIG_VERSION]
    for name, fn, weight, desc in SCORE_DIMS:
        parts.append(f"{name}|{weight}|{desc}")
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _filter_scored_results(results: list[dict]) -> list[dict]:
    """用当前筛选条件对已评分结果重新过滤（复用指标，无需重新拉数据/重评）。
    f5 等字段是带 % 字符串，统一去符号/百分号再比较。"""
    if not _FILTER_CONDITIONS:
        return list(results)
    out: list[dict] = []
    for r in results:
        ok = True
        for cond in _FILTER_CONDITIONS:
            field = cond.get("field", "")
            op = cond.get("op", "gte")
            val = cond.get("value")
            if val is None or field not in _RANK_FIELD_MAP:
                continue
            raw = r.get(field)
            if raw is None:
                ok = False
                break
            try:
                num = float(str(raw).replace("%", "").replace("+", ""))
            except (ValueError, TypeError):
                ok = False
                break
            if op == "gte" and not (num >= val):
                ok = False
                break
            elif op == "lte" and not (num <= val):
                ok = False
                break
            elif op == "eq" and not (abs(num - val) < 0.01):
                ok = False
                break
        if ok:
            out.append(r)
    return out


def _save_result(results: list[dict]) -> bool:
    """保存评分结果到文件"""
    if not results:
        print("\n⚠️ 未找到匹配基金，保留上次结果")
        return False
    # 清理残留锁文件（超过5分钟）
    lock_file = _RESULT_FILE + ".lock"
    try:
        if os.path.exists(lock_file) and time.time() - os.path.getmtime(lock_file) > 300:
            os.remove(lock_file)
    except OSError:
        pass
    try:
        data = {
            "date": datetime.date.today().isoformat(),
            "config_hash": _config_hash(),
            "filter_hash": _filter_hash(),
            "score_hash": _score_hash(),
            "results": results,
            "timeout_count": _timeout_count,
        }
        # 原子写入
        _tmp = _RESULT_FILE + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(_tmp, _RESULT_FILE)
        print(f"\n📁 已保存 {len(results)} 只基金评分结果到 {_RESULT_FILE}")
        return True
    except Exception as e:
        print(f"\n⚠️ 保存结果失败: {e}")
        return False


def _load_result() -> list[dict] | None:
    """加载上次推荐结果"""
    if not os.path.exists(_RESULT_FILE):
        return None
    try:
        with open(_RESULT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        print(f"📁 上次推荐结果 ({data.get('date', '未知日期')})")
        return data.get("results", [])
    except (json.JSONDecodeError, OSError):
        return None


def _add_to_fund_list(code: str, name: str = "") -> bool:
    """将基金代码加入 fund_list.json"""
    if not os.path.exists(_FUND_LIST_FILE):
        print(f"⚠️  {_FUND_LIST_FILE} 不存在")
        return False
    try:
        with open(_FUND_LIST_FILE, encoding="utf-8") as f:
            fl = json.load(f)
        for item in fl:
            if item["code"] == code:
                print(f"⚠️  {code}({name}) 已在 fund_list.json 中")
                return True
        fl.append({"code": code})
        with open(_FUND_LIST_FILE, "w", encoding="utf-8") as f:
            json.dump(fl, f, ensure_ascii=False, indent=2)
        print(f"✅ 已加入监控: {code}({name})")
        return True
    except (json.JSONDecodeError, OSError) as e:
        print(f"❌ 写入失败: {e}")
        return False


def _print_results(results: list[dict]) -> None:
    """打印评分结果"""
    from fund_scoring import SCORE_DIMS
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(results[:SHOW_TOP], 1):
        badge = medals[i - 1] if i <= 3 else f" {i}."
        print(f"{badge} {r['name']} ({r['code']}) — {r['score']:.1f}分  年化{r.get('annual_return',0):.1f}%")
    print()
    print("💡 一键加入监控: python fund_recommend.py --add 基金代码")


def _score_one(code: str, name: str, limit_amount: float | None = None,
               td_map: dict | None = None) -> dict | None:
    """单只基金评分（td_map 为批量预取的当日涨跌映射，避免每只独立发多个网络请求）"""
    try:
        from fund_watch import get_scoring_data as _get
        d = _get(code)
        if not d.get("n"):
            return None
        # 预热近三年净值走势到共享磁盘缓存（供前端折线图近1月/3月/6月/1年/2年/3年复用）
        try:
            _nav_all = d.get("nav", [])
            if len(_nav_all) >= 250:
                with _trend_flush_lock:
                    _trend_flush_buf[code] = [[n["d"], n["v"]] for n in _nav_all[-760:]]
        except Exception:
            pass
        # 计算近一周涨跌幅（需在缺失检查前计算，因为 f5 不在原始数据中）
        navs = d.get("nav", [])
        f5_val = ""
        if len(navs) >= 6:
            # 近一周 = 最近5个交易日（与排行API口径一致，navs[-1]最新，往前第6个点即5个交易日前）
            pct = (navs[-1]["v"] - navs[-6]["v"]) / navs[-6]["v"] * 100
            f5_val = f"{pct:+.1f}%"
        elif len(navs) >= 5:
            pct = (navs[-1]["v"] - navs[-5]["v"]) / navs[-5]["v"] * 100
            f5_val = f"{pct:+.1f}%"
        d["f5"] = f5_val
        # 筛掉缺失收益数据
        if _SKIP_MISSING_PERF:
            perf_keys = ["m1", "m3", "y1", "f5", "sy6", "sy2", "sy3", "annual_return"]
            if any(d.get(k) is None or d.get(k) == "" for k in perf_keys):
                log.debug("跳过 %s(%s): 缺失收益维度", name, code)
                return None
        # 获取当日涨跌（供td维度评分）：优先用批量预取值，命中则跳过独立网络请求
        td_src = ""
        td = None
        if td_map:
            _item = td_map.get(code)
            if _item and _item[0] is not None:
                td = round(_item[0], 2)
                td_src = _item[1]
        if td is None:
            _fe = _fetch_fund_estimate(code)
            if _fe is not None and _fe[1] is not None:
                td = round(_fe[1], 2)
                td_src = _fe[2]
        if td is not None:
            d["td"] = td
            day_str = f"{td:+.2f}%"
        else:
            # 无实时数据时从净值算最近交易日涨跌
            navs_local = d.get("nav", [])
            if navs_local and len(navs_local) >= 2:
                td_val = (navs_local[-1]["v"] - navs_local[-2]["v"]) / navs_local[-2]["v"] * 100
                d["td"] = td_val
                day_str = f"{td_val:+.2f}%"
                td_src = "lsjz"
            else:
                day_str = ""
        score = _calc_score(d)  # 带td值重新评分
        # 多窗口指标（供维度按窗口评分：改窗口后无需重新运行推荐，直接从结果文件读取）
        _win_dims = ["max_dd", "volatility", "max_loss_days", "sharpe", "sortino",
                     "calmar", "recovery", "win_rate", "profit_ratio", "annual_return"]
        _win_fields = {f"{_dk}_{_lb}": d.get(f"{_dk}_{_lb}") for _dk in _win_dims for _lb in ("1y", "2y", "3y")}
        return {
            "code": code, "name": name, "score": score,
            "limit_amount": limit_amount,
            "annual_return": d.get("annual_return"),
            "m1": d.get("m1"), "m3": d.get("m3"), "y1": d.get("y1"),
            "sharpe": d.get("sharpe"), "sortino": d.get("sortino"),
            "max_dd": d.get("max_dd"), "win_rate": d.get("win_rate"),
            "inst": d.get("inst"), "sc": d.get("sc"), "rate": d.get("rate"),
            "profit_ratio": d.get("profit_ratio"),
            "recovery": d.get("recovery"), "sy3": d.get("sy3"),
            "f5": f5_val, "sy2": d.get("sy2"),
            "volatility": d.get("volatility"), "calmar": d.get("calmar"),
            "max_loss_days": d.get("max_loss_days"), "sy6": d.get("sy6"),
            "td": d.get("td"),
            "_td_src": td_src,
            "_trend": (lambda _n: [[_n[0]["d"], 0.0]] + [[_n[i]["d"], round((_n[i]["v"] - _n[i-1]["v"]) / _n[i-1]["v"] * 100, 2)] for i in range(1, len(_n))] if len(_n) >= 2 else None)(d.get("nav", [])[-66:]),
            "mgr": (d.get("mgr") or "")[:6],
            "day": day_str,
            **_win_fields,
        }
    except Exception as e:
        log.debug("跳过 %s: %s", code, e)
        return None


def _re_score_and_refresh(cached_results: list[dict], total_candidates: int) -> None:
    """用当前权重重新评分 + 刷新涨跌（复用缓存数据）"""
    from fund_scoring import _calc_score as _calc_score2
    _t = time.time()
    total = total_candidates
    print(f"📋 重新评分 {total} 只基金（新权重）...")
    update_heartbeat("fund_recommend", progress=0, total=total, overall_pct=50,
                     phase="重新评分",
                     detail=f"重新评分 {total} 只", elapsed=0)

    for i, r in enumerate(cached_results):
        r["score"] = _calc_score2(r)
        if (i + 1) % 200 == 0:
            pct = (i + 1) / total * 100
            update_heartbeat("fund_recommend", progress=i + 1, total=total,
                             overall_pct=min(50 + int(pct * 0.35), 85), phase="重新评分",
                             detail=f"重评 {i+1}/{total} ({pct:.0f}%)", elapsed=round(time.time() - _t, 1))

    print(f"  重评完成 ({time.time()-_t:.1f}s)")
    cached_results.sort(key=lambda x: x.get("score", 0), reverse=True)

    if _HAS_TD:
        _t2 = time.time()
        print(f"📋 当日涨跌维度开启，刷新 {total} 只基金td值...")
        update_heartbeat("fund_recommend", progress=0, total=total, overall_pct=85,
                         phase="刷新涨跌",
                         detail=f"获取 {total} 只基金当日涨跌", elapsed=round(time.time() - _t, 1))
        all_codes = [r.get("code", "") for r in cached_results]
        td_map = _batch_fetch_estimates([c for c in all_codes if c])
        print(f"  td刷新完成 ({time.time()-_t2:.1f}s), 获取到 {len(td_map)} 只")
        for r in cached_results:
            code = r.get("code", "")
            _td_item = td_map.get(code)
            if _td_item:
                td_val, td_src = _td_item
                r["td"] = td_val
                r["_td_src"] = td_src
                r["day"] = f"{td_val:+.2f}%" if td_val is not None else ""
                if td_val is not None:
                    r["score"] = _calc_score2(r)
        cached_results.sort(key=lambda x: x.get("score", 0), reverse=True)
    else:
        _t2 = time.time()
        print(f"📋 刷新前 {SHOW_TOP} 只显示涨跌...")
        update_heartbeat("fund_recommend", progress=0, total=SHOW_TOP, overall_pct=50,
                         phase="更新涨跌",
                         detail=f"刷新前 {SHOW_TOP} 只涨跌", elapsed=round(time.time() - _t, 1))

        def _update_day(code: str) -> tuple[str, str]:
            try:
                td = _fetch_fund_estimate(code)
                if td is not None:
                    return (code, f"{td[1]:+.2f}%")
            except Exception:
                pass
            return (code, "")

        day_map: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "recommend_update_day", default=50)) as ex:
            futs = {ex.submit(_update_day, r.get("code", "")): r for r in cached_results[:SHOW_TOP]}
            for i, fut in enumerate(as_completed(futs), 1):
                code, day = fut.result()
                day_map[code] = day
                pct = i / SHOW_TOP * 100
                update_heartbeat("fund_recommend", progress=i, total=SHOW_TOP, phase="涨跌",
                                 detail=f"刷新涨跌 {i}/{SHOW_TOP}", elapsed=round(time.time() - _t, 1))

        print(f"  涨跌刷新完成 ({time.time()-_t2:.1f}s)")
        for r in cached_results:
            code = r.get("code", "")
            if code in day_map:
                r["day"] = day_map[code]

        cached_results.sort(key=lambda x: x.get("score", 0), reverse=True)

    update_heartbeat("fund_recommend", progress=total_candidates, total=total_candidates, status="保存")
    _save_result(cached_results)
    print(f"\n🏆 基金推荐 TOP {SHOW_TOP}")
    print("=" * 50)
    _print_results(cached_results)


def _supplement_self_selected(base_results: list | None = None, td_map: dict | None = None) -> None:
    """补拉自选基金数据到推荐结果
    如果提供 base_results，则在其基础上追加（不保存文件）；
    否则加载现有结果文件追加。
    td_map: 已批量预取的当日涨跌映射（可选）。缺失自选基金不在其中时，
    内部批量补拉一次，避免 _score_one 每只单独走 fundgz（已死，重试拖慢）。
    """
    try:
        _fund_list_file = os.path.join(_RECOMMEND_DIR, "data", "fund_list.json")
        if not os.path.exists(_fund_list_file):
            return
        with open(_fund_list_file, encoding="utf-8") as _fl:
            _fl_data = json.load(_fl)
        if not _fl_data:
            return
        if base_results is not None:
            _existing = {r["code"] for r in base_results}
        else:
            _old = _load_result()
            _existing = {r["code"] for r in _old} if _old else set()
        _missing = [f for f in _fl_data if f["code"] not in _existing]
        if not _missing:
            update_heartbeat("fund_recommend", progress=1, total=1,
                             overall_pct=98, phase="检查自选基金",
                             detail="自选基金数据已存在")
            return
        _total = len(_missing)
        print(f"\n📋 补拉 {_total} 只自选基金数据...")
        _extra: list[dict] = []
        _done_supp = 0
        # 预热净值走势磁盘缓存：首次全量读 SQLite(6332只) 若被并发 20 线程同时触发
        # 会造成 IO 风暴+连接争抢（冷启动极慢，实测并发20=243s vs 串行3s）。
        # 先串行加载一次进内存，后续 mtime 命中 O(1)
        try:
            from fund_utils import _load_fund_trend_cache
            _load_fund_trend_cache()
        except Exception:
            pass
        # 缺失自选基金的当日涨跌批量预取：_score_one 命中 td_map 后跳过
        # 逐只 fundgz（该接口已死，每次重试 2×timeout 很慢），收盘后走新浪批量
        if td_map is None or not td_map:
            try:
                _td_map = _batch_fetch_estimates([f["code"] for f in _missing])
            except Exception:
                _td_map = {}
        else:
            _td_map = td_map
        _miss_td = [f["code"] for f in _missing if f["code"] not in _td_map]
        if _miss_td:
            try:
                _extra_td = _batch_fetch_estimates(_miss_td)
                _td_map = {**_td_map, **_extra_td}
            except Exception:
                pass

        def _score_and_check(f: dict) -> dict | None:
            try:
                # 自选基金是用户手动加入的，必须始终出现在推荐表里，
                # 不受筛选条件限制（否则不符合 m1≥5% 等条件的自选会被静默丢弃）。
                # 只评分（含缺失收益维度的照常纳入），不应用 _FILTER_CONDITIONS 过滤。
                _r = _score_one(f["code"], f.get("name", ""), td_map=_td_map)
                if not _r:
                    return None
                return _r
            except Exception:
                return None

        # 串行评分（自选基金数量少，一般 ≤50 只）。实测并发 20 在冷启动 SQLite
        # 净值缓存时会因锁竞争+IO 风暴慢到 243s/24只，串行反而只要 3s——自选量小，
        # 串行更稳更快。_score_one 命中 trend 缓存 + td_map 后单只 <0.1s。
        for _f in _missing:
            _r = _score_and_check(_f)
            _done_supp += 1
            if _r:
                _extra.append(_r)
                print(f"  ✅ {_f['code']} {_r['name']} — {_r['score']:.1f}分")
            else:
                print(f"  ⏭️ {_f['code']} {_f.get('name','')[:12]} — 跳过")
            # 逐只更新心跳：串行每只可能几秒（拉净值），每 1 只反馈更及时，避免"卡住"假象
            update_heartbeat("fund_recommend", progress=_done_supp, total=_total,
                             overall_pct=max(97, 97 + int(_done_supp / _total * 2)),
                             phase="检查自选基金",
                             detail=f"补充自选基金 {_done_supp}/{_total} {_f['code']} {_f.get('name','')[:10]}")
        if not _extra:
            return
        if base_results is not None:
            # 追加到调用者提供的列表，由调用者统一保存
            base_results.extend(_extra)
            base_results.sort(key=lambda x: x.get("score", 0), reverse=True)
            print(f"  已补入 {len(_extra)} 只自选基金，追加到待保存列表")
        else:
            _old_list = _old or []
            _old_list.extend(_extra)
            _old_list.sort(key=lambda x: x.get("score", 0), reverse=True)
            _save_result(_old_list)
            print(f"  已补入 {len(_extra)} 只自选基金，重新保存")
    except Exception as _e:
        print(f"⚠️ 补拉自选基金数据失败: {_e}")


def main() -> None:
    _t0 = time.time()  # 全局计时起点
    _has_error = False  # 标记是否发生异常
    # 进入 main 后重新加载配置，保证使用最新筛选条件
    _reload_config()

    def _elapsed() -> float:
        return round(time.time() - _t0, 1)

    # 全局进度百分比计算（各阶段权重：排行2% + 初筛1% + 限购12% + 评分82% + 保存3%）
    _phase_weights = {}  # 用于跟踪各阶段的 scale

    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        # 三级缓存检查
        cur_hash = _config_hash()
        cur_filter_hash = _filter_hash()
        cache_mode = None  # "full": 全命中, "re-score": 仅权重变, None: 全量
        print("=" * 60)
        print(f"🔍 基金优选推荐 — 全市场深度评分  ({datetime.datetime.now():%Y-%m-%d %H:%M:%S})")
        print("=" * 60)

        if os.path.exists(_RESULT_FILE):
            try:
                with open(_RESULT_FILE, encoding="utf-8") as _f:
                    old = json.load(_f)
                saved_date = old.get("date")
                n_cached = len(old.get("results", []))
                print(f"\n📁 发现缓存结果: 日期={saved_date}, {n_cached} 只基金")
                print(f"   当前 config_hash={cur_hash[:12]}..  filter_hash={cur_filter_hash[:12]}..")
                print(f"   缓存 config_hash={str(old.get('config_hash',''))[:12]}..  filter_hash={str(old.get('filter_hash',''))[:12]}..")

                if saved_date == datetime.date.today().isoformat():
                    _sc_same = old.get("score_hash") == _score_hash()
                    _fl_same = old.get("filter_hash") == cur_filter_hash
                    if _fl_same and _sc_same:
                        print(f"✅ 筛选与评分配置均未变化 ({_elapsed()}s)")
                        print(f"   使用缓存结果（仅更新涨跌）")
                        cache_mode = "full"
                    elif _fl_same:
                        # 仅权重/维度变化 → 复用结果数据重新评分（无需重新拉净值/重筛）
                        print(f"🔄 评分配置已变化，筛选条件未变 ({_elapsed()}s)")
                        print(f"   复用 {n_cached} 只已评分数据重新评分")
                        cache_mode = "rescore"
                    elif _sc_same:
                        # 仅筛选条件变化、评分配置(权重/维度)未变 → 复用已评分结果重新过滤
                        print(f"🔄 筛选条件已变化，评分配置未变 ({_elapsed()}s)")
                        print(f"   复用已评分结果重新过滤，仅补充新增候选")
                        cache_mode = "refilter"
                    else:
                        print(f"🔄 筛选条件与评分配置均已变化 ({_elapsed()}s)")
                        print(f"   全量重新拉取排行和评分")
                else:
                    print(f"📅 缓存日期 ({saved_date}) ≠ 今天 ({datetime.date.today()})")
                    print(f"   跨日必须全量重新拉取排行和评分，确保使用最新数据")
            except Exception as e:
                print(f"⚠️ 缓存读取失败: {e}，全量重新运行")

        if cache_mode:
            cached_results = old["results"]
            total_candidates = len(cached_results)
            print(f"   候选基金: {total_candidates} 只")

            if cache_mode == "rescore":
                # 仅权重/维度变化 → 复用结果数据按新权重重新评分（不重新拉净值/重筛）
                _re_score_and_refresh(cached_results, total_candidates)
                print(f"\n⏱ 总耗时: {_elapsed()}s (重评模式)")
                return

            if cache_mode == "refilter":
                # 筛选条件变了但评分配置(权重/维度)没变 → 复用已评分结果重新过滤，只补充新增候选
                print(f"\n🔄 重筛模式: 复用 {total_candidates} 只已评分基金...")
                update_heartbeat("fund_recommend", progress=0, total=1, overall_pct=5,
                                 phase="重新筛选", detail=f"过滤 {total_candidates} 只已评分基金", elapsed=_elapsed())
                _base = _filter_scored_results(cached_results)
                _base_set = {r["code"] for r in _base}
                print(f"   ✅ 旧结果按新条件过滤: {len(_base)} 只 ({_elapsed()}s)")
                # 从排行 API 初筛当前条件全部候选，找出需新增评分的
                _t2 = time.time()
                _rows = _fetch_rank_list(_TOP)
                _cands = _filter_candidates(_rows)
                print(f"   ✅ 排行初筛: {len(_cands)} 只 ({time.time()-_t2:.1f}s)")
                _new_cands = [c for c in _cands if c["code"] not in _base_set]
                print(f"   ➕ 新增需评分: {len(_new_cands)} 只 (复用已评分 {len(_base)} 只)")
                _scored_new: list[dict] = []
                if _new_cands:
                    _t3 = time.time()
                    update_heartbeat("fund_recommend", progress=0, total=len(_new_cands), overall_pct=30,
                                     phase="评分", detail=f"评分新增 {len(_new_cands)} 只", elapsed=_elapsed())
                    _td_prefetch = _batch_fetch_estimates([c["code"] for c in _new_cands], pct_base=30)
                    with ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "recommend_scoring", default=50)) as _ex:
                        _futs = {_ex.submit(_score_one, c["code"], c["name"], c.get("_limit_amount"), _td_prefetch): c for c in _new_cands}
                        for _i, _f in enumerate(as_completed(_futs), 1):
                            _rr = _f.result()
                            if _rr:
                                _scored_new.append(_rr)
                            if _i % 20 == 0 or _i == len(_new_cands):
                                _pp = int(_i / len(_new_cands) * 100)
                                update_heartbeat("fund_recommend", progress=_i, total=len(_new_cands),
                                                 overall_pct=30 + int(_pp * 0.6), phase="评分",
                                                 detail=f"评分新增 {_i}/{len(_new_cands)} ({_pp}%)", elapsed=_elapsed())
                    print(f"   ✅ 新增评分完成: {len(_scored_new)} 只 ({time.time()-_t3:.1f}s)")
                _combined = _base + _scored_new
                _combined.sort(key=lambda x: x.get("score", 0), reverse=True)
                update_heartbeat("fund_recommend", progress=0, total=1, overall_pct=95,
                                 phase="保存", detail=f"合并保存 {len(_combined)} 只", elapsed=_elapsed())
                _supplement_self_selected(_combined)
                _final_c = len(_combined)
                _save_result(_combined)
                update_heartbeat("fund_recommend", progress=_final_c, total=_final_c, overall_pct=100,
                                 phase="完成", detail="推荐完成", elapsed=_elapsed())
                print(f"\n🏆 基金推荐 TOP {SHOW_TOP}")
                print("=" * 50)
                _print_results(_combined)
                print(f"\n📊 统计: 复用已评分 {len(_base)}只 + 新增 {len(_scored_new)}只 → {_final_c}只")
                print(f"⏱ 总耗时: {_elapsed()}s (重筛模式)")
                return

            if cache_mode == "full":
                if _HAS_TD:
                    _t1 = time.time()
                    print(f"\n📋 当日涨跌维度开启，刷新 {total_candidates} 只基金td值...")
                    update_heartbeat("fund_recommend", progress=0, total=total_candidates,
                                     overall_pct=0, phase="刷新涨跌",
                                     detail=f"获取 {total_candidates} 只基金当日涨跌", elapsed=_elapsed())
                    from fund_scoring import _calc_score as _calc_score2

                    all_codes = [r.get("code", "") for r in cached_results]
                    td_map = _batch_fetch_estimates([c for c in all_codes if c])
                    print(f"  td刷新完成 ({time.time()-_t1:.1f}s), 获取到 {len(td_map)} 只")

                    for idx, r in enumerate(cached_results):
                        code = r.get("code", "")
                        _td_item = td_map.get(code)
                        if _td_item:
                            td_val, td_src = _td_item
                            r["td"] = td_val
                            r["_td_src"] = td_src
                            r["day"] = f"{td_val:+.2f}%" if td_val is not None else ""
                            if td_val is not None:
                                r["score"] = _calc_score2(r)
                        if (idx + 1) % 200 == 0:
                            opct = 95 + (idx + 1) / total_candidates * 4
                            update_heartbeat("fund_recommend", progress=idx + 1, total=total_candidates,
                                             overall_pct=opct, phase="评分",
                                             detail=f"重算评分 {idx+1}/{total_candidates}",
                                             elapsed=_elapsed())

                    cached_results.sort(key=lambda x: x.get("score", 0), reverse=True)
                else:
                    cached_results.sort(key=lambda x: x.get("score", 0), reverse=True)

                print(f"\n💾 保存缓存结果...")
                # 先补充自选基金再保存，确保最终数量与评分阶段一致
                _supplement_self_selected(cached_results, td_map=td_map if _HAS_TD else None)
                _final_count = len(cached_results)
                update_heartbeat("fund_recommend", progress=_final_count, total=_final_count,
                                 overall_pct=97, phase="保存",
                                 detail=f"保存 {_final_count} 只结果", elapsed=_elapsed())
                _save_result(cached_results)
                update_heartbeat("fund_recommend", progress=_final_count, total=_final_count,
                                 overall_pct=100, phase="完成",
                                 detail="推荐完成", elapsed=_elapsed())
                print(f"🏆 基金推荐 TOP {SHOW_TOP}")
                print("=" * 50)
                _print_results(cached_results)
                print(f"\n⏱ 总耗时: {_elapsed()}s (缓存模式)")
                return
            else:
                _re_score_and_refresh(cached_results, total_candidates)
                print(f"\n⏱ 总耗时: {_elapsed()}s (重评模式)")
                return

        # ── 全量运行 ──
        _t1 = time.time()
        print(f"\n📥 阶段1/5: 获取全市场基金排行 (TOP {_TOP})...")
        update_heartbeat("fund_recommend", progress=0, total=_TOP,
                         overall_pct=0, phase="获取排行",
                         detail=f"拉取排行 API top {_TOP}", elapsed=_elapsed())
        rows = _fetch_rank_list(_TOP)
        rows_count = len(rows)
        print(f"   ✅ API 返回 {rows_count} 只基金 ({time.time()-_t1:.1f}s)")
        update_heartbeat("fund_recommend", progress=_TOP, total=_TOP,
                         overall_pct=2, phase="获取排行",
                         detail=f"排行API返回 {rows_count} 只", elapsed=_elapsed())

        _t2 = time.time()
        print(f"\n📊 阶段2/5: 初筛 (多条件筛选)...")
        update_heartbeat("fund_recommend", progress=0, total=rows_count,
                         overall_pct=2, phase="初筛",
                         detail=f"按 {len(_FILTER_CONDITIONS)} 个条件筛选 {rows_count} 只", elapsed=_elapsed())
        candidates = _filter_candidates(rows)
        candidates_count = len(candidates)
        print(f"   ✅ 多条件筛选后: {candidates_count} 只 ({time.time()-_t2:.1f}s)")
        if not candidates:
            print("   ⚠️ 无候选基金，请降低最低年化收益门槛")
            update_heartbeat("fund_recommend", progress=0, total=0,
                             overall_pct=100, phase="完成",
                             detail="无候选基金", elapsed=_elapsed())
            return
        update_heartbeat("fund_recommend", progress=rows_count, total=rows_count,
                         overall_pct=3, phase="初筛",
                         detail=f"初筛通过 {candidates_count} 只", elapsed=_elapsed())

        # ── 并行评分 ──
        scored: list[dict] = []
        total = len(candidates)
        est_min = total * 2 // 60
        _t4 = time.time()
        print(f"\n🧮 阶段4/5: 并行评分 ({total} 只基金, 预计 ~{est_min} 分钟)")
        print(f"   数据来源: pingzhongdata (~400KB/只, 50线程)")
        update_heartbeat("fund_recommend", progress=0, total=total,
                         overall_pct=15, phase="评分",
                         detail=f"启动评分: {total} 只, {50}线程", elapsed=_elapsed())

        print(f"\n{'进度':<8} {'代码':<7} {'基金名':<20} {'年化':<8} {'评分':<6} {'耗时':<7}")
        print("-" * 65)

        # 批量预取所有候选当日涨跌（盘中合并持仓行情填缓存；_fetch_one_td 只做 1 次估算，
        # 避免 _score_one 每只独立发 3 个网络请求导致评分阶段被拖慢）
        _td_prefetch: dict[str, tuple[float, str]] = {}
        try:
            print(f"   ⚡ 批量预取 {total} 只当日涨跌...", flush=True)
            update_heartbeat("fund_recommend", progress=0, total=total,
                             overall_pct=15, phase="评分",
                             detail=f"批量预取 {total} 只当日涨跌", elapsed=_elapsed())
            _td_prefetch = _batch_fetch_estimates([c["code"] for c in candidates], pct_base=15)
            print(f"   ✅ 预取完成: {len(_td_prefetch)} 只 ({time.time()-_t4:.1f}s)", flush=True)
            # 预取刚结束立即更新心跳，避免"获取当日涨跌 100%"后长时间无反馈的假卡顿
            update_heartbeat("fund_recommend", progress=0, total=total,
                             overall_pct=15, phase="评分",
                             detail=f"开始评分 {total} 只", elapsed=_elapsed())
        except Exception as _pf_exc:
            print(f"   ⚠️ 预取失败(评分兜底重试): {_pf_exc}", flush=True)

        with ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "recommend_scoring", default=50)) as executor:
            futs = {executor.submit(_score_one, c["code"], c["name"], c.get("_limit_amount"), _td_prefetch): c for c in candidates}
            for i, fut in enumerate(as_completed(futs), 1):
                c = futs[fut]
                result = fut.result()
                if result:
                    scored.append(result)
                    ar = result.get("annual_return")
                    ar_str = f"{ar:.1f}%" if isinstance(ar, (int, float)) else "?"
                    print(f"  {i}/{total:<4} {c['code']:<7} {c['name'][:18]:<20} {ar_str:<8} {result['score']:<6.1f} {time.time()-_t4:<7.1f}s")
                else:
                    print(f"  {i}/{total:<4} {c['code']:<7} {c['name'][:18]:<20} {'失败':<8} {'':6} {time.time()-_t4:<7.1f}s")
                # 定期把累积的净值走势写盘并清空，控制内存（候选多时避免OOM）
                if i % 500 == 0 or i == total:
                    _flush_trend_cache()
                pct = i / total * 100
                opct = 15 + i / total * 82
                # 评分心跳：显示进度+当前基金代码/名称+耗时（区分"网络慢"与"卡住"）
                _cur_code = c["code"]
                _cur_name = c.get("name", "")
                _cur_cost = time.time() - _t4
                _rate = (i / _cur_cost) if _cur_cost > 0 else 0  # 只/秒
                update_heartbeat("fund_recommend", progress=i, total=total,
                                 overall_pct=opct, phase="评分",
                                 detail=f"评分 {i}/{total} ({pct:.0f}%) {_cur_code} {_cur_name[:10]} · {_cur_cost:.0f}s/{_rate:.1f}只/秒",
                                 elapsed=_elapsed())

        print(f"\n   ✅ 评分完成: {len(scored)}/{total} 只成功 ({time.time()-_t4:.1f}s)")

        # ── 排序保存 ──
        _t5 = time.time()
        print(f"\n🧮 评分完成: {len(scored)}/{total} 只成功 ({total - len(scored)} 只无数据跳过) ({time.time()-_t4:.1f}s)")
        # 用实际评分成功数更新心跳，使前端显示与缓存一致
        update_heartbeat("fund_recommend", progress=len(scored), total=len(scored),
                         overall_pct=97, phase="评分",
                         detail=f"评分完成: {len(scored)} 只成功 ({total - len(scored)} 只无数据跳过)",
                         elapsed=_elapsed())
        print(f"\n💾 阶段5/5: 排序保存...")
        scored.sort(key=lambda x: x.get("score", 0), reverse=True)
        # 评分后限购检查：只检查排名靠前的候选（TOP 缓冲），避免对初筛后的
        # 全部候选逐只拉详情页（勾选筛限购时从几千只 → 几百只，大幅提速）。
        # 实测限购筛除率很低，TOP 候选基本都能入选；磁盘缓存 24h 复用二次更快。
        if _SKIP_LIMITED and scored:
            _t3 = time.time()
            from fund_watch import _parse_purchase_limit
            _limit_pool_size = max(SHOW_TOP * 10, 300)  # TOP 缓冲：展示数的 10 倍，至少 300
            _limit_dropped = 0

            def _check_limit_top(c: dict) -> dict:
                """检查单只限购：≤2万 → 移除；无限购/异常 → 保留"""
                try:
                    amount = _parse_purchase_limit(c["code"])
                    c["_limit_amount"] = amount
                    # 明确限购≤2万才筛掉；None(网络失败/无限购) 与异常都保留，避免误杀
                    if amount is not None and amount <= 2:
                        return None
                except Exception:
                    log.warning("限购检查异常(保留): %s", c.get("code"))
                return c

            # 并发检查 TOP 缓冲（网络 IO 密集），串行 300 只会很慢
            _top_pool = scored[:_limit_pool_size]
            with ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "recommend_limit_check", default=50)) as _le:
                _lfuts = {_le.submit(_check_limit_top, _c): _c for _c in _top_pool}
                _kept: list[dict] = []
                for _j, _lf in enumerate(as_completed(_lfuts), 1):
                    _r = _lf.result()
                    if _r:
                        _kept.append(_r)
                    else:
                        _limit_dropped += 1
                    if _j % 100 == 0 or _j == len(_top_pool):
                        update_heartbeat("fund_recommend", progress=_j, total=len(_top_pool),
                                         overall_pct=97, phase="限购",
                                         detail=f"限购检查 TOP {_j}/{len(_top_pool)}",
                                         elapsed=_elapsed())
            # 保留的 TOP 不限购候选 + 排名靠后未检查的候选（不影响展示，但保留完整结果）
            scored = _kept + scored[_limit_pool_size:]
            _t3b = time.time()
            print(f"   ✅ 限购检查 TOP {len(_top_pool)} 只: 筛掉 {_limit_dropped} 只, 保留 {len(_kept)} 只 ({_t3b-_t3:.1f}s)")
        # 先补充自选基金再保存，确保最终数量与评分阶段一致
        _supplement_self_selected(scored)
        _final_count = len(scored)
        # 把评分时拉取的净值走势批量写入共享缓存，供前端折线图直接复用
        _flush_trend_cache()
        # 把当日涨跌(td)固定值落盘，供同一天跨进程（重复推荐/页面）复用
        try:
            from fund_utils import flush_td_cache
            flush_td_cache()
        except Exception:
            pass
        update_heartbeat("fund_recommend", progress=_final_count, total=_final_count,
                         overall_pct=97, phase="保存",
                         detail=f"保存 {_final_count} 只结果到 {_RESULT_FILE}", elapsed=_elapsed())
        _save_result(scored)

        # 保存后立即增量回填新进榜基金的估算差异（只补无记录基金，秒级），
        # 避免新基金进榜后当天市场优选表无差异徽章（默认异步回填赶不上前端渲染）
        try:
            from fund_utils import backfill_estimate_errors
            _bf_t0 = time.time()
            _bf_n = backfill_estimate_errors(days=10)
            if _bf_n:
                print(f"   └─ 差异回填: {_bf_n} 条 ({time.time()-_bf_t0:.1f}s)")
        except Exception:
            pass

        print(f"\n🏆 基金推荐 TOP {SHOW_TOP}")
        print("=" * 50)
        _print_results(scored)
        print()
        print(f"📊 统计: 排行{_TOP}只 → 初筛{len(candidates)}只 → 评分{len(scored)}只 → 展示{SHOW_TOP}只")
        print(f"⏱ 总耗时: {_elapsed()}s")
        print(f"   ├─ 排行拉取: {_t2-_t1:.1f}s")
        if _SKIP_LIMITED:
            # 限购检查在评分后对 TOP 缓冲执行，_t3b 为其结束时间
            print(f"   ├─ 限购检查(评分后TOP): {_t3b-_t3:.1f}s")
        print(f"   ├─ 评分阶段: {_t5-_t4:.1f}s")
        print(f"   └─ 保存结果: {time.time()-_t5:.1f}s")
        # 统计同时写日志（server 启动时 stdout 被丢弃，日志确保耗时可回溯）
        log.info("推荐完成: 排行%d→初筛%d→评分%d→展示%d, 总耗时%.1fs (排行%.1fs/评分%.1fs/保存%.1fs)",
                 _TOP, len(candidates), len(scored), SHOW_TOP, _elapsed(),
                 _t2 - _t1, _t5 - _t4, time.time() - _t5)
    except Exception as _main_exc:
        _has_error = True
        import traceback
        _tb = traceback.format_exc()
        print(f"\n❌ 推荐过程异常: {_main_exc}", file=sys.stderr)
        print(_tb, file=sys.stderr)
        log.error("推荐过程异常", exc_info=True)
        update_heartbeat("fund_recommend", progress=0, total=0, overall_pct=100,
                         phase="失败", detail=str(_main_exc)[:200], error=str(_main_exc)[:200])
        raise
    finally:
        if _has_error:
            # 异常已在 except 中写入错误心跳，这里不再覆盖
            pass
        else:
            # 不传 total：保留前面保存阶段写入的实际基金数（避免覆盖成 1 导致前端显示"1只"）
            update_heartbeat("fund_recommend", progress=1, overall_pct=100,
                             phase="完成", detail="推荐完成", elapsed=_elapsed())
        if _timeout_count > 0:
            print(f"\n⚠️ 超时警告: {_timeout_count} 次请求超时")
            for _td in _timeout_details[:10]:
                print(f"   ⏱  {_td}")
        print(f"\n{'❌' if _has_error else '✅'} 推荐任务{'失败' if _has_error else '完成'} ({_elapsed()}s)")


if __name__ == "__main__":
    # CLI 参数处理
    if "--load" in sys.argv:
        results = _load_result()
        if results:
            print(f"\n候选基金: {len(results)} 只")
            _print_results(results)
        else:
            print("暂无候选数据")
    elif "--add" in sys.argv:
        idx = sys.argv.index("--add")
        if idx + 1 < len(sys.argv):
            code = sys.argv[idx + 1]
            # 从候选列表查名字
            results = _load_result() or []
            name = next((r.get("name", "") for r in results if r.get("code") == code), "")
            _add_to_fund_list(code, name)
        else:
            print("用法: python fund_recommend.py --add 基金代码")
    else:
        main()
