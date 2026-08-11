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


# ── 跨进程文件锁（server/recommend/monitor 共享磁盘缓存时，保证读-改-写互斥）──
# Windows 用 msvcrt 独占锁，Linux 用 fcntl；进程内同文件用 threading 锁兜底防重入。
class _InterProcessLock:
    """跨进程互斥锁：对指定文件加独占锁，避免多进程同时写共享缓存互相覆盖。
    用法：with inter_process_lock(path): ...（内部自动加进程内防重入锁）"""

    def __init__(self, path: str, timeout: float = 15.0):
        self._path = path + ".lock"
        self._timeout = timeout
        self._fh = None
        self._proc_lock = threading.Lock()

    def acquire(self) -> None:
        import time as _t
        # 进程内防重入（同一进程内多线程写同一缓存）
        self._proc_lock.acquire()
        _deadline = _t.time() + self._timeout
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        while True:
            try:
                self._fh = open(self._path, "a+")
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (OSError, IOError):
                if self._fh:
                    try:
                        self._fh.close()
                    except Exception:
                        pass
                    self._fh = None
                if _t.time() >= _deadline:
                    # 超时仍强行继续（避免死锁阻塞业务），进程内锁已保证同进程互斥
                    log.warning("跨进程锁等待超时: %s（继续执行）", self._path)
                    return
                _t.sleep(0.05)

    def release(self) -> None:
        if self._fh:
            try:
                if os.name == "nt":
                    import msvcrt
                    try:
                        self._fh.seek(0)
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    try:
                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        self._proc_lock.release()

    def __enter__(self) -> "_InterProcessLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def inter_process_lock(path: str, timeout: float = 15.0) -> _InterProcessLock:
    """对共享缓存文件加跨进程锁，保证多进程读-改-写互斥"""
    return _InterProcessLock(path, timeout)


# ── 基金净值走势磁盘缓存（推荐进程与 Web 服务器共享，避免重复请求）──
# 存储：SQLite（WAL 模式，跨进程并发安全），替代原 111MB 单 JSON 文件。
# 对外接口 _load/_save/_get/_set_fund_trend_cache 保持原签名，调用点零改动。
_TREND_CACHE_PATH = os.path.join(HISTORY_DIR, "data", "fund_trend_cache.db")
_TREND_DISK_LOCK = threading.Lock()
# 进程内缓存 + 文件 mtime，避免评分阶段每只基金都重读整个 DB
_TREND_CACHE_MEM: dict | None = None
_TREND_CACHE_MTIME: float = -1.0
_TREND_DB_INIT: bool = False


