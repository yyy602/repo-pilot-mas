"""Reviewed root-cause gates layered on the deterministic orchestration engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from repo_pilot_mas.orchestration.engine import (
    EngineStatus,
    OrchestrationEngine as LegacyOrchestrationEngine,
    WorkflowStage,
)
from repo_pilot_mas.orchestration.policy_violation import DecisionPolicyViolation
from repo_pilot_mas.orchestration.task_graph import NodeType
from repo_pilot_mas.schemas.artifact import Artifact, ArtifactType
from repo_pilot_mas.schemas.hypothesis_resolution import HypothesisResolutionStatus
from repo_pilot_mas.schemas.supervisor_decision import DecisionAction, SupervisorDecision


@dataclass(frozen=True, slots=True)
class DecisionResult:
    ok: bool
    code: str
    message: str
    decision_id: str | None
    state_version: int
    mutated_node_ids: tuple[str, ...] = ()
    recoverable: bool = False
    recommended_stage: str | None = None
    allowed_next_actions: tuple[str, ...] = ()
    trigger_artifact_refs: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "decision_id": self.decision_id,
            "state_version": self.state_version,
            "mutated_node_ids": list(self.mutated_node_ids),
            "recoverable": self.recoverable,
            "recommended_stage": self.recommended_stage,
            "allowed_next_actions": list(self.allowed_next_actions),
            "trigger_artifact_refs": list(self.trigger_artifact_refs),
            "details": dict(self.details),
        }


class OrchestrationEngine(LegacyOrchestrationEngine):
    """Engine variant that requires reviewed root causes before patch execution."""

    def apply_decision(self, decision: SupervisorDecision) -> DecisionResult:
        try:
            return super().apply_decision(decision)
        except DecisionPolicyViolation as exc:
            return self._reject_policy_violation(decision.decision_id, exc)

    def _reject_policy_violation(
        self,
        decision_id: str,
        exc: DecisionPolicyViolation,
    ) -> DecisionResult:
        result = DecisionResult(
            False,
            exc.code,
            str(exc),
            decision_id,
            self.state_version,
            recoverable=exc.recoverable,
            recommended_stage=exc.recommended_stage,
            allowed_next_actions=exc.allowed_next_actions,
            trigger_artifact_refs=exc.trigger_artifact_refs,
            details=exc.details,
        )
        if self.last_supervisor_call is not None:
            self.last_supervisor_call["decision_result"] = result.to_dict()
        self._trace(
            "supervisor_policy_violation",
            result.to_dict(),
        )
        return result

    def _validate_decision(self, decision: SupervisorDecision) -> None:
        super()._validate_decision(decision)
        if decision.action is DecisionAction.CREATE_TASK:
            for request in decision.create_tasks:
                if NodeType(request.node_type) is NodeType.PATCH_TASK:
                    self._validate_patch_eligibility(request)

    def _validate_patch_eligibility(self, request: Any) -> None:
        resolution = self.blackboard.hypothesis_resolution
        if resolution.status is not HypothesisResolutionStatus.ACCEPTED:
            raise DecisionPolicyViolation(
                "HYPOTHESIS_NOT_RESOLVED",
                "PatchTask requires a reviewed and accepted Hypothesis resolution",
                recommended_stage=WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "CHANGE_WORKFLOW_STAGE", "TERMINATE_TASK"),
                trigger_artifact_refs=resolution.candidate_refs,
            )

    def snapshot(self) -> dict[str, Any]:
        snapshot = super().snapshot()
        resolution = self.blackboard.hypothesis_resolution
        selections = dict(snapshot.get("selections", {}))
        selections["hypothesis_resolution"] = resolution.to_dict()
        snapshot["selections"] = selections
        return snapshot

    def _current_reviews_for_hypotheses(self, hypotheses: tuple[Artifact, ...]) -> tuple[Artifact, ...]:
        return tuple(
            artifact
            for artifact in self.blackboard.artifacts.latest_values()
            if artifact.artifact_type is ArtifactType.REVIEW
        )
