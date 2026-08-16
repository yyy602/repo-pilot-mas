"""Reviewed root-cause gates for the final orchestration engine."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import repo_pilot_mas.orchestration.engine as legacy_engine
from repo_pilot_mas.orchestration.agent_contracts import (
    AgentContractViolation,
    validate_agent_input_contract,
)
from repo_pilot_mas.orchestration.policy_violation import DecisionPolicyViolation
from repo_pilot_mas.orchestration.task_graph import NodeStatus, NodeType
from repo_pilot_mas.schemas.artifact import Artifact, ArtifactType
from repo_pilot_mas.schemas.hypothesis_resolution import (
    HypothesisResolution,
    HypothesisResolutionStatus,
)
from repo_pilot_mas.schemas.supervisor_decision import (
    DecisionAction,
    SupervisorDecision,
    additional_investigation_required,
)

_ROOT_CAUSE_REVIEW_MODES = frozenset({"hypothesis_comparison", "root_cause_recommendation"})
_ACCEPTABLE_ROOT_CAUSE_VERDICTS = frozenset({"approved", "compatible", "supported"})
_BLOCKING_REVIEW_VERDICTS = frozenset(
    {"needs_more_evidence", "changes_requested", "conflict", "unsupported"}
)
_BLOCKING_RESOLUTION_POLICY = {
    HypothesisResolutionStatus.NEEDS_EVIDENCE: (
        "HYPOTHESIS_NEEDS_EVIDENCE",
        legacy_engine.WorkflowStage.INVESTIGATION.value,
    ),
    HypothesisResolutionStatus.NEEDS_REVISION: (
        "HYPOTHESIS_NEEDS_REVISION",
        legacy_engine.WorkflowStage.DIAGNOSIS.value,
    ),
    HypothesisResolutionStatus.CONFLICT: (
        "HYPOTHESIS_CONFLICTING",
        legacy_engine.WorkflowStage.REVIEW.value,
    ),
    HypothesisResolutionStatus.REJECTED: (
        "HYPOTHESIS_REJECTED",
        legacy_engine.WorkflowStage.DIAGNOSIS.value,
    ),
    HypothesisResolutionStatus.INVALIDATED: (
        "HYPOTHESIS_NOT_RESOLVED",
        legacy_engine.WorkflowStage.REVIEW.value,
    ),
}


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
            "confirmed_failure_reproduction_refs": list(confirmed_reproduction_refs),
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

    def run_supervisor(self, supervisor: Any) -> DecisionResult:
        """Run Supervisor without turning recoverable model failures into task death."""

        budget_error = self._supervisor_budget_error()
        if budget_error:
            return self._reject(
                None,
                budget_error,
                "supervisor budget is exhausted",
                terminate=True,
            )
        started = time.perf_counter()
        try:
            outcome = supervisor.decide(self.snapshot())
        except RuntimeError as exc:
            self.budget.supervisor_calls += 1
            usage = getattr(exc, "usage", None)
            input_tokens = int(usage.input_tokens) if usage is not None else 0
            output_tokens = int(usage.output_tokens) if usage is not None else 0
            self.budget.input_tokens += input_tokens
            self.budget.output_tokens += output_tokens
            code = str(getattr(exc, "code", "SUPERVISOR_ERROR"))
            recoverable = bool(getattr(exc, "recoverable", True))
            latency_ms = int((time.perf_counter() - started) * 1000)
            self.last_supervisor_call = {
                "ok": False,
                "code": code,
                "recoverable": recoverable,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
                "details": dict(getattr(exc, "details", {})),
            }
            self.blackboard.bump()
            self._trace(
                "supervisor_failed",
                {
                    "code": code,
                    "message": str(exc),
                    "recoverable": recoverable,
                    "latency_ms": latency_ms,
                },
            )
            return self._reject(
                None,
                code,
                str(exc),
                terminate=not recoverable,
                recoverable=recoverable,
                recommended_stage=(
                    self.blackboard.workflow_stage if recoverable else None
                ),
                allowed_next_actions=(
                    ("CREATE_TASK", "TERMINATE_TASK") if recoverable else ()
                ),
                details=dict(getattr(exc, "details", {})),
            )

        self.budget.supervisor_calls += 1
        self.budget.input_tokens += outcome.input_tokens
        self.budget.output_tokens += outcome.output_tokens
        self.last_supervisor_call = {
            "ok": True,
            "model_id": outcome.model_id,
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "latency_ms": outcome.latency_ms,
            "attempts": outcome.attempts,
            "raw_response_ref": outcome.raw_response_ref,
            "trace_id": outcome.trace_id,
            "metadata": dict(outcome.metadata),
        }
        self.blackboard.bump()
        token_error = self._token_budget_error()
        if token_error:
            return self._reject(
                outcome.decision.decision_id,
                token_error,
                "supervisor token budget is exhausted",
                terminate=True,
            )
        result = self.apply_decision(outcome.decision)
        if self.last_supervisor_call is not None:
            self.last_supervisor_call["decision_result"] = result.to_dict()
        return result

    def add_artifact(self, artifact: Artifact) -> str:
        """Add an Artifact and synchronize Phase C review state deterministically."""

        previous_status = self.blackboard.hypothesis_resolution.status
        ref = super().add_artifact(artifact)
        self._decision_fingerprints.difference_update(
            self._rejected_decision_fingerprints
        )
        self._rejected_decision_fingerprints.clear()
        self._no_progress_decisions = 0

        if (
            artifact.artifact_type is ArtifactType.HYPOTHESIS
            and previous_status in _BLOCKING_RESOLUTION_POLICY
        ):
            candidate_refs = tuple(
                item.ref
                for item in self.blackboard.artifacts.latest_values()
                if item.artifact_type is ArtifactType.HYPOTHESIS
            )
            self.blackboard.hypothesis_resolution = HypothesisResolution.unresolved(candidate_refs)
            self.blackboard.selected_hypothesis_ref = None
            self.blackboard.selected_patch_ref = None
            self.blackboard.validation_ref = None
            self.blackboard.bump()
            self._trace(
                "hypothesis_resolution_reset",
                {
                    "trigger_hypothesis_ref": ref,
                    "previous_status": previous_status.value,
                    "hypothesis_resolution": (self.blackboard.hypothesis_resolution.to_dict()),
                    "state_version": self.state_version,
                },
            )
            return ref

        if (
            artifact.artifact_type is ArtifactType.REVIEW
            and artifact.content.get("mode") in _ROOT_CAUSE_REVIEW_MODES
        ):
            resolution = self.blackboard.hypothesis_resolution
            cancelled = ()
            if resolution.status in _BLOCKING_RESOLUTION_POLICY:
                cancelled = self._cancel_active_patch_path(
                    f"root-cause Review {artifact.ref} set resolution to {resolution.status.value}"
                )
            self._trace(
                "hypothesis_resolution_updated",
                {
                    "review_ref": artifact.ref,
                    "review_mode": artifact.content.get("mode"),
                    "verdict": artifact.content.get("verdict"),
                    "target_hypothesis_refs": sorted(self._review_target_refs(artifact)),
                    "cancelled_node_ids": list(cancelled),
                    "hypothesis_resolution": resolution.to_dict(),
                    "state_version": self.state_version,
                },
            )
        return ref

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
            terminate = self._record_rejected_decision(decision)
            return self._reject(
                decision.decision_id,
                exc.code,
                str(exc),
                recoverable=exc.recoverable,
                recommended_stage=exc.recommended_stage,
                allowed_next_actions=exc.allowed_next_actions,
                trigger_artifact_refs=exc.trigger_artifact_refs,
                details=exc.details,
                terminate=terminate,
            )
        except (KeyError, TypeError, ValueError) as exc:
            terminate = (
                decision.action is DecisionAction.REQUEST_REPLAN
                and self.budget.replans >= self.budget.max_replans
            )
            terminate = self._record_rejected_decision(decision) or terminate
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

    def _record_rejected_decision(
        self,
        decision: SupervisorDecision,
    ) -> bool:
        self._processed_decision_ids.add(decision.decision_id)
        self._decision_fingerprints.add(decision.fingerprint)
        self._rejected_decision_fingerprints.add(decision.fingerprint)
        self._no_progress_decisions = 0
        return False

    def _validate_decision(self, decision: SupervisorDecision) -> None:
        if decision.action is DecisionAction.CREATE_TASK:
            for request in decision.create_tasks:
                exhausted = tuple(
                    node
                    for node in self.graph.nodes
                    if node.node_type.value == request.node_type
                    and node.agent_type == request.agent_type
                    and node.mode == request.mode
                    and node.input_artifact_ids == request.input_artifact_ids
                    and node.status in {NodeStatus.FAILED, NodeStatus.TIMED_OUT}
                    and node.retry_count >= self.budget.max_retries_per_node
                )
                if exhausted:
                    raise DecisionPolicyViolation(
                        "LOGICAL_TASK_RETRY_EXHAUSTED",
                        "A logically equivalent Worker task already exhausted its "
                        "retry budget; a new node_id cannot reset that budget",
                        recommended_stage=self._contract_recovery_stage(
                            NodeType(request.node_type)
                        ),
                        allowed_next_actions=("REQUEST_REPLAN", "TERMINATE_TASK"),
                        trigger_artifact_refs=tuple(
                            ref
                            for node in exhausted
                            for ref in node.output_artifact_ids
                        ),
                        details={
                            "exhausted_node_ids": [node.node_id for node in exhausted],
                            "node_type": request.node_type,
                            "agent_type": request.agent_type,
                            "mode": request.mode,
                            "input_artifact_ids": list(request.input_artifact_ids),
                        },
                    )
        if decision.action is DecisionAction.CREATE_TASK and any(
            request.node_type == NodeType.INVESTIGATION_TASK.value
            for request in decision.create_tasks
        ):
            recovery = self._replan_recovery_state()
            artifacts = self.blackboard.artifact_summaries()
            if (
                not recovery.get("pending_replan")
                and not additional_investigation_required(artifacts)
            ):
                evidence_refs = tuple(
                    artifact.ref
                    for artifact in self.blackboard.artifacts.latest_values()
                    if artifact.artifact_type is ArtifactType.EVIDENCE
                    and artifact.content.get("verified") is True
                    and artifact.content.get("tool_trace_ids")
                )
                raise DecisionPolicyViolation(
                    "EVIDENCE_COLLECTION_ALREADY_SUFFICIENT",
                    "Verified source and failure evidence already satisfy the minimum "
                    "Evidence Gate; continue with Diagnosis instead of expanding "
                    "Investigation",
                    recommended_stage=legacy_engine.WorkflowStage.DIAGNOSIS.value,
                    allowed_next_actions=(
                        "CREATE_TASK",
                        "CHANGE_WORKFLOW_STAGE",
                        "TERMINATE_TASK",
                    ),
                    trigger_artifact_refs=evidence_refs,
                    details={
                        "required_next_node_type": NodeType.DIAGNOSIS_TASK.value,
                    },
                )
        super()._validate_decision(decision)
        if decision.action is DecisionAction.ACCEPT_HYPOTHESIS:
            self._validate_hypothesis_acceptance(decision)
        elif decision.action is DecisionAction.CREATE_TASK:
            confirmed_reproduction_refs = set(self._confirmed_failure_reproduction_refs())
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
                        recommended_stage=(legacy_engine.WorkflowStage.INVESTIGATION.value),
                        allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                        trigger_artifact_refs=existing_evidence_refs,
                        details={
                            "failing_tests": list(self.task.failing_tests),
                            "test_command": list(self.task.test_command),
                            "required_investigator_mode": "failure_reproduction",
                        },
                    )
                for request in diagnosis_requests:
                    if not confirmed_reproduction_refs.intersection(request.input_artifact_ids):
                        raise DecisionPolicyViolation(
                            "FAILURE_REPRODUCTION_INPUT_REQUIRED",
                            "DiagnosisTask inputs must include a confirmed "
                            "failure_reproduction Evidence",
                            recommended_stage=(legacy_engine.WorkflowStage.INVESTIGATION.value),
                            allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                            trigger_artifact_refs=tuple(sorted(confirmed_reproduction_refs)),
                            details={
                                "confirmed_failure_reproduction_refs": sorted(
                                    confirmed_reproduction_refs
                                ),
                                "actual_input_artifact_ids": list(request.input_artifact_ids),
                            },
                        )
            for request in decision.create_tasks:
                node_type = NodeType(request.node_type)
                input_artifacts = tuple(
                    self.blackboard.artifacts.get(str(ref)).to_dict()
                    for ref in request.input_artifact_ids
                )
                try:
                    validate_agent_input_contract(
                        request.agent_type,
                        request.mode,
                        input_artifacts,
                    )
                except AgentContractViolation as exc:
                    self._trace(
                        "agent_input_contract_rejected",
                        {
                            "decision_id": decision.decision_id,
                            "code": AgentContractViolation.code,
                            "node_type": node_type.value,
                            "agent_type": request.agent_type,
                            "mode": request.mode,
                            "input_artifact_refs": list(
                                request.input_artifact_ids
                            ),
                            "message": str(exc),
                        },
                    )
                    raise DecisionPolicyViolation(
                        AgentContractViolation.code,
                        str(exc),
                        recommended_stage=self._contract_recovery_stage(node_type),
                        allowed_next_actions=("CREATE_TASK",),
                        trigger_artifact_refs=request.input_artifact_ids,
                        details={
                            "node_type": node_type.value,
                            "agent_type": request.agent_type,
                            "mode": request.mode,
                            "input_artifact_refs": list(request.input_artifact_ids),
                            "input_artifact_types": [
                                item.artifact_type.value
                                for item in (
                                    self.blackboard.artifacts.get(str(ref))
                                    for ref in request.input_artifact_ids
                                )
                            ],
                        },
                    ) from exc
                if node_type is NodeType.PATCH_TASK:
                    self._validate_patch_eligibility(request)
                elif node_type is NodeType.VALIDATION_TASK:
                    self._validate_validation_eligibility(request)
        elif decision.action in {
            DecisionAction.SELECT_PATCH,
            DecisionAction.FINALIZE_TASK,
        }:
            self._validate_patch_review_for_selection(
                str(decision.patch_ref),
                str(decision.validation_ref),
            )

    @staticmethod
    def _contract_recovery_stage(node_type: NodeType) -> str:
        if node_type is NodeType.INVESTIGATION_TASK:
            return legacy_engine.WorkflowStage.INVESTIGATION.value
        if node_type in {
            NodeType.DIAGNOSIS_TASK,
            NodeType.CHALLENGE_TASK,
            NodeType.REBUTTAL_TASK,
        }:
            return legacy_engine.WorkflowStage.DIAGNOSIS.value
        if node_type is NodeType.REVIEW_TASK:
            return legacy_engine.WorkflowStage.REVIEW.value
        return legacy_engine.WorkflowStage.PATCH.value

    def _execute_decision(self, decision: SupervisorDecision) -> tuple[str, ...]:
        if decision.action is DecisionAction.ACCEPT_HYPOTHESIS:
            hypotheses, reviews = self._validate_hypothesis_acceptance(decision)
            primary = self.blackboard.artifacts.get(str(decision.primary_hypothesis_ref)).ref
            self.blackboard.set_hypotheses(
                tuple(artifact.ref for artifact in hypotheses),
                primary_ref=primary,
                review_refs=tuple(artifact.ref for artifact in reviews),
                decision_id=decision.decision_id,
            )
            self._trace(
                "hypothesis_resolution_accepted",
                {
                    "decision_id": decision.decision_id,
                    "accepted_hypothesis_refs": [artifact.ref for artifact in hypotheses],
                    "primary_hypothesis_ref": primary,
                    "review_refs": [artifact.ref for artifact in reviews],
                    "hypothesis_resolution": (self.blackboard.hypothesis_resolution.to_dict()),
                    "state_version": self.state_version,
                },
            )
            return ()

        mutated = list(super()._execute_decision(decision))
        if decision.action is DecisionAction.REQUEST_REPLAN and decision.next_workflow_stage in {
            legacy_engine.WorkflowStage.INVESTIGATION.value,
            legacy_engine.WorkflowStage.DIAGNOSIS.value,
        }:
            reason = (
                f"Supervisor requested replan to {decision.next_workflow_stage}: "
                f"{decision.failure_class}"
            )
            if self.blackboard.hypothesis_resolution.status is HypothesisResolutionStatus.ACCEPTED:
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
            if reproduction.get("attempted") is True and reproduction.get("succeeded") is True:
                confirmed.append(artifact.ref)
        return tuple(confirmed)

    def _validate_hypothesis_acceptance(
        self,
        decision: SupervisorDecision,
    ) -> tuple[tuple[Artifact, ...], tuple[Artifact, ...]]:
        resolution = self.blackboard.hypothesis_resolution
        if resolution.status is HypothesisResolutionStatus.ACCEPTED:
            raise DecisionPolicyViolation(
                "HYPOTHESIS_ALREADY_ACCEPTED",
                "The current Hypothesis resolution is already accepted",
                recommended_stage=legacy_engine.WorkflowStage.PATCH.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=resolution.accepted_refs,
            )
        if resolution.status in _BLOCKING_RESOLUTION_POLICY:
            code, stage = _BLOCKING_RESOLUTION_POLICY[resolution.status]
            raise DecisionPolicyViolation(
                code,
                f"Hypothesis resolution is blocked by status {resolution.status.value}",
                recommended_stage=stage,
                allowed_next_actions=(
                    "CREATE_TASK",
                    "REQUEST_REPLAN",
                    "TERMINATE_TASK",
                ),
                trigger_artifact_refs=resolution.review_refs or resolution.candidate_refs,
                details={
                    "hypothesis_resolution": resolution.to_dict(),
                },
            )

        hypotheses = self._canonical_artifacts(
            decision.hypothesis_refs,
            ArtifactType.HYPOTHESIS,
            stale_code="HYPOTHESIS_SELECTION_STALE",
        )
        accepted_refs = {artifact.ref for artifact in hypotheses}
        primary = self.blackboard.artifacts.get(str(decision.primary_hypothesis_ref))
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
        observed_reviews = set(resolution.review_refs)
        if observed_reviews and not {item.ref for item in reviews}.issubset(observed_reviews):
            raise DecisionPolicyViolation(
                "HYPOTHESIS_REVIEW_STATE_STALE",
                "ACCEPT_HYPOTHESIS cites Reviews not represented by the current "
                "Hypothesis resolution",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(item.ref for item in reviews),
                details={"observed_review_refs": sorted(observed_reviews)},
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

        candidate_refs = set(resolution.candidate_refs) or accepted_refs
        has_recommendation = any(
            review.content.get("mode") == "root_cause_recommendation"
            and accepted_refs.issubset(self._review_target_refs(review))
            for review in reviews
        )
        has_full_comparison = any(
            review.content.get("mode") == "hypothesis_comparison"
            and candidate_refs.issubset(self._review_target_refs(review))
            for review in reviews
        )
        if len(hypotheses) == 1:
            if not (has_recommendation or has_full_comparison):
                raise DecisionPolicyViolation(
                    "ROOT_CAUSE_RECOMMENDATION_REQUIRED",
                    "A selected Hypothesis requires either its own root-cause "
                    "recommendation or a comparison covering the candidate set",
                    recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                    allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                    trigger_artifact_refs=tuple(review.ref for review in reviews),
                )
        elif not has_full_comparison:
            raise DecisionPolicyViolation(
                "HYPOTHESIS_COMPARISON_REQUIRED",
                "Multiple candidate Hypotheses require one comparison Review "
                "covering the full candidate set",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(sorted(candidate_refs)),
            )
        self._validate_evidence_gate(hypotheses, reviews)
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

        verification_steps = review.content.get("verification_steps_executed", ())
        alternative_causes = review.content.get("alternative_causes", ())
        remaining_uncertainty = review.content.get("remaining_uncertainty", ())
        if (
            review.content.get("failure_explained") is not True
            or review.content.get("causal_chain_complete") is not True
            or not verification_steps
            or (
                not alternative_causes
                and review.content.get("counterexample_checked") is not True
            )
            or bool(remaining_uncertainty)
        ):
            raise DecisionPolicyViolation(
                "REVIEW_INDEPENDENT_VERIFICATION_INCOMPLETE",
                f"Review {review.ref} does not satisfy independent verification gates",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=(review.ref,),
                details={
                    "failure_explained": review.content.get("failure_explained"),
                    "causal_chain_complete": review.content.get(
                        "causal_chain_complete"
                    ),
                    "alternative_causes": list(alternative_causes),
                    "counterexample_checked": review.content.get(
                        "counterexample_checked"
                    ),
                    "verification_steps_executed": list(verification_steps),
                    "remaining_uncertainty": list(remaining_uncertainty),
                },
            )

        raw_evidence_refs = review.content.get("evidence_refs", ())
        if isinstance(raw_evidence_refs, (str, bytes)):
            raw_evidence_refs = (raw_evidence_refs,)
        evidence = [self.blackboard.artifacts.get(str(ref)) for ref in raw_evidence_refs]
        if not any(artifact.artifact_type is ArtifactType.EVIDENCE for artifact in evidence):
            raise DecisionPolicyViolation(
                "HYPOTHESIS_REVIEW_LACKS_DIRECT_EVIDENCE",
                f"Review {review.ref} must cite at least one Evidence Artifact",
                recommended_stage=legacy_engine.WorkflowStage.INVESTIGATION.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=(review.ref,),
            )
        return self._review_target_refs(review)

    def _validate_evidence_gate(
        self,
        hypotheses: Sequence[Artifact],
        reviews: Sequence[Artifact],
    ) -> None:
        supporting_refs = tuple(
            dict.fromkeys(
                str(ref)
                for hypothesis in hypotheses
                for ref in hypothesis.content.get("supporting_evidence", ())
            )
        )
        if any(hypothesis.content.get("missing_evidence") for hypothesis in hypotheses):
            self._reject_evidence_gate(
                "HYPOTHESIS_HAS_BLOCKING_EVIDENCE_GAPS",
                "accepted Hypothesis candidates still declare missing evidence",
                supporting_refs,
            )
        evidence: list[Artifact] = []
        for ref in supporting_refs:
            try:
                artifact = self.blackboard.artifacts.get(ref)
            except KeyError:
                self._reject_evidence_gate(
                    "HYPOTHESIS_SUPPORTING_EVIDENCE_MISSING",
                    f"Hypothesis cites unavailable Evidence: {ref}",
                    supporting_refs,
                )
            if artifact.artifact_type is not ArtifactType.EVIDENCE:
                self._reject_evidence_gate(
                    "HYPOTHESIS_SUPPORTING_EVIDENCE_INVALID",
                    f"Hypothesis supporting ref is not Evidence: {artifact.ref}",
                    supporting_refs,
                )
            evidence.append(artifact)
        if not evidence:
            self._reject_evidence_gate(
                "HYPOTHESIS_SUPPORTING_EVIDENCE_MISSING",
                "accepted Hypothesis candidates require supporting Evidence",
                supporting_refs,
            )

        unverified = [
            item.ref
            for item in evidence
            if item.content.get("verified") is not True
            or item.content.get("status") != "verified"
            or not item.content.get("tool_trace_ids")
        ]
        if unverified:
            self._reject_evidence_gate(
                "EVIDENCE_NOT_VERIFIED",
                "supporting Evidence must be verified by successful tool traces",
                unverified,
            )

        has_source = any(
            item.content.get("evidence_kind") == "source"
            or isinstance(item.content.get("source"), Mapping)
            for item in evidence
        )
        has_behavior = any(
            (
                item.content.get("evidence_kind") == "reproduction"
                and isinstance(item.content.get("reproduction"), Mapping)
                and item.content["reproduction"].get("succeeded") is True
            )
            or item.content.get("evidence_kind") in {"execution", "counterexample"}
            for item in evidence
        )
        has_execution = any(
            (
                isinstance(item.content.get("reproduction"), Mapping)
                and bool(item.content["reproduction"].get("failure_output"))
            )
            or item.content.get("evidence_kind") in {"execution", "dependency"}
            for item in evidence
        )
        coverage = {
            str(ref)
            for review in reviews
            for ref in review.content.get("evidence_refs", ())
        }
        missing_review_coverage = set(supporting_refs) - coverage
        if not (has_source and has_behavior and has_execution):
            self._reject_evidence_gate(
                "MINIMUM_EVIDENCE_GATE_NOT_MET",
                "Hypothesis acceptance requires source, failure behavior, and execution Evidence",
                supporting_refs,
                details={
                    "has_source": has_source,
                    "has_failure_behavior": has_behavior,
                    "has_execution_path_or_output": has_execution,
                },
            )
        if missing_review_coverage:
            self._reject_evidence_gate(
                "REVIEW_EVIDENCE_COVERAGE_INCOMPLETE",
                "root-cause Reviews do not cover every supporting Evidence",
                tuple(sorted(missing_review_coverage)),
            )

        self._trace(
            "evidence_gate_checked",
            {
                "passed": True,
                "hypothesis_refs": [item.ref for item in hypotheses],
                "evidence_refs": list(supporting_refs),
                "review_refs": [item.ref for item in reviews],
                "has_source": has_source,
                "has_failure_behavior": has_behavior,
                "has_execution_path_or_output": has_execution,
            },
        )

    def _reject_evidence_gate(
        self,
        code: str,
        message: str,
        trigger_refs: Sequence[str],
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        payload = {
            "passed": False,
            "code": code,
            "trigger_artifact_refs": list(trigger_refs),
            **dict(details or {}),
        }
        self._trace("evidence_gate_checked", payload)
        raise DecisionPolicyViolation(
            code,
            message,
            recommended_stage=legacy_engine.WorkflowStage.INVESTIGATION.value,
            allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
            trigger_artifact_refs=trigger_refs,
            details=details,
        )

    def _validate_patch_eligibility(self, request: Any) -> None:
        resolution = self.blackboard.hypothesis_resolution
        if resolution.status is not HypothesisResolutionStatus.ACCEPTED:
            stage_by_status = {
                HypothesisResolutionStatus.CONFLICT: legacy_engine.WorkflowStage.REVIEW.value,
                HypothesisResolutionStatus.INVALIDATED: legacy_engine.WorkflowStage.REVIEW.value,
                HypothesisResolutionStatus.NEEDS_EVIDENCE: legacy_engine.WorkflowStage.INVESTIGATION.value,
                HypothesisResolutionStatus.NEEDS_REVISION: legacy_engine.WorkflowStage.DIAGNOSIS.value,
                HypothesisResolutionStatus.REJECTED: legacy_engine.WorkflowStage.DIAGNOSIS.value,
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
            self.blackboard.artifacts.get(str(ref)) for ref in request.input_artifact_ids
        )
        actual_hypothesis_refs = {
            artifact.ref for artifact in inputs if artifact.artifact_type is ArtifactType.HYPOTHESIS
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
            artifact.ref for artifact in inputs if artifact.artifact_type is ArtifactType.REVIEW
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

    def _validate_validation_eligibility(self, request: Any) -> None:
        resolution = self.blackboard.hypothesis_resolution
        if resolution.status is not HypothesisResolutionStatus.ACCEPTED:
            raise DecisionPolicyViolation(
                "HYPOTHESIS_NOT_RESOLVED",
                "ValidationTask requires an accepted Hypothesis resolution",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=resolution.candidate_refs,
                details={"hypothesis_resolution_status": resolution.status.value},
            )

        inputs = tuple(
            self.blackboard.artifacts.get(str(ref)) for ref in request.input_artifact_ids
        )
        patches = tuple(
            item for item in inputs if item.artifact_type is ArtifactType.PATCH_CANDIDATE
        )
        if len(patches) != 1:
            raise DecisionPolicyViolation(
                "VALIDATION_PATCH_BINDING_INVALID",
                "ValidationTask must contain exactly one PatchCandidate",
                recommended_stage=legacy_engine.WorkflowStage.PATCH.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(item.ref for item in patches),
                details={"patch_candidate_count": len(patches)},
            )
        patch = patches[0]

        self._validate_patch_candidate_binding(patch)

        previous_validations = tuple(
            item
            for item in self.blackboard.artifacts.latest_values()
            if item.artifact_type is ArtifactType.VALIDATION_RESULT
            and item.content.get("patch_ref") in {patch.ref, patch.artifact_id}
        )
        if previous_validations:
            raise DecisionPolicyViolation(
                "PATCH_ALREADY_VALIDATED",
                "Each PatchCandidate can be validated only once",
                recommended_stage=legacy_engine.WorkflowStage.PATCH.value,
                allowed_next_actions=("REQUEST_REPLAN", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(
                    item.ref for item in previous_validations
                ),
                details={"patch_ref": patch.ref},
            )

        reviews = self._matching_patch_reviews(patch, inputs)
        if not reviews:
            raise DecisionPolicyViolation(
                "PATCH_REVIEW_REQUIRED",
                "ValidationTask requires a patch_review targeting its PatchCandidate",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=(patch.ref,),
            )
        blocking = tuple(
            review
            for review in reviews
            if review.content.get("verdict") in _BLOCKING_REVIEW_VERDICTS
        )
        if blocking:
            raise DecisionPolicyViolation(
                "PATCH_REVIEW_BLOCKING",
                "ValidationTask cannot use a PatchCandidate with a blocking Review",
                recommended_stage=legacy_engine.WorkflowStage.PATCH.value,
                allowed_next_actions=("CREATE_TASK", "REQUEST_REPLAN", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(item.ref for item in blocking),
                details={
                    "blocking_verdicts": {
                        item.ref: item.content.get("verdict") for item in blocking
                    }
                },
            )
        accepted = tuple(
            review
            for review in reviews
            if review.content.get("verdict") in _ACCEPTABLE_ROOT_CAUSE_VERDICTS
        )
        if not accepted:
            raise DecisionPolicyViolation(
                "PATCH_REVIEW_REQUIRED",
                "ValidationTask requires a non-blocking patch_review",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(item.ref for item in reviews),
            )

    def _validate_patch_review_for_selection(
        self,
        patch_ref: str,
        validation_ref: str,
    ) -> None:
        patch = self.blackboard.artifacts.get(patch_ref)
        validation = self.blackboard.artifacts.get(validation_ref)
        self._validate_patch_candidate_binding(patch)
        reviews = self._matching_patch_reviews(
            patch,
            self.blackboard.artifacts.latest_values(),
        )
        if not reviews:
            raise DecisionPolicyViolation(
                "PATCH_REVIEW_REQUIRED",
                "Patch selection requires a patch_review targeting the selected PatchCandidate",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=(patch.ref, validation.ref),
            )
        blocking = tuple(
            item for item in reviews if item.content.get("verdict") in _BLOCKING_REVIEW_VERDICTS
        )
        if blocking:
            raise DecisionPolicyViolation(
                "PATCH_REVIEW_BLOCKING",
                "The selected PatchCandidate has an unresolved blocking Review",
                recommended_stage=legacy_engine.WorkflowStage.PATCH.value,
                allowed_next_actions=("CREATE_TASK", "REQUEST_REPLAN", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(item.ref for item in blocking),
            )
        if not any(
            item.content.get("verdict") in _ACCEPTABLE_ROOT_CAUSE_VERDICTS for item in reviews
        ):
            raise DecisionPolicyViolation(
                "PATCH_REVIEW_REQUIRED",
                "The selected PatchCandidate lacks an acceptable patch_review",
                recommended_stage=legacy_engine.WorkflowStage.REVIEW.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=tuple(item.ref for item in reviews),
            )

    def _validate_patch_candidate_binding(self, patch: Artifact) -> None:
        resolution = self.blackboard.hypothesis_resolution
        based_on = patch.content.get("based_on_hypothesis_refs", ())
        if not isinstance(based_on, Sequence) or isinstance(based_on, (str, bytes)):
            based_on = ()
        actual_hypotheses = {str(item) for item in based_on}
        expected_hypotheses = set(resolution.accepted_refs)
        primary = str(patch.content.get("primary_hypothesis_ref", ""))
        if actual_hypotheses != expected_hypotheses or primary != resolution.primary_ref:
            raise DecisionPolicyViolation(
                "PATCH_HYPOTHESIS_BINDING_MISMATCH",
                "PatchCandidate must bind exactly to the accepted Hypothesis set and primary",
                recommended_stage=legacy_engine.WorkflowStage.PATCH.value,
                allowed_next_actions=("CREATE_TASK", "TERMINATE_TASK"),
                trigger_artifact_refs=(patch.ref,),
                details={
                    "expected_hypothesis_refs": sorted(expected_hypotheses),
                    "actual_hypothesis_refs": sorted(actual_hypotheses),
                    "expected_primary_ref": resolution.primary_ref,
                    "actual_primary_ref": primary,
                },
            )

    def _matching_patch_reviews(
        self,
        patch: Artifact,
        artifacts: Sequence[Artifact],
    ) -> tuple[Artifact, ...]:
        return tuple(
            item
            for item in artifacts
            if item.artifact_type is ArtifactType.REVIEW
            and item.content.get("mode") == "patch_review"
            and item.content.get("target_artifact_ref") in {patch.ref, patch.artifact_id}
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
            if (
                node.node_type
                not in {
                    NodeType.PATCH_TASK,
                    NodeType.VALIDATION_TASK,
                    NodeType.FINALIZATION_TASK,
                }
                or node.terminal
            ):
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