def _trend_db_conn() -> "object":
    """获取 SQLite 连接（WAL 模式，允许跨进程并发读写）"""
    import sqlite3
    _conn = sqlite3.connect(_TREND_CACHE_PATH, timeout=30, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


def _trend_migrate_from_json() -> bool:
    """从旧 JSON 文件迁移到 SQLite（一次性）。返回是否迁移成功/已有数据。"""
    global _TREND_DB_INIT
    if _TREND_DB_INIT:
        return True
    _TREND_DB_INIT = True
    try:
        _json_path = os.path.join(HISTORY_DIR, "data", "fund_trend_cache.json")
        if not os.path.exists(_json_path):
            return True  # 无旧文件，直接使用空库
        try:
            _conn = _trend_db_conn()
            _conn.execute("CREATE TABLE IF NOT EXISTS fund_trend (code TEXT PRIMARY KEY, date TEXT, navs TEXT)")
            _cnt = _conn.execute("SELECT COUNT(*) FROM fund_trend").fetchone()[0]
            if _cnt > 0:
                return True  # 已迁移过
            with open(_json_path, encoding="utf-8") as _f:
                _data = json.load(_f)
            _conn.executemany(
                "INSERT OR REPLACE INTO fund_trend (code, date, navs) VALUES (?, ?, ?)",
                [(c, e.get("date", ""), json.dumps(e.get("navs", []), ensure_ascii=False))
                 for c, e in _data.items()]
            )
            _conn.commit()
            _conn.close()
            log.info("基金净值走势缓存已从 JSON 迁移到 SQLite: %d 只", len(_data))
            return True
        except Exception:
            return False  # 迁移失败，回退 JSON
    except Exception:
        return False


def _load_fund_trend_cache() -> dict:
    """读取净值走势磁盘缓存 {code: {date, navs:[[d,v],...]}}（进程内缓存+mtime 检测）
    优先 SQLite，失败回退 JSON。"""
    global _TREND_CACHE_MEM, _TREND_CACHE_MTIME
    try:
        if not _trend_migrate_from_json():
            return _load_fund_trend_cache_json()
        if os.path.exists(_TREND_CACHE_PATH):
            _mtime = os.path.getmtime(_TREND_CACHE_PATH)
            if _TREND_CACHE_MEM is not None and _mtime == _TREND_CACHE_MTIME:
                return _TREND_CACHE_MEM
            try:
                _conn = _trend_db_conn()
                _rows = _conn.execute("SELECT code, date, navs FROM fund_trend").fetchall()
                _conn.close()
                _mem = {}
                for _c, _d, _n in _rows:
                    try:
                        _mem[_c] = {"date": _d, "navs": json.loads(_n)}
                    except Exception:
                        pass
                _TREND_CACHE_MEM = _mem
                _TREND_CACHE_MTIME = _mtime
                return _mem
            except Exception:
                return _load_fund_trend_cache_json()
    except Exception:
        return _load_fund_trend_cache_json()
    return {}


def _load_fund_trend_cache_json() -> dict:
    """回退：读取旧 JSON 缓存"""
    _json_path = os.path.join(HISTORY_DIR, "data", "fund_trend_cache.json")
    try:
        if os.path.exists(_json_path):
            with open(_json_path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_fund_trend_cache(cache: dict) -> None:
    """批量写入净值走势缓存到 SQLite（原子事务），失败回退 JSON。"""
    global _TREND_CACHE_MEM, _TREND_CACHE_MTIME
    try:
        if not _trend_migrate_from_json():
            return _save_fund_trend_cache_json(cache)
        os.makedirs(os.path.dirname(_TREND_CACHE_PATH), exist_ok=True)
        _conn = _trend_db_conn()
        _conn.execute("CREATE TABLE IF NOT EXISTS fund_trend (code TEXT PRIMARY KEY, date TEXT, navs TEXT)")
        _conn.executemany(
            "INSERT OR REPLACE INTO fund_trend (code, date, navs) VALUES (?, ?, ?)",
            [(c, e.get("date", ""), json.dumps(e.get("navs", []), ensure_ascii=False))
             for c, e in cache.items()]
        )
        _conn.commit()
        _conn.close()
        _TREND_CACHE_MEM = cache
        _TREND_CACHE_MTIME = os.path.getmtime(_TREND_CACHE_PATH)
    except Exception:
        _save_fund_trend_cache_json(cache)


def _save_fund_trend_cache_json(cache: dict) -> None:
    """回退：写入旧 JSON 缓存"""
    global _TREND_CACHE_MEM, _TREND_CACHE_MTIME
    try:
        _json_path = os.path.join(HISTORY_DIR, "data", "fund_trend_cache.json")
        os.makedirs(os.path.dirname(_json_path), exist_ok=True)
        _tmp = _json_path + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(_tmp, _json_path)
        _TREND_CACHE_MEM = cache
        _TREND_CACHE_MTIME = os.path.getmtime(_json_path)
    except Exception:
        pass


def _get_fund_trend_navs(code: str) -> list | None:
    """读取当天净值走势缓存，返回 [{d,v}] 或 None（无缓存/已过期）"""
    # 优先 SQLite 单行查询（快，不必全量加载）
    try:
        if _trend_migrate_from_json() and os.path.exists(_TREND_CACHE_PATH):
            _conn = _trend_db_conn()
            _row = _conn.execute("SELECT date, navs FROM fund_trend WHERE code=?", (code,)).fetchone()
            _conn.close()
            if _row:
                today = datetime.datetime.now().strftime("%Y-%m-%d")
                if _row[0] == today:
                    try:
                        return [{"d": d, "v": v} for d, v in json.loads(_row[1])]
                    except Exception:
                        pass
            return None
    except Exception:
        pass
    entry = _load_fund_trend_cache().get(code)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if entry and entry.get("date") == today and entry.get("navs"):
        return [{"d": d, "v": v} for d, v in entry["navs"]]
    return None


def _set_fund_trend_navs(code: str, navs: list) -> None:
    """把 [{d,v}] 写入当天净值走势缓存（跨进程共享，SQLite upsert 单行）"""
    if not navs:
        return
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    try:
        if _trend_migrate_from_json():
            _conn = _trend_db_conn()
            _conn.execute("CREATE TABLE IF NOT EXISTS fund_trend (code TEXT PRIMARY KEY, date TEXT, navs TEXT)")
            _conn.execute(
                "INSERT OR REPLACE INTO fund_trend (code, date, navs) VALUES (?, ?, ?)",
                (code, today, json.dumps([[n["d"], n["v"]] for n in navs], ensure_ascii=False))
            )
            _conn.commit()
            _conn.close()
            return
    except Exception:
        pass
    # 回退：全量内存更新 + 落盘
    with _TREND_DISK_LOCK:
        cache = _load_fund_trend_cache()
        cache[code] = {"date": today, "navs": [[n["d"], n["v"]] for n in navs]}
        _save_fund_trend_cache(cache)


# ── 日志 ──────────────────────────────────────
_handlers: list[logging.Handler] = [logging.StreamHandler()]
_log_name = "fund_watch.log"


def setup_log(name: str) -> None:
    """设置日志文件名，不同进程用不同文件名避免冲突。
    格式带 模块/函数/行号，方便快速定位日志来源。"""
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
        format="%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d | %(message)s",
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
    format="%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d | %(message)s",
    handlers=_handlers,
)
log = logging.getLogger(__name__)


def safe_call(name: str, fn, fallback=None, log_level: int = logging.WARNING, exc: bool = True):
    """统一"捕获异常 + 记录 + 降级"的工具，替代散落的 `except Exception: pass`。

    Args:
        name: 调用标识（如 "settle_estimate_errors"），用于日志定位
        fn: 要执行的可调用对象
        fallback: 异常时的降级返回值（默认 None）
        log_level: 日志级别（默认 WARNING；预期会失败的路径可传 DEBUG）
        exc: 是否记录 traceback（默认 True，便于排查；高频/预期失败路径可传 False）

    Returns:
        fn() 的成功结果，或 fallback。

    Example:
        data = safe_call("_parse_holdings(002910)", lambda: _parse_holdings("002910"), fallback=None)
    """
    try:
        return fn()
    except Exception as e:
        log.log(log_level, "safe_call %s 失败: %s", name, e, exc_info=exc)
        return fallback


# ── 网络缓存 ──────────────────────────────────
_cache: dict[str, tuple[float, str]] = {}       # url -> (timestamp, data)
_cache_lock = threading.Lock()
_CACHE_TTL = CFG.get("network", {}).get("cache_ttl_seconds", 300)
_CACHE_MAX = CFG.get("network", {}).get("cache_max_entries", 100)
_RETRY_MAX = CFG.get("network", {}).get("retry_max", 3)
_RETRY_BACKOFF = CFG.get("network", {}).get("retry_backoff_seconds", [1, 3, 8])

# ── 域名级限速器（防止触发 API 频率限制） ──────
_RATE_LIMIT_DELAY = CFG.get("network", {}).get("rate_limit_delay", 0.3)
_last_request_time: dict[str, float] = {}
_domain_locks: dict[str, threading.Lock] = {}  # 每域名一把锁，不同域名可并行
_rate_limit_guard = threading.Lock()  # 仅保护 _domain_locks 字典本身（不参与 sleep）


def _rate_limit_domain(url: str) -> None:
    """对同一域名施加最小请求间隔，防止触发 API 频率限制（如东方财富 514）。
    不同域名各自独立限速（互不阻塞），避免全局锁把多域名并发请求串行化。"""
    from urllib.parse import urlparse
    domain = urlparse(url).hostname or "unknown"
    # 取该域名的专属锁（不同域名并行，同一域名才串行）
    with _rate_limit_guard:
        _lock = _domain_locks.get(domain)
        if _lock is None:
            _lock = threading.Lock()
            _domain_locks[domain] = _lock
    with _lock:
        last = _last_request_time.get(domain, 0.0)
        now = time.time()
        elapsed = now - last
        if elapsed < _RATE_LIMIT_DELAY:
            time.sleep(_RATE_LIMIT_DELAY - elapsed)
        _last_request_time[domain] = time.time()


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
    # 限速：同一域名至少间隔 _RATE_LIMIT_DELAY 秒
    _rate_limit_domain(req.full_url)
    last_err = None
    for attempt in range(1, _RETRY_MAX + 1):
        try:
            # 用 with 确保 response 被正确关闭，防止连接/句柄泄漏导致进程 OOM 被杀
            with urllib.request.urlopen(req, timeout=get_timeout("request_with_retry", 15)) as _resp:
                _raw = _resp.read()
            if decode:
                return _raw.decode("utf-8", errors="ignore")  # type: ignore[no-any-return]
            return _raw  # type: ignore[no-any-return]
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

# 基金代码→名称映射缓存（懒加载天天基金全市场索引，线程安全）
_FUND_NAME_MAP: dict[str, str] | None = None
_FUND_NAME_MAP_LOCK = threading.Lock()
_FUND_NAME_MAP_LAST_FAIL: float = 0.0  # 上次索引加载失败时间戳（用于定时重试）
_FUND_NAME_INDEX_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "data", "fund_name_index.json")
_FUND_NAME_INDEX_TTL = 7 * 24 * 3600  # 索引7天刷新一次（新基金偶尔加入，可接受滞后）


