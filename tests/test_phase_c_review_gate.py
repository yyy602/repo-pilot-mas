from __future__ import annotations

from pathlib import Path

import pytest

from repo_pilot_mas.orchestration import OrchestrationEngine, WorkflowStage
from repo_pilot_mas.orchestration.policy_violation import DecisionPolicyViolation
from repo_pilot_mas.schemas import (
    Artifact,
    ArtifactType,
    CreateTaskRequest,
    DecisionAction,
    SupervisorDecision,
    TaskSpec,
    additional_investigation_required,
    evidence_gap_recovery_stage,
    supervisor_decision_schema_for_state,
)
from repo_pilot_mas.schemas.hypothesis_resolution import HypothesisResolutionStatus
from repo_pilot_mas.schemas.worker_artifact import validate_worker_artifact


def _task(tmp_path: Path) -> TaskSpec:
    return TaskSpec(
        "phase-c-review",
        tmp_path,
        "repair binary search boundary failure",
        failing_tests=("tests/test_target.py::test_failure",),
    )


def _engine(tmp_path: Path) -> OrchestrationEngine:
    return OrchestrationEngine(_task(tmp_path))


def _evidence(node: str = "N1") -> Artifact:
    return Artifact(
        "N1.evidence",
        ArtifactType.EVIDENCE,
        node,
        {
            "mode": "failure_reproduction",
            "evidence_kind": "reproduction",
            "claim": "target test reproduces IndexError",
            "supports_claims": ["target test reproduces IndexError"],
            "contradicts_claims": [],
            "verified": True,
            "source": {"path": "target.py", "line_start": 1, "line_end": 3},
            "content": "arr[mid] accesses outside range",
            "observation_type": "direct",
            "confidence": 0.9,
            "status": "verified",
            "tool_trace_ids": ["trace"],
            "missing_evidence": [],
            "reproduction": {
                "attempted": True,
                "succeeded": True,
                "exit_code": 1,
                "failure_type": "IndexError",
                "failure_output": "out of range",
                "command": ["pytest"],
            },
        },
    )


def _hypothesis() -> Artifact:
    evidence = _evidence()
    return Artifact(
        "N2.hypothesis",
        ArtifactType.HYPOTHESIS,
        "N2",
        {
            "perspective": "control_flow",
            "root_cause": "inclusive and exclusive boundaries are mixed",
            "direct_cause": "hi can become len(arr)",
            "supporting_evidence": [evidence.ref],
            "counter_evidence": [],
            "affected_symbols": ["find_first_in_sorted"],
            "verification_plan": ["run target test"],
            "missing_evidence": [],
            "confidence": 0.9,
        },
        input_refs=(evidence.ref,),
    )


def _review(hypothesis: Artifact, verdict: str = "supported") -> Artifact:
    return Artifact(
        "N3.review",
        ArtifactType.REVIEW,
        "N3",
        {
            "mode": "root_cause_recommendation",
            "target_artifact_ref": hypothesis.ref,
            "evidence_refs": ["N1.evidence@v1"],
            "verdict": verdict,
            "findings": ["Evidence supports the hypothesis"],
            "risk_notes": [],
            "recommendation": "continue",
            "failure_explained": True,
            "causal_chain_complete": True,
            "alternative_causes": ["排除了目标测试配置错误"],
            "counterexample_checked": True,
            "verification_steps_executed": ["核对失败输出与源码分支"],
            "remaining_uncertainty": [],
        },
        input_refs=(hypothesis.ref, "N1.evidence@v1"),
    )


