"""
基金优选 — 本地 HTTP 服务器
提供交互式网页 + API，用于增删监控基金。
"""
import json
import os
import re
import sys
import subprocess
import http.server
import threading
import time
import datetime
import urllib.parse
import concurrent.futures
import urllib.request

# 同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fund_utils import read_all_heartbeats, is_heartbeat_alive, write_heartbeat, update_heartbeat, clear_heartbeat, HISTORY_DIR, setup_log
from config import CFG, api_url, get_timeout, get_config

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 后台任务管理 ──
_recommend_state = {"proc": None}
_proc_lock = threading.Lock()

# 通用任务进程跟踪（供启停控制使用）
_task_procs: dict[str, subprocess.Popen] = {}
_task_scripts = {
    "fund_monitor": "fund_monitor.py",
}
_task_heartbeats = {
    "fund_monitor": "fund_monitor",
}


def _spawn_task(task_id: str) -> bool:
    """启动一个定时任务，返回是否成功"""
    script_name = _task_scripts.get(task_id)
    if not script_name:
        return False
    script = os.path.join(_SCRIPT_DIR, script_name)
    hb_name = _task_heartbeats.get(task_id, task_id)
    try:
        proc = subprocess.Popen(
            [sys.executable, script],
            cwd=_SCRIPT_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # 立即写心跳，不等待
        write_heartbeat(hb_name)
        with _proc_lock:
            _task_procs[task_id] = proc
        def _wait_and_cleanup(p=proc, hb=hb_name, tid=task_id) -> None:
            p.wait()
            clear_heartbeat(hb)
            with _proc_lock:
                if _task_procs.get(tid) is p:
                    del _task_procs[tid]
        threading.Thread(target=_wait_and_cleanup, daemon=True).start()
        return True
    except Exception:
        clear_heartbeat(hb_name)
        return False


def _stop_task(task_id: str) -> bool:
    """停止一个正在运行的任务"""
    with _proc_lock:
        proc = _task_procs.get(task_id)
        if proc and proc.poll() is None:
            proc.terminate()
            return True
        return False


def _spawn_recommend() -> bool:
    """启动推荐任务，返回是否成功"""
    script = os.path.join(_SCRIPT_DIR, "fund_recommend.py")
    try:
        proc = subprocess.Popen(
            [sys.executable, script],
            cwd=_SCRIPT_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # 立即写心跳，前端立刻就能看到进度
        write_heartbeat("fund_recommend", progress=0, total=0, status="启动中")
        with _proc_lock:
            _recommend_state["proc"] = proc

        def _wait_and_cleanup(p=proc) -> None:
            p.wait()
            if p.returncode != 0:
                _err_msg = f"推荐进程异常退出(code={p.returncode})，请查看 recommend.log"
                write_heartbeat("fund_recommend", progress=0, total=0, overall_pct=100,
                                phase="失败", detail=_err_msg, error=_err_msg)
                print(f"[recommend] {_err_msg}", flush=True)
                # 保留 30 秒再清除，给前端时间读取
                threading.Timer(30, clear_heartbeat, ["fund_recommend"]).start()
            else:
                clear_heartbeat("fund_recommend")
            with _proc_lock:
                if _recommend_state["proc"] is p:
                    _recommend_state["proc"] = None

        threading.Thread(target=_wait_and_cleanup, daemon=True).start()
        return True
    except Exception as e:
        print(f"[ERROR] 推荐启动异常: {e}", flush=True)
        clear_heartbeat("fund_recommend")
        return False





_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# ── fund-table 缓存 ──
_fund_table_cache: tuple[float, str] | None = None
_FUND_TABLE_CACHE_TTL = get_config("server", "fund_table_cache_ttl", default=86400)  # 秒（长效缓存，仅数据变化时主动清空）
# 推荐表缓存（用模块级 dict 避免 global 作用域问题）
_recommend_table_cache: dict[str, tuple[float, str]] = {"data": None}
_recommend_cache_ttl = _FUND_TABLE_CACHE_TTL
_FUND_LIST_PATH = os.path.join(_PROJECT_ROOT, "data", "fund_list.json")
_CONFIG_PATH = os.path.join(_PROJECT_ROOT, "data", "config.json")
_PORT = get_config("server", "port", default=8080)


def _fetch_fund_name(code: str) -> str:
    """从 fundgz 实时估值 API 获取基金名称（160B 轻量请求）"""
    import urllib.request, re, json as _json
    try:
        url = f"https://fundgz.1234567.com.cn/js/{code}.js"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=get_timeout("fetch_fund_name", 10)) as r:
            text = r.read().decode("utf-8")
        m = re.search(r"jsonpgz\((.+)\)", text)
        if m:
            data = _json.loads(m.group(1))
            return data.get("name", "")
    except Exception:
        pass
    return ""


# 全市场基金代码/名称索引（按需加载）
_FUND_INDEX: list[dict] | None = None


def _load_fund_index() -> list[dict]:
    """加载天天基金全市场基金索引（代码+名称），按需一次性加载"""
    global _FUND_INDEX
    if _FUND_INDEX is not None:
        return _FUND_INDEX
    try:
        url = api_url("fund_search_index")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=get_timeout("load_fund_index", 15)) as r:
            data = r.read().decode("utf-8")
        # 格式: var r = [["000001","HXCZHH","华夏成长混合","混合型-灵活","HUAXIACHENGZHANGHUNHE"], ...]
        m = re.search(r"var r\s*=\s*(\[.*?\]);", data, re.DOTALL)
        if m:
            raw = json.loads(m.group(1))
            _FUND_INDEX = [{"code": item[0], "name": item[2]} for item in raw]
        else:
            _FUND_INDEX = []
    except Exception:
        _FUND_INDEX = []
    return _FUND_INDEX


def _search_funds(q: str, limit: int = 10) -> list[dict]:
    """按代码或名称模糊搜索基金"""
    q = q.strip().lower()
    if not q:
        return []
    index = _load_fund_index()
    if not index:
        return []
    results: list[dict] = []
    for f in index:
        if q == f["code"] or q == f["code"].lstrip("0"):
            # 精确匹配代码排最前
            results.insert(0, f)
            if len(results) > limit:
                results.pop()
        elif q in f["name"].lower() or q in f["code"]:
            results.append(f)
            if len(results) >= limit:
                break
    return results[:limit]


