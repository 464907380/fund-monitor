"""
fund_utils.py 核心逻辑测试：跨进程锁、估算误差合并写入
（用临时文件隔离，不打真接口、不碰真实数据文件）
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestInterProcessLock(unittest.TestCase):
    """跨进程文件锁：多线程并发读-改-写互斥（单测内用线程模拟进程）"""

    def test_mutex_between_threads(self):
        from fund_utils import inter_process_lock
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "testcache")
            counter = {"v": 0}
            errors = []

            def worker():
                try:
                    for _ in range(50):
                        with inter_process_lock(lock_path, timeout=10):
                            cur = counter["v"]
                            counter["v"] = cur + 1
                except Exception as e:  # pragma: no cover
                    errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(counter["v"], 400,
                             f"并发应无损, 实际 {counter['v']} (errors={errors})")

    def test_release_cleans_up(self):
        from fund_utils import inter_process_lock
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "c")
            with inter_process_lock(lock_path):
                pass
            with inter_process_lock(lock_path):  # 释放后可再获取
                pass


class TestSaveEstErrorMerge(unittest.TestCase):
    """_save_est_error 合并式写入：长任务旧快照不覆盖他进程新增"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.err_path = os.path.join(self.tmpdir.name, "fund_est_error.json")

    def _disk(self) -> dict:
        try:
            with open(self.err_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def test_merge_preserves_concurrent_estimates(self):
        """模拟：record_estimate 写入 A，随后 backfill 用不含 A 的旧快照写 errors"""
        import fund_utils
        with patch("fund_utils._EST_ERROR_PATH", self.err_path), \
             patch("fund_utils._EST_ERROR_MEM", None), \
             patch("fund_utils._EST_ERROR_MTIME", -1.0):
            # 1. 进程 A record_estimate 写入 TESTCODE
            fund_utils._save_est_error(
                {"estimates": {"2026-08-11": {"TESTCODE": 1.0}}, "errors": {}})
            # 2. 进程 B backfill 用"不含 TESTCODE 的旧快照"写 errors
            fund_utils._save_est_error(
                {"estimates": {"2026-08-11": {}},
                 "errors": {"TESTCODE": {"2026-08-10": {"est": 1, "actual": 2, "err": 1}}}})
            disk = self._disk()
            self.assertIn("TESTCODE", disk.get("estimates", {}).get("2026-08-11", {}),
                          "合并式写入应保留他进程新增的 estimates")
            self.assertIn("TESTCODE", disk.get("errors", {}),
                          "errors 应合并写入")

    def test_merge_errors_accumulates(self):
        """errors 历史累积：多次追加不同日期不丢失"""
        import fund_utils
        with patch("fund_utils._EST_ERROR_PATH", self.err_path), \
             patch("fund_utils._EST_ERROR_MEM", None), \
             patch("fund_utils._EST_ERROR_MTIME", -1.0):
            fund_utils._save_est_error(
                {"errors": {"000001": {"2026-08-10": {"est": 1, "actual": 2, "err": 1}}}})
            fund_utils._save_est_error(
                {"errors": {"000001": {"2026-08-09": {"est": 1, "actual": 1, "err": 0}}}})
            disk = self._disk()
            self.assertEqual(len(disk.get("errors", {}).get("000001", {})), 2,
                             "多次合并应累积不同日期")


if __name__ == "__main__":
    unittest.main()
