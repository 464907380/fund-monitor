"""
基金渲染/推送模块
"""
import datetime
import json
import os
import re
import html as _html
from email.header import Header
from email.mime.text import MIMEText

from config import CFG
from fund_scoring import SCORE_DIMS
from fund_utils import HISTORY_DIR, log, _color_inline, _strip_html, _send_smtp, send_wechat
from fund_utils import get_secret as _get_secret


def _get_webhook() -> str | None:
    """惰性读取企业微信 Webhook（支持长进程环境变量刷新）"""
    return _get_secret("WECHAT_WEBHOOK")


def _get_email_user() -> str | None:
    """惰性读取 QQ 邮箱（支持长进程环境变量刷新）"""
    return _get_secret("QQ_EMAIL")


def _get_email_auth() -> str | None:
    """惰性读取 QQ 邮箱授权码"""
    return _get_secret("QQ_MAIL_AUTH")

ALERT_DROP_1M = CFG.get("fund_watch", {}).get("alert_drop_1m", -10)
ALERT_DROP_1M_RED = CFG.get("fund_watch", {}).get("alert_drop_1m_red", -15)
ALERT_SCALE_2X = CFG.get("fund_watch", {}).get("alert_scale_2x", 2.0)
ALERT_SCALE_1_5X = CFG.get("fund_watch", {}).get("alert_scale_1_5x", 1.5)

_show_top = CFG.get("recommend", {}).get("show_top", 20)


# ── 推荐结果文件（需要 HISTORY_DIR 定义后）──
_RECOMMEND_RESULT_FILE = os.path.join(HISTORY_DIR, ".fund_recommend_result.json")

_BRIEFING_FILE = os.path.join(HISTORY_DIR, ".briefing_fund.html")
"""晚报 HTML 文件（供 Web 页面展示）"""


# ── 推送 ──────────────────────────────────────

def _pipe_table_to_html(ranking_lines: list[str]) -> str:
    """将 Markdown 管道表行列表转为 HTML <table> 字符串"""
    cp = '<tr><td style="padding:12px 14px;background:#222;border:1px solid #333;border-radius:6px;">'
    num_dims = len(SCORE_DIMS)
    cp += f'<p style="margin:0 0 8px;font-size:14px;font-weight:600;color:#ccc;">🏆 市场优选基金 TOP {_show_top} （{num_dims} 维评分）</p>'
    in_table = False
    header_done = False
    for line in ranking_lines:
        clean = line.strip()
        if clean.startswith("🏆"):
            continue
        if not clean:
            if in_table:
                cp += '</tbody></table>'
                in_table = False
            cp += '<br>'
            continue
        if clean.startswith("|:---"):
            continue
        if clean.startswith("|"):
            if not in_table:
                in_table = True
                header_done = False
                cp += '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:4px;">'
            if not header_done:
                cp += '<thead><tr>'
                for c in clean.strip("|").split("|"):
                    cp += f'<th style="padding:4px 6px;text-align:center;border-bottom:1px solid #444;color:#888;white-space:nowrap;">{_html.escape(c.strip())}</th>'
                cp += '</tr></thead><tbody>'
                header_done = True
            else:
                cp += '<tr>'
                for c in clean.strip("|").split("|"):
                    cell = c.strip()
                    # 判断颜色：数值正绿负红
                    cell_color = "#bbb"
                    try:
                        num_str = cell.replace("%", "").replace("+", "").replace(",", "")
                        if num_str.lstrip("-").replace(".", "").isdigit():
                            num = float(num_str)
                            cell_color = "#66bb6a" if num > 0 else ("#ef5350" if num < 0 else "#bbb")
                    except (ValueError, TypeError):
                        pass
                    cp += f"<td style=\"padding:3px 6px;text-align:center;border-bottom:1px solid #333;color:{cell_color};white-space:nowrap;\">{_html.escape(cell)}</td>" 
                cp += '</tr>'
            continue
        if not in_table:
            cp += f'<p style="margin:2px 0;font-size:12px;color:#888;">{_html.escape(clean)}</p>'
    if in_table:
        cp += '</tbody></table>'
    cp += '</td></tr>'
    return cp


