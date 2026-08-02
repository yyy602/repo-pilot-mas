"""Structured, recoverable policy violations raised by the deterministic Engine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class DecisionPolicyViolation(ValueError):
    """Reject a Supervisor decision without crashing or terminating the Engine."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        recommended_stage: str | None = None,
        allowed_next_actions: Sequence[str] = (),
        trigger_artifact_refs: Sequence[str] = (),
        details: Mapping[str, Any] | None = None,
        recoverable: bool = True,
    ) -> None:
        if not code.strip() or not message.strip():
            raise ValueError("policy violation code and message must not be empty")
        super().__init__(message)
        self.code = code
        self.recoverable = bool(recoverable)
        self.recommended_stage = recommended_stage
        self.allowed_next_actions = tuple(
            dict.fromkeys(str(item) for item in allowed_next_actions if str(item))
        )
        self.trigger_artifact_refs = tuple(
            dict.fromkeys(str(item) for item in trigger_artifact_refs if str(item))
        )
        self.details = dict(details or {})
