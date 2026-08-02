from __future__ import annotations

import json
from pathlib import Path

from repo_pilot_mas.agents import SupervisorAgent
from repo_pilot_mas.models import FakeModelAdapter, GenerationConfig
from repo_pilot_mas.orchestration import EngineStatus, OrchestrationEngine
from repo_pilot_mas.runtime import TraceWriter
from repo_pilot_mas.schemas import DecisionAction, TaskSpec


def _valid_decision() -> dict[str, object]:
    return {
        "decision_id": "D-realistic-1",
        "action": "CREATE_TASK",
        "reason": "先收集可验证证据",
        "create_tasks": [
            {
                "node_type": "INVESTIGATION_TASK",
                "agent_type": "InvestigatorAgent",
                "mode": "focused",
                "objective": "定位失败测试对应的代码入口",
                "depends_on": [],
                "input_artifact_ids": [],
                "dependency_policy": "all_succeeded",
                "critical": True,
                "timeout_seconds": 120,
            }
        ],
        "next_workflow_stage": "investigation",
    }


def _task(tmp_path: Path) -> TaskSpec:
    return TaskSpec(
        task_id="supervisor-test",
        repository_path=tmp_path,
        issue="修复失败测试",
        acceptance_criteria=("目标测试通过",),
    )


def test_supervisor_repairs_one_malformed_response_and_traces_metadata(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    model = FakeModelAdapter(
        ["not-json", _valid_decision()],
        raw_log_dir=tmp_path / "raw",
    )
    supervisor = SupervisorAgent(
        model,
        generation_config=GenerationConfig(max_retries=1),
        trace_writer=TraceWriter(trace_path),
    )

    outcome = supervisor.decide({"workflow_stage": "initialization", "nodes": []})

    assert outcome.decision.action is DecisionAction.CREATE_TASK
    assert outcome.attempts == 2
    assert outcome.input_tokens > 0
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event_type"] == "supervisor_decision_generated"
    assert events[-1]["data"]["model_id"] == "fake-model"
    assert "secret" not in trace_path.read_text(encoding="utf-8").casefold()


def test_malformed_response_after_repair_limit_terminates_engine_structurally(
    tmp_path: Path,
) -> None:
    model = FakeModelAdapter(["bad", "still bad"])
    supervisor = SupervisorAgent(
        model,
        generation_config=GenerationConfig(max_retries=1),
    )
    engine = OrchestrationEngine(_task(tmp_path))

    result = engine.run_supervisor(supervisor)

    assert not result.ok
    assert result.code == "STRUCTURED_OUTPUT_ERROR"
    assert engine.status is EngineStatus.FAILED
    assert engine.termination_reason == "STRUCTURED_OUTPUT_ERROR"
    assert engine.budget.supervisor_calls == 1
    assert engine.budget.total_tokens > 0


def test_trace_writer_redacts_dashscope_secret_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    trace_path = tmp_path / "redacted.jsonl"
    monkeypatch.setenv("DASHSCOPE_API_KEY_TEST", "sk-test-value")

    TraceWriter(trace_path).write(
        "provider_error",
        {"nested": {"message": "bad key sk-test-value"}},
    )

    text = trace_path.read_text(encoding="utf-8")
    assert "sk-test-value" not in text
    assert "[REDACTED]" in text
