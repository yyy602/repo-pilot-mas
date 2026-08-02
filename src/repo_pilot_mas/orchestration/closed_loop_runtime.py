"""LangGraph runtime bindings that restore the reviewed closed-loop Engine."""

from __future__ import annotations

from repo_pilot_mas.orchestration import langgraph_runtime as _runtime
from repo_pilot_mas.orchestration.closed_loop_engine import OrchestrationEngine

# The original runtime centralizes serialization and restore logic around a module-level
# OrchestrationEngine symbol. Rebinding that symbol preserves its public API and durable
# checkpoint format while ensuring every restore returns the reviewed closed-loop Engine.
_runtime.OrchestrationEngine = OrchestrationEngine

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
