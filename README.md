# FundSelect — 基金优选

Python 标准库实现的本地基金优选 Web 应用，无需外部框架依赖。

---

## 功能总览

### 📋 基金优选（Web 页面）

- **自选基金** — 添加/删除监控基金（支持关键词搜索）
- **自选基金完整数据表** — 当日涨跌 · 各期收益 · 年化收益率 · 夏普比率 · 最大回撤 · 评分等 20+ 维度
- **市场优选 TOP 表** — 全市场扫描的优秀基金排行，支持点击查看持仓股票

### 📊 基金晚报

每日收盘后（15:30）自动生成：

- 自选基金的当日涨跌 · 近5日 · 近1月/3月/1年收益
- 市场优选基金 TOP 排行（多维度评分）
- 异常警报：净值停滞 · 连跌趋势 · 分红除权
- 推送方式：企业微信 / QQ 邮件

### 🔍 市场优选推荐

全市场基金深度评分系统：

- 从天天基金拉取全市场排行（≥20000只）
- `y1 ≥ 100%` 筛选候选基金
- 独立拉取净值数据，真实计算各项指标（非接口直接返回）
- 20 个维度加权打分（0-100分）
- 结果缓存，随时查看

### 🔔 盘中监控

交易日 9:30–15:00 运行：

- 每 10 分钟轮询基金实时估算涨跌幅
- 双阈值警报：单次急涨急跌 + 当日累计涨跌
- 节假日自动检测 · 进程崩溃恢复

### 🌏 全球股市简报

交易日 9:30 推送：

- A 股：上证 · 深证 · 创业板 · 沪深300 · 成交额 · 涨跌家数
- 全球：道琼斯 · 纳斯达克 · 标普500 · 恒生 · 日经225 · 韩国KOSPI · 英国富时100 · 德国DAX · 法国CAC40 · 瑞士SMI

### ⚙️ 评分维度配置

- 16 个可开关的评分维度
- 可自定义权重（自动归一化）
- 可编辑评分曲线（拖拽断点，线性插值）
- 基于百分位自动校准曲线
- 预设方案：系统默认 · 进攻型 · 防守型 · 短炒型

---

## 快速开始

```bash
# 启动 Web 服务
python src/fund_server.py
# 访问 http://localhost:8080

# 或使用一键脚本
# Windows:
run.bat
# Linux/macOS:
chmod +x run.sh && ./run.sh
```

> 默认监听 `0.0.0.0:8080`，局域网内其他设备可通过 `http://你的IP:8080` 访问。
> 如需更改端口：`python src/fund_server.py 9090` 或修改 `data/config.json` 中 `server.port`。

```bash
# 运行市场优选推荐
python src/fund_recommend.py

# 查看上次推荐结果
python fund_recommend.py --load

# 将基金加入自选
python fund_recommend.py --add 基金代码
```

```bash
# 命令行管理自选基金
python fund_manage.py list
python fund_manage.py add 001438 180031
python fund_manage.py remove 001438
```

```bash
# 手动生成晚报
python fund_watch.py
```

---

## 项目结构

```
├── src/
│   ├── fund_server.py       # HTTP 服务器（Web 页面 + API）
│   ├── fund_manage.py       # 自选基金命令行管理
│   ├── fund_recommend.py    # 市场优选推荐（全扫描+评分）
│   ├── fund_watch.py        # 晚报生成 + 基金数据获取
│   ├── fund_monitor.py      # 盘中监控轮询
│   ├── global_briefing.py   # 全球股市简报
│   ├── fund_scoring.py      # 评分引擎（曲线/维度/权重）
│   ├── fund_render.py       # HTML/Markdown 渲染
│   ├── fund_utils.py        # 工具函数（网络/缓存/推送）
│   ├── fund_metrics.py      # 净值指标计算
│   ├── fund_alerts.py       # 异常检测（停滞/连跌/分红）
│   └── config.py            # 配置加载
├── templates/
│   └── fund_manage.html     # Web 前端页面
├── data/
│   ├── config.json          # 运行配置（自动创建）
│   └── fund_list.json       # 自选基金列表（自动创建）
├── deploy/                  # 部署配置
├── run.bat                  # Windows 一键启动
├── run.sh                   # Linux/macOS 一键启动
├── pyproject.toml
└── README.md
```

---

## 数据流

```
天天基金/新浪 API
      ↓
  fund_watch.py (数据获取)
      ↓
  fund_scoring.py (评分计算)
      ↓
  fund_render.py (HTML渲染)
      ↓
  fund_server.py (HTTP服务)
      ↓
  浏览器 Web 页面
```

---

## 部署

### Windows（计划任务）

```powershell
# 安装所有定时任务
powershell -File install_task.ps1
```

### Linux（systemd）

```bash
bash deploy/install.sh
```

---

## 配置说明

`config.json` 主要配置项：

| 配置路径 | 说明 |
|---------|------|
| `recommend.top_n` | 全市场排行拉取数量（默认 20000） |
| `recommend.min_y1_return` | 候选基金最低近1年收益（默认 100%） |
| `recommend.show_top` | 展示条数（默认 20） |
| `fund_monitor.poll_interval_seconds` | 盘中轮询间隔 |
| `fund_monitor.alert_drop_once` | 基金单次急跌阈值 |
| `fund_monitor.alert_jump_once` | 基金单次急涨阈值 |
| `network.cache_ttl_seconds` | 网络请求缓存时间 |
| `network.cache_max_entries` | 缓存最大条目数 |
| `scoring.dims` | 评分维度定义（名称/权重/曲线） |
