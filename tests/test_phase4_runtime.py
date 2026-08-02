from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from repo_pilot_mas.agents import SupervisorOutcome
from repo_pilot_mas.orchestration import (
    AsyncLangGraphRuntime,
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


class ParallelSupervisor:
    def __init__(self, *, timeout_seconds: float = 1.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.snapshots: list[Mapping[str, Any]] = []

    def decide(self, snapshot: Mapping[str, Any]) -> SupervisorOutcome:
        self.snapshots.append(snapshot)
        if not snapshot["nodes"]:
            decision = SupervisorDecision(
                "phase4-create",
                DecisionAction.CREATE_TASK,
                "并发创建两个调查节点",
                create_tasks=tuple(
                    CreateTaskRequest(
                        "INVESTIGATION_TASK",
                        "InvestigatorAgent",
                        "code_retrieval",
                        f"调查 {index}",
                        timeout_seconds=self.timeout_seconds,
                    )
                    for index in (1, 2)
                ),
                next_workflow_stage="investigation",
            )
        else:
            decision = SupervisorDecision(
                "phase4-stop",
                DecisionAction.TERMINATE_TASK,
                "Worker 收集完成后由 Supervisor 终止",
            )
        return SupervisorOutcome(decision)


class DelayedExecutor:
    def __init__(self, delays: Mapping[str, float]) -> None:
        self.delays = dict(delays)
        self.intervals: dict[str, tuple[float, float]] = {}

    def execute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        del task, node, artifacts
        raise AssertionError("async runtime must use aexecute")

    async def aexecute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        del task, artifacts
        node_id = str(node["node_id"])
        started = time.perf_counter()
        await asyncio.sleep(self.delays[node_id])
        finished = time.perf_counter()
        self.intervals[node_id] = (started, finished)
        artifact = Artifact(
            f"{node_id}.evidence",
            ArtifactType.EVIDENCE,
            node_id,
            {"claim": "async test"},
        )
        return WorkerOutcome(node_id, NodeStatus.SUCCEEDED, (artifact,))


def _task(tmp_path: Path, task_id: str) -> TaskSpec:
    return TaskSpec(task_id, tmp_path, "验证 Phase 4 asyncio Dispatcher")


def test_async_dispatch_has_real_time_overlap_and_collector_wakes_supervisor(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        from repo_pilot_mas.runtime import TraceWriter

        supervisor = ParallelSupervisor()
        executor = DelayedExecutor({"N1": 0.08, "N2": 0.08})
        trace_path = tmp_path / "parallel.jsonl"
        runtime = await AsyncLangGraphRuntime.create(
            supervisor,
            executor,
            tmp_path / "parallel.sqlite3",
            trace_writer=TraceWriter(trace_path),
        )
        async with runtime:
            state = await runtime.arun(
                OrchestrationEngine(_task(tmp_path, "parallel-task")),
                thread_id="parallel-thread",
            )
            history = await runtime.astate_history(
                thread_id="parallel-thread",
                task_id="parallel-task",
            )

        n1 = executor.intervals["N1"]
        n2 = executor.intervals["N2"]
        assert max(n1[0], n2[0]) < min(n1[1], n2[1])
        restored = restore_engine_from_runtime_state(state)
        assert all(node.status is NodeStatus.SUCCEEDED for node in restored.graph.nodes)
        assert len(supervisor.snapshots) == 2
        assert len(history) >= 6
        assert len(supervisor.snapshots[1]["artifacts"]) == 2
        events = [json.loads(line)["event_type"] for line in trace_path.read_text().splitlines()]
        assert events.count("worker_started") == 2
        assert events.count("worker_completed") == 2
        assert events.index("worker_artifacts_collected") < events.index(
            "supervisor_decision_applied", events.index("worker_artifacts_collected")
        )

    asyncio.run(scenario())


def test_one_async_worker_timeout_does_not_crash_engine(tmp_path: Path) -> None:
    async def scenario() -> None:
        supervisor = ParallelSupervisor(timeout_seconds=0.03)
        executor = DelayedExecutor({"N1": 0.01, "N2": 0.20})
        runtime = await AsyncLangGraphRuntime.create(
            supervisor,
            executor,
            tmp_path / "timeout.sqlite3",
        )
        async with runtime:
            state = await runtime.arun(
                OrchestrationEngine(_task(tmp_path, "timeout-task")),
                thread_id="timeout-thread",
            )
        restored = restore_engine_from_runtime_state(state)
        statuses = {node.node_id: node.status for node in restored.graph.nodes}
        assert statuses == {"N1": NodeStatus.SUCCEEDED, "N2": NodeStatus.TIMED_OUT}
        assert state["runtime_status"] == "terminated"
        assert len(supervisor.snapshots) == 2

    asyncio.run(scenario())