def _load_fund_name_index() -> dict:
    """读取全市场基金名称索引磁盘缓存（跨进程复用，避免每进程重拉几MB索引）"""
    try:
        if os.path.exists(_FUND_NAME_INDEX_PATH):
            with open(_FUND_NAME_INDEX_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if time.time() - data.get("ts", 0) < _FUND_NAME_INDEX_TTL:
                return data.get("index", {})
    except Exception:
        pass
    return {}


def _save_fund_name_index(index: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_FUND_NAME_INDEX_PATH), exist_ok=True)
        _tmp = _FUND_NAME_INDEX_PATH + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "index": index}, f, ensure_ascii=False)
        os.replace(_tmp, _FUND_NAME_INDEX_PATH)
    except Exception:
        pass


def _get_fund_name(code: str) -> str:
    """获取基金名称（全市场索引缓存，首次加载后 O(1)；加载失败后定时重试）"""
    global _FUND_NAME_MAP, _FUND_NAME_MAP_LAST_FAIL
    now = time.time()
    if _FUND_NAME_MAP is None or (not _FUND_NAME_MAP and now - _FUND_NAME_MAP_LAST_FAIL > 120):
        with _FUND_NAME_MAP_LOCK:
            if _FUND_NAME_MAP is None or (not _FUND_NAME_MAP and now - _FUND_NAME_MAP_LAST_FAIL > 120):
                _FUND_NAME_MAP = {}
                # 优先读磁盘索引缓存（跨进程复用，避免每进程重新拉几MB全市场索引）
                _FUND_NAME_MAP = _load_fund_name_index()
                if not _FUND_NAME_MAP:
                    try:
                        url = api_url("fund_search_index")
                        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=get_timeout("load_fund_index", 15)) as r:
                            data = r.read().decode("utf-8")
                        # 格式: var r = [["000001","HXCZHH","华夏成长混合","混合型-灵活",...], ...]
                        m = re.search(r"var r\s*=\s*(\[.*?\]);", data, re.DOTALL)
                        if m:
                            raw = json.loads(m.group(1))
                            _FUND_NAME_MAP = {item[0]: item[2] for item in raw}
                            _save_fund_name_index(_FUND_NAME_MAP)
                        _FUND_NAME_MAP_LAST_FAIL = 0.0
                    except Exception:
                        _FUND_NAME_MAP_LAST_FAIL = now
    return _FUND_NAME_MAP.get(code, "")

