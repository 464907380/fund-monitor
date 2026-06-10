"""
基金推荐工具 — 从全市场筛选优质基金

策略：
  1. 从天天基金拉取全市场近 1 年收益排行 TOP 500（不限类型）
  2. 收益率负数的剔除，其余全部进入 12 维深度评分
  3. 输出 TOP 10 推荐，支持保存结果和一键加入监控

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

try:
    from fund_watch import get, log, _calc_score
except ImportError:
    print("请先在 fund_watch.py 同一目录运行")
    sys.exit(1)

# ── 配置 ──────────────────────────────────────
_TOP = 500
SHOW_TOP = 10
_RESULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fund_recommend_result.json")
_FUND_LIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fund_list.json")


def _fetch_rank_list(pn: int) -> list[list[str]]:
    """从天天基金排行 API 获取全市场基金排行"""
    sd = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
    ed = datetime.date.today().isoformat()
    url = (
        f"https://fund.eastmoney.com/data/rankhandler.aspx"
        f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc=1yz&st=desc"
        f"&sd={sd}&ed={ed}&pi=1&pn={pn}&dx=1"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://fund.eastmoney.com/data/fundranking.html",
    })
    data = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")
    raw = data.replace("var rankData = ", "", 1).rstrip(";")
    raw_clean = re.sub(r'(\{|,)\s*(\w+)\s*:', lambda m: m.group(1) + '"' + m.group(2) + '":', raw)
    result = json.loads(raw_clean)
    return [row.split(",") for row in result.get("datas", [])]


def _save_result(scored: list[tuple]) -> None:
    """保存推荐结果到文件"""
    data = {
        "date": datetime.date.today().isoformat(),
        "results": [
            {"code": item[1], "name": item[2], "score": item[0],
             "annual_return": item[3], "sharpe": item[4], "sortino": item[5],
             "max_dd": item[6], "win_rate": item[7], "inst": item[8],
             "sc": item[9], "rate": item[10], "profit_ratio": item[11],
             "recovery": item[12], "sy6": item[13], "internal": item[14]}
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
        h = f"{'排名':<4} {'代码':<7} {'评分':<6} {'年化%':<7} {'夏普':<6} {'索提诺':<7} {'回撤':<6} {'胜率':<6} {'盈亏比':<7} {'修复':<7} {'近6年':<7} {'内部%':<6}"
        print(h)
        print("-" * 82)
        medals2 = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(results[:SHOW_TOP], 1):
            b = medals2[i-1] if i <= 3 else f" {i}."
            print(f"{b:<4} {r['code']:<7} {r['score']:<6.1f} {r.get('annual_return',0):<7.1f} {r.get('sharpe',0):<6.2f} {r.get('sortino',0):<7.2f} {r.get('max_dd',0):<6.1f} {r.get('win_rate',0):<6.1f} {r.get('profit_ratio',0):<7.2f} {r.get('recovery',0):<7.1f} {r.get('sy6',0):<7.1f} {r.get('internal',0):<6.3f}")


def main() -> None:
    # ── --load: 查看上次结果 ──
    if "--load" in sys.argv:
        results = _load_result()
        if results:
            _print_results(results)
            print("\n💡 加入监控: python fund_recommend.py --add 基金代码")
        else:
            print("❌ 没有找到上次推荐结果，请先运行 python fund_recommend.py")
        return

    # ── --add: 一键加入监控 ──
    if "--add" in sys.argv:
        try:
            idx = sys.argv.index("--add")
            code = sys.argv[idx + 1]
            results = _load_result()
            name = ""
            if results:
                for r in results:
                    if r["code"] == code:
                        name = r["name"]
                        break
            _add_to_fund_list(code, name)
        except (IndexError, ValueError):
            print("用法: python fund_recommend.py --add 基金代码")
        return

    # ── 正常推荐流程 ──
    print("=" * 60)
    print("🔍 基金优选推荐工具 — 全市场 TOP 500 深度评分")
    print("=" * 60)

    est_min = _TOP * 2 // 60

    print(f"\n📥 获取全市场基金排行 (TOP {_TOP})...")
    rows = _fetch_rank_list(_TOP)
    print(f"   获取到 {len(rows)} 只基金")

    candidates = []
    for r in rows:  # type: ignore[assignment]
        try:
            y1 = float(r[11]) if len(r) > 11 and r[11] else 0
            if y1 <= 0:
                continue
            candidates.append(r)
        except (ValueError, IndexError):
            continue

    print(f"   剔除负收益后: {len(candidates)} 只全部进入深度评分")
    print(f"   ⏱ 预计耗时约 {est_min} 分钟\n")

    # ── 逐个拉取数据，12 维评分 ──
    scored: list[tuple] = []

    print(f"{'进度':<8} {'代码':<7} {'基金名':<20} {'年化':<8} {'评分':<6}")
    print("-" * 55)

    for i, row in enumerate(candidates, 1):
        code = row[0]
        name = row[1]

        try:
            d = get(code)
            score = _calc_score(d)
            ar = d.get("annual_return", 0)
            scored.append((score, code, name, ar,
                          d.get("sharpe", 0), d.get("sortino", 0),
                          d.get("max_dd", 0), d.get("win_rate", 0),
                          d.get("inst", 0), d.get("sc", 0), d.get("rate", 0),
                          d.get("profit_ratio", 0), d.get("recovery", 0),
                          d.get("sy6", 0), d.get("internal", 0)))
            ar_str = f"{ar:.1f}%" if isinstance(ar, (int, float)) else "?"
            print(f"  {i}/{len(candidates):<4} {code:<7} {name[:18]:<20} {ar_str:<8} {score:<6.1f}")
        except Exception as e:
            log.debug("跳过 %s: %s", code, e)
            continue

    scored.sort(key=lambda x: x[0], reverse=True)

    # ── 保存结果 ──
    _save_result(scored)

    # ── 输出推荐 ──
    print()
    print("=" * 90)
    print(f"🏆 基金推荐 TOP {SHOW_TOP}  (12 维评分)")
    print("=" * 90)

    medals = ["🥇", "🥈", "🥉"]
    for i, item in enumerate(scored[:SHOW_TOP], 1):
        badge = medals[i - 1] if i <= 3 else f" {i}."
        print(f"{badge} {item[2]} ({item[1]}) — {item[0]:.1f}分  年化{item[3]:.1f}%")

    # 详细对比表
    print()
    h = f"{'排名':<4} {'代码':<7} {'评分':<6} {'年化%':<7} {'夏普':<6} {'索提诺':<7} {'回撤':<6} {'胜率':<6} {'盈亏比':<7} {'修复':<7} {'近6年':<7} {'内部%':<6}"
    print(h)
    print("-" * 82)
    medals2 = ["🥇", "🥈", "🥉"]
    for i, item in enumerate(scored[:SHOW_TOP], 1):
        b = medals2[i-1] if i <= 3 else f" {i}."
        print(f"{b:<4} {item[1]:<7} {item[0]:<6.1f} {item[3]:<7.1f} {item[4]:<6.2f} {item[5]:<7.2f} {item[6]:<6.1f} {item[7]:<6.1f} {item[11]:<7.2f} {item[12]:<7.1f} {item[13]:<7.1f} {item[14]:<6.3f}")

    print()
    print("💡 一键加入监控: python fund_recommend.py --add 基金代码")
    print("   查看上次结果: python fund_recommend.py --load")


if __name__ == "__main__":
    main()
