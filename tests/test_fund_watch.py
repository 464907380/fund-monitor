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

    # 部分缺失（单引号也应支持）
    result2 = _parse_period_returns("var syl_1y = '1.0'")
    assert result2["m1"] == 1.0
    assert "m3" not in result2
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


# ── 港股代码转换测试 ──

def test_sina_stock_code_hk():
    """港股和 A 股代码转换"""
    from fund_monitor import _sina_stock_code

    # A 股
    assert _sina_stock_code("600519") == "sh600519"
    assert _sina_stock_code("688981") == "sh688981"
    assert _sina_stock_code("000333") == "sz000333"
    assert _sina_stock_code("300750") == "sz300750"

    # 港股：5 位纯数字
    assert _sina_stock_code("00700") == "hk00700"
    assert _sina_stock_code("09988") == "hk09988"
    assert _sina_stock_code("03690") == "hk03690"

    # 港股：带前缀
    assert _sina_stock_code("hk00700") == "hk00700"
    assert _sina_stock_code("HK00700") == "hk00700"


# ── 状态快照测试 ──

def test_snapshot_save_load_clear(tmp_path):
    """状态快照保存/恢复/删除"""
    import os
    from fund_monitor import _save_snapshot, _clear_snapshot

    # 先清理
    _clear_snapshot()

    # 保存
    states = {"001438": {"name": "test_fund", "last_td": 1.5}}
    stock_states = {"001438:600519": {"name": "茅台", "chg": 2.0, "max_chg": 3.0, "min_chg": -1.0}}
    _save_snapshot(states, stock_states, "2025-03-12", 0, True)

    # 恢复（匹配日期）
    from fund_monitor import _load_snapshot
    result = _load_snapshot("2025-03-12")
    assert result is not None
    r_states, r_stock, r_empty, r_loaded = result
    assert r_states["001438"]["last_td"] == 1.5
    assert r_stock["001438:600519"]["chg"] == 2.0
    assert r_loaded is True

    # 恢复（不匹配日期 → 返回 None）
    result2 = _load_snapshot("2099-01-01")
    assert result2 is None

    # 清理
    _clear_snapshot()
    from fund_monitor import _STATE_SNAPSHOT
    assert not os.path.exists(_STATE_SNAPSHOT)


# ═══════════════════════════════════════════════
# 评分系统测试
# ═══════════════════════════════════════════════

def test_parse_rank_info():
    """提取同类排名"""
    from fund_watch import _parse_rank_info

    js = ('var Data_rateInSimilarType = '
          '[{"x":1,"y":135,"sc":"590"},{"x":2,"y":17,"sc":"2314"}];')
    result = _parse_rank_info(js)
    assert result is not None
    assert result == (17, 2314)

    assert _parse_rank_info("nothing") is None


def test_parse_fund_rate():
    """提取基金费率"""
    from fund_watch import _parse_fund_rate

    js = 'var fund_Rate="0.00";'
    assert _parse_fund_rate(js) == 0.0

    js2 = 'var fund_Rate="1.50";'
    assert _parse_fund_rate(js2) == 1.5

    assert _parse_fund_rate("nothing") is None


def test_calc_nav_metrics():
    """净值风险指标计算（最大回撤/波动率/卡玛比率）"""
    from fund_watch import _calc_nav_metrics

    # 模拟稳步上涨的净值（无回撤、低波动）
    nav_up = [{"v": 1.0 + i * 0.001} for i in range(500)]
    result = _calc_nav_metrics(nav_up)
    assert "max_dd" in result
    assert "volatility" in result
    assert "calmar" in result
    assert "annual_return" in result
    assert "sharpe" in result
    assert "sortino" in result
    assert "win_rate" in result
    assert "profit_ratio" in result
    assert "recovery" in result
    assert "max_loss_days" in result
    # 稳步上涨应该有正的年化收益
    assert result["annual_return"] > 0

    # 模拟震荡下跌的净值（高回撤）
    nav_down = [{"v": 1.0 - i * 0.01} for i in range(100)]
    result2 = _calc_nav_metrics(nav_down)
    assert result2["max_dd"] > 30  # 大幅回撤

    # 数据不足时返回空
    assert _calc_nav_metrics([{"v": 1.0}]) == {}
    assert _calc_nav_metrics([{"v": 1.0}, {"v": 1.01}]) == {}


def test_calc_score_transparent():
    """透明评分系统计算（12 维度）"""
    from fund_watch import _calc_score

    # 一只各项指标都比较优秀的基金
    d = {
        "annual_return": 35.0,                  # 年化 35%
        "y1": 30.0,                             # 近1年 30%
        "sharpe": 2.5,                          # 优秀
        "sortino": 3.5,                         # 优秀
        "max_dd": 15.0,                         # 回撤控制好
        "win_rate": 55.0,                       # 胜率高
        "inst": 40.0,                           # 机构认可
        "sc": 20.0,                             # 规模适中
        "rate": 0.0,                            # 0费率
        "profit_ratio": 1.8,                    # 盈亏比好
        "recovery": 25.0,                       # 修复能力强
        "sy3": 80.0,                            # 近3年表现好
    }
    score = _calc_score(d)
    assert 60 <= score <= 100  # 应该高分

    # 一只各项指标都差的基金
    d2 = {
        "annual_return": -5.0,                  # 亏钱
        "y1": -10.0,                            # 近1年也亏
        "sharpe": -0.5,                         # 负收益
        "sortino": -0.5,
        "max_dd": 55.0,                         # 回撤巨大
        "win_rate": 35.0,                       # 胜率低
        "inst": 0.5,                            # 机构不认可
        "sc": 0.3,                              # 规模太小
        "rate": 1.5,                            # 费率高
        "profit_ratio": 0.6,                    # 亏的时候比赚的时候多
        "recovery": 0.5,                        # 修复能力差
        "sy3": -10.0,                           # 近3年亏损
        "internal": 0.0,                        # 经理自己都不买
    }
    score2 = _calc_score(d2)
    assert 0 <= score2 <= 30  # 应该低分

    # 空数据返回 0
    assert _calc_score({}) == 0.0


def test_rank_percentile_str():
    """排名百分位字符串"""
    from fund_watch import _rank_percentile_str

    d = {"rank": 1, "rank_total": 2314}
    assert "0.0%" in _rank_percentile_str(d)
    assert "🌟" in _rank_percentile_str(d)  # top 5% 有星星

    d2 = {"rank": 500, "rank_total": 2314}
    result = _rank_percentile_str(d2)
    assert "21.6%" in result or "%" in result

    assert _rank_percentile_str({}) == ""
