"""
基金评分模块 — 12 维评分模型
"""
# mypy: ignore-errors
import math
from typing import Callable


# ── 单项评分函数（每项 0-100 分） ──────────────

def _score_annual_return(d: dict) -> float:
    """年化收益率评分"""
    ann_ret = d.get("annual_return")
    if ann_ret is None:
        return 0.0
    if ann_ret >= 30:   return 90 + (ann_ret - 30) / 30 * 10
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
    if sy6 >= 50:   sy6_score = 90 + (sy6 - 50) / 50 * 10
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


SCORE_DIMS: list[tuple[str, Callable, float, str]] = [
    ("近1年收益",    _score_y1,             0.09, "最近一年的表现，反映基金近期赚钱能力"),
    ("近3月收益",    _score_m3,             0.12, "近三个月涨跌幅，中期趋势"),
    ("夏普比率",     _score_sharpe,         0.06, "每承受 1 份波动能换来多少超额收益"),
    ("上行胜率",     _score_win_rate,       0.07, "赚钱天数占总交易天数的比例"),
    ("盈亏比",       _score_profit_ratio,   0.07, "平均盈利÷平均亏损，>1说明赚比亏多"),
    ("索提诺比率",   _score_sortino,        0.06, "只考虑下跌波动，更贴近真实风险感受"),
    ("修复系数",     _score_recovery,       0.04, "总收益÷最大回撤，衡量跌下去能不能涨回来"),
    ("近6月收益",    _score_sy6,            0.06, "近六个月表现，补充近1年的中短期维度"),
    ("近3年收益",    _score_sy3,            0.07, "从净值数据取约750个交易日精确计算，看穿越牛熊能力"),
    ("近1月收益",    _score_m1,             0.15, "近一个月涨跌幅，捕捉短期动量"),
    ("最大回撤",     _score_max_dd,         0.10, "历史最大跌幅"),
    ("费率",         _score_rate,           0.03, "申购费越低越好"),
    ("基金规模",     _score_scale,          0.02, "1~50亿最理想，太小不灵活、太大难操作"),
    ("年化收益率",    _score_annual_return,  0.04, "基金成立以来年化回报"),
    ("机构持有比例", _score_institutional,  0.02, "专业机构认可度，小幅参考"),
]


def _calc_score(d: dict) -> float:
    """
    计算基金综合评分 (0-100)

    从 SCORE_DIMS 注册表动态读取维度和权重。
    增删改维度只需编辑 SCORE_DIMS，无需改此函数。
    """
    total = 0.0
    weight_sum = 0.0
    for name, fn, weight, desc in SCORE_DIMS:
        if weight <= 0:
            continue
        s = fn(d)
        total += s * weight
        weight_sum += weight
    return round(total / weight_sum, 1) if weight_sum > 0 else 0.0


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
