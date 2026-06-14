# 全市场筛选可配置化 — 实现计划

> **For agentic workers:** implement this plan task-by-task.

**Goal:** 将 fund_recommend.py 的硬编码筛选参数（TOP N、最低收益、剔除负收益）改为网页可配置，并将"运行推荐"按钮移至评分维度卡片底部。

**Architecture:** 参数存入 config.json 的 recommend 段，后端新增 API 读写，前端在评分维度卡片底部新增筛选表单 + 进度条。

**Tech Stack:** Python 标准库 + 纯前端 JS

---

## 阶段 1：后端 — config 读取 + API

- **修改 `fund_recommend.py`** 将模块顶部常量改为从 `CFG` 读取

```python
# 替换硬编码常量
_TOP = CFG.get("recommend", {}).get("top_n", 200)
_SHOW_TOP = 10  # 输出条数不变，保持硬编码
_MIN_Y1 = CFG.get("recommend", {}).get("min_y1_return", 20)
_EXCLUDE_NEG = CFG.get("recommend", {}).get("exclude_negative", True)
```

- **修改 `_filter_candidates()`** 使用 `_EXCLUDE_NEG` 和 `_MIN_Y1`

```python
def _filter_candidates(rows: list) -> list:
    candidates = []
    for r in rows:
        try:
            y1 = float(r[11]) if len(r) > 11 and r[11] else 0
            if _EXCLUDE_NEG and y1 <= 0:
                continue
            if y1 < _MIN_Y1:
                continue
            candidates.append(r)
        except (ValueError, IndexError):
            continue
    return candidates
```

- **修改 `fund_server.py`** 新增两个 API 端点（在 GET 分支的 `/api/dims` 附近添加）

GET /api/recommend-config:
```python
if parsed.path == "/api/recommend-config":
    try:
        cfg = json.load(open(_CONFIG_PATH, encoding="utf-8"))
        rc = cfg.get("recommend", {})
        self._send(*_json_response({
            "ok": True,
            "config": {
                "top_n": rc.get("top_n", 200),
                "min_y1_return": rc.get("min_y1_return", 20),
                "exclude_negative": rc.get("exclude_negative", True),
            }
        }))
    except Exception as e:
        self._send(*_json_response({"ok": False, "error": str(e)}, 500))
    return
```

POST /api/recommend-config:
```python
if self.path == "/api/recommend-config":
    try:
        cfg = json.load(open(_CONFIG_PATH, encoding="utf-8"))
        cfg["recommend"] = {
            "top_n": int(body.get("top_n", 200)),
            "min_y1_return": int(body.get("min_y1_return", 20)),
            "exclude_negative": bool(body.get("exclude_negative", True)),
        }
        json.dump(cfg, open(_CONFIG_PATH, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        self._send(*_json_response({"ok": True, "message": "推荐配置已更新"}))
    except Exception as e:
        self._send(*_json_response({"ok": False, "error": str(e)}, 500))
    return
```

---

## 阶段 2：前端 — 筛选表单 + 移动运行推荐按钮

- **修改 `fund_manage.html`**

在评分维度卡片底部（重置按钮下方、提示文字上方）插入全市场筛选表单 HTML：

```html
<div class="divider" style="margin:12px 0 8px;"></div>
<div style="padding:0 4px;">
  <div style="font-size:13px;font-weight:600;color:#ccc;margin-bottom:8px;">🔍 全市场筛选</div>
  <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:12px;">
    <label style="color:#888;">拉取数量
      <input type="number" id="recTopN" value="200" min="50" max="500" style="width:70px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:#e0e0e0;padding:4px 8px;margin-left:4px;font-family:Consolas;">
    </label>
    <label style="color:#888;">最低近1年收益
      <input type="number" id="recMinY1" value="20" min="0" max="100" style="width:60px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:4px;color:#e0e0e0;padding:4px 8px;margin-left:4px;font-family:Consolas;"> %
    </label>
    <label style="color:#888;display:flex;align-items:center;gap:4px;">
      <input type="checkbox" id="recExcludeNeg" checked> 剔除负收益
    </label>
  </div>
  <div style="margin-top:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
    <button onclick="runRecommendFromFilter()" id="recFilterBtn" class="toolbar-btn blue">▶ 运行推荐</button>
    <div style="flex:1;max-width:200px;">
      <div style="height:4px;background:rgba(255,255,255,0.08);border-radius:2px;overflow:hidden;">
        <div id="recFilterProgress" style="width:0%;height:100%;background:linear-gradient(90deg,#66bb6a,#42a5f5);border-radius:2px;transition:width 0.5s;"></div>
      </div>
    </div>
    <span id="recFilterStatus" style="font-size:11px;color:#888;"></span>
  </div>
</div>
```

- **删除顶部工具栏中的"运行推荐"按钮行**（`<div class="toolbar-btn-wrap">` 包含 recBtn2 和 recProgress 的部分）

- **新增 JS 函数** `loadRecommendConfig()` 和 `runRecommendFromFilter()`

```javascript
async function loadRecommendConfig() {
  try {
    const r = await fetch(API + '/api/recommend-config');
    const d = await r.json();
    if (d.ok && d.config) {
      document.getElementById('recTopN').value = d.config.top_n || 200;
      document.getElementById('recMinY1').value = d.config.min_y1_return || 20;
      document.getElementById('recExcludeNeg').checked = d.config.exclude_negative !== false;
    }
  } catch(e) {}
}

async function runRecommendFromFilter() {
  // 先保存配置
  try {
    await fetch(API + '/api/recommend-config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        top_n: parseInt(document.getElementById('recTopN').value) || 200,
        min_y1_return: parseInt(document.getElementById('recMinY1').value) || 20,
        exclude_negative: document.getElementById('recExcludeNeg').checked,
      }),
    });
  } catch(e) {}
  // 启动推荐
  runRecommend();
}
```

- **修改 `runRecommend()`** 让它在推荐结束后更新筛选区的进度条和状态文字。保持原有逻辑不变，额外通过 ID `recFilterBtn` / `recFilterProgress` / `recFilterStatus` 同步进度。

- **在 `init()` 中调用 `loadRecommendConfig()`**

---

## 阶段 3：验证

- 重启服务
- 检查 JS 语法
- 检查 API: GET /api/recommend-config 返回正确配置
- 检查 API: POST /api/recommend-config 保存并返回 ok
- 全量测试: `pytest tests/ -v --tb=short -q`
- 提交到 git
