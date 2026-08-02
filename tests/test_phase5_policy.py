from __future__ import annotations

from pathlib import Path

from repo_pilot_mas.orchestration import (
    EngineBudget,
    EngineStatus,
    ExecutionPath,
    NodeType,
    OrchestrationEngine,
    TaskNode,
    classify_execution_path,
    hypotheses_materially_different,
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


def _evidence() -> Artifact:
    return Artifact("E1", ArtifactType.EVIDENCE, "N1", {"claim": "直接证据"})


def test_dynamic_expansion_requires_auditable_gate(tmp_path: Path) -> None:
    engine = OrchestrationEngine(TaskSpec("gate", tmp_path, "验证动态门"))
    evidence = _evidence()
    engine.add_artifact(evidence)
    engine.blackboard.set_stage("diagnosis")
    tasks = tuple(
        CreateTaskRequest(
            "DIAGNOSIS_TASK",
            "DiagnosticianAgent",
            perspective,
            f"按 {perspective} 诊断",
            input_artifact_ids=(evidence.ref,),
        )
        for perspective in ("control_flow", "data_flow")
    )
    rejected = engine.apply_decision(
        SupervisorDecision("D-no-gate", DecisionAction.CREATE_TASK, "扩展诊断", create_tasks=tasks)
    )
    assert rejected.code == "INVALID_SUPERVISOR_DECISION"
    accepted = engine.apply_decision(
        SupervisorDecision(
            "D-gated",
            DecisionAction.CREATE_TASK,
            "证据存在两条因果解释，增加独立诊断",
            create_tasks=tasks,
            evidence_refs=(evidence.ref,),
            gate_record=GateRecord(
                "additional_diagnosis",
                (evidence.ref,),
                "证据支持控制流和数据流两种解释",
                2,
                "新增 2 个节点，占用 2 次 Worker 调用",
            ),
        )
    )
    assert accepted.ok


def test_replan_stage_is_classified_and_budget_exhaustion_terminates(tmp_path: Path) -> None:
    engine = OrchestrationEngine(
        TaskSpec("replan", tmp_path, "验证定向重规划"),
        budget=EngineBudget(max_replans=1),
    )
    evidence = _evidence()
    engine.add_artifact(evidence)
    engine.blackboard.set_stage("validation")
    wrong = engine.apply_decision(
        SupervisorDecision(
            "D-wrong-target",
            DecisionAction.REQUEST_REPLAN,
            "两个补丁均未解释目标失败",
            next_workflow_stage="patch",
            evidence_refs=(evidence.ref,),
            failure_class="both_patches_fail_target",
        )
    )
    assert wrong.code == "INVALID_SUPERVISOR_DECISION"
    accepted = engine.apply_decision(
        SupervisorDecision(
            "D-replan",
            DecisionAction.REQUEST_REPLAN,
            "两个补丁均未解释目标失败，返回诊断层",
            next_workflow_stage="diagnosis",
            evidence_refs=(evidence.ref,),
            failure_class="both_patches_fail_target",
        )
    )
    assert accepted.ok
    assert engine.graph.nodes[-1].node_type is NodeType.REPLAN_TASK
    assert engine.blackboard.artifacts.get("replan.1").content["target_stage"] == "diagnosis"
    engine.blackboard.set_stage("validation")
    exhausted = engine.apply_decision(
        SupervisorDecision(
            "D-replan-again",
            DecisionAction.REQUEST_REPLAN,
            "再次失败",
            next_workflow_stage="patch",
            evidence_refs=(evidence.ref,),
            failure_class="regression_failure",
        )
    )
    assert exhausted.code == "INVALID_SUPERVISOR_DECISION"
    assert engine.status is EngineStatus.FAILED


def test_path_class_uses_actual_nodes_and_hypothesis_conflict_is_structural() -> None:
    def node(index: int, node_type: NodeType) -> TaskNode:
        return TaskNode(f"N{index}", node_type, "Agent", "mode", "objective")

    assert classify_execution_path(
        (node(1, NodeType.INVESTIGATION_TASK), node(2, NodeType.PATCH_TASK))
    ) is ExecutionPath.FAST
    assert classify_execution_path(
        (
            node(1, NodeType.INVESTIGATION_TASK),
            node(2, NodeType.DIAGNOSIS_TASK),
            node(3, NodeType.DIAGNOSIS_TASK),
        )
    ) is ExecutionPath.STANDARD
    assert classify_execution_path(
        (
            node(1, NodeType.INVESTIGATION_TASK),
            node(2, NodeType.CHALLENGE_TASK),
        )
    ) is ExecutionPath.DEEP
    assert hypotheses_materially_different(
        {
            "root_cause": "缓存键遗漏租户",
            "direct_cause": "共享缓存命中",
            "affected_symbols": ["cache_key"],
        },
        {
            "root_cause": "异步任务提前提交",
            "direct_cause": "事务外读取",
            "affected_symbols": ["commit_job"],
        },
    )


def test_engine_limits_challenge_round_and_patch_revision(tmp_path: Path) -> None:
    engine = OrchestrationEngine(TaskSpec("bounded", tmp_path, "验证对抗轮次上限"))
    evidence = _evidence()
    hypothesis = Artifact("H1", ArtifactType.HYPOTHESIS, "N1", {"root_cause": "根因"})
    engine.add_artifact(evidence)
    engine.add_artifact(hypothesis)
    engine.blackboard.set_stage("diagnosis")
    first = engine.apply_decision(
        SupervisorDecision(
            "D-challenge-1",
            DecisionAction.CREATE_TASK,
            "创建唯一一轮 Challenge",
            create_tasks=(
                CreateTaskRequest(
                    "CHALLENGE_TASK",
                    "DiagnosticianAgent",
                    "challenge",
                    "质疑根因",
                    input_artifact_ids=(hypothesis.ref, evidence.ref),
                ),
            ),
            evidence_refs=(hypothesis.ref,),
            gate_record=GateRecord(
                "adversarial_review",
                (hypothesis.ref,),
                "根因存在具体反例",
                1,
                "新增 1 个节点",
            ),
        )
    )
    assert first.ok
    second = engine.apply_decision(
        SupervisorDecision(
            "D-challenge-2",
            DecisionAction.CREATE_TASK,
            "尝试创建第二轮 Challenge",
            create_tasks=(
                CreateTaskRequest(
                    "CHALLENGE_TASK",
                    "DiagnosticianAgent",
                    "challenge",
                    "再次质疑根因",
                    input_artifact_ids=(hypothesis.ref, evidence.ref),
                ),
            ),
            evidence_refs=(hypothesis.ref,),
            gate_record=GateRecord(
                "adversarial_review",
                (hypothesis.ref,),
                "重复一轮",
                1,
                "新增 1 个节点",
            ),
        )
    )
    assert second.code == "INVALID_SUPERVISOR_DECISION"

    patch1 = Artifact("P1", ArtifactType.PATCH_CANDIDATE, "N2", {"diff": "v1"})
    patch2 = Artifact(
        "P1",
        ArtifactType.PATCH_CANDIDATE,
        "N3",
        {"diff": "v2"},
        version=2,
        supersedes=patch1.ref,
    )
    patch3 = Artifact(
        "P1",
        ArtifactType.PATCH_CANDIDATE,
        "N4",
        {"diff": "v3"},
        version=3,
        supersedes=patch2.ref,
    )
    engine.add_artifact(patch1)
    engine.add_artifact(patch2)
    try:
        engine.add_artifact(patch3)
    except ValueError as exc:
        assert "at most once" in str(exc)
    else:
        raise AssertionError("third PatchCandidate version must be rejected")
