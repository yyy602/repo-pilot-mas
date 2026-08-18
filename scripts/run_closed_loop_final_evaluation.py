"""Run the final closed-loop evaluation on the frozen QuixBugs split."""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repo_pilot_mas.agents import (
    DYNAMIC_SUPERVISOR_PROMPT,
    SupervisorAgent,
    WorkerPool,
)
from repo_pilot_mas.config import load_yaml
from repo_pilot_mas.evaluation import (
    budget_outcomes_fail_closed,
    hybrid_mechanism_metrics,
    load_evaluation_suite,
    raw_usage,
    read_trace,
    trace_metrics,
    tree_digest,
)
from repo_pilot_mas.evaluation_budget import (
    effective_engine_budget,
    effective_engine_limit_view,
)
from repo_pilot_mas.final_evaluation import (
    aggregate_closed_loop_results,
    build_artifact_ref,
    closed_loop_task_mechanism,
    save_json,
    write_closed_loop_final_report,
    write_closed_loop_results_csv,
)
from repo_pilot_mas.models import (
    create_supervisor_model_adapter,
    create_worker_model_pool,
)
from repo_pilot_mas.orchestration import (
    AsyncLangGraphRuntime,
    EngineBudget,
    EngineStatus,
    OrchestrationEngine,
    ReactBudget,
    restore_engine_from_runtime_state,
)
from repo_pilot_mas.runtime import TraceWriter
from repo_pilot_mas.schemas import TaskSpec

_SYSTEM_ID = "closed_loop_dynamic"
_PHASE = 6
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
_RUNTIME_STATUS_PATHS = (
    "pyproject.toml",
    "configs",
    "data",
    "scripts",
    "src",
    "tests",
)
_REQUIRED_PREFLIGHT_CHECKS = {
    "pytest": "pytest",
    "ruff": "ruff check",
    "compileall": "compileall",
    "diff": "git diff --check",
    "runtime_tree": "git status --short",
}


