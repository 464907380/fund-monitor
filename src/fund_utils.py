"""
公共基础设施：网络请求、缓存、推送、日志
从 fund_watch.py 提取，供 fund_monitor.py / global_briefing.py 复用
"""
# mypy: ignore-errors
import datetime
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
from config import CFG, get_secret, get_timeout, api_url

# ── 交易日检测 ──────────────────────────────────

_HOLIDAY_CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".holiday_cache.json")
_HOLIDAY_CACHE_TTL = CFG.get("fund_monitor", {}).get("holiday_cache_ttl", 86400)


def _load_holiday_cache() -> dict:
    if os.path.exists(_HOLIDAY_CACHE_FILE):
        try:
            with open(_HOLIDAY_CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.debug("节假日缓存读取失败，重新获取")
    return {}


def _save_holiday_cache(data: dict) -> None:
    try:
        with open(_HOLIDAY_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log.debug("保存节假日缓存失败: %s", e)


def is_holiday_api(date_str: str) -> bool | None:
    """调用节假日 API 判断是否为非交易日。返回 True=非交易日, False=交易日, None=API 不可用。"""
    cache = _load_holiday_cache()
    now_ts = time.time()
    if date_str in cache:
        entry = cache[date_str]
        if now_ts - entry.get("ts", 0) < _HOLIDAY_CACHE_TTL:
            return entry["holiday"]
    try:
        data = fetch(api_url("holiday", date=date_str))
        j = json.loads(data)
        if j.get("code") == 0 and "type" in j.get("type", {}):
            holiday = j["type"]["type"] != 0
            log.debug("节假日 API: %s -> %s", date_str, "非交易日" if holiday else "交易日")
            cache[date_str] = {"holiday": holiday, "ts": now_ts}
            _save_holiday_cache(cache)
            return holiday
    except Exception as e:
        log.debug("节假日 API 请求失败: %s", e)
    return None


def is_trading_day(d: datetime.date) -> bool:
    """判断指定日期是否为交易日：1. API检测(优先) 2. 周末判断 3. 固定假日列表"""
    api_result = is_holiday_api(d.isoformat())
    if api_result is not None:
        return not api_result
    if d.weekday() >= 5:
        return False
    return True


# ── 路径 ──────────────────────────────────────
HISTORY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 日志 ──────────────────────────────────────
_handlers: list[logging.Handler] = [logging.StreamHandler()]
_log_name = "fund_watch.log"

def setup_log(name: str) -> None:
    """设置日志文件名，不同进程用不同文件名避免冲突"""
    global _log_name, _handlers
    _log_name = name
    _handlers = [logging.StreamHandler()]
    try:
        _handlers.insert(0, RotatingFileHandler(
            os.path.join(HISTORY_DIR, name),
            maxBytes=5 * 1024 * 1024, backupCount=3,
        ))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=_handlers,
        force=True,
    )

try:
    _handlers.insert(0, RotatingFileHandler(
        os.path.join(HISTORY_DIR, _log_name),
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
            resp = urllib.request.urlopen(req, timeout=get_timeout("request_with_retry", 15)).read()
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


def _retry_fetch(url: str, headers: dict | None = None) -> str:
    """带指数退避的 HTTP GET 请求"""
    _cache_evict()
    req_headers = {"User-Agent": "Mozilla/5.0"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    result = _request_with_retry(req, decode=True)
    return result if isinstance(result, str) else ""


def fetch(url: str, headers: dict | None = None) -> str:
    """带缓存的 HTTP GET，可传自定义 headers"""
    with _cache_lock:
        entry = _cache.get(url)
        if entry:
            ts, data = entry
            if time.time() - ts <= _CACHE_TTL:
                return data
            del _cache[url]
    resp = _retry_fetch(url, headers)
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


def _color_inline(val: float | str | None) -> str:
    """数值颜色内联样式：涨红跌绿（深色背景优化）"""
    if val is None:
        return ""
    if isinstance(val, (int, float)):
        return "color:#ef5350;" if val > 0 else "color:#66bb6a;" if val < 0 else ""
    s = str(val)
    if s.startswith("+"):
        return "color:#ef5350;"
    if s.startswith("-"):
        return "color:#66bb6a;"
    return ""


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# ── 基金实时估算（fund_watch 和 fund_monitor 共用） ──────────

def _fetch_fund_estimate(code: str) -> tuple[str, float] | None:
    """获取基金当日涨跌幅，优先返回实际净值，降级到实时估算。
    
    优先级：
      1. 天天基金历史净值 API（实际净值，收盘后可用）
      2. 天天基金实时估值 API（盘中估算）
      3. 新浪财经基金行情（最终降级）
    返回 (基金名, 涨跌幅%)
    """
    import urllib.request
    import datetime

    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    # 判断是否收盘（15:00 之后）
    is_after_market = now.hour > 15 or (now.hour == 15 and now.minute >= 0)

    # 1. 先尝试实际净值（历史净值 API）
    actual: tuple[str, float] | None = None
    try:
        url = f"https://api.fund.eastmoney.com/f10/lsjz?callback=j&fundCode={code}&pageIndex=1&pageSize=1"
        req = urllib.request.Request(url, headers={"Referer": "https://fund.eastmoney.com/", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=get_timeout("default", 10)) as r:
            gz_data = r.read().decode("utf-8")
        m_date = re.search(r'FSRQ":"(\d{4}-\d{2}-\d{2})"', gz_data)
        m_val = re.search(r'"JZZZL":"([-+\d.]+)"', gz_data)
        if m_date and m_val and m_date.group(1) == today_str:
            actual = (code, float(m_val.group(1)))
    except Exception:
        pass

    # 收盘后直接返回实际净值（不纠结估算值）
    if is_after_market and actual is not None:
        return actual

    # 2. 盘中或实际净值不可用 → 尝试实时估算
    for url in [api_url("fund_estimate", code=code), api_url("fund_estimate_fallback", code=code)]:
        try:
            gz = fetch(url)
            json_str = re.sub(r"^\w+\(", "", gz).rstrip(");")
            data = json.loads(json_str)
            return (data.get("name", code), float(data["gszzl"]))
        except Exception:
            continue

    # 如果估算失败但实际净值可用，返回实际净值
    if actual is not None:
        return actual

    # 3. 新浪财经基金行情（最终降级）
    try:
        url = f"http://hq.sinajs.cn/list=of{code}"
        req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=get_timeout("default", 10)) as r:
            raw = r.read()
        gz = raw.decode("gbk")
        m = re.search(r'"([^,]*),([-\d.]+),[-\d.]+,([-\d.]+),([-\d.]+),(\d{4}-\d{2}-\d{2})"', gz)
        if m:
            return m.group(1), float(m.group(4))
    except Exception:
        pass

    return None


# ── 推送 ──────────────────────────────────────

def _send_smtp(msg: MIMEText) -> None:
    """发送 SMTP 邮件（QQ 邮箱）"""
    qq_email = get_secret("QQ_EMAIL")
    qq_auth = get_secret("QQ_MAIL_AUTH")
    s = None
    try:
        s = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=get_timeout("smtp", 10))
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
        urllib.request.urlopen(req, timeout=get_timeout("wechat_push", 10)).read()
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

# ── 心跳监控（运行状态追踪） ──────────────────
_HEARTBEAT_DIR = os.path.join(HISTORY_DIR, ".heartbeats")


def _ensure_heartbeat_dir() -> None:
    os.makedirs(_HEARTBEAT_DIR, exist_ok=True)


def write_heartbeat(name: str, **kwargs) -> None:
    _ensure_heartbeat_dir()
    path = os.path.join(_HEARTBEAT_DIR, f"{name}.json")
    try:
        hb = {"name": name, "start": time.time(),
              "start_str": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "pid": os.getpid(), "progress": 0, "total": 0, "status": ""}
        hb.update(kwargs)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hb, f)
        os.replace(tmp, path)
    except Exception as e:
        log.debug("写入心跳失败 %s: %s", name, e)


def update_heartbeat(name: str, **kwargs) -> None:
    """更新心跳中的 progress/status 等字段，不重置 start/pid"""
    _ensure_heartbeat_dir()
    path = os.path.join(_HEARTBEAT_DIR, f"{name}.json")
    try:
        hb = read_heartbeat(name) or {"name": name, "start": time.time(),
                                       "start_str": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                       "pid": os.getpid()}
        hb.update(kwargs)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hb, f)
        os.replace(tmp, path)
    except Exception as e:
        log.debug("更新心跳失败 %s: %s", name, e)


def clear_heartbeat(name: str) -> None:
    path = os.path.join(_HEARTBEAT_DIR, f"{name}.json")
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        log.debug("清除心跳失败 %s: %s", name, e)


def read_heartbeat(name: str) -> dict | None:
    path = os.path.join(_HEARTBEAT_DIR, f"{name}.json")
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def read_all_heartbeats() -> dict[str, dict]:
    _ensure_heartbeat_dir()
    result = {}
    try:
        for fname in os.listdir(_HEARTBEAT_DIR):
            if fname.endswith(".json"):
                name = fname[:-5]
                hb = read_heartbeat(name)
                if hb:
                    result[name] = hb
    except Exception:
        pass
    return result


def is_heartbeat_alive(name: str, timeout: int = 1800) -> bool:
    """判断心跳是否存活（未超时），timeout=30分钟"""
    hb = read_heartbeat(name)
    if hb is None:
        return False
    start = hb.get("start")
    if not start:
        return False
    return time.time() - start < timeout

