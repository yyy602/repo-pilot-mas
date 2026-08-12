"""Deterministic orchestration engine for Supervisor decisions and task state."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from repo_pilot_mas.orchestration.phase5_policy import (
    classify_execution_path,
    required_replan_stage,
)
from repo_pilot_mas.orchestration.task_graph import (
    DependencyPolicy,
    NodeStatus,
    NodeType,
    TaskGraph,
    TaskNode,
)
from repo_pilot_mas.runtime.trace import TraceWriter
from repo_pilot_mas.schemas import TaskSpec
from repo_pilot_mas.schemas.artifact import Artifact, ArtifactType
from repo_pilot_mas.schemas.supervisor_decision import DecisionAction, SupervisorDecision
from repo_pilot_mas.schemas.tool_result import utc_now_iso
from repo_pilot_mas.schemas.worker_artifact import validate_worker_artifact
from repo_pilot_mas.state.blackboard import Blackboard


class WorkflowStage(str, Enum):
    INITIALIZATION = "initialization"
    INVESTIGATION = "investigation"
    DIAGNOSIS = "diagnosis"
    REVIEW = "review"
    PATCH = "patch"
    VALIDATION = "validation"
    REPLAN = "replan"
    FINALIZATION = "finalization"
    COMPLETED = "completed"
    TERMINATED = "terminated"


class EngineStatus(str, Enum):
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TERMINATED = "terminated"


_STAGE_TRANSITIONS = {
    WorkflowStage.INITIALIZATION: {WorkflowStage.INVESTIGATION, WorkflowStage.TERMINATED},
    WorkflowStage.INVESTIGATION: {WorkflowStage.DIAGNOSIS, WorkflowStage.TERMINATED},
    WorkflowStage.DIAGNOSIS: {
        WorkflowStage.INVESTIGATION,
        WorkflowStage.REVIEW,
        WorkflowStage.PATCH,
        WorkflowStage.TERMINATED,
    },
    WorkflowStage.REVIEW: {
        WorkflowStage.INVESTIGATION,
        WorkflowStage.DIAGNOSIS,
        WorkflowStage.PATCH,
        WorkflowStage.TERMINATED,
    },
    WorkflowStage.PATCH: {
        WorkflowStage.DIAGNOSIS,
        WorkflowStage.VALIDATION,
        WorkflowStage.TERMINATED,
    },
    WorkflowStage.VALIDATION: {
        WorkflowStage.INVESTIGATION,
        WorkflowStage.DIAGNOSIS,
        WorkflowStage.PATCH,
        WorkflowStage.FINALIZATION,
        WorkflowStage.TERMINATED,
    },
    WorkflowStage.REPLAN: {
        WorkflowStage.INVESTIGATION,
        WorkflowStage.DIAGNOSIS,
        WorkflowStage.PATCH,
        WorkflowStage.TERMINATED,
    },
    WorkflowStage.FINALIZATION: {WorkflowStage.COMPLETED, WorkflowStage.TERMINATED},
}

_STAGE_NODE_TYPES = {
    WorkflowStage.INITIALIZATION: {NodeType.INVESTIGATION_TASK},
    WorkflowStage.INVESTIGATION: {
        NodeType.INVESTIGATION_TASK,
        NodeType.DIAGNOSIS_TASK,
        NodeType.REVIEW_TASK,
    },
    WorkflowStage.DIAGNOSIS: {
        NodeType.INVESTIGATION_TASK,
        NodeType.DIAGNOSIS_TASK,
        NodeType.CHALLENGE_TASK,
        NodeType.REBUTTAL_TASK,
        NodeType.REVIEW_TASK,
        NodeType.PATCH_TASK,
    },
    WorkflowStage.REVIEW: {
        NodeType.INVESTIGATION_TASK,
        NodeType.DIAGNOSIS_TASK,
        NodeType.CHALLENGE_TASK,
        NodeType.REBUTTAL_TASK,
        NodeType.REVIEW_TASK,
        NodeType.PATCH_TASK,
        NodeType.VALIDATION_TASK,
    },
    WorkflowStage.PATCH: {
        NodeType.PATCH_TASK,
        NodeType.REVIEW_TASK,
        NodeType.VALIDATION_TASK,
    },
    WorkflowStage.VALIDATION: {
        NodeType.PATCH_TASK,
        NodeType.REVIEW_TASK,
        NodeType.VALIDATION_TASK,
        NodeType.REPLAN_TASK,
        NodeType.FINALIZATION_TASK,
    },
    WorkflowStage.FINALIZATION: {NodeType.FINALIZATION_TASK},
}


@dataclass(slots=True)
class EngineBudget:
    max_nodes: int = 64
    max_concurrent_nodes: int = 4
    max_supervisor_calls: int = 18
    max_tool_calls: int = 30
    max_input_tokens: int = 200_000
    max_output_tokens: int = 20_000
    max_runtime_seconds: float = 900.0
    max_replans: int = 1
    max_retries_per_node: int = 1
    max_no_progress_decisions: int = 2
    supervisor_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    replans: int = 0
    started_at: str = ""

    def __post_init__(self) -> None:
        limits = (
            self.max_nodes,
            self.max_concurrent_nodes,
            self.max_supervisor_calls,
            self.max_tool_calls,
            self.max_input_tokens,
            self.max_output_tokens,
            self.max_replans,
            self.max_retries_per_node,
            self.max_no_progress_decisions,
            self.supervisor_calls,
            self.tool_calls,
            self.input_tokens,
            self.output_tokens,
            self.replans,
        )
        if (
            any(value < 0 for value in limits)
            or not math.isfinite(self.max_runtime_seconds)
            or self.max_runtime_seconds <= 0
        ):
            raise ValueError("engine budgets must be non-negative and runtime must be positive")
        if self.max_concurrent_nodes == 0:
            raise ValueError("max_concurrent_nodes must be positive")
        if not self.started_at:
            self.started_at = utc_now_iso()

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def elapsed_seconds(self) -> float:
        started = datetime.fromisoformat(self.started_at)
        now = datetime.fromisoformat(utc_now_iso())
        return max((now - started).total_seconds(), 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EngineBudget:
        fields = cls.__dataclass_fields__
        return cls(**{name: value[name] for name in fields if name in value})


@dataclass(frozen=True, slots=True)
class DecisionResult:
    ok: bool
    code: str
    message: str
    decision_id: str | None
    state_version: int
    mutated_node_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "decision_id": self.decision_id,
            "state_version": self.state_version,
            "mutated_node_ids": list(self.mutated_node_ids),
        }


class OrchestrationEngine:
    def __init__(
        self,
        task: TaskSpec,
        *,
        budget: EngineBudget | None = None,
        graph: TaskGraph | None = None,
        blackboard: Blackboard | None = None,
        trace_writer: TraceWriter | None = None,
    ) -> None:
        self.task = task
        self.budget = budget or EngineBudget()
        self.graph = graph or TaskGraph()
        self.blackboard = blackboard or Blackboard(task.task_id, task.to_dict())
        if self.blackboard.task_id != task.task_id:
            raise ValueError("blackboard belongs to another task")
        self.trace_writer = trace_writer
        self.status = EngineStatus.ACTIVE
        self.termination_reason: str | None = None
        self.termination_code: str | None = None
        self.termination_stage: str | None = None
        self._node_sequence = _next_node_sequence(self.graph.nodes)
        self._processed_decision_ids: set[str] = set()
        self._decision_fingerprints: set[str] = set()
        self._rejected_decision_fingerprints: set[str] = set()
        self._no_progress_decisions = 0
        self._decision_count = 0
        self.last_supervisor_call: dict[str, Any] | None = None

    @property
    def state_version(self) -> int:
        return self.blackboard.state_version

    def snapshot(self) -> dict[str, Any]:
        return {
            "task": {
                "task_id": self.task.task_id,
                "issue": self.task.issue,
                "acceptance_criteria": list(self.task.acceptance_criteria),
                "protected_paths": list(self.task.protected_paths),
            },
            "engine_status": self.status.value,
            "termination": {
                "code": self.termination_code,
                "stage": self.termination_stage,
                "message": self.termination_reason,
            },
            "workflow_stage": self.blackboard.workflow_stage,
            "execution_path_class": classify_execution_path(self.graph.nodes).value,
            "state_version": self.state_version,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "node_type": node.node_type.value,
                    "status": node.status.value,
                    "agent_type": node.agent_type,
                    "mode": node.mode,
                    "objective": node.objective,
                    "dependencies": list(node.dependencies),
                    "input_artifact_ids": list(node.input_artifact_ids),
                    "output_artifact_ids": list(node.output_artifact_ids),
                    "dependency_policy": node.dependency_policy.value,
                    "critical": node.critical,
                    "retry_count": node.retry_count,
                    "failure_reason": node.failure_reason,
                }
                for node in self.graph.nodes
            ],
            "artifacts": self.blackboard.artifact_summaries(),
            "selections": {
                "hypothesis_ref": self.blackboard.selected_hypothesis_ref,
                "patch_ref": self.blackboard.selected_patch_ref,
                "validation_ref": self.blackboard.validation_ref,
            },
            "decision_history": {
                "processed_decision_ids": sorted(self._processed_decision_ids),
                "last_supervisor_call": (
                    dict(self.last_supervisor_call)
                    if self.last_supervisor_call is not None
                    else None
                ),
            },
            "recovery": self._replan_recovery_state(),
            "budget": self.budget.to_dict(),
        }

    def _replan_recovery_state(self) -> dict[str, Any]:
        nodes = list(self.graph.nodes)
        replan_indexes = [
            index
            for index, node in enumerate(nodes)
            if node.node_type is NodeType.REPLAN_TASK
        ]
        if not replan_indexes:
            return {
                "pending_replan": False,
                "remaining_replans": self.budget.max_replans - self.budget.replans,
            }
        replan_index = replan_indexes[-1]
        records = [
            artifact
            for artifact in self.blackboard.artifacts.latest_values()
            if artifact.artifact_type is ArtifactType.REPLAN_RECORD
        ]
        if not records:
            return {
                "pending_replan": False,
                "remaining_replans": self.budget.max_replans - self.budget.replans,
            }
        record = records[-1]
        target_stage = str(record.content.get("target_stage", ""))
        target_node_type = {
            WorkflowStage.INVESTIGATION.value: NodeType.INVESTIGATION_TASK,
            WorkflowStage.DIAGNOSIS.value: NodeType.DIAGNOSIS_TASK,
            WorkflowStage.PATCH.value: NodeType.PATCH_TASK,
        }.get(target_stage)
        recovery_started = target_node_type is not None and any(
            node.node_type is target_node_type for node in nodes[replan_index + 1 :]
        )
        return {
            "pending_replan": target_node_type is not None and not recovery_started,
            "target_stage": target_stage,
            "target_node_type": target_node_type.value if target_node_type else None,
            "replan_ref": record.ref,
            "trigger_refs": list(record.content.get("trigger_refs", ())),
            "remaining_replans": self.budget.max_replans - self.budget.replans,
            "semantics": (
                "remaining_replans only limits new REQUEST_REPLAN actions; "
                "an approved pending replan must still create its target recovery node"
            ),
        }

    def run_supervisor(self, supervisor: Any) -> DecisionResult:
        budget_error = self._supervisor_budget_error()
        if budget_error:
            return self._reject(None, budget_error, "supervisor budget is exhausted", terminate=True)
        started = time.perf_counter()
        try:
            outcome = supervisor.decide(self.snapshot())
        except RuntimeError as exc:
            self.budget.supervisor_calls += 1
            usage = getattr(exc, "usage", None)
            if usage is not None:
                self.budget.input_tokens += int(usage.input_tokens)
                self.budget.output_tokens += int(usage.output_tokens)
            code = str(getattr(exc, "code", "SUPERVISOR_ERROR"))
            self.last_supervisor_call = {
                "ok": False,
                "code": code,
                "input_tokens": int(usage.input_tokens) if usage is not None else 0,
                "output_tokens": int(usage.output_tokens) if usage is not None else 0,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
            self.blackboard.bump()
            self._trace(
                "supervisor_failed",
                {
                    "code": code,
                    "message": str(exc),
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                },
            )
            return self._reject(None, code, str(exc), terminate=True)

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

    def apply_decision(self, decision: SupervisorDecision) -> DecisionResult:
        if self.status is not EngineStatus.ACTIVE:
            return self._reject(decision.decision_id, "ENGINE_NOT_ACTIVE", "engine is not active")
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

    def start_node(self, node_id: str) -> None:
        if self.status is not EngineStatus.ACTIVE:
            raise RuntimeError("engine is not active")
        node = self.graph.get(node_id)
        if node.status is not NodeStatus.READY:
            raise ValueError(f"node is not ready: {node_id} ({node.status.value})")
        running = sum(node.status is NodeStatus.RUNNING for node in self.graph.nodes)
        if running >= self.budget.max_concurrent_nodes:
            raise RuntimeError("node concurrency budget is exhausted")
        self._graph_transition(node_id, NodeStatus.RUNNING)

    def finish_node(
        self,
        node_id: str,
        status: NodeStatus,
        *,
        artifact_refs: Sequence[str] = (),
        reason: str | None = None,
    ) -> None:
        if self.status is not EngineStatus.ACTIVE:
            raise RuntimeError("engine is not active")
        if status not in {
            NodeStatus.SUCCEEDED,
            NodeStatus.FAILED,
            NodeStatus.TIMED_OUT,
            NodeStatus.BLOCKED,
        }:
            raise ValueError("finish_node requires a worker terminal status")
        refs = tuple(artifact_refs)
        for ref in refs:
            self.blackboard.artifacts.get(ref)
        node = self.graph.get(node_id)
        before = self.graph.version
        changed = self.graph.transition(node_id, status, reason=reason)
        node.output_artifact_ids = refs
        self.blackboard.bump()
        self._bump_graph_delta(before)
        self._trace_node_changes(changed, "node_finished")

    def retry_node(self, node_id: str) -> None:
        if self.status is not EngineStatus.ACTIVE:
            raise RuntimeError("engine is not active")
        node = self.graph.get(node_id)
        if node.status not in {NodeStatus.FAILED, NodeStatus.TIMED_OUT}:
            raise ValueError("only failed or timed-out nodes can be retried")
        if node.retry_count >= self.budget.max_retries_per_node:
            raise ValueError("node retry budget is exhausted")
        node.retry_count += 1
        self.blackboard.bump()
        self._graph_transition(node_id, NodeStatus.PENDING)

    def add_artifact(self, artifact: Artifact) -> str:
        if self.status is not EngineStatus.ACTIVE:
            raise RuntimeError("engine is not active")
        if artifact.artifact_type is ArtifactType.PATCH_CANDIDATE and artifact.version > 2:
            raise ValueError("each PatchCandidate may be revised at most once")
        ref = self.blackboard.add_artifact(artifact)
        invalidated: list[str] = []
        if artifact.supersedes:
            for node in self.graph.nodes:
                if (
                    node.node_id == artifact.created_by
                    or artifact.supersedes not in node.input_artifact_ids
                    or node.terminal
                ):
                    continue
                before = self.graph.version
                descendants = self.graph.invalidate_descendants(
                    node.node_id,
                    reason=f"input artifact superseded by {ref}",
                )
                changed = self.graph.transition(
                    node.node_id,
                    NodeStatus.CANCELLED,
                    reason=f"input artifact superseded by {ref}",
                )
                self._bump_graph_delta(before)
                invalidated.extend((*changed, *descendants))
        self._trace("artifact_added", {"artifact": artifact.to_dict(), "state_version": self.state_version})
        if invalidated:
            self._trace_node_changes(invalidated, "node_invalidated")
        return ref

    def expire_timed_out_nodes(self, *, now: datetime | None = None) -> tuple[str, ...]:
        active_now = now or datetime.fromisoformat(utc_now_iso())
        expired: list[str] = []
        for node in self.graph.nodes:
            if node.status is not NodeStatus.RUNNING or not node.started_at:
                continue
            started = datetime.fromisoformat(node.started_at)
            if (active_now - started).total_seconds() > node.timeout_seconds:
                self.finish_node(node.node_id, NodeStatus.TIMED_OUT, reason="node timeout")
                expired.append(node.node_id)
        return tuple(expired)

    def record_tool_calls(self, count: int = 1) -> None:
        if self.status is not EngineStatus.ACTIVE:
            raise RuntimeError("engine is not active")
        if count < 0:
            raise ValueError("tool call count must be non-negative")
        self.budget.tool_calls += count
        if count:
            self.blackboard.bump()
        if self.budget.tool_calls > self.budget.max_tool_calls:
            self._terminate(EngineStatus.FAILED, "TOOL_CALL_BUDGET_EXHAUSTED")

    def to_task_state_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "budget": self.budget.to_dict(),
            "status": self.status.value,
            "termination_reason": self.termination_reason,
            "termination_code": self.termination_code,
            "termination_stage": self.termination_stage,
            "node_sequence": self._node_sequence,
            "processed_decision_ids": sorted(self._processed_decision_ids),
            "decision_fingerprints": sorted(self._decision_fingerprints),
            "rejected_decision_fingerprints": sorted(
                self._rejected_decision_fingerprints
            ),
            "no_progress_decisions": self._no_progress_decisions,
            "decision_count": self._decision_count,
            "last_supervisor_call": self.last_supervisor_call,
            "state_version": self.state_version,
        }

    @classmethod
    def restore(
        cls,
        task_state: Mapping[str, Any],
        graph_value: Mapping[str, Any],
        blackboard_value: Mapping[str, Any],
        *,
        trace_writer: TraceWriter | None = None,
    ) -> OrchestrationEngine:
        task = TaskSpec.from_dict(task_state["task"])
        engine = cls(
            task,
            budget=EngineBudget.from_dict(task_state["budget"]),
            graph=TaskGraph.from_dict(graph_value),
            blackboard=Blackboard.from_dict(blackboard_value),
            trace_writer=trace_writer,
        )
        if engine.blackboard.task_spec != task.to_dict():
            raise ValueError("checkpoint TaskSpec values are inconsistent")
        if int(task_state["state_version"]) != engine.state_version:
            raise ValueError("checkpoint state versions are inconsistent")
        engine.status = EngineStatus(str(task_state["status"]))
        engine.termination_reason = task_state.get("termination_reason")
        engine.termination_code = task_state.get("termination_code")
        engine.termination_stage = task_state.get("termination_stage")
        if engine.status is not EngineStatus.ACTIVE and not engine.termination_code:
            legacy_reason = str(engine.termination_reason or "")
            engine.termination_code = (
                legacy_reason
                if re.fullmatch(r"[A-Z][A-Z0-9_]*", legacy_reason)
                else "SUPERVISOR_TERMINATED"
            )
        if engine.status is not EngineStatus.ACTIVE and not engine.termination_stage:
            engine.termination_stage = engine.blackboard.workflow_stage
        saved_sequence = int(task_state.get("node_sequence", engine._node_sequence))
        if saved_sequence < _next_node_sequence(engine.graph.nodes):
            raise ValueError("checkpoint node sequence would reuse an existing node_id")
        engine._node_sequence = saved_sequence
        engine._processed_decision_ids = set(task_state.get("processed_decision_ids", ()))
        engine._decision_fingerprints = set(task_state.get("decision_fingerprints", ()))
        engine._rejected_decision_fingerprints = set(
            task_state.get("rejected_decision_fingerprints", ())
        )
        engine._no_progress_decisions = int(task_state.get("no_progress_decisions", 0))
        engine._decision_count = int(task_state.get("decision_count", 0))
        last_call = task_state.get("last_supervisor_call")
        engine.last_supervisor_call = dict(last_call) if isinstance(last_call, Mapping) else None
        return engine

    def _validate_decision(self, decision: SupervisorDecision) -> None:
        recovery = self._replan_recovery_state()
        if recovery.get("pending_replan"):
            target_node_type = str(recovery["target_node_type"])
            if decision.action is not DecisionAction.CREATE_TASK or any(
                request.node_type != target_node_type
                for request in decision.create_tasks
            ):
                raise ValueError(
                    "pending replan must create its target recovery node before any "
                    "new replan or termination"
                )
        for ref in decision.evidence_refs:
            self.blackboard.artifacts.get(ref)
        if decision.action is DecisionAction.CREATE_TASK:
            if len(self.graph.nodes) + len(decision.create_tasks) > self.budget.max_nodes:
                raise ValueError("node budget would be exceeded")
            current_stage = WorkflowStage(self.blackboard.workflow_stage)
            requested_types = [NodeType(request.node_type) for request in decision.create_tasks]
            for bounded_type in (NodeType.CHALLENGE_TASK, NodeType.REBUTTAL_TASK):
                if bounded_type in requested_types and any(
                    node.node_type is bounded_type for node in self.graph.nodes
                ):
                    raise ValueError(f"MVP permits only one {bounded_type.value} round")
            if (
                _requires_gate_record(self.graph.nodes, decision, current_stage)
                and decision.gate_record is None
            ):
                raise ValueError("dynamic graph expansion requires gate_record")
            if decision.gate_record is not None:
                gate = decision.gate_record
                if gate.added_node_count != len(decision.create_tasks):
                    raise ValueError("gate added_node_count does not match create_tasks")
                if not set(gate.trigger_artifact_refs).issubset(set(decision.evidence_refs)):
                    raise ValueError("gate triggers must also be included in decision evidence_refs")
                for ref in gate.trigger_artifact_refs:
                    self.blackboard.artifacts.get(ref)
            allowed_node_types = _STAGE_NODE_TYPES.get(current_stage, set())
            fingerprints: set[tuple[Any, ...]] = set()
            for request in decision.create_tasks:
                node_type = NodeType(request.node_type)
                if node_type not in allowed_node_types:
                    raise ValueError(
                        f"{node_type.value} is not valid during {current_stage.value}"
                    )
                for dependency in request.depends_on:
                    self.graph.get(dependency)
                for ref in request.input_artifact_ids:
                    self.blackboard.artifacts.get(ref)
                fingerprint = (
                    request.node_type,
                    request.agent_type,
                    request.mode,
                    request.objective,
                    request.input_artifact_ids,
                )
                if fingerprint in fingerprints:
                    raise ValueError("decision creates duplicate tasks")
                fingerprints.add(fingerprint)
                candidate = self._task_node(request, node_id="candidate", decision_id=decision.decision_id)
                duplicate = self.graph.semantic_duplicate(candidate)
                if duplicate:
                    raise ValueError(f"equivalent active task already exists: {duplicate}")
            if (
                decision.next_workflow_stage
                and decision.next_workflow_stage != current_stage.value
            ):
                self._validate_stage(decision.next_workflow_stage)
        elif decision.action in {
            DecisionAction.CANCEL_TASK,
            DecisionAction.PAUSE_TASK,
            DecisionAction.RESUME_TASK,
        }:
            for node_id in decision.target_task_ids:
                node = self.graph.get(node_id)
                if decision.action is DecisionAction.CANCEL_TASK and node.terminal:
                    raise ValueError(f"cannot cancel terminal node: {node_id}")
                if decision.action is DecisionAction.PAUSE_TASK and node.status not in {
                    NodeStatus.PENDING,
                    NodeStatus.READY,
                    NodeStatus.RUNNING,
                }:
                    raise ValueError(f"cannot pause node in state {node.status.value}")
                if decision.action is DecisionAction.RESUME_TASK and node.status is not NodeStatus.PAUSED:
                    raise ValueError(f"cannot resume node in state {node.status.value}")
        elif decision.action is DecisionAction.CHANGE_WORKFLOW_STAGE:
            self._validate_stage(decision.next_workflow_stage)
        elif decision.action is DecisionAction.REQUEST_REPLAN:
            if self.budget.replans >= self.budget.max_replans:
                raise ValueError("replan budget is exhausted")
            target = WorkflowStage(str(decision.next_workflow_stage))
            if target not in {
                WorkflowStage.INVESTIGATION,
                WorkflowStage.DIAGNOSIS,
                WorkflowStage.PATCH,
            }:
                raise ValueError("replan must target investigation, diagnosis, or patch")
            if not decision.evidence_refs and self.blackboard.artifacts.latest_values():
                raise ValueError("replan requires triggering Artifact refs")
            required = required_replan_stage(str(decision.failure_class))
            if target.value != required:
                raise ValueError(
                    f"failure class {decision.failure_class} requires replan to {required}"
                )
        elif decision.action is DecisionAction.ACCEPT_HYPOTHESIS:
            current = WorkflowStage(self.blackboard.workflow_stage)
            if current not in {WorkflowStage.DIAGNOSIS, WorkflowStage.REVIEW}:
                raise ValueError("ACCEPT_HYPOTHESIS requires diagnosis or review stage")
            artifact = self.blackboard.artifacts.get(str(decision.hypothesis_ref))
            if artifact.artifact_type is not ArtifactType.HYPOTHESIS:
                raise ValueError("hypothesis_ref is not a hypothesis")
            adversarial_types = {
                item.artifact_type for item in self.blackboard.artifacts.latest_values()
            }
            if {ArtifactType.CHALLENGE, ArtifactType.REBUTTAL}.issubset(adversarial_types):
                cited_types = {
                    self.blackboard.artifacts.get(ref).artifact_type
                    for ref in decision.evidence_refs
                }
                required_types = {
                    ArtifactType.EVIDENCE,
                    ArtifactType.CHALLENGE,
                    ArtifactType.REBUTTAL,
                    ArtifactType.REVIEW,
                }
                if not required_types.issubset(cited_types):
                    raise ValueError(
                        "adversarial root-cause decision must cite Evidence, Challenge, "
                        "Rebuttal, and Review"
                    )
        elif decision.action in {DecisionAction.SELECT_PATCH, DecisionAction.FINALIZE_TASK}:
            self._validate_patch_selection(str(decision.patch_ref), str(decision.validation_ref))
            current = WorkflowStage(self.blackboard.workflow_stage)
            if decision.action is DecisionAction.SELECT_PATCH and current is not WorkflowStage.VALIDATION:
                raise ValueError("SELECT_PATCH requires validation stage")
            if decision.action is DecisionAction.FINALIZE_TASK and current not in {
                WorkflowStage.VALIDATION,
                WorkflowStage.FINALIZATION,
            }:
                raise ValueError("FINALIZE_TASK requires validation or finalization stage")

    def _execute_decision(self, decision: SupervisorDecision) -> tuple[str, ...]:
        mutated: list[str] = []
        if decision.action is DecisionAction.CREATE_TASK:
            before = self.graph.version
            for request in decision.create_tasks:
                node_id = self._next_node_id()
                node = self._task_node(request, node_id=node_id, decision_id=decision.decision_id)
                self.graph.add_node(node)
                mutated.append(node_id)
            self._bump_graph_delta(before)
            if (
                decision.next_workflow_stage
                and decision.next_workflow_stage != self.blackboard.workflow_stage
            ):
                self._change_stage(str(decision.next_workflow_stage))
        elif decision.action is DecisionAction.CANCEL_TASK:
            for node_id in decision.target_task_ids:
                before = self.graph.version
                descendants = self.graph.invalidate_descendants(
                    node_id,
                    reason=f"invalidated by cancellation of {node_id}",
                )
                changed = self.graph.transition(node_id, NodeStatus.CANCELLED, reason=decision.reason)
                self._bump_graph_delta(before)
                mutated.extend((*changed, *descendants))
        elif decision.action is DecisionAction.PAUSE_TASK:
            for node_id in decision.target_task_ids:
                mutated.extend(self._graph_transition(node_id, NodeStatus.PAUSED))
        elif decision.action is DecisionAction.RESUME_TASK:
            for node_id in decision.target_task_ids:
                mutated.extend(self._graph_transition(node_id, NodeStatus.PENDING))
        elif decision.action is DecisionAction.CHANGE_WORKFLOW_STAGE:
            self._change_stage(str(decision.next_workflow_stage))
        elif decision.action is DecisionAction.REQUEST_REPLAN:
            from_stage = self.blackboard.workflow_stage
            self.budget.replans += 1
            self.blackboard.bump()
            record_ref: str | None = None
            if decision.evidence_refs:
                record = Artifact(
                    artifact_id=f"replan.{self.budget.replans}",
                    artifact_type=ArtifactType.REPLAN_RECORD,
                    created_by="OrchestrationEngine",
                    content={
                        "decision_id": decision.decision_id,
                        "attempt": self.budget.replans,
                        "from_stage": from_stage,
                        "target_stage": str(decision.next_workflow_stage),
                        "failure_class": str(decision.failure_class),
                        "trigger_refs": list(decision.evidence_refs),
                        "reason": decision.reason,
                        "remaining_replans": self.budget.max_replans - self.budget.replans,
                    },
                    input_refs=decision.evidence_refs,
                )
                validate_worker_artifact(
                    record,
                    expected_type=ArtifactType.REPLAN_RECORD,
                    allowed_input_refs=decision.evidence_refs,
                )
                record_ref = self.add_artifact(record)
            before = self.graph.version
            node_id = self._next_node_id()
            self.graph.add_node(
                TaskNode(
                    node_id=node_id,
                    node_type=NodeType.REPLAN_TASK,
                    agent_type="OrchestrationEngine",
                    mode="targeted_replan",
                    objective=decision.reason,
                    input_artifact_ids=decision.evidence_refs,
                    output_artifact_ids=((record_ref,) if record_ref else ()),
                    status=NodeStatus.SUCCEEDED,
                    created_by_decision_id=decision.decision_id,
                    finished_at=utc_now_iso(),
                )
            )
            self._bump_graph_delta(before)
            mutated.append(node_id)
            self.blackboard.set_stage(WorkflowStage.REPLAN.value)
            self._change_stage(str(decision.next_workflow_stage), from_replan=True)
        elif decision.action is DecisionAction.ACCEPT_HYPOTHESIS:
            self.blackboard.set_hypothesis(str(decision.hypothesis_ref))
        elif decision.action is DecisionAction.SELECT_PATCH:
            self.blackboard.set_patch(str(decision.patch_ref), str(decision.validation_ref))
        elif decision.action is DecisionAction.FINALIZE_TASK:
            self.blackboard.set_patch(str(decision.patch_ref), str(decision.validation_ref))
            if self.blackboard.workflow_stage != WorkflowStage.FINALIZATION.value:
                self._change_stage(WorkflowStage.FINALIZATION.value)
            self._terminate(
                EngineStatus.SUCCEEDED,
                "VALIDATION_PASSED",
                code="VALIDATION_PASSED",
            )
        elif decision.action is DecisionAction.TERMINATE_TASK:
            self._terminate(
                EngineStatus.TERMINATED,
                decision.reason,
                code="SUPERVISOR_TERMINATED",
            )
        return tuple(dict.fromkeys(mutated))

    def _task_node(self, request: Any, *, node_id: str, decision_id: str) -> TaskNode:
        normalized_inputs = tuple(
            self.blackboard.artifacts.get(ref).ref for ref in request.input_artifact_ids
        )
        return TaskNode(
            node_id=node_id,
            node_type=NodeType(request.node_type),
            agent_type=request.agent_type,
            mode=request.mode,
            objective=request.objective,
            dependencies=request.depends_on,
            input_artifact_ids=normalized_inputs,
            dependency_policy=DependencyPolicy(request.dependency_policy),
            critical=request.critical,
            timeout_seconds=request.timeout_seconds,
            created_by_decision_id=decision_id,
        )

    def _next_node_id(self) -> str:
        node_id = f"N{self._node_sequence}"
        self._node_sequence += 1
        return node_id

    def _validate_stage(self, target_value: str | None) -> None:
        if target_value is None:
            raise ValueError("workflow stage is required")
        current = WorkflowStage(self.blackboard.workflow_stage)
        target = WorkflowStage(target_value)
        if target not in _STAGE_TRANSITIONS.get(current, set()):
            raise ValueError(f"illegal workflow transition: {current.value} -> {target.value}")

    def _change_stage(self, target_value: str, *, from_replan: bool = False) -> None:
        if not from_replan:
            self._validate_stage(target_value)
        else:
            current = WorkflowStage(self.blackboard.workflow_stage)
            target = WorkflowStage(target_value)
            if current is not WorkflowStage.REPLAN or target not in _STAGE_TRANSITIONS[current]:
                raise ValueError(f"illegal replan transition: {current.value} -> {target.value}")
        self.blackboard.set_stage(target_value)

    def _validate_patch_selection(self, patch_ref: str, validation_ref: str) -> None:
        patch = self.blackboard.artifacts.get(patch_ref)
        validation = self.blackboard.artifacts.get(validation_ref)
        if patch.artifact_type is not ArtifactType.PATCH_CANDIDATE:
            raise ValueError("patch_ref is not a patch candidate")
        if validation.artifact_type is not ArtifactType.VALIDATION_RESULT:
            raise ValueError("validation_ref is not a validation result")
        if not validation.content.get("passed"):
            raise ValueError("patch validation did not pass")
        if validation.content.get("patch_ref") not in {patch.ref, patch.artifact_id}:
            raise ValueError("validation result belongs to another patch")
        if not patch.content.get("diff") or not patch.content.get("diff_sha256"):
            raise ValueError("patch candidate has no concrete diff and hash")
        if not patch.content.get("changed_files") or not patch.content.get("workspace_id"):
            raise ValueError("patch candidate has no changed files or workspace")
        if patch.content.get("protected_path_check") is not True:
            raise ValueError("patch candidate did not pass protected path checks")
        if validation.content.get("applied") is not True:
            raise ValueError("patch was not actually applied during validation")
        if validation.content.get("patch_sha256") != patch.content.get("diff_sha256"):
            raise ValueError("validation result patch hash is inconsistent")
        if validation.content.get("protected_path_check") is not True:
            raise ValueError("validation did not pass protected path checks")
        for label in ("target_test", "regression_test", "static_check"):
            result = validation.content.get(label)
            if not isinstance(result, Mapping) or result.get("exit_code") != 0:
                raise ValueError(f"validation lacks a successful {label} result")
            if not result.get("trace_id"):
                raise ValueError(f"validation {label} has no trace_id")
        for review in self.blackboard.artifacts.latest_values():
            if (
                review.artifact_type is ArtifactType.REVIEW
                and review.content.get("mode") == "patch_review"
                and review.content.get("target_artifact_ref") in {patch.ref, patch.artifact_id}
                and review.content.get("verdict") in {"unsupported", "changes_requested"}
            ):
                raise ValueError(f"selected patch has unresolved blocking review: {review.ref}")
            if (
                review.artifact_type is ArtifactType.REVIEW
                and review.content.get("severity") == "blocking"
                and review.status not in {"resolved", "rejected"}
            ):
                raise ValueError(f"blocking review is unresolved: {review.ref}")
        active_critical = [
            node.node_id for node in self.graph.nodes if node.critical and not node.terminal
        ]
        if active_critical:
            raise ValueError(f"critical task nodes are still active: {active_critical}")

    def _graph_transition(self, node_id: str, status: NodeStatus) -> tuple[str, ...]:
        before = self.graph.version
        changed = self.graph.transition(node_id, status)
        self._bump_graph_delta(before)
        self._trace_node_changes(changed, "node_state_changed")
        return changed

    def _bump_graph_delta(self, previous_version: int) -> None:
        for _ in range(self.graph.version - previous_version):
            self.blackboard.bump()

    def _trace_node_changes(self, node_ids: Sequence[str], event_type: str) -> None:
        for node_id in dict.fromkeys(node_ids):
            self._trace(
                event_type,
                {
                    "node": self.graph.get(node_id).to_dict(),
                    "state_version": self.state_version,
                },
            )

    def _supervisor_budget_error(self) -> str | None:
        if self.status is not EngineStatus.ACTIVE:
            return "ENGINE_NOT_ACTIVE"
        if self.budget.supervisor_calls >= self.budget.max_supervisor_calls:
            return "SUPERVISOR_CALL_BUDGET_EXHAUSTED"
        if self.budget.elapsed_seconds() > self.budget.max_runtime_seconds:
            return "RUNTIME_BUDGET_EXHAUSTED"
        return self._token_budget_error()

    def _token_budget_error(self) -> str | None:
        if self.budget.input_tokens > self.budget.max_input_tokens:
            return "INPUT_TOKEN_BUDGET_EXHAUSTED"
        if self.budget.output_tokens > self.budget.max_output_tokens:
            return "OUTPUT_TOKEN_BUDGET_EXHAUSTED"
        return None

    def _terminate(
        self,
        status: EngineStatus,
        reason: str,
        *,
        code: str | None = None,
    ) -> None:
        source_stage = self.blackboard.workflow_stage
        self.status = status
        self.termination_reason = reason
        self.termination_code = code or reason
        self.termination_stage = source_stage
        target_stage = WorkflowStage.COMPLETED if status is EngineStatus.SUCCEEDED else WorkflowStage.TERMINATED
        self.blackboard.set_stage(target_stage.value)
        self._trace(
            "engine_terminated",
            {
                "status": status.value,
                "code": self.termination_code,
                "stage": source_stage,
                "reason": reason,
                "state_version": self.state_version,
            },
        )

    def _reject(
        self,
        decision_id: str | None,
        code: str,
        message: str,
        *,
        terminate: bool = False,
    ) -> DecisionResult:
        if terminate and self.status is EngineStatus.ACTIVE:
            self._terminate(EngineStatus.FAILED, code)
        result = DecisionResult(False, code, message, decision_id, self.state_version)
        if self.last_supervisor_call is not None:
            self.last_supervisor_call["decision_result"] = result.to_dict()
        self._trace(
            "supervisor_decision_rejected",
            {
                "decision_id": decision_id,
                "code": code,
                "message": message,
                "state_version": self.state_version,
            },
        )
        return result

    def _trace(self, event_type: str, data: Mapping[str, Any]) -> None:
        if self.trace_writer is not None:
            self.trace_writer.write(event_type, data)


def _next_node_sequence(nodes: Sequence[TaskNode]) -> int:
    numbers = [int(node.node_id[1:]) for node in nodes if node.node_id[1:].isdigit()]
    return max(numbers, default=0) + 1


def _requires_gate_record(
    nodes: Sequence[TaskNode],
    decision: SupervisorDecision,
    current_stage: WorkflowStage,
) -> bool:
    if current_stage is WorkflowStage.INITIALIZATION:
        return False
    new_types = [NodeType(request.node_type) for request in decision.create_tasks]
    if any(item in {NodeType.CHALLENGE_TASK, NodeType.REBUTTAL_TASK} for item in new_types):
        return True
    expandable = {
        NodeType.INVESTIGATION_TASK,
        NodeType.DIAGNOSIS_TASK,
        NodeType.PATCH_TASK,
    }
    for node_type in expandable:
        existing = sum(
            node.node_type is node_type
            and node.status
            not in {
                NodeStatus.FAILED,
                NodeStatus.TIMED_OUT,
                NodeStatus.BLOCKED,
                NodeStatus.CANCELLED,
            }
            for node in nodes
        )
        added = new_types.count(node_type)
        if added and existing + added > 1:
            return True
    return False
