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


class TestSettleEstimateErrors(unittest.TestCase):
    """settle_estimate_errors 结算逻辑（mock 网络与文件 IO，纯逻辑验证）"""

    def setUp(self):
        self.mem = {}  # 内存版 est_error 缓存

    def _run(self, estimates, fetch_map, relevant=False, today_tasks=2):
        """执行 settle，返回写入后的 _save_est_error 收到的 cache"""
        import datetime
        import fund_utils
        saved = {}
        today = datetime.date.today().isoformat()

        def _mock_load():
            return self.mem

        def _mock_save(cache):
            saved.clear()
            saved.update(cache)
            self.mem.clear()
            self.mem.update(cache)

        def _mock_fetch(code, date):
            return fetch_map.get((date, code))

        def _mock_exists(p):
            # 让相关基金文件（data/fund_list.json / .fund_recommend_result.json）不存在
            # → _relevant 为空 → 不过滤；其余路径按真实存在处理
            return False

        with patch("fund_utils._load_est_error", side_effect=_mock_load), \
             patch("fund_utils._save_est_error", side_effect=_mock_save), \
             patch("fund_utils._fetch_actual_nav_pct", side_effect=_mock_fetch), \
             patch("fund_utils.os.path.exists", side_effect=_mock_exists), \
             patch("fund_utils._EST_ERROR_LOCK", threading.Lock()):
            self.mem.clear()
            self.mem.update({"estimates": estimates, "errors": {}})
            fund_utils.settle_estimate_errors()
        return saved

    def test_small_today_tasks_settle_individual(self):
        """今日任务少(<100)：不做整体跳过，逐只结算——已发布生成差异，未发布跳过保留"""
        import datetime
        today = datetime.date.today().isoformat()
        # 007639 今日已发布(actual=-2.76)，001437 今日未发布(fetch 返回 None)
        estimates = {today: {"007639": -2.83, "001437": 0.58}}
        fetch_map = {(today, "007639"): -2.76}  # 001437 无记录 → 未发布
        saved = self._run(estimates, fetch_map)
        err = saved.get("errors", {})
        # 007639 已生成今日差异
        self.assertIn("007639", err, "已发布基金应生成今日差异")
        self.assertEqual(err["007639"][today]["actual"], -2.76)
        self.assertEqual(err["007639"][today]["est"], -2.83)
        self.assertAlmostEqual(err["007639"][today]["err"], 0.07, places=2)
        # 001437 未发布 → 不生成差异，且 estimates 保留待下次
        self.assertNotIn("001437", err, "未发布基金不应生成差异")
        est_left = saved.get("estimates", {})
        self.assertIn("001437", est_left.get(today, {}), "未发布基金应保留在 estimates 待下次结算")

    def test_large_today_tasks_probe_skip(self):
        """今日任务≥100 且探测样本未发布：整体跳过今日，只结算历史"""
        import datetime
        today = datetime.date.today().isoformat()
        # 100+ 个今日任务，探测前 3 只都未发布
        estimates = {today: {f"T{i:06d}": 1.0 for i in range(100)},
                     "2026-08-10": {"H001": 2.0}}
        fetch_map = {(today, "T000000"): 5.0, ("2026-08-10", "H001"): 1.5}
        saved = self._run(estimates, fetch_map, today_tasks=100)
        # 历史任务照常结算
        self.assertIn("H001", saved.get("errors", {}), "历史任务应结算")
        # 今日任务被跳过（探测未发布）→ 今日不产生差异
        err = saved.get("errors", {})
        today_any = any(today in v for v in err.values())
        self.assertFalse(today_any, "今日任务探测未发布时应整体跳过")

    def test_relevant_filter_not_removed_when_no_files(self):
        """相关基金文件缺失时 _relevant 为空 → 不过滤（保留全量结算）"""
        import datetime
        today = datetime.date.today().isoformat()
        estimates = {today: {"A001": 1.0, "B002": 2.0}}
        fetch_map = {(today, "A001"): 1.5, (today, "B002"): 1.0}
        saved = self._run(estimates, fetch_map)
        err = saved.get("errors", {})
        self.assertIn("A001", err)
        self.assertIn("B002", err)


if __name__ == "__main__":
    unittest.main()