def _mapping(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    selected = value.get(key, {})
    if not isinstance(selected, Mapping):
        raise TypeError(f"配置段必须是 mapping：{key}")
    return dict(selected)


def _git_commit(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


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


def _runtime_tree_is_clean(root: Path) -> bool:
    """Require every executable evaluation input to come from the recorded HEAD."""

    completed = subprocess.run(
        [
            "git",
            "status",
            "--short",
            "--untracked-files=all",
            "--",
            *_RUNTIME_STATUS_PATHS,
        ],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )
    return not completed.stdout.strip()


def _file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _load_json_mapping(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError(f"JSON 根节点必须是 mapping：{path}")
    return dict(value)


def pre_freeze_record_gates(
    record: Mapping[str, Any],
    repository_fingerprint: str,
    *,
    runtime_tree_clean: bool = False,
) -> dict[str, bool]:
    """Validate the code checks required before a complete Development run."""

    checks = record.get("checks", ())
    valid_checks = (
        checks
        if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes))
        else ()
    )
    successful_checks = [
        item
        for item in valid_checks
        if isinstance(item, Mapping) and item.get("exit_code") == 0
    ]
    successful_commands = [str(item.get("command", "")) for item in successful_checks]
    recorded_runtime_clean = any(
        _REQUIRED_PREFLIGHT_CHECKS["runtime_tree"]
        in str(item.get("command", ""))
        and item.get("result") == "clean"
        for item in successful_checks
    )
    return {
        "preflight_declared_passed": record.get("passed") is True,
        "preflight_repository_fingerprint_matches": (
            record.get("repository_fingerprint") == repository_fingerprint
        ),
        **{
            f"preflight_{name}_passed": any(
                marker in command for command in successful_commands
            )
            for name, marker in _REQUIRED_PREFLIGHT_CHECKS.items()
            if name != "runtime_tree"
        },
        "preflight_runtime_tree_clean": (
            runtime_tree_clean and recorded_runtime_clean
        ),
    }


def verify_frozen_identity(
    development_manifest: Mapping[str, Any],
    development_acceptance: Mapping[str, Any],
    current_identity: Mapping[str, Any],
) -> dict[str, bool]:
    """Verify that a full Test run uses the accepted Development identity."""

    preflight = development_manifest.get("pre_freeze_verification", {})
    preflight_gates = (
        preflight.get("verification_gates", {})
        if isinstance(preflight, Mapping)
        else {}
    )
    preflight_passed = bool(
        isinstance(preflight, Mapping)
        and preflight.get("passed") is True
        and preflight.get("repository_fingerprint")
        == development_manifest.get("repository_fingerprint")
        and isinstance(preflight_gates, Mapping)
        and set(preflight_gates)
        == set(pre_freeze_record_gates({}, "", runtime_tree_clean=False))
        and all(preflight_gates.values())
    )

    return {
        "freeze_phase_is_6": int(development_manifest.get("phase", -1)) == _PHASE,
        "freeze_is_full_development": (
            development_manifest.get("evaluation_split") == "development"
            and development_manifest.get("execution_mode") == "runtime"
            and development_manifest.get("final_evaluation_executed") is True
            and development_acceptance.get("passed") is True
            and development_acceptance.get("full_development_run") is True
        ),
        "freeze_repository_fingerprint_matches": (
            development_manifest.get("repository_fingerprint")
            == current_identity.get("repository_fingerprint")
        ),
        "freeze_repository_commit_matches": (
            development_manifest.get("repository_commit")
            == current_identity.get("repository_commit")
        ),
        "freeze_runtime_tree_clean": (
            current_identity.get("runtime_tree_clean") is True
        ),
        "freeze_config_sha256_matches": (
            development_manifest.get("config_sha256")
            == current_identity.get("config_sha256")
        ),
        "freeze_supervisor_prompt_matches": (
            development_manifest.get("supervisor_prompt_sha256")
            == current_identity.get("supervisor_prompt_sha256")
        ),
        "freeze_model_routing_matches": (
            development_manifest.get("model_routing")
            == current_identity.get("model_routing")
        ),
        "freeze_preflight_verification_passed": preflight_passed,
    }


def _within_budget(
    usage: Mapping[str, Any],
    limits: Mapping[str, Any],
) -> bool:
    return bool(
        int(usage.get("duration_ms", 0))
        <= int(float(limits["max_runtime_seconds"]) * 1000)
        and int(usage.get("tool_calls", 0)) <= int(limits["max_tool_calls"])
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


def development_acceptance_gates(
    results: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    limits: Mapping[str, Any],
    *,
    expected_task_count: int,
) -> dict[str, bool]:
    """Return the frozen pre-Test gates for one complete Development run."""

    return {
        "development_solved_at_least_4_of_5": (
            expected_task_count == 5
            and len(results) == expected_task_count
            and int(summary.get("solved", 0)) >= 4
        ),
        "framework_runner_errors_zero": all(
            not str(item.get("reason", "")).startswith("RUNNER_ERROR:")
            for item in results
        ),
        "framework_terminal_errors_zero": all(
            str(item.get("termination", {}).get("failure_class", ""))
            not in _FRAMEWORK_FAILURE_CLASSES
            for item in results
        ),
        "reviewer_input_contract_errors_zero": (
            int(summary.get("contract_rejection_count", 0)) == 0
        ),
        "budget_accounting_contradictions_zero": all(
            bool(item.get("budget", {}).get("within_budget", False))
            == _within_budget(item.get("usage", {}), limits)
            for item in results
        ),
        "supervisor_snapshots_compacted": (
            int(summary.get("supervisor_snapshot_count", 0)) > 0
            and float(
                summary.get("supervisor_snapshot_reduction_ratio", 0.0)
                or 0.0
            )
            >= 0.2
        ),
        "supervisor_routes_traced": all(
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
        ),
        "terminal_reasons_structured": all(
            all(
                str(item.get("termination", {}).get(field, "")).strip()
                for field in ("code", "stage", "failure_class")
            )
            for item in results
        ),
    }


def frozen_test_acceptance_gates(
    results: Sequence[Mapping[str, Any]],
    *,
    expected_task_count: int,
) -> dict[str, bool]:
    """Return gates specific to one complete, immutable Frozen Test run."""

    failure_classes = [
        str(item.get("termination", {}).get("failure_class", ""))
        for item in results
    ]
    return {
        "frozen_test_result_count_complete": (
            expected_task_count > 0
            and len(results) == expected_task_count
        ),
        "frozen_test_runner_errors_zero": all(
            failure_class != "runner_error"
            for failure_class in failure_classes
        ),
        "frozen_test_not_uniform_framework_failure": not (
            failure_classes
            and all(
                failure_class in _FRAMEWORK_FAILURE_CLASSES
                for failure_class in failure_classes
            )
        ),
        "frozen_test_supervisor_routes_traced": all(
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
        ),
        "frozen_test_terminal_reasons_structured": all(
            all(
                str(item.get("termination", {}).get(field, "")).strip()
                for field in ("code", "stage", "failure_class")
            )
            for item in results
        ),
    }


def _terminal_failure_class(
    *,
    engine_status: str,
    termination_code: str,
    artifacts: Sequence[Mapping[str, Any]],
) -> str:
    if engine_status == EngineStatus.SUCCEEDED.value:
        return "none"
    if termination_code.startswith("RUNNER_ERROR"):
        return "runner_error"
    if (
        termination_code.endswith("BUDGET_EXHAUSTED")
        or termination_code == "BUDGET_VIOLATION"
    ):
        return "budget_exhausted"
    if termination_code in {"SOURCE_REPOSITORY_MUTATED", "WORKSPACE_LEAK"}:
        return "evaluation_integrity_failure"
    if termination_code == "SUPERVISOR_ROUTES_EXHAUSTED":
        return "provider_quota_exhausted"
    if termination_code == "SUPERVISOR_ROUTES_UNAVAILABLE":
        return "provider_unavailable"
    for item in reversed(artifacts):
        if item.get("artifact_type") not in {
            "validation_result",
            "replan_record",
        }:
            continue
        content = item.get("content", {})
        failure_class = (
            str(content.get("failure_class", ""))
            if isinstance(content, Mapping)
            else ""
        )
        if failure_class and failure_class != "none":
            return failure_class
    if termination_code == "SUPERVISOR_TERMINATED":
        return "task_terminated"
    return "orchestration_failure"


def _formal_evaluation_executed(
    *,
    full_run: bool,
    full_development_run: bool,
) -> bool:
    """Return whether a complete Development or Frozen Test really ran."""

    return bool(full_run or full_development_run)


def _validation_metrics(
    artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    patches = [
        item
        for item in artifacts
        if item.get("artifact_type") == "patch_candidate"
    ]
    validations = [
        item
        for item in artifacts
        if item.get("artifact_type") == "validation_result"
    ]

    def command_passed(item: Mapping[str, Any], label: str) -> bool:
        content = item.get("content", {})
        value = content.get(label, {}) if isinstance(content, Mapping) else {}
        return isinstance(value, Mapping) and value.get("exit_code") == 0

    return {
        "patch_applied": any(
            bool(item.get("content", {}).get("applied"))
            for item in validations
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
            bool(item.get("content", {}).get("passed"))
            for item in validations
        ),
        "changed_files": sorted(
            {
                str(path)
                for item in validations
                for path in item.get("content", {}).get("changed_files", ())
            }
        ),
    }


def _usage_metrics(
    task_root: Path,
    events: Sequence[Mapping[str, Any]],
    pricing: Mapping[str, Any],
    *,
    duration_ms: int,
    supervisor_policy_calls: int,
) -> dict[str, Any]:
    supervisor_usage = raw_usage(
        task_root / "raw_model_responses" / "supervisor",
        pricing,
    )
    worker_usage = raw_usage(
        task_root / "raw_model_responses" / "workers"
    )
    supervisor_attempts = sum(
        event.get("event_type") == "supervisor_route_attempt"
        for event in events
    )
    return {
        "model_calls": supervisor_attempts + worker_usage["calls"],
        "supervisor_api_calls": supervisor_attempts,
        "supervisor_policy_calls": supervisor_policy_calls,
        "worker_model_calls": worker_usage["calls"],
        "tool_calls": sum(
            event.get("event_type") == "tool_call" for event in events
        ),
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
        "duration_ms": duration_ms,
        "estimated_api_cost_cny": supervisor_usage["estimated_cost_cny"],
        "by_model": {
            **worker_usage["by_model"],
            **supervisor_usage["by_model"],
        },
    }


def _workspace_clean(task_root: Path, task_id: str) -> bool:
    task_workspace_root = task_root / "workspaces" / task_id
    if not task_workspace_root.exists():
        return True
    return task_workspace_root.is_dir() and not any(
        task_workspace_root.iterdir()
    )


def _successful_result(
    task: TaskSpec,
    restored: Any,
    *,
    task_root: Path,
    trace_path: Path,
    duration_ms: int,
    source_digest_before: str,
    source_digest_after: str,
    workspace_clean: bool,
    limits: Mapping[str, Any],
    pricing: Mapping[str, Any],
    expected_complexity: str,
    seed: int,
) -> dict[str, Any]:
    artifacts = [
        item.to_dict() for item in restored.blackboard.artifacts.values()
    ]
    nodes = [item.to_dict() for item in restored.graph.nodes]
    metric_trace = TraceWriter(trace_path)
    for item in artifacts:
        metric_trace.write(
            "metric_artifact_ref_built",
            {
                "artifact_id": item["artifact_id"],
                "version": item["version"],
                "artifact_ref": build_artifact_ref(item),
                "artifact_type": item["artifact_type"],
            },
        )
    events = read_trace(trace_path)
    traced = trace_metrics(events)
    usage = _usage_metrics(
        task_root,
        events,
        pricing,
        duration_ms=duration_ms,
        supervisor_policy_calls=restored.budget.supervisor_calls,
    )
    validation = _validation_metrics(artifacts)
    mechanism = hybrid_mechanism_metrics(
        nodes,
        artifacts,
        succeeded=restored.status is EngineStatus.SUCCEEDED,
        expected_complexity=expected_complexity,
    )
    mechanism.update(
        parallel_overlap_pairs=traced["parallel_overlap_pairs"],
        invalid_decisions=traced["invalid_decisions"],
        gate_records=traced["gate_records"],
        route_usage=traced["route_usage"],
    )
    resolution = restored.blackboard.hypothesis_resolution.to_dict()
    closed_loop = closed_loop_task_mechanism(
        nodes,
        artifacts,
        resolution,
        engine_succeeded=restored.status is EngineStatus.SUCCEEDED,
        workspace_clean=workspace_clean,
        recovery_metrics=traced,
    )
    within_budget = _within_budget(usage, limits)
    source_unchanged = source_digest_before == source_digest_after
    succeeded = bool(
        restored.status is EngineStatus.SUCCEEDED
        and within_budget
        and source_unchanged
        and workspace_clean
    )
    reason = restored.termination_reason or restored.status.value
    termination_code = restored.termination_code or "UNKNOWN_TERMINATION"
    termination_stage = restored.termination_stage or restored.blackboard.workflow_stage
    if not source_unchanged:
        reason = "SOURCE_REPOSITORY_MUTATED"
        termination_code = reason
    elif not workspace_clean:
        reason = "WORKSPACE_LEAK"
        termination_code = reason
    elif not within_budget:
        reason = "BUDGET_VIOLATION"
        termination_code = reason
    termination = {
        "code": termination_code,
        "stage": termination_stage,
        "message": reason,
        "failure_class": _terminal_failure_class(
            engine_status=restored.status.value,
            termination_code=termination_code,
            artifacts=artifacts,
        ),
    }

    result_path = task_root / "result.json"
    result = {
        "schema_version": 1,
        "protocol_version": "closed_loop_v1",
        "system_id": _SYSTEM_ID,
        "task_id": task.task_id,
        "seed": seed,
        "status": "succeeded" if succeeded else "failed",
        "reason": reason,
        "termination": termination,
        "engine_status": restored.status.value,
        "workflow_stage": restored.blackboard.workflow_stage,
        "hypothesis_resolution": resolution,
        "selected_hypothesis_ref": (
            restored.blackboard.selected_hypothesis_ref
        ),
        "selected_patch_ref": restored.blackboard.selected_patch_ref,
        "validation_ref": restored.blackboard.validation_ref,
        "validation": validation,
        "closed_loop": closed_loop,
        "usage": usage,
        "budget": {
            "limits": dict(limits),
            "effective_engine_limits": effective_engine_limit_view(
                restored.budget.to_dict()
            ),
            "within_budget": within_budget,
        },
        "mechanism": mechanism,
        "nodes": nodes,
        "artifacts": artifacts,
        "source_integrity": {
            "before_sha256": source_digest_before,
            "after_sha256": source_digest_after,
            "unchanged": source_unchanged,
        },
        "trace_path": str(trace_path),
        "checkpoint_path": str(task_root / "langgraph.sqlite3"),
        "result_path": str(result_path),
    }
    save_json(result_path, result)
    return result


def _error_result(
    task: TaskSpec,
    *,
    task_root: Path,
    trace_path: Path,
    exc: Exception,
    duration_ms: int,
    source_digest_before: str,
    source_digest_after: str,
    workspace_clean: bool,
    limits: Mapping[str, Any],
    pricing: Mapping[str, Any],
    seed: int,
) -> dict[str, Any]:
    events = read_trace(trace_path)
    usage = _usage_metrics(
        task_root,
        events,
        pricing,
        duration_ms=duration_ms,
        supervisor_policy_calls=0,
    )
    source_unchanged = source_digest_before == source_digest_after
    closed_loop = closed_loop_task_mechanism(
        (),
        (),
        {},
        engine_succeeded=False,
        workspace_clean=workspace_clean,
    )
    result_path = task_root / "result.json"
    result = {
        "schema_version": 1,
        "protocol_version": "closed_loop_v1",
        "system_id": _SYSTEM_ID,
        "task_id": task.task_id,
        "seed": seed,
        "status": "failed",
        "reason": f"RUNNER_ERROR:{type(exc).__name__}:{exc}",
        "termination": {
            "code": "RUNNER_ERROR",
            "stage": "runner",
            "message": f"{type(exc).__name__}:{exc}",
            "failure_class": "runner_error",
        },
        "validation": {
            "patch_applied": False,
            "target_test_passed": False,
            "regression_passed": False,
            "syntax_valid": False,
            "protected_path_violations": 0,
            "validation_count": 0,
            "passed_validation_count": 0,
            "changed_files": [],
        },
        "closed_loop": closed_loop,
        "usage": usage,
        "budget": {
            "limits": dict(limits),
            "within_budget": False,
        },
        "mechanism": {
            "execution_path": "error",
            "invalid_decisions": sum(
                event.get("event_type") == "supervisor_decision_rejected"
                for event in events
            ),
        },
        "source_integrity": {
            "before_sha256": source_digest_before,
            "after_sha256": source_digest_after,
            "unchanged": source_unchanged,
        },
        "trace_path": str(trace_path),
        "checkpoint_path": str(task_root / "langgraph.sqlite3"),
        "result_path": str(result_path),
    }
    save_json(result_path, result)
    return result


def _set_raw_log_dir(model: Any, path: Path) -> None:
    model.raw_log_dir = path


def _release_cuda() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        return


def _cleanup_task_workspaces(
    pool: WorkerPool,
    task: TaskSpec,
    trace: TraceWriter,
) -> None:
    try:
        pool.discard_task_workspaces(
            task=task.to_dict(),
            reason="final_evaluation_task_finalizer",
        )
    except Exception as exc:  # noqa: BLE001 - cleanup failure is reported, not hidden
        trace.write(
            "evaluation_workspace_cleanup_failed",
            {
                "error_type": type(exc).__name__,
                "message": str(exc),
            },
        )


async def _run_tasks(
    tasks: Sequence[TaskSpec],
    *,
    run_root: Path,
    model_config: Mapping[str, Any],
    supervisor_config: Path,
    runtime_config: Mapping[str, Any],
    phase6_config: Mapping[str, Any],
    limits: Mapping[str, Any],
    pricing: Mapping[str, Any],
    annotations: Mapping[str, Mapping[str, Any]],
    seed: int,
    require_human_approval: bool,
    fail_fast: bool,
) -> list[dict[str, Any]]:
    worker_models, worker_generation = create_worker_model_pool(
        model_config,
        raw_log_dir=run_root / "initial_raw_model_responses" / "workers",
    )
    preparing_pool = WorkerPool(
        worker_models,
        workspace_root=run_root / "initial_workspaces",
    )
    preparing_pool.prepare()
    router, supervisor_generation = create_supervisor_model_adapter(
        supervisor_config,
        raw_log_dir=run_root / "initial_raw_model_responses" / "supervisor",
        shared_worker_models=worker_models,
    )
    supervisor_values = _mapping(load_yaml(supervisor_config), "supervisor")
    supervisor_recovery = _mapping(supervisor_values, "recovery")
    orchestration = _mapping(runtime_config, "orchestration")
    langgraph = _mapping(runtime_config, "langgraph")
    react = _mapping(phase6_config, "worker_react")
    budget_values = effective_engine_budget(orchestration, limits)
    engine_limits = effective_engine_limit_view(budget_values)
    results: list[dict[str, Any]] = []

    try:
        for index, task in enumerate(tasks, 1):
            print(
                f"[ClosedLoop] {index}/{len(tasks)} {task.task_id}",
                flush=True,
            )
            task_root = run_root / "tasks" / task.task_id
            task_root.mkdir(parents=True, exist_ok=False)
            trace_path = task_root / "trace.jsonl"
            trace = TraceWriter(trace_path)
            trace.write(
                "effective_budget_loaded",
                {
                    "evaluation_limits": dict(limits),
                    "engine_limits": engine_limits,
                },
            )
            shared_supervisor_model = any(router is model for model in worker_models)
            if not shared_supervisor_model:
                router.raw_log_dir = (
                    task_root / "raw_model_responses" / "supervisor"
                )
            if hasattr(router, "trace_writer"):
                router.trace_writer = trace
            for slot, model in enumerate(worker_models):
                _set_raw_log_dir(
                    model,
                    task_root
                    / "raw_model_responses"
                    / "workers"
                    / f"slot-{slot}",
                )

            supervisor = SupervisorAgent(
                router,
                generation_config=supervisor_generation,
                trace_writer=trace,
                system_prompt=DYNAMIC_SUPERVISOR_PROMPT,
                additional_schema_retries=int(
                    supervisor_recovery.get("additional_schema_retries", 1)
                ),
                safe_fallback_enabled=bool(
                    supervisor_recovery.get("safe_fallback_enabled", True)
                ),
            )
            pool = WorkerPool(
                worker_models,
                workspace_root=task_root / "workspaces",
                trace_writer=trace,
                react_budget=ReactBudget(**react),
                generation_config=worker_generation,
            )
            engine = OrchestrationEngine(
                task,
                budget=EngineBudget(**budget_values),
                trace_writer=trace,
            )
            thread_id = f"closed-loop-{task.task_id}-{seed}"
            before = tree_digest(task.repository_path)
            started = time.perf_counter()
            restored: Any | None = None
            failure: Exception | None = None

            try:
                runtime = await AsyncLangGraphRuntime.create(
                    supervisor,
                    pool,
                    task_root / "langgraph.sqlite3",
                    trace_writer=trace,
                    recursion_limit=int(
                        langgraph.get("recursion_limit", 128)
                    ),
                )
                async with runtime:
                    state = await runtime.arun(
                        engine,
                        thread_id=thread_id,
                        require_human_approval=require_human_approval,
                    )
                restored = restore_engine_from_runtime_state(state)
            except Exception as exc:  # noqa: BLE001 - preserve each failed task
                failure = exc
                trace.write(
                    "evaluation_task_failed",
                    {
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
            finally:
                _cleanup_task_workspaces(pool, task, trace)

            duration_ms = int((time.perf_counter() - started) * 1000)
            after = tree_digest(task.repository_path)
            workspace_is_clean = _workspace_clean(task_root, task.task_id)
            if restored is not None:
                result = _successful_result(
                    task,
                    restored,
                    task_root=task_root,
                    trace_path=trace_path,
                    duration_ms=duration_ms,
                    source_digest_before=before,
                    source_digest_after=after,
                    workspace_clean=workspace_is_clean,
                    limits=limits,
                    pricing=pricing,
                    expected_complexity=str(
                        annotations.get(task.task_id, {}).get(
                            "expected_complexity",
                            "unknown",
                        )
                    ),
                    seed=seed,
                )
            else:
                assert failure is not None
                result = _error_result(
                    task,
                    task_root=task_root,
                    trace_path=trace_path,
                    exc=failure,
                    duration_ms=duration_ms,
                    source_digest_before=before,
                    source_digest_after=after,
                    workspace_clean=workspace_is_clean,
                    limits=limits,
                    pricing=pricing,
                    seed=seed,
                )
            results.append(result)
            _release_cuda()
            if failure is not None and fail_fast:
                raise failure
    finally:
        for model in worker_models:
            model.close()
        if not any(router is model for model in worker_models):
            close_router = getattr(router, "close", None)
            if callable(close_router):
                close_router()
        del preparing_pool, worker_models, router
        _release_cuda()

    return results


def _write_failures(
    run_root: Path,
    results: Sequence[Mapping[str, Any]],
) -> None:
    failure_root = run_root / "failures"
    failure_root.mkdir(parents=True, exist_ok=True)
    for item in results:
        if item.get("status") == "succeeded":
            continue
        save_json(failure_root / f"{item['task_id']}.json", dict(item))


def _dry_run(
    root: Path,
    tasks: Sequence[TaskSpec],
) -> None:
    save_json(
        root / "pending_tasks.json",
        {
            "tasks": [
                {
                    "task_id": task.task_id,
                    "status": "pending",
                    "repository_path": str(task.repository_path),
                }
                for task in tasks
            ]
        },
    )


def _validate_supervisor_execution_tier(
    supervisor: Mapping[str, Any],
    *,
    split: str,
    task_id: str | None,
    dry_run: bool,
) -> None:
    """Keep local Supervisor runs diagnostic and formal runs API-backed."""

    provider = str(supervisor.get("provider", "")).strip()
    complete_runtime_run = task_id is None and not dry_run
    if (
        complete_runtime_run
        and split in {"development", "test"}
        and provider != "dashscope_openai_compatible"
    ):
        raise ValueError(
            "完整 Development/Frozen Test 必须使用正式 DashScope Supervisor；"
            "本地 Supervisor 只允许单任务调试或 dry-run"
        )


async def run(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    repo_root = Path.cwd().resolve()
    config = load_yaml(args.config)
    protocol = _mapping(config, "protocol")
    reports = _mapping(config, "reports")
    execution = _mapping(config, "execution")
    suite_path = args.suite or str(protocol["suite"])
    suite = load_evaluation_suite(suite_path)
    expected_test_count = int(protocol["frozen_test_task_count"])
    if len(suite.test_tasks) != expected_test_count:
        raise ValueError(
            "冻结测试集规模不一致："
            f"expected={expected_test_count}, actual={len(suite.test_tasks)}"
        )
    if suite.seed != int(protocol["seed"]):
        raise ValueError("评测套件 seed 与闭环协议不一致")

    selected_tasks = list(
        suite.test_tasks
        if args.split == "test"
        else suite.development_tasks
    )
    if args.task_id:
        selected_tasks = [
            task for task in selected_tasks if task.task_id == args.task_id
        ]
        if not selected_tasks:
            raise ValueError(
                f"task_id 不属于 {args.split} 集：{args.task_id}"
            )

    run_id = args.run_id or datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    report_root = Path(
        args.report_root or str(reports["root"])
    ).expanduser().resolve(strict=False)
    run_root = report_root / run_id

    shared_phase6_path = str(config["shared_phase6_config"])
    phase6_config = load_yaml(shared_phase6_path)
    model_config = load_yaml(args.model_config)
    supervisor_config = load_yaml(args.supervisor_config)
    runtime_config = load_yaml(args.runtime_config)
    supervisor_values = _mapping(supervisor_config, "supervisor")
    _validate_supervisor_execution_tier(
        supervisor_values,
        split=args.split,
        task_id=args.task_id,
        dry_run=bool(args.dry_run),
    )
    limits = _mapping(phase6_config, "hybrid_limits")
    effective_budget_values = effective_engine_budget(
        _mapping(runtime_config, "orchestration"),
        limits,
    )
    pricing_config = _mapping(phase6_config, "pricing")
    pricing = _mapping(pricing_config, "models")
    require_human_approval = bool(
        execution.get("require_human_approval", False)
    )
    fail_fast = bool(args.fail_fast or execution.get("fail_fast", False))
    config_sha256 = {
        "closed_loop": _file_sha256(args.config),
        "phase6": _file_sha256(shared_phase6_path),
        "model": _file_sha256(args.model_config),
        "supervisor": _file_sha256(args.supervisor_config),
        "runtime": _file_sha256(args.runtime_config),
        "suite": _file_sha256(suite_path),
    }
    model_routing = _mapping(
        supervisor_values,
        "routing",
    )
    current_identity = {
        "repository_commit": _git_commit(repo_root),
        "repository_fingerprint": _repository_fingerprint(repo_root),
        "runtime_tree_clean": _runtime_tree_is_clean(repo_root),
        "config_sha256": config_sha256,
        "supervisor_prompt_sha256": _text_sha256(DYNAMIC_SUPERVISOR_PROMPT),
        "model_routing": model_routing,
    }
    pre_freeze_verification: dict[str, Any] | None = None
    if (
        args.split == "development"
        and not args.task_id
        and not args.dry_run
    ):
        if not args.verification_record:
            raise ValueError(
                "完整 Development 必须提供 --verification-record"
            )
        verification_path = Path(args.verification_record).expanduser().resolve()
        pre_freeze_verification = _load_json_mapping(verification_path)
        verification_gates = pre_freeze_record_gates(
            pre_freeze_verification,
            str(current_identity["repository_fingerprint"]),
            runtime_tree_clean=bool(current_identity["runtime_tree_clean"]),
        )
        if not all(verification_gates.values()):
            failed = [
                name for name, passed in verification_gates.items() if not passed
            ]
            raise ValueError(f"冻结前代码验收记录无效：{failed}")
        pre_freeze_verification = {
            **pre_freeze_verification,
            "record_path": str(verification_path),
            "record_sha256": _file_sha256(verification_path),
            "verification_gates": verification_gates,
        }
    freeze_verification: dict[str, Any] | None = None
    if args.split == "test" and not args.task_id and not args.dry_run:
        if not args.freeze_manifest:
            if not args.allow_unfrozen_test:
                raise ValueError(
                    "完整 Frozen Test 必须提供 --freeze-manifest；"
                    "只有显式传入 --allow-unfrozen-test 才能以探索模式直接运行"
                    "（结果标记 frozen=false，不作为冻结身份/简历正式结果）"
                )
            freeze_verification = {
                "skipped": True,
                "reason": (
                    "探索模式：未绑定已冻结的 Development，结果 frozen=false，"
                    "不作为冻结身份或正式简历数字"
                ),
            }
        else:
            freeze_path = Path(args.freeze_manifest).expanduser().resolve()
            development_manifest = _load_json_mapping(freeze_path)
            development_acceptance = _load_json_mapping(
                freeze_path.with_name("acceptance.json")
            )
            freeze_gates = verify_frozen_identity(
                development_manifest,
                development_acceptance,
                current_identity,
            )
            if not all(freeze_gates.values()):
                failed = [name for name, passed in freeze_gates.items() if not passed]
                raise ValueError(f"冻结身份校验失败：{failed}")
            freeze_verification = {
                "development_manifest": str(freeze_path),
                "development_manifest_sha256": _file_sha256(freeze_path),
                "development_acceptance": str(
                    freeze_path.with_name("acceptance.json")
                ),
                "development_acceptance_sha256": _file_sha256(
                    freeze_path.with_name("acceptance.json")
                ),
                "gates": freeze_gates,
            }

    run_root.mkdir(parents=True, exist_ok=False)

    manifest = {
        "schema_version": 1,
        "phase": _PHASE,
        "run_id": run_id,
        "protocol_version": str(protocol["version"]),
        "baseline_protocol": str(protocol["baseline_protocol"]),
        "system_id": str(protocol.get("system_id", _SYSTEM_ID)),
        "suite_id": suite.suite_id,
        "dataset": suite.dataset,
        "upstream_commit": suite.upstream_commit,
        "evaluation_split": args.split,
        "development_task_ids": list(suite.development_task_ids),
        "frozen_test_task_ids": [
            task.task_id for task in suite.test_tasks
        ],
        "selected_task_ids": [task.task_id for task in selected_tasks],
        "task_count": len(selected_tasks),
        "seed": suite.seed,
        "generation_temperature": 0.0,
        "hybrid_limits": limits,
        "effective_engine_limits": effective_engine_limit_view(
            effective_budget_values
        ),
        "pricing": pricing_config,
        "require_human_approval": require_human_approval,
        "fail_fast": fail_fast,
        "runtime_class": (
            f"{AsyncLangGraphRuntime.__module__}."
            f"{AsyncLangGraphRuntime.__name__}"
        ),
        "repository_commit": current_identity["repository_commit"],
        "repository_fingerprint": current_identity["repository_fingerprint"],
        "repository_fingerprint_scope": [
            "pyproject.toml",
            *_RUNTIME_FINGERPRINT_PREFIXES,
        ],
        "supervisor_prompt_sha256": current_identity[
            "supervisor_prompt_sha256"
        ],
        "model_routing": current_identity["model_routing"],
        "supervisor_provider": str(supervisor_values["provider"]),
        "config_sha256": current_identity["config_sha256"],
        "pre_freeze_verification": pre_freeze_verification,
        "freeze_verification": freeze_verification,
        "python": sys.executable,
        "command": list(sys.argv),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "execution_mode": "dry_run" if args.dry_run else "runtime",
        "final_evaluation_executed": False,
    }
    save_json(run_root / "manifest.json", manifest)

    if args.dry_run:
        _dry_run(run_root, selected_tasks)
        manifest.update(
            finished_at=datetime.now(timezone.utc).isoformat(),
            final_evaluation_executed=False,
        )
        save_json(run_root / "manifest.json", manifest)
        acceptance = {
            "phase": _PHASE,
            "run_id": run_id,
            "passed": True,
            "full_run": False,
            "dry_run": True,
            "manifest_path": str(run_root / "manifest.json"),
            "pending_tasks_path": str(run_root / "pending_tasks.json"),
        }
        save_json(run_root / "acceptance.json", acceptance)
        return acceptance, True

    results = await _run_tasks(
        selected_tasks,
        run_root=run_root,
        model_config=model_config,
        supervisor_config=Path(args.supervisor_config),
        runtime_config=runtime_config,
        phase6_config=phase6_config,
        limits=limits,
        pricing=pricing,
        annotations=suite.task_annotations,
        seed=suite.seed,
        require_human_approval=require_human_approval,
        fail_fast=fail_fast,
    )
    summary = aggregate_closed_loop_results(_SYSTEM_ID, results)
    save_json(run_root / "summary.json", summary)
    save_json(run_root / "task_results.json", {"tasks": results})
    write_closed_loop_results_csv(run_root / "task_results.csv", results)
    write_closed_loop_final_report(
        run_root / "final_report.md",
        summary,
        results,
    )
    _write_failures(run_root, results)

    full_run = bool(
        args.split == "test"
        and not args.task_id
        and len(selected_tasks) == expected_test_count
    )
    full_development_run = bool(
        args.split == "development"
        and not args.task_id
        and len(selected_tasks) == len(suite.development_tasks)
    )
    gates = {
        "final_runtime_entrypoint": (
            AsyncLangGraphRuntime.__module__
            == "repo_pilot_mas.orchestration.final_runtime"
        ),
        "frozen_test_set_has_expected_tasks": (
            len(suite.test_tasks) == expected_test_count
        ),
        "selected_task_count_complete": (
            len(results) == len(selected_tasks)
        ),
        "every_task_has_result": all(
            Path(item["result_path"]).is_file() for item in results
        ),
        "every_task_has_trace": all(
            Path(item["trace_path"]).is_file() for item in results
        ),
        "source_repositories_unchanged": all(
            item.get("source_integrity", {}).get("unchanged", False)
            for item in results
        ),
        "budget_outcomes_fail_closed": budget_outcomes_fail_closed(results),
        "terminal_workspaces_cleaned": all(
            item.get("closed_loop", {}).get("workspace_clean", False)
            for item in results
        ),
        "failures_are_included": (
            summary["task_count"] == len(selected_tasks)
        ),
    }
    if full_run:
        if freeze_verification and freeze_verification.get("skipped"):
            # 探索模式：不绑定冻结身份，结果 frozen=false。
            gates["frozen_identity_verified"] = True
            gates["exploratory_unfrozen_test"] = True
        else:
            gates["frozen_identity_verified"] = bool(
                freeze_verification
                and all(freeze_verification["gates"].values())
            )
        gates.update(
            frozen_test_acceptance_gates(
                results,
                expected_task_count=expected_test_count,
            )
        )
    if full_development_run:
        gates.update(
            development_acceptance_gates(
                results,
                summary,
                limits,
                expected_task_count=len(suite.development_tasks),
            )
        )
    passed = all(gates.values())
    frozen = not bool(
        freeze_verification and freeze_verification.get("skipped")
    )
    manifest.update(
        finished_at=datetime.now(timezone.utc).isoformat(),
        final_evaluation_executed=_formal_evaluation_executed(
            full_run=full_run,
            full_development_run=full_development_run,
        ),
        frozen=frozen,
    )
    save_json(run_root / "manifest.json", manifest)
    acceptance = {
        "phase": _PHASE,
        "run_id": run_id,
        "passed": passed,
        "full_run": full_run,
        "full_development_run": full_development_run,
        "dry_run": False,
        "frozen": frozen,
        "gates": gates,
        "summary": summary,
        "manifest_path": str(run_root / "manifest.json"),
        "summary_path": str(run_root / "summary.json"),
        "results_json": str(run_root / "task_results.json"),
        "results_csv": str(run_root / "task_results.csv"),
        "final_report": str(run_root / "final_report.md"),
        "failures_path": str(run_root / "failures"),
    }
    save_json(run_root / "acceptance.json", acceptance)
    save_json(
        report_root / "latest_summary.json",
        {
            "phase": _PHASE,
            "run_id": run_id,
            "passed": passed,
            "full_run": full_run,
            "split": args.split,
            "summary": summary,
            "acceptance_path": str(run_root / "acceptance.json"),
        },
    )
    return acceptance, passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="configs/closed_loop_evaluation.yaml",
    )
    parser.add_argument("--suite")
    parser.add_argument(
        "--split",
        choices=("development", "test"),
        default="test",
    )
    parser.add_argument("--task-id")
    parser.add_argument("--model-config", default="configs/model.yaml")
    parser.add_argument(
        "--supervisor-config",
        default="configs/supervisor.yaml",
    )
    parser.add_argument("--runtime-config", default="configs/runtime.yaml")
    parser.add_argument(
        "--freeze-manifest",
        help="完整 Frozen Test 对应的已通过 Development manifest.json",
    )
    parser.add_argument(
        "--allow-unfrozen-test",
        action="store_true",
        help="探索模式：不绑定已冻结的 Development，直接运行 Frozen Test；"
        "结果 frozen=false，不作为冻结身份或正式简历数字",
    )
    parser.add_argument(
        "--verification-record",
        help="完整 Development 对应的冻结前代码验收 JSON",
    )
    parser.add_argument("--report-root")
    parser.add_argument("--run-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()
    report, passed = asyncio.run(run(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.dry_run or args.split == "development" or args.task_id:
        return 0 if passed else 1
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
