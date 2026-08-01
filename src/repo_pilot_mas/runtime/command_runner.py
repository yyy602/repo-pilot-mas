"""Safe, timeout-aware subprocess execution without shell expansion."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from repo_pilot_mas.runtime.path_guard import PathGuard


@dataclass(frozen=True, slots=True)
class CommandExecution:
    command: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False


class CommandPolicyError(ValueError):
    """Raised when a command violates the execution allowlist."""


class CommandRunner:
    """Execute allowlisted commands inside a guarded workspace."""

    DEFAULT_ALLOWED = frozenset({"git", "python", "python3", "pytest", "py.test", "ruff"})

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        allowed_executables: frozenset[str] | None = None,
    ) -> None:
        self.guard = PathGuard(workspace_root)
        self.allowed_executables = allowed_executables or self.DEFAULT_ALLOWED

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path = ".",
        timeout_seconds: float = 30.0,
        max_output_chars: int = 20_000,
        input_text: str | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> CommandExecution:
        normalized = tuple(str(part) for part in command)
        self._validate_command(normalized)
        safe_cwd = self.guard.resolve(cwd, must_exist=True)
        if not safe_cwd.is_dir():
            raise CommandPolicyError("cwd must be a directory")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_output_chars < 200:
            raise ValueError("max_output_chars must be at least 200")

        env = os.environ.copy()
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "REPO_PILOT_NETWORK_DISABLED": "1",
            }
        )
        for key in (
            "http_proxy",
            "https_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "all_proxy",
        ):
            env.pop(key, None)
        if extra_env:
            env.update({str(key): str(value) for key, value in extra_env.items()})

        started = time.perf_counter()
        process = subprocess.Popen(
            normalized,
            cwd=safe_cwd,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            env=env,
            start_new_session=os.name == "posix",
        )
        try:
            stdout_raw, stderr_raw = process.communicate(input=input_text, timeout=timeout_seconds)
            timed_out = False
            exit_code: int | None = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
            _terminate_process_tree(process)
            stdout_raw, stderr_raw = process.communicate()

        duration_ms = int((time.perf_counter() - started) * 1000)
        stdout, out_truncated = _truncate(stdout_raw, max_output_chars)
        stderr, err_truncated = _truncate(stderr_raw, max_output_chars)
        return CommandExecution(
            command=normalized,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            truncated=out_truncated or err_truncated,
        )

    def _validate_command(self, command: tuple[str, ...]) -> None:
        if not command:
            raise CommandPolicyError("command must not be empty")
        executable = Path(command[0]).name
        if executable not in self.allowed_executables:
            raise CommandPolicyError(f"command is not allowlisted: {executable}")
        if any("\x00" in argument for argument in command):
            raise CommandPolicyError("command arguments must not contain NUL bytes")


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
    process.kill()


def _truncate(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    marker = "\n... <output truncated> ...\n"
    remaining = max(limit - len(marker), 2)
    head = int(remaining * 0.6)
    tail = remaining - head
    return value[:head] + marker + value[-tail:], True
