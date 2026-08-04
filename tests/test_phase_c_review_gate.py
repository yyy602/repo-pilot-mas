from __future__ import annotations

from pathlib import Path

import pytest

from repo_pilot_mas.orchestration import OrchestrationEngine, WorkflowStage
from repo_pilot_mas.orchestration.policy_violation import DecisionPolicyViolation
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec
from repo_pilot_mas.schemas.hypothesis_resolution import HypothesisResolutionStatus


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
            "claim": "target test reproduces IndexError",
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


def test_engine_snapshot_exposes_phase_c_resolution(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    snapshot = engine.snapshot()

    assert "hypothesis_resolution" in snapshot["selections"]
    assert snapshot["workflow_stage"] == WorkflowStage.INITIALIZATION.value
