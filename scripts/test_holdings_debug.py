"""测试持仓数据流程"""
import sys, os, time, json, urllib.request, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from fund_watch import _parse_holdings
from fund_utils import _retry_fetch, api_url

code = '000979'
holds = _parse_holdings(code) or []
if not holds:
    print('No holdings')
    exit()

codes_str = ','.join((h.get('m','sz')+h['c']) for h in holds)
print(f'codes_str: {codes_str}')

# Step 1: Tencent
try:
    raw = _retry_fetch(api_url('tencent_realtime', code=codes_str))
    matched = 0
    for line in raw.strip().split(';'):
        if not line: continue
        parts = line.split('~')
        if len(parts) > 32:
            code_resp = parts[2]
            price = float(parts[3]) if parts[3] else 0
            for h in holds:
                if h['c'] == code_resp:
                    h['price'] = price
                    h['pe'] = float(parts[39]) if len(parts)>39 and parts[39] else None
                    h['mkt_cap'] = float(parts[45]) if len(parts)>45 and parts[45] else None
                    matched += 1
                    break
    print(f'Step 1 Tencent: matched {matched}/{len(holds)}')
except Exception as e:
    print(f'Step 1 FAIL: {e}')

print(f'After Tencent: {sum(1 for h in holds if h.get("price"))}/{len(holds)} have price')
for h in holds:
    print(f'  {h["c"]} {h["n"]}: price={h.get("price","-")} pe={h.get("pe","-")} mkt_cap={h.get("mkt_cap","-")}')
