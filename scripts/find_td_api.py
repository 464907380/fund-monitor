import urllib.request, re, json, time as _time

code = '002910'

# Try crawling the fund detail page for the JS that loads gsz
url1 = 'https://fund.eastmoney.com/' + code + '.html'
r = urllib.request.urlopen(urllib.request.Request(url1, headers={'User-Agent':'Mozilla/5.0','Referer':'https://fund.eastmoney.com/'}), timeout=10)
t = r.read().decode('utf-8')

# Find gsz-related things in the page
for m in re.finditer(r'gz_gsz|fundgz|gszzl|gszdata|jjgz|estimate', t):
    start = max(0, m.start()-60)
    end = min(len(t), m.end()+120)
    print('Context:', t[start:end].replace('\n',' '))
    print()
    break

# Find API URLs in the page
for m in re.finditer(r'(https?://[^\"\'<> ]+(?:gsz|jjgz)[^\"\'<> ]*)', t):
    print('Found gsz URL:', m.group(1))
    break

# Also check the fund.eastmoney.com JS bundle for how gsz is loaded
# Common pattern: they use a data API with callback
for m in re.finditer(r'(https?://[^\"\'<> ]+data[^\"\'<> ]*[Ee]ast[mM]oney[^\"\'<> ]*)', t):
    print('Found data URL:', m.group(1)[:150])
    break
