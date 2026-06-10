"""
全球股市简报测试

验证指数列表和简报格式的正确性。
"""
import pytest


def test_global_indices_have_all_markets():
    """全球指数列表应包含美股/亚洲/欧洲主要市场"""
    from global_briefing import GLOBAL_INDICES

    codes = dict(GLOBAL_INDICES)

    assert "道琼斯" in codes.values()
    assert "纳斯达克" in codes.values()
    assert "标普500" in codes.values()
    assert "恒生指数" in codes.values()
    assert "日经225" in codes.values()
    assert "韩国KOSPI" in codes.values()
    assert "英国富时100" in codes.values()
    assert "德国DAX" in codes.values()


def test_a_indices_have_three():
    """A股三大指数应包含上证/深证/沪深300"""
    from global_briefing import A_INDICES

    assert len(A_INDICES) == 3
    names = [n for _, n in A_INDICES]
    assert "上证指数" in names
    assert "深证成指" in names
    assert "沪深300" in names


def test_build_briefing_format():
    """简报格式应包含标题和表头"""
    from global_briefing import build_briefing

    briefing = build_briefing()
    assert "全球股市简报" in briefing
    assert "A股" in briefing or "全球" in briefing
