"""Phase 7 阶段二/三：WebTaskRunner 状态机与审批流程测试。

阶段二：注入 fake task_executor（不加载模型）；
阶段三：注入 async runtime_factory（FakeWorkerExecutor + 确定性 Supervisor），
真跑 LangGraph interrupt/resume 审批流。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
from repo_pilot_mas.visualization.runner import WebTaskRunner, available_web_tasks


def _task(tmp_path: Path, task_id: str = "quixbugs_gcd") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        repository_path=str(tmp_path / "repo"),
        issue="demo",
        failing_tests=("tests/test_demo.py",),
        acceptance_criteria=("target test passes",),
        test_command=("python", "-m", "pytest", "-q"),
        protected_paths=("tests",),
        max_runtime_seconds=60,
    )


class StateAwareSupervisor:
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


def _wait_for_finish(runner: WebTaskRunner, run_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = runner.status(run_id)
        if record and record["status"] in ("done", "failed"):
            return record
        time.sleep(0.05)
    raise AssertionError("runner 状态未在超时前收敛")


def test_submit_runs_to_done(tmp_path: Path) -> None:
    calls: list[dict] = []

    def fake_executor(*, task: TaskSpec, run_id: str, run_root: Path) -> None:
        calls.append({"task_id": task.task_id, "run_id": run_id})
        task_root = run_root / "tasks" / task.task_id
        (task_root / "result.json").write_text(
            json.dumps(
                {
                    "task_id": task.task_id,
                    "status": "succeeded",
                    "termination": {
                        "code": "VALIDATION_PASSED",
                        "failure_class": "none",
                    },
                    "nodes": [],
                    "artifacts": [],
                }
            ),
            encoding="utf-8",
        )

    runner = WebTaskRunner(
        model_config=Path("configs/model.yaml"),
        supervisor_config=Path("configs/supervisor.yaml"),
        runtime_config=Path("configs/runtime.yaml"),
        phase6_config=Path("configs/phase6.yaml"),
        web_runs_root=tmp_path / "web_runs",
        task_executor=fake_executor,
    )
    task = _task(tmp_path)
    run_id = runner.submit(task, run_id="web-test-1")
    record = _wait_for_finish(runner, run_id)
    assert record["status"] == "done"
    assert record["task_id"] == "quixbugs_gcd"
    assert calls == [{"task_id": "quixbugs_gcd", "run_id": "web-test-1"}]
    run_root = tmp_path / "web_runs" / "web-test-1"
    assert (run_root / "run.json").is_file()
    manifest = json.loads((run_root / "run.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "done"
    assert (run_root / "tasks" / "quixbugs_gcd" / "result.json").is_file()
    runner.close()


def test_submit_is_serial(tmp_path: Path) -> None:
    release = {"flag": False}

    def slow_executor(*, task: TaskSpec, run_id: str, run_root: Path) -> None:
        while not release["flag"]:
            time.sleep(0.02)

    runner = WebTaskRunner(
        model_config=Path("configs/model.yaml"),
        supervisor_config=Path("configs/supervisor.yaml"),
        runtime_config=Path("configs/runtime.yaml"),
        phase6_config=Path("configs/phase6.yaml"),
        web_runs_root=tmp_path / "web_runs",
        task_executor=slow_executor,
    )
    first = runner.submit(_task(tmp_path, "quixbugs_gcd"), run_id="web-a")
    time.sleep(0.2)
    second = runner.submit(_task(tmp_path, "quixbugs_flatten"), run_id="web-b")
    assert runner.status(second)["status"] == "failed"
    assert "串行" in runner.status(second)["error"]
    release["flag"] = True
    _wait_for_finish(runner, first)
    assert runner.status(first)["status"] == "done"
    runner.close()


def test_executor_failure_marks_failed(tmp_path: Path) -> None:
    def failing_executor(*, task: TaskSpec, run_id: str, run_root: Path) -> None:
        raise RuntimeError("boom")

    runner = WebTaskRunner(
        model_config=Path("configs/model.yaml"),
        supervisor_config=Path("configs/supervisor.yaml"),
        runtime_config=Path("configs/runtime.yaml"),
        phase6_config=Path("configs/phase6.yaml"),
        web_runs_root=tmp_path / "web_runs",
        task_executor=failing_executor,
    )
    run_id = runner.submit(_task(tmp_path), run_id="web-fail")
    record = _wait_for_finish(runner, run_id)
    assert record["status"] == "failed"
    assert "boom" in record["error"]
    runner.close()


def test_submit_rejects_human_approval(tmp_path: Path) -> None:
    # 阶段三起允许 require_human_approval=True（不再拒绝）
    runner = WebTaskRunner(
        model_config=Path("configs/model.yaml"),
        supervisor_config=Path("configs/supervisor.yaml"),
        runtime_config=Path("configs/runtime.yaml"),
        phase6_config=Path("configs/phase6.yaml"),
        web_runs_root=tmp_path / "web_runs",
        task_executor=lambda **_: None,
    )
    try:
        run_id = runner.submit(_task(tmp_path), require_human_approval=True)
        _wait_for_finish(runner, run_id)
        assert runner.status(run_id)["status"] == "done"
    finally:
        runner.close()


def _wait_for_pending(runner: WebTaskRunner, run_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = runner.pending_approval(run_id)
        if item is not None:
            return item
        time.sleep(0.05)
    raise AssertionError("审批未在超时前挂起")


def _approval_runner(tmp_path: Path, worker: FakeWorkerExecutor) -> WebTaskRunner:
    def runtime_factory(*, checkpoint_db: Path, trace: Any):
        return LangGraphRuntime(
            supervisor=StateAwareSupervisor(),
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


def test_approval_pauses_then_resumes(tmp_path: Path) -> None:
    worker = FakeWorkerExecutor()
    runner = _approval_runner(tmp_path, worker)
    run_id = runner.submit(
        _task(tmp_path),
        run_id="web-approve",
        require_human_approval=True,
    )
    pending = _wait_for_pending(runner, run_id)
    assert pending["decision_id"] == "runtime-create"
    assert worker.calls == []  # 未批准前不派发 Worker
    assert runner.decide(run_id, approved=True)
    record = _wait_for_finish(runner, run_id)
    assert record["status"] == "done"
    assert worker.calls == ["N1"]  # 批准后继续派发
    assert (tmp_path / "web_runs" / "web-approve" / "tasks" / "quixbugs_gcd" / "result.json").is_file()
    runner.close()


def test_approval_reject_stops_dispatch(tmp_path: Path) -> None:
    worker = FakeWorkerExecutor()
    runner = _approval_runner(tmp_path, worker)
    run_id = runner.submit(
        _task(tmp_path),
        run_id="web-reject",
        require_human_approval=True,
    )
    pending = _wait_for_pending(runner, run_id)
    assert pending["decision_id"] == "runtime-create"
    assert runner.decide(run_id, approved=False)
    record = _wait_for_finish(runner, run_id)
    assert record["status"] == "done"
    assert worker.calls == []  # 拒绝后不派发
    result = json.loads(
        (tmp_path / "web_runs" / "web-reject" / "tasks" / "quixbugs_gcd" / "result.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "failed"  # human_rejected 路径结构化收尾
    runner.close()


def test_decide_without_pending_returns_false(tmp_path: Path) -> None:
    runner = _approval_runner(tmp_path, FakeWorkerExecutor())
    assert runner.decide("web-ghost", approved=True) is False
    assert runner.approvals_all() == ()
    runner.close()


def test_duplicate_run_id_rejected(tmp_path: Path) -> None:
    runner = WebTaskRunner(
        model_config=Path("configs/model.yaml"),
        supervisor_config=Path("configs/supervisor.yaml"),
        runtime_config=Path("configs/runtime.yaml"),
        phase6_config=Path("configs/phase6.yaml"),
        web_runs_root=tmp_path / "web_runs",
        task_executor=lambda **_: None,
    )
    try:
        runner.submit(_task(tmp_path), run_id="web-dup")
        try:
            runner.submit(_task(tmp_path), run_id="web-dup")
            raise AssertionError("重复 run_id 应当被拒绝")
        except ValueError:
            pass
    finally:
        runner.close()


def test_available_web_tasks() -> None:
    tasks = available_web_tasks()
    assert tasks
    assert all(task.task_id for task in tasks)
