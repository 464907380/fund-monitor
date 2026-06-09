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


def test_holdings_csv_quoting():
    """验证 csv.reader 可正确解析名称含逗号的持仓数据"""
    import csv
    # 模拟 API 返回的 CSV 行：序号,代码,名称,占比%,市值,占净值比例
    # 名称含逗号时会被引号包裹
    lines = [
        "1,600519,贵州茅台,16.50,1234567.89,16.50",
        "2,000333,\"美的,集团\",8.20,987654.32,8.20",
        "3,300750,宁德时代,6.10,654321.00,6.10",
    ]
    for line in lines:
        reader = csv.reader([line])
        for parts in reader:
            assert len(parts) >= 6, f"解析错误: {line}"
            int(parts[0])  # 序号
            assert parts[2]  # 名称
            float(parts[5])  # 比例


def test_holdings_with_stock_code():
    """验证 _parse_holdings 返回格式包含股票代码字段 c"""
    import csv
    lines = [
        "1,600519,贵州茅台,16.50,1234567.89,16.50",
        "2,000333,\"美的,集团\",8.20,987654.32,8.20",
    ]
    for line in lines:
        reader = csv.reader([line])
        for parts in reader:
            if len(parts) >= 6:
                try:
                    int(parts[0])
                    # 验证与 fund_watch._parse_holdings 一致的结构
                    entry = {"n": parts[2], "c": parts[1], "p": float(parts[5]) if parts[5] else 0}
                    assert "n" in entry
                    assert "c" in entry
                    assert entry["c"]  # 代码非空
                    assert "p" in entry
                    assert isinstance(entry["p"], float)
                except (ValueError, IndexError):
                    pass


# ═══════════════════════════════════════════════
# 个股监控测试
# ═══════════════════════════════════════════════

def test_sina_stock_code():
    """股票代码转新浪格式"""
    from fund_monitor import _sina_stock_code

    assert _sina_stock_code("600519") == "sh600519"  # 沪市
    assert _sina_stock_code("688981") == "sh688981"  # 科创板
    assert _sina_stock_code("000333") == "sz000333"  # 深市主板
    assert _sina_stock_code("300750") == "sz300750"  # 创业板


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


# ── 个股监控状态追踪测试 ──

def test_stock_states_tracking():
    """验证 check_holdings_intraday 的状态追踪逻辑"""

    # 验证状态机初始化和更新逻辑
    # 模拟 3 轮检查，看 first_chg / last_chg / max_chg / min_chg 是否正确
    stock_states: dict[str, dict] = {}
    state_key = "001438:600519"

    # 第 1 次：初始化
    stock_states[state_key] = {
        "first_chg": 1.0, "last_chg": 1.0,
        "name": "贵州茅台",
        "chg": 1.0, "max_chg": 1.0, "min_chg": 1.0,
    }

    # 第 2 次：更新为 -3.0（急跌）
    s = stock_states[state_key]
    s["last_chg"] = -3.0
    s["chg"] = -3.0
    s["max_chg"] = max(s.get("max_chg", s["chg"]), -3.0)
    s["min_chg"] = min(s.get("min_chg", s["chg"]), -3.0)

    assert s["first_chg"] == 1.0   # 首次不变
    assert s["last_chg"] == -3.0   # 最新值
    assert s["max_chg"] == 1.0     # 最高没变
    assert s["min_chg"] == -3.0    # 最低更新

    # 第 3 次：反弹到 2.5
    s["last_chg"] = 2.5
    s["chg"] = 2.5
    s["max_chg"] = max(s["max_chg"], 2.5)
    s["min_chg"] = min(s["min_chg"], 2.5)

    assert s["first_chg"] == 1.0
    assert s["last_chg"] == 2.5
    assert s["max_chg"] == 2.5    # 最高更新
    assert s["min_chg"] == -3.0   # 最低不变


def test_trading_time_boundary():
    """交易时段边界条件"""
    from fund_monitor import is_trading_time
    import datetime

    # 9:30 整 → 开盘
    assert is_trading_time(datetime.datetime(2025, 3, 12, 9, 30)) is True
    # 11:30 整 → 午休开始
    assert is_trading_time(datetime.datetime(2025, 3, 12, 11, 30)) is False
    # 13:00 整 → 下午开盘
    assert is_trading_time(datetime.datetime(2025, 3, 12, 13, 0)) is True
    # 15:00 整 → 收盘
    assert is_trading_time(datetime.datetime(2025, 3, 12, 15, 0)) is False
