"""
基金评分模块 — 12 维评分模型
"""
# mypy: ignore-errors
import logging
import math
from config import CFG
from typing import Callable

log = logging.getLogger(__name__)


# ── 单项评分函数（每项 0-100 分） ──────────────

def _score_annual_return(d: dict) -> float:
    """年化收益率评分"""
    ann_ret = d.get("annual_return")
    if ann_ret is None:
        return 0.0
    if ann_ret >= 30:   return min(100, 90 + (ann_ret - 30) / 30 * 10)
    elif ann_ret >= 15:  return 60 + (ann_ret - 15) / 15 * 30
    elif ann_ret >= 5:   return 20 + (ann_ret - 5) / 10 * 40
    elif ann_ret >= 0:   return ann_ret / 5 * 20
    else:                return 0


def _score_y1(d: dict) -> float:
    """近1年收益评分"""
    y1 = d.get("y1")
    if y1 is None:
        return 0.0
    if y1 >= 100:   y1_score = 100
    elif y1 >= 50:  y1_score = 80 + (y1 - 50) / 50 * 20
    elif y1 >= 20:  y1_score = 50 + (y1 - 20) / 30 * 30
    elif y1 >= 0:   y1_score = y1 / 20 * 50
    else:           y1_score = 0
    return y1_score


def _score_sharpe(d: dict) -> float:
    """夏普比率评分"""
    sharpe = d.get("sharpe")
    if sharpe is None:
        return 0.0
    if sharpe >= 1.5:   sharpe_score = 100
    elif sharpe >= 1:   sharpe_score = 70 + (sharpe - 1) / 0.5 * 30
    elif sharpe >= 0.5: sharpe_score = 30 + (sharpe - 0.5) / 0.5 * 40
    elif sharpe >= 0:   sharpe_score = sharpe / 0.5 * 30
    else:               sharpe_score = 0
    return sharpe_score


def _score_sortino(d: dict) -> float:
    """索提诺比率评分（只考虑下跌波动）"""
    sortino = d.get("sortino")
    if sortino is None:
        return 0.0
    if sortino >= 2:    sortino_score = 100
    elif sortino >= 1:  sortino_score = 60 + (sortino - 1) / 1 * 40
    elif sortino >= 0.5: sortino_score = 20 + (sortino - 0.5) / 0.5 * 40
    elif sortino >= 0:  sortino_score = sortino / 0.5 * 20
    else:               sortino_score = 0
    return sortino_score


def _score_profit_ratio(d: dict) -> float:
    """盈亏比评分"""
    pr = d.get("profit_ratio")
    if pr is None:
        return 0.0
    if pr >= 2:    pr_score = 100
    elif pr >= 1:  pr_score = (pr - 1) / 1 * 80 + 20
    elif pr >= 0:  pr_score = pr * 20
    else:          pr_score = 0
    return pr_score


def _score_recovery(d: dict) -> float:
    """修复系数评分（总收益÷最大回撤）"""
    rec = d.get("recovery")
    if rec is None:
        return 0.0
    if rec >= 50:   rec_score = 100
    elif rec >= 20:  rec_score = 60 + (rec - 20) / 30 * 40
    elif rec >= 5:   rec_score = 20 + (rec - 5) / 15 * 40
    elif rec >= 0:   rec_score = rec / 5 * 20
    else:            rec_score = 0
    return rec_score


def _score_sy3(d: dict) -> float:
    """近3年收益评分 — 从净值数据计算"""
    sy3 = d.get("sy3")
    if sy3 is None:
        return 0.0
    if sy3 >= 100:   sy3_score = 100
    elif sy3 >= 50:  sy3_score = 80 + (sy3 - 50) / 50 * 20
    elif sy3 >= 20:  sy3_score = 60 + (sy3 - 20) / 30 * 20
    elif sy3 >= 0:   sy3_score = 20 + sy3 / 20 * 40
    else:            sy3_score = 0
    return sy3_score


