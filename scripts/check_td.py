import json
d = json.load(open(".fund_recommend_result.json"))
results = d.get("results", [])
print(f"Total: {len(results)} results")
for r in results[:3]:
    print(f'  {r.get("code")} {r.get("name","")[:10]}: td={r.get("td")} day={r.get("day")} score={r.get("score",0):.1f}')
td_count = sum(1 for r in results if r.get("td") is not None)
print(f"Funds with td value: {td_count}/{len(results)}")
# Check td values
td_vals = [r.get("td") for r in results if r.get("td") is not None]
if td_vals:
    print(f"td range: {min(td_vals):.2f} ~ {max(td_vals):.2f}")
