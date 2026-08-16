from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.run_closed_loop_final_evaluation import (
    _PHASE,
    _formal_evaluation_executed,
    _runtime_tree_is_clean,
    _terminal_failure_class,
    _validate_supervisor_execution_tier,
    development_acceptance_gates,
    frozen_test_acceptance_gates,
    pre_freeze_record_gates,
    verify_frozen_identity,
)

_LIMITS = {
    "max_runtime_seconds": 100,
    "max_tool_calls": 10,
    "max_input_tokens": 100,
    "max_output_tokens": 100,
    "max_supervisor_decisions": 10,
    "max_supervisor_api_calls": 10,
    "max_worker_model_calls": 10,
}


def _result(
    status: str = "succeeded",
    *,
    reason: str = "VALIDATION_PASSED",
    within_budget: bool = True,
) -> dict[str, object]:
    return {
        "status": status,
        "reason": reason,
        "termination": {
            "code": reason,
            "stage": "validation",
            "message": reason,
            "failure_class": "none" if status == "succeeded" else "target_test_failure",
        },
        "closed_loop": {
            "route_attempts_by_model": {"max": 1},
            "route_successes_by_model": {"max": 1},
        },
        "usage": {
            "duration_ms": 1,
            "tool_calls": 1,
            "input_tokens": 1,
            "output_tokens": 1,
            "supervisor_api_calls": 1,
            "worker_model_calls": 1,
        },
        "budget": {"within_budget": within_budget},
    }


def test_complete_development_gate_requires_four_solutions_and_clean_framework() -> None:
    gates = development_acceptance_gates(
        [*(_result() for _ in range(4)), _result("failed", reason="VALIDATION_FAILED")],
        {
            "solved": 4,
            "contract_rejection_count": 0,
            "supervisor_snapshot_count": 5,
            "supervisor_snapshot_reduction_ratio": 0.5,
        },
        _LIMITS,
        expected_task_count=5,
    )

    assert all(gates.values())


def test_complete_development_and_frozen_test_are_both_formal_evaluations() -> None:
    assert _formal_evaluation_executed(
        full_run=False,
        full_development_run=True,
    )
    assert _formal_evaluation_executed(
        full_run=True,
        full_development_run=False,
    )
    assert not _formal_evaluation_executed(
        full_run=False,
        full_development_run=False,
    )


def test_local_supervisor_is_limited_to_diagnostic_runs() -> None:
    local = {"provider": "local_transformers"}

    _validate_supervisor_execution_tier(
        local,
        split="development",
        task_id="quixbugs_gcd",
        dry_run=False,
    )
    _validate_supervisor_execution_tier(
        local,
        split="development",
        task_id=None,
        dry_run=True,
    )
    with pytest.raises(ValueError, match="必须使用正式 DashScope Supervisor"):
        _validate_supervisor_execution_tier(
            local,
            split="development",
            task_id=None,
            dry_run=False,
        )
    with pytest.raises(ValueError, match="必须使用正式 DashScope Supervisor"):
        _validate_supervisor_execution_tier(
            local,
            split="test",
            task_id=None,
            dry_run=False,
        )


def test_formal_runs_require_pre_freeze_and_frozen_identity_records() -> None:
    cases = (
        (
            "development",
            "missing-verification-record",
            "完整 Development 必须提供 --verification-record",
        ),
        (
            "test",
            "missing-freeze-manifest",
            "完整 Frozen Test 必须提供 --freeze-manifest",
        ),
    )
    for split, run_id, expected_error in cases:
        completed = subprocess.run(
            (
                sys.executable,
                "scripts/run_closed_loop_final_evaluation.py",
                "--split",
                split,
                "--run-id",
                run_id,
            ),
            check=False,
            text=True,
            capture_output=True,
        )

        assert completed.returncode != 0
        assert expected_error in completed.stderr


def test_pre_freeze_record_requires_checks_and_clean_runtime_tree() -> None:
    record = {
        "passed": True,
        "repository_fingerprint": "repo-sha",
        "checks": [
            {"command": "python -m pytest -q", "exit_code": 0},
            {"command": "python -m ruff check .", "exit_code": 0},
            {"command": "python -m compileall -q src", "exit_code": 0},
            {"command": "git diff --check", "exit_code": 0},
            {
                "command": "git status --short -- pyproject.toml configs data scripts src tests",
                "exit_code": 0,
                "result": "clean",
            },
        ],
    }

    assert all(
        pre_freeze_record_gates(
            record,
            "repo-sha",
            runtime_tree_clean=True,
        ).values()
    )
    assert not all(
        pre_freeze_record_gates(
            record,
            "repo-sha",
            runtime_tree_clean=False,
        ).values()
    )
    record["checks"] = record["checks"][:-1]
    assert not all(
        pre_freeze_record_gates(
            record,
            "repo-sha",
            runtime_tree_clean=True,
        ).values()
    )


