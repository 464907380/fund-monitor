"""查找FGL页面的报告期信息"""
import urllib.request as r
import re

codes = ["600519", "300502", "000002"]  # 茅台，新易盛，万科

for code in codes:
    url = f"https://vip.stock.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine/stockid/{code}/displaytype/4.phtml"
    html = r.urlopen(r.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10).read().decode("gbk", "ignore")
    
    print(f"\n=== {code} ===")
    
    # 找表格标题栏
    # 新浪FGL页面有"本期"和"上期"两个数据列，找它们的表头
    ths = re.findall(r'<th[^>]*>(.*?)</th>', html, re.DOTALL)
    for th in ths:
        clean = re.sub(r'<[^>]+>', '', th).strip()
        if clean and len(clean) < 50:
            print(f"  TH: {clean}")
    
    # 找"本期"所在的td
    for m in re.finditer(r'<td[^>]*>\s*本期\s*</td>', html):
        ctx = html[m.start()-200:m.end()+200]
        # 找附近的日期
        for dm in re.finditer(r'(20\d{2}[-/年]\d{1,2}(?:[-/月]\d{1,2})?)[^<>]*?(?:日|度|期)?', ctx):
            print(f"  附近日期: {dm.group(0)[:40]}")
    
    # 找所有包含年份的文本
    for m in re.finditer(r'(20\d{2})年(?:一季|二季|三季|四季|中|半年|年报|度)', html):
        print(f"  期次: {m.group(0)}")
    
    # 找页面中间的财务指标表格标题（可能包含日期）
    for m in re.finditer(r'<td[^>]*>\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})\s*</td>', html):
        print(f"  表格日期: {m.group(1)}")
    
    # 新的尝试：找div class="content"或类似区域
    for m in re.finditer(r'<div[^>]*class="(?:content|main|con)\b[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL):
        text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        if '财务' in text and len(text) < 200:
            print(f"  content div: {text[:100]}")

print("\n=== done ===")
