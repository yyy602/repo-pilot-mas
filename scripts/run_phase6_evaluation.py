"""运行 Phase 6 单任务或完整批量评测，并生成可追溯对照结果。"""

from __future__ import annotations

import argparse
import asyncio
import csv
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
    SingleAgent,
    SupervisorAgent,
    WorkerPool,
)
from repo_pilot_mas.config import load_yaml
from repo_pilot_mas.evaluation import (
    ALL_SYSTEMS,
    MAIN_SYSTEMS,
    FixedPipelineSupervisor,
    aggregate_system_results,
    budget_outcomes_fail_closed,
    constrained_dynamic_prompt,
    hybrid_mechanism_metrics,
    load_evaluation_suite,
    raw_usage,
    read_trace,
    trace_metrics,
    tree_digest,
)
from repo_pilot_mas.evaluation_budget import effective_engine_budget
from repo_pilot_mas.models import (
    create_model_adapter,
    create_supervisor_model_router,
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
from repo_pilot_mas.schemas import FinalReport, TaskSpec


def _mapping(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    selected = value.get(key, {})
    if not isinstance(selected, Mapping):
        raise TypeError(f"配置段必须是 mapping：{key}")
    return dict(selected)


def _save_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _single_validation(report: FinalReport) -> dict[str, Any]:
    values = report.validations
    diff = values.get("diff", {})
    target = values.get("target_test", {})
    regression = values.get("regression_test", {})
    static = values.get("static_check", {})
    diff_data = diff.get("data", {}) if isinstance(diff, Mapping) else {}
    violations = diff_data.get("protected_path_violations", ())
    return {
        "patch_applied": bool(report.patch_sha256 and report.changed_files),
        "target_test_passed": bool(isinstance(target, Mapping) and target.get("ok")),
        "regression_passed": bool(isinstance(regression, Mapping) and regression.get("ok")),
        "syntax_valid": bool(isinstance(static, Mapping) and static.get("ok")),
        "protected_path_violations": len(violations) if isinstance(violations, Sequence) else 0,
        "changed_files": list(report.changed_files),
        "patch_sha256": report.patch_sha256,
    }


def _within_budget(usage: Mapping[str, Any], limits: Mapping[str, Any], *, hybrid: bool) -> bool:
    if int(usage.get("duration_ms", 0)) > int(float(limits["max_runtime_seconds"]) * 1000):
        return False
    if int(usage.get("tool_calls", 0)) > int(limits["max_tool_calls"]):
        return False
    if int(usage.get("input_tokens", 0)) > int(limits["max_input_tokens"]):
        return False
    if int(usage.get("output_tokens", 0)) > int(limits["max_output_tokens"]):
        return False
    if hybrid:
        return bool(
            int(usage.get("supervisor_api_calls", 0)) <= int(limits["max_supervisor_api_calls"])
            and int(usage.get("supervisor_policy_calls", 0))
            <= int(limits["max_supervisor_decisions"])
            and int(usage.get("worker_model_calls", 0)) <= int(limits["max_worker_model_calls"])
        )
    return int(usage.get("model_calls", 0)) <= int(limits["max_model_calls"])


def _single_result(
    system_id: str,
    task: TaskSpec,
    report: FinalReport,
    *,
    task_root: Path,
    source_digest_before: str,
    source_digest_after: str,
    limits: Mapping[str, Any],
    seed: int,
) -> dict[str, Any]:
    validation = _single_validation(report)
    usage = {
        "model_calls": report.model_calls,
        "supervisor_api_calls": 0,
        "worker_model_calls": report.model_calls,
        "tool_calls": report.tool_calls,
        "input_tokens": report.input_tokens,
        "output_tokens": report.output_tokens,
        "total_tokens": report.input_tokens + report.output_tokens,
        "duration_ms": report.duration_ms,
        "estimated_api_cost_cny": 0.0,
        "by_model": {
            report.model_id: {
                "calls": report.model_calls,
                "input_tokens": report.input_tokens,
                "output_tokens": report.output_tokens,
                "estimated_cost_cny": 0.0,
            }
        },
    }
    within = _within_budget(usage, limits, hybrid=False)
    source_unchanged = source_digest_before == source_digest_after
    status = report.status if within and source_unchanged else "failed"
    reason = report.reason
    if not source_unchanged:
        reason = "SOURCE_REPOSITORY_MUTATED"
    elif not within:
        reason = "BUDGET_VIOLATION"
    result_path = task_root / "result.json"
    result = {
        "schema_version": 1,
        "system_id": system_id,
        "task_id": task.task_id,
        "seed": seed,
        "status": status,
        "reason": reason,
        "validation": validation,
        "usage": usage,
        "budget": {"limits": dict(limits), "within_budget": within},
        "mechanism": {"execution_path": "single"},
        "source_integrity": {
            "before_sha256": source_digest_before,
            "after_sha256": source_digest_after,
            "unchanged": source_unchanged,
        },
        "trace_path": report.trace_path,
        "agent_report_path": report.report_path,
        "result_path": str(result_path),
    }
    _save_json(result_path, result)
    return result


def _hybrid_validation(artifacts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    patches = [item for item in artifacts if item["artifact_type"] == "patch_candidate"]
    validations = [item for item in artifacts if item["artifact_type"] == "validation_result"]

    def passed(item: Mapping[str, Any], label: str) -> bool:
        value = item["content"].get(label, {})
        return isinstance(value, Mapping) and value.get("exit_code") == 0

    return {
        "patch_applied": any(bool(item["content"].get("applied")) for item in validations),
        "target_test_passed": any(passed(item, "target_test") for item in validations),
        "regression_passed": any(passed(item, "regression_test") for item in validations),
        "syntax_valid": any(passed(item, "static_check") for item in validations),
        "protected_path_violations": sum(
            item["content"].get("protected_path_check") is not True for item in validations
        )
        + sum(item["content"].get("protected_path_check") is not True for item in patches),
        "validation_count": len(validations),
        "passed_validation_count": sum(bool(item["content"].get("passed")) for item in validations),
        "changed_files": sorted(
            {str(path) for item in validations for path in item["content"].get("changed_files", ())}
        ),
    }


def _hybrid_result(
    system_id: str,
    task: TaskSpec,
    *,
    restored: Any,
    task_root: Path,
    trace_path: Path,
    duration_ms: int,
    source_digest_before: str,
    source_digest_after: str,
    limits: Mapping[str, Any],
    pricing: Mapping[str, Any],
    expected_complexity: str,
    seed: int,
) -> dict[str, Any]:
    artifacts = [item.to_dict() for item in restored.blackboard.artifacts.values()]
    nodes = [item.to_dict() for item in restored.graph.nodes]
    events = read_trace(trace_path)
    traced = trace_metrics(events)
    supervisor_usage = raw_usage(task_root / "raw_model_responses" / "supervisor", pricing)
    worker_usage = raw_usage(task_root / "raw_model_responses" / "workers")
    supervisor_attempts = sum(
        event.get("event_type") == "supervisor_route_attempt" for event in events
    )
    usage = {
        "model_calls": supervisor_attempts + worker_usage["calls"],
        "supervisor_api_calls": supervisor_attempts,
        "supervisor_policy_calls": restored.budget.supervisor_calls,
        "worker_model_calls": worker_usage["calls"],
        "tool_calls": traced["tool_calls"],
        "input_tokens": supervisor_usage["input_tokens"] + worker_usage["input_tokens"],
        "output_tokens": supervisor_usage["output_tokens"] + worker_usage["output_tokens"],
        "total_tokens": supervisor_usage["total_tokens"] + worker_usage["total_tokens"],
        "duration_ms": duration_ms,
        "estimated_api_cost_cny": supervisor_usage["estimated_cost_cny"],
        "by_model": {**worker_usage["by_model"], **supervisor_usage["by_model"]},
    }
    validation = _hybrid_validation(artifacts)
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
    within = _within_budget(usage, limits, hybrid=True)
    source_unchanged = source_digest_before == source_digest_after
    succeeded = restored.status is EngineStatus.SUCCEEDED and within and source_unchanged
    reason = restored.termination_reason or restored.status.value
    if not source_unchanged:
        reason = "SOURCE_REPOSITORY_MUTATED"
    elif not within:
        reason = "BUDGET_VIOLATION"
    result_path = task_root / "result.json"
    result = {
        "schema_version": 1,
        "system_id": system_id,
        "task_id": task.task_id,
        "seed": seed,
        "status": "succeeded" if succeeded else "failed",
        "reason": reason,
        "engine_status": restored.status.value,
        "workflow_stage": restored.blackboard.workflow_stage,
        "selected_hypothesis_ref": restored.blackboard.selected_hypothesis_ref,
        "selected_patch_ref": restored.blackboard.selected_patch_ref,
        "validation_ref": restored.blackboard.validation_ref,
        "validation": validation,
        "usage": usage,
        "budget": {"limits": dict(limits), "within_budget": within},
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
    _save_json(result_path, result)
    return result


def _error_result(
    system_id: str,
    task: TaskSpec,
    task_root: Path,
    *,
    exc: Exception,
    duration_ms: int,
    limits: Mapping[str, Any],
    seed: int,
) -> dict[str, Any]:
    result_path = task_root / "result.json"
    result = {
        "schema_version": 1,
        "system_id": system_id,
        "task_id": task.task_id,
        "seed": seed,
        "status": "failed",
        "reason": f"RUNNER_ERROR:{type(exc).__name__}:{exc}",
        "validation": {
            "patch_applied": False,
            "target_test_passed": False,
            "regression_passed": False,
            "syntax_valid": False,
            "protected_path_violations": 0,
        },
        "usage": {
            "model_calls": 0,
            "supervisor_api_calls": 0,
            "worker_model_calls": 0,
            "tool_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "duration_ms": duration_ms,
            "estimated_api_cost_cny": 0.0,
        },
        "budget": {"limits": dict(limits), "within_budget": False},
        "mechanism": {"execution_path": "error"},
        "trace_path": str(task_root / "trace.jsonl"),
        "result_path": str(result_path),
    }
    _save_json(result_path, result)
    return result


def _set_raw_log_dir(model: Any, path: Path) -> None:
    model.raw_log_dir = path


def _run_single_system(
    tasks: Sequence[TaskSpec],
    *,
    system_root: Path,
    model_config: Mapping[str, Any],
    limits: Mapping[str, Any],
    seed: int,
) -> list[dict[str, Any]]:
    model = create_model_adapter(
        _mapping(model_config, "model"),
        raw_log_dir=system_root / "initial_raw_model_responses",
    )
    prepare = getattr(model, "prepare", None)
    if prepare is not None:
        prepare()
    results: list[dict[str, Any]] = []
    generation = _mapping(model_config, "generation")
    for index, task in enumerate(tasks, 1):
        print(f"[Phase6] local_single_agent {index}/{len(tasks)} {task.task_id}", flush=True)
        task_root = system_root / task.task_id
        task_root.mkdir(parents=True, exist_ok=False)
        _set_raw_log_dir(model, task_root / "raw_model_responses")
        before = tree_digest(task.repository_path)
        started = time.perf_counter()
        agent: SingleAgent | None = None
        try:
            agent = SingleAgent(
                model,
                workspace_root=task_root / "workspaces",
                report_root=task_root / "agent_runs",
                generation_config=_generation_config(generation),
                budget=ReactBudget(
                    max_steps=10,
                    max_model_calls=int(limits["max_model_calls"]),
                    max_tool_calls=12,
                    max_input_tokens=int(limits["max_input_tokens"]),
                    max_output_tokens=int(limits["max_output_tokens"]),
                    max_runtime_seconds=float(limits["max_runtime_seconds"]),
                ),
            )
            report = agent.run(task)
            result = _single_result(
                "local_single_agent",
                task,
                report,
                task_root=task_root,
                source_digest_before=before,
                source_digest_after=tree_digest(task.repository_path),
                limits=limits,
                seed=seed,
            )
        except Exception as exc:  # noqa: BLE001 - 单任务失败必须进入结果
            result = _error_result(
                "local_single_agent",
                task,
                task_root,
                exc=exc,
                duration_ms=int((time.perf_counter() - started) * 1000),
                limits=limits,
                seed=seed,
            )
        results.append(result)
        del agent
    model.close()
    del prepare, model
    _release_cuda()
    return results


async def _run_hybrid_system(
    system_id: str,
    tasks: Sequence[TaskSpec],
    *,
    system_root: Path,
    model_config: Mapping[str, Any],
    supervisor_config: Path,
    runtime_config: Mapping[str, Any],
    phase6_config: Mapping[str, Any],
    limits: Mapping[str, Any],
    pricing: Mapping[str, Any],
    annotations: Mapping[str, Mapping[str, Any]],
    seed: int,
) -> list[dict[str, Any]]:
    worker_models, worker_generation = create_worker_model_pool(
        model_config,
        raw_log_dir=system_root / "initial_raw_model_responses" / "workers",
    )
    preparing_pool = WorkerPool(
        worker_models,
        workspace_root=system_root / "initial_workspaces",
    )
    preparing_pool.prepare()
    router, supervisor_generation = create_supervisor_model_router(
        supervisor_config,
        raw_log_dir=system_root / "initial_raw_model_responses" / "supervisor",
    )
    supervisor_values = _mapping(load_yaml(supervisor_config), "supervisor")
    supervisor_recovery = _mapping(supervisor_values, "recovery")
    orchestration = _mapping(runtime_config, "orchestration")
    langgraph = _mapping(runtime_config, "langgraph")
    react = _mapping(phase6_config, "worker_react")
    results: list[dict[str, Any]] = []

    for index, task in enumerate(tasks, 1):
        print(f"[Phase6] {system_id} {index}/{len(tasks)} {task.task_id}", flush=True)
        task_root = system_root / task.task_id
        task_root.mkdir(parents=True, exist_ok=False)
        trace_path = task_root / "trace.jsonl"
        trace = TraceWriter(trace_path)
        router.raw_log_dir = task_root / "raw_model_responses" / "supervisor"
        router.trace_writer = trace
        for slot, model in enumerate(worker_models):
            _set_raw_log_dir(model, task_root / "raw_model_responses" / "workers" / f"slot-{slot}")
        if system_id == "fixed_hybrid":
            supervisor: Any = FixedPipelineSupervisor(
                router,
                generation_config=supervisor_generation,
                trace_writer=trace,
            )
        else:
            supervisor = SupervisorAgent(
                router,
                generation_config=supervisor_generation,
                trace_writer=trace,
                system_prompt=constrained_dynamic_prompt(
                    DYNAMIC_SUPERVISOR_PROMPT,
                    system_id,
                ),
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
        budget_values = effective_engine_budget(orchestration, limits)
        engine = OrchestrationEngine(
            task,
            budget=EngineBudget(**budget_values),
            trace_writer=trace,
        )
        thread_id = f"{system_id}-{task.task_id}-{seed}"
        before = tree_digest(task.repository_path)
        started = time.perf_counter()
        runtime: AsyncLangGraphRuntime | None = None
        try:
            runtime = await AsyncLangGraphRuntime.create(
                supervisor,
                pool,
                task_root / "langgraph.sqlite3",
                trace_writer=trace,
                recursion_limit=int(langgraph.get("recursion_limit", 128)),
            )
            async with runtime:
                state = await runtime.arun(engine, thread_id=thread_id)
            restored = restore_engine_from_runtime_state(state)
            result = _hybrid_result(
                system_id,
                task,
                restored=restored,
                task_root=task_root,
                trace_path=trace_path,
                duration_ms=int((time.perf_counter() - started) * 1000),
                source_digest_before=before,
                source_digest_after=tree_digest(task.repository_path),
                limits=limits,
                pricing=pricing,
                expected_complexity=str(
                    annotations.get(task.task_id, {}).get("expected_complexity", "unknown")
                ),
                seed=seed,
            )
        except Exception as exc:  # noqa: BLE001 - 单任务失败必须保留并继续批量评测
            trace.write(
                "evaluation_task_failed",
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
            result = _error_result(
                system_id,
                task,
                task_root,
                exc=exc,
                duration_ms=int((time.perf_counter() - started) * 1000),
                limits=limits,
                seed=seed,
            )
        results.append(result)
        del runtime, engine, pool, supervisor
        _release_cuda()

    for model in worker_models:
        model.close()
    del model, preparing_pool, worker_models, router
    _release_cuda()
    return results


def _generation_config(value: Mapping[str, Any]) -> Any:
    from repo_pilot_mas.models import GenerationConfig

    return GenerationConfig(
        temperature=float(value.get("temperature", 0.0)),
        max_output_tokens=int(value.get("max_output_tokens", 1024)),
        stop=tuple(str(item) for item in value.get("stop", ())),
        timeout_seconds=float(value.get("timeout_seconds", 180)),
        max_retries=int(value.get("max_retries", 1)),
    )


def _release_cuda() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        return


def _write_results_csv(path: Path, results: Sequence[Mapping[str, Any]]) -> None:
    fields = [
        "system_id",
        "task_id",
        "status",
        "reason",
        "target_test_passed",
        "regression_passed",
        "syntax_valid",
        "model_calls",
        "tool_calls",
        "total_tokens",
        "duration_ms",
        "estimated_api_cost_cny",
        "execution_path",
        "result_path",
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for item in results:
            writer.writerow(
                {
                    "system_id": item["system_id"],
                    "task_id": item["task_id"],
                    "status": item["status"],
                    "reason": item["reason"],
                    "target_test_passed": item["validation"]["target_test_passed"],
                    "regression_passed": item["validation"]["regression_passed"],
                    "syntax_valid": item["validation"]["syntax_valid"],
                    "model_calls": item["usage"]["model_calls"],
                    "tool_calls": item["usage"]["tool_calls"],
                    "total_tokens": item["usage"]["total_tokens"],
                    "duration_ms": item["usage"]["duration_ms"],
                    "estimated_api_cost_cny": item["usage"]["estimated_api_cost_cny"],
                    "execution_path": item["mechanism"]["execution_path"],
                    "result_path": item["result_path"],
                }
            )


def _write_summary_markdown(path: Path, summaries: Sequence[Mapping[str, Any]]) -> None:
    lines = [
        "# Phase 6 真实评测结果",
        "",
        "| 系统 | 解决数 | Resolution Rate | Token | 工具调用 | 中位时延(ms) | API 等价成本(元) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in summaries:
        lines.append(
            "| {system_id} | {solved}/{task_count} | {rate:.1%} | {tokens} | {tools} | "
            "{latency} | {cost:.6f} |".format(
                system_id=item["system_id"],
                solved=item["solved"],
                task_count=item["task_count"],
                rate=item["task_resolution_rate"],
                tokens=item["totals"]["total_tokens"],
                tools=item["totals"]["tool_calls"],
                latency=item["median_latency_ms"],
                cost=item["estimated_api_cost_cny"],
            )
        )
    lines.extend(
        [
            "",
            "> Resolution Rate 仅统计冻结测试集；失败、超时和预算违规均保留并计入分母。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _repository_fingerprint(root: Path) -> str:
    command = ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
    completed = subprocess.run(command, cwd=root, check=True, text=True, capture_output=True)
    digest = hashlib.sha256()
    for relative in sorted(line for line in completed.stdout.splitlines() if line):
        path = root / relative
        if not path.is_file() or relative.startswith("reports/"):
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


async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    repo_root = Path.cwd().resolve()
    phase6_config = load_yaml(args.phase6_config)
    protocol = _mapping(phase6_config, "protocol")
    suite = load_evaluation_suite(args.suite or protocol["suite"])
    selected_tasks = list(suite.test_tasks if args.split == "test" else suite.development_tasks)
    if args.task_id:
        selected_tasks = [task for task in selected_tasks if task.task_id == args.task_id]
        if not selected_tasks:
            raise ValueError(f"task_id 不属于 {args.split} 集：{args.task_id}")
    systems = list(ALL_SYSTEMS) if args.systems == "all" else args.systems.split(",")
    invalid = set(systems) - set(ALL_SYSTEMS)
    if invalid:
        raise ValueError(f"未知系统：{sorted(invalid)}")

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_root = Path(args.report_root).expanduser().resolve(strict=False) / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    model_config = load_yaml(args.model_config)
    runtime_config = load_yaml(args.runtime_config)
    single_limits = _mapping(phase6_config, "single_agent_limits")
    hybrid_limits = _mapping(phase6_config, "hybrid_limits")
    pricing_config = _mapping(phase6_config, "pricing")
    pricing = _mapping(pricing_config, "models")
    seed = suite.seed
    manifest = {
        "phase": 6,
        "run_id": run_id,
        "protocol_version": protocol["version"],
        "suite_id": suite.suite_id,
        "dataset": suite.dataset,
        "upstream_commit": suite.upstream_commit,
        "evaluation_split": args.split,
        "development_task_ids": list(suite.development_task_ids),
        "test_task_ids": [task.task_id for task in suite.test_tasks],
        "selected_task_ids": [task.task_id for task in selected_tasks],
        "systems": systems,
        "main_systems": list(MAIN_SYSTEMS),
        "seed": seed,
        "generation_temperature": 0.0,
        "single_agent_limits": single_limits,
        "hybrid_limits_shared_by_fixed_and_proposed": hybrid_limits,
        "pricing": pricing_config,
        "repository_commit": _git_commit(repo_root),
        "repository_fingerprint": _repository_fingerprint(repo_root),
        "python": sys.executable,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "final_evaluation_executed": False,
    }
    _save_json(run_root / "manifest.json", manifest)

    all_results: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for system_id in systems:
        system_root = run_root / system_id
        system_root.mkdir(parents=True, exist_ok=False)
        if system_id == "local_single_agent":
            results = _run_single_system(
                selected_tasks,
                system_root=system_root,
                model_config=model_config,
                limits=single_limits,
                seed=seed,
            )
        else:
            results = await _run_hybrid_system(
                system_id,
                selected_tasks,
                system_root=system_root,
                model_config=model_config,
                supervisor_config=Path(args.supervisor_config),
                runtime_config=runtime_config,
                phase6_config=phase6_config,
                limits=hybrid_limits,
                pricing=pricing,
                annotations=suite.task_annotations,
                seed=seed,
            )
        summary = aggregate_system_results(system_id, results)
        _save_json(system_root / "summary.json", summary)
        all_results.extend(results)
        summaries.append(summary)

    _write_results_csv(run_root / "task_results.csv", all_results)
    _write_summary_markdown(run_root / "results.md", summaries)
    full_run = args.split == "test" and not args.task_id and tuple(systems) == ALL_SYSTEMS
    gates = {
        "frozen_test_set_has_ten_tasks": len(selected_tasks) >= 10,
        "three_main_systems_executed": set(MAIN_SYSTEMS).issubset(systems),
        "two_key_ablations_executed": {
            "no_second_diagnostician",
            "no_challenge_rebuttal",
        }.issubset(systems),
        "every_system_has_every_task": all(
            summary["task_count"] == len(selected_tasks) for summary in summaries
        ),
        "all_results_traceable": all(
            Path(item["result_path"]).is_file() and Path(item["trace_path"]).is_file()
            for item in all_results
        ),
        "source_repositories_unchanged": all(
            item.get("source_integrity", {}).get("unchanged", False) for item in all_results
        ),
        "budget_outcomes_fail_closed": budget_outcomes_fail_closed(all_results),
        "fixed_and_proposed_share_hybrid_limits": True,
        "failures_are_included": len(all_results) == len(selected_tasks) * len(systems),
    }
    passed = full_run and all(gates.values())
    manifest.update(
        finished_at=datetime.now(timezone.utc).isoformat(),
        final_evaluation_executed=full_run,
    )
    _save_json(run_root / "manifest.json", manifest)
    acceptance = {
        "phase": 6,
        "run_id": run_id,
        "passed": passed,
        "full_run": full_run,
        "gates": gates,
        "systems": summaries,
        "manifest_path": str(run_root / "manifest.json"),
        "results_csv": str(run_root / "task_results.csv"),
        "results_markdown": str(run_root / "results.md"),
    }
    _save_json(run_root / "acceptance.json", acceptance)
    latest = Path(args.report_root).expanduser().resolve(strict=False) / "latest_summary.json"
    _save_json(
        latest,
        {
            "phase": 6,
            "run_id": run_id,
            "passed": passed,
            "full_run": full_run,
            "gates": gates,
            "report_path": str(run_root / "acceptance.json"),
        },
    )
    return acceptance, passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--systems", default="all", help="all 或逗号分隔系统 ID")
    parser.add_argument(
        "--split",
        choices=("development", "test"),
        default="test",
        help="开发调试必须使用 development；正式结果使用 test",
    )
    parser.add_argument("--task-id", help="只运行所选 split 中的一个任务")
    parser.add_argument("--suite", help="覆盖 phase6.yaml 中的评测套件路径")
    parser.add_argument("--phase6-config", default="configs/phase6.yaml")
    parser.add_argument("--model-config", default="configs/model.yaml")
    parser.add_argument("--supervisor-config", default="configs/supervisor.yaml")
    parser.add_argument("--runtime-config", default="configs/runtime.yaml")
    parser.add_argument("--report-root", default="reports/phase6/evaluation")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    report, passed = asyncio.run(_run(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed or args.task_id or args.split == "development" else 1


if __name__ == "__main__":
    raise SystemExit(main())