def _build_briefing_html(rows: list[dict], alerts: list[str], today: str,
                         ranking_lines: list[str] | None = None) -> str | None:
    """构建晚报完整 HTML，返回字符串；模板缺失时返回 None"""
    tpl_path = os.path.join(HISTORY_DIR, "email_template.html")
    if not os.path.exists(tpl_path):
        return None
    with open(tpl_path, encoding="utf-8") as f:
        tpl_html = f.read()
    tpl_html = tpl_html.replace("{{DATE}}", today)

    # 表格行
    row_htmls = []
    for r in rows:
        _code = _html.escape(str(r.get("code", "")))
        _name = _html.escape(str(r.get("name_short", "")))
        _day = _html.escape(str(r.get("day", "")))
        _m1 = _html.escape(str(r.get("m1", "")))
        _m3 = _html.escape(str(r.get("m3", "")))
        _y1 = _html.escape(str(r.get("y1", "")))
        row_htmls.append("<tr>"
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;font-family:Consolas;font-size:12px;color:#888;white-space:nowrap;">{_code}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;font-size:13px;color:#ccc;white-space:nowrap;">{_name}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["day"])}">{_day}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["f5"])}">{_html.escape(str(r.get("f5","")))}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["m1"])}">{_m1}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["m3"])}">{_m3}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["y1"])}">{_y1}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;text-align:right;font-family:Consolas;font-size:12px;font-weight:600;color:#66bb6a;white-space:nowrap;">{r.get("score","")}</td>'
            + "</tr>"
        )
    html = tpl_html.replace("{{ROWS}}", "\n".join(row_htmls))

    extra_parts = []

    # 推荐排行
    if ranking_lines is None:
        ranking_lines = _format_recommend_rankings()
    if ranking_lines:
        extra_parts.append(_pipe_table_to_html(ranking_lines))

    # 警报
    if alerts:
        al = '<tr><td style="padding:12px 14px;"><p style="margin:0 0 8px;font-size:14px;font-weight:600;color:#ef5350;">🚨 警报</p>'
        for a in alerts:
            al += f'<p style="margin:3px 0;padding:4px 0;font-size:12px;color:#aaa;">{_strip_html(a)}</p>'
        al += '</td></tr>'
        extra_parts.append(al)

    html = html.replace("{{ALERTS}}", "\n".join(extra_parts) if extra_parts else "")
    return html


def send_mail_html(subject: str, rows: list[dict], alerts: list[str], today: str,
                   ranking_lines: list[str] | None = None) -> None:
    """通过 QQ 邮箱发送邮件（MJML 编译渲染）"""
    qq_email = _get_email_user()
    qq_auth = _get_email_auth()
    if not qq_email or not qq_auth:
        log.debug("QQ_EMAIL 或 QQ_MAIL_AUTH 未配置，邮件推送跳过")
        return
    html = _build_briefing_html(rows, alerts, today, ranking_lines)
    if html is None:
        log.warning("email_template.html 不存在，跳过邮件")
        return
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")  # type: ignore[assignment]
    msg["From"] = msg["To"] = qq_email
    _send_smtp(msg)


def push(subject: str, rows: list[dict], alerts: list[str], today: str,
         ranking_lines: list[str] | None = None) -> None:
    # 预计算推荐排行，两个推送通道共用
    if ranking_lines is None:
        ranking_lines = _format_recommend_rankings()
    sent = send_wechat(md_content(rows, alerts, today, ranking_lines))
    if not sent:
        send_mail_html(subject, rows, alerts, today, ranking_lines)
    _save_briefing(rows, alerts, today, ranking_lines)