def _score_sy6(d: dict) -> float:
    """近6月收益评分 — 从净值数据计算"""
    sy6 = d.get("sy6")
    if sy6 is None:
        return 0.0
    if sy6 >= 50:   sy6_score = min(100, 90 + (sy6 - 50) / 50 * 10)
    elif sy6 >= 20:  sy6_score = 60 + (sy6 - 20) / 30 * 30
    elif sy6 >= 0:   sy6_score = 10 + sy6 / 20 * 50
    else:            sy6_score = 0
    return sy6_score


def _score_m1(d: dict) -> float:
    """近1月收益评分"""
    # 可能传入字符串 "+3.45%" 或数值
    raw = d.get("m1", "")
    if isinstance(raw, str) and raw.endswith("%"):
        m1 = float(raw.rstrip("%").lstrip("+"))
    elif isinstance(raw, (int, float)):
        m1 = float(raw)
    else:
        return 0.0
    if m1 >= 30:    return 100
    elif m1 >= 15:  return 80 + (m1 - 15) / 15 * 20
    elif m1 >= 5:   return 50 + (m1 - 5) / 10 * 30
    elif m1 >= 0:   return m1 / 5 * 50
    else:            return 0


def _score_m3(d: dict) -> float:
    """近3月收益评分"""
    raw = d.get("m3", "")
    if isinstance(raw, str) and raw.endswith("%"):
        m3 = float(raw.rstrip("%").lstrip("+"))
    elif isinstance(raw, (int, float)):
        m3 = float(raw)
    else:
        return 0.0
    if m3 >= 60:    return 100
    elif m3 >= 30:  return 80 + (m3 - 30) / 30 * 20
    elif m3 >= 10:  return 50 + (m3 - 10) / 20 * 30
    elif m3 >= 0:   return m3 / 10 * 50
    else:            return 0


def _score_f5(d: dict) -> float:
    """近一周收益评分"""
    raw = d.get("f5", "")
    if isinstance(raw, str) and raw.endswith("%"):
        f5 = float(raw.rstrip("%").lstrip("+"))
    elif isinstance(raw, (int, float)):
        f5 = float(raw)
    else:
        return 0.0
    if f5 >= 10:    return 100
    elif f5 >= 5:   return 70 + (f5 - 5) / 5 * 30
    elif f5 >= 0:   return f5 / 5 * 70
    else:            return 0


def _score_sy2(d: dict) -> float:
    """近2年收益评分"""
    sy2 = d.get("sy2")
    if sy2 is None:
        return 0.0
    if sy2 >= 150:   return 100
    elif sy2 >= 80:  return 70 + (sy2 - 80) / 70 * 30
    elif sy2 >= 30:  return 40 + (sy2 - 30) / 50 * 30
    elif sy2 >= 0:   return sy2 / 30 * 40
    else:            return 0


def _score_volatility(d: dict) -> float:
    """波动率评分（越低越好）"""
    v = d.get("volatility")
    if v is None:
        return 0.0
    if v <= 10:     return 100
    elif v <= 20:   return 80 + (20 - v) / 10 * 20
    elif v <= 40:   return 40 + (40 - v) / 20 * 40
    elif v <= 60:   return (60 - v) / 20 * 40
    else:           return 0


def _score_calmar(d: dict) -> float:
    """卡玛比率评分（年化收益/最大回撤，越高越好）"""
    c = d.get("calmar")
    if c is None:
        return 0.0
    if c >= 3:      return 100
    elif c >= 1:    return 60 + (c - 1) / 2 * 40
    elif c >= 0.3:  return 20 + (c - 0.3) / 0.7 * 40
    elif c >= 0:    return c / 0.3 * 20
    else:           return 0


