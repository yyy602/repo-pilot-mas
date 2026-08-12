from __future__ import annotations

import json
from pathlib import Path

import pytest

from repo_pilot_mas.evaluation import (
    FixedPipelineSupervisor,
    aggregate_system_results,
    budget_outcomes_fail_closed,
    constrained_dynamic_prompt,
    hybrid_mechanism_metrics,
    load_evaluation_suite,
    raw_usage,
    trace_metrics,
)
from repo_pilot_mas.models import FakeModelAdapter, GenerationConfig


def test_phase6_suite_freezes_ten_unseen_tasks() -> None:
    suite = load_evaluation_suite("data/quixbugs/phase6_suite.json")

    assert len(suite.development_tasks) == 5
    assert len(suite.test_tasks) == 10
    assert not set(suite.development_task_ids).intersection(
        task.task_id for task in suite.test_tasks
    )
    assert {task.metadata["dataset"] for task in suite.test_tasks} == {"quixbugs"}
    assert set(suite.development_task_ids) == {
        "quixbugs_find_first_in_sorted",
        "quixbugs_gcd",
        "quixbugs_is_valid_parenthesization",
        "quixbugs_bucketsort",
        "quixbugs_flatten",
    }
    assert {
        "quixbugs_max_sublist_sum",
        "quixbugs_powerset",
    }.issubset(task.task_id for task in suite.test_tasks)


def test_fixed_pipeline_starts_two_investigators_then_two_diagnosticians() -> None:
    supervisor = FixedPipelineSupervisor(
        FakeModelAdapter([]),
        generation_config=GenerationConfig(),
    )
    initial = {
        "state_version": 0,
        "nodes": [],
        "artifacts": [],
        "selections": {},
    }
    first = supervisor.decide(initial).decision

    assert [item.mode for item in first.create_tasks] == [
        "code_retrieval",
        "failure_reproduction",
    ]

    investigated = {
        "state_version": 8,
        "nodes": [
            {"node_id": "N1", "node_type": "INVESTIGATION_TASK", "status": "SUCCEEDED"},
            {"node_id": "N2", "node_type": "INVESTIGATION_TASK", "status": "SUCCEEDED"},
        ],
        "artifacts": [
            {"artifact_ref": "E1@v1", "artifact_type": "evidence", "content": {}},
            {"artifact_ref": "E2@v1", "artifact_type": "evidence", "content": {}},
        ],
        "selections": {},
    }
    second = supervisor.decide(investigated).decision

    assert [item.mode for item in second.create_tasks] == ["control_flow", "data_flow"]
    assert second.gate_record is not None
    assert second.gate_record.gate_name == "fixed_protocol_second_diagnosis"


@pytest.mark.parametrize(
    ("system_id", "fragment"),
    [
        ("no_second_diagnostician", "最多创建一个 DIAGNOSIS_TASK"),
        ("no_challenge_rebuttal", "禁止创建 CHALLENGE_TASK"),
    ],
)
def test_ablation_prompt_adds_only_declared_constraint(system_id: str, fragment: str) -> None:
    prompt = constrained_dynamic_prompt("BASE", system_id)
    assert prompt.startswith("BASE")
    assert fragment in prompt


def test_raw_usage_applies_per_request_price_tier(tmp_path: Path) -> None:
    records = [
        ("a", 10_000, 1_000),
        ("b", 40_000, 2_000),
    ]
    for name, input_tokens, output_tokens in records:
        (tmp_path / f"{name}.json").write_text(
            json.dumps(
                {
                    "model_id": "flash",
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    },
                }
            ),
            encoding="utf-8",
        )
    pricing = {
        "flash": [
            {
                "max_input_tokens": 32_000,
                "input_cny_per_million": 0.2,
                "output_cny_per_million": 0.8,
            },
            {
                "max_input_tokens": 256_000,
                "input_cny_per_million": 0.6,
                "output_cny_per_million": 2.4,
            },
        ]
    }

    usage = raw_usage(tmp_path, pricing)

    assert usage["calls"] == 2
    assert usage["total_tokens"] == 53_000
    assert usage["estimated_cost_cny"] == pytest.approx(0.0316)


def test_trace_metrics_counts_real_overlap_routes_and_gates() -> None:
    events = [
        {
            "event_type": "worker_completed",
            "data": {
                "started_at": "2026-08-02T00:00:00+00:00",
                "finished_at": "2026-08-02T00:00:02+00:00",
            },
        },
        {
            "event_type": "worker_completed",
            "data": {
                "started_at": "2026-08-02T00:00:01+00:00",
                "finished_at": "2026-08-02T00:00:03+00:00",
            },
        },
        {
            "event_type": "supervisor_route_succeeded",
            "data": {"model_id": "max", "api_key_env": "KEY_1"},
        },
        {
            "event_type": "supervisor_decision_applied",
            "data": {"decision": {"gate_record": {"gate_name": "evidence"}}},
        },
        {"event_type": "tool_call", "data": {}},
        {
            "event_type": "supervisor_decision_rejected",
            "data": {"decision_id": "D1", "code": "INVALID_WORKER"},
        },
        {
            "event_type": "supervisor_decision_rejected",
            "data": {"decision_id": "D1", "message": "runtime mirror"},
        },
    ]

    metrics = trace_metrics(events)

    assert metrics["parallel_overlap_pairs"] == 1
    assert metrics["tool_calls"] == 1
    assert metrics["gate_records"] == [{"gate_name": "evidence"}]
    assert metrics["invalid_decisions"] == 1
    assert metrics["route_usage"] == {
        "supervisor_route_succeeded:max:KEY_1": 1,
    }


def test_hybrid_mechanism_and_aggregate_keep_failures_in_denominator() -> None:
    mechanism = hybrid_mechanism_metrics(
        [
            {"node_type": "INVESTIGATION_TASK"},
            {"node_type": "DIAGNOSIS_TASK"},
            {"node_type": "PATCH_TASK"},
        ],
        [],
        succeeded=True,
        expected_complexity="simple",
    )
    assert mechanism["execution_path"] == "fast"
    assert mechanism["unnecessary_expansion"] is False

    base = {
        "validation": {
            "target_test_passed": True,
            "regression_passed": True,
            "patch_applied": True,
            "syntax_valid": True,
            "protected_path_violations": 0,
        },
        "usage": {
            "model_calls": 1,
            "supervisor_api_calls": 0,
            "worker_model_calls": 1,
            "tool_calls": 2,
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "duration_ms": 100,
            "estimated_api_cost_cny": 0,
        },
        "budget": {"within_budget": True},
        "mechanism": mechanism,
        "result_path": "one.json",
    }
    summary = aggregate_system_results(
        "test",
        [
            {**base, "status": "succeeded"},
            {
                **base,
                "status": "failed",
                "validation": {**base["validation"], "target_test_passed": False},
                "result_path": "two.json",
            },
        ],
    )

    assert summary["task_count"] == 2
    assert summary["solved"] == 1
    assert summary["task_resolution_rate"] == 0.5
    assert summary["target_test_pass_rate"] == 0.5


def test_budget_exhaustion_is_valid_only_when_result_fails_closed() -> None:
    assert budget_outcomes_fail_closed(
        [
            {"status": "succeeded", "budget": {"within_budget": True}},
            {"status": "failed", "budget": {"within_budget": False}},
        ]
    )
    assert not budget_outcomes_fail_closed(
        [{"status": "succeeded", "budget": {"within_budget": False}}]
    )