def md_content(rows: list[dict], alerts: list[str], today: str,
               ranking_lines: list[str] | None = None) -> str:
    """构造 Markdown 内容（企业微信推送用）"""
    md_lines = [
        f"📊 **基金晚报 {today}**",
        "",
        "|代码|基金名|涨跌|近5日|近1月|近3月|近1年|评分|经理|",
        "|:---|:---|---:|----:|----:|----:|----:|:---:|:---|",
    ]
    for r in rows:
        md_lines.append(
            f"|{r['code']}|{r['name_short']}|{r['day']}|{r['f5']}|{r['m1']}|{r['m3']}|{r['y1']}|{r.get('score','')}|{r['mgr']}|"
        )

    # 推荐排行
    if ranking_lines is None:
        ranking_lines = _format_recommend_rankings()
    if ranking_lines:
        md_lines.append("")
        md_lines.extend(ranking_lines)

    if alerts:
        md_lines.append("")
        md_lines.append("**🚨 警报:**")
        for a in alerts:
            md_lines.append(f"> {_strip_html(a)}")
    return "\n".join(md_lines)

# ── 数据获取 ──────────────────────────────────


def _web_rich_fund_table(rows: list[dict]) -> str:
    """生成自选基金完整数据 HTML 表格（Web 版，维度列动态跟随 SCORE_DIMS）"""
    from fund_scoring import SCORE_DIMS
    dim_names = [d[0] for d in SCORE_DIMS]
    parts = ['<div style="margin-top:16px;padding:0 10px;">'
             '<p style="margin:8px 0;font-size:13px;font-weight:600;color:#ccc;">\U0001f4ca 自选基金完整数据</p>'
             '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:12px;">'
             '<thead><tr style="background:#2a2a2a;">'
             '<th style="padding:4px 6px;text-align:left;color:#888;border-bottom:1px solid #333;white-space:nowrap;">代码</th>'
             '<th style="padding:4px 6px;text-align:left;color:#888;border-bottom:1px solid #333;white-space:nowrap;">基金名</th>'
             '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;white-space:nowrap;">涨跌</th>'
             '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;white-space:nowrap;">近5日</th>'
             '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;white-space:nowrap;">近1月</th>'
             '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;white-space:nowrap;">近3月</th>'
             '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;white-space:nowrap;">近1年</th>'
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
        parts.append(f'<td style="padding:3px 6px;border-bottom:1px solid #333;white-space:nowrap;color:#ccc;">{_html.escape(str(r.get("name_short","")))}{_warn}</td>')
        for col, fcol in (("day","_day"),("f5","_f5"),("m1","_m1"),("m3","_m3"),("y1","_y1")):
            _v = r.get(fcol, "")
            parts.append(f'<td style="padding:3px 6px;border-bottom:1px solid #333;text-align:right;font-family:Consolas;white-space:nowrap;{_color_inline(_v)}">{_html.escape(str(_v))}</td>')
        # 评分（带明细弹窗）
        _v_detail = r.get("_score_detail", [])
        _detail_json = json.dumps(_v_detail, ensure_ascii=False) if _v_detail else "[]"
        parts.append(f"<td style=\"padding:3px 6px;border-bottom:1px solid #333;text-align:right;font-family:Consolas;font-weight:600;color:#66bb6a;cursor:pointer;font-size:13px;\" onclick='showScoreDetail({_detail_json})'>{r.get('score','')}</td>")
        # 动态维度列
        for dim_name in dim_names:
            val = _get_dim_value(r, dim_name)
            raw_val = r.get(_dim_value_to_key(dim_name))
            if val in ("-", ""):
                color = "#555"
                style_extra = "font-style:italic;"
            else:
                color = "#bbb"
                style_extra = ""
                if isinstance(raw_val, (int, float)):
                    lower_better = dim_name in ("波动率", "最大回撤", "最大连跌天数", "费率")
                    if lower_better:
                        color = "#66bb6a" if raw_val <= 10 else ("#ef5350" if raw_val >= 30 else "#ffa726")
                    else:
                        color = "#66bb6a" if raw_val > 0 else ("#ef5350" if raw_val < 0 else "#bbb")
            parts.append(f'<td style="padding:3px 6px;border-bottom:1px solid #333;text-align:right;font-family:Consolas;color:{color};{style_extra}white-space:nowrap;">{val}</td>')
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
    dim_names = [d[0] for d in SCORE_DIMS]
    dims_shown = dim_names
    parts = ['<div style="margin-top:16px;padding:0 10px;">'
             f'<p style="margin:8px 0;font-size:13px;font-weight:600;color:#ccc;">\U0001f3c6 \u5e02\u573a\u4f18\u9009 TOP {_show_top} \uff08\u5168\u7ef4\u5ea6\uff09</p>'
             '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:12px;">'
             '<thead><tr style="background:#2a2a2a;">'
             '<th style="padding:4px 6px;text-align:center;color:#888;border-bottom:1px solid #333;white-space:nowrap;">#</th>'
             '<th style="padding:4px 6px;text-align:left;color:#888;border-bottom:1px solid #333;white-space:nowrap;">基金</th>'
             '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;white-space:nowrap;">总分</th>'
             '<th style="padding:4px 6px;text-align:right;color:#888;border-bottom:1px solid #333;white-space:nowrap;">年化%</th>']
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
        parts.append(f'<td style="padding:3px 6px;border-bottom:1px solid #333;color:#e0e0e0;white-space:nowrap;">{_html.escape(str(r.get("n","")))}{warn} <span style="color:#666;font-family:Consolas;font-size:12px;">{r.get("code","")}</span></td>')
        parts.append(f"<td style=\"padding:3px 6px;border-bottom:1px solid #333;text-align:right;font-family:Consolas;font-weight:600;color:#66bb6a;cursor:pointer;font-size:13px;\" onclick='showScoreDetail({detail_json})'>{r.get('score',0):.1f}</td>")
        parts.append(f'<td style="padding:3px 6px;border-bottom:1px solid #333;text-align:right;font-family:Consolas;color:#ccc;">{r.get("annual_return",0):.1f}</td>')
        for dim_name in dims_shown:
            val = _get_dim_value(r, dim_name)
            raw_val = r.get(_dim_value_to_key(dim_name))
            if val in ("-", ""):
                color = "#555"
                style_extra = 'font-style:italic;'
            else:
                color = "#bbb"
                style_extra = ""
                if isinstance(raw_val, (int, float)):
                    lower_better = dim_name in ("波动率", "最大回撤", "最大连跌天数", "费率")
                    if lower_better:
                        color = "#66bb6a" if raw_val <= 10 else ("#ef5350" if raw_val >= 30 else "#ffa726")
                    else:
                        color = "#66bb6a" if raw_val > 0 else ("#ef5350" if raw_val < 0 else "#bbb")
            parts.append('<td style="padding:3px 6px;border-bottom:1px solid #333;text-align:right;font-family:Consolas;color:' + color + ';' + style_extra + '">' + val + '</td>')
        parts.append('</tr>')
    parts.append('</tbody></table></div></div>')
    return "\n".join(parts)


def _save_briefing(rows: list[dict], alerts: list[str], today: str,
                   ranking_lines: list[str] | None = None) -> None:
    """保存晚报 HTML 到文件，供 Web 页面展示"""
    html = _build_briefing_html(rows, alerts, today, ranking_lines)
    if html is None:
        log.warning("email_template.html 不存在，跳过保存晚报")
        return
    try:
        # 生成邮件版简报（基础表格）
        web = html
        web = web.replace("background:#000", "background:#1a1a1a")
        web = web.replace('bgcolor="#000000"', '')
        web = web.replace('padding:20px 10px;', 'padding:0;')
        web = re.sub(r'<tr><td[^>]*>Fund Monitor[^<]*</td></tr>', '', web)
        web = re.sub(r'^<!DOCTYPE[^>]*>', '', web)
        web = re.sub(r'<html[^>]*>', '', web)
        web = re.sub(r'</html>', '', web)
        web = re.sub(r'<head>.*?</head>', '', web, flags=re.DOTALL)
        web = re.sub(r'<body[^>]*>', '', web)
        web = re.sub(r'</body>', '', web)
        web = re.sub(r'<center>', '', web)
        web = re.sub(r'</center>', '', web)
        # 注：完整基金数据表格和推荐全维度表格不放在此处——
        #     在「运行推荐」完成时由前端通过 /api/recommend-table 加载
        web = re.sub(r'\n{3,}', '\n\n', web)
        web = web.strip()
        # 注入评分明细弹窗 JS
        tmp_path = _BRIEFING_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(web)
        os.replace(tmp_path, _BRIEFING_FILE)
        log.info("晚报已保存到 %s (%d chars)", _BRIEFING_FILE, len(web))
    except Exception as e:
        log.warning("保存晚报失败: %s", e)


def _fmt(v) -> str:
    """格式化数值，None/空返回 '-'"""
    if v is None or v == "":
        return "-"
    if isinstance(v, float):
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
    }
    return m.get(dim_name)


