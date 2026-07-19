"""检查东财等数据源是否有更新的财务数据"""
import urllib.request as r
import json

# East Money 财务数据API - 尝试不同接口
urls = [
    # API 1: 主要财务数据
    "https://datacenter.eastmoney.com/securities/api/data/v1/get?reportName=RPT_F10_FINANCE_MAINFINADATA&columns=REPORT_DATE,BASIC_EPS,ROEJQ,OPR&filter=(SECUCODE=%22300502%22)&pageNumber=1&pageSize=5&sortTypes=-1&sortColumns=REPORT_DATE",
    # API 2: 财报披露日期
    "https://datacenter.eastmoney.com/securities/api/data/v1/get?reportName=RPT_F10_FINANCE_REPORTDATE&columns=REPORT_DATE,NOTICE_DATE,END_DATE&filter=(SECUCODE=%22300502%22)&pageNumber=1&pageSize=5&sortTypes=-1&sortColumns=END_DATE",
]

for i, url in enumerate(urls):
    try:
        resp = r.urlopen(r.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10).read().decode("utf-8")
        data = json.loads(resp)
        if data.get("result") and data["result"].get("data"):
            for item in data["result"]["data"]:
                print(f"API{i+1}: Report={item.get('REPORT_DATE','?')[:10] if item.get('REPORT_DATE') else '?'} EPS={item.get('BASIC_EPS','?')} ROE={item.get('ROEJQ','?')}")
        else:
            print(f"API{i+1}: No data - {resp[:200]}")
    except Exception as e:
        print(f"API{i+1}: Error - {e}")

# 也检查一下新浪FGL页面是否有显示不同期数的参数
print("\n--- 当前新浪FGL日期 ---")
import re
html = r.urlopen(r.Request("https://vip.stock.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine/stockid/300502/displaytype/4.phtml",
                           headers={"User-Agent": "Mozilla/5.0"}), timeout=10).read().decode("gbk", "ignore")
for m in re.finditer(r"报告日期.*?</td>\s*<td[^>]*>(\d{4}-\d{2}-\d{2})", html):
    print(f"  最新报告日期: {m.group(1)}")

# 检查是否有其他displaytype可显示更多数据
for dt in [2, 3, 5]:
    try:
        url2 = f"https://vip.stock.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine/stockid/300502/displaytype/{dt}.phtml"
        html2 = r.urlopen(r.Request(url2, headers={"User-Agent": "Mozilla/5.0"}), timeout=10).read().decode("gbk", "ignore")
        dates = re.findall(r"20\d{2}-\d{2}-\d{2}", html2)
        if dates:
            print(f"  displaytype={dt} dates: {list(set(dates))[:5]}")
    except:
        pass
