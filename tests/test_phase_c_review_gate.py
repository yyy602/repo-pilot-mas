from __future__ import annotations

from pathlib import Path

import pytest

from repo_pilot_mas.orchestration import OrchestrationEngine, WorkflowStage
from repo_pilot_mas.orchestration.policy_violation import DecisionPolicyViolation
from repo_pilot_mas.schemas import (
    Artifact,
    ArtifactType,
    DecisionAction,
    SupervisorDecision,
    TaskSpec,
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
