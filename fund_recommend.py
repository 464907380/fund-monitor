"""
基金推荐工具 — 从全市场筛选优质基金

策略：
  1. 从天天基金拉取全市场近 1 年收益排行 TOP 200/500（不限类型）
  2. 收益率负数的剔除，其余全部进入 8 维深度评分
  3. 输出 TOP 10 推荐

用法：
  python fund_recommend.py          # 拉取 TOP 500，全部深度评分（~15 分钟）
"""
import sys
import json
import re
import urllib.request
import datetime

try:
    from fund_watch import get, log, _calc_score
except ImportError:
    print("请先在 fund_watch.py 同一目录运行")
    sys.exit(1)

# ── 配置 ──────────────────────────────────────
_TOP = 500             # 从全市场拉取 TOP 500（不限类型）
SHOW_TOP = 10          # 最终推荐数量


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


def main() -> None:
    print("=" * 60)
    print("🔍 基金优选推荐工具 — 全市场 TOP 500 深度评分")
    print("=" * 60)

    est_min = _TOP * 2 // 60

    print(f"\n📥 获取全市场基金排行 (TOP {_TOP})...")
    rows = _fetch_rank_list(_TOP)
    print(f"   获取到 {len(rows)} 只基金")

    # 去掉收益为负的，其余全量深度评分
    candidates = []
    for r in rows:
        try:
            y1 = float(r[11]) if len(r) > 11 and r[11] else 0
            if y1 <= 0:
                continue
            candidates.append(r)
        except (ValueError, IndexError):
            continue

    print(f"   剔除负收益后: {len(candidates)} 只全部进入深度评分")
    print(f"   ⏱ 预计耗时约 {est_min} 分钟\n")

    # ── 逐个拉取完整数据，8 维评分 ──
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
                          d.get("inst", 0), d.get("sc", 0), d.get("rate", 0)))
            ar_str = f"{ar:.1f}%" if isinstance(ar, (int, float)) else "?"
            print(f"  {i}/{len(candidates):<4} {code:<7} {name[:18]:<20} {ar_str:<8} {score:<6.1f}")
        except Exception as e:
            log.debug("跳过 %s: %s", code, e)
            continue

    scored.sort(key=lambda x: x[0], reverse=True)

    # ── 输出推荐 ──
    print()
    print("=" * 80)
    print(f"🏆 基金推荐 TOP {SHOW_TOP}")
    print("=" * 80)
    medals = ["🥇", "🥈", "🥉"]
    for i, item in enumerate(scored[:SHOW_TOP], 1):
        badge = medals[i - 1] if i <= 3 else f" {i}."
        print(f"{badge} {item[2]} ({item[1]}) — {item[0]:.1f}分  年化{item[3]:.1f}%")

    # 详细对比表
    print()
    print(f"{'排名':<4} {'代码':<7} {'评分':<6} {'年化%':<7} {'夏普':<6} {'索提诺':<7} {'回撤':<6} {'胜率':<6} {'机构%':<6} {'规模':<7}")
    print("-" * 68)
    medals2 = ["🥇", "🥈", "🥉"]
    for i, item in enumerate(scored[:SHOW_TOP], 1):
        b = medals2[i-1] if i <= 3 else f" {i}."
        print(f"{b:<4} {item[1]:<7} {item[0]:<6.1f} {item[3]:<7.1f} {item[4]:<6.2f} {item[5]:<7.2f} {item[6]:<6.1f} {item[7]:<6.1f} {item[8]:<6.1f} {item[9]:<7.1f}亿")

    print("\n💡 提示: 将感兴趣的基金代码加入 fund_list.json 即可开始监控")


if __name__ == "__main__":
    main()
