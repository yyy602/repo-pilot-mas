"""Structured results returned by deterministic tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4


def utc_now_iso() -> str:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ToolError:
    """Machine-readable error information for a failed tool call."""

    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(slots=True)
class ToolResult:
    """Uniform result envelope used by every Phase 1 tool."""

    tool: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: ToolError | None = None
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    command: tuple[str, ...] | None = None
    duration_ms: int = 0
    truncated: bool = False
    trace_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def success(
        cls,
        tool: str,
        *,
        data: Mapping[str, Any] | None = None,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        command: tuple[str, ...] | None = None,
        duration_ms: int = 0,
        truncated: bool = False,
        trace_id: str | None = None,
        started_at: str | None = None,
    ) -> ToolResult:
        return cls(
            tool=tool,
            ok=True,
            data=dict(data or {}),
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            command=command,
            duration_ms=duration_ms,
            truncated=truncated,
            trace_id=trace_id or uuid4().hex,
            started_at=started_at or utc_now_iso(),
        )

    @classmethod
    def failure(
        cls,
        tool: str,
        *,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        command: tuple[str, ...] | None = None,
        duration_ms: int = 0,
        truncated: bool = False,
        trace_id: str | None = None,
        started_at: str | None = None,
    ) -> ToolResult:
        return cls(
            tool=tool,
            ok=False,
            data=dict(data or {}),
            error=ToolError(code=code, message=message, details=dict(details or {})),
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            command=command,
            duration_ms=duration_ms,
            truncated=truncated,
            trace_id=trace_id or uuid4().hex,
            started_at=started_at or utc_now_iso(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "data": self.data,
            "error": self.error.to_dict() if self.error else None,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "command": list(self.command) if self.command else None,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
            "trace_id": self.trace_id,
            "started_at": self.started_at,
        }
