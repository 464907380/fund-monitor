import urllib.request, json, re

code = '002910'

# 1. LSJZ API (历史净值 - 昨天收盘)
url = 'https://api.fund.eastmoney.com/f10/lsjz?callback=j&fundCode=' + code + '&pageIndex=1&pageSize=1'
r = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0','Referer':'https://fund.eastmoney.com/'}), timeout=10)
text = r.read().decode('utf-8')
print('1. LSJZ:', text[:300])

# 2. pingzhongdata (包含实时估值)
url2 = 'https://fund.eastmoney.com/pingzhongdata/' + code + '.js'
r2 = urllib.request.urlopen(urllib.request.Request(url2, headers={'User-Agent':'Mozilla/5.0','Referer':'https://fund.eastmoney.com/'}), timeout=10)
text2 = r2.read().decode('utf-8')
m_gsz = re.search(r'var\s+gsz\s*=\s*[\"\']?([^\"\';]+)', text2)
m_gszzl = re.search(r'var\s+gszzl\s*=\s*[\"\']?([^\"\';]+)', text2)
print('2. pingzhongdata gsz:', m_gsz.group(1)[:80] if m_gsz else 'not found')
print('   pingzhongdata gszzl:', m_gszzl.group(1)[:80] if m_gszzl else 'not found')

# 3. fundgz (老接口)
url3 = 'https://fundgz.1234567.com.cn/js/' + code + '.js'
try:
    r3 = urllib.request.urlopen(urllib.request.Request(url3, headers={'User-Agent':'Mozilla/5.0','Referer':'https://fund.eastmoney.com/'}), timeout=10)
    text3 = r3.read().decode('utf-8')
    print('3. fundgz:', text3[:200])
except Exception as e:
    print('3. fundgz error:', e)

# 4. 新浪
url4 = 'https://hq.sinajs.cn/list=of' + code
r4 = urllib.request.urlopen(urllib.request.Request(url4, headers={'User-Agent':'Mozilla/5.0','Referer':'https://finance.sina.com.cn'}), timeout=10)
text4 = r4.read().decode('gbk')
print('4. Sina:', text4[:200])
