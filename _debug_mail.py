import sys, os, re
sys.path.insert(0, ".")
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from fund_utils import _strip_html, send_mail_html
from fund_monitor import push_alert

# 模拟上次的测试内容
push_alert(
    ["\U0001F534 <font color=\"warning\">招商中证白酒(161725) 急跌 -3.2%（当前-0.11%）</font>",
     "\U0001F7E2 <font color=\"info\">诺安成长混合(320007) 急涨 +5.1%（当前+6.20%）</font>"],
    [],
    {"招商中证白酒": ("161725", []),
     "诺安成长混合": ("320007", [])}
)
print("done")
