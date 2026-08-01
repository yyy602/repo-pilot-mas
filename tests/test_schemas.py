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
        target_files=("src/math_utils.py",),
        protected_paths=("tests", "pyproject.toml"),
        max_runtime_seconds=90,
        metadata={"source": "fixture"},
    )

    restored = TaskSpec.from_dict(spec.to_dict())

    assert restored == spec
    assert restored.test_command == ("python", "-m", "pytest", "-q")
    assert restored.max_runtime_seconds == 90.0


def test_task_spec_rejects_unsafe_identifier(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TaskSpec(task_id="../escape", repository_path=tmp_path, issue="bad")


def test_task_spec_rejects_string_instead_of_string_sequence(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        TaskSpec.from_dict(
            {
                "task_id": "bug-001",
                "repository_path": tmp_path,
                "issue": "bad failing_tests type",
                "failing_tests": "tests/test_bug.py",
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("failing_tests", ("../outside.py",)),
        ("target_files", ("/etc/passwd",)),
        ("protected_paths", (".git/config",)),
    ],
)
def test_task_spec_rejects_unsafe_repository_paths(
    tmp_path: Path,
    field: str,
    value: tuple[str, ...],
) -> None:
    arguments = {
        "task_id": "bug-001",
        "repository_path": tmp_path,
        "issue": "unsafe path",
        field: value,
    }

    with pytest.raises(ValueError):
        TaskSpec(**arguments)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_task_spec_rejects_invalid_runtime_limit(tmp_path: Path, value: float) -> None:
    with pytest.raises(ValueError):
        TaskSpec(
            task_id="bug-001",
            repository_path=tmp_path,
            issue="invalid runtime",
            max_runtime_seconds=value,
        )


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