def _score_max_loss_days(d: dict) -> float:
    """最大连跌天数评分（越短越好）"""
    m = d.get("max_loss_days")
    if m is None:
        return 0.0
    if m <= 3:      return 100
    elif m <= 7:    return 80 + (7 - m) / 4 * 20
    elif m <= 15:   return 40 + (15 - m) / 8 * 40
    elif m <= 30:   return (30 - m) / 15 * 40
    else:           return 0


def _score_max_dd(d: dict) -> float:
    """最大回撤评分"""
    max_dd = d.get("max_dd")
    if max_dd is None:
        return 0.0
    raw = max(0, min(90, 110 - max_dd * 1.2))
    return raw


def _score_win_rate(d: dict) -> float:
    """上行胜率评分"""
    win_rate = d.get("win_rate")
    if win_rate is None:
        return 0.0
    if win_rate >= 70:  wr_score = 100
    elif win_rate >= 50: wr_score = 40 + (win_rate - 50) / 20 * 60
    elif win_rate >= 30: wr_score = 10 + (win_rate - 30) / 20 * 30
    else:               wr_score = 0
    return wr_score


def _score_institutional(d: dict) -> float:
    """机构持有比例评分"""
    inst = d.get("inst")
    if inst is None:
        return 0.0
    if inst >= 60:   inst_score = 90
    elif inst >= 30:  inst_score = 50 + (inst - 30) / 30 * 40
    elif inst >= 5:   inst_score = 10 + (inst - 5) / 25 * 40
    else:             inst_score = 0
    return inst_score


def _score_scale(d: dict) -> float:
    """基金规模评分（1~50亿最理想）"""
    sc = d.get("sc")
    if sc is None:
        return 0.0
    if sc <= 0:       return 0
    elif sc >= 100:    sc_score = 30
    elif sc >= 50:     sc_score = 30 + (100 - sc) / 50 * 40
    elif sc >= 20:     sc_score = 70 + (50 - sc) / 30 * 30
    elif sc >= 1:      sc_score = 50 + (20 - sc) / 19 * 20
    else:              sc_score = 50
    return sc_score


def _score_rate(d: dict) -> float:
    """费率评分（申购费越低越好）"""
    rate = d.get("rate")
    if rate is None:
        return 0.0
    if rate <= 0:     return 100
    elif rate <= 0.15: return 80 + (0.15 - rate) / 0.15 * 20
    elif rate <= 0.5:  return 40 + (0.5 - rate) / 0.35 * 40
    elif rate <= 1.5:  return (1.5 - rate) / 1 * 40
    else:              return 0


# ── 评分维度注册表 ─────────────────────────────
_SCORE_FUNCS: dict[str, Callable] = {
    "y1": _score_y1,
    "m3": _score_m3,
    "m1": _score_m1,
    "sharpe": _score_sharpe,
    "win_rate": _score_win_rate,
    "profit_ratio": _score_profit_ratio,
    "sortino": _score_sortino,
    "recovery": _score_recovery,
    "sy6": _score_sy6,
    "sy3": _score_sy3,
    "max_dd": _score_max_dd,
    "rate": _score_rate,
    "scale": _score_scale,
    "annual_return": _score_annual_return,
    "institutional": _score_institutional,
    "f5": _score_f5,
    "sy2": _score_sy2,
    "volatility": _score_volatility,
    "calmar": _score_calmar,
    "max_loss_days": _score_max_loss_days,
}

# 维度名称 → 数据字典 key 映射（用于取值展示）
_DIM_VALUE_KEYS: dict[str, str] = {
    "近1年收益": "y1",
    "近3月收益": "m3",
    "近1月收益": "m1",
    "近一周收益": "f5",
    "近2年收益": "sy2",
    "夏普比率": "sharpe",
    "上行胜率": "win_rate",
    "盈亏比": "profit_ratio",
    "索提诺比率": "sortino",
    "修复系数": "recovery",
    "近3年收益": "sy3",
    "近6月收益": "sy6",
    "波动率": "volatility",
    "卡玛比率": "calmar",
    "最大连跌天数": "max_loss_days",
    "费率": "rate",
    "最大回撤": "max_dd",
    "基金规模": "sc",
    "年化收益率": "annual_return",
    "机构持有比例": "inst",
}

