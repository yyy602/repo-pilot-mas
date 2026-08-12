from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from repo_pilot_mas.orchestration import (
    DependencyPolicy,
    EngineBudget,
    NodeStatus,
    NodeType,
    OrchestrationEngine,
    TaskGraph,
    TaskNode,
)
from repo_pilot_mas.schemas import (
    Artifact,
    ArtifactType,
    CreateTaskRequest,
    DecisionAction,
    GateRecord,
    SupervisorDecision,
    TaskSpec,
)
from repo_pilot_mas.state import CheckpointStore


def _task(tmp_path: Path) -> TaskSpec:
    return TaskSpec(
        task_id="phase3-test",
        repository_path=tmp_path,
        issue="修复一个可复现缺陷",
        acceptance_criteria=("测试通过",),
    )


def _node(node_id: str, **overrides: object) -> TaskNode:
    values: dict[str, object] = {
        "node_id": node_id,
        "node_type": NodeType.INVESTIGATION_TASK,
        "agent_type": "InvestigatorAgent",
        "mode": "focused",
        "objective": f"调查 {node_id}",
    }
    values.update(overrides)
    return TaskNode(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "terminal",
    [NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.TIMED_OUT, NodeStatus.BLOCKED],
)
def test_running_node_reaches_each_worker_terminal_status(terminal: NodeStatus) -> None:
    graph = TaskGraph()
    graph.add_node(_node("N1"))

    graph.transition("N1", NodeStatus.RUNNING)
    graph.transition("N1", terminal, reason="test")

    assert graph.get("N1").status is terminal
    assert graph.get("N1").terminal


def test_pending_ready_and_running_nodes_can_be_cancelled() -> None:
    graph = TaskGraph()
    graph.add_node(_node("N1"))
    graph.add_node(_node("N2", dependencies=("N1",)))
    graph.add_node(_node("N3"))
    graph.transition("N3", NodeStatus.RUNNING)

    graph.transition("N2", NodeStatus.CANCELLED)
    graph.transition("N1", NodeStatus.CANCELLED)
    graph.transition("N3", NodeStatus.CANCELLED)

    assert {node.status for node in graph.nodes} == {NodeStatus.CANCELLED}


def test_illegal_state_transition_is_rejected() -> None:
    graph = TaskGraph()
    graph.add_node(_node("N1"))

    with pytest.raises(ValueError, match="illegal node transition"):
        graph.transition("N1", NodeStatus.SUCCEEDED)


def test_join_all_terminal_accepts_mixed_failure_and_timeout() -> None:
    graph = TaskGraph()
    graph.add_node(_node("N1"))
    graph.add_node(_node("N2"))
    graph.add_node(_node("N3"))
    graph.add_node(
        _node(
            "N4",
            node_type=NodeType.REVIEW_TASK,
            dependencies=("N1", "N2", "N3"),
            dependency_policy=DependencyPolicy.ALL_TERMINAL,
        )
    )

    graph.transition("N1", NodeStatus.RUNNING)
    graph.transition("N1", NodeStatus.SUCCEEDED)
    graph.transition("N2", NodeStatus.RUNNING)
    graph.transition("N2", NodeStatus.FAILED)
    graph.transition("N3", NodeStatus.RUNNING)
    graph.transition("N3", NodeStatus.TIMED_OUT)

    assert graph.get("N4").status is NodeStatus.READY


def test_all_succeeded_dependency_blocks_after_failure() -> None:
    graph = TaskGraph()
    graph.add_node(_node("N1"))
    graph.add_node(_node("N2", dependencies=("N1",)))

    graph.transition("N1", NodeStatus.RUNNING)
    graph.transition("N1", NodeStatus.FAILED)

    assert graph.get("N2").status is NodeStatus.BLOCKED


