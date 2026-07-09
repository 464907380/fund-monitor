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
import urllib.parse
import concurrent.futures
import urllib.request

# 同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fund_utils import read_all_heartbeats, is_heartbeat_alive, write_heartbeat, update_heartbeat, clear_heartbeat, HISTORY_DIR, setup_log
from config import CFG, api_url, get_timeout, get_config

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 后台任务管理 ──
_recommend_proc: subprocess.Popen | None = None
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
    global _recommend_proc
    script = os.path.join(_SCRIPT_DIR, "fund_recommend.py")
    try:
        proc = subprocess.Popen(
            [sys.executable, script],
            cwd=_SCRIPT_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        # 立即写心跳，前端立刻就能看到进度
        write_heartbeat("fund_recommend", progress=0, total=0, status="启动中")
        with _proc_lock:
            _recommend_proc = proc

        def _wait_and_cleanup(p=proc) -> None:
            p.wait()
            clear_heartbeat("fund_recommend")
            with _proc_lock:
                global _recommend_proc
                if _recommend_proc is p:
                    _recommend_proc = None

        threading.Thread(target=_wait_and_cleanup, daemon=True).start()
        return True
    except Exception as e:
        print(f"[ERROR] 推荐启动异常: {e}", flush=True)
        clear_heartbeat("fund_recommend")
        return False





_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# ── fund-table 缓存 ──
_fund_table_cache: tuple[float, str] | None = None
_FUND_TABLE_CACHE_TTL = get_config("server", "fund_table_cache_ttl", default=300)  # 秒（5分钟）
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
    with open(_FUND_LIST_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


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
        def _validate_td(val):
            if val is None: return None
            if isinstance(val, (int, float)) and abs(val) > 10: return None
            return val

        for r in results:
            score_d = {k: _validate_td(r.get(k)) if k == "td" else r.get(k) for k in score_keys}
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
                from fund_watch import _parse_holdings
                from fund_utils import fetch
                holds = _parse_holdings(code) or []
                # 用腾讯财经接口批量获取实时涨跌（速度快，不需要Referer）
                if holds:
                    codes_str = ",".join(
                        (h.get("m", "sz") + h["c"])
                        for h in holds
                    )
                    try:
                        raw = fetch(api_url("tencent_realtime", code=codes_str))
                        for line in raw.strip().split(";"):
                            if not line:
                                continue
                            parts = line.split("~")
                            if len(parts) > 32:
                                code_from_resp = parts[2] if len(parts) > 2 else ""
                                price = float(parts[3]) if parts[3] else 0
                                prev_close = float(parts[4]) if parts[4] else 0
                                chg = round((price - prev_close) / prev_close * 100, 2) if prev_close else None
                                for h in holds:
                                    if h["c"] == code_from_resp:
                                        h["chg"] = chg
                                        break
                    except Exception:
                        pass
                self._send(*_json_response({"ok": True, "code": code, "holdings": holds}))
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
                    self._send(*_json_response({"ok": True, "date": data.get("date", ""), "results": data.get("results", [])}))
                except Exception as e:
                    self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            else:
                self._send(*_json_response({"ok": True, "date": "", "results": []}))
            return

        if parsed.path == "/api/fund-table":
            """为自选基金生成完整数据富表格（含评分）—— 并行拉取数据"""
            # 短缓存：30秒内不重复拉取
            global _fund_table_cache
            now = time.time()
            if _fund_table_cache and now - _fund_table_cache[0] < _FUND_TABLE_CACHE_TTL:
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
                    try:
                        cached = _rec_cache.get(code)
                        if cached:
                            # ── 推荐缓存命中：直接用缓存数据，不另行拉取 ──
                            _td = cached.get("td")
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
                            # 净值走势（从缓存取）
                            _trend = cached.get("_trend")
                            if _trend and len(_trend) >= 2:
                                row["_trend"] = _trend
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
                            row["_trend"] = [round(n["v"], 4) for n in navs[-60:]]
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
                with open(_CONFIG_PATH, "w", encoding="utf-8") as _fwcfg2:
                    json.dump(cfg, _fwcfg2, indent=2, ensure_ascii=False)
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
                    dim["curve"] = {"points": curve}

                with open(_CONFIG_PATH, "w", encoding="utf-8") as _fw:
                    json.dump(cfg, _fw, indent=2, ensure_ascii=False)
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
                with open(_CONFIG_PATH, "w", encoding="utf-8") as _fwmc:
                    json.dump(cfg, _fwmc, indent=2, ensure_ascii=False)
                self._send(*_json_response({"ok": True, "message": "监控配置已更新"}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/recommend-config":
            try:
                with open(_CONFIG_PATH, encoding="utf-8") as _fcfg:
                    cfg = json.load(_fcfg)
                cfg["recommend"] = {
                    "top_n": int(body.get("top_n", 200)),
                    "filter_conditions": body.get("filter_conditions", []),
                    "show_top": int(body.get("show_top", 20)),
                    "skip_missing_perf": bool(body.get("skip_missing_perf", False)),
                    "skip_limited": bool(body.get("skip_limited", False)),
                    "rank_sort": str(body.get("rank_sort", "1n")),
                }
                with open(_CONFIG_PATH, "w", encoding="utf-8") as _fwcfg:
                    json.dump(cfg, _fwcfg, indent=2, ensure_ascii=False)
                # 重载 config 再重载 fund_render，让 _show_top 读到新值
                self._send(*_json_response({"ok": True, "message": "推荐配置已更新"}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/recommend":
            try:
                with _proc_lock:
                    if _recommend_proc and _recommend_proc.poll() is None:
                        self._send(*_json_response({"ok": False, "error": "推荐任务正在运行中"}))
                        return
                if _spawn_recommend():
                    self._send(*_json_response({"ok": True, "message": "推荐任务已启动，约需 16 分钟"}))
                else:
                    self._send(*_json_response({"ok": False, "error": "推荐任务启动失败"}, 500))
            except Exception as e:
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
                with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2, ensure_ascii=False)
                current = cfg.get("scoring", {}).get("current_preset", "系统默认")
                self._send(*_json_response({"ok": True, "presets": presets, "current": current}))
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
    host = "127.0.0.1"
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