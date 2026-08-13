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
    "y1":  {"points": [[-50,0], [-20,15], [0,30], [20,50], [50,75], [100,95], [150,100]], "desc": "近1年收益%"},
    "m3":  {"points": [[-25,0], [-10,12], [0,28], [10,45], [25,70], [50,95], [60,100]], "desc": "近3月收益%"},
    "m1":  {"points": [[-30,0], [-20,10], [-10,22], [0,35], [5,50], [10,65], [20,85], [30,100]], "desc": "近1月收益%"},
    "f5":  {"points": [[-10,0], [-5,8], [0,20], [3,45], [7,70], [12,90], [18,100]], "desc": "近一周收益%"},
    "sy6": {"points": [[-40,0], [-20,12], [0,30], [20,55], [40,80], [60,100]], "desc": "近6月收益%"},
    "sy2": {"points": [[0,0], [30,20], [60,40], [100,70], [200,100]], "desc": "近2年收益%"},
    "sy3": {"points": [[0,0], [30,20], [60,40], [100,70], [200,100]], "desc": "近3年收益%"},
    "annual_return": {"points": [[-20,0], [-10,10], [0,25], [10,50], [20,70], [40,90], [60,100]], "desc": "年化收益率%"},
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
    "td": {"points": [[-5,0], [-2,40], [0,60], [2,80], [5,100]], "desc": "当日涨跌幅%"},
}

# 运行时曲线配置（由 _load_dim_curves 填充，可从 config.json 覆盖）
_DIM_CURVES: dict[str, dict] = {}


def _load_dim_curves():
    """从 config.json 的 dims 列表中提取各维度的 curve 配置，
    缺失项用 _DEFAULT_CURVES 补齐，结果写入模块级 _DIM_CURVES。"""
    import json, os
    curves = {}
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "config.json")
        with open(cfg_path, encoding="utf-8") as _f:
            cfg = json.load(_f)
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

def _make_scorer(data_key: str, curve_key: str, parse_pct: bool = False) -> Callable:
    """工厂函数：生成评分函数
    data_key — 从数据字典中取值的键名
    curve_key — 从 _DIM_CURVES 中取曲线配置的键名
    parse_pct — 是否处理 "+3.5%" 格式的字符串
    """
    if parse_pct:
        def _scorer(d: dict) -> float:
            raw = d.get(data_key, "")
            if isinstance(raw, str) and raw.endswith("%"):
                val = float(raw.rstrip("%").lstrip("+"))
            elif isinstance(raw, (int, float)):
                val = float(raw)
            else:
                return 0.0
            pts = _DIM_CURVES.get(curve_key, _DEFAULT_CURVES.get(curve_key, {})).get("points", [])
            return _score_piecewise(val, pts)
    else:
        def _scorer(d: dict) -> float:
            val = d.get(data_key)
            pts = _DIM_CURVES.get(curve_key, _DEFAULT_CURVES.get(curve_key, {})).get("points", [])
            return _score_piecewise(val, pts)
    return _scorer


# ── 评分维度注册表 ─────────────────────────────
# curve_key → (data_key, needs_percent_parsing)
# 曲线键名与数据键名不一致的已标注（如 scale→sc, institutional→inst）
_SCORE_DEFS: dict[str, tuple[str, bool]] = {
    "y1": ("y1", False),
    "m3": ("m3", True),
    "m1": ("m1", True),
    "f5": ("f5", True),
    "sy6": ("sy6", False),
    "sy2": ("sy2", False),
    "sy3": ("sy3", False),
    "annual_return": ("annual_return", False),
    "sharpe": ("sharpe", False),
    "sortino": ("sortino", False),
    "profit_ratio": ("profit_ratio", False),
    "win_rate": ("win_rate", False),
    "recovery": ("recovery", False),
    "max_dd": ("max_dd", False),
    "volatility": ("volatility", False),
    "calmar": ("calmar", False),
    "max_loss_days": ("max_loss_days", False),
    "rate": ("rate", False),
    "scale": ("sc", False),
    "institutional": ("inst", False),
    "td": ("td", False),
}

_SCORE_FUNCS: dict[str, Callable] = {
    curve_key: _make_scorer(data_key, curve_key, parse_pct)
    for curve_key, (data_key, parse_pct) in _SCORE_DEFS.items()
}

# 支持按窗口（lookback）计算的维度：原始值基于净值，可按 近1年/2年/3年/全部 计算
_WINDOW_KEYS = {"max_dd", "volatility", "max_loss_days", "sharpe", "sortino",
                "calmar", "recovery", "win_rate", "profit_ratio", "annual_return"}
# 窗口选项：key → 交易日数（None=全部）；与 fund_watch.get_scoring_data 的多窗口 key 对应
_WINDOW_CHOICES = {"all": None, "1y": 250, "2y": 500, "3y": 750}
# 窗口标签（用于动态说明）
_WINDOW_LABELS = {"all": "全部(约3年)", "1y": "近1年", "2y": "近2年", "3y": "近3年"}
# 窗口维度说明模板（{lbl} 会被替换为窗口标签）
_WINDOW_DESC = {
    "max_dd": "基于{lbl}净值的最大回撤",
    "volatility": "基于{lbl}净值的年化波动率",
    "max_loss_days": "{lbl}最长连续下跌天数",
    "sharpe": "超额年化收益÷年化波动率(基于{lbl}净值)",
    "sortino": "超额年化收益÷下行波动率(基于{lbl}净值)",
    "calmar": "年化收益÷最大回撤(基于{lbl}净值)",
    "recovery": "区间总收益÷最大回撤(基于{lbl}净值)",
    "win_rate": "赚钱交易日占比(基于{lbl}净值)",
    "profit_ratio": "平均盈利÷平均亏损(基于{lbl}净值)",
    "annual_return": "基于{lbl}净值的年化复合收益",
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
    "当日涨跌": "td",
}

