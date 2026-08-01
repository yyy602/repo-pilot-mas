from __future__ import annotations

import json
from pathlib import Path

from repo_pilot_mas.agents import SingleAgent
from repo_pilot_mas.models import FakeModelAdapter, GenerationConfig
from repo_pilot_mas.orchestration import ReactBudget
from repo_pilot_mas.schemas import FinalReport, TaskSpec


def test_single_agent_completes_patch_validation_and_cleanup(tmp_path: Path) -> None:
    repository = _buggy_repository(tmp_path)
    patch = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(left: int, right: int) -> int:
-    return left - right
+    return left + right
"""
    model = FakeModelAdapter(
        [
            _tool_action("inspect_code", {"file_path": "calculator.py"}),
            _tool_action("apply_patch", {"patch_text": patch}),
            _final_action("success", "patch applied"),
        ],
        raw_log_dir=tmp_path / "raw",
    )
    task = _task(repository, "single-success")
    agent = SingleAgent(
        model,
        workspace_root=tmp_path / "workspaces",
        report_root=tmp_path / "reports",
        budget=ReactBudget(max_steps=5, max_runtime_seconds=30),
        generation_config=GenerationConfig(max_retries=1),
    )

    report = agent.run(task)

    assert report.status == "succeeded"
    assert report.reason == "VALIDATION_PASSED"
    assert report.patch_sha256
    assert report.changed_files == ("calculator.py",)
    assert report.model_calls == 3
    assert report.tool_calls >= 8
    assert Path(report.trace_path).is_file()
    assert Path(report.report_path).is_file()
    assert FinalReport.from_dict(json.loads(Path(report.report_path).read_text())) == report
    events = [json.loads(line) for line in Path(report.trace_path).read_text().splitlines()]
    assert {event["event_type"] for event in events} >= {
        "task_started",
        "model_call",
        "tool_call",
        "final_report",
    }
    assert (repository / "calculator.py").read_text(encoding="utf-8").endswith(
        "return left - right\n"
    )
    assert not (tmp_path / "workspaces" / task.task_id / report.workspace_id).exists()


def test_single_agent_max_steps_produces_failed_final_report(tmp_path: Path) -> None:
    repository = _buggy_repository(tmp_path)
    model = FakeModelAdapter([_tool_action("list_files", {})])
    task = _task(repository, "single-max-steps")
    agent = SingleAgent(
        model,
        workspace_root=tmp_path / "workspaces",
        report_root=tmp_path / "reports",
        budget=ReactBudget(max_steps=1, max_runtime_seconds=30),
    )

    report = agent.run(task)

    assert report.status == "failed"
    assert report.reason == "MAX_STEPS_EXHAUSTED"
    assert Path(report.report_path).is_file()
    assert report.validations["cleanup"]["ok"] is True


def test_single_agent_cannot_turn_protected_patch_into_success(tmp_path: Path) -> None:
    repository = _buggy_repository(tmp_path)
    protected_patch = """--- a/tests/test_calculator.py
+++ b/tests/test_calculator.py
@@ -3,2 +3,2 @@
 def test_add() -> None:
-    assert add(2, 3) == 5
+    assert add(2, 3) == -1
"""
    model = FakeModelAdapter(
        [
            _tool_action("apply_patch", {"patch_text": protected_patch}),
            _final_action("success", "claimed success"),
        ]
    )
    task = _task(repository, "single-protected")
    agent = SingleAgent(
        model,
        workspace_root=tmp_path / "workspaces",
        report_root=tmp_path / "reports",
        budget=ReactBudget(max_steps=3, max_runtime_seconds=30),
    )

    report = agent.run(task)

    assert report.status == "failed"
    assert report.reason == "NO_APPLIED_PATCH"
    assert "apply_patch" not in report.changed_files
    assert "assert add(2, 3) == 5" in (
        repository / "tests" / "test_calculator.py"
    ).read_text(encoding="utf-8")


def _buggy_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "buggy"
    (repository / "tests").mkdir(parents=True)
    (repository / "calculator.py").write_text(
        "def add(left: int, right: int) -> int:\n    return left - right\n",
        encoding="utf-8",
    )
    (repository / "tests" / "test_calculator.py").write_text(
        "from calculator import add\n\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    return repository


def _task(repository: Path, task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        repository_path=repository,
        issue="add returns the wrong result",
        failing_tests=("tests/test_calculator.py",),
        protected_paths=("tests",),
        max_runtime_seconds=10,
    )


def _tool_action(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "thought_summary": f"call {tool_name}",
        "action": {"type": "tool", "tool_name": tool_name, "arguments": arguments},
    }


def _final_action(status: str, reason: str) -> dict[str, object]:
    return {
        "thought_summary": "finish",
        "action": {"type": "final", "status": status, "reason": reason},
    }
