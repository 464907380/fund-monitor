"""检查推荐结果中td值是否来自今日最新拉取"""
import json, os, datetime

d = json.load(open(".fund_recommend_result.json"))
results = d.get("results", [])
print(f"Results: {len(results)}")
print(f"Date: {d.get('date')}")
print(f"Filter hash: {d.get('filter_hash','')[:16]}")

# Check cache file mtime
mtime = os.path.getmtime(".fund_recommend_result.json")
mt = datetime.datetime.fromtimestamp(mtime)
print(f"File mtime: {mt.strftime('%Y-%m-%d %H:%M:%S')}")

# Check original score vs current score
for r in results[:5]:
    y1 = r.get("y1", 0)
    score = r.get("score", 0)
    td = r.get("td")
    print(f'  {r.get("code")} {r.get("name","")[:12]}: score={score:.1f} y1={y1:.1f}% td={td}')