def _get_dim_value(r: dict, dim_name: str) -> str:
    """根据维度名称从推荐结果中取值"""
    mapping = {
        "近1年收益": lambda: f'{r.get("y1", 0):.1f}' if isinstance(r.get("y1"), (int, float)) else str(r.get("y1", "")),
        "近3月收益": lambda: f'{r.get("m3", 0):.1f}' if isinstance(r.get("m3"), (int, float)) else str(r.get("m3", "")),
        "近1月收益": lambda: f'{r.get("m1", 0):.1f}' if isinstance(r.get("m1"), (int, float)) else str(r.get("m1", "")),
        "近一周收益": lambda: str(r.get("f5", "")),
        "近2年收益": lambda: f'{r.get("sy2", 0):.1f}' if isinstance(r.get("sy2"), (int, float)) else "",
        "夏普比率": lambda: _fmt(r.get("sharpe")),
        "上行胜率": lambda: _fmt(r.get("win_rate")),
        "盈亏比": lambda: _fmt(r.get("profit_ratio")),
        "索提诺比率": lambda: _fmt(r.get("sortino")),
        "修复系数": lambda: _fmt(r.get("recovery")),
        "近3年收益": lambda: f'{r.get("sy3", 0):.1f}' if isinstance(r.get("sy3"), (int, float)) else "",
        "近6月收益": lambda: f'{r.get("sy6", 0):.1f}' if isinstance(r.get("sy6"), (int, float)) else "",
        "波动率": lambda: _fmt(r.get("volatility")),
        "卡玛比率": lambda: _fmt(r.get("calmar")),
        "最大连跌天数": lambda: _fmt(r.get("max_loss_days")),
        "费率": lambda: _fmt(r.get("rate")),
        "最大回撤": lambda: _fmt(r.get("max_dd")),
        "基金规模": lambda: _fmt(r.get("sc")),
        "年化收益率": lambda: f'{r.get("annual_return", 0):.1f}' if isinstance(r.get("annual_return"), (int, float)) else str(r.get("annual_return", "")),
        "机构持有比例": lambda: _fmt(r.get("inst")),
    }
    fn = mapping.get(dim_name)
    return fn() if fn else "-"


