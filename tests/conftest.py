from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    root = tmp_path / "sample_repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / ".git").mkdir()
    (root / "src" / "math_utils.py").write_text(
        "def add(left: int, right: int) -> int:\n"
        "    return left + right\n\n\n"
        "class Calculator:\n"
        "    def total(self, values: list[int]) -> int:\n"
        "        return sum(values)\n\n\n"
        "def use_add() -> int:\n"
        "    return add(1, 2)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_math.py").write_text(
        "from src.math_utils import add\n\n\n"
        "def test_add() -> None:\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("sample repository\n", encoding="utf-8")
    (root / "binary.dat").write_bytes(b"abc\x00def")
    (root / ".git" / "config").write_text("secret\n", encoding="utf-8")
    return root


@pytest.fixture()
def fake_run(tmp_path: Path) -> Path:
    """Build a minimal evaluation run directory tree for dashboard tests."""

    run_root = tmp_path / "evaluation" / "fake_run"
    (run_root / "tasks" / "quixbugs_gcd").mkdir(parents=True)
    (run_root / "manifest.json").write_text(
        json.dumps(
            {
                "evaluation_split": "development",
                "started_at": "2026-08-17T00:00:00+00:00",
                "frozen": False,
                "repository_commit": "abc12345",
            }
        ),
        encoding="utf-8",
    )
    (run_root / "summary.json").write_text(
        json.dumps(
            {
                "task_count": 1,
                "solved": 1,
                "termination_failure_classes": {"none": 1},
                "task_result_paths": [
                    "tasks/quixbugs_gcd/result.json",
                ],
            }
        ),
        encoding="utf-8",
    )
    (run_root / "acceptance.json").write_text(
        json.dumps({"passed": True, "frozen": False}),
        encoding="utf-8",
    )
    result = {
        "task_id": "quixbugs_gcd",
        "status": "succeeded",
        "termination": {
            "code": "VALIDATION_PASSED",
            "stage": "finalization",
            "failure_class": "none",
        },
        "nodes": [
            {
                "node_id": "N1",
                "node_type": "INVESTIGATION_TASK",
                "status": "SUCCEEDED",
                "dependencies": [],
                "input_artifact_ids": [],
            }
        ],
        "artifacts": [
            {
                "artifact_id": "N1.evidence",
                "artifact_type": "evidence",
                "version": 1,
                "content": {"claim": "复现成功"},
            }
        ],
        "usage": {"duration_ms": 1000, "total_tokens": 100},
    }
    (run_root / "tasks" / "quixbugs_gcd" / "result.json").write_text(
        json.dumps(result),
        encoding="utf-8",
    )
    events = [
        {
            "event_type": "supervisor_decision_applied",
            "created_at": "t0",
            "data": {
                "decision": {"action": "CREATE_TASK", "reason": "start"},
                "state_version": 1,
            },
        },
        {
            "event_type": "node_state_changed",
            "created_at": "t1",
            "data": {
                "state_version": 2,
                "node": {"node_id": "N1", "status": "RUNNING"},
            },
        },
        {
            "event_type": "node_finished",
            "created_at": "t2",
            "data": {
                "state_version": 3,
                "node": {"node_id": "N1", "status": "SUCCEEDED"},
            },
        },
        {
            "event_type": "model_call",
            "created_at": "t3",
            "data": {
                "model_id": "Qwen3-8B",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        },
    ]
    (run_root / "tasks" / "quixbugs_gcd" / "trace.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events),
        encoding="utf-8",
    )
    return tmp_path
