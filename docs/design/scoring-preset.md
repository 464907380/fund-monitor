# 评分维度预设 — 详细设计文档

## 1. 数据模型

### 1.1 config.json 存储结构

在 `config.json` 根级新增 `scoring_presets` 字段，与已有的 `scoring` 并列：

```json
{
  "scoring": {
    "dims": [...]          ← 当前生效的维度配置（不变）
  },
  "scoring_presets": {     ← 新增：所有预设
    "系统默认": {
      "dims": [
        {"name": "近3月收益", "key": "m3", "weight": 0.12, "enabled": true,
         "desc": "近三个月涨跌幅，中期趋势",
         "curve": {"points": [[0,0],[30,50],[60,80],[90,100]]},
         "category": "perf"},
        ...
      ]
    },
    "进攻型": { "dims": [...] },
    "防守型": { "dims": [...] },
    "短炒型": { "dims": [...] }
  }
}
```

每个预设的 `dims` 数组结构与 `scoring.dims` 完全一致，可直接替换。

### 1.2 内置预设定义

| 预设名 | 数据来源 |
|--------|---------|
| **系统默认** | 从 `fund_manage.html` 中 `resetDims()` 的 `defaults` 数组提取 |
| **进攻型** | 写死在 `fund_server.py` 或 `config.py` 的 `_BUILTIN_PRESETS` |
| **防守型** | 同上 |
| **短炒型** | 同上 |

内置预设仅在首次初始化时写入 `config.json`（如果 `scoring_presets` 字段不存在）。

## 2. 后端设计

### 2.1 新增文件：`fund_preset.py`（可选）

如果预设逻辑较复杂，可抽离为独立模块。简单方案：直接在 `fund_server.py` 中处理。

### 2.2 API 接口

#### GET /api/dims-presets

**作用**：返回所有预设列表 + 当前选中预设名称。

**响应**：
```json
{
  "ok": true,
  "presets": {
    "系统默认": { "dims": [...] },
    "进攻型":    { "dims": [...] }
  },
  "current": "系统默认"
}
```

**实现**：
```python
if parsed.path == "/api/dims-presets":
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        presets = cfg.get("scoring_presets", {})
        if not presets:
            presets = _init_builtin_presets(cfg)  # 首次初始化
        # current 从 cfg 中读取，或默认为 "系统默认"
        current = cfg.get("scoring", {}).get("current_preset", "系统默认")
        self._send(*_json_response({"ok": True, "presets": presets, "current": current}))
    except Exception as e:
        self._send(*_json_response({"ok": False, "error": str(e)}, 500))
    return
```

#### POST /api/dims-presets

**作用**：保存/另存为/删除预设。

**请求体**：
```json
// 保存（覆盖已有）
{"action": "save", "name": "进攻型"}
// 另存为（新建）
{"action": "save_as", "name": "自定义预设1"}
// 删除
{"action": "delete", "name": "进攻型"}
```

**说明**：`save` 和 `save_as` 的 dims 数据**从当前 `scoring.dims` 读取**，而非从请求体传入。因为前端维度表格的数据已通过 `POST /api/dims` 保存到 `scoring.dims`，预设操作应当读取已持久化的数据。

**响应**：
```json
{"ok": true, "presets": {...}, "current": "进攻型"}
```

**实现**：
```python
if self.path == "/api/dims-presets":
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        presets = cfg.setdefault("scoring_presets", {})
        action = body.get("action")
        name = body.get("name", "").strip()
        if not name:
            self._send(*_json_response({"ok": False, "error": "预设名称不能为空"}, 400))
            return
        if action == "save":
            if name == "系统默认":
                self._send(*_json_response({"ok": False, "error": "系统默认预设不可覆盖"}, 400))
                return
            if name not in presets:
                self._send(*_json_response({"ok": False, "error": f"预设「{name}」不存在"}, 404))
                return
            # 用当前 scoring.dims 覆盖该预设
            presets[name] = {"dims": cfg["scoring"]["dims"]}
        elif action == "save_as":
            if name in presets:
                self._send(*_json_response({"ok": False, "error": f"预设「{name}」已存在"}, 400))
                return
            if len(presets) >= 20:
                self._send(*_json_response({"ok": False, "error": "预设数量已达上限（20）"}, 400))
                return
            presets[name] = {"dims": cfg["scoring"]["dims"]}
        elif action == "delete":
            if name == "系统默认":
                self._send(*_json_response({"ok": False, "error": "系统默认预设不可删除"}, 400))
                return
            if name not in presets:
                self._send(*_json_response({"ok": False, "error": f"预设「{name}」不存在"}, 404))
                return
            del presets[name]
        else:
            self._send(*_json_response({"ok": False, "error": "未知操作"}, 400))
            return
        # 更新当前选中预设
        if action in ("save", "save_as"):
            cfg.setdefault("scoring", {})["current_preset"] = name
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        self._send(*_json_response({"ok": True, "presets": presets, "current": cfg.get("scoring", {}).get("current_preset", "系统默认")}))
    except Exception as e:
        self._send(*_json_response({"ok": False, "error": str(e)}, 500))
    return
```

