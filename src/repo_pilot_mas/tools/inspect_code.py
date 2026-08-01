"""File-range and Python-symbol inspection."""

from __future__ import annotations

import ast
from pathlib import Path

from repo_pilot_mas.runtime.path_guard import PathGuard, PathSecurityError
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.tools._common import ToolContext, safe_read_text


def inspect_code(
    root: str | Path,
    file_path: str | Path,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    symbol: str | None = None,
    context_lines: int = 3,
    max_lines: int = 300,
    max_file_bytes: int = 1_000_000,
) -> ToolResult:
    """Inspect a line range or a function/class definition with context."""

    context = ToolContext.start("inspect_code")
    try:
        if start_line <= 0:
            raise ValueError("start_line must be positive")
        if context_lines < 0:
            raise ValueError("context_lines must be non-negative")
        if max_lines <= 0:
            raise ValueError("max_lines must be positive")

        guard = PathGuard(root)
        target = guard.resolve(file_path)
        if not target.is_file():
            raise ValueError("file_path must point to a file")
        text = safe_read_text(target, max_file_bytes=max_file_bytes)
        lines = text.splitlines()
        symbol_info: dict[str, object] | None = None

        if symbol:
            if target.suffix != ".py":
                raise ValueError("symbol inspection currently supports Python files only")
            tree = ast.parse(text, filename=str(target))
            node = _find_symbol(tree, symbol)
            if node is None:
                raise ValueError(f"symbol not found: {symbol}")
            node_start = getattr(node, "lineno", 1)
            node_end = getattr(node, "end_lineno", node_start)
            start = max(node_start - context_lines, 1)
            end = min(node_end + context_lines, len(lines))
            symbol_info = {
                "name": symbol,
                "kind": type(node).__name__,
                "definition_start": node_start,
                "definition_end": node_end,
            }
        else:
            start = start_line
            if not lines and start == 1:
                end = 0
            else:
                if start > len(lines):
                    raise ValueError(
                        f"start_line {start} exceeds file length {len(lines)}"
                    )
                end = (
                    end_line
                    if end_line is not None
                    else min(start + max_lines - 1, len(lines))
                )
                if end < start:
                    raise ValueError("end_line must be greater than or equal to start_line")
                end = min(end, len(lines))

        requested_count = max(end - start + 1, 0)
        truncated = requested_count > max_lines
        if truncated:
            end = start + max_lines - 1
        selected = [
            {"line_number": number, "text": lines[number - 1]}
            for number in range(start, min(end, len(lines)) + 1)
        ]
        numbered_text = "\n".join(
            f"{item['line_number']:>6} | {item['text']}" for item in selected
        )
        return context.success(
            data={
                "path": target.relative_to(guard.root).as_posix(),
                "start_line": start,
                "end_line": end,
                "total_lines": len(lines),
                "symbol": symbol_info,
                "lines": selected,
                "text": numbered_text,
            },
            truncated=truncated,
        )
    except SyntaxError as exc:
        return context.failure(
            code="SYNTAX_ERROR",
            message=str(exc),
            details={"line": exc.lineno, "offset": exc.offset},
        )
    except (OSError, ValueError, PathSecurityError) as exc:
        return context.failure(code="INSPECT_CODE_ERROR", message=str(exc))


def _find_symbol(tree: ast.AST, symbol: str) -> ast.AST | None:
    target = symbol.rsplit(".", 1)[-1]
    candidates: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
            node.name == target
        ):
            candidates.append(node)
    return min(candidates, key=lambda item: getattr(item, "lineno", 0)) if candidates else None
