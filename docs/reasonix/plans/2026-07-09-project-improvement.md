# 项目全面完善 Implementation Plan

> **For agentic workers:** implement this plan task-by-task — dispatch a fresh subagent per task with the native `task` tool (recommended for quality), or use the superpowers-executing-plans skill to work through it inline. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复已知问题、清理死代码、统一路径管理、提高健壮性和用户体验

**Architecture:** 按模块分组，每项改动独立，每完成一项测试验证一次。涉及删除的模块先确认无外部依赖再删。

**Tech Stack:** Python 3.14, 标准库, pytest

---

### Task 1: 删除孤立模块 fund_alerts.py 及相关测试

**Files:**
- Create: 无
- Modify: `tests/test_fund_watch.py` — 删除引用 fund_alerts 的测试用例
- Delete: `src/fund_alerts.py`
- Delete: `tests/test_check_and_push.py`
- Delete: `tests/test_calibrate.py`
- Delete: `tests/check_post_calibrate.py`
- Ignore: `.gitignore` 中已忽略 `tests/check_post_calibrate.py` 和 `tests/test_calibrate.py`，确认即可

- [ ] **Step 1: 确认 fund_alerts.py 无外部依赖**

检查 `src/fund_alerts.py` 中的函数是否被其他模块引用。已被分析确认无任何文件 import 它，可以安全删除。

- [ ] **Step 2: 删除 fund_alerts.py**

```bash
git rm src/fund_alerts.py
```

- [ ] **Step 3: 删除测试文件**

```bash
git rm tests/test_check_and_push.py
git rm tests/test_calibrate.py
git rm tests/check_post_calibrate.py
```

- [ ] **Step 4: 清理 test_fund_watch.py 中引用 fund_alerts 的用例**

删除 `tests/test_fund_watch.py` 中对以下函数的测试用例：
- `check_stagnation`
- `check_consecutive_drop`
- `check_dividend`

这些测试位于文件末尾，大约后 30 行。删除后确保剩余的测试用例能正常收集。

- [ ] **Step 5: 运行测试验证**

```bash
cd D:\Users\基金监控
python -m pytest tests/ --collect-only 2>&1 | tail -20
```

Expected: 不再出现 `fund_alerts` 相关的 ImportError，测试收集数量减少。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "清理：删除已废弃的 fund_alerts 模块和测试文件"
```

---

### Task 2: 修复 test_fund_watch.py 中的 ImportError

**Files:**
- Modify: `tests/test_fund_watch.py` — 修复引用已不存在或已更名的函数

**背景：** 该测试文件中引用了几个在 `fund_watch.py` 中已不存在的函数：
- `check_stagnation`、`check_consecutive_drop`、`check_dividend` → 已在 Task 1 中删除
- `_get_webhook` → 已不存在
- `load_hist` → 已不存在
- `is_trading_time`、`is_trading_weekday` 等 → 已挪到其他模块

- [ ] **Step 1: 确认当前失败的测试列表**

```bash
cd D:\Users\基金监控
python -m pytest tests/test_fund_watch.py --tb=line 2>&1 | grep "FAILED\|ERROR"
```

- [ ] **Step 2: 逐个修复失败的 import**

对于每个失败的 import：
- 如果函数已不存在且无替代，删除该测试用例
- 如果函数已改名/挪位置，更新 import 路径
- 如果测试用例测试的是已删除的功能（如 fund_alerts），直接删掉

- [ ] **Step 3: 运行测试验证**

```bash
cd D:\Users\基金监控
python -m pytest tests/test_fund_watch.py -v 2>&1 | tail -30
```

Expected: 所有剩余的测试用例 PASS。

- [ ] **Step 4: 提交**

```bash
git add tests/test_fund_watch.py
git commit -m "修复：清理 test_fund_watch.py 中失效的引用"
```

---

### Task 3: 统一管理文件路径常量

**Files:**
- Modify: `src/fund_recommend.py` — 使用统一路径变量
- Modify: `src/fund_server.py` — 使用统一路径变量
- Modify: `src/fund_render.py` — 使用统一路径变量
- Modify: `src/fund_utils.py` — 使用统一路径变量
- Modify: `src/fund_watch.py` — 使用统一路径变量
- Modify: `src/fund_manage.py` — 使用统一路径变量

**背景：** 目前各模块通过不同的方式拼接路径：
- `os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "fund_list.json")`
- `os.path.join(_PROJECT_ROOT, "data", "fund_list.json")`
- 直接硬编码 `"data/fund_list.json"`

统一到一个共享常量模块中。

- [ ] **Step 1: 在 config.py 中定义统一路径常量**

在 `src/config.py` 中添加：

```python
import os
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(_PROJECT_ROOT, "data", "config.json")
FUND_LIST_PATH = os.path.join(_PROJECT_ROOT, "data", "fund_list.json")
RECOMMEND_RESULT_PATH = os.path.join(_PROJECT_ROOT, ".fund_recommend_result.json")
HEARTBEATS_DIR = os.path.join(_PROJECT_ROOT, ".heartbeats")
```

导出这些常量（在模块级别定义即可被其他模块 import）。

- [ ] **Step 2: 替换 fund_recommend.py 中的路径引用**

找到所有 `.fund_recommend_result.json`、`fund_list.json` 的路径拼接，改为从 config 导入：

```python
from config import RECOMMEND_RESULT_PATH, FUND_LIST_PATH
```

- [ ] **Step 3: 替换 fund_server.py 中的路径引用**

同样替换 `_CONFIG_PATH`、`_FUND_LIST_PATH`、`_PROJECT_ROOT` 等。

- [ ] **Step 4: 替换其他模块中的路径引用**

同样替换 fund_render.py、fund_utils.py、fund_watch.py、fund_manage.py 中的路径。

- [ ] **Step 5: 运行测试验证**

```bash
cd D:\Users\基金监控
python -m pytest tests/ -k "test_fund_recommend or test_fund_server" -v 2>&1 | tail -20
```

Expected: 所有测试 PASS。

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "重构：统一管理文件路径常量到 config.py"
```

