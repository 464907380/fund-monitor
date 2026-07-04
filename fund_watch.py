"""
基金风险监控 v5.2 — 每日晚报 + 企业微信推送
"""
import json
import os
import re
import datetime
import time
from config import CFG, api_url
from config import get_secret as _get_secret
from fund_utils import fetch, log, HISTORY_DIR, write_heartbeat, update_heartbeat, clear_heartbeat, _fetch_fund_estimate
from fund_scoring import SCORE_DIMS, calc_score_detail
from fund_metrics import _calc_nav_metrics
from fund_alerts import check_stagnation, check_consecutive_drop, check_dividend
from fund_render import push, _load_recommend_data, _format_recommend_rankings

# ── 基金列表 ──────────────────────────────────
_FUND_LIST_FALLBACK = [
    {"code": "001438"}, {"code": "180031"}, {"code": "018998"},
    {"code": "000979"}, {"code": "320007"}, {"code": "161725"},
    {"code": "001480"}, {"code": "001753"}, {"code": "001170"},
]
FUND_LIST: list[dict] = []  # 占位，稍后由 _load_fund_list() 或 _ensure_fund_list_loaded() 填充
_fund_list_loaded = False


def _ensure_fund_list_loaded() -> None:
    """惰性加载基金列表（替代模块级副作用）"""
    global _fund_list_loaded
    if _fund_list_loaded:
        return  # 已加载
    _fund_list_path = os.path.join(HISTORY_DIR, "fund_list.json")
    if os.path.exists(_fund_list_path):
        try:
            with open(_fund_list_path, encoding="utf-8") as _f:
                loaded = json.load(_f)
            FUND_LIST[:] = loaded
            log.info("已从 fund_list.json 加载 %d 只基金", len(FUND_LIST))
        except Exception as _e:
            log.warning("读取 fund_list.json 失败 (%s)，使用内置默认列表", _e)
            FUND_LIST[:] = _FUND_LIST_FALLBACK
    else:
        FUND_LIST[:] = _FUND_LIST_FALLBACK
    _fund_list_loaded = True

def _parse_name(data: str) -> str | None:
    """从 pingzhongdata JS 中提取基金名称"""
    m = re.search(r'var fS_name\s*=\s*"([^"]+)"', data)
    return m.group(1) if m else None





def _parse_scale(data: str) -> float | None:
    """提取基金规模（亿元）"""
    m = re.findall(r'"y":([\d.]+),"mom":"[\d.-]+%"', data)
    return float(m[-1]) if m else None


def _parse_period_returns(data: str) -> dict:
    """提取阶段收益：近1月/近3月/近1年

    注意：天天基金 JS 变量命名容易误解：
    syl_1y = 近1月 (1y=1月), syl_3y = 近3月, syl_1n = 近1年
    """
    result = {}
    for key, js_var in [("m1", "syl_1y"), ("m3", "syl_3y"), ("y1", "syl_1n")]:
        m = re.search(rf'var {js_var}\s*=\s*["\']([-\d.]+)["\']', data)
        if m:
            result[key] = float(m.group(1))
    return result




def _calc_period_return(full_nav: list[dict] | None, lookback_days: int) -> float | None:
    """从净值数据计算指定区间收益(%)，lookback_days≈交易日数"""
    if not full_nav or len(full_nav) < 2:
        return None
    prices: list[float] = [float(n["v"]) for n in full_nav]
    if len(prices) < lookback_days:
        return None  # 数据不够
    start = prices[-lookback_days]
    end = prices[-1]
    return (end - start) / start * 100


def _parse_manager(data: str) -> str | None:
    """提取基金经理"""
    m = re.search(r'Data_currentFundManager.*?"name":"([^"]+)"', data, re.DOTALL)
    return m.group(1) if m else None


def _parse_institutional_ratio(data: str) -> float | None:
    """提取机构持有比例"""
    m = re.search(r'"机构持有比例","data":\[([^\]]+)\]', data)
    if not m:
        return None
    vs = m.group(1).split(",")
    return float(vs[-1].strip()) if vs else None


def _parse_syl_6y(data: str) -> float | None:
    """提取近6月收益率"""
    m = re.search(r'syl_6y="([-\d.]+)"', data)
    return float(m.group(1)) if m else None

def _parse_net_trend(data: str, full_nav: list[dict] | None = None) -> list[dict] | None:
    """提取净值趋势（最近6条，供日报表使用）

    可传入已解析的 full_nav 复用，避免重复解析大 JSON。
    """
    nav = full_nav if full_nav is not None else _parse_full_nav(data)
    if not nav:
        return None
    return nav[-6:]


