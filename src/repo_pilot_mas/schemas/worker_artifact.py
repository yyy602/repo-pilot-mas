"""Worker artifact schemas and cross-reference validation."""

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
REBUTTAL_DECISIONS = ("accept", "partial_accept", "reject")

_TEXT = {"type": "string", "minLength": 1}
_STRING = {"type": "string"}
_TEXTS = {"type": "array", "items": _TEXT}
_NON_EMPTY_TEXTS = {"type": "array", "items": _TEXT, "minItems": 1}
_CONFIDENCE = {"type": "number", "minimum": 0, "maximum": 1}

_REPRODUCTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "attempted": {"type": "boolean"},
        "succeeded": {"type": "boolean"},
        "exit_code": {"type": "integer"},
        "failure_type": _STRING,
        "failure_output": {"type": "string"},
        "command": {"type": "array", "items": _TEXT, "minItems": 1},
    },
    "required": [
        "attempted",
        "succeeded",
        "exit_code",
        "failure_type",
        "failure_output",
        "command",
    ],
    "additionalProperties": False,
}

EVIDENCE_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": list(INVESTIGATOR_MODES)},
        "evidence_kind": {
            "type": "string",
            "enum": [
                "source",
                "reproduction",
                "execution",
                "dependency",
                "counterexample",
            ],
        },
        "claim": _TEXT,
        "supports_claims": _NON_EMPTY_TEXTS,
        "contradicts_claims": _TEXTS,
        "verified": {"type": "boolean"},
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
        "status": {
            "type": "string",
            "enum": ["unverified", "verified", "rejected"],
        },
        "tool_trace_ids": _NON_EMPTY_TEXTS,
        "missing_evidence": _TEXTS,
        "reproduction": _REPRODUCTION_SCHEMA,
    },
    "required": [
        "mode",
        "evidence_kind",
        "claim",
        "supports_claims",
        "contradicts_claims",
        "verified",
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
        "perspective": {
            "type": "string",
            "enum": list(DIAGNOSIS_PERSPECTIVES),
        },
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
        "target_artifact_refs": _NON_EMPTY_TEXTS,
        "evidence_refs": _NON_EMPTY_TEXTS,
        "verdict": {
            "type": "string",
            "enum": [
                "supported",
                "unsupported",
                "needs_more_evidence",
                "changes_requested",
                "approved",
                "conflict",
                "compatible",
            ],
        },
        "findings": _NON_EMPTY_TEXTS,
        "risk_notes": _TEXTS,
        "recommendation": _TEXT,
        "failure_explained": {"type": "boolean"},
        "causal_chain_complete": {"type": "boolean"},
        "alternative_causes": _TEXTS,
        "counterexample_checked": {"type": "boolean"},
        "verification_steps_executed": _NON_EMPTY_TEXTS,
        "remaining_uncertainty": _TEXTS,
    },
    "required": [
        "mode",
        "target_artifact_ref",
        "evidence_refs",
        "verdict",
        "findings",
        "risk_notes",
        "recommendation",
        "failure_explained",
        "causal_chain_complete",
        "alternative_causes",
        "counterexample_checked",
        "verification_steps_executed",
        "remaining_uncertainty",
    ],
    "additionalProperties": False,
}

CHALLENGE_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "challenged_hypothesis_ref": _TEXT,
        "challenged_claim": _TEXT,
        "insufficiency_reason": _TEXT,
        "counterexample": _TEXT,
        "alternative_causal_chain": _TEXT,
        "required_evidence": _NON_EMPTY_TEXTS,
        "severity": {
            "type": "string",
            "enum": ["blocking", "non_blocking"],
        },
    },
    "required": [
        "challenged_hypothesis_ref",
        "challenged_claim",
        "insufficiency_reason",
        "counterexample",
        "alternative_causal_chain",
        "required_evidence",
        "severity",
    ],
    "additionalProperties": False,
}

REBUTTAL_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "response_to": _TEXT,
        "defended_hypothesis_ref": _TEXT,
        "decision": {"type": "string", "enum": list(REBUTTAL_DECISIONS)},
        "new_evidence_refs": _TEXTS,
        "resulting_hypothesis_ref": _TEXT,
        "counterexample_explanation": _TEXT,
        "revision_summary": _TEXT,
    },
    "required": [
        "response_to",
        "defended_hypothesis_ref",
        "decision",
        "new_evidence_refs",
        "resulting_hypothesis_ref",
        "counterexample_explanation",
        "revision_summary",
    ],
    "additionalProperties": False,
}

