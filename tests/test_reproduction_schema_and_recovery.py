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
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec, validate_worker_artifact


def _engine(tmp_path: Path) -> OrchestrationEngine:
    engine = OrchestrationEngine(
        TaskSpec("schema-recovery", tmp_path, "schema recovery")
    )
    engine.graph.add_node(
        TaskNode(
            "N1",
            NodeType.INVESTIGATION_TASK,
            "InvestigatorAgent",
            "failure_reproduction",
            "collect reproduction evidence",
        )
    )
    engine.start_node("N1")
    return engine


def _reproduction_evidence() -> Artifact:
    return Artifact(
        "N1.evidence",
        ArtifactType.EVIDENCE,
        "N1",
        {
            "mode": "failure_reproduction",
            "claim": "target test reproduces the defect",
            "source": {"path": "target.py", "line_start": 1, "line_end": 2},
            "content": "pytest failed with IndexError",
            "observation_type": "direct",
            "confidence": 1.0,
            "status": "verified",
            "tool_trace_ids": ["run-test-trace"],
            "missing_evidence": [],
            "reproduction": {
                "attempted": True,
                "succeeded": True,
                "exit_code": 1,
                "failure_type": "IndexError",
                "failure_output": "list index out of range",
                "command": ["pytest", "-q", "test_target"],
            },
        },
    )


def test_failure_reproduction_evidence_uses_nested_schema(tmp_path: Path) -> None:
    artifact = _reproduction_evidence()

    validate_worker_artifact(
        artifact,
        expected_type=ArtifactType.EVIDENCE,
    )

    assert artifact.content["reproduction"]["succeeded"] is True
    assert "reproduction_attempted" not in artifact.content
    assert "test_exit_code" not in artifact.content


def test_schema_validation_failure_is_not_retried_same_node(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    invalid_artifact = Artifact(
        "N1.invalid",
        ArtifactType.EVIDENCE,
        "N1",
        {
            "mode": "failure_reproduction",
            "claim": "invalid legacy schema",
            "source": {"path": "target.py", "line_start": 1, "line_end": 2},
            "content": "invalid",
            "observation_type": "direct",
            "confidence": 1.0,
            "status": "verified",
            "tool_trace_ids": ["trace"],
            "missing_evidence": [],
            "reproduction_attempted": True,
        },
    )

    result = collect_worker_outcome(
        engine,
        WorkerOutcome("N1", NodeStatus.SUCCEEDED, (invalid_artifact,)),
    )

    assert result.retry_scheduled is False
    assert result.details["code"] == "ARTIFACT_CONTENT_INVALID"
    assert engine.graph.get("N1").retry_count == 0
