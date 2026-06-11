"""
测试 fund_monitor.py 核心路径
所有网络调用通过 mock 隔离
"""
from unittest.mock import patch, MagicMock


# ── check_intraday 测试 ──────────────────────

def _make_gzjs(code: str, name: str, gszzl: str) -> str:
    """模拟天天基金实时估值 JS 响应"""
    return f'jsonpgz({{"fundcode":"{code}","name":"{name}","gszzl":"{gszzl}"}});'


@patch("fund_monitor.fetch")
def test_check_intraday_first_call(mock_fetch):
    """首次调用应初始化状态，返回空警报"""
    mock_fetch.return_value = _make_gzjs("001234", "测试基金", "-0.50")
    from fund_monitor import check_intraday
    state: dict = {}
    alerts = check_intraday("001234", state)
    assert len(alerts) == 0
    assert state["first_td"] == -0.50
    assert state["last_td"] == -0.50


@patch("fund_monitor.fetch")
def test_check_intraday_drop_red(mock_fetch):
    """跌幅超过红色阈值触发急跌警报"""
    from fund_monitor import check_intraday
    state = {"first_td": 0.0, "last_td": 0.0, "name": "测试基金"}
    mock_fetch.return_value = _make_gzjs("001234", "测试基金", "-4.00")
    alerts = check_intraday("001234", state)
    assert any("急跌" in a for a in alerts), f"应触发急跌警报，实际: {alerts}"


@patch("fund_monitor.fetch")
def test_check_intraday_drop_yellow(mock_fetch):
    """跌幅超过黄色阈值触发下跌警报"""
    from fund_monitor import check_intraday
    state = {"first_td": 0.0, "last_td": 0.0, "name": "测试基金"}
    mock_fetch.return_value = _make_gzjs("001234", "测试基金", "-2.50")
    alerts = check_intraday("001234", state)
    assert any("下跌" in a for a in alerts)


@patch("fund_monitor.fetch")
def test_check_intraday_jump_red(mock_fetch):
    """涨幅超过红色阈值触发急涨警报"""
    from fund_monitor import check_intraday
    state = {"first_td": 0.0, "last_td": 0.0, "name": "测试基金"}
    mock_fetch.return_value = _make_gzjs("001234", "测试基金", "4.00")
    alerts = check_intraday("001234", state)
    assert any("急涨" in a for a in alerts)


@patch("fund_monitor.fetch")
def test_check_intraday_accum_drop_red(mock_fetch):
    """当日累计跌幅超过红色阈值"""
    from fund_monitor import check_intraday
    state = {"first_td": 0.0, "last_td": -1.0, "name": "测试基金"}
    mock_fetch.return_value = _make_gzjs("001234", "测试基金", "-8.00")
    alerts = check_intraday("001234", state)
    assert any("累计跌" in a for a in alerts)


@patch("fund_monitor.fetch")
def test_check_intraday_no_alert(mock_fetch):
    """小幅波动不触发警报"""
    from fund_monitor import check_intraday
    state = {"first_td": 0.0, "last_td": 0.0, "name": "测试基金"}
    mock_fetch.return_value = _make_gzjs("001234", "测试基金", "+0.50")
    alerts = check_intraday("001234", state)
    assert len(alerts) == 0


@patch("fund_monitor.fetch")
def test_check_intraday_invalid_data(mock_fetch):
    """非法数据应返回空列表"""
    from fund_monitor import check_intraday
    state = {"first_td": 0.0, "last_td": 0.0}
    mock_fetch.return_value = "not valid js"
    alerts = check_intraday("001234", state)
    assert alerts == []


# ── _fetch_stock_change 测试 ─────────────────

@patch("fund_monitor._fetch_stock_change")
def test_fetch_stock_change_normal(mock_fetch):
    """正常获取个股涨跌幅"""
    mock_fetch.return_value = ("贵州茅台", 1.25)
    from fund_monitor import _fetch_stock_change
    result = _fetch_stock_change("sh600519")
    assert result is not None
    assert result[0] == "贵州茅台"
    assert result[1] == 1.25


# ── push_alert 测试 ────────────────────────────

@patch("fund_monitor.send_wechat")
@patch("fund_monitor.WECHAT_WEBHOOK", "https://qyapi.weixin.qq.com/hook")
def test_push_alert(mock_send):
    """推送单条警报"""
    from fund_monitor import push_alert
    mock_send.return_value = True
    push_alert(["🚩 测试警报"])
    mock_send.assert_called_once()


@patch("fund_monitor.send_wechat")
def test_push_alert_empty(mock_send):
    """空警报列表不应推送"""
    from fund_monitor import push_alert
    push_alert([])
    mock_send.assert_not_called()




# ── is_trading_day / is_trading_time 补充边界测试 ──

def test_is_trading_time_before_open():
    """开盘前不是交易时间"""
    from fund_monitor import is_trading_time
    import datetime
    assert not is_trading_time(datetime.datetime(2026, 6, 10, 9, 0))


def test_is_trading_time_during():
    """盘中是交易时间"""
    from fund_monitor import is_trading_time
    import datetime
    assert is_trading_time(datetime.datetime(2026, 6, 10, 10, 30))


def test_is_trading_time_lunch():
    """午休不是交易时间"""
    from fund_monitor import is_trading_time
    import datetime
    assert not is_trading_time(datetime.datetime(2026, 6, 10, 11, 45))


def test_is_trading_time_after_close():
    """收盘后不是交易时间"""
    from fund_monitor import is_trading_time
    import datetime
    assert not is_trading_time(datetime.datetime(2026, 6, 10, 15, 30))