def _load_saved_recommend_data() -> list[dict]:
    """从保存的结果文件直接读取推荐数据，并用当前 SCORE_DIMS 重新评分。

    关键修复：即使用户移除了某些维度并保存权重后,
    推荐列表的分数和明细会立刻反映最新维度配置，无需等待推荐子进程完成。
    """
    try:
        from fund_scoring import calc_score_detail
        data = _load_recommend_data()
        if not data:
            return []
        results = data.get("results", [])
        if not results:
            return []
        out = []
        for r in results[:_show_top]:
            entry = {
                "n": r.get("name", ""),
                "code": r.get("code", ""),
                "annual_return": r.get("annual_return", 0),
                "m1": r.get("m1"),
                "m3": r.get("m3"),
                "y1": r.get("y1"),
                "sharpe": r.get("sharpe"),
                "sortino": r.get("sortino"),
                "max_dd": r.get("max_dd"),
                "win_rate": r.get("win_rate"),
                "inst": r.get("inst"),
                "sc": r.get("sc"),
                "rate": r.get("rate"),
                "profit_ratio": r.get("profit_ratio"),
                "recovery": r.get("recovery"),
                "sy3": r.get("sy3"),
                "f5": r.get("f5"),
                "sy2": r.get("sy2"),
                "sy6": r.get("sy6"),
                "volatility": r.get("volatility"),
                "calmar": r.get("calmar"),
                "max_loss_days": r.get("max_loss_days"),
                "mgr": r.get("mgr", ""),
            }
            # 用当前 SCORE_DIMS 重新评分，确保已移除的维度不会残留在总分或明细中
            score_d = {k: entry.get(k) for k in (
                "y1", "m3", "m1", "f5", "sy6", "sy2", "sy3",
                "annual_return", "sharpe", "sortino",
                "profit_ratio", "win_rate", "recovery", "calmar",
                "max_dd", "volatility", "max_loss_days",
                "sc", "rate", "inst",
            )}
            score, details, skipped = calc_score_detail(score_d)
            entry["score"] = score
            entry["score_detail"] = details
            entry["_skipped_weight"] = skipped
            out.append(entry)
        return out
    except Exception:
        return []


