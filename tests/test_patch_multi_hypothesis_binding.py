from __future__ import annotations

from repo_pilot_mas.agents.patch_agent import _hypothesis_binding
from repo_pilot_mas.schemas import Artifact, ArtifactType


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