_COMMAND_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {"type": "array", "items": _TEXT, "minItems": 1},
        "exit_code": {"type": "integer"},
        "trace_id": _TEXT,
        "duration_ms": {"type": "integer", "minimum": 0},
        "output_tail": {"type": "string"},
    },
    "required": [
        "command",
        "exit_code",
        "trace_id",
        "duration_ms",
        "output_tail",
    ],
    "additionalProperties": False,
}

VALIDATION_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "patch_ref": _TEXT,
        "patch_review_refs": _NON_EMPTY_TEXTS,
        "patch_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "workspace_id": _TEXT,
        "applied": {"type": "boolean"},
        "target_test": _COMMAND_RESULT_SCHEMA,
        "regression_test": _COMMAND_RESULT_SCHEMA,
        "static_check": _COMMAND_RESULT_SCHEMA,
        "protected_path_check": {"type": "boolean"},
        "changed_files": _TEXTS,
        "changed_lines": {"type": "integer", "minimum": 0},
        "passed": {"type": "boolean"},
        "failure_class": {
            "type": "string",
            "enum": [
                "none",
                "patch_apply_failure",
                "target_test_failure",
                "regression_failure",
                "syntax_failure",
            ],
        },
        "recommended_stage": {
            "type": "string",
            "enum": ["completed", "patch"],
        },
        "invalidated_refs": _TEXTS,
        "recoverable": {"type": "boolean"},
        "tool_trace_ids": {
            "type": "array",
            "items": _TEXT,
            "minItems": 4,
        },
    },
    "required": [
        "patch_ref",
        "patch_review_refs",
        "patch_sha256",
        "workspace_id",
        "applied",
        "target_test",
        "regression_test",
        "static_check",
        "protected_path_check",
        "changed_files",
        "changed_lines",
        "passed",
        "failure_class",
        "recommended_stage",
        "invalidated_refs",
        "recoverable",
        "tool_trace_ids",
    ],
    "additionalProperties": False,
}

REPLAN_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision_id": _TEXT,
        "attempt": {"type": "integer", "minimum": 1},
        "from_stage": _TEXT,
        "target_stage": {
            "type": "string",
            "enum": ["investigation", "diagnosis", "patch"],
        },
        "failure_class": _TEXT,
        "trigger_refs": _NON_EMPTY_TEXTS,
        "reason": _TEXT,
        "remaining_replans": {"type": "integer", "minimum": 0},
    },
    "required": [
        "decision_id",
        "attempt",
        "from_stage",
        "target_stage",
        "failure_class",
        "trigger_refs",
        "reason",
        "remaining_replans",
    ],
    "additionalProperties": False,
}

_PATCH_COMMON_PROPERTIES: dict[str, Any] = {
    "strategy": {"type": "string", "enum": list(PATCH_STRATEGIES)},
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
    "pre_patch_behavior": _TEXT,
    "post_patch_expected_behavior": _TEXT,
    "failure_input_walkthrough": _TEXT,
    "semantic_rationale": _TEXT,
    "precheck_command": {"type": "array", "items": _TEXT, "minItems": 1},
    "precheck_result": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "trace_id": _TEXT,
            "exit_code": {"type": "integer"},
            "output_tail": {"type": "string"},
        },
        "required": ["ok", "trace_id", "exit_code", "output_tail"],
        "additionalProperties": False,
    },
}
_PATCH_COMMON_REQUIRED = [
    "strategy",
    "diff",
    "diff_sha256",
    "changed_files",
    "rationale",
    "risk_notes",
    "protected_path_check",
    "workspace_id",
    "pre_patch_behavior",
    "post_patch_expected_behavior",
    "failure_input_walkthrough",
    "semantic_rationale",
    "precheck_command",
    "precheck_result",
]
PATCH_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_PATCH_COMMON_PROPERTIES,
        "based_on_hypothesis_refs": _NON_EMPTY_TEXTS,
        "primary_hypothesis_ref": _TEXT,
        "covered_root_causes": {"type": "object"},
    },
    "required": [
        *_PATCH_COMMON_REQUIRED,
        "based_on_hypothesis_refs",
        "primary_hypothesis_ref",
        "covered_root_causes",
    ],
    "additionalProperties": False,
}

