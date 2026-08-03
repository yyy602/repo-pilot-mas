from __future__ import annotations

import json
from pathlib import Path

from repo_pilot_mas.agents import InvestigatorAgent
from repo_pilot_mas.models import FakeModelAdapter
from repo_pilot_mas.orchestration import (
    NodeStatus,
    NodeType,
    OrchestrationEngine,
    TaskNode,
    WorkerOutcome,
)
from repo_pilot_mas.orchestration.worker_recovery import collect_worker_outcome
from repo_pilot_mas.runtime import TraceWriter
from repo_pilot_mas.schemas import ArtifactType, TaskSpec, ToolResult
from repo_pilot_mas.tools.registry import ToolDefinition, ToolRegistry


def _task(tmp_path: Path) -> TaskSpec:
    return TaskSpec(
        "schema-recovery",
        tmp_path,
        "reproduce the target failure",
    )


def _engine(tmp_path: Path) -> OrchestrationEngine:
    engine = OrchestrationEngine(_task(tmp_path))
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


def _reproduction_tools() -> ToolRegistry:
    registry = ToolRegistry()
    open_schema = {
        "type": "object",
        "additionalProperties": True,
    }
    for name in ("list_files", "search_code", "inspect_code"):
        registry.register(
            ToolDefinition(
                name,
                name,
                open_schema,
                lambda _args, tool=name: ToolResult.success(
                    tool,
                    data={"text": "target.py:1-2"},
                    trace_id=f"{tool}-trace",
                ),
            )
        )
    registry.register(
        ToolDefinition(
            "run_tests",
            "run target tests",
            {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["target", "full"],
                    }
                },
                "required": ["scope"],
                "additionalProperties": False,
            },
            lambda _args: ToolResult.failure(
                "run_tests",
                code="TEST_FAILED",
                message="target test failed",
                data={"timed_out": False, "passed": False},
                stdout=(
                    "FAILED test_target.py::test_above_range\n"
                    "E   IndexError: list index out of range"
                ),
                exit_code=1,
                command=("python", "-m", "pytest", "-q", "test_target.py"),
                trace_id="run-tests-trace",
            ),
        )
    )
    return registry


def _events(path: Path, event_type: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        if payload["event_type"] == event_type:
            events.append(payload["data"])
    return events


def test_investigator_uses_tool_observation_for_nested_reproduction(
    tmp_path: Path,
) -> None:
    model = FakeModelAdapter(
        [
            {
                "thought_summary": "run the target test",
                "action": {
                    "type": "tool",
                    "tool_name": "run_tests",
                    "arguments": {"scope": "target"},
                },
            },
            {
                "thought_summary": "return reproduction evidence",
                "action": {
                    "type": "final",
                    "status": "failure",
                    "reason": "model status is overridden by deterministic test evidence",
                    "artifact": {
                        "mode": "failure_reproduction",
                        "claim": "the target test reproduces the defect",
                        "source": {
                            "path": "target.py",
                            "line_start": 1,
                            "line_end": 2,
                        },
                        "content": "the target test raises IndexError",
                        "observation_type": "direct",
                        "confidence": 1.0,
                        "status": "verified",
                        "tool_trace_ids": ["run-tests-trace"],
                        "missing_evidence": [],
                        "reproduction": {
                            "attempted": False,
                            "succeeded": False,
                            "exit_code": 0,
                            "failure_type": "",
                            "failure_output": "model placeholder",
                            "command": ["pytest"],
                        },
                    },
                },
            },
        ]
    )
    trace_path = tmp_path / "investigator-trace.jsonl"

    artifact = InvestigatorAgent(
        model,
        _reproduction_tools(),
        trace_writer=TraceWriter(trace_path),
    ).run(
        _task(tmp_path),
        "N1",
        "failure_reproduction",
        "reproduce the target failure",
    )

    reproduction = artifact.content["reproduction"]
    assert artifact.artifact_type is ArtifactType.EVIDENCE
    assert reproduction["attempted"] is True
    assert reproduction["succeeded"] is True
    assert reproduction["exit_code"] == 1
    assert reproduction["failure_type"] == "IndexError"
    assert reproduction["command"] == (
        "python",
        "-m",
        "pytest",
        "-q",
        "test_target.py",
    )
    assert "reproduction_attempted" not in artifact.content
    assert "reproduction_succeeded" not in artifact.content
    assert "test_exit_code" not in artifact.content
    assert "failing_command" not in artifact.content

    confirmed = _events(trace_path, "bug_reproduction_confirmed")
    assert len(confirmed) == 1
    assert confirmed[0]["node_id"] == "N1"
    assert confirmed[0]["failure_type"] == "IndexError"
    assert confirmed[0]["test_exit_code"] == 1


def test_worker_schema_validation_failure_is_not_retried_same_node(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    outcome = WorkerOutcome(
        "N1",
        NodeStatus.FAILED,
        (),
        reason=(
            "WORKER_AGENT_ERROR:SchemaValidationError:"
            "$.reproduction_attempted: additional property is not allowed"
        ),
    )

    result = collect_worker_outcome(engine, outcome)

    assert result.retry_scheduled is False
    assert result.details["code"] == "ARTIFACT_SCHEMA_ERROR"
    assert result.details["recovery_class"] == "artifact_validation"
    assert result.details["recovery_action"] == "return_to_supervisor"
    assert engine.graph.get("N1").retry_count == 0
    assert result.rejection_ref is not None

    rejection = engine.blackboard.artifacts.get(result.rejection_ref)
    assert rejection.content["code"] == "ARTIFACT_SCHEMA_ERROR"
    assert "RETRY_TASK" not in rejection.content["allowed_next_actions"]
    assert set(rejection.content["allowed_next_actions"]) == {
        "CREATE_TASK",
        "REQUEST_REPLAN",
    }
