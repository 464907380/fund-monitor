"""
配置层测试

验证 config.py 能正确加载 config.json、合并默认值、处理文件缺失。
"""
import json
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def backup_cwd():
    """在所有测试前后保持工作目录不变"""
    cwd = os.getcwd()
    yield
    os.chdir(cwd)


def test_load_config_with_json():
    """config.json 存在时正确加载并覆盖默认值"""
    from config import load_config

    cfg = load_config()
    assert "fund_watch" in cfg
    assert "fund_monitor" in cfg
    assert "network" in cfg
    assert "global_briefing" in cfg

    # 验证默认值与 config.json 一致
    assert cfg["fund_watch"]["alert_drop_1m"] == -10
    assert cfg["fund_monitor"]["poll_interval_seconds"] == 600
    assert cfg["network"]["retry_max"] == 3


def test_cfg_module_singleton():
    """CFG 单例可导入且结构正确"""
    from config import CFG

    assert CFG["fund_watch"]["stagnation_days"] == 3
    assert CFG["fund_monitor"]["alert_drop_once"] == -3


def test_deep_merge_override():
    """深度合并应让用户值覆盖默认值"""
    from config import _deep_merge

    base = {"a": 1, "b": {"c": 2, "d": 3}}
    override = {"b": {"c": 99}}
    result = _deep_merge(base, override)
    assert result == {"a": 1, "b": {"c": 99, "d": 3}}


def test_config_json_missing(tmp_path):
    """config.json 缺失时返回全套默认值"""
    # 切换到临时目录（没有 config.json）
    old_cwd = os.getcwd()
    os.chdir(tmp_path)

    # 需要重新加载模块以清除缓存
    import importlib
    import config as cfg_mod

    importlib.reload(cfg_mod)
    reloaded = cfg_mod.load_config()

    assert reloaded["fund_watch"]["alert_drop_1m"] == -10
    assert reloaded["fund_monitor"]["poll_interval_seconds"] == 600
    assert reloaded["network"]["retry_max"] == 3

    os.chdir(old_cwd)
