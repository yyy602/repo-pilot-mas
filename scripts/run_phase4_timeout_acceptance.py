"""生成 Phase 4 单 Worker 超时隔离的确定性 LangGraph 验收证据。"""

from __future__ import annotations

import asyncio
import json
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
from repo_pilot_mas.runtime import TraceWriter
from repo_pilot_mas.schemas import (
    Artifact,
    ArtifactType,
    CreateTaskRequest,
    DecisionAction,
    SupervisorDecision,
    TaskSpec,
)


class _TimeoutSupervisor:
    def decide(self, snapshot: Mapping[str, Any]) -> SupervisorOutcome:
        if not snapshot["nodes"]:
            decision = SupervisorDecision(
                "timeout-create",
                DecisionAction.CREATE_TASK,
                "并发创建一个正常 Worker 和一个超时 Worker",
                create_tasks=tuple(
                    CreateTaskRequest(
                        "INVESTIGATION_TASK",
                        "InvestigatorAgent",
                        "code_retrieval",
                        f"超时隔离验收 {index}",
                        timeout_seconds=0.05,
                    )
                    for index in (1, 2)
                ),
                next_workflow_stage="investigation",
            )
        else:
            decision = SupervisorDecision(
                "timeout-stop",
                DecisionAction.TERMINATE_TASK,
                "Collector 已提交混合终态，Supervisor 正常恢复决策",
            )
        return SupervisorOutcome(decision, model_id="deterministic-timeout-supervisor")


class _TimeoutExecutor:
    def execute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        del task, node, artifacts
        raise AssertionError("async runtime must call aexecute")

    async def aexecute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        del task, artifacts
        node_id = str(node["node_id"])
        await asyncio.sleep(0.01 if node_id == "N1" else 0.20)
        artifact = Artifact(
            f"{node_id}.evidence",
            ArtifactType.EVIDENCE,
            node_id,
            {"claim": "确定性超时验收"},
        )
        return WorkerOutcome(node_id, NodeStatus.SUCCEEDED, (artifact,))


async def _run(report_root: Path) -> bool:
    report_root.mkdir(parents=True, exist_ok=True)
    trace = TraceWriter(report_root / "trace.jsonl")
    task = TaskSpec(
        "phase4-timeout-isolation",
        Path.cwd(),
        "验证一个 Worker 超时不会使 Engine 崩溃",
    )
    runtime = await AsyncLangGraphRuntime.create(
        _TimeoutSupervisor(),
        _TimeoutExecutor(),
        report_root / "langgraph.sqlite3",
        trace_writer=trace,
    )
    async with runtime:
        state = await runtime.arun(
            OrchestrationEngine(task, trace_writer=trace),
            thread_id="phase4-timeout-thread",
        )
    engine = restore_engine_from_runtime_state(state)
    statuses = {node.node_id: node.status.value for node in engine.graph.nodes}
    events = [
        json.loads(line)["event_type"]
        for line in trace.path.read_text(encoding="utf-8").splitlines()
    ]
    gates = {
        "fast_worker_succeeded": statuses.get("N1") == NodeStatus.SUCCEEDED.value,
        "slow_worker_timed_out": statuses.get("N2") == NodeStatus.TIMED_OUT.value,
        "engine_remained_operational": state["runtime_status"] == "terminated",
        "collector_woke_supervisor": events.count("worker_artifacts_collected") == 1
        and events[-1] == "supervisor_decision_applied",
    }
    report = {
        "phase": 4,
        "scenario": "worker_timeout_isolation",
        "passed": all(gates.values()),
        "gates": gates,
        "node_statuses": statuses,
        "runtime_status": state["runtime_status"],
        "trace_path": str(trace.path),
        "checkpoint_db": str(report_root / "langgraph.sqlite3"),
    }
    (report_root / "acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return bool(report["passed"])


def main() -> int:
    passed = asyncio.run(_run(Path("reports/phase4/timeout_acceptance")))
    print(json.dumps({"passed": passed}, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
