from __future__ import annotations

import json
from pathlib import Path

import pytest

from repo_pilot_mas.agents import SupervisorAgent
from repo_pilot_mas.agents.supervisor import _supervisor_snapshot_view
from repo_pilot_mas.models import (
    FakeModelAdapter,
    GenerationConfig,
    ModelAdapterError,
)
from repo_pilot_mas.orchestration import EngineStatus, OrchestrationEngine
from repo_pilot_mas.runtime import TraceWriter
from repo_pilot_mas.schemas import (
    DecisionAction,
    TaskSpec,
    supervisor_decision_schema_for_state,
)
from repo_pilot_mas.schemas.json_schema import SchemaValidationError, validate_json_schema


def _valid_decision() -> dict[str, object]:
    return {
        "decision_id": "D-realistic-1",
        "action": "CREATE_TASK",
        "reason": "先收集可验证证据",
        "create_tasks": [
            {
                "node_type": "INVESTIGATION_TASK",
                "agent_type": "InvestigatorAgent",
                "mode": "code_retrieval",
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


def test_supervisor_snapshot_view_removes_bulk_outputs_but_keeps_semantics() -> None:
    snapshot = {
        "task": {"task_id": "T1", "issue": "修复失败", "test_command": ["python"]},
        "engine_status": "active",
        "workflow_stage": "validation",
        "state_version": 8,
        "nodes": [
            {
                "node_id": "N1",
                "node_type": "VALIDATION_TASK",
                "status": "SUCCEEDED",
                "objective": "验证补丁",
                "input_artifact_ids": ["P1@v1"],
                "output_artifact_ids": ["V1@v1"],
            }
        ],
        "artifacts": [
            {
                "artifact_ref": "P1@v1",
                "artifact_type": "patch_candidate",
                "status": "created",
                "created_by": "N1",
                "content": {
                    "strategy": "minimal",
                    "diff": "x" * 10_000,
                    "diff_sha256": "patch-sha",
                    "changed_files": ["module.py"],
                },
            },
            {
                "artifact_ref": "V1@v1",
                "artifact_type": "validation_result",
                "status": "created",
                "created_by": "N2",
                "content": {
                    "patch_ref": "P1@v1",
                    "passed": False,
                    "failure_class": "target_test_failure",
                    "target_test": {
                        "exit_code": 1,
                        "trace_id": "target-trace",
                        "output_tail": "failure" * 1000,
                    },
                },
            },
        ],
        "selections": {},
        "decision_history": {
            "processed_decision_ids": [f"D{i}" for i in range(20)],
            "last_supervisor_call": {
                "decision_result": {
                    "code": "INVALID_SUPERVISOR_DECISION",
                    "message": "bad" * 1000,
                },
                "raw_response_ref": "/secret/path",
            },
        },
        "recovery": {},
        "budget": {"supervisor_calls": 2},
    }

    compact = _supervisor_snapshot_view(snapshot)
    text = json.dumps(compact, ensure_ascii=False)

    assert "x" * 100 not in text
    assert "output_tail" not in text
    assert "test_command" not in compact["task"]
    assert compact["artifacts"][1]["content"]["target_test"] == {
        "exit_code": 1,
        "trace_id": "target-trace",
    }
    assert compact["decision_history"]["processed_decision_count"] == 20
    assert compact["decision_history"]["processed_decision_ids"] == [
        f"D{i}" for i in range(8, 20)
    ]
    assert len(text) < len(json.dumps(snapshot, ensure_ascii=False)) / 4


def test_supervisor_repairs_invalid_worker_contract() -> None:
    invalid = _valid_decision()
    invalid["create_tasks"][0]["agent_type"] = "Investigator"
    invalid["create_tasks"][0]["mode"] = "default"
    model = FakeModelAdapter([invalid, _valid_decision()])

    outcome = SupervisorAgent(
        model,
        generation_config=GenerationConfig(max_retries=1),
    ).decide({"workflow_stage": "initialization", "nodes": []})

    assert outcome.attempts == 2
    assert outcome.decision.create_tasks[0].agent_type == "InvestigatorAgent"
    assert outcome.decision.create_tasks[0].mode == "code_retrieval"


def test_structured_output_error_is_recoverable(
    tmp_path: Path,
) -> None:
    model = FakeModelAdapter(["bad"])
    supervisor = SupervisorAgent(
        model,
        generation_config=GenerationConfig(max_retries=0),
        additional_schema_retries=0,
        safe_fallback_enabled=False,
    )
    engine = OrchestrationEngine(_task(tmp_path))

    result = engine.run_supervisor(supervisor)

    assert not result.ok
    assert result.code == "STRUCTURED_OUTPUT_ERROR"
    assert result.recoverable is True
    assert result.allowed_next_actions == ("CREATE_TASK", "TERMINATE_TASK")
    assert engine.status is EngineStatus.ACTIVE
    assert engine.termination_reason is None
    assert engine.budget.supervisor_calls == 1
    assert engine.budget.total_tokens > 0


def test_exhausted_supervisor_routes_fail_closed_without_policy_loop(
    tmp_path: Path,
) -> None:
    class ExhaustedAdapter:
        model_id = "exhausted-adapter"

        def generate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            del args, kwargs
            raise ModelAdapterError(
                "SUPERVISOR_ROUTES_EXHAUSTED",
                "all configured supervisor routes exhausted free quota",
                attempts=6,
            )

    supervisor = SupervisorAgent(
        ExhaustedAdapter(),  # type: ignore[arg-type]
        generation_config=GenerationConfig(max_retries=0),
        additional_schema_retries=0,
        safe_fallback_enabled=False,
    )
    engine = OrchestrationEngine(_task(tmp_path))

    result = engine.run_supervisor(supervisor)

    assert not result.ok
    assert result.code == "SUPERVISOR_ROUTES_EXHAUSTED"
    assert result.recoverable is False
    assert engine.status is EngineStatus.FAILED
    assert engine.termination_reason == "SUPERVISOR_ROUTES_EXHAUSTED"
    assert engine.budget.supervisor_calls == 1


def test_stage_specific_schema_only_exposes_legal_actions() -> None:
    initialization = supervisor_decision_schema_for_state(
        {"workflow_stage": "initialization", "nodes": [], "artifacts": []}
    )
    diagnosis = supervisor_decision_schema_for_state(
        {
            "workflow_stage": "diagnosis",
            "nodes": [],
            "artifacts": [
                {
                    "artifact_ref": "E1@v1",
                    "artifact_type": "evidence",
                    "content": {},
                },
                {
                    "artifact_ref": "H1@v1",
                    "artifact_type": "hypothesis",
                    "content": {"missing_evidence": []},
                },
                {
                    "artifact_ref": "R1@v1",
                    "artifact_type": "review",
                    "content": {
                        "mode": "root_cause_recommendation",
                        "verdict": "supported",
                        "target_artifact_ref": "H1@v1",
                    },
                },
            ],
            "selections": {"hypothesis_resolution": {"status": "under_review"}},
        }
    )

    assert _schema_actions(initialization) == {"CREATE_TASK", "TERMINATE_TASK"}
    assert _schema_node_types(initialization) == {"INVESTIGATION_TASK"}
    assert "ACCEPT_HYPOTHESIS" in _schema_actions(diagnosis)
    assert "SELECT_PATCH" not in _schema_actions(diagnosis)
    assert "PATCH_TASK" not in _schema_node_types(diagnosis)


def test_hypothesis_with_missing_evidence_cannot_be_accepted() -> None:
    schema = supervisor_decision_schema_for_state(
        {
            "workflow_stage": "diagnosis",
            "nodes": [],
            "artifacts": [
                {
                    "artifact_ref": "H1@v1",
                    "artifact_type": "hypothesis",
                    "content": {"missing_evidence": ["边界执行结果"]},
                },
                {
                    "artifact_ref": "R1@v1",
                    "artifact_type": "review",
                    "content": {
                        "mode": "root_cause_recommendation",
                        "verdict": "supported",
                        "target_artifact_ref": "H1@v1",
                    },
                },
            ],
            "selections": {
                "hypothesis_resolution": {"status": "under_review"}
            },
        }
    )

    assert "ACCEPT_HYPOTHESIS" not in _schema_actions(schema)


def _verified_reproduction(ref: str = "E1@v1") -> dict[str, object]:
    return {
        "artifact_ref": ref,
        "artifact_type": "evidence",
        "content": {
            "evidence_kind": "reproduction",
            "verified": True,
            "status": "verified",
            "source": {"path": "target.py", "line_start": 8, "line_end": 8},
            "content": "failing branch",
            "tool_trace_ids": ["trace-1"],
            "missing_evidence": [],
            "reproduction": {
                "attempted": True,
                "succeeded": True,
                "failure_output": "IndexError",
            },
        },
    }


def test_sufficient_verified_evidence_closes_investigation_schema() -> None:
    schema = supervisor_decision_schema_for_state(
        {
            "workflow_stage": "investigation",
            "nodes": [],
            "artifacts": [_verified_reproduction()],
        }
    )

    assert "INVESTIGATION_TASK" not in _schema_node_types(schema)
    assert "DIAGNOSIS_TASK" in _schema_node_types(schema)


def test_declared_failing_test_keeps_schema_in_investigation_until_reproduced() -> None:
    schema = supervisor_decision_schema_for_state(
        {
            "workflow_stage": "investigation",
            "task": {
                "requires_failure_reproduction": True,
                "confirmed_failure_reproduction_refs": [],
            },
            "nodes": [],
            "artifacts": [
                {
                    **_verified_reproduction(),
                    "content": {
                        **_verified_reproduction()["content"],
                        "evidence_kind": "source",
                        "reproduction": None,
                    },
                }
            ],
        }
    )

    assert "DIAGNOSIS_TASK" not in _schema_node_types(schema)
    assert "REVIEW_TASK" not in _schema_node_types(schema)
    assert "CHANGE_WORKFLOW_STAGE" not in _schema_actions(schema)
    assert "INVESTIGATION_TASK" in _schema_node_types(schema)


def test_explicit_gap_reopens_investigation_until_new_verified_evidence() -> None:
    gap = {
        "artifact_ref": "H1@v1",
        "artifact_type": "hypothesis",
        "content": {"missing_evidence": ["边界执行路径"]},
    }
    before_completion = supervisor_decision_schema_for_state(
        {
            "workflow_stage": "diagnosis",
            "nodes": [],
            "artifacts": [_verified_reproduction(), gap],
        }
    )
    after_completion = supervisor_decision_schema_for_state(
        {
            "workflow_stage": "diagnosis",
            "nodes": [],
            "artifacts": [
                _verified_reproduction(),
                gap,
                _verified_reproduction("E2@v1"),
            ],
        }
    )

    assert "INVESTIGATION_TASK" in _schema_node_types(before_completion)
    assert "INVESTIGATION_TASK" not in _schema_node_types(after_completion)
    assert "DIAGNOSIS_TASK" in _schema_node_types(after_completion)


def test_diagnosis_schema_only_accepts_evidence_inputs() -> None:
    schema = supervisor_decision_schema_for_state(
        {
            "workflow_stage": "diagnosis",
            "nodes": [],
            "artifacts": [
                _verified_reproduction(),
                {
                    "artifact_ref": "H1@v1",
                    "artifact_type": "hypothesis",
                    "content": {"missing_evidence": []},
                },
                {
                    "artifact_ref": "R1@v1",
                    "artifact_type": "review",
                    "content": {"verdict": "needs_more_evidence"},
                },
            ],
        }
    )
    create_variants = [
        variant
        for variant in schema["oneOf"]
        if variant["properties"]["action"]["const"] == "CREATE_TASK"
    ]
    diagnosis_variants = [
        task
        for variant in create_variants
        for task in variant["properties"]["create_tasks"]["items"]["oneOf"]
        if task["properties"]["node_type"]["const"] == "DIAGNOSIS_TASK"
    ]

    assert diagnosis_variants
    assert all(
        item["properties"]["input_artifact_ids"]["items"]["enum"]
        == ["E1@v1"]
        for item in diagnosis_variants
    )


def test_multiple_initial_diagnoses_require_expansion_gate() -> None:
    schema = supervisor_decision_schema_for_state(
        {
            "workflow_stage": "investigation",
            "nodes": [],
            "artifacts": [_verified_reproduction()],
        }
    )
    task = {
        "node_type": "DIAGNOSIS_TASK",
        "agent_type": "DiagnosticianAgent",
        "mode": "control_flow",
        "objective": "分析边界控制流",
        "input_artifact_ids": ["E1@v1"],
    }
    decision = {
        "decision_id": "two-diagnoses-without-gate",
        "action": "CREATE_TASK",
        "reason": "并行生成两个根因视角",
        "create_tasks": [
            task,
            {**task, "mode": "data_flow", "objective": "分析边界数据流"},
        ],
        "next_workflow_stage": "diagnosis",
    }

    with pytest.raises(SchemaValidationError):
        validate_json_schema(decision, schema)

    decision["decision_id"] = "two-diagnoses-with-gate"
    decision["evidence_refs"] = ["E1@v1"]
    decision["gate_record"] = {
        "gate_name": "independent_diagnosis_expansion",
        "trigger_artifact_refs": ["E1@v1"],
        "reason": "证据显示控制流和数据流均需独立核验",
        "added_node_count": 2,
        "budget_effect": "新增两个受限诊断节点",
    }
    validate_json_schema(decision, schema)


def test_ungated_review_cannot_match_expansion_gate_variant() -> None:
    schema = supervisor_decision_schema_for_state(
        {
            "workflow_stage": "diagnosis",
            "nodes": [
                {
                    "node_id": "N3",
                    "node_type": "DIAGNOSIS_TASK",
                    "status": "SUCCEEDED",
                }
            ],
            "artifacts": [
                _verified_reproduction(),
                {
                    "artifact_ref": "H1@v1",
                    "artifact_type": "hypothesis",
                    "content": {"missing_evidence": []},
                },
            ],
        }
    )
    decision = {
        "decision_id": "root-review-with-spurious-gate",
        "action": "CREATE_TASK",
        "reason": "审查已有根因",
        "evidence_refs": ["E1@v1", "H1@v1"],
        "create_tasks": [
            {
                "node_type": "REVIEW_TASK",
                "agent_type": "ReviewerAgent",
                "mode": "root_cause_recommendation",
                "objective": "独立审查根因",
                "input_artifact_ids": ["E1@v1", "H1@v1"],
            }
        ],
        "next_workflow_stage": "review",
        "gate_record": {
            "gate_name": "spurious-review-gate",
            "trigger_artifact_refs": ["H1@v1"],
            "reason": "普通根因审查不应进入扩图分支",
            "added_node_count": 1,
            "budget_effect": "none",
        },
    }

    with pytest.raises(SchemaValidationError):
        validate_json_schema(decision, schema)

    decision.pop("gate_record")
    validate_json_schema(decision, schema)


def test_initialization_schema_forbids_spurious_gate_without_artifacts() -> None:
    schema = supervisor_decision_schema_for_state(
        {"workflow_stage": "initialization", "nodes": [], "artifacts": []}
    )
    decision = _valid_decision()
    decision["gate_record"] = {
        "gate_name": "spurious",
        "trigger_artifact_refs": ["task-id-is-not-an-artifact"],
        "reason": "initialization",
        "added_node_count": 1,
        "budget_effect": "one node",
    }

    with pytest.raises(SchemaValidationError, match="additional property"):
        validate_json_schema(decision, schema)

    decision.pop("gate_record")
    decision["create_tasks"] = [
        decision["create_tasks"][0],
        {
            **decision["create_tasks"][0],
            "mode": "failure_reproduction",
            "objective": "并行复现失败",
        },
    ]
    with pytest.raises(SchemaValidationError, match="at most 1 items"):
        validate_json_schema(decision, schema)


def test_pending_replan_schema_requires_target_recovery_node() -> None:
    schema = supervisor_decision_schema_for_state(
        {
                "workflow_stage": "patch",
                "nodes": [],
                "artifacts": [
                    {"artifact_ref": "H1@v1", "artifact_type": "hypothesis"},
                    {"artifact_ref": "R1@v1", "artifact_type": "review"},
                    {"artifact_ref": "P1@v1", "artifact_type": "patch_candidate"},
                    {"artifact_ref": "V1@v1", "artifact_type": "validation_result"},
                    {"artifact_ref": "RP1@v1", "artifact_type": "replan_record"},
                ],
            "selections": {"hypothesis_resolution": {"status": "accepted"}},
            "recovery": {
                "pending_replan": True,
                "target_stage": "patch",
                "target_node_type": "PATCH_TASK",
                "replan_ref": "RP1@v1",
                "trigger_refs": ["V1@v1"],
                "remaining_replans": 0,
            },
        }
    )

    assert _schema_actions(schema) == {"CREATE_TASK"}
    assert _schema_node_types(schema) == {"PATCH_TASK"}
    variant = schema["oneOf"][0]
    assert {"evidence_refs", "gate_record"}.issubset(variant["required"])
    assert variant["properties"]["evidence_refs"] == {
        "type": "array",
        "items": {"type": "string", "enum": ["RP1@v1", "V1@v1"]},
        "minItems": 2,
        "maxItems": 2,
        "uniqueItems": True,
    }


def test_exhausted_failed_patch_schema_only_allows_termination() -> None:
    schema = supervisor_decision_schema_for_state(
        {
            "workflow_stage": "patch",
            "nodes": [],
            "artifacts": [
                {
                    "artifact_ref": "P1@v1",
                    "artifact_type": "patch_candidate",
                    "content": {},
                },
                {
                    "artifact_ref": "V1@v1",
                    "artifact_type": "validation_result",
                    "content": {"patch_ref": "P1@v1", "passed": False},
                },
            ],
            "selections": {
                "hypothesis_resolution": {"status": "accepted"}
            },
            "recovery": {
                "pending_replan": False,
                "remaining_replans": 0,
            },
        }
    )

    assert _schema_actions(schema) == {"TERMINATE_TASK"}


def test_stage_schema_rejects_illegal_create_transition() -> None:
    invalid = {
        "decision_id": "D-illegal-stage",
        "action": "CREATE_TASK",
        "reason": "创建审查",
        "create_tasks": [
                {
                    "node_type": "INVESTIGATION_TASK",
                    "agent_type": "InvestigatorAgent",
                    "mode": "code_retrieval",
                    "objective": "补充源码定位证据",
            }
        ],
        "next_workflow_stage": "review",
    }
    valid = {
        **invalid,
        "decision_id": "D-legal-stage",
        "next_workflow_stage": "diagnosis",
    }
    snapshot = {
        "workflow_stage": "investigation",
        "nodes": [],
        "artifacts": [],
    }

    outcome = SupervisorAgent(
        FakeModelAdapter([invalid, valid]),
        generation_config=GenerationConfig(max_retries=1),
    ).decide(snapshot)

    assert outcome.attempts == 2
    assert outcome.decision.next_workflow_stage == "diagnosis"


def test_stage_schema_binds_replan_failure_class_to_target_stage() -> None:
    invalid = {
        "decision_id": "D-wrong-replan-target",
        "action": "REQUEST_REPLAN",
        "reason": "证据不足需要补证",
        "next_workflow_stage": "diagnosis",
        "failure_class": "evidence_incomplete",
    }
    valid = {
        **invalid,
        "decision_id": "D-correct-replan-target",
        "next_workflow_stage": "investigation",
    }
    snapshot = {"workflow_stage": "review", "nodes": [], "artifacts": []}

    outcome = SupervisorAgent(
        FakeModelAdapter([invalid, valid]),
        generation_config=GenerationConfig(max_retries=1),
    ).decide(snapshot)

    assert outcome.attempts == 2
    assert outcome.decision.failure_class == "evidence_incomplete"
    assert outcome.decision.next_workflow_stage == "investigation"


def test_stage_schema_requires_patch_review_artifact_types() -> None:
    snapshot = {
        "workflow_stage": "patch",
        "nodes": [],
        "selections": {"hypothesis_resolution": {"status": "accepted"}},
        "artifacts": [
            {"artifact_ref": "E1@v1", "artifact_type": "evidence"},
            {"artifact_ref": "H1@v1", "artifact_type": "hypothesis"},
            {"artifact_ref": "R1@v1", "artifact_type": "review"},
            {"artifact_ref": "P1@v1", "artifact_type": "patch_candidate"},
        ],
    }
    schema = supervisor_decision_schema_for_state(snapshot)
    decision = {
        "decision_id": "D-patch-review",
        "action": "CREATE_TASK",
        "reason": "审查补丁",
        "create_tasks": [
            {
                "node_type": "REVIEW_TASK",
                "agent_type": "ReviewerAgent",
                "mode": "patch_review",
                "objective": "依据根因与直接证据审查补丁",
                "input_artifact_ids": ["H1@v1", "R1@v1", "P1@v1"],
            }
        ],
        "next_workflow_stage": "patch",
    }

    with pytest.raises(SchemaValidationError):
        validate_json_schema(decision, schema)

    decision["create_tasks"][0]["input_artifact_ids"].append("E1@v1")
    validate_json_schema(decision, schema)


def test_stage_schema_requires_gate_for_dynamic_expansion() -> None:
    create_task = {
        "node_type": "INVESTIGATION_TASK",
        "agent_type": "InvestigatorAgent",
        "mode": "failure_reproduction",
        "objective": "补充真实失败复现证据",
    }
    invalid = {
        "decision_id": "D-expansion-without-gate",
        "action": "CREATE_TASK",
        "reason": "新增复现调查",
        "create_tasks": [create_task],
        "next_workflow_stage": "investigation",
    }
    valid = {
        **invalid,
        "decision_id": "D-expansion-with-gate",
        "evidence_refs": ["E1@v1"],
        "gate_record": {
            "gate_name": "missing_reproduction_evidence",
            "trigger_artifact_refs": ["E1@v1"],
            "reason": "现有源码证据不能代替失败复现",
            "added_node_count": 1,
            "budget_effect": "新增一个受限复现节点",
        },
    }
    snapshot = {
        "workflow_stage": "investigation",
        "nodes": [
            {
                "node_id": "N1",
                "node_type": "INVESTIGATION_TASK",
                "status": "SUCCEEDED",
            }
        ],
        "artifacts": [
            {"artifact_ref": "E1@v1", "artifact_type": "evidence"}
        ],
    }

    outcome = SupervisorAgent(
        FakeModelAdapter([invalid, valid]),
        generation_config=GenerationConfig(max_retries=1),
    ).decide(snapshot)

    assert outcome.attempts == 2
    assert outcome.decision.gate_record is not None
    assert outcome.decision.evidence_refs == ("E1@v1",)


def test_safe_fallback_executes_approved_patch_replan() -> None:
    supervisor = SupervisorAgent(
        FakeModelAdapter(["bad"]),
        generation_config=GenerationConfig(max_retries=0),
        additional_schema_retries=0,
    )
    snapshot = {
        "workflow_stage": "patch",
        "state_version": 63,
        "nodes": [
            {"node_id": "N4", "node_type": "PATCH_TASK", "status": "SUCCEEDED"},
            {"node_id": "N7", "node_type": "REPLAN_TASK", "status": "SUCCEEDED"},
        ],
        "selections": {
            "hypothesis_resolution": {
                "status": "accepted",
                "accepted_refs": ["H1@v1"],
                "review_refs": ["R1@v1"],
            }
        },
        "recovery": {
            "pending_replan": True,
            "target_stage": "patch",
            "target_node_type": "PATCH_TASK",
            "replan_ref": "RP1@v1",
            "trigger_refs": ["V1@v1"],
            "remaining_replans": 0,
        },
        "artifacts": [
            {"artifact_ref": "H1@v1", "artifact_type": "hypothesis", "content": {}},
            {"artifact_ref": "R1@v1", "artifact_type": "review", "content": {}},
            {"artifact_ref": "P1@v1", "artifact_type": "patch_candidate", "content": {}},
            {"artifact_ref": "V1@v1", "artifact_type": "validation_result", "content": {}},
            {"artifact_ref": "RP1@v1", "artifact_type": "replan_record", "content": {}},
        ],
    }

    outcome = supervisor.decide(snapshot)

    assert outcome.model_id == "deterministic-safe-fallback"
    assert outcome.decision.action is DecisionAction.CREATE_TASK
    assert outcome.decision.create_tasks[0].node_type == "PATCH_TASK"
    assert set(outcome.decision.create_tasks[0].input_artifact_ids) == {
        "H1@v1",
        "R1@v1",
        "P1@v1",
        "V1@v1",
    }
    assert outcome.decision.gate_record is not None
    assert set(outcome.decision.gate_record.trigger_artifact_refs) == {
        "RP1@v1",
        "V1@v1",
    }


def test_safe_fallback_does_not_auto_accept_hypothesis(tmp_path: Path) -> None:
    trace_path = tmp_path / "safe-fallback.jsonl"
    supervisor = SupervisorAgent(
        FakeModelAdapter(["bad"]),
        generation_config=GenerationConfig(max_retries=0),
        additional_schema_retries=0,
        trace_writer=TraceWriter(trace_path),
    )
    snapshot = {
        "workflow_stage": "diagnosis",
        "state_version": 7,
        "nodes": [],
        "selections": {"hypothesis_resolution": {"status": "unresolved"}},
        "artifacts": [
            {
                "artifact_ref": "E1@v1",
                "artifact_type": "evidence",
                "content": {"claim": "边界输入触发 IndexError"},
            },
            {
                "artifact_ref": "H1@v1",
                "artifact_type": "hypothesis",
                "content": {"supporting_evidence": ["E1@v1"]},
            },
        ],
    }

    outcome = supervisor.decide(snapshot)

    assert outcome.decision.action is DecisionAction.CREATE_TASK
    request = outcome.decision.create_tasks[0]
    assert request.agent_type == "ReviewerAgent"
    assert request.mode == "root_cause_recommendation"
    assert set(request.input_artifact_ids) == {"E1@v1", "H1@v1"}
    assert outcome.decision.gate_record is not None
    assert outcome.model_id == "deterministic-safe-fallback"
    events = [json.loads(line)["event_type"] for line in trace_path.read_text().splitlines()]
    assert "supervisor_decision_recovery" in events


def _schema_actions(schema: dict[str, object]) -> set[str]:
    variants = schema["oneOf"]
    assert isinstance(variants, list)
    return {
        str(item["properties"]["action"]["const"])
        for item in variants
    }


def _schema_node_types(schema: dict[str, object]) -> set[str]:
    variants = schema["oneOf"]
    assert isinstance(variants, list)
    return {
        str(item["properties"]["node_type"]["const"])
        for variant in variants
        if variant["properties"]["action"]["const"] == "CREATE_TASK"
        for item in variant["properties"]["create_tasks"]["items"]["oneOf"]
    }


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
