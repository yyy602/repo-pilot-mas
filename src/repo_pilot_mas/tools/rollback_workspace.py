"""Restore a candidate workspace to its immutable baseline."""

from __future__ import annotations

import shutil
from pathlib import Path

from repo_pilot_mas.runtime.path_guard import PathGuard, PathSecurityError
from repo_pilot_mas.runtime.workspace import Workspace
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.tools._common import ToolContext


def rollback_workspace(workspace: Workspace) -> ToolResult:
    """Discard all candidate changes and recreate the worktree from baseline."""

    context = ToolContext.start("rollback_workspace")
    try:
        container = Path(workspace.container_root).resolve(strict=True)
        root = Path(workspace.root).resolve(strict=False)
        baseline = Path(workspace.baseline_root).resolve(strict=True)
        expected_root = (container / "worktree").resolve(strict=False)
        expected_baseline = (container / "baseline").resolve(strict=True)
        if root != expected_root or baseline != expected_baseline:
            raise ValueError("workspace paths do not match the managed layout")
        PathGuard(baseline)

        if root.exists():
            shutil.rmtree(root)
        shutil.copytree(baseline, root)
        return context.success(
            data={
                "rolled_back": True,
                "workspace_id": workspace.workspace_id,
                "path": str(root),
            }
        )
    except (OSError, ValueError, PathSecurityError) as exc:
        return context.failure(code="ROLLBACK_WORKSPACE_ERROR", message=str(exc))
