"""LangGraph runtime bindings that restore the reviewed closed-loop Engine."""

from __future__ import annotations

from typing import Any

from repo_pilot_mas.orchestration import langgraph_runtime as _runtime
from repo_pilot_mas.orchestration.closed_loop_engine import OrchestrationEngine
from repo_pilot_mas.orchestration.worker_recovery import collect_worker_outcome

_runtime.OrchestrationEngine = OrchestrationEngine

_original_collector = _runtime.LangGraphRuntime._collector_node


def _closed_loop_collector(self: Any, state: Any, config: Any) -> dict[str, Any]:
    """Collector adapter that isolates invalid Worker artifacts."""

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
        outcome = _runtime.WorkerOutcome.from_dict(results[node_id])
        result = collect_worker_outcome(engine, outcome)
        collection_results[node_id] = result.to_dict()
        artifact_refs.extend(result.artifact_refs)

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


_runtime.LangGraphRuntime._collector_node = _closed_loop_collector
_runtime.AsyncLangGraphRuntime._collector_node = _closed_loop_collector

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
