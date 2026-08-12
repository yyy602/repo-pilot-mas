from __future__ import annotations

from repo_pilot_mas.evaluation_budget import (
    effective_engine_budget,
    effective_engine_limit_view,
)
from repo_pilot_mas.orchestration import EngineBudget
from scripts.run_closed_loop_final_evaluation import _within_budget


def _evaluation_limits() -> dict[str, int]:
    return {
        "max_supervisor_decisions": 24,
        "max_supervisor_api_calls": 40,
        "max_worker_model_calls": 120,
        "max_tool_calls": 120,
        "max_input_tokens": 600_000,
        "max_output_tokens": 100_000,
        "max_runtime_seconds": 1200,
    }


def _runtime_limits() -> dict[str, int]:
    return {
        "max_nodes": 64,
        "max_concurrent_nodes": 4,
        "max_supervisor_calls": 18,
        "max_tool_calls": 30,
        "max_input_tokens": 200_000,
        "max_output_tokens": 20_000,
        "max_runtime_seconds": 900,
        "max_replans": 1,
        "max_retries_per_node": 1,
        "max_no_progress_decisions": 2,
    }


def test_final_runner_maps_all_hybrid_limits() -> None:
    resolved = effective_engine_budget(
        _runtime_limits(),
        _evaluation_limits(),
    )

    assert resolved["max_supervisor_calls"] == 24
    assert resolved["max_tool_calls"] == 120
    assert resolved["max_input_tokens"] == 600_000
    assert resolved["max_output_tokens"] == 100_000
    assert resolved["max_runtime_seconds"] == 1200
    assert resolved["max_nodes"] == 64
    assert resolved["max_replans"] == 1


def test_engine_and_report_use_same_effective_budget() -> None:
    resolved = effective_engine_budget(
        _runtime_limits(),
        _evaluation_limits(),
    )
    engine = EngineBudget(**resolved)

    assert effective_engine_limit_view(
        engine.to_dict()
    ) == effective_engine_limit_view(resolved)


def test_budget_exhausted_cannot_be_reported_within_budget() -> None:
    limits = _evaluation_limits()
    usage = {
        "duration_ms": 1,
        "tool_calls": 1,
        "input_tokens": 1,
        "output_tokens": limits["max_output_tokens"] + 1,
        "supervisor_policy_calls": 1,
        "supervisor_api_calls": 1,
        "worker_model_calls": 1,
    }

    assert _within_budget(usage, limits) is False


def test_engine_decision_budget_is_distinct_from_api_route_attempts() -> None:
    limits = _evaluation_limits()
    resolved = effective_engine_budget(
        {
            "max_supervisor_calls": 18,
            "max_tool_calls": 30,
            "max_input_tokens": 200_000,
            "max_output_tokens": 20_000,
            "max_runtime_seconds": 900,
        },
        limits,
    )

    assert resolved["max_supervisor_calls"] == 24
    usage = {
        "duration_ms": 1,
        "tool_calls": 1,
        "input_tokens": 1,
        "output_tokens": 1,
        "supervisor_policy_calls": 24,
        "supervisor_api_calls": 29,
        "worker_model_calls": 1,
    }
    assert _within_budget(usage, limits) is True
    usage["supervisor_api_calls"] = 41
    assert _within_budget(usage, limits) is False
