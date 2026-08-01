"""Timeout-aware test execution tool."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

from repo_pilot_mas.runtime.command_runner import CommandPolicyError, CommandRunner
from repo_pilot_mas.runtime.path_guard import PathSecurityError
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.tools._common import ToolContext


def run_tests(
    root: str | Path,
    *,
    command: Sequence[str] | None = None,
    path: str | Path = ".",
    timeout_seconds: float = 120.0,
    max_output_chars: int = 30_000,
) -> ToolResult:
    """Run pytest or unittest without shell interpolation."""

    context = ToolContext.start("run_tests")
    normalized = tuple(command or (sys.executable, "-m", "pytest", "-q"))
    try:
        _validate_test_command(normalized)
        runner = CommandRunner(root)
        execution = runner.run(
            normalized,
            cwd=path,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )
        common = {
            "stdout": execution.stdout,
            "stderr": execution.stderr,
            "exit_code": execution.exit_code,
            "command": execution.command,
            "truncated": execution.truncated,
            "duration_ms": execution.duration_ms,
        }
        if execution.timed_out:
            return ToolResult.failure(
                "run_tests",
                code="TEST_TIMEOUT",
                message=f"test command exceeded {timeout_seconds} seconds",
                data={"timed_out": True, "passed": False},
                trace_id=context.trace_id,
                started_at=context.started_at,
                **common,
            )
        if execution.exit_code != 0:
            return ToolResult.failure(
                "run_tests",
                code="TEST_FAILED",
                message=f"test command exited with code {execution.exit_code}",
                data={"timed_out": False, "passed": False},
                trace_id=context.trace_id,
                started_at=context.started_at,
                **common,
            )
        return ToolResult.success(
            "run_tests",
            data={"timed_out": False, "passed": True},
            trace_id=context.trace_id,
            started_at=context.started_at,
            **common,
        )
    except (ValueError, CommandPolicyError, PathSecurityError, OSError) as exc:
        return context.failure(
            code="RUN_TESTS_ERROR",
            message=str(exc),
            command=normalized,
        )


def _validate_test_command(command: tuple[str, ...]) -> None:
    if not command:
        raise ValueError("command must not be empty")
    executable = Path(command[0]).name
    if executable in {"python", "python3"}:
        if len(command) < 3 or command[1] != "-m" or command[2] not in {"pytest", "unittest"}:
            raise CommandPolicyError("Python test commands must use '-m pytest' or '-m unittest'")
        if "-c" in command:
            raise CommandPolicyError("python -c is not allowed")
    elif executable not in {"pytest", "py.test"}:
        raise CommandPolicyError("only pytest and unittest commands are allowed")
