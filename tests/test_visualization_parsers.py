"""Phase 7 dashboard: parser tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repo_pilot_mas.visualization.parsers import (
    build_decision_events,
    build_model_call_events,
    build_node_timeline,
    discover_runs,
    load_run,
    load_task,
    read_json,
    read_jsonl,
)


def test_discover_and_load_run(fake_run: Path) -> None:
    runs = discover_runs(
        fake_run / "evaluation",
        demo_root=fake_run / "no_demo",
        web_root=fake_run / "no_web",
    )
    assert len(runs) == 1
    run = runs[0]
    assert run.run_id == "fake_run"
    assert run.split == "development"
    assert run.frozen is False
    assert run.passed is True
    assert run.solved == 1
    assert run.task_ids == ("quixbugs_gcd",)
    assert load_run("fake_run", fake_run / "evaluation").run_id == "fake_run"


def test_load_task(fake_run: Path) -> None:
    task = load_task("fake_run", "quixbugs_gcd", fake_run / "evaluation")
    assert task.task_id == "quixbugs_gcd"
    assert task.status == "succeeded"
    assert task.termination["code"] == "VALIDATION_PASSED"
    assert len(task.events) == 4
    assert task.nodes[0]["node_id"] == "N1"
    assert task.artifacts[0]["artifact_type"] == "evidence"


def test_node_timeline(fake_run: Path) -> None:
    task = load_task("fake_run", "quixbugs_gcd", fake_run / "evaluation")
    timeline = build_node_timeline(task.events)
    assert timeline["N1"] == [
        {"state_version": 2, "status": "RUNNING", "started_at": None, "finished_at": None, "failure_reason": None},
        {"state_version": 3, "status": "SUCCEEDED", "started_at": None, "finished_at": None, "failure_reason": None},
    ]


def test_event_selectors(fake_run: Path) -> None:
    task = load_task("fake_run", "quixbugs_gcd", fake_run / "evaluation")
    assert len(build_decision_events(task.events)) == 1
    assert len(build_model_call_events(task.events)) == 1


def test_read_helpers(fake_run: Path) -> None:
    manifest = read_json(fake_run / "evaluation" / "fake_run" / "manifest.json")
    assert manifest["evaluation_split"] == "development"
    events = read_jsonl(
        fake_run / "evaluation" / "fake_run" / "tasks" / "quixbugs_gcd" / "trace.jsonl"
    )
    assert len(events) == 4
    # 合法对象可解析；非对象根触发 TypeError
    summary = read_json(fake_run / "evaluation" / "fake_run" / "summary.json")
    assert summary["solved"] == 1
    bad = fake_run / "bad.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(TypeError):
        read_json(bad)


def test_load_unknown_run_raises(fake_run: Path) -> None:
    with pytest.raises(KeyError):
        load_run("missing_run", fake_run / "evaluation")
    with pytest.raises(KeyError):
        load_task("missing_run", "quixbugs_gcd", fake_run / "evaluation")


def test_discover_web_runs(fake_run: Path) -> None:
    web_root = fake_run / "web_runs"
    run_root = web_root / "web-1"
    (run_root / "tasks" / "quixbugs_gcd").mkdir(parents=True)
    (run_root / "run.json").write_text(
        json.dumps(
            {
                "run_id": "web-1",
                "evaluation_split": "web",
                "status": "done",
            }
        ),
        encoding="utf-8",
    )
    (run_root / "tasks" / "quixbugs_gcd" / "result.json").write_text(
        json.dumps({"task_id": "quixbugs_gcd", "status": "succeeded"}),
        encoding="utf-8",
    )
    runs = discover_runs(
        fake_run / "evaluation",
        demo_root=fake_run / "no_demo",
        web_root=web_root,
    )
    web_runs = [run for run in runs if run.run_id == "web-1"]
    assert len(web_runs) == 1
    assert web_runs[0].split == "web"
    assert web_runs[0].task_ids == ("quixbugs_gcd",)
    assert web_runs[0].solved == 1
    assert web_runs[0].task_count == 1
    task = load_task(
        "web-1",
        "quixbugs_gcd",
        fake_run / "evaluation",
        web_root=web_root,
    )
    assert task.status == "succeeded"


def test_discover_running_web_run_without_result(fake_run: Path) -> None:
    """运行中 web 任务只有 trace.jsonl（尚无 result.json）也必须可见。"""

    web_root = fake_run / "web_runs"
    run_root = web_root / "web-running"
    (run_root / "tasks" / "quixbugs_gcd").mkdir(parents=True)
    (run_root / "run.json").write_text(
        json.dumps(
            {
                "run_id": "web-running",
                "evaluation_split": "web",
                "status": "running",
            }
        ),
        encoding="utf-8",
    )
    (run_root / "tasks" / "quixbugs_gcd" / "trace.jsonl").write_text(
        json.dumps(
            {"event_type": "node_state_changed", "data": {"state_version": 2, "node": {"node_id": "N1", "status": "RUNNING"}}}
        )
        + "\n",
        encoding="utf-8",
    )
    runs = discover_runs(
        fake_run / "evaluation",
        demo_root=fake_run / "no_demo",
        web_root=web_root,
    )
    running = next(run for run in runs if run.run_id == "web-running")
    assert running.status == "running"
    assert running.task_ids == ("quixbugs_gcd",)
    assert running.solved == 0
    assert running.task_count == 1
    task = load_task(
        "web-running",
        "quixbugs_gcd",
        fake_run / "evaluation",
        web_root=web_root,
    )
    assert task.result is None
    assert len(task.events) == 1
