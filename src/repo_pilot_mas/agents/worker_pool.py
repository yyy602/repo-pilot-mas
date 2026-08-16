"""Worker pool with GPU-slot locking, attempt leases, and isolated workspaces."""

from __future__ import annotations

import asyncio
import hashlib
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from repo_pilot_mas.agents.diagnostician import DiagnosticianAgent
from repo_pilot_mas.agents.investigator import InvestigatorAgent
from repo_pilot_mas.agents.patch_agent import PatchAgent
from repo_pilot_mas.agents.reviewer import ReviewerAgent
from repo_pilot_mas.agents.worker_common import WorkerAgentError
from repo_pilot_mas.models import GenerationConfig, ModelAdapter
from repo_pilot_mas.models.base import ModelAdapterError
from repo_pilot_mas.orchestration.langgraph_runtime import WorkerOutcome
from repo_pilot_mas.orchestration.react_loop import ReactBudget
from repo_pilot_mas.orchestration.task_graph import NodeStatus, NodeType, TaskNode
from repo_pilot_mas.orchestration.validation import ValidationExecutor
from repo_pilot_mas.runtime import TraceWriter, Workspace, WorkspaceManager
from repo_pilot_mas.schemas import (
    Artifact,
    ArtifactType,
    TaskSpec,
    validate_worker_artifact,
)
from repo_pilot_mas.schemas.tool_result import utc_now_iso
from repo_pilot_mas.tools import build_workspace_tool_registry

_EXPECTED_AGENTS = {
    NodeType.INVESTIGATION_TASK: "InvestigatorAgent",
    NodeType.DIAGNOSIS_TASK: "DiagnosticianAgent",
    NodeType.CHALLENGE_TASK: "DiagnosticianAgent",
    NodeType.REBUTTAL_TASK: "DiagnosticianAgent",
    NodeType.REVIEW_TASK: "ReviewerAgent",
    NodeType.PATCH_TASK: "PatchAgent",
    NodeType.VALIDATION_TASK: "ValidationExecutor",
}
_EXPECTED_ARTIFACTS = {
    NodeType.INVESTIGATION_TASK: ArtifactType.EVIDENCE,
    NodeType.DIAGNOSIS_TASK: ArtifactType.HYPOTHESIS,
    NodeType.CHALLENGE_TASK: ArtifactType.CHALLENGE,
    NodeType.REBUTTAL_TASK: ArtifactType.REBUTTAL,
    NodeType.REVIEW_TASK: ArtifactType.REVIEW,
    NodeType.PATCH_TASK: ArtifactType.PATCH_CANDIDATE,
    NodeType.VALIDATION_TASK: ArtifactType.VALIDATION_RESULT,
}
_TRAILING_NUMBER = re.compile(r"(\d+)$")


class WorkerAttemptRevokedError(RuntimeError):
    """Raised when a timed-out or superseded Worker attempts more side effects."""