_DEFAULT_DIMS: list[tuple[str, Callable, float, str]] = [
    ("\u8fd11\u5e74\u6536\u76ca",    _score_y1,             0.10, "\u6700\u8fd1\u4e00\u5e74\u7684\u8868\u73b0\uff0c\u53cd\u6620\u57fa\u91d1\u8fd1\u671f\u8d5a\u94b1\u80fd\u529b"),
    ("\u8fd13\u6708\u6536\u76ca",    _score_m3,             0.15, "\u8fd1\u4e09\u4e2a\u6708\u6da8\u8dcc\u5e45\uff0c\u4e2d\u671f\u8d8b\u52bf"),
    ("\u590f\u666e\u6bd4\u7387",     _score_sharpe,         0.08, "\u6bcf\u627f\u53d7 1 \u4efd\u6ce2\u52a8\u80fd\u6362\u6765\u591a\u5c11\u989d\u5916\u6536\u76ca"),
    ("\u4e0a\u884c\u80dc\u7387",     _score_win_rate,       0.07, "\u8d5a\u94b1\u5929\u6570\u5360\u603b\u4ea4\u6613\u5929\u6570\u7684\u6bd4\u4f8b"),
    ("\u76c8\u4e8f\u6bd4",       _score_profit_ratio,   0.07, "\u5e73\u5747\u76c8\u5229\u00f7\u5e73\u5747\u4e8f\u635f\uff0c>1\u8bf4\u660e\u8d5a\u6bd4\u4e8f\u591a"),
    ("\u7d22\u63d0\u8bfa\u6bd4\u7387",   _score_sortino,        0.08, "\u53ea\u8003\u8651\u4e0b\u8dcc\u6ce2\u52a8\uff0c\u66f4\u8d34\u8fd1\u771f\u5b9e\u98ce\u9669\u611f\u53d7"),
    ("\u4fee\u590d\u7cfb\u6570",     _score_recovery,       0.06, "\u603b\u6536\u76ca\u00f7\u6700\u5927\u56de\u64a4\uff0c\u8861\u91cf\u8dcc\u4e0b\u53bb\u80fd\u4e0d\u80fd\u6da8\u56de\u6765"),
    ("\u8fd16\u6708\u6536\u76ca",    _score_sy6,            0.06, "\u8fd1\u516d\u4e2a\u6708\u8868\u73b0\uff0c\u8865\u5145\u8fd11\u5e74\u7684\u4e2d\u77ed\u671f\u7ef4\u5ea6"),
    ("\u8fd13\u5e74\u6536\u76ca",    _score_sy3,            0.07, "\u4ece\u51c0\u503c\u6570\u636e\u53d6\u7ea7750\u4e2a\u4ea4\u6613\u65e5\u7cbe\u786e\u8ba1\u7b97\uff0c\u770b\u7a7f\u8d8a\u725b\u718a\u80fd\u529b"),
    ("\u8fd11\u6708\u6536\u76ca",    _score_m1,             0.10, "\u8fd1\u4e00\u4e2a\u6708\u6da8\u8dcc\u5e45\uff0c\u6355\u6349\u77ed\u671f\u52a8\u91cf"),
    ("\u6700\u5927\u56de\u64a4",     _score_max_dd,         0.05, "\u5386\u53f2\u6700\u5927\u8dcc\u5e45"),
    ("\u8d39\u7387",         _score_rate,           0.03, "\u7533\u8d2d\u8d39\u8d8a\u4f4e\u8d8a\u597d"),
    ("\u57fa\u91d1\u89c4\u6a21",     _score_scale,          0.02, "1~50\u4ebf\u6700\u7406\u60f3\uff0c\u592a\u5c0f\u4e0d\u7075\u6d3b\u3001\u592a\u5927\u96be\u64cd\u4f5c"),
    ("\u5e74\u5316\u6536\u76ca\u7387",    _score_annual_return,  0.04, "\u57fa\u91d1\u6210\u7acb\u4ee5\u6765\u5e74\u5316\u56de\u62a5"),
    ("\u673a\u6784\u6301\u6709\u6bd4\u4f8b", _score_institutional,  0.02, "\u4e13\u4e1a\u673a\u6784\u8ba4\u53ef\u5ea6\uff0c\u5c0f\u5e45\u53c2\u8003"),
]


