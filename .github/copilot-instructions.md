# Python 编码规范

## 类型注解
- 函数参数和返回值必须标注类型（Python 3.10+ 用 `|` 而非 `Optional`）
- 变量赋值尽量标注类型，尤其在模块作用域
- `None` 判断用 `is None` / `is not None`，不用 `== None`

## 异常处理
- 优先捕获具体异常类型，避免 `bare except`
- 使用 `try-except-else-finally` 结构，不要吞异常不加日志
- 网络请求异常统一用 `log.warning` 记录，不中断流程

## 命名规范
- 模块级常量用 `UPPER_CASE`
- 内部函数/变量用 `_` 前缀
- 类和类型别名用 `PascalCase`
- 普通函数和变量用 `snake_case`

## 导入规范
- 标准库 → 第三方库 → 本地模块，分组排序
- 优先用 `import X` 而非 `from X import *`
- 本地模块用相对导入时保持路径清晰

## 代码风格
- 字符串拼接用 f-string，不用 `%` 或 `+` 拼接
- 字典访问用 `.get(key, default)` 替代 `try-except KeyError`
- 列表/字典生成式优先于手动循环
- 类型判断用 `isinstance()` 而非 `type() ==`

## 并发
- 使用 `concurrent.futures.ThreadPoolExecutor`，控制 `max_workers`
- 线程间共享数据用 `threading.Lock()` 保护
- 子进程用 `subprocess.Popen` 时捕获 stderr
