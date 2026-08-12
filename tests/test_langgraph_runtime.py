from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from repo_pilot_mas.agents import SupervisorOutcome
from repo_pilot_mas.orchestration import (
    FakeWorkerExecutor,
    LangGraphRuntime,
    NodeStatus,
    OrchestrationEngine,
    WorkerOutcome,
    restore_engine_from_runtime_state,
)
from repo_pilot_mas.schemas import (
    Artifact,
    ArtifactType,
    CreateTaskRequest,
    DecisionAction,
    SupervisorDecision,
    TaskSpec,
)
from repo_pilot_mas.state import CheckpointStore


def _task(tmp_path: Path, task_id: str = "langgraph-test") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        repository_path=tmp_path,
        issue="验证 Supervisor 主导的持久化运行循环",
        acceptance_criteria=("运行时可恢复",),
    )


class StateAwareSupervisor:
    """Stateless decisions model how the real Supervisor reads persisted snapshots."""

    def __init__(self, *, worker_count: int = 1) -> None:
        self.worker_count = worker_count
        self.snapshots: list[Mapping[str, Any]] = []

    def decide(self, snapshot: Mapping[str, Any]) -> SupervisorOutcome:
        self.snapshots.append(snapshot)
        if not snapshot["nodes"]:
            tasks = tuple(
                CreateTaskRequest(
                    "INVESTIGATION_TASK",
                    "InvestigatorAgent",
                    "code_retrieval",
                    f"执行调查 {index}",
                )
                for index in range(1, self.worker_count + 1)
            )
            decision = SupervisorDecision(
                "runtime-create",
                DecisionAction.CREATE_TASK,
                "创建可执行调查任务",
                create_tasks=tasks,
                next_workflow_stage="investigation",
            )
        else:
            decision = SupervisorDecision(
                "runtime-stop",
                DecisionAction.TERMINATE_TASK,
                "Phase 3 运行时闭环验收结束",
            )
        return SupervisorOutcome(decision)


class RecoverableWorker:
    def __init__(self, calls: list[str], *, fail_node_ids: set[str]) -> None:
        self.calls = calls
        self.fail_node_ids = fail_node_ids

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
        if node_id in self.fail_node_ids:
            raise RuntimeError(f"planned worker crash: {node_id}")
        artifact = Artifact(
            f"{node_id}-result",
            ArtifactType.EVIDENCE,
            node_id,
            {
                "mode": "code_retrieval",
                "evidence_kind": "source",
                "claim": "restart test evidence",
                "supports_claims": ["restart test evidence"],
                "contradicts_claims": [],
                "verified": True,
                "source": {"path": "worker.py", "line_start": 1, "line_end": 1},
                "content": "deterministic restart evidence",
                "observation_type": "direct",
                "confidence": 1.0,
                "status": "verified",
                "tool_trace_ids": [f"restart-tool-{node_id}"],
                "missing_evidence": [],
            },
        )
        return WorkerOutcome(node_id, NodeStatus.SUCCEEDED, (artifact,))


def test_supervisor_led_loop_streams_worker_and_artifact_events(tmp_path: Path) -> None:
    supervisor = StateAwareSupervisor()
    worker = FakeWorkerExecutor()
    engine = OrchestrationEngine(_task(tmp_path))
    trace_path = tmp_path / "runtime.jsonl"

    from repo_pilot_mas.runtime import TraceWriter

    with LangGraphRuntime(
        supervisor,
        worker,
        tmp_path / "runtime.sqlite3",
        trace_writer=TraceWriter(trace_path),
    ) as runtime:
        updates = list(runtime.stream(engine, thread_id="loop-thread"))
        state = runtime.get_state(thread_id="loop-thread", task_id=engine.task.task_id)
        history = runtime.state_history(thread_id="loop-thread", task_id=engine.task.task_id)

    node_names = [next(iter(update)) for update in updates]
    restored = restore_engine_from_runtime_state(state)
    assert node_names == [
        "validate",
        "supervisor",
        "prepare_dispatch",
        "worker",
        "collector",
        "supervisor",
    ]
    assert worker.calls == ["N1"]
    assert len(supervisor.snapshots) == 2
    assert supervisor.snapshots[1]["artifacts"][0]["artifact_type"] == "evidence"
    assert restored.status.value == "terminated"
    assert state["runtime_status"] == "terminated"
    assert len(history) >= len(updates)
    trace_text = trace_path.read_text(encoding="utf-8")
    assert "worker_completed" in trace_text
    assert "worker_artifacts_collected" in trace_text


def test_sqlite_restart_retries_only_failed_parallel_worker(tmp_path: Path) -> None:
    calls: list[str] = []
    database = tmp_path / "restart.sqlite3"
    engine = OrchestrationEngine(_task(tmp_path, "restart-task"))

    with pytest.raises(RuntimeError, match="planned worker crash: N2"), LangGraphRuntime(
        StateAwareSupervisor(worker_count=2),
        RecoverableWorker(calls, fail_node_ids={"N2"}),
        database,
    ) as runtime:
        runtime.run(engine, thread_id="restart-thread")

    with LangGraphRuntime(
        StateAwareSupervisor(worker_count=2),
        RecoverableWorker(calls, fail_node_ids=set()),
        database,
    ) as runtime:
        state = runtime.resume(thread_id="restart-thread", task_id="restart-task")

    assert calls.count("N1") == 1
    assert calls.count("N2") == 2
    restored = restore_engine_from_runtime_state(state)
    assert {node.status for node in restored.graph.nodes} == {NodeStatus.SUCCEEDED}
    assert state["runtime_status"] == "terminated"


