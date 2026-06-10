"""
统一配置加载器

从 config.json 加载所有可调参数（非密钥）。
自动从 .env 加载密钥到 os.environ（无需 python-dotenv）。

用法：
    from config import CFG
    webhook = os.getenv("WECHAT_WEBHOOK", "")
"""

import json
import os
import re

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "config.json")
_ENV_PATH = os.path.join(_SCRIPT_DIR, ".env")

# 内置默认值（config.json 不存在或字段缺失时使用）
def _load_env(path: str) -> None:
    """
    加载 .env 文件，将 KEY=VALUE 写入 os.environ。
    支持 # 注释和空行，引号自动剥离。
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
                if m:
                    key, val = m.group(1), m.group(2)
                    # 去除引号
                    if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
                        val = val[1:-1]
                    os.environ[key] = val
    except OSError:
        pass  # .env 文件不存在是正常的


# 在加载任何配置前，先加载 .env 到环境变量（这样 fund_watch.py 的 os.getenv() 能读到）
_load_env(_ENV_PATH)

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
        "stock_alert_drop_red": -5,
        "stock_alert_drop_yellow": -3,
        "stock_alert_jump_red": 5,
        "stock_alert_jump_yellow": 3,
        "stock_alert_accum_drop_red": -10,
        "stock_alert_accum_drop_yellow": -7,
        "stock_alert_accum_jump_red": 10,
        "stock_alert_accum_jump_yellow": 7,
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
