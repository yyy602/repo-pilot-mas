from __future__ import annotations

from pathlib import Path

from repo_pilot_mas.orchestration import LangGraphRuntime, OrchestrationEngine
from repo_pilot_mas.schemas import (
    Artifact,
    ArtifactType,
    CreateTaskRequest,
    DecisionAction,
    SupervisorDecision,
    TaskSpec,
)


def _engine(tmp_path: Path) -> OrchestrationEngine:
    return OrchestrationEngine(
        TaskSpec(
            task_id="closed-loop-test",
            repository_path=tmp_path,
            issue="validate hypothesis gate",
        )
    )


def _hypothesis(engine: OrchestrationEngine) -> Artifact:
    artifact = Artifact(
        "h1",
        ArtifactType.HYPOTHESIS,
        "diagnostician",
        {
            "root_cause": "example root cause",
            "direct_cause": "example direct cause",
            "perspective": "control_flow",
            "supporting_evidence": ["e1"],
            "counter_evidence": [],
            "affected_symbols": ["module"],
            "verification_plan": ["run focused test"],
            "missing_evidence": [],
            "confidence": 0.8,
        },
    )
    engine.add_artifact(artifact)
    return artifact


def test_accept_requires_review(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    hypothesis = _hypothesis(engine)
    engine.blackboard.set_stage("diagnosis")

    result = engine.apply_decision(
        SupervisorDecision(
            "accept-without-review",
            DecisionAction.ACCEPT_HYPOTHESIS,
            "accept",
            hypothesis_refs=(hypothesis.ref,),
            primary_hypothesis_ref=hypothesis.ref,
            review_refs=(),
        )
    )

    assert not result.ok
    assert result.code == "HYPOTHESIS_REVIEW_REQUIRED"
    assert result.recoverable
    assert result.recommended_stage == "review"


def test_public_runtime_uses_closed_loop_binding() -> None:
    assert LangGraphRuntime.__name__ == "LangGraphRuntime"
    assert OrchestrationEngine.__module__.endswith("closed_loop_engine")


def test_engine_rejects_redundant_investigation_after_evidence_gate(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    evidence = Artifact(
        "e1",
        ArtifactType.EVIDENCE,
        "investigator",
        {
            "mode": "failure_reproduction",
            "evidence_kind": "reproduction",
            "verified": True,
            "status": "verified",
            "source": {"path": "target.py", "line_start": 8, "line_end": 8},
            "content": "failing branch",
            "tool_trace_ids": ["trace-1"],
            "missing_evidence": [],
            "reproduction": {
                "attempted": True,
                "succeeded": True,
                "failure_output": "IndexError",
            },
        },
    )
    engine.add_artifact(evidence)
    engine.blackboard.set_stage("investigation")

    result = engine.apply_decision(
        SupervisorDecision(
            "redundant-investigation",
            DecisionAction.CREATE_TASK,
            "collect more evidence",
            create_tasks=(
                CreateTaskRequest(
                    "INVESTIGATION_TASK",
                    "InvestigatorAgent",
                    "evidence_completion",
                    "repeat evidence collection",
                    input_artifact_ids=(evidence.ref,),
                ),
            ),
            next_workflow_stage="investigation",
        )
    )

    assert not result.ok
    assert result.code == "EVIDENCE_COLLECTION_ALREADY_SUFFICIENT"
    assert result.recoverable
    assert result.recommended_stage == "diagnosis"


def test_new_node_id_cannot_reset_exhausted_logical_task_retry(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    evidence = Artifact(
        "e1",
        ArtifactType.EVIDENCE,
        "investigator",
        {"claim": "direct evidence"},
    )
    engine.add_artifact(evidence)
    engine.blackboard.set_stage("diagnosis")
    request = CreateTaskRequest(
        "REVIEW_TASK",
        "ReviewerAgent",
        "evidence_review",
        "review evidence",
        input_artifact_ids=(evidence.ref,),
    )
    first = engine.apply_decision(
        SupervisorDecision(
            "create-review",
            DecisionAction.CREATE_TASK,
            "review",
            create_tasks=(request,),
        )
    )
    assert first.ok
    node = engine.graph.get(first.mutated_node_ids[0])
    engine.start_node(node.node_id)
    engine.finish_node(node.node_id, "FAILED", reason="MODEL_FORMAT_ERROR")
    node.retry_count = engine.budget.max_retries_per_node

    repeated = engine.apply_decision(
        SupervisorDecision(
            "recreate-review",
            DecisionAction.CREATE_TASK,
            "retry under a new node id",
            create_tasks=(
                CreateTaskRequest(
                    "REVIEW_TASK",
                    "ReviewerAgent",
                    "evidence_review",
                    "review the same evidence with reworded objective",
                    input_artifact_ids=(evidence.ref,),
                ),
            ),
        )
    )

    assert not repeated.ok
    assert repeated.code == "LOGICAL_TASK_RETRY_EXHAUSTED"
    assert repeated.recoverable
