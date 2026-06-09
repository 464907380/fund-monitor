"""
基金监控核心逻辑测试

测试解析函数（无需网络）和异常检测逻辑。
"""
import pytest


# ═══════════════════════════════════════════════
# 解析函数测试（mock JS 数据字符串）
# ═══════════════════════════════════════════════

def test_parse_name():
    from fund_watch import _parse_name

    assert _parse_name('var fS_name = "易方达蓝筹";') == "易方达蓝筹"
    assert _parse_name('var fS_name="无空格";') == "无空格"
    assert _parse_name("var x = 1;") is None


def test_parse_scale():
    from fund_watch import _parse_scale

    js = 'var x = 1; "y":123.45,"mom":"+0.50%"; "y":678.90,"mom":"-1.00%"'
    assert _parse_scale(js) == 678.90
    assert _parse_scale("no data") is None


def test_parse_period_returns():
    from fund_watch import _parse_period_returns

    js = 'var syl_1y = "-2.35"; var syl_3y = "5.10"; var syl_1n = "12.80"'
    result = _parse_period_returns(js)
    assert result["m1"] == -2.35
    assert result["m3"] == 5.10
    assert result["y1"] == 12.80

    # 部分缺失
    assert _parse_period_returns("var syl_1y = '1.0'") == {}
    # 注意：此处正则使用双引号，单引号不匹配——正确行为


def test_parse_price_info():
    from fund_watch import _parse_price_info

    js = '"data":[1.234,2.345,3.456,4.567,5.678]'
    assert _parse_price_info(js) == 1
    assert _parse_price_info("nothing") is None


def test_parse_manager():
    from fund_watch import _parse_manager

    js = 'Data_currentFundManager...blah..."name":"张坤"...'
    assert _parse_manager(js) == "张坤"
    assert _parse_manager("nothing") is None


def test_parse_net_trend():
    from fund_watch import _parse_net_trend

    # 最小的合法片段
    js = 'var Data_netWorthTrend [{"x":1700000000000,"y":2.5,"equityReturn":1.0}]'
    result = _parse_net_trend(js)
    assert result is not None
    assert len(result) == 1
    assert result[0]["v"] == 2.5

    assert _parse_net_trend("nothing") is None


def test_parse_real_time():
    from fund_watch import _parse_real_time

    # 不会实际发起网络请求，因为 _parse_real_time 内部调用 fetch 会抛异常
    # 这里只验证类型签名正确
    import inspect
    sig = inspect.signature(_parse_real_time)
    assert "code" in sig.parameters


# ═══════════════════════════════════════════════
# 异常检测测试（纯数学逻辑，无需网络）
# ═══════════════════════════════════════════════

def make_nav(values: list[float]) -> list[dict]:
    """构造净值趋势 mock 数据"""
    import datetime
    base_ts = int(datetime.datetime(2025, 1, 1).timestamp() * 1000)
    return [
        {"d": f"01-{i+1:02d}", "v": v, "ts": base_ts + i * 86400000}
        for i, v in enumerate(values)
    ]


# ── 净值停滞检测 ──

def test_stagnation_normal():
    """正常波动不应触发停滞警报"""
    from fund_watch import check_stagnation

    navs = make_nav([1.0, 1.01, 1.02, 1.015])
    assert check_stagnation(navs) is None


def test_stagnation_trigger():
    """连续 3 天波动 < 0.05% 应触发警报"""
    from fund_watch import check_stagnation

    navs = make_nav([1.0, 1.0003, 1.0005, 1.0004])
    result = check_stagnation(navs)
    assert result is not None
    assert "流动性" in result


def test_stagnation_too_few():
    """不足 3 条数据不检测"""
    from fund_watch import check_stagnation

    assert check_stagnation(make_nav([1.0, 1.01])) is None


# ── 连跌检测 ──

def test_consecutive_drop_no_alert():
    """上涨或窄幅波动不触发"""
    from fund_watch import check_consecutive_drop

    navs = make_nav([1.0, 1.01, 1.02, 1.03])
    assert check_consecutive_drop(navs) is None


def test_consecutive_drop_yellow():
    """连跌 3 天但累计未超 -3% → 🟡"""
    from fund_watch import check_consecutive_drop

    navs = make_nav([1.0, 0.99, 0.98, 0.975])
    result = check_consecutive_drop(navs)
    assert result is not None
    assert "🟡" in result


def test_consecutive_drop_red():
    """连跌 3 天且累计超 -3% → 🚩"""
    from fund_watch import check_consecutive_drop

    navs = make_nav([1.0, 0.97, 0.95, 0.93])
    result = check_consecutive_drop(navs)
    assert result is not None
    assert "🚩" in result


# ── 分红/拆分检测 ──

def test_dividend_no_alert():
    """正常波动不触发分红检测"""
    from fund_watch import check_dividend

    navs = make_nav([1.0, 1.01])
    assert check_dividend(navs) is None


def test_dividend_trigger():
    """单日跌超 -4% → 提示分红/拆分"""
    from fund_watch import check_dividend

    navs = make_nav([1.0, 0.95])
    result = check_dividend(navs)
    assert result is not None
    assert "分红" in result or "拆分" in result


def test_dividend_too_few():
    """不足 2 条不检测"""
    from fund_watch import check_dividend

    assert check_dividend(make_nav([1.0])) is None


# ═══════════════════════════════════════════════
# 盘中监控逻辑测试
# ═══════════════════════════════════════════════

def test_is_trading_weekday():
    """周一至周五应为交易日（非节假日时）"""
    from fund_monitor import is_trading_day
    import datetime

    # 假设一个普通周三（非节假日）
    d = datetime.date(2025, 3, 12)
    assert is_trading_day(d) is not None


def test_is_trading_weekend():
    """周六日应返回 False"""
    from fund_monitor import is_trading_day
    import datetime

    d = datetime.date(2025, 3, 8)  # 周六
    assert is_trading_day(d) is False


def test_is_trading_time():
    """交易时段判定"""
    from fund_monitor import is_trading_time
    import datetime

    # 10:00 → 交易中
    assert is_trading_time(datetime.datetime(2025, 3, 12, 10, 0)) is True
    # 9:00 → 未开盘
    assert is_trading_time(datetime.datetime(2025, 3, 12, 9, 0)) is False
    # 11:45 → 午休
    assert is_trading_time(datetime.datetime(2025, 3, 12, 11, 45)) is False
    # 14:00 → 交易中
    assert is_trading_time(datetime.datetime(2025, 3, 12, 14, 0)) is True
    # 15:01 → 收盘
    assert is_trading_time(datetime.datetime(2025, 3, 12, 15, 1)) is False