# ── 当日涨跌(td)缓存：收盘后是固定值，缓存当天供同一天跨进程复用（推荐评分每只都调）──
_TD_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "fund_td_cache.json")
_TD_CACHE_LOCK = threading.Lock()
_TD_MEM: dict | None = None
_TD_MTIME: float = -1.0
_TD_PROC: dict[str, dict] = {}  # code -> {date, td, src}


def _load_td_cache() -> dict:
    """读取当日涨跌磁盘缓存 {code: {date, td, src}}（mtime 缓存）"""
    global _TD_MEM, _TD_MTIME
    try:
        if os.path.exists(_TD_CACHE_PATH):
            _m = os.path.getmtime(_TD_CACHE_PATH)
            if _TD_MEM is not None and _m == _TD_MTIME:
                return _TD_MEM
            with open(_TD_CACHE_PATH, encoding="utf-8") as f:
                _TD_MEM = json.load(f)
            _TD_MTIME = _m
            return _TD_MEM
    except Exception:
        pass
    return {}


def _save_td_cache(cache: dict) -> None:
    global _TD_MEM, _TD_MTIME
    try:
        os.makedirs(os.path.dirname(_TD_CACHE_PATH), exist_ok=True)
        # 跨进程锁：server 与 recommend 都可能 flush td 缓存
        with inter_process_lock(_TD_CACHE_PATH):
            _tmp = _TD_CACHE_PATH + ".tmp"
            with open(_tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
            os.replace(_tmp, _TD_CACHE_PATH)
            _TD_MEM = cache
            _TD_MTIME = os.path.getmtime(_TD_CACHE_PATH)
    except Exception:
        pass


def flush_td_cache() -> None:
    """把进程内当日涨跌缓存落盘（收盘后固定值，供同一天跨进程复用）"""
    try:
        with _TD_CACHE_LOCK:
            if not _TD_PROC:
                return
            _today = datetime.date.today().isoformat()
            _disk = _load_td_cache()
            _changed = False
            for _c, _e in _TD_PROC.items():
                if _e.get("date") == _today and _disk.get(_c) != _e:
                    _disk[_c] = _e
                    _changed = True
            if _changed:
                _save_td_cache(_disk)
    except Exception:
        pass


def _get_td_lsjz_cache(code: str) -> float | None:
    """读取当日实际净值(lsjz)缓存：进程内 → 磁盘。仅收盘后固定值，返回 None 未缓存。"""
    _today = datetime.date.today().isoformat()
    _c = _TD_PROC.get(code)
    if _c and _c.get("date") == _today and _c.get("src") == "lsjz":
        return _c["td"]
    try:
        _d = _load_td_cache().get(code)
        if _d and _d.get("date") == _today and _d.get("src") == "lsjz":
            _TD_PROC[code] = _d
            return _d["td"]
    except Exception:
        pass
    return None


def _set_td_lsjz_cache(code: str, td: float) -> None:
    """写入当日实际净值(lsjz)缓存到进程内（跨进程落盘由 flush_td_cache 统一处理）"""
    _today = datetime.date.today().isoformat()
    _TD_PROC[code] = {"date": _today, "td": round(float(td), 2), "src": "lsjz"}


def _fetch_fund_estimate(code: str) -> tuple[str, float, str] | None:
    """获取基金当日涨跌幅（带当天缓存）。

    注意：盘中无真实当日涨跌（只有估算），收盘后每只基金不定时公布实际净值。
    因此**只缓存实际净值(lsjz)** 作为当天固定值；估算(holdings)/昨日(fallback)不缓存，
    未公布净值的基金每次重试，直到公布。"""
    import datetime as _dt
    _now = _dt.datetime.now()
    _today_str = _now.strftime("%Y-%m-%d")
    # 进程内缓存（仅 lsjz 实际净值，当天固定）
    _cached = _TD_PROC.get(code)
    if _cached and _cached.get("date") == _today_str and _cached.get("src") == "lsjz":
        return (_get_fund_name(code) or code, _cached["td"], _cached["src"])
    # 磁盘缓存（跨进程，仅 lsjz）
    try:
        _disk = _load_td_cache().get(code)
        if _disk and _disk.get("date") == _today_str and _disk.get("src") == "lsjz":
            _TD_PROC[code] = _disk
            return (_get_fund_name(code) or code, _disk["td"], _disk["src"])
    except Exception:
        pass
    _result = _fetch_fund_estimate_uncached(code)
    # 只缓存实际净值(lsjz)为当天固定值；holdings估算/fallback昨日不缓存
    if _result is not None and _result[1] is not None and _result[2] == "lsjz":
        _TD_PROC[code] = {"date": _today_str, "td": _result[1], "src": _result[2]}
    return _result


def _fetch_fund_estimate_uncached(code: str) -> tuple[str, float, str] | None:
    """获取基金当日涨跌幅（无缓存原逻辑），优先返回实际净值，降级到实时估算。

    优先级：
      1. 天天基金历史净值 API（实际净值，收盘后可用）
      2. 天天基金实时估值 API（盘中估算）
      3. 持仓估算（盘中实时）
      4. 新浪财经基金行情（最终降级，昨日数据）
    返回 (基金名, 涨跌幅%, 来源)
      来源: lsjz=今日实际净值, holdings=实时估算, fallback=昨日数据
    """
    import urllib.request
    import datetime

    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%d")

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

    # 2. 盘中或实际净值不可用 → 尝试实时估算
    for url in [api_url("fund_estimate", code=code), api_url("fund_estimate_fallback", code=code)]:
        try:
            gz = fetch(url)
            json_str = re.sub(r"^\w+\(", "", gz).rstrip(");")
            data = json.loads(json_str)
            return (data.get("name", code), float(data["gszzl"]), "holdings")
        except Exception:
            continue

    # 如果估算失败但实际净值可用，返回实际净值
    if actual is not None:
        return (_get_fund_name(code) or actual[0], actual[1], "lsjz")

    # 3. 持仓估算（仅盘中有效；盘前股票行情为昨日数据，估算无意义）
    _h, _m = now.hour, now.minute
    _in_trading = (_h > 9 or (_h == 9 and _m >= 30)) and _h < 15
    if _in_trading:
        try:
            from fund_watch import _estimate_from_holdings
            est = _estimate_from_holdings(code)
            if est is not None:
                record_estimate(code, est)  # 记录盘中估算，供收盘后对比实际净值
                return (_get_fund_name(code) or code, est, "holdings")
        except Exception:
            pass
    # 4. 新浪昨日数据（盘前回退到此）
    try:
        url = f"http://hq.sinajs.cn/list=of{code}"
        req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=get_timeout("default", 10)) as r:
            raw = r.read()
        gz = raw.decode("gbk")
        m = re.search(r'"([^,]*),([-\d.]+),[-\d.]+,([-\d.]+),([-\d.]+),(\d{4}-\d{2}-\d{2})"', gz)
        if m:
            return (m.group(1), float(m.group(4)), "fallback")
    except Exception:
        pass

    return None


