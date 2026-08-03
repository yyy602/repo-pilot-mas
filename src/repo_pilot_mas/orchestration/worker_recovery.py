"""Atomic Worker collection, rejection records, and bounded recovery."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repo_pilot_mas.orchestration.task_graph import NodeStatus, NodeType, TaskNode
from repo_pilot_mas.schemas.artifact import Artifact, ArtifactType
from repo_pilot_mas.schemas.hypothesis_resolution import HypothesisResolutionStatus
from repo_pilot_mas.schemas.worker_artifact import validate_worker_artifact
from repo_pilot_mas.state.blackboard import ArtifactStore

MAX_WORKER_ATTEMPTS = 2

_EXPECTED_ARTIFACTS = {
    NodeType.INVESTIGATION_TASK: ArtifactType.EVIDENCE,
    NodeType.DIAGNOSIS_TASK: ArtifactType.HYPOTHESIS,
    NodeType.CHALLENGE_TASK: ArtifactType.CHALLENGE,
    NodeType.REBUTTAL_TASK: ArtifactType.REBUTTAL,
    NodeType.REVIEW_TASK: ArtifactType.REVIEW,
    NodeType.PATCH_TASK: ArtifactType.PATCH_CANDIDATE,
    NodeType.VALIDATION_TASK: ArtifactType.VALIDATION_RESULT,
}


class ArtifactCollectionError(ValueError):
    """A recoverable Worker artifact policy or validation failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        recommended_stage: str = "review",
        allowed_next_actions: Sequence[str] = (
            "RETRY_TASK",
            "CREATE_TASK",
            "REQUEST_REPLAN",
        ),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})
        self.recommended_stage = recommended_stage
        self.allowed_next_actions = tuple(
            dict.fromkeys(str(item) for item in allowed_next_actions if str(item))
        )


@dataclass(frozen=True, slots=True)
class WorkerCollectionResult:
    """Deterministic result of applying one Worker outcome to the Engine."""

    node_id: str
    status: NodeStatus
    artifact_refs: tuple[str, ...] = ()
    rejection_ref: str | None = None
    retry_scheduled: bool = False
    attempt: int = 1
    max_attempts: int = MAX_WORKER_ATTEMPTS
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return not self.retry_scheduled

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status.value,
            "artifact_refs": list(self.artifact_refs),
            "rejection_ref": self.rejection_ref,
            "retry_scheduled": self.retry_scheduled,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "terminal": self.terminal,
            "details": dict(self.details),
        }


def artifact_rejection(
    node_id: str,
    code: str,
    reason: str,
    *,
    attempt: int,
    max_attempts: int = MAX_WORKER_ATTEMPTS,
    origin: str,
    terminal_status: NodeStatus = NodeStatus.FAILED,
    recoverable: bool = True,
    recommended_stage: str = "review",
    allowed_next_actions: Sequence[str] = (
        "RETRY_TASK",
        "CREATE_TASK",
        "REQUEST_REPLAN",
    ),
    expected_artifact_type: ArtifactType | None = None,
    actual_artifact_refs: Sequence[str] = (),
    workspace_id: str = "",
    details: Mapping[str, Any] | None = None,
) -> Artifact:
    """Create one deterministic rejection record for a Worker attempt."""

    if attempt <= 0 or max_attempts <= 0 or attempt > max_attempts:
        raise ValueError("invalid Worker attempt range")
    content = {
        "node_id": node_id,
        "code": code,
        "reason": reason,
        "origin": origin,
        "terminal_status": NodeStatus(terminal_status).value,
        "recoverable": bool(recoverable),
        "recommended_stage": recommended_stage,
        "allowed_next_actions": list(
            dict.fromkeys(str(item) for item in allowed_next_actions if str(item))
        ),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "expected_artifact_type": (
            expected_artifact_type.value if expected_artifact_type is not None else ""
        ),
        "actual_artifact_refs": list(
            dict.fromkeys(str(item) for item in actual_artifact_refs if str(item))
        ),
        "workspace_id": workspace_id,
        "details": dict(details or {}),
    }
    return Artifact(
        artifact_id=f"{node_id}.rejection.a{attempt}",
        artifact_type=ArtifactType.ARTIFACT_REJECTION,
        created_by=node_id,
        content=content,
    )


