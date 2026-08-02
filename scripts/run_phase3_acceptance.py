"""生成 Phase 3 确定性动态图、降级路径和检查点验收证据。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from repo_pilot_mas.agents import ScriptedSupervisor, SupervisorOutcome
from repo_pilot_mas.orchestration import (
    EngineStatus,
    FakeWorkerExecutor,
    LangGraphRuntime,
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
from repo_pilot_mas.state import CheckpointStore

_RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
_REPORT_ROOT = Path("reports/phase3/acceptance/scripted") / _RUN_ID


def main() -> int:
    _REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    dynamic_engine = _run_dynamic_graph()
    degradation_engine, rejection_code = _run_degradation()
    langgraph_report = _run_langgraph_acceptance()
    checkpoint_store = CheckpointStore(_REPORT_ROOT / "checkpoints")
    checkpoint_path = checkpoint_store.save(
        dynamic_engine,
        "scripted-dynamic-graph",
        router_state={"exhausted_routes": ["demo-model|DEMO_KEY_ENV"]},
        workspace_refs={"workspace_id": "phase3-demo"},
    )
    restored = checkpoint_store.load("scripted-dynamic-graph")
    checkpoint_consistent = (
        restored.engine.to_task_state_dict() == dynamic_engine.to_task_state_dict()
        and restored.engine.graph.to_dict() == dynamic_engine.graph.to_dict()
        and restored.engine.blackboard.to_dict() == dynamic_engine.blackboard.to_dict()
    )
    report = {
        "phase": 3,
        "scripted_dynamic_graph": {
            "engine_status": dynamic_engine.status.value,
            "workflow_stage": dynamic_engine.blackboard.workflow_stage,
            "state_version": dynamic_engine.state_version,
            "nodes": [node.to_dict() for node in dynamic_engine.graph.nodes],
            "trace_path": str(_REPORT_ROOT / "dynamic_graph_trace.jsonl"),
        },
        "degradation": {
            "illegal_decision_code": rejection_code,
            "timed_out_nodes": [
                node.node_id
                for node in degradation_engine.graph.nodes
                if node.status is NodeStatus.TIMED_OUT
            ],
            "trace_path": str(_REPORT_ROOT / "degradation_trace.jsonl"),
        },
        "checkpoint": {
            "consistent": checkpoint_consistent,
            "path": str(checkpoint_path),
            "router_state": dict(restored.router_state),
        },
        "langgraph_runtime": langgraph_report,
    }
    report_path = _REPORT_ROOT / "scripted_acceptance.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    langgraph_passed = all(
        (
            langgraph_report["automatic_loop"]["passed"],
            langgraph_report["restart_recovery"]["passed"],
            langgraph_report["human_interrupt"]["passed"],
        )
    )
    return 0 if checkpoint_consistent and langgraph_passed else 1


def _task(task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        repository_path=Path.cwd(),
        issue="验证 Phase 3 动态编排内核",
        acceptance_criteria=("动态图可复现", "失败可降级", "检查点一致"),
    )


def _run_dynamic_graph() -> OrchestrationEngine:
    trace = TraceWriter(_REPORT_ROOT / "dynamic_graph_trace.jsonl")
    engine = OrchestrationEngine(_task("phase3-scripted-dynamic"), trace_writer=trace)
    create_fork = SupervisorDecision(
        "D1",
        DecisionAction.CREATE_TASK,
        "并行收集代码与复现证据",
        create_tasks=(
            CreateTaskRequest(
                "INVESTIGATION_TASK",
                "InvestigatorAgent",
                "code_retrieval",
                "定位失败路径",
            ),
            CreateTaskRequest(
                "INVESTIGATION_TASK",
                "InvestigatorAgent",
                "failure_reproduction",
                "独立复现失败",
                critical=False,
            ),
        ),
        next_workflow_stage="investigation",
    )
    supervisor = ScriptedSupervisor((create_fork,))
    assert engine.run_supervisor(supervisor).ok
    evidence = Artifact(
        "E1",
        ArtifactType.EVIDENCE,
        "N1",
        {"claim": "已定位失败入口", "status": "verified"},
    )
    engine.start_node("N1")
    engine.start_node("N2")
    engine.add_artifact(evidence)
    engine.finish_node("N1", NodeStatus.SUCCEEDED, artifact_refs=(evidence.ref,))
    engine.finish_node("N2", NodeStatus.TIMED_OUT, reason="复现任务超时")
    create_join = SupervisorDecision(
        "D2",
        DecisionAction.CREATE_TASK,
        "混合终态后进入证据审查",
        create_tasks=(
            CreateTaskRequest(
                "REVIEW_TASK",
                "ReviewerAgent",
                "evidence_join",
                "审查已完成的调查结果",
                depends_on=("N1", "N2"),
                input_artifact_ids=(evidence.ref,),
                dependency_policy="all_terminal",
            ),
        ),
    )
    assert engine.apply_decision(create_join).ok
    engine.start_node("N3")
    engine.finish_node("N3", NodeStatus.SUCCEEDED)
    revised = Artifact(
        "E1",
        ArtifactType.EVIDENCE,
        "N1",
        {"claim": "失败入口证据已修订", "status": "verified"},
        version=2,
        status="revised",
        supersedes=evidence.ref,
    )
    create_consumer = SupervisorDecision(
        "D3",
        DecisionAction.CREATE_TASK,
        "基于当前证据形成诊断",
        create_tasks=(
            CreateTaskRequest(
                "DIAGNOSIS_TASK",
                "DiagnosisAgent",
                "focused",
                "形成根因假设",
                input_artifact_ids=(evidence.ref,),
            ),
        ),
    )
    assert engine.apply_decision(create_consumer).ok
    engine.add_artifact(revised)
    assert engine.graph.get("N4").status is NodeStatus.CANCELLED
    return engine


def _run_degradation() -> tuple[OrchestrationEngine, str]:
    trace = TraceWriter(_REPORT_ROOT / "degradation_trace.jsonl")
    engine = OrchestrationEngine(_task("phase3-degradation"), trace_writer=trace)
    illegal = SupervisorDecision(
        "D-illegal",
        DecisionAction.CREATE_TASK,
        "故意验证非法依赖拒绝路径",
        create_tasks=(
            CreateTaskRequest(
                "DIAGNOSIS_TASK",
                "DiagnosisAgent",
                "focused",
                "非法后继",
                depends_on=("N404",),
            ),
        ),
    )
    rejection = engine.apply_decision(illegal)
    timeout = SupervisorDecision(
        "D-timeout",
        DecisionAction.CREATE_TASK,
        "验证超时降级路径",
        create_tasks=(
            CreateTaskRequest(
                "INVESTIGATION_TASK",
                "InvestigatorAgent",
                "failure_reproduction",
                "运行有界复现",
                timeout_seconds=1,
            ),
        ),
    )
    assert engine.apply_decision(timeout).ok
    engine.start_node("N1")
    engine.graph.get("N1").started_at = (
        datetime.now(timezone.utc) - timedelta(seconds=2)
    ).isoformat()
    assert engine.expire_timed_out_nodes() == ("N1",)
    assert engine.status is EngineStatus.ACTIVE
    return engine, rejection.code


class _StateAwareSupervisor:
    def __init__(self, worker_count: int = 1) -> None:
        self.worker_count = worker_count

    def decide(self, snapshot: Mapping[str, Any]) -> SupervisorOutcome:
        if not snapshot["nodes"]:
            tasks = tuple(
                CreateTaskRequest(
                    "INVESTIGATION_TASK",
                    "InvestigatorAgent",
                    "focused",
                    f"LangGraph 调查 {index}",
                )
                for index in range(1, self.worker_count + 1)
            )
            decision = SupervisorDecision(
                "LG-create",
                DecisionAction.CREATE_TASK,
                "创建运行时验收任务",
                create_tasks=tasks,
                next_workflow_stage="investigation",
            )
        else:
            decision = SupervisorDecision(
                "LG-stop",
                DecisionAction.TERMINATE_TASK,
                "Phase 3 运行时验收完成",
            )
        return SupervisorOutcome(decision)


class _RecoverableWorker:
    def __init__(self, calls: list[str], fail_node_ids: set[str]) -> None:
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
            f"{node_id}-runtime-evidence",
            ArtifactType.EVIDENCE,
            node_id,
            {"status": "verified"},
        )
        return WorkerOutcome(node_id, NodeStatus.SUCCEEDED, (artifact,))


def _run_langgraph_acceptance() -> dict[str, Any]:
    automatic_trace = TraceWriter(_REPORT_ROOT / "langgraph_automatic_trace.jsonl")
    automatic_worker = FakeWorkerExecutor()
    with LangGraphRuntime(
        _StateAwareSupervisor(),
        automatic_worker,
        _REPORT_ROOT / "langgraph_automatic.sqlite3",
        trace_writer=automatic_trace,
    ) as runtime:
        updates = list(
            runtime.stream(
                OrchestrationEngine(_task("phase3-langgraph-automatic")),
                thread_id="phase3-langgraph-automatic",
            )
        )
        automatic_state = runtime.get_state(
            thread_id="phase3-langgraph-automatic",
            task_id="phase3-langgraph-automatic",
        )
    automatic_engine = restore_engine_from_runtime_state(automatic_state)
    automatic_events = [next(iter(update)) for update in updates]
    automatic_passed = (
        automatic_state["runtime_status"] == "terminated"
        and automatic_worker.calls == ["N1"]
        and automatic_events.count("supervisor") == 2
        and "worker" in automatic_events
        and "collector" in automatic_events
        and len(automatic_engine.blackboard.artifacts.latest_values()) == 1
    )

    restart_calls: list[str] = []
    restart_db = _REPORT_ROOT / "langgraph_restart.sqlite3"
    crash_observed = False
    try:
        with LangGraphRuntime(
            _StateAwareSupervisor(worker_count=2),
            _RecoverableWorker(restart_calls, {"N2"}),
            restart_db,
        ) as runtime:
            runtime.run(
                OrchestrationEngine(_task("phase3-langgraph-restart")),
                thread_id="phase3-langgraph-restart",
            )
    except RuntimeError as exc:
        crash_observed = str(exc) == "planned worker crash: N2"
    with LangGraphRuntime(
        _StateAwareSupervisor(worker_count=2),
        _RecoverableWorker(restart_calls, set()),
        restart_db,
    ) as runtime:
        restart_state = runtime.resume(
            thread_id="phase3-langgraph-restart",
            task_id="phase3-langgraph-restart",
        )
    restart_engine = restore_engine_from_runtime_state(restart_state)
    restart_passed = (
        crash_observed
        and restart_calls.count("N1") == 1
        and restart_calls.count("N2") == 2
        and all(node.status is NodeStatus.SUCCEEDED for node in restart_engine.graph.nodes)
    )

    human_worker = FakeWorkerExecutor()
    human_db = _REPORT_ROOT / "langgraph_human.sqlite3"
    with LangGraphRuntime(_StateAwareSupervisor(), human_worker, human_db) as runtime:
        runtime.run(
            OrchestrationEngine(_task("phase3-langgraph-human")),
            thread_id="phase3-langgraph-human",
            require_human_approval=True,
        )
        snapshot = runtime.graph.get_state(
            {"configurable": {"thread_id": "phase3-langgraph-human"}}
        )
        interrupted = bool(snapshot.tasks and snapshot.tasks[0].interrupts)
        worker_calls_before_resume = list(human_worker.calls)
    with LangGraphRuntime(_StateAwareSupervisor(), human_worker, human_db) as runtime:
        human_state = runtime.resume(
            thread_id="phase3-langgraph-human",
            task_id="phase3-langgraph-human",
            human_response=True,
        )
    human_passed = (
        interrupted
        and not worker_calls_before_resume
        and human_worker.calls == ["N1"]
        and human_state["runtime_status"] == "terminated"
    )

    return {
        "automatic_loop": {
            "passed": automatic_passed,
            "events": automatic_events,
            "worker_calls": list(automatic_worker.calls),
            "state_version": automatic_engine.state_version,
        },
        "restart_recovery": {
            "passed": restart_passed,
            "crash_observed": crash_observed,
            "worker_calls": restart_calls,
            "completed_node_ids": [
                node.node_id
                for node in restart_engine.graph.nodes
                if node.status is NodeStatus.SUCCEEDED
            ],
        },
        "human_interrupt": {
            "passed": human_passed,
            "interrupt_observed": interrupted,
            "worker_calls_before_resume": worker_calls_before_resume,
            "worker_calls_after_resume": list(human_worker.calls),
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
