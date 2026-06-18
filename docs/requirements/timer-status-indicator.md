# 定时任务激活状态指示功能定义

## 概述
在管理页面「功能概览」区域，为每个定时任务增加激活状态指示器，让用户一目了然地知道系统级定时任务（Windows 计划任务 / Linux systemd timer）是否**已注册、已启用、即将触发**，而不仅仅是进程当前是否在运行。

## 用户故事

**故事 1：部署后确认**
> 我刚在 Linux 服务器上运行了 `deploy/install.sh`，想确认三个定时器都正确安装并启用了。我需要一个可视化的确认，而不是 SSH 进去敲 `systemctl list-timers`。

**故事 2：故障排查**
> 昨天的晚报没有推送，我不知道是定时任务没触发还是脚本运行失败了。如果界面能告诉我「定时器状态：已启用」和「上次运行结果：失败 0x1」，我就能快速判断问题出在哪个环节。

**故事 3：定时器被误停**
> 我可能不小心 `systemctl stop fund-monitor.timer` 了，界面上应该能立即反映出来，而不是等到第二天发现没监控。

## 当前状态分析

### 现有实现

当前「功能概览」已包含：

| 信息 | 来源 | 刷新频率 |
|------|------|---------|
| 任务名称、描述 | `TASK_DEFS`（硬编码） | 静态 |
| 运行时间规则 | `TASK_DEFS.timer` | 静态 |
| 上次运行结果（成功/失败） | `schtasks` / systemd API | 30 秒 |
| 下次运行时间 | `schtasks` / systemd API | 30 秒 |
| 进程当前是否运行（心跳） | `.heartbeats/*.json` | 10 秒 |

### 缺失的信息

| 缺失项 | 说明 |
|--------|------|
| ✅ 定时器/计划任务**是否已注册** | 服务文件是否存在并被 systemd 或 schtasks 加载 |
| ✅ **启用/禁用**状态 | `systemctl is-enabled` 或 schtasks 的 Status 字段 |
| ✅ **激活/非激活**状态 | `systemctl is-active` 或 schtasks 的 Status=Ready/Running |
| ✅ 定时器**下次触发倒计时** | 距离下次执行还有多久（人性化显示）|

## 功能需求

### FR-1：状态指示器 UI

在每条功能概览行右侧的状态区域，增加**三态指示器**：

```
┌─ 功能概览 ────────────────────────────────┐
│                                             │
│  🌏 全球股市简报                             │
│     A 股：上证指数 · 深证成指 · 创业板指...   │
│     上次成功 · 下次 2026/6/19 9:30:00       │
│                  ┌──────────────────────┐   │
│                  │ 🔵 定时器已启用 │ 09:30 │   │
│                  └──────────────────────┘   │
│                                             │
│  📊 基金晚报                                │
│     每只监控基金：当日涨跌 · 近5日...         │
│     上次成功 · 下次 2026/6/19 15:30:00      │
│                  ┌──────────────────────┐   │
│                  │ 🔵 定时器已启用 │ 15:30 │   │
│                  └──────────────────────┘   │
│                                             │
│  🔔 盘中监控                                │
│     交易日 9:30–15:00 每 10 分钟轮询...      │
│     ▶ 运行中 · 下次 2026/6/19 9:25:00       │
│                  ┌──────────────────────┐   │
│                  │ 🟢 正在运行 │ 9:25    │   │
│                  └──────────────────────┘   │
│                                             │
└─────────────────────────────────────────────┘
```

#### 指示器颜色与文字

| 状态 | 颜色 | 图标 | 文字 | 含义 |
|------|------|------|------|------|
| 已启用+等待触发 | 🔵 蓝色 | `◉` | `定时器已启用` | systemd timer 或 schtasks 已注册且启用，等待下次触发 |
| 正在运行 | 🟢 绿色 | `▶` | `正在运行` | 进程心跳 alive，任务正在执行 |
| 已禁用/未注册 | ⚪ 灰色 | `○` | `定时器未启用` | 服务未安装、被禁用或 timer 不存在 |
| 状态未知 | ⚫ 暗灰 | `?` | `状态未知` | 查询失败（如 Windows 上没有 schtasks 权限） |

### FR-2：后端 API 扩展

#### 当前 `/api/tasks` 响应结构

```json
{
  "ok": true,
  "tasks": [
    {
      "id": "fund_watch",
      "taskname": "基金晚报",
      "icon": "📊",
      "label": "基金晚报",
      "desc": "...",
      "time": "交易日 15:30",
      "status": "Ready",
      "next_run": "2026/6/19 15:30:00",
      "last_run": "2026/6/18 15:30:00",
      "last_result": "0",
      "ok": true,
      "running": false
    }
  ]
}
```

