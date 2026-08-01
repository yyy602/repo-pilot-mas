"""Python syntax and optional Ruff validation."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

from repo_pilot_mas.runtime.command_runner import CommandRunner
from repo_pilot_mas.runtime.path_guard import PathGuard, PathSecurityError
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.tools._common import ToolContext, iter_files, safe_read_text


def static_check(
    root: str | Path,
    *,
    path: str | Path = ".",
    run_ruff: bool = True,
    timeout_seconds: float = 60.0,
    max_output_chars: int = 20_000,
    max_file_bytes: int = 1_000_000,
) -> ToolResult:
    """Parse every Python file and run Ruff when it is installed."""

    context = ToolContext.start("static_check")
    try:
        guard = PathGuard(root)
        check_root = guard.resolve(path)
        if not check_root.is_dir():
            raise ValueError("path must point to a directory")

        checked_files = 0
        syntax_errors: list[dict[str, object]] = []
        skipped_files: list[dict[str, str]] = []
        for file_path in iter_files(check_root, patterns=("*.py",)):
            try:
                text = safe_read_text(file_path, max_file_bytes=max_file_bytes)
                ast.parse(text, filename=str(file_path))
                checked_files += 1
            except SyntaxError as exc:
                syntax_errors.append(
                    {
                        "path": file_path.relative_to(guard.root).as_posix(),
                        "line": exc.lineno,
                        "offset": exc.offset,
                        "message": exc.msg,
                    }
                )
            except (OSError, ValueError) as exc:
                skipped_files.append(
                    {
                        "path": file_path.relative_to(guard.root).as_posix(),
                        "reason": str(exc),
                    }
                )

        data: dict[str, object] = {
            "checked_files": checked_files,
            "syntax_errors": syntax_errors,
            "skipped_files": skipped_files,
            "ruff": {"requested": run_ruff, "executed": False},
        }
        if syntax_errors:
            return context.failure(
                code="STATIC_CHECK_FAILED",
                message=f"found {len(syntax_errors)} Python syntax error(s)",
                data=data,
            )

        if run_ruff and importlib.util.find_spec("ruff") is not None:
            runner = CommandRunner(root)
            execution = runner.run(
                (sys.executable, "-m", "ruff", "check", "."),
                cwd=path,
                timeout_seconds=timeout_seconds,
                max_output_chars=max_output_chars,
            )
            data["ruff"] = {
                "requested": True,
                "executed": True,
                "exit_code": execution.exit_code,
                "timed_out": execution.timed_out,
            }
            if execution.timed_out or execution.exit_code != 0:
                return ToolResult.failure(
                    "static_check",
                    code="STATIC_CHECK_FAILED",
                    message=(
                        f"Ruff exceeded {timeout_seconds} seconds"
                        if execution.timed_out
                        else f"Ruff exited with code {execution.exit_code}"
                    ),
                    data=data,
                    stdout=execution.stdout,
                    stderr=execution.stderr,
                    exit_code=execution.exit_code,
                    command=execution.command,
                    duration_ms=execution.duration_ms,
                    truncated=execution.truncated,
                    trace_id=context.trace_id,
                    started_at=context.started_at,
                )
        return context.success(data=data)
    except (OSError, ValueError, PathSecurityError) as exc:
        return context.failure(code="STATIC_CHECK_ERROR", message=str(exc))
