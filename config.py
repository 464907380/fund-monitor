"""
统一配置加载器

从 config.json 加载所有可调参数（非密钥）。
密钥通过环境变量读取，示例见 .env.example。

用法：
    from config import CFG
    poll_interval = CFG["fund_monitor"]["poll_interval_seconds"]
"""
import json
import os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "config.json")

# 内置默认值（config.json 不存在或字段缺失时使用）
_DEFAULTS = {
    "fund_watch": {
        "alert_drop_1m": -10,
        "alert_drop_1m_red": -15,
        "alert_scale_2x": 2.0,
        "alert_scale_1_5x": 1.5,
        "stagnation_threshold": 0.05,
        "stagnation_days": 3,
        "consecutive_drop_days": 3,
        "consecutive_drop_total": -3,
        "dividend_drop": -4,
    },
    "fund_monitor": {
        "alert_drop_once": -3,
        "alert_drop_once_yellow": -2,
        "alert_jump_once": 3,
        "alert_jump_once_yellow": 2,
        "alert_accum_drop": -7,
        "alert_accum_drop_yellow": -5,
        "accum_jump": 7,
        "accum_jump_yellow": 5,
        "poll_interval_seconds": 600,
        "max_empty_rounds": 2,
        "holiday_cache_ttl": 86400,
    },
    "network": {
        "retry_max": 3,
        "retry_backoff_seconds": [1, 3, 8],
        "cache_ttl_seconds": 300,
        "cache_max_entries": 100,
    },
    "global_briefing": {
        "retry_max": 3,
        "retry_backoff_seconds": [1, 3, 8],
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典，override 中的值覆盖 base"""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config() -> dict:
    """加载 config.json 并与默认值合并返回"""
    if not os.path.exists(_CONFIG_PATH):
        return _DEFAULTS.copy()
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            user_cfg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _DEFAULTS.copy()
    return _deep_merge(_DEFAULTS, user_cfg)


# 模块级单例
CFG = load_config()
