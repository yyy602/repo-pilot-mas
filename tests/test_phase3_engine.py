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
    GateRecord,
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
    evidence = next(
        (
            item
            for item in engine.blackboard.artifacts.latest_values()
            if item.artifact_type is ArtifactType.EVIDENCE
        ),
        None,
    )
    if evidence is None:
        evidence = _root_evidence()
        engine.add_artifact(evidence)
    resolution = engine.blackboard.hypothesis_resolution
    if resolution.status.value != "accepted":
        hypothesis = _root_hypothesis(evidence)
        root_review = _review_artifact(
            "root-review",
            "root_cause_recommendation",
            hypothesis.ref,
            evidence.ref,
        )
        engine.add_artifact(hypothesis)
        engine.add_artifact(root_review)
        engine.blackboard.set_hypotheses(
            (hypothesis.ref,),
            primary_ref=hypothesis.ref,
            review_refs=(root_review.ref,),
            decision_id="fixture-accept",
        )
        resolution = engine.blackboard.hypothesis_resolution
    accepted_refs = tuple(resolution.accepted_refs)
    primary_ref = str(resolution.primary_ref)
    patch = Artifact(
        "patch-1",
        ArtifactType.PATCH_CANDIDATE,
        "N1",
        {
            "strategy": "minimal",
            "based_on_hypothesis_refs": list(accepted_refs),
            "primary_hypothesis_ref": primary_ref,
            "covered_root_causes": {ref: "缺少最终状态检查" for ref in accepted_refs},
            "diff": "--- a/module.py\n+++ b/module.py\n-old\n+new\n",
            "diff_sha256": "a" * 64,
            "changed_files": ["module.py"],
            "rationale": "修复已接受根因",
            "semantic_rationale": "增加最终状态检查以消除直接原因",
            "pre_patch_behavior": "失败输入绕过最终状态检查",
            "post_patch_expected_behavior": "失败输入返回正确结果",
            "failure_input_walkthrough": "失败输入进入缺少保护的返回分支",
            "precheck_command": ["static_check", ".", "--no-ruff"],
            "precheck_result": {
                "ok": True,
                "trace_id": "precheck",
                "exit_code": 0,
                "output_tail": "",
            },
            "risk_notes": [],
            "protected_path_check": True,
            "workspace_id": "ws-1",
        },
        input_refs=(*accepted_refs, *resolution.review_refs),
    )
    patch_review = _review_artifact(
        "patch-review",
        "patch_review",
        patch.ref,
        evidence.ref,
    )
    validation = Artifact(
        "validation-passed",
        ArtifactType.VALIDATION_RESULT,
        "N3",
        {
            "passed": True,
            "patch_ref": patch.ref,
            "patch_review_refs": [patch_review.ref],
            "patch_sha256": "a" * 64,
            "workspace_id": "ws-1",
            "applied": True,
            "protected_path_check": True,
            "changed_files": ["module.py"],
            "changed_lines": 2,
            "failure_class": "none",
            "recommended_stage": "completed",
            "invalidated_refs": [],
            "recoverable": False,
            "target_test": _command_result("trace-target"),
            "regression_test": _command_result("trace-regression"),
            "static_check": _command_result("trace-static"),
            "tool_trace_ids": ["diff", "trace-target", "trace-regression", "trace-static"],
        },
        input_refs=(patch.ref, patch_review.ref),
    )
    engine.add_artifact(patch)
    engine.add_artifact(patch_review)
    engine.add_artifact(validation)
    return patch, validation


def _root_evidence() -> Artifact:
    return Artifact(
        "root-evidence",
        ArtifactType.EVIDENCE,
        "N0",
        {
            "mode": "failure_reproduction",
            "evidence_kind": "reproduction",
            "claim": "失败输入复现最终状态错误",
            "supports_claims": ["失败输入复现最终状态错误"],
            "contradicts_claims": [],
            "verified": True,
            "source": {"path": "module.py", "line_start": 1, "line_end": 3},
            "content": "目标输入得到错误返回值",
            "observation_type": "direct",
            "confidence": 1.0,
            "status": "verified",
            "tool_trace_ids": ["reproduce"],
            "missing_evidence": [],
            "reproduction": {
                "attempted": True,
                "succeeded": True,
                "exit_code": 1,
                "failure_type": "AssertionError",
                "failure_output": "expected False, got True",
                "command": ["python", "-m", "pytest", "-q"],
            },
        },
    )


