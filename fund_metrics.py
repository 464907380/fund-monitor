"""
基金净值指标计算模块
"""
import math
from typing import Callable


def _calc_daily_returns(prices: list[float]) -> list[float]:
    """计算日收益率（小数形式）"""
    return [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]


def _calc_max_drawdown(prices: list[float]) -> float:
    """计算最大回撤（%）"""
    peak = prices[0]
    max_dd = 0.0
    for p in prices:
        if p > peak:
            peak = p
        dd = (peak - p) / peak * 100
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _calc_downside_deviation(daily_r: list[float], mean_r: float, n: int) -> float:
    """计算下行波动率"""
    neg_r = [r for r in daily_r if r < 0]
    if len(neg_r) > 1:
        down_var = sum((r - mean_r) ** 2 for r in neg_r) / len(neg_r)
        return float(math.sqrt(down_var * 250) * 100)
    return 0.0


def _calc_nav_metrics(full_nav: list[dict]) -> dict:
    """
    从完整净值列表计算风险指标。
    拆分为多个小函数以降低圈复杂度。
    """
    if not full_nav or len(full_nav) < 30:
        return {}
    prices = [n["v"] for n in full_nav]
    days = len(prices)

    daily_r = _calc_daily_returns(prices)
    n = len(daily_r)
    total_return = (prices[-1] - prices[0]) / prices[0]
    annual_return = ((1 + total_return) ** (250 / days) - 1) * 100

    # 波动率
    mean_r = sum(daily_r) / n
    variance = sum((r - mean_r) ** 2 for r in daily_r) / n
    volatility = math.sqrt(variance * 250) * 100

    # 最大回撤
    max_dd = _calc_max_drawdown(prices)
    calmar = annual_return / max_dd if max_dd > 0 else 0

    # 下行波动率
    down_dev = _calc_downside_deviation(daily_r, mean_r, n)
    if down_dev == 0:
        down_dev = volatility

    # 夏普比率 & 索提诺比率
    rf = 2.5
    sharpe = (annual_return - rf) / volatility if volatility > 0 else 0
    sortino = (annual_return - rf) / down_dev if down_dev > 0 else 0

    # 上行胜率
    win_rate = sum(1 for r in daily_r if r > 0) / n * 100

    # 盈亏比
    avg_win = sum(r for r in daily_r if r > 0) / max(sum(1 for r in daily_r if r > 0), 1)
    avg_loss = abs(sum(r for r in daily_r if r < 0) / max(sum(1 for r in daily_r if r < 0), 1))
    profit_ratio = avg_win / avg_loss if avg_loss > 0 else 0

    # 修复系数
    total_return_pct = total_return * 100
    recovery = abs(total_return_pct / max_dd) if max_dd > 0 else 0

    # 最长连续下跌天数
    max_loss_days = 0
    cur = 0
    for r in daily_r:
        if r < 0:
            cur += 1
            max_loss_days = max(max_loss_days, cur)
        else:
            cur = 0

    return {
        "annual_return": round(annual_return, 2),
        "volatility": round(volatility, 2),
        "max_dd": round(max_dd, 2),
        "calmar": round(calmar, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "win_rate": round(win_rate, 1),
        "profit_ratio": round(profit_ratio, 2),
        "recovery": round(recovery, 2),
        "max_loss_days": max_loss_days,
    }
