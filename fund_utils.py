"""
公共基础设施：网络请求、缓存、推送、日志
从 fund_watch.py 提取，供 fund_monitor.py / global_briefing.py 复用
"""
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
import smtplib
from logging.handlers import RotatingFileHandler
from config import CFG, get_secret

# ── 路径 ──────────────────────────────────────
HISTORY_DIR = os.path.dirname(os.path.abspath(__file__))

# ── 日志 ──────────────────────────────────────
_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _handlers.insert(0, RotatingFileHandler(
        os.path.join(HISTORY_DIR, "fund_watch.log"),
        maxBytes=5 * 1024 * 1024, backupCount=3,
    ))
except OSError:
    pass  # 日志目录不可写时只用控制台输出
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger(__name__)

# ── 网络缓存 ──────────────────────────────────
_cache: dict[str, tuple[float, str]] = {}       # url -> (timestamp, data)
_cache_lock = threading.Lock()
_CACHE_TTL = CFG.get("network", {}).get("cache_ttl_seconds", 300)
_CACHE_MAX = CFG.get("network", {}).get("cache_max_entries", 100)
_RETRY_MAX = CFG.get("network", {}).get("retry_max", 3)
_RETRY_BACKOFF = CFG.get("network", {}).get("retry_backoff_seconds", [1, 3, 8])


def _cache_evict() -> None:
    """清除过期缓存；超出上限时清除最旧的条目"""
    now = time.time()
    with _cache_lock:
        expired = [k for k, (t, _) in _cache.items() if now - t > _CACHE_TTL]
        for k in expired:
            del _cache[k]
        if len(_cache) > _CACHE_MAX:
            sorted_items = sorted(_cache.items(), key=lambda kv: kv[1][0])
            for k, _ in sorted_items[:len(sorted_items) // 2]:
                del _cache[k]
    log.debug("缓存清理: 过期 %d, 当前 %d 条", len(expired), len(_cache))


def _request_with_retry(req: urllib.request.Request, decode: bool = True) -> str | bytes | None:
    """带指数退避的 HTTP 请求，返回 str（decode=True）或 bytes（decode=False），失败返回 None"""
    last_err = None
    for attempt in range(1, _RETRY_MAX + 1):
        try:
            resp = urllib.request.urlopen(req, timeout=15).read()
            if decode:
                return resp.decode("utf-8", errors="ignore")  # type: ignore[no-any-return]
            return resp  # type: ignore[no-any-return]
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            last_err = e
            if attempt < _RETRY_MAX:
                wait = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
                time.sleep(wait)
    log.warning("请求失败 %s (已重试 %d 次) %s", req.full_url, _RETRY_MAX, last_err)
    return None


def _retry_fetch(url: str) -> str:
    """带指数退避的 HTTP GET 请求"""
    _cache_evict()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    result = _request_with_retry(req, decode=True)
    return result if isinstance(result, str) else ""


def fetch(url: str) -> str:
    """带缓存的 HTTP GET"""
    with _cache_lock:
        entry = _cache.get(url)
        if entry:
            ts, data = entry
            if time.time() - ts <= _CACHE_TTL:
                return data
            del _cache[url]
    resp = _retry_fetch(url)
    with _cache_lock:
        _cache[url] = (time.time(), resp)
    return resp


def clear_cache() -> None:
    """清空所有缓存（供外部强制刷新使用）"""
    with _cache_lock:
        _cache.clear()


def fetch_bytes(url: str, headers: dict | None = None) -> bytes | None:
    """带指数退避的 HTTP GET，返回原始 bytes（不缓存，供新浪等非标准编码使用）"""
    _cache_evict()
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    result = _request_with_retry(req, decode=False)
    return result if isinstance(result, bytes) else None


def parse_sina_csv(data: str | bytes, encoding: str = "utf-8") -> list[str] | None:
    """解析新浪财经 CSV 数据，返回字段列表"""
    if isinstance(data, bytes):
        text = data.decode(encoding, errors="ignore")
    else:
        text = data
    m = re.search(r'"(.*?)"', text)
    if not m:
        return None
    parts = m.group(1).split(",")
    return parts if len(parts) >= 4 else None


# ── 颜色与文本工具 ────────────────────────────

def _color_cls(val: str) -> str:
    """数值颜色 class：涨红跌绿"""
    if not val:
        return ""
    if val.startswith("+"):
        return ' class="red"'
    if val.startswith("-"):
        return ' class="green"'
    return ""


def _color_inline(val: str) -> str:
    """数值颜色内联样式：涨红跌绿（深色背景优化）"""
    if not val:
        return ""
    if val.startswith("+"):
        return "color:#ef5350;"
    if val.startswith("-"):
        return "color:#66bb6a;"
    return ""


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# ── 推送 ──────────────────────────────────────

def _send_smtp(msg: MIMEText) -> None:
    """发送 SMTP 邮件（QQ 邮箱）"""
    qq_email = get_secret("QQ_EMAIL")
    qq_auth = get_secret("QQ_MAIL_AUTH")
    s = None
    try:
        s = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=10)
        s.login(qq_email, qq_auth)
        s.sendmail(qq_email, [qq_email], msg.as_string())
        log.info("邮件发送成功")
    except Exception as e:
        log.error("邮件发送失败: %s", e)
    finally:
        if s:
            try:
                s.quit()
            except Exception:
                pass


def send_wechat(content: str, markdown: bool = True) -> bool:
    """发送企业微信消息"""
    webhook = get_secret("WECHAT_WEBHOOK")
    if not webhook:
        return False
    msgtype = "markdown" if markdown else "text"
    payload = json.dumps({"msgtype": msgtype, msgtype: {"content": content}}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
        log.info("企业微信推送成功")
        return True
    except Exception as e:
        log.error("企业微信推送失败: %s", e)
        return False


def send_mail(subject: str, text: str) -> None:
    """通过 QQ 邮箱发送纯文本邮件"""
    qq_email = get_secret("QQ_EMAIL")
    qq_auth = get_secret("QQ_MAIL_AUTH")
    if not qq_email or not qq_auth:
        log.debug("QQ_EMAIL 或 QQ_MAIL_AUTH 未配置，邮件推送跳过")
        return
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")  # type: ignore[assignment]
    msg["From"] = msg["To"] = qq_email
    _send_smtp(msg)


def send_mail_html(subject: str, html: str) -> None:
    """通过 QQ 邮箱发送 HTML 邮件"""
    qq_email = get_secret("QQ_EMAIL")
    qq_auth = get_secret("QQ_MAIL_AUTH")
    if not qq_email or not qq_auth:
        log.debug("QQ_EMAIL 或 QQ_MAIL_AUTH 未配置，邮件推送跳过")
        return
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")  # type: ignore[assignment]
    msg["From"] = msg["To"] = qq_email
    _send_smtp(msg)
