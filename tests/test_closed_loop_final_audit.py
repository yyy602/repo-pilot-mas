from __future__ import annotations

import hashlib
from pathlib import Path

from repo_pilot_mas.evaluation import (
    load_evaluation_suite,
    trace_metrics,
    tree_digest,
)
from repo_pilot_mas.final_evaluation import (
    aggregate_closed_loop_results,
    closed_loop_task_mechanism,
    save_json,
)
from repo_pilot_mas.runtime import TraceWriter
from scripts.audit_closed_loop_final_evaluation import (
    _budget_limits_match_manifest,
    _development_gates_valid,
    _freeze_verification_valid,
    _frozen_test_gates_valid,
    _git_commit,
    _pre_freeze_verification_valid,
    _repository_fingerprint,
    _saved_result_files_match,
    _successful_results_valid,
    _uniform_framework_failure,
    audit_closed_loop_runs,
    resolve_audit_suite_path,
)


def _auditable_result(status: str = "succeeded") -> dict[str, object]:
    return {
        "status": status,
        "reason": "VALIDATION_PASSED" if status == "succeeded" else "VALIDATION_FAILED",
        "termination": {
            "code": "VALIDATION_PASSED" if status == "succeeded" else "VALIDATION_FAILED",
            "stage": "validation",
            "failure_class": "none" if status == "succeeded" else "target_test_failure",
        },
        "closed_loop": {
            "route_attempts_by_model": {"max": 1},
            "route_successes_by_model": {"max": 1},
        },
    }


def test_audit_resolves_the_same_five_plus_ten_suite_as_runner() -> None:
    suite_path = resolve_audit_suite_path(
        Path("configs/closed_loop_evaluation.yaml")
    )
    suite = load_evaluation_suite(suite_path)

    assert suite_path == Path("data/quixbugs/phase6_suite.json")
    assert len(suite.development_tasks) == 5
    assert len(suite.test_tasks) == 10
    assert not set(suite.development_task_ids).intersection(
        task.task_id for task in suite.test_tasks
    )


def test_audit_suite_override_is_explicit() -> None:
    override = Path("data/quixbugs/closed_loop_suite.json")

    assert resolve_audit_suite_path(
        Path("configs/closed_loop_evaluation.yaml"),
        override,
    ) == override


def test_pre_freeze_verification_requires_record_hash_and_all_checks(
    tmp_path: Path,
) -> None:
    record = tmp_path / "pre_freeze.json"
    record.write_text("{}", encoding="utf-8")

    preflight = {
        "passed": True,
        "repository_fingerprint": "repo-sha",
        "record_path": str(record),
        "record_sha256": hashlib.sha256(record.read_bytes()).hexdigest(),
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
        "verification_gates": {
            "preflight_declared_passed": True,
            "preflight_repository_fingerprint_matches": True,
            "preflight_pytest_passed": True,
            "preflight_ruff_passed": True,
            "preflight_compileall_passed": True,
            "preflight_diff_passed": True,
            "preflight_runtime_tree_clean": True,
        },
    }
    manifest = {
        "repository_fingerprint": "repo-sha",
        "pre_freeze_verification": preflight,
    }

    assert _pre_freeze_verification_valid(manifest) is True
    preflight["checks"] = preflight["checks"][:-1]
    assert _pre_freeze_verification_valid(manifest) is False


def test_freeze_verification_rejects_missing_or_unbound_gates(
    tmp_path: Path,
) -> None:
    development_root = tmp_path / "development"
    development_root.mkdir()
    gates = {
        "freeze_phase_is_6": True,
        "freeze_is_full_development": True,
        "freeze_repository_fingerprint_matches": True,
        "freeze_repository_commit_matches": True,
        "freeze_runtime_tree_clean": True,
        "freeze_config_sha256_matches": True,
        "freeze_supervisor_prompt_matches": True,
        "freeze_model_routing_matches": True,
        "freeze_preflight_verification_passed": True,
    }
    manifest = {
        "freeze_verification": {
            "development_manifest": str(
                (development_root / "manifest.json").resolve()
            ),
            "development_manifest_sha256": "placeholder",
            "development_acceptance": str(
                (development_root / "acceptance.json").resolve()
            ),
            "development_acceptance_sha256": "placeholder",
            "gates": gates,
        }
    }

    (development_root / "manifest.json").write_text("manifest", encoding="utf-8")
    (development_root / "acceptance.json").write_text(
        "acceptance",
        encoding="utf-8",
    )
    verification = manifest["freeze_verification"]
    verification["development_manifest_sha256"] = hashlib.sha256(
        (development_root / "manifest.json").read_bytes()
    ).hexdigest()
    verification["development_acceptance_sha256"] = hashlib.sha256(
        (development_root / "acceptance.json").read_bytes()
    ).hexdigest()

    assert _freeze_verification_valid(manifest, development_root) is True
    manifest["freeze_verification"]["gates"] = {}
    assert _freeze_verification_valid(manifest, development_root) is False