def test_artifact_store_is_append_only_and_revision_invalidates_consumers(
    tmp_path: Path,
) -> None:
    engine = OrchestrationEngine(_task(tmp_path))
    evidence_v1 = Artifact(
        "evidence-1",
        ArtifactType.EVIDENCE,
        "N0",
        {"finding": "old"},
    )
    engine.add_artifact(evidence_v1)
    create_consumer = SupervisorDecision(
        "D1",
        DecisionAction.CREATE_TASK,
        "分析证据",
        create_tasks=(
            CreateTaskRequest(
                "INVESTIGATION_TASK",
                "InvestigatorAgent",
                "code_retrieval",
                "基于证据形成假设",
                input_artifact_ids=(evidence_v1.artifact_id,),
            ),
        ),
        next_workflow_stage="investigation",
    )
    assert engine.apply_decision(create_consumer).ok
    assert engine.graph.get("N1").input_artifact_ids == (evidence_v1.ref,)
    create_successor = SupervisorDecision(
        "D2",
        DecisionAction.CREATE_TASK,
        "审查假设",
        create_tasks=(
            CreateTaskRequest(
                "INVESTIGATION_TASK",
                "InvestigatorAgent",
                "code_retrieval",
                "审查上游结论",
                depends_on=("N1",),
                input_artifact_ids=(evidence_v1.ref,),
            ),
        ),
        evidence_refs=(evidence_v1.ref,),
        gate_record=GateRecord(
            "revision-propagation-test",
            (evidence_v1.ref,),
            "测试第二个调查节点对上游修订的失效传播",
            1,
            "新增一个测试节点",
        ),
    )
    assert engine.apply_decision(create_successor).ok
    engine.start_node("N1")

    evidence_v2 = Artifact(
        "evidence-1",
        ArtifactType.EVIDENCE,
        "N0",
        {"finding": "corrected"},
        version=2,
        supersedes=evidence_v1.ref,
    )
    engine.add_artifact(evidence_v2)

    assert engine.blackboard.artifacts.get("evidence-1").ref == evidence_v2.ref
    assert engine.graph.get("N1").status is NodeStatus.CANCELLED
    assert engine.graph.get("N2").status is NodeStatus.CANCELLED
    with pytest.raises(ValueError, match="already exists"):
        engine.add_artifact(evidence_v2)
    with pytest.raises(ValueError, match="artifact_type"):
        engine.add_artifact(
            Artifact(
                "evidence-1",
                ArtifactType.HYPOTHESIS,
                "N0",
                {"root_cause": "invalid type change"},
                version=3,
                supersedes=evidence_v2.ref,
            )
        )
    with pytest.raises(TypeError):
        evidence_v1.content["finding"] = "mutated"  # type: ignore[index]


def test_timeout_retry_and_retry_budget(tmp_path: Path) -> None:
    engine = OrchestrationEngine(
        _task(tmp_path),
        budget=EngineBudget(max_retries_per_node=1),
    )
    decision = SupervisorDecision(
        "D-timeout",
        DecisionAction.CREATE_TASK,
        "创建超时任务",
        create_tasks=(
            CreateTaskRequest(
                "INVESTIGATION_TASK",
                "InvestigatorAgent",
                "focused",
                "执行超时调查",
                timeout_seconds=1,
            ),
        ),
    )
    assert engine.apply_decision(decision).ok
    engine.start_node("N1")
    engine.graph.get("N1").started_at = (
        datetime.now(timezone.utc) - timedelta(seconds=2)
    ).isoformat()

    assert engine.expire_timed_out_nodes() == ("N1",)
    engine.retry_node("N1")
    assert engine.graph.get("N1").status is NodeStatus.READY
    engine.start_node("N1")
    engine.finish_node("N1", NodeStatus.FAILED)
    with pytest.raises(ValueError, match="retry budget"):
        engine.retry_node("N1")


def test_checkpoint_roundtrip_preserves_all_engine_state(tmp_path: Path) -> None:
    engine = OrchestrationEngine(_task(tmp_path))
    assert engine.apply_decision(
        SupervisorDecision(
            "D-checkpoint",
            DecisionAction.CREATE_TASK,
            "保存任务图",
            create_tasks=(
                CreateTaskRequest(
                    "INVESTIGATION_TASK",
                    "InvestigatorAgent",
                    "focused",
                    "调查入口",
                ),
            ),
            next_workflow_stage="investigation",
        )
    ).ok
    router_state = {"exhausted_routes": ["model-a|KEY_1"]}
    store = CheckpointStore(tmp_path / "checkpoints")
    store.save(
        engine,
        "checkpoint-1",
        router_state=router_state,
        workspace_refs={"workspace_id": "ws-1"},
    )

    restored = store.load("checkpoint-1")

    assert restored.engine.to_task_state_dict() == engine.to_task_state_dict()
    assert restored.engine.graph.to_dict() == engine.graph.to_dict()
    assert restored.engine.blackboard.to_dict() == engine.blackboard.to_dict()
    assert restored.router_state == router_state
    assert restored.workspace_refs == {"workspace_id": "ws-1"}
    assert restored.trace_path.is_file()


def test_checkpoint_detects_tampering(tmp_path: Path) -> None:
    engine = OrchestrationEngine(_task(tmp_path))
    store = CheckpointStore(tmp_path / "checkpoints")
    checkpoint = store.save(engine, "checkpoint-1")
    (checkpoint / "task_state.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        store.load("checkpoint-1")
