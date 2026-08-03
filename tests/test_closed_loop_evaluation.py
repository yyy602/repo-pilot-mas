from __future__ import annotations

from repo_pilot_mas.evaluation import load_evaluation_suite


def test_closed_loop_suite_contains_twenty_tasks() -> None:
    suite = load_evaluation_suite("data/quixbugs/closed_loop_suite.json")
    assert len(suite.test_tasks) == 20
    assert len({task.task_id for task in suite.test_tasks}) == 20


def test_closed_loop_suite_contains_quixbugs_tasks() -> None:
    suite = load_evaluation_suite("data/quixbugs/closed_loop_suite.json")
    assert {task.metadata["dataset"] for task in suite.test_tasks} == {"quixbugs"}
