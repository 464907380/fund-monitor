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
from concurrent.futures import ThreadPoolExecutor, as_completed

from fund_utils import update_heartbeat, clear_heartbeat, _fetch_fund_estimate

try:
    from fund_watch import log, fetch
    from fund_scoring import _calc_score, SCORE_DIMS
    from config import api_url, CFG, get_timeout, get_config
except ImportError:
    print("请先在 fund_watch.py 同一目录运行")
    sys.exit(1)
    sys.exit(1)


# ── 批量实时估值（新浪行情接口，支持多只基金一次查询）───────

def _batch_fetch_estimates(codes: list[str]) -> dict[str, float]:
    """批量获取基金当日涨跌幅，返回 {code: 涨跌幅%, ...}
    
    先使用新浪财经批量行情接口快速获取估算值（每批最多200只）。
    收盘后（≥15:00）尝试用天天基金实际净值替换估算值，保证数据准确。
    """
    result: dict[str, float] = {}
    if not codes:
        return result
    batch_size = 200
    batches = [codes[i:i + batch_size] for i in range(0, len(codes), batch_size)]

    def _fetch_one(batch: list[str]) -> dict[str, float]:
        batch_result: dict[str, float] = {}
        try:
            codes_str = ",".join("of" + c for c in batch)
            url = "http://hq.sinajs.cn/list=" + codes_str
            _req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/",
            })
            with urllib.request.urlopen(_req, timeout=get_timeout("sina_quote", 15)) as _resp:
                raw = _resp.read()
            text = raw.decode("gbk", errors="ignore")
            # 解析每行: var hq_str_of000001="name,?,?,nav_price,pct,date,...";
            for line in text.strip().split("\n"):
                line = line.strip()
                if "hq_str_of" not in line:
                    continue
                parts = line.split('"')
                if len(parts) < 2:
                    continue
                fields = parts[1].split(",")
                if len(fields) < 5:
                    continue
                m = re.search(r'of(\d{6})', line[:30])
                code = m.group(1) if m else ""
                # fields[3]=基金净值, fields[4]=涨跌幅百分比
                if code and fields[4]:
                    try:
                        pct = float(fields[4])
                        batch_result[code] = pct
                    except ValueError:
                        pass
        except Exception:
            pass
        return batch_result

    with ThreadPoolExecutor(max_workers=min(5, len(batches))) as _ex:
        _futs = {_ex.submit(_fetch_one, b): b for b in batches}
        for _f in as_completed(_futs):
            result.update(_f.result())

    # 收盘后尝试用实际净值替换新浪估算值
    now = datetime.datetime.now()
    is_after_market = now.hour > 15 or (now.hour == 15 and now.minute >= 0)

    # 用 fundgz 实时估值替换新浪数据（盘中更准确）
    if result:
        def _fetch_fundgz(code: str) -> tuple[str, float | None]:
            try:
                url = f"https://fundgz.1234567.com.cn/js/{code}.js"
                _req_gz = urllib.request.Request(url, headers={
                    "Referer": "https://fund.eastmoney.com/",
                    "User-Agent": "Mozilla/5.0",
                })
                with urllib.request.urlopen(_req_gz, timeout=get_timeout("default", 10)) as _r_gz:
                    raw_gz = _r_gz.read().decode("utf-8")
                m = re.search(r'"gszzl":"([-+\d.]+)"', raw_gz)
                if m and m.group(1):
                    return (code, float(m.group(1)))
            except Exception:
                pass
            return (code, None)

        codes_list = list(result.keys())
        replaced_gz = 0
        _failed_codes: list[str] = []
        _total_gz = len(codes_list)
        _start_gz = time.time()
        _last_hb_pct = -1
        with ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "recommend_net_value", default=50)) as _ge:
            _gfuts = {_ge.submit(_fetch_fundgz, c): c for c in codes_list}
            for _gf in as_completed(_gfuts):
                code, gz_val = _gf.result()
                if gz_val is not None:
                    result[code] = gz_val
                    replaced_gz += 1
                else:
                    _failed_codes.append(code)
                _done = replaced_gz + len(_failed_codes)
                _pct = int(_done / _total_gz * 100) if _total_gz else 0
                if _pct != _last_hb_pct and _done % 50 == 0 or _done == _total_gz:
                    _last_hb_pct = _pct
                    update_heartbeat("fund_recommend", progress=_done, total=_total_gz,
                                     overall_pct=_pct, phase="刷新td",
                                     detail=f"拉取实时估值 {_done}/{_total_gz} ({_pct}%)",
                                     elapsed=round(time.time() - _start_gz, 1))
        if _failed_codes:
            # 失败的逐个重试（_fetch_fund_estimate 有多层降级）
            from fund_utils import _fetch_fund_estimate
            for _i, _code in enumerate(_failed_codes):
                _td = _fetch_fund_estimate(_code)
                if _td and _td[1] is not None:
                    result[_code] = round(_td[1], 2)
                    replaced_gz += 1
                _done2 = replaced_gz + len(_failed_codes)
                if (_i + 1) % 10 == 0 or _i + 1 == len(_failed_codes):
                    update_heartbeat("fund_recommend", progress=_done2, total=_total_gz,
                                     overall_pct=int(_done2 / _total_gz * 100), phase="刷新td",
                                     detail=f"重试 {_i+1}/{len(_failed_codes)} 失败基金",
                                     elapsed=round(time.time() - _start_gz, 1))

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
        with ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "recommend_net_value", default=50)) as _ae:
            _afuts = {_ae.submit(_fetch_actual, c): c for c in codes_list}
            for _af in as_completed(_afuts):
                code, actual_val = _af.result()
                if actual_val is not None:
                    result[code] = actual_val
                    replaced += 1
                if time.time() - _start > _max_dur:
                    break
        if replaced:
            log.info("收盘后实际净值替换: %d/%d 只基金(%.1fs)", replaced, len(codes_list), time.time()-_start)

    return result