def _parse_full_nav(data: str) -> list[dict] | None:
    """提取完整净值趋势（全部历史数据，供评分计算使用）"""
    ts = data.find("var Data_netWorthTrend")
    if ts < 0:
        return None
    as_ = data.find("[{", ts)
    if as_ < 0:
        return None
    dep, end = 0, -1
    for i in range(as_, len(data)):
        if data[i] == "[":
            dep += 1
        elif data[i] == "]":
            dep -= 1
            if dep == 0:
                end = i
                break
    if end < 0:
        return None
    try:
        full = json.loads(data[as_:end + 1])
        return [{"d": datetime.datetime.fromtimestamp(int(n["x"]) // 1000).strftime("%Y-%m-%d"),
                 "v": float(n["y"]), "ts": int(n["x"])} for n in full]
    except (ValueError, KeyError, TypeError, IndexError):
        return None


def _parse_real_time(code: str) -> float | None:
    """获取实时估算涨跌幅"""
    result = _fetch_fund_estimate(code)
    if result:
        _, gszzl = result
        return gszzl
    return None


def _parse_holdings(code: str) -> list[dict] | None:
    """获取前10大持仓明细（含股票名称/代码/占比），同时返回实时涨跌幅"""
    import html as _html
    url = api_url("fund_holdings", code=code)
    try:
        jj = fetch(url)
        cm = re.search(r'content:"(.+?)"', jj, re.DOTALL)
        if not cm:
            return None
        content = cm.group(1)
        content = content.replace('\\n', '\n').replace('\\"', '"').replace('\\/', '/')
        content = _html.unescape(content)
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', content, re.DOTALL)
        holds = []
        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            if len(cells) < 7:
                continue
            clean = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
            try:
                idx = int(clean[0])
            except (ValueError, IndexError):
                continue
            code_s = clean[1] if len(clean) > 1 else ""
            name = clean[2] if len(clean) > 2 else ""
            pct_str = clean[6] if len(clean) > 6 else "0"
            try:
                pct = float(pct_str.replace("%", ""))
            except ValueError:
                pct = 0
            # 从链接中推断市场：116→港股, 0→深市, 1→沪市
            href_match = re.search(r'href=["\'](?:[^"\']*[/.])(\d+)\.(\d+)["\']', cells[1])
            market = "sz"  # 默认深市
            if href_match:
                prefix = href_match.group(1)
                if prefix == "116":
                    market = "hk"
                elif prefix == "1":
                    market = "sh"
            holds.append({"n": name, "c": code_s, "p": pct, "m": market})
        return holds if holds else None
    except Exception as e:
        log.debug("拉取重仓股失败 %s: %s", code, e)
        return None


# ── 评分相关解析 ──────────────────────────────

def _parse_rank_info(data: str) -> tuple[int, int] | None:
    """提取同类排名 (当前排名, 同类总数)"""
    m = re.search(r'var Data_rateInSimilarType = (\[.*?\]);', data, re.DOTALL)
    if not m:
        return None
    try:
        ranks = json.loads(m.group(1))
        if not ranks:
            return None
        last = ranks[-1]
        return int(last["y"]), int(last.get("sc", 1))
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _parse_fund_rate(data: str) -> float | None:
    """提取基金现费率（%）"""
    m = re.search(r'fund_Rate="([^"]+)"', data)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None




def get(code: str) -> dict:
    """拉取一只基金的全量数据并组装返回"""
    d: dict = {"code": code}
    data = fetch(api_url("fund_pingzhongdata", code=code))

    if name := _parse_name(data):
        d["n"] = name
    if sc := _parse_scale(data):
        d["sc"] = sc
    d.update(_parse_period_returns(data))
    if mgr := _parse_manager(data):
        d["mgr"] = mgr
    if inst := _parse_institutional_ratio(data):
        d["inst"] = inst
    # 完整净值（用于计算回撤/波动率/卡玛比率）
    if full_nav := _parse_full_nav(data):
        d["full_nav"] = full_nav
        d["nav"] = _parse_net_trend(data, full_nav)
        metrics = _calc_nav_metrics(full_nav)
        d.update(metrics)
        # 从净值数据计算近3年收益
        d["sy3"] = _calc_period_return(full_nav, 750)  # ≈3年（约250个交易日/年 × 3）
        d["sy2"] = _calc_period_return(full_nav, 500)  # ≈2年
    else:
        if nav := _parse_net_trend(data):
            d["nav"] = nav
    td = _parse_real_time(code)
    if td is not None:
        d["td"] = td
    if holds := _parse_holdings(code):
        d["holds"] = holds
    if rp := _parse_rank_info(data):
        d["rank"], d["rank_total"] = rp
    if rate := _parse_fund_rate(data):
        d["rate"] = rate
    d["sy6"] = _parse_syl_6y(data)  # 近6月收益（暂未用于评分，保留供未来使用）

    return d


def get_scoring_data(code: str) -> dict:
    """拉取评分所需的最小数据集（跳过实时估值和持仓，减少网络请求）"""
    d: dict = {"code": code}
    data = fetch(api_url("fund_pingzhongdata", code=code))

    if name := _parse_name(data):
        d["n"] = name
    if sc := _parse_scale(data):
        d["sc"] = sc
    d.update(_parse_period_returns(data))
    if mgr := _parse_manager(data):
        d["mgr"] = mgr
    if inst := _parse_institutional_ratio(data):
        d["inst"] = inst
    if full_nav := _parse_full_nav(data):
        d["full_nav"] = full_nav
        d["nav"] = _parse_net_trend(data, full_nav)
        metrics = _calc_nav_metrics(full_nav)
        d.update(metrics)
        d["sy3"] = _calc_period_return(full_nav, 750)
        d["sy2"] = _calc_period_return(full_nav, 500)
    else:
        if nav := _parse_net_trend(data):
            d["nav"] = nav
    if rp := _parse_rank_info(data):
        d["rank"], d["rank_total"] = rp
    if rate := _parse_fund_rate(data):
        d["rate"] = rate
    d["sy6"] = _parse_syl_6y(data)
    return d


# ── 限购信息缓存 ──────────────────────────────
_limit_cache: dict[str, tuple[float, float | None]] = {}  # code -> (timestamp, amount_in_wan)
_LIMIT_CACHE_TTL = 3600  # 秒（1小时）


def _parse_purchase_limit(code: str) -> float | None:
    """获取基金单日限购金额（万元），None=无限购/获取失败"""
    import urllib.request
    now = time.time()
    if code in _limit_cache and now - _limit_cache[code][0] < _LIMIT_CACHE_TTL:
        return _limit_cache[code][1]

    result: float | None = None
    try:
        url = f"https://fund.eastmoney.com/{code}.html"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://fund.eastmoney.com/",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="ignore")
        # 提取限购金额 "单日累计购买上限XX.XX万元"
        m = re.search(r"单日累计购买上限\s*([\d.]+)\s*万元", html)
        if m:
            result = float(m.group(1))
        # 查找 fundBuyStatus="0" = 暂停申购
        if re.search(r'fundBuyStatus\s*=\s*"0"', html):
            result = 0.0  # 暂停申购
    except Exception:
        pass
    _limit_cache[code] = (now, result)
    return result


# ── 历史快照 ──────────────────────────────────

def _validate_fund_code(code: str) -> None:
    """校验基金代码：仅允许 6 位数字，防止路径遍历"""
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"非法基金代码: {code}")


