from __future__ import annotations

from pathlib import Path

from repo_pilot_mas.orchestration import (
    NodeStatus,
    NodeType,
    OrchestrationEngine,
    TaskNode,
    WorkerOutcome,
)
from repo_pilot_mas.orchestration.worker_recovery import collect_worker_outcome
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec


def _engine(tmp_path: Path) -> OrchestrationEngine:
    engine = OrchestrationEngine(
        TaskSpec("recovery-test", tmp_path, "collector recovery")
    )
    node = TaskNode(
        "N1",
        NodeType.INVESTIGATION_TASK,
        "InvestigatorAgent",
        "code_retrieval",
        "collect evidence",
    )
    engine.graph.add_node(node)
    engine.start_node("N1")
    return engine


def test_invalid_worker_artifact_becomes_rejection(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    outcome = WorkerOutcome(
        "N1",
        NodeStatus.SUCCEEDED,
        (
            Artifact(
                "bad",
                ArtifactType.HYPOTHESIS,
                "N1",
                {"invalid": True},
            ),
        ),
    )

    result = collect_worker_outcome(engine, outcome)

    assert result.rejection_ref is not None
    assert result.status is NodeStatus.FAILED
    rejection = engine.blackboard.artifacts.get(result.rejection_ref)
    assert rejection.artifact_type is ArtifactType.ARTIFACT_REJECTION


def test_valid_worker_artifact_is_collected(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    artifact = Artifact(
        "evidence",
        ArtifactType.EVIDENCE,
        "N1",
        {
            "mode": "code_retrieval",
            "evidence_kind": "source",
            "claim": "found issue",
            "supports_claims": ["found issue"],
            "contradicts_claims": [],
            "verified": True,
            "source": {"path": "a.py", "line_start": 1, "line_end": 2},
            "content": "evidence",
            "observation_type": "direct",
            "confidence": 1.0,
            "status": "verified",
            "tool_trace_ids": ["trace"],
            "missing_evidence": [],
        },
    )

    result = collect_worker_outcome(
        engine,
        WorkerOutcome("N1", NodeStatus.SUCCEEDED, (artifact,)),
    )

    assert result.status is NodeStatus.SUCCEEDED
    assert engine.graph.get("N1").status is NodeStatus.SUCCEEDED


def test_patch_target_test_failure_keeps_patch_recovery_class(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)

    result = collect_worker_outcome(
        engine,
        WorkerOutcome(
            "N1",
            NodeStatus.FAILED,
            reason=(
                "PATCH_TARGET_TEST_FAILED:WorkerAgentError:"
                "target pytest failed with AssertionError"
            ),
        ),
    )

    assert result.retry_scheduled is True
    assert result.details["code"] == "PATCH_TARGET_TEST_FAILED"
    assert result.details["recovery_class"] == "patch_policy"
    rejection = engine.blackboard.artifacts.get(str(result.rejection_ref))
    assert rejection.content["code"] == "PATCH_TARGET_TEST_FAILED"


def test_invalid_root_review_target_becomes_recoverable_schema_rejection(
    tmp_path: Path,
) -> None:
    engine = OrchestrationEngine(TaskSpec("review-recovery", tmp_path, "review"))
    evidence = Artifact(
        "E1",
        ArtifactType.EVIDENCE,
        "N1",
        {
            "mode": "code_retrieval",
            "evidence_kind": "source",
            "claim": "边界条件错误",
            "supports_claims": ["边界条件错误"],
            "contradicts_claims": [],
            "verified": True,
            "source": {"path": "a.py", "line_start": 1, "line_end": 2},
            "content": "hi 使用了错误的开区间边界",
            "observation_type": "direct",
            "confidence": 1.0,
            "status": "verified",
            "tool_trace_ids": ["trace"],
            "missing_evidence": [],
        },
    )
    engine.add_artifact(evidence)
    hypotheses = []
    for index in (1, 2):
        hypothesis = Artifact(
            f"H{index}",
            ArtifactType.HYPOTHESIS,
            f"N{index}",
            {
                "perspective": "control_flow",
                "root_cause": f"候选根因 {index}",
                "direct_cause": "边界更新不收敛",
                "supporting_evidence": [evidence.ref],
                "counter_evidence": [],
                "affected_symbols": ["target"],
                "verification_plan": ["运行边界用例"],
                "missing_evidence": [],
                "confidence": 0.8,
            },
            input_refs=(evidence.ref,),
        )
        engine.add_artifact(hypothesis)
        hypotheses.append(hypothesis)
    node = TaskNode(
        "N3",
        NodeType.REVIEW_TASK,
        "ReviewerAgent",
        "hypothesis_comparison",
        "比较候选根因",
        input_artifact_ids=(
            evidence.ref,
            hypotheses[0].ref,
            hypotheses[1].ref,
        ),
    )
    engine.graph.add_node(node)
    engine.start_node(node.node_id)
    malformed = Artifact(
        "N3.review",
        ArtifactType.REVIEW,
        node.node_id,
        {
            "mode": "hypothesis_comparison",
            "target_artifact_ref": evidence.ref,
            "target_artifact_refs": [hypotheses[0].ref, hypotheses[1].ref],
            "evidence_refs": [evidence.ref],
            "verdict": "supported",
            "findings": ["候选二更完整"],
            "risk_notes": [],
            "recommendation": "接受候选二",
            "failure_explained": True,
            "causal_chain_complete": True,
            "alternative_causes": ["排除测试配置错误"],
            "counterexample_checked": True,
            "verification_steps_executed": ["核对两个因果链"],
            "remaining_uncertainty": [],
        },
        input_refs=node.input_artifact_ids,
    )

    result = collect_worker_outcome(
        engine,
        WorkerOutcome(node.node_id, NodeStatus.SUCCEEDED, (malformed,)),
    )

    assert result.retry_scheduled is True
    rejection = engine.blackboard.artifacts.get(str(result.rejection_ref))
    assert rejection.content["code"] == "ARTIFACT_SCHEMA_ERROR"
    assert not engine.blackboard.artifacts.contains(malformed.ref)
