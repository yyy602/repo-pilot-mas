from __future__ import annotations

from pathlib import Path

import pytest

from repo_pilot_mas.agents import ReviewerAgent, WorkerAgentError
from repo_pilot_mas.models import FakeModelAdapter
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec
from repo_pilot_mas.schemas.worker_artifact import validate_worker_artifact


def _task(tmp_path: Path) -> TaskSpec:
    return TaskSpec(
        "phase-c-patch-review",
        tmp_path,
        "verify patch review contract",
        failing_tests=("tests/test_target.py",),
    )


def _artifact(
    artifact_id: str,
    artifact_type: ArtifactType,
    created_by: str,
    content: dict,
    input_refs: tuple[str, ...] = (),
) -> Artifact:
    return Artifact(
        artifact_id,
        artifact_type,
        created_by,
        content,
        input_refs=input_refs,
    )


def _evidence() -> Artifact:
    return _artifact(
        "E1",
        ArtifactType.EVIDENCE,
        "N1",
        {
            "mode": "failure_reproduction",
            "claim": "test reproduces IndexError",
            "source": {"path": "target.py", "line_start": 1, "line_end": 5},
            "content": "arr[mid] can exceed array bound",
            "observation_type": "direct",
            "confidence": 0.95,
            "status": "verified",
            "tool_trace_ids": ["trace"],
            "missing_evidence": [],
            "reproduction": {
                "attempted": True,
                "succeeded": True,
                "exit_code": 1,
                "failure_type": "IndexError",
                "failure_output": "IndexError",
                "command": ["pytest"],
            },
        },
    )


def _hypothesis(evidence: Artifact) -> Artifact:
    return _artifact(
        "H1",
        ArtifactType.HYPOTHESIS,
        "N2",
        {
            "perspective": "control_flow",
            "root_cause": "inclusive and exclusive bounds are mixed",
            "direct_cause": "hi uses len(arr) with <= loop",
            "supporting_evidence": [evidence.ref],
            "counter_evidence": [],
            "affected_symbols": ["find_first"],
            "verification_plan": ["run target test"],
            "missing_evidence": [],
            "confidence": 0.9,
        },
        (evidence.ref,),
    )


def _patch(hypothesis: Artifact) -> Artifact:
    return _artifact(
        "P1",
        ArtifactType.PATCH_CANDIDATE,
        "N3",
        {
            "workspace_id": "ws1",
            "diff": "- hi=len(arr)\n+ hi=len(arr)-1",
            "diff_sha256": "a" * 64,
            "changed_files": ["target.py"],
            "protected_path_check": True,
            "based_on_hypothesis_refs": [hypothesis.ref],
        },
        (hypothesis.ref,),
    )


def test_patch_review_requires_single_patch_and_root_review(tmp_path: Path) -> None:
    evidence = _evidence()
    hypothesis = _hypothesis(evidence)
    patch = _patch(hypothesis)
    root_review = _artifact(
        "R1",
        ArtifactType.REVIEW,
        "N4",
        {
            "mode": "root_cause_recommendation",
            "target_artifact_ref": hypothesis.ref,
            "evidence_refs": [evidence.ref],
            "verdict": "supported",
        },
        (hypothesis.ref, evidence.ref),
    )

    model = FakeModelAdapter(
        [
            {
                "mode": "patch_review",
                "target_artifact_ref": patch.ref,
                "evidence_refs": [evidence.ref],
                "verdict": "supported",
                "findings": ["patch covers root cause"],
                "risk_notes": [],
                "recommendation": "continue validation",
            }
        ]
    )

    review = ReviewerAgent(model).run(
        _task(tmp_path),
        "N5",
        "patch_review",
        "review patch",
        (evidence, hypothesis, root_review, patch),
    )

    assert review.content["target_artifact_ref"] == patch.ref
    assert review.content["verdict"] == "supported"


def test_patch_review_rejects_missing_root_review(tmp_path: Path) -> None:
    evidence = _evidence()
    hypothesis = _hypothesis(evidence)
    patch = _patch(hypothesis)

    with pytest.raises(WorkerAgentError):
        ReviewerAgent(FakeModelAdapter([])).run(
            _task(tmp_path),
            "N5",
            "patch_review",
            "review patch",
            (evidence, hypothesis, patch),
        )
