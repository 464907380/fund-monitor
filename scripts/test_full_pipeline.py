"""Full pipeline test: exactly what the HTTP server does"""
import sys, os, time, re, json, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from fund_watch import _parse_holdings
from fund_utils import _retry_fetch, api_url

code = '000979'
holds = _parse_holdings(code) or []
if not holds:
    print('No holdings')
    exit()

codes_str = ','.join((h.get('m', 'sz') + h['c']) for h in holds)
print(f'codes_str: {codes_str}')
print(f'Holdings codes: {[h["c"] for h in holds]}')

# Simulate EXACTLY what the server does
if holds:
    import urllib.request as _ur
    _tencent_cache = {}
    _tencent_key = 'realtime_' + codes_str
    _tencent_now = time.time()
    
    raw = _retry_fetch(api_url('tencent_realtime', code=codes_str))
    _tencent_cache[_tencent_key] = (_tencent_now, raw)
    
    print(f'Raw length: {len(raw)}')
    print(f'Raw lines after split (non-empty): {sum(1 for l in raw.strip().split(";") if l)}')
    
    _tc = 0
    try:
        for line in raw.strip().split(';'):
            if not line:
                continue
            parts = line.split('~')
            nparts = len(parts)
            code_resp = parts[2] if len(parts) > 2 else '?'
            matched = False
            if nparts > 32:
                _tc += 1
                for h in holds:
                    if h['c'] == code_resp:
                        h['price'] = float(parts[3]) if parts[3] else 0
                        matched = True
                        break
                print(f'  Line: parts={nparts} code={code_resp} matched={matched} price_set={"price" in str(h) if matched else "N/A"}')
            else:
                print(f'  SKIP: parts={nparts} code={code_resp} (too few fields)')
    except Exception as e:
        print(f'Error: {e}')
    
    print(f'\n_tc={_tc}')
    print(f'With price: {sum(1 for h in holds if h.get("price"))}/{len(holds)}')