# 维度名称 → 实际取数 key（窗口维度按 lookback 动态，如 最大回撤→max_dd_1y；未配置时回退 _DIM_VALUE_KEYS）
_DIM_DATA_KEYS: dict[str, str] = {}

_DEFAULT_DIMS: list[tuple[str, Callable, float, str]] = [
    ("近1年收益",    _SCORE_FUNCS["y1"],             0.10, "最近一年的表现，反映基金近期赚钱能力"),
    ("近3月收益",    _SCORE_FUNCS["m3"],             0.15, "近三个月涨跌幅，中期趋势"),
    ("夏普比率",     _SCORE_FUNCS["sharpe"],         0.08, "每承受 1 份波动能换来多少额外收益"),
    ("上行胜率",     _SCORE_FUNCS["win_rate"],       0.07, "赚钱天数占总交易天数的比例"),
    ("盈亏比",       _SCORE_FUNCS["profit_ratio"],   0.07, "平均盈利÷平均亏损，>1说明赚比亏多"),
    ("索提诺比率",   _SCORE_FUNCS["sortino"],        0.08, "只考虑下跌波动，更贴近真实风险感受"),
    ("修复系数",     _SCORE_FUNCS["recovery"],       0.06, "总收益÷最大回撤，衡量跌下去能不能涨回来"),
    ("近6月收益",    _SCORE_FUNCS["sy6"],            0.06, "近六个月表现，补充近1年的中短期维度"),
    ("近3年收益",    _SCORE_FUNCS["sy3"],            0.07, "近3个自然年净值复利收益（约728个交易日），看穿越牛熊能力"),
    ("近1月收益",    _SCORE_FUNCS["m1"],             0.10, "近一个月涨跌幅，捕捉短期动量"),
    ("最大回撤",     _SCORE_FUNCS["max_dd"],         0.05, "历史最大跌幅"),
    ("费率",         _SCORE_FUNCS["rate"],           0.03, "申购费越低越好"),
    ("基金规模",     _SCORE_FUNCS["scale"],          0.02, "1~50亿最理想，太小不灵活、太大难操作"),
    ("年化收益率",    _SCORE_FUNCS["annual_return"],  0.04, "基金成立以来年化回报"),
    ("机构持有比例", _SCORE_FUNCS["institutional"],  0.02, "专业机构认可度，小幅参考"),
    ("当日涨跌",    _SCORE_FUNCS["td"],              0.03, "当日实时涨跌幅，捕捉盘中动量"),
]


def _load_score_dims() -> list[tuple[str, Callable, float, str]]:
    """从 config.json 加载评分维度配置。先加载曲线，再构建维度列表。"""
    _load_dim_curves()
    import json, os
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "config.json")
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        cfg_dims = cfg.get("scoring", {}).get("dims", [])
    except Exception:
        cfg_dims = []
    if not cfg_dims:
        log.warning("config.json 中未找到评分维度配置，使用内置默认值（共 %d 维）", len(_DEFAULT_DIMS))
        return _DEFAULT_DIMS
    result = []
    _new_data_keys: dict[str, str] = {}
    for d in cfg_dims:
        if not d.get("enabled", True):
            continue
        name = d.get("name", "")
        key = d.get("key", "")
        weight = d.get("weight", 0)
        desc = d.get("desc", "")
        func = _SCORE_FUNCS.get(key)
        # 数据键优先用 _SCORE_DEFS 定义的数据 key（如 scale→sc, institutional→inst）：
        # 若用曲线 key(scale)，_DIM_DATA_KEYS 取值会落空 → 评分取中性50、表格显示缺失
        data_key = _SCORE_DEFS.get(key, (key, False))[0]
        # 窗口维度：按配置 lookback 动态绑定对应窗口的数据键（如 max_dd_1y；all 用原 key 兼容已有缓存/推荐数据）
        lookback = d.get("lookback", "all")
        if key in _WINDOW_KEYS and func is not None:
            _lb = lookback if lookback in _WINDOW_CHOICES else "all"
            if _lb != "all":
                data_key = f"{key}_{_lb}"
                _parse_pct = _SCORE_DEFS.get(key, (key, False))[1]
                func = _make_scorer(data_key, key, _parse_pct)
            # 维度说明随窗口动态变化
            _tmpl = _WINDOW_DESC.get(key)
            if _tmpl:
                desc = _tmpl.format(lbl=_WINDOW_LABELS.get(_lb, "全部"))
        if func is None and d.get("enabled", True):
            log.warning("评分维度 key='%s' (%s) 在评分函数中未找到，已跳过", key, name)
        if func and weight > 0:
            result.append((name, func, weight, desc))
            _new_data_keys[name] = data_key
    if not result:
        log.warning("筛选后无有效评分维度，使用内置默认值（共 %d 维）", len(_DEFAULT_DIMS))
        return _DEFAULT_DIMS
    total = sum(w for _,_,w,_ in result)
    if abs(total - 1.0) > 0.001:
        result = [(n, f, w/total, d) for n,f,w,d in result]
    log.info("评分维度加载完成：%d 维（来源：config.json）", len(result))
    globals()["_DIM_DATA_KEYS"] = _new_data_keys
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
        key = _DIM_DATA_KEYS.get(name) or _DIM_VALUE_KEYS.get(name)
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
        key = _DIM_DATA_KEYS.get(name) or _DIM_VALUE_KEYS.get(name)
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
