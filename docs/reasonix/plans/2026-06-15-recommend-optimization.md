# 运行推荐效率优化 — 实施计划

> **For agentic workers:** implement this plan task-by-task.

**目标：** 将全市场推荐总时间从 ~16 分钟减少到 ~5 分钟

**瓶颈分析（已验证）：**
- 200 只候选基金 × 每只 3 次 HTTP GET = 600 次请求
- `max_workers=5` 硬编码，网络 I/O 瓶颈下并发度不足
- 评分用不到实时估值（td）和持仓个股（holds），但 `get()` 每次都会拉取，浪费 2/3 的请求
- `_calc_nav_metrics` 对每只基金跑 10 趟 O(n) 纯 Python 循环

**优化策略：**
1. **减少每只基金的网络请求** — 新增 `get_scoring_data()` 跳过实时估值和持仓，3 次 → 1 次
2. **提高并发度** — `max_workers` 从 5 提高到 20
3. **优化排行拉取** — 改用 `fetch()` 走缓存，并行尝试多个 URL
4. **减少指标计算趟数** — `_calc_nav_metrics` 合并循环，单趟扫描完成所有指标

**Tech Stack:** Python 3.10+, ThreadPoolExecutor, urllib

---

### Task 1: 新增轻量级评分数据拉取函数

**Files:**
- Modify: `fund_watch.py:227-263` — 在 `get()` 旁新增 `get_scoring_data(code)`

- [ ] **Step 1: 在 `get()` 函数下方添加 `get_scoring_data()`**

```python
def get_scoring_data(code: str) -> dict:
    """拉取评分所需的最小数据集（跳过实时估值和持仓，减少网络请求）"""
    d: dict = {"code": code}
    data = fetch(api_url("fund_pingzhongdata", code=code))

    if name := _parse_name(data):
        d["n"] = name
    if sc := _parse_scale(data):
        d["sc"] = sc
    d.update(_parse_period_returns(data))
    if mgr := _parse_manager(data):
        d["mgr"] = mgr
    if inst := _parse_institutional_ratio(data):
        d["inst"] = inst
    if full_nav := _parse_full_nav(data):
        d["full_nav"] = full_nav
        d["nav"] = _parse_net_trend(data, full_nav)
        metrics = _calc_nav_metrics(full_nav)
        d.update(metrics)
        d["sy3"] = _calc_period_return(full_nav, 750)
        d["sy2"] = _calc_period_return(full_nav, 500)
    else:
        if nav := _parse_net_trend(data):
            d["nav"] = nav
    if rp := _parse_rank_info(data):
        d["rank"], d["rank_total"] = rp
    if rate := _parse_fund_rate(data):
        d["rate"] = rate
    d["sy6"] = _parse_syl_6y(data)
    return d
```

去掉的调用：`_parse_real_time`（实时估值）、`_parse_holdings`（持仓）。这些在评分中不需要。

- [ ] **Step 2: 在 fund_recommend.py 中将 `get()` 替换为 `get_scoring_data()`**

```python
# fund_recommend.py _score_one() 中
from fund_watch import get_scoring_data  # 新增导入

def _score_one(code: str, name: str) -> tuple | None:
    try:
        d = get_scoring_data(code)  # 原来是 get(code)
        ...
```

- [ ] **Step 3: 提交**

```bash
git add fund_watch.py fund_recommend.py
git commit -m "opt: 新增 get_scoring_data() 轻量评分函数，跳过实时估值和持仓请求"
```

---

### Task 2: 提高评分并发度

**Files:**
- Modify: `fund_recommend.py:244` — 修改 max_workers

- [ ] **Step 1: 将 `max_workers=5` 改为 `max_workers=20`**

```python
# fund_recommend.py:244
with ThreadPoolExecutor(max_workers=20) as executor:
```

理由：瓶颈在网络 I/O，20 个线程不会显著增加 CPU 争用。200 只基金 / 20 = 10 轮 batch，理想情况下提速 4 倍。

- [ ] **Step 2: 提交**

```bash
git add fund_recommend.py
git commit -m "opt: max_workers 从 5 提高到 20"
```

---

### Task 3: 优化排行拉取（走缓存 + 并行尝试）

**Files:**
- Modify: `fund_recommend.py:41-72` — `_fetch_rank_list()`

- [ ] **Step 1: 重构 `_fetch_rank_list()`，改用 `fetch()` 并并发尝试前 2 个 URL**

