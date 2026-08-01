"""Agent-visible tool registry bound to one immutable task and workspace."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from repo_pilot_mas.runtime.workspace import Workspace
from repo_pilot_mas.schemas.json_schema import SchemaValidationError, validate_json_schema
from repo_pilot_mas.schemas.task import TaskSpec
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.tools.apply_patch import apply_patch
from repo_pilot_mas.tools.collect_diff import collect_diff
from repo_pilot_mas.tools.find_references import find_references
from repo_pilot_mas.tools.inspect_code import inspect_code
from repo_pilot_mas.tools.list_files import list_files
from repo_pilot_mas.tools.rollback_workspace import rollback_workspace
from repo_pilot_mas.tools.run_tests import run_tests
from repo_pilot_mas.tools.search_code import search_code
from repo_pilot_mas.tools.static_check import static_check

ToolHandler = Callable[[Mapping[str, Any]], ToolResult]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: ToolHandler

    def for_model(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if not definition.name.strip():
            raise ValueError("tool name must not be empty")
        if definition.name in self._tools:
            raise ValueError(f"tool is already registered: {definition.name}")
        self._tools[definition.name] = definition

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def definitions_for_model(self) -> list[dict[str, Any]]:
        return [self._tools[name].for_model() for name in self.names]

    def invoke(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        definition = self._tools.get(name)
        if definition is None:
            return ToolResult.failure(
                name or "tool_registry",
                code="TOOL_NOT_REGISTERED",
                message=f"tool is not registered: {name}",
                data={"registered_tools": list(self.names)},
            )
        if not isinstance(arguments, Mapping):
            return ToolResult.failure(
                name,
                code="TOOL_ARGUMENT_ERROR",
                message="tool arguments must be a JSON object",
            )
        try:
            validate_json_schema(arguments, definition.parameters)
            return definition.handler(arguments)
        except (SchemaValidationError, TypeError, ValueError) as exc:
            return ToolResult.failure(
                name,
                code="TOOL_ARGUMENT_ERROR",
                message=str(exc),
            )


def build_workspace_tool_registry(task: TaskSpec, workspace: Workspace) -> ToolRegistry:
    """Bind all nine Phase 1 tools without exposing task-level security arguments."""

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            "list_files",
            "列出仓库内文件和目录。",
            _object_schema(
                {
                    "path": {"type": "string"},
                    "max_depth": {"type": "integer"},
                    "max_entries": {"type": "integer"},
                }
            ),
            lambda args: list_files(workspace.root, **dict(args)),
        )
    )
    registry.register(
        ToolDefinition(
            "search_code",
            "在文本代码中搜索关键词或正则。",
            _object_schema(
                {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "regex": {"type": "boolean"},
                    "case_sensitive": {"type": "boolean"},
                    "max_results": {"type": "integer"},
                },
                required=("query",),
            ),
            lambda args: search_code(workspace.root, **dict(args)),
        )
    )
    registry.register(
        ToolDefinition(
            "inspect_code",
            "查看文件行区间或 Python 符号。",
            _object_schema(
                {
                    "file_path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "symbol": {"type": "string"},
                    "context_lines": {"type": "integer"},
                    "max_lines": {"type": "integer"},
                },
                required=("file_path",),
            ),
            lambda args: inspect_code(workspace.root, **dict(args)),
        )
    )
    registry.register(
        ToolDefinition(
            "find_references",
            "查找 Python 符号定义和引用。",
            _object_schema(
                {
                    "symbol": {"type": "string"},
                    "path": {"type": "string"},
                    "include_definitions": {"type": "boolean"},
                    "max_results": {"type": "integer"},
                },
                required=("symbol",),
            ),
            lambda args: find_references(workspace.root, **dict(args)),
        )
    )
    registry.register(
        ToolDefinition(
            "run_tests",
            "运行 TaskSpec 固定的目标测试或完整回归测试；不能指定命令。",
            _object_schema(
                {"scope": {"type": "string", "enum": ["target", "full"]}},
                required=("scope",),
            ),
            lambda args: run_tests(
                workspace.root,
                command=task_test_command(task, target=args["scope"] == "target"),
                timeout_seconds=task.max_runtime_seconds,
            ),
        )
    )
    registry.register(
        ToolDefinition(
            "apply_patch",
            "应用文本 Unified Diff；受保护路径由 Engine 固定。",
            _object_schema(
                {"patch_text": {"type": "string"}},
                required=("patch_text",),
            ),
            lambda args: apply_patch(
                workspace.root,
                str(args["patch_text"]),
                timeout_seconds=task.max_runtime_seconds,
                protected_paths=task.protected_paths,
            ),
        )
    )
    registry.register(
        ToolDefinition(
            "collect_diff",
            "收集候选工作区相对只读基线的真实差异。",
            _object_schema({}),
            lambda _args: collect_diff(workspace, protected_paths=task.protected_paths),
        )
    )
    registry.register(
        ToolDefinition(
            "rollback_workspace",
            "将候选工作区恢复到已校验的只读基线。",
            _object_schema({}),
            lambda _args: rollback_workspace(workspace),
        )
    )
    registry.register(
        ToolDefinition(
            "static_check",
            "执行 Python 语法检查和 Ruff。",
            _object_schema({}),
            lambda _args: static_check(
                workspace.root,
                timeout_seconds=task.max_runtime_seconds,
            ),
        )
    )
    return registry


def task_test_command(task: TaskSpec, *, target: bool) -> tuple[str, ...]:
    command = task.test_command
    if not target or not task.failing_tests:
        return command
    if len(command) >= 3 and command[1:3] == ("-m", "pytest"):
        return (*command, *task.failing_tests)
    if command and command[0] in {"pytest", "py.test"}:
        return (*command, *task.failing_tests)
    return command


def _object_schema(
    properties: Mapping[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }
