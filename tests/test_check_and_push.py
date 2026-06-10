"""
测试核心检查逻辑 check() 和推送模块
所有网络调用通过 mock 隔离
"""
import json
from unittest.mock import patch, MagicMock
import urllib.error
import pytest


# ── check() 测试 ──────────────────────────────

@pytest.fixture
def mock_get():
    """mock fund_watch.get() 返回标准测试数据"""
    with patch("fund_watch.get") as m:
        m.return_value = {
            "n": "测试基金",
            "code": "001234",
            "mgr": "张三",
            "m1": -2.0,
            "m3": 5.0,
            "y1": 10.0,
            "td": -0.5,
            "sc": 30.0,
            "nav": [
                {"v": 1.0, "d": "2026-01-01"}, {"v": 1.02, "d": "2026-01-02"},
                {"v": 1.01, "d": "2026-01-03"}, {"v": 1.03, "d": "2026-01-04"},
                {"v": 1.02, "d": "2026-01-05"}, {"v": 1.04, "d": "2026-01-06"},
                {"v": 1.03, "d": "2026-01-07"},
            ],
            "rank": 50, "rank_total": 1000,
        }
        yield m


@pytest.fixture
def mock_hist():
    """mock fund_watch.load_hist() 返回标准历史"""
    with patch("fund_watch.load_hist") as m:
        m.return_value = {"m": "", "s": 0.0}
        yield m


def test_check_normal(mock_get, mock_hist):
    """正常数据不产生警报"""
    from fund_watch import check
    row, alerts = check("001234")
    assert len(alerts) == 0
    assert row["code"] == "001234"
    assert row["name"] == "测试基金"
    assert row["day"] == "-0.50%"


def test_check_manager_change(mock_get, mock_hist):
    """经理变更触发警报"""
    mock_hist.return_value = {"m": "李四", "s": 30.0}
    from fund_watch import check
    row, alerts = check("001234")
    assert any("经理" in a for a in alerts)


def test_check_scale_double(mock_get, mock_hist):
    """规模翻倍触发警报"""
    mock_hist.return_value = {"m": "张三", "s": 10.0}
    from fund_watch import check
    row, alerts = check("001234")
    assert any("规模翻倍" in a for a in alerts)


def test_check_monthly_drop_red(mock_get, mock_hist):
    """近月亏损超过红色阈值触发警报"""
    mock_get.return_value["m1"] = -16.0
    from fund_watch import check
    row, alerts = check("001234")
    assert any("近一月亏" in a for a in alerts)


def test_check_monthly_drop_yellow(mock_get, mock_hist):
    """近月亏损超过黄色阈值触发警报"""
    mock_get.return_value["m1"] = -11.0
    from fund_watch import check
    row, alerts = check("001234")
    # ALERT_DROP_1M 默认 -10，-11 应触发
    assert any("近一月亏" in a for a in alerts)


def test_check_scale_normal(mock_get, mock_hist):
    """规模正常增长不触发警报"""
    mock_hist.return_value = {"m": "", "s": 25.0}
    from fund_watch import check
    row, alerts = check("001234")
    scale_alerts = [a for a in alerts if "规模" in a]
    assert len(scale_alerts) == 0


# ── 推送模块测试 ──────────────────────────────

@patch("fund_utils.urllib.request.urlopen")
def test_send_wechat_success(mock_urlopen):
    """企业微信推送成功"""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"ok"
    mock_urlopen.return_value = mock_resp
    from fund_utils import send_wechat
    with patch.dict("os.environ", {"WECHAT_WEBHOOK": "https://qyapi.weixin.qq.com/hook"}):
        result = send_wechat("测试消息")
    assert result is True
    mock_urlopen.assert_called_once()


@patch("fund_utils.urllib.request.urlopen")
def test_send_wechat_no_webhook(mock_urlopen):
    """未配置 webhook 时直接返回 False"""
    from fund_utils import send_wechat
    with patch.dict("os.environ", {"WECHAT_WEBHOOK": ""}):
        result = send_wechat("测试消息")
    assert result is False
    mock_urlopen.assert_not_called()


@patch("fund_utils.urllib.request.urlopen")
def test_send_wechat_failure(mock_urlopen):
    """网络错误时返回 False"""
    mock_urlopen.side_effect = urllib.error.URLError("timeout")
    from fund_utils import send_wechat
    with patch.dict("os.environ", {"WECHAT_WEBHOOK": "https://qyapi.weixin.qq.com/hook"}):
        result = send_wechat("测试消息")
    assert result is False


@patch("fund_utils.smtplib.SMTP_SSL")
def test_send_smtp_success(mock_smtp):
    """SMTP 邮件发送成功"""
    from fund_utils import _send_smtp, send_mail
    from email.mime.text import MIMEText
    with patch.dict("os.environ", {"QQ_EMAIL": "test@qq.com", "QQ_MAIL_AUTH": "abc123"}):
        msg = MIMEText("test", "plain", "utf-8")
        _send_smtp(msg)
    mock_smtp.assert_called_once_with("smtp.qq.com", 465, timeout=10)


@patch("fund_utils.urllib.request.urlopen")
def test_send_mail_no_creds(mock_urlopen):
    """未配置邮件凭证时跳过"""
    from fund_utils import send_mail
    with patch.dict("os.environ", {"QQ_EMAIL": "", "QQ_MAIL_AUTH": ""}):
        send_mail("subject", "body")
    mock_urlopen.assert_not_called()


@patch("fund_utils.smtplib.SMTP_SSL")
def test_send_smtp_sendmail_failure(mock_smtp):
    """发送邮件失败应记录日志但不抛异常"""
    from fund_utils import _send_smtp
    from email.mime.text import MIMEText
    instance = MagicMock()
    instance.sendmail.side_effect = Exception("SMTP error")
    mock_smtp.return_value = instance
    with patch.dict("os.environ", {"QQ_EMAIL": "test@qq.com", "QQ_MAIL_AUTH": "abc123"}):
        _send_smtp(MIMEText("test", "plain", "utf-8"))
    # 不应抛异常


# ── 关键解析函数补充测试 ──────────────────────

def test_parse_institutional_ratio():
    """提取机构持有比例（取最后一个值）"""
    from fund_watch import _parse_institutional_ratio
    # 模拟天天基金 JS 中逗号分隔的数值列表
    data = '''"机构持有比例","data":[45.2,48.5]}]}'''
    result = _parse_institutional_ratio(data)
    assert result == 48.5


def test_parse_institutional_ratio_none():
    """无机构持有数据"""
    from fund_watch import _parse_institutional_ratio
    data = '{"other":"data"}'
    result = _parse_institutional_ratio(data)
    assert result is None


def test_parse_syl_6y():
    """提取近6月收益率"""
    from fund_watch import _parse_syl_6y
    data = '''syl_6y="25.36"'''
    result = _parse_syl_6y(data)
    assert result == 25.36


def test_parse_syl_6y_none():
    """无近6月收益率"""
    from fund_watch import _parse_syl_6y
    data = 'var other="1.0"'
    result = _parse_syl_6y(data)
    assert result is None
