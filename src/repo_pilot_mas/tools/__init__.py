"""Deterministic repository tools exposed to future agents."""

from repo_pilot_mas.tools.apply_patch import apply_patch
from repo_pilot_mas.tools.collect_diff import collect_diff
from repo_pilot_mas.tools.find_references import find_references
from repo_pilot_mas.tools.inspect_code import inspect_code
from repo_pilot_mas.tools.list_files import list_files
from repo_pilot_mas.tools.registry import (
    ToolDefinition,
    ToolRegistry,
    build_workspace_tool_registry,
)
from repo_pilot_mas.tools.rollback_workspace import rollback_workspace
from repo_pilot_mas.tools.run_tests import run_tests
from repo_pilot_mas.tools.search_code import search_code
from repo_pilot_mas.tools.static_check import static_check

__all__ = [
    "ToolDefinition",
    "ToolRegistry",
    "apply_patch",
    "build_workspace_tool_registry",
    "collect_diff",
    "find_references",
    "inspect_code",
    "list_files",
    "rollback_workspace",
    "run_tests",
    "search_code",
    "static_check",
]
