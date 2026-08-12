from __future__ import annotations

import json
from pathlib import Path

import pytest

from repo_pilot_mas.agents import InvestigatorAgent, WorkerAgentError
from repo_pilot_mas.agents.investigator import _reproduction_observation
from repo_pilot_mas.models import FakeModelAdapter, Message
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


def _reproduction_tools(
    run_tests_result: ToolResult | None = None,
) -> ToolRegistry:
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
            lambda _args: run_tests_result
            or ToolResult.failure(
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


def test_reproduction_failure_is_successful_evidence(
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
                        "evidence_kind": "reproduction",
                        "claim": "the target test reproduces the defect",
                        "supports_claims": ["the target test reproduces the defect"],
                        "contradicts_claims": [],
                        "verified": True,
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


def test_reproduction_evidence_contains_exit_code_and_output() -> None:
    result = ToolResult.failure(
        "run_tests",
        code="TEST_FAILED",
        message="target failed",
        data={"timed_out": False, "passed": False},
        stdout="E   IndexError: list index out of range",
        exit_code=1,
        command=("python", "-m", "pytest", "-q", "test_target.py"),
        trace_id="trace-reproduction-fields",
    )
    reproduction = _reproduction_observation(
        (
            Message(
                "user",
                "工具执行结果："
                + json.dumps(result.to_dict(), ensure_ascii=False),
            ),
        )
    )

    assert reproduction is not None
    assert reproduction["exit_code"] == 1
    assert reproduction["failure_type"] == "IndexError"
    assert "list index out of range" in reproduction["failure_output"]


def test_tool_failure_is_worker_failure(tmp_path: Path) -> None:
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
                "thought_summary": "incorrectly claim success",
                "action": {
                    "type": "final",
                    "status": "success",
                    "reason": "done",
                    "artifact": {
                        "mode": "failure_reproduction",
                        "claim": "reproduced",
                        "source": {
                            "path": "target.py",
                            "line_start": 1,
                            "line_end": 2,
                        },
                        "content": "tool failed before running tests",
                        "observation_type": "direct",
                        "confidence": 1.0,
                        "status": "verified",
                        "tool_trace_ids": ["run-tests-error-trace"],
                        "missing_evidence": [],
                    },
                },
            },
        ]
    )
    tools = _reproduction_tools(
        ToolResult.failure(
            "run_tests",
            code="RUN_TESTS_ERROR",
            message="pytest executable failed to start",
            data={"timed_out": False, "passed": False},
            stderr="could not start pytest",
            exit_code=2,
            command=("python", "-m", "pytest", "-q", "test_target.py"),
            trace_id="run-tests-error-trace",
        )
    )

    with pytest.raises(WorkerAgentError) as captured:
        InvestigatorAgent(model, tools).run(
            _task(tmp_path),
            "N-tool-error",
            "failure_reproduction",
            "reproduce the target failure",
        )

    assert captured.value.code == "TOOL_EXECUTION_ERROR"


def test_worker_schema_validation_failure_gets_one_format_retry(
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

    assert result.retry_scheduled is True
    assert result.details["code"] == "ARTIFACT_SCHEMA_ERROR"
    assert result.details["recovery_class"] == "model_format"
    assert result.details["recovery_action"] == "retry_same_node"
    assert engine.graph.get("N1").retry_count == 1
    assert result.rejection_ref is not None

    rejection = engine.blackboard.artifacts.get(result.rejection_ref)
    assert rejection.content["code"] == "ARTIFACT_SCHEMA_ERROR"
    assert set(rejection.content["allowed_next_actions"]) == {
        "RETRY_TASK",
        "CREATE_TASK",
        "REQUEST_REPLAN",
    }


def test_transient_worker_error_can_retry(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    outcome = WorkerOutcome(
        "N1",
        NodeStatus.FAILED,
        (),
        reason="TRANSIENT_WORKER_ERROR:ConnectionError:temporary provider failure",
    )

    result = collect_worker_outcome(engine, outcome)

    assert result.retry_scheduled is True
    assert result.details["code"] == "TRANSIENT_WORKER_ERROR"
    assert result.details["recovery_class"] == "transient_worker"
    assert engine.graph.get("N1").retry_count == 1


def test_contract_error_does_not_retry_same_node(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    result = collect_worker_outcome(
        engine,
        WorkerOutcome(
            "N1",
            NodeStatus.FAILED,
            reason=(
                "AGENT_INPUT_CONTRACT_VIOLATION:"
                "Reviewer requires direct Evidence"
            ),
        ),
    )

    assert result.retry_scheduled is False
    assert result.details["code"] == "AGENT_INPUT_CONTRACT_VIOLATION"
    assert result.details["recovery_class"] == "input_contract"
    assert result.details["recovery_action"] == "return_to_supervisor"
    assert engine.graph.get("N1").retry_count == 0


def test_business_evidence_error_does_not_retry_same_node(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    outcome = WorkerOutcome(
        "N1",
        NodeStatus.FAILED,
        (),
        reason=(
            "BUSINESS_EVIDENCE_INSUFFICIENT:WorkerAgentError:"
            "failure_reproduction did not execute run_tests"
        ),
    )

    result = collect_worker_outcome(engine, outcome)

    assert result.retry_scheduled is False
    assert result.details["code"] == "BUSINESS_EVIDENCE_INSUFFICIENT"
    assert result.details["recovery_class"] == "business_evidence"
    assert result.details["recovery_action"] == "return_to_supervisor"
    assert engine.graph.get("N1").retry_count == 0
