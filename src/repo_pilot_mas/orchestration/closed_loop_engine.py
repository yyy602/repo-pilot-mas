"""Reviewed root-cause gates for the final orchestration engine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import repo_pilot_mas.orchestration.engine as legacy_engine
from repo_pilot_mas.orchestration.policy_violation import DecisionPolicyViolation
from repo_pilot_mas.orchestration.task_graph import NodeStatus, NodeType
from repo_pilot_mas.schemas.artifact import Artifact, ArtifactType
from repo_pilot_mas.schemas.hypothesis_resolution import HypothesisResolutionStatus
from repo_pilot_mas.schemas.supervisor_decision import DecisionAction, SupervisorDecision

_ROOT_CAUSE_REVIEW_MODES = frozenset(
    {"hypothesis_comparison", "root_cause_recommendation"}
)
_ACCEPTABLE_ROOT_CAUSE_VERDICTS = frozenset({"approved", "compatible", "supported"})


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """Result of one deterministic decision application."""

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


class OrchestrationEngine(legacy_engine.OrchestrationEngine):
    """Final Engine that requires reviewed root causes before patch execution."""

    def snapshot(self) -> dict[str, Any]:
        snapshot = super().snapshot()
        resolution = self.blackboard.hypothesis_resolution
        confirmed_reproduction_refs = self._confirmed_failure_reproduction_refs()
        task_snapshot = snapshot.get("task", {})
        snapshot["task"] = {
            **(dict(task_snapshot) if isinstance(task_snapshot, Mapping) else {}),
            "failing_tests": list(self.task.failing_tests),
            "test_command": list(self.task.test_command),
            "target_files": list(self.task.target_files),
            "max_runtime_seconds": self.task.max_runtime_seconds,
            "requires_failure_reproduction": bool(self.task.failing_tests),
            "confirmed_failure_reproduction_refs": list(
                confirmed_reproduction_refs
            ),
        }
        snapshot["selections"] = {
            "hypothesis_ref": resolution.primary_ref,
            "hypothesis_refs": list(resolution.accepted_refs),
            "review_refs": list(resolution.review_refs),
            "hypothesis_resolution": resolution.to_dict(),
            "patch_ref": self.blackboard.selected_patch_ref,
            "validation_ref": self.blackboard.validation_ref,
        }
        return snapshot

    def apply_decision(self, decision: SupervisorDecision) -> DecisionResult:
        if self.status is not legacy_engine.EngineStatus.ACTIVE:
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
            self._validate_hypothesis_acceptance(decision)
        elif decision.action is DecisionAction.CREATE_TASK:
            confirmed_reproduction_refs = set(
                self._confirmed_failure_reproduction_refs()
            )
            diagnosis_requests = tuple(
                request
                for request in decision.create_tasks
                if NodeType(request.node_type) is NodeType.DIAGNOSIS_TASK
            )
            if self.task.failing_tests and diagnosis_requests:
                existing_evidence_refs = tuple(
                    artifact.ref
                    for artifact in self.blackboard.artifacts.latest_values()
                    if artifact.artifact_type is ArtifactType.EVIDENCE
                )
                if not confirmed_reproduction_refs:
                    raise DecisionPolicyViolation(
                        "FAILURE_REPRODUCTION_REQUIRED",
                        "Diagnosis requires a successful failure_reproduction Evidence "
                        "when the task declares failing_tests",
                        recommended_stage=(
                            legacy_engine.WorkflowStage.INVESTIGATION.value
                        ),
                        allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                        trigger_artifact_refs=existing_evidence_refs,
                        details={
                            "failing_tests": list(self.task.failing_tests),
                            "test_command": list(self.task.test_command),
                            "required_investigator_mode": "failure_reproduction",
                        },
                    )
                for request in diagnosis_requests:
                    if not confirmed_reproduction_refs.intersection(
                        request.input_artifact_ids
                    ):
                        raise DecisionPolicyViolation(
                            "FAILURE_REPRODUCTION_INPUT_REQUIRED",
                            "DiagnosisTask inputs must include a confirmed "
                            "failure_reproduction Evidence",
                            recommended_stage=(
                                legacy_engine.WorkflowStage.INVESTIGATION.value
                            ),
                            allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                            trigger_artifact_refs=tuple(
                                sorted(confirmed_reproduction_refs)
                            ),
                            details={
                                "confirmed_failure_reproduction_refs": sorted(
                                    confirmed_reproduction_refs
                                ),
                                "actual_input_artifact_ids": list(
                                    request.input_artifact_ids
                                ),
                            },
                        )
            for request in decision.create_tasks:
                if NodeType(request.node_type) is NodeType.PATCH_TASK:
                    self._validate_patch_eligibility(request)

    def _execute_decision(self, decision: SupervisorDecision) -> tuple[str, ...]:
        if decision.action is DecisionAction.ACCEPT_HYPOTHESIS:
            hypotheses, reviews = self._validate_hypothesis_acceptance(decision)
            primary = self.blackboard.artifacts.get(
                str(decision.primary_hypothesis_ref)
            ).ref
            self.blackboard.set_hypotheses(
                tuple(artifact.ref for artifact in hypotheses),
                primary_ref=primary,
                review_refs=tuple(artifact.ref for artifact in reviews),
                decision_id=decision.decision_id,
            )
            return ()

        mutated = list(super()._execute_decision(decision))
        if (
            decision.action is DecisionAction.REQUEST_REPLAN
            and decision.next_workflow_stage
            in {
                legacy_engine.WorkflowStage.INVESTIGATION.value,
                legacy_engine.WorkflowStage.DIAGNOSIS.value,
            }
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

    def _confirmed_failure_reproduction_refs(self) -> tuple[str, ...]:
        confirmed: list[str] = []
        for artifact in self.blackboard.artifacts.latest_values():
            if (
                artifact.artifact_type is not ArtifactType.EVIDENCE
                or artifact.content.get("mode") != "failure_reproduction"
            ):
                continue
            reproduction = artifact.content.get("reproduction")
            if not isinstance(reproduction, Mapping):
                continue
            if (
                reproduction.get("attempted") is True
                and reproduction.get("succeeded") is True
            ):
                confirmed.append(artifact.ref)
        return tuple(confirmed)

    def _validate_hypothesis_acceptance(
        self,
        decision: SupervisorDecision,
    ) -> tuple[tuple[Artifact, ...], tuple[Artifact, ...]]:
        hypotheses = self._canonical_artifacts(
            decision.hypothesis_refs,
            ArtifactType.HYPOTHESIS,
            stale_code="HYPOTHESIS_SELECTION_STALE",
        )
        accepted_refs = {artifact.ref for artifact in hypotheses}
        primary = self.blackboard.artifacts.get(
            str(decision.primary_hypothesis_ref)
        )
        if primary.artifact_type is not ArtifactType.HYPOTHESIS:
            raise DecisionPolicyViolation(
                "PRIMARY_HYPOTHESIS_INVALID",
                "primary_hypothesis_ref must reference a Hypothesis Artifact",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("ACCEPT_HYPOTHESIS", "CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=(primary.ref,),
            )
        if primary.ref not in accepted_refs:
            raise DecisionPolicyViolation(
                "PRIMARY_HYPOTHESIS_MISMATCH",
                "primary_hypothesis_ref must belong to hypothesis_refs",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("ACCEPT_HYPOTHESIS", "CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(sorted(accepted_refs)),
            )
        if not decision.review_refs:
            raise DecisionPolicyViolation(
                "HYPOTHESIS_REVIEW_REQUIRED",
                "ACCEPT_HYPOTHESIS requires current root-cause Review artifacts",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "CHANGE_WORKFLOW_STAGE", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(sorted(accepted_refs)),
                details={
                    "required_reviewer_modes": sorted(_ROOT_CAUSE_REVIEW_MODES),
                    "candidate_hypothesis_refs": sorted(accepted_refs),
                },
            )

        reviews = self._canonical_artifacts(
            decision.review_refs,
            ArtifactType.REVIEW,
            stale_code="HYPOTHESIS_REVIEW_STALE",
        )
        covered_refs: set[str] = set()
        for review in reviews:
            covered_refs.update(self._validate_root_cause_review(review))
        missing_refs = accepted_refs - covered_refs
        if missing_refs:
            raise DecisionPolicyViolation(
                "HYPOTHESIS_REVIEW_REQUIRED",
                "The cited Reviews do not cover every accepted Hypothesis",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "ACCEPT_HYPOTHESIS", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(sorted(missing_refs)),
                details={"unreviewed_hypothesis_refs": sorted(missing_refs)},
            )

        if len(hypotheses) == 1:
            if not any(
                review.content.get("mode") == "root_cause_recommendation"
                for review in reviews
            ):
                raise DecisionPolicyViolation(
                    "ROOT_CAUSE_RECOMMENDATION_REQUIRED",
                    "A single Hypothesis requires a root_cause_recommendation Review",
                    recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                    allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                    trigger_artifact_refs=tuple(review.ref for review in reviews),
                )
        elif not any(
            review.content.get("mode") == "hypothesis_comparison"
            and accepted_refs.issubset(self._review_target_refs(review))
            for review in reviews
        ):
            raise DecisionPolicyViolation(
                "HYPOTHESIS_COMPARISON_REQUIRED",
                "Multiple Hypotheses require one comparison Review covering the full set",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(sorted(accepted_refs)),
            )
        return hypotheses, reviews

    def _validate_root_cause_review(self, review: Artifact) -> frozenset[str]:
        mode = str(review.content.get("mode", ""))
        if mode not in _ROOT_CAUSE_REVIEW_MODES:
            raise DecisionPolicyViolation(
                "ROOT_CAUSE_REVIEW_MODE_INVALID",
                f"Review {review.ref} is not a root-cause Review",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=(review.ref,),
            )

        verdict = str(review.content.get("verdict", ""))
        blocking = {
            "needs_more_evidence": (
                "HYPOTHESIS_NEEDS_EVIDENCE",
                legacy_engine.WorkflowStage.INVESTIGATION.value,
            ),
            "changes_requested": (
                "HYPOTHESIS_NEEDS_REVISION",
                legacy_engine.WorkflowStage.DIAGNOSIS.value,
            ),
            "conflict": (
                "HYPOTHESIS_CONFLICTING",
                legacy_engine.WorkflowStage.REVIEW.value,
            ),
            "unsupported": (
                "HYPOTHESIS_REJECTED",
                legacy_engine.WorkflowStage.DIAGNOSIS.value,
            ),
        }
        if verdict in blocking:
            code, stage = blocking[verdict]
            raise DecisionPolicyViolation(
                code,
                f"Review {review.ref} has blocking verdict {verdict}",
                recommended_stage=stage,
                allowed_next_actions=("CREATE_TASK", "REQUEST_REPLAN", "TERMINATE_TASK"),
                trigger_artifact_refs=(review.ref,),
                details={"review_ref": review.ref, "verdict": verdict},
            )
        if verdict not in _ACCEPTABLE_ROOT_CAUSE_VERDICTS:
            raise DecisionPolicyViolation(
                "HYPOTHESIS_REVIEW_INVALID",
                f"Review {review.ref} has unsupported verdict {verdict!r}",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=(review.ref,),
            )

        raw_evidence_refs = review.content.get("evidence_refs", ())
        if isinstance(raw_evidence_refs, (str, bytes)):
            raw_evidence_refs = (raw_evidence_refs,)
        evidence = [
            self.blackboard.artifacts.get(str(ref))
            for ref in raw_evidence_refs
        ]
        if not any(artifact.artifact_type is ArtifactType.EVIDENCE for artifact in evidence):
            raise DecisionPolicyViolation(
                "HYPOTHESIS_REVIEW_LACKS_DIRECT_EVIDENCE",
                f"Review {review.ref} must cite at least one Evidence Artifact",
                recommended_stage=legacy_engine.WorkflowStage.INVESTIGATION.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=(review.ref,),
            )
        return self._review_target_refs(review)

    def _validate_patch_eligibility(self, request: Any) -> None:
        resolution = self.blackboard.hypothesis_resolution
        if resolution.status is not HypothesisResolutionStatus.ACCEPTED:
            stage_by_status = {
                HypothesisResolutionStatus.CONFLICT: legacy_engine.WorkflowStage.REVIEW.value,
                HypothesisResolutionStatus.INVALIDATED: legacy_engine.WorkflowStage.REVIEW.value,
                HypothesisResolutionStatus.NEEDS_EVIDENCE:
                    legacy_engine.WorkflowStage.INVESTIGATION.value,
                HypothesisResolutionStatus.NEEDS_REVISION:
                    legacy_engine.WorkflowStage.DIAGNOSIS.value,
                HypothesisResolutionStatus.REJECTED:
                    legacy_engine.WorkflowStage.DIAGNOSIS.value,
            }
            raise DecisionPolicyViolation(
                "HYPOTHESIS_NOT_RESOLVED",
                "PatchTask requires an accepted Hypothesis resolution",
                recommended_stage=stage_by_status.get(
                    resolution.status,
                    legacy_engine.WorkflowStage.REVIEW.value,
                ),
                allowed_next_actions=("CREATE_TASK", "CHANGE_WORKFLOW_STAGE", "TERMINATE_TASK"),
                trigger_artifact_refs=resolution.candidate_refs,
                details={"hypothesis_resolution_status": resolution.status.value},
            )

        accepted = self._canonical_artifacts(
            resolution.accepted_refs,
            ArtifactType.HYPOTHESIS,
            stale_code="HYPOTHESIS_SELECTION_STALE",
        )
        reviews = self._canonical_artifacts(
            resolution.review_refs,
            ArtifactType.REVIEW,
            stale_code="HYPOTHESIS_REVIEW_STALE",
        )
        for review in reviews:
            self._validate_root_cause_review(review)

        inputs = tuple(
            self.blackboard.artifacts.get(str(ref))
            for ref in request.input_artifact_ids
        )
        actual_hypothesis_refs = {
            artifact.ref
            for artifact in inputs
            if artifact.artifact_type is ArtifactType.HYPOTHESIS
        }
        expected_hypothesis_refs = {artifact.ref for artifact in accepted}
        if actual_hypothesis_refs != expected_hypothesis_refs:
            raise DecisionPolicyViolation(
                "PATCH_HYPOTHESIS_BINDING_MISMATCH",
                "PatchTask inputs must contain exactly the accepted Hypothesis set",
                recommended_stage=legacy_engine.WorkflowStage.PATCH.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(
                    sorted(actual_hypothesis_refs | expected_hypothesis_refs)
                ),
                details={
                    "expected_hypothesis_refs": sorted(expected_hypothesis_refs),
                    "actual_hypothesis_refs": sorted(actual_hypothesis_refs),
                },
            )

        actual_review_refs = {
            artifact.ref
            for artifact in inputs
            if artifact.artifact_type is ArtifactType.REVIEW
        }
        expected_review_refs = {artifact.ref for artifact in reviews}
        if not expected_review_refs.issubset(actual_review_refs):
            raise DecisionPolicyViolation(
                "PATCH_REVIEW_CONTEXT_MISSING",
                "PatchTask inputs must include every accepted root-cause Review",
                recommended_stage=legacy_engine.WorkflowStage.PATCH.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(sorted(expected_review_refs)),
                details={
                    "expected_review_refs": sorted(expected_review_refs),
                    "actual_review_refs": sorted(actual_review_refs),
                },
            )

    def _canonical_artifacts(
        self,
        refs: Sequence[str],
        expected_type: ArtifactType,
        *,
        stale_code: str,
    ) -> tuple[Artifact, ...]:
        artifacts_by_ref: dict[str, Artifact] = {}
        for ref in refs:
            artifact = self.blackboard.artifacts.get(str(ref))
            if artifact.artifact_type is not expected_type:
                raise DecisionPolicyViolation(
                    "ARTIFACT_TYPE_MISMATCH",
                    f"Expected {expected_type.value}, got {artifact.artifact_type.value}",
                    recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                    allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                    trigger_artifact_refs=(artifact.ref,),
                )
            if not self.blackboard.artifacts.is_latest(artifact.ref):
                raise DecisionPolicyViolation(
                    stale_code,
                    f"Artifact is not the latest revision: {artifact.ref}",
                    recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                    allowed_next_actions=("CREATE_TASK", "ACCEPT_HYPOTHESIS", "TERMINATE_TASK"),
                    trigger_artifact_refs=(artifact.ref,),
                )
            artifacts_by_ref[artifact.ref] = artifact
        result = tuple(artifacts_by_ref.values())
        if not result:
            raise DecisionPolicyViolation(
                "ARTIFACT_SET_EMPTY",
                f"At least one {expected_type.value} Artifact is required",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
            )
        return result

    def _review_target_refs(self, review: Artifact) -> frozenset[str]:
        raw_targets = review.content.get("target_artifact_refs")
        if raw_targets is None:
            target = review.content.get("target_artifact_ref")
            raw_targets = (target,) if target else ()
        if isinstance(raw_targets, (str, bytes)):
            raw_targets = (raw_targets,)

        refs: set[str] = set()
        for value in raw_targets:
            artifact = self.blackboard.artifacts.get(str(value))
            if artifact.artifact_type is ArtifactType.HYPOTHESIS:
                refs.add(artifact.ref)
        for ref in review.input_refs:
            artifact = self.blackboard.artifacts.get(ref)
            if artifact.artifact_type is ArtifactType.HYPOTHESIS:
                refs.add(artifact.ref)
        return frozenset(refs)

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
        if terminate and self.status is legacy_engine.EngineStatus.ACTIVE:
            self._terminate(legacy_engine.EngineStatus.FAILED, code)
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
