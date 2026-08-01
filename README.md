# RepoPilot-MAS

基于动态任务图与对抗审查的多智能体代码修复系统。

## 当前阶段

Phase 0、Phase 1 已完成；当前下一阶段为 Phase 2（模型适配层与 Single-Agent 基线），尚未开始。

当前版本暂不加载大模型。系统先把代码读取、检索、测试、补丁应用、差异收集和回滚等能力做成可测试的确定性工具，为后续 Single-Agent 和 Multi-Agent 调度提供可靠执行基础。阶段状态与后续验收以唯一计划基线为准。

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
- `WorkspaceManager`：为每个候选 Patch 创建独立的只读校验基线与可写工作树；
- `CommandRunner`：禁用 Shell 展开、绑定受信可执行文件、终止超时进程组并流式限制输出。

## 安装开发依赖

```bash
python -m pip install -e ".[dev]"
```

## 运行测试

```bash
conda activate multi_agent
python -m pytest
```

## 代码检查

```bash
conda activate multi_agent
python -m ruff check .
```

Phase 1 验收环境为 Python 3.10.20、pytest 9.1.1、Ruff 0.16.1；验收结果为 48 项测试通过、Ruff 无告警。详见 `docs/Phase1_验收报告.md`。

## 最小使用示例

```python
from pathlib import Path

from repo_pilot_mas.runtime import WorkspaceManager
from repo_pilot_mas.tools import apply_patch, collect_diff, run_tests

source_repo = Path("/path/to/buggy-repository")
manager = WorkspaceManager(source_repo, Path("/tmp/repo-pilot-workspaces"))
workspace = manager.create("task-001", "candidate-a")

protected_paths = ("tests",)
patch_result = apply_patch(workspace.root, patch_text, protected_paths=protected_paths)
if patch_result.ok:
    test_result = run_tests(workspace.root)
    diff_result = collect_diff(workspace, protected_paths=protected_paths)
```

## 项目计划

- 唯一计划基线（v2.1）：`docs/RepoPilot-MAS_完整计划.md`
- Phase 1 从属设计说明：`docs/Phase1_确定性工具层.md`
- Phase 1 验收报告：`docs/Phase1_验收报告.md`