def test_human_interrupt_survives_new_runtime_and_can_resume(tmp_path: Path) -> None:
    worker = FakeWorkerExecutor()
    database = tmp_path / "human.sqlite3"
    engine = OrchestrationEngine(_task(tmp_path, "human-task"))

    with LangGraphRuntime(StateAwareSupervisor(), worker, database) as runtime:
        paused = runtime.run(
            engine,
            thread_id="human-thread",
            require_human_approval=True,
        )
        snapshot = runtime.graph.get_state(
            {"configurable": {"thread_id": "human-thread"}}
        )
        assert snapshot.next == ("human_review",)
        assert snapshot.tasks[0].interrupts
        assert paused["runtime_status"] == "running"
        assert worker.calls == []

    with LangGraphRuntime(StateAwareSupervisor(), worker, database) as runtime:
        completed = runtime.resume(
            thread_id="human-thread",
            task_id="human-task",
            human_response={"approved": True},
        )

    assert worker.calls == ["N1"]
    assert completed["runtime_status"] == "terminated"


def test_human_rejection_stops_dispatch_without_mutating_engine_terminal_state(
    tmp_path: Path,
) -> None:
    worker = FakeWorkerExecutor()
    engine = OrchestrationEngine(_task(tmp_path, "rejected-task"))

    with LangGraphRuntime(
        StateAwareSupervisor(),
        worker,
        tmp_path / "rejected.sqlite3",
    ) as runtime:
        runtime.run(engine, thread_id="rejected-thread", require_human_approval=True)
        state = runtime.resume(
            thread_id="rejected-thread",
            task_id="rejected-task",
            human_response=False,
        )

    restored = restore_engine_from_runtime_state(state)
    assert state["runtime_status"] == "human_rejected"
    assert restored.status.value == "active"
    assert restored.graph.get("N1").status is NodeStatus.READY
    assert worker.calls == []


def test_runtime_rejects_task_thread_and_state_version_mismatches(tmp_path: Path) -> None:
    engine = OrchestrationEngine(_task(tmp_path, "binding-task"))
    database = tmp_path / "binding.sqlite3"

    with LangGraphRuntime(StateAwareSupervisor(), FakeWorkerExecutor(), database) as runtime:
        runtime.run(
            engine,
            thread_id="binding-thread",
            require_human_approval=True,
        )
        state = runtime.get_state(thread_id="binding-thread", task_id="binding-task")
        with pytest.raises(ValueError, match="task_id"):
            runtime.assert_thread_binding(
                thread_id="binding-thread",
                task_id="another-task",
            )
        with pytest.raises(ValueError, match="state_version"):
            runtime.assert_thread_binding(
                thread_id="binding-thread",
                task_id="binding-task",
                expected_state_version=int(state["engine_state_version"]) + 1,
            )
        with pytest.raises(ValueError, match="does not exist"):
            runtime.assert_thread_binding(
                thread_id="another-thread",
                task_id="binding-task",
            )
        runtime.graph.update_state(
            {"configurable": {"thread_id": "binding-thread"}},
            {"thread_id": "tampered-thread"},
        )
        with pytest.raises(ValueError, match="thread_id"):
            runtime.assert_thread_binding(
                thread_id="binding-thread",
                task_id="binding-task",
            )


def test_audit_checkpoint_binding_matches_langgraph_state(tmp_path: Path) -> None:
    engine = OrchestrationEngine(_task(tmp_path, "audit-task"))
    database = tmp_path / "audit.sqlite3"

    with LangGraphRuntime(StateAwareSupervisor(), FakeWorkerExecutor(), database) as runtime:
        runtime.run(
            engine,
            thread_id="audit-thread",
            require_human_approval=True,
        )
        state = runtime.get_state(thread_id="audit-thread", task_id="audit-task")
    restored = restore_engine_from_runtime_state(state)
    store = CheckpointStore(tmp_path / "audit")
    store.save(restored, "snapshot", thread_id="audit-thread")

    bundle = store.load(
        "snapshot",
        expected_task_id="audit-task",
        expected_thread_id="audit-thread",
        expected_state_version=restored.state_version,
    )
    assert bundle.thread_id == "audit-thread"
    with pytest.raises(ValueError, match="thread_id"):
        store.load("snapshot", expected_thread_id="other-thread")
    with pytest.raises(ValueError, match="state_version"):
        store.load("snapshot", expected_state_version=restored.state_version + 1)


def test_runtime_rejects_duplicate_thread_and_invalid_worker_outcome(tmp_path: Path) -> None:
    engine = OrchestrationEngine(_task(tmp_path, "duplicate-task"))
    database = tmp_path / "duplicate.sqlite3"
    worker = FakeWorkerExecutor()

    with LangGraphRuntime(StateAwareSupervisor(), worker, database) as runtime:
        runtime.run(
            engine,
            thread_id="duplicate-thread",
            require_human_approval=True,
        )
        with pytest.raises(ValueError, match="already exists"):
            runtime.run(
                OrchestrationEngine(_task(tmp_path, "duplicate-task")),
                thread_id="duplicate-thread",
            )

    with pytest.raises(ValueError, match="at least one artifact"):
        WorkerOutcome("N1", NodeStatus.SUCCEEDED)
