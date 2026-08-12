from __future__ import annotations

from repo_pilot_mas.evaluation import trace_metrics
from repo_pilot_mas.final_evaluation import (
    aggregate_closed_loop_results,
    build_artifact_ref,
    closed_loop_task_mechanism,
)


def _artifact(
    artifact_id: str,
    artifact_type: str,
    content: dict[str, object],
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "version": 1,
        "content": content,
    }


def _closed_loop_artifacts(*, review_verdict: str = "supported") -> list[dict[str, object]]:
    return [
        _artifact(
            "E1",
            "evidence",
            {
                "verified": True,
                "tool_trace_ids": ["tool-1"],
            },
        ),
        _artifact(
            "H1",
            "hypothesis",
            {"supporting_evidence": ["E1@v1"]},
        ),
        _artifact(
            "R1",
            "review",
            {
                "mode": "root_cause_recommendation",
                "verdict": review_verdict,
                "evidence_refs": ["E1@v1"],
                "failure_explained": True,
                "causal_chain_complete": True,
                "alternative_causes": ["排除了测试配置错误"],
                "counterexample_checked": True,
                "verification_steps_executed": ["核对失败输出"],
                "remaining_uncertainty": [],
            },
        ),
        _artifact(
            "P1",
            "patch_candidate",
            {
                "based_on_hypothesis_refs": ["H1@v1"],
                "primary_hypothesis_ref": "H1@v1",
            },
        ),
        _artifact(
            "V1",
            "validation_result",
            {"patch_ref": "P1@v1", "passed": True},
        ),
    ]


def test_artifact_ref_is_built_from_id_and_version() -> None:
    item = {
        "artifact_id": "H1",
        "version": 2,
        "artifact_ref": "incorrect@v9",
    }

    assert build_artifact_ref(item) == "H1@v2"


def test_supported_review_is_counted() -> None:
    metrics = closed_loop_task_mechanism(
        (),
        _closed_loop_artifacts(),
        {
            "status": "accepted",
            "accepted_refs": ["H1@v1"],
            "primary_ref": "H1@v1",
            "review_refs": ["R1@v1"],
        },
        engine_succeeded=True,
        workspace_clean=True,
    )

    assert metrics["hypothesis_accepted"] is True
    assert metrics["evidence_gate_passed"] is True
    assert metrics["root_cause_review_passed"] is True
    assert metrics["patch_bound_to_accepted_set"] is True
    assert metrics["patch_validation_passed"] is True


def test_rejected_review_is_not_counted() -> None:
    metrics = closed_loop_task_mechanism(
        (),
        _closed_loop_artifacts(review_verdict="unsupported"),
        {
            "status": "accepted",
            "accepted_refs": ["H1@v1"],
            "primary_ref": "H1@v1",
            "review_refs": ["R1@v1"],
        },
        engine_succeeded=False,
        workspace_clean=True,
    )

    assert metrics["root_cause_review_passed"] is False


def test_trace_metrics_separate_routing_and_recovery() -> None:
    events = [
        {
            "event_type": "supervisor_route_attempt",
            "data": {"model_id": "max", "api_key_env": "KEY_1"},
        },
        {
            "event_type": "supervisor_route_exhausted",
            "data": {"model_id": "max", "api_key_env": "KEY_1"},
        },
        {
            "event_type": "supervisor_route_attempt",
            "data": {"model_id": "max", "api_key_env": "KEY_2"},
        },
        {
            "event_type": "supervisor_route_succeeded",
            "data": {"model_id": "max", "api_key_env": "KEY_2"},
        },
        {
            "event_type": "supervisor_format_repair_attempted",
            "data": {"layer": "model_adapter"},
        },
        {
            "event_type": "supervisor_snapshot_compacted",
            "data": {"original_chars": 1000, "compact_chars": 250},
        },
        {
            "event_type": "agent_input_contract_rejected",
            "data": {"node_id": "N1"},
        },
        {
            "event_type": "supervisor_decision_applied",
            "data": {
                "decision": {
                    "action": "CREATE_TASK",
                    "create_tasks": [{"node_type": "REVIEW_TASK"}],
                }
            },
        },
        {
            "event_type": "deterministic_worker_retry_scheduled",
            "data": {"node_ids": ["N2"]},
        },
    ]

    metrics = trace_metrics(events)

    assert metrics["route_attempts_by_model"] == {"max": 2}
    assert metrics["route_successes_by_model"] == {"max": 1}
    assert metrics["route_exhausted_by_model"] == {"max": 1}
    assert metrics["supervisor_format_repair_count"] == 1
    assert metrics["supervisor_snapshot_count"] == 1
    assert metrics["supervisor_snapshot_reduction_ratio"] == 0.75
    assert metrics["contract_rejection_count"] == 1
    assert metrics["node_rebuild_count"] == 1
    assert metrics["worker_retry_count"] == 1


def test_contract_rejection_metric_counts_creation_time_rejection_once() -> None:
    metrics = trace_metrics(
        [
            {
                "event_id": "contract-event",
                "event_type": "agent_input_contract_rejected",
                "data": {
                    "decision_id": "D-bad-review",
                    "code": "AGENT_INPUT_CONTRACT_VIOLATION",
                },
            },
            {
                "event_id": "engine-event",
                "event_type": "supervisor_decision_rejected",
                "data": {
                    "decision_id": "D-bad-review",
                    "code": "AGENT_INPUT_CONTRACT_VIOLATION",
                },
            },
        ]
    )

    assert metrics["contract_rejection_count"] == 1


def test_route_fallback_does_not_reduce_task_success() -> None:
    closed_loop = {
        "hypothesis_accepted": True,
        "evidence_gate_passed": True,
        "root_cause_review_passed": True,
        "patch_bound_to_accepted_set": True,
        "patch_validation_passed": True,
        "validation_passed": True,
        "workspace_clean": True,
        "route_attempts_by_model": {"max": 2},
        "route_successes_by_model": {"max": 1},
        "route_exhausted_by_model": {"max": 1},
        "supervisor_snapshot_count": 2,
        "supervisor_snapshot_original_chars": 1000,
        "supervisor_snapshot_compact_chars": 250,
    }
    result = {
        "status": "succeeded",
        "termination": {
            "code": "VALIDATION_PASSED",
            "stage": "finalization",
            "failure_class": "none",
        },
        "validation": {
            "target_test_passed": True,
            "regression_passed": True,
            "patch_applied": True,
            "syntax_valid": True,
        },
        "closed_loop": closed_loop,
        "usage": {
            "total_tokens": 100,
            "duration_ms": 10,
            "supervisor_tokens": 60,
            "worker_tokens": 40,
        },
        "budget": {"within_budget": True},
        "source_integrity": {"unchanged": True},
        "mechanism": {"execution_path": "standard"},
        "result_path": "result.json",
    }

    summary = aggregate_closed_loop_results("closed_loop", [result])

    assert summary["task_resolution_rate"] == 1.0
    assert summary["route_exhausted_by_model"] == {"max": 1}
    assert summary["supervisor_tokens"] == 60
    assert summary["worker_tokens"] == 40
    assert summary["supervisor_snapshot_count"] == 2
    assert summary["supervisor_snapshot_reduction_ratio"] == 0.75
    assert summary["termination_codes"] == {"VALIDATION_PASSED": 1}
    assert summary["termination_stages"] == {"finalization": 1}
    assert summary["termination_failure_classes"] == {"none": 1}
