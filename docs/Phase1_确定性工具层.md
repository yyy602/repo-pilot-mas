# Phase 1：确定性代码工具层

> 文档性质：Phase 1 从属设计说明。整体架构、阶段状态和验收要求以 `docs/RepoPilot-MAS_完整计划.md` 唯一计划基线为准。
> 阶段状态：已于 2026-08-01 完成验收；证据见 `docs/Phase1_验收报告.md`。

## 1. 阶段目标

Phase 1 不接入大模型，也不实现 Supervisor 或动态任务图。本阶段只构建后续 Agent 可以可靠调用的确定性执行层，确保相同输入能够得到可解释、可测试和可追踪的结果。

主要交付包括：

1. 统一任务结构 `TaskSpec`；
2. 统一工具返回结构 `ToolResult`；
3. 路径安全边界 `PathGuard`；
4. 候选补丁隔离工作区 `WorkspaceManager`；
5. 九个必须工具；
6. 超时、输出截断、错误码和 Trace ID；
7. 覆盖正常流程与异常流程的单元测试。

## 2. 目录结构

```text
src/repo_pilot_mas/
├── schemas/
│   ├── task.py
│   └── tool_result.py
├── runtime/
│   ├── command_runner.py
│   ├── path_guard.py
│   └── workspace.py
└── tools/
    ├── list_files.py
    ├── search_code.py
    ├── inspect_code.py
    ├── find_references.py
    ├── run_tests.py
    ├── apply_patch.py
    ├── collect_diff.py
    ├── rollback_workspace.py
    └── static_check.py
```

## 3. 统一返回结构

每个工具都返回 `ToolResult`，不把可预期错误直接抛给 Agent。

主要字段：

| 字段 | 含义 |
| --- | --- |
| `tool` | 工具名称 |
| `ok` | 是否成功 |
| `data` | 结构化结果 |
| `error` | 机器可读错误码、消息和细节 |
| `stdout` / `stderr` | 命令标准输出和标准错误 |
| `exit_code` | 子进程退出码 |
| `command` | 实际执行的参数列表 |
| `duration_ms` | 调用耗时 |
| `truncated` | 输出是否被截断 |
| `trace_id` | 单次工具调用标识 |
| `started_at` | UTC 开始时间 |

## 4. 工作区模型

每个候选补丁使用独立目录：

```text
<workspace_root>/<task_id>/<workspace_id>/
├── workspace.json
├── baseline/     # 去除写权限并带内容摘要的恢复基线
└── worktree/     # Patch 和测试只允许作用于此处
```

`collect_diff` 始终比较 `baseline` 与 `worktree`。回滚和删除前均验证受管目录布局、元数据身份、符号链接状态和基线摘要；只有验证通过后才会从 `baseline` 恢复，因此不会把被篡改的基线当作恢复源。

## 5. 工具说明

### 5.1 `list_files`

- 确定性排序；
- 支持最大深度与最大条目数；
- 默认忽略 `.git`、缓存目录、虚拟环境和隐藏路径；
- 不跟随符号链接。

### 5.2 `search_code`

- 支持普通关键词和正则表达式；
- 支持大小写控制和文件模式过滤；
- 跳过二进制文件与超大文件；
- 限制结果数量和单行长度。

### 5.3 `inspect_code`

- 支持指定行区间；
- 支持定位 Python 函数、异步函数和类；
- 返回带行号文本；
- 限制最大输出行数。

### 5.4 `find_references`

- 使用 Python AST，不依赖简单子串匹配；
- 区分函数定义、类定义、导入、变量写入、名称引用和属性引用；
- 对语法错误文件进行记录而不是中断整个检索。

### 5.5 `run_tests`

- 仅允许 pytest 和 unittest 形式；
- 将解释器和可执行文件解析为受信绝对路径，拒绝同名程序伪造；
- 禁止 `python -c`；
- 不通过 Shell 执行；
- 支持超时、进程组终止和输出截断；
- 测试失败以结构化错误返回。

### 5.6 `apply_patch`

- 只接受文本 Unified Diff；
- 拒绝二进制 Patch；
- 解析并校验所有文件路径；
- 应用前拒绝对 `protected_paths` 的修改；
- 先执行 `git apply --check`，通过后再实际应用；
- 校验失败时不修改工作区。

### 5.7 `collect_diff`

- 比较 `baseline` 和 `worktree`；
- 识别新增、删除、修改和二进制变化；
- 返回 Unified Diff 与每个文件的大小变化；
- 流式比较文件并用有界缓冲区生成 Diff；
- 收集后再次检查 `protected_paths`，捕获绕过 Patch API 的直接修改。

### 5.8 `rollback_workspace`

- 校验工作区必须符合受管目录结构；
- 拒绝被符号链接替换的工作树和摘要不一致的基线；
- 只删除候选 `worktree`；
- 从 `baseline` 完整恢复；
- 不修改原始仓库。

### 5.9 `static_check`

- 使用 AST 检查所有 Python 文件的语法；
- Ruff 已安装时执行 `ruff check .`；
- Ruff 不可用时保留语法检查结果并标记未执行。

## 6. 安全约束

当前实现包含以下应用层约束：

- 只接受仓库内相对路径；
- 拒绝 `..`、绝对路径和越界符号链接；
- 禁止访问 `.git`；
- 子进程全部使用参数数组，禁止 `shell=True`；
- 外部命令采用白名单；
- 受信命令绑定到 Engine 环境解析出的绝对可执行路径；
- 清除代理环境变量，不提供 curl、wget、pip 等网络工具入口；
- 每个命令必须设置超时；
- 超时后终止整个进程组；
- 标准输出、标准错误和 Diff 在读取或生成过程中即受有界缓冲区限制；
- 每个候选 Patch 使用独立工作区；
- `protected_paths` 在 Patch 应用前和 Diff 收集后均检查；
- 原始仓库永远不作为 Patch 写入目标。

应用层白名单不能替代操作系统级沙箱。生产部署仍应在容器或网络命名空间中运行工具，并配置只读挂载、CPU/内存限制以及 `--network none`。

## 7. 验收标准

Phase 1 至少应验证：

- 九个工具均可从 `repo_pilot_mas.tools` 导入；
- 路径穿越和越界符号链接被拒绝；
- 二进制文件与超大输出受到限制；
- 测试命令可以成功、失败和超时；
- Patch 校验失败不污染工作区；
- Patch 成功后能够收集真实 Diff；
- 回滚后工作区重新变为 clean；
- Python 语法错误可以被 `static_check` 发现；
- 所有返回结果都包含 Trace ID 和结构化错误信息。

以上项目及唯一计划基线第 10.4 节的负向分支均已通过，最终命令、环境版本和逐项证据见 `docs/Phase1_验收报告.md`。
