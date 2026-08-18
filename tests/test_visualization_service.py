"""Phase 7 dashboard: FastAPI service tests (阶段一/二/三)."""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from repo_pilot_mas.agents import SupervisorOutcome
from repo_pilot_mas.orchestration import (
    FakeWorkerExecutor,
    LangGraphRuntime,
)
from repo_pilot_mas.schemas import (
    CreateTaskRequest,
    DecisionAction,
    SupervisorDecision,
    TaskSpec,
)
from repo_pilot_mas.visualization.runner import WebTaskRunner
from repo_pilot_mas.visualization.service import build_app


def test_health(fake_run: Path) -> None:
    app = build_app(fake_run / "evaluation")
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}


def test_runs_list(fake_run: Path) -> None:
    app = build_app(
        fake_run / "evaluation",
        demo_root=fake_run / "no_demo",
        web_root=fake_run / "no_web",
    )
    client = TestClient(app)
    payload = client.get("/runs").json()
    assert [run["run_id"] for run in payload["runs"]] == ["fake_run"]


def test_run_detail(fake_run: Path) -> None:
    app = build_app(fake_run / "evaluation")
    client = TestClient(app)
    payload = client.get("/runs/fake_run").json()
    assert payload["split"] == "development"
    assert payload["solved"] == 1
    assert payload["termination_failure_classes"] == {"none": 1}


def test_task_detail(fake_run: Path) -> None:
    app = build_app(fake_run / "evaluation")
    client = TestClient(app)
    payload = client.get("/runs/fake_run/tasks/quixbugs_gcd").json()
    assert payload["status"] == "succeeded"
    assert payload["node_timeline"]["N1"][-1]["status"] == "SUCCEEDED"
    assert len(payload["decisions"]) == 1
    assert len(payload["model_calls"]) == 1


def test_compare(fake_run: Path) -> None:
    app = build_app(fake_run / "evaluation")
    client = TestClient(app)
    payload = client.get("/runs/fake_run/compare").json()
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["task_id"] == "quixbugs_gcd"
    assert payload["tasks"][0]["failure_class"] == "none"


def test_unknown_run_404(fake_run: Path) -> None:
    app = build_app(fake_run / "evaluation")
    client = TestClient(app)
    assert client.get("/runs/missing").status_code == 404


def _fake_runner(tmp_path: Path) -> WebTaskRunner:
    def fake_executor(*, task: TaskSpec, run_id: str, run_root: Path) -> None:
        task_root = run_root / "tasks" / task.task_id
        import json

        (task_root / "result.json").write_text(
            json.dumps(
                {
                    "task_id": task.task_id,
                    "status": "succeeded",
                    "termination": {"code": "VALIDATION_PASSED", "failure_class": "none"},
                    "nodes": [],
                    "artifacts": [],
                }
            ),
            encoding="utf-8",
        )

    return WebTaskRunner(
        model_config=Path("configs/model.yaml"),
        supervisor_config=Path("configs/supervisor.yaml"),
        runtime_config=Path("configs/runtime.yaml"),
        phase6_config=Path("configs/phase6.yaml"),
        web_runs_root=tmp_path / "web_runs",
        task_executor=fake_executor,
    )


def test_stage2_submit_and_status(fake_run: Path, tmp_path: Path) -> None:
    runner = _fake_runner(tmp_path)
    app = build_app(
        fake_run / "evaluation",
        demo_root=fake_run / "no_demo",
        runner=runner,
    )
    client = TestClient(app)
    response = client.post("/runs", json={"task_id": "quixbugs_gcd"})
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    record: dict = {}
    for _ in range(100):
        record = client.get(f"/runs/{run_id}/status").json()
        if record["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert record["status"] == "done"
    assert client.get("/runs").json()  # 只读列表仍可用
    runner.close()


def test_stage2_unknown_task_400(fake_run: Path, tmp_path: Path) -> None:
    runner = _fake_runner(tmp_path)
    app = build_app(fake_run / "evaluation", runner=runner)
    client = TestClient(app)
    assert client.post("/runs", json={"task_id": "not_a_real_task"}).status_code == 400
    runner.close()


def test_stage2_requires_runner(fake_run: Path) -> None:
    app = build_app(fake_run / "evaluation")
    client = TestClient(app)
    assert client.post("/runs", json={"task_id": "quixbugs_gcd"}).status_code == 503
    assert client.get("/runs/web-any/status").status_code == 503


class _StateAwareSupervisor:
    """确定性 Supervisor：先 CREATE_TASK，再 TERMINATE_TASK。"""

    def decide(self, snapshot: Mapping[str, Any]) -> SupervisorOutcome:
        if not snapshot["nodes"]:
            decision = SupervisorDecision(
                "runtime-create",
                DecisionAction.CREATE_TASK,
                "创建可执行调查任务",
                create_tasks=(
                    CreateTaskRequest(
                        "INVESTIGATION_TASK",
                        "InvestigatorAgent",
                        "code_retrieval",
                        "执行调查",
                    ),
                ),
                next_workflow_stage="investigation",
            )
        else:
            decision = SupervisorDecision(
                "runtime-stop",
                DecisionAction.TERMINATE_TASK,
                "运行时闭环验收结束",
            )
        return SupervisorOutcome(decision)


def _approval_runner(tmp_path: Path, worker: FakeWorkerExecutor) -> WebTaskRunner:
    def runtime_factory(*, checkpoint_db: Path, trace: Any):
        return LangGraphRuntime(
            supervisor=_StateAwareSupervisor(),
            worker_executor=worker,
            checkpoint_db=checkpoint_db,
            trace_writer=trace,
        )

    return WebTaskRunner(
        model_config=Path("configs/model.yaml"),
        supervisor_config=Path("configs/supervisor.yaml"),
        runtime_config=Path("configs/runtime.yaml"),
        phase6_config=Path("configs/phase6.yaml"),
        web_runs_root=tmp_path / "web_runs",
        runtime_factory=runtime_factory,
    )


def test_stage3_approval_endpoints(fake_run: Path, tmp_path: Path) -> None:
    worker = FakeWorkerExecutor()
    runner = _approval_runner(tmp_path, worker)
    app = build_app(
        fake_run / "evaluation",
        demo_root=fake_run / "no_demo",
        runner=runner,
    )
    client = TestClient(app)
    response = client.post(
        "/runs",
        json={"task_id": "quixbugs_gcd", "require_human_approval": True},
    )
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    pending: dict = {}
    for _ in range(100):
        resp = client.get(f"/runs/{run_id}/pending")
        if resp.status_code == 200:
            pending = resp.json()
            break
        time.sleep(0.05)
    assert pending["decision_id"] == "runtime-create"
    assert worker.calls == []
    assert client.get("/approvals").json()["approvals"]
    assert (
        client.post(f"/runs/{run_id}/approve", json={"approved": True}).status_code
        == 200
    )
    record: dict = {}
    for _ in range(100):
        record = client.get(f"/runs/{run_id}/status").json()
        if record["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert record["status"] == "done"
    assert worker.calls == ["N1"]
    runner.close()


def test_stage3_approve_without_pending_409(fake_run: Path, tmp_path: Path) -> None:
    runner = _approval_runner(tmp_path, FakeWorkerExecutor())
    app = build_app(fake_run / "evaluation", runner=runner)
    client = TestClient(app)
    assert (
        client.post("/runs/web-ghost/approve", json={"approved": True}).status_code
        == 409
    )
    runner.close()
