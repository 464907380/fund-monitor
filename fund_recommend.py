"""
基金推荐工具 — 从全市场筛选优质基金

策略：
  1. 从天天基金排行 API 拉取全市场近 1 年收益排行 TOP 500（不限类型）
  2. 近 1 年收益为负的剔除，其余全部进入 12 维深度评分
  3. 输出 TOP 10 推荐，支持保存结果

用法：
  python fund_recommend.py                    # 全市场 TOP 500 深度评分
  python fund_recommend.py --load             # 查看上次推荐结果
  python fund_recommend.py --add 基金代码     # 将基金加入 fund_list.json
"""
import sys
import json
import re
import urllib.request
import datetime
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from fund_watch import get, log, fetch
    from fund_scoring import _calc_score, SCORE_DIMS
    from config import api_url
except ImportError:
    print("请先在 fund_watch.py 同一目录运行")
    sys.exit(1)

# ── 配置 ──────────────────────────────────────
_TOP = 500
SHOW_TOP = 10
_RESULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fund_recommend_result.json")
_FUND_LIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fund_list.json")


def _fetch_rank_list(pn: int) -> list[list[str]]:
    """从天天基金排行 API 获取全市场基金排行（多URL备选）"""
    sd = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
    ed = datetime.date.today().isoformat()
    urls = [
        api_url("fund_rank") + f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc=1yz&st=desc"
                              f"&sd={sd}&ed={ed}&pi=1&pn={pn}&dx=1",
        api_url("fund_rank") + f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc=1n&st=desc"
                              f"&sd={sd}&ed={ed}&pi=1&pn={pn}",
    ]
    for url in urls:
        data = fetch(url)
        if not data:
            continue
        try:
            raw = data.replace("var rankData = ", "", 1).rstrip(";")
            raw_clean = re.sub(r'(\{|,)\s*(\w+)\s*:', lambda m: m.group(1) + '"' + m.group(2) + '":', raw)
            result = json.loads(raw_clean)
            rows = [row.split(",") for row in result.get("datas", [])]
            if rows:
                return rows
        except (json.JSONDecodeError, KeyError, IndexError):
            continue
    return []