def _root_hypothesis(evidence: Artifact) -> Artifact:
    return Artifact(
        "root-hypothesis",
        ArtifactType.HYPOTHESIS,
        "N1",
        {
            "perspective": "control_flow",
            "root_cause": "缺少最终状态检查",
            "direct_cause": "函数无条件返回成功",
            "supporting_evidence": [evidence.ref],
            "counter_evidence": [],
            "affected_symbols": ["target"],
            "verification_plan": ["运行目标测试"],
            "missing_evidence": [],
            "confidence": 0.9,
        },
        input_refs=(evidence.ref,),
    )


def _review_artifact(
    artifact_id: str,
    mode: str,
    target_ref: str,
    evidence_ref: str,
) -> Artifact:
    return Artifact(
        artifact_id,
        ArtifactType.REVIEW,
        "N2",
        {
            "mode": mode,
            "target_artifact_ref": target_ref,
            "evidence_refs": [evidence_ref],
            "verdict": "supported",
            "findings": ["直接证据支持目标"],
            "risk_notes": [],
            "recommendation": "继续闭环",
            "failure_explained": True,
            "causal_chain_complete": True,
            "alternative_causes": ["排除了测试配置问题"],
            "counterexample_checked": True,
            "verification_steps_executed": ["核对失败输出"],
            "remaining_uncertainty": [],
        },
        input_refs=(target_ref, evidence_ref),
    )


def _command_result(trace_id: str) -> dict[str, object]:
    return {
        "command": ["python", "-m", "pytest", "-q"],
        "exit_code": 0,
        "trace_id": trace_id,
        "duration_ms": 1,
        "output_tail": "passed",
    }


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


