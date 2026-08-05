from __future__ import annotations

from pathlib import Path

from repo_pilot_mas.agents.investigator import _investigator_payload_schema
from repo_pilot_mas.schemas.json_schema import validate_json_schema


def test_code_retrieval_schema_does_not_require_reproduction() -> None:
    schema = _investigator_payload_schema("code_retrieval")
    assert "reproduction" not in schema["properties"]

    validate_json_schema(
        {
            "mode": "code_retrieval",
            "claim": "located source code",
            "source": {"path": "target.py", "line_start": 1, "line_end": 3},
            "content": "source",
            "observation_type": "direct",
            "confidence": 1.0,
            "status": "verified",
            "tool_trace_ids": ["trace"],
            "missing_evidence": [],
        },
        schema,
    )


def test_failure_reproduction_schema_is_only_runtime_injected() -> None:
    schema = _investigator_payload_schema("failure_reproduction")
    assert "reproduction" not in schema["properties"]

    validate_json_schema(
        {
            "mode": "failure_reproduction",
            "claim": "test reproduced failure",
            "source": {"path": "target.py", "line_start": 1, "line_end": 3},
            "content": "IndexError",
            "observation_type": "direct",
            "confidence": 1.0,
            "status": "verified",
            "tool_trace_ids": ["trace"],
            "missing_evidence": [],
        },
        schema,
    )
