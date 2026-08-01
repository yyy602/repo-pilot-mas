"""Structured terminal report for one Single-Agent task run."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class FinalReport:
    task_id: str
    status: Literal["succeeded", "failed"]
    reason: str
    model_id: str
    workspace_id: str | None
    patch_sha256: str | None
    changed_files: tuple[str, ...]
    validations: Mapping[str, Any]
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    duration_ms: int
    trace_path: str
    started_at: str
    finished_at: str
    report_path: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_id or not self.reason or not self.model_id:
            raise ValueError("task_id, reason, and model_id must not be empty")
        if self.status not in {"succeeded", "failed"}:
            raise ValueError(f"unsupported final status: {self.status}")
        if min(
            self.model_calls,
            self.tool_calls,
            self.input_tokens,
            self.output_tokens,
            self.duration_ms,
        ) < 0:
            raise ValueError("FinalReport counters must be non-negative")
        object.__setattr__(self, "changed_files", tuple(self.changed_files))
        object.__setattr__(self, "validations", dict(self.validations))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "reason": self.reason,
            "model_id": self.model_id,
            "workspace_id": self.workspace_id,
            "patch_sha256": self.patch_sha256,
            "changed_files": list(self.changed_files),
            "validations": dict(self.validations),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "token_usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
            },
            "duration_ms": self.duration_ms,
            "trace_path": self.trace_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "report_path": self.report_path,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FinalReport:
        usage = value.get("token_usage", {})
        changed_files = _as_strings(value.get("changed_files", ()))
        return cls(
            task_id=str(value["task_id"]),
            status=str(value["status"]),
            reason=str(value["reason"]),
            model_id=str(value["model_id"]),
            workspace_id=(
                str(value["workspace_id"]) if value.get("workspace_id") is not None else None
            ),
            patch_sha256=(
                str(value["patch_sha256"]) if value.get("patch_sha256") is not None else None
            ),
            changed_files=changed_files,
            validations=dict(value.get("validations", {})),
            model_calls=int(value.get("model_calls", 0)),
            tool_calls=int(value.get("tool_calls", 0)),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            duration_ms=int(value.get("duration_ms", 0)),
            trace_path=str(value["trace_path"]),
            started_at=str(value["started_at"]),
            finished_at=str(value["finished_at"]),
            report_path=str(value.get("report_path", "")),
            metadata=dict(value.get("metadata", {})),
        )


def _as_strings(value: Sequence[Any]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError("expected a sequence of strings")
    return tuple(str(item) for item in value)