def test_runtime_tree_clean_scope_ignores_docs_but_rejects_runtime_files(
    tmp_path: Path,
) -> None:
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("说明", encoding="utf-8")

    assert _runtime_tree_is_clean(tmp_path) is True

    source = tmp_path / "src"
    source.mkdir()
    (source / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert _runtime_tree_is_clean(tmp_path) is False


def test_development_gate_fails_on_runner_contract_or_budget_accounting_error() -> None:
    results = [*(_result() for _ in range(4)), _result("failed", reason="RUNNER_ERROR:x")]
    results[0]["budget"] = {"within_budget": False}

    gates = development_acceptance_gates(
        results,
        {
            "solved": 4,
            "contract_rejection_count": 1,
            "supervisor_snapshot_count": 5,
            "supervisor_snapshot_reduction_ratio": 0.5,
        },
        _LIMITS,
        expected_task_count=5,
    )

    assert gates["framework_runner_errors_zero"] is False
    assert gates["reviewer_input_contract_errors_zero"] is False
    assert gates["budget_accounting_contradictions_zero"] is False


def test_development_gate_rejects_structured_framework_terminal_error() -> None:
    results = [*(_result() for _ in range(4)), _result("failed")]
    results[-1]["termination"] = {
        "code": "OUTPUT_TOKEN_BUDGET_EXHAUSTED",
        "stage": "review",
        "message": "budget exhausted",
        "failure_class": "budget_exhausted",
    }

    gates = development_acceptance_gates(
        results,
        {
            "solved": 4,
            "contract_rejection_count": 0,
            "supervisor_snapshot_count": 5,
            "supervisor_snapshot_reduction_ratio": 0.5,
        },
        _LIMITS,
        expected_task_count=5,
    )

    assert gates["framework_terminal_errors_zero"] is False


def test_terminal_failure_class_prefers_validation_over_generic_termination() -> None:
    failure_class = _terminal_failure_class(
        engine_status="terminated",
        termination_code="SUPERVISOR_TERMINATED",
        artifacts=(
            {
                "artifact_type": "validation_result",
                "content": {"failure_class": "regression_failure"},
            },
        ),
    )

    assert _PHASE == 6
    assert failure_class == "regression_failure"


def test_terminal_failure_class_keeps_budget_as_framework_failure() -> None:
    failure_class = _terminal_failure_class(
        engine_status="failed",
        termination_code="SUPERVISOR_CALL_BUDGET_EXHAUSTED",
        artifacts=(
            {
                "artifact_type": "validation_result",
                "content": {"failure_class": "target_test_failure"},
            },
        ),
    )

    assert failure_class == "budget_exhausted"


def test_terminal_failure_class_distinguishes_provider_quota_exhaustion() -> None:
    failure_class = _terminal_failure_class(
        engine_status="failed",
        termination_code="SUPERVISOR_ROUTES_EXHAUSTED",
        artifacts=(),
    )

    assert failure_class == "provider_quota_exhausted"


def test_frozen_identity_requires_accepted_development_and_exact_runtime() -> None:
    identity = {
        "repository_commit": "commit-sha",
        "repository_fingerprint": "repo-sha",
        "runtime_tree_clean": True,
        "config_sha256": {"runtime": "config-sha"},
        "supervisor_prompt_sha256": "prompt-sha",
        "model_routing": {"model_order": ["max", "flash"]},
    }
    manifest = {
        "phase": 6,
        "evaluation_split": "development",
        "execution_mode": "runtime",
        "final_evaluation_executed": True,
        "pre_freeze_verification": {
            "passed": True,
            "repository_fingerprint": "repo-sha",
            "verification_gates": {
                "preflight_declared_passed": True,
                "preflight_repository_fingerprint_matches": True,
                "preflight_pytest_passed": True,
                "preflight_ruff_passed": True,
                "preflight_compileall_passed": True,
                "preflight_diff_passed": True,
                "preflight_runtime_tree_clean": True,
            },
        },
        **identity,
    }
    acceptance = {"passed": True, "full_development_run": True}

    assert all(
        verify_frozen_identity(manifest, acceptance, identity).values()
    )
    changed = {**identity, "supervisor_prompt_sha256": "changed"}
    gates = verify_frozen_identity(manifest, acceptance, changed)
    assert gates["freeze_supervisor_prompt_matches"] is False
    changed_commit = {**identity, "repository_commit": "changed"}
    gates = verify_frozen_identity(manifest, acceptance, changed_commit)
    assert gates["freeze_repository_commit_matches"] is False
    dirty = {**identity, "runtime_tree_clean": False}
    gates = verify_frozen_identity(manifest, acceptance, dirty)
    assert gates["freeze_runtime_tree_clean"] is False


def test_frozen_test_gate_rejects_uniform_framework_failure() -> None:
    results = [_result("failed") for _ in range(10)]
    for item in results:
        item["termination"] = {
            "code": "OUTPUT_TOKEN_BUDGET_EXHAUSTED",
            "stage": "review",
            "message": "budget exhausted",
            "failure_class": "budget_exhausted",
        }

    gates = frozen_test_acceptance_gates(
        results,
        expected_task_count=10,
    )

    assert gates["frozen_test_not_uniform_framework_failure"] is False


def test_provider_quota_exhaustion_is_a_framework_terminal_failure() -> None:
    development_results = [*(_result() for _ in range(4)), _result("failed")]
    development_results[-1]["termination"] = {
        "code": "SUPERVISOR_ROUTES_EXHAUSTED",
        "stage": "initialization",
        "message": "all routes exhausted",
        "failure_class": "provider_quota_exhausted",
    }
    development_gates = development_acceptance_gates(
        development_results,
        {
            "solved": 4,
            "contract_rejection_count": 0,
            "supervisor_snapshot_count": 5,
            "supervisor_snapshot_reduction_ratio": 0.5,
        },
        _LIMITS,
        expected_task_count=5,
    )
    frozen_results = [dict(development_results[-1]) for _ in range(10)]

    assert development_gates["framework_terminal_errors_zero"] is False
    assert frozen_test_acceptance_gates(
        frozen_results,
        expected_task_count=10,
    )["frozen_test_not_uniform_framework_failure"] is False


def test_frozen_test_gate_accepts_mixed_business_outcomes() -> None:
    results = [_result() for _ in range(4)]
    results.extend(_result("failed", reason="VALIDATION_FAILED") for _ in range(6))

    gates = frozen_test_acceptance_gates(
        results,
        expected_task_count=10,
    )

    assert all(gates.values())