# ── 配置 ──────────────────────────────────────
_TOP = CFG.get("recommend", {}).get("top_n", 200)
SHOW_TOP = CFG.get("recommend", {}).get("show_top", 20)
_SKIP_MISSING_PERF = CFG.get("recommend", {}).get("skip_missing_perf", False)
_SKIP_LIMITED = CFG.get("recommend", {}).get("skip_limited", False)
_HAS_TD = any(dim_name == "\u5f53\u65e5\u6da8\u8dcc" for dim_name, _, _, _ in SCORE_DIMS)
"""当日涨跌维度是否开启：开启时缓存命中后仍需刷新td值重新评分"""
_RANK_SORT = CFG.get("recommend", {}).get("rank_sort", "1n")
"""排行排序方式：1n=近1年收益, 6n=近6月收益, 3y=近3月收益, 1y=近1月收益"""
# 筛选条件（多条件组合）
_FILTER_CONDITIONS = CFG.get("recommend", {}).get("filter_conditions", [])
"""筛选条件列表：[{field, op, value}, ...]  field: y1/sy6/m3/m1/sy2/sy3"""
# 排行API字段映射
_RANK_FIELD_MAP = {
    "y1":  {"idx": 11, "name": "近1年收益"},
    "sy6": {"idx": 10, "name": "近6月收益"},
    "m3":  {"idx": 9,  "name": "近3月收益"},
    "m1":  {"idx": 8,  "name": "近1月收益"},
    "sy2": {"idx": 12, "name": "近2年收益"},
    "sy3": {"idx": 13, "name": "近3年收益"},
}
_RECOMMEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RESULT_FILE = os.path.join(_RECOMMEND_DIR, ".fund_recommend_result.json")
_FUND_LIST_FILE = os.path.join(_RECOMMEND_DIR, "data", "fund_list.json")

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