ARTIFACT_REJECTION_CONTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "node_id": _TEXT,
        "code": _TEXT,
        "reason": _TEXT,
        "origin": {
            "type": "string",
            "enum": ["worker", "collector", "runtime"],
        },
        "terminal_status": {
            "type": "string",
            "enum": ["FAILED", "TIMED_OUT", "BLOCKED"],
        },
        "recoverable": {"type": "boolean"},
        "recommended_stage": _TEXT,
        "allowed_next_actions": _NON_EMPTY_TEXTS,
        "attempt": {"type": "integer", "minimum": 1},
        "max_attempts": {"type": "integer", "minimum": 1},
        "expected_artifact_type": _STRING,
        "actual_artifact_refs": _TEXTS,
        "workspace_id": _STRING,
        "details": {"type": "object"},
    },
    "required": [
        "node_id",
        "code",
        "reason",
        "origin",
        "terminal_status",
        "recoverable",
        "recommended_stage",
        "allowed_next_actions",
        "attempt",
        "max_attempts",
        "expected_artifact_type",
        "actual_artifact_refs",
        "workspace_id",
        "details",
    ],
    "additionalProperties": False,
}

_SCHEMAS = {
    ArtifactType.EVIDENCE: EVIDENCE_CONTENT_SCHEMA,
    ArtifactType.HYPOTHESIS: HYPOTHESIS_CONTENT_SCHEMA,
    ArtifactType.REVIEW: REVIEW_CONTENT_SCHEMA,
    ArtifactType.PATCH_CANDIDATE: PATCH_CONTENT_SCHEMA,
    ArtifactType.CHALLENGE: CHALLENGE_CONTENT_SCHEMA,
    ArtifactType.REBUTTAL: REBUTTAL_CONTENT_SCHEMA,
    ArtifactType.VALIDATION_RESULT: VALIDATION_CONTENT_SCHEMA,
    ArtifactType.REPLAN_RECORD: REPLAN_CONTENT_SCHEMA,
    ArtifactType.ARTIFACT_REJECTION: ARTIFACT_REJECTION_CONTENT_SCHEMA,
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
            f"expected {expected_type.value} artifact, "
            f"got {artifact.artifact_type.value}"
        )
    try:
        schema = _SCHEMAS[artifact.artifact_type]
    except KeyError as exc:
        raise ValueError(
            f"unsupported Worker artifact type: {artifact.artifact_type.value}"
        ) from exc
    validate_json_schema(artifact.content, schema)
    if artifact.artifact_type is ArtifactType.EVIDENCE:
        if bool(artifact.content["verified"]) != (
            artifact.content["status"] == "verified"
        ):
            raise ValueError("Evidence verified must match status=verified")
        if artifact.content["mode"] in {
            "evidence_completion",
            "regression_scope",
        } and (
            artifact.content["verified"] is not True
            or bool(artifact.content["missing_evidence"])
        ):
            raise ValueError(
                "evidence_completion/regression_scope must close the evidence gap"
            )
        if (
            artifact.content["evidence_kind"] == "reproduction"
            and "reproduction" not in artifact.content
        ):
            raise ValueError("reproduction Evidence requires reproduction details")
    elif artifact.artifact_type is ArtifactType.REVIEW:
        _validate_review_quality(artifact.content)
    elif artifact.artifact_type is ArtifactType.PATCH_CANDIDATE:
        _validate_patch_semantics(artifact.content)
    elif artifact.artifact_type is ArtifactType.VALIDATION_RESULT:
        passed = artifact.content["passed"] is True
        if passed != (artifact.content["failure_class"] == "none"):
            raise ValueError("Validation passed and failure_class are inconsistent")
        if passed != (artifact.content["recommended_stage"] == "completed"):
            raise ValueError("Validation recommended_stage is inconsistent")
        if passed and artifact.content["invalidated_refs"]:
            raise ValueError("passed Validation cannot invalidate artifacts")
        if not passed and not artifact.content["invalidated_refs"]:
            raise ValueError("failed Validation must identify invalidated refs")
    if artifact.artifact_type is ArtifactType.ARTIFACT_REJECTION:
        attempt = int(artifact.content["attempt"])
        max_attempts = int(artifact.content["max_attempts"])
        if attempt > max_attempts:
            raise ValueError("artifact rejection attempt exceeds max_attempts")
    if allowed_input_refs is not None:
        allowed = set(allowed_input_refs)
        if not set(artifact.input_refs).issubset(allowed):
            raise ValueError("artifact input_refs include unavailable artifacts")
        _validate_content_refs(artifact, allowed)


