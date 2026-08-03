from __future__ import annotations

from pathlib import Path

from repo_pilot_mas.orchestration import (
    NodeStatus,
    NodeType,
    OrchestrationEngine,
    TaskNode,
    WorkerOutcome,
)
from repo_pilot_mas.orchestration.worker_recovery import collect_worker_outcome
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec


def _engine(tmp_path: Path) -> OrchestrationEngine:
    engine = OrchestrationEngine(
        TaskSpec("recovery-test", tmp_path, "collector recovery")
    )
    node = TaskNode(
        "N1",
        NodeType.INVESTIGATION_TASK,
        "InvestigatorAgent",
        "code_retrieval",
        "collect evidence",
    )
    engine.graph.add_node(node)
    engine.start_node("N1")
    return engine


def test_invalid_worker_artifact_becomes_rejection(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    outcome = WorkerOutcome(
        "N1",
        NodeStatus.SUCCEEDED,
        (
            Artifact(
                "bad",
                ArtifactType.HYPOTHESIS,
                "N1",
                {"invalid": True},
            ),
        ),
    )

    result = collect_worker_outcome(engine, outcome)

    assert result.rejection_ref is not None
    assert result.status is NodeStatus.FAILED
    rejection = engine.blackboard.artifacts.get(result.rejection_ref)
    assert rejection.artifact_type is ArtifactType.ARTIFACT_REJECTION


def test_valid_worker_artifact_is_collected(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    artifact = Artifact(
        "evidence",
        ArtifactType.EVIDENCE,
        "N1",
        {
            "mode": "code_retrieval",
            "claim": "found issue",
            "source": {"path": "a.py", "line_start": 1, "line_end": 2},
            "content": "evidence",
            "observation_type": "direct",
            "confidence": 1.0,
            "status": "verified",
            "tool_trace_ids": ["trace"],
            "missing_evidence": [],
        },
    )

    result = collect_worker_outcome(
        engine,
        WorkerOutcome("N1", NodeStatus.SUCCEEDED, (artifact,)),
    )

    assert result.status is NodeStatus.SUCCEEDED
    assert engine.graph.get("N1").status is NodeStatus.SUCCEEDED
