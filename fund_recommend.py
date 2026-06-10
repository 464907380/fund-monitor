"""
基金推荐工具 — 从全市场筛选优质基金

策略：
  1. 从天天基金拉取全市场近 1 年收益排行 TOP 200/500（不限类型）
  2. 快速评分初筛 → 取前 20 名
  3. 逐个拉取详细数据，8 维加权综合评分
  4. 输出 TOP 10 推荐

用法：
  python fund_recommend.py          # 快速推荐（TOP 200）
  python fund_recommend.py --deep   # 深度推荐（TOP 500）
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
_TOP_NORMAL = 200      # 快速模式初筛条数
_TOP_DEEP = 500        # 深度模式初筛条数
MIN_Y1 = 0.0           # 近 1 年收益率最低门槛（不设限，让评分自己判断）
MAX_CANDIDATES = 100   # 拉取详细数据的候选数（200候选→100只深度评分，约2~3分钟）
SHOW_TOP = 10          # 最终推荐数量


def _fetch_rank_list(pi: int = 1, pn: int = 50) -> list[list[str]]:
    """从天天基金排行 API 获取混合型基金列表"""
    sd = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
    ed = datetime.date.today().isoformat()
    url = (
        f"https://fund.eastmoney.com/data/rankhandler.aspx"
        f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc=1yz&st=desc"
        f"&sd={sd}&ed={ed}&pi={pi}&pn={pn}&dx=1"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://fund.eastmoney.com/data/fundranking.html",
    })
    data = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")
    raw = data.replace("var rankData = ", "", 1).rstrip(";")
    # JS 对象转 JSON
    raw_clean = re.sub(r'(\{|,)\s*(\w+)\s*:', lambda m: m.group(1) + '"' + m.group(2) + '":', raw)
    result = json.loads(raw_clean)
    return [row.split(",") for row in result.get("datas", [])]


def quick_score_from_rank(row: list[str]) -> float:
    """
    仅用排行数据做快速评分（不拉取详细 JS，更快）
    0:代码 1:名称 8:近1月% 9:近3月% 11:近1年%
    """
    try:
        m1 = float(row[8]) if row[8] else 0
        m3 = float(row[9]) if row[9] else 0
        y1 = float(row[11]) if row[11] else 0
    except (ValueError, IndexError):
        return 0.0
    # 简单加权：近1年占 60%，近3月占 25%，近1月占 15%
    return round(min(100, max(0, y1 * 0.2 + m3 * 0.5 + m1 * 0.3)), 1)


def main() -> None:
    print("=" * 60)
    print("🔍 基金优选推荐工具")
    print("=" * 60)

    deep = "--deep" in sys.argv
    n = _TOP_DEEP if deep else _TOP_NORMAL
    mode_label = "深度" if deep else "快速"

    print(f"\n📥 获取全市场基金排行 ({mode_label}模式, TOP {n})...")
    rows = _fetch_rank_list(1, n)
    print(f"   获取到 {len(rows)} 只基金")

    # 初步过滤 + 快速评分
    candidates = []
    for r in rows:
        try:
            y1 = float(r[11]) if len(r) > 11 and r[11] else 0
            if y1 < MIN_Y1:
                continue
            score = quick_score_from_rank(r)
            candidates.append((score, r))
        except (ValueError, IndexError):
            continue

    candidates.sort(key=lambda x: x[0], reverse=True)
    print(f"   收益率 > {MIN_Y1}% 过滤后: {len(candidates)} 只")
    print(f"   取前 {MAX_CANDIDATES} 只进入深度评分（约需 {MAX_CANDIDATES * 2 // 60} 分钟）\n")

    # 拉取详细评分数据
    top_candidates = candidates[:MAX_CANDIDATES]
    scored: list[tuple] = []  # (score, code, name, sharpe, sortino, max_dd, win_rate, inst, sc, rate)

    print(f"{'进度':<6} {'代码':<7} {'基金名':<20} {'近1年':<8} {'评分':<6}")
    print("-" * 55)

    for i, (_, row) in enumerate(top_candidates, 1):
        code = row[0]
        name = row[1]
        y1_display = row[11] if len(row) > 11 else "?"

        try:
            d = get(code)
            score = _calc_score(d)
            scored.append((score, code, name,
                          d.get("sharpe", 0), d.get("sortino", 0),
                          d.get("max_dd", 0), d.get("win_rate", 0),
                          d.get("inst", 0), d.get("sc", 0), d.get("rate", 0)))
            print(f"{i}/{MAX_CANDIDATES:<3} {code:<7} {name[:18]:<20} {y1_display:<8} {score:<6.1f}")
        except Exception as e:
            log.debug("跳过 %s: %s", code, e)
            continue

    scored.sort(key=lambda x: x[0], reverse=True)

    # 输出推荐
    print()
    print("=" * 80)
    print(f"🏆 基金推荐 TOP {SHOW_TOP}")
    print("=" * 80)
    medals = ["🥇", "🥈", "🥉"]
    for i, item in enumerate(scored[:SHOW_TOP], 1):
        score, code, name = item[0], item[1], item[2]
        badge = medals[i - 1] if i <= 3 else f" {i}."
        print(f"{badge} {name} ({code}) — {score:.1f}分")

    # 详细对比表
    print()
    print(f"{'排名':<4} {'代码':<7} {'评分':<6} {'夏普':<6} {'索提诺':<7} {'回撤':<6} {'胜率':<6} {'机构%':<6} {'规模':<7}")
    print("-" * 54)
    medals2 = ["🥇", "🥈", "🥉"]
    for i, item in enumerate(scored[:SHOW_TOP], 1):
        b = medals2[i-1] if i <= 3 else f" {i}."
        print(f"{b:<4} {item[1]:<7} {item[0]:<6.1f} {item[4]:<6.2f} {item[5]:<7.2f} {item[6]:<6.1f} {item[7]:<6.1f} {item[8]:<6.1f} {item[9]:<7.1f}亿")

    print("\n💡 提示: 将感兴趣的基金代码加入 fund_list.json 即可开始监控")


if __name__ == "__main__":
    main()
