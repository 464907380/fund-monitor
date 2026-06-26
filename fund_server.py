"""
基金管理 — 本地 HTTP 服务器
提供交互式网页 + API，用于增删监控基金。
"""
import json
import os
import re
import sys
import subprocess
import http.server
import threading
import urllib.parse
import concurrent.futures
import urllib.request

# 同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fund_utils import read_all_heartbeats, is_heartbeat_alive, write_heartbeat, clear_heartbeat, HISTORY_DIR
from config import CFG
from config import api_url

# ── 后台任务管理 ──
_recommend_proc: subprocess.Popen | None = None
_briefing_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()

# 通用任务进程跟踪（供启停控制使用）
_task_procs: dict[str, subprocess.Popen] = {}
_task_scripts = {
    "global_briefing": "global_briefing.py",
    "fund_watch": "fund_watch.py",
    "fund_monitor": "fund_monitor.py",
}
_task_heartbeats = {
    "global_briefing": "global_briefing",
    "fund_watch": "fund_briefing",
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
        # 确认进程已成功启动且仍在运行，再写心跳
        try:
            proc.wait(timeout=3)
            # 进程在3秒内退出了（很可能是启动失败）
            return False
        except subprocess.TimeoutExpired:
            pass  # 进程还在运行，正常
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
            stderr=subprocess.DEVNULL,
        )
        # 确认进程已成功启动且仍在运行，再写心跳
        try:
            proc.wait(timeout=3)
            return False  # 进程在3秒内退出=启动失败
        except subprocess.TimeoutExpired:
            pass  # 进程还在运行，正常
        write_heartbeat("fund_recommend")
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
    except Exception:
        clear_heartbeat("fund_recommend")
        return False


def _spawn_briefing() -> bool:
    """启动晚报生成，完成后自动清理心跳，返回是否成功"""
    global _briefing_proc
    script = os.path.join(_SCRIPT_DIR, "fund_watch.py")
    try:
        proc = subprocess.Popen(
            [sys.executable, script],
            cwd=_SCRIPT_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # 确认进程已成功启动且仍在运行，再写心跳
        try:
            proc.wait(timeout=3)
            return False  # 进程在3秒内退出=启动失败
        except subprocess.TimeoutExpired:
            pass  # 进程还在运行，正常
        write_heartbeat("fund_briefing")
        with _proc_lock:
            _briefing_proc = proc

        def _wait_and_cleanup(p=proc) -> None:
            p.wait()
            clear_heartbeat("fund_briefing")

        threading.Thread(target=_wait_and_cleanup, daemon=True).start()
        return True
    except Exception:
        clear_heartbeat("fund_briefing")
        return False


_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_FUND_LIST_PATH = os.path.join(_SCRIPT_DIR, "fund_list.json")
_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "config.json")
_PORT = 8080


def _fetch_fund_name(code: str) -> str:
    """从天天基金获取基金名称"""
    try:
        url = api_url("fund_pingzhongdata", code=code)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read().decode("utf-8")
        m = re.search(r'var fS_name\s*=\s*"([^"]+)"', data)
        return m.group(1) if m else ""
    except Exception:
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
        with urllib.request.urlopen(req, timeout=15) as r:
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
    import subprocess
    # 先尝试 Windows schtasks
    try:
        r = subprocess.run(
            ["schtasks", "/query", "/tn", taskname, "/fo", "LIST", "/v"],
            capture_output=True, text=True, timeout=10
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
    {"id": "global_briefing", "taskname": "全球股市简报", "icon": "🌏", "label": "全球股市简报",
     "desc": "A 股：上证指数 · 深证成指 · 创业板指 · 沪深300 · 成交额 · 涨跌家数 | 全球：道琼斯 · 纳斯达克 · 标普500 · 恒生指数 · 日经225 · 韩国KOSPI · 英国富时100 · 德国DAX · 法国CAC40 · 瑞士SMI",
     "time": "交易日 09:30"},
    {"id": "fund_watch", "taskname": "基金晚报", "icon": "📊", "label": "基金晚报",
     "desc": "每只监控基金：当日涨跌 · 近5日 · 近1月/3月/1年收益 | 警报：经理变更 · 规模翻倍 · 净值停滞 · 连跌趋势 · 分红除权 | 附：市场优选基金 TOP 10 排行（12 维评分）",
     "time": "交易日 15:30"},
    {"id": "fund_monitor", "taskname": "基金盘中监控", "icon": "🔔", "label": "盘中监控",
     "desc": "交易日 9:30–15:00 每 10 分钟轮询 | 基金实时估算涨跌幅 · 基金前 5 大重仓个股实时涨跌 | 双重警报：单次急涨急跌 + 当日累计涨跌（红/黄双阈值）| 节假日自动检测 · 进程崩溃恢复",
     "time": "交易日 9:25 启动"},
]