### 2.3 加载预设的流程

加载预设**不通过独立 API**，而是通过已有机制：

1. 前端从 `GET /api/dims-presets` 获取预设列表
2. 用户选择预设点击「加载」
3. 前端将预设的 `dims` 赋值给维度表格的输入控件（`loadDims()` 的渲染逻辑）
4. 页面标记为"未保存"，提示用户点击「💾 保存权重」持久化
5. 用户点击「💾 保存权重」→ `POST /api/dims` 写入 `scoring.dims`

**为什么加载预设不直接写 `scoring.dims`？** 与 `resetDims()` 保持一致——加载预设只是填充界面，用户确认后才保存。

### 2.4 内置预设初始化

```python
_BUILTIN_PRESETS = {
    "系统默认": {"dims": [...]},  # 从 resetDims() 的 defaults 数组提取
    "进攻型": {"dims": [
        {"name": "近1年收益", "key": "y1", "weight": 0.20, "enabled": true,
         "desc": "最近一年的表现", "category": "perf",
         "curve": {"points": [[0,0],[50,50],[100,80],[150,100]]}},
        {"name": "近3月收益", "key": "m3", "weight": 0.15, "enabled": true,
         "desc": "近三个月涨跌幅", "category": "perf",
         "curve": {"points": [[0,0],[30,50],[60,80],[90,100]]}},
        {"name": "夏普比率", "key": "sharpe", "weight": 0.12, "enabled": true,
         "desc": "每承受1份波动能换来多少额外收益", "category": "quality",
         "curve": {"points": [[0,0],[0.5,30],[1,70],[1.5,100]]}},
        {"name": "盈亏比", "key": "profit_ratio", "weight": 0.10, "enabled": true, ...},
        {"name": "年化收益率", "key": "annual_return", "weight": 0.10, "enabled": true, ...},
        {"name": "近一月收益", "key": "m1", "weight": 0.08, "enabled": true, ...},
        {"name": "近6月收益", "key": "sy6", "weight": 0.06, "enabled": true, ...},
        {"name": "近2年收益", "key": "sy2", "weight": 0.05, "enabled": true, ...},
        {"name": "索提诺比率", "key": "sortino", "weight": 0.05, "enabled": true, ...},
        {"name": "基金规模", "key": "sc", "weight": 0.04, "enabled": true, ...},
        {"name": "机构持有比例", "key": "inst", "weight": 0.03, "enabled": true, ...},
        {"name": "费率", "key": "rate", "weight": 0.02, "enabled": true, ...},
    ]},
    "防守型": {"dims": [...]},  # 侧重 max_dd, volatility, calmar
    "短炒型": {"dims": [...]},  # 侧重 f5, m1, m3
}

def _init_builtin_presets(cfg: dict) -> dict:
    """首次初始化内置预设"""
    presets = {}
    for name, data in _BUILTIN_PRESETS.items():
        presets[name] = data
    cfg["scoring_presets"] = presets
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return presets
```

## 3. 前端设计

### 3.1 UI 布局

在评分权重卡片中，维度表格上方新增预设操作栏：

```html
<div class="preset-bar" style="display:flex;align-items:center;gap:8px;padding:8px 4px 12px;border-bottom:1px solid rgba(255,255,255,0.06);margin-bottom:12px;">
  <span style="font-size:12px;color:#888;white-space:nowrap;">📁 预设</span>
  <select id="presetSelect" style="flex:1;max-width:200px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.15);border-radius:4px;color:#ccc;padding:4px 8px;font-size:12px;"></select>
  <button onclick="loadPreset()" style="...">📂 加载</button>
  <button onclick="savePreset()" style="...">💾 保存</button>
  <button onclick="saveAsPreset()" style="...">📝 另存为</button>
  <button onclick="deletePreset()" style="...">🗑 删除</button>
</div>
```

### 3.2 核心函数

#### `loadPresets()` — 初始化预设下拉框

页面加载时调用，填充预设列表并选中当前预设：