# ── 持仓估算误差（盘中估算 vs 收盘实际净值，供界面分辨估算不准的基金）──
_EST_ERROR_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "data", "fund_est_error.json")
_EST_ERROR_LOCK = threading.Lock()
_EST_ERROR_MEM: dict | None = None
_EST_ERROR_MTIME: float = -1.0


def _load_est_error() -> dict:
    """读取估算误差文件 {estimates:{date:{code:est}}, errors:{code:{date:{est,actual,err}}}}（mtime 缓存）"""
    global _EST_ERROR_MEM, _EST_ERROR_MTIME
    try:
        if os.path.exists(_EST_ERROR_PATH):
            _mtime = os.path.getmtime(_EST_ERROR_PATH)
            if _EST_ERROR_MEM is not None and _mtime == _EST_ERROR_MTIME:
                return _EST_ERROR_MEM
            with open(_EST_ERROR_PATH, encoding="utf-8") as f:
                _EST_ERROR_MEM = json.load(f)
            _EST_ERROR_MTIME = _mtime
            return _EST_ERROR_MEM
    except Exception:
        pass
    return {}


def _save_est_error(cache: dict) -> None:
    """合并式原子写入估算误差文件。
    - estimates（临时待结算区）与 errors（历史累积）均**合并**进磁盘最新快照：
      长任务（settle/backfill）的旧快照不会覆盖他进程（如 record_estimate）新增的条目。
    - settle 对已结算条目的删除退化为幂等（errors 已有记录则不再结算，仅多查一次净值）。
    跨进程锁保证并发写不互相覆盖。同步进程内缓存。"""
    global _EST_ERROR_MEM, _EST_ERROR_MTIME
    try:
        os.makedirs(os.path.dirname(_EST_ERROR_PATH), exist_ok=True)
        with inter_process_lock(_EST_ERROR_PATH):
            # 重新读磁盘最新内容
            try:
                if os.path.exists(_EST_ERROR_PATH):
                    with open(_EST_ERROR_PATH, encoding="utf-8") as _f:
                        _disk = json.load(_f)
                else:
                    _disk = {}
            except Exception:
                _disk = {}
            # estimates 与 errors 都合并
            for _k in ("estimates", "errors"):
                _incoming = cache.get(_k)
                if isinstance(_incoming, dict) and _incoming:
                    _dst = _disk.setdefault(_k, {})
                    for _ck, _cv in _incoming.items():
                        if isinstance(_cv, dict):
                            _d2 = _dst.setdefault(_ck, {})
                            _d2.update(_cv)
                        else:
                            _dst[_ck] = _cv
            _tmp = _EST_ERROR_PATH + ".tmp"
            with open(_tmp, "w", encoding="utf-8") as f:
                json.dump(_disk, f, ensure_ascii=False)
            os.replace(_tmp, _EST_ERROR_PATH)
            _EST_ERROR_MEM = _disk
            _EST_ERROR_MTIME = os.path.getmtime(_EST_ERROR_PATH)
    except Exception:
        pass


