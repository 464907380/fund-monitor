import urllib.request, re

code = '002910'
today = '2026-07-21'

# LSJZ
url = 'https://api.fund.eastmoney.com/f10/lsjz?callback=j&fundCode=' + code + '&pageIndex=1&pageSize=1'
r = urllib.request.urlopen(urllib.request.Request(url, headers={'Referer':'https://fund.eastmoney.com/','User-Agent':'Mozilla/5.0'}), timeout=10)
t = r.read().decode('utf-8')
print('=== LSJZ ===')
print(t[:400])
m_date = re.search(r'FSRQ":"(\d{4}-\d{2}-\d{2})"', t)
m_val = re.search(r'"JZZZL":"([-+\d.]+)"', t)
if m_date and m_val:
    print('Date: ' + m_date.group(1) + ', Change: ' + m_val.group(1) + '%, Is today: ' + str(m_date.group(1) == today))

# Sina
print('\n=== Sina ===')
url2 = 'https://hq.sinajs.cn/list=of' + code
r2 = urllib.request.urlopen(urllib.request.Request(url2, headers={'User-Agent':'Mozilla/5.0','Referer':'https://finance.sina.com.cn'}), timeout=10)
t2 = r2.read().decode('gbk')
print(t2[:200])
m2 = re.search(r'"([^,]*),([^,]+),([^,]+),([^,]+),([^,]+),([^,]+)"', t2)
if m2:
    print('Name: ' + m2.group(1))
    print('Field2 (nav?): ' + m2.group(2))
    print('Field3 (acc_nav?): ' + m2.group(3))
    print('Field4 (est_nav?): ' + m2.group(4))
    print('Field5 (change%): ' + m2.group(5))
    print('Field6 (date): ' + m2.group(6))
