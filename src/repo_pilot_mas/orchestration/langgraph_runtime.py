"""Supervisor-led durable execution loop built on LangGraph."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Protocol, TypedDict

import aiosqlite
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt
from typing_extensions import Self

from repo_pilot_mas.orchestration.engine import EngineStatus, OrchestrationEngine
from repo_pilot_mas.orchestration.task_graph import NodeStatus
from repo_pilot_mas.runtime.trace import TraceWriter
from repo_pilot_mas.schemas.artifact import Artifact, ArtifactType
from repo_pilot_mas.schemas.tool_result import utc_now_iso

_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_WORKER_TERMINAL_STATUSES = {
    NodeStatus.SUCCEEDED,
    NodeStatus.FAILED,
    NodeStatus.TIMED_OUT,
    NodeStatus.BLOCKED,
}


def _merge_worker_results(
    current: Mapping[str, Mapping[str, Any]] | None,
    update: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    merged = {key: dict(value) for key, value in (current or {}).items()}
    merged.update({key: dict(value) for key, value in (update or {}).items()})
    return merged


def _take_latest_event(
    current: Mapping[str, Any] | None,
    update: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return dict(update or current or {})


class RuntimeState(TypedDict, total=False):
    task_id: str
    thread_id: str
    engine: dict[str, Any]
    engine_state_version: int
    decision_result: dict[str, Any] | None
    pending_worker_ids: list[str]
    dispatch_id: str | None
    worker_results: Annotated[dict[str, dict[str, Any]], _merge_worker_results]
    require_human_approval: bool
    approved_decision_ids: list[str]
    human_approved: bool | None
    runtime_status: str
    last_event: Annotated[dict[str, Any], _take_latest_event]


class WorkerInput(TypedDict):
    task_id: str
    thread_id: str
    dispatch_id: str
    engine_state_version: int
    task: dict[str, Any]
    node: dict[str, Any]
    artifacts: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    """A Worker result that the Collector asks Engine to apply."""

    node_id: str
    status: NodeStatus
    artifacts: tuple[Artifact, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.node_id.strip():
            raise ValueError("worker outcome node_id must not be empty")
        object.__setattr__(self, "status", NodeStatus(self.status))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        if self.status not in _WORKER_TERMINAL_STATUSES:
            raise ValueError("worker outcome requires a terminal worker status")
        if self.status is NodeStatus.SUCCEEDED and not self.artifacts:
            raise ValueError("successful worker outcome must contain at least one artifact")

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status.value,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkerOutcome:
        raw_artifacts = value.get("artifacts", ())
        if isinstance(raw_artifacts, (str, bytes)):
            raise TypeError("worker artifacts must be a sequence")
        return cls(
            node_id=str(value["node_id"]),
            status=NodeStatus(str(value["status"])),
            artifacts=tuple(Artifact.from_dict(item) for item in raw_artifacts),
            reason=str(value["reason"]) if value.get("reason") else None,
        )


class WorkerExecutor(Protocol):
    """Phase 4 Worker pool boundary used by the Phase 3 runtime."""

    def execute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome: ...

    async def aexecute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome: ...


@dataclass(slots=True)
class FakeWorkerExecutor:
    """Deterministic Phase 3 executor; it is not a production Worker Agent."""

    calls: list[str] = field(default_factory=list)

    def execute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        del task, artifacts
        node_id = str(node["node_id"])
        self.calls.append(node_id)
        artifact = Artifact(
            artifact_id=f"{node_id}-evidence",
            artifact_type=ArtifactType.EVIDENCE,
            created_by=node_id,
            content={
                "mode": "code_retrieval",
                "evidence_kind": "source",
                "claim": f"Fake Worker 已完成：{node['objective']}",
                "supports_claims": [f"Fake Worker 已完成：{node['objective']}"],
                "contradicts_claims": [],
                "verified": True,
                "source": {
                    "path": "fake_worker.py",
                    "line_start": 1,
                    "line_end": 1,
                },
                "content": "仅用于运行时控制流测试的确定性证据",
                "observation_type": "direct",
                "confidence": 1.0,
                "status": "verified",
                "tool_trace_ids": [f"fake-tool-{node_id}"],
                "missing_evidence": [],
            },
        )
        return WorkerOutcome(node_id, NodeStatus.SUCCEEDED, (artifact,))

    async def aexecute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        return self.execute(task=task, node=node, artifacts=artifacts)


class LangGraphRuntime:
    """Durable runtime around Supervisor, Engine, dynamic dispatch, and collection."""

    def __init__(
        self,
        supervisor: Any,
        worker_executor: WorkerExecutor,
        checkpoint_db: str | Path,
        *,
        trace_writer: TraceWriter | None = None,
        recursion_limit: int = 128,
    ) -> None:
        if recursion_limit <= 0:
            raise ValueError("recursion_limit must be positive")
        self.supervisor = supervisor
        self.worker_executor = worker_executor
        self.trace_writer = trace_writer
        self.recursion_limit = recursion_limit
        self.checkpoint_db = _checkpoint_path(checkpoint_db)
        self._connection = sqlite3.connect(self.checkpoint_db, check_same_thread=False)
        serializer = JsonPlusSerializer(allowed_msgpack_modules=())
        self._checkpointer = SqliteSaver(self._connection, serde=serializer)
        self.graph = self._build_graph()
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def run(
        self,
        engine: OrchestrationEngine,
        *,
        thread_id: str,
        require_human_approval: bool = False,
    ) -> dict[str, Any]:
        """Start a new durable thread and run until terminal, interrupt, or error."""

        self._ensure_open()
        config = self._config(thread_id)
        if self.graph.get_state(config).values:
            raise ValueError(f"LangGraph thread already exists: {thread_id}")
        initial = self._initial_state(engine, thread_id, require_human_approval)
        return dict(self.graph.invoke(initial, config, durability="sync"))

    def stream(
        self,
        engine: OrchestrationEngine,
        *,
        thread_id: str,
        require_human_approval: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Start a new thread and yield LangGraph node updates."""

        self._ensure_open()
        config = self._config(thread_id)
        if self.graph.get_state(config).values:
            raise ValueError(f"LangGraph thread already exists: {thread_id}")
        initial = self._initial_state(engine, thread_id, require_human_approval)
        return self.graph.stream(
            initial,
            config,
            stream_mode="updates",
            durability="sync",
        )

    def resume(
        self,
        *,
        thread_id: str,
        task_id: str,
        expected_state_version: int | None = None,
        human_response: bool | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resume a failed or interrupted thread after validating its identity."""

        self._ensure_open()
        config = self._config(thread_id)
        self.assert_thread_binding(
            thread_id=thread_id,
            task_id=task_id,
            expected_state_version=expected_state_version,
        )
        command: Command[Any] | None = None
        if human_response is not None:
            command = Command(resume=human_response)
        return dict(self.graph.invoke(command, config, durability="sync"))

    def stream_resume(
        self,
        *,
        thread_id: str,
        task_id: str,
        expected_state_version: int | None = None,
        human_response: bool | Mapping[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Resume a thread and yield LangGraph node updates."""

        self._ensure_open()
        config = self._config(thread_id)
        self.assert_thread_binding(
            thread_id=thread_id,
            task_id=task_id,
            expected_state_version=expected_state_version,
        )
        command: Command[Any] | None = None
        if human_response is not None:
            command = Command(resume=human_response)
        return self.graph.stream(
            command,
            config,
            stream_mode="updates",
            durability="sync",
        )

    def get_state(self, *, thread_id: str, task_id: str) -> dict[str, Any]:
        self.assert_thread_binding(thread_id=thread_id, task_id=task_id)
        return dict(self.graph.get_state(self._config(thread_id)).values)

    def state_history(self, *, thread_id: str, task_id: str) -> tuple[dict[str, Any], ...]:
        self.assert_thread_binding(thread_id=thread_id, task_id=task_id)
        config = self._config(thread_id)
        return tuple(dict(snapshot.values) for snapshot in self.graph.get_state_history(config))

    def assert_thread_binding(
        self,
        *,
        thread_id: str,
        task_id: str,
        expected_state_version: int | None = None,
    ) -> None:
        self._ensure_open()
        state = self.graph.get_state(self._config(thread_id)).values
        if not state:
            raise ValueError(f"LangGraph thread does not exist: {thread_id}")
        if state.get("thread_id") != thread_id:
            raise ValueError("LangGraph checkpoint thread_id is inconsistent")
        if state.get("task_id") != task_id:
            raise ValueError("LangGraph checkpoint task_id is inconsistent")
        engine = self._restore_engine(state)
        if expected_state_version is not None and engine.state_version != expected_state_version:
            raise ValueError("LangGraph checkpoint state_version is inconsistent")

    def _build_graph(self) -> Any:
        builder = StateGraph(RuntimeState)
        builder.add_node("validate", self._validate_node)
        builder.add_node("supervisor", self._supervisor_node)
        # 异步图需要异步审批节点（interrupt 依赖 runnable context，
        # 同步节点在 async 图里被丢进线程池会导致 interrupt 失效）。
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
        builder.add_conditional_edges("prepare_dispatch", self._fan_out_workers, ["worker"])
        builder.add_edge("worker", "collector")
        builder.add_edge("collector", "supervisor")
        return builder.compile(checkpointer=self._checkpointer, name="repo-pilot-supervisor-runtime")

    def _validate_node(self, state: RuntimeState, config: RunnableConfig) -> RuntimeState:
        engine = self._restore_engine(state, config)
        return {
            "runtime_status": "running",
            "last_event": self._event("runtime_started", state, engine),
        }

    def _supervisor_node(self, state: RuntimeState, config: RunnableConfig) -> RuntimeState:
        engine = self._restore_engine(state, config)
        result = engine.run_supervisor(self.supervisor)
        event = self._event(
            "supervisor_decision_applied" if result.ok else "supervisor_decision_rejected",
            state,
            engine,
            decision_result=result.to_dict(),
        )
        self._trace(event)
        return {
            "engine": _serialize_engine(engine),
            "engine_state_version": engine.state_version,
            "decision_result": result.to_dict(),
            "runtime_status": _runtime_status(engine),
            "last_event": event,
        }

    def _human_review_node(self, state: RuntimeState, config: RunnableConfig) -> RuntimeState:
        engine = self._restore_engine(state, config)
        decision = state.get("decision_result") or {}
        response = interrupt(
            {
                "type": "worker_dispatch_approval",
                "task_id": state["task_id"],
                "thread_id": state["thread_id"],
                "state_version": engine.state_version,
                "decision_id": decision.get("decision_id"),
                "ready_worker_ids": _ready_node_ids(engine),
            }
        )
        approved = _human_approval(response)
        approved_ids = list(state.get("approved_decision_ids", ()))
        decision_id = decision.get("decision_id")
        if approved and isinstance(decision_id, str) and decision_id not in approved_ids:
            approved_ids.append(decision_id)
        event = self._event(
            "human_review_resumed",
            state,
            engine,
            approved=approved,
            decision_id=decision_id,
        )
        self._trace(event)
        return {
            "human_approved": approved,
            "approved_decision_ids": approved_ids,
            "last_event": event,
        }

    def _human_rejected_node(self, state: RuntimeState, config: RunnableConfig) -> RuntimeState:
        engine = self._restore_engine(state, config)
        event = self._event("human_review_rejected", state, engine)
        self._trace(event)
        return {"runtime_status": "human_rejected", "last_event": event}

    def _prepare_dispatch_node(
        self,
        state: RuntimeState,
        config: RunnableConfig,
    ) -> RuntimeState:
        engine = self._restore_engine(state, config)
        ready_ids = list(_ready_node_ids(engine))[: engine.budget.max_concurrent_nodes]
        if not ready_ids:
            raise RuntimeError("dispatcher was invoked without READY nodes")
        for node_id in ready_ids:
            engine.start_node(node_id)
        dispatch_id = (
            f"{state['thread_id']}:{engine.state_version}:" + ",".join(ready_ids)
        )
        event = self._event(
            "workers_dispatched",
            state,
            engine,
            dispatch_id=dispatch_id,
            node_ids=ready_ids,
        )
        self._trace(event)
        return {
            "engine": _serialize_engine(engine),
            "engine_state_version": engine.state_version,
            "pending_worker_ids": ready_ids,
            "dispatch_id": dispatch_id,
            "human_approved": None,
            "runtime_status": "running",
            "last_event": event,
        }

    def _fan_out_workers(self, state: RuntimeState) -> list[Send]:
        engine = self._restore_engine(state)
        dispatch_id = state.get("dispatch_id")
        if not dispatch_id:
            raise ValueError("dispatch_id is missing")
        sends: list[Send] = []
        for node_id in state.get("pending_worker_ids", ()):
            node = engine.graph.get(node_id)
            if node.status is not NodeStatus.RUNNING:
                raise ValueError(f"dispatched node is not RUNNING: {node_id}")
            artifacts = [
                engine.blackboard.artifacts.get(ref).to_dict()
                for ref in node.input_artifact_ids
            ]
            worker_input: WorkerInput = {
                "task_id": state["task_id"],
                "thread_id": state["thread_id"],
                "dispatch_id": dispatch_id,
                "engine_state_version": engine.state_version,
                "task": engine.task.to_dict(),
                "node": node.to_dict(),
                "artifacts": artifacts,
            }
            sends.append(Send("worker", worker_input))
        return sends

    def _worker_node(self, state: WorkerInput) -> RuntimeState:
        started_at = utc_now_iso()
        started_perf = time.perf_counter()
        self._trace(_worker_started_event(state, started_at))
        outcome = self.worker_executor.execute(
            task=state["task"],
            node=state["node"],
            artifacts=state["artifacts"],
        )
        return self._worker_update(state, outcome, started_at, started_perf)

    async def _aworker_node(self, state: WorkerInput) -> RuntimeState:
        started_at = utc_now_iso()
        started_perf = time.perf_counter()
        self._trace(_worker_started_event(state, started_at))
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
        except asyncio.TimeoutError:
            outcome = WorkerOutcome(
                str(state["node"]["node_id"]),
                NodeStatus.TIMED_OUT,
                reason=f"worker exceeded {timeout_seconds} seconds",
            )
        except Exception as exc:  # noqa: BLE001 - isolates arbitrary Worker failures
            outcome = WorkerOutcome(
                str(state["node"]["node_id"]),
                NodeStatus.FAILED,
                reason=f"WORKER_EXECUTION_ERROR:{type(exc).__name__}:{exc}",
            )
        return self._worker_update(state, outcome, started_at, started_perf)

    def _worker_update(
        self,
        state: WorkerInput,
        outcome: WorkerOutcome,
        started_at: str,
        started_perf: float,
    ) -> RuntimeState:
        expected_node_id = str(state["node"]["node_id"])
        if outcome.node_id != expected_node_id:
            raise ValueError("worker outcome belongs to another node")
        payload = outcome.to_dict()
        payload["dispatch_id"] = state["dispatch_id"]
        payload["engine_state_version"] = state["engine_state_version"]
        event = {
            "event_type": "worker_completed",
            "task_id": state["task_id"],
            "thread_id": state["thread_id"],
            "state_version": state["engine_state_version"],
            "node_id": outcome.node_id,
            "status": outcome.status.value,
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "duration_ms": int((time.perf_counter() - started_perf) * 1000),
            "reason": outcome.reason,
        }
        self._trace(event)
        return {"worker_results": {outcome.node_id: payload}, "last_event": event}

    def _collector_node(self, state: RuntimeState, config: RunnableConfig) -> RuntimeState:
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
        for node_id in pending_ids:
            raw = results[node_id]
            if raw.get("dispatch_id") != dispatch_id:
                raise ValueError("worker result dispatch_id is inconsistent")
            if int(raw.get("engine_state_version", -1)) != base_version:
                raise ValueError("worker result state_version is inconsistent")
            outcome = WorkerOutcome.from_dict(raw)
            refs: list[str] = []
            for artifact in outcome.artifacts:
                if artifact.created_by != node_id:
                    raise ValueError("worker artifact created_by is inconsistent")
                refs.append(engine.add_artifact(artifact))
            engine.finish_node(
                node_id,
                outcome.status,
                artifact_refs=refs,
                reason=outcome.reason,
            )
            artifact_refs.extend(refs)
        event = self._event(
            "worker_artifacts_collected",
            state,
            engine,
            dispatch_id=dispatch_id,
            node_ids=list(pending_ids),
            artifact_refs=artifact_refs,
        )
        self._trace(event)
        return {
            "engine": _serialize_engine(engine),
            "engine_state_version": engine.state_version,
            "pending_worker_ids": [],
            "dispatch_id": None,
            "runtime_status": "running",
            "last_event": event,
        }

    def _route_after_supervisor(self, state: RuntimeState) -> str:
        engine = self._restore_engine(state)
        if engine.status is not EngineStatus.ACTIVE:
            return "end"
        ready = _ready_node_ids(engine)
        if not ready:
            return "supervisor"
        decision = state.get("decision_result") or {}
        decision_id = decision.get("decision_id")
        approved = decision_id in state.get("approved_decision_ids", ())
        if state.get("require_human_approval", False) and not approved:
            return "human_review"
        return "prepare_dispatch"

    @staticmethod
    def _route_after_human_review(state: RuntimeState) -> str:
        return "approved" if state.get("human_approved") is True else "rejected"

    def _initial_state(
        self,
        engine: OrchestrationEngine,
        thread_id: str,
        require_human_approval: bool,
    ) -> RuntimeState:
        if engine.status is not EngineStatus.ACTIVE:
            raise ValueError("new LangGraph runtime requires an active Engine")
        return {
            "task_id": engine.task.task_id,
            "thread_id": thread_id,
            "engine": _serialize_engine(engine),
            "engine_state_version": engine.state_version,
            "decision_result": None,
            "pending_worker_ids": [],
            "dispatch_id": None,
            "worker_results": {},
            "require_human_approval": require_human_approval,
            "approved_decision_ids": [],
            "human_approved": None,
            "runtime_status": "running",
            "last_event": {},
        }

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
        if not all(isinstance(item, Mapping) for item in (task_state, task_graph, blackboard)):
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
            actual_thread_id = configurable.get("thread_id") if isinstance(configurable, Mapping) else None
            if state.get("thread_id") != actual_thread_id:
                raise ValueError("LangGraph state thread_id is inconsistent")
        return engine

    def _config(self, thread_id: str) -> dict[str, Any]:
        if not _THREAD_ID_PATTERN.fullmatch(thread_id) or ".." in thread_id:
            raise ValueError(f"invalid LangGraph thread_id: {thread_id!r}")
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self.recursion_limit,
        }

    def _event(
        self,
        event_type: str,
        state: Mapping[str, Any],
        engine: OrchestrationEngine,
        **details: Any,
    ) -> dict[str, Any]:
        return {
            "event_type": event_type,
            "task_id": state["task_id"],
            "thread_id": state["thread_id"],
            "state_version": engine.state_version,
            **details,
        }

    def _trace(self, event: Mapping[str, Any]) -> None:
        if self.trace_writer is not None:
            self.trace_writer.write(str(event["event_type"]), event)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("LangGraph runtime is closed")