def _fetch_rank_list(pn: int) -> list[list[str]]:
    """从天天基金排行 API 获取全市场基金排行（并发多URL，走缓存）"""
    # 根据排序方式决定日期范围
    sort_days = {"1n": 365, "6n": 180, "3y": 90, "1y": 30}
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
            data = fetch(url, {"Referer": "https://fund.eastmoney.com/"})
            return _parse_rank_response(data)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(_try_one, url): url for url in urls[:2]}
        for f in as_completed(futs):
            rows = f.result()
            if rows:
                return rows

    for url in urls[2:]:
        rows = _try_one(url)
        if rows:
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


def _save_result(results: list[dict]) -> bool:
    """保存评分结果到文件"""
    lock_file = _RESULT_FILE + ".lock"
    try:
        for _ in range(get_config("recommend", "lock_retry_count", default=30)):
            try:
                with open(lock_file, "x") as _:
                    break
            except FileExistsError:
                time.sleep(get_config("recommend", "lock_retry_interval", default=1.0))
        else:
            print("⚠️ 无法获取文件锁，跳过保存")
            return False

        if not results:
            print("\n⚠️ 未找到匹配基金，保留上次结果")
            return False

        data = {
            "date": datetime.date.today().isoformat(),
            "config_hash": _config_hash(),
            "filter_hash": _filter_hash(),
            "results": results,
        }
        with open(_RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n📁 已保存 {len(results)} 只基金评分结果到 {_RESULT_FILE}")
        return True
    finally:
        try:
            os.remove(lock_file)
        except OSError:
            pass


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


def _score_one(code: str, name: str, limit_amount: float | None = None) -> dict | None:
    """单只基金评分"""
    try:
        from fund_watch import get_scoring_data as _get
        d = _get(code)
        if not d.get("n"):
            return None
        # 计算近一周涨跌幅（需在缺失检查前计算，因为 f5 不在原始数据中）
        navs = d.get("nav", [])
        f5_val = ""
        if len(navs) >= 5:
            pct = (navs[-1]["v"] - navs[-5]["v"]) / navs[-5]["v"] * 100
            f5_val = f"{pct:+.1f}%"
        d["f5"] = f5_val
        # 筛掉缺失收益数据
        if _SKIP_MISSING_PERF:
            perf_keys = ["m1", "m3", "y1", "f5", "sy6", "sy2", "sy3", "annual_return"]
            if any(d.get(k) is None or d.get(k) == "" or (k in ("sy3", "sy2") and d.get(k) == 0) for k in perf_keys):
                log.debug("跳过 %s(%s): 缺失收益维度", name, code)
                return None
        score = _calc_score(d)
        # 获取当日涨跌（供td维度评分）
        td = _fetch_fund_estimate(code)
        if td is not None:
            d["td"] = round(td[1], 2)
            day_str = f"{td[1]:+.2f}%"
        else:
            # 无实时数据时从净值算最近交易日涨跌
            navs_local = d.get("nav", [])
            if navs_local and len(navs_local) >= 2:
                td_val = (navs_local[-1]["v"] - navs_local[-2]["v"]) / navs_local[-2]["v"] * 100
                d["td"] = td_val
                day_str = f"{td_val:+.2f}%"
            else:
                day_str = ""
        score = _calc_score(d)  # 带td值重新评分
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
            "mgr": (d.get("mgr") or "")[:6],
            "day": day_str,
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
    update_heartbeat("fund_recommend", progress=0, total=total, phase="重新评分",
                     detail=f"重新评分 {total} 只", elapsed=0)

    for i, r in enumerate(cached_results):
        r["score"] = _calc_score2(r)
        if (i + 1) % 200 == 0:
            pct = (i + 1) / total * 100
            update_heartbeat("fund_recommend", progress=i + 1, total=total, phase="重新评分",
                             detail=f"重评 {i+1}/{total} ({pct:.0f}%)", elapsed=round(time.time() - _t, 1))

    print(f"  重评完成 ({time.time()-_t:.1f}s)")
    cached_results.sort(key=lambda x: x.get("score", 0), reverse=True)

    if _HAS_TD:
        _t2 = time.time()
        print(f"📋 当日涨跌维度开启，刷新 {total} 只基金td值...")
        update_heartbeat("fund_recommend", progress=0, total=total, phase="刷新td",
                         detail=f"批量获取 {total} 只基金实时涨跌", elapsed=round(time.time() - _t, 1))
        all_codes = [r.get("code", "") for r in cached_results]
        td_map = _batch_fetch_estimates([c for c in all_codes if c])
        print(f"  td刷新完成 ({time.time()-_t2:.1f}s), 获取到 {len(td_map)} 只")
        for r in cached_results:
            code = r.get("code", "")
            td_val = td_map.get(code)
            r["td"] = td_val
            r["day"] = f"{td_val:+.2f}%" if td_val is not None else ""
            if td_val is not None:
                r["score"] = _calc_score2(r)
        cached_results.sort(key=lambda x: x.get("score", 0), reverse=True)
    else:
        _t2 = time.time()
        print(f"📋 刷新前 {SHOW_TOP} 只显示涨跌...")
        update_heartbeat("fund_recommend", progress=0, total=SHOW_TOP, phase="更新涨跌",
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


def _supplement_self_selected() -> None:
    """补拉自选基金数据到推荐结果文件"""
    try:
        _fund_list_file = os.path.join(_RECOMMEND_DIR, "data", "fund_list.json")
        if not os.path.exists(_fund_list_file):
            return
        with open(_fund_list_file, encoding="utf-8") as _fl:
            _fl_data = json.load(_fl)
        if not _fl_data:
            return
        _old = _load_result()
        _existing = {r["code"] for r in _old} if _old else set()
        _missing = [f for f in _fl_data if f["code"] not in _existing]
        if not _missing:
            return
        print(f"\n📋 补拉 {len(_missing)} 只自选基金数据...")
        _extra = []
        for _f in _missing:
            _r = _score_one(_f["code"], _f.get("name", ""))
            if _r:
                # 检查是否满足当前筛选条件
                _pass = True
                for _cond in _FILTER_CONDITIONS:
                    _fld = _cond.get("field", "")
                    _op = _cond.get("op", "gte")
                    _val = _cond.get("value")
                    if _val is None or _fld not in _RANK_FIELD_MAP:
                        continue
                    _raw = _r.get(_fld)
                    if _raw is None:
                        _pass = False
                        break
                    try:
                        if _op == "gte" and not (float(_raw) >= _val):
                            _pass = False
                            break
                        elif _op == "lte" and not (float(_raw) <= _val):
                            _pass = False
                            break
                    except (ValueError, TypeError):
                        _pass = False
                        break
                if _pass:
                    _extra.append(_r)
                    print(f"  ✅ {_f['code']} {_r['name']} — {_r['score']:.1f}分")
                else:
                    print(f"  ⏭️ {_f['code']} {_r.get('name','')} — 不满足筛选条件，跳过")
        if not _extra:
            return
        _old_list = _old or []
        _old_list.extend(_extra)
        _old_list.sort(key=lambda x: x.get("score", 0), reverse=True)
        _save_result(_old_list)
        print(f"  已补入 {len(_extra)} 只自选基金，重新保存")
    except Exception as _e:
        print(f"⚠️ 补拉自选基金数据失败: {_e}")


def main() -> None:
    _t0 = time.time()  # 全局计时起点

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
                    if old.get("config_hash") == cur_hash:
                        print(f"✅ 评分配置与筛选条件未变化 ({_elapsed()}s)")
                        print(f"   使用缓存结果（仅更新涨跌）")
                        cache_mode = "full"
                    elif old.get("filter_hash") == cur_filter_hash:
                        print(f"⚠️ 筛选条件未变化，评分权重变更 ({_elapsed()}s)")
                        print(f"   使用缓存数据重新评分（保留候选基金列表）")
                        cache_mode = "re-score"
                    else:
                        print(f"🔄 筛选参数已变化 ({_elapsed()}s)")
                        print(f"   全量重新拉取排行和评分")
                else:
                    if old.get("config_hash") == cur_hash:
                        print(f"📅 缓存日期 ({saved_date}) 与今天不同，但配置未变化")
                        print(f"   使用缓存结果（仅更新涨跌）")
                        cache_mode = "full"
                    elif old.get("filter_hash") == cur_filter_hash:
                        print(f"📅 缓存日期 ({saved_date}) 与今天不同，但筛选条件未变化")
                        print(f"   使用缓存数据重新评分")
                        cache_mode = "re-score"
                    else:
                        print(f"🔄 缓存日期 ({saved_date}) ≠ 今天 ({datetime.date.today()})")
                        print(f"   筛选参数已变化，全量重新拉取排行和评分")
            except Exception as e:
                print(f"⚠️ 缓存读取失败: {e}，全量重新运行")

        if cache_mode:
            cached_results = old["results"]
            total_candidates = len(cached_results)
            print(f"   候选基金: {total_candidates} 只")

            if cache_mode == "full":
                if _HAS_TD:
                    _t1 = time.time()
                    print(f"\n📋 当日涨跌维度开启，刷新 {total_candidates} 只基金td值...")
                    update_heartbeat("fund_recommend", progress=0, total=total_candidates,
                                     overall_pct=0, phase="刷新td",
                                     detail=f"批量获取 {total_candidates} 只实时涨跌", elapsed=_elapsed())
                    from fund_scoring import _calc_score as _calc_score2

                    all_codes = [r.get("code", "") for r in cached_results]
                    td_map = _batch_fetch_estimates([c for c in all_codes if c])
                    print(f"  td刷新完成 ({time.time()-_t1:.1f}s), 获取到 {len(td_map)} 只")

                    for idx, r in enumerate(cached_results):
                        code = r.get("code", "")
                        td_val = td_map.get(code)
                        r["td"] = td_val
                        r["day"] = f"{td_val:+.2f}%" if td_val is not None else ""
                        if td_val is not None:
                            r["score"] = _calc_score2(r)
                        if (idx + 1) % 200 == 0:
                            opct = (idx + 1) / total_candidates * 95
                            update_heartbeat("fund_recommend", progress=idx + 1, total=total_candidates,
                                             overall_pct=opct, phase="评分",
                                             detail=f"重算评分 {idx+1}/{total_candidates}",
                                             elapsed=_elapsed())

                    cached_results.sort(key=lambda x: x.get("score", 0), reverse=True)
                else:
                    _t1 = time.time()
                    print(f"\n📋 刷新前 {SHOW_TOP} 只显示涨跌 (td维度未开启)...")
                    update_heartbeat("fund_recommend", progress=0, total=SHOW_TOP,
                                     overall_pct=0, phase="更新涨跌",
                                     detail=f"并行获取前 {SHOW_TOP} 只实时涨跌", elapsed=_elapsed())

                    day_map: dict[str, str] = {}
                    with ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "recommend_update_day", default=50)) as ex:
                        def _update_day(code: str) -> tuple[str, str]:
                            try:
                                td = _fetch_fund_estimate(code)
                                if td is not None:
                                    return (code, f"{td[1]:+.2f}%")
                            except Exception:
                                pass
                            return (code, "")

                        futs = {ex.submit(_update_day, r.get("code", "")): r for r in cached_results[:SHOW_TOP]}
                        for i, fut in enumerate(as_completed(futs), 1):
                            code, day = fut.result()
                            day_map[code] = day
                            opct = i / SHOW_TOP * 95
                            update_heartbeat("fund_recommend", progress=i, total=SHOW_TOP,
                                             overall_pct=opct, phase="涨跌",
                                             detail=f"刷新 {i}/{SHOW_TOP}", elapsed=_elapsed())

                    print(f"  涨跌刷新完成 ({time.time()-_t1:.1f}s)")
                    for r in cached_results:
                        code = r.get("code", "")
                        if code in day_map:
                            r["day"] = day_map[code]

                    cached_results.sort(key=lambda x: x.get("score", 0), reverse=True)

                print(f"\n💾 保存缓存结果...")
                update_heartbeat("fund_recommend", progress=total_candidates, total=total_candidates,
                                 overall_pct=97, phase="保存",
                                 detail=f"保存 {total_candidates} 只结果", elapsed=_elapsed())
                _save_result(cached_results)
                update_heartbeat("fund_recommend", progress=total_candidates, total=total_candidates,
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

        # ── 限购检查 ──
        limit_before = candidates_count
        if _SKIP_LIMITED and candidates:
            _t3 = time.time()
            print(f"\n🔒 阶段3/5: 限购检查 ({limit_before} 只)...")
            update_heartbeat("fund_recommend", progress=0, total=limit_before,
                             overall_pct=3, phase="限购",
                             detail=f"检查 {limit_before} 只限购", elapsed=_elapsed())
            from fund_watch import _parse_purchase_limit
            limit_checked: list[dict] = []

            def _check_limit(c: dict) -> dict | None:
                amount = _parse_purchase_limit(c["code"])
                if amount is not None and amount <= 2:
                    return None
                c["_limit_amount"] = amount
                return c

            with ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "recommend_limit_check", default=50)) as _le:
                _lfuts = {_le.submit(_check_limit, c): c for c in candidates}
                for _j, _lf in enumerate(as_completed(_lfuts), 1):
                    _r = _lf.result()
                    if _r:
                        limit_checked.append(_r)
                    if _j % 50 == 0 or _j == limit_before:
                        opct = 3 + _j / limit_before * 12
                        update_heartbeat("fund_recommend", progress=_j, total=limit_before,
                                         overall_pct=opct, phase="限购",
                                         detail=f"限购检查 {_j}/{limit_before}",
                                         elapsed=_elapsed())

            candidates = limit_checked
            print(f"   ✅ 限购筛掉 {limit_before - len(candidates)} 只, 剩余 {len(candidates)} 只 ({time.time()-_t3:.1f}s)")

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

        with ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "recommend_scoring", default=50)) as executor:
            futs = {executor.submit(_score_one, c["code"], c["name"], c.get("_limit_amount")): c for c in candidates}
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
                pct = i / total * 100
                opct = 15 + i / total * 82
                update_heartbeat("fund_recommend", progress=i, total=total,
                                 overall_pct=opct, phase="评分",
                                 detail=f"评分 {i}/{total} ({pct:.0f}%) {c['name'][:12]}",
                                 elapsed=_elapsed())

        print(f"\n   ✅ 评分完成: {len(scored)}/{total} 只成功 ({time.time()-_t4:.1f}s)")

        # ── 排序保存 ──
        _t5 = time.time()
        print(f"\n💾 阶段5/5: 排序保存...")
        scored.sort(key=lambda x: x.get("score", 0), reverse=True)
        update_heartbeat("fund_recommend", progress=total, total=total,
                         overall_pct=97, phase="保存",
                         detail=f"保存 {len(scored)} 只结果到 {_RESULT_FILE}", elapsed=_elapsed())
        _save_result(scored)

        print(f"\n🏆 基金推荐 TOP {SHOW_TOP}")
        print("=" * 50)
        _print_results(scored)
        print()
        print(f"📊 统计: 排行{_TOP}只 → 初筛{len(candidates)}只 → 评分{len(scored)}只 → 展示{SHOW_TOP}只")
        print(f"⏱ 总耗时: {_elapsed()}s")
        print(f"   ├─ 排行拉取: {_t2-_t1:.1f}s")
        if _SKIP_LIMITED:
            print(f"   ├─ 限购检查: {_t4-_t3:.1f}s (如有此阶段)")
        print(f"   ├─ 评分阶段: {_t5-_t4:.1f}s")
        print(f"   └─ 保存结果: {time.time()-_t5:.1f}s")
    finally:
        _supplement_self_selected()
        clear_heartbeat("fund_recommend")
        print(f"\n✅ 推荐任务完成 ({_elapsed()}s)")


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
