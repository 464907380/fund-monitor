"""
网络 I/O 函数测试 — mock urllib 避免真实网络请求
测试 fund_utils.py 中的 fetch / fetch_bytes / clear_cache
"""
import json
import unittest
from unittest.mock import patch, MagicMock
import urllib.error

from fund_utils import fetch, fetch_bytes, clear_cache


class TestFetch(unittest.TestCase):
    """测试 fetch（带缓存的 HTTP GET）"""

    @patch("fund_utils._retry_fetch")
    def test_fetch_success(self, mock_retry):
        """正常返回字符串"""
        mock_retry.return_value = '{"key": "value"}'
        result = fetch("https://example.com/api")
        self.assertEqual(result, '{"key": "value"}')
        mock_retry.assert_called_once_with("https://example.com/api")

    @patch("fund_utils._retry_fetch")
    def test_fetch_cache_hit(self, mock_retry):
        """第二次请求走缓存，不再调 _retry_fetch"""
        clear_cache()
        mock_retry.return_value = "cached_data"
        fetch("https://example.com/api")
        fetch("https://example.com/api")
        mock_retry.assert_called_once()  # 只调了一次

    @patch("fund_utils._retry_fetch")
    def test_fetch_cache_miss_then_hit(self, mock_retry):
        """第一次 miss 第二次 hit（两次不同 URL 都调 _retry_fetch）"""
        clear_cache()
        mock_retry.return_value = "data"
        fetch("https://example.com/a")
        fetch("https://example.com/b")
        self.assertEqual(mock_retry.call_count, 2)

    @patch("fund_utils._retry_fetch")
    def test_clear_cache(self, mock_retry):
        """clear_cache 后重新请求"""
        clear_cache()
        mock_retry.return_value = "data"
        fetch("https://example.com/api")
        clear_cache()
        fetch("https://example.com/api")
        self.assertEqual(mock_retry.call_count, 2)

    @patch("fund_utils._retry_fetch")
    def test_fetch_empty_response(self, mock_retry):
        """返回空字符串"""
        mock_retry.return_value = ""
        result = fetch("https://example.com/empty")
        self.assertEqual(result, "")


class TestFetchBytes(unittest.TestCase):
    """测试 fetch_bytes（无缓存的原始 bytes GET）"""

    @patch("fund_utils.urllib.request.urlopen")
    def test_fetch_bytes_success(self, mock_urlopen):
        """正常返回 bytes"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"\x00\x01\x02"
        mock_urlopen.return_value = mock_resp
        result = fetch_bytes("https://example.com/bin")
        self.assertEqual(result, b"\x00\x01\x02")

    @patch("fund_utils.urllib.request.urlopen")
    def test_fetch_bytes_timeout_then_retry(self, mock_urlopen):
        """超时后重试并成功"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"retry_ok"
        mock_urlopen.side_effect = [
            urllib.error.URLError("timeout"),
            mock_resp,
        ]
        result = fetch_bytes("https://example.com/flaky")
        self.assertEqual(result, b"retry_ok")
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("fund_utils.urllib.request.urlopen")
    def test_fetch_bytes_all_retries_exhausted(self, mock_urlopen):
        """所有重试耗尽后返回 None"""
        mock_urlopen.side_effect = [
            urllib.error.URLError("err1"),
            urllib.error.URLError("err2"),
            urllib.error.URLError("err3"),
        ]
        from fund_utils import _RETRY_MAX
        result = fetch_bytes("https://example.com/dead")
        self.assertIsNone(result, "所有重试耗尽应返回 None")
        self.assertEqual(mock_urlopen.call_count, _RETRY_MAX)

    @patch("fund_utils.urllib.request.urlopen")
    def test_fetch_bytes_custom_headers(self, mock_urlopen):
        """自定义请求头"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"ok"
        mock_urlopen.return_value = mock_resp
        fetch_bytes("https://example.com", headers={"Authorization": "Bearer xyz"})
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.headers.get("Authorization"), "Bearer xyz")


if __name__ == "__main__":
    unittest.main()
