"""Append-only JSONL tracing for model, tool, and agent events."""

from __future__ import annotations

import json
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
            "data": dict(data),
        }
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
        return event_id
