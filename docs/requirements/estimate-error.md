# 持仓估算误差识别 — 功能计划

> 目标：让"持仓估算涨跌与基金实际涨跌差异较大"的基金在界面上一眼可辨，
> 用户盘中看到估算值时能判断其可信度。

## 1. 背景与现状

- **盘中**（交易日 9:30-15:00）：涨跌来源为持仓估算（`_estimate_from_holdings`），
  界面显示橙色"估算"徽章（`_td_src == "holdings"`）
- **收盘后**：涨跌来源为实际净值（LSJZ `JZZZL`），界面显示绿色"净值"徽章（`lsjz`）
- **现状问题**：盘中只能看到估算值，无法判断这只基金"估得准不准"。
  持仓披露滞后（季报）、重仓股集中度变化、大额申赎等都会导致估算偏差，且各基金偏差程度差异很大。

## 2. 核心思路

**盘中记录估算值 → 收盘后立即结算当天差异 → 盘中+收盘后都展示差异**

一天即可看到差异（不需要累计几天）：
- **当天盘中**：采集每只基金持仓估算值（来源=holdings）
- **当天收盘后**：实际净值已出，立即对当天采集过的基金结算 `err = 估算 - 实际`，
  当天就能看到当天的差异
- **次日盘中**：显示历史差异（昨天等），提示该基金估算可信度

时间线示例：
```
Day1 盘中    采集估算（实际未出，暂无差异）
Day1 收盘后  结算 Day1 估算 vs 实际 → 立即显示当天差异
Day2 盘中    显示 Day1（及更早）差异 → 有徽章可用
```

## 3. 数据存储

新增 `data/fund_est_error.json`（gitignored），结构：

```json
{
  "estimates": {
    "2026-08-10": { "002910": 2.30, "012187": -1.10 }
  },
  "errors": {
    "002910": {
      "2026-08-07": { "est": 2.30, "actual": 1.88, "err": 0.42 },
      "2026-08-06": { "est": -0.50, "actual": 0.35, "err": -0.85 }
    }
  }
}
```

- `estimates[date][code]`：当天盘中采集到的估算涨跌（%）
- `errors[code][date]`：历史每天明细 `{est: 估算, actual: 实际, err: 估算-实际}`
  ——**保留每天具体差异**，供详情弹窗逐日展示
- MAE 计算：取 `errors[code]` 最近 N 天的 `|err|` 平均；无数据 → 无标记

## 4. 估算记录采集（盘中）

估算值在多处产生（自选表/优选表/监控/推荐），集中采集：
新增 `record_estimate(code, est_pct)`（放 `fund_utils.py`，供各处调用）：

```python
def record_estimate(code: str, est_pct: float) -> None:
    """盘中记录持仓估算值，供收盘后与实际净值对比"""
    # 写 data/fund_est_error.json 的 estimates[今天][code] = est_pct
```

在以下返回 `holdings` 来源处调用：
- `fund_watch._parse_real_time`：返回 `(est, "holdings")` 前
- `fund_utils._fetch_fund_estimate`：持仓估算分支返回前
- `fund_recommend._batch_fetch_estimates`（`_fetch_one_td`）盘中估算分支

## 5. 误差计算（收盘后/读取时）

新增 `settle_estimate_errors()`（放 `fund_utils.py` 或独立模块）：

1. 读取 `estimates` 中**早于今天**的所有日期
2. 对每个 (date, code)：拉该基金该日实际净值涨跌
   - 复用 LSJZ 逻辑：按日期过滤当日 `JZZZL`（已有 `_fetch_nav_from_lsjz` 类似解析）
   - 或对"最新一条"场景用 pageSize=1 + 日期校验
3. `err = est - actual`，写入 `errors[code][date]`
4. 清理已结算的 `estimates[date]`
5. 触发时机：`/api/fund-table`、`/api/recommend-table` 读取时后台懒结算
   （`threading.Thread`，避免阻塞），或用现有后台刷新线程

## 6. 界面展示（盘中+收盘后都可分辨）

**盘中**（`_td_src == "holdings"`）和**收盘后**（`_td_src == "lsjz"`）都显示差异徽章，
只是含义不同：

| 时段 | td 涨跌 | 差异徽章含义 |
|---|---|---|
| 盘中 | 持仓估算 | 最近历史差异（昨天等），提示估算可信度 |
| 收盘后 | 实际净值 | **当天**估算 vs 实际差异（当天结算） |

差异徽章按 MAE（近10天平均绝对误差）分档：

| MAE | 徽章 | 颜色 |
|---|---|---|
| < 1% | `±0.6% 准` | 绿 |
| 1% ~ 2% | `±1.4% 中` | 黄 |
| > 2% | `±2.6% 偏差大` | 红 |
| 暂无数据 | （不显示） | — |

- 自选表 / 优选表 td 列：数值旁追加可点击的差异徽章
- **详情弹窗**（点击徽章）：近 10 天**每天明细** `日期 | 估算涨跌 | 实际涨跌 | 误差`，
  误差大的日子红色高亮，底部 MAE 汇总；盘中收盘后都可打开
- 收盘后若当天已结算，徽章显示当天差异数值

## 7. 配置项（config.json）

```json
"estimate_error": {
  "history_days": 10,        // MAE 统计窗口（详情弹窗逐日明细也取近 N 天）
  "bad_threshold": 2.0,      // 偏差大阈值(%)
  "mid_threshold": 1.0       // 准/中分界(%)
}
```

## 8. 涉及文件

| 文件 | 改动 |
|---|---|
| `src/fund_utils.py` | 新增 `record_estimate`、`settle_estimate_errors`、`get_est_mae`、`_load/_save` 误差文件 |
| `src/fund_watch.py` | `_parse_real_time` 返回估算时记录 |
| `src/fund_recommend.py` | `_batch_fetch_estimates` 盘中估算记录 |
| `src/fund_render.py` | 自选/优选表 td 列追加误差徽章 |
| `templates/fund_manage.html` | 误差徽章样式/（可选）详情弹窗 |
| `src/fund_server.py` | 读取表格时触发误差结算 |
| `.gitignore` | `fund_est_error.json` |

## 9. 实施步骤

1. `fund_utils.py`：误差文件读写 + `record_estimate` + `settle_estimate_errors` + `get_est_mae`
2. 三处估算采集点接入 `record_estimate`
3. `fund_render.py` 两表 td 列加误差徽章
4. 前端样式 + 可选详情弹窗
5. server 表格读取时懒触发结算
6. 测试：模拟盘中记录 → 收盘结算 → MAE 徽章显示
7. 提交 + 重启

## 10. 边界与风险

- **当天收盘后立即结算**：结算在收盘后第一次读取表格时懒触发（后台线程），
  当天即可显示当天差异；次日盘中显示历史差异
- **盘中当天无差异**：当天实际未出，盘中显示最近一次历史差异（昨天等）；
  若该基金从未结算过（首次），暂不显示徽章
- **结算失败**（某基金实际净值拉取失败）：该条目跳过，下次再结算，不影响其他
- **文件大小**：仅存 code→date→{est,actual,err}，几十 KB 级别，定期清理超 60 天历史
- **正确性**：`record_estimate` 只在来源确认是 holdings 时记录，避免误录净值
