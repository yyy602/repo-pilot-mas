"""Deterministic input contracts for closed-loop worker dispatch."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

_ACCEPTABLE_REVIEW_VERDICTS = frozenset({"approved", "compatible", "supported"})
_ROOT_CAUSE_REVIEW_MODES = frozenset(
    {"root_cause_recommendation", "hypothesis_comparison"}
)
_PATCH_PEER_REVIEW_MODES = frozenset(
    {"minimal_critiques_robust", "robust_critiques_minimal"}
)


class AgentContractViolation(ValueError):
    """Raised when a worker task is created with insufficient artifacts."""

    code = "AGENT_INPUT_CONTRACT_VIOLATION"


def validate_agent_input_contract(
    agent_type: str,
    mode: str,
    artifacts: Iterable[Mapping[str, object]],
) -> None:
    """Validate worker inputs before dispatch.

    This prevents deterministic dependency mistakes from reaching WorkerPool.
    The worker should not be used to discover missing upstream artifacts.
    Phase C additionally requires reviewed root causes before patch generation and
    a non-blocking Patch Review before deterministic validation.
    """

    items = list(artifacts)
    types = {_artifact_type(item) for item in items}

    def require(*required: str) -> None:
        missing = [item for item in required if item not in types]
        if missing:
            raise AgentContractViolation(
                f"{agent_type}/{mode} missing required artifacts: {missing}"
            )

    def require_count(artifact_type: str, *, minimum: int = 1, maximum: int | None = None) -> None:
        count = sum(_artifact_type(item) == artifact_type for item in items)
        if count < minimum or (maximum is not None and count > maximum):
            expected = (
                str(minimum)
                if maximum == minimum
                else f"{minimum}..{maximum if maximum is not None else 'n'}"
            )
            raise AgentContractViolation(
                f"{agent_type}/{mode} requires {expected} {artifact_type} artifacts; got {count}"
            )

    if agent_type == "InvestigatorAgent":
        if mode == "evidence_completion":
            require("evidence", "review")
            _reject_unexpected_types(
                agent_type,
                mode,
                types,
                {"evidence", "review", "artifact_rejection"},
            )
        return

    if agent_type == "DiagnosticianAgent":
        require("evidence")
        if mode in {"control_flow", "data_flow"}:
            _reject_unexpected_types(agent_type, mode, types, {"evidence"})
            return
        if mode == "challenge":
            require("hypothesis")
            require_count("hypothesis", minimum=1, maximum=1)
            _reject_unexpected_types(
                agent_type,
                mode,
                types,
                {"evidence", "hypothesis"},
            )
            return
        if mode == "rebuttal":
            require("hypothesis", "challenge")
            require_count("hypothesis", minimum=1, maximum=1)
            require_count("challenge", minimum=1, maximum=1)
            _reject_unexpected_types(
                agent_type,
                mode,
                types,
                {"evidence", "hypothesis", "challenge"},
            )
            return
        raise AgentContractViolation(
            f"DiagnosticianAgent has unsupported mode: {mode}"
        )

    if agent_type == "ReviewerAgent":
        _validate_reviewer_contract(mode, items, require, require_count)
        return

    if agent_type == "PatchAgent":
        if mode in _PATCH_PEER_REVIEW_MODES:
            require("evidence", "patch_candidate")
            require_count("patch_candidate", minimum=2, maximum=2)
            return
        require("hypothesis", "review")
        if not _has_non_blocking_review(items, _ROOT_CAUSE_REVIEW_MODES):
            raise AgentContractViolation(
                f"PatchAgent/{mode} requires a non-blocking root-cause Review"
            )
        return

    if agent_type == "ValidationExecutor":
        require("patch_candidate", "review")
        require_count("patch_candidate", minimum=1, maximum=1)
        patch = next(
            item for item in items if _artifact_type(item) == "patch_candidate"
        )
        if not _has_targeted_non_blocking_review(items, "patch_review", patch):
            raise AgentContractViolation(
                "ValidationExecutor/deterministic requires a non-blocking "
                "patch_review targeting the PatchCandidate"
            )


def _reject_unexpected_types(
    agent_type: str,
    mode: str,
    actual: set[str],
    allowed: set[str],
) -> None:
    unexpected = sorted(actual.difference(allowed))
    if unexpected:
        raise AgentContractViolation(
            f"{agent_type}/{mode} received unsupported artifacts: {unexpected}"
        )


def _validate_reviewer_contract(
    mode: str,
    items: list[Mapping[str, object]],
    require: Any,
    require_count: Any,
) -> None:
    if mode == "evidence_review":
        require("evidence")
        return
    if mode == "root_cause_recommendation":
        require("evidence", "hypothesis")
        require_count("hypothesis", minimum=1, maximum=1)
        return
    if mode == "hypothesis_comparison":
        require("evidence", "hypothesis")
        require_count("hypothesis", minimum=2)
        return
    if mode == "challenge_quality":
        require("evidence", "hypothesis", "challenge")
        return
    if mode == "patch_review":
        require("evidence", "hypothesis", "patch_candidate", "review")
        require_count("patch_candidate", minimum=1, maximum=1)
        if not _has_non_blocking_review(items, _ROOT_CAUSE_REVIEW_MODES):
            raise AgentContractViolation(
                "ReviewerAgent/patch_review requires accepted root-cause Review context"
            )
        return
    if mode == "final_risk_review":
        require("evidence", "patch_candidate", "validation_result", "review")
        require_count("patch_candidate", minimum=1, maximum=1)
        require_count("validation_result", minimum=1, maximum=1)
        patch = next(
            item for item in items if _artifact_type(item) == "patch_candidate"
        )
        if not _has_targeted_non_blocking_review(items, "patch_review", patch):
            raise AgentContractViolation(
                "ReviewerAgent/final_risk_review requires a non-blocking Patch Review"
            )
        validation = next(
            item for item in items if _artifact_type(item) == "validation_result"
        )
        if _content(validation).get("passed") is not True:
            raise AgentContractViolation(
                "ReviewerAgent/final_risk_review requires passed deterministic validation"
            )
        return
    raise AgentContractViolation(f"ReviewerAgent has unsupported mode: {mode}")


def _has_non_blocking_review(
    items: list[Mapping[str, object]],
    modes: frozenset[str],
) -> bool:
    return any(
        _artifact_type(item) == "review"
        and str(_content(item).get("mode", "")) in modes
        and str(_content(item).get("verdict", "")) in _ACCEPTABLE_REVIEW_VERDICTS
        for item in items
    )


def _has_targeted_non_blocking_review(
    items: list[Mapping[str, object]],
    mode: str,
    target: Mapping[str, object],
) -> bool:
    target_ids = {_artifact_ref(target), str(target.get("artifact_id", ""))}
    return any(
        _artifact_type(item) == "review"
        and str(_content(item).get("mode", "")) == mode
        and str(_content(item).get("verdict", "")) in _ACCEPTABLE_REVIEW_VERDICTS
        and str(_content(item).get("target_artifact_ref", "")) in target_ids
        for item in items
    )


def _artifact_type(item: Mapping[str, object]) -> str:
    return str(item.get("artifact_type", ""))


def _content(item: Mapping[str, object]) -> Mapping[str, object]:
    value = item.get("content", {})
    return value if isinstance(value, Mapping) else {}


def _artifact_ref(item: Mapping[str, object]) -> str:
    artifact_id = str(item.get("artifact_id", ""))
    version = item.get("version")
    if artifact_id and isinstance(version, int) and not isinstance(version, bool):
        return f"{artifact_id}@v{version}"
    return artifact_id
