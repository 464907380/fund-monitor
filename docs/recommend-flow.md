# 基金推荐运行流程

## 整体流程图

```
用户点击「运行推荐」
       │
       ▼
┌──────────────────────────────┐
│ 前端收集筛选条件/DOM数据      │
│ POST /api/recommend-config   │
│ → 写入 data/config.json      │
└──────────────────────────────┘
       │
       ▼
┌──────────────────────────────┐
│ POST /api/recommend          │
│ → 启动子进程 fund_recommend   │
└──────────────────────────────┘
       │
       ▼
┌──────────────────────────────┐
│ fund_recommend.py main()     │
│                              │
│ 1. _reload_config()          │
│    从 data/config.json 重新   │
│    加载 _FILTER_CONDITIONS   │
│    等运行时变量               │
└──────────────────────────────┘
       │
       ▼
┌──────────────────────────────┐
│ 2. 检查缓存                  │
│                              │
│ 读取 .fund_recommend_result  │
│ _result.json（如果存在）      │
│                              │
│ 比较 filter_hash:            │
│  旧缓存.hash vs 当前配置.hash│
│                              │
│ 哈希一致?                    │
│  ├─ 是 → cache_mode="full"  │
│  └─ 否 → cache_mode=None     │
│         （全量重新运行）      │
└──────────────────────────────┘
       │
       ▼
┌─ cache_mode? ─────────────────┐
│                               │
│ 命中 ("full")                 │ 未命中 (None)
│                               │
│ ┌─────────────────────────┐   │ ┌─────────────────────────┐
│ │ 3a. 刷新涨跌 _HAS_TD    │   │ │ 3b. 全量运行            │
│ │     =True 时:            │   │ │                         │
│ │   - _batch_fetch_       │   │ │ 阶段1: 获取排行          │
│ │     estimates()         │   │ │   _fetch_rank_list(TOP)  │
│ │     拉取实时估值         │   │ │   → 排行API              │
│ │   - 重新评分             │   │ │                         │
│ │     _calc_score2()      │   │ │ 阶段2: 初筛              │
│ │                         │   │ │   _filter_candidates()   │
│ │   4a. 补充自选基金       │   │ │   ① 用户筛选条件         │
│ │   _supplement_self_     │   │ │   ② 筛掉缺失收益数据     │
│ │   selected(results)     │   │ │                         │
│ │                         │   │ │ 阶段3: 限购检查          │
│ │   5a. 保存              │   │ │   筛掉单日限购≤2万        │
│ │   _save_result(results) │   │ │                         │
│ └─────────────────────────┘   │ │ 阶段4: 并行评分          │
│                               │ │   50线程拉取 ping-       │
│                               │ │   zhongdata 评分          │
│                               │ │                         │
│                               │ │ 4b. 补充自选基金          │
│                               │ │ _supplement_self_        │
│                               │ │ selected(scored)        │
│                               │ │                         │
│                               │ │ 5b. 保存 + 写缓存        │
│                               │ │ _save_result(scored)    │
│ └─────────────────────────┘   │ └─────────────────────────┘
└───────────────────────────────┘
       │
       ▼
┌──────────────────────────────┐
│ finally: 写入 完成 心跳       │
│ → 前端检测到 phase=完成       │
└──────────────────────────────┘
       │
       ▼
┌──────────────────────────────┐
│ 前端渲染表格                 │
│  FET /api/recommend-table    │
│  GET /api/fund-table?fresh=1 │
└──────────────────────────────┘
```

## 关键文件

| 文件 | 作用 |
|------|------|
| `src/fund_recommend.py` | 推荐主流程（子进程） |
| `src/fund_server.py` | HTTP 服务，启动子进程 |
| `templates/fund_manage.html` | 前端按钮 + 轮询心跳 + 渲染 |
| `data/config.json` | 存储筛选条件、评分权重等配置 |
| `.fund_recommend_result.json` | 推荐结果缓存文件 |
| `recommend.log` | 推荐日志（在项目根目录） |

## 核心配置字段

`data/config.json` 中的 `recommend` 段：

```json
{
  "recommend": {
    "top_n": 24500,              // 拉取排行数量
    "filter_conditions": [       // 筛选条件
      {"field": "y1", "op": "gte", "value": 110}
    ],
    "show_top": 50,              // 展示前N只
    "skip_missing_perf": true,   // 筛掉缺失收益数据
    "skip_limited": true,        // 筛掉单日限购≤2万
    "rank_sort": "1n",           // 排行排序方式
    "lock_retry_count": 30,
    "lock_retry_interval": 1.0,
    "net_value_timeout": 10
  }
}
```

## 缓存机制

### filter_hash 计算

`_filter_hash()` 影响缓存是否命中的参数：

```
_CONFIG_VERSION + str(top_n) + str(skip_missing_perf)
+ str(skip_limited) + rank_sort
+ json.dumps(filter_conditions, sort_keys=True)
→ MD5
```

任一参数变化 → filter_hash 变化 → 缓存不命中 → 全量运行。

### config_hash 计算

`_config_hash()` 在 filter_hash 基础上增加评分维度/权重，仅用于日志对比，不影响缓存策略。

## 各阶段耗时参考

| 阶段 | 耗时 | 说明 |
|------|------|------|
| 获取排行 | 2-5s | 4个URL并发请求 |
| 初筛 | <1s | 内存中遍历 |
| 限购检查 | 5-30s | 逐只HTTP查基金页面 |
| 评分 | 10-60s/1000只 | 50线程拉pingzhongdata |
| 缓存刷新涨跌 | 2-5s/200只 | 拉取实时估值 |

## 常见问题排查

### 评分数量与初筛数量不一致
- 初筛通过数 > 评分成功数 → 部分基金 `pingzhongdata` 拉取失败（网络超时、无数据）
- 查看 `recommend.log` 中的 `WARNING` 日志

### 缓存不命中
- 检查 `_reload_config()` 是否成功（日志中 `_reload_config 失败`）
- 检查 `data/config.json` 是否完整（`filter_conditions` 字段是否存在）
- 子进程启动时 config.json 可能正在被写入（已改为原子写入避免）

### 推荐进程启动失败
- 确认 `fund_server.py` 已运行
- 检查是否有残留的 Python 进程占用资源
- stderr 已改为 DEVNULL，错误信息通过心跳 `phase="失败"` 传递给前端

### 前端进度不更新
- 前端每 2 秒轮询 `/api/heartbeat`
- 子进程通过 `update_heartbeat("fund_recommend", ...)` 写入心跳
- 子进程退出后 `_wait_and_cleanup` 会清除心跳（异常退出保留30秒）
