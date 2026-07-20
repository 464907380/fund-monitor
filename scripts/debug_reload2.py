"""诊断：检查推荐缓存与配置的一致性"""
import json, os, hashlib

cfg = json.load(open("data/config.json"))
rec = cfg.get("recommend", {})
top_n = int(rec.get("top_n", 200))
skip_missing = bool(rec.get("skip_missing_perf", False))
skip_limited = bool(rec.get("skip_limited", False))
rank_sort = str(rec.get("rank_sort", "1n"))
filters = rec.get("filter_conditions", [])

_CONFIG_VERSION = "2"
parts = [
    _CONFIG_VERSION,
    str(top_n), str(skip_missing), str(skip_limited), rank_sort,
    json.dumps(filters, sort_keys=True),
]
cur_hash = hashlib.md5("|".join(parts).encode()).hexdigest()
print("Current config:")
print(f"  top_n={top_n}")
print(f"  skip_missing={skip_missing}")
print(f"  skip_limited={skip_limited}")
print(f"  rank_sort={rank_sort}")
print(f"  filters={json.dumps(filters)}")
print(f"  filter_hash={cur_hash[:16]}")

res_file = ".fund_recommend_result.json"
if os.path.exists(res_file):
    d = json.load(open(res_file))
    cached_hash = d.get("filter_hash", "")
    n = len(d.get("results", []))
    print(f"\nCached result:")
    print(f"  count={n}")
    print(f"  filter_hash={cached_hash[:16]}")
    print(f"  date={d.get('date')}")
    print(f"  hash match: {cur_hash == cached_hash}")
    
    if cur_hash != cached_hash:
        print("\n  HASH MISMATCH! Comparing components:")
        print(f"    top_n: {top_n} vs ?")
        print(f"    skip_limited: {skip_limited} vs ?")
        print(f"    filters: {json.dumps(filters)} vs ?")
else:
    print("\nNo cache file")
