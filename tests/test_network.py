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
        mock_retry.assert_called_once_with("https://example.com/api", None)  # headers 默认 None

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

    @patch("fund_utils._request_with_retry")
    def test_fetch_bytes_success(self, mock_retry):
        """正常返回 bytes"""
        mock_retry.return_value = b"\x00\x01\x02"
        result = fetch_bytes("https://example.com/bin")
        self.assertEqual(result, b"\x00\x01\x02")
        # fetch_bytes 用 decode=False 调用 _request_with_retry
        _call = mock_retry.call_args
        self.assertFalse(_call.kwargs.get("decode", _call.args[1]) if _call.args and len(_call.args) > 1 else _call.kwargs.get("decode", False))

    @patch("fund_utils._request_with_retry")
    def test_fetch_bytes_timeout_then_retry(self, mock_retry):
        """内部重试：_request_with_retry 负责重试，最终返回 bytes"""
        mock_retry.return_value = b"retry_ok"
        result = fetch_bytes("https://example.com/flaky")
        self.assertEqual(result, b"retry_ok")
        self.assertEqual(mock_retry.call_count, 1)

    @patch("fund_utils._request_with_retry")
    def test_fetch_bytes_all_retries_exhausted(self, mock_retry):
        """重试耗尽：_request_with_retry 返回 None → fetch_bytes 返回 None"""
        mock_retry.return_value = None
        result = fetch_bytes("https://example.com/dead")
        self.assertIsNone(result, "所有重试耗尽应返回 None")

    @patch("fund_utils._request_with_retry")
    def test_fetch_bytes_custom_headers(self, mock_retry):
        """自定义请求头：透传给 _request_with_retry 的 Request"""
        mock_retry.return_value = b"ok"
        fetch_bytes("https://example.com", headers={"Authorization": "Bearer xyz"})
        req = mock_retry.call_args[0][0]
        self.assertEqual(req.headers.get("Authorization"), "Bearer xyz")


if __name__ == "__main__":
    unittest.main()
