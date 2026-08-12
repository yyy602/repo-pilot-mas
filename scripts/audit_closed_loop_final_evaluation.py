"""独立审计闭环 Development 与 Frozen Test 的完整证据链。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from repo_pilot_mas.config import load_yaml
from repo_pilot_mas.evaluation import (
    budget_outcomes_fail_closed,
    load_evaluation_suite,
    raw_usage,
    read_trace,
    trace_metrics,
    tree_digest,
)
from repo_pilot_mas.final_evaluation import (
    aggregate_closed_loop_results,
    closed_loop_task_mechanism,
    save_json,
)

_FRAMEWORK_FAILURE_CLASSES = frozenset(
    {
        "budget_exhausted",
        "contract_failure",
        "evaluation_integrity_failure",
        "orchestration_failure",
        "provider_quota_exhausted",
        "provider_unavailable",
        "runner_error",
    }
)
_RUNTIME_FINGERPRINT_PREFIXES = (
    "configs/",
    "data/",
    "scripts/",
    "src/",
    "tests/",
)
_REQUIRED_FREEZE_GATES = frozenset(
    {
        "freeze_phase_is_6",
        "freeze_is_full_development",
        "freeze_repository_fingerprint_matches",
        "freeze_repository_commit_matches",
        "freeze_runtime_tree_clean",
        "freeze_config_sha256_matches",
        "freeze_supervisor_prompt_matches",
        "freeze_model_routing_matches",
        "freeze_preflight_verification_passed",
    }
)
_REQUIRED_PREFLIGHT_COMMAND_MARKERS = (
    "pytest",
    "ruff check",
    "compileall",
    "git diff --check",
    "git status --short",
)
_REQUIRED_PREFLIGHT_GATE_NAMES = frozenset(
    {
        "preflight_declared_passed",
        "preflight_repository_fingerprint_matches",
        "preflight_pytest_passed",
        "preflight_ruff_passed",
        "preflight_compileall_passed",
        "preflight_diff_passed",
        "preflight_runtime_tree_clean",
    }
)


def resolve_audit_suite_path(
    config_path: Path,
    suite_override: Path | None = None,
) -> Path:
    """Resolve the same suite source used by the closed-loop runner."""

    if suite_override is not None:
        return suite_override
    config = load_yaml(config_path)
    protocol = config.get("protocol", {})
    if not isinstance(protocol, Mapping) or not protocol.get("suite"):
        raise ValueError("闭环评测配置缺少 protocol.suite")
    return Path(str(protocol["suite"]))


def _load_mapping(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError(f"JSON 顶层必须是对象：{path}")
    return dict(value)


def _load_results(path: Path) -> list[dict[str, Any]]:
    payload = _load_mapping(path)
    tasks = payload.get("tasks", ())
    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
        raise TypeError(f"tasks 必须是数组：{path}")
    return [dict(item) for item in tasks if isinstance(item, Mapping)]


def _pre_freeze_verification_valid(
    development_manifest: Mapping[str, Any],
) -> bool:
    preflight = development_manifest.get("pre_freeze_verification", {})
    if not isinstance(preflight, Mapping):
        return False
    checks = preflight.get("checks", ())
    if not isinstance(checks, Sequence) or isinstance(checks, (str, bytes)):
        return False
    successful_checks = [
        item
        for item in checks
        if isinstance(item, Mapping) and item.get("exit_code") == 0
    ]
    commands = [str(item.get("command", "")) for item in successful_checks]
    recorded_runtime_clean = any(
        "git status --short" in str(item.get("command", ""))
        and item.get("result") == "clean"
        for item in successful_checks
    )
    record_path = Path(str(preflight.get("record_path", "")))
    verification_gates = preflight.get("verification_gates", {})
    if not record_path.is_file():
        return False
    return bool(
        preflight.get("passed") is True
        and preflight.get("repository_fingerprint")
        == development_manifest.get("repository_fingerprint")
        and preflight.get("record_sha256") == _file_sha256(record_path)
        and isinstance(verification_gates, Mapping)
        and set(verification_gates) == _REQUIRED_PREFLIGHT_GATE_NAMES
        and all(verification_gates.values())
        and all(
            any(marker in command for command in commands)
            for marker in _REQUIRED_PREFLIGHT_COMMAND_MARKERS
        )
        and recorded_runtime_clean
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _freeze_verification_valid(
    test_manifest: Mapping[str, Any],
    development_root: Path,
) -> bool:
    verification = test_manifest.get("freeze_verification", {})
    if not isinstance(verification, Mapping):
        return False
    gates = verification.get("gates", {})
    if not isinstance(gates, Mapping):
        return False
    return bool(
        set(gates) == _REQUIRED_FREEZE_GATES
        and all(gates.values())
        and Path(str(verification.get("development_manifest", "")))
        == (development_root / "manifest.json").resolve()
        and verification.get("development_manifest_sha256")
        == _file_sha256(development_root / "manifest.json")
        and Path(str(verification.get("development_acceptance", "")))
        == (development_root / "acceptance.json").resolve()
        and verification.get("development_acceptance_sha256")
        == _file_sha256(development_root / "acceptance.json")
    )


def _repository_fingerprint(root: Path) -> str:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )
    digest = hashlib.sha256()
    for relative in sorted(
        line for line in completed.stdout.splitlines() if line
    ):
        path = root / relative
        if not path.is_file() or not (
            relative == "pyproject.toml"
            or relative.startswith(_RUNTIME_FINGERPRINT_PREFIXES)
        ):
            continue
        digest.update(relative.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _git_commit(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _within_recorded_budget(item: Mapping[str, Any]) -> bool:
    usage = item.get("usage", {})
    limits = item.get("budget", {}).get("limits", {})
    if not isinstance(usage, Mapping) or not isinstance(limits, Mapping):
        return False
    required = {
        "max_runtime_seconds",
        "max_tool_calls",
        "max_input_tokens",
        "max_output_tokens",
        "max_supervisor_decisions",
        "max_supervisor_api_calls",
        "max_worker_model_calls",
    }
    if not required.issubset(limits):
        return False
    return bool(
        int(usage.get("duration_ms", 0))
        <= int(float(limits["max_runtime_seconds"]) * 1000)
        and int(usage.get("tool_calls", 0))
        <= int(limits["max_tool_calls"])
        and int(usage.get("input_tokens", 0))
        <= int(limits["max_input_tokens"])
        and int(usage.get("output_tokens", 0))
        <= int(limits["max_output_tokens"])
        and int(usage.get("supervisor_api_calls", 0))
        <= int(limits["max_supervisor_api_calls"])
        and int(usage.get("supervisor_policy_calls", 0))
        <= int(limits["max_supervisor_decisions"])
        and int(usage.get("worker_model_calls", 0))
        <= int(limits["max_worker_model_calls"])
    )


def _successful_results_valid(results: Sequence[Mapping[str, Any]]) -> bool:
    return all(
        item.get("status") != "succeeded"
        or (
            item.get("validation", {}).get("patch_applied") is True
            and item.get("validation", {}).get("target_test_passed") is True
            and item.get("validation", {}).get("regression_passed") is True
            and item.get("validation", {}).get("syntax_valid") is True
            and item.get("validation", {}).get("protected_path_violations")
            == 0
        )
        for item in results
    )


def _uniform_framework_failure(results: Sequence[Mapping[str, Any]]) -> bool:
    return bool(
        results
        and all(
            str(item.get("termination", {}).get("failure_class", ""))
            in _FRAMEWORK_FAILURE_CLASSES
            for item in results
        )
    )


def _is_exact_task_evidence_path(
    path: Path,
    *,
    run_root: Path,
    task_id: str,
    filename: str,
) -> bool:
    expected = (run_root / "tasks" / task_id / filename).resolve()
    return bool(
        not path.is_symlink()
        and path.resolve() == expected
        and path.is_file()
    )


def _trace_evidence_valid(
    results: Sequence[Mapping[str, Any]],
    run_root: Path,
    manifest: Mapping[str, Any],
) -> bool:
    for item in results:
        task_id = str(item.get("task_id", ""))
        result_path = Path(str(item.get("result_path", "")))
        trace_path = Path(str(item.get("trace_path", "")))
        checkpoint_path = Path(str(item.get("checkpoint_path", "")))
        if not all(
            (
                _is_exact_task_evidence_path(
                    result_path,
                    run_root=run_root,
                    task_id=task_id,
                    filename="result.json",
                ),
                _is_exact_task_evidence_path(
                    trace_path,
                    run_root=run_root,
                    task_id=task_id,
                    filename="trace.jsonl",
                ),
                _is_exact_task_evidence_path(
                    checkpoint_path,
                    run_root=run_root,
                    task_id=task_id,
                    filename="langgraph.sqlite3",
                ),
            )
        ):
            return False
        try:
            events = read_trace(trace_path)
        except (OSError, json.JSONDecodeError):
            return False
        if not events:
            return False
        budget_events = [
            event.get("data", {})
            for event in events
            if event.get("event_type") == "effective_budget_loaded"
        ]
        if not (
            len(budget_events) == 1
            and budget_events[0].get("evaluation_limits")
            == manifest.get("hybrid_limits")
            and budget_events[0].get("engine_limits")
            == manifest.get("effective_engine_limits")
        ):
            return False
        metrics = trace_metrics(events)
        closed_loop = item.get("closed_loop", {})
        if not isinstance(closed_loop, Mapping):
            return False
        compared_fields = (
            "route_attempts_by_model",
            "route_successes_by_model",
            "route_exhausted_by_model",
            "worker_retry_count",
            "contract_rejection_count",
            "node_rebuild_count",
            "supervisor_format_repair_count",
            "supervisor_snapshot_count",
            "supervisor_snapshot_original_chars",
            "supervisor_snapshot_compact_chars",
            "supervisor_snapshot_reduction_ratio",
        )
        if any(
            metrics[field] != closed_loop.get(field, _metric_default(field))
            for field in compared_fields
        ):
            return False
    return True


def _metric_default(field: str) -> Any:
    if field.endswith("_by_model"):
        return {}
    if field == "supervisor_snapshot_reduction_ratio":
        return None
    return 0


def _saved_result_files_match(
    results: Sequence[Mapping[str, Any]],
) -> bool:
    for item in results:
        result_path = Path(str(item.get("result_path", "")))
        try:
            persisted = _load_mapping(result_path)
        except (OSError, json.JSONDecodeError, TypeError):
            return False
        if persisted != item:
            return False
    return True


def _result_identity_valid(
    results: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> bool:
    return all(
        item.get("schema_version") == 1
        and item.get("protocol_version") == manifest.get("protocol_version")
        and item.get("system_id") == manifest.get("system_id")
        and item.get("seed") == manifest.get("seed")
        and (
            (item.get("status") == "succeeded")
            == (item.get("engine_status") == "succeeded")
        )
        and (
            (item.get("status") == "succeeded")
            == (
                item.get("termination", {}).get("failure_class") == "none"
            )
        )
        for item in results
    )


def _workspace_is_clean(run_root: Path, task_id: str) -> bool:
    workspace_root = run_root / "tasks" / task_id / "workspaces" / task_id
    return bool(
        not workspace_root.exists()
        or (workspace_root.is_dir() and not any(workspace_root.iterdir()))
    )


def _source_integrity_valid(
    results: Sequence[Mapping[str, Any]],
    tasks: Sequence[Any],
) -> bool:
    task_by_id = {str(task.task_id): task for task in tasks}
    for item in results:
        task = task_by_id.get(str(item.get("task_id", "")))
        source = item.get("source_integrity", {})
        if task is None or not isinstance(source, Mapping):
            return False
        before = str(source.get("before_sha256", ""))
        after = str(source.get("after_sha256", ""))
        if not (
            source.get("unchanged") is True
            and len(before) == 64
            and before == after
            and tree_digest(task.repository_path) == after
        ):
            return False
    return True


def _validation_metrics_from_artifacts(
    artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    patches = [
        item for item in artifacts if item.get("artifact_type") == "patch_candidate"
    ]
    validations = [
        item for item in artifacts if item.get("artifact_type") == "validation_result"
    ]

    def command_passed(item: Mapping[str, Any], label: str) -> bool:
        content = item.get("content", {})
        value = content.get(label, {}) if isinstance(content, Mapping) else {}
        return isinstance(value, Mapping) and value.get("exit_code") == 0

    return {
        "patch_applied": any(
            bool(item.get("content", {}).get("applied")) for item in validations
        ),
        "target_test_passed": any(
            command_passed(item, "target_test") for item in validations
        ),
        "regression_passed": any(
            command_passed(item, "regression_test") for item in validations
        ),
        "syntax_valid": any(
            command_passed(item, "static_check") for item in validations
        ),
        "protected_path_violations": sum(
            item.get("content", {}).get("protected_path_check") is not True
            for item in validations
        )
        + sum(
            item.get("content", {}).get("protected_path_check") is not True
            for item in patches
        ),
        "validation_count": len(validations),
        "passed_validation_count": sum(
            bool(item.get("content", {}).get("passed")) for item in validations
        ),
        "changed_files": sorted(
            {
                str(path)
                for item in validations
                for path in item.get("content", {}).get("changed_files", ())
            }
        ),
    }


def _task_metrics_recompute_exactly(
    results: Sequence[Mapping[str, Any]],
    run_root: Path,
) -> bool:
    for item in results:
        task_id = str(item.get("task_id", ""))
        try:
            events = read_trace(
                run_root / "tasks" / task_id / "trace.jsonl"
            )
        except (OSError, json.JSONDecodeError):
            return False
        artifacts = item.get("artifacts", ())
        nodes = item.get("nodes", ())
        resolution = item.get("hypothesis_resolution", {})
        if (
            not isinstance(artifacts, Sequence)
            or isinstance(artifacts, (str, bytes))
            or not isinstance(nodes, Sequence)
            or isinstance(nodes, (str, bytes))
            or not isinstance(resolution, Mapping)
        ):
            return False
        workspace_clean = _workspace_is_clean(run_root, task_id)
        try:
            recomputed_closed_loop = closed_loop_task_mechanism(
                nodes,
                artifacts,
                resolution,
                engine_succeeded=item.get("engine_status") == "succeeded",
                workspace_clean=workspace_clean,
                recovery_metrics=trace_metrics(events),
            )
            recomputed_validation = _validation_metrics_from_artifacts(
                artifacts
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
        if item.get("validation") != recomputed_validation or item.get(
            "closed_loop"
        ) != recomputed_closed_loop:
            return False
    return True


def _usage_evidence_valid(
    results: Sequence[Mapping[str, Any]],
    run_root: Path,
    manifest: Mapping[str, Any],
) -> bool:
    pricing_section = manifest.get("pricing", {})
    pricing = (
        pricing_section.get("models", {})
        if isinstance(pricing_section, Mapping)
        else {}
    )
    if not isinstance(pricing, Mapping):
        return False
    for item in results:
        task_id = str(item.get("task_id", ""))
        task_root = run_root / "tasks" / task_id
        try:
            events = read_trace(task_root / "trace.jsonl")
            supervisor_usage = raw_usage(
                task_root / "raw_model_responses" / "supervisor",
                pricing,
            )
            worker_usage = raw_usage(
                task_root / "raw_model_responses" / "workers"
            )
        except (
            AttributeError,
            OSError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            return False
        traced = trace_metrics(events)
        supervisor_attempts = sum(
            event.get("event_type") == "supervisor_route_attempt"
            for event in events
        )
        expected = {
            "model_calls": supervisor_attempts + worker_usage["calls"],
            "supervisor_api_calls": supervisor_attempts,
            "supervisor_policy_calls": traced["supervisor_snapshot_count"],
            "worker_model_calls": worker_usage["calls"],
            "tool_calls": traced["tool_calls"],
            "input_tokens": (
                supervisor_usage["input_tokens"] + worker_usage["input_tokens"]
            ),
            "output_tokens": (
                supervisor_usage["output_tokens"] + worker_usage["output_tokens"]
            ),
            "total_tokens": (
                supervisor_usage["total_tokens"] + worker_usage["total_tokens"]
            ),
            "supervisor_tokens": supervisor_usage["total_tokens"],
            "worker_tokens": worker_usage["total_tokens"],
            "estimated_api_cost_cny": supervisor_usage["estimated_cost_cny"],
            "by_model": {
                **worker_usage["by_model"],
                **supervisor_usage["by_model"],
            },
        }
        usage = item.get("usage", {})
        if not isinstance(usage, Mapping) or any(
            usage.get(field) != value for field, value in expected.items()
        ):
            return False
    return True


def _routes_traced(results: Sequence[Mapping[str, Any]]) -> bool:
    return all(
        sum(
            int(count)
            for count in item.get("closed_loop", {})
            .get("route_attempts_by_model", {})
            .values()
        )
        > 0
        and sum(
            int(count)
            for count in item.get("closed_loop", {})
            .get("route_successes_by_model", {})
            .values()
        )
        > 0
        for item in results
    )


def _terminal_reasons_structured(
    results: Sequence[Mapping[str, Any]],
) -> bool:
    return all(
        all(
            str(item.get("termination", {}).get(field, "")).strip()
            for field in ("code", "stage", "failure_class")
        )
        for item in results
    )


def _development_gates_valid(
    results: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    expected_task_count: int,
) -> bool:
    return bool(
        expected_task_count == 5
        and len(results) == expected_task_count
        and int(summary.get("solved", 0)) >= 4
        and all(
            not str(item.get("reason", "")).startswith("RUNNER_ERROR:")
            for item in results
        )
        and all(
            str(item.get("termination", {}).get("failure_class", ""))
            not in _FRAMEWORK_FAILURE_CLASSES
            for item in results
        )
        and int(summary.get("contract_rejection_count", 0)) == 0
        and int(summary.get("supervisor_snapshot_count", 0)) > 0
        and float(summary.get("supervisor_snapshot_reduction_ratio", 0.0) or 0.0)
        >= 0.2
        and _routes_traced(results)
        and _terminal_reasons_structured(results)
    )


def _frozen_test_gates_valid(
    results: Sequence[Mapping[str, Any]],
    expected_task_count: int,
) -> bool:
    return bool(
        expected_task_count == 10
        and len(results) == expected_task_count
        and all(
            item.get("termination", {}).get("failure_class") != "runner_error"
            for item in results
        )
        and not _uniform_framework_failure(results)
        and _routes_traced(results)
        and _terminal_reasons_structured(results)
    )


def _budget_limits_match_manifest(
    results: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> bool:
    limits = manifest.get("hybrid_limits", {})
    engine_limits = manifest.get("effective_engine_limits", {})
    return bool(
        isinstance(limits, Mapping)
        and isinstance(engine_limits, Mapping)
        and limits
        and engine_limits
        and all(
            item.get("budget", {}).get("limits") == limits
            and item.get("budget", {}).get("effective_engine_limits")
            == engine_limits
            for item in results
        )
    )


def _contains_secret(root: Path) -> bool:
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(
            part.startswith("sk-") and len(part) >= 19
            for part in content.replace('"', " ").replace("'", " ").split()
        ):
            return True
    return False


def audit_closed_loop_runs(
    development_root: Path,
    test_root: Path,
    output_path: Path,
    repo_root: Path,
    suite_path: Path,
) -> dict[str, Any]:
    """Recompute the final protocol gates from saved, immutable artifacts."""

    suite = load_evaluation_suite(suite_path)
    development_manifest = _load_mapping(development_root / "manifest.json")
    development_acceptance = _load_mapping(
        development_root / "acceptance.json"
    )
    development_summary = _load_mapping(development_root / "summary.json")
    development_results = _load_results(
        development_root / "task_results.json"
    )
    test_manifest = _load_mapping(test_root / "manifest.json")
    test_acceptance = _load_mapping(test_root / "acceptance.json")
    test_summary = _load_mapping(test_root / "summary.json")
    test_results = _load_results(test_root / "task_results.json")
    all_results = [*development_results, *test_results]
    development_ids = [task.task_id for task in suite.development_tasks]
    test_ids = [task.task_id for task in suite.test_tasks]
    identity_fields = (
        "repository_commit",
        "repository_fingerprint",
        "config_sha256",
        "supervisor_prompt_sha256",
        "model_routing",
        "protocol_version",
        "baseline_protocol",
        "system_id",
    )
    recomputed_development = aggregate_closed_loop_results(
        "closed_loop_dynamic",
        development_results,
    )
    recomputed_test = aggregate_closed_loop_results(
        "closed_loop_dynamic",
        test_results,
    )
    gates = {
        "phase_is_6": (
            development_manifest.get("phase") == 6
            and test_manifest.get("phase") == 6
        ),
        "development_accepted": (
            development_acceptance.get("passed") is True
            and development_acceptance.get("full_development_run") is True
            and int(development_summary.get("solved", 0)) >= 4
        ),
        "development_gates_recomputed": _development_gates_valid(
            development_results,
            development_summary,
            len(development_ids),
        ),
        "frozen_test_accepted": (
            test_acceptance.get("passed") is True
            and test_acceptance.get("full_run") is True
        ),
        "frozen_test_gates_recomputed": _frozen_test_gates_valid(
            test_results,
            len(test_ids),
        ),
        "real_runs_executed": (
            development_manifest.get("evaluation_split") == "development"
            and test_manifest.get("evaluation_split") == "test"
            and development_manifest.get("execution_mode") == "runtime"
            and test_manifest.get("execution_mode") == "runtime"
            and development_manifest.get("final_evaluation_executed") is True
            and test_manifest.get("final_evaluation_executed") is True
        ),
        "pre_freeze_verification_valid": _pre_freeze_verification_valid(
            development_manifest
        ),
        "split_membership_exact_and_disjoint": (
            development_manifest.get("selected_task_ids") == development_ids
            and test_manifest.get("selected_task_ids") == test_ids
            and not set(development_ids).intersection(test_ids)
        ),
        "suite_identity_matches_manifests": all(
            manifest.get("suite_id") == suite.suite_id
            and manifest.get("dataset") == suite.dataset
            and manifest.get("upstream_commit") == suite.upstream_commit
            and manifest.get("seed") == suite.seed
            and manifest.get("development_task_ids") == development_ids
            and manifest.get("frozen_test_task_ids") == test_ids
            for manifest in (development_manifest, test_manifest)
        ),
        "all_results_retained": (
            [item.get("task_id") for item in development_results]
            == development_ids
            and [item.get("task_id") for item in test_results] == test_ids
        ),
        "per_task_result_files_match": _saved_result_files_match(all_results),
        "task_result_identity_valid": (
            _result_identity_valid(
                development_results,
                development_manifest,
            )
            and _result_identity_valid(test_results, test_manifest)
        ),
        "frozen_identity_matches": all(
            development_manifest.get(field) == test_manifest.get(field)
            for field in identity_fields
        ),
        "frozen_identity_gates_passed": _freeze_verification_valid(
            test_manifest,
            development_root,
        ),
        "current_runtime_still_matches_freeze": (
            development_manifest.get("repository_fingerprint")
            == _repository_fingerprint(repo_root)
            and development_manifest.get("repository_commit")
            == _git_commit(repo_root)
        ),
        "saved_summaries_recompute_exactly": (
            development_summary == recomputed_development
            and test_summary == recomputed_test
        ),
        "budget_accounting_consistent": all(
            item.get("budget", {}).get("within_budget")
            == _within_recorded_budget(item)
            for item in all_results
        ),
        "budget_limits_match_manifests": (
            _budget_limits_match_manifest(
                development_results,
                development_manifest,
            )
            and _budget_limits_match_manifest(test_results, test_manifest)
        ),
        "budget_outcomes_fail_closed": budget_outcomes_fail_closed(
            all_results
        ),
        "successful_results_pass_validation": _successful_results_valid(
            all_results
        ),
        "source_integrity_and_cleanup_hold": (
            _source_integrity_valid(
                development_results,
                suite.development_tasks,
            )
            and _source_integrity_valid(test_results, suite.test_tasks)
            and all(
                item.get("closed_loop", {}).get("workspace_clean") is True
                and _workspace_is_clean(root, str(item.get("task_id", "")))
                for root, results in (
                    (development_root, development_results),
                    (test_root, test_results),
                )
                for item in results
            )
        ),
        "terminal_reasons_structured": _terminal_reasons_structured(
            all_results
        ),
        "runner_errors_zero": all(
            item.get("termination", {}).get("failure_class")
            != "runner_error"
            for item in all_results
        ),
        "frozen_test_not_uniform_framework_failure": not (
            _uniform_framework_failure(test_results)
        ),
        "trace_and_checkpoint_evidence_valid": (
            _trace_evidence_valid(
                development_results,
                development_root,
                development_manifest,
            )
            and _trace_evidence_valid(
                test_results,
                test_root,
                test_manifest,
            )
        ),
        "raw_usage_evidence_valid": (
            _usage_evidence_valid(
                development_results,
                development_root,
                development_manifest,
            )
            and _usage_evidence_valid(
                test_results,
                test_root,
                test_manifest,
            )
        ),
        "task_metrics_recompute_exactly": (
            _task_metrics_recompute_exactly(
                development_results,
                development_root,
            )
            and _task_metrics_recompute_exactly(test_results, test_root)
        ),
        "raw_reports_contain_no_api_key": not (
            _contains_secret(development_root) or _contains_secret(test_root)
        ),
    }
    audit = {
        "schema_version": 1,
        "phase": 6,
        "passed": all(gates.values()),
        "development_run_id": development_manifest.get("run_id"),
        "frozen_test_run_id": test_manifest.get("run_id"),
        "gates": gates,
        "development_summary": development_summary,
        "frozen_test_summary": test_summary,
        "limitations": [
            "Frozen Test 仅含 10 个小型 Python 任务，不能外推为通用仓库修复能力。",
            "仅使用一个固定 seed，未进行重复采样或统计显著性检验。",
            "模型路由受评测时各账号实际配额状态影响。",
        ],
    }
    save_json(output_path, audit)
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="configs/closed_loop_evaluation.yaml",
    )
    parser.add_argument(
        "--suite",
        help="覆盖评测配置中的 protocol.suite（通常不需要）",
    )
    parser.add_argument("--development-root", required=True)
    parser.add_argument("--test-root", required=True)
    parser.add_argument(
        "--output",
        default="reports/closed_loop/audit/final_audit.json",
    )
    args = parser.parse_args()
    suite_path = resolve_audit_suite_path(
        Path(args.config),
        Path(args.suite) if args.suite else None,
    )
    audit = audit_closed_loop_runs(
        Path(args.development_root).resolve(),
        Path(args.test_root).resolve(),
        Path(args.output).resolve(),
        Path.cwd().resolve(),
        suite_path.resolve(),
    )
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0 if audit["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