---

### Task 4: 修正 _SCRIPT_DIR 中的 Briefing 文件路径

**Files:**
- Modify: `src/fund_server.py` — 修正 Briefing 文件路径

**背景：** 第 388 行使用 `_SCRIPT_DIR`（即 `src/` 目录）拼接 `.briefing_fund.html` 路径，但该文件在项目根目录。

- [ ] **Step 1: 找到路径拼接的位置**

在 `fund_server.py` 中搜索 `.briefing_fund.html`，确认行号和拼接方式。

- [ ] **Step 2: 修正为根目录路径**

改为使用 `_PROJECT_ROOT`（根目录）拼接路径。

- [ ] **Step 3: 验证**

```bash
cd D:\Users\基金监控
python -c "import sys; sys.path.insert(0,'src'); from fund_server import Handler; print('OK')"
```

- [ ] **Step 4: 提交**

```bash
git add src/fund_server.py
git commit -m "修复：Briefing 文件路径指向根目录而非 src/"
```

---

### Task 5: 删除重复的 _update_day 定义

**Files:**
- Modify: `src/fund_recommend.py` — 删除死代码

**背景：** 函数 `_update_day` 在 `_re_score_and_refresh` 中定义，同时在 `main()` 的 `else` 分支中重复定义了第二个。第二个从未被外部调用（它只在同一 `else` 分支内被引用），是重构遗留物。

- [ ] **Step 1: 确认两个 _update_day 的位置**

第一个在第 471 行附近（在 `_re_score_and_refresh` 函数内），第二个在第 661 行附近（在 `main()` 的 `else` 分支内）。

- [ ] **Step 2: 删除第二个 _update_day**

删除第 661 行附近的 `def _update_day(code)` 定义及其内部的代码块。保留第 660 行开始的 `with ThreadPoolExecutor` 循环，该循环引用的 `_update_day` 是第一个定义（在 `_re_score_and_refresh` 中），且只适用于 `_HAS_TD = False` 的分支。

注意：第二个 `_update_day` 所在的 `else` 分支是 "td 维度未开启" 的路径。这个路径的 td 更新逻辑由 `_re_score_and_refresh` 中的同名函数处理。确认删除后不影响功能。

- [ ] **Step 3: 运行测试验证**

```bash
cd D:\Users\基金监控
python -m pytest tests/ -k "test_fund_recommend" -v 2>&1
```

Expected: 全部 PASS。

- [ ] **Step 4: 提交**

```bash
git add src/fund_recommend.py
git commit -m "清理：删除重复的 _update_day 函数定义"
```

---

### Task 6: 自选表首次加载加超时保护

**Files:**
- Modify: `src/fund_server.py` — 为 fund-table 每只基金的 HTTP 请求加超时

**背景：** `/api/fund-table` 在推荐缓存未命中时，每只基金都会拉取 pingzhongdata（约 400KB）。若某只基金的请求卡死，会阻塞整个 HTTP 线程。

- [ ] **Step 1: 确认超时设置位置**

在 `fund_server.py` 的 `_process_one` 函数（缓存未命中分支）中，`get_scoring_data(code)` 内部有网络请求，但外层没有总超时控制。

- [ ] **Step 2: 为 ThreadPoolExecutor 添加总超时**

在 `fut_map = {executor.submit(...)}` 之后添加：

```python
done, not_done = concurrent.futures.wait(fut_map.keys(), timeout=120)
for fut in not_done:
    fut.cancel()
```

这样超过 120 秒仍未完成的基金请求会被取消，不会卡死页面。

- [ ] **Step 3: 运行测试验证**

```bash
cd D:\Users\基金监控
python -m pytest tests/ -k "test_fund_server" -v 2>&1
```

Expected: 全部 PASS。

- [ ] **Step 4: 提交**

```bash
git add src/fund_server.py
git commit -m "优化：自选表加载添加 120 秒超时保护"
```

---

### Task 7: 日志多进程分离

**Files:**
- Modify: `src/fund_utils.py` — 日志配置
- Modify: `src/fund_monitor.py` — 日志配置  
- Modify: `src/fund_recommend.py` — 日志配置

**背景：** 目前多个进程可能同时写入同一个日志文件，内容交错混乱。

- [ ] **Step 1: 检查当前日志配置**

`fund_utils.py` 中设置了 `fund_watch.log` 的 RotatingFileHandler。`fund_monitor.py` 和 `fund_recommend.py` 也有自己的 logger。确认它们是否写入相同文件。

- [ ] **Step 2: 分离日志文件**

确保每个进程的日志写入不同文件：
- 监控进程 → `monitor.log`
- 推荐进程 → `recommend.log`
- 服务进程 → `server.log`

如果已经分离，则跳过此任务。

- [ ] **Step 3: 提交**

如果确实有改动：
```bash
git add src/fund_utils.py src/fund_monitor.py src/fund_recommend.py
git commit -m "修复：不同进程日志写入不同文件"
```

---

### Task 8: 限购检查失败加日志

**Files:**
- Modify: `src/fund_recommend.py` — `_check_limit` 函数

- [ ] **Step 1: 找到 _check_limit 函数位置**

在 `main()` 函数的限购检查阶段，找到 `_check_limit` 定义。

- [ ] **Step 2: 添加失败日志**

在 `_check_limit` 的 `except` 或失败返回路径中，添加 `log.warning` 记录失败的基金代码和原因。

- [ ] **Step 3: 提交**

```bash
git add src/fund_recommend.py
git commit -m "优化：限购检查失败时记录日志"
```

---

### Task 9: 热更新机制改为直接读配置

**Files:**
- Modify: `src/fund_server.py` — 替换 importlib.reload

**背景：** 保存权重/配置后，目前通过 `importlib.reload(fund_scoring)` 热加载模块。在多线程请求下存在竞态风险（一个请求正在评分，另一个请求触发 reload）。

- [ ] **Step 1: 确认 importlib.reload 的使用位置**

在 `fund_server.py` 的 `do_POST` 方法中搜索 `importlib.reload`，确认所有使用点。

- [ ] **Step 2: 改为子进程重启**

改为在保存配置后，重新启动推荐子进程来加载新配置，而不是在服务进程内热加载。对于评分权重，由前端下次请求时重新拉取评分（从推荐结果文件读取）。

具体：去掉 `importlib.reload` 调用，改为清除所有缓存（`_fund_table_cache = None`、`_recommend_table_cache["data"] = None`），下次请求时自动用新配置生成新结果。