def record_estimate(code: str, est_pct: float) -> None:
    """盘中记录持仓估算涨跌(%)，供收盘后与实际净值对比。同一天值未变化时不重复写盘。"""
    if est_pct is None:
        return
    try:
        _today = datetime.datetime.now().strftime("%Y-%m-%d")
        # 跨进程锁：server 与 recommend 可能同时写估算误差文件，
        # 必须保证"读-改-写"在进程间互斥，避免互相覆盖丢数据。
        with inter_process_lock(_EST_ERROR_PATH):
            with _EST_ERROR_LOCK:
                _cache = _load_est_error()
                _est_map = _cache.setdefault("estimates", {})
                _day = _est_map.setdefault(_today, {})
                _val = round(float(est_pct), 2)
                if _day.get(code) == _val:
                    return  # 值未变，避免反复写盘（自选表每次刷新都会调用）
                _day[code] = _val
                _save_est_error(_cache)
    except Exception:
        pass


def _fetch_actual_nav_pct(code: str, date: str) -> float | None:
    """拉取基金指定交易日的实际净值涨跌幅(%)，LSJZ 接口（最新 1 页，覆盖最近 20 个交易日）"""
    _url = (f"https://api.fund.eastmoney.com/f10/lsjz"
            f"?callback=j&fundCode={code}&pageIndex=1&pageSize=20")
    try:
        _rate_limit_domain(_url)  # 域名限速，避免并发打爆 LSJZ
        _req = urllib.request.Request(_url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"})
        with urllib.request.urlopen(_req, timeout=get_timeout("default", 10)) as _r:
            _text = _r.read().decode("utf-8")
        _m = re.search(r"j\((.+)\)", _text)
        if not _m:
            return None
        _data = json.loads(_m.group(1))
        for _it in _data.get("Data", {}).get("LSJZList", []):
            if _it.get("FSRQ") == date and _it.get("JZZZL"):
                return float(_it["JZZZL"])
    except Exception:
        pass
    return None


def _probe_latest_nav_date(code: str) -> str | None:
    """轻量探测：拉基金 LSJZ 最新 1 条，返回最新净值日期 YYYY-MM-DD，失败返回 None"""
    _url = (f"https://api.fund.eastmoney.com/f10/lsjz"
            f"?callback=j&fundCode={code}&pageIndex=1&pageSize=1")
    try:
        _req = urllib.request.Request(_url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"})
        with urllib.request.urlopen(_req, timeout=6) as _r:
            _text = _r.read().decode("utf-8")
        _m = re.search(r"j\((.+)\)", _text)
        if _m:
            _data = json.loads(_m.group(1))
            _items = _data.get("Data", {}).get("LSJZList", [])
            if _items:
                return _items[0].get("FSRQ")
    except Exception:
        pass
    return None


def settle_estimate_errors() -> None:
    """结算估算误差：对已有估算记录的日期（含今天）每只基金独立并行拉实际净值算差异。
    净值已出的基金结算，未出的保留待下次——每只独立判断，
    不会因某只基金净值未发布而耽误其它已发布的基金（如不同基金发布时间不同）。幂等。
    优化：结算前先探测今日净值是否发布——收盘后净值通常晚上才出，若未发布则跳过
    今日全部结算（避免对数千只基金无效请求占满网络，拖慢 fund-table 首屏）。
    过滤：只结算"相关基金"（自选基金 ∪ 当前推荐结果）——徽章只在这两处展示，
    estimates 里大量历史候选（多次运行累加、已不再符合当前筛选条件）不再白白请求。"""
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with _EST_ERROR_LOCK:
            _cache = _load_est_error()
            _est_map = _cache.setdefault("estimates", {})
            _errors = _cache.setdefault("errors", {})
            # 待结算任务 (date, code) -> est
            _tasks: dict[tuple[str, str], float] = {}
            for _d, _day_map in list(_est_map.items()):
                for _code, _est in list(_day_map.items()):
                    _tasks[(_d, _code)] = _est
            if not _tasks:
                return
            # 相关基金集合：自选基金 ∪ 当前推荐结果（估算误差徽章只在这两处展示）
            _relevant: set[str] = set()
            try:
                _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                _fl_path = os.path.join(_root, "data", "fund_list.json")
                if os.path.exists(_fl_path):
                    with open(_fl_path, encoding="utf-8") as _f:
                        for _item in json.load(_f):
                            if _item.get("code"):
                                _relevant.add(_item["code"])
                _res_path = os.path.join(_root, ".fund_recommend_result.json")
                if os.path.exists(_res_path):
                    with open(_res_path, encoding="utf-8") as _f:
                        _res = json.load(_f)
                    for _r in _res.get("results", []):
                        if _r.get("code"):
                            _relevant.add(_r["code"])
            except Exception:
                pass
            # 过滤：只结算相关基金（文件读取失败则保留全量，避免过度限制）
            if _relevant:
                _tasks = {(_d, _c): _e for (_d, _c), _e in _tasks.items() if _c in _relevant}
                if not _tasks:
                    return
            # 净值日期探测：若今日净值普遍未发布（收盘后，最新净值日期 < 今天），
            # 跳过今日任务的结算，仅结算历史日期——避免数千无效请求占满网络。
            _today = datetime.date.today().isoformat()
            _hist = {(_d, _c): _e for (_d, _c), _e in _tasks.items() if _d < _today}
            _today_tasks = {(_d, _c): _e for (_d, _c), _e in _tasks.items() if _d >= _today}
            if _today_tasks:
                _probe_codes = list(dict.fromkeys(_c for (_d, _c) in _today_tasks))[:3]
                _today_ready = False
                for _pc in _probe_codes:
                    try:
                        _ld = _probe_latest_nav_date(_pc)
                        if _ld and _ld >= _today:
                            _today_ready = True
                            break
                    except Exception:
                        pass
                # 今日未发布 → 仅结算历史；已发布 → 连同今日一起结算
                if not _today_ready:
                    _tasks = _hist
            if not _tasks:
                return
            _settled: dict[tuple[str, str], float] = {}
            with ThreadPoolExecutor(max_workers=20) as _ex:
                _futs = {_ex.submit(_fetch_actual_nav_pct, _c, _d): (_d, _c) for (_d, _c) in _tasks}
                for _f in as_completed(_futs):
                    _d, _c = _futs[_f]
                    try:
                        _actual = _f.result()
                    except Exception:
                        _actual = None
                    if _actual is not None:
                        _settled[(_d, _c)] = _actual
            # 写入 errors + 从 estimates 移除已结算条目
            for (_d, _c), _actual in _settled.items():
                _est = _tasks[(_d, _c)]
                _errors.setdefault(_c, {})[_d] = {
                    "est": _est, "actual": round(_actual, 2),
                    "err": round(_actual - _est, 2)}  # 实际-估算：正=实际好于估算
                _day_map = _est_map.get(_d)
                if _day_map is not None and _c in _day_map:
                    _day_map.pop(_c, None)
                if _day_map is not None and not _day_map:
                    _est_map.pop(_d, None)
            _save_est_error(_cache)
    except Exception:
        pass


def get_est_error_summary(code: str, history_days: int = 10) -> dict | None:
    """返回基金最近 history_days 天估算误差汇总 {mae, count, detail:[{date,est,actual,err},...]}
    detail 按日期倒序（最新在前）。无数据返回 None。"""
    try:
        _errors = _load_est_error().get("errors", {}).get(code, {})
        if not _errors:
            return None
        _dates = sorted(_errors.keys())[-history_days:]
        _detail = []
        _abs_errs = []
        for _d in _dates:
            _e = _errors[_d]
            _abs_errs.append(abs(_e.get("err", 0)))
            _detail.append({"date": _d, "est": _e.get("est"), "actual": _e.get("actual"), "err": _e.get("err")})
        if not _abs_errs:
            return None
        _detail.sort(key=lambda x: x["date"], reverse=True)
        return {"mae": round(sum(_abs_errs) / len(_abs_errs), 2), "count": len(_detail), "detail": _detail}
    except Exception:
        return None


# ── 历史股票日K线缓存（回填估算差异用，1小时TTL）──
_KLINE_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_KLINE_TTL = 3600


def _fetch_stock_kline_chg(secid: str, days: int = 12) -> dict[str, float]:
    """拉股票最近 days 个交易日涨跌幅 {date: chg%}（新浪日K线，1小时缓存）。
    secid 格式: sh600000 / sz000001（东财历史K线接口限频，改用新浪）"""
    import urllib.request
    _now = time.time()
    _c = _KLINE_CACHE.get(secid)
    if _c and _now - _c[0] < _KLINE_TTL:
        return _c[1]
    result: dict[str, float] = {}
    try:
        _url = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_k=/CN_MarketDataService.getKLineData"
                f"?symbol={secid}&scale=240&ma=no&datalen={days + 2}")
        _req = urllib.request.Request(_url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
        with urllib.request.urlopen(_req, timeout=10) as _r:
            _raw = _r.read().decode("utf-8", errors="ignore")
        _m = re.search(r"\((\[.*\])\)", _raw, re.DOTALL)
        if _m:
            _arr = json.loads(_m.group(1))
            _prev = None
            for _it in _arr:
                try:
                    _close = float(_it.get("close"))
                except (TypeError, ValueError):
                    _prev = None
                    continue
                if _prev is not None and _prev:
                    result[_it.get("day", "")] = round((_close - _prev) / _prev * 100, 2)
                _prev = _close
    except Exception:
        pass
    _KLINE_CACHE[secid] = (_now, result)
    return result


def backfill_estimate_errors(days: int = 10) -> int:
    """用当前持仓 + 历史股票行情回填最近 days 天估算差异（只补没有差异记录的**历史**日期）。
    基于当前季报持仓近似回算历史每日估算，对比实际净值。今天交给 settle 真实采集结算。
    返回本次回填条数。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    _filled = 0
    try:
        from fund_watch import _parse_holdings, _fetch_nav_from_lsjz
    except Exception:
        return 0
    try:
        with _EST_ERROR_LOCK:
            _cache = _load_est_error()
            _errors = _cache.setdefault("errors", {})
        # 待回填基金：所有自选基金 + 市场优选(推荐结果)按评分前 N 只（不限当天估算记录）
        _codes: list[str] = []
        try:
            _fl_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "data", "fund_list.json")
            if os.path.exists(_fl_path):
                with open(_fl_path, encoding="utf-8") as _f:
                    for _item in json.load(_f):
                        _c = _item.get("code")
                        if _c:
                            _codes.append(_c)
        except Exception:
            pass
        # 市场优选：推荐结果按评分降序取前 N（覆盖市场优选表显示范围）
        try:
            _res_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     ".fund_recommend_result.json")
            if os.path.exists(_res_path):
                with open(_res_path, encoding="utf-8") as _f:
                    _res = json.load(_f)
                _top_n = 100
                _res_sorted = sorted(_res.get("results", []),
                                     key=lambda x: x.get("score", 0), reverse=True)[:_top_n]
                for _r in _res_sorted:
                    _c = _r.get("code")
                    if _c and _c not in _codes:
                        _codes.append(_c)
        except Exception:
            pass
        if not _codes:
            return 0
        # 预过滤：只处理完全无误差记录的基金（增量回填）。
        # 推荐结果更新后新进榜基金无历史记录，立即补齐；已有记录的不重复解析持仓/拉行情，秒级完成。
        _need = []
        for _c in _codes:
            if not _errors.get(_c):
                _need.append(_c)
        _codes = _need
        if not _codes:
            return 0
        _today = datetime.date.today().isoformat()
        # 1. 收集每只基金持仓股票 secid
        _holdings: dict[str, list] = {}
        _stock_map: dict[str, set[str]] = {}
        for _c in _codes:
            try:
                _h = _parse_holdings(_c)
                if _h:
                    _holdings[_c] = _h
                    _stock_map[_c] = {("sh" if _hh.get("m") == "sh" else "sz") + _hh["c"]
                                      for _hh in _h if _hh.get("c") and _hh.get("p")}
            except Exception:
                pass
        # 2. 并行拉所有股票历史日K线（含涨跌幅）
        _all_secids = set()
        for _s in _stock_map.values():
            _all_secids |= _s
        _kline: dict[str, dict[str, float]] = {}
        with ThreadPoolExecutor(max_workers=20) as _ex:
            _futs = {_ex.submit(_fetch_stock_kline_chg, _sid, days): _sid for _sid in _all_secids}
            for _f in as_completed(_futs):
                try:
                    _kline[_futs[_f]] = _f.result()
                except Exception:
                    pass
        # 3. 每只基金：回算历史每日估算，对比实际净值写入
        for _c, _h in _holdings.items():
            _est_days: dict[str, float] = {}
            _date_set: set[str] = set()
            for _secid in _stock_map.get(_c, ()):
                _date_set |= set(_kline.get(_secid, {}).keys())
            for _dt_str in _date_set:
                _tw = 0.0
                _ws = 0.0
                for _hh in _h:
                    if not _hh.get("c") or not _hh.get("p"):
                        continue
                    _secid = ("sh" if _hh.get("m") == "sh" else "sz") + _hh["c"]
                    _chg = _kline.get(_secid, {}).get(_dt_str)
                    if _chg is None:
                        continue
                    _tw += _hh["p"]
                    _ws += _chg * _hh["p"]
                if _tw >= 5:
                    _est_days[_dt_str] = _ws / _tw
            # 实际净值涨跌（最近1页20条，升序）
            try:
                _navs = _fetch_nav_from_lsjz(_c, max_pages=1)
            except Exception:
                _navs = None
            _actual_days: dict[str, float] = {}
            if _navs and len(_navs) >= 2:
                for _i in range(1, len(_navs)):
                    _pv = _navs[_i - 1]["v"]
                    if _pv:
                        _actual_days[_navs[_i]["d"]] = (_navs[_i]["v"] - _pv) / _pv * 100
            _err_map = _errors.setdefault(_c, {})
            for _dt_str, _est in _est_days.items():
                if _dt_str >= _today or _dt_str in _err_map or _dt_str not in _actual_days:
                    continue
                _actual = _actual_days[_dt_str]
                _err_map[_dt_str] = {"est": round(_est, 2), "actual": round(_actual, 2),
                                     "err": round(_actual - _est, 2)}  # 实际-估算：正=实际好于估算
                _filled += 1
        if _filled:
            with _EST_ERROR_LOCK:
                _save_est_error(_cache)
    except Exception:
        pass
    return _filled


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
        with urllib.request.urlopen(req, timeout=get_timeout("wechat_push", 10)) as _resp:
            _resp.read()
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


def heartbeat_age(name: str) -> float:
    """返回心跳文件最后写入时间距今的秒数；无心跳文件返回 -1。
    用于判断进程是否真的挂死（心跳长时间未更新），而不是凭 progress==total 误判。"""
    try:
        path = os.path.join(_HEARTBEAT_DIR, f"{name}.json")
        if os.path.exists(path):
            return time.time() - os.path.getmtime(path)
    except Exception:
        pass
    return -1.0


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
        # 非完成/失败阶段时，overall_pct 不超过 99，防止前端误判完成
        if hb.get("phase") not in ("完成", "失败", "保存") and hb.get("overall_pct", 0) >= 100:
            hb["overall_pct"] = 99
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
    """判断心跳是否存活：先检查进程是否还在运行，再检查超时"""
    hb = read_heartbeat(name)
    if hb is None:
        return False
    # 检查进程是否还活着（比纯超时检查更可靠）
    pid = hb.get("pid")
    if pid:
        try:
            import os as _os
            if hasattr(_os, "kill"):
                _os.kill(pid, 0)
            else:
                import subprocess as _sp
                _r = _sp.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=5)
                if str(pid) not in _r.stdout:
                    _clear_hb = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".heartbeats", f"{name}.json")
                    try:
                        os.remove(_clear_hb)
                    except OSError:
                        pass
                    return False
        except Exception:
            pass  # 无法检查时回退到超时判断
    start = hb.get("start")
    if not start:
        return False
    return time.time() - start < timeout

