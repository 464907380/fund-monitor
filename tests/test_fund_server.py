"""
fund_server.py 路由测试 — 直接 mock handler，避免 BaseHTTPRequestHandler 初始化
"""
import json
import unittest
from unittest.mock import patch, MagicMock, PropertyMock
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 在 import fund_server 前 mock socket 以避免实际绑定端口
with patch("socketserver.TCPServer.server_bind"), \
     patch("socketserver.TCPServer.server_activate"):
    from fund_server import Handler


def make_handler(method: str, path: str, body: bytes = b"") -> Handler:
    """创建 Handler 实例，手动设属性避开 __init__ 的 socket 需求"""
    h = Handler.__new__(Handler)
    h.command = method
    h.path = path
    h.request_version = "HTTP/1.1"
    h.headers = MagicMock()
    h.headers.get.return_value = str(len(body))
    h.rfile = MagicMock()
    h.rfile.read.return_value = body
    h.wfile = MagicMock()
    h.close_connection = True
    h.send_response = MagicMock()
    h.send_header = MagicMock()
    h.end_headers = MagicMock()
    return h


class TestFundServerRoutes(unittest.TestCase):
    """fund_server.py HTTP 路由测试"""

    maxDiff = None

    def _send(self, handler, status, headers, body):
        handler._send(status, headers, body)

    @patch("fund_server._load")
    def test_api_list(self, mock_load):
        """GET /api/list 返回基金列表"""
        mock_load.return_value = [{"code": "000001", "name": "测试基金"}]
        h = make_handler("GET", "/api/list")
        with patch.object(h, "_send") as mock_send:
            h.do_GET()
            args = mock_send.call_args[0]
            self.assertEqual(args[0], 200)
            resp = json.loads(args[2])
            self.assertTrue(resp["ok"])
            self.assertEqual(len(resp["funds"]), 1)
            self.assertEqual(resp["funds"][0]["code"], "000001")

    @patch("fund_server._load")
    @patch("fund_server._save")
    @patch("fund_server._fetch_fund_name")
    def test_api_add(self, mock_fetch, mock_save, mock_load):
        """POST /api/add 添加基金"""
        mock_load.return_value = []
        mock_fetch.return_value = "测试基金"
        body = json.dumps({"codes": ["000001"]}).encode()
        h = make_handler("POST", "/api/add", body)
        with patch.object(h, "_send") as mock_send:
            h.do_POST()
            args = mock_send.call_args[0]
            self.assertEqual(args[0], 200)
            resp = json.loads(args[2])
            self.assertTrue(resp["ok"])
            self.assertIn("000001", resp["added"])

    @patch("fund_server._load")
    @patch("fund_server._save")
    def test_api_remove(self, mock_save, mock_load):
        """POST /api/remove 移除基金"""
        mock_load.return_value = [{"code": "000001", "name": "测试基金"}, {"code": "000002", "name": "测试2"}]
        body = json.dumps({"codes": ["000001"]}).encode()
        h = make_handler("POST", "/api/remove", body)
        with patch.object(h, "_send") as mock_send:
            h.do_POST()
            args = mock_send.call_args[0]
            self.assertEqual(args[0], 200)
            resp = json.loads(args[2])
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["removed"], ["000001"])

    @patch("fund_server._load")
    @patch("fund_server._save")
    def test_api_add_duplicate(self, mock_save, mock_load):
        """POST /api/add 重复添加应跳过"""
        mock_load.return_value = [{"code": "000001", "name": "测试基金"}]
        body = json.dumps({"codes": ["000001"]}).encode()
        h = make_handler("POST", "/api/add", body)
        with patch.object(h, "_send") as mock_send:
            h.do_POST()
            args = mock_send.call_args[0]
            resp = json.loads(args[2])
            self.assertTrue(resp["ok"])
            self.assertIn("000001", resp["skipped"])

    def test_api_404(self):
        """未知路径返回 404"""
        h = make_handler("GET", "/api/unknown")
        with patch.object(h, "_send") as mock_send:
            h.do_GET()
            args = mock_send.call_args[0]
            self.assertEqual(args[0], 404)

    def test_send_file_forbidden(self):
        """路径穿越应返回 403"""
        h = make_handler("GET", "/../../../etc/passwd")
        with patch.object(h, "_send") as mock_send:
            h.do_GET()
            args = mock_send.call_args[0]
            self.assertEqual(args[0], 403)

    def test_send_file_not_found(self):
        """不存在的文件返回 404"""
        h = make_handler("GET", "/nonexistent.html")
        with patch.object(h, "_send") as mock_send:
            h.do_GET()
            args = mock_send.call_args[0]
            self.assertEqual(args[0], 404)


if __name__ == "__main__":
    unittest.main()