```javascript
async function loadPresets() {
  const r = await fetch(API + '/api/dims-presets');
  const d = await r.json();
  if (!d.ok) return;
  const sel = document.getElementById('presetSelect');
  sel.innerHTML = Object.keys(d.presets).map(function(name) {
    return '<option value="' + name + '">' + name + '</option>';
  }).join('');
  sel.value = d.current || '系统默认';
}
```

#### `loadPreset()` — 加载选中预设到界面

```javascript
async function loadPreset() {
  const sel = document.getElementById('presetSelect');
  const name = sel.value;
  if (!name) return;
  if (!confirm('加载预设将替换当前所有维度配置，确定继续？')) return;
  const r = await fetch(API + '/api/dims-presets');
  const d = await r.json();
  if (!d.ok) return;
  const preset = d.presets[name];
  if (!preset) return;
  // 将预设的 dims 渲染到维度表格
  renderDims(preset.dims);
  markDimsDirty();  // 标记为未保存
}
```

#### `savePreset()` — 覆盖保存当前预设

```javascript
async function savePreset() {
  const sel = document.getElementById('presetSelect');
  const name = sel.value;
  if (!name || name === '系统默认') {
    showMsg('系统默认预设不可覆盖', 'fail');
    return;
  }
  const r = await fetch(API + '/api/dims-presets', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'save', name: name}),
  });
  const d = await r.json();
  if (d.ok) {
    showMsg('✔ 预设已保存', 'ok');
    loadPresets();
  } else {
    showMsg('✖ ' + (d.error || '保存失败'), 'fail');
  }
}
```

#### `saveAsPreset()` — 另存为新预设

```javascript
async function saveAsPreset() {
  const name = prompt('请输入新预设名称：');
  if (!name || !name.trim()) return;
  const r = await fetch(API + '/api/dims-presets', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'save_as', name: name.trim()}),
  });
  const d = await r.json();
  if (d.ok) {
    showMsg('✔ 预设已保存', 'ok');
    loadPresets();
  } else {
    showMsg('✖ ' + (d.error || '保存失败'), 'fail');
  }
}
```

#### `deletePreset()` — 删除预设

```javascript
async function deletePreset() {
  const sel = document.getElementById('presetSelect');
  const name = sel.value;
  if (!name || name === '系统默认') {
    showMsg('系统默认预设不可删除', 'fail');
    return;
  }
  if (!confirm('确定删除预设「' + name + '」？')) return;
  const r = await fetch(API + '/api/dims-presets', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'delete', name: name}),
  });
  const d = await r.json();
  if (d.ok) {
    showMsg('✔ 预设已删除', 'ok');
    loadPresets();
  } else {
    showMsg('✖ ' + (d.error || '删除失败'), 'fail');
  }
}
```

#### `renderDims(dims)` — 将预设 dims 渲染到表格

复用 `loadDims()` 的渲染逻辑。将 `loadDims()` 重构为可接受外部 dims 数据：

```javascript
async function loadDims(customDims) {
  let dims;
  if (customDims) {
    dims = customDims;
  } else {
    const r = await fetch(API + '/api/dims');
    const d = await r.json();
    if (!d.ok) return;
    dims = d.dims;
  }
  // ... 现有渲染代码 ...
}
```

### 3.3 页面加载顺序

```javascript
loadDims();       // 加载当前维度配置到表格
loadPresets();    // 加载预设列表到下拉框
```

两个调用互不依赖，可并行。

## 4. 边界情况与错误处理

| 场景 | 处理方式 |
|------|---------|
| `config.json` 无 `scoring_presets` 字段 | `GET /api/dims-presets` 自动调用 `_init_builtin_presets()` 初始化 |
| 预设数量达到 20 个上限 | `save_as` 时返回 400 + 错误提示 |
| 预设名称与已有预设重名 | `save_as` 时返回 400 + 错误提示 |
| 删除最后一个非内置预设 | 正常删除，下拉框回退到「系统默认」 |
| 保存预设时 `scoring.dims` 为空 | 保存当前表格的 dims（由前端保证不为空） |
| 用户未保存维度就切换预设 | 加载预设填充表格，用户需要点击「保存权重」持久化 |

## 5. 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `fund_server.py` | 修改 | 新增 `GET/POST /api/dims-presets` 路由 + `_BUILTIN_PRESETS` + `_init_builtin_presets()` |
| `fund_manage.html` | 修改 | 新增预设 UI 区域 + `loadPresets()`/`loadPreset()`/`savePreset()`/`saveAsPreset()`/`deletePreset()` |
| `config.json` | 修改 | 运行时新增 `scoring_presets` 字段（无需手动编辑） |