```python
def _fetch_rank_list(pn: int) -> list[list[str]]:
    """从天天基金排行 API 获取全市场基金排行（并发尝试多个 URL）"""
    sd = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
    ed = datetime.date.today().isoformat()
    urls = [
        api_url("fund_rank") + f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc=1yz&st=desc"
                              f"&sd={sd}&ed={ed}&pi=1&pn={pn}&dx=1",
        api_url("fund_rank") + f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc=1n&st=desc"
                              f"&sd={sd}&ed={ed}&pi=1&pn={pn}",
        "http://fund.eastmoney.com/data/rankhandler.aspx" + f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc=1yz&st=desc"
                              f"&sd={sd}&ed={ed}&pi=1&pn={pn}&dx=1",
        "http://fund.eastmoney.com/data/rankhandler.aspx" + f"?op=ph&dt=kf&ft=all&rs=&gs=0&sc=1n&st=desc"
                              f"&sd={sd}&ed={ed}&pi=1&pn={pn}",
    ]
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _try_url(url: str) -> list[list[str]] | None:
        try:
            data = fetch(url)  # 走 fetch() 缓存
            raw = data.replace("var rankData = ", "", 1).rstrip(";")
            raw_clean = re.sub(r'(\{|,)\s*(\w+)\s*:', lambda m: m.group(1) + '"' + m.group(2) + '":', raw)
            result = json.loads(raw_clean)
            rows = [row.split(",") for row in result.get("datas", [])]
            return rows if rows else None
        except Exception:
            return None

    # 先并发尝试前两个主流 URL
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {ex.submit(_try_url, url): url for url in urls[:2]}
        for f in as_completed(futures):
            rows = f.result()
            if rows:
                return rows

    # 降级尝试后两个备选（串行，较少触发）
    for url in urls[2:]:
        rows = _try_url(url)
        if rows:
            return rows
    return []
```

- [ ] **Step 2: 提交**

```bash
git add fund_recommend.py
git commit -m "opt: _fetch_rank_list 改用 fetch() + 并发URL尝试"
```

---

### Task 4: 合并指标计算循环

**Files:**
- Modify: `fund_metrics.py:35-101` — 将 10 趟独立循环合并为单趟扫描

- [ ] **Step 1: 重构 `_calc_nav_metrics()` 合并循环**

当前问题是：`_calc_daily_returns` 1 趟、`_calc_max_drawdown` 1 趟、`_calc_downside_deviation` 1 趟、方差 1 趟、上行胜率 1 趟等，约 10 趟扫描。

优化为单趟扫描同时计算所有指标：

```python
def _calc_nav_metrics(full_nav: list[dict]) -> dict:
    """从完整净值列表计算风险指标（单趟扫描优化版）"""
    if not full_nav or len(full_nav) < 30:
        return {}

    prices = [n["v"] for n in full_nav]
    days = len(prices)

    # 单趟扫描：同时计算日收益率、均值、方差、最大回撤、胜率、盈亏、连跌天数
    n = days - 1
    if n < 1:
        return {}

    daily_r = [0.0] * n
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
        daily_r[i-1] = r
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
            max_loss_days = max(max_loss_days, cur_loss)
        # 最大回撤
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

    # 下行波动率
    neg_r = [r for r in daily_r if r < 0]
    if len(neg_r) > 1:
        down_var = sum((r - mean_r) ** 2 for r in neg_r) / len(neg_r)
        down_dev = math.sqrt(down_var * 250) * 100
    else:
        down_dev = volatility

    # 夏普 & 索提诺
    rf = 2.5
    sharpe = (annual_return - rf) / volatility if volatility > 0 else 0
    sortino = (annual_return - rf) / down_dev if down_dev > 0 else 0

    # 胜率 & 盈亏比
    win_rate = count_pos / n * 100
    avg_win = sum_pos / count_pos if count_pos > 0 else 0
    avg_loss = abs(sum_neg / count_neg) if count_neg > 0 else 1
    profit_ratio = avg_win / avg_loss if avg_loss > 0 else 0

    # 卡玛 & 修复系数
    calmar = annual_return / max_dd if max_dd > 0 else 0
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
```

注意：删掉了已不再需要的 `_calc_daily_returns`、`_calc_max_drawdown`、`_calc_downside_deviation` 三个辅助函数。检查是否有其他模块还在使用它们。

- [ ] **Step 2: 检查删除函数是否被外部引用**

```bash
grep -rn "_calc_daily_returns\|_calc_max_drawdown\|_calc_downside_deviation" fund_*.py fund_manage.py tests/
```

只在 `fund_metrics.py` 内部使用 → 可以安全删除。

- [ ] **Step 3: 运行现有测试验证不破坏功能**

```bash
python -m pytest tests/ -v --timeout=30
```

- [ ] **Step 4: 提交**

```bash
git add fund_metrics.py
git commit -m "opt: _calc_nav_metrics 合并为单趟扫描，删除多余辅助函数"
```

---

### Task 5: 验证总效果

- [ ] **Step 1: 重启服务**

```bash
# 停旧进程、启动新服务
```

- [ ] **Step 2: 触发一次完整推荐**

```bash
curl -X POST http://127.0.0.1:8080/api/recommend
```

- [ ] **Step 3: 观察运行时间**

通过心跳 API 监控 `recData.total` 和进度，或查看 `fund_watch.log` 的总耗时。

---

## 预期效果

| 指标 | 优化前 | 优化后 | 说明 |
|------|--------|--------|------|
| 每只基金请求数 | 3 次 | 1 次 | 去掉实时估值 + 持仓 |
| 并发数 | 5 | 20 | 提高 4 倍 |
| 排行拉取 | 串行 4 URL | 并行 2 URL | 减少首屏等待 |
| 指标计算 | ~10 趟扫描 | 1 趟扫描 | CPU 减少 ~70% |
| **总耗时估计** | **~16 分钟** | **~5 分钟** | 200 只 / 20 并发 × 每个请求 ~25s + 排名 |
