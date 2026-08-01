"""Keyword and regular-expression code search."""

from __future__ import annotations

import re
from pathlib import Path

from repo_pilot_mas.runtime.path_guard import PathGuard, PathSecurityError
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.tools._common import ToolContext, iter_files, safe_read_text


def search_code(
    root: str | Path,
    query: str,
    *,
    path: str | Path = ".",
    regex: bool = False,
    case_sensitive: bool = False,
    patterns: tuple[str, ...] = ("*",),
    max_results: int = 100,
    max_file_bytes: int = 1_000_000,
    max_line_chars: int = 500,
) -> ToolResult:
    """Search text files while bounding file size and result volume."""

    context = ToolContext.start("search_code")
    try:
        if not query:
            raise ValueError("query must not be empty")
        if max_results <= 0:
            raise ValueError("max_results must be positive")
        guard = PathGuard(root)
        search_root = guard.resolve(path)
        if not search_root.is_dir():
            raise ValueError("path must point to a directory")

        flags = 0 if case_sensitive else re.IGNORECASE
        expression = re.compile(query if regex else re.escape(query), flags)
        results: list[dict[str, object]] = []
        scanned_files = 0
        skipped_files: list[dict[str, str]] = []
        truncated = False

        for file_path in iter_files(search_root, patterns=patterns):
            scanned_files += 1
            try:
                text = safe_read_text(file_path, max_file_bytes=max_file_bytes)
            except (OSError, ValueError) as exc:
                skipped_files.append(
                    {
                        "path": file_path.relative_to(guard.root).as_posix(),
                        "reason": str(exc),
                    }
                )
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                match = expression.search(line)
                if not match:
                    continue
                displayed_line = line
                line_truncated = False
                if len(displayed_line) > max_line_chars:
                    displayed_line = displayed_line[:max_line_chars] + "..."
                    line_truncated = True
                results.append(
                    {
                        "path": file_path.relative_to(guard.root).as_posix(),
                        "line_number": line_number,
                        "column": match.start() + 1,
                        "line": displayed_line,
                        "line_truncated": line_truncated,
                    }
                )
                if len(results) >= max_results:
                    truncated = True
                    break
            if truncated:
                break

        return context.success(
            data={
                "query": query,
                "regex": regex,
                "results": results,
                "count": len(results),
                "scanned_files": scanned_files,
                "skipped_files": skipped_files,
            },
            truncated=truncated,
        )
    except re.error as exc:
        return context.failure(code="INVALID_REGEX", message=str(exc))
    except (OSError, ValueError, PathSecurityError) as exc:
        return context.failure(code="SEARCH_CODE_ERROR", message=str(exc))
