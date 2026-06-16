---
name: project-architecture
description: 基金监控项目架构与改动规范
---

# 🎯 项目根本目的

> **通过盘中异常监控、收盘数据追踪、全球市场简报、全市场基金推荐，辅助用户及时止盈止损、发现更好标的、了解市场环境，最终服务于"买卖基金赚钱"这个根本目的。**

# 🏗 项目结构

| 文件 | 职责 |
|------|------|
| `fund_server.py` | Web 服务器（端口 8080），REST API + 管理页面 |
| `fund_watch.py` | 基金晚报核心：数据获取、历史快照、检查逻辑 |
| `fund_monitor.py` | 盘中实时监控：每 10 分钟轮询实时估值 + 个股持仓 |
| `fund_recommend.py` | 全市场基金推荐：拉取排行 → 筛选 → 并行评分 |
| `fund_scoring.py` | 20 维可配置评分模型（分段线性 + 注册表驱动） |
| `fund_render.py` | 渲染/推送：Markdown/HTML 表格、企业微信、邮件 |
| `fund_utils.py` | 公共基础设施：HTTP 缓存/重试、交易日、心跳、颜色 |
| `fund_alerts.py` | 净值警报检测：停滞、连跌、分红除权 |
| `fund_metrics.py` | 净值指标计算：年化、波动率、夏普比率等 |
| `global_briefing.py` | 全球股市晨报：A 股 4 指数 + 全球 10 指数 |
| `config.py` | 统一配置加载（config.json + .env） |
| `fund_manage.html` | 管理前端（单页应用，JS+CSS 内联） |

# 📡 推送通道

| 通道 | 函数 | 条件 |
|------|------|------|
| 企业微信 | `send_wechat()` | 配了 `WECHAT_WEBHOOK` 时发送 |
| QQ 邮箱 | `send_mail_html()` / `send_mail()` | 配了 `QQ_EMAIL` + `QQ_MAIL_AUTH` 时发送 |

**各模块推送策略：**
- **基金晚报 `fund_watch.py`**：企业微信 + 邮件双通道同时发
- **盘中监控 `fund_monitor.py`**：企业微信优先，无则邮件降级
- **全球市场简报 `global_briefing.py`**：**双通道同时发**（2026-06-15 改，原为二选一）
- **基金推荐 `fund_recommend.py`**：无主动推送，结果保存到文件供 Web 读取

# ⏰ 定时任务

| 任务 | 时间 | 脚本 |
|------|------|------|
| 全球股市简报 | 交易日 09:30 | `global_briefing.py` |
| 基金盘中监控 | 交易日 09:25~15:00 | `fund_monitor.py` |
| 基金晚报 | 交易日 15:30 | `fund_watch.py` |

所有计划任务使用 `pythonw.exe`（无控制台窗口）。

# 🔄 关键调用链

- `fund_server.py` → `subprocess.Popen` → `fund_recommend.py` / `fund_watch.py`
- `fund_watch.py` → `get(code)` → `fund_utils.fetch()` → 天天基金 API
- `fund_watch.py` / `fund_recommend.py` → `fund_scoring.calc_score_detail()`
- `fund_watch.py` → `fund_render.push()` / `fund_render._save_briefing()`
- `fund_watch.py` → `fund_alerts.check_*()` / `fund_metrics._calc_nav_metrics()`

# 🧩 改动规范

1. **先计划再改** — 任何结构性改动前先读项目架构，调用 superpowers-brainstorming 制定计划
2. **评分维度** — 增删维度只需改 `config.json` 的 `scoring.dims` 列表 + `fund_scoring.py` 注册表
3. **配置热加载** — 前端改评分维度/推荐配置后，server 自动 `importlib.reload()` 使新配置生效
4. **推送降级** — 企业微信不可用时自动走邮件，不发噪音日志
5. **心跳机制** — 后端进程通过 `.heartbeats/*.json` 文件通信，前端 2 秒轮询检测
