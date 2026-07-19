"""更精确地找FGL报告期"""
import urllib.request as r
import re

html = r.urlopen(r.Request("https://vip.stock.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine/stockid/600519/displaytype/4.phtml",
                           headers={"User-Agent": "Mozilla/5.0"}), timeout=10).read().decode("gbk", "ignore")

# 找到包含日期的行 - 通常前面是"本期"或"上期"
# 找表格第一行的内容
for i, m in enumerate(re.finditer(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)):
    row = m.group(1)
    tds = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)
    clean = [re.sub(r'<[^>]+>', '', t).strip() for t in tds]
    if any('202' in c or '本期' in c or '上期' in c or '去年同期' in c for c in clean if c):
        print(f"Row {i}: {clean}")

# Another approach: look for all patterns like "><td>日期</td>"
# The FGL page has a specific structure where the first table row
# after the header is "本期" with a date

# Find the "本期" label and the date right after it
for m in re.finditer(r'<td[^>]*>(本期|上期|最新一期|去年同期)</td>\s*<td[^>]*>([^<]+)</td>', html):
    label = re.sub(r'<[^>]+>', '', m.group(1)).strip()
    date = m.group(2).strip()
    print(f"Label '{label}' = Date '{date}'")

# Try a broader pattern
for m in re.finditer(r'<td[^>]*>(本期|上期|最新一期|去年同期)</td>', html):
    pos = m.end()
    # Get next 500 chars
    ctx = html[pos:pos+500]
    dates = re.findall(r'(\d{4}[-/]\d{2}[-/]\d{2})', ctx)
    if dates:
        print(f"After '{m.group(1)}': {dates[:3]}")
