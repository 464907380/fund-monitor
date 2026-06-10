"""
基金推荐工具测试

测试不需要网络的功能（_add_to_fund_list 等）。
"""
import json
import os
import shutil
import pytest


@pytest.fixture
def temp_fund_list(tmp_path):
    """创建临时 fund_list.json，测试后恢复"""
    import fund_recommend as fr
    
    original = fr._FUND_LIST_FILE
    
    # 创建临时文件
    test_file = os.path.join(tmp_path, "fund_list.json")
    with open(test_file, "w", encoding="utf-8") as f:
        json.dump([{"code": "001438"}, {"code": "180031"}], f)
    
    fr._FUND_LIST_FILE = test_file
    yield test_file
    
    # 恢复
    fr._FUND_LIST_FILE = original


class TestAddToFundList:
    """_add_to_fund_list 测试"""

    def test_add_new_code(self, temp_fund_list, capsys):
        """添加新基金代码"""
        import fund_recommend as fr
        
        result = fr._add_to_fund_list("999999", "测试基金")
        assert result is True
        
        with open(temp_fund_list, encoding="utf-8") as f:
            data = json.load(f)
        codes = [item["code"] for item in data]
        assert "999999" in codes
        assert len(data) == 3

    def test_add_duplicate(self, temp_fund_list, capsys):
        """添加已存在的基金代码不重复"""
        import fund_recommend as fr
        
        result = fr._add_to_fund_list("001438", "易方达瑞享混合E")
        assert result is True
        
        with open(temp_fund_list, encoding="utf-8") as f:
            data = json.load(f)
        assert len(data) == 2  # 没有新增

    def test_missing_file(self, capsys):
        """fund_list.json 不存在时返回 False"""
        import fund_recommend as fr
        import tempfile
        
        fake_path = os.path.join(tempfile.gettempdir(), "_test_fund_list_not_exists.json")
        if os.path.exists(fake_path):
            os.remove(fake_path)
        
        original = fr._FUND_LIST_FILE
        fr._FUND_LIST_FILE = fake_path
        result = fr._add_to_fund_list("999999", "测试基金")
        fr._FUND_LIST_FILE = original
        
        assert result is False
        captured = capsys.readouterr()
        assert "不存在" in captured.out


def test_print_results(capsys):
    """_print_results 输出格式正确"""
    from fund_recommend import _print_results
    
    results = [
        {"code": "001438", "name": "易方达瑞享混合E", "score": 85.5,
         "annual_return": 30.5, "sharpe": 1.5, "sortino": 2.0,
         "max_dd": 25.0, "win_rate": 52.0, "inst": 30.0,
         "sc": 20.0, "rate": 0.0, "profit_ratio": 1.5,
         "recovery": 15.0, "sy6": 80.0, "internal": 0.1},
    ]
    _print_results(results)
    captured = capsys.readouterr()
    assert "易方达瑞享" in captured.out
    assert "85.5" in captured.out
