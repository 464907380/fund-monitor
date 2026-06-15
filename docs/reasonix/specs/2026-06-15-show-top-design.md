# 市场优选 TOP N 可配置化 — 设计文档

**目标：** 将全市场优选从硬编码 TOP 10 改为可配置参数，默认 20，可通过网页调节。

## 配置

`config.json` recommend 段新增：

```json
{"recommend": {"top_n": 200, "min_y1_return": 20, "exclude_negative": true, "show_top": 20}}
```

## 改动文件

| 文件 | 改动 |
|------|------|
| `fund_recommend.py` | `SHOW_TOP = CFG.get("recommend",{}).get("show_top", 20)` |
| `fund_render.py` | 所有 `TOP 10` 字面量改为 `f"TOP {show_top}"`，从 CFG 读取 |
| `fund_server.py` | GET/POST /api/recommend-config 增加 show_top 字段 |
| `fund_manage.html` | 筛选表单新增"展示条数"输入框 |

## 数据流

```
config.json.recommend.show_top
  → fund_recommend.py 控制输出 TOP N 条
  → fund_render.py 渲染晚报邮件/企业微信/网页时显示 TOP N
  → 前端筛选表单可读写该参数
```
