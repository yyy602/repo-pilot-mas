"""Collect the actual difference between a workspace and its baseline."""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from pathlib import Path

from repo_pilot_mas.runtime.workspace import Workspace, validate_workspace
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.tools._common import (
    DEFAULT_IGNORED_DIRS,
    BoundedTextBuffer,
    ToolContext,
    is_binary_file,
    protected_path_violations,
)


def collect_diff(
    workspace: Workspace,
    *,
    max_diff_chars: int = 200_000,
    max_file_bytes: int = 2_000_000,
    protected_paths: Sequence[str] = (),
) -> ToolResult:
    """Return a unified diff and per-file change summary."""

    context = ToolContext.start("collect_diff")
    try:
        if max_diff_chars < 500:
            raise ValueError("max_diff_chars must be at least 500")
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        validate_workspace(workspace)
        worktree = workspace.root
        baseline = workspace.baseline_root

        baseline_files = _relative_files(baseline)
        current_files = _relative_files(worktree)
        all_paths = sorted(set(baseline_files) | set(current_files))
        diff_buffer = BoundedTextBuffer(max_diff_chars)
        file_changes: list[dict[str, object]] = []

        for relative in all_paths:
            old_path = baseline_files.get(relative)
            new_path = current_files.get(relative)
            if _files_equal(old_path, new_path):
                continue

            old_size = old_path.stat().st_size if old_path else 0
            new_size = new_path.stat().st_size if new_path else 0
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
                    "old_size_bytes": old_size,
                    "new_size_bytes": new_size,
                }
            )
            if binary:
                diff_buffer.append(f"Binary files a/{relative} and b/{relative} differ\n")
                continue
            if old_size > max_file_bytes or new_size > max_file_bytes:
                diff_buffer.append(
                    f"File {relative} differs but exceeds the diff size limit "
                    f"({old_size} -> {new_size} bytes)\n"
                )
                continue

            old_bytes = old_path.read_bytes() if old_path else None
            new_bytes = new_path.read_bytes() if new_path else None
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
            for line in diff_lines:
                diff_buffer.append(line)

        data = {
            "diff": diff_buffer.render(),
            "files": file_changes,
            "changed_count": len(file_changes),
            "is_clean": not file_changes,
        }
        violations = protected_path_violations(
            (str(change["path"]) for change in file_changes),
            protected_paths,
        )
        data["protected_path_violations"] = violations
        if violations:
            return context.failure(
                code="PROTECTED_PATH_VIOLATION",
                message="workspace modifies one or more protected paths",
                data=data,
                truncated=diff_buffer.truncated,
            )
        return context.success(data=data, truncated=diff_buffer.truncated)
    except (OSError, ValueError) as exc:
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


def _files_equal(old_path: Path | None, new_path: Path | None) -> bool:
    if old_path is None or new_path is None:
        return old_path is new_path
    if old_path.stat().st_size != new_path.stat().st_size:
        return False
    with old_path.open("rb") as old_stream, new_path.open("rb") as new_stream:
        while old_chunk := old_stream.read(64 * 1024):
            if old_chunk != new_stream.read(len(old_chunk)):
                return False
        return not new_stream.read(1)
