from __future__ import annotations

from pathlib import Path

from repo_pilot_mas.orchestration import LangGraphRuntime, OrchestrationEngine
from repo_pilot_mas.schemas import (
    Artifact,
    ArtifactType,
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
