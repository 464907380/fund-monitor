"""检查FGL页面中的报告期信息"""
import urllib.request as r
import re

code = "600519"
url = f"https://vip.stock.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine/stockid/{code}/displaytype/4.phtml"
html = r.urlopen(r.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10).read().decode("gbk", "ignore")

# 找到表格区域
print("=== 查找报告期 ===")
# 找"本期"所在的行或周围的文本
for m in re.finditer(r'(?:本期|上期|最新一期|报告期).{0,30}?(202\d)', html, re.DOTALL):
    print(f"  找到: {m.group(0)[:60]}")

# 找所有可见的年/季度文本
for m in re.finditer(r'202\d年(?:一季|二季|三季|四季|中|半年|年报|一季报|中报|三季报|年报)', html):
    print(f"  期次: {m.group(0)}")

# 找table中的第一个数据行，看列标题
# 提取表格部分
tables = re.findall(r'<table[^>]*>(.*?)</table>', html, re.DOTALL)
for i, tbl in enumerate(tables[:3]):
    ths = re.findall(r'<th[^>]*>(.*?)</th>', tbl, re.DOTALL)
    clean = [re.sub(r'<[^>]+>', '', t).strip() for t in ths]
    if clean:
        print(f"  表格{i}表头: {clean}")

# 找class=f14的文本，通常包含"财务指标"等相关信息
for m in re.finditer(r'class="f14"[^>]*>(.*?)</td>', html, re.DOTALL):
    txt = re.sub(r'<[^>]+>', '', m.group(1)).strip()
    if txt and len(txt) < 100:
        print(f"  f14文本: {txt}")

print("=== 页面标题 ===")
tm = re.search(r'<title>(.*?)</title>', html)
print(f"  {tm.group(1) if tm else 'none'}")
