from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from repo_pilot_mas.agents import ScriptedSupervisor, SupervisorOutcome
from repo_pilot_mas.orchestration import (
    EngineBudget,
    EngineStatus,
    NodeStatus,
    OrchestrationEngine,
)
from repo_pilot_mas.schemas import (
    Artifact,
    ArtifactType,
    CreateTaskRequest,
    DecisionAction,
    SupervisorDecision,
    TaskSpec,
)


def _engine(tmp_path: Path, *, budget: EngineBudget | None = None) -> OrchestrationEngine:
    task = TaskSpec(
        task_id="engine-test",
        repository_path=tmp_path,
        issue="修复缺陷",
        acceptance_criteria=("验证通过",),
    )
    return OrchestrationEngine(task, budget=budget)


def _create(decision_id: str, objective: str = "调查失败入口") -> SupervisorDecision:
    return SupervisorDecision(
        decision_id,
        DecisionAction.CREATE_TASK,
        "创建调查任务",
        create_tasks=(
            CreateTaskRequest(
                "INVESTIGATION_TASK",
                "InvestigatorAgent",
                "focused",
                objective,
            ),
        ),
    )


def _validated_patch_pair(engine: OrchestrationEngine) -> tuple[Artifact, Artifact]:
    patch = Artifact(
        "patch-1",
        ArtifactType.PATCH_CANDIDATE,
        "N1",
        {
            "diff": "--- a/module.py\n+++ b/module.py\n",
            "diff_sha256": "abc",
            "changed_files": ["module.py"],
            "protected_path_check": True,
            "workspace_id": "ws-1",
        },
    )
    validation = Artifact(
        "validation-passed",
        ArtifactType.VALIDATION_RESULT,
        "N3",
        {
            "passed": True,
            "patch_ref": patch.ref,
            "patch_sha256": "abc",
            "applied": True,
            "protected_path_check": True,
            "target_test": {"exit_code": 0, "trace_id": "trace-target"},
            "regression_test": {"exit_code": 0, "trace_id": "trace-regression"},
            "static_check": {"exit_code": 0, "trace_id": "trace-static"},
        },
    )
    engine.add_artifact(patch)
    engine.add_artifact(validation)
    return patch, validation