class WorkerPool:
    """Execute one TaskNode in a fresh agent context and return only Artifact data."""

    def __init__(
        self,
        models: Sequence[ModelAdapter],
        *,
        workspace_root: str | Path,
        trace_writer: TraceWriter | None = None,
        react_budget: ReactBudget | None = None,
        generation_config: GenerationConfig | None = None,
    ) -> None:
        if not models:
            raise ValueError("WorkerPool requires at least one model slot")
        self.models = tuple(models)
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.trace_writer = trace_writer
        self.react_budget = react_budget
        self.generation_config = generation_config
        self._model_locks = tuple(threading.Lock() for _ in self.models)
        self._attempt_lock = threading.Lock()
        self._attempt_leases: dict[str, threading.Event] = {}

    def prepare(self) -> None:
        """Load model slots sequentially before the first concurrent dispatch."""

        for slot, model in enumerate(self.models):
            prepare = getattr(model, "prepare", None)
            if prepare is None:
                continue
            self._trace(
                "worker_model_slot_preparing",
                {
                    "slot": slot,
                    "model_id": model.model_id,
                    "device": getattr(model, "device", None),
                },
            )
            prepare()
            self._trace(
                "worker_model_slot_prepared",
                {
                    "slot": slot,
                    "model_id": model.model_id,
                    "device": getattr(model, "device", None),
                },
            )

    def execute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        task_spec = TaskSpec.from_dict(task)
        task_node = TaskNode.from_dict(node)
        attempt = _attempt_number(node, task_node)
        attempt_id = str(
            node.get("attempt_id") or _attempt_id(task_spec.task_id, task_node.node_id, attempt)
        )
        input_artifacts = tuple(Artifact.from_dict(item) for item in artifacts)
        expected_agent = _expected_agent(task_node)
        if expected_agent is None:
            return WorkerOutcome(
                task_node.node_id,
                NodeStatus.FAILED,
                reason=f"UNSUPPORTED_WORKER_NODE:{task_node.node_type.value}",
            )
        if task_node.agent_type != expected_agent:
            return WorkerOutcome(
                task_node.node_id,
                NodeStatus.FAILED,
                reason=f"AGENT_TYPE_MISMATCH:expected={expected_agent}",
            )

        lease = self._register_attempt(attempt_id)
        attempt_guard = lambda: self._assert_attempt_active(attempt_id)
        manager: WorkspaceManager | None = None
        workspace: Workspace | None = None
        retain_workspace = False
        self._trace(
            "worker_attempt_started",
            {
                "task_id": task_spec.task_id,
                "node_id": task_node.node_id,
                "attempt": attempt,
                "attempt_id": attempt_id,
            },
        )
        try:
            attempt_guard()
            if task_node.node_type in {
                NodeType.INVESTIGATION_TASK,
                NodeType.PATCH_TASK,
            }:
                manager = WorkspaceManager(
                    task_spec.repository_path,
                    self.workspace_root,
                )
                workspace = manager.create(
                    task_spec.task_id,
                    _workspace_id(
                        task_node.node_id,
                        task_node.created_at,
                        attempt,
                    ),
                )
                self._trace(
                    "worker_workspace_created",
                    {
                        "node_id": task_node.node_id,
                        "attempt": attempt,
                        "attempt_id": attempt_id,
                        "workspace_id": workspace.workspace_id,
                        "workspace_root": str(workspace.root),
                        "baseline_digest": workspace.baseline_digest,
                    },
                )
            if task_node.node_type is NodeType.VALIDATION_TASK:
                attempt_guard()
                outputs = (
                    ValidationExecutor(
                        str(self.workspace_root),
                        trace_writer=self.trace_writer,
                    ).run(task_spec, task_node.node_id, input_artifacts),
                )
            else:
                slot = self._slot_for(task_node.node_id)
                with self._model_locks[slot]:
                    attempt_guard()
                    model_started = time.perf_counter()
                    self._trace(
                        "worker_model_slot_acquired",
                        {
                            "node_id": task_node.node_id,
                            "attempt": attempt,
                            "attempt_id": attempt_id,
                            "slot": slot,
                            "model_id": self.models[slot].model_id,
                            "device": getattr(self.models[slot], "device", None),
                            "started_at": utc_now_iso(),
                        },
                    )
                    try:
                        outputs = self._run_agent(
                            self.models[slot],
                            task_spec,
                            task_node,
                            input_artifacts,
                            workspace,
                            attempt_guard,
                        )
                    finally:
                        self._trace(
                            "worker_model_slot_released",
                            {
                                "node_id": task_node.node_id,
                                "attempt": attempt,
                                "attempt_id": attempt_id,
                                "slot": slot,
                                "model_id": self.models[slot].model_id,
                                "device": getattr(
                                    self.models[slot],
                                    "device",
                                    None,
                                ),
                                "finished_at": utc_now_iso(),
                                "duration_ms": int((time.perf_counter() - model_started) * 1000),
                            },
                        )
            attempt_guard()
            expected_type = _EXPECTED_ARTIFACTS[task_node.node_type]
            available_refs = [item.ref for item in input_artifacts]
            if task_node.node_type is NodeType.REBUTTAL_TASK:
                types = [item.artifact_type for item in outputs]
                if types.count(ArtifactType.REBUTTAL) != 1 or any(
                    item
                    not in {
                        ArtifactType.HYPOTHESIS,
                        ArtifactType.REBUTTAL,
                    }
                    for item in types
                ):
                    raise ValueError("RebuttalTask 必须返回 Rebuttal 和至多一个修订 Hypothesis")
            elif len(outputs) != 1 or outputs[0].artifact_type is not expected_type:
                raise ValueError(f"Worker 必须返回一个 {expected_type.value} Artifact")
            for artifact in outputs:
                validate_worker_artifact(
                    artifact,
                    expected_type=(
                        None if task_node.node_type is NodeType.REBUTTAL_TASK else expected_type
                    ),
                    allowed_input_refs=tuple(available_refs),
                )
                if artifact.created_by != task_node.node_id:
                    raise ValueError("Worker Artifact created_by 与节点不一致")
                available_refs.append(artifact.ref)
            attempt_guard()
            retain_workspace = task_node.node_type is NodeType.PATCH_TASK
            self._trace(
                "worker_attempt_completed",
                {
                    "task_id": task_spec.task_id,
                    "node_id": task_node.node_id,
                    "attempt": attempt,
                    "attempt_id": attempt_id,
                    "status": NodeStatus.SUCCEEDED.value,
                    "workspace_id": workspace.workspace_id if workspace else None,
                },
            )
            return WorkerOutcome(
                task_node.node_id,
                NodeStatus.SUCCEEDED,
                outputs,
            )
        except Exception as exc:  # noqa: BLE001 - Worker isolation boundary
            revoked = isinstance(exc, WorkerAttemptRevokedError) or lease.is_set()
            code = "MODEL_TIMEOUT" if revoked else _worker_failure_code(exc)
            self._trace(
                "worker_agent_failed",
                {
                    "node_id": task_node.node_id,
                    "attempt": attempt,
                    "attempt_id": attempt_id,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "workspace_id": workspace.workspace_id if workspace else None,
                    "attempt_revoked": revoked,
                },
            )
            return WorkerOutcome(
                task_node.node_id,
                NodeStatus.FAILED,
                reason=f"{code}:{type(exc).__name__}:{exc}",
            )
        finally:
            revoked = lease.is_set()
            if manager is not None and workspace is not None and (not retain_workspace or revoked):
                deleted = manager.discard(
                    task_spec.task_id,
                    workspace.workspace_id,
                )
                self._trace(
                    "worker_workspace_deleted",
                    {
                        "node_id": task_node.node_id,
                        "attempt": attempt,
                        "attempt_id": attempt_id,
                        "workspace_id": workspace.workspace_id,
                        "deleted": deleted,
                        "reason": (
                            "worker_attempt_revoked"
                            if revoked
                            else "worker_not_returning_accepted_patch_candidate"
                        ),
                    },
                )
            self._release_attempt(attempt_id, lease)

    async def aexecute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        return await asyncio.to_thread(
            self.execute,
            task=task,
            node=node,
            artifacts=artifacts,
        )

    def revoke_attempt(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        attempt_id: str | None = None,
        reason: str = "runtime_timeout",
    ) -> bool:
        """Revoke one attempt so later tool calls and Artifact publication fail closed."""

        task_spec = TaskSpec.from_dict(task)
        task_node = TaskNode.from_dict(node)
        attempt = _attempt_number(node, task_node)
        identity = attempt_id or str(
            node.get("attempt_id") or _attempt_id(task_spec.task_id, task_node.node_id, attempt)
        )
        with self._attempt_lock:
            lease = self._attempt_leases.setdefault(identity, threading.Event())
            already_revoked = lease.is_set()
            lease.set()
        self._trace(
            "worker_attempt_revoked",
            {
                "task_id": task_spec.task_id,
                "node_id": task_node.node_id,
                "attempt": attempt,
                "attempt_id": identity,
                "reason": reason,
                "already_revoked": already_revoked,
            },
        )
        return not already_revoked

    def discard_workspace(
        self,
        *,
        task: Mapping[str, Any],
        workspace_id: str,
        node_id: str | None = None,
        reason: str = "collector_rejected_patch_candidate",
    ) -> bool:
        """Idempotently discard a Patch workspace rejected after Worker return."""

        task_spec = TaskSpec.from_dict(task)
        manager = WorkspaceManager(task_spec.repository_path, self.workspace_root)
        deleted = manager.discard(task_spec.task_id, workspace_id)
        self._trace(
            "worker_workspace_discarded",
            {
                "task_id": task_spec.task_id,
                "node_id": node_id,
                "workspace_id": workspace_id,
                "deleted": deleted,
                "reason": reason,
            },
        )
        return deleted

    def discard_task_workspaces(
        self,
        *,
        task: Mapping[str, Any],
        reason: str = "task_terminal_cleanup",
    ) -> tuple[str, ...]:
        """Revoke active attempts and release every retained workspace for one task."""

        task_spec = TaskSpec.from_dict(task)
        prefix = f"{task_spec.task_id}:"
        with self._attempt_lock:
            for attempt_id, lease in self._attempt_leases.items():
                if attempt_id.startswith(prefix):
                    lease.set()
        manager = WorkspaceManager(task_spec.repository_path, self.workspace_root)
        deleted = manager.discard_task(task_spec.task_id)
        self._trace(
            "worker_task_workspaces_discarded",
            {
                "task_id": task_spec.task_id,
                "workspace_ids": list(deleted),
                "reason": reason,
            },
        )
        return deleted

    def _run_agent(
        self,
        model: ModelAdapter,
        task: TaskSpec,
        node: TaskNode,
        artifacts: Sequence[Artifact],
        workspace: Workspace | None,
        attempt_guard: Callable[[], None],
    ) -> tuple[Artifact, ...]:
        attempt_guard()
        if node.node_type is NodeType.INVESTIGATION_TASK:
            assert workspace is not None
            tools = build_workspace_tool_registry(task, workspace).guarded(attempt_guard)
            agent = InvestigatorAgent(
                model,
                tools,
                trace_writer=self.trace_writer,
                budget=self.react_budget,
                generation_config=self.generation_config,
            )
            return (
                agent.run(
                    task,
                    node.node_id,
                    node.mode,
                    node.objective,
                    artifacts,
                ),
            )
        if node.node_type is NodeType.DIAGNOSIS_TASK:
            return (
                DiagnosticianAgent(
                    model,
                    trace_writer=self.trace_writer,
                    generation_config=self.generation_config,
                ).run(
                    task,
                    node.node_id,
                    node.mode,
                    node.objective,
                    artifacts,
                ),
            )
        if node.node_type is NodeType.CHALLENGE_TASK:
            return (
                DiagnosticianAgent(
                    model,
                    trace_writer=self.trace_writer,
                    generation_config=self.generation_config,
                ).challenge(
                    task,
                    node.node_id,
                    node.objective,
                    artifacts,
                ),
            )
        if node.node_type is NodeType.REBUTTAL_TASK:
            return DiagnosticianAgent(
                model,
                trace_writer=self.trace_writer,
                generation_config=self.generation_config,
            ).rebuttal(
                task,
                node.node_id,
                node.objective,
                artifacts,
            )
        if node.node_type is NodeType.REVIEW_TASK:
            if node.agent_type == "PatchAgent":
                return (
                    PatchAgent.review_peer(
                        model,
                        task,
                        node.node_id,
                        node.mode,
                        node.objective,
                        artifacts,
                        trace_writer=self.trace_writer,
                        generation_config=self.generation_config,
                    ),
                )
            return (
                ReviewerAgent(
                    model,
                    trace_writer=self.trace_writer,
                    generation_config=self.generation_config,
                ).run(
                    task,
                    node.node_id,
                    node.mode,
                    node.objective,
                    artifacts,
                ),
            )
        assert node.node_type is NodeType.PATCH_TASK and workspace is not None
        tools = build_workspace_tool_registry(task, workspace).guarded(attempt_guard)
        return (
            PatchAgent(
                model,
                tools,
                workspace,
                trace_writer=self.trace_writer,
                budget=self.react_budget,
                generation_config=self.generation_config,
            ).run(
                task,
                node.node_id,
                node.mode,
                node.objective,
                artifacts,
            ),
        )

    def _register_attempt(self, attempt_id: str) -> threading.Event:
        with self._attempt_lock:
            lease = self._attempt_leases.get(attempt_id)
            if lease is None:
                lease = threading.Event()
                self._attempt_leases[attempt_id] = lease
            if lease.is_set():
                raise WorkerAttemptRevokedError(f"Worker attempt is already revoked: {attempt_id}")
            return lease

    def _assert_attempt_active(self, attempt_id: str) -> None:
        with self._attempt_lock:
            lease = self._attempt_leases.get(attempt_id)
            revoked = lease is None or lease.is_set()
        if revoked:
            raise WorkerAttemptRevokedError(f"Worker attempt is no longer active: {attempt_id}")

    def _release_attempt(
        self,
        attempt_id: str,
        lease: threading.Event,
    ) -> None:
        with self._attempt_lock:
            if self._attempt_leases.get(attempt_id) is lease:
                self._attempt_leases.pop(attempt_id, None)

    def _slot_for(self, node_id: str) -> int:
        match = _TRAILING_NUMBER.search(node_id)
        if match is not None:
            return (int(match.group(1)) - 1) % len(self.models)
        digest = hashlib.sha256(node_id.encode()).digest()
        return int.from_bytes(digest[:4], "big") % len(self.models)

    def _trace(self, event_type: str, data: Mapping[str, Any]) -> None:
        if self.trace_writer is not None:
            self.trace_writer.write(event_type, data)