def _load() -> list[dict]:
    if os.path.exists(_FUND_LIST_PATH):
        try:
            with open(_FUND_LIST_PATH, encoding="utf-8") as f:
                return json.load(f)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save(data: list[dict]) -> None:
    os.makedirs(os.path.dirname(_FUND_LIST_PATH), exist_ok=True)
    with open(_FUND_LIST_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _write_config(cfg: dict) -> None:
    """安全写入 config.json（原子写入：先写临时文件再 rename）"""
    import tempfile
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    _tmp = _CONFIG_PATH + ".tmp"
    try:
        with open(_tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(_tmp, _CONFIG_PATH)
    except Exception:
        # 回退：直接写入
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)


def _json_response(data, status=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return (status, {"Content-Type": "application/json; charset=utf-8"}, body)


def _check_task_status(taskname: str) -> dict:
    """查询计划任务/定时器状态（支持 Windows schtasks 和 Linux systemd）"""
    # 先尝试 Windows schtasks
    try:
        r = subprocess.run(
            ["schtasks", "/query", "/tn", taskname, "/fo", "LIST", "/v"],
            capture_output=True, text=True, timeout=get_timeout("schtasks", 10)
        )
        if r.returncode == 0:
            out = r.stdout
            result = {"status": "未知", "next_run": "", "last_run": "", "last_result": "", "ok": None}
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("Status:"):
                    result["status"] = line.split(":", 1)[1].strip()
                elif line.startswith("Next Run Time:"):
                    val = line.split(":", 1)[1].strip()
                    if val and val != "N/A":
                        result["next_run"] = val
                elif line.startswith("Last Run Time:"):
                    val = line.split(":", 1)[1].strip()
                    if val and val != "N/A":
                        result["last_run"] = val
                elif line.startswith("Last Result:"):
                    val = line.split(":", 1)[1].strip()
                    if val and val != "N/A":
                        result["last_result"] = val
            result["ok"] = (result["last_result"] == "0") if result["last_result"] is not None else None
            result["timer_enabled"] = result["status"] in ("Ready", "Running")
            return result
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Windows schtasks 不可用，降级尝试 Linux systemd
    try:
        tid = taskname.lower().replace(" ", "-")
        enabled = subprocess.run(
            ["systemctl", "is-enabled", f"{tid}.timer"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        active = subprocess.run(
            ["systemctl", "is-active", f"{tid}.timer"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        result = {
            "status": active,
            "next_run": "",
            "last_run": "",
            "last_result": "",
            "ok": None,
            "timer_enabled": enabled == "enabled",
        }
        # 获取下次触发时间
        r = subprocess.run(
            ["systemctl", "show", f"{tid}.timer", "--property=NextElapseUSecRealtime"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            val = r.stdout.strip().split("=", 1)[-1]
            if val and val != "(null)":
                result["next_run"] = val
        return result
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return {"status": "未找到", "timer_enabled": False}


TASK_DEFS = [
    {"id": "fund_monitor", "taskname": "基金盘中监控", "icon": "🔔", "label": "盘中监控",
     "desc": "交易日 9:30–15:00 每 10 分钟轮询 | 基金实时估算涨跌幅 · 基金前 5 大重仓个股实时涨跌 | 双重警报：单次急涨急跌 + 当日累计涨跌（红/黄双阈值）| 节假日自动检测 · 进程崩溃恢复",
     "time": "交易日 9:25 启动"},
]


def _recalc_cached_scores() -> None:
    """用当前评分权重重新计算缓存结果中的评分，不重新拉取数据。"""
    rec_path = os.path.join(_PROJECT_ROOT, ".fund_recommend_result.json")
    if not os.path.exists(rec_path):
        return
    try:
        with open(rec_path, encoding="utf-8") as _f:
            data = json.load(_f)
        results = data.get("results", [])
        if not results:
            return
        from fund_scoring import calc_score_detail
        score_keys = [
            "y1", "m3", "m1", "f5", "sy6", "sy2", "sy3",
            "annual_return", "sharpe", "sortino",
            "profit_ratio", "win_rate", "recovery", "calmar",
            "max_dd", "volatility", "max_loss_days",
            "sc", "rate", "inst", "td",
        ]
        for r in results:
            score_d = {k: r.get(k) for k in score_keys}
            score, details, skipped = calc_score_detail(score_d)
            r["score"] = score
            r["_score_detail"] = details
            r["_skipped_weight"] = skipped
        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        data["results"] = results
        tmp = rec_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as _f:
            json.dump(data, _f, indent=2, ensure_ascii=False)
        os.replace(tmp, rec_path)
        print(f"[recalc] 已用新权重重新评分 {len(results)} 只基金", flush=True)
    except Exception as e:
        print(f"[recalc] 重新评分失败: {e}", flush=True)


class Handler(http.server.BaseHTTPRequestHandler):

    # 静默常规轮询请求和耗时批量API，减少终端刷屏
    _quiet_paths = {"/api/heartbeat", "/api/tasks", "/api/fund-table", "/api/recommend-table"}

    def log_request(self, code: int | str = ..., size: int | str = ...) -> None:
        if hasattr(self, "path") and self.path in self._quiet_paths:
            return
        super().log_request(code, size)

    def _send(self, status: int, headers: dict, body: bytes):
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/api/list":
            try:
                funds = _load()
                self._send(*_json_response({"ok": True, "funds": funds}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/check-trade-time":
            """轻量接口：判断当前是否为交易时间（无外部请求，不耗流量）"""
            try:
                from fund_utils import is_trading_day
                import datetime
                now = datetime.datetime.now()
                today = now.date()
                is_trading_day_bool = is_trading_day(today)
                h, m = now.hour, now.minute
                in_time = (h > 9 or (h == 9 and m >= 30)) and h < 15
                is_trading = is_trading_day_bool and in_time
                # 下次刷新参考时间（秒）
                next_in = 0
                if not is_trading_day_bool:
                    next_in = 86400  # 明天再说
                elif not in_time:
                    if h < 9 or (h == 9 and m < 30):
                        # 盘前，距离开盘
                        next_in = (9 * 3600 + 30 * 60) - (h * 3600 + m * 60)
                    else:
                        # 盘后，距离明天开盘 15小时+（简化）
                        next_in = (24 * 3600 - h * 3600 - m * 60) + 9 * 3600 + 30 * 60
                self._send(*_json_response({"ok": True, "is_trading": is_trading, "next_check_seconds": next_in}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/market-indices":
            try:
                from fund_utils import fetch_bytes, is_trading_day
                import datetime
                today = datetime.date.today()
                is_trading = is_trading_day(today) and (datetime.datetime.now().hour >= 9 and datetime.datetime.now().hour < 15)
                url = "http://hq.sinajs.cn/list=sh000001,sz399001,sz399006"
                raw = fetch_bytes(url, {"Referer": "https://finance.sina.com.cn/"})
                indices = []
                if raw:
                    text = raw.decode("gbk", errors="ignore")
                    for line in text.strip().split("\n"):
                        if "hq_str_" not in line:
                            continue
                        parts = line.split('"')
                        if len(parts) < 2:
                            continue
                        fields = parts[1].split(",")
                        if len(fields) < 30:
                            continue
                        name = fields[0]
                        # 字段格式: name,今开,昨收,现价,最高,最低,...
                        prev_close = float(fields[2]) if fields[2] else 0
                        price = float(fields[3]) if fields[3] else 0
                        chg_pts = price - prev_close
                        chg_pct = (chg_pts / prev_close * 100) if prev_close else 0
                        indices.append({
                            "name": name, "price": price,
                            "change_points": round(chg_pts, 2), "change_pct": round(chg_pct, 2),
                        })
                self._send(*_json_response({"ok": True, "indices": indices, "is_trading": is_trading}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/market-trends":
            """大盘指数当日5分钟K线数据（用于画分时折线图）"""
            try:
                from fund_utils import fetch
                import datetime, json as _json
                now = datetime.datetime.now()
                today_str = now.strftime("%Y-%m-%d")
                symbols = [
                    ("sh000001", "上证指数"),
                    ("sz399001", "深证成指"),
                    ("sz399006", "创业板指"),
                ]
                def _trading_offset(day_str: str) -> int:
                    """计算交易偏移量（分钟），09:30→0, 11:30→120, 13:00→120, 15:00→240"""
                    import datetime as _dt
                    try:
                        dt = _dt.datetime.strptime(day_str, "%Y-%m-%d %H:%M:%S")
                        mins = dt.hour * 60 + dt.minute
                        if mins < 570:  # 09:30 之前
                            return 0
                        if mins <= 690:  # 09:30-11:30
                            return mins - 570
                        if mins < 780:  # 11:30-13:00 午休
                            return 120
                        # 13:00-15:00
                        return 120 + (mins - 780)
                    except Exception:
                        return 0

                def _fetch_pre_close(sym: str) -> float | None:
                    """从日K线获取昨日收盘价"""
                    try:
                        daily_url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen=2"
                        raw_daily = fetch(daily_url)
                        daily_data = _json.loads(raw_daily)
                        if daily_data and len(daily_data) >= 2:
                            return float(daily_data[-2]["close"])
                    except Exception:
                        pass
                    return None

                result = []
                for sym, name in symbols:
                    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sym}&scale=5&ma=no&datalen=48"
                    raw = fetch(url)
                    points = _json.loads(raw)
                    # 从原始数据中提取昨日收盘价（最后一个非今日的close）
                    pre_close = None
                    for p in reversed(points):
                        if not p.get("day", "").startswith(today_str) and p.get("close"):
                            pre_close = float(p["close"])
                            break
                    # 如果5分钟K线中没有非今日数据（如全天的数据全是今天），从日K线获取
                    if pre_close is None:
                        pre_close = _fetch_pre_close(sym)
                    # 只取今日数据；若今日无数据（如周末），取最近一天
                    today_points = [p for p in points if p.get("day", "").startswith(today_str)]
                    if not today_points and points:
                        last_day = points[-1].get("day", "")[:10]
                        today_points = [p for p in points if p.get("day", "").startswith(last_day)]
                        # 重新获取昨日收盘价（用日K线API）
                        pre_close = _fetch_pre_close(sym)
                    pt_list = []
                    for p in today_points:
                        day_str = p.get("day", "")
                        close = float(p.get("close", 0))
                        off = _trading_offset(day_str)
                        pt_list.append({"t": day_str, "close": close, "offset": off})
                    closes = [pt["close"] for pt in pt_list]
                    result.append({
                        "name": name,
                        "symbol": sym,
                        "closes": closes,
                        "points": pt_list,
                        "pre_close": pre_close,
                    })
                self._send(*_json_response({"ok": True, "trends": result}))
            except Exception as e:
                print(f"[ERROR] /api/market-trends: {e}", flush=True)
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/search":
            try:
                q = params.get("q", [""])[0]
                results = _search_funds(q)
                self._send(*_json_response({"ok": True, "results": results}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/tasks":
            try:
                tasks = []
                for t in TASK_DEFS:
                    info = _check_task_status(t["taskname"])
                    running = is_heartbeat_alive(t["id"], 1800)
                    tasks.append({**t, **info, "running": running})
                self._send(*_json_response({"ok": True, "tasks": tasks}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/heartbeat":
            try:
                hb = read_all_heartbeats()
                alive = {k: is_heartbeat_alive(k, 1800) for k in hb}
                brief_path = os.path.join(_PROJECT_ROOT, ".briefing_fund.html")
                brief_mtime = os.path.getmtime(brief_path) if os.path.exists(brief_path) else 0
                self._send(*_json_response({"ok": True, "heartbeats": hb, "alive": alive, "briefing_mtime": brief_mtime}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/dims":
            try:
                with open(_CONFIG_PATH, encoding="utf-8") as _fdims:
                    dims = json.load(_fdims).get("scoring", {}).get("dims", [])
                # 归一化权重，让页面展示实际生效的值
                total = sum(d.get("weight", 0) for d in dims)
                if total > 0 and abs(total - 1.0) > 0.001:
                    for d in dims:
                        d["weight"] = round(d["weight"] / total, 4)
                self._send(*_json_response({"ok": True, "dims": dims}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/dims-presets":
            try:
                with open(_CONFIG_PATH, encoding="utf-8") as f:
                    cfg = json.load(f)
                presets = cfg.get("scoring_presets", {})
                if not presets:
                    _init_builtin_presets(cfg)
                    presets = cfg["scoring_presets"]
                current = cfg.get("scoring", {}).get("current_preset", "系统默认")
                self._send(*_json_response({"ok": True, "presets": presets, "current": current}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/holdings-dims":
            """获取持仓股票评分维度配置"""
            try:
                dims = _load_hld_dims()
                self._send(*_json_response({"ok": True, "dims": dims}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/holdings-col-order":
            """获取持仓表格列顺序"""
            try:
                with open(_CONFIG_PATH, encoding="utf-8") as _f:
                    _cfg = json.load(_f)
                order = _cfg.get("holdings_col_order", [])
                self._send(*_json_response({"ok": True, "order": order}))
            except Exception:
                self._send(*_json_response({"ok": True, "order": []}))
            return

        if parsed.path == "/api/prefs":
            """获取用户偏好设置"""
            try:
                with open(_CONFIG_PATH, encoding="utf-8") as _f:
                    _cfg = json.load(_f)
                prefs = _cfg.get("user_prefs", {})
                self._send(*_json_response({"ok": True, "prefs": prefs}))
            except Exception:
                self._send(*_json_response({"ok": True, "prefs": {}}))
            return

        if parsed.path == "/api/recommend-config":
            try:
                with open(_CONFIG_PATH, encoding="utf-8") as _frc:
                    cfg = json.load(_frc)
                rc = cfg.get("recommend", {})
                self._send(*_json_response({
                    "ok": True,
                    "config": {
                        "top_n": rc.get("top_n", 200),
                        "filter_conditions": rc.get("filter_conditions", []),
                        "show_top": rc.get("show_top", 20),
                        "skip_missing_perf": rc.get("skip_missing_perf", False),
                        "skip_limited": rc.get("skip_limited", False),
                        "rank_sort": rc.get("rank_sort", "1n"),
                    }
                }))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/holdings":
            try:
                q = urllib.parse.parse_qs(parsed.query)
                code = q.get("code", [""])[0]
                if not code:
                    self._send(*_json_response({"ok": False, "error": "缺少code参数"}, 400))
                    return
                from fund_watch import _parse_holdings, _parse_holdings_meta
                from fund_utils import fetch, _retry_fetch
                holds = _parse_holdings(code) or []
                # 去重：同一股票代码只保留第一次出现
                _seen = set()
                _deduped = []
                for h in holds:
                    c = h.get("c", "")
                    if c and c not in _seen:
                        _seen.add(c)
                        _deduped.append(h)
                holds = _deduped
                hld_meta = _parse_holdings_meta(code) if holds else {}
                # 用腾讯财经接口批量获取实时涨跌（速度快，不需要Referer）
                if holds:
                    codes_str = ",".join(
                        (h.get("m", "sz") + h["c"])
                        for h in holds
                    )
                    # Tencent 实时行情：60秒缓存（盘中股价实时，PE/PB/市值变化慢）
                    _tencent_cache = globals().setdefault("_tencent_realtime_cache", {})
                    _tencent_key = f"realtime_{codes_str}"
                    _tencent_now = time.time()
                    _tencent_cached = _tencent_cache.get(_tencent_key)
                    if _tencent_cached and _tencent_now - _tencent_cached[0] < 60:
                        raw = _tencent_cached[1]
                    else:
                        # Tencent 返回 GBK 编码，_retry_fetch 用 UTF-8 会导致中文乱码
                        # 但数字和 ~ 分隔符不受影响。直接使用 urllib 用 GBK 解码
                        import urllib.request as _tencent_ur
                        _tencent_url = api_url("tencent_realtime", code=codes_str)
                        _tencent_req = _tencent_ur.Request(_tencent_url, headers={"User-Agent": "Mozilla/5.0"})
                        try:
                            with _tencent_ur.urlopen(_tencent_req, timeout=15) as _tencent_r:
                                raw = _tencent_r.read().decode("gbk", errors="ignore")
                        except Exception:
                            raw = ""
                        _tencent_cache[_tencent_key] = (_tencent_now, raw)
                    def _sf(v):
                        """safe float conversion"""
                        try: return float(v) if v else None
                        except: return None
                    try:
                        for line in raw.strip().split(";"):
                            if not line:
                                continue
                            parts = line.split("~")
                            if len(parts) > 32:
                                code_from_resp = parts[2] if len(parts) > 2 else ""
                                price = _sf(parts[3]) or 0
                                prev_close = _sf(parts[4]) or 0
                                chg = round((price - prev_close) / prev_close * 100, 2) if prev_close else None
                                pe = _sf(parts[39]) if len(parts) > 39 else None
                                ret_1w = _sf(parts[63]) if len(parts) > 63 else None
                                mkt_cap = _sf(parts[45]) if len(parts) > 45 else None
                                pb = _sf(parts[46]) if len(parts) > 46 else None
                                turnover = _sf(parts[38]) if len(parts) > 38 else None
                                vol_ratio = _sf(parts[49]) if len(parts) > 49 else None
                                float_mkt_cap = _sf(parts[44]) if len(parts) > 44 else None
                                open_price = _sf(parts[5]) if len(parts) > 5 else None
                                amplitude = _sf(parts[43]) if len(parts) > 43 else None
                                turnover_amount = _sf(parts[37]) if len(parts) > 37 else None
                                volume = _sf(parts[6]) if len(parts) > 6 else None
                                limit_up = _sf(parts[47]) if len(parts) > 47 else None
                                limit_down = _sf(parts[48]) if len(parts) > 48 else None
                                for h in holds:
                                    if h["c"] == code_from_resp:
                                        h["chg"] = chg
                                        h["pe"] = pe
                                        h["ret_1w"] = ret_1w
                                        h["price"] = price
                                        h["mkt_cap"] = mkt_cap
                                        h["pb"] = pb
                                        h["turnover"] = turnover
                                        h["vol_ratio"] = vol_ratio
                                        h["float_mkt_cap"] = float_mkt_cap
                                        h["open"] = open_price
                                        h["amplitude"] = amplitude
                                        h["turnover_amount"] = turnover_amount
                                        h["volume"] = volume
                                        h["limit_up"] = limit_up
                                        h["limit_down"] = limit_down
                                        # 52周最高/最低
                                        wk_high = _sf(parts[67]) if len(parts) > 67 else None
                                        wk_low = _sf(parts[68]) if len(parts) > 68 else None
                                        h["wk_high"] = wk_high
                                        h["wk_low"] = wk_low
                                        if wk_high is not None and wk_low is not None and wk_high > wk_low and price:
                                            h["wk_position"] = round((price - wk_low) / (wk_high - wk_low) * 100, 1)
                                        break
                    except Exception:
                        # 个别字段解析失败不中断整个流程
                        pass
                    # 从新浪F10财务指标页获取股息率和市销率数据
                    import urllib.request as _f10_ur, re as _f10_re
                    _f10_cache = globals().setdefault("_f10_cache", {})
                    _f10_now = time.time()

                    # ── 并行抓取：FGL 财务指标 ──
                    _fgl_codes = [h.get("c","") for h in holds if h.get("c") and h.get("mkt_cap")]
                    _fgl_urls = {}
                    for sc in _fgl_codes:
                        ck = f"fgl_{sc}"
                        if ck in _f10_cache and _f10_now - _f10_cache[ck][0] < 86400:
                            continue
                        _fgl_urls[sc] = f"https://vip.stock.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine/stockid/{sc}/displaytype/4.phtml"
                    if _fgl_urls:
                        def _fetch_fgl(sc):
                            try:
                                req = _f10_ur.Request(_fgl_urls[sc], headers={"User-Agent": "Mozilla/5.0"})
                                with _f10_ur.urlopen(req, timeout=10) as r:
                                    return sc, r.read().decode("gbk", errors="ignore")
                            except Exception:
                                return sc, None
                        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                            for sc, html in ex.map(_fetch_fgl, _fgl_urls):
                                if html:
                                    _f10_cache[f"fgl_{sc}"] = (_f10_now, html)
                    # 串行解析（已全部缓存，无网络开销）
                    for h in holds:
                        stock_code = h.get("c", "")
                        if not stock_code or not h.get("mkt_cap"):
                            continue
                        cache_key = f"fgl_{stock_code}"
                        cached = _f10_cache.get(cache_key)
                        if not cached:
                            continue
                        fgl_html = cached[1]
                        # 从原始HTML提取财报报告日期（"报告日期"行第一列日期）
                        h["fgl_date"] = ""
                        _fgl_date_m = _f10_re.search(
                            r'<strong>\s*报告日期\s*</strong></td>\s*<td[^>]*>(\d{4}-\d{2}-\d{2})',
                            fgl_html
                        )
                        if _fgl_date_m:
                            h["fgl_date"] = _fgl_date_m.group(1)
                        # 解析财务指标
                        fgl_rows = _f10_re.findall(
                            r'<tr[^>]*>'
                            r'<td[^>]*>(.*?)</td>'
                            r'\s*<td[^>]*>([^<]*)</td>'
                            r'\s*<td[^>]*>([^<]*)</td>',
                            fgl_html, _f10_re.DOTALL
                        )
                        fin_data = {}
                        for name_raw, v_latest, _ in fgl_rows:
                            name_clean = _f10_re.sub(r'<[^>]+>', '', name_raw).strip()
                            try:
                                fin_data[name_clean] = float(v_latest.strip()) if v_latest.strip() not in ["", "--"] else None
                            except ValueError:
                                pass
                        # 模糊匹配：字段名可能带前缀（如"成长能力主营业务收入增长率(%)"）
                        def _fget(*keys):
                            for k in keys:
                                for fk, fv in fin_data.items():
                                    if k in fk and fv is not None:
                                        return fv
                            return None
                        # 股息率 = 股息发放率 × 每股收益 ÷ 股价
                        div_payout = _fget("股息发放率")
                        eps_val = _fget("摊薄每股收益", "每股收益_调整后", "加权每股收益")
                        if div_payout is not None and eps_val is not None and h.get("price"):
                            h["dividend_yield"] = round(div_payout * eps_val / h["price"], 4)
                        # 市销率 PS
                        op_profit = _fget("主营业务利润(元)")
                        op_margin = _fget("主营业务利润率")
                        if op_profit is not None and op_margin is not None and op_margin > 0 and h.get("mkt_cap"):
                            estimated_revenue = op_profit / (op_margin / 100)
                            h["ps"] = round(h["mkt_cap"] * 1e8 / estimated_revenue, 2)
                        # 扩展财务指标（模糊匹配）
                        roe = _fget("净资产收益率")
                        if roe is not None:
                            h["roe"] = round(roe, 2)
                        op_margin_rate = _fget("营业利润率")
                        if op_margin_rate is not None:
                            h["op_margin"] = round(op_margin_rate, 2)
                        rev_growth = _fget("主营业务收入增长率", "营业收入增长率")
                        if rev_growth is not None:
                            h["rev_growth"] = round(rev_growth, 2)
                        debt_ratio = _fget("资产负债率")
                        if debt_ratio is not None:
                            h["debt_ratio"] = round(debt_ratio, 2)
                        cf_ps = _fget("每股经营性现金流")
                        if cf_ps is not None:
                            h["cf_ps"] = round(cf_ps, 4)
                        nav_ps = _fget("每股净资产")
                        if nav_ps is not None:
                            h["nav_ps"] = round(nav_ps, 2)
                        gross_margin = _fget("销售毛利率")
                        if gross_margin is not None:
                            h["gross_margin"] = round(gross_margin, 2)
                        quick_ratio = _fget("速动比率")
                        if quick_ratio is not None:
                            h["quick_ratio"] = round(quick_ratio, 2)
                        weighted_roe = _fget("加权净资产收益率")
                        if weighted_roe is not None:
                            h["weighted_roe"] = round(weighted_roe, 2)
                        retained_ps = _fget("每股未分配利润")
                        if retained_ps is not None:
                            h["retained_ps"] = round(retained_ps, 2)
                        total_assets = _fget("总资产(元)", "总资产)(元")
                        if total_assets is not None:
                            h["total_assets"] = round(total_assets / 1e8, 2)  # 转亿
                        net_profit_margin = _fget("销售净利率")
                        if net_profit_margin is not None:
                            h["net_profit_margin"] = round(net_profit_margin, 2)
                        # 净利润增长率（用于正确计算PEG）
                        net_profit_growth = _fget("净利润增长率")
                        if net_profit_growth is not None:
                            h["net_profit_growth"] = round(net_profit_growth, 2)
                            if h.get("pe") and net_profit_growth > 0:
                                h["peg"] = round(h["pe"] / net_profit_growth, 2)
                        main_biz_margin = _fget("主营业务利润率")
                        if main_biz_margin is not None:
                            h["main_biz_margin"] = round(main_biz_margin, 2)
                        current_ratio = _fget("流动比率")
                        if current_ratio is not None:
                            h["current_ratio"] = round(current_ratio, 2)
                        net_asset_growth = _fget("净资产增长率")
                        if net_asset_growth is not None:
                            h["net_asset_growth"] = round(net_asset_growth, 2)
                        capital_reserve_ps = _fget("每股资本公积金")
                        if capital_reserve_ps is not None:
                            h["capital_reserve_ps"] = round(capital_reserve_ps, 4)
                        roa = _fget("总资产利润率")
                        if roa is not None:
                            h["roa"] = round(roa, 2)
                        # ── 新增 FGL 字段 ──
                        # 主营业务成本率(%)：替代销售毛利率（新浪无毛利率数据），越低越好
                        main_biz_cost = _fget("主营业务成本率")
                        if main_biz_cost is not None:
                            h["main_biz_cost_ratio"] = round(main_biz_cost, 2)
                        # 总资产增长率(%)：衡量资产扩张速度
                        total_asset_growth = _fget("总资产增长率")
                        if total_asset_growth is not None:
                            h["total_asset_growth"] = round(total_asset_growth, 2)
                        # 现金比率(%)：最保守的短期偿债指标（现金类资产/流动负债）
                        cash_ratio = _fget("现金比率")
                        if cash_ratio is not None:
                            h["cash_ratio"] = round(cash_ratio, 2)
                        # 成本费用利润率(%)：每元成本费用创造的利润，越高越好
                        cost_profit = _fget("成本费用利润率")
                        if cost_profit is not None:
                            h["cost_profit_margin"] = round(cost_profit, 2)
                        # 经营现金净流量/净利润：利润质量，>1说明利润有真实现金支撑
                        cf_to_profit = _fget("经营现金净流量与净利润的比率")
                        if cf_to_profit is not None:
                            h["cashflow_to_profit"] = round(cf_to_profit, 2)
                    # ── 并行抓取：日K线 ──
                    _kl_codes = [(h.get("c",""), h.get("m","sz")) for h in holds if h.get("c")]
                    _kl_urls = {}
                    for sc, mk in _kl_codes:
                        ck = f"kline_{sc}"
                        if ck in _f10_cache and _f10_now - _f10_cache[ck][0] < 86400:
                            continue
                        _kl_urls[(sc, mk)] = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={mk}{sc}&scale=240&ma=no&datalen=504"
                    if _kl_urls:
                        def _fetch_kl(item):
                            (sc, mk), url = item
                            try:
                                req = _f10_ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                                with _f10_ur.urlopen(req, timeout=10) as r:
                                    return sc, json.loads(r.read().decode())
                            except Exception:
                                return sc, None
                        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                            for sc, data in ex.map(_fetch_kl, _kl_urls.items()):
                                if data:
                                    _f10_cache[f"kline_{sc}"] = (_f10_now, data)
                    # 串行解析 K 线
                    for h in holds:
                        stock_code = h.get("c", "")
                        market = h.get("m", "sz")
                        if not stock_code:
                            continue
                        cache_key = f"kline_{stock_code}"
                        cached = _f10_cache.get(cache_key)
                        if cached and _f10_now - cached[0] < 86400:
                            kline = cached[1]
                        else:
                            try:
                                kl_url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={market}{stock_code}&scale=240&ma=no&datalen=504"
                                kl_req = _f10_ur.Request(kl_url, headers={"User-Agent": "Mozilla/5.0"})
                                with _f10_ur.urlopen(kl_req, timeout=10) as kl_r:
                                    kl_raw = kl_r.read().decode()
                                kline = json.loads(kl_raw)
                                _f10_cache[cache_key] = (_f10_now, kline)
                            except Exception:
                                continue
                        if not kline or len(kline) < 2:
                            continue
                        closes = [float(p["close"]) for p in kline]
                        latest = closes[-1]
                        # 近1月/3月/6月/1年收益
                        for days, key in [(22, "ret_1m"), (66, "ret_3m"), (126, "ret_6m"), (252, "ret_1y")]:
                            if len(closes) >= days + 1:
                                h[key] = round((latest - closes[-(days + 1)]) / closes[-(days + 1)] * 100, 2)
                        # 最大回撤: 近1年(252天)和近2年(504天)
                        for period_days, key in [(252, "mdd_1y"), (504, "mdd_2y")]:
                            if len(closes) >= period_days:
                                period_closes = closes[-period_days:]
                                peak = period_closes[0]
                                max_dd = 0
                                for c in period_closes:
                                    if c > peak:
                                        peak = c
                                    dd = (peak - c) / peak * 100
                                    if dd > max_dd:
                                        max_dd = dd
                                h[key] = round(max_dd, 2)
                    # ── 并行抓取：公司概况（7天缓存，基本信息几乎不变）──
                    _corp_codes = [h.get("c","") for h in holds if h.get("c")]
                    _corp_urls = {}
                    for sc in _corp_codes:
                        ck = f"corp_{sc}"
                        if ck in _f10_cache and _f10_now - _f10_cache[ck][0] < 604800:
                            continue
                        _corp_urls[sc] = f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_CorpInfo/stockid/{sc}.phtml"
                    if _corp_urls:
                        def _fetch_corp(sc):
                            try:
                                req = _f10_ur.Request(_corp_urls[sc], headers={"User-Agent": "Mozilla/5.0"})
                                with _f10_ur.urlopen(req, timeout=10) as r:
                                    return sc, r.read().decode("gbk", errors="ignore")
                            except Exception:
                                return sc, None
                        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                            for sc, html in ex.map(_fetch_corp, _corp_urls):
                                if html:
                                    _f10_cache[f"corp_{sc}"] = (_f10_now, html)
                    # 串行解析公司概况
                    for h in holds:
                        stock_code = h.get("c", "")
                        if not stock_code:
                            continue
                        cache_key = f"corp_{stock_code}"
                        cached = _f10_cache.get(cache_key)
                        if not cached:
                            continue
                        corp_html = cached[1]
                        # 解析基本面字段（值单元格可能含有a标签等内嵌HTML）
                        corp_pairs = _f10_re.findall(
                            r'<td[^>]*>([^<]*(?:上市日期|机构类型|主营业务|所属行业|成立日期|注册资本)[^<]*)</td>\s*<td[^>]*>(.*?)</td>',
                            corp_html
                        )
                        for label, value_html in corp_pairs:
                            label_c = label.strip().rstrip("：:")
                            # 提取纯文本（去掉内嵌的HTML标签）
                            val_c = _f10_re.sub(r'<[^>]+>', '', value_html).strip()
                            if "上市日期" in label_c:
                                h["listing_date"] = val_c
                            elif "机构类型" in label_c or "所属行业" in label_c:
                                h["industry"] = val_c
                            elif "主营业务" in label_c:
                                h["main_biz"] = val_c[:80]
                            elif "成立日期" in label_c:
                                h["establish_date"] = val_c
                            elif "注册资本" in label_c:
                                h["reg_capital"] = val_c
                # ── 数据质量校验：过滤明显异常值 ──
                for h in holds:
                    for key in ('roe','main_biz_margin','net_profit_margin',
                                'rev_growth','net_profit_growth','net_asset_growth','total_asset_growth',
                                'quick_ratio','current_ratio','cash_ratio','cost_profit_margin',
                                'debt_ratio','main_biz_cost_ratio','roa',
                                'ret_1m','ret_3m','ret_1y','ret_1w',
                                'mdd_1y','mdd_2y','wk_position',
                                'cashflow_to_profit'):
                        v = h.get(key)
                        if v is not None and (v > 10000 or v < -10000):
                            h[key] = None  # 异常值置空
                    # 比率类不能为负（有明确下界的）
                    for key in ('quick_ratio','current_ratio','cash_ratio','debt_ratio'):
                        v = h.get(key)
                        if v is not None and v < 0:
                            h[key] = None
                # ── 持仓股票评分 ──
                hld_dims = _load_hld_dims()
                total_w = sum(d["w"] for d in hld_dims)
                for h in holds:
                    dim_scores = {}
                    for d in hld_dims:
                        val = h.get(d["key"])
                        dim_scores[d["key"]] = _hld_score(val, d["curve"])
                    weighted = sum(dim_scores[d["key"]] * d["w"] for d in hld_dims)
                    avg = weighted / total_w if total_w else 0
                    h["hld_score"] = round(avg, 1)
                    # 各维度明细（用于点击评分查看明细）
                    h["hld_dim_scores"] = [
                        {"k": d["key"], "n": d["name"], "s": round(dim_scores[d["key"]], 1),
                         "v": h.get(d["key"]), "w": d["w"]}
                        for d in hld_dims
                    ]
                self._send(*_json_response({"ok": True, "code": code, "holdings": holds, "report": hld_meta}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/stock-info":
            """查询个股实时行情（腾讯财经接口，GBK解码）"""
            try:
                q = urllib.parse.parse_qs(parsed.query)
                code = q.get("code", [""])[0]
                if not code:
                    self._send(*_json_response({"ok": False, "error": "缺少code参数"}, 400))
                    return
                _req = urllib.request.Request(f"http://qt.gtimg.cn/q={code}",
                                              headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(_req, timeout=get_timeout("default", 10)) as r:
                    raw = r.read()
                text = raw.decode("gbk")
                parts = text.split("~")
                if len(parts) < 46:
                    self._send(*_json_response({"ok": False, "error": "未找到股票信息"}, 404))
                    return
                def _n(idx):
                    try:
                        return float(parts[idx])
                    except (ValueError, IndexError):
                        return None
                def _s(idx):
                    try:
                        return parts[idx].strip()
                    except IndexError:
                        return ""
                data = {
                    "name": _s(1), "code": _s(2),
                    "price": _n(3), "prev_close": _n(4),
                    "open": _n(5),
                    "high": _n(33), "low": _n(34),
                    "volume": _s(6), "amount": _s(37),
                    "change_pct": _n(32), "change_amt": _n(31),
                    "turnover_rate": _n(38), "amplitude": _n(43),
                    "pe": _n(39),
                    "market_cap": _s(45), "float_market_cap": _s(44),
                    "high_52w": _s(47), "low_52w": _s(48),
                    "chg_60d": _n(52),
                    "buy_volume": _s(8), "sell_volume": _s(7),
                }
                self._send(*_json_response({"ok": True, "data": data}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/stock-profile":
            """查询个股公司概况+财务指标+股东结构（新浪F10，24h缓存）"""
            try:
                q = urllib.parse.parse_qs(parsed.query)
                code = q.get("code", [""])[0]
                if not code or not re.fullmatch(r"\d{6}", code):
                    self._send(*_json_response({"ok": False, "error": "缺少有效code参数(6位数字)"}, 400))
                    return
                now = time.time()
                _profile_cache: dict = globals().setdefault("_profile_cache", {})
                if code in _profile_cache and now - _profile_cache[code][0] < 86400:
                    self._send(*_json_response({"ok": True, "data": _profile_cache[code][1]}))
                    return
                import urllib.request as _ur

                def _fetch_f10(path: str) -> str:
                    url = f"https://vip.stock.finance.sina.com.cn/corp/go.php/{path}"
                    r = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    return _ur.urlopen(r, timeout=get_timeout("default", 10)).read().decode("gbk", errors="ignore")

                result: dict = {}

                # 1. 公司概况
                html = _fetch_f10(f"vCI_CorpInfo/stockid/{code}.phtml")
                for pat, key in [
                    (r'公司名称[：:]\s*</td>\s*<td[^>]*>(.*?)</td>', "company_name"),
                    (r'主营业务[：:]\s*</td>\s*<td[^>]*>(.*?)</td>', "main_business"),
                    (r'上市日期[：:]\s*</td>\s*<td[^>]*>(.*?)</td>', "listing_date"),
                    (r'成立日期[：:]\s*</td>\s*<td[^>]*>(.*?)</td>', "establish_date"),
                    (r'发行价格[：:]\s*</td>\s*<td[^>]*>(.*?)</td>', "issue_price"),
                    (r'注册资本[：:]\s*</td>\s*<td[^>]*>(.*?)</td>', "registered_capital"),
                    (r'组织形式[：:]\s*</td>\s*<td[^>]*>(.*?)</td>', "organization_form"),
                    (r'公司网址[：:]\s*</td>\s*<td[^>]*>(.*?)</td>', "website"),
                    (r'上市市场[：:]\s*</td>\s*<td[^>]*>(.*?)</td>', "listing_market"),
                ]:
                    m = re.search(pat, html)
                    if m:
                        val = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                        if val:
                            result[key] = val

                # 2. 财务指标（最新一期）
                html = _fetch_f10(f"vFD_FinancialGuideLine/stockid/{code}/displaytype/4.phtml")
                fin_rows = re.findall(
                    r'<tr[^>]*>'
                    r'<td[^>]*>(.*?)</td>'  # 指标名（含可能的<a>标签）
                    r'\s*<td[^>]*>([^<]*)</td>'  # 最新一期值
                    r'\s*<td[^>]*>([^<]*)</td>',  # 上一期值
                    html, re.DOTALL
                )
                fin_map_exact = {
                    "摊薄每股收益(元)": "eps",
                    "每股净资产_调整前(元)": "bps",
                    "净资产收益率(%)": "roe",
                    "主营业务利润率(%)": "profit_margin",
                    "销售净利率(%)": "net_margin",
                    "净利润增长率(%)": "net_profit_growth",
                    "主营业务收入增长率(%)": "revenue_growth",
                    "资产负债率(%)": "debt_ratio",
                    "流动比率": "current_ratio",
                    "速动比率": "quick_ratio",
                }
                finances = {}
                for name_cn_raw, val_latest, _ in fin_rows:
                    name_clean = re.sub(r'<[^>]+>', '', name_cn_raw).strip()
                    key = fin_map_exact.get(name_clean)
                    if not key:
                        # 模糊匹配
                        if "摊薄" in name_clean and "每股收益" in name_clean:
                            key = "eps"
                        elif "每股净资产" in name_clean:
                            key = "bps"
                        elif "净资产收益率" in name_clean and "扣除非" not in name_clean:
                            key = "roe"
                        elif "主营业务收入增长率" in name_clean:
                            key = "revenue_growth"
                        elif "主营业务利润率" in name_clean:
                            key = "profit_margin"
                        elif "净利润增长率" in name_clean:
                            key = "net_profit_growth"
                        elif "资产负债率" in name_clean:
                            key = "debt_ratio"
                        elif "流动比率" in name_clean and "速动" not in name_clean:
                            key = "current_ratio"
                        elif "速动比率" in name_clean:
                            key = "quick_ratio"
                    if key and key not in finances:
                        try:
                            finances[key] = round(float(val_latest.strip()), 2)
                        except (ValueError, TypeError):
                            pass
                if finances:
                    result["finances"] = finances

                # 3. 前5大股东
                html = _fetch_f10(f"vCI_StockHolder/stockid/{code}/displaytype/4.phtml")
                holder_rows = re.findall(
                    r'<td[^>]*>\s*<div[^>]*>\s*(\d+)\s*</div>\s*</td>'
                    r'\s*<td[^>]*>\s*<div[^>]*>(.*?)</div>\s*</td>'
                    r'\s*<td[^>]*>\s*<div[^>]*>(.*?)</div>\s*</td>'
                    r'\s*<td[^>]*>\s*<div[^>]*>(.*?)</div>\s*</td>',
                    html, re.DOTALL
                )
                if not holder_rows:
                    # 降级：尝试无<div>结构
                    holder_rows = re.findall(
                        r'<td[^>]*>(\d+)</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>',
                        html, re.DOTALL
                    )
                shareholders = []
                for num, name_raw, shares_raw, pct_raw in holder_rows[:5]:
                    name_clean = re.sub(r'<[^>]+>', '', name_raw).strip()
                    shares_clean = re.sub(r'<[^>]+>|&nbsp;', '', shares_raw).strip()
                    pct_str = re.sub(r'<[^>]+>|&nbsp;|[↑↓\s]', '', pct_raw).strip()
                    try:
                        pct_val = round(float(pct_str), 2)
                    except (ValueError, TypeError):
                        pct_val = 0
                    shareholders.append({"name": name_clean, "shares": shares_clean, "pct": pct_val})
                if shareholders:
                    result["shareholders"] = shareholders

                # 4. 基金持股数
                html = _fetch_f10(f"vCI_FundStockHolder/stockid/{code}.phtml")
                fund_count = len(set(re.findall(r'<td[^>]*>([\u4e00-\u9fff]{2,30}?基金)</td>', html)))
                if fund_count:
                    result["fund_count"] = fund_count

                _profile_cache[code] = (now, result)
                self._send(*_json_response({"ok": True, "data": result}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/monitor-config":
            try:
                with open(_CONFIG_PATH, encoding="utf-8") as _fmc:
                    cfg = json.load(_fmc)
                mc = cfg.get("fund_monitor", {})
                self._send(*_json_response({
                    "ok": True,
                    "config": {
                        "alert_drop_once": mc.get("alert_drop_once", -3),
                        "alert_jump_once": mc.get("alert_jump_once", 5),
                        "alert_accum_drop": mc.get("alert_accum_drop", -7),
                        "accum_jump": mc.get("accum_jump", 10),
                        "stock_alert_drop_red": mc.get("stock_alert_drop_red", -5),
                        "stock_alert_jump_red": mc.get("stock_alert_jump_red", 7),
                        "stock_alert_accum_drop_red": mc.get("stock_alert_accum_drop_red", -10),
                        "stock_alert_accum_jump_red": mc.get("stock_alert_accum_jump_red", 12),
                        "poll_interval_seconds": mc.get("poll_interval_seconds", 600),
                    }
                }))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/recommend-table":
            """返回市场优选全维度表格 HTML（实时拉取 TOP N 基金数据）"""
            _rec_file = os.path.join(_PROJECT_ROOT, ".fund_recommend_result.json")
            _rt_now = time.time()
            # 检查文件修改时间，若文件比缓存新则强制重建
            _rec_mtime = os.path.getmtime(_rec_file) if os.path.exists(_rec_file) else 0
            if _recommend_table_cache["data"]:
                _cache_time = _recommend_table_cache["data"]["time"]
                _cache_mtime = _recommend_table_cache["data"]["mtime"]
                if _rt_now - _cache_time < _recommend_cache_ttl and _rec_mtime <= _cache_mtime:
                    self._send(200, {"Content-Type": "text/html; charset=utf-8"}, _recommend_table_cache["data"]["html"].encode("utf-8"))
                    return
            try:
                from fund_render import _web_rich_recommend_table, _load_saved_recommend_data
                _saved = _load_saved_recommend_data()
                if _saved:
                    html = _web_rich_recommend_table(_saved)
                else:
                    html = ""
                if html:
                    _recommend_table_cache["data"] = {"html": html, "mtime": _rec_mtime, "time": _rt_now}
                    self._send(200, {"Content-Type": "text/html; charset=utf-8"}, html.encode("utf-8"))
                else:
                    self._send(200, {"Content-Type": "text/html; charset=utf-8"}, "<p style=\"color:#888;\">暂无推荐数据</p>".encode("utf-8"))
            except Exception as e:
                self._send(500, {"Content-Type": "text/html; charset=utf-8"},
                           f"<p style=\"color:#ef5350;\">获取推荐表格失败: {e}</p>".encode("utf-8"))
            return

        if parsed.path == "/api/recommend":
            path = os.path.join(_PROJECT_ROOT, ".fund_recommend_result.json")
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    self._send(*_json_response({"ok": True, "date": data.get("date", ""), "results": data.get("results", []), "timeout_count": data.get("timeout_count", 0)}))
                except Exception as e:
                    self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            else:
                self._send(*_json_response({"ok": True, "date": "", "results": []}))
            return

        if parsed.path == "/api/fund-table":
            """为自选基金生成完整数据富表格（含评分）—— 并行拉取数据"""
            # 检查是否有 fresh=1 参数强制跳过缓存（自动刷新用）
            _skip_cache = params.get("fresh", [""])[0] == "1"
            global _fund_table_cache
            now = time.time()
            if not _skip_cache and _fund_table_cache and now - _fund_table_cache[0] < _FUND_TABLE_CACHE_TTL:
                self._send(200, {"Content-Type": "text/html; charset=utf-8"}, _fund_table_cache[1].encode("utf-8"))
                return
            try:
                from fund_render import _web_rich_fund_table
                from fund_watch import get_scoring_data, _parse_real_time, _fetch_nav_from_lsjz
                from fund_scoring import calc_score_detail
                # 直接从文件读取基金列表（不使用缓存，因为页面可能刚增删过）
                fl_path = os.path.join(_PROJECT_ROOT, "data", "fund_list.json")
                if os.path.exists(fl_path):
                    with open(fl_path, encoding="utf-8") as _f:
                        fund_list = json.load(_f)
                else:
                    fund_list = []
                rows: list[dict] = []

                # ── 加载推荐缓存（避免每个基金重复拉取 ~200KB pingzhongdata）──
                _rec_cache_path = os.path.join(_PROJECT_ROOT, ".fund_recommend_result.json")
                _rec_cache: dict[str, dict] = {}
                if os.path.exists(_rec_cache_path):
                    try:
                        with open(_rec_cache_path, encoding="utf-8") as _f:
                            _rec_data = json.load(_f)
                        for _r in _rec_data.get("results", []):
                            _code = _r.get("code", "")
                            if _code:
                                _rec_cache[_code] = _r
                    except Exception:
                        pass

                def _process_one(code: str) -> dict | None:
                    """拉取一只基金数据并计算评分（优先从推荐缓存复用）"""
                    import datetime
                    try:
                        cached = _rec_cache.get(code)
                        if cached:
                            # ── 推荐缓存命中：直接用缓存数据，不另行拉取 ──
                            _td = _parse_real_time(code) if _skip_cache else cached.get("td")
                            day_s = f"{_td:+.2f}%" if _td is not None else ""
                            name = cached.get("name", "")
                            row = {
                                "code": code, "name": name,
                                "name_short": name[:12],
                                "day": day_s, "f5": cached.get("f5", ""),
                                "m1": cached.get("m1"), "m3": cached.get("m3"), "y1": cached.get("y1"),
                                "_day": day_s, "_f5": cached.get("f5", ""),
                                "_m1": f"{cached['m1']:+.1f}%" if cached.get("m1") is not None else "",
                                "_m3": f"{cached['m3']:+.1f}%" if cached.get("m3") is not None else "",
                                "_y1": f"{cached['y1']:+.1f}%" if cached.get("y1") is not None else "",
                                "mgr": (cached.get("mgr", "") or "")[:6],
                                "annual_return": cached.get("annual_return"),
                                "sharpe": cached.get("sharpe"), "sortino": cached.get("sortino"),
                                "max_dd": cached.get("max_dd"), "win_rate": cached.get("win_rate"),
                                "profit_ratio": cached.get("profit_ratio"),
                                "recovery": cached.get("recovery"),
                                "sy3": cached.get("sy3"), "sy6": cached.get("sy6"),
                                "sy2": cached.get("sy2"),
                                "volatility": cached.get("volatility"),
                                "calmar": cached.get("calmar"),
                                "max_loss_days": cached.get("max_loss_days"),
                                "sc": cached.get("sc"), "rate": cached.get("rate"),
                                "inst": cached.get("inst"),
                                "td": _td,
                            }
                            # 净值走势（从缓存取，无缓存时尝试拉取）
                            _trend = cached.get("_trend")
                            if _trend and len(_trend) >= 2:
                                row["_trend"] = _trend
                            elif code:
                                try:
                                    _nav_data = _fetch_nav_from_lsjz(code, max_pages=1)
                                    if _nav_data and len(_nav_data) >= 2:
                                        _trend_list = [[_nav_data[0]["d"], 0.0]] + [[_nav_data[i]["d"], round((_nav_data[i]["v"] - _nav_data[i-1]["v"]) / _nav_data[i-1]["v"] * 100, 2)] for i in range(1, len(_nav_data))]
                                        row["_trend"] = _trend_list
                                except Exception:
                                    pass
                            # 追加今日实时涨跌到走势图末尾
                            if _td is not None and row.get("_trend"):
                                row["_trend"].append((datetime.date.today().isoformat(), round(_td, 2)))
                            score_d = {k: cached.get(k) for k in (
                                "y1","m3","m1","f5","sy6","sy2","sy3",
                                "annual_return","sharpe","sortino",
                                "profit_ratio","win_rate","recovery","calmar",
                                "max_dd","volatility","max_loss_days",
                                "sc","rate","inst","td",
                            )}
                            score, details, skipped = calc_score_detail(score_d)
                            row["score"] = score
                            row["_score_detail"] = details
                            row["_skipped_weight"] = skipped
                            return row

                        # ── 推荐缓存未命中：全量拉取 pingzhongdata ──
                        d = get_scoring_data(code)
                        if not d.get("n"):
                            return None
                        _td = _parse_real_time(code)
                        d["td"] = _td
                        navs = d.get("nav", [])
                        td = d.get("td")
                        day_s = f"{td:+.2f}%" if td is not None else ""
                        if len(navs) >= 5:
                            pct = (navs[-1]["v"] - navs[-5]["v"]) / navs[-5]["v"] * 100
                            d["f5"] = f"{pct:+.1f}%"
                        row = {
                            "code": code, "name": d.get("n", ""),
                            "name_short": (d.get("n", "") or "")[:12],
                            "day": day_s, "f5": d.get("f5", ""),
                            "m1": d.get("m1"), "m3": d.get("m3"), "y1": d.get("y1"),
                            "_day": day_s, "_f5": d.get("f5", ""),
                            "_m1": f"{d['m1']:+.1f}%" if d.get("m1") is not None else "",
                            "_m3": f"{d['m3']:+.1f}%" if d.get("m3") is not None else "",
                            "_y1": f"{d['y1']:+.1f}%" if d.get("y1") is not None else "",
                            "mgr": (d.get("mgr", "") or "")[:6],
                            "annual_return": d.get("annual_return"),
                            "sharpe": d.get("sharpe"), "sortino": d.get("sortino"),
                            "max_dd": d.get("max_dd"), "win_rate": d.get("win_rate"),
                            "profit_ratio": d.get("profit_ratio"),
                            "recovery": d.get("recovery"),
                            "sy3": d.get("sy3"), "sy6": d.get("sy6"),
                            "sy2": d.get("sy2"),
                            "volatility": d.get("volatility"),
                            "calmar": d.get("calmar"),
                            "max_loss_days": d.get("max_loss_days"),
                            "sc": d.get("sc"), "rate": d.get("rate"),
                            "inst": d.get("inst"),
                            "td": d.get("td"),
                        }
                        # 近20日净值走势
                        if len(navs) >= 2:
                            _trend_navs = navs[-20:]
                            row["_trend"] = [[_trend_navs[0]["d"], 0.0]] + [[_trend_navs[i]["d"], round((_trend_navs[i]["v"] - _trend_navs[i-1]["v"]) / _trend_navs[i-1]["v"] * 100, 2)] for i in range(1, len(_trend_navs))]
                            # 追加今日实时涨跌到走势图末尾
                            if td is not None:
                                row["_trend"].append((datetime.date.today().isoformat(), round(td, 2)))
                        score_d = {k: d.get(k) for k in (
                            "y1","m3","m1","f5","sy6","sy2","sy3",
                            "annual_return","sharpe","sortino",
                            "profit_ratio","win_rate","recovery","calmar",
                            "max_dd","volatility","max_loss_days",
                            "sc","rate","inst","td",
                        )}
                        score, details, skipped = calc_score_detail(score_d)
                        row["score"] = score
                        row["_score_detail"] = details
                        row["_skipped_weight"] = skipped
                        return row
                    except Exception:
                        return None

                # 并行拉取所有基金数据
                _fund_list_for_progress = list(fund_list)
                write_heartbeat("fund-td-refresh", total=len(_fund_list_for_progress),
                                progress=0, phase="刷新td",
                                detail=f"0/{len(_fund_list_for_progress)} 只基金")
                _last_hb_pct = -1
                _fund_td_done = 0
                with concurrent.futures.ThreadPoolExecutor(max_workers=get_config("network", "max_workers", "server_fund_table", default=20)) as executor:
                    fut_map = {executor.submit(_process_one, f["code"]): f["code"] for f in _fund_list_for_progress}
                    try:
                        for fut in concurrent.futures.as_completed(fut_map, timeout=120):
                            try:
                                result = fut.result()
                            except Exception:
                                continue
                            _fund_td_done += 1
                            _pct = int(_fund_td_done / len(_fund_list_for_progress) * 100) if _fund_list_for_progress else 100
                            if _pct != _last_hb_pct or _fund_td_done == len(_fund_list_for_progress):
                                _last_hb_pct = _pct
                                update_heartbeat("fund-td-refresh", progress=_fund_td_done,
                                                 total=len(_fund_list_for_progress),
                                                 detail=f"{_fund_td_done}/{len(_fund_list_for_progress)} 只基金")
                            if result is not None:
                                rows.append(result)
                    except concurrent.futures.TimeoutError:
                        # 超时后取消剩余任务
                        for _f in fut_map:
                            _f.cancel()
                        print(f"[fund-table] 超时: {_fund_td_done}/{len(_fund_list_for_progress)} 只完成", flush=True)
                clear_heartbeat("fund-td-refresh")

                # 按 fund_list 原始顺序排序
                order = {f["code"]: i for i, f in enumerate(fund_list)}
                rows.sort(key=lambda r: order.get(r["code"], 999))

                html = _web_rich_fund_table(rows)
                _fund_table_cache = (now, html)
                self._send(200, {"Content-Type": "text/html; charset=utf-8"}, html.encode("utf-8"))
            except Exception as e:
                self._send(500, {"Content-Type": "text/html; charset=utf-8"},
                           f"<p style=\"color:#ef5350;\">获取基金表格失败: {e}</p>".encode("utf-8"))
            return

        if parsed.path == "/" or parsed.path == "/index.html":
            self._send_file("fund_manage.html")
            return

        if parsed.path == "/config.json":
            self._send_file("config.json", is_config=True)
            return

        # 尝试提供静态文件（JS/CSS）
        self._send_file(parsed.path.lstrip("/"))

    def _send_file(self, filename: str, is_config: bool = False):
        # 路径穿越防护：确保请求的文件在项目目录内
        if is_config:
            # 返回 data/ 下的 config.json（排除法，勿用于前端页面）
            path = os.path.normpath(os.path.join(_PROJECT_ROOT, "data", filename))
        else:
            # 优先在 templates/ 查找，降级到 src/
            tpl = os.path.normpath(os.path.join(_PROJECT_ROOT, "templates", filename))
            if os.path.exists(tpl):
                path = tpl
            else:
                path = os.path.normpath(os.path.join(_SCRIPT_DIR, filename))
        allowed = [
            os.path.normpath(_SCRIPT_DIR),
            os.path.normpath(os.path.join(_PROJECT_ROOT, "templates")),
        ]
        if not any(path.startswith(d + os.sep) or path == d for d in allowed):
            self._send(403, {"Content-Type": "text/plain"}, b"Forbidden")
            return
        if not os.path.exists(path):
            self._send(404, {"Content-Type": "text/plain"}, b"Not Found")
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            self._send(500, {"Content-Type": "text/plain"}, f"Internal error: {e}".encode())
            return
        ext = os.path.splitext(filename)[1]
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(ext, "application/octet-stream")
        self._send(200, {"Content-Type": ctype}, data)

    def do_POST(self):
        global _fund_table_cache
        length = 0
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            pass
        raw = self.rfile.read(length)
        body = {}
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                self._send(*_json_response({"ok": False, "error": "JSON 格式错误"}, 400))
                return

        if self.path == "/api/add":
            try:
                codes = body.get("codes", [])
                if not isinstance(codes, list):
                    codes = [codes]
                funds = _load()
                existing = {f["code"] for f in funds}
                added = []
                skipped = []
                invalid = []
                for code in codes:
                    code = code.strip()
                    if not re.fullmatch(r"\d{6}", code):
                        invalid.append(code)
                    elif code in existing:
                        skipped.append(code)
                    else:
                        name = _fetch_fund_name(code)
                        funds.append({"code": code, "name": name})
                        existing.add(code)
                        added.append(code)
                _save(funds)
                _fund_table_cache = None
                self._send(*_json_response({
                    "ok": True,
                    "added": added,
                    "skipped": skipped,
                    "invalid": invalid,
                    "total": len(funds),
                }))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/remove":
            try:
                codes = body.get("codes", [])
                if not isinstance(codes, list):
                    codes = [codes]
                funds = _load()
                before = len(funds)
                remove_set = set(codes)
                removed = [f["code"] for f in funds if f["code"] in remove_set]
                funds = [f for f in funds if f["code"] not in remove_set]
                _save(funds)
                _fund_table_cache = None
                self._send(*_json_response({
                    "ok": True,
                    "removed": removed,
                    "total": len(funds),
                }))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/reorder-funds":
            try:
                codes = body.get("codes", [])
                if not isinstance(codes, list) or not codes:
                    self._send(*_json_response({"ok": False, "error": "codes 不能为空"}, 400))
                    return
                funds = _load()
                # 按新顺序重排，保留原名
                code_map = {f["code"]: f.get("name", "") for f in funds}
                new_funds = []
                for c in codes:
                    if c in code_map:
                        new_funds.append({"code": c, "name": code_map[c]})
                # 补上不在新顺序中的基金
                existing = set(codes)
                for f in funds:
                    if f["code"] not in existing:
                        new_funds.append(f)
                _save(new_funds)
                _fund_table_cache = None
                self._send(*_json_response({"ok": True, "total": len(new_funds)}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/dims":
            try:
                dims = body.get("dims", [])
                if not dims:
                    self._send(*_json_response({"ok": False, "error": "dims 不能为空"}, 400))
                    return
                with open(_CONFIG_PATH, encoding="utf-8") as _fcfg2:
                    cfg = json.load(_fcfg2)
                if "scoring" not in cfg:
                    cfg["scoring"] = {}
                cfg["scoring"]["dims"] = dims
                _write_config(cfg)
                # 重新加载评分模块使新配置生效
                import importlib
                import fund_scoring
                importlib.reload(fund_scoring)
                import fund_render
                importlib.reload(fund_render)
                # 重新计算缓存中的评分（无需重新拉取数据）
                _recalc_cached_scores()
                # 预热推荐表缓存（从文件读取，不拉取实时 td，速度快）
                _fund_table_cache = None
                try:
                    from fund_render import _web_rich_recommend_table, _load_saved_recommend_data
                    _saved = _load_saved_recommend_data()
                    if _saved:
                        _rec_mtime = os.path.getmtime(os.path.join(_PROJECT_ROOT, ".fund_recommend_result.json"))
                        _recommend_table_cache["data"] = {"html": _web_rich_recommend_table(_saved), "mtime": _rec_mtime, "time": time.time()}
                        print(f"[dims] 推荐表缓存已预热，{len(_saved)} 只", flush=True)
                    else:
                        _recommend_table_cache["data"] = None
                except Exception as ex:
                    print(f"[dims] 推荐表缓存预热失败: {ex}", flush=True)
                    _recommend_table_cache["data"] = None
                self._send(*_json_response({"ok": True, "message": "权重已保存"}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/dims/calibrate":
            """基于推荐数据的百分位自动校准评分曲线"""
            try:
                rec_path = os.path.join(_PROJECT_ROOT, ".fund_recommend_result.json")
                if not os.path.exists(rec_path):
                    self._send(*_json_response({"ok": False, "error": "暂无推荐数据，请先运行推荐"}, 400))
                    return
                with open(rec_path, encoding="utf-8") as _fr:
                    rec_data = json.load(_fr).get("results", [])
                if not rec_data:
                    self._send(*_json_response({"ok": False, "error": "暂无推荐数据，请先运行推荐"}, 400))
                    return
                with open(_CONFIG_PATH, encoding="utf-8") as _fc:
                    cfg = json.load(_fc)
                dims = cfg.get("scoring", {}).get("dims", [])
                if not dims:
                    self._send(*_json_response({"ok": False, "error": "评分维度配置为空"}, 400))
                    return

                # 从推荐结果取维度值用于校准
                # "越低越好"的维度
                lower_better = {"波动率", "最大回撤", "最大连跌天数", "费率"}
                # 需要解析百分号字符串的字段
                pct_keys = {"f5"}

                # 直接使用推荐缓存中的维度数据（无需重新拉取）
                all_data = rec_data
                for dim in dims:
                    key = dim.get("key", "")
                    name = dim.get("name", "")
                    vals = []
                    for d in all_data:
                        v = d.get(key)
                        if key in pct_keys and isinstance(v, str) and "%" in v:
                            v = float(v.replace("%", "").replace("+", ""))
                        if v is not None and isinstance(v, (int, float)):
                            vals.append(float(v))
                    if len(vals) < 10:
                        continue  # 数据不足不校准
                    vals.sort()
                    n = len(vals)
                    # 每10%一个百分位点，共11个点，粒度更细
                    percentiles = list(range(0, 101, 10))
                    pts = []
                    for p in percentiles:
                        idx = min(int(n * p / 100), n - 1)
                        pts.append(vals[idx])
                    # 去重：相邻值相同则保留一个（避免曲线上出现平线断点）
                    uniq = []
                    for v in pts:
                        if not uniq or abs(v - uniq[-1]) > 1e-9:
                            uniq.append(v)
                    pts = uniq
                    # 重新计算对应的百分位索引
                    is_lower = name in lower_better
                    curve = []
                    n_pts = len(pts)
                    for i, v in enumerate(pts):
                        pct_pos = i / (n_pts - 1) if n_pts > 1 else 0.5
                        if is_lower:
                            score = round((1 - pct_pos) * 100)
                        else:
                            score = round(pct_pos * 100)
                        curve.append([v, score])
                    # 极值延展：在得分接近0的一端外推一段，让超低/超高值也有区分度
                    data_range = curve[-1][0] - curve[0][0] if len(curve) >= 2 else 10
                    extend = max(round(data_range * 0.3, 2), round(abs(curve[0][0]) * 0.15, 2), 3)
                    if not is_lower and len(curve) >= 2 and curve[0][1] < 10:
                        # 越高越好：左端延展（提升首点分数+加左延展点）
                        bump = min(10, max(5, curve[1][1] / 2))
                        new_left = round(curve[0][0] - extend, 2)
                        curve.insert(0, [new_left, 0])
                        if len(curve) >= 3 and curve[2][1] > bump:
                            curve[1][1] = round(bump)
                    elif is_lower and len(curve) >= 2 and curve[-1][1] < 10:
                        # 越低越好：右端延展（提升末点分数+加右延展点）
                        bump = min(10, max(5, curve[-2][1] / 2))
                        new_right = round(curve[-1][0] + extend, 2)
                        curve.append([new_right, 0])
                        if len(curve) >= 3 and curve[-3][1] > bump:
                            curve[-2][1] = round(bump)
                    dim["curve"] = {"points": curve}

                _write_config(cfg)
                import importlib
                import fund_scoring
                importlib.reload(fund_scoring)
                import fund_render
                importlib.reload(fund_render)
                # 重新计算缓存中的评分
                _recalc_cached_scores()
                # 预热推荐表缓存（从文件读取，不拉取实时 td，速度快）
                _fund_table_cache = None
                try:
                    from fund_render import _web_rich_recommend_table, _load_saved_recommend_data
                    _saved = _load_saved_recommend_data()
                    if _saved:
                        _rec_mtime = os.path.getmtime(os.path.join(_PROJECT_ROOT, ".fund_recommend_result.json"))
                        _recommend_table_cache["data"] = {"html": _web_rich_recommend_table(_saved), "mtime": _rec_mtime, "time": time.time()}
                        print(f"[dims/calibrate] 推荐表缓存已预热，{len(_saved)} 只", flush=True)
                    else:
                        _recommend_table_cache["data"] = None
                except Exception as ex:
                    print(f"[dims/calibrate] 推荐表缓存预热失败: {ex}", flush=True)
                    _recommend_table_cache["data"] = None
                self._send(*_json_response({"ok": True, "message": "评分曲线已基于百分位自动校准"}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/monitor-config":
            try:
                with open(_CONFIG_PATH, encoding="utf-8") as _fmc:
                    cfg = json.load(_fmc)
                cfg["fund_monitor"] = {
                    "alert_drop_once": float(body.get("alert_drop_once", -3)),
                    "alert_jump_once": float(body.get("alert_jump_once", 5)),
                    "alert_accum_drop": float(body.get("alert_accum_drop", -7)),
                    "accum_jump": float(body.get("accum_jump", 10)),
                    "stock_alert_drop_red": float(body.get("stock_alert_drop_red", -5)),
                    "stock_alert_jump_red": float(body.get("stock_alert_jump_red", 7)),
                    "stock_alert_accum_drop_red": float(body.get("stock_alert_accum_drop_red", -10)),
                    "stock_alert_accum_jump_red": float(body.get("stock_alert_accum_jump_red", 12)),
                    "poll_interval_seconds": int(body.get("poll_interval_seconds", 600)),
                }
                _write_config(cfg)
                self._send(*_json_response({"ok": True, "message": "监控配置已更新"}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/recommend-config":
            try:
                with open(_CONFIG_PATH, encoding="utf-8") as _fcfg:
                    cfg = json.load(_fcfg)
                rec = cfg.setdefault("recommend", {})
                rec.update({
                    "top_n": int(body.get("top_n", rec.get("top_n", 200))),
                    "filter_conditions": body.get("filter_conditions", rec.get("filter_conditions", [])),
                    "show_top": int(body.get("show_top", rec.get("show_top", 20))),
                    "skip_missing_perf": bool(body.get("skip_missing_perf", rec.get("skip_missing_perf", False))),
                    "skip_limited": bool(body.get("skip_limited", rec.get("skip_limited", False))),
                    "rank_sort": str(body.get("rank_sort", rec.get("rank_sort", "1n"))),
                })
                _write_config(cfg)
                self._send(*_json_response({"ok": True, "message": "推荐配置已更新"}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/recommend/stop":
            try:
                with _proc_lock:
                    if _recommend_state["proc"] and _recommend_state["proc"].poll() is None:
                        _recommend_state["proc"].terminate()
                        _recommend_state["proc"] = None
                        clear_heartbeat("fund_recommend")
                        self._send(*_json_response({"ok": True, "message": "推荐任务已取消"}))
                        print("[recommend] 用户取消推荐任务", flush=True)
                    else:
                        self._send(*_json_response({"ok": False, "error": "当前没有正在运行的推荐任务"}, 404))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/recommend":
            try:
                with _proc_lock:
                    if _recommend_state["proc"] and _recommend_state["proc"].poll() is None:
                        self._send(*_json_response({"ok": False, "error": "推荐任务正在运行中"}))
                        return
                if _spawn_recommend():
                    self._send(*_json_response({"ok": True, "message": "推荐任务已启动，约需 16 分钟"}))
                else:
                    self._send(*_json_response({"ok": False, "error": "推荐任务启动失败"}, 500))
            except Exception as e:
                print(f"[ERROR] /api/recommend 异常: {e}", flush=True)
                import traceback; traceback.print_exc()
                clear_heartbeat("fund_recommend")
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/task/start":
            task_id = body.get("task_id", "")
            if task_id not in _task_scripts:
                self._send(*_json_response({"ok": False, "error": f"未知任务: {task_id}"}, 400))
                return
            if _spawn_task(task_id):
                self._send(*_json_response({"ok": True, "message": f"任务 {task_id} 已启动"}))
            else:
                self._send(*_json_response({"ok": False, "error": f"启动任务 {task_id} 失败"}, 500))
            return

        if self.path == "/api/task/stop":
            task_id = body.get("task_id", "")
            if task_id not in _task_scripts:
                self._send(*_json_response({"ok": False, "error": f"未知任务: {task_id}"}, 400))
                return
            if _stop_task(task_id):
                self._send(*_json_response({"ok": True, "message": f"任务 {task_id} 已停止"}))
            else:
                self._send(*_json_response({"ok": False, "error": f"任务 {task_id} 未在运行"}, 404))
            return

        if self.path == "/api/dims-presets":
            try:
                with open(_CONFIG_PATH, encoding="utf-8") as f:
                    cfg = json.load(f)
                action = body.get("action")
                name = (body.get("name") or "").strip()
                presets = cfg.setdefault("scoring_presets", {})
                if not presets:
                    _init_builtin_presets(cfg)
                    presets = cfg["scoring_presets"]
                if action == "save":
                    if name == "系统默认":
                        self._send(*_json_response({"ok": False, "error": "系统默认预设不可覆盖"}, 400))
                        return
                    if name not in presets:
                        self._send(*_json_response({"ok": False, "error": f"预设「{name}」不存在"}, 404))
                        return
                    presets[name] = {"dims": cfg["scoring"]["dims"]}
                elif action == "save_as":
                    if not name:
                        self._send(*_json_response({"ok": False, "error": "预设名称不能为空"}, 400))
                        return
                    if name in presets:
                        self._send(*_json_response({"ok": False, "error": f"预设「{name}」已存在"}, 400))
                        return
                    if len(presets) >= 20:
                        self._send(*_json_response({"ok": False, "error": "预设数量已达上限（20）"}, 400))
                        return
                    presets[name] = {"dims": cfg["scoring"]["dims"]}
                elif action == "delete":
                    if name == "系统默认":
                        self._send(*_json_response({"ok": False, "error": "系统默认预设不可删除"}, 400))
                        return
                    if name not in presets:
                        self._send(*_json_response({"ok": False, "error": f"预设「{name}」不存在"}, 404))
                        return
                    del presets[name]
                elif action:
                    self._send(*_json_response({"ok": False, "error": "未知操作"}, 400))
                    return
                if action in ("save", "save_as"):
                    cfg.setdefault("scoring", {})["current_preset"] = name
                _write_config(cfg)
                current = cfg.get("scoring", {}).get("current_preset", "系统默认")
                self._send(*_json_response({"ok": True, "presets": presets, "current": current}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/holdings-col-order":
            """保存持仓表格列顺序"""
            try:
                order = body.get("order", [])
                with open(_CONFIG_PATH, encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg["holdings_col_order"] = order
                _write_config(cfg)
                self._send(*_json_response({"ok": True}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/prefs":
            """保存用户偏好设置"""
            try:
                prefs = body.get("prefs", {})
                with open(_CONFIG_PATH, encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg["user_prefs"] = prefs
                _write_config(cfg)
                self._send(*_json_response({"ok": True}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/holdings-dims/save":
            """保存持仓评分维度配置"""
            try:
                dims = body.get("dims")
                if not dims:
                    self._send(*_json_response({"ok": False, "error": "缺少dims参数"}, 400))
                    return
                with open(_CONFIG_PATH, encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg.setdefault("holdings_scoring", {})["dims"] = dims
                _write_config(cfg)
                globals()["_HLD_DIMS_CACHE"] = None
                self._send(*_json_response({"ok": True}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/holdings-dims/reset":
            """重置持仓评分维度到默认"""
            try:
                with open(_CONFIG_PATH, encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg.setdefault("holdings_scoring", {}).pop("dims", None)
                _write_config(cfg)
                globals()["_HLD_DIMS_CACHE"] = None
                self._send(*_json_response({"ok": True}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/holdings-dims/calibrate":
            """自动校准持仓评分曲线"""
            try:
                code = body.get("code")
                if not code:
                    self._send(*_json_response({"ok": False, "error": "缺少code参数"}, 400))
                    return
                from fund_watch import _parse_holdings
                from fund_utils import fetch, _retry_fetch
                holds = _parse_holdings(code) or []
                # 去重
                _seen = set()
                _deduped = []
                for h in holds:
                    c = h.get("c", "")
                    if c and c not in _seen:
                        _seen.add(c)
                        _deduped.append(h)
                holds = _deduped
                if not holds:
                    self._send(*_json_response({"ok": False, "error": "无持仓数据"}, 400))
                    return
                codes_str = ",".join((h.get("m","sz")+h["c"]) for h in holds)
                try:
                    raw = _retry_fetch(api_url("tencent_realtime", code=codes_str))
                    for line in raw.strip().split(";"):
                        if not line: continue
                        parts = line.split("~")
                        if len(parts) > 67:
                            code_r = parts[2]; price = float(parts[3]) if parts[3] else 0
                            for h in holds:
                                if h["c"] == code_r:
                                    h["price"] = price
                                    h["mkt_cap"] = float(parts[45]) if len(parts)>45 and parts[45] else None
                                    h["pe"] = float(parts[39]) if len(parts)>39 and parts[39] else None
                                    h["pb"] = float(parts[46]) if len(parts)>46 and parts[46] else None
                                    h["turnover"] = float(parts[38]) if len(parts)>38 and parts[38] else None
                                    h["vol_ratio"] = float(parts[49]) if len(parts)>49 and parts[49] else None
                                    h["float_mkt_cap"] = float(parts[44]) if len(parts)>44 and parts[44] else None
                                    h["wk_high"] = float(parts[67]) if len(parts)>67 and parts[67] else None
                                    h["wk_low"] = float(parts[68]) if len(parts)>68 and parts[68] else None
                                    h["amplitude"] = float(parts[43]) if len(parts)>43 and parts[43] else None
                                    h["open"] = float(parts[5]) if len(parts)>5 and parts[5] else None
                                    wk_high = h.get("wk_high"); wk_low = h.get("wk_low")
                                    if wk_high and wk_low and wk_high > wk_low and price:
                                        h["wk_position"] = round((price - wk_low) / (wk_high - wk_low) * 100, 1)
                                    break
                except: pass
                import urllib.request as _cal_ur, re as _cal_re
                _cal_cache = globals().setdefault("_f10_cache", {})
                _cal_now = time.time()
                for h in holds:
                    sc = h.get("c", "")
                    if not sc or not h.get("mkt_cap"): continue
                    ck = f"fgl_{sc}"; cached = _cal_cache.get(ck)
                    if cached and _cal_now - cached[0] < 86400:
                        fgl_html = cached[1]
                    else:
                        try:
                            fgl_url = f"https://vip.stock.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine/stockid/{sc}/displaytype/4.phtml"
                            fgl_req = _cal_ur.Request(fgl_url, headers={"User-Agent": "Mozilla/5.0"})
                            with _cal_ur.urlopen(fgl_req, timeout=10) as fgl_r:
                                fgl_html = fgl_r.read().decode("gbk", errors="ignore")
                            _cal_cache[ck] = (_cal_now, fgl_html)
                        except: continue
                    fgl_rows = _cal_re.findall(r'<tr[^>]*><td[^>]*>(.*?)</td>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>', fgl_html, _cal_re.DOTALL)
                    fin = {}
                    for nr, vl, _ in fgl_rows:
                        nc = _cal_re.sub(r'<[^>]+>', '', nr).strip()
                        try: fin[nc] = float(vl.strip()) if vl.strip() not in ["","--"] else None
                        except: pass
                    def _fg(*ks):
                        for k in ks:
                            for fk, fv in fin.items():
                                if k in fk and fv is not None: return fv
                        return None
                    roe = _fg("净资产收益率")
                    if roe: h["roe"] = round(roe, 2)
                    opm = _fg("营业利润率")
                    if opm: h["op_margin"] = round(opm, 2)
                    rg = _fg("主营业务收入增长率","营业收入增长率")
                    if rg: h["rev_growth"] = round(rg, 2)
                    dr = _fg("资产负债率")
                    if dr: h["debt_ratio"] = round(dr, 2)
                    cf = _fg("每股经营性现金流")
                    if cf: h["cf_ps"] = round(cf, 4)
                    nav = _fg("每股净资产")
                    if nav: h["nav_ps"] = round(nav, 2)
                    gm = _fg("销售毛利率")
                    if gm: h["gross_margin"] = round(gm, 2)
                    qr = _fg("速动比率")
                    if qr: h["quick_ratio"] = round(qr, 2)
                    npm = _fg("销售净利率")
                    if npm: h["net_profit_margin"] = round(npm, 2)
                for h in holds:
                    sc = h.get("c",""); mk = h.get("m","sz")
                    if not sc: continue
                    ck = f"kline_{sc}"; cached = _cal_cache.get(ck)
                    if cached and _cal_now - cached[0] < 86400:
                        kline = cached[1]
                    else:
                        try:
                            kl_url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={mk}{sc}&scale=240&ma=no&datalen=504"
                            kl_req = _cal_ur.Request(kl_url, headers={"User-Agent": "Mozilla/5.0"})
                            with _cal_ur.urlopen(kl_req, timeout=10) as kl_r:
                                kline = json.loads(kl_r.read().decode())
                            _cal_cache[ck] = (_cal_now, kline)
                        except: continue
                    if not kline or len(kline) < 2: continue
                    closes = [float(p["close"]) for p in kline]
                    lc = closes[-1]
                    for days, key in [(22,"ret_1m"),(66,"ret_3m"),(126,"ret_6m"),(252,"ret_1y")]:
                        if len(closes) >= days+1: h[key] = round((lc-closes[-(days+1)])/closes[-(days+1)]*100, 2)
                    for pdays, key in [(252,"mdd_1y"),(504,"mdd_2y")]:
                        if len(closes) >= pdays:
                            pc = closes[-pdays:]; pk = pc[0]; md = 0
                            for c in pc:
                                if c > pk: pk = c
                                dd = (pk-c)/pk*100
                                if dd > md: md = dd
                            h[key] = round(md, 2)
                hld_dims = _load_hld_dims()
                sample_map = {d["key"]: [] for d in hld_dims}
                for d in hld_dims:
                    for h in holds:
                        v = h.get(d["key"])
                        if v is not None:
                            sample_map[d["key"]].append(v)
                new_dims = []
                for d in hld_dims:
                    # 锁定维度跳过校准，使用绝对标准曲线
                    if d.get("locked"):
                        new_dims.append(dict(d))
                        continue
                    samples = sample_map.get(d["key"], [])
                    if len(samples) < 3:
                        new_dims.append(dict(d))
                        continue
                    samples.sort()
                    n = len(samples)
                    pcts = [0, 20, 40, 60, 80, 100]
                    pts = []
                    for pi, pc in enumerate(pcts):
                        idx = int(n * pc / 100)
                        if idx >= n: idx = n - 1
                        x = round(samples[idx], 2)
                        y = round(pi * 20, 0)
                        pts.append([x, y])
                    unique = [pts[0]]
                    for p in pts[1:]:
                        if p[0] != unique[-1][0]:
                            unique.append(p)
                    if len(unique) < 2:
                        unique = [[0,0],[100,100]]
                    is_lower = d["key"] in ("debt_ratio", "mdd_1y", "wk_position", "pe")
                    curve = unique
                    data_range = curve[-1][0] - curve[0][0] if len(curve) >= 2 else 10
                    extend = max(round(data_range * 0.3, 2), round(abs(curve[0][0]) * 0.15, 2), 3)
                    if not is_lower and len(curve) >= 2 and curve[0][1] < 10:
                        bump = min(10, max(5, curve[1][1] / 2))
                        new_left = round(curve[0][0] - extend, 2)
                        curve.insert(0, [new_left, 0])
                        if len(curve) >= 3 and curve[2][1] > bump:
                            curve[1][1] = round(bump)
                    elif is_lower and len(curve) >= 2 and curve[-1][1] < 10:
                        bump = min(10, max(5, curve[-2][1] / 2))
                        new_right = round(curve[-1][0] + extend, 2)
                        curve.append([new_right, 0])
                        if len(curve) >= 3 and curve[-3][1] > bump:
                            curve[-2][1] = round(bump)
                    new_dims.append(dict(d, curve=curve))
                with open(_CONFIG_PATH, encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg.setdefault("holdings_scoring", {})["dims"] = new_dims
                _write_config(cfg)
                globals()["_HLD_DIMS_CACHE"] = None
                self._send(*_json_response({"ok": True, "dims": new_dims, "message": f"基于{len(holds)}只持仓数据校准完成"}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        self._send(*_json_response({"ok": False, "error": "未知接口"}, 404))


# ── 内置预设 ────────────────────────────────────
_BUILTIN_PRESETS = {
    "系统默认": {
        "dims": [
            {"name":"近3月收益","key":"m3","weight":0.12,"enabled":True,"desc":"近三个月涨跌幅，中期趋势","curve":{"points":[[0,0],[30,50],[60,80],[90,100]]},"category":"perf"},
            {"name":"近1月收益","key":"m1","weight":0.15,"enabled":True,"desc":"近一个月涨跌幅，捕捉短期动量","curve":{"points":[[0,0],[15,50],[30,80],[45,100]]},"category":"perf"},
            {"name":"近1年收益","key":"y1","weight":0.09,"enabled":True,"desc":"最近一年的表现，反映基金近期赚钱能力","curve":{"points":[[0,0],[50,50],[100,80],[150,100]]},"category":"perf"},
            {"name":"近一周收益","key":"f5","weight":0.03,"enabled":True,"desc":"近五个交易日涨跌幅，捕捉短期动量","curve":{"points":[[0,0],[5,40],[10,60],[15,80],[20,100]]},"category":"perf"},
            {"name":"近6月收益","key":"sy6","weight":0.06,"enabled":True,"desc":"近六个月表现，补充近1年的中短期维度","curve":{"points":[[0,10],[20,50],[50,75],[100,100]]},"category":"perf"},
            {"name":"近2年收益","key":"sy2","weight":0.05,"enabled":True,"desc":"近两年精确收益，填补中期维度","curve":{"points":[[0,0],[30,20],[60,40],[100,70],[200,100]]},"category":"perf"},
            {"name":"近3年收益","key":"sy3","weight":0.07,"enabled":True,"desc":"从净值数据取级750个交易日精确计算，看穿越牛熊能力","curve":{"points":[[0,0],[50,20],[100,40],[150,70],[300,100]]},"category":"perf"},
            {"name":"年化收益率","key":"annual_return","weight":0.04,"enabled":True,"desc":"基金成立以来年化回报","curve":{"points":[[0,0],[10,20],[20,50],[30,80],[60,100]]},"category":"perf"},
            {"name":"最大回撤","key":"max_dd","weight":0.10,"enabled":True,"desc":"历史最大跌幅","curve":{"points":[[0,90],[16.67,90],[20,86],[50,50],[75,20],[91.67,0]]},"category":"risk"},
            {"name":"波动率","key":"volatility","weight":0.03,"enabled":True,"desc":"年化波动率，衡量基金震荡幅度","curve":{"points":[[10,100],[20,80],[40,40],[60,0]]},"category":"risk"},
            {"name":"最大连跌天数","key":"max_loss_days","weight":0.02,"enabled":True,"desc":"历史最长连续下跌天数","curve":{"points":[[3,100],[7,80],[15,40],[30,0]]},"category":"risk"},
            {"name":"夏普比率","key":"sharpe","weight":0.06,"enabled":True,"desc":"每承受 1 份波动能换来多少额外收益","curve":{"points":[[0,0],[0.5,30],[1,70],[1.5,100]]},"category":"quality"},
            {"name":"索提诺比率","key":"sortino","weight":0.06,"enabled":True,"desc":"只考虑下跌波动，更贴近真实风险感受","curve":{"points":[[0,0],[0.5,20],[1,60],[2,100]]},"category":"quality"},
            {"name":"盈亏比","key":"profit_ratio","weight":0.07,"enabled":True,"desc":"平均盈利÷平均亏损，>1说明赚比亏多","curve":{"points":[[0,0],[1,20],[2,100]]},"category":"quality"},
            {"name":"上行胜率","key":"win_rate","weight":0.07,"enabled":True,"desc":"赚钱天数占总交易天数的比例","curve":{"points":[[30,10],[50,40],[70,100]]},"category":"quality"},
            {"name":"修复系数","key":"recovery","weight":0.04,"enabled":True,"desc":"总收益÷最大回撤，衡量跌下去能不能涨回来","curve":{"points":[[0,0],[5,20],[20,60],[50,100]]},"category":"quality"},
            {"name":"卡玛比率","key":"calmar","weight":0.03,"enabled":True,"desc":"年化收益/最大回撤，衡量收益/风险比","curve":{"points":[[0,0],[0.3,20],[1,60],[3,100]]},"category":"quality"},
            {"name":"费率","key":"rate","weight":0.03,"enabled":True,"desc":"申购费越低越好","curve":{"points":[[0,100],[0.15,80],[0.5,40],[1.5,0]]},"category":"other"},
            {"name":"基金规模","key":"scale","weight":0.02,"enabled":True,"desc":"1~50亿最理想，太小不灵活、太大难操作","curve":{"points":[[0,0],[1,70],[20,100],[50,70],[100,30]]},"category":"other"},
            {"name":"机构持有比例","key":"institutional","weight":0.02,"enabled":True,"desc":"专业机构认可度，小幅参考","curve":{"points":[[5,10],[30,50],[60,90]]},"category":"other"},
        ]
    },
    "进攻型": {
        "dims": [
            {"name":"近1年收益","key":"y1","weight":0.20,"enabled":True,"desc":"最近一年的表现，反映基金近期赚钱能力","curve":{"points":[[0,0],[50,50],[100,80],[150,100]]},"category":"perf"},
            {"name":"近3月收益","key":"m3","weight":0.15,"enabled":True,"desc":"近三个月涨跌幅，中期趋势","curve":{"points":[[0,0],[30,50],[60,80],[90,100]]},"category":"perf"},
            {"name":"夏普比率","key":"sharpe","weight":0.12,"enabled":True,"desc":"每承受 1 份波动能换来多少额外收益","curve":{"points":[[0,0],[0.5,30],[1,70],[1.5,100]]},"category":"quality"},
            {"name":"盈亏比","key":"profit_ratio","weight":0.10,"enabled":True,"desc":"平均盈利÷平均亏损，>1说明赚比亏多","curve":{"points":[[0,0],[1,20],[2,100]]},"category":"quality"},
            {"name":"年化收益率","key":"annual_return","weight":0.10,"enabled":True,"desc":"基金成立以来年化回报","curve":{"points":[[0,0],[10,20],[20,50],[30,80],[60,100]]},"category":"perf"},
            {"name":"近1月收益","key":"m1","weight":0.08,"enabled":True,"desc":"近一个月涨跌幅，捕捉短期动量","curve":{"points":[[0,0],[15,50],[30,80],[45,100]]},"category":"perf"},
            {"name":"近6月收益","key":"sy6","weight":0.06,"enabled":True,"desc":"近六个月表现，补充近1年的中短期维度","curve":{"points":[[0,10],[20,50],[50,75],[100,100]]},"category":"perf"},
            {"name":"近2年收益","key":"sy2","weight":0.05,"enabled":True,"desc":"近两年精确收益，填补中期维度","curve":{"points":[[0,0],[30,20],[60,40],[100,70],[200,100]]},"category":"perf"},
            {"name":"索提诺比率","key":"sortino","weight":0.05,"enabled":True,"desc":"只考虑下跌波动，更贴近真实风险感受","curve":{"points":[[0,0],[0.5,20],[1,60],[2,100]]},"category":"quality"},
            {"name":"基金规模","key":"scale","weight":0.03,"enabled":True,"desc":"1~50亿最理想，太小不灵活、太大难操作","curve":{"points":[[0,0],[1,70],[20,100],[50,70],[100,30]]},"category":"other"},
            {"name":"机构持有比例","key":"institutional","weight":0.03,"enabled":True,"desc":"专业机构认可度，小幅参考","curve":{"points":[[5,10],[30,50],[60,90]]},"category":"other"},
            {"name":"费率","key":"rate","weight":0.03,"enabled":True,"desc":"申购费越低越好","curve":{"points":[[0,100],[0.15,80],[0.5,40],[1.5,0]]},"category":"other"},
        ]
    },
    "防守型": {
        "dims": [
            {"name":"最大回撤","key":"max_dd","weight":0.20,"enabled":True,"desc":"历史最大跌幅","curve":{"points":[[0,90],[16.67,90],[20,86],[50,50],[75,20],[91.67,0]]},"category":"risk"},
            {"name":"波动率","key":"volatility","weight":0.15,"enabled":True,"desc":"年化波动率，衡量基金震荡幅度","curve":{"points":[[10,100],[20,80],[40,40],[60,0]]},"category":"risk"},
            {"name":"卡玛比率","key":"calmar","weight":0.12,"enabled":True,"desc":"年化收益/最大回撤，衡量收益/风险比","curve":{"points":[[0,0],[0.3,20],[1,60],[3,100]]},"category":"quality"},
            {"name":"最大连跌天数","key":"max_loss_days","weight":0.10,"enabled":True,"desc":"历史最长连续下跌天数","curve":{"points":[[3,100],[7,80],[15,40],[30,0]]},"category":"risk"},
            {"name":"索提诺比率","key":"sortino","weight":0.10,"enabled":True,"desc":"只考虑下跌波动，更贴近真实风险感受","curve":{"points":[[0,0],[0.5,20],[1,60],[2,100]]},"category":"quality"},
            {"name":"修复系数","key":"recovery","weight":0.08,"enabled":True,"desc":"总收益÷最大回撤，衡量跌下去能不能涨回来","curve":{"points":[[0,0],[5,20],[20,60],[50,100]]},"category":"quality"},
            {"name":"上行胜率","key":"win_rate","weight":0.06,"enabled":True,"desc":"赚钱天数占总交易天数的比例","curve":{"points":[[30,10],[50,40],[70,100]]},"category":"quality"},
            {"name":"年化收益率","key":"annual_return","weight":0.05,"enabled":True,"desc":"基金成立以来年化回报","curve":{"points":[[0,0],[10,20],[20,50],[30,80],[60,100]]},"category":"perf"},
            {"name":"近1月收益","key":"m1","weight":0.05,"enabled":True,"desc":"近一个月涨跌幅，捕捉短期动量","curve":{"points":[[0,0],[15,50],[30,80],[45,100]]},"category":"perf"},
            {"name":"费率","key":"rate","weight":0.04,"enabled":True,"desc":"申购费越低越好","curve":{"points":[[0,100],[0.15,80],[0.5,40],[1.5,0]]},"category":"other"},
            {"name":"基金规模","key":"scale","weight":0.03,"enabled":True,"desc":"1~50亿最理想，太小不灵活、太大难操作","curve":{"points":[[0,0],[1,70],[20,100],[50,70],[100,30]]},"category":"other"},
            {"name":"机构持有比例","key":"institutional","weight":0.02,"enabled":True,"desc":"专业机构认可度，小幅参考","curve":{"points":[[5,10],[30,50],[60,90]]},"category":"other"},
        ]
    },
    "短炒型": {
        "dims": [
            {"name":"近一周收益","key":"f5","weight":0.25,"enabled":True,"desc":"近五个交易日涨跌幅，捕捉短期动量","curve":{"points":[[0,0],[5,40],[10,60],[15,80],[20,100]]},"category":"perf"},
            {"name":"近1月收益","key":"m1","weight":0.20,"enabled":True,"desc":"近一个月涨跌幅，捕捉短期动量","curve":{"points":[[0,0],[15,50],[30,80],[45,100]]},"category":"perf"},
            {"name":"近3月收益","key":"m3","weight":0.15,"enabled":True,"desc":"近三个月涨跌幅，中期趋势","curve":{"points":[[0,0],[30,50],[60,80],[90,100]]},"category":"perf"},
            {"name":"上行胜率","key":"win_rate","weight":0.10,"enabled":True,"desc":"赚钱天数占总交易天数的比例","curve":{"points":[[30,10],[50,40],[70,100]]},"category":"quality"},
            {"name":"修复系数","key":"recovery","weight":0.08,"enabled":True,"desc":"总收益÷最大回撤，衡量跌下去能不能涨回来","curve":{"points":[[0,0],[5,20],[20,60],[50,100]]},"category":"quality"},
            {"name":"盈亏比","key":"profit_ratio","weight":0.06,"enabled":True,"desc":"平均盈利÷平均亏损，>1说明赚比亏多","curve":{"points":[[0,0],[1,20],[2,100]]},"category":"quality"},
            {"name":"夏普比率","key":"sharpe","weight":0.05,"enabled":True,"desc":"每承受 1 份波动能换来多少额外收益","curve":{"points":[[0,0],[0.5,30],[1,70],[1.5,100]]},"category":"quality"},
            {"name":"最大回撤","key":"max_dd","weight":0.05,"enabled":True,"desc":"历史最大跌幅","curve":{"points":[[0,90],[16.67,90],[20,86],[50,50],[75,20],[91.67,0]]},"category":"risk"},
            {"name":"费率","key":"rate","weight":0.03,"enabled":True,"desc":"申购费越低越好","curve":{"points":[[0,100],[0.15,80],[0.5,40],[1.5,0]]},"category":"other"},
            {"name":"基金规模","key":"scale","weight":0.03,"enabled":True,"desc":"1~50亿最理想，太小不灵活、太大难操作","curve":{"points":[[0,0],[1,70],[20,100],[50,70],[100,30]]},"category":"other"},
        ]
    },
    "📈 短线进攻 A": {
        "dims": [
            {"name":"近一周收益","key":"f5","weight":0.25,"enabled":True,"desc":"近五个交易日涨跌幅，捕捉短期动量","curve":{"points":[[0,0],[5,40],[10,60],[15,80],[20,100]]},"category":"perf"},
            {"name":"近1月收益","key":"m1","weight":0.20,"enabled":True,"desc":"近一个月涨跌幅，捕捉短期动量","curve":{"points":[[0,0],[15,50],[30,80],[45,100]]},"category":"perf"},
            {"name":"当日涨跌","key":"td","weight":0.15,"enabled":True,"desc":"当日实时涨跌幅，捕捉盘中动量","curve":{"points":[[-5,0],[-2,40],[0,60],[2,80],[5,100]]},"category":"perf"},
            {"name":"近3月收益","key":"m3","weight":0.15,"enabled":True,"desc":"近三个月涨跌幅，中期趋势","curve":{"points":[[0,0],[30,50],[60,80],[90,100]]},"category":"perf"},
            {"name":"上行胜率","key":"win_rate","weight":0.10,"enabled":True,"desc":"赚钱天数占总交易天数的比例","curve":{"points":[[30,10],[50,40],[70,100]]},"category":"quality"},
            {"name":"盈亏比","key":"profit_ratio","weight":0.10,"enabled":True,"desc":"平均盈利÷平均亏损，>1说明赚比亏多","curve":{"points":[[0,0],[1,20],[2,100]]},"category":"quality"},
            {"name":"近6月收益","key":"sy6","weight":0.05,"enabled":True,"desc":"近六个月表现，补充中短期维度","curve":{"points":[[0,10],[20,50],[50,75],[100,100]]},"category":"perf"},
        ]
    },
    "🛡️ 短线平衡 B": {
        "dims": [
            {"name":"近1月收益","key":"m1","weight":0.20,"enabled":True,"desc":"近一个月涨跌幅，捕捉短期动量","curve":{"points":[[0,0],[15,50],[30,80],[45,100]]},"category":"perf"},
            {"name":"近一周收益","key":"f5","weight":0.15,"enabled":True,"desc":"近五个交易日涨跌幅，捕捉短期动量","curve":{"points":[[0,0],[5,40],[10,60],[15,80],[20,100]]},"category":"perf"},
            {"name":"近3月收益","key":"m3","weight":0.15,"enabled":True,"desc":"近三个月涨跌幅，中期趋势","curve":{"points":[[0,0],[30,50],[60,80],[90,100]]},"category":"perf"},
            {"name":"当日涨跌","key":"td","weight":0.10,"enabled":True,"desc":"当日实时涨跌幅，捕捉盘中动量","curve":{"points":[[-5,0],[-2,40],[0,60],[2,80],[5,100]]},"category":"perf"},
            {"name":"近6月收益","key":"sy6","weight":0.10,"enabled":True,"desc":"近六个月表现，补充中短期维度","curve":{"points":[[0,10],[20,50],[50,75],[100,100]]},"category":"perf"},
            {"name":"上行胜率","key":"win_rate","weight":0.10,"enabled":True,"desc":"赚钱天数占总交易天数的比例","curve":{"points":[[30,10],[50,40],[70,100]]},"category":"quality"},
            {"name":"盈亏比","key":"profit_ratio","weight":0.10,"enabled":True,"desc":"平均盈利÷平均亏损，>1说明赚比亏多","curve":{"points":[[0,0],[1,20],[2,100]]},"category":"quality"},
            {"name":"最大回撤","key":"max_dd","weight":0.05,"enabled":True,"desc":"历史最大跌幅","curve":{"points":[[0,90],[16.67,90],[20,86],[50,50],[75,20],[91.67,0]]},"category":"risk"},
            {"name":"基金规模","key":"scale","weight":0.05,"enabled":True,"desc":"1~50亿最理想，太小不灵活、太大难操作","curve":{"points":[[0,0],[1,70],[20,100],[50,70],[100,30]]},"category":"other"},
        ]
    },
    "⚡ 激进理性 C": {
        "dims": [
            {"name":"近1月收益","key":"m1","weight":0.20,"enabled":True,"desc":"近一个月涨跌幅，捕捉短期动量","curve":{"points":[[0,0],[15,50],[30,80],[45,100]]},"category":"perf"},
            {"name":"近一周收益","key":"f5","weight":0.15,"enabled":True,"desc":"近五个交易日涨跌幅，捕捉短期动量","curve":{"points":[[0,0],[5,40],[10,60],[15,80],[20,100]]},"category":"perf"},
            {"name":"近3月收益","key":"m3","weight":0.15,"enabled":True,"desc":"近三个月涨跌幅，中期趋势","curve":{"points":[[0,0],[30,50],[60,80],[90,100]]},"category":"perf"},
            {"name":"盈亏比","key":"profit_ratio","weight":0.15,"enabled":True,"desc":"平均盈利÷平均亏损，>1说明赚比亏多","curve":{"points":[[0,0],[1,20],[2,100]]},"category":"quality"},
            {"name":"当日涨跌","key":"td","weight":0.10,"enabled":True,"desc":"当日实时涨跌幅，捕捉盘中动量","curve":{"points":[[-5,0],[-2,40],[0,60],[2,80],[5,100]]},"category":"perf"},
            {"name":"最大回撤","key":"max_dd","weight":0.10,"enabled":True,"desc":"历史最大跌幅","curve":{"points":[[0,90],[16.67,90],[20,86],[50,50],[75,20],[91.67,0]]},"category":"risk"},
            {"name":"上行胜率","key":"win_rate","weight":0.10,"enabled":True,"desc":"赚钱天数占总交易天数的比例","curve":{"points":[[30,10],[50,40],[70,100]]},"category":"quality"},
            {"name":"近6月收益","key":"sy6","weight":0.05,"enabled":True,"desc":"近六个月表现，补充中短期维度","curve":{"points":[[0,10],[20,50],[50,75],[100,100]]},"category":"perf"},
        ]
    },
}


_HLD_DEFAULT_DIMS = [
    {"key":"roe","name":"ROE","w":10,"curve":[[0,0],[5,20],[10,40],[15,60],[20,80],[30,100]],"locked":True},
    {"key":"main_biz_margin","name":"主营利润率","w":8,"curve":[[0,0],[10,10],[20,30],[40,60],[60,80],[80,100]]},
    {"key":"net_profit_margin","name":"销售净利率","w":6,"curve":[[0,0],[3,15],[8,40],[15,65],[25,85],[40,100]],"locked":True},
    {"key":"cost_profit_margin","name":"成本费用利润率","w":4,"curve":[[0,0],[20,15],[50,35],[100,60],[150,80],[300,100]]},
    {"key":"debt_ratio","name":"资产负债率","w":10,"curve":[[0,90],[20,85],[40,70],[60,50],[80,20],[95,0]],"locked":True},
    {"key":"rev_growth","name":"营收增长","w":6,"curve":[[-20,0],[0,30],[10,50],[30,75],[60,90],[100,100]]},
    {"key":"net_profit_growth","name":"净利润增长率","w":6,"curve":[[-50,0],[-20,15],[0,35],[15,60],[30,80],[60,100]]},
    {"key":"quick_ratio","name":"速动比率","w":6,"curve":[[0,0],[0.3,20],[0.6,40],[1,65],[1.5,85],[3,100]],"locked":True},
    {"key":"cf_ps","name":"每股现金流","w":3,"curve":[[-2,0],[0,25],[0.5,50],[1,70],[3,90],[5,100]]},
    {"key":"cashflow_to_profit","name":"现金流/净利润","w":4,"curve":[[-2,0],[0,10],[0.3,30],[0.5,50],[1,70],[2,85],[5,100]]},
    {"key":"mdd_1y","name":"1年回撤","w":8,"curve":[[0,100],[10,85],[20,65],[30,40],[50,15],[70,0]],"locked":True},
    {"key":"ret_1m","name":"近1月收益","w":5,"curve":[[-30,0],[-10,15],[0,35],[10,65],[20,85],[30,100]]},
    {"key":"ret_3m","name":"近3月收益","w":4,"curve":[[-40,0],[-15,15],[0,35],[15,60],[30,80],[60,100]]},
    {"key":"ret_1y","name":"近1年收益","w":6,"curve":[[-50,0],[-20,15],[0,35],[20,60],[50,85],[100,100]]},
    {"key":"pe","name":"PE","w":4,"curve":[[0,90],[10,70],[20,50],[40,30],[60,15],[80,5]],"locked":True},
    {"key":"pb","name":"市净率PB","w":4,"curve":[[0,90],[1,70],[3,50],[5,35],[10,20],[20,5]]},
    {"key":"wk_position","name":"52周位置","w":2,"curve":[[0,90],[20,70],[50,50],[70,30],[90,15],[100,5]]},
    {"key":"roa","name":"总资产利润率","w":5,"curve":[[0,0],[2,10],[5,30],[10,60],[15,80],[20,100]]},
]

_HLD_DIMS_CACHE: list | None = None

def _load_hld_dims() -> list:
    global _HLD_DIMS_CACHE
    if _HLD_DIMS_CACHE is not None:
        return _HLD_DIMS_CACHE
    # 直接从文件读取（绕过可能过时的 CFG 缓存）
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as _f:
            _cfg = json.load(_f)
        raw = _cfg.get("holdings_scoring", {}).get("dims")
        if raw:
            # 为已保存的配置补充 locked 标记
            _locked_map = {"roe":1,"debt_ratio":1,"quick_ratio":1,"mdd_1y":1,"pe":1,"net_profit_margin":1}
            for d in raw:
                if d.get("key") in _locked_map:
                    d["locked"] = True
            _HLD_DIMS_CACHE = raw
            return _HLD_DIMS_CACHE
    except Exception:
        pass
    _HLD_DIMS_CACHE = _HLD_DEFAULT_DIMS
    return _HLD_DIMS_CACHE


def _hld_score(val, curve):
    if val is None or not curve or len(curve) < 2:
        return 0.0
    xs = [p[0] for p in curve]; ys = [p[1] for p in curve]
    if val <= xs[0]: return max(0.0, min(100.0, ys[0]))
    if val >= xs[-1]: return max(0.0, min(100.0, ys[-1]))
    for i in range(len(xs)-1):
        if xs[i] <= val <= xs[i+1]:
            if xs[i+1]==xs[i]: return float(ys[i])
            r = (val-xs[i])/(xs[i+1]-xs[i])
            return max(0.0, min(100.0, ys[i] + (ys[i+1]-ys[i])*r))
    return 0.0


def _init_builtin_presets(cfg: dict) -> None:
    """首次初始化内置预设"""
    cfg["scoring_presets"] = {}
    for name, data in _BUILTIN_PRESETS.items():
        cfg["scoring_presets"][name] = data


def _background_refresh_recommend_cache():
    """后台线程：每 60 秒刷新一次推荐表缓存，确保始终温暖。"""
    while True:
        time.sleep(60)
        try:
            from fund_render import _web_rich_recommend_table, _load_saved_recommend_data
            _saved = _load_saved_recommend_data()
            if _saved:
                _rec_f = os.path.join(_PROJECT_ROOT, ".fund_recommend_result.json")
                _rec_mtime = os.path.getmtime(_rec_f) if os.path.exists(_rec_f) else 0
                _recommend_table_cache["data"] = {"html": _web_rich_recommend_table(_saved), "mtime": _rec_mtime, "time": time.time()}
        except Exception:
            pass


def main():
    setup_log("server.log")
    # 启动时清理上次残留的心跳，避免前端读到旧进度
    for _hb_name in ["fund_recommend", "fund_watch", "fund_monitor", "fund_briefing"]:
        clear_heartbeat(_hb_name)
    # 自动启动盘中监控
    _spawn_task("fund_monitor")
    # 后台线程刷新推荐表缓存
    threading.Thread(target=_background_refresh_recommend_cache, daemon=True).start()
    host = get_config("server", "host", default="0.0.0.0")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else _PORT
    server = http.server.ThreadingHTTPServer((host, port), Handler)
    print(f"🌐 基金优选页面：http://{host}:{port}")
    print("   按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()