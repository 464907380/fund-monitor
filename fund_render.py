"""
基金渲染/推送模块
"""
import datetime
import json
import os
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


# ── 推荐结果文件（需要 HISTORY_DIR 定义后）──
_RECOMMEND_RESULT_FILE = os.path.join(HISTORY_DIR, ".fund_recommend_result.json")

_BRIEFING_FILE = os.path.join(HISTORY_DIR, ".briefing_fund.html")
"""晚报 HTML 文件（供 Web 页面展示）"""


# ── 推送 ──────────────────────────────────────

def _pipe_table_to_html(ranking_lines: list[str]) -> str:
    """将 Markdown 管道表行列表转为 HTML <table> 字符串"""
    cp = '<tr><td style="padding:12px 14px;background:#222;border:1px solid #333;border-radius:6px;">'
    cp += '<p style="margin:0 0 8px;font-size:14px;font-weight:600;color:#ccc;">🏆 市场优选基金 TOP 10 （12 维评分）</p>'
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
                    cp += f'<td style="padding:3px 6px;text-align:center;border-bottom:1px solid #333;color:#bbb;white-space:nowrap;">{_html.escape(c.strip())}</td>'
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
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;font-family:Consolas;font-size:11px;color:#888;white-space:nowrap;">{_code}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;font-size:13px;color:#ccc;white-space:nowrap;">{_name}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["day"])}">{_day}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["m1"])}">{_m1}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["m3"])}">{_m3}</td>'
            + f'<td style="padding:6px 4px;border-bottom:1px solid #333;text-align:right;font-weight:600;font-family:Consolas;font-size:12px;white-space:nowrap;{_color_inline(r["y1"])}">{_y1}</td>'
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
        "|代码|基金名|涨跌|近5日|近1月|近3月|近1年|经理|",
        "|:---|:---|---:|----:|----:|----:|----:|:---|",
    ]
    for r in rows:
        md_lines.append(
            f"|{r['code']}|{r['name_short']}|{r['day']}|{r['f5']}|{r['m1']}|{r['m3']}|{r['y1']}|{r['mgr']}|"
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
            md_lines.append(f"> {a}")
    return "\n".join(md_lines)

# ── 数据获取 ──────────────────────────────────


def _save_briefing(rows: list[dict], alerts: list[str], today: str,
                   ranking_lines: list[str] | None = None) -> None:
    """保存晚报 HTML 到文件，供 Web 页面展示"""
    html = _build_briefing_html(rows, alerts, today, ranking_lines)
    if html is None:
        log.warning("email_template.html 不存在，跳过保存晚报")
        return
    try:
        with open(_BRIEFING_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        log.info("晚报已保存到 %s", _BRIEFING_FILE)
    except OSError as e:
        log.warning("保存晚报失败: %s", e)


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
    """展示市场优选基金排行（来自上次推荐结果）"""
    data = _load_recommend_data()
    recs = data.get("results", []) if data else None
    lines: list[str] = []
    if not recs:
        lines.append("")
        lines.append("💡 **想看看市场上有哪些优秀基金？**")
        lines.append("   运行 python fund_recommend.py（约4分钟）")
        lines.append("   之后晚报自动展示推荐排行")
        return lines

    # 检查推荐结果是否过旧
    if data:
        try:
            rec_date = data.get("date", "")
            if rec_date:
                rec_dt = datetime.date.fromisoformat(rec_date)
                days_old = (datetime.date.today() - rec_dt).days
                if days_old > 14:
                    lines.append("")
                    lines.append(f"⚠️ 推荐结果已是 {days_old} 天前的，建议重新运行")
                    lines.append(f"   python fund_recommend.py")
        except (ValueError, KeyError, TypeError):
            pass

    lines.append("")
    lines.append("🏆 **市场优选基金 TOP 10**  （12 维评分）")
    lines.append("")
    lines.append(f"|{'排名':<4}|{'基金名':<14}|{'年化%':<6}|{'近1月':<7}|{'近3月':<7}|{'近1年':<7}|{'夏普':<5}|{'回撤':<5}|{'近3年':<6}|")
    lines.append(f"|:---:|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(recs[:10], 1):
        badge = medals[i - 1] if i <= 3 else f" {i}."
        name = r.get("name", "")[:14]
        ar = r.get("annual_return", 0)
        m1 = str(r.get("m1", ""))
        m3 = str(r.get("m3", ""))
        y1 = str(r.get("y1", ""))
        sharpe = r.get("sharpe", 0)
        dd = r.get("max_dd", 0)
        sy3 = 0 if r.get("sy3") is None else r["sy3"]
        lines.append(f"|{badge:<4}|{name:<14}|{ar:<6.1f}%|{m1:<7s}|{m3:<7s}|{y1:<7s}|{sharpe:<5.2f}|{dd:<5.1f}%|{sy3:<5.1f}%|")

    lines.append("")
    lines.append("  ── 排名依据：从全市场 500 只基金中精选 TOP 10 ──")
    lines.append("  📡 数据源：天天基金排行 API（https://fund.eastmoney.com）")
    lines.append("     拉取全市场近 1 年收益排行前 500 名（不限类型），")
    lines.append("     再剔除近 1 年收益为负的基金，其余全部进入深度评分。")
    lines.append("     每只基金独立拉取净值数据，从净值数组真实计算各项指标。")
    num = len(SCORE_DIMS)
    lines.append(f"  🧮 评分方式：{num} 个维度加权打分（0-100 分），权重合计 100%")
    lines.append("")
    medals_cn = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟","1️⃣1️⃣","1️⃣2️⃣","1️⃣3️⃣","1️⃣4️⃣","1️⃣5️⃣"]
    for i, (name, fn, weight, desc) in enumerate(SCORE_DIMS):
        badge = medals_cn[i] if i < len(medals_cn) else f"  {i+1}."
        lines.append(f"  {badge} {name}（权重 {int(weight*100)}%）")
        lines.append(f"      → {desc}")

    return lines


# ── 主程序 ────────────────────────────────────


