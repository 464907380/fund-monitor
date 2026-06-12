"""
global_briefing.py 构建函数测试 — mock 网络请求
"""
import unittest
from unittest.mock import patch, MagicMock
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


MOCK_A_SHARES = [
    {"code": "上证", "current": 3200.0, "change": 0.5},
    {"code": "深证", "current": 10500.0, "change": 1.2},
]

MOCK_GLOBALS = [
    {"code": "道指", "current": 38000.0, "change": -0.3},
    {"code": "纳斯达克", "current": 17000.0, "change": 0.8},
]


class TestBuildBriefingMd(unittest.TestCase):
    """build_briefing_md — Markdown 简报构建"""

    def test_basic_md_structure(self):
        from global_briefing import build_briefing_md
        result = build_briefing_md(
            a_shares=MOCK_A_SHARES,
            globals_=MOCK_GLOBALS,
            senti={"recent": {"2026-06-12": 8000, "2026-06-11": 7500}, "rank_str": "近20日第5"},
            breadth={"up": 2000, "down": 1500},
        )
        self.assertIn("全球股市简报", result)
        self.assertIn("上证", result)
        self.assertIn("3200", result)
        self.assertIn("道指", result)
        self.assertIn("38000", result)
        self.assertIn("2000", result)  # breadth up
        self.assertIn("1500", result)  # breadth down

    @patch("global_briefing.get_a_share", return_value=[])
    @patch("global_briefing.get_global", return_value=[])
    @patch("global_briefing._fetch_sentiment", return_value=None)
    @patch("global_briefing._fetch_market_breadth", return_value=None)
    def test_empty_data(self, *mocks):
        from global_briefing import build_briefing_md
        result = build_briefing_md()
        self.assertIn("全球股市简报", result)
        self.assertIn("所有数据源均不可用", result)

    @patch("global_briefing.get_global", return_value=[])
    @patch("global_briefing._fetch_sentiment", return_value=None)
    @patch("global_briefing._fetch_market_breadth", return_value=None)
    def test_partial_data(self, *mocks):
        from global_briefing import build_briefing_md
        result = build_briefing_md(a_shares=MOCK_A_SHARES)
        self.assertIn("A股", result)
        self.assertIn("上证", result)


class TestBuildBriefingHtml(unittest.TestCase):
    """build_briefing_html — HTML 简报构建"""

    def test_basic_html_structure(self):
        from global_briefing import build_briefing_html
        result = build_briefing_html(
            a_shares=MOCK_A_SHARES,
            globals_=MOCK_GLOBALS,
            senti={"recent": {"2026-06-12": 8000}, "rank_str": ""},
            breadth={"up": 2000, "down": 1500},
        )
        self.assertIn("上证", result)
        self.assertIn("道指", result)
        self.assertIn("<table", result)
        self.assertIn("</table>", result)

    @patch("global_briefing.get_a_share", return_value=[])
    @patch("global_briefing.get_global", return_value=[])
    @patch("global_briefing._fetch_sentiment", return_value=None)
    @patch("global_briefing._fetch_market_breadth", return_value=None)
    def test_empty_data(self, *mocks):
        from global_briefing import build_briefing_html
        result = build_briefing_html()
        self.assertIn("所有数据源均不可用", result)

    @patch("global_briefing.get_global", return_value=[])
    @patch("global_briefing._fetch_sentiment", return_value=None)
    @patch("global_briefing._fetch_market_breadth", return_value=None)
    def test_partial_data(self, *mocks):
        from global_briefing import build_briefing_html
        result = build_briefing_html(a_shares=MOCK_A_SHARES)
        self.assertIn("A股", result)
        self.assertIn("</html>", result)


class TestHtmlSectionHelpers(unittest.TestCase):
    """_html_*_section 辅助函数"""

    def test_a_share_section(self):
        from global_briefing import _html_a_share_section
        rows = _html_a_share_section(MOCK_A_SHARES)
        self.assertTrue(len(rows) > 0)
        combined = " ".join(rows)
        self.assertIn("上证", combined)
        self.assertIn("3200", combined)

    def test_volume_section(self):
        from global_briefing import _html_volume_section
        senti = {"recent": {"2026-06-12": 8000, "2026-06-11": 7500, "2026-06-10": 7000}, "rank_str": "近20日第5"}
        rows = _html_volume_section(senti)
        self.assertTrue(len(rows) > 0)
        combined = " ".join(rows)
        self.assertIn("8000", combined)

    def test_breadth_section(self):
        from global_briefing import _html_breadth_section
        rows = _html_breadth_section({"up": 2000, "down": 1500})
        self.assertTrue(len(rows) > 0)
        combined = " ".join(rows)
        self.assertIn("2000", combined)

    def test_global_section(self):
        from global_briefing import _html_global_section
        rows = _html_global_section(MOCK_GLOBALS)
        self.assertTrue(len(rows) > 0)
        combined = " ".join(rows)
        self.assertIn("道指", combined)

    def test_empty_sections(self):
        from global_briefing import _html_a_share_section, _html_volume_section
        from global_briefing import _html_breadth_section, _html_global_section
        self.assertEqual(_html_a_share_section(None), [])
        self.assertEqual(_html_volume_section(None), [])
        self.assertEqual(_html_breadth_section(None), [])
        self.assertEqual(_html_global_section(None), [])


if __name__ == "__main__":
    unittest.main()
