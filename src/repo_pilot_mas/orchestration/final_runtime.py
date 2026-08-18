"""Final LangGraph runtime bound directly to the reviewed closed-loop Engine."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from repo_pilot_mas.orchestration import langgraph_runtime as _base
from repo_pilot_mas.orchestration.agent_contracts import (
    AgentContractViolation,
    validate_agent_input_contract,
)
from repo_pilot_mas.orchestration.closed_loop_engine import OrchestrationEngine
from repo_pilot_mas.orchestration.engine import EngineStatus
from repo_pilot_mas.orchestration.task_graph import NodeStatus, NodeType, TaskNode
from repo_pilot_mas.orchestration.worker_recovery import (
    MAX_WORKER_ATTEMPTS,
    collect_worker_outcome,
    expected_artifact_type,
    rejection_outcome,
)
from repo_pilot_mas.runtime.trace import TraceWriter
from repo_pilot_mas.schemas.artifact import ArtifactType
from repo_pilot_mas.schemas.tool_result import utc_now_iso

_MINIMUM_WORKER_TIMEOUT_SECONDS = {
    NodeType.INVESTIGATION_TASK: 60.0,
    NodeType.DIAGNOSIS_TASK: 60.0,
    NodeType.CHALLENGE_TASK: 60.0,
    NodeType.REBUTTAL_TASK: 60.0,
    NodeType.REVIEW_TASK: 60.0,
    # PatchAgent 需要多次慢速本地生成 + apply_patch + 目标测试预检查；
    # 120s 在单流 Qwen3-8B 上只能容纳 1-2 次模型调用，会把正常补丁生成
    # 误判为超时，因此最小墙钟放宽到 240s。
    NodeType.PATCH_TASK: 240.0,
}


class _ClosedLoopRuntimeMixin:
    """Closed-loop restore, collection, recovery, and workspace lifecycle hooks."""

    def _build_graph(self) -> Any:
        """Build the final graph with a pre-dispatch contract gate."""

        builder = StateGraph(_base.RuntimeState)
        builder.add_node("validate", self._validate_node)
        builder.add_node("supervisor", self._supervisor_node)
        human_review_node = (
            getattr(self, "_ahuman_review_node", None) or self._human_review_node
        )
        builder.add_node("human_review", human_review_node)
        builder.add_node("human_rejected", self._human_rejected_node)
        builder.add_node("prepare_dispatch", self._prepare_dispatch_node)
        builder.add_node(
            "worker",
            RunnableLambda(self._worker_node, afunc=self._aworker_node),
        )
        builder.add_node("collector", self._collector_node)
        builder.add_edge(START, "validate")
        builder.add_edge("validate", "supervisor")
        builder.add_conditional_edges(
            "supervisor",
            self._route_after_supervisor,
            {
                "end": END,
                "human_review": "human_review",
                "prepare_dispatch": "prepare_dispatch",
                "supervisor": "supervisor",
            },
        )
        builder.add_conditional_edges(
            "human_review",
            self._route_after_human_review,
            {"approved": "prepare_dispatch", "rejected": "human_rejected"},
        )
        builder.add_edge("human_rejected", END)
        builder.add_conditional_edges(
            "prepare_dispatch",
            self._route_prepared_dispatch,
            {"supervisor": "supervisor", "worker": "worker"},
        )
        builder.add_edge("worker", "collector")
        builder.add_conditional_edges(
            "collector",
            self._route_after_collector,
            {
                "end": END,
                "prepare_dispatch": "prepare_dispatch",
                "supervisor": "supervisor",
            },
        )
        return builder.compile(
            checkpointer=self._checkpointer,
            name="repo-pilot-supervisor-runtime",
        )

    def _restore_engine(
        self,
        state: Mapping[str, Any],
        config: RunnableConfig | None = None,
    ) -> OrchestrationEngine:
        value = state.get("engine")
        if not isinstance(value, Mapping):
            raise TypeError("LangGraph state has no serialized Engine")
        task_state = value.get("task_state")
        task_graph = value.get("task_graph")
        blackboard = value.get("blackboard")
        if not all(
            isinstance(item, Mapping)
            for item in (task_state, task_graph, blackboard)
        ):
            raise TypeError("LangGraph serialized Engine is incomplete")
        engine = OrchestrationEngine.restore(
            task_state,  # type: ignore[arg-type]
            task_graph,  # type: ignore[arg-type]
            blackboard,  # type: ignore[arg-type]
            trace_writer=self.trace_writer,
        )
        if state.get("task_id") != engine.task.task_id:
            raise ValueError("LangGraph state task_id is inconsistent")
        if int(state.get("engine_state_version", -1)) != engine.state_version:
            raise ValueError("LangGraph state_version is inconsistent")
        if config is not None:
            configurable = config.get("configurable", {})
            actual_thread_id = (
                configurable.get("thread_id")
                if isinstance(configurable, Mapping)
                else None
            )
            if state.get("thread_id") != actual_thread_id:
                raise ValueError("LangGraph state thread_id is inconsistent")
        return engine

    def _prepare_dispatch_node(
        self,
        state: _base.RuntimeState,
        config: RunnableConfig,
    ) -> _base.RuntimeState:
        """Reject invalid worker inputs before calling WorkerPool."""

        engine = self._restore_engine(state, config)
        retry_ids = _retry_node_ids(state)
        if retry_ids:
            ready_ids = [
                node_id
                for node_id in retry_ids
                if engine.graph.get(node_id).status is NodeStatus.READY
            ][: engine.budget.max_concurrent_nodes]
        else:
            ready_ids = list(_base._ready_node_ids(engine))[
                : engine.budget.max_concurrent_nodes
            ]
        if not ready_ids:
            raise RuntimeError("dispatcher was invoked without READY nodes")

        dispatch_ids: list[str] = []
        rejected_ids: list[str] = []
        contract_results: dict[str, Any] = {}
        for node_id in ready_ids:
            node = engine.graph.get(node_id)
            artifacts = [
                engine.blackboard.artifacts.get(ref).to_dict()
                for ref in node.input_artifact_ids
            ]
            input_artifact_types = [
                str(artifact.get("artifact_type", ""))
                for artifact in artifacts
            ]
            contract_context = {
                "node_id": node_id,
                "node_type": node.node_type.value,
                "agent_type": node.agent_type,
                "mode": node.mode,
                "input_artifact_refs": list(node.input_artifact_ids),
                "input_artifact_types": input_artifact_types,
            }
            try:
                validate_agent_input_contract(
                    node.agent_type,
                    node.mode,
                    artifacts,
                )
            except AgentContractViolation as exc:
                self._trace(
                    self._event(
                        "agent_input_contract_checked",
                        state,
                        engine,
                        **contract_context,
                        passed=False,
                        code=AgentContractViolation.code,
                    )
                )
                engine.start_node(node_id)
                max_attempts = max(
                    1,
                    min(
                        MAX_WORKER_ATTEMPTS,
                        int(engine.budget.max_retries_per_node) + 1,
                    ),
                )
                outcome = rejection_outcome(
                    node_id,
                    AgentContractViolation.code,
                    str(exc),
                    status=NodeStatus.FAILED,
                    attempt=node.retry_count + 1,
                    max_attempts=max_attempts,
                    origin="runtime",
                    recoverable=True,
                    recommended_stage=_contract_recovery_stage(node.node_type),
                    allowed_next_actions=("CREATE_TASK", "REQUEST_REPLAN"),
                    expected_artifact_type=expected_artifact_type(node.node_type),
                    details={
                        "agent_type": node.agent_type,
                        "mode": node.mode,
                        "input_artifact_refs": list(node.input_artifact_ids),
                        "input_artifact_types": input_artifact_types,
                    },
                )
                collection = collect_worker_outcome(
                    engine,
                    outcome,
                    max_attempts=max_attempts,
                )
                collection_payload = collection.to_dict()
                contract_results[node_id] = collection_payload
                rejected_ids.append(node_id)
                self._trace(
                    self._event(
                        "agent_input_contract_rejected",
                        state,
                        engine,
                        **contract_context,
                        code=AgentContractViolation.code,
                        reason=str(exc),
                        rejection_ref=collection.rejection_ref,
                        recoverable=True,
                        recommended_stage=_contract_recovery_stage(node.node_type),
                        allowed_next_actions=["CREATE_TASK", "REQUEST_REPLAN"],
                        retry_scheduled=collection.retry_scheduled,
                        recovery_class="input_contract",
                        recovery_action=collection.details.get(
                            "recovery_action",
                            "return_to_supervisor",
                        ),
                    )
                )
                continue
            self._trace(
                self._event(
                    "agent_input_contract_checked",
                    state,
                    engine,
                    **contract_context,
                    passed=True,
                    code="AGENT_INPUT_CONTRACT_VALID",
                )
            )
            dispatch_ids.append(node_id)

        for node_id in dispatch_ids:
            engine.start_node(node_id)

        dispatch_id = None
        attempt_metadata: dict[str, Any] = {}
        if dispatch_ids:
            dispatch_id = (
                f"{state['thread_id']}:{engine.state_version}:"
                + ",".join(dispatch_ids)
            )
            for node_id in dispatch_ids:
                attempt_metadata[node_id] = _worker_dispatch_metadata(
                    engine.task.task_id,
                    engine.graph.get(node_id),
                    engine.task.to_dict(),
                )
        event = self._event(
            "workers_dispatched" if dispatch_ids else "worker_dispatch_skipped",
            state,
            engine,
            dispatch_id=dispatch_id,
            requested_node_ids=ready_ids,
            node_ids=dispatch_ids,
            attempts=attempt_metadata,
            deterministic_retry=bool(retry_ids),
            contract_rejected_node_ids=rejected_ids,
            contract_results=contract_results,
        )
        self._trace(event)
        return {
            "engine": _base._serialize_engine(engine),
            "engine_state_version": engine.state_version,
            "pending_worker_ids": dispatch_ids,
            "dispatch_id": dispatch_id,
            "human_approved": None,
            "runtime_status": "running",
            "last_event": event,
        }

    def _route_prepared_dispatch(
        self,
        state: _base.RuntimeState,
    ) -> str | list[Any]:
        """Dispatch valid nodes or return directly to Supervisor after rejections."""

        if state.get("pending_worker_ids"):
            return self._fan_out_workers(state)
        return "supervisor"

    def _fan_out_workers(self, state: _base.RuntimeState) -> list[Send]:
        engine = self._restore_engine(state)
        dispatch_id = state.get("dispatch_id")
        if not dispatch_id:
            raise ValueError("dispatch_id is missing")
        sends: list[Send] = []
        task = engine.task.to_dict()
        for node_id in state.get("pending_worker_ids", ()):
            node = engine.graph.get(node_id)
            if node.status is not NodeStatus.RUNNING:
                raise ValueError(f"dispatched node is not RUNNING: {node_id}")
            artifacts = [
                engine.blackboard.artifacts.get(ref).to_dict()
                for ref in node.input_artifact_ids
            ]
            node_payload = node.to_dict()
            node_payload.update(
                _worker_dispatch_metadata(
                    engine.task.task_id,
                    node,
                    task,
                )
            )
            worker_input: _base.WorkerInput = {
                "task_id": state["task_id"],
                "thread_id": state["thread_id"],
                "dispatch_id": dispatch_id,
                "engine_state_version": engine.state_version,
                "task": task,
                "node": node_payload,
                "artifacts": artifacts,
            }
            sends.append(Send("worker", worker_input))
        return sends

    async def _aworker_node(
        self,
        state: _base.WorkerInput,
    ) -> _base.RuntimeState:
        started_at = utc_now_iso()
        started_perf = time.perf_counter()
        self._trace(_base._worker_started_event(state, started_at))
        timeout_seconds = float(state["node"]["timeout_seconds"])
        try:
            async_method = getattr(self.worker_executor, "aexecute", None)
            if async_method is None:
                call = asyncio.to_thread(
                    self.worker_executor.execute,
                    task=state["task"],
                    node=state["node"],
                    artifacts=state["artifacts"],
                )
            else:
                call = async_method(
                    task=state["task"],
                    node=state["node"],
                    artifacts=state["artifacts"],
                )
            outcome = await asyncio.wait_for(call, timeout=timeout_seconds)
        except asyncio.CancelledError:
            self._revoke_worker_attempt(state, reason="runtime_cancelled")
            raise
        except asyncio.TimeoutError:
            self._revoke_worker_attempt(
                state,
                reason=f"worker_timeout:{timeout_seconds}",
            )
            outcome = _base.WorkerOutcome(
                str(state["node"]["node_id"]),
                NodeStatus.TIMED_OUT,
                reason=f"worker exceeded {timeout_seconds} seconds",
            )
        except Exception as exc:  # noqa: BLE001 - isolates arbitrary Worker failures
            outcome = _base.WorkerOutcome(
                str(state["node"]["node_id"]),
                NodeStatus.FAILED,
                reason=f"WORKER_EXECUTION_ERROR:{type(exc).__name__}:{exc}",
            )
        return self._worker_update(state, outcome, started_at, started_perf)

    def _worker_update(
        self,
        state: _base.WorkerInput,
        outcome: _base.WorkerOutcome,
        started_at: str,
        started_perf: float,
    ) -> _base.RuntimeState:
        expected_node_id = str(state["node"]["node_id"])
        if outcome.node_id != expected_node_id:
            raise ValueError("worker outcome belongs to another node")
        attempt = int(state["node"]["attempt"])
        attempt_id = str(state["node"]["attempt_id"])
        payload = outcome.to_dict()
        payload["dispatch_id"] = state["dispatch_id"]
        payload["engine_state_version"] = state["engine_state_version"]
        payload["attempt"] = attempt
        payload["attempt_id"] = attempt_id
        event = {
            "event_type": "worker_completed",
            "task_id": state["task_id"],
            "thread_id": state["thread_id"],
            "state_version": state["engine_state_version"],
            "node_id": outcome.node_id,
            "attempt": attempt,
            "attempt_id": attempt_id,
            "status": outcome.status.value,
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "duration_ms": int((time.perf_counter() - started_perf) * 1000),
            "reason": outcome.reason,
            "requested_timeout_seconds": state["node"].get(
                "requested_timeout_seconds"
            ),
            "effective_timeout_seconds": state["node"].get("timeout_seconds"),
        }
        self._trace(event)
        return {"worker_results": {outcome.node_id: payload}, "last_event": event}

    def _collector_node(
        self,
        state: _base.RuntimeState,
        config: RunnableConfig,
    ) -> _base.RuntimeState:
        engine = self._restore_engine(state, config)
        dispatch_id = state.get("dispatch_id")
        pending_ids = tuple(state.get("pending_worker_ids", ()))
        if not dispatch_id or not pending_ids:
            raise ValueError("collector has no active dispatch")

        results = state.get("worker_results", {})
        missing = [node_id for node_id in pending_ids if node_id not in results]
        if missing:
            raise ValueError(f"collector has missing worker results: {missing}")

        base_version = engine.state_version
        artifact_refs: list[str] = []
        collection_results: dict[str, Any] = {}
        task = engine.task.to_dict()
        for node_id in pending_ids:
            raw = results[node_id]
            if raw.get("dispatch_id") != dispatch_id:
                raise ValueError("worker result dispatch_id is inconsistent")
            if int(raw.get("engine_state_version", -1)) != base_version:
                raise ValueError("worker result state_version is inconsistent")
            expected = _worker_dispatch_metadata(
                engine.task.task_id,
                engine.graph.get(node_id),
                task,
            )
            if int(raw.get("attempt", -1)) != int(expected["attempt"]):
                raise ValueError("worker result attempt is inconsistent")
            if str(raw.get("attempt_id", "")) != str(expected["attempt_id"]):
                raise ValueError("worker result attempt_id is inconsistent")
            outcome = _base.WorkerOutcome.from_dict(raw)
            result = collect_worker_outcome(engine, outcome)
            collection_results[node_id] = result.to_dict()
            artifact_refs.extend(result.artifact_refs)
            if result.rejection_ref is not None:
                self._discard_rejected_patch_workspaces(
                    engine,
                    node_id,
                    outcome,
                )

        event = self._event(
            "worker_artifacts_collected",
            state,
            engine,
            dispatch_id=dispatch_id,
            node_ids=list(pending_ids),
            artifact_refs=artifact_refs,
            collection_results=collection_results,
        )
        self._trace(event)
        return {
            "engine": _base._serialize_engine(engine),
            "engine_state_version": engine.state_version,
            "pending_worker_ids": [],
            "dispatch_id": None,
            "runtime_status": "running",
            "last_event": event,
        }

    def _route_after_collector(self, state: _base.RuntimeState) -> str:
        engine = self._restore_engine(state)
        if engine.status is not EngineStatus.ACTIVE:
            self._discard_terminal_task_workspaces(engine, state)
            return "end"
        retry_ids = _retry_node_ids(state)
        ready = set(_base._ready_node_ids(engine))
        eligible = [node_id for node_id in retry_ids if node_id in ready]
        if eligible:
            self._trace(
                {
                    "event_type": "deterministic_worker_retry_scheduled",
                    "task_id": engine.task.task_id,
                    "thread_id": state.get("thread_id"),
                    "state_version": engine.state_version,
                    "node_ids": eligible,
                    "reason": "collector_retry_same_node",
                }
            )
            return "prepare_dispatch"
        return "supervisor"

    def _route_after_supervisor(self, state: _base.RuntimeState) -> str:
        engine = self._restore_engine(state)
        if engine.status is not EngineStatus.ACTIVE:
            self._discard_terminal_task_workspaces(engine, state)
            return "end"
        ready = _base._ready_node_ids(engine)
        if not ready:
            return "supervisor"
        decision = state.get("decision_result") or {}
        decision_id = decision.get("decision_id")
        approved = decision_id in state.get("approved_decision_ids", ())
        if state.get("require_human_approval", False) and not approved:
            return "human_review"
        return "prepare_dispatch"

    def _revoke_worker_attempt(
        self,
        state: _base.WorkerInput,
        *,
        reason: str,
    ) -> bool:
        revoke = getattr(self.worker_executor, "revoke_attempt", None)
        attempt_id = str(state["node"].get("attempt_id", ""))
        event = {
            "event_type": "worker_attempt_revocation_requested",
            "task_id": state["task_id"],
            "thread_id": state["thread_id"],
            "state_version": state["engine_state_version"],
            "node_id": state["node"]["node_id"],
            "attempt": state["node"].get("attempt"),
            "attempt_id": attempt_id,
            "reason": reason,
        }
        if not callable(revoke):
            event["revoked"] = False
            event["code"] = "WORKER_EXECUTOR_REVOCATION_UNAVAILABLE"
            self._trace(event)
            return False
        try:
            revoked = bool(
                revoke(
                    task=state["task"],
                    node=state["node"],
                    attempt_id=attempt_id or None,
                    reason=reason,
                )
            )
        except Exception as exc:  # noqa: BLE001 - timeout handling must fail closed
            event.update(
                {
                    "revoked": False,
                    "code": "WORKER_ATTEMPT_REVOCATION_FAILED",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            self._trace(event)
            return False
        event["revoked"] = revoked
        event["code"] = "WORKER_ATTEMPT_REVOKED" if revoked else "ALREADY_REVOKED"
        self._trace(event)
        return revoked

    def _discard_rejected_patch_workspaces(
        self,
        engine: OrchestrationEngine,
        node_id: str,
        outcome: _base.WorkerOutcome,
    ) -> None:
        discard = getattr(self.worker_executor, "discard_workspace", None)
        if not callable(discard):
            return
        workspace_ids = {
            str(artifact.content.get("workspace_id", ""))
            for artifact in outcome.artifacts
            if artifact.artifact_type is ArtifactType.PATCH_CANDIDATE
            and str(artifact.content.get("workspace_id", ""))
        }
        for workspace_id in sorted(workspace_ids):
            try:
                discard(
                    task=engine.task.to_dict(),
                    workspace_id=workspace_id,
                    node_id=node_id,
                    reason="collector_rejected_patch_candidate",
                )
            except Exception as exc:  # noqa: BLE001 - cleanup must not crash recovery
                self._trace(
                    {
                        "event_type": "workspace_cleanup_failed",
                        "task_id": engine.task.task_id,
                        "node_id": node_id,
                        "workspace_id": workspace_id,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )

    def _discard_terminal_task_workspaces(
        self,
        engine: OrchestrationEngine,
        state: Mapping[str, Any],
    ) -> None:
        discard = getattr(self.worker_executor, "discard_task_workspaces", None)
        if not callable(discard):
            return
        try:
            workspace_ids = discard(
                task=engine.task.to_dict(),
                reason=f"engine_terminal:{engine.status.value}",
            )
        except Exception as exc:  # noqa: BLE001 - terminal state must remain durable
            self._trace(
                {
                    "event_type": "workspace_cleanup_failed",
                    "task_id": engine.task.task_id,
                    "thread_id": state.get("thread_id"),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            return
        self._trace(
            {
                "event_type": "terminal_workspaces_discarded",
                "task_id": engine.task.task_id,
                "thread_id": state.get("thread_id"),
                "workspace_ids": list(workspace_ids),
            }
        )


class LangGraphRuntime(_ClosedLoopRuntimeMixin, _base.LangGraphRuntime):
    """Synchronous durable runtime for the final closed-loop Engine."""


class AsyncLangGraphRuntime(_ClosedLoopRuntimeMixin, _base.AsyncLangGraphRuntime):
    """Asynchronous durable runtime for the final closed-loop Engine."""


FakeWorkerExecutor = _base.FakeWorkerExecutor
RuntimeState = _base.RuntimeState
WorkerExecutor = _base.WorkerExecutor
WorkerInput = _base.WorkerInput
WorkerOutcome = _base.WorkerOutcome


def _contract_recovery_stage(node_type: NodeType) -> str:
    if node_type is NodeType.INVESTIGATION_TASK:
        return "investigation"
    if node_type in {
        NodeType.DIAGNOSIS_TASK,
        NodeType.CHALLENGE_TASK,
        NodeType.REBUTTAL_TASK,
    }:
        return "diagnosis"
    if node_type is NodeType.REVIEW_TASK:
        return "review"
    if node_type in {NodeType.PATCH_TASK, NodeType.VALIDATION_TASK}:
        return "patch"
    return "review"


def _retry_node_ids(state: Mapping[str, Any]) -> tuple[str, ...]:
    event = state.get("last_event")
    if not isinstance(event, Mapping) or event.get("event_type") != "worker_artifacts_collected":
        return ()
    results = event.get("collection_results")
    if not isinstance(results, Mapping):
        return ()
    return tuple(
        str(node_id)
        for node_id, result in results.items()
        if isinstance(result, Mapping) and result.get("retry_scheduled") is True
    )


def _worker_dispatch_metadata(
    task_id: str,
    node: TaskNode,
    task: Mapping[str, Any],
) -> dict[str, Any]:
    attempt = node.retry_count + 1
    requested_timeout = float(node.timeout_seconds)
    effective_timeout = _effective_worker_timeout_seconds(node, task)
    return {
        "attempt": attempt,
        "attempt_id": f"{task_id}:{node.node_id}:a{attempt}",
        "requested_timeout_seconds": requested_timeout,
        "timeout_seconds": effective_timeout,
    }


def _effective_worker_timeout_seconds(
    node: TaskNode,
    task: Mapping[str, Any],
) -> float:
    requested = float(node.timeout_seconds)
    if node.node_type is NodeType.VALIDATION_TASK:
        task_runtime = float(task.get("max_runtime_seconds", 20.0))
        minimum = max(60.0, task_runtime * 3.0 + 15.0)
    else:
        minimum = _MINIMUM_WORKER_TIMEOUT_SECONDS.get(node.node_type, 60.0)
    return max(requested, minimum)


def restore_engine_from_runtime_state(
    state: Mapping[str, Any],
    *,
    trace_writer: TraceWriter | None = None,
) -> OrchestrationEngine:
    """Restore the reviewed Engine from an exported runtime state."""

    value = state.get("engine")
    if not isinstance(value, Mapping):
        raise TypeError("LangGraph state has no serialized Engine")
    task_state = value.get("task_state")
    task_graph = value.get("task_graph")
    blackboard = value.get("blackboard")
    if not all(
        isinstance(item, Mapping)
        for item in (task_state, task_graph, blackboard)
    ):
        raise TypeError("LangGraph serialized Engine is incomplete")
    engine = OrchestrationEngine.restore(
        task_state,  # type: ignore[arg-type]
        task_graph,  # type: ignore[arg-type]
        blackboard,  # type: ignore[arg-type]
        trace_writer=trace_writer,
    )
    if state.get("task_id") != engine.task.task_id:
        raise ValueError("LangGraph state task_id is inconsistent")
    if int(state.get("engine_state_version", -1)) != engine.state_version:
        raise ValueError("LangGraph state_version is inconsistent")
    return engine


__all__ = [
    "AsyncLangGraphRuntime",
    "FakeWorkerExecutor",
    "LangGraphRuntime",
    "RuntimeState",
    "WorkerExecutor",
    "WorkerInput",
    "WorkerOutcome",
    "restore_engine_from_runtime_state",
]
