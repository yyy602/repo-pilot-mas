from __future__ import annotations

from pathlib import Path

from repo_pilot_mas.orchestration import LangGraphRuntime, OrchestrationEngine
from repo_pilot_mas.orchestration.worker_recovery import artifact_rejection
from repo_pilot_mas.schemas import (
    Artifact,
    ArtifactType,
    CreateTaskRequest,
    DecisionAction,
    GateRecord,
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
    boundary_evidence = Artifact(
        "e2",
        ArtifactType.EVIDENCE,
        "investigator",
        {"claim": "boundary evidence"},
    )
    engine.add_artifact(evidence)
    engine.add_artifact(boundary_evidence)
    engine.blackboard.set_stage("diagnosis")
    request = CreateTaskRequest(
        "REVIEW_TASK",
        "ReviewerAgent",
        "evidence_review",
        "review evidence",
        input_artifact_ids=(evidence.ref, boundary_evidence.ref),
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
                    input_artifact_ids=(boundary_evidence.ref, evidence.ref),
                ),
            ),
        )
    )

    assert not repeated.ok
    assert repeated.code == "LOGICAL_TASK_RETRY_EXHAUSTED"
    assert repeated.recoverable


def test_new_node_id_cannot_reset_non_retryable_logical_task(
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
            "create-non-retryable-review",
            DecisionAction.CREATE_TASK,
            "review",
            create_tasks=(request,),
        )
    )
    node = engine.graph.get(first.mutated_node_ids[0])
    engine.start_node(node.node_id)
    rejection = artifact_rejection(
        node.node_id,
        "BUSINESS_EVIDENCE_INSUFFICIENT",
        "review did not produce actionable evidence",
        attempt=1,
        origin="worker",
        allowed_next_actions=("CREATE_TASK", "REQUEST_REPLAN"),
        expected_artifact_type=ArtifactType.REVIEW,
    )
    rejection_ref = engine.add_artifact(rejection)
    engine.finish_node(
        node.node_id,
        "FAILED",
        artifact_refs=(rejection_ref,),
        reason="BUSINESS_EVIDENCE_INSUFFICIENT",
    )

    repeated = engine.apply_decision(
        SupervisorDecision(
            "recreate-non-retryable-review",
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
    assert repeated.details["non_retryable_node_ids"] == [node.node_id]


def test_retry_feedback_does_not_change_logical_task_identity(
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
            "create-feedback-review",
            DecisionAction.CREATE_TASK,
            "review",
            create_tasks=(request,),
        )
    )
    node = engine.graph.get(first.mutated_node_ids[0])
    engine.start_node(node.node_id)
    first_rejection = artifact_rejection(
        node.node_id,
        "MODEL_FORMAT_ERROR",
        "invalid structured output",
        attempt=1,
        origin="worker",
        allowed_next_actions=("RETRY_TASK", "CREATE_TASK"),
        expected_artifact_type=ArtifactType.REVIEW,
    )
    first_rejection_ref = engine.add_artifact(first_rejection)
    engine.finish_node(
        node.node_id,
        "FAILED",
        artifact_refs=(first_rejection_ref,),
        reason="MODEL_FORMAT_ERROR",
    )
    engine.retry_node(
        node.node_id,
        feedback_artifact_refs=(first_rejection_ref,),
    )
    engine.start_node(node.node_id)
    second_rejection = artifact_rejection(
        node.node_id,
        "MODEL_FORMAT_ERROR",
        "invalid structured output again",
        attempt=2,
        origin="worker",
        allowed_next_actions=("RETRY_TASK", "CREATE_TASK"),
        expected_artifact_type=ArtifactType.REVIEW,
    )
    second_rejection_ref = engine.add_artifact(second_rejection)
    engine.finish_node(
        node.node_id,
        "FAILED",
        artifact_refs=(second_rejection_ref,),
        reason="MODEL_FORMAT_ERROR",
    )

    repeated = engine.apply_decision(
        SupervisorDecision(
            "recreate-feedback-review",
            DecisionAction.CREATE_TASK,
            "retry under a new node id",
            create_tasks=(
                CreateTaskRequest(
                    "REVIEW_TASK",
                    "ReviewerAgent",
                    "evidence_review",
                    "same task with feedback omitted",
                    input_artifact_ids=(evidence.ref,),
                ),
            ),
        )
    )

    assert not repeated.ok
    assert repeated.code == "LOGICAL_TASK_RETRY_EXHAUSTED"
    assert repeated.details["exhausted_node_ids"] == [node.node_id]


def test_approved_replan_can_restart_exhausted_logical_task(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    evidence = Artifact(
        "E1",
        ArtifactType.EVIDENCE,
        "N1",
        {
            "mode": "failure_reproduction",
            "evidence_kind": "reproduction",
            "verified": True,
            "status": "verified",
            "source": {"path": "target.py"},
            "content": "failing branch",
            "tool_trace_ids": ["trace-1"],
            "reproduction": {
                "attempted": True,
                "succeeded": True,
                "failure_output": "IndexError",
            },
        },
    )
    hypothesis = Artifact(
        "H1",
        ArtifactType.HYPOTHESIS,
        "N2",
        {
            "root_cause": "boundary mismatch",
            "supporting_evidence": [evidence.ref],
        },
    )
    review = Artifact(
        "R1",
        ArtifactType.REVIEW,
        "N3",
        {
            "mode": "root_cause_recommendation",
            "verdict": "needs_more_evidence",
            "target_artifact_ref": hypothesis.ref,
            "remaining_uncertainty": ["缺少边界执行证据"],
        },
    )
    for artifact in (evidence, hypothesis, review):
        engine.add_artifact(artifact)
    engine.blackboard.set_stage("investigation")
    request = CreateTaskRequest(
        "INVESTIGATION_TASK",
        "InvestigatorAgent",
        "evidence_completion",
        "collect boundary evidence",
        input_artifact_ids=(evidence.ref, review.ref),
    )
    first = engine.apply_decision(
        SupervisorDecision(
            "create-evidence-completion",
            DecisionAction.CREATE_TASK,
            "collect evidence",
            create_tasks=(request,),
            evidence_refs=(review.ref,),
            gate_record=GateRecord(
                "evidence_gap",
                (review.ref,),
                "root review requires evidence",
                1,
                "one investigation node",
            ),
            next_workflow_stage="investigation",
        )
    )
    node = engine.graph.get(first.mutated_node_ids[0])
    engine.start_node(node.node_id)
    engine.finish_node(
        node.node_id,
        "TIMED_OUT",
        reason="MODEL_TIMEOUT",
    )
    node.retry_count = engine.budget.max_retries_per_node
    requested = engine.apply_decision(
        SupervisorDecision(
            "request-investigation-replan",
            DecisionAction.REQUEST_REPLAN,
            "retry evidence collection under explicit replan budget",
            evidence_refs=(evidence.ref, review.ref),
            next_workflow_stage="investigation",
            failure_class="evidence_incomplete",
        )
    )
    assert requested.ok
    recovery = engine.snapshot()["recovery"]
    assert recovery["pending_replan"] is True
    replan_ref = str(recovery["replan_ref"])

    restarted = engine.apply_decision(
        SupervisorDecision(
            "execute-investigation-replan",
            DecisionAction.CREATE_TASK,
            "execute approved recovery",
            create_tasks=(request,),
            evidence_refs=(replan_ref, evidence.ref, review.ref),
            gate_record=GateRecord(
                "approved_investigation_replan",
                (replan_ref, evidence.ref, review.ref),
                "approved replan restarts the exhausted logical task once",
                1,
                "consume the already approved recovery node",
            ),
            next_workflow_stage="investigation",
        )
    )

    assert restarted.ok
    assert engine.snapshot()["recovery"]["pending_replan"] is False
