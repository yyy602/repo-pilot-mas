"""Append-only structured artifacts shared through the Blackboard."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from repo_pilot_mas.schemas.tool_result import utc_now_iso

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class ArtifactType(str, Enum):
    EVIDENCE = "evidence"
    EVIDENCE_REVIEW = "evidence_review"
    HYPOTHESIS = "hypothesis"
    CHALLENGE = "challenge"
    REBUTTAL = "rebuttal"
    REVIEW = "review"
    PATCH_CANDIDATE = "patch_candidate"
    GENERATED_TEST = "generated_test"
    VALIDATION_RESULT = "validation_result"
    REPLAN_RECORD = "replan_record"
    ARTIFACT_REJECTION = "artifact_rejection"


@dataclass(frozen=True, slots=True)
class Artifact:
    artifact_id: str
    artifact_type: ArtifactType
    created_by: str
    content: Mapping[str, Any]
    version: int = 1
    status: str = "created"
    supersedes: str | None = None
    input_refs: tuple[str, ...] = ()
    trace_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if not _ID_PATTERN.fullmatch(self.artifact_id) or ".." in self.artifact_id:
            raise ValueError(f"invalid artifact_id: {self.artifact_id!r}")
        if not self.created_by.strip() or not self.status.strip():
            raise ValueError("created_by and status must not be empty")
        if isinstance(self.version, bool) or self.version <= 0:
            raise ValueError("artifact version must be positive")
        if not self.trace_id.strip():
            raise ValueError("trace_id must not be empty")
        object.__setattr__(self, "artifact_type", ArtifactType(self.artifact_type))
        object.__setattr__(self, "content", _freeze_json(self.content))
        object.__setattr__(self, "input_refs", tuple(str(item) for item in self.input_refs))

    @property
    def ref(self) -> str:
        return f"{self.artifact_id}@v{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type.value,
            "version": self.version,
            "status": self.status,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "supersedes": self.supersedes,
            "input_refs": list(self.input_refs),
            "trace_id": self.trace_id,
            "content": _thaw_json(self.content),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Artifact:
        content = value.get("content", {})
        if not isinstance(content, Mapping):
            raise TypeError("artifact content must be a mapping")
        return cls(
            artifact_id=str(value["artifact_id"]),
            artifact_type=ArtifactType(str(value["artifact_type"])),
            version=int(value.get("version", 1)),
            status=str(value.get("status", "created")),
            created_by=str(value["created_by"]),
            created_at=str(value.get("created_at", utc_now_iso())),
            supersedes=(str(value["supersedes"]) if value.get("supersedes") else None),
            input_refs=_strings(value.get("input_refs", ())),
            trace_id=str(value.get("trace_id", uuid4().hex)),
            content=dict(content),
        )


def _strings(value: Sequence[Any]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError("expected a sequence, not a string")
    return tuple(str(item) for item in value)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("artifact content keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("artifact content numbers must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"artifact content is not JSON-compatible: {type(value).__name__}")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value
