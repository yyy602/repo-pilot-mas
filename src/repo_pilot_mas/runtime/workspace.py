"""Isolated workspace creation and restoration."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from repo_pilot_mas.runtime.path_guard import PathGuard

_IGNORE_NAMES = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    ".venv",
    "venv",
}
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class Workspace:
    task_id: str
    workspace_id: str
    root: Path
    baseline_root: Path
    container_root: Path

    def guard(self) -> PathGuard:
        return PathGuard(self.root)


class WorkspaceManager:
    """Create one independent working copy per candidate patch."""

    def __init__(self, source_root: str | Path, workspace_root: str | Path) -> None:
        self.source_root = Path(source_root).expanduser().resolve(strict=True)
        if not self.source_root.is_dir():
            raise ValueError("source_root must be a directory")
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        if _is_within(self.workspace_root, self.source_root):
            raise ValueError("workspace_root must not be inside source_root")
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def create(self, task_id: str, workspace_id: str | None = None) -> Workspace:
        task_id = _validate_id(task_id, "task_id")
        workspace_id = _validate_id(workspace_id or uuid4().hex[:12], "workspace_id")
        container_root = self.workspace_root / task_id / workspace_id
        if container_root.exists():
            raise FileExistsError(f"workspace already exists: {container_root}")

        baseline_root = container_root / "baseline"
        worktree_root = container_root / "worktree"
        container_root.mkdir(parents=True)
        shutil.copytree(self.source_root, baseline_root, ignore=_ignore_entries)
        shutil.copytree(baseline_root, worktree_root)
        metadata = {
            "task_id": task_id,
            "workspace_id": workspace_id,
            "source_root": str(self.source_root),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (container_root / "workspace.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return Workspace(
            task_id=task_id,
            workspace_id=workspace_id,
            root=worktree_root,
            baseline_root=baseline_root,
            container_root=container_root,
        )

    def open(self, task_id: str, workspace_id: str) -> Workspace:
        task_id = _validate_id(task_id, "task_id")
        workspace_id = _validate_id(workspace_id, "workspace_id")
        container_root = (self.workspace_root / task_id / workspace_id).resolve(strict=True)
        baseline_root = (container_root / "baseline").resolve(strict=True)
        worktree_root = (container_root / "worktree").resolve(strict=True)
        return Workspace(task_id, workspace_id, worktree_root, baseline_root, container_root)

    def delete(self, workspace: Workspace) -> None:
        expected_parent = (self.workspace_root / workspace.task_id).resolve(strict=False)
        container = workspace.container_root.resolve(strict=True)
        if not _is_within(container, expected_parent):
            raise ValueError("workspace is outside the managed root")
        shutil.rmtree(container)


def _ignore_entries(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in _IGNORE_NAMES or name.endswith((".pyc", ".pyo"))}


def _validate_id(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized or not _ID_PATTERN.fullmatch(normalized) or ".." in normalized:
        raise ValueError(f"invalid {label}: {value!r}")
    return normalized


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False