def _fetch_fresh_recommend_data() -> list[dict]:
    """从推荐结果取 TOP 10 基金代码，实时拉取数据并重新评分"""
    try:
        from fund_recommend import _load_result
        recs = _load_result()
        if not recs:
            return []
        # 取推荐结果前10的代码
        codes = [(r.get("code", ""), r.get("name", "")) for r in recs[:_show_top]]
        codes = [(c, n) for c, n in codes if c]

        from fund_watch import get
        from fund_scoring import calc_score_detail, SCORE_DIMS

        fresh = []
        for code, cached_name in codes:
            try:
                d = get(code)
                if not d.get("n"):
                    continue
                # 补充日涨跌/近一周收益（get 不计算这些字段）
                navs = d.get("nav", [])
                td = d.get("td")
                if navs and len(navs) >= 2:
                    if len(navs) >= 5:
                        d["f5"] = f"{(navs[-1]['v'] - navs[-5]['v']) / navs[-5]['v'] * 100:+.1f}%"
                score_d = {
                    "annual_return": d.get("annual_return"),
                    "sharpe": d.get("sharpe"),
                    "sortino": d.get("sortino"),
                    "max_dd": d.get("max_dd"),
                    "win_rate": d.get("win_rate"),
                    "inst": d.get("inst"),
                    "sc": d.get("sc"),
                    "rate": d.get("rate"),
                    "profit_ratio": d.get("profit_ratio"),
                    "recovery": d.get("recovery"),
                    "y1": d.get("y1"),
                    "sy3": d.get("sy3"),
                    "m1": d.get("m1"),
                    "m3": d.get("m3"),
                    "sy6": d.get("sy6"),
                    "f5": d.get("f5"),
                    "sy2": d.get("sy2"),
                    "volatility": d.get("volatility"),
                    "calmar": d.get("calmar"),
                    "max_loss_days": d.get("max_loss_days"),
                }
                score, details, skipped = calc_score_detail(score_d)
                d["score"] = score
                d["score_detail"] = details
                d["_skipped_weight"] = skipped
                fresh.append(d)
            except Exception:
                continue

        fresh.sort(key=lambda r: r.get("score", 0), reverse=True)
        return fresh
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