#### 新增字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `timer_enabled` | `bool` | 定时器是否已注册并启用 |
| `timer_active` | `bool` | 定时器当前是否激活（systemd 特有） |
| `timer_status` | `string` | 原始状态文本（如 Windows: "Ready", Linux: "active"） |

#### Windows 实现

```python
def _check_task_status(taskname: str) -> dict:
    # ... 现有逻辑 ...
    result["timer_enabled"] = result["status"] in ("Ready", "Running")
    result["timer_active"] = result["status"] == "Running"
    result["timer_status"] = result["status"]
    return result
```

#### Linux systemd 实现

首次未找到 schtasks 时，降级尝试 systemctl：

```python
def _check_systemd_timer(taskname: str) -> dict:
    """查询 systemd timer 状态"""
    try:
        # 检查 timer 是否启用
        enabled = subprocess.run(
            ["systemctl", "is-enabled", f"{taskname}.timer"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        # 检查 timer 是否激活
        active = subprocess.run(
            ["systemctl", "is-active", f"{taskname}.timer"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        # 获取下次触发时间
        next_run = ""
        r = subprocess.run(
            ["systemctl", "show", f"{taskname}.timer", "--property=NextElapseUSecRealtime"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0 and r.stdout.strip():
            val = r.stdout.strip().split("=", 1)[-1]
            if val and val != "(null)":
                next_run = val  # 格式需解析
        return {
            "timer_enabled": enabled == "enabled",
            "timer_active": active == "active",
            "timer_status": active,
            "next_run": next_run,
            "status": active,
        }
    except Exception:
        return {"timer_enabled": False, "timer_active": False, "timer_status": "未知"}
```

### FR-3：前端渲染更新

#### 状态优先级

定时任务整体状态按以下优先级决策：

```
                                 ┌── 心跳 alive ──→ 🟢 正在运行
                                 │
当前任务状态 ──→ timer_enabled ──┼── 心跳 dead ───→ 🔵 定时器已启用
                                 │
                                 └── false ──────→ ⚪ 定时器未启用
```

#### 模板修改

```javascript
const timerStatusHtml = running
  ? '<span class="timer-badge timer-running">▶ 正在运行</span>'
  : t.timer_enabled
    ? '<span class="timer-badge timer-enabled">◉ 定时器已启用</span>'
    : '<span class="timer-badge timer-disabled">○ 定时器未启用</span>';
```

### FR-4：定时器停用告警

当检测到某个定时任务**被禁用或未注册**时，在前端显示提示：

- 在功能概览卡片顶部显示黄色横幅：「⚠️ 部分定时任务未启用，可能导致监控中断」
- 点击可展开查看详情
- 定时器恢复启用后横幅自动消失

### FR-5：下次触发倒计时（进阶）

鼠标悬停在指示器上时，tooltip 显示距离下次触发的**剩余时间**（人性化格式）：

| 剩余时间 | 显示 |
|---------|------|
| > 1 天 | `2 天后 09:30` |
| > 1 小时 | `5 小时 23 分后` |
| < 1 小时 | `42 分钟后` |
| < 1 分钟 | `即将触发` |

## 非功能需求

| 需求 | 说明 |
|------|------|
| 兼容性 | 同时支持 Windows `schtasks` 和 Linux `systemd` |
| 降级友好 | `schtasks` 或 `systemctl` 命令不存在时，timer_enabled 默认 false，不报错 |
| 刷新频率 | 每 30 秒随 `loadFeatures()` 一并刷新 |
| 心跳优先 | 如果进程心跳 alive（▶ 运行中），即使 timer 显示 disabled 也以心跳为准（进程可能是手动启动的）|

## 前端 CSS 样式

```css
.timer-badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
  white-space: nowrap;
  letter-spacing: 0.5px;
}
.timer-badge.timer-enabled {
  background: rgba(66, 165, 245, 0.15);
  color: #42a5f5;
  border: 1px solid rgba(66, 165, 245, 0.3);
}
.timer-badge.timer-running {
  background: rgba(102, 187, 106, 0.15);
  color: #66bb6a;
  border: 1px solid rgba(102, 187, 106, 0.3);
}
.timer-badge.timer-disabled {
  background: rgba(255, 255, 255, 0.05);
  color: #666;
  border: 1px solid rgba(255, 255, 255, 0.1);
}
```

## 不在此功能范围内

- ❌ 在界面上直接启用/禁用定时器（需要 root/admin 权限，安全风险较高）
- ❌ 定时任务执行历史记录（保留给日志系统）
- ❌ 邮件/企业微信推送定时器异常告警
