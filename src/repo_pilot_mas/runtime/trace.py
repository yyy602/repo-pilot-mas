"""Append-only JSONL tracing for model, tool, and agent events."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from repo_pilot_mas.schemas.tool_result import utc_now_iso


class TraceWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(
        self,
        event_type: str,
        data: Mapping[str, Any],
        *,
        trace_id: str | None = None,
    ) -> str:
        if not event_type.strip():
            raise ValueError("event_type must not be empty")
        event_id = uuid4().hex
        payload = {
            "event_id": event_id,
            "event_type": event_type,
            "created_at": utc_now_iso(),
            "trace_id": trace_id or uuid4().hex,
            "data": redact_secrets(dict(data)),
        }
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
        return event_id


def redact_secrets(value: Any) -> Any:
    """Recursively redact loaded DashScope credentials from serializable values."""

    secrets = tuple(
        secret
        for name, secret in os.environ.items()
        if name.startswith("DASHSCOPE_API_KEY_") and secret
    )
    return _redact_value(value, secrets)


def _redact_value(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
    if isinstance(value, Mapping):
        return {
            _redact_value(key, secrets): _redact_value(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, secrets) for item in value]
    return value
