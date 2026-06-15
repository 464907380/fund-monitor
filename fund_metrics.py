"""
基金净值指标计算模块
"""
import math
from typing import Callable


def _calc_nav_metrics(full_nav: list[dict]) -> dict:
    """
    从完整净值列表计算风险指标（单趟扫描优化版）。
    同时计算日收益率、波动率、最大回撤、胜率、盈亏比、连跌天数。
    """
    if not full_nav or len(full_nav) < 30:
        return {}
    prices = [n["v"] for n in full_nav]
    days = len(prices)
    n = days - 1
    if n < 1:
        return {}

    # 单趟扫描：日收益率 + 均值 + 方差 + 最大回撤 + 胜率 + 盈亏 + 连跌
    sum_r = 0.0
    sum_sq = 0.0
    sum_pos = 0.0
    sum_neg = 0.0
    count_pos = 0
    count_neg = 0
    cur_loss = 0
    max_loss_days = 0
    peak = prices[0]
    max_dd = 0.0

    for i in range(1, days):
        r = (prices[i] - prices[i-1]) / prices[i-1]
        sum_r += r
        sum_sq += r * r
        if r > 0:
            sum_pos += r
            count_pos += 1
            cur_loss = 0
        elif r < 0:
            sum_neg += r
            count_neg += 1
            cur_loss += 1
            if cur_loss > max_loss_days:
                max_loss_days = cur_loss
        if prices[i] > peak:
            peak = prices[i]
        dd = (peak - prices[i]) / peak * 100
        if dd > max_dd:
            max_dd = dd

    mean_r = sum_r / n
    variance = sum_sq / n - mean_r * mean_r
    total_return = (prices[-1] - prices[0]) / prices[0]
    annual_return = ((1 + total_return) ** (250 / days) - 1) * 100
    volatility = math.sqrt(max(variance, 0) * 250) * 100

    # 下行波动率（只算负收益日）
    if count_neg > 1:
        neg_r = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, days) if prices[i] < prices[i-1]]
        down_var = sum((r_neg - mean_r) ** 2 for r_neg in neg_r) / count_neg
        down_dev = math.sqrt(down_var * 250) * 100
    else:
        down_dev = volatility

    rf = 2.5
    sharpe = (annual_return - rf) / volatility if volatility > 0 else 0
    sortino = (annual_return - rf) / down_dev if down_dev > 0 else 0
    calmar = annual_return / max_dd if max_dd > 0 else 0
    win_rate = count_pos / n * 100
    avg_win = sum_pos / count_pos if count_pos > 0 else 0
    avg_loss = abs(sum_neg / count_neg) if count_neg > 0 else 1
    profit_ratio = avg_win / avg_loss if avg_loss > 0 else 0
    total_return_pct = total_return * 100
    recovery = abs(total_return_pct / max_dd) if max_dd > 0 else 0

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