def _format_recommend_rankings() -> list[str]:
    """生成推荐排名的 Markdown 表格（实时拉取）"""
    lines: list[str] = []
    try:
        from fund_recommend import _load_result, _TOP

        # 检查推荐数据是否存在及过期
        recs = _load_result()
        if recs:
            try:
                rec_date = recs[0].get("date", "")
                if rec_date:
                    rec_dt = datetime.date.fromisoformat(rec_date)
                    days_old = (datetime.date.today() - rec_dt).days
                    if days_old > 14:
                        lines.append("")
                        lines.append(f"⚠️ 推荐结果已是 {days_old} 天前的，建议重新运行")
                        lines.append(f"   python fund_recommend.py")
            except (ValueError, KeyError, TypeError):
                pass

        # 使用已保存的推荐结果数据（避免重复网络请求）
        fresh = _load_saved_recommend_data()
        if not fresh:
            lines.append("")
            lines.append("💡 **想看看市场上有哪些优秀基金？**")
            lines.append("   运行 python fund_recommend.py（约16分钟）")
            lines.append("   之后晚报自动展示推荐排行")
            return lines

        lines.append("")
        num_dims = len(SCORE_DIMS)
        lines.append(f"🏆 **市场优选基金 TOP {_show_top}**  （{num_dims} 维评分）")
        lines.append("")
        lines.append(f"|{'排名':<4}|{'代码':<7}|{'基金名':<18}|{'评分':<5}|{'年化%':<6}|{'近1月':<7}|{'近3月':<7}|{'近1年':<7}|{'夏普':<5}|{'回撤':<5}|{'近3年':<6}|")
        lines.append(f"|:---:|:---|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
        medals = ["🥇", "🥈", "🥉"]
        for i, r in enumerate(fresh[:_show_top], 1):
            badge = medals[i - 1] if i <= 3 else f" {i}."
            name = (r.get("n", "") or "")[:18]
            code = r.get("code", "")
            score = r.get("score", 0)
            ar = r.get("annual_return", 0) or 0
            m1 = f"{r.get('m1', 0):+.1f}" if isinstance(r.get("m1"), (int, float)) else str(r.get("m1", ""))
            m3 = f"{r.get('m3', 0):+.1f}" if isinstance(r.get("m3"), (int, float)) else str(r.get("m3", ""))
            y1 = f"{r.get('y1', 0):+.1f}" if isinstance(r.get("y1"), (int, float)) else str(r.get("y1", ""))
            sharpe = r.get("sharpe", 0) or 0
            dd = r.get("max_dd", 0) or 0
            sy3 = r.get("sy3", 0) or 0
            lines.append(f"|{badge:<4}|{code:<7}|{name:<18}|{score:<5.1f}|{ar:<6.1f}%|{m1:<7s}|{m3:<7s}|{y1:<7s}|{sharpe:<5.2f}|{dd:<5.1f}%|{sy3:<5.1f}%|")

        lines.append("")
        lines.append(f"  ── 排名依据：从全市场 {_TOP} 只基金中精选 TOP {_show_top} ──")
        lines.append("  📡 数据源：天天基金排行 + 东财净值 + 新浪行情 等综合数据")
        lines.append(f"     拉取全市场近 1 年收益排行前 {_TOP} 名，筛选后进入深度评分。")
        lines.append("     每只基金独立拉取净值数据，从净值数组真实计算各项指标。")
        num = len(SCORE_DIMS)
        lines.append(f"  🧮 评分方式：{num} 个维度加权打分（0-100 分），权重合计 100%")
        lines.append("")
        medals_cn = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟",
                     "1️⃣1️⃣","1️⃣2️⃣","1️⃣3️⃣","1️⃣4️⃣","1️⃣5️⃣","1️⃣6️⃣","1️⃣7️⃣","1️⃣8️⃣","1️⃣9️⃣","2️⃣0️⃣"]
        for i, (name, fn, weight, desc) in enumerate(SCORE_DIMS):
            badge = medals_cn[i] if i < len(medals_cn) else f"  {i+1}."
            lines.append(f"  {badge} {name}（{int(weight*100)}%）— {desc}")

    except Exception as e:
        lines.append(f"⚠️ 推荐排行加载失败: {e}")

    return lines


# ── 主程序 ────────────────────────────────────