def _load_score_dims() -> list[tuple[str, Callable, float, str]]:
    """"""
    import json, os
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        cfg_dims = cfg.get("scoring", {}).get("dims", [])
    except Exception:
        cfg_dims = []
    if not cfg_dims:
        return _DEFAULT_DIMS
    result = []
    for d in cfg_dims:
        if not d.get("enabled", True):
            continue
        name = d.get("name", "")
        key = d.get("key", "")
        weight = d.get("weight", 0)
        desc = d.get("desc", "")
        func = _SCORE_FUNCS.get(key)
        if func is None and d.get("enabled", True):
            log.warning("评分维度 key='%s' (%s) 在评分函数中未找到，已跳过", key, name)
        if func and weight > 0:
            result.append((name, func, weight, desc))
    if not result:
        return _DEFAULT_DIMS
    total = sum(w for _,_,w,_ in result)
    if abs(total - 1.0) > 0.001:
        result = [(n, f, w/total, d) for n,f,w,d in result]
    return result


SCORE_DIMS = _load_score_dims()
def _calc_score(d: dict) -> float:
    """
    计算基金综合评分 (0-100)

    从 SCORE_DIMS 注册表动态读取维度和权重。
    无数据的维度得中性分 50（既不惩罚也不奖励）。
    """
    total = 0.0
    weight_sum = 0.0
    for name, fn, weight, desc in SCORE_DIMS:
        if weight <= 0:
            continue
        key = _DIM_VALUE_KEYS.get(name)
        if key and d.get(key) is None:
            total += 50.0 * weight
        else:
            s = fn(d)
            total += s * weight
        weight_sum += weight
    return round(total / weight_sum, 1) if weight_sum > 0 else 0.0


def calc_score_detail(d: dict) -> tuple[float, list[tuple[str, float | None, float, object, str]], float]:
    """
    计算基金综合评分并返回各维度明细

    返回: (总分, [(维度名, 单项得分或None, 权重, 原始值, 说明), ...], 中性分处理的权重和)
    无数据的维度得中性分 50，不跳过。
    """
    total = 0.0
    weight_sum = 0.0
    neutral_weight = 0.0
    details: list[tuple[str, float | None, float, object, str]] = []
    for name, fn, weight, desc in SCORE_DIMS:
        if weight <= 0:
            continue
        key = _DIM_VALUE_KEYS.get(name)
        raw = d.get(key) if key else None
        if raw is None:
            details.append((name, 50.0, weight, None, desc + "（无原始数据，取中性分50）"))
            total += 50.0 * weight
            neutral_weight += weight
        else:
            s = fn(d)
            details.append((name, round(s, 1), weight, raw, desc))
            total += s * weight
        weight_sum += weight
    score = round(total / weight_sum, 1) if weight_sum > 0 else 0.0
    return score, details, round(neutral_weight, 4)


def _rank_percentile_str(d: dict) -> str:
    """返回排名百分位字符串，如 'top 1.2%'"""
    rk = d.get("rank")
    total = d.get("rank_total")
    if rk is not None and total:
        pct = rk / total * 100
        if pct <= 5:
            return f"top {pct:.1f}%🌟"
        elif pct <= 20:
            return f"top {pct:.1f}%"
        else:
            return f"{pct:.1f}%"
    return ""
