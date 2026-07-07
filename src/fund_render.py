"""
基金渲染模块 — Web 表格渲染
"""
import datetime
import json
import os
import html as _html

from config import CFG, get_config
from fund_scoring import SCORE_DIMS, _score_piecewise
import sys as _sys
from fund_utils import HISTORY_DIR, log, _color_inline

_show_top = CFG.get("recommend", {}).get("show_top", 20)


# ── 推荐结果文件 ──
_RECOMMEND_RESULT_FILE = os.path.join(HISTORY_DIR, ".fund_recommend_result.json")

# ── 数据获取 ──────────────────────────────────


def _web_rich_fund_table(rows: list[dict]) -> str:
    """生成自选基金完整数据 HTML 表格（Web 版，维度列动态跟随 SCORE_DIMS）"""
    from fund_scoring import SCORE_DIMS
    dim_names = [d[0] for d in sorted(SCORE_DIMS, key=lambda x: -x[2])]
    parts = ['<div style="margin-top:16px;padding:0 10px;">'
             '<p style="margin:8px 0;font-size:13px;font-weight:600;color:#ccc;">\U0001f4ca 自选基金完整数据</p>'
             '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:12px;">'
             '<thead><tr style="background:#2a2a2a;">'
             '<th style="padding:4px 6px;text-align:left;color:#888;border-bottom:1px solid #333;white-space:nowrap;">代码</th>'
             '<th style="padding:4px 6px;text-align:left;color:#888;border-bottom:1px solid #333;white-space:nowrap;">基金名</th>'
             '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;white-space:nowrap;">涨跌</th>'
             '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;white-space:nowrap;">评分</th>']
    # 动态维度列
    for dn in dim_names:
        parts.append(f'<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;white-space:nowrap;">{_html.escape(dn)}</th>')
    parts.append('<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;white-space:nowrap;">经理</th>'
                 '</tr></thead><tbody>')
    for r in rows:
        parts.append('<tr>')
        parts.append(f'<td style="padding:3px 6px;border-bottom:1px solid #333;font-family:Consolas;color:#888;">{_html.escape(str(r.get("code","")))}</td>')
        _warn = _skipped_icon(r.get("_skipped_weight", 0))
        _warn_title = ""
        if _warn:
            _missing_dims = []
            for _d in (r.get("_score_detail") or []):
                if len(_d) >= 4 and _d[3] is None:
                    _missing_dims.append(f"{_d[0]}({_d[2]*100:.0f}%)")
            if _missing_dims:
                _neutral = r.get("_skipped_weight", 0) * 50
                _warn_title = 'title="缺失: ' + ', '.join(_missing_dims) + f' | 中性分贡献+{_neutral:.1f}分"'
            _warn = f'<span style="cursor:help;" {_warn_title}>{_warn}</span>'
        _fcode = r.get("code", "")
        _fname_js = str(r.get("name_short", "")).replace("\\", "\\\\").replace("'", "\\'")
        _fname_html = _html.escape(str(r.get("name_short", "")))
        parts.append(f'<td style="padding:3px 6px;border-bottom:1px solid #333;white-space:nowrap;color:#ccc;"><span onclick="showHoldings(\'{_fcode}\',\'{_fname_js}\')" style="cursor:pointer;border-bottom:1px dashed rgba(255,255,255,0.15);" title="点击查看持仓">{_fname_html}</span>{_warn}</td>')
        _v = r.get("_day", "")
        parts.append(f'<td style="padding:3px 6px;border-bottom:1px solid #333;text-align:right;font-family:Consolas;white-space:nowrap;{_color_inline(_v)}">{_html.escape(str(_v))}</td>')
        # 评分（带明细弹窗）
        _v_detail = r.get("_score_detail", [])
        _detail_json = json.dumps(_v_detail, ensure_ascii=False) if _v_detail else "[]"
        parts.append(f"<td style=\"padding:3px 6px;border-bottom:1px solid #333;text-align:right;font-family:Consolas;font-weight:600;color:{_score_color(r.get('score',0))};cursor:pointer;font-size:13px;\" onclick='showScoreDetail({_detail_json})'>{r.get('score','')}</td>")
        # 动态维度列
        for dim_name in dim_names:
            val = _get_dim_value(r, dim_name)
            raw_val = r.get(_dim_value_to_key(dim_name))
            if val in ("-", ""):
                color = "#666"
                style_extra = "font-style:italic;"
                title_attr = 'title="数据缺失"'
                display_val = "—"
            else:
                color = "#bbb"
                style_extra = ""
                title_attr = ""
                display_val = val
                if raw_val is not None and raw_val != "":
                    color = _curve_color(dim_name, raw_val)
            parts.append(f'<td style="padding:3px 6px;border-bottom:1px solid #333;text-align:right;font-family:Consolas;color:{color};{style_extra}white-space:nowrap;" {title_attr}>{display_val}</td>')
        parts.append(f'<td style="padding:3px 6px;border-bottom:1px solid #333;font-size:12px;color:#888;white-space:nowrap;">{_html.escape(str(r.get("mgr","")))}</td>')
        parts.append('</tr>')
    parts.append('</tbody></table></div></div>')
    return "\n".join(parts)


