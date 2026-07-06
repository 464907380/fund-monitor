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
    from config import api_url, CFG
except ImportError:
    print("请先在 fund_watch.py 同一目录运行")
    sys.exit(1)
    sys.exit(1)

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
        for _ in range(30):
            try:
                with open(lock_file, "x") as _:
                    break
            except FileExistsError:
                time.sleep(1)
        else:
            print("⚠️ 无法获取文件锁，跳过保存")
            return False

        if not results:
            print("\n⚠️ 未找到匹配基金，保留上次结果")
            return False

        data = {
            "date": datetime.date.today().isoformat(),
            "config_hash": _config_hash(),
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


def _score_one(code: str, name: str) -> dict | None:
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
        # 筛掉单日限购≤2万的基金
        limit_amount: float | None = None
        if _SKIP_LIMITED:
            from fund_watch import _parse_purchase_limit
            limit_amount = _parse_purchase_limit(code)
            if limit_amount is not None and limit_amount <= 2:
                log.debug("跳过 %s(%s): 限购%.2f万", name, code, limit_amount)
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


def main() -> None:
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        # 检查缓存是否仍有效（配置未变+同一天）
        cur_hash = _config_hash()
        cache_valid = False
        if os.path.exists(_RESULT_FILE):
            try:
                with open(_RESULT_FILE, encoding="utf-8") as _f:
                    old = json.load(_f)
                if old.get("config_hash") == cur_hash and old.get("date") == datetime.date.today().isoformat():
                    print("📋 评分配置与筛选条件未变化，使用缓存结果（仅更新涨跌）")
                    cache_valid = True
            except Exception:
                pass

        if cache_valid:
            cached_results = old["results"]
            total_candidates = len(cached_results)

            if _HAS_TD:
                # 当日涨跌维度开启：缓存中td已过期，需刷新全部候选基金的td并重算评分
                from fund_scoring import _calc_score as _calc_score2
                print(f"📋 当日涨跌维度开启，刷新 {total_candidates} 只基金td值...")
                update_heartbeat("fund_recommend", progress=0, total=total_candidates, status="刷新td")

                def _fetch_td(code: str) -> tuple[str, float | None]:
                    try:
                        td = _fetch_fund_estimate(code)
                        return (code, td[1] if td else None)
                    except Exception:
                        return (code, None)

                td_map: dict[str, float | None] = {}
                with ThreadPoolExecutor(max_workers=30) as ex:
                    futs = {ex.submit(_fetch_td, r.get("code", "")): r for r in cached_results}
                    for i, fut in enumerate(as_completed(futs), 1):
                        code, td_val = fut.result()
                        td_map[code] = td_val
                        if i % 50 == 0:
                            update_heartbeat("fund_recommend", progress=i, total=total_candidates, status="td")

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
                with ThreadPoolExecutor(max_workers=20) as ex:
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

        # ── 并行评分 ──
        scored: list[dict] = []
        total = len(candidates)
        print(f"\n{'进度':<8} {'代码':<7} {'基金名':<20} {'年化':<8} {'评分':<6}")
        print("-" * 55)

        with ThreadPoolExecutor(max_workers=30) as executor:
            futs = {executor.submit(_score_one, c["code"], c["name"]): c for c in candidates}
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
                update_heartbeat("fund_recommend", progress=i, total=total + 1,
                                 status=f"评分 {c['name'][:18]}({c['code']})")

        # ── 排序保存 ──
        scored.sort(key=lambda x: x.get("score", 0), reverse=True)
        update_heartbeat("fund_recommend", progress=total + 1, total=total + 1, status="保存结果")
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
