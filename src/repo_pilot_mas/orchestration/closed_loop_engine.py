"""Reviewed root-cause gates layered on the deterministic orchestration engine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from repo_pilot_mas.orchestration.engine import (
    EngineStatus,
    OrchestrationEngine as LegacyOrchestrationEngine,
    WorkflowStage,
)
from repo_pilot_mas.orchestration.policy_violation import DecisionPolicyViolation
from repo_pilot_mas.orchestration.task_graph import NodeStatus, NodeType, TaskNode
from repo_pilot_mas.schemas.artifact import Artifact, ArtifactType
from repo_pilot_mas.schemas.hypothesis_resolution import HypothesisResolutionStatus
from repo_pilot_mas.schemas.supervisor_decision import DecisionAction, SupervisorDecision

_ROOT_CAUSE_REVIEW_MODES = frozenset(
    {"hypothesis_comparison", "root_cause_recommendation"}
)
_ACCEPTABLE_ROOT_CAUSE_VERDICTS = frozenset({"supported", "compatible", "approved"})


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """Result of one deterministic decision application, including recovery guidance."""

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

    def snapshot(self) -> dict[str, Any]:
        snapshot = super().snapshot()
        resolution = self.blackboard.hypothesis_resolution
        selections = dict(snapshot.get("selections", {}))
        selections.update(
            {
                "hypothesis_ref": resolution.primary_ref,
                "hypothesis_refs": list(resolution.accepted_refs),
                "review_refs": list(resolution.review_refs),
                "hypothesis_resolution": resolution.to_dict(),
            }
        )
        snapshot["selections"] = selections
        return snapshot

    def apply_decision(self, decision: SupervisorDecision) -> DecisionResult:
        if self.status is not EngineStatus.ACTIVE:
            return self._reject(
                decision.decision_id,
                "ENGINE_NOT_ACTIVE",
                "engine is not active",
            )
        if self.budget.elapsed_seconds() > self.budget.max_runtime_seconds:
            return self._reject(
                decision.decision_id,
                "RUNTIME_BUDGET_EXHAUSTED",
                "engine runtime budget is exhausted",
                terminate=True,
            )
        if decision.decision_id in self._processed_decision_ids:
            return self._reject(
                decision.decision_id,
                "DUPLICATE_DECISION_ID",
                "decision_id has already been processed",
            )

        self._decision_count += 1
        self.blackboard.bump()
        if decision.fingerprint in self._decision_fingerprints:
            self._no_progress_decisions += 1
            terminate = self._no_progress_decisions >= self.budget.max_no_progress_decisions
            return self._reject(
                decision.decision_id,
                "NO_PROGRESS_LOOP",
                "semantically identical decision has already been applied",
                terminate=terminate,
            )

        try:
            self._validate_decision(decision)
            mutated = self._execute_decision(decision)
        except DecisionPolicyViolation as exc:
            return self._reject(
                decision.decision_id,
                exc.code,
                str(exc),
                recoverable=exc.recoverable,
                recommended_stage=exc.recommended_stage,
                allowed_next_actions=exc.allowed_next_actions,
                trigger_artifact_refs=exc.trigger_artifact_refs,
                details=exc.details,
            )
        except (KeyError, TypeError, ValueError) as exc:
            terminate = (
                decision.action is DecisionAction.REQUEST_REPLAN
                and self.budget.replans >= self.budget.max_replans
            )
            return self._reject(
                decision.decision_id,
                "INVALID_SUPERVISOR_DECISION",
                str(exc),
                terminate=terminate,
            )

        self._processed_decision_ids.add(decision.decision_id)
        self._decision_fingerprints.add(decision.fingerprint)
        self._no_progress_decisions = 0
        self._trace(
            "supervisor_decision_applied",
            {
                "decision": decision.to_dict(),
                "mutated_node_ids": list(mutated),
                "state_version": self.state_version,
                "engine_status": self.status.value,
                "hypothesis_resolution": self.blackboard.hypothesis_resolution.to_dict(),
            },
        )
        return DecisionResult(
            True,
            "DECISION_APPLIED",
            "supervisor decision was applied",
            decision.decision_id,
            self.state_version,
            tuple(mutated),
        )

    def _validate_decision(self, decision: SupervisorDecision) -> None:
        super()._validate_decision(decision)
        if decision.action is DecisionAction.ACCEPT_HYPOTHESIS:
            self._resolve_hypothesis_acceptance(decision)
            return
        if decision.action is DecisionAction.CREATE_TASK:
            for request in decision.create_tasks:
                if NodeType(request.node_type) is NodeType.PATCH_TASK:
                    self._validate_patch_eligibility(request)

    def _execute_decision(self, decision: SupervisorDecision) -> tuple[str, ...]:
        if decision.action is DecisionAction.ACCEPT_HYPOTHESIS:
            hypotheses, reviews = self._resolve_hypothesis_acceptance(decision)
            primary = self.blackboard.artifacts.get(
                str(decision.primary_hypothesis_ref)
            ).ref
            if reviews:
                self.blackboard.set_hypotheses(
                    tuple(item.ref for item in hypotheses),
                    primary_ref=primary,
                    review_refs=tuple(item.ref for item in reviews),
                    decision_id=decision.decision_id,
                )
            else:
                # Compatibility for historic test/checkpoint data. PatchTask remains gated
                # until a current Review is added.
                self.blackboard.set_hypothesis(primary)
            return ()

        mutated = list(super()._execute_decision(decision))
        if (
            decision.action is DecisionAction.REQUEST_REPLAN
            and decision.next_workflow_stage
            in {WorkflowStage.INVESTIGATION.value, WorkflowStage.DIAGNOSIS.value}
        ):
            reason = (
                f"Supervisor requested replan to {decision.next_workflow_stage}: "
                f"{decision.failure_class}"
            )
            if (
                self.blackboard.hypothesis_resolution.status
                is HypothesisResolutionStatus.ACCEPTED
            ):
                self.blackboard.invalidate_hypotheses(reason)
            mutated.extend(self._cancel_active_patch_path(reason))
        return tuple(dict.fromkeys(mutated))

    def _task_node(self, request: Any, *, node_id: str, decision_id: str) -> TaskNode:
        node = super()._task_node(request, node_id=node_id, decision_id=decision_id)
        if node.node_type is not NodeType.PATCH_TASK:
            return node
        resolution = self.blackboard.hypothesis_resolution
        if resolution.status is HypothesisResolutionStatus.ACCEPTED:
            node.input_artifact_ids = tuple(
                dict.fromkeys((*node.input_artifact_ids, *resolution.review_refs))
            )
        return node

    def _resolve_hypothesis_acceptance(
        self,
        decision: SupervisorDecision,
    ) -> tuple[tuple[Artifact, ...], tuple[Artifact, ...]]:
        current = WorkflowStage(self.blackboard.workflow_stage)
        if current not in {WorkflowStage.DIAGNOSIS, WorkflowStage.REVIEW}:
            raise DecisionPolicyViolation(
                "HYPOTHESIS_ACCEPTANCE_STAGE_INVALID",
                "Hypotheses may be accepted only during diagnosis or review",
                recommended_stage=WorkflowStage.REVIEW.value,
                allowed_next_actions=("CHANGE_WORKFLOW_STAGE", "CREATE_TASK", "TERMINATE_TASK"),
            )

        hypotheses = self._canonical_artifacts(
            decision.hypothesis_refs,
            ArtifactType.HYPOTHESIS,
            latest=True,
        )
        primary = self.blackboard.artifacts.get(str(decision.primary_hypothesis_ref))
        accepted_refs = {item.ref for item in hypotheses}
        if primary.ref not in accepted_refs:
            raise DecisionPolicyViolation(
                "PRIMARY_HYPOTHESIS_MISMATCH",
                "primary_hypothesis_ref must belong to the accepted hypothesis set",
                recommended_stage=WorkflowStage.REVIEW.value,
                allowed_next_actions=("ACCEPT_HYPOTHESIS", "CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(sorted(accepted_refs)),
            )

        reviews = self._current_reviews_for_hypotheses(hypotheses)
        if not reviews and all(self._is_legacy_hypothesis(item) for item in hypotheses):
            return hypotheses, ()
        covered_refs = set().union(
            *(self._review_target_refs(review) for review in reviews)
        ) if reviews else set()
        missing_refs = accepted_refs - covered_refs
        if missing_refs:
            raise DecisionPolicyViolation(
                "HYPOTHESIS_REVIEW_REQUIRED",
                "A current root-cause Review is required for every accepted Hypothesis",
                recommended_stage=WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "CHANGE_WORKFLOW_STAGE", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(sorted(missing_refs)),
                details={
                    "required_reviewer_modes": sorted(_ROOT_CAUSE_REVIEW_MODES),
                    "unreviewed_hypothesis_refs": sorted(missing_refs),
                },
            )

        selected_review_refs = {item.ref for item in reviews}
        cited_review_refs = self._decision_review_refs(decision)
        if not selected_review_refs.issubset(cited_review_refs):
            raise DecisionPolicyViolation(
                "HYPOTHESIS_REVIEW_STALE",
                "The decision must cite the current Review for every accepted Hypothesis",
                recommended_stage=WorkflowStage.REVIEW.value,
                allowed_next_actions=("ACCEPT_HYPOTHESIS", "CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(sorted(selected_review_refs)),
                details={
                    "required_review_refs": sorted(selected_review_refs),
                    "cited_review_refs": sorted(cited_review_refs),
                },
            )

        for review in reviews:
            self._validate_root_cause_review(review)
        return hypotheses, reviews

    def _current_reviews_for_hypotheses(
        self,
        hypotheses: Sequence[Artifact],
    ) -> tuple[Artifact, ...]:
        latest_reviews = [
            item
            for item in self.blackboard.artifacts.latest_values()
            if item.artifact_type is ArtifactType.REVIEW
            and str(item.content.get("mode", "")) in _ROOT_CAUSE_REVIEW_MODES
        ]
        selected_by_ref: dict[str, Artifact] = {}
        for hypothesis in hypotheses:
            covering = [
                review
                for review in latest_reviews
                if hypothesis.ref in self._review_target_refs(review)
            ]
            if not covering:
                continue
            selected = max(covering, key=lambda item: (item.created_at, item.ref))
            selected_by_ref[selected.ref] = selected
        return tuple(selected_by_ref.values())

    def _validate_root_cause_review(self, review: Artifact) -> None:
        verdict = str(review.content.get("verdict", ""))
        recovery = {
            "needs_more_evidence": (
                "HYPOTHESIS_NEEDS_EVIDENCE",
                WorkflowStage.INVESTIGATION.value,
                ("CREATE_TASK", "CHANGE_WORKFLOW_STAGE", "TERMINATE_TASK"),
            ),
            "changes_requested": (
                "HYPOTHESIS_NEEDS_REVISION",
                WorkflowStage.DIAGNOSIS.value,
                ("CREATE_TASK", "CHANGE_WORKFLOW_STAGE", "TERMINATE_TASK"),
            ),
            "conflict": (
                "HYPOTHESIS_CONFLICTING",
                WorkflowStage.REVIEW.value,
                ("CREATE_TASK", "CHANGE_WORKFLOW_STAGE", "TERMINATE_TASK"),
            ),
            "unsupported": (
                "HYPOTHESIS_REJECTED",
                WorkflowStage.DIAGNOSIS.value,
                ("CREATE_TASK", "REQUEST_REPLAN", "TERMINATE_TASK"),
            ),
        }
        if verdict in recovery:
            code, stage, actions = recovery[verdict]
            raise DecisionPolicyViolation(
                code,
                f"Root-cause Review {review.ref} has blocking verdict {verdict}",
                recommended_stage=stage,
                allowed_next_actions=actions,
                trigger_artifact_refs=(review.ref,),
                details={"review_ref": review.ref, "verdict": verdict},
            )
        if verdict not in _ACCEPTABLE_ROOT_CAUSE_VERDICTS:
            raise DecisionPolicyViolation(
                "HYPOTHESIS_REVIEW_INVALID",
                f"Root-cause Review {review.ref} has unsupported verdict {verdict!r}",
                recommended_stage=WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=(review.ref,),
            )

        evidence_refs = review.content.get("evidence_refs", ())
        if isinstance(evidence_refs, (str, bytes)):
            evidence_refs = (evidence_refs,)
        evidence = [self.blackboard.artifacts.get(str(ref)) for ref in evidence_refs]
        if not evidence or not any(
            item.artifact_type is ArtifactType.EVIDENCE for item in evidence
        ):
            raise DecisionPolicyViolation(
                "HYPOTHESIS_REVIEW_LACKS_DIRECT_EVIDENCE",
                f"Root-cause Review {review.ref} must cite direct Evidence",
                recommended_stage=WorkflowStage.INVESTIGATION.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=(review.ref,),
            )

    def _validate_patch_eligibility(self, request: Any) -> None:
        resolution = self.blackboard.hypothesis_resolution
        status = resolution.status
        if status is not HypothesisResolutionStatus.ACCEPTED:
            stage_by_status = {
                HypothesisResolutionStatus.NEEDS_EVIDENCE: WorkflowStage.INVESTIGATION.value,
                HypothesisResolutionStatus.NEEDS_REVISION: WorkflowStage.DIAGNOSIS.value,
                HypothesisResolutionStatus.CONFLICT: WorkflowStage.REVIEW.value,
                HypothesisResolutionStatus.REJECTED: WorkflowStage.DIAGNOSIS.value,
                HypothesisResolutionStatus.INVALIDATED: WorkflowStage.REVIEW.value,
            }
            raise DecisionPolicyViolation(
                "HYPOTHESIS_NOT_RESOLVED",
                "PatchTask requires a reviewed and accepted Hypothesis resolution",
                recommended_stage=stage_by_status.get(status, WorkflowStage.REVIEW.value),
                allowed_next_actions=("CREATE_TASK", "CHANGE_WORKFLOW_STAGE", "TERMINATE_TASK"),
                trigger_artifact_refs=resolution.candidate_refs,
                details={
                    "hypothesis_resolution_status": status.value,
                    "candidate_hypothesis_refs": list(resolution.candidate_refs),
                },
            )
        if resolution.metadata.get("legacy_migrated") is True or not resolution.review_refs:
            raise DecisionPolicyViolation(
                "HYPOTHESIS_REVIEW_REQUIRED",
                "Legacy Hypothesis selection must be reviewed before creating PatchTask",
                recommended_stage=WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "CHANGE_WORKFLOW_STAGE", "TERMINATE_TASK"),
                trigger_artifact_refs=resolution.accepted_refs,
            )

        accepted = self._canonical_artifacts(
            resolution.accepted_refs,
            ArtifactType.HYPOTHESIS,
            latest=True,
        )
        reviews = self._canonical_artifacts(
            resolution.review_refs,
            ArtifactType.REVIEW,
            latest=True,
        )
        for review in reviews:
            self._validate_root_cause_review(review)

        input_artifacts = tuple(
            self.blackboard.artifacts.get(str(ref))
            for ref in request.input_artifact_ids
        )
        input_hypotheses = {
            item.ref
            for item in input_artifacts
            if item.artifact_type is ArtifactType.HYPOTHESIS
        }
        accepted_refs = {item.ref for item in accepted}
        if input_hypotheses != accepted_refs:
            raise DecisionPolicyViolation(
                "PATCH_HYPOTHESIS_BINDING_MISMATCH",
                "PatchTask inputs must contain exactly the accepted Hypothesis set",
                recommended_stage=WorkflowStage.PATCH.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(sorted(accepted_refs | input_hypotheses)),
                details={
                    "expected_hypothesis_refs": sorted(accepted_refs),
                    "actual_hypothesis_refs": sorted(input_hypotheses),
                },
            )

        explicit_review_refs = {
            item.ref
            for item in input_artifacts
            if item.artifact_type is ArtifactType.REVIEW
        }
        dependency_review_refs = self._dependency_output_refs(
            request.depends_on,
            ArtifactType.REVIEW,
        )
        required_review_refs = {item.ref for item in reviews}
        available_review_refs = explicit_review_refs | dependency_review_refs
        if not required_review_refs.issubset(available_review_refs):
            raise DecisionPolicyViolation(
                "PATCH_REVIEW_CONTEXT_MISSING",
                "PatchTask must cite or depend on the accepted root-cause Review",
                recommended_stage=WorkflowStage.PATCH.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(sorted(required_review_refs)),
                details={
                    "required_review_refs": sorted(required_review_refs),
                    "available_review_refs": sorted(available_review_refs),
                },
            )

    def _canonical_artifacts(
        self,
        refs: Sequence[str],
        expected_type: ArtifactType,
        *,
        latest: bool,
    ) -> tuple[Artifact, ...]:
        artifacts_by_ref: dict[str, Artifact] = {}
        for ref in refs:
            artifact = self.blackboard.artifacts.get(str(ref))
            if artifact.artifact_type is not expected_type:
                raise DecisionPolicyViolation(
                    "ARTIFACT_TYPE_MISMATCH",
                    f"Expected {expected_type.value}, got {artifact.artifact_type.value}",
                    recommended_stage=WorkflowStage.REVIEW.value,
                    allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                    trigger_artifact_refs=(artifact.ref,),
                )
            if latest and not self.blackboard.artifacts.is_latest(artifact.ref):
                raise DecisionPolicyViolation(
                    "HYPOTHESIS_SELECTION_STALE",
                    f"Artifact is not the latest revision: {artifact.ref}",
                    recommended_stage=WorkflowStage.REVIEW.value,
                    allowed_next_actions=("CREATE_TASK", "ACCEPT_HYPOTHESIS", "TERMINATE_TASK"),
                    trigger_artifact_refs=(artifact.ref,),
                )
            artifacts_by_ref[artifact.ref] = artifact
        result = tuple(artifacts_by_ref.values())
        if not result:
            raise DecisionPolicyViolation(
                "HYPOTHESIS_SET_EMPTY",
                f"At least one {expected_type.value} Artifact is required",
                recommended_stage=WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
            )
        return result

    def _review_target_refs(self, review: Artifact) -> frozenset[str]:
        raw = review.content.get("target_artifact_refs")
        if raw is None:
            target = review.content.get("target_artifact_ref")
            raw = (target,) if target else ()
        if isinstance(raw, (str, bytes)):
            raw = (raw,)

        refs: set[str] = set()
        for value in raw:
            artifact = self.blackboard.artifacts.get(str(value))
            if artifact.artifact_type is ArtifactType.HYPOTHESIS:
                refs.add(artifact.ref)
        recommended = review.content.get("recommended_hypothesis_refs", ())
        if isinstance(recommended, (str, bytes)):
            recommended = (recommended,)
        for value in recommended:
            artifact = self.blackboard.artifacts.get(str(value))
            if artifact.artifact_type is ArtifactType.HYPOTHESIS:
                refs.add(artifact.ref)
        for ref in review.input_refs:
            artifact = self.blackboard.artifacts.get(ref)
            if artifact.artifact_type is ArtifactType.HYPOTHESIS:
                refs.add(artifact.ref)
        return frozenset(refs)

    def _decision_review_refs(self, decision: SupervisorDecision) -> set[str]:
        refs: set[str] = set()
        for ref in (*decision.review_refs, *decision.evidence_refs):
            artifact = self.blackboard.artifacts.get(str(ref))
            if artifact.artifact_type is ArtifactType.REVIEW:
                refs.add(artifact.ref)
        return refs

    def _dependency_output_refs(
        self,
        node_ids: Sequence[str],
        artifact_type: ArtifactType,
    ) -> set[str]:
        refs: set[str] = set()
        for node_id in node_ids:
            node = self.graph.get(node_id)
            for ref in node.output_artifact_ids:
                artifact = self.blackboard.artifacts.get(ref)
                if artifact.artifact_type is artifact_type:
                    refs.add(artifact.ref)
        return refs

    @staticmethod
    def _is_legacy_hypothesis(artifact: Artifact) -> bool:
        required = {
            "perspective",
            "root_cause",
            "direct_cause",
            "supporting_evidence",
            "affected_symbols",
            "verification_plan",
        }
        return not required.issubset(artifact.content)

    def _cancel_active_patch_path(self, reason: str) -> tuple[str, ...]:
        changed_ids: list[str] = []
        for node in self.graph.nodes:
            if node.node_type not in {
                NodeType.PATCH_TASK,
                NodeType.VALIDATION_TASK,
                NodeType.FINALIZATION_TASK,
            } or node.terminal:
                continue
            before = self.graph.version
            changed = self.graph.transition(
                node.node_id,
                NodeStatus.CANCELLED,
                reason=reason,
            )
            self._bump_graph_delta(before)
            changed_ids.extend(changed)
        if changed_ids:
            self._trace_node_changes(changed_ids, "node_invalidated")
        return tuple(dict.fromkeys(changed_ids))

    def _reject(
        self,
        decision_id: str | None,
        code: str,
        message: str,
        *,
        terminate: bool = False,
        recoverable: bool = False,
        recommended_stage: str | None = None,
        allowed_next_actions: Sequence[str] = (),
        trigger_artifact_refs: Sequence[str] = (),
        details: Mapping[str, Any] | None = None,
    ) -> DecisionResult:
        if terminate and self.status is EngineStatus.ACTIVE:
            self._terminate(EngineStatus.FAILED, code)
        result = DecisionResult(
            False,
            code,
            message,
            decision_id,
            self.state_version,
            recoverable=recoverable,
            recommended_stage=recommended_stage,
            allowed_next_actions=tuple(allowed_next_actions),
            trigger_artifact_refs=tuple(trigger_artifact_refs),
            details=dict(details or {}),
        )
        if self.last_supervisor_call is not None:
            self.last_supervisor_call["decision_result"] = result.to_dict()
        self._trace(
            "supervisor_decision_rejected",
            {
                **result.to_dict(),
                "hypothesis_resolution": self.blackboard.hypothesis_resolution.to_dict(),
            },
        )
        return result
