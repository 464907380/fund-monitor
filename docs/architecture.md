# 项目架构

```
基金监控/
├── src/                     # 核心代码
│   ├── fund_server.py       # HTTP 服务器（入口），提供 Web API
│   ├── fund_recommend.py    # 全市场基金推荐引擎（评分+筛选）
│   ├── fund_render.py       # HTML 表格渲染（自选表+推荐表）
│   ├── fund_scoring.py      # 评分模型（20维分段线性评分）
│   ├── fund_watch.py        # 基金数据获取（净值、实时估值、持仓）
│   ├── fund_utils.py        # 工具函数（HTTP请求、心跳、日志）
│   ├── fund_metrics.py      # 风险指标计算（波动率、最大回撤等）
│   ├── fund_monitor.py      # 盘中监控（定时轮询+警报）
│   ├── fund_manage.py       # CLI 命令行管理工具
│   └── config.py            # 配置加载（config.json）
├── templates/
│   └── fund_manage.html     # 前端页面（单页应用）
├── data/
│   ├── config.json          # 评分维度、网络参数、推荐配置
│   └── fund_list.json       # 自选基金列表
├── deploy/                  # 部署脚本（systemd timer）
├── docker/                  # Docker 部署
├── docs/                    # 设计文档
├── tests/                   # 测试
└── .github/
    └── copilot-instructions.md  # 编码规范
```

## 数据流

```
天天基金API → fund_watch.py → fund_recommend.py → .fund_recommend_result.json
                                                        ↓
用户浏览器 ← fund_server.py ← fund_render.py ← fund_scoring.py
```

## 模块职责

| 模块 | 职责 |
|------|------|
| `fund_server.py` | HTTP 服务，处理所有前端 API 请求，管理子进程 |
| `fund_recommend.py` | 全市场基金扫描、评分、筛选、排序 |
| `fund_render.py` | 生成自选基金表和市场优选表的 HTML |
| `fund_scoring.py` | 20 维度评分模型定义和计算 |
| `fund_watch.py` | 调用天天基金 API 获取净值、实时估值、持仓等 |
| `fund_utils.py` | HTTP 请求、心跳读写、日志配置等工具函数 |
| `fund_metrics.py` | 从净值数据计算年化收益、波动率、最大回撤等 |
| `fund_monitor.py` | 盘中定时轮询基金实时涨跌、触发警报 |
| `config.py` | 读取 data/config.json 的配置 |
| `fund_manage.html` | 前端单页应用 |
