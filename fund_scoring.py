"""
基金评分模块 — 20 维可配置评分模型
"""
# mypy: ignore-errors
import logging
import math
from config import CFG
from typing import Callable

log = logging.getLogger(__name__)


# ── 通用分段线性评分函数 ─────────────────────────

def _score_piecewise(val, points):
    """分段线性评分：val 为输入值，points = [[x0,y0], [x1,y1], ...]
    返回 0~100 分。val 为 None 或 points 不足两点时返回 0.0。
    低于最低断点 → 向第一段外推截断；高于最高断点 → 向最后一段外推截断。
    """
    if val is None or not points or len(points) < 2:
        return 0.0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    # 低于最低断点
    if val <= xs[0]:
        return max(0.0, min(100.0, ys[0]))
    # 高于最高断点
    if val >= xs[-1]:
        return max(0.0, min(100.0, ys[-1]))
    # 线性插值
    for i in range(len(xs) - 1):
        if xs[i] <= val <= xs[i + 1]:
            if xs[i + 1] == xs[i]:
                return float(ys[i])
            ratio = (val - xs[i]) / (xs[i + 1] - xs[i])
            return max(0.0, min(100.0, ys[i] + (ys[i + 1] - ys[i]) * ratio))
    return 0.0


# ── 默认评分曲线（在 config.json 中没有显式 curve 时使用） ──────────
# 每项格式：{key: {"points": [[x0,y0], [x1,y1], ...], "desc": "说明"}}

_DEFAULT_CURVES: dict = {
    "y1":  {"points": [[0,0], [20,50], [50,80], [100,100]], "desc": "近1年收益%"},
    "m3":  {"points": [[0,0], [10,50], [30,80], [60,100]], "desc": "近3月收益%"},
    "m1":  {"points": [[0,0], [5,50], [15,80], [30,100]], "desc": "近1月收益%"},
    "f5":  {"points": [[0,0], [5,70], [10,100]], "desc": "近一周收益%"},
    "sy6": {"points": [[0,10], [20,60], [50,90], [100,100]], "desc": "近6月收益%"},
    "sy2": {"points": [[0,0], [30,20], [60,40], [100,70], [200,100]], "desc": "近2年收益%"},
    "sy3": {"points": [[0,0], [30,20], [60,40], [100,70], [200,100]], "desc": "近3年收益%"},
    "annual_return": {"points": [[0,0], [5,20], [15,60], [30,90], [60,100]], "desc": "年化收益率%"},
    "sharpe": {"points": [[0,0], [0.5,30], [1,70], [1.5,100]], "desc": "夏普比率"},
    "sortino": {"points": [[0,0], [0.5,20], [1,60], [2,100]], "desc": "索提诺比率"},
    "profit_ratio": {"points": [[0,0], [1,20], [2,100]], "desc": "盈亏比"},
    "win_rate": {"points": [[30,10], [50,40], [70,100]], "desc": "上行胜率%"},
    "recovery": {"points": [[0,0], [5,20], [20,60], [50,100]], "desc": "修复系数"},
    "max_dd": {"points": [[0,90], [16.67,90], [20,86], [50,50], [75,20], [91.67,0]], "desc": "最大回撤%"},
    "volatility": {"points": [[10,100], [20,80], [40,40], [60,0]], "desc": "波动率%（越低越好）"},
    "calmar": {"points": [[0,0], [0.3,20], [1,60], [3,100]], "desc": "卡玛比率"},
    "max_loss_days": {"points": [[3,100], [7,80], [15,40], [30,0]], "desc": "最大连跌天数（越短越好）"},
    "rate": {"points": [[0,100], [0.15,80], [0.5,40], [1.5,0]], "desc": "费率%（越低越好）"},
    "scale": {"points": [[0,0], [1,70], [20,100], [50,70], [100,30]], "desc": "基金规模（亿）"},
    "institutional": {"points": [[5,10], [30,50], [60,90]], "desc": "机构持有比例%"},
}

