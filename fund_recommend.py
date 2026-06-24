"""
基金推荐工具 — 从全市场筛选候选基金（评分由前端实时计算）

流程：
  1. 拉取全市场排行
  2. 按 y1 > min_y1_return 筛选
  3. 保存候选列表 (code, name, y1) 到文件
  4. 前端展示时实时拉取 TOP N 的深度数据并评分

用法：
  python fund_recommend.py                    # 更新候选列表
  python fund_recommend.py --load             # 查看候选数量
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
    from fund_watch import log, fetch, get_scoring_data
    from config import api_url, CFG
except ImportError:
    print("请先在 fund_watch.py 同一目录运行")
    sys.exit(1)

# ── 配置 ──────────────────────────────────────
_TOP = CFG.get("recommend", {}).get("top_n", 200)
SHOW_TOP = CFG.get("recommend", {}).get("show_top", 20)
_MIN_Y1 = CFG.get("recommend", {}).get("min_y1_return", 20)
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


def _save_candidates(candidates: list[dict]) -> bool:
    """保存候选基金列表到文件"""
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

        if not candidates:
            print("\n⚠️ 未找到匹配基金，保留上次结果")
            return False

        data = {
            "date": datetime.date.today().isoformat(),
            "candidates": candidates,
        }
        with open(_RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n📁 已保存 {len(candidates)} 只候选基金到 {_RESULT_FILE}")
        return True
    finally:
        try:
            os.remove(lock_file)
        except OSError:
            pass


def _load_result() -> list[dict] | None:
    """加载候选列表"""
    if not os.path.exists(_RESULT_FILE):
        return None
    try:
        with open(_RESULT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        print(f"📁 候选列表 ({data.get('date', '未知日期')})")
        return data.get("candidates", [])
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


def _print_results(candidates: list[dict]) -> None:
    """打印候选基金列表"""
    for i, r in enumerate(candidates[:SHOW_TOP], 1):
        print(f"  {i}. {r['name']} ({r['code']}) — y1={r['y1']:.1f}%")


def main() -> None:
    try:
        print("=" * 60)
        print("🔍 发现候选基金")
        print("=" * 60)

        print(f"\n📥 获取全市场基金排行 (TOP {_TOP})...")
        update_heartbeat("fund_recommend", progress=0, total=_TOP, status="获取排行榜")
        rows = _fetch_rank_list(_TOP)
        print(f"   获取到 {len(rows)} 只基金")

        candidates = _filter_candidates(rows)
        print(f"   y1 >= {_MIN_Y1}% 筛选后: {len(candidates)} 只")

        # 筛掉缺失收益数据的基金（需拉取深度数据判断）
        _skip_missing = CFG.get("recommend", {}).get("skip_missing_perf", False)
        if _skip_missing and candidates:
            print(f"\n📥 正在拉取深度数据以筛掉缺失收益的基金...")
            _perf_keys = ["m1", "m3", "y1", "f5", "sy6", "sy2", "sy3", "annual_return"]
            _filtered = []
            _total = len(candidates)
            with ThreadPoolExecutor(max_workers=20) as executor:
                def _check_one(c: dict) -> dict | None:
                    try:
                        d = get_scoring_data(c["code"])
                        if not d.get("n"):
                            return None
                        if any(d.get(k) is None or d.get(k) == "" or (k in ("sy3", "sy2") and d.get(k) == 0) for k in _perf_keys):
                            return None
                        return c
                    except Exception:
                        return None
                _futs = {executor.submit(_check_one, c): c for c in candidates}
                for i, _fut in enumerate(as_completed(_futs), 1):
                    _r = _fut.result()
                    if _r:
                        _filtered.append(_r)
                    if i % 50 == 0:
                        print(f"     进度: {i}/{_total}")
            candidates = _filtered
            print(f"   筛掉缺失收益数据后: {len(candidates)} 只")

        if candidates:
            update_heartbeat("fund_recommend", progress=1, total=1, status="保存候选列表")
            _save_candidates(candidates)
            print(f"\n🏆 候选基金 TOP {min(SHOW_TOP, len(candidates))}")
            print("=" * 40)
            _print_results(candidates)

        print()
        print("💡 前端展示时会实时拉取深度数据并评分")
        print("   一键加入监控: python fund_recommend.py --add 基金代码")
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
