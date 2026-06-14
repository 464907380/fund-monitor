# 全市场筛选可配置化 — 设计文档

> **目标：** 在网页"评分维度"卡片内新增"全市场筛选"功能，将 fund_recommend.py 的硬编码筛选参数改为网页可配置，并将"运行推荐"按钮移至该区域。

## 配置存储

在 `config.json` 新增 `recommend` 配置段：

```json
{
  "recommend": {
    "top_n": 200,
    "min_y1_return": 20,
    "exclude_negative": true
  }
}
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `top_n` | 200 | 从天天基金排行拉取的数量 |
| `min_y1_return` | 20 | 最低近 1 年收益（%），低于此值的基金不进入评分 |
| `exclude_negative` | true | 是否剔除负收益基金 |

## 后端改动

### `fund_recommend.py`

将模块顶部的硬编码常量改为从 `CFG` 读取：

```python
_TOP = CFG.get("recommend", {}).get("top_n", 200)
_MIN_Y1 = CFG.get("recommend", {}).get("min_y1_return", 20)
_EXCLUDE_NEG = CFG.get("recommend", {}).get("exclude_negative", True)
```

`_filter_candidates()` 使用 `_MIN_Y1` 和 `_EXCLUDE_NEG` 替代字面量 20 和硬编码逻辑。

### `fund_server.py`

新增两个 API 端点：

**`GET /api/recommend-config`** — 读取当前推荐配置

```json
{"ok": true, "config": {"top_n": 200, "min_y1_return": 20, "exclude_negative": true}}
```

**`POST /api/recommend-config`** — 保存推荐配置

```json
// 请求体
{"top_n": 200, "min_y1_return": 20, "exclude_negative": true}
// 响应
{"ok": true, "message": "推荐配置已更新"}
```

## 前端改动

### `fund_manage.html`

在"评分维度"卡片底部新增"全市场筛选"区块。

**新增 JS 函数：**

- `loadRecommendConfig()` — 页面加载时调用 GET /api/recommend-config 填充表单
- `saveRecommendConfig()` — 保存当前表单值到后端
- 修改 `runRecommend()` — 点击时先调用 `saveRecommendConfig()` 再启动推荐

## 数据流

```
网页填写参数 → 点击运行推荐
  → saveRecommendConfig() → POST /api/recommend-config → 写入 config.json
  → runRecommend() → POST /api/recommend
  → fund_recommend.py 从 config.json 读取 _TOP/_MIN_Y1/_EXCLUDE_NEG
  → 执行筛选 + 评分 + 心跳进度更新
  → 前端轮询 /api/heartbeat 显示进度条
```

## 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `config.json` | 新增 | 新增 `recommend` 配置段 |
| `fund_recommend.py` | 修改 | 硬编码常量改为从 CFG 读取 |
| `fund_server.py` | 修改 | 新增 GET/POST /api/recommend-config |
| `fund_manage.html` | 修改 | 新增筛选参数表单 + 移动运行推荐按钮 + 进度条 |
