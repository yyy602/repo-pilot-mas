"""LangGraph runtime bindings for the reviewed closed-loop Engine."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from repo_pilot_mas.orchestration import langgraph_runtime as _runtime
from repo_pilot_mas.orchestration.closed_loop_engine import OrchestrationEngine
from repo_pilot_mas.orchestration.worker_recovery import collect_worker_outcome
from repo_pilot_mas.schemas import ArtifactType

_runtime.OrchestrationEngine = OrchestrationEngine

_original_route_after_supervisor = _runtime.LangGraphRuntime._route_after_supervisor


def _closed_loop_collector(
    self: Any,
    state: Mapping[str, Any],
    config: Any,
) -> dict[str, Any]:
    """Collect every Worker result while isolating invalid artifacts."""

    engine = self._restore_engine(state, config)
    dispatch_id = state.get("dispatch_id")
    pending_ids = tuple(state.get("pending_worker_ids", ()))
    if not dispatch_id or not pending_ids:
        raise ValueError("collector has no active dispatch")

    results = state.get("worker_results", {})
    missing = [node_id for node_id in pending_ids if node_id not in results]
    if missing:
        raise ValueError(f"collector has missing worker results: {missing}")

    artifact_refs: list[str] = []
    collection_results: dict[str, Any] = {}
    for node_id in pending_ids:
        raw = results[node_id]
        if raw.get("dispatch_id") != dispatch_id:
            raise ValueError("worker result dispatch_id is inconsistent")
        outcome = _runtime.WorkerOutcome.from_dict(raw)
        result = collect_worker_outcome(engine, outcome)
        collection_results[node_id] = result.to_dict()
        artifact_refs.extend(result.artifact_refs)
        if result.rejection_ref is not None:
            _discard_rejected_patch_workspaces(self, engine, node_id, outcome)

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
        "engine": _runtime._serialize_engine(engine),
        "engine_state_version": engine.state_version,
        "pending_worker_ids": [],
        "dispatch_id": None,
        "runtime_status": "running",
        "last_event": event,
    }


def _closed_loop_route_after_supervisor(
    self: Any,
    state: Mapping[str, Any],
) -> str:
    route = _original_route_after_supervisor(self, state)
    if route == "end":
        engine = self._restore_engine(state)
        discard = getattr(self.worker_executor, "discard_task_workspaces", None)
        if callable(discard):
            discard(
                task=engine.task.to_dict(),
                reason=f"engine_terminal:{engine.status.value}",
            )
    return route


def _discard_rejected_patch_workspaces(
    runtime: Any,
    engine: OrchestrationEngine,
    node_id: str,
    outcome: Any,
) -> None:
    discard = getattr(runtime.worker_executor, "discard_workspace", None)
    if not callable(discard):
        return
    workspace_ids = {
        str(artifact.content.get("workspace_id", ""))
        for artifact in outcome.artifacts
        if artifact.artifact_type is ArtifactType.PATCH_CANDIDATE
        and str(artifact.content.get("workspace_id", ""))
    }
    for workspace_id in sorted(workspace_ids):
        discard(
            task=engine.task.to_dict(),
            workspace_id=workspace_id,
            node_id=node_id,
            reason="collector_rejected_patch_candidate",
        )


_runtime.LangGraphRuntime._collector_node = _closed_loop_collector
_runtime.AsyncLangGraphRuntime._collector_node = _closed_loop_collector
_runtime.LangGraphRuntime._route_after_supervisor = _closed_loop_route_after_supervisor
_runtime.AsyncLangGraphRuntime._route_after_supervisor = _closed_loop_route_after_supervisor

AsyncLangGraphRuntime = _runtime.AsyncLangGraphRuntime
FakeWorkerExecutor = _runtime.FakeWorkerExecutor
LangGraphRuntime = _runtime.LangGraphRuntime
RuntimeState = _runtime.RuntimeState
WorkerExecutor = _runtime.WorkerExecutor
WorkerInput = _runtime.WorkerInput
WorkerOutcome = _runtime.WorkerOutcome
restore_engine_from_runtime_state = _runtime.restore_engine_from_runtime_state

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
