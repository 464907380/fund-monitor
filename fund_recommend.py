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

from fund_utils import update_heartbeat

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
        from fund_watch import get as _get
        d = _get(code)
        if not d.get("n"):
            return None
        # 筛掉缺失收益数据
        if _SKIP_MISSING_PERF:
            perf_keys = ["m1", "m3", "y1", "f5", "sy6", "sy2", "sy3", "annual_return"]
            if any(d.get(k) is None or d.get(k) == "" or (k in ("sy3", "sy2") and d.get(k) == 0) for k in perf_keys):
                log.debug("跳过 %s(%s): 缺失收益维度", name, code)
                return None
        # 计算近一周涨跌幅
        navs = d.get("nav", [])
        f5_val = ""
        if len(navs) >= 5:
            pct = (navs[-1]["v"] - navs[-5]["v"]) / navs[-5]["v"] * 100
            f5_val = f"{pct:+.1f}%"
        d["f5"] = f5_val
        score = _calc_score(d)
        return {
            "code": code, "name": name, "score": score,
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
            "mgr": (d.get("mgr") or "")[:6],
            "day": f"{d.get('td'):+.2f}%" if d.get("td") is not None else "",
        }
    except Exception as e:
        log.debug("跳过 %s: %s", code, e)
        return None


def main() -> None:
    try:
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