def _web_rich_recommend_table(fresh: list[dict] | None = None) -> str:
    """生成推荐 TOP 10 完整维度数据 HTML 表格（Web 版）
    
    可传入已准备好的数据，否则实时拉取。
    """
    if fresh is None:
        fresh = _load_saved_recommend_data()
    if not fresh:
        return ""
    from fund_scoring import SCORE_DIMS
    dim_names = [d[0] for d in sorted(SCORE_DIMS, key=lambda x: -x[2])]
    dims_shown = dim_names
    parts = ['<div style="margin-top:16px;padding:0 10px;">'
             f'<p style="margin:8px 0;font-size:13px;font-weight:600;color:#ccc;">\U0001f3c6 \u5e02\u573a\u4f18\u9009 TOP {_show_top} \uff08\u5168\u7ef4\u5ea6\uff09</p>'
             '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:12px;">'
             '<thead><tr style="background:#2a2a2a;">'
             '<th style="padding:4px 6px;text-align:center;color:#888;border-bottom:1px solid #333;white-space:nowrap;">#</th>'
             '<th style="padding:4px 6px;text-align:left;color:#888;border-bottom:1px solid #333;white-space:nowrap;">基金</th>'
             '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;white-space:nowrap;">涨跌</th>'
             '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;white-space:nowrap;">总分</th>']
    for dn in dims_shown:
        parts.append(f'<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;white-space:nowrap;">{_html.escape(dn)}</th>')
    parts.append('</tr></thead><tbody>')
    medals = ["\U0001f947", "\U0001f948", "\U0001f949"]
    for i, r in enumerate(fresh[:_show_top]):
        badge = medals[i] if i < 3 else f'{i+1}.'
        detail = r.get("score_detail", [])
        detail_json = json.dumps(detail, ensure_ascii=False)
        parts.append('<tr>')
        parts.append(f'<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;font-size:13px;">{badge}</td>')
        warn = _skipped_icon(r.get("_skipped_weight", 0))
        warn_title = ""
        if warn:
            missing_dims = []
            for d_item in detail:
                if len(d_item) >= 4 and d_item[3] is None:
                    missing_dims.append(f"{d_item[0]}({d_item[2]*100:.0f}%)")
            if missing_dims:
                neutral_contrib = r.get("_skipped_weight", 0) * 50
                warn_title = 'title="缺失维度: ' + ', '.join(missing_dims) + f' | 均取中性分50, 贡献+{neutral_contrib:.1f}分"'
            warn = f'<span style="cursor:help;" {warn_title}>{warn}</span>'
        _fund_name = str(r.get("n", "")).replace("\\", "\\\\").replace("'", "\\'")
        _fund_code = r.get("code", "")
        parts.append(f'<td style="padding:3px 6px;border-bottom:1px solid #333;color:#e0e0e0;white-space:nowrap;"><span onclick="showHoldings(\'{_fund_code}\',\'{_fund_name}\')" style="cursor:pointer;border-bottom:1px dashed rgba(255,255,255,0.15);" title="点击查看持仓">{_html.escape(str(r.get("n","")))}</span>{warn} <span style="color:#666;font-family:Consolas;font-size:12px;">{_fund_code}</span></td>')
        # 涨跌（当日实时涨跌幅）
        day_raw = r.get("day", "")
        day_color = "#ef5350" if day_raw.startswith("+") else ("#66bb6a" if day_raw.startswith("-") else "#888")
        parts.append(f'<td style="padding:3px 6px;border-bottom:1px solid #333;text-align:right;font-family:Consolas;color:{day_color};">{_html.escape(day_raw)}</td>')
        parts.append(f"<td style=\"padding:3px 6px;border-bottom:1px solid #333;text-align:right;font-family:Consolas;font-weight:600;color:{_score_color(r.get('score',0))};cursor:pointer;font-size:13px;\" onclick='showScoreDetail({detail_json})'>{r.get('score',0):.1f}</td>")
        for dim_name in dims_shown:
            val = _get_dim_value(r, dim_name)
            raw_val = r.get(_dim_value_to_key(dim_name))
            if val in ("-", ""):
                color = "#666"
                style_extra = 'font-style:italic;'
                title_attr = 'title="数据缺失"'
                display_val = "—"
            else:
                color = "#bbb"
                style_extra = ""
                title_attr = ""
                display_val = val
                if raw_val is not None and raw_val != "":
                    color = _curve_color(dim_name, raw_val)
            parts.append(f'<td style="padding:3px 6px;border-bottom:1px solid #333;text-align:right;font-family:Consolas;color:{color};{style_extra}" {title_attr}>{display_val}</td>')
        parts.append('</tr>')
    parts.append('</tbody></table></div></div>')
    return "\n".join(parts)





