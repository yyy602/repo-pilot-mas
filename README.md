# RepoPilot-MAS

基于动态任务图与对抗审查的多智能体代码修复系统。

## 当前阶段

Phase 1：任务数据格式、隔离工作区和确定性代码工具层。

当前版本暂不加载大模型。系统先把代码读取、检索、测试、补丁应用、差异收集和回滚等能力做成可测试的确定性工具，为后续 Single-Agent 和 Multi-Agent 调度提供可靠执行基础。

## 已实现工具

| 工具 | 功能 |
| --- | --- |
| `list_files` | 在安全路径边界内列出仓库目录 |
| `search_code` | 关键词或正则检索文本代码 |
| `inspect_code` | 查看文件行区间或 Python 符号上下文 |
| `find_references` | 基于 Python AST 查找符号定义与引用 |
| `run_tests` | 在超时和命令白名单约束下运行 pytest 或 unittest |
| `apply_patch` | 先校验再使用 `git apply` 应用 Unified Diff |
| `collect_diff` | 比较候选工作区与只读基线并生成差异 |
| `rollback_workspace` | 将候选工作区恢复到初始基线 |
| `static_check` | 执行 Python 语法检查，并在可用时运行 Ruff |

## 核心基础设施

- `TaskSpec`：代码修复任务的统一输入结构；
- `ToolResult`：包含状态、错误、标准输出、退出码、耗时、截断标记和 Trace ID；
- `PathGuard`：拒绝绝对路径、目录穿越、`.git` 访问和越界符号链接；
- `WorkspaceManager`：为每个候选 Patch 创建独立的 `baseline` 与 `worktree`；
- `CommandRunner`：禁用 Shell 展开、限制命令类型、控制超时并截断过长输出。

## 安装开发依赖

```bash
python -m pip install -e ".[dev]"
```

## 运行测试

```bash
python -m pytest
```

## 代码检查

```bash
python -m ruff check .
```

## 最小使用示例

```python
from pathlib import Path

from repo_pilot_mas.runtime import WorkspaceManager
from repo_pilot_mas.tools import apply_patch, collect_diff, run_tests

source_repo = Path("/path/to/buggy-repository")
manager = WorkspaceManager(source_repo, Path("/tmp/repo-pilot-workspaces"))
workspace = manager.create("task-001", "candidate-a")

patch_result = apply_patch(workspace.root, patch_text)
if patch_result.ok:
    test_result = run_tests(workspace.root)
    diff_result = collect_diff(workspace)
```

## 项目计划

- 完整开发计划：`docs/RepoPilot-MAS_完整计划.md`
- Phase 1 设计说明：`docs/Phase1_确定性工具层.md`
