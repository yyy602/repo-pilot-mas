from pathlib import Path

from repo_pilot_mas.tasks import load_tasks
from repo_pilot_mas.tools import run_tests
from repo_pilot_mas.tools.registry import task_test_command


def test_phase6_quixbugs_tasks_are_valid_and_reproduce_failures() -> None:
    root = Path(__file__).parents[1]
    tasks = load_tasks(root / "data" / "quixbugs" / "tasks")

    assert len(tasks) == 15
    assert all(task.metadata["dataset"] == "quixbugs" for task in tasks)
    for task in tasks:
        result = run_tests(
            task.repository_path,
            command=task_test_command(task, target=True),
            timeout_seconds=task.max_runtime_seconds,
        )
        assert not result.ok
        assert result.error is not None
        assert result.error.code in {"TEST_FAILED", "TEST_TIMEOUT"}