def _attempt_number(node: Mapping[str, Any], task_node: TaskNode) -> int:
    attempt = int(node.get("attempt", task_node.retry_count + 1))
    expected = task_node.retry_count + 1
    if attempt != expected or attempt <= 0:
        raise ValueError(
            f"Worker attempt does not match node retry state: "
            f"attempt={attempt}, expected={expected}"
        )
    return attempt


def _attempt_id(task_id: str, node_id: str, attempt: int) -> str:
    return f"{task_id}:{node_id}:a{attempt}"


def _workspace_id(node_id: str, created_at: str, attempt: int) -> str:
    digest = hashlib.sha256(f"{node_id}:{created_at}:attempt={attempt}".encode()).hexdigest()[:10]
    normalized = re.sub(r"[^A-Za-z0-9_.-]", "-", node_id).strip("-.") or "worker"
    return f"{normalized[:24]}-a{attempt}-{digest}"


def _expected_agent(node: TaskNode) -> str | None:
    if node.node_type is NodeType.REVIEW_TASK and node.mode in {
        "minimal_critiques_robust",
        "robust_critiques_minimal",
    }:
        return "PatchAgent"
    return _EXPECTED_AGENTS.get(node.node_type)


def _worker_failure_code(exc: Exception) -> str:
    """Map Worker boundary failures to the closed-loop recovery taxonomy."""

    if isinstance(exc, ModelAdapterError):
        if exc.code == "MODEL_TIMEOUT":
            return "MODEL_TIMEOUT"
        if exc.code == "STRUCTURED_OUTPUT_ERROR":
            return "MODEL_FORMAT_ERROR"
        return "TRANSIENT_WORKER_ERROR"
    if isinstance(exc, WorkerAgentError):
        message = str(exc)
        if "MODEL_TIMEOUT" in message:
            return "MODEL_TIMEOUT"
        if "STRUCTURED_OUTPUT_ERROR" in message:
            return "MODEL_FORMAT_ERROR"
        if exc.code:
            return exc.code
    if type(exc).__name__ == "SchemaValidationError":
        return "ARTIFACT_SCHEMA_ERROR"
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return "ARTIFACT_SCHEMA_ERROR"
    return "TRANSIENT_WORKER_ERROR"