def test_audit_recomputes_development_and_frozen_test_gates() -> None:
    development = [*(_auditable_result() for _ in range(4)), _auditable_result("failed")]
    development_summary = {
        "solved": 4,
        "contract_rejection_count": 0,
        "supervisor_snapshot_count": 5,
        "supervisor_snapshot_reduction_ratio": 0.5,
    }
    frozen = [_auditable_result() for _ in range(10)]

    assert _development_gates_valid(development, development_summary, 5)
    assert _frozen_test_gates_valid(frozen, 10)
    development[0]["closed_loop"]["route_successes_by_model"] = {}
    assert not _development_gates_valid(development, development_summary, 5)
    frozen[0]["termination"]["failure_class"] = "provider_quota_exhausted"
    frozen[0]["closed_loop"]["route_successes_by_model"] = {}
    assert not _frozen_test_gates_valid(frozen, 10)


def test_audit_budget_limits_must_match_manifest_exactly() -> None:
    manifest = {
        "hybrid_limits": {"max_tool_calls": 10},
        "effective_engine_limits": {"max_tool_calls": 10},
    }
    result = {
        "budget": {
            "limits": {"max_tool_calls": 10},
            "effective_engine_limits": {"max_tool_calls": 10},
        }
    }

    assert _budget_limits_match_manifest([result], manifest)
    result["budget"]["limits"]["max_tool_calls"] = 11
    assert not _budget_limits_match_manifest([result], manifest)


