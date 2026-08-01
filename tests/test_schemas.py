from pathlib import Path

import pytest

from repo_pilot_mas.schemas import TaskSpec, ToolResult


def test_task_spec_round_trip(tmp_path: Path) -> None:
    spec = TaskSpec(
        task_id="bug-001",
        repository_path=tmp_path,
        issue="Fix addition",
        failing_tests=("tests/test_math.py",),
        acceptance_criteria=("all tests pass",),
        metadata={"source": "fixture"},
    )

    restored = TaskSpec.from_dict(spec.to_dict())

    assert restored == spec
    assert restored.test_command == ("python", "-m", "pytest", "-q")


def test_task_spec_rejects_unsafe_identifier(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TaskSpec(task_id="../escape", repository_path=tmp_path, issue="bad")


def test_tool_result_serialization() -> None:
    result = ToolResult.failure(
        "demo",
        code="DEMO_ERROR",
        message="failed",
        details={"attempt": 1},
        command=("python", "-m", "pytest"),
    )

    payload = result.to_dict()

    assert payload["ok"] is False
    assert payload["error"]["code"] == "DEMO_ERROR"
    assert payload["command"] == ["python", "-m", "pytest"]
    assert payload["trace_id"]