def _fmt(v) -> str:
    """格式化数值，None/空返回 '-'"""
    if v is None or v == "":
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:.2f}"
    return str(v)


def _skipped_icon(weight: float) -> str:
    """根据缺失权重返回分级提示图标"""
    if weight > 0.30:
        return " 🚫"  # 严重缺失
    if weight > 0.15:
        return " ⚠️"  # 中等缺失
    if weight > 0:
        return " ℹ️"  # 少量缺失
    return ""

def _dim_value_to_key(dim_name: str) -> str | None:
    """维度中文名 → 数据字典 key"""
    m = {
        "\u8fd11\u5e74\u6536\u76ca": "y1", "\u8fd13\u6708\u6536\u76ca": "m3",
        "\u8fd11\u6708\u6536\u76ca": "m1", "\u8fd1\u4e00\u5468\u6536\u76ca": "f5",
        "\u8fd12\u5e74\u6536\u76ca": "sy2", "\u8fd13\u5e74\u6536\u76ca": "sy3",
        "\u8fd16\u6708\u6536\u76ca": "sy6",
        "\u590f\u666e\u6bd4\u7387": "sharpe", "\u7d22\u63d0\u8bfa\u6bd4\u7387": "sortino",
        "\u76c8\u4e8f\u6bd4": "profit_ratio", "\u4e0a\u884c\u80dc\u7387": "win_rate",
        "\u4fee\u590d\u7cfb\u6570": "recovery", "\u5361\u739b\u6bd4\u7387": "calmar",
        "\u5e74\u5316\u6536\u76ca\u7387": "annual_return",
        "\u6ce2\u52a8\u7387": "volatility", "\u6700\u5927\u56de\u64a4": "max_dd",
        "\u6700\u5927\u8fde\u8dcc\u5929\u6570": "max_loss_days",
        "\u8d39\u7387": "rate", "\u57fa\u91d1\u89c4\u6a21": "sc",
        "\u673a\u6784\u6301\u6709\u6bd4\u4f8b": "inst",
        "\u5f53\u65e5\u6da8\u8dcc": "td",
    }
    return m.get(dim_name)