def test_dynamic_create_pause_resume_and_cancel(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert engine.apply_decision(_create("D1")).ok
    assert engine.graph.get("N1").status is NodeStatus.READY
    engine.start_node("N1")

    assert engine.apply_decision(
        SupervisorDecision(
            "D2",
            DecisionAction.PAUSE_TASK,
            "等待更多证据",
            target_task_ids=("N1",),
        )
    ).ok
    assert engine.graph.get("N1").status is NodeStatus.PAUSED

    assert engine.apply_decision(
        SupervisorDecision(
            "D3",
            DecisionAction.RESUME_TASK,
            "证据已齐备",
            target_task_ids=("N1",),
        )
    ).ok
    assert engine.graph.get("N1").status is NodeStatus.READY

    assert engine.apply_decision(
        SupervisorDecision(
            "D4",
            DecisionAction.CANCEL_TASK,
            "路径无效",
            target_task_ids=("N1",),
        )
    ).ok
    assert engine.graph.get("N1").status is NodeStatus.CANCELLED


def test_illegal_decision_dependency_evidence_and_budget_are_rejected(tmp_path: Path) -> None:
    engine = _engine(tmp_path, budget=EngineBudget(max_nodes=1))
    missing_dependency = SupervisorDecision(
        "D-missing-dep",
        DecisionAction.CREATE_TASK,
        "非法依赖",
        create_tasks=(
            CreateTaskRequest(
                "INVESTIGATION_TASK",
                "InvestigatorAgent",
                "focused",
                "诊断",
                depends_on=("N404",),
            ),
        ),
    )
    assert engine.apply_decision(missing_dependency).code == "INVALID_SUPERVISOR_DECISION"

    missing_evidence = SupervisorDecision(
        "D-missing-evidence",
        DecisionAction.CREATE_TASK,
        "非法证据",
        create_tasks=(
            CreateTaskRequest(
                "INVESTIGATION_TASK",
                "InvestigatorAgent",
                "focused",
                "诊断",
                input_artifact_ids=("unknown@v1",),
            ),
        ),
    )
    assert engine.apply_decision(missing_evidence).code == "INVALID_SUPERVISOR_DECISION"

    wrong_stage = SupervisorDecision(
        "D-wrong-stage",
        DecisionAction.CREATE_TASK,
        "初始化时不得直接创建补丁",
        create_tasks=(
            CreateTaskRequest(
                "PATCH_TASK",
                "PatchAgent",
                "minimal",
                "越过调查直接修补",
            ),
        ),
    )
    assert engine.apply_decision(wrong_stage).code == "INVALID_SUPERVISOR_DECISION"

    assert engine.apply_decision(_create("D-valid")).ok
    over_budget = engine.apply_decision(_create("D-over", "第二个调查"))
    assert not over_budget.ok
    assert over_budget.code == "INVALID_SUPERVISOR_DECISION"
    assert len(engine.graph.nodes) == 1


def test_unmet_dependencies_and_concurrency_budget_prevent_start(tmp_path: Path) -> None:
    engine = _engine(tmp_path, budget=EngineBudget(max_concurrent_nodes=1))
    assert engine.apply_decision(_create("D1", "第一个调查")).ok
    dependent = SupervisorDecision(
        "D2",
        DecisionAction.CREATE_TASK,
        "创建后继任务",
        create_tasks=(
            CreateTaskRequest(
                "INVESTIGATION_TASK",
                "InvestigatorAgent",
                "focused",
                "等待调查结果",
                depends_on=("N1",),
            ),
        ),
    )
    assert engine.apply_decision(dependent).ok
    assert engine.apply_decision(_create("D3", "并行调查")).ok

    with pytest.raises(ValueError, match="not ready"):
        engine.start_node("N2")
    engine.start_node("N1")
    with pytest.raises(RuntimeError, match="concurrency budget"):
        engine.start_node("N3")
    assert engine.graph.get("N2").status is NodeStatus.PENDING


def test_duplicate_decisions_trigger_no_progress_termination(tmp_path: Path) -> None:
    engine = _engine(tmp_path, budget=EngineBudget(max_no_progress_decisions=2))
    assert engine.apply_decision(_create("D1")).ok

    first_repeat = engine.apply_decision(_create("D2"))
    second_repeat = engine.apply_decision(_create("D3"))

    assert first_repeat.code == "NO_PROGRESS_LOOP"
    assert second_repeat.code == "NO_PROGRESS_LOOP"
    assert engine.status is EngineStatus.FAILED
    assert engine.termination_reason == "NO_PROGRESS_LOOP"


def test_scripted_supervisor_reproduces_graph_changes(tmp_path: Path) -> None:
    decisions = (
        _create("D1"),
        SupervisorDecision(
            "D2",
            DecisionAction.PAUSE_TASK,
            "暂停",
            target_task_ids=("N1",),
        ),
        SupervisorDecision(
            "D3",
            DecisionAction.RESUME_TASK,
            "恢复",
            target_task_ids=("N1",),
        ),
        SupervisorDecision(
            "D4",
            DecisionAction.CANCEL_TASK,
            "取消",
            target_task_ids=("N1",),
        ),
    )
    results: list[tuple[str, str, int]] = []
    for suffix in ("a", "b"):
        root = tmp_path / suffix
        root.mkdir()
        engine = _engine(root)
        supervisor = ScriptedSupervisor(decisions)
        for _ in decisions:
            result = engine.run_supervisor(supervisor)
            assert result.ok
        node = engine.graph.get("N1")
        results.append((node.node_id, node.status.value, engine.state_version))

    assert results[0] == results[1]


def test_invalid_finalization_is_rejected_and_validated_patch_can_finish(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    patch, passed_validation = _validated_patch_pair(engine)
    failed_validation = Artifact(
        "validation-failed",
        ArtifactType.VALIDATION_RESULT,
        "N2",
        {
            "passed": False,
            "patch_ref": patch.ref,
            "patch_sha256": "abc",
            "applied": True,
            "protected_path_check": True,
        },
    )
    engine.add_artifact(failed_validation)
    engine.blackboard.set_stage("validation")

    rejected = engine.apply_decision(
        SupervisorDecision(
            "D-failed",
            DecisionAction.FINALIZE_TASK,
            "错误地接受失败验证",
            patch_ref=patch.ref,
            validation_ref=failed_validation.ref,
        )
    )
    assert not rejected.ok
    assert engine.status is EngineStatus.ACTIVE

    blocking_review = Artifact(
        "review-1",
        ArtifactType.REVIEW,
        "N4",
        {"severity": "blocking", "claim": "需要补充边界验证"},
        status="open",
    )
    engine.add_artifact(blocking_review)

    blocked = engine.apply_decision(
        SupervisorDecision(
            "D-blocked",
            DecisionAction.FINALIZE_TASK,
            "仍有阻塞审查",
            patch_ref=patch.ref,
            validation_ref=passed_validation.ref,
        )
    )
    assert not blocked.ok
    engine.add_artifact(
        Artifact(
            "review-1",
            ArtifactType.REVIEW,
            "N4",
            {"severity": "blocking", "claim": "边界验证已补充"},
            version=2,
            status="resolved",
            supersedes=blocking_review.ref,
        )
    )

    accepted = engine.apply_decision(
        SupervisorDecision(
            "D-passed",
            DecisionAction.FINALIZE_TASK,
            "验证通过",
            patch_ref=patch.ref,
            validation_ref=passed_validation.ref,
        )
    )
    assert accepted.ok
    assert engine.status is EngineStatus.SUCCEEDED
    assert engine.blackboard.workflow_stage == "completed"


def test_supervisor_call_budget_terminates_before_call(tmp_path: Path) -> None:
    engine = _engine(tmp_path, budget=EngineBudget(max_supervisor_calls=0))
    supervisor = ScriptedSupervisor((_create("D1"),))

    result = engine.run_supervisor(supervisor)

    assert result.code == "SUPERVISOR_CALL_BUDGET_EXHAUSTED"
    assert engine.status is EngineStatus.FAILED
    assert supervisor.calls == 0


def test_budget_mutations_increment_state_version(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    initial = engine.state_version
    engine.record_tool_calls(1)
    assert engine.state_version == initial + 1

    result = engine.run_supervisor(ScriptedSupervisor((_create("D1"),)))
    assert result.ok
    assert engine.budget.supervisor_calls == 1
    assert engine.state_version > initial + 1


def test_tool_token_runtime_and_replan_budgets_are_enforced(tmp_path: Path) -> None:
    tool_engine = _engine(tmp_path / "tool", budget=EngineBudget(max_tool_calls=0))
    tool_engine.record_tool_calls(1)
    assert tool_engine.status is EngineStatus.FAILED
    assert tool_engine.termination_reason == "TOOL_CALL_BUDGET_EXHAUSTED"

    class TokenSupervisor:
        def decide(self, snapshot):  # type: ignore[no-untyped-def]
            del snapshot
            return SupervisorOutcome(_create("D-token"), input_tokens=2)

    token_engine = _engine(tmp_path / "token", budget=EngineBudget(max_input_tokens=1))
    token_result = token_engine.run_supervisor(TokenSupervisor())
    assert token_result.code == "INPUT_TOKEN_BUDGET_EXHAUSTED"
    assert token_engine.graph.nodes == ()

    old_start = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    runtime_engine = _engine(
        tmp_path / "runtime",
        budget=EngineBudget(max_runtime_seconds=1, started_at=old_start),
    )
    runtime_result = runtime_engine.apply_decision(_create("D-runtime"))
    assert runtime_result.code == "RUNTIME_BUDGET_EXHAUSTED"

    replan_engine = _engine(tmp_path / "replan", budget=EngineBudget(max_replans=1))
    replan_engine.blackboard.set_stage("validation")
    assert replan_engine.apply_decision(
        SupervisorDecision(
            "D-replan-1",
            DecisionAction.REQUEST_REPLAN,
            "回退调查",
            next_workflow_stage="investigation",
        )
    ).ok
    second_replan = replan_engine.apply_decision(
        SupervisorDecision(
            "D-replan-2",
            DecisionAction.REQUEST_REPLAN,
            "再次回退",
            next_workflow_stage="diagnosis",
        )
    )
    assert second_replan.code == "INVALID_SUPERVISOR_DECISION"


def test_remaining_global_supervisor_actions(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    for decision_id, target in (
        ("D-stage-1", "investigation"),
        ("D-stage-2", "diagnosis"),
    ):
        assert engine.apply_decision(
            SupervisorDecision(
                decision_id,
                DecisionAction.CHANGE_WORKFLOW_STAGE,
                "推进工作流",
                next_workflow_stage=target,
            )
        ).ok
    hypothesis = Artifact(
        "hypothesis-1",
        ArtifactType.HYPOTHESIS,
        "N1",
        {"root_cause": "缺少最终深度检查"},
    )
    engine.add_artifact(hypothesis)
    assert engine.apply_decision(
        SupervisorDecision(
            "D-hypothesis",
            DecisionAction.ACCEPT_HYPOTHESIS,
            "证据支持该根因",
            hypothesis_ref=hypothesis.ref,
        )
    ).ok
    for decision_id, target in (("D-stage-3", "patch"), ("D-stage-4", "validation")):
        assert engine.apply_decision(
            SupervisorDecision(
                decision_id,
                DecisionAction.CHANGE_WORKFLOW_STAGE,
                "推进工作流",
                next_workflow_stage=target,
            )
        ).ok
    patch, validation = _validated_patch_pair(engine)
    assert engine.apply_decision(
        SupervisorDecision(
            "D-select",
            DecisionAction.SELECT_PATCH,
            "选择已验证补丁",
            patch_ref=patch.ref,
            validation_ref=validation.ref,
        )
    ).ok
    assert engine.apply_decision(
        SupervisorDecision(
            "D-terminate",
            DecisionAction.TERMINATE_TASK,
            "人工终止验收场景",
        )
    ).ok
    assert engine.status is EngineStatus.TERMINATED
