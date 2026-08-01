"""Restore a candidate workspace to its immutable baseline."""

from __future__ import annotations

from repo_pilot_mas.runtime.workspace import Workspace, restore_workspace
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.tools._common import ToolContext


def rollback_workspace(workspace: Workspace) -> ToolResult:
    """Discard all candidate changes and recreate the worktree from baseline."""

    context = ToolContext.start("rollback_workspace")
    try:
        restore_workspace(workspace)
        root = workspace.root
        return context.success(
            data={
                "rolled_back": True,
                "workspace_id": workspace.workspace_id,
                "path": str(root),
            }
        )
    except (OSError, ValueError) as exc:
        return context.failure(code="ROLLBACK_WORKSPACE_ERROR", message=str(exc))