class Handler(http.server.BaseHTTPRequestHandler):

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
                brief_path = os.path.join(_SCRIPT_DIR, ".briefing_fund.html")
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
                        "min_y1_return": rc.get("min_y1_return", 20),
                        "show_top": rc.get("show_top", 20),
                        "skip_missing_perf": rc.get("skip_missing_perf", False),
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
            """返回市场优选全维度表格 HTML（从缓存文件读取）"""
            try:
                from fund_render import _web_rich_recommend_table, _load_saved_recommend_data
                from fund_watch import _parse_real_time
                _saved = _load_saved_recommend_data()
                if _saved:
                    # 单独补充实时涨跌（轻量请求）
                    for entry in _saved:
                        try:
                            td = _parse_real_time(entry.get("code", ""))
                            entry["day"] = f"{td:+.2f}%" if td is not None else ""
                        except Exception:
                            entry["day"] = entry.get("day", "")
                    html = _web_rich_recommend_table(_saved)
                else:
                    html = ""
                if html:
                    self._send(200, {"Content-Type": "text/html; charset=utf-8"}, html.encode("utf-8"))
                else:
                    self._send(200, {"Content-Type": "text/html; charset=utf-8"}, "<p style=\"color:#888;\">暂无推荐数据</p>".encode("utf-8"))
            except Exception as e:
                self._send(500, {"Content-Type": "text/html; charset=utf-8"},
                           f"<p style=\"color:#ef5350;\">获取推荐表格失败: {e}</p>".encode("utf-8"))
            return

        if parsed.path == "/api/briefing":
            path = os.path.join(HISTORY_DIR, ".briefing_fund.html")
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        html = f.read()
                    mtime = os.path.getmtime(path)
                    self._send(200, {"Content-Type": "text/html; charset=utf-8", "X-Last-Modified": str(mtime)}, html.encode("utf-8"))
                except Exception as e:
                    body = ("<html><body style='background:#1a1a1a;color:#666;padding:40px;text-align:center;font-family:sans-serif;'>"
                            "<p>读取晚报失败</p></body></html>")
                    self._send(200, {"Content-Type": "text/html; charset=utf-8"}, body.encode("utf-8"))
            else:
                body = ("<html><body style='background:#1a1a1a;color:#666;padding:40px;text-align:center;font-family:sans-serif;'>"
                        "<p>\u2622\ufe0f 晚报尚未生成</p>"
                        "<p style='font-size:12px;color:#555;'>等待 15:30 定时任务运行</p></body></html>")
                self._send(200, {"Content-Type": "text/html; charset=utf-8"}, body.encode("utf-8"))
            return

        if parsed.path == "/api/recommend":
            path = os.path.join(_SCRIPT_DIR, ".fund_recommend_result.json")
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
            try:
                from fund_render import _web_rich_fund_table
                from fund_watch import get
                from fund_scoring import calc_score_detail
                # 直接从文件读取基金列表（不使用缓存，因为页面可能刚增删过）
                fl_path = os.path.join(_SCRIPT_DIR, "fund_list.json")
                if os.path.exists(fl_path):
                    with open(fl_path, encoding="utf-8") as _f:
                        fund_list = json.load(_f)
                else:
                    fund_list = []
                rows: list[dict] = []

                def _process_one(code: str) -> dict | None:
                    """拉取一只基金数据并计算评分"""
                    try:
                        d = get(code)
                        if not d.get("n"):
                            return None
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
                        }
                        score_d = {k: d.get(k) for k in (
                            "y1","m3","m1","f5","sy6","sy2","sy3",
                            "annual_return","sharpe","sortino",
                            "profit_ratio","win_rate","recovery","calmar",
                            "max_dd","volatility","max_loss_days",
                            "sc","rate","inst",
                        )}
                        score, details, skipped = calc_score_detail(score_d)
                        row["score"] = score
                        row["_score_detail"] = details
                        row["_skipped_weight"] = skipped
                        return row
                    except Exception:
                        return None

                # 并行拉取所有基金数据（网络IO密集，20线程足够）
                with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                    fut_map = {executor.submit(_process_one, f["code"]): f["code"] for f in fund_list}
                    for fut in concurrent.futures.as_completed(fut_map):
                        result = fut.result()
                        if result is not None:
                            rows.append(result)

                # 按 fund_list 原始顺序排序
                order = {f["code"]: i for i, f in enumerate(fund_list)}
                rows.sort(key=lambda r: order.get(r["code"], 999))

                html = _web_rich_fund_table(rows)
                self._send(200, {"Content-Type": "text/html; charset=utf-8"}, html.encode("utf-8"))
            except Exception as e:
                self._send(500, {"Content-Type": "text/html; charset=utf-8"},
                           f"<p style=\"color:#ef5350;\">获取基金表格失败: {e}</p>".encode("utf-8"))
            return

        if parsed.path == "/" or parsed.path == "/index.html":
            self._send_file("fund_manage.html")
            return

        # 尝试提供静态文件（JS/CSS）
        self._send_file(parsed.path.lstrip("/"))

    def _send_file(self, filename: str):
        # 路径穿越防护：确保请求的文件在项目目录内
        path = os.path.normpath(os.path.join(_SCRIPT_DIR, filename))
        if not path.startswith(os.path.normpath(_SCRIPT_DIR) + os.sep) and path != os.path.normpath(_SCRIPT_DIR):
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
                import fund_render
                importlib.reload(fund_scoring)
                importlib.reload(fund_render)
                self._send(*_json_response({"ok": True, "message": "评分配置已更新"}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/dims/calibrate":
            """基于推荐数据的百分位自动校准评分曲线"""
            try:
                rec_path = os.path.join(_SCRIPT_DIR, ".fund_recommend_result.json")
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
                codes = [(r["code"], r.get("name", "")) for r in rec_data]
                if len(codes) > 1000:
                    codes = codes[:1000]

                # "越低越好"的维度
                lower_better = {"波动率", "最大回撤", "最大连跌天数", "费率"}
                # 需要解析百分号字符串的字段
                pct_keys = {"f5"}

                # 并行拉取实时数据
                from fund_watch import get_scoring_data
                all_data: list[dict] = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                    _fut_map = {executor.submit(get_scoring_data, code): code for code, _ in codes}
                    for _fut in concurrent.futures.as_completed(_fut_map):
                        _d = _fut.result()
                        if _d and _d.get("n"):
                            all_data.append(_d)

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
                    "min_y1_return": int(body.get("min_y1_return", 20)),
                    "show_top": int(body.get("show_top", 20)),
                    "skip_missing_perf": bool(body.get("skip_missing_perf", False)),
                }
                with open(_CONFIG_PATH, "w", encoding="utf-8") as _fwcfg:
                    json.dump(cfg, _fwcfg, indent=2, ensure_ascii=False)
                # 重载 config 再重载 fund_render，让 _show_top 读到新值
                import importlib, config, fund_render
                importlib.reload(config)
                importlib.reload(fund_render)
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

        if self.path == "/api/briefing":
            try:
                with _proc_lock:
                    if _briefing_proc and _briefing_proc.poll() is None:
                        self._send(*_json_response({"ok": False, "error": "晚报生成任务正在运行中"}))
                        return
                if _spawn_briefing():
                    self._send(*_json_response({"ok": True, "message": "晚报生成已启动，约需 2 分钟"}))
                else:
                    self._send(*_json_response({"ok": False, "error": "晚报生成启动失败"}, 500))
            except Exception as e:
                clear_heartbeat("fund_briefing")
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
}


def _init_builtin_presets(cfg: dict) -> None:
    """首次初始化内置预设"""
    cfg["scoring_presets"] = {}
    for name, data in _BUILTIN_PRESETS.items():
        cfg["scoring_presets"][name] = data


def main():
    host = "127.0.0.1"
    port = int(sys.argv[1]) if len(sys.argv) > 1 else _PORT
    server = http.server.ThreadingHTTPServer((host, port), Handler)
    print(f"🌐 基金管理页面：http://{host}:{port}")
    print("   按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()