# 基金监控项目 — 工作规则

## 核心原则：先写计划，批准后再动手

**任何代码改动、功能变更、格式调整，哪怕只改一行，都必须：**
1. 先写计划（todo_write + 文字描述）—— 改什么文件、改哪里、预期效果
2. 等用户说"可以"或"批准"
3. 批了才执行，执行时逐条标记完成

**例外：** 读文件、查代码、回答问题这类只读操作不需要计划。

## 工作纪律

1. **不跳步骤** — 不要因为"改动小"就跳过计划。用户已经反复强调过这一点。
2. **不在计划外顺手改东西** — 计划写什么就改什么，不加料。
3. **先确认再执行** — 用户只是问问题 / 提想法时，先回答问题，不要直接动手改代码。
4. **改完要验证** — 跑 mypy + pytest，确认不破坏任何东西。

## 已安装的 Skill（需要用户触发时我会提醒）

- `sdd-vibe-coding` — 规范驱动开发完整工作流
- `spec-proposal-creation-cn` — 创建提案
- `spec-implementation-cn` — 按计划实施
- `spec-archiving-cn` — 归档
- `spec-context-loading-cn` — 规范检索
- `writing-plans` — 写计划
- `executing-plans` — 执行计划
- `verification-before-completion` — 完成前验证

## 项目记忆

当前已保存的记忆（每次启动自动加载）：
- `must-plan-before-act` — 必须先计划后动手
- `project-purpose-reminder` — 项目目的
- `project-purpose` — 项目说明

## 代码规范

- 改 Python 代码后必须跑 `mypy` + `pytest`
- 不要提交临时文件（`_check*.py`、`patch_*.py`、`_debug*.py`）
- commit 信息写清楚改了什么
