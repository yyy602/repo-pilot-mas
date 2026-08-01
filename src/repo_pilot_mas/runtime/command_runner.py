"""Safe, timeout-aware subprocess execution without shell expansion."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

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
        self.allowed_executables = (
            self.DEFAULT_ALLOWED if allowed_executables is None else allowed_executables
        )
        self._trusted_executables = self._resolve_trusted_executables()

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
        normalized = self._normalize_command(command)
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
        deadline = started + timeout_seconds
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
        stdout_capture = _BoundedStreamCapture(max_output_chars)
        stderr_capture = _BoundedStreamCapture(max_output_chars)
        assert process.stdout is not None
        assert process.stderr is not None
        readers = [
            threading.Thread(
                target=_read_stream,
                args=(process.stdout, stdout_capture),
                daemon=True,
            ),
            threading.Thread(
                target=_read_stream,
                args=(process.stderr, stderr_capture),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        writer = None
        if input_text is not None:
            assert process.stdin is not None
            writer = threading.Thread(
                target=_write_stdin,
                args=(process.stdin, input_text),
                daemon=True,
            )
            writer.start()
        io_threads = [*readers, *([writer] if writer is not None else [])]
        try:
            process.wait(timeout=max(deadline - time.perf_counter(), 0.001))
            timed_out = False
            exit_code: int | None = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
            _terminate_process_tree(process)
            process.wait()

        if not _join_threads_until(io_threads, deadline):
            timed_out = True
            exit_code = None
            _terminate_process_tree(process)
            if process.poll() is None:
                process.wait()
            _join_threads_until(io_threads, time.perf_counter() + 1.0)

        duration_ms = int((time.perf_counter() - started) * 1000)
        stdout, out_truncated = stdout_capture.render()
        stderr, err_truncated = stderr_capture.render()
        return CommandExecution(
            command=normalized,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            truncated=out_truncated or err_truncated,
        )

    def _normalize_command(self, command: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(str(part) for part in command)
        command = normalized
        if not command:
            raise CommandPolicyError("command must not be empty")
        if any("\x00" in argument for argument in command):
            raise CommandPolicyError("command arguments must not contain NUL bytes")

        raw_executable = command[0]
        executable = Path(command[0]).name
        if _contains_path_component(raw_executable):
            try:
                requested_path = Path(raw_executable).expanduser().resolve(strict=True)
            except OSError as exc:
                raise CommandPolicyError(
                    f"command executable does not exist: {raw_executable}"
                ) from exc
            python_path = Path(sys.executable).resolve(strict=True)
            if requested_path == python_path and self.allowed_executables.intersection(
                {"python", "python3"}
            ):
                return (str(python_path), *command[1:])
        if executable not in self.allowed_executables:
            raise CommandPolicyError(f"command is not allowlisted: {executable}")

        trusted_path = self._trusted_executables.get(executable)
        if trusted_path is None:
            raise CommandPolicyError(f"allowlisted command is not available: {executable}")
        if _contains_path_component(raw_executable) and requested_path != trusted_path:
            raise CommandPolicyError(f"command executable is not trusted: {raw_executable}")

        return (str(trusted_path), *command[1:])

    def _resolve_trusted_executables(self) -> dict[str, Path]:
        trusted: dict[str, Path] = {}
        python_path = Path(sys.executable).resolve(strict=True)
        for executable in self.allowed_executables:
            if executable in {"python", "python3"}:
                trusted[executable] = python_path
                continue
            discovered = shutil.which(executable)
            if discovered is not None:
                trusted[executable] = Path(discovered).resolve(strict=True)
        return trusted


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def _contains_path_component(value: str) -> bool:
    return Path(value).is_absolute() or "/" in value or "\\" in value


def _join_threads_until(threads: Sequence[threading.Thread], deadline: float) -> bool:
    for thread in threads:
        thread.join(max(deadline - time.perf_counter(), 0.0))
    return not any(thread.is_alive() for thread in threads)


_TRUNCATION_MARKER = "\n... <output truncated> ...\n"


@dataclass(slots=True)
class _BoundedStreamCapture:
    """Keep bounded head/tail output while continuously draining a process pipe."""

    limit: int
    prefix: str = ""
    suffix: str = ""
    total_chars: int = 0

    @property
    def _payload_limit(self) -> int:
        return self.limit - len(_TRUNCATION_MARKER)

    @property
    def _prefix_limit(self) -> int:
        return int(self._payload_limit * 0.6)

    @property
    def _suffix_limit(self) -> int:
        return self._payload_limit - self._prefix_limit

    def append(self, value: str) -> None:
        self.total_chars += len(value)
        prefix_room = self._prefix_limit - len(self.prefix)
        if prefix_room > 0:
            self.prefix += value[:prefix_room]
            value = value[prefix_room:]
        if value:
            self.suffix = (self.suffix + value)[-self._suffix_limit :]

    def render(self) -> tuple[str, bool]:
        if self.total_chars <= self._payload_limit:
            return self.prefix + self.suffix, False
        return self.prefix + _TRUNCATION_MARKER + self.suffix, True


def _read_stream(stream: TextIO, capture: _BoundedStreamCapture) -> None:
    try:
        while chunk := stream.read(8192):
            capture.append(chunk)
    finally:
        stream.close()


def _write_stdin(stream: TextIO, value: str) -> None:
    try:
        stream.write(value)
        stream.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        stream.close()
