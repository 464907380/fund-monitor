"""
基金管理 — 本地 HTTP 服务器
提供交互式网页 + API，用于增删监控基金。
"""
import json
import os
import re
import sys
import http.server
import urllib.parse
import urllib.request

# 同目录模块
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fund_utils import read_all_heartbeats

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_FUND_LIST_PATH = os.path.join(_SCRIPT_DIR, "fund_list.json")
_PORT = 8080


def _fetch_fund_name(code: str) -> str:
    """从天天基金获取基金名称"""
    try:
        url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
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
        url = "https://fund.eastmoney.com/js/fundcode_search.js"
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
    results = []
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
                return json.load(f)
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
        result["ok"] = result["last_result"] == "0" if result["last_result"] else None
        # 如果上次运行不是今天，状态算"未知"而非"成功"
        if result["last_run"]:
            import datetime as _dt
            try:
                last_dt = _dt.datetime.strptime(result["last_run"][:10].strip(), "%Y/%m/%d")
                if last_dt.date() != _dt.date.today():
                    result["ok"] = None  # 不是今天的运行结果，不算数
            except ValueError:
                pass
        return result
    except Exception as e:
        return {"status": f"查询失败: {e}"}


TASK_DEFS = [
    {"id": "briefing", "taskname": "全球股市简报", "icon": "🌏", "label": "全球股市简报",
     "desc": "A 股：上证指数 · 深证成指 · 创业板指 · 沪深300 · 成交额 · 涨跌家数 | 全球：道琼斯 · 纳斯达克 · 标普500 · 恒生指数 · 日经225 · 韩国KOSPI · 英国富时100 · 德国DAX · 法国CAC40 · 瑞士SMI",
     "time": "交易日 09:30"},
    {"id": "watch", "taskname": "基金晚报", "icon": "📊", "label": "基金晚报",
     "desc": "每只监控基金：当日涨跌 · 近5日 · 近1月/3月/1年收益 | 警报：经理变更 · 规模翻倍 · 净值停滞 · 连跌趋势 · 分红除权 | 附：市场优选基金 TOP 10 排行（12 维评分）",
     "time": "交易日 15:30"},
    {"id": "monitor", "taskname": "基金盘中监控", "icon": "🔔", "label": "盘中监控",
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
                tasks.append({**t, **info})
            self._send(*_json_response({"ok": True, "tasks": tasks}))
            return

        if parsed.path == "/api/heartbeat":
            hb = read_all_heartbeats()
            self._send(*_json_response({"ok": True, "heartbeats": hb}))
            return

        if parsed.path == "/" or parsed.path == "/index.html":
            self._send_file("fund_manage.html")
            return

        # 尝试提供静态文件（JS/CSS）
        self._send_file(parsed.path.lstrip("/"))

    def _send_file(self, filename: str):
        path = os.path.join(_SCRIPT_DIR, filename)
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