- [ ] **Step 3: 验证**

保存权重后，确认页面显示新权重计算的评分。

- [ ] **Step 4: 提交**

```bash
git add src/fund_server.py
git commit -m "重构：移除 importlib.reload，改为清除缓存由下次请求重新生成"
```

---

### Task 10: 改进前端操作反馈

**Files:**
- Modify: `templates/fund_manage.html` — 增删基金、保存配置后的反馈

- [ ] **Step 1: 检查当前反馈机制**

增删基金后已有 `showMsg` 提示，但推荐运行时的进度展示不直观。

- [ ] **Step 2: 优化进度展示**

确保推荐运行时进度条每一步都更新：拉取排行 → 筛选 → 评分 → 保存 → 补拉自选基金。目前心跳已有各阶段信息，前端也已轮询，检查是否有延时或卡顿。

如果确认没有明显问题，此任务可跳过。

- [ ] **Step 3: 提交**

如有改动：
```bash
git add templates/fund_manage.html
git commit -m "优化：推荐运行进度展示"
```

---

### Task 11: 删除硬编码节假日列表

**Files:**
- Modify: `src/fund_utils.py` — 删除 FIXED_HOLIDAYS

- [ ] **Step 1: 删除 FIXED_HOLIDAYS 定义**

删除第 22-27 行的 `FIXED_HOLIDAYS` 字典。

- [ ] **Step 2: 删除 is_trading_day 中对 FIXED_HOLIDAYS 的引用**

删除第 79 行的 `if (d.month, d.day) in FIXED_HOLIDAYS` 判断。

- [ ] **Step 3: 验证**

```bash
cd D:\Users\基金监控
python -c "import sys; sys.path.insert(0,'src'); from fund_utils import is_trading_day; print(is_trading_day(__import__('datetime').date.today()))"
```

- [ ] **Step 4: 提交**

```bash
git add src/fund_utils.py
git commit -m "清理：删除硬编码的节假日列表（API优先，兜底按周末判断）"
```

---

### Task 12: 修复 mypy 类型检查跳过

**Files:**
- Modify: `src/fund_utils.py` — 去掉 `# mypy: ignore-errors`
- Modify: `src/fund_watch.py` — 去掉 `# mypy: ignore-errors`
- Modify: `src/fund_scoring.py` — 去掉 `# mypy: ignore-errors`
- Modify: `src/config.py` — 去掉 `# mypy: ignore-errors`

- [ ] **Step 1: 逐个去掉 ignore-errors 注释**

从 4 个文件的顶部删除 `# mypy: ignore-errors` 行。

- [ ] **Step 2: 运行 mypy 检查**

```bash
cd D:\Users\基金监控
python -m mypy src/ 2>&1 | head -30
```

Expected: 可能产生类型错误警告，但不会阻止正常功能。优先确保运行时正确性，类型标注可以逐步完善。

- [ ] **Step 3: 如果错误太多，恢复 ignore-errors 并单独忽略**

如果 mypy 报错过多，恢复 `# mypy: ignore-errors` 并标记此任务为暂缓。否则提交。

- [ ] **Step 4: 提交**

```bash
git add src/fund_utils.py src/fund_watch.py src/fund_scoring.py src/config.py
git commit -m "清理：移除 mypy: ignore-errors 注释"
```

---

### Task 13: 添加 conftest.py

**Files:**
- Create: `tests/conftest.py`

- [ ] **Step 1: 创建 conftest.py**

创建 `tests/conftest.py`，添加共享的 `sys.path.insert` 逻辑，避免每个测试文件都重复写：

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
```

- [ ] **Step 2: 清理测试文件中重复的 sys.path.insert**

从各测试文件中删除重复的 `sys.path.insert(0, ...)` 行（如有）。

- [ ] **Step 3: 运行测试验证**

```bash
cd D:\Users\基金监控
python -m pytest tests/ --collect-only 2>&1 | tail -5
```

Expected: 测试收集正常。

- [ ] **Step 4: 提交**

```bash
git add tests/conftest.py
git commit -m "测试：添加 conftest.py 共享路径配置"
```
