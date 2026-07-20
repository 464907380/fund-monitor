"""调试：验证 _reload_config 与模块级值的一致性"""
import os, sys, json, hashlib

_CONFIG_VERSION = "2"

# 模拟模块级别读取（从 CFG 即 config.py 的 deep_merge 结果）
_MOD_CFG = json.load(open(os.path.join(os.path.dirname(__file__), "..", "data", "config.json")))
_mod_rec = _MOD_CFG.get("recommend", {})
_mod_TOP = int(_mod_rec.get("top_n", 200))
_mod_SKIP_MISSING = bool(_mod_rec.get("skip_missing_perf", False))
_mod_SKIP_LIMITED = bool(_mod_rec.get("skip_limited", False))
_mod_RANK_SORT = str(_mod_rec.get("rank_sort", "1n"))
_mod_FILTER = _mod_rec.get("filter_conditions", [])

# 模拟 _reload_config 读取（直接读文件 raw）
_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "config.json")
_CFG = json.load(open(_PATH))
_REC = _CFG.get("recommend", {})
_rl_TOP = int(_REC.get("top_n", 200))
_rl_SKIP_MISSING = bool(_REC.get("skip_missing_perf", False))
_rl_SKIP_LIMITED = bool(_REC.get("skip_limited", False))
_rl_RANK_SORT = str(_REC.get("rank_sort", "1n"))
_rl_FILTER = _REC.get("filter_conditions", [])

print("=== 模块级别 vs _reload_config ===")
print(f"top_n:          {_mod_TOP} vs {_rl_TOP}")
print(f"skip_missing:   {_mod_SKIP_MISSING} vs {_rl_SKIP_MISSING}")
print(f"skip_limited:   {_mod_SKIP_LIMITED} vs {_rl_SKIP_LIMITED}")
print(f'rank_sort:      "{_mod_RANK_SORT}" vs "{_rl_RANK_SORT}"')
print(f"filter_conds:   {_mod_FILTER} vs {_rl_FILTER}")

def _h(TOP, SKIP_MISSING, SKIP_LIMITED, RANK_SORT, FILTER):
    parts = [_CONFIG_VERSION, str(TOP), str(SKIP_MISSING), str(SKIP_LIMITED), RANK_SORT, json.dumps(FILTER, sort_keys=True)]
    return hashlib.md5("|".join(parts).encode()).hexdigest()

h_mod = _h(_mod_TOP, _mod_SKIP_MISSING, _mod_SKIP_LIMITED, _mod_RANK_SORT, _mod_FILTER)
h_rl = _h(_rl_TOP, _rl_SKIP_MISSING, _rl_SKIP_LIMITED, _rl_RANK_SORT, _rl_FILTER)

print(f"\nmodule hash:    {h_mod[:16]}")
print(f"reload hash:    {h_rl[:16]}")
print(f"match:          {h_mod == h_rl}")

# 也检查缓存
res_file = os.path.join(os.path.dirname(__file__), "..", ".fund_recommend_result.json")
if os.path.exists(res_file):
    d = json.load(open(res_file))
    cached_hash = d.get("filter_hash", "")
    print(f"cached hash:    {cached_hash[:16]}")
    print(f"cur == cached:  {h_mod == cached_hash}")
