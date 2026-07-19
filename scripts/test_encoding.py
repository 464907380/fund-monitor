"""Test GBK vs UTF-8 decoding of Tencent API"""
import urllib.request

url = 'http://qt.gtimg.cn/q=sh601138,sz300394,sz300476,hk02899,sz300502'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=10) as r:
    raw_bytes = r.read()

# Decode as GBK (correct)
text_gbk = raw_bytes.decode('gbk')
gbk_lines = [l for l in text_gbk.strip().split(';') if l]
print('=== GBK decode ===')
for line in gbk_lines:
    parts = line.split('~')
    n = len(parts)
    code = parts[2] if len(parts) > 2 else '?'
    first_part = parts[0][:20] if parts[0] else '?'
    print(f'  parts={n} code={code} first={first_part}')

# Decode as UTF-8 (what _request_with_retry does)
text_utf8 = raw_bytes.decode('utf-8', errors='ignore')
utf8_lines = [l for l in text_utf8.strip().split(';') if l]
print()
print('=== UTF-8 decode (current code) ===')
for line in utf8_lines:
    parts = line.split('~')
    n = len(parts)
    code = parts[2] if len(parts) > 2 else '?'
    first_part = parts[0][:20] if parts[0] else '?'
    print(f'  parts={n} code={code} first={first_part}')
