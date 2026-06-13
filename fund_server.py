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
import urllib.request

# 同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fund_utils import read_all_heartbeats, is_heartbeat_alive, write_heartbeat, clear_heartbeat, HISTORY_DIR
from config import CFG
from config import api_url

# ── 后台任务管理 ──
_recommend_proc: subprocess.Popen | None = None
_briefing_proc: subprocess.Popen | None = None
"""晚报子进程引用"""


def _spawn_recommend() -> None:
    """启动推荐任务，完成后自动清理心跳"""
    global _recommend_proc
    script = os.path.join(_SCRIPT_DIR, "fund_recommend.py")
    write_heartbeat("fund_recommend")
    _recommend_proc = subprocess.Popen(
        [sys.executable, script],
        cwd=_SCRIPT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def _wait_and_cleanup() -> None:
        _recommend_proc.wait()
        clear_heartbeat("fund_recommend")

    threading.Thread(target=_wait_and_cleanup, daemon=True).start()


def _spawn_briefing() -> None:
    """启动晚报生成，完成后自动清理心跳"""
    global _briefing_proc
    script = os.path.join(_SCRIPT_DIR, "fund_watch.py")
    write_heartbeat("fund_briefing")
    _briefing_proc = subprocess.Popen(
        [sys.executable, script],
        cwd=_SCRIPT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def _wait_and_cleanup() -> None:
        _briefing_proc.wait()
        clear_heartbeat("fund_briefing")

    threading.Thread(target=_wait_and_cleanup, daemon=True).start()


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
    """查询 Windows 计划任务状态"""
    import subprocess
    try:
        r = subprocess.run(
            ["schtasks", "/query", "/tn", taskname, "/fo", "LIST", "/v"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            return {"status": "未找到"}
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
        result["ok"] = (result["last_result"] == "0") if result["last_result"] is not None else None  # type: ignore[assignment]
        return result
    except Exception as e:
        return {"status": f"查询失败: {e}"}


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
            funds = _load()
            self._send(*_json_response({"ok": True, "funds": funds}))
            return

        if parsed.path == "/api/search":
            q = params.get("q", [""])[0]
            results = _search_funds(q)
            self._send(*_json_response({"ok": True, "results": results}))
            return

        if parsed.path == "/api/tasks":
            tasks = []
            for t in TASK_DEFS:
                info = _check_task_status(t["taskname"])
                running = is_heartbeat_alive(t["id"], 1800)
                tasks.append({**t, **info, "running": running})
            self._send(*_json_response({"ok": True, "tasks": tasks}))
            return

        if parsed.path == "/api/heartbeat":
            hb = read_all_heartbeats()
            # 附加 alive 状态（含超时判断），供前端使用
            alive = {k: is_heartbeat_alive(k, 1800) for k in hb}
            self._send(*_json_response({"ok": True, "heartbeats": hb, "alive": alive}))
            return

        if parsed.path == "/api/dims":
            try:
                dims = json.load(open(_CONFIG_PATH, encoding="utf-8")).get("scoring", {}).get("dims", [])
                self._send(*_json_response({"ok": True, "dims": dims}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if parsed.path == "/api/briefing":
            path = os.path.join(HISTORY_DIR, ".briefing_fund.html")
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        html = f.read()
                    self._send(200, {"Content-Type": "text/html; charset=utf-8"}, html.encode("utf-8"))
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
        with open(path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(filename)[1]
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(ext, "application/octet-stream")
        self._send(200, {"Content-Type": ctype}, data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        body = {}
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                self._send(*_json_response({"ok": False, "error": "JSON 格式错误"}, 400))
                return

        if self.path == "/api/add":
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
            return

        if self.path == "/api/remove":
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
            return

        if self.path == "/api/dims":
            try:
                dims = body.get("dims", [])
                if not dims:
                    self._send(*_json_response({"ok": False, "error": "dims 不能为空"}, 400))
                    return
                cfg = json.load(open(_CONFIG_PATH, encoding="utf-8"))
                if "scoring" not in cfg:
                    cfg["scoring"] = {}
                cfg["scoring"]["dims"] = dims
                json.dump(cfg, open(_CONFIG_PATH, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
                # 重新加载评分模块使新配置生效
                import importlib
                import fund_scoring
                importlib.reload(fund_scoring)
                self._send(*_json_response({"ok": True, "message": "评分配置已更新"}))
            except Exception as e:
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/recommend":
            try:
                if _recommend_proc and _recommend_proc.poll() is None:
                    self._send(*_json_response({"ok": False, "error": "推荐任务正在运行中"}))
                    return
                _spawn_recommend()
                self._send(*_json_response({"ok": True, "message": "推荐任务已启动，约需 16 分钟"}))
            except Exception as e:
                clear_heartbeat("fund_recommend")
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        if self.path == "/api/briefing":
            try:
                if _briefing_proc and _briefing_proc.poll() is None:
                    self._send(*_json_response({"ok": False, "error": "晚报生成任务正在运行中"}))
                    return
                _spawn_briefing()
                self._send(*_json_response({"ok": True, "message": "晚报生成已启动，约需 2 分钟"}))
            except Exception as e:
                clear_heartbeat("fund_briefing")
                self._send(*_json_response({"ok": False, "error": str(e)}, 500))
            return

        self._send(*_json_response({"ok": False, "error": "未知接口"}, 404))


def main():
    host = "127.0.0.1"
    port = int(sys.argv[1]) if len(sys.argv) > 1 else _PORT
    server = http.server.HTTPServer((host, port), Handler)
    print(f"🌐 基金管理页面：http://{host}:{port}")
    print("   按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
