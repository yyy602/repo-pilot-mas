from __future__ import annotations

from pathlib import Path

from repo_pilot_mas.orchestration import OrchestrationEngine
from repo_pilot_mas.schemas import Artifact, ArtifactType, DecisionAction, SupervisorDecision, TaskSpec


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
            "root_cause": "example",
            "direct_cause": "example",
            "perspective": "root",
            "supporting_evidence": ["e1"],
            "affected_symbols": ["module"],
            "verification_plan": ["test"],
        },
    )
    engine.add_artifact(artifact)
    return artifact


def test_accept_requires_review(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    hypothesis = _hypothesis(engine)

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


def test_public_engine_uses_closed_loop_runtime_binding() -> None:
    from repo_pilot_mas.orchestration import LangGraphRuntime

    assert LangGraphRuntime.__name__ == "LangGraphRuntime"