class AsyncLangGraphRuntime(LangGraphRuntime):
    """Async Phase 4 runtime using AsyncSqliteSaver and concurrent Worker sends."""

    async def _ahuman_review_node(
        self,
        state: RuntimeState,
        config: RunnableConfig,
    ) -> RuntimeState:
        """异步图专用审批节点。

        同步 _human_review_node 在 async 图中会被 LangGraph 丢进线程池执行，
        而 interrupt() 依赖当前 runnable context（contextvar），在子线程里调用会
        抛 "Called get_config outside of a runnable context"。异步版本在事件循环
        上下文中直接调用 interrupt，保证 require_human_approval 在 AsyncRuntime
        下可用（Phase 7 阶段三审批控制面依赖此修复）。
        """

        engine = self._restore_engine(state, config)
        decision = state.get("decision_result") or {}
        response = interrupt(
            {
                "type": "worker_dispatch_approval",
                "task_id": state["task_id"],
                "thread_id": state["thread_id"],
                "state_version": engine.state_version,
                "decision_id": decision.get("decision_id"),
                "ready_worker_ids": _ready_node_ids(engine),
            }
        )
        approved = _human_approval(response)
        approved_ids = list(state.get("approved_decision_ids", ()))
        decision_id = decision.get("decision_id")
        if approved and isinstance(decision_id, str) and decision_id not in approved_ids:
            approved_ids.append(decision_id)
        event = self._event(
            "human_review_resumed",
            state,
            engine,
            approved=approved,
            decision_id=decision_id,
        )
        self._trace(event)
        return {
            "human_approved": approved,
            "approved_decision_ids": approved_ids,
            "last_event": event,
        }

    @classmethod
    async def create(
        cls,
        supervisor: Any,
        worker_executor: WorkerExecutor,
        checkpoint_db: str | Path,
        *,
        trace_writer: TraceWriter | None = None,
        recursion_limit: int = 128,
    ) -> AsyncLangGraphRuntime:
        if recursion_limit <= 0:
            raise ValueError("recursion_limit must be positive")
        self = cls.__new__(cls)
        self.supervisor = supervisor
        self.worker_executor = worker_executor
        self.trace_writer = trace_writer
        self.recursion_limit = recursion_limit
        self.checkpoint_db = _checkpoint_path(checkpoint_db)
        self._async_connection = await aiosqlite.connect(self.checkpoint_db)
        serializer = JsonPlusSerializer(allowed_msgpack_modules=())
        self._checkpointer = AsyncSqliteSaver(
            self._async_connection,
            serde=serializer,
        )
        self.graph = self._build_graph()
        self._closed = False
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if not self._closed:
            await self._async_connection.close()
            self._closed = True

    def close(self) -> None:
        raise RuntimeError("AsyncLangGraphRuntime requires 'await aclose()'")

    async def arun(
        self,
        engine: OrchestrationEngine,
        *,
        thread_id: str,
        require_human_approval: bool = False,
    ) -> dict[str, Any]:
        self._ensure_open()
        config = self._config(thread_id)
        if (await self.graph.aget_state(config)).values:
            raise ValueError(f"LangGraph thread already exists: {thread_id}")
        initial = self._initial_state(engine, thread_id, require_human_approval)
        return dict(await self.graph.ainvoke(initial, config, durability="sync"))

    async def astream(
        self,
        engine: OrchestrationEngine,
        *,
        thread_id: str,
        require_human_approval: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        self._ensure_open()
        config = self._config(thread_id)
        if (await self.graph.aget_state(config)).values:
            raise ValueError(f"LangGraph thread already exists: {thread_id}")
        initial = self._initial_state(engine, thread_id, require_human_approval)
        async for update in self.graph.astream(
            initial,
            config,
            stream_mode="updates",
            durability="sync",
        ):
            yield update

    async def aresume(
        self,
        *,
        thread_id: str,
        task_id: str,
        expected_state_version: int | None = None,
        human_response: bool | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_open()
        config = self._config(thread_id)
        await self.assert_thread_binding_async(
            thread_id=thread_id,
            task_id=task_id,
            expected_state_version=expected_state_version,
        )
        command: Command[Any] | None = None
        if human_response is not None:
            command = Command(resume=human_response)
        return dict(await self.graph.ainvoke(command, config, durability="sync"))

    async def aget_state(self, *, thread_id: str, task_id: str) -> dict[str, Any]:
        await self.assert_thread_binding_async(thread_id=thread_id, task_id=task_id)
        snapshot = await self.graph.aget_state(self._config(thread_id))
        return dict(snapshot.values)

    async def astate_history(
        self,
        *,
        thread_id: str,
        task_id: str,
    ) -> tuple[dict[str, Any], ...]:
        await self.assert_thread_binding_async(thread_id=thread_id, task_id=task_id)
        config = self._config(thread_id)
        snapshots = [
            dict(snapshot.values)
            async for snapshot in self.graph.aget_state_history(config)
        ]
        return tuple(snapshots)

    async def assert_thread_binding_async(
        self,
        *,
        thread_id: str,
        task_id: str,
        expected_state_version: int | None = None,
    ) -> None:
        self._ensure_open()
        state = (await self.graph.aget_state(self._config(thread_id))).values
        if not state:
            raise ValueError(f"LangGraph thread does not exist: {thread_id}")
        if state.get("thread_id") != thread_id:
            raise ValueError("LangGraph checkpoint thread_id is inconsistent")
        if state.get("task_id") != task_id:
            raise ValueError("LangGraph checkpoint task_id is inconsistent")
        engine = self._restore_engine(state)
        if expected_state_version is not None and engine.state_version != expected_state_version:
            raise ValueError("LangGraph checkpoint state_version is inconsistent")


def _serialize_engine(engine: OrchestrationEngine) -> dict[str, Any]:
    return {
        "task_state": engine.to_task_state_dict(),
        "task_graph": engine.graph.to_dict(),
        "blackboard": engine.blackboard.to_dict(),
    }


def _checkpoint_path(checkpoint_db: str | Path) -> Path:
    candidate = Path(checkpoint_db).expanduser()
    if candidate.is_symlink():
        raise ValueError("LangGraph checkpoint path must not be a symbolic link")
    resolved = candidate.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if resolved.is_symlink() or (resolved.exists() and not resolved.is_file()):
        raise ValueError("LangGraph checkpoint path must be one regular file")
    return resolved


def _worker_started_event(state: WorkerInput, started_at: str) -> dict[str, Any]:
    return {
        "event_type": "worker_started",
        "task_id": state["task_id"],
        "thread_id": state["thread_id"],
        "state_version": state["engine_state_version"],
        "dispatch_id": state["dispatch_id"],
        "node_id": state["node"]["node_id"],
        "started_at": started_at,
    }


def restore_engine_from_runtime_state(
    state: Mapping[str, Any],
    *,
    trace_writer: TraceWriter | None = None,
) -> OrchestrationEngine:
    """Restore an Engine from a validated runtime state for reporting/export."""

    value = state.get("engine")
    if not isinstance(value, Mapping):
        raise TypeError("LangGraph state has no serialized Engine")
    engine = OrchestrationEngine.restore(
        value["task_state"],
        value["task_graph"],
        value["blackboard"],
        trace_writer=trace_writer,
    )
    if state.get("task_id") != engine.task.task_id:
        raise ValueError("LangGraph state task_id is inconsistent")
    if int(state.get("engine_state_version", -1)) != engine.state_version:
        raise ValueError("LangGraph state_version is inconsistent")
    return engine


def _ready_node_ids(engine: OrchestrationEngine) -> tuple[str, ...]:
    return tuple(
        node.node_id for node in engine.graph.nodes if node.status is NodeStatus.READY
    )


def _human_approval(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("approved"), bool):
        return bool(value["approved"])
    raise ValueError("human response must be a boolean or contain boolean 'approved'")


def _runtime_status(engine: OrchestrationEngine) -> str:
    if engine.status is EngineStatus.ACTIVE:
        return "running"
    return engine.status.value
