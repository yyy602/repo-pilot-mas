"""Shared helpers for deterministic tools."""

from __future__ import annotations

import fnmatch
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from repo_pilot_mas.schemas.tool_result import ToolResult, utc_now_iso

DEFAULT_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
    }
)


@dataclass(frozen=True, slots=True)
class ToolContext:
    tool: str
    trace_id: str
    started_at: str
    started_perf: float

    @classmethod
    def start(cls, tool: str) -> ToolContext:
        return cls(tool, uuid4().hex, utc_now_iso(), time.perf_counter())

    def success(self, **kwargs: object) -> ToolResult:
        return ToolResult.success(
            self.tool,
            trace_id=self.trace_id,
            started_at=self.started_at,
            duration_ms=self.duration_ms(),
            **kwargs,
        )

    def failure(self, *, code: str, message: str, **kwargs: object) -> ToolResult:
        return ToolResult.failure(
            self.tool,
            code=code,
            message=message,
            trace_id=self.trace_id,
            started_at=self.started_at,
            duration_ms=self.duration_ms(),
            **kwargs,
        )

    def duration_ms(self) -> int:
        return int((time.perf_counter() - self.started_perf) * 1000)


def is_binary_file(path: Path, *, sample_size: int = 8192) -> bool:
    try:
        with path.open("rb") as stream:
            sample = stream.read(sample_size)
    except OSError:
        return True
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    decoded = sample.decode("utf-8", errors="replace")
    replacement_ratio = decoded.count("\ufffd") / max(len(decoded), 1)
    return replacement_ratio > 0.10


def iter_files(
    root: Path,
    *,
    ignored_dirs: Iterable[str] = DEFAULT_IGNORED_DIRS,
    include_hidden: bool = False,
    patterns: tuple[str, ...] = ("*",),
) -> Iterator[Path]:
    ignored = set(ignored_dirs)
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if any(part in ignored for part in relative.parts[:-1]):
            continue
        if not include_hidden and any(part.startswith(".") for part in relative.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        matches_pattern = any(
            fnmatch.fnmatch(relative.as_posix(), pattern) for pattern in patterns
        )
        if patterns and not matches_pattern:
            continue
        yield path


def safe_read_text(path: Path, *, max_file_bytes: int) -> str:
    size = path.stat().st_size
    if size > max_file_bytes:
        raise ValueError(f"file exceeds size limit ({size} > {max_file_bytes} bytes)")
    if is_binary_file(path):
        raise ValueError("binary files are not supported")
    return path.read_text(encoding="utf-8", errors="replace")


def truncate_text(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    marker = "\n... <truncated> ...\n"
    remaining = max(limit - len(marker), 2)
    head = int(remaining * 0.65)
    tail = remaining - head
    return value[:head] + marker + value[-tail:], True


@dataclass(slots=True)
class BoundedTextBuffer:
    """Accumulate display text without retaining content beyond the configured limit."""

    limit: int
    _value: str = ""
    _prefix: str = ""
    _suffix: str = ""
    truncated: bool = False

    def append(self, value: str) -> None:
        if not value:
            return
        marker = "\n... <truncated> ...\n"
        payload_limit = max(self.limit - len(marker), 2)
        head_limit = int(payload_limit * 0.65)
        tail_limit = payload_limit - head_limit
        if self.truncated:
            self._suffix = (self._suffix + value)[-tail_limit:]
            return

        combined = self._value + value
        if len(combined) <= self.limit:
            self._value = combined
            return
        self.truncated = True
        self._prefix = combined[:head_limit]
        self._suffix = combined[-tail_limit:]
        self._value = ""

    def render(self) -> str:
        if not self.truncated:
            return self._value
        return self._prefix + "\n... <truncated> ...\n" + self._suffix


def protected_path_violations(
    changed_paths: Iterable[str],
    protected_paths: Sequence[str],
) -> list[str]:
    """Return changed paths equal to or nested below a protected repository path."""

    normalized_protected = tuple(_normalize_relative_path(path) for path in protected_paths)
    violations: list[str] = []
    for changed_path in changed_paths:
        normalized_changed = _normalize_relative_path(changed_path)
        if any(
            normalized_changed == protected
            or protected in normalized_changed.parents
            for protected in normalized_protected
        ):
            violations.append(normalized_changed.as_posix())
    return sorted(set(violations))


def _normalize_relative_path(value: str) -> PurePosixPath:
    raw_path = str(value).strip().replace("\\", "/")
    path = PurePosixPath(raw_path)
    if (
        not raw_path
        or "\x00" in raw_path
        or path.is_absolute()
        or raw_path == "."
        or any(part in {"..", ".git"} for part in path.parts)
    ):
        raise ValueError(f"unsafe repository path: {value!r}")
    return path