def _get_dim_value(r: dict, dim_name: str) -> str:
    """根据维度名称从推荐结果中取值，缺失统一返回 '-'"""
    def _v(key, decimals=1):
        v = r.get(key)
        if v is None or v == "":
            return "-"
        if isinstance(v, (int, float)):
            return f"{v:.{decimals}f}"
        return str(v)
    mapping = {
        "近1年收益": lambda: _v("y1"),
        "近3月收益": lambda: _v("m3"),
        "近1月收益": lambda: _v("m1"),
        "近一周收益": lambda: _v("f5"),
        "近2年收益": lambda: _v("sy2"),
        "夏普比率": lambda: _v("sharpe", 2),
        "上行胜率": lambda: _v("win_rate", 2),
        "盈亏比": lambda: _v("profit_ratio", 2),
        "索提诺比率": lambda: _v("sortino", 2),
        "修复系数": lambda: _v("recovery", 2),
        "近3年收益": lambda: _v("sy3"),
        "近6月收益": lambda: _v("sy6"),
        "波动率": lambda: _v("volatility", 2),
        "卡玛比率": lambda: _v("calmar", 2),
        "最大连跌天数": lambda: _v("max_loss_days", 1),
        "费率": lambda: _v("rate", 2),
        "最大回撤": lambda: _v("max_dd", 2),
        "基金规模": lambda: _v("sc", 2),
        "年化收益率": lambda: _v("annual_return"),
        "机构持有比例": lambda: _v("inst", 2),
        "当日涨跌": lambda: _v("td", 2),
    }
    fn = mapping.get(dim_name)
    return fn() if fn else "-"


def _score_color(score: float | int | str) -> str:
    """评分颜色：高分(≥80)绿、中分(40~80)橙、低分(<40)红"""
    try:
        s = float(score)
    except (ValueError, TypeError):
        return "#bbb"
    return "#66bb6a" if s >= 80 else "#ffa726" if s >= 40 else "#ef5350"


def _curve_color(dim_name: str, raw_val) -> str:
    """基于评分曲线返回颜色：高分(≥80)绿、中分(40~80)橙、低分(<40)红"""
    # 处理百分比字符串 "+3.5%" → 3.5
    if isinstance(raw_val, str):
        raw_val = raw_val.strip()
        if raw_val.endswith("%"):
            try:
                raw_val = float(raw_val.rstrip("%").lstrip("+"))
            except (ValueError, TypeError):
                return "#bbb"
    if not isinstance(raw_val, (int, float)):
        return "#bbb"
    # dim_name → curve_key（部分维度的数据key和曲线key不同）
    _CURVE_KEY_MAP = {"基金规模": "scale"}
    curve_key = _CURVE_KEY_MAP.get(dim_name) or _dim_value_to_key(dim_name)
    curves = _sys.modules.get("fund_scoring")
    if curves is None:
        return "#bbb"
    dim_curves = getattr(curves, "_DIM_CURVES", None)
    if curve_key and dim_curves and curve_key in dim_curves:
        points = dim_curves[curve_key].get("points", [])
        if points and len(points) >= 2:
            score = _score_piecewise(raw_val, points)
            if score >= 80:
                return "#66bb6a"   # 绿 - 优秀
            elif score >= 40:
                return "#ffa726"   # 橙 - 中等
            else:
                return "#ef5350"   # 红 - 偏差
    return "#bbb"


