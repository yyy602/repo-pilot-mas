"""Timeout-aware test execution tool."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

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
    requested = tuple(command or (sys.executable, "-m", "pytest", "-q"))
    try:
        normalized = _normalize_test_command(requested)
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
            command=requested,
        )


def _normalize_test_command(command: tuple[str, ...]) -> tuple[str, ...]:
    if not command:
        raise ValueError("command must not be empty")
    raw_executable = command[0]
    executable = Path(raw_executable).name
    if _contains_path_component(raw_executable):
        _validate_python_executable(raw_executable)
        executable = "python"
    if executable in {"python", "python3"}:
        if len(command) < 3 or command[1] != "-m" or command[2] not in {"pytest", "unittest"}:
            raise CommandPolicyError("Python test commands must use '-m pytest' or '-m unittest'")
        if "-c" in command:
            raise CommandPolicyError("python -c is not allowed")
        return (sys.executable, *command[1:])
    if executable in {"pytest", "py.test"}:
        if _contains_path_component(raw_executable):
            raise CommandPolicyError("pytest executable paths are not allowed")
        return (sys.executable, "-m", "pytest", *command[1:])
    raise CommandPolicyError("only pytest and unittest commands are allowed")


def _validate_python_executable(value: str) -> None:
    if not _contains_path_component(value):
        return
    try:
        requested = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise CommandPolicyError(f"Python executable does not exist: {value}") from exc
    trusted = Path(sys.executable).resolve(strict=True)
    if requested != trusted:
        raise CommandPolicyError(f"Python executable is not trusted: {value}")


def _contains_path_component(value: str) -> bool:
    return Path(value).is_absolute() or "/" in value or "\\" in value