def test_audit_detects_task_result_divergence(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    persisted = {"task_id": "task-1", "status": "succeeded"}
    save_json(result_path, persisted)
    listed = {**persisted, "result_path": str(result_path)}
    save_json(result_path, listed)

    assert _saved_result_files_match([listed])
    listed["status"] = "failed"
    assert not _saved_result_files_match([listed])


def _write_audit_result(
    run_root: Path,
    task_id: str,
    *,
    succeeded: bool,
    limits: dict[str, int],
    engine_limits: dict[str, int],
    source_digest: str,
) -> dict[str, object]:
    task_root = run_root / "tasks" / task_id
    task_root.mkdir(parents=True)
    trace_path = task_root / "trace.jsonl"
    trace = TraceWriter(trace_path)
    trace.write(
        "effective_budget_loaded",
        {
            "evaluation_limits": limits,
            "engine_limits": engine_limits,
        },
    )
    trace.write(
        "supervisor_route_attempt",
        {"model_id": "max", "api_key_env": "DASHSCOPE_API_KEY_1"},
    )
    trace.write(
        "supervisor_route_succeeded",
        {"model_id": "max", "api_key_env": "DASHSCOPE_API_KEY_1"},
    )
    trace.write(
        "supervisor_snapshot_compacted",
        {"original_chars": 100, "compact_chars": 50},
    )
    raw_supervisor = task_root / "raw_model_responses" / "supervisor"
    raw_supervisor.mkdir(parents=True)
    save_json(
        raw_supervisor / "response.json",
        {
            "model_id": "max",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    (task_root / "langgraph.sqlite3").write_bytes(b"sqlite evidence")
    artifacts = [
        {
            "artifact_id": "P1",
            "artifact_type": "patch_candidate",
            "version": 1,
            "content": {"protected_path_check": True},
        },
        {
            "artifact_id": "V1",
            "artifact_type": "validation_result",
            "version": 1,
            "content": {
                "applied": True,
                "passed": succeeded,
                "target_test": {"exit_code": 0 if succeeded else 1},
                "regression_test": {"exit_code": 0},
                "static_check": {"exit_code": 0},
                "protected_path_check": True,
                "changed_files": ["target.py"],
            },
        },
    ]
    validation = {
        "patch_applied": True,
        "target_test_passed": succeeded,
        "regression_passed": True,
        "syntax_valid": True,
        "protected_path_violations": 0,
        "validation_count": 1,
        "passed_validation_count": 1 if succeeded else 0,
        "changed_files": ["target.py"],
    }
    closed_loop = closed_loop_task_mechanism(
        (),
        artifacts,
        {},
        engine_succeeded=succeeded,
        workspace_clean=True,
        recovery_metrics=trace_metrics(
            [
                {
                    "event_type": "supervisor_route_attempt",
                    "data": {
                        "model_id": "max",
                        "api_key_env": "DASHSCOPE_API_KEY_1",
                    },
                },
                {
                    "event_type": "supervisor_route_succeeded",
                    "data": {
                        "model_id": "max",
                        "api_key_env": "DASHSCOPE_API_KEY_1",
                    },
                },
                {
                    "event_type": "supervisor_snapshot_compacted",
                    "data": {"original_chars": 100, "compact_chars": 50},
                },
            ]
        ),
    )
    result = {
        "schema_version": 1,
        "protocol_version": "closed_loop_v1",
        "system_id": "closed_loop_dynamic",
        "seed": 0,
        "task_id": task_id,
        "status": "succeeded" if succeeded else "failed",
        "reason": "VALIDATION_PASSED" if succeeded else "VALIDATION_FAILED",
        "termination": {
            "code": "VALIDATION_PASSED" if succeeded else "VALIDATION_FAILED",
            "stage": "validation",
            "failure_class": "none" if succeeded else "target_test_failure",
        },
        "validation": validation,
        "closed_loop": closed_loop,
        "engine_status": "succeeded" if succeeded else "failed",
        "hypothesis_resolution": {},
        "nodes": [],
        "artifacts": artifacts,
        "mechanism": {"execution_path": "fast"},
        "usage": {
            "duration_ms": 1,
            "tool_calls": 0,
            "input_tokens": 1,
            "output_tokens": 1,
            "supervisor_policy_calls": 1,
            "supervisor_api_calls": 1,
            "worker_model_calls": 0,
            "model_calls": 1,
            "supervisor_tokens": 2,
            "worker_tokens": 0,
            "total_tokens": 2,
            "estimated_api_cost_cny": 0.0,
            "by_model": {
                "max": {
                    "calls": 1,
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "estimated_cost_cny": 0.0,
                }
            },
        },
        "budget": {
            "limits": limits,
            "effective_engine_limits": engine_limits,
            "within_budget": True,
        },
        "source_integrity": {
            "before_sha256": source_digest,
            "after_sha256": source_digest,
            "unchanged": True,
        },
        "result_path": str((task_root / "result.json").resolve()),
        "trace_path": str(trace_path.resolve()),
        "checkpoint_path": str((task_root / "langgraph.sqlite3").resolve()),
    }
    save_json(task_root / "result.json", result)
    return result


def test_complete_audit_recomputes_a_valid_five_plus_ten_protocol(
    tmp_path: Path,
) -> None:
    repo_root = Path.cwd().resolve()
    suite_path = Path("data/quixbugs/phase6_suite.json").resolve()
    suite = load_evaluation_suite(suite_path)
    development_root = tmp_path / "development"
    test_root = tmp_path / "test"
    limits = {
        "max_runtime_seconds": 1200,
        "max_tool_calls": 120,
        "max_input_tokens": 600_000,
        "max_output_tokens": 100_000,
        "max_supervisor_decisions": 24,
        "max_supervisor_api_calls": 40,
        "max_worker_model_calls": 120,
    }
    engine_limits = {
        "max_nodes": 64,
        "max_concurrent_nodes": 4,
        "max_supervisor_calls": 24,
        "max_tool_calls": 120,
        "max_input_tokens": 600_000,
        "max_output_tokens": 100_000,
        "max_runtime_seconds": 1200,
        "max_replans": 1,
        "max_retries_per_node": 1,
        "max_no_progress_decisions": 2,
    }
    development_results = [
        _write_audit_result(
            development_root,
            task.task_id,
            succeeded=index < 4,
            limits=limits,
            engine_limits=engine_limits,
            source_digest=tree_digest(task.repository_path),
        )
        for index, task in enumerate(suite.development_tasks)
    ]
    test_results = [
        _write_audit_result(
            test_root,
            task.task_id,
            succeeded=True,
            limits=limits,
            engine_limits=engine_limits,
            source_digest=tree_digest(task.repository_path),
        )
        for task in suite.test_tasks
    ]
    development_summary = aggregate_closed_loop_results(
        "closed_loop_dynamic",
        development_results,
    )
    test_summary = aggregate_closed_loop_results(
        "closed_loop_dynamic",
        test_results,
    )
    identity = {
        "repository_commit": _git_commit(repo_root),
        "repository_fingerprint": _repository_fingerprint(repo_root),
        "config_sha256": {"closed_loop": "config-sha"},
        "supervisor_prompt_sha256": "prompt-sha",
        "model_routing": {"model_order": ["max"]},
    }
    preflight_path = tmp_path / "pre_freeze.json"
    preflight_payload = {
        "passed": True,
        "repository_fingerprint": identity["repository_fingerprint"],
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
    save_json(preflight_path, preflight_payload)
    development_ids = [task.task_id for task in suite.development_tasks]
    test_ids = [task.task_id for task in suite.test_tasks]
    common_manifest = {
        "phase": 6,
        "protocol_version": "closed_loop_v1",
        "baseline_protocol": "phase6_v1",
        "system_id": "closed_loop_dynamic",
        "suite_id": suite.suite_id,
        "dataset": suite.dataset,
        "upstream_commit": suite.upstream_commit,
        "seed": suite.seed,
        "development_task_ids": development_ids,
        "frozen_test_task_ids": test_ids,
        "execution_mode": "runtime",
        "final_evaluation_executed": True,
        "hybrid_limits": limits,
        "effective_engine_limits": engine_limits,
        "pricing": {"models": {}},
        **identity,
    }
    development_manifest = {
        **common_manifest,
        "run_id": "development",
        "evaluation_split": "development",
        "selected_task_ids": development_ids,
        "pre_freeze_verification": {
            **preflight_payload,
            "record_path": str(preflight_path.resolve()),
            "record_sha256": hashlib.sha256(
                preflight_path.read_bytes()
            ).hexdigest(),
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
    }
    development_root.mkdir(exist_ok=True)
    save_json(development_root / "manifest.json", development_manifest)
    save_json(
        development_root / "acceptance.json",
        {"passed": True, "full_development_run": True},
    )
    save_json(development_root / "summary.json", development_summary)
    save_json(
        development_root / "task_results.json",
        {"tasks": development_results},
    )
    freeze_gates = {
        "freeze_phase_is_6": True,
        "freeze_is_full_development": True,
        "freeze_repository_fingerprint_matches": True,
        "freeze_repository_commit_matches": True,
        "freeze_runtime_tree_clean": True,
        "freeze_config_sha256_matches": True,
        "freeze_supervisor_prompt_matches": True,
        "freeze_model_routing_matches": True,
        "freeze_preflight_verification_passed": True,
    }
    test_manifest = {
        **common_manifest,
        "run_id": "test",
        "evaluation_split": "test",
        "selected_task_ids": test_ids,
        "freeze_verification": {
            "development_manifest": str(
                (development_root / "manifest.json").resolve()
            ),
            "development_manifest_sha256": hashlib.sha256(
                (development_root / "manifest.json").read_bytes()
            ).hexdigest(),
            "development_acceptance": str(
                (development_root / "acceptance.json").resolve()
            ),
            "development_acceptance_sha256": hashlib.sha256(
                (development_root / "acceptance.json").read_bytes()
            ).hexdigest(),
            "gates": freeze_gates,
        },
    }
    save_json(test_root / "manifest.json", test_manifest)
    save_json(
        test_root / "acceptance.json",
        {"passed": True, "full_run": True},
    )
    save_json(test_root / "summary.json", test_summary)
    save_json(test_root / "task_results.json", {"tasks": test_results})

    audit = audit_closed_loop_runs(
        development_root,
        test_root,
        tmp_path / "audit.json",
        repo_root,
        suite_path,
    )

    failed_gates = [
        name for name, passed in audit["gates"].items() if not passed
    ]
    assert audit["passed"] is True, failed_gates
    assert not failed_gates

    development_results[0]["usage"]["input_tokens"] = 99
    save_json(
        Path(development_results[0]["result_path"]),
        development_results[0],
    )
    save_json(
        development_root / "task_results.json",
        {"tasks": development_results},
    )
    save_json(
        development_root / "summary.json",
        aggregate_closed_loop_results(
            "closed_loop_dynamic",
            development_results,
        ),
    )
    tampered = audit_closed_loop_runs(
        development_root,
        test_root,
        tmp_path / "tampered_audit.json",
        repo_root,
        suite_path,
    )

    assert tampered["gates"]["per_task_result_files_match"] is True
    assert tampered["gates"]["saved_summaries_recompute_exactly"] is True
    assert tampered["gates"]["raw_usage_evidence_valid"] is False
    assert tampered["passed"] is False


def test_uniform_framework_failure_requires_every_task_to_fail_in_framework() -> None:
    results = [
        {"termination": {"failure_class": "budget_exhausted"}},
        {"termination": {"failure_class": "runner_error"}},
    ]

    assert _uniform_framework_failure(results) is True
    results[1]["termination"]["failure_class"] = "target_test_failure"
    assert _uniform_framework_failure(results) is False


def test_success_requires_all_deterministic_validation_checks() -> None:
    result = {
        "status": "succeeded",
        "validation": {
            "patch_applied": True,
            "target_test_passed": True,
            "regression_passed": True,
            "syntax_valid": True,
            "protected_path_violations": 0,
        },
    }

    assert _successful_results_valid([result]) is True
    result["validation"]["regression_passed"] = False
    assert _successful_results_valid([result]) is False