def test_blocking_review_prevents_hypothesis_acceptance(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    hypothesis = _hypothesis()
    engine.add_artifact(_evidence())
    engine.add_artifact(hypothesis)
    engine.add_artifact(_review(hypothesis, "changes_requested"))

    with pytest.raises(DecisionPolicyViolation):
        engine._validate_hypothesis_acceptance(
            type(
                "Decision",
                (),
                {
                    "hypothesis_refs": (hypothesis.ref,),
                    "primary_hypothesis_ref": hypothesis.ref,
                    "review_refs": ("N3.review@v1",),
                },
            )()
        )


def test_supported_review_moves_resolution_to_acceptance_path(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    evidence = _evidence()
    hypothesis = _hypothesis()
    review = _review(hypothesis)
    engine.add_artifact(evidence)
    engine.add_artifact(hypothesis)
    engine.add_artifact(review)

    assert engine.blackboard.hypothesis_resolution.status is not HypothesisResolutionStatus.ACCEPTED

    hypotheses, reviews = engine._validate_hypothesis_acceptance(
        type(
            "Decision",
            (),
            {
                "hypothesis_refs": (hypothesis.ref,),
                "primary_hypothesis_ref": hypothesis.ref,
                "review_refs": (review.ref,),
            },
        )()
    )

    assert hypotheses[0].ref == hypothesis.ref
    assert reviews[0].ref == review.ref


def test_comparison_review_can_select_one_of_multiple_candidates(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    evidence = _evidence()
    first = _hypothesis()
    second = Artifact(
        "N4.hypothesis",
        ArtifactType.HYPOTHESIS,
        "N4",
        {
            **first.to_dict()["content"],
            "root_cause": "the upper bound starts one position beyond the array",
        },
        input_refs=(evidence.ref,),
    )
    comparison = Artifact(
        "N5.review",
        ArtifactType.REVIEW,
        "N5",
        {
            **_review(second).to_dict()["content"],
            "mode": "hypothesis_comparison",
            "target_artifact_ref": second.ref,
            "target_artifact_refs": [first.ref, second.ref],
        },
        input_refs=(evidence.ref, first.ref, second.ref),
    )
    for artifact in (evidence, first, second, comparison):
        engine.add_artifact(artifact)

    hypotheses, reviews = engine._validate_hypothesis_acceptance(
        type(
            "Decision",
            (),
            {
                "hypothesis_refs": (second.ref,),
                "primary_hypothesis_ref": second.ref,
                "review_refs": (comparison.ref,),
            },
        )()
    )

    assert [item.ref for item in hypotheses] == [second.ref]
    assert [item.ref for item in reviews] == [comparison.ref]


def test_comparison_acceptance_ignores_superseded_candidate_versions(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    evidence = _evidence()
    first = _hypothesis()
    revised = Artifact(
        "N2.hypothesis",
        ArtifactType.HYPOTHESIS,
        "N5",
        {
            **first.to_dict()["content"],
            "direct_cause": "hi 在空数组时成为负索引",
        },
        version=2,
        supersedes=first.ref,
        input_refs=(evidence.ref,),
    )
    second = Artifact(
        "N4.hypothesis",
        ArtifactType.HYPOTHESIS,
        "N4",
        {
            **first.to_dict()["content"],
            "root_cause": "the upper bound starts one position beyond the array",
        },
        input_refs=(evidence.ref,),
    )
    comparison = Artifact(
        "N6.review",
        ArtifactType.REVIEW,
        "N6",
        {
            **_review(second).to_dict()["content"],
            "mode": "hypothesis_comparison",
            "target_artifact_ref": revised.ref,
            "target_artifact_refs": [revised.ref, second.ref],
        },
        input_refs=(evidence.ref, revised.ref, second.ref),
    )
    for artifact in (evidence, first, revised, second, comparison):
        engine.add_artifact(artifact)

    # 历史版本仍留在 candidate_refs 中，但不得阻断最新候选的接受
    assert first.ref in engine.blackboard.hypothesis_resolution.candidate_refs
    assert revised.ref in engine.blackboard.hypothesis_resolution.candidate_refs

    hypotheses, reviews = engine._validate_hypothesis_acceptance(
        type(
            "Decision",
            (),
            {
                "hypothesis_refs": (revised.ref, second.ref),
                "primary_hypothesis_ref": revised.ref,
                "review_refs": (comparison.ref,),
            },
        )()
    )

    assert [item.ref for item in hypotheses] == [revised.ref, second.ref]
    assert [item.ref for item in reviews] == [comparison.ref]


def test_recommendation_can_select_one_after_multiple_candidates(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    evidence = _evidence()
    first = _hypothesis()
    second = Artifact(
        "N4.hypothesis",
        ArtifactType.HYPOTHESIS,
        "N4",
        {
            **first.to_dict()["content"],
            "root_cause": "the recursive call does not reduce its second argument",
        },
        input_refs=(evidence.ref,),
    )
    recommendation = Artifact(
        "N5.review",
        ArtifactType.REVIEW,
        "N5",
        {
            **_review(second).to_dict()["content"],
            "target_artifact_ref": second.ref,
            "target_artifact_refs": [second.ref],
        },
        input_refs=(evidence.ref, second.ref),
    )
    for artifact in (evidence, first, second, recommendation):
        engine.add_artifact(artifact)

    hypotheses, reviews = engine._validate_hypothesis_acceptance(
        type(
            "Decision",
            (),
            {
                "hypothesis_refs": (second.ref,),
                "primary_hypothesis_ref": second.ref,
                "review_refs": (recommendation.ref,),
            },
        )()
    )

    assert [item.ref for item in hypotheses] == [second.ref]
    assert [item.ref for item in reviews] == [recommendation.ref]


def test_engine_snapshot_exposes_phase_c_resolution(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    snapshot = engine.snapshot()

    assert "hypothesis_resolution" in snapshot["selections"]
    assert snapshot["workflow_stage"] == WorkflowStage.INITIALIZATION.value


def test_engine_rejects_patch_stage_before_hypothesis_acceptance(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    evidence = _evidence()
    hypothesis = _hypothesis()
    review = _review(hypothesis)
    for artifact in (evidence, hypothesis, review):
        engine.add_artifact(artifact)
    engine.blackboard.set_stage(WorkflowStage.DIAGNOSIS.value)

    result = engine.apply_decision(
        SupervisorDecision(
            "enter-patch-before-acceptance",
            DecisionAction.CHANGE_WORKFLOW_STAGE,
            "review supported the hypothesis, so enter patch",
            next_workflow_stage=WorkflowStage.PATCH.value,
        )
    )

    assert result.ok is False
    assert result.code == "HYPOTHESIS_ACCEPTANCE_REQUIRED"
    assert result.recoverable is True
    assert result.recommended_stage == WorkflowStage.REVIEW.value
    assert engine.blackboard.workflow_stage == WorkflowStage.DIAGNOSIS.value


def test_engine_rejects_stage_churn_when_supported_review_is_ready(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    evidence = _evidence()
    hypothesis = _hypothesis()
    review = _review(hypothesis)
    for artifact in (evidence, hypothesis, review):
        engine.add_artifact(artifact)
    engine.blackboard.set_stage(WorkflowStage.DIAGNOSIS.value)

    result = engine.apply_decision(
        SupervisorDecision(
            "stage-churn-after-supported-review",
            DecisionAction.CHANGE_WORKFLOW_STAGE,
            "Review 已支持，但只切换到 Review 阶段",
            next_workflow_stage=WorkflowStage.REVIEW.value,
        )
    )

    assert result.ok is False
    assert result.code == "HYPOTHESIS_ACCEPTANCE_DECISION_REQUIRED"
    assert result.recoverable is True
    assert result.allowed_next_actions == (
        "ACCEPT_HYPOTHESIS",
        "TERMINATE_TASK",
    )
    assert set(result.trigger_artifact_refs) == {hypothesis.ref, review.ref}
    assert engine.blackboard.workflow_stage == WorkflowStage.DIAGNOSIS.value


def test_engine_requires_rediagnosis_after_gap_evidence(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    evidence = _evidence()
    hypothesis = Artifact(
        "N2.hypothesis",
        ArtifactType.HYPOTHESIS,
        "N2",
        {
            **_hypothesis().to_dict()["content"],
            "missing_evidence": ["大于最大值时的执行路径"],
        },
        input_refs=(evidence.ref,),
    )
    review = Artifact(
        "N3.review",
        ArtifactType.REVIEW,
        "N3",
        {
            **_review(hypothesis).to_dict()["content"],
            "verdict": "needs_more_evidence",
            "remaining_uncertainty": ["缺少边界执行证据"],
        },
        input_refs=(hypothesis.ref, evidence.ref),
    )
    completion = Artifact(
        "N4.evidence",
        ArtifactType.EVIDENCE,
        "N4",
        {
            **evidence.to_dict()["content"],
            "claim": "已验证大于最大值时的执行路径",
            "supports_claims": ["已验证大于最大值时的执行路径"],
        },
    )
    for artifact in (evidence, hypothesis, review, completion):
        engine.add_artifact(artifact)
    engine.blackboard.set_stage(WorkflowStage.REVIEW.value)

    result = engine.apply_decision(
        SupervisorDecision(
            "review-stale-hypothesis",
            DecisionAction.CREATE_TASK,
            "review the old hypothesis after evidence completion",
            create_tasks=(
                CreateTaskRequest(
                    "REVIEW_TASK",
                    "ReviewerAgent",
                    "root_cause_recommendation",
                    "review the stale hypothesis",
                    input_artifact_ids=(evidence.ref, hypothesis.ref),
                ),
            ),
            next_workflow_stage=WorkflowStage.REVIEW.value,
        )
    )

    assert result.ok is False
    assert result.code == "EVIDENCE_GAP_REQUIRES_REDIAGNOSIS"
    assert result.recommended_stage == WorkflowStage.DIAGNOSIS.value
    assert result.details["required_node_type"] == "DIAGNOSIS_TASK"

    missing_completion = engine.apply_decision(
        SupervisorDecision(
            "rediagnose-with-stale-evidence-only",
            DecisionAction.CREATE_TASK,
            "重新诊断，但遗漏刚补充的证据",
            create_tasks=(
                CreateTaskRequest(
                    "DIAGNOSIS_TASK",
                    "DiagnosticianAgent",
                    "control_flow",
                    "结合新增证据重新诊断",
                    input_artifact_ids=(evidence.ref,),
                ),
            ),
            next_workflow_stage=WorkflowStage.DIAGNOSIS.value,
        )
    )

    assert missing_completion.ok is False
    assert missing_completion.code == "EVIDENCE_GAP_COMPLETION_INPUT_REQUIRED"
    assert missing_completion.trigger_artifact_refs == (completion.ref,)


def test_rediagnosis_replaces_stale_hypothesis_candidates(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    evidence = _evidence()
    stale = _hypothesis()
    review = Artifact(
        "N3.review",
        ArtifactType.REVIEW,
        "N3",
        {
            **_review(stale).to_dict()["content"],
            "verdict": "needs_more_evidence",
            "remaining_uncertainty": ["缺少边界执行证据"],
        },
        input_refs=(stale.ref, evidence.ref),
    )
    completion = Artifact(
        "N4.evidence",
        ArtifactType.EVIDENCE,
        "N4",
        {
            **evidence.to_dict()["content"],
            "mode": "evidence_completion",
            "evidence_kind": "execution",
            "claim": "边界执行路径已验证",
            "supports_claims": ["边界执行路径已验证"],
        },
    )
    revised = Artifact(
        "N5.hypothesis",
        ArtifactType.HYPOTHESIS,
        "N5",
        {
            **stale.to_dict()["content"],
            "root_cause": "补证后修订的边界根因",
            "supporting_evidence": [evidence.ref, completion.ref],
            "missing_evidence": [],
        },
        input_refs=(evidence.ref, completion.ref),
    )
    for artifact in (evidence, stale, review, completion):
        engine.add_artifact(artifact)

    assert (
        engine.blackboard.hypothesis_resolution.status
        is HypothesisResolutionStatus.NEEDS_EVIDENCE
    )

    engine.add_artifact(revised)

    resolution = engine.blackboard.hypothesis_resolution
    assert resolution.status is HypothesisResolutionStatus.UNRESOLVED
    assert resolution.candidate_refs == (revised.ref,)
    assert resolution.review_refs == ()


def test_patch_review_uncertainty_is_not_a_root_cause_evidence_gap() -> None:
    artifacts = [
        _evidence().to_dict(),
        {
            "artifact_ref": "N8.patch@v1",
            "artifact_type": "patch_candidate",
            "content": {},
        },
        {
            "artifact_ref": "N9.review@v1",
            "artifact_type": "review",
            "content": {
                "mode": "patch_review",
                "verdict": "needs_more_evidence",
                "remaining_uncertainty": ["还需覆盖相邻边界输入"],
            },
        },
    ]

    assert additional_investigation_required(artifacts) is False
    assert evidence_gap_recovery_stage(artifacts) is None


def test_unverified_gap_evidence_forces_investigation_work_not_stage_churn(
    tmp_path: Path,
) -> None:
    evidence = _evidence()
    hypothesis = _hypothesis()
    review = Artifact(
        "N3.review",
        ArtifactType.REVIEW,
        "N3",
        {
            **_review(hypothesis).to_dict()["content"],
            "verdict": "needs_more_evidence",
            "remaining_uncertainty": ["缺少边界执行证据"],
        },
        input_refs=(hypothesis.ref, evidence.ref),
    )
    unverified = Artifact(
        "N4.evidence",
        ArtifactType.EVIDENCE,
        "N4",
        {
            **evidence.to_dict()["content"],
            "mode": "evidence_completion",
            "evidence_kind": "execution",
            "verified": False,
            "status": "unverified",
            "missing_evidence": ["目标测试执行结果"],
        },
    )
    artifacts = [
        item.to_dict() for item in (evidence, hypothesis, review, unverified)
    ]
    schema = supervisor_decision_schema_for_state(
        {
            "workflow_stage": "investigation",
            "nodes": [],
            "artifacts": artifacts,
            "selections": {
                "hypothesis_resolution": {"status": "needs_evidence"}
            },
        }
    )
    actions = {
        variant["properties"]["action"]["const"] for variant in schema["oneOf"]
    }
    create_types = {
        task_variant["properties"]["node_type"]["const"]
        for variant in schema["oneOf"]
        if variant["properties"]["action"]["const"] == "CREATE_TASK"
        for task_variant in variant["properties"]["create_tasks"]["items"]["oneOf"]
    }

    assert "CHANGE_WORKFLOW_STAGE" not in actions
    assert create_types == {"INVESTIGATION_TASK"}

    engine = _engine(tmp_path)
    for artifact in (evidence, hypothesis, review, unverified):
        engine.add_artifact(artifact)
    engine.blackboard.set_stage(WorkflowStage.INVESTIGATION.value)
    result = engine.apply_decision(
        SupervisorDecision(
            "stage-churn-with-open-gap",
            DecisionAction.CHANGE_WORKFLOW_STAGE,
            "只切换阶段而不补充执行证据",
            next_workflow_stage=WorkflowStage.DIAGNOSIS.value,
        )
    )

    assert result.ok is False
    assert result.code == "EVIDENCE_GAP_REQUIRES_INVESTIGATION"
    assert result.allowed_next_actions == ("CREATE_TASK", "TERMINATE_TASK")


def test_minimum_evidence_gate_rejects_source_only_hypothesis(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    source = Artifact(
        "source.evidence",
        ArtifactType.EVIDENCE,
        "N1",
        {
            "mode": "code_retrieval",
            "evidence_kind": "source",
            "claim": "located boundary expression",
            "supports_claims": ["located boundary expression"],
            "contradicts_claims": [],
            "verified": True,
            "source": {"path": "target.py", "line_start": 1, "line_end": 3},
            "content": "hi starts at len(arr)",
            "observation_type": "direct",
            "confidence": 0.9,
            "status": "verified",
            "tool_trace_ids": ["inspect-trace"],
            "missing_evidence": [],
        },
    )
    hypothesis = Artifact(
        "source.hypothesis",
        ArtifactType.HYPOTHESIS,
        "N2",
        {
            **_hypothesis().to_dict()["content"],
            "supporting_evidence": [source.ref],
        },
        input_refs=(source.ref,),
    )
    review = Artifact(
        "source.review",
        ArtifactType.REVIEW,
        "N3",
        {
            **_review(hypothesis).to_dict()["content"],
            "target_artifact_ref": hypothesis.ref,
            "evidence_refs": [source.ref],
        },
        input_refs=(hypothesis.ref, source.ref),
    )
    for artifact in (source, hypothesis, review):
        engine.add_artifact(artifact)

    with pytest.raises(DecisionPolicyViolation) as caught:
        engine._validate_hypothesis_acceptance(
            type(
                "Decision",
                (),
                {
                    "hypothesis_refs": (hypothesis.ref,),
                    "primary_hypothesis_ref": hypothesis.ref,
                    "review_refs": (review.ref,),
                },
            )()
        )

    assert caught.value.code == "MINIMUM_EVIDENCE_GATE_NOT_MET"


def test_supported_review_requires_independent_verification_fields() -> None:
    hypothesis = _hypothesis()
    review = _review(hypothesis)
    invalid = Artifact(
        review.artifact_id,
        review.artifact_type,
        review.created_by,
        {
            **review.to_dict()["content"],
            "counterexample_checked": False,
            "alternative_causes": [],
        },
        input_refs=review.input_refs,
    )

    with pytest.raises(ValueError, match="alternative cause or counterexample"):
        validate_worker_artifact(invalid)