def test_failed_worker_retry_does_not_require_artifact_gate(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert engine.apply_decision(_create("D-retry-1")).ok
    engine.start_node("N1")
    engine.finish_node("N1", NodeStatus.FAILED, reason="worker contract failure")

    retry = engine.apply_decision(
        SupervisorDecision(
            "D-retry-2",
            DecisionAction.CREATE_TASK,
            "使用合法 Worker 契约重试调查",
            create_tasks=(
                CreateTaskRequest(
                    "INVESTIGATION_TASK",
                    "InvestigatorAgent",
                    "code_retrieval",
                    "使用合法 Worker 契约重试调查",
                ),
            ),
            next_workflow_stage="investigation",
        )
    )

    assert retry.ok
    assert engine.graph.get("N2").status is NodeStatus.READY


def test_snapshot_exposes_decision_ids_and_last_rejection(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    decision = _create("D-visible")
    assert engine.run_supervisor(ScriptedSupervisor((decision,))).ok

    duplicate = engine.run_supervisor(ScriptedSupervisor((decision,)))
    snapshot = engine.snapshot()

    assert duplicate.code == "DUPLICATE_DECISION_ID"
    assert snapshot["decision_history"]["processed_decision_ids"] == ["D-visible"]
    assert snapshot["decision_history"]["last_supervisor_call"]["decision_result"][
        "code"
    ] == "DUPLICATE_DECISION_ID"


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
    assert engine.termination_code == "NO_PROGRESS_LOOP"
    assert engine.termination_stage == "initialization"
    assert engine.snapshot()["termination"] == {
        "code": "NO_PROGRESS_LOOP",
        "stage": "initialization",
        "message": "NO_PROGRESS_LOOP",
    }


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


def test_same_patch_cannot_be_validated_twice(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    patch, _ = _validated_patch_pair(engine)
    patch_review = next(
        item
        for item in engine.blackboard.artifacts.latest_values()
        if item.artifact_type is ArtifactType.REVIEW
        and item.content.get("mode") == "patch_review"
    )
    engine.blackboard.set_stage("patch")

    result = engine.apply_decision(
        SupervisorDecision(
            "D-duplicate-validation",
            DecisionAction.CREATE_TASK,
            "错误地重复验证同一个补丁",
            create_tasks=(
                CreateTaskRequest(
                    "VALIDATION_TASK",
                    "ValidationExecutor",
                    "deterministic",
                    "重复验证同一个补丁",
                    input_artifact_ids=(patch.ref, patch_review.ref),
                ),
            ),
            next_workflow_stage="validation",
        )
    )

    assert result.ok is False
    assert result.code == "PATCH_ALREADY_VALIDATED"
    repeated = engine.apply_decision(
        SupervisorDecision(
            "D-duplicate-validation-again",
            DecisionAction.CREATE_TASK,
            "错误地重复验证同一个补丁",
            create_tasks=(
                CreateTaskRequest(
                    "VALIDATION_TASK",
                    "ValidationExecutor",
                    "deterministic",
                    "重复验证同一个补丁",
                    input_artifact_ids=(patch.ref, patch_review.ref),
                ),
            ),
            next_workflow_stage="validation",
        )
    )
    assert repeated.code == "NO_PROGRESS_LOOP"
    repeated_again = engine.apply_decision(
        SupervisorDecision(
            "D-duplicate-validation-third",
            DecisionAction.CREATE_TASK,
            "错误地重复验证同一个补丁",
            create_tasks=(
                CreateTaskRequest(
                    "VALIDATION_TASK",
                    "ValidationExecutor",
                    "deterministic",
                    "重复验证同一个补丁",
                    input_artifact_ids=(patch.ref, patch_review.ref),
                ),
            ),
            next_workflow_stage="validation",
        )
    )
    assert repeated_again.code == "NO_PROGRESS_LOOP"
    assert engine.status is EngineStatus.FAILED


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

    evidence_ref = next(
        item.ref
        for item in engine.blackboard.artifacts.latest_values()
        if item.artifact_type is ArtifactType.EVIDENCE
    )
    blocking_content = {
        **_review_artifact(
            "review-1",
            "patch_review",
            patch.ref,
            evidence_ref,
        ).to_dict()["content"],
        "verdict": "changes_requested",
        "remaining_uncertainty": ["边界验证尚未补充"],
    }
    blocking_review = Artifact(
        "review-1",
        ArtifactType.REVIEW,
        "N4",
        blocking_content,
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
            {
                **blocking_content,
                "verdict": "supported",
                "remaining_uncertainty": [],
            },
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
    assert tool_engine.termination_code == "TOOL_CALL_BUDGET_EXHAUSTED"
    assert tool_engine.termination_stage == "initialization"

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


def test_approved_replan_budget_zero_still_allows_target_recovery_node(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, budget=EngineBudget(max_replans=1))
    _validated_patch_pair(engine)
    resolution = engine.blackboard.hypothesis_resolution
    validation_ref = next(
        item.ref
        for item in engine.blackboard.artifacts.latest_values()
        if item.artifact_type is ArtifactType.VALIDATION_RESULT
    )
    engine.blackboard.set_stage("validation")

    requested = engine.apply_decision(
        SupervisorDecision(
            "D-replan-patch",
            DecisionAction.REQUEST_REPLAN,
            "回退补丁阶段修复回归失败",
            evidence_refs=(validation_ref,),
            next_workflow_stage="patch",
            failure_class="regression_failure",
        )
    )

    assert requested.ok
    recovery = engine.snapshot()["recovery"]
    assert recovery["pending_replan"] is True
    assert recovery["remaining_replans"] == 0
    rejected_termination = engine.apply_decision(
        SupervisorDecision(
            "D-wrong-termination",
            DecisionAction.TERMINATE_TASK,
            "错误地把重规划次数耗尽解释为不能执行恢复",
        )
    )
    assert not rejected_termination.ok
    assert engine.status is EngineStatus.ACTIVE

    replan_ref = str(recovery["replan_ref"])
    created = engine.apply_decision(
        SupervisorDecision(
            "D-execute-replan",
            DecisionAction.CREATE_TASK,
            "执行已经获准的补丁恢复",
            create_tasks=(
                CreateTaskRequest(
                    "PATCH_TASK",
                    "PatchAgent",
                    "minimal",
                    "针对回归失败生成新的最小补丁",
                    input_artifact_ids=(
                        *resolution.accepted_refs,
                        *resolution.review_refs,
                    ),
                ),
            ),
            evidence_refs=(replan_ref, validation_ref),
            gate_record=GateRecord(
                "approved_replan_recovery",
                (replan_ref, validation_ref),
                "已有定向重规划授权",
                1,
                "执行已批准的一个恢复节点，不新增重规划次数",
            ),
            next_workflow_stage="patch",
        )
    )

    assert created.ok
    assert engine.snapshot()["recovery"]["pending_replan"] is False
    assert engine.budget.replans == 1


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
    evidence = _root_evidence()
    engine.add_artifact(evidence)
    hypothesis = _root_hypothesis(evidence)
    engine.add_artifact(hypothesis)
    review = _review_artifact(
        "global-root-review",
        "root_cause_recommendation",
        hypothesis.ref,
        evidence.ref,
    )
    engine.add_artifact(review)
    assert engine.apply_decision(
        SupervisorDecision(
            "D-hypothesis",
            DecisionAction.ACCEPT_HYPOTHESIS,
            "证据支持该根因",
            hypothesis_refs=(hypothesis.ref,),
            primary_hypothesis_ref=hypothesis.ref,
            review_refs=(review.ref,),
            evidence_refs=(evidence.ref, review.ref),
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
    assert engine.termination_code == "SUPERVISOR_TERMINATED"
    assert engine.termination_stage == "validation"
