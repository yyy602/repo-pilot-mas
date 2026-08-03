from __future__ import annotations

from pathlib import Path

from repo_pilot_mas.orchestration.closed_loop_engine import OrchestrationEngine
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec


def test_challenge_rebuttal_acceptance_path(tmp_path: Path) -> None:
    engine = OrchestrationEngine(
        TaskSpec(
            task_id="adversarial-loop",
            repository_path=tmp_path,
            issue="challenge rebuttal loop",
        )
    )
    evidence = Artifact(
        "E1",
        ArtifactType.EVIDENCE,
        "investigator",
        {
            "mode": "code_retrieval",
            "claim": "root cause evidence",
            "source": {"path": "a.py", "line_start": 1, "line_end": 2},
            "content": "evidence",
            "observation_type": "direct",
            "confidence": 1.0,
            "status": "verified",
            "tool_trace_ids": ["trace"],
            "missing_evidence": [],
        },
    )
    hypothesis = Artifact(
        "H1",
        ArtifactType.HYPOTHESIS,
        "diagnostician",
        {
            "perspective": "control_flow",
            "root_cause": "bad branch",
            "direct_cause": "missing guard",
            "supporting_evidence": [evidence.ref],
            "counter_evidence": [],
            "affected_symbols": ["func"],
            "verification_plan": ["test"],
            "missing_evidence": [],
            "confidence": 0.8,
        },
        input_refs=(evidence.ref,),
    )
    challenge = Artifact(
        "C1",
        ArtifactType.CHALLENGE,
        "diagnostician",
        {
            "challenged_hypothesis_ref": hypothesis.ref,
            "challenged_claim": "claim incomplete",
            "insufficiency_reason": "needs regression evidence",
            "counterexample": "normal path",
            "alternative_causal_chain": "wider issue",
            "required_evidence": [evidence.ref],
            "severity": "blocking",
        },
        input_refs=(hypothesis.ref,),
    )
    rebuttal = Artifact(
        "R1",
        ArtifactType.REBUTTAL,
        "diagnostician",
        {
            "response_to": challenge.ref,
            "defended_hypothesis_ref": hypothesis.ref,
            "decision": "accept",
            "new_evidence_refs": [evidence.ref],
            "resulting_hypothesis_ref": hypothesis.ref,
            "counterexample_explanation": "handled",
            "revision_summary": "confirmed",
        },
        input_refs=(challenge.ref,),
    )
    review = Artifact(
        "V1",
        ArtifactType.REVIEW,
        "reviewer",
        {
            "mode": "root_cause_recommendation",
            "target_artifact_ref": hypothesis.ref,
            "evidence_refs": [evidence.ref],
            "verdict": "approved",
            "findings": ["supported"],
            "risk_notes": [],
            "recommendation": "accept",
        },
        input_refs=(hypothesis.ref, evidence.ref),
    )
    for artifact in (evidence, hypothesis, challenge, rebuttal, review):
        engine.add_artifact(artifact)

    assert engine.blackboard.hypothesis_resolution.review_refs == (review.ref,)