# ── 推荐排行 ────────────────────────────────────

def main() -> None:
    _ensure_fund_list_loaded()
    today = datetime.date.today()
    today_str = today.isoformat()
    log.info("====== 基金晚报 %s 开始 ======", today_str)
    write_heartbeat("fund_watch")
    update_heartbeat("fund_briefing", progress=0, total=0, status="加载推荐数据...")
    try:
        # 直接从推荐结果文件加载数据，不再单独拉取网络数据
        rec_data = _load_recommend_data()
        if not rec_data:
            log.warning("推荐结果不存在，跳过晚报生成")
            return

        rec_results = rec_data.get("results", [])
        total_count = len(rec_results)
        raw_rows: list[dict] = []
        all_alerts: list[str] = []
        total_steps = total_count + 2  # 整理N + 推送

        # 将推荐结果转为晚报行格式
        for i, item in enumerate(rec_results):
            name = item.get("name", item.get("code", "?"))
            code = item.get("code", "")
            row = {
                "code": code,
                "name": name,
                "name_short": name[:12],
                "day": item.get("day", "-"),
                "f5": item.get("f5", ""),
                "m1": item.get("m1", ""),
                "m3": item.get("m3", ""),
                "y1": item.get("y1", ""),
                "score": item.get("score", 0),
                "mgr": "-",
                "annual_return": item.get("annual_return"),
                "sharpe": item.get("sharpe"),
                "sortino": item.get("sortino"),
                "max_dd": item.get("max_dd"),
                "win_rate": item.get("win_rate"),
                "inst": item.get("inst"),
                "sc": item.get("sc"),
                "rate": item.get("rate"),
                "profit_ratio": item.get("profit_ratio"),
                "recovery": item.get("recovery"),
                "sy3": item.get("sy3"),
                "sy2": item.get("sy2"),
                "volatility": item.get("volatility"),
                "calmar": item.get("calmar"),
                "max_loss_days": item.get("max_loss_days"),
                "sy6": item.get("sy6"),
            }
            raw_rows.append(row)
            log.info("  %s(%s) 评分%.1f", name, code, item.get("score", 0))
            update_heartbeat("fund_briefing", progress=i + 1, total=total_steps,
                             status=f"整理 {name[:18]}({code})")

        # 推送（两条通道共用推荐排行数据）
        update_heartbeat("fund_briefing", progress=total_steps - 1, total=total_steps, status="获取市场优选")
        ranking_lines = _format_recommend_rankings() if raw_rows else None
        # 主表只展示自选基金，推荐排行单独生成
        self_codes = {f["code"] for f in FUND_LIST}
        self_rows = [r for r in raw_rows if r["code"] in self_codes]
        # 补充推荐结果中缺失的自选基金（评分过低未入选推荐列表的基金）
        found_codes = {r["code"] for r in self_rows}
        missing_codes = self_codes - found_codes
        if missing_codes:
            log.info("补充 %d 只自选基金数据（未在推荐结果中）: %s", len(missing_codes), ",".join(sorted(missing_codes)))
            update_heartbeat("fund_briefing", progress=total_steps - 1, total=total_steps, status=f"补充{len(missing_codes)}只自选基金...")
            for code in sorted(missing_codes):
                try:
                    d = get(code)
                    if not d.get("n"):
                        log.warning("  跳过 %s：未获取到数据", code)
                        continue
                    navs = d.get("nav", [])
                    td = d.get("td")
                    day_s = f"{td:+.2f}%" if td is not None else ""
                    f5_s = ""
                    if len(navs) >= 5:
                        pct = (navs[-1]["v"] - navs[-5]["v"]) / navs[-5]["v"] * 100
                        f5_s = f"{pct:+.1f}%"
                    score_d = {k: d.get(k) for k in (
                        "y1","m3","m1","f5","sy6","sy2","sy3",
                        "annual_return","sharpe","sortino",
                        "profit_ratio","win_rate","recovery","calmar",
                        "max_dd","volatility","max_loss_days",
                        "sc","rate","inst",
                    )}
                    score, details, skipped = calc_score_detail(score_d)
                    name = d.get("n", code)
                    row = {
                        "code": code, "name": name, "name_short": name[:12],
                        "day": day_s,
                        "f5": f5_s,
                        "m1": f"{d['m1']:+.1f}%" if d.get("m1") is not None else "",
                        "m3": f"{d['m3']:+.1f}%" if d.get("m3") is not None else "",
                        "y1": f"{d['y1']:+.1f}%" if d.get("y1") is not None else "",
                        "score": score,
                        "mgr": (d.get("mgr", "") or "")[:6],
                        "annual_return": d.get("annual_return"),
                        "sharpe": d.get("sharpe"), "sortino": d.get("sortino"),
                        "max_dd": d.get("max_dd"), "win_rate": d.get("win_rate"),
                        "profit_ratio": d.get("profit_ratio"),
                        "recovery": d.get("recovery"),
                        "sy3": d.get("sy3"), "sy2": d.get("sy2"),
                        "sy6": d.get("sy6"),
                        "volatility": d.get("volatility"),
                        "calmar": d.get("calmar"),
                        "max_loss_days": d.get("max_loss_days"),
                        "inst": d.get("inst"), "sc": d.get("sc"), "rate": d.get("rate"),
                    }
                    self_rows.append(row)
                    log.info("  ✅ %s(%s) 评分%.1f（补充获取）", name, code, score)
                except Exception as e:
                    log.warning("  跳过 %s：获取失败 %s", code, e)
        update_heartbeat("fund_briefing", progress=total_steps - 1, total=total_steps, status="推送中")
        push("📊 基金晚报", self_rows, all_alerts, today_str, ranking_lines)
        update_heartbeat("fund_briefing", progress=total_steps, total=total_steps, status="完成")
        log.info("====== 基金晚报 %s 完成 ======", today_str)
    finally:
        clear_heartbeat("fund_watch")
        clear_heartbeat("fund_briefing")


if __name__ == "__main__":
    main()