def _validate_content_refs(artifact: Artifact, allowed: set[str]) -> None:
    content: Mapping[str, Any] = artifact.content
    if artifact.artifact_type is ArtifactType.HYPOTHESIS:
        _require_refs(
            content["supporting_evidence"],
            allowed,
            "supporting_evidence",
        )
    elif artifact.artifact_type is ArtifactType.REVIEW:
        _require_refs(
            (content["target_artifact_ref"],),
            allowed,
            "target_artifact_ref",
        )
        if "target_artifact_refs" in content:
            _require_refs(
                content["target_artifact_refs"],
                allowed,
                "target_artifact_refs",
            )
        _require_refs(content["evidence_refs"], allowed, "evidence_refs")
    elif artifact.artifact_type is ArtifactType.PATCH_CANDIDATE:
        _require_refs(
            content["based_on_hypothesis_refs"],
            allowed,
            "based_on_hypothesis_refs",
        )
    elif artifact.artifact_type is ArtifactType.CHALLENGE:
        _require_refs(
            (content["challenged_hypothesis_ref"],),
            allowed,
            "challenged_hypothesis_ref",
        )
    elif artifact.artifact_type is ArtifactType.REBUTTAL:
        _require_refs(
            (
                content["response_to"],
                content["defended_hypothesis_ref"],
                content["resulting_hypothesis_ref"],
            ),
            allowed,
            "rebuttal refs",
        )
        _require_refs(
            content["new_evidence_refs"],
            allowed,
            "new_evidence_refs",
        )
    elif artifact.artifact_type is ArtifactType.VALIDATION_RESULT:
        _require_refs((content["patch_ref"],), allowed, "patch_ref")
        _require_refs(
            content["patch_review_refs"],
            allowed,
            "patch_review_refs",
        )
        _require_refs(
            content["invalidated_refs"],
            allowed,
            "invalidated_refs",
        )
    elif artifact.artifact_type is ArtifactType.REPLAN_RECORD:
        _require_refs(content["trigger_refs"], allowed, "trigger_refs")


def _validate_review_quality(content: Mapping[str, Any]) -> None:
    if content["mode"] == "hypothesis_comparison":
        targets = tuple(str(item) for item in content.get("target_artifact_refs", ()))
        if len(targets) < 2 or len(targets) != len(set(targets)):
            raise ValueError(
                "hypothesis_comparison requires at least two unique target_artifact_refs"
            )
        if content["target_artifact_ref"] not in targets:
            raise ValueError(
                "hypothesis_comparison target_artifact_ref must be one compared Hypothesis"
            )
    if content["verdict"] not in {"approved", "compatible", "supported"}:
        return
    if content["failure_explained"] is not True:
        raise ValueError("acceptable Review must explain the observed failure")
    if content["causal_chain_complete"] is not True:
        raise ValueError("acceptable Review requires a complete causal chain")
    if not content["alternative_causes"] and content["counterexample_checked"] is not True:
        raise ValueError("acceptable Review must check an alternative cause or counterexample")
    if not content["verification_steps_executed"]:
        raise ValueError("acceptable Review must execute at least one verification step")
    if content["remaining_uncertainty"]:
        raise ValueError("acceptable Review cannot retain unresolved uncertainty")


def _validate_patch_semantics(content: Mapping[str, Any]) -> None:
    refs = tuple(str(item) for item in content["based_on_hypothesis_refs"])
    if len(refs) != len(set(refs)):
        raise ValueError("Patch hypothesis refs must be unique")
    if content["primary_hypothesis_ref"] not in refs:
        raise ValueError("Patch primary_hypothesis_ref must belong to accepted refs")
    if set(content["covered_root_causes"]) != set(refs):
        raise ValueError("Patch covered_root_causes must match hypothesis refs")
    if content["protected_path_check"] is not True:
        raise ValueError("Patch did not pass protected path checks")
    if content["precheck_result"]["ok"] is not True:
        raise ValueError("Patch semantic precheck did not pass")


def _require_refs(
    values: Sequence[Any],
    allowed: set[str],
    label: str,
) -> None:
    missing = [str(value) for value in values if str(value) not in allowed]
    if missing:
        raise ValueError(f"{label} cites unavailable artifacts: {missing}")
