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

from fund_utils import update_heartbeat, _fetch_fund_estimate

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
            # 解析每行: var hq_str_of000001="name,?,?,pct,date,...";
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
                if code and fields[3]:
                    try:
                        pct = float(fields[3])
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
_MIN_Y1 = CFG.get("recommend", {}).get("min_y1_return", 20)
_SKIP_MISSING_PERF = CFG.get("recommend", {}).get("skip_missing_perf", False)
_SKIP_LIMITED = CFG.get("recommend", {}).get("skip_limited", False)
_HAS_TD = any(dim_name == "\u5f53\u65e5\u6da8\u8dcc" for dim_name, _, _, _ in SCORE_DIMS)
"""当日涨跌维度是否开启：开启时缓存命中后仍需刷新td值重新评分"""
_RESULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fund_recommend_result.json")
_FUND_LIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fund_list.json")


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
    sd = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
    ed = datetime.date.today().isoformat()
    urls = [
        api_url("fund_rank") + f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc=1yz&st=desc"
                              f"&sd={sd}&ed={ed}&pi=1&pn={pn}&dx=1",
        api_url("fund_rank") + f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc=1n&st=desc"
                              f"&sd={sd}&ed={ed}&pi=1&pn={pn}",
        "http://fund.eastmoney.com/data/rankhandler.aspx" + f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc=1yz&st=desc"
                              f"&sd={sd}&ed={ed}&pi=1&pn={pn}&dx=1",
        "http://fund.eastmoney.com/data/rankhandler.aspx" + f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc=1n&st=desc"
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
    """根据配置筛选候选基金，返回 [{code, name, y1}, ...]"""
    candidates = []
    for r in rows:
        try:
            code = r[0]
            name = r[1]
            y1 = float(r[11]) if len(r) > 11 and r[11] else 0
            if y1 < _MIN_Y1:
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
        str(_TOP), str(_MIN_Y1), str(_SKIP_MISSING_PERF), str(_SKIP_LIMITED),
    ]
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _config_hash() -> str:
    """计算当前配置的哈希值，用于检测评分/筛选参数是否变化"""
    import hashlib
    from fund_scoring import SCORE_DIMS
    parts = [
        _CONFIG_VERSION,
        str(_TOP), str(_MIN_Y1), str(SHOW_TOP), str(_SKIP_MISSING_PERF), str(_SKIP_LIMITED),
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
            d["td"] = td[1]
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
    print(f"📋 重新评分 {total_candidates} 只基金（新权重）...")
    update_heartbeat("fund_recommend", progress=0, total=total_candidates, status="重新评分")

    for i, r in enumerate(cached_results):
        r["score"] = _calc_score2(r)
        if (i + 1) % 200 == 0:
            update_heartbeat("fund_recommend", progress=i + 1, total=total_candidates, status="重新评分")

    cached_results.sort(key=lambda x: x.get("score", 0), reverse=True)

    if _HAS_TD:
        # 刷新全部td并重算评分
        print(f"📋 当日涨跌维度开启，刷新 {total_candidates} 只基金td值...")
        update_heartbeat("fund_recommend", progress=0, total=total_candidates, status="刷新td")
        all_codes = [r.get("code", "") for r in cached_results]
        td_map = _batch_fetch_estimates([c for c in all_codes if c])
        for r in cached_results:
            code = r.get("code", "")
            td_val = td_map.get(code)
            r["td"] = td_val
            r["day"] = f"{td_val:+.2f}%" if td_val is not None else ""
            if td_val is not None:
                r["score"] = _calc_score2(r)
        cached_results.sort(key=lambda x: x.get("score", 0), reverse=True)
    else:
        # 只刷新前SHOW_TOP只的显示涨跌
        update_heartbeat("fund_recommend", progress=0, total=SHOW_TOP, status="更新涨跌")

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
                update_heartbeat("fund_recommend", progress=i, total=SHOW_TOP, status="涨跌")

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


def main() -> None:
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        # 三级缓存检查
        cur_hash = _config_hash()
        cur_filter_hash = _filter_hash()
        cache_mode = None  # "full": 全命中, "re-score": 仅权重变, None: 全量
        if os.path.exists(_RESULT_FILE):
            try:
                with open(_RESULT_FILE, encoding="utf-8") as _f:
                    old = json.load(_f)
                saved_date = old.get("date")
                if saved_date == datetime.date.today().isoformat():
                    if old.get("config_hash") == cur_hash:
                        print("📋 评分配置与筛选条件未变化，使用缓存结果（仅更新涨跌）")
                        cache_mode = "full"
                    elif old.get("filter_hash") == cur_filter_hash:
                        print("📋 筛选条件未变化，使用缓存数据重新评分（仅权重变更）")
                        cache_mode = "re-score"
            except Exception:
                pass

        if cache_mode:
            cached_results = old["results"]
            total_candidates = len(cached_results)

            if cache_mode == "full":
                if _HAS_TD:
                    # 当日涨跌维度开启：批量刷新全部候选基金的td并重算评分
                    from fund_scoring import _calc_score as _calc_score2
                    print(f"📋 当日涨跌维度开启，刷新 {total_candidates} 只基金td值...")
                    update_heartbeat("fund_recommend", progress=0, total=total_candidates, status="刷新td")

                    all_codes = [r.get("code", "") for r in cached_results]
                    td_map = _batch_fetch_estimates([c for c in all_codes if c])

                    for r in cached_results:
                        code = r.get("code", "")
                        td_val = td_map.get(code)
                        r["td"] = td_val
                        r["day"] = f"{td_val:+.2f}%" if td_val is not None else ""
                        if td_val is not None:
                            r["score"] = _calc_score2(r)

                    cached_results.sort(key=lambda x: x.get("score", 0), reverse=True)
                else:
                    # TD关闭：只刷新前SHOW_TOP只的显示涨跌
                    update_heartbeat("fund_recommend", progress=0, total=SHOW_TOP, status="更新涨跌")

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
                            update_heartbeat("fund_recommend", progress=i, total=SHOW_TOP, status="涨跌")

                    for r in cached_results:
                        code = r.get("code", "")
                        if code in day_map:
                            r["day"] = day_map[code]

                    cached_results.sort(key=lambda x: x.get("score", 0), reverse=True)
            else:
                # re-score: 仅权重变更，复用缓存数据重新评分（函数内已保存+打印）
                _re_score_and_refresh(cached_results, total_candidates)
                return

            update_heartbeat("fund_recommend", progress=total_candidates, total=total_candidates, status="保存")
            _save_result(cached_results)
            print(f"\n🏆 基金推荐 TOP {SHOW_TOP}")
            print("=" * 50)
            _print_results(cached_results)
            return

        print("=" * 60)
        print("🔍 基金优选推荐 — 全市场深度评分")
        print("=" * 60)

        print(f"\n📥 获取全市场基金排行 (TOP {_TOP})...")
        update_heartbeat("fund_recommend", progress=0, total=_TOP, status="获取排行榜")
        rows = _fetch_rank_list(_TOP)
        print(f"   获取到 {len(rows)} 只基金")

        candidates = _filter_candidates(rows)
        print(f"   y1 >= {_MIN_Y1}% 筛选后: {len(candidates)} 只")
        est_min = len(candidates) * 2 // 60
        print(f"   ⏱ 预计评分耗时约 {est_min} 分钟")

        # ── 限购检查（前置到评分前，批量并行）──
        if _SKIP_LIMITED and candidates:
            from fund_watch import _parse_purchase_limit
            print("   🔒 检查限购...")
            update_heartbeat("fund_recommend", progress=0, total=len(candidates), status="限购")
            limit_checked: list[dict] = []

            def _check_limit(c: dict) -> dict | None:
                amount = _parse_purchase_limit(c["code"])
                if amount is not None and amount <= 2:
                    log.debug("跳过 %s(%s): 限购%.2f万", c["name"], c["code"], amount)
                    return None
                c["_limit_amount"] = amount
                return c

            with ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "recommend_limit_check", default=50)) as _le:
                _lfuts = {_le.submit(_check_limit, c): c for c in candidates}
                for _j, _lf in enumerate(as_completed(_lfuts), 1):
                    _r = _lf.result()
                    if _r:
                        limit_checked.append(_r)
                    if _j % 100 == 0:
                        update_heartbeat("fund_recommend", progress=_j, total=len(candidates), status="限购")

            candidates = limit_checked
            print(f"   限购筛选后: {len(candidates)} 只")

        # ── 并行评分 ──
        scored: list[dict] = []
        total = len(candidates)
        print(f"\n{'进度':<8} {'代码':<7} {'基金名':<20} {'年化':<8} {'评分':<6}")
        print("-" * 55)

        with ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "recommend_scoring", default=50)) as executor:
            futs = {executor.submit(_score_one, c["code"], c["name"], c.get("_limit_amount")): c for c in candidates}
            for i, fut in enumerate(as_completed(futs), 1):
                c = futs[fut]
                result = fut.result()
                if result:
                    scored.append(result)
                    ar = result.get("annual_return")
                    ar_str = f"{ar:.1f}%" if isinstance(ar, (int, float)) else "?"
                    print(f"  {i}/{total:<4} {c['code']:<7} {c['name'][:18]:<20} {ar_str:<8} {result['score']:<6.1f}")
                else:
                    print(f"  {i}/{total:<4} {c['code']:<7} {c['name'][:18]:<20} {'跳过':<8}")
                update_heartbeat("fund_recommend", progress=i, total=total,
                                 status=f"评分 {c['name'][:18]}({c['code']})")

        # ── 排序保存 ──
        scored.sort(key=lambda x: x.get("score", 0), reverse=True)
        update_heartbeat("fund_recommend", progress=total, total=total, status="保存结果")
        _save_result(scored)

        print(f"\n🏆 基金推荐 TOP {SHOW_TOP}")
        print("=" * 50)
        _print_results(scored)
        print()
        print("💡 一键加入监控: python fund_recommend.py --add 基金代码")
    finally:
        print("  推荐任务完成")


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
