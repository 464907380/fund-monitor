"""Check the raw Tencent response for semicolons in unexpected places"""
import sys, os, urllib.request, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from fund_utils import api_url

codes_str = 'sz300502,sz300308,hk02899,sh601138,sz300394,sz300476,hk00700,sz002463,sh688195,sh688498'
url = api_url('tencent_realtime', code=codes_str)
print(f'URL: {url}')

req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=15) as r:
    raw_bytes = r.read()

# Decode as UTF-8 (what the code does)
text = raw_bytes.decode('utf-8', errors='ignore')
print(f'Total chars: {len(text)}')
print(f'Semicolons: {text.count(";")}')

# Split and show each segment
segments = text.split(';')
print(f'Segments after split: {len(segments)}')
non_empty = [s for s in segments if s.strip()]
print(f'Non-empty segments: {len(non_empty)}')

for i, seg in enumerate(non_empty[:15]):
    # Find the stock code (first ~ separated field after v_ prefix)
    parts = seg.split('~')
    code = parts[2] if len(parts) > 2 else '?'
    has_data = len(parts) > 32
    print(f'  [{i}] prefix={seg[:30]!r} code={code} parts={len(parts)} has_data={has_data}')

# Also decode as GBK to compare
text_gbk = raw_bytes.decode('gbk')
gbk_segments = text_gbk.split(';')
gbk_non_empty = [s for s in gbk_segments if s.strip()]
print(f'\nGBK decode - non-empty segments: {len(gbk_non_empty)}')
for i, seg in enumerate(gbk_non_empty[:15]):
    parts = seg.split('~')
    code = parts[2] if len(parts) > 2 else '?'
    has_data = len(parts) > 32
    print(f'  [{i}] code={code} parts={len(parts)} has_data={has_data}')
