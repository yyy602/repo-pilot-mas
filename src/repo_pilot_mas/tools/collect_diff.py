"""Collect the actual difference between a workspace and its baseline."""

from __future__ import annotations

import difflib
from pathlib import Path

from repo_pilot_mas.runtime.path_guard import PathGuard, PathSecurityError
from repo_pilot_mas.runtime.workspace import Workspace
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.tools._common import (
    DEFAULT_IGNORED_DIRS,
    ToolContext,
    is_binary_file,
    truncate_text,
)


def collect_diff(
    workspace: Workspace,
    *,
    max_diff_chars: int = 200_000,
    max_file_bytes: int = 2_000_000,
) -> ToolResult:
    """Return a unified diff and per-file change summary."""

    context = ToolContext.start("collect_diff")
    try:
        if max_diff_chars < 500:
            raise ValueError("max_diff_chars must be at least 500")
        container = Path(workspace.container_root).resolve(strict=True)
        worktree = Path(workspace.root).resolve(strict=True)
        baseline = Path(workspace.baseline_root).resolve(strict=True)
        if worktree != (container / "worktree").resolve(strict=True):
            raise ValueError("workspace root does not match the managed layout")
        if baseline != (container / "baseline").resolve(strict=True):
            raise ValueError("baseline root does not match the managed layout")
        PathGuard(worktree)
        PathGuard(baseline)

        baseline_files = _relative_files(baseline)
        current_files = _relative_files(worktree)
        all_paths = sorted(set(baseline_files) | set(current_files))
        chunks: list[str] = []
        file_changes: list[dict[str, object]] = []

        for relative in all_paths:
            old_path = baseline_files.get(relative)
            new_path = current_files.get(relative)
            old_bytes = old_path.read_bytes() if old_path else None
            new_bytes = new_path.read_bytes() if new_path else None
            if old_bytes == new_bytes:
                continue

            status = "modified"
            if old_path is None:
                status = "added"
            elif new_path is None:
                status = "deleted"
            binary = bool(
                (old_path and is_binary_file(old_path)) or (new_path and is_binary_file(new_path))
            )
            file_changes.append(
                {
                    "path": relative,
                    "status": status,
                    "binary": binary,
                    "old_size_bytes": len(old_bytes) if old_bytes is not None else 0,
                    "new_size_bytes": len(new_bytes) if new_bytes is not None else 0,
                }
            )
            if binary:
                chunks.append(f"Binary files a/{relative} and b/{relative} differ\n")
                continue
            if (old_bytes is not None and len(old_bytes) > max_file_bytes) or (
                new_bytes is not None and len(new_bytes) > max_file_bytes
            ):
                chunks.append(f"File {relative} differs but exceeds the diff size limit\n")
                continue

            old_text = old_bytes.decode("utf-8", errors="replace") if old_bytes is not None else ""
            new_text = new_bytes.decode("utf-8", errors="replace") if new_bytes is not None else ""
            from_file = f"a/{relative}" if old_path else "/dev/null"
            to_file = f"b/{relative}" if new_path else "/dev/null"
            diff_lines = difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=from_file,
                tofile=to_file,
                lineterm="\n",
            )
            chunks.append("".join(diff_lines))

        full_diff = "".join(chunks)
        displayed_diff, truncated = truncate_text(full_diff, max_diff_chars)
        return context.success(
            data={
                "diff": displayed_diff,
                "files": file_changes,
                "changed_count": len(file_changes),
                "is_clean": not file_changes,
            },
            truncated=truncated,
        )
    except (OSError, ValueError, PathSecurityError) as exc:
        return context.failure(code="COLLECT_DIFF_ERROR", message=str(exc))


def _relative_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in DEFAULT_IGNORED_DIRS for part in relative.parts):
            continue
        files[relative.as_posix()] = path
    return files