# 运行时曲线配置（由 _load_dim_curves 填充，可从 config.json 覆盖）
_DIM_CURVES: dict[str, dict] = {}


def _load_dim_curves():
    """从 config.json 的 dims 列表中提取各维度的 curve 配置，
    缺失项用 _DEFAULT_CURVES 补齐，结果写入模块级 _DIM_CURVES。"""
    import json, os
    curves = {}
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        for dim in cfg.get("scoring", {}).get("dims", []):
            key = dim.get("key", "")
            curve = dim.get("curve")
            if key and curve and isinstance(curve, dict) and "points" in curve:
                curves[key] = curve
    except Exception:
        pass
    # 用默认值补齐缺失项
    for k, v in _DEFAULT_CURVES.items():
        if k not in curves:
            curves[k] = dict(v)  # 复制，避免引用共享
    # 更新模块级变量
    globals()["_DIM_CURVES"] = curves
    return curves


# ── 单项评分函数（每项 0-100 分） ──────────────
# 全部委托给 _score_piecewise + _DIM_CURVES

def _score_annual_return(d: dict) -> float:
    ann_ret = d.get("annual_return")
    pts = _DIM_CURVES.get("annual_return", _DEFAULT_CURVES["annual_return"]).get("points", [])
    return _score_piecewise(ann_ret, pts)


def _score_y1(d: dict) -> float:
    y1 = d.get("y1")
    pts = _DIM_CURVES.get("y1", _DEFAULT_CURVES["y1"]).get("points", [])
    return _score_piecewise(y1, pts)


def _score_sharpe(d: dict) -> float:
    sharpe = d.get("sharpe")
    pts = _DIM_CURVES.get("sharpe", _DEFAULT_CURVES["sharpe"]).get("points", [])
    return _score_piecewise(sharpe, pts)


def _score_sortino(d: dict) -> float:
    sortino = d.get("sortino")
    pts = _DIM_CURVES.get("sortino", _DEFAULT_CURVES["sortino"]).get("points", [])
    return _score_piecewise(sortino, pts)


def _score_profit_ratio(d: dict) -> float:
    pr = d.get("profit_ratio")
    pts = _DIM_CURVES.get("profit_ratio", _DEFAULT_CURVES["profit_ratio"]).get("points", [])
    return _score_piecewise(pr, pts)


def _score_recovery(d: dict) -> float:
    rec = d.get("recovery")
    pts = _DIM_CURVES.get("recovery", _DEFAULT_CURVES["recovery"]).get("points", [])
    return _score_piecewise(rec, pts)


def _score_sy3(d: dict) -> float:
    sy3 = d.get("sy3")
    pts = _DIM_CURVES.get("sy3", _DEFAULT_CURVES["sy3"]).get("points", [])
    return _score_piecewise(sy3, pts)


def _score_sy6(d: dict) -> float:
    sy6 = d.get("sy6")
    pts = _DIM_CURVES.get("sy6", _DEFAULT_CURVES["sy6"]).get("points", [])
    return _score_piecewise(sy6, pts)


def _score_m1(d: dict) -> float:
    """近1月收益评分（处理字符串格式）"""
    raw = d.get("m1", "")
    if isinstance(raw, str) and raw.endswith("%"):
        m1 = float(raw.rstrip("%").lstrip("+"))
    elif isinstance(raw, (int, float)):
        m1 = float(raw)
    else:
        return 0.0
    pts = _DIM_CURVES.get("m1", _DEFAULT_CURVES["m1"]).get("points", [])
    return _score_piecewise(m1, pts)


def _score_m3(d: dict) -> float:
    """近3月收益评分（处理字符串格式）"""
    raw = d.get("m3", "")
    if isinstance(raw, str) and raw.endswith("%"):
        m3 = float(raw.rstrip("%").lstrip("+"))
    elif isinstance(raw, (int, float)):
        m3 = float(raw)
    else:
        return 0.0
    pts = _DIM_CURVES.get("m3", _DEFAULT_CURVES["m3"]).get("points", [])
    return _score_piecewise(m3, pts)


