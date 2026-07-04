"""
fund_render.py 推送/保存/渲染逻辑测试
"""
import json
import os
import unittest
from unittest.mock import patch, MagicMock, mock_open
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestPipeTableToHtml(unittest.TestCase):
    """_pipe_table_to_html — 纯字符串转换"""

    def test_converts_pipe_table(self):
        from fund_render import _pipe_table_to_html
        lines = [
            "🏆 **市场优选基金 TOP 10**",
            "",
            "|排名|基金名|年化%|",
            "|:---:|:---|:---:|",
            "|🥇|测试基金A|21.6%|",
            "|🥈|测试基金B|18.5%|",
        ]
        result = _pipe_table_to_html(lines)
        self.assertIn("测试基金A", result)
        self.assertIn("21.6%", result)
        self.assertIn("<table", result)
        self.assertIn("</table>", result)

    def test_empty_lines(self):
        from fund_render import _pipe_table_to_html
        result = _pipe_table_to_html([])
        self.assertIn("</td></tr>", result)

    def test_non_table_lines(self):
        from fund_render import _pipe_table_to_html
        lines = ["普通文本行"]
        result = _pipe_table_to_html(lines)
        self.assertIn("普通文本行", result)


class TestMdContent(unittest.TestCase):
    """md_content — Markdown 构建"""

    def test_basic_content(self):
        from fund_render import md_content
        rows = [
            {"code": "000001", "name_short": "测试A", "day": "+1.23%",
             "f5": "+2.1%", "m1": "+3.4%", "m3": "+5.6%", "y1": "+10.0%", "mgr": "张三"}
        ]
        result = md_content(rows, [], "2026-06-13")
        self.assertIn("基金晚报", result)
        self.assertIn("000001", result)
        self.assertIn("测试A", result)
        self.assertIn("+1.23%", result)

    def test_with_alerts(self):
        from fund_render import md_content
        rows = [{"code": "A", "name_short": "T", "day": "", "f5": "", "m1": "", "m3": "", "y1": "", "mgr": ""}]
        result = md_content(rows, ["🚨 测试警报"], "2026-06-13")
        self.assertIn("测试警报", result)
        self.assertIn("警报", result)

    @patch("fund_render._format_recommend_rankings")
    def test_with_ranking(self, mock_fmt):
        from fund_render import md_content
        mock_fmt.return_value = ["| 排名 | 基金 | 评分 |", "|:---:|:---|:---:|", "| 1 | 优质基金 | 95 |"]
        rows = [{"code": "A", "name_short": "T", "day": "", "f5": "", "m1": "", "m3": "", "y1": "", "mgr": ""}]
        result = md_content(rows, [], "2026-06-13")
        self.assertIn("优质基金", result)
        self.assertIn("95", result)


class TestBuildBriefingHtml(unittest.TestCase):
    """_build_briefing_html — HTML 模板填充"""

    @patch("builtins.open", new_callable=mock_open, read_data="<html>{{DATE}}{{ROWS}}{{ALERTS}}</html>")
    @patch("fund_render.os.path.exists", return_value=True)
    def test_builds_html(self, mock_exists, mock_file):
        from fund_render import _build_briefing_html
        rows = [{"code": "000001", "name_short": "测试A", "day": "+1.23%",
                 "f5": "+2.1%", "m1": "+3.4%", "m3": "+5.6%", "y1": "+10.0%", "score": 72.5}]
        result = _build_briefing_html(rows, [], "2026-06-13")
        self.assertIsNotNone(result)
        self.assertIn("2026-06-13", result)  # DATE 替换
        self.assertIn("000001", result)       # 行数据

    @patch("fund_render.os.path.exists", return_value=False)
    def test_missing_template(self, mock_exists):
        from fund_render import _build_briefing_html
        result = _build_briefing_html([], [], "2026-06-13")
        self.assertIsNone(result)


class TestFormatRecommendRankings(unittest.TestCase):
    """_format_recommend_rankings — 推荐排行格式化"""

    @patch("fund_render._load_saved_recommend_data")
    def test_with_data(self, mock_load):
        from fund_render import _format_recommend_rankings
        mock_load.return_value = [
            {"n": "测试基金A", "code": "720001", "score": 71.4, "annual_return": 21.6,
             "m1": 28.6, "m3": 87.1, "y1": 342.1,
             "sharpe": 0.62, "max_dd": 62.22, "sy3": 284.1}
            for _ in range(10)
        ]
        lines = _format_recommend_rankings()
        self.assertIn("测试基金A", str(lines))

    @patch("fund_render._load_saved_recommend_data")
    def test_no_data(self, mock_load):
        from fund_render import _format_recommend_rankings
        mock_load.return_value = []
        lines = _format_recommend_rankings()
        combined = " ".join(lines)
        self.assertIn("想看看市场", combined)

    @patch("fund_render._load_saved_recommend_data")
    @patch("fund_render.os.path.exists", return_value=False)
    def test_stale_data(self, mock_exists, mock_data):
        from fund_render import _format_recommend_rankings
        mock_data.return_value = []
        lines = _format_recommend_rankings()
        combined = " ".join(lines)
        self.assertIn("想看看市场", combined)


class TestSaveBriefing(unittest.TestCase):
    """_save_briefing — 晚报保存"""

    @patch("fund_render._build_briefing_html")
    def test_save_skips_if_no_html(self, mock_build):
        from fund_render import _save_briefing
        mock_build.return_value = None
        # 不应抛出异常
        _save_briefing([], [], "2026-06-13")
        mock_build.assert_called_once()

    @patch("fund_render._build_briefing_html")
    @patch("fund_render.os.path.exists", return_value=True)
    @patch("builtins.open", new_callable=mock_open)
    def test_save_writes_file(self, mock_file, mock_exists, mock_build):
        from fund_render import _save_briefing
        mock_build.return_value = "<html><body>简报内容</body></html>"
        _save_briefing([], [], "2026-06-13")
        # 验证文件被写入
        mock_file.assert_called()
        # 写入内容包含处理后的文本
        handle = mock_file()
        written = "".join(call[0][0] for call in handle.write.call_args_list)
        self.assertIn("简报内容", written)


if __name__ == "__main__":
    unittest.main()