def rejection_outcome(
    node_id: str,
    code: str,
    reason: str,
    *,
    status: NodeStatus = NodeStatus.FAILED,
    attempt: int = 1,
    max_attempts: int = MAX_WORKER_ATTEMPTS,
    origin: str = "worker",
    expected_artifact_type: ArtifactType | None = None,
    actual_artifact_refs: Sequence[str] = (),
    workspace_id: str = "",
    details: Mapping[str, Any] | None = None,
) -> Any:
    """Build a failed WorkerOutcome without importing the runtime at module load."""

    from repo_pilot_mas.orchestration.langgraph_runtime import WorkerOutcome

    rejection = artifact_rejection(
        node_id,
        code,
        reason,
        attempt=attempt,
        max_attempts=max_attempts,
        origin=origin,
        terminal_status=status,
        expected_artifact_type=expected_artifact_type,
        actual_artifact_refs=actual_artifact_refs,
        workspace_id=workspace_id,
        details=details,
    )
    return WorkerOutcome(node_id, status, (rejection,), reason=reason)


def worker_outcome_from_artifact_path(
    node_id: str,
    artifact_path: str | Path | None,
    *,
    expected_type: ArtifactType,
    allowed_input_refs: Sequence[str] = (),
    attempt: int = 1,
    max_attempts: int = MAX_WORKER_ATTEMPTS,
) -> Any:
    """Load a file-based Worker handoff and convert every failure to a rejection."""

    if artifact_path is None or not str(artifact_path).strip():
        return rejection_outcome(
            node_id,
            "ARTIFACT_MISSING",
            "Worker did not provide an artifact path",
            attempt=attempt,
            max_attempts=max_attempts,
            expected_artifact_type=expected_type,
        )
    path = Path(artifact_path).expanduser()
    if not path.exists():
        return rejection_outcome(
            node_id,
            "ARTIFACT_MISSING",
            f"Worker artifact does not exist: {path}",
            attempt=attempt,
            max_attempts=max_attempts,
            expected_artifact_type=expected_type,
            details={"artifact_path": str(path)},
        )
    if path.is_symlink():
        return rejection_outcome(
            node_id,
            "ARTIFACT_PATH_UNSAFE",
            f"Worker artifact path must not be a symbolic link: {path}",
            attempt=attempt,
            max_attempts=max_attempts,
            expected_artifact_type=expected_type,
            details={"artifact_path": str(path)},
        )
    if path.is_dir():
        return rejection_outcome(
            node_id,
            "ARTIFACT_PATH_IS_DIRECTORY",
            f"Worker artifact path is a directory: {path}",
            attempt=attempt,
            max_attempts=max_attempts,
            expected_artifact_type=expected_type,
            details={"artifact_path": str(path)},
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return rejection_outcome(
            node_id,
            "ARTIFACT_UNREADABLE",
            f"Worker artifact could not be read: {type(exc).__name__}: {exc}",
            attempt=attempt,
            max_attempts=max_attempts,
            expected_artifact_type=expected_type,
            details={"artifact_path": str(path)},
        )
    try:
        value = json.loads(text)
        if not isinstance(value, Mapping):
            raise TypeError("artifact JSON must be an object")
        artifact = Artifact.from_dict(value)
        if artifact.created_by != node_id:
            raise ValueError("artifact created_by does not match node_id")
        validate_worker_artifact(
            artifact,
            expected_type=expected_type,
            allowed_input_refs=allowed_input_refs,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return rejection_outcome(
            node_id,
            "ARTIFACT_CONTENT_INVALID",
            f"Worker artifact content is invalid: {type(exc).__name__}: {exc}",
            attempt=attempt,
            max_attempts=max_attempts,
            expected_artifact_type=expected_type,
            details={"artifact_path": str(path)},
        )

    from repo_pilot_mas.orchestration.langgraph_runtime import WorkerOutcome

    return WorkerOutcome(node_id, NodeStatus.SUCCEEDED, (artifact,))


def collect_worker_outcome(
    engine: Any,
    outcome: Any,
    *,
    max_attempts: int = MAX_WORKER_ATTEMPTS,
) -> WorkerCollectionResult:
    """Validate an entire outcome before mutating Blackboard or TaskGraph."""

    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    node = engine.graph.get(str(outcome.node_id))
    if node.status is not NodeStatus.RUNNING:
        if node.terminal:
            rejection_refs = tuple(
                ref
                for ref in node.output_artifact_ids
                if engine.blackboard.artifacts.get(ref).artifact_type
                is ArtifactType.ARTIFACT_REJECTION
            )
            return WorkerCollectionResult(
                node.node_id,
                node.status,
                tuple(node.output_artifact_ids),
                rejection_ref=rejection_refs[0] if rejection_refs else None,
                retry_scheduled=False,
                attempt=min(node.retry_count + 1, max_attempts),
                max_attempts=max_attempts,
                details={"idempotent_replay": True},
            )
        raise ValueError(
            f"Worker outcome requires a RUNNING node: {node.node_id} ({node.status.value})"
        )

    attempt = node.retry_count + 1
    effective_max_attempts = min(
        max_attempts,
        int(engine.budget.max_retries_per_node) + 1,
    )
    if effective_max_attempts <= 0:
        effective_max_attempts = 1

    try:
        if NodeStatus(outcome.status) is not NodeStatus.SUCCEEDED:
            rejection = _rejection_from_failed_outcome(
                node,
                outcome,
                attempt=attempt,
                max_attempts=effective_max_attempts,
            )
            raise ArtifactCollectionError(
                str(rejection.content["code"]),
                str(rejection.content["reason"]),
                details={"provided_rejection": rejection},
                recommended_stage=str(rejection.content["recommended_stage"]),
                allowed_next_actions=tuple(
                    str(item) for item in rejection.content["allowed_next_actions"]
                ),
            )
        artifacts = _preflight_success(engine, node, tuple(outcome.artifacts))
    except ArtifactCollectionError as exc:
        provided = exc.details.get("provided_rejection")
        if isinstance(provided, Artifact):
            rejection = provided
        else:
            rejection = artifact_rejection(
                node.node_id,
                exc.code,
                str(exc),
                attempt=attempt,
                max_attempts=effective_max_attempts,
                origin="collector",
                terminal_status=NodeStatus.FAILED,
                recoverable=attempt < effective_max_attempts,
                recommended_stage=exc.recommended_stage,
                allowed_next_actions=exc.allowed_next_actions,
                expected_artifact_type=_EXPECTED_ARTIFACTS.get(node.node_type),
                actual_artifact_refs=tuple(
                    artifact.ref for artifact in tuple(outcome.artifacts)
                ),
                details={
                    key: value
                    for key, value in exc.details.items()
                    if key != "provided_rejection"
                },
            )
        rejection_ref = _commit_artifact(engine, rejection)
        terminal_status = NodeStatus(outcome.status)
        if terminal_status not in {
            NodeStatus.FAILED,
            NodeStatus.TIMED_OUT,
            NodeStatus.BLOCKED,
        }:
            terminal_status = NodeStatus.FAILED
        reason = str(rejection.content["reason"])
        engine.finish_node(
            node.node_id,
            terminal_status,
            artifact_refs=(rejection_ref,),
            reason=reason,
        )
        retry_scheduled = False
        if (
            terminal_status in {NodeStatus.FAILED, NodeStatus.TIMED_OUT}
            and attempt < effective_max_attempts
            and node.retry_count < engine.budget.max_retries_per_node
        ):
            engine.retry_node(node.node_id)
            retry_scheduled = True
        return WorkerCollectionResult(
            node.node_id,
            terminal_status,
            (rejection_ref,),
            rejection_ref=rejection_ref,
            retry_scheduled=retry_scheduled,
            attempt=attempt,
            max_attempts=effective_max_attempts,
            details={
                "code": str(rejection.content["code"]),
                "reason": reason,
            },
        )

    refs = tuple(_commit_artifact(engine, artifact) for artifact in artifacts)
    engine.finish_node(
        node.node_id,
        NodeStatus.SUCCEEDED,
        artifact_refs=refs,
        reason=outcome.reason,
    )
    return WorkerCollectionResult(
        node.node_id,
        NodeStatus.SUCCEEDED,
        refs,
        attempt=attempt,
        max_attempts=effective_max_attempts,
    )


def _preflight_success(
    engine: Any,
    node: TaskNode,
    artifacts: tuple[Artifact, ...],
) -> tuple[Artifact, ...]:
    expected_type = _EXPECTED_ARTIFACTS.get(node.node_type)
    if expected_type is None:
        raise ArtifactCollectionError(
            "UNSUPPORTED_WORKER_NODE",
            f"No Worker artifact contract exists for {node.node_type.value}",
        )
    if not artifacts:
        raise ArtifactCollectionError(
            "ARTIFACT_MISSING",
            "Successful Worker outcome did not include an Artifact",
            details={"expected_artifact_type": expected_type.value},
        )

    if node.node_type is NodeType.REBUTTAL_TASK:
        types = [artifact.artifact_type for artifact in artifacts]
        if types.count(ArtifactType.REBUTTAL) != 1 or any(
            artifact_type not in {ArtifactType.HYPOTHESIS, ArtifactType.REBUTTAL}
            for artifact_type in types
        ):
            raise ArtifactCollectionError(
                "ARTIFACT_SET_INVALID",
                "RebuttalTask must emit one Rebuttal and at most one revised Hypothesis",
                details={"actual_types": [item.value for item in types]},
            )
    elif len(artifacts) != 1 or artifacts[0].artifact_type is not expected_type:
        raise ArtifactCollectionError(
            "ARTIFACT_SET_INVALID",
            f"Worker must emit exactly one {expected_type.value} Artifact",
            details={
                "actual_types": [artifact.artifact_type.value for artifact in artifacts]
            },
        )

    staged = ArtifactStore.from_list(engine.blackboard.artifacts.to_list())
    available_refs = list(node.input_artifact_ids)
    for artifact in artifacts:
        if artifact.artifact_type is ArtifactType.ARTIFACT_REJECTION:
            raise ArtifactCollectionError(
                "ARTIFACT_SET_INVALID",
                "Successful Worker outcome cannot contain ArtifactRejection",
            )
        if artifact.created_by != node.node_id:
            raise ArtifactCollectionError(
                "ARTIFACT_CREATED_BY_MISMATCH",
                "Worker Artifact created_by does not match its node",
                details={
                    "node_id": node.node_id,
                    "created_by": artifact.created_by,
                },
            )
        try:
            validate_worker_artifact(
                artifact,
                expected_type=(
                    None
                    if node.node_type is NodeType.REBUTTAL_TASK
                    else expected_type
                ),
                allowed_input_refs=tuple(available_refs),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactCollectionError(
                "ARTIFACT_CONTENT_INVALID",
                f"Worker Artifact failed schema/reference validation: {exc}",
                details={"artifact_ref": artifact.ref},
            ) from exc
        try:
            staged.add(artifact)
        except (KeyError, TypeError, ValueError) as exc:
            message = str(exc)
            code = (
                "ARTIFACT_REFERENCE_INVALID"
                if "unknown input" in message
                else "ARTIFACT_VERSION_INVALID"
            )
            raise ArtifactCollectionError(
                code,
                f"Worker Artifact failed version/reference preflight: {message}",
                details={"artifact_ref": artifact.ref},
            ) from exc
        available_refs.append(artifact.ref)

    if node.node_type is NodeType.PATCH_TASK:
        _validate_patch_binding(engine, artifacts[0])
    return artifacts


def _validate_patch_binding(engine: Any, patch: Artifact) -> None:
    resolution = engine.blackboard.hypothesis_resolution
    if resolution.status is not HypothesisResolutionStatus.ACCEPTED:
        raise ArtifactCollectionError(
            "PATCH_WITHOUT_ACCEPTED_HYPOTHESIS",
            "PatchCandidate cannot be collected before Hypothesis acceptance",
            recommended_stage="review",
        )
    raw_refs = patch.content.get("based_on_hypothesis_refs")
    if raw_refs is None:
        legacy_ref = patch.content.get("based_on_hypothesis")
        raw_refs = (legacy_ref,) if legacy_ref else ()
    if isinstance(raw_refs, (str, bytes)):
        raw_refs = (raw_refs,)
    actual_refs = tuple(dict.fromkeys(str(item) for item in raw_refs if str(item)))
    expected_refs = tuple(resolution.accepted_refs)
    if set(actual_refs) != set(expected_refs) or len(actual_refs) != len(expected_refs):
        raise ArtifactCollectionError(
            "PATCH_HYPOTHESIS_SET_MISMATCH",
            "PatchCandidate must bind exactly to the accepted Hypothesis set",
            details={
                "expected_hypothesis_refs": list(expected_refs),
                "actual_hypothesis_refs": list(actual_refs),
            },
            recommended_stage="patch",
            allowed_next_actions=(
                "RETRY_TASK",
                "CREATE_PATCH_TASK",
                "REQUEST_REPLAN",
            ),
        )
    primary = patch.content.get("primary_hypothesis_ref")
    if primary is not None and str(primary) != resolution.primary_ref:
        raise ArtifactCollectionError(
            "PATCH_PRIMARY_HYPOTHESIS_MISMATCH",
            "PatchCandidate primary Hypothesis does not match the accepted primary",
            details={
                "expected_primary_ref": resolution.primary_ref,
                "actual_primary_ref": str(primary),
            },
            recommended_stage="patch",
        )
    if patch.content.get("protected_path_check") is not True:
        raise ArtifactCollectionError(
            "PATCH_PROTECTED_PATH_VIOLATION",
            "PatchCandidate did not pass protected path validation",
            details={"changed_files": list(patch.content.get("changed_files", ()))},
            recommended_stage="patch",
        )


def _rejection_from_failed_outcome(
    node: TaskNode,
    outcome: Any,
    *,
    attempt: int,
    max_attempts: int,
) -> Artifact:
    rejections = tuple(
        artifact
        for artifact in tuple(outcome.artifacts)
        if artifact.artifact_type is ArtifactType.ARTIFACT_REJECTION
    )
    if len(rejections) == 1:
        rejection = rejections[0]
        try:
            validate_worker_artifact(
                rejection,
                expected_type=ArtifactType.ARTIFACT_REJECTION,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactCollectionError(
                "ARTIFACT_REJECTION_INVALID",
                f"Worker rejection record is invalid: {exc}",
            ) from exc
        if rejection.content.get("node_id") != node.node_id:
            raise ArtifactCollectionError(
                "ARTIFACT_REJECTION_NODE_MISMATCH",
                "Worker rejection record belongs to another node",
            )
        return rejection

    status = NodeStatus(outcome.status)
    code = "WORKER_TIMEOUT" if status is NodeStatus.TIMED_OUT else "WORKER_DIED"
    reason = outcome.reason or (
        "Worker timed out before producing a rejection"
        if status is NodeStatus.TIMED_OUT
        else "Worker terminated before producing a rejection"
    )
    return artifact_rejection(
        node.node_id,
        code,
        reason,
        attempt=attempt,
        max_attempts=max_attempts,
        origin="collector",
        terminal_status=status,
        recoverable=attempt < max_attempts,
        recommended_stage="patch" if node.node_type is NodeType.PATCH_TASK else "review",
        expected_artifact_type=_EXPECTED_ARTIFACTS.get(node.node_type),
    )


def _commit_artifact(engine: Any, artifact: Artifact) -> str:
    if engine.blackboard.artifacts.contains(artifact.ref):
        existing = engine.blackboard.artifacts.get(artifact.ref)
        if existing.to_dict() != artifact.to_dict():
            raise ValueError(f"artifact replay conflicts with existing value: {artifact.ref}")
        return existing.ref
    return engine.add_artifact(artifact)
