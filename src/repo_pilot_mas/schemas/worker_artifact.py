"""Phase 4 Worker artifact schemas and cross-reference validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from repo_pilot_mas.schemas.artifact import Artifact, ArtifactType
from repo_pilot_mas.schemas.json_schema import validate_json_schema

INVESTIGATOR_MODES = (
    "code_retrieval",
    "failure_reproduction",
    "dependency_trace",
    "evidence_completion",
    "regression_scope",
)
DIAGNOSIS_PERSPECTIVES = ("control_flow", "data_flow")
REVIEWER_MODES = (
    "evidence_review",
    "hypothesis_comparison",
    "challenge_quality",
    "root_cause_recommendation",
    "patch_review",
    "final_risk_review",
)
PATCH_STRATEGIES = ("minimal", "robust")

_TEXT = {"type": "string", "minLength": 1}
_TEXTS = {"type": "array", "items": _TEXT}
_NON_EMPTY_TEXTS = {"type": "array", "items": _TEXT, "minItems": 1}
_CONFIDENCE = {"type": "number", "minimum": 0, "maximum": 1}

EVIDENCE_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": list(INVESTIGATOR_MODES)},
        "claim": _TEXT,
        "source": {
            "type": "object",
            "properties": {
                "path": _TEXT,
                "line_start": {"type": "integer", "minimum": 1},
                "line_end": {"type": "integer", "minimum": 1},
            },
            "required": ["path", "line_start", "line_end"],
            "additionalProperties": False,
        },
        "content": _TEXT,
        "observation_type": {
            "type": "string",
            "enum": ["direct", "derived", "hypothesis"],
        },
        "confidence": _CONFIDENCE,
        "status": {"type": "string", "enum": ["unverified", "verified", "rejected"]},
        "tool_trace_ids": _NON_EMPTY_TEXTS,
        "missing_evidence": _TEXTS,
    },
    "required": [
        "mode",
        "claim",
        "source",
        "content",
        "observation_type",
        "confidence",
        "status",
        "tool_trace_ids",
        "missing_evidence",
    ],
    "additionalProperties": False,
}

HYPOTHESIS_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "perspective": {"type": "string", "enum": list(DIAGNOSIS_PERSPECTIVES)},
        "root_cause": _TEXT,
        "direct_cause": _TEXT,
        "supporting_evidence": _NON_EMPTY_TEXTS,
        "counter_evidence": _TEXTS,
        "affected_symbols": _NON_EMPTY_TEXTS,
        "verification_plan": _NON_EMPTY_TEXTS,
        "missing_evidence": _TEXTS,
        "confidence": _CONFIDENCE,
    },
    "required": [
        "perspective",
        "root_cause",
        "direct_cause",
        "supporting_evidence",
        "counter_evidence",
        "affected_symbols",
        "verification_plan",
        "missing_evidence",
        "confidence",
    ],
    "additionalProperties": False,
}

REVIEW_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": list(REVIEWER_MODES)},
        "target_artifact_ref": _TEXT,
        "evidence_refs": _NON_EMPTY_TEXTS,
        "verdict": {
            "type": "string",
            "enum": [
                "supported",
                "unsupported",
                "needs_more_evidence",
                "changes_requested",
                "approved",
            ],
        },
        "findings": _NON_EMPTY_TEXTS,
        "risk_notes": _TEXTS,
        "recommendation": _TEXT,
    },
    "required": [
        "mode",
        "target_artifact_ref",
        "evidence_refs",
        "verdict",
        "findings",
        "risk_notes",
        "recommendation",
    ],
    "additionalProperties": False,
}

PATCH_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "strategy": {"type": "string", "enum": list(PATCH_STRATEGIES)},
        "based_on_hypothesis": _TEXT,
        "diff": _TEXT,
        "diff_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "changed_files": _NON_EMPTY_TEXTS,
        "rationale": _TEXT,
        "risk_notes": _TEXTS,
        "protected_path_check": {"type": "boolean"},
        "workspace_id": _TEXT,
    },
    "required": [
        "strategy",
        "based_on_hypothesis",
        "diff",
        "diff_sha256",
        "changed_files",
        "rationale",
        "risk_notes",
        "protected_path_check",
        "workspace_id",
    ],
    "additionalProperties": False,
}

_SCHEMAS = {
    ArtifactType.EVIDENCE: EVIDENCE_CONTENT_SCHEMA,
    ArtifactType.HYPOTHESIS: HYPOTHESIS_CONTENT_SCHEMA,
    ArtifactType.REVIEW: REVIEW_CONTENT_SCHEMA,
    ArtifactType.PATCH_CANDIDATE: PATCH_CONTENT_SCHEMA,
}


def validate_worker_artifact(
    artifact: Artifact,
    *,
    expected_type: ArtifactType | None = None,
    allowed_input_refs: Sequence[str] | None = None,
) -> None:
    """Validate content plus references that JSON Schema alone cannot express."""

    if expected_type is not None and artifact.artifact_type is not expected_type:
        raise ValueError(
            f"expected {expected_type.value} artifact, got {artifact.artifact_type.value}"
        )
    try:
        schema = _SCHEMAS[artifact.artifact_type]
    except KeyError as exc:
        raise ValueError(f"unsupported Phase 4 artifact type: {artifact.artifact_type.value}") from exc
    validate_json_schema(artifact.content, schema)
    if allowed_input_refs is not None:
        allowed = set(allowed_input_refs)
        if not set(artifact.input_refs).issubset(allowed):
            raise ValueError("artifact input_refs include unavailable artifacts")
        _validate_content_refs(artifact, allowed)


def _validate_content_refs(artifact: Artifact, allowed: set[str]) -> None:
    content: Mapping[str, Any] = artifact.content
    if artifact.artifact_type is ArtifactType.HYPOTHESIS:
        _require_refs(content["supporting_evidence"], allowed, "supporting_evidence")
    elif artifact.artifact_type is ArtifactType.REVIEW:
        _require_refs((content["target_artifact_ref"],), allowed, "target_artifact_ref")
        _require_refs(content["evidence_refs"], allowed, "evidence_refs")
    elif artifact.artifact_type is ArtifactType.PATCH_CANDIDATE:
        _require_refs((content["based_on_hypothesis"],), allowed, "based_on_hypothesis")


def _require_refs(values: Sequence[Any], allowed: set[str], label: str) -> None:
    missing = [str(value) for value in values if str(value) not in allowed]
    if missing:
        raise ValueError(f"{label} cites unavailable artifacts: {missing}")
