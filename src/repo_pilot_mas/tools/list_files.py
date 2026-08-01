"""Repository directory listing tool."""

from __future__ import annotations

from pathlib import Path

from repo_pilot_mas.runtime.path_guard import PathGuard, PathSecurityError
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.tools._common import DEFAULT_IGNORED_DIRS, ToolContext


def list_files(
    root: str | Path,
    *,
    path: str | Path = ".",
    max_depth: int = 4,
    max_entries: int = 500,
    include_hidden: bool = False,
) -> ToolResult:
    """List files and directories below a guarded repository path."""

    context = ToolContext.start("list_files")
    try:
        if max_depth < 0:
            raise ValueError("max_depth must be non-negative")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        guard = PathGuard(root)
        start = guard.resolve(path)
        if not start.is_dir():
            raise ValueError("path must point to a directory")

        entries: list[dict[str, object]] = []
        truncated = False
        stack: list[tuple[Path, int]] = [(start, 0)]
        while stack:
            directory, depth = stack.pop()
            children = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name))
            directories_to_visit: list[Path] = []
            for child in children:
                relative_to_root = child.relative_to(guard.root)
                if any(part in DEFAULT_IGNORED_DIRS for part in relative_to_root.parts):
                    continue
                is_hidden = any(
                    part.startswith(".") for part in relative_to_root.parts
                )
                if not include_hidden and is_hidden:
                    continue
                if child.is_symlink():
                    entry_type = "symlink"
                elif child.is_dir():
                    entry_type = "directory"
                elif child.is_file():
                    entry_type = "file"
                else:
                    entry_type = "other"
                entry: dict[str, object] = {
                    "path": relative_to_root.as_posix(),
                    "type": entry_type,
                    "depth": depth,
                }
                if entry_type == "file":
                    entry["size_bytes"] = child.stat().st_size
                entries.append(entry)
                if len(entries) >= max_entries:
                    truncated = True
                    break
                if entry_type == "directory" and depth < max_depth:
                    directories_to_visit.append(child)
            if truncated:
                break
            stack.extend((child, depth + 1) for child in reversed(directories_to_visit))

        return context.success(
            data={
                "path": start.relative_to(guard.root).as_posix(),
                "entries": entries,
                "count": len(entries),
            },
            truncated=truncated,
        )
    except (OSError, ValueError, PathSecurityError) as exc:
        return context.failure(code="LIST_FILES_ERROR", message=str(exc))
