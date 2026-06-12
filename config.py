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


def get_secret(name: str, default: str = "") -> str:
    """统一获取密钥配置（唯一 os.getenv 入口）"""
    return os.getenv(name, default)


def api_url(name: str, **kwargs) -> str:
    """获取 API URL，支持 {key} 占位符替换"""
    url = CFG.get("network", {}).get("api", {}).get(name, "")
    if kwargs:
        url = url.format(**kwargs)
    return url


def _warn_missing_secrets() -> None:
    """启动时检查密钥配置，缺失时输出警告"""
    missing = []
    webhook = get_secret("WECHAT_WEBHOOK")
    qq_email = get_secret("QQ_EMAIL")
    qq_auth = get_secret("QQ_MAIL_AUTH")
    if not webhook and not (qq_email and qq_auth):
        missing.append("💡 提示：未配置 WECHAT_WEBHOOK 和 QQ 邮箱，无法推送")
    elif qq_email and not qq_auth:
        missing.append("💡 提示：QQ_MAIL_AUTH 未配置，邮件推送不可用")
    if missing:
        import logging
        for msg in missing:
            logging.info("%s", msg)


# 在加载任何配置前，先加载 .env 到环境变量（这样 fund_watch.py 的 os.getenv() 能读到）
_load_env(_ENV_PATH)
_warn_missing_secrets()

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
        "api": {
            "fund_pingzhongdata": "https://fund.eastmoney.com/pingzhongdata/{code}.js",
            "fund_estimate": "https://fundgz.1234567.com.cn/js/{code}.js",
            "fund_estimate_fallback": "http://fundgz.1234567.com.cn/js/{code}.js",
            "fund_holdings": "https://fund.eastmoney.com/f10/FundArchivesDatas.aspx?type=jjcc&code={code}&topline=5&year=&month=&rt=0.1",
            "fund_rank": "https://fund.eastmoney.com/data/rankhandler.aspx",
            "fund_search_index": "https://fund.eastmoney.com/js/fundcode_search.js",
            "sina_hq": "https://hq.sinajs.cn/list={code}",
            "sina_volume": "https://hq.sinajs.cn/list=sh000001,sz399001",
            "sina_market": "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
            "tencent_kline": "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,10,qfq",
            "eastmoney_quote": "https://push2.eastmoney.com/api/qt/stock/get",
            "tencent_realtime": "http://qt.gtimg.cn/q={code}",
            "holiday": "https://timor.tech/api/holiday/info/{date}",
        },
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
        import logging
        logging.warning("⚠️ config.json 损坏或无法读取，使用默认配置（部分阈值可能不符合预期）")
        return _DEFAULTS.copy()
    return _deep_merge(_DEFAULTS, user_cfg)


# 模块级单例
CFG = load_config()