def _save_result(scored: list[tuple]) -> None:
    """保存推荐结果到文件"""
    data = {
        "date": datetime.date.today().isoformat(),
        "results": [
            {"code": item[1], "name": item[2], "score": item[0],
             "annual_return": item[3],
             "m1": item[4], "m3": item[5], "y1": item[6],
             "sharpe": item[7], "sortino": item[8],
             "max_dd": item[9], "win_rate": item[10], "inst": item[11],
             "sc": item[12], "rate": item[13], "profit_ratio": item[14],
             "recovery": item[15], "sy3": item[16]}
            for item in scored
        ]
    }
    with open(_RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n📁 结果已保存到 {_RESULT_FILE}")


def _load_result() -> list[dict] | None:
    """加载上次推荐结果"""
    if not os.path.exists(_RESULT_FILE):
        return None
    try:
        with open(_RESULT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        print(f"📁 上次推荐结果 ({data.get('date', '未知日期')})")
        return data.get("results", [])  # type: ignore[no-any-return]
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
        # 检查是否已存在
        for item in fl:
            if item["code"] == code:
                print(f"⚠️  {code}({name}) 已在 fund_list.json 中")
                return True
        fl.append({"code": code})
        with open(_FUND_LIST_FILE, "w", encoding="utf-8") as f:
            json.dump(fl, f, ensure_ascii=False, indent=2)
        print(f"✅ 已加入监控: {code}({name})")
        print(f"   下次运行基金晚报会自动追踪")
        return True
    except (json.JSONDecodeError, OSError) as e:
        print(f"❌ 写入失败: {e}")
        return False


def _print_results(results: list[dict], show_detail: bool = True) -> None:
    """打印推荐结果"""
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(results[:SHOW_TOP], 1):
        badge = medals[i - 1] if i <= 3 else f" {i}."
        ar = r.get("annual_return", r.get("ar_str", 0))
        print(f"{badge} {r['name']} ({r['code']}) — {r['score']:.1f}分  年化{ar:.1f}%")

    if show_detail:
        print()
        parts = [f"{name} {int(w*100)}%" for name, _, w, _ in SCORE_DIMS]
        print("  评分维度说明: " + " | ".join(parts))
        print()
        h = f"{'排名':<4} {'代码':<7} {'评分':<6} {'年化%':<7} {'近1月':<8} {'近3月':<8} {'近1年':<8} {'夏普':<6} {'索提诺':<6} {'回撤':<6}"
        print(h)
        print("-" * 72)
        medals2 = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(results[:SHOW_TOP], 1):
            b = medals2[i-1] if i <= 3 else f" {i}."
            print(f"{b:<4} {r['code']:<7} {r['score']:<6.1f} {r.get('annual_return',0):<7.1f} "
                  f"{str(r.get('m1','')):<8s} {str(r.get('m3','')):<8s} {str(r.get('y1','')):<8s} "
                  f"{r.get('sharpe',0):<6.2f} {r.get('sortino',0):<6.2f} {r.get('max_dd',0):<6.1f}")


def main() -> None:
    # ── 正常推荐流程 ──
    try:
        print("=" * 60)
        print("🔍 基金优选推荐工具 — 全市场 TOP 500 深度评分")
        print("=" * 60)

        est_min = _TOP * 2 // 60

        print(f"\n📥 获取全市场基金排行 (TOP {_TOP})...")
        rows = _fetch_rank_list(_TOP)
        print(f"   获取到 {len(rows)} 只基金")

        candidates = _filter_candidates(rows)
        print(f"   剔除负收益后: {len(candidates)} 只全部进入深度评分")
        print(f"   ⏱ 预计耗时约 {est_min} 分钟\n")

        scored = _run_scoring_pipeline(candidates)

        # ── 保存并输出 ──
        _save_result(scored)
        print(f"\n🏆 基金推荐 TOP {SHOW_TOP}  (12 维评分)")
        print("=" * 90)

        medals = ["🥇", "🥈", "🥉"]
        for i, item in enumerate(scored[:SHOW_TOP], 1):
            badge = medals[i - 1] if i <= 3 else f" {i}."
            print(f"{badge} {item[2]} ({item[1]}) — {item[0]:.1f}分  年化{item[3]:.1f}%")

        # 详细评分表
        print()
        parts = [f"{name} {int(w*100)}%" for name, _, w, _ in SCORE_DIMS]
        print("  " + " | ".join(parts))
        print()
        for i, (name, _, weight, desc) in enumerate(SCORE_DIMS):
            print(f"  {i+1}. {name} ({int(weight*100)}%)")
            print(f"      -> {desc}")

        print()
        print("💡 一键加入监控: python fund_recommend.py --add 基金代码")
        print("   查看上次结果: python fund_recommend.py --load")
    finally:
        print("  推荐任务完成")


def _filter_candidates(rows: list) -> list:
    """剔除近1年收益为负或低于5%的基金（多为债券基金混入）"""
    candidates = []
    for r in rows:
        try:
            y1 = float(r[11]) if len(r) > 11 and r[11] else 0
            if y1 <= 0:
                continue
            # 年化收益低于 5% 的不适合推荐（多为债券基金，收益不足以覆盖风险）
            if y1 < 5:
                continue
            candidates.append(r)
        except (ValueError, IndexError):
            continue
    return candidates


def _run_scoring_pipeline(candidates: list) -> list[tuple]:
    """并行评分管道，返回已排序的评分结果列表"""
    scored: list[tuple] = []
    futures = {}

    print(f"{'进度':<8} {'代码':<7} {'基金名':<20} {'年化':<8} {'评分':<6}")
    print("-" * 55)

    def _score_one(code: str, name: str) -> tuple | None:
        """单只基金评分（供并行使用）"""
        try:
            d = get(code)
            score = _calc_score(d)
            ar = d.get("annual_return", 0)
            return (score, code, name, ar,
                    d.get("m1", ""), d.get("m3", ""), d.get("y1", ""),
                    d.get("sharpe", 0), d.get("sortino", 0),
                    d.get("max_dd", 0), d.get("win_rate", 0),
                    d.get("inst", 0), d.get("sc", 0), d.get("rate", 0),
                    d.get("profit_ratio", 0), d.get("recovery", 0),
                    0 if d.get("sy3") is None else d["sy3"])
        except Exception as e:
            log.debug("跳过 %s: %s", code, e)
            return None

    with ThreadPoolExecutor(max_workers=5) as executor:
        for row in candidates:
            code = row[0]
            name = row[1]
            futures[executor.submit(_score_one, code, name)] = (code, name)

        for i, future in enumerate(as_completed(futures), 1):
            code, name = futures[future]
            result = future.result()
            if result:
                scored.append(result)
                ar_str = f"{result[3]:.1f}%" if isinstance(result[3], (int, float)) else "?"
                print(f"  {i}/{len(candidates):<4} {code:<7} {name[:18]:<20} {ar_str:<8} {result[0]:<6.1f}")
            else:
                print(f"  {i}/{len(candidates):<4} {code:<7} {name[:18]:<20} {'跳过':<8}")

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


if __name__ == "__main__":
    main()
