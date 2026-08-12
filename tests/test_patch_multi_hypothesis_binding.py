from __future__ import annotations

from types import SimpleNamespace

import pytest

from repo_pilot_mas.agents.patch_agent import _hypothesis_binding
from repo_pilot_mas.orchestration.worker_recovery import (
    ArtifactCollectionError,
    _validate_patch_binding,
)
from repo_pilot_mas.schemas import Artifact, ArtifactType
from repo_pilot_mas.schemas.hypothesis_resolution import (
    HypothesisResolution,
    HypothesisResolutionStatus,
)
from repo_pilot_mas.schemas.worker_artifact import validate_worker_artifact


def _hypothesis(ref: str, root_cause: str) -> Artifact:
    return Artifact(
        ref.split(".")[0] + ".hypothesis",
        ArtifactType.HYPOTHESIS,
        ref.split(".")[0],
        {
            "perspective": "control_flow",
            "root_cause": root_cause,
            "direct_cause": root_cause,
            "supporting_evidence": ["E1@v1"],
            "counter_evidence": [],
            "affected_symbols": ["target"],
            "verification_plan": ["run tests"],
            "missing_evidence": [],
            "confidence": 0.9,
        },
        version=1,
        input_refs=("E1@v1",),
    )


def test_patch_binding_contains_all_hypotheses() -> None:
    first = _hypothesis("H1", "missing boundary condition")
    second = _hypothesis("H2", "incorrect state transition")

    refs, primary, causes = _hypothesis_binding((first, second))

    assert refs == (first.ref, second.ref)
    assert primary == first.ref
    assert causes == {
        first.ref: "missing boundary condition",
        second.ref: "incorrect state transition",
    }


def test_patch_schema_requires_exact_explicit_binding_and_precheck() -> None:
    first = _hypothesis("H1", "missing boundary condition")
    second = _hypothesis("H2", "incorrect state transition")
    refs = (first.ref, second.ref)
    valid = {
        "strategy": "minimal",
        "based_on_hypothesis_refs": list(refs),
        "primary_hypothesis_ref": first.ref,
        "covered_root_causes": {
            first.ref: "missing boundary condition",
            second.ref: "incorrect state transition",
        },
        "diff": "--- a/a.py\n+++ b/a.py\n-x\n+y\n",
        "diff_sha256": "a" * 64,
        "changed_files": ["a.py"],
        "rationale": "repair both accepted causes",
        "semantic_rationale": "the new guard removes both direct causes",
        "pre_patch_behavior": "the failure input reaches an invalid transition",
        "post_patch_expected_behavior": "the guard returns the expected value",
        "failure_input_walkthrough": "input reaches branch A then invalid state B",
        "precheck_command": ["static_check", ".", "--no-ruff"],
        "precheck_result": {
            "ok": True,
            "trace_id": "precheck",
            "exit_code": 0,
            "output_tail": "",
        },
        "risk_notes": [],
        "protected_path_check": True,
        "workspace_id": "workspace",
    }
    artifact = Artifact(
        "P1",
        ArtifactType.PATCH_CANDIDATE,
        "N1",
        valid,
        input_refs=refs,
    )

    validate_worker_artifact(artifact, allowed_input_refs=refs)

    invalid = Artifact(
        "P2",
        ArtifactType.PATCH_CANDIDATE,
        "N2",
        {**valid, "primary_hypothesis_ref": "H3@v1"},
        input_refs=refs,
    )
    with pytest.raises(ValueError, match="primary_hypothesis_ref"):
        validate_worker_artifact(invalid, allowed_input_refs=refs)


def test_collector_rejects_patch_not_bound_to_full_accepted_set() -> None:
    first = _hypothesis("H1", "missing boundary condition")
    second = _hypothesis("H2", "incorrect state transition")
    resolution = HypothesisResolution(
        status=HypothesisResolutionStatus.ACCEPTED,
        candidate_refs=(first.ref, second.ref),
        accepted_refs=(first.ref, second.ref),
        primary_ref=first.ref,
        review_refs=("R1@v1",),
    )
    patch = Artifact(
        "P3",
        ArtifactType.PATCH_CANDIDATE,
        "N3",
        {
            "based_on_hypothesis_refs": [first.ref],
            "primary_hypothesis_ref": first.ref,
            "protected_path_check": True,
        },
    )
    engine = SimpleNamespace(
        blackboard=SimpleNamespace(hypothesis_resolution=resolution)
    )

    with pytest.raises(ArtifactCollectionError) as caught:
        _validate_patch_binding(engine, patch)

    assert caught.value.code == "PATCH_HYPOTHESIS_SET_MISMATCH"