def _score_f5(d: dict) -> float:
    """近一周收益评分（处理字符串格式）"""
    raw = d.get("f5", "")
    if isinstance(raw, str) and raw.endswith("%"):
        f5 = float(raw.rstrip("%").lstrip("+"))
    elif isinstance(raw, (int, float)):
        f5 = float(raw)
    else:
        return 0.0
    pts = _DIM_CURVES.get("f5", _DEFAULT_CURVES["f5"]).get("points", [])
    return _score_piecewise(f5, pts)


def _score_sy2(d: dict) -> float:
    sy2 = d.get("sy2")
    pts = _DIM_CURVES.get("sy2", _DEFAULT_CURVES["sy2"]).get("points", [])
    return _score_piecewise(sy2, pts)


def _score_volatility(d: dict) -> float:
    v = d.get("volatility")
    pts = _DIM_CURVES.get("volatility", _DEFAULT_CURVES["volatility"]).get("points", [])
    return _score_piecewise(v, pts)


def _score_calmar(d: dict) -> float:
    c = d.get("calmar")
    pts = _DIM_CURVES.get("calmar", _DEFAULT_CURVES["calmar"]).get("points", [])
    return _score_piecewise(c, pts)


def _score_max_loss_days(d: dict) -> float:
    m = d.get("max_loss_days")
    pts = _DIM_CURVES.get("max_loss_days", _DEFAULT_CURVES["max_loss_days"]).get("points", [])
    return _score_piecewise(m, pts)


def _score_max_dd(d: dict) -> float:
    max_dd = d.get("max_dd")
    pts = _DIM_CURVES.get("max_dd", _DEFAULT_CURVES["max_dd"]).get("points", [])
    return _score_piecewise(max_dd, pts)


def _score_win_rate(d: dict) -> float:
    win_rate = d.get("win_rate")
    pts = _DIM_CURVES.get("win_rate", _DEFAULT_CURVES["win_rate"]).get("points", [])
    return _score_piecewise(win_rate, pts)


def _score_institutional(d: dict) -> float:
    inst = d.get("inst")
    pts = _DIM_CURVES.get("institutional", _DEFAULT_CURVES["institutional"]).get("points", [])
    return _score_piecewise(inst, pts)


def _score_scale(d: dict) -> float:
    sc = d.get("sc")
    pts = _DIM_CURVES.get("scale", _DEFAULT_CURVES["scale"]).get("points", [])
    return _score_piecewise(sc, pts)


def _score_rate(d: dict) -> float:
    rate = d.get("rate")
    pts = _DIM_CURVES.get("rate", _DEFAULT_CURVES["rate"]).get("points", [])
    return _score_piecewise(rate, pts)


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
    """从 config.json 加载评分维度配置。先加载曲线，再构建维度列表。"""
    _load_dim_curves()
    import json, os
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        cfg_dims = cfg.get("scoring", {}).get("dims", [])
    except Exception:
        cfg_dims = []
    if not cfg_dims:
        log.warning("config.json 中未找到评分维度配置，使用内置默认值（共 %d 维）", len(_DEFAULT_DIMS))
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
        log.warning("筛选后无有效评分维度，使用内置默认值（共 %d 维）", len(_DEFAULT_DIMS))
        return _DEFAULT_DIMS
    total = sum(w for _,_,w,_ in result)
    if abs(total - 1.0) > 0.001:
        result = [(n, f, w/total, d) for n,f,w,d in result]
    log.info("评分维度加载完成：%d 维（来源：config.json）", len(result))
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
            return f"top {pct:.1f}%\U0001f31f"
        elif pct <= 20:
            return f"top {pct:.1f}%"
        else:
            return f"{pct:.1f}%"
    return ""