def _load_saved_recommend_data() -> list[dict]:
    """从保存的结果文件读取推荐数据，并用当前 SCORE_DIMS 重新评分。"""
    try:
        from fund_scoring import calc_score_detail
        data = _load_recommend_data()
        if not data:
            return []
        results = data.get("results", [])
        if not results:
            return []
        out = []
        for r in results:
            entry = {
                "n": r.get("name", ""),
                "code": r.get("code", ""),
                "annual_return": r.get("annual_return", 0),
                "m1": r.get("m1"), "m3": r.get("m3"), "y1": r.get("y1"),
                "sharpe": r.get("sharpe"), "sortino": r.get("sortino"),
                "max_dd": r.get("max_dd"), "win_rate": r.get("win_rate"),
                "inst": r.get("inst"), "sc": r.get("sc"), "rate": r.get("rate"),
                "profit_ratio": r.get("profit_ratio"),
                "recovery": r.get("recovery"), "sy3": r.get("sy3"),
                "f5": r.get("f5"), "sy2": r.get("sy2"),
                "sy6": r.get("sy6"),
                "volatility": r.get("volatility"), "calmar": r.get("calmar"),
                "max_loss_days": r.get("max_loss_days"),
                "mgr": r.get("mgr", ""), "day": r.get("day", ""),
                "td": r.get("td"),
            }
            score_d = {k: entry.get(k) for k in (
                "y1", "m3", "m1", "f5", "sy6", "sy2", "sy3",
                "annual_return", "sharpe", "sortino",
                "profit_ratio", "win_rate", "recovery", "calmar",
                "max_dd", "volatility", "max_loss_days",
                "sc", "rate", "inst", "td",
            )}
            score, details, skipped = calc_score_detail(score_d)
            entry["score"] = score
            entry["score_detail"] = details
            entry["_skipped_weight"] = skipped
            out.append(entry)
        out.sort(key=lambda x: x.get("score", 0), reverse=True)
        return out  # 返回全部，由调用方决定截取数量
    except Exception:
        return []


def _fetch_fresh_recommend_data() -> list[dict]:
    """从缓存推荐数据刷新实时涨跌，返回最新 TOP 表格数据
    
    推荐结果文件中已保存所有评分维度数据，无需重复拉取 pingzhongdata。
    只需并行获取各基金的实时涨跌即可。
    """
    try:
        from fund_watch import _parse_real_time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        fresh = _load_saved_recommend_data()
        if not fresh:
            return []

        codes = [r.get("code", "") for r in fresh if r.get("code")]
        if not codes:
            return fresh

        # 刷新实时涨跌，同时更新td值和评分
        from fund_scoring import calc_score_detail
        day_map: dict[str, str] = {}
        td_map: dict[str, float] = {}

        def _fetch_one(code: str) -> tuple[str, float | None]:
            try:
                td = _parse_real_time(code)
                if td is not None:
                    return (code, td)
                return (code, None)
            except Exception:
                return (code, None)

        with ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "render_recommend", default=20)) as ex:
            futs = {ex.submit(_fetch_one, code): code for code in codes}
            for fut in as_completed(futs):
                code, td_val = fut.result()
                if td_val is not None:
                    day_map[code] = f"{td_val:+.2f}%"
                    td_map[code] = td_val

        for r in fresh:
            code = r.get("code", "")
            if code in day_map:
                r["day"] = day_map[code]
            if code in td_map:
                r["td"] = td_map[code]
                # 用新td值重算评分
                score_d = {k: r.get(k) for k in (
                    "y1", "m3", "m1", "f5", "sy6", "sy2", "sy3",
                    "annual_return", "sharpe", "sortino",
                    "profit_ratio", "win_rate", "recovery", "calmar",
                    "max_dd", "volatility", "max_loss_days",
                    "sc", "rate", "inst", "td",
                )}
                score, details, skipped = calc_score_detail(score_d)
                r["score"] = score
                r["score_detail"] = details
                r["_skipped_weight"] = skipped

        # 重算评分后排序列会乱，重新排序
        fresh.sort(key=lambda x: x.get("score", 0), reverse=True)
        return fresh[:_show_top]
    except Exception:
        return []


def _load_recommend_data() -> dict | None:
    """加载推荐结果完整数据（含日期和结果列表），合并文件读取"""
    if not os.path.exists(_RECOMMEND_RESULT_FILE):
        return None
    try:
        with open(_RECOMMEND_RESULT_FILE, encoding="utf-8") as f:
            return json.load(f)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None







# ── 主程序 ────────────────────────────────────


