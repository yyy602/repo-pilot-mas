"""Pure metrics and report writers for the final closed-loop evaluation."""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_ROOT_CAUSE_REVIEW_MODES = frozenset(
    {"hypothesis_comparison", "root_cause_recommendation"}
)
_ACCEPTABLE_ROOT_CAUSE_VERDICTS = frozenset(
    {"approved", "compatible", "supported"}
)
_WORKER_FAILURE_STATUSES = frozenset({"FAILED", "TIMED_OUT", "BLOCKED"})


def save_json(path: str | Path, value: Any) -> None:
    """Write one deterministic UTF-8 JSON report."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def closed_loop_task_mechanism(
    nodes: Sequence[Mapping[str, Any]],
    artifacts: Sequence[Mapping[str, Any]],
    hypothesis_resolution: Mapping[str, Any],
    *,
    engine_succeeded: bool,
    workspace_clean: bool,
    recovery_metrics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive auditable closed-loop mechanism metrics for one task."""

    artifact_by_ref = {
        build_artifact_ref(item): item
        for item in artifacts
        if _has_artifact_identity(item)
    }
    accepted_refs = _unique_strings(
        hypothesis_resolution.get("accepted_refs", ())
    )
    review_refs = _unique_strings(hypothesis_resolution.get("review_refs", ()))
    hypothesis_accepted = bool(
        hypothesis_resolution.get("status") == "accepted"
        and accepted_refs
        and hypothesis_resolution.get("primary_ref") in accepted_refs
        and review_refs
    )
    root_cause_review_passed = bool(
        hypothesis_accepted
        and all(
            _is_acceptable_root_cause_review(artifact_by_ref.get(ref))
            for ref in review_refs
        )
    )
    accepted_hypotheses = [artifact_by_ref.get(ref) for ref in accepted_refs]
    supporting_evidence_refs = _unique_strings(
        tuple(
            ref
            for hypothesis in accepted_hypotheses
            if hypothesis is not None
            for ref in _content(hypothesis).get("supporting_evidence", ())
        )
    )
    review_evidence_refs = {
        str(ref)
        for ref in review_refs
        if artifact_by_ref.get(ref) is not None
        for ref in _content(artifact_by_ref[ref]).get("evidence_refs", ())
    }
    evidence_gate_passed = bool(
        hypothesis_accepted
        and supporting_evidence_refs
        and set(supporting_evidence_refs).issubset(review_evidence_refs)
        and all(
            (evidence := artifact_by_ref.get(ref)) is not None
            and evidence.get("artifact_type") == "evidence"
            and _content(evidence).get("verified") is True
            and bool(_content(evidence).get("tool_trace_ids"))
            for ref in supporting_evidence_refs
        )
    )

    patch_candidates = [
        item
        for item in artifacts
        if item.get("artifact_type") == "patch_candidate"
    ]
    patch_bound_to_accepted_set = (
        any(_same_ref_set(_patch_refs(item), accepted_refs) for item in patch_candidates)
        if accepted_refs
        else False
    )
    validations = [
        item
        for item in artifacts
        if item.get("artifact_type") == "validation_result"
    ]
    patch_validation_passed = any(
        bool(_content(item).get("passed")) for item in validations
    )

    rejection_items = [
        item
        for item in artifacts
        if item.get("artifact_type") == "artifact_rejection"
    ]
    rejection_node_ids = {
        str(_content(item).get("node_id", ""))
        for item in rejection_items
        if _content(item).get("node_id")
    }
    failed_worker_ids = {
        str(item.get("node_id", ""))
        for item in nodes
        if item.get("status") in _WORKER_FAILURE_STATUSES
    }
    isolated_worker_ids = failed_worker_ids.intersection(rejection_node_ids)
    replan_records = sum(
        item.get("artifact_type") == "replan_record" for item in artifacts
    )
    retry_nodes = sum(int(item.get("retry_count", 0)) > 0 for item in nodes)
    recovery_attempted = bool(rejection_items or replan_records or retry_nodes)
    recovery = dict(recovery_metrics or {})
    contract_rejections = sum(
        _content(item).get("code") == "AGENT_INPUT_CONTRACT_VIOLATION"
        for item in rejection_items
    )

    return {
        "hypothesis_accepted": hypothesis_accepted,
        "evidence_gate_passed": evidence_gate_passed,
        "accepted_hypothesis_count": len(accepted_refs),
        "root_cause_review_passed": root_cause_review_passed,
        "accepted_review_count": len(review_refs),
        "patch_bound_to_accepted_set": patch_bound_to_accepted_set,
        "patch_validation_passed": patch_validation_passed,
        "validation_passed": patch_validation_passed,
        "artifact_rejection_count": len(rejection_items),
        "worker_failure_count": len(failed_worker_ids),
        "isolated_worker_failure_count": len(isolated_worker_ids),
        "replan_record_count": replan_records,
        "retry_node_count": retry_nodes,
        "worker_retry_count": sum(int(item.get("retry_count", 0)) for item in nodes),
        "contract_rejection_count": int(
            recovery.get("contract_rejection_count", contract_rejections)
        ),
        "node_rebuild_count": int(recovery.get("node_rebuild_count", 0)),
        "supervisor_format_repair_count": int(
            recovery.get("supervisor_format_repair_count", 0)
        ),
        "supervisor_snapshot_count": int(
            recovery.get("supervisor_snapshot_count", 0)
        ),
        "supervisor_snapshot_original_chars": int(
            recovery.get("supervisor_snapshot_original_chars", 0)
        ),
        "supervisor_snapshot_compact_chars": int(
            recovery.get("supervisor_snapshot_compact_chars", 0)
        ),
        "supervisor_snapshot_reduction_ratio": recovery.get(
            "supervisor_snapshot_reduction_ratio"
        ),
        "replan_attempt_count": replan_records,
        "replan_succeeded": bool(replan_records and engine_succeeded),
        "recovery_attempted": recovery_attempted,
        "recovery_succeeded": bool(recovery_attempted and engine_succeeded),
        "workspace_clean": bool(workspace_clean),
        "route_attempts_by_model": dict(
            recovery.get("route_attempts_by_model", {})
        ),
        "route_successes_by_model": dict(
            recovery.get("route_successes_by_model", {})
        ),
        "route_exhausted_by_model": dict(
            recovery.get("route_exhausted_by_model", {})
        ),
        "latency_by_stage": dict(recovery.get("latency_by_stage", {})),
    }


def aggregate_closed_loop_results(
    system_id: str,
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate final task outcomes without dropping failures."""

    if not results:
        raise ValueError("closed-loop summary requires at least one task result")
    total = len(results)
    solved = sum(item.get("status") == "succeeded" for item in results)
    usage_fields = (
        "model_calls",
        "supervisor_api_calls",
        "worker_model_calls",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "duration_ms",
    )
    totals = {
        field: sum(
            int(item.get("usage", {}).get(field, 0)) for item in results
        )
        for field in usage_fields
    }
    total_cost = round(
        sum(
            float(
                item.get("usage", {}).get("estimated_api_cost_cny", 0.0)
            )
            for item in results
        ),
        6,
    )
    worker_failures = sum(
        int(item.get("closed_loop", {}).get("worker_failure_count", 0))
        for item in results
    )
    isolated_failures = sum(
        int(
            item.get("closed_loop", {}).get(
                "isolated_worker_failure_count",
                0,
            )
        )
        for item in results
    )
    recovery_attempts = sum(
        bool(item.get("closed_loop", {}).get("recovery_attempted"))
        for item in results
    )
    recovery_successes = sum(
        bool(item.get("closed_loop", {}).get("recovery_succeeded"))
        for item in results
    )
    paths = Counter(
        str(item.get("mechanism", {}).get("execution_path", "unknown"))
        for item in results
    )
    termination_codes = Counter(
        str(item.get("termination", {}).get("code", "missing"))
        for item in results
    )
    termination_stages = Counter(
        str(item.get("termination", {}).get("stage", "missing"))
        for item in results
    )
    termination_failure_classes = Counter(
        str(item.get("termination", {}).get("failure_class", "missing"))
        for item in results
    )
    snapshot_original_chars = _closed_loop_sum(
        results,
        "supervisor_snapshot_original_chars",
    )
    snapshot_compact_chars = _closed_loop_sum(
        results,
        "supervisor_snapshot_compact_chars",
    )

    return {
        "schema_version": 1,
        "system_id": system_id,
        "task_count": total,
        "solved": solved,
        "failed": total - solved,
        "task_resolution_rate": solved / total,
        "target_test_pass_rate": _validation_rate(
            results,
            "target_test_passed",
        ),
        "regression_pass_rate": _validation_rate(
            results,
            "regression_passed",
        ),
        "patch_apply_rate": _validation_rate(results, "patch_applied"),
        "syntax_valid_rate": _validation_rate(results, "syntax_valid"),
        "hypothesis_accept_rate": _closed_loop_rate(
            results,
            "hypothesis_accepted",
        ),
        "evidence_gate_pass_rate": _closed_loop_rate(
            results,
            "evidence_gate_passed",
        ),
        "review_pass_rate": _closed_loop_rate(
            results,
            "root_cause_review_passed",
        ),
        "patch_binding_rate": _closed_loop_rate(
            results,
            "patch_bound_to_accepted_set",
        ),
        "patch_success_rate": _closed_loop_rate(
            results,
            "patch_validation_passed",
        ),
        "validation_pass_rate": _closed_loop_rate(
            results,
            "validation_passed",
        ),
        "worker_failure_count": worker_failures,
        "isolated_worker_failure_count": isolated_failures,
        "worker_failure_isolation_rate": (
            isolated_failures / worker_failures if worker_failures else None
        ),
        "recovery_attempted_tasks": recovery_attempts,
        "recovery_succeeded_tasks": recovery_successes,
        "recovery_success_rate": (
            recovery_successes / recovery_attempts
            if recovery_attempts
            else None
        ),
        "termination_codes": dict(sorted(termination_codes.items())),
        "termination_stages": dict(sorted(termination_stages.items())),
        "termination_failure_classes": dict(
            sorted(termination_failure_classes.items())
        ),
        "workspace_cleanup_rate": _closed_loop_rate(
            results,
            "workspace_clean",
        ),
        "artifact_rejection_count": sum(
            int(
                item.get("closed_loop", {}).get(
                    "artifact_rejection_count",
                    0,
                )
            )
            for item in results
        ),
        "worker_retry_count": _closed_loop_sum(results, "worker_retry_count"),
        "contract_rejection_count": _closed_loop_sum(
            results,
            "contract_rejection_count",
        ),
        "node_rebuild_count": _closed_loop_sum(results, "node_rebuild_count"),
        "supervisor_format_repair_count": _closed_loop_sum(
            results,
            "supervisor_format_repair_count",
        ),
        "supervisor_snapshot_count": _closed_loop_sum(
            results,
            "supervisor_snapshot_count",
        ),
        "supervisor_snapshot_original_chars": snapshot_original_chars,
        "supervisor_snapshot_compact_chars": snapshot_compact_chars,
        "supervisor_snapshot_reduction_ratio": (
            1.0 - snapshot_compact_chars / snapshot_original_chars
            if snapshot_original_chars
            else None
        ),
        "replan_attempt_count": _closed_loop_sum(results, "replan_attempt_count"),
        "replan_success_rate": _conditional_closed_loop_rate(
            results,
            attempted="replan_attempt_count",
            succeeded="replan_succeeded",
        ),
        "route_attempts_by_model": _merge_counters(
            results,
            "route_attempts_by_model",
        ),
        "route_successes_by_model": _merge_counters(
            results,
            "route_successes_by_model",
        ),
        "route_exhausted_by_model": _merge_counters(
            results,
            "route_exhausted_by_model",
        ),
        "supervisor_tokens": sum(
            int(item.get("usage", {}).get("supervisor_tokens", 0))
            for item in results
        ),
        "worker_tokens": sum(
            int(item.get("usage", {}).get("worker_tokens", 0))
            for item in results
        ),
        "latency_by_stage": _merge_latency_by_stage(results),
        "budget_violations": sum(
            not bool(item.get("budget", {}).get("within_budget", False))
            for item in results
        ),
        "source_integrity_violations": sum(
            not bool(
                item.get("source_integrity", {}).get("unchanged", False)
            )
            for item in results
        ),
        "totals": totals,
        "average_tokens": totals["total_tokens"] / total,
        "average_latency_ms": totals["duration_ms"] / total,
        "median_latency_ms": int(
            statistics.median(
                int(item.get("usage", {}).get("duration_ms", 0))
                for item in results
            )
        ),
        "estimated_api_cost_cny": total_cost,
        "estimated_api_cost_per_solved_cny": (
            round(total_cost / solved, 6) if solved else None
        ),
        "route_distribution": dict(sorted(paths.items())),
        "metric_layers": {
            "business_results": {
                "task_resolution_rate": solved / total,
                "target_test_pass_rate": _validation_rate(
                    results, "target_test_passed"
                ),
                "regression_pass_rate": _validation_rate(
                    results, "regression_passed"
                ),
                "patch_success_rate": _closed_loop_rate(
                    results, "patch_validation_passed"
                ),
            },
            "closed_loop_mechanism": {
                "evidence_gate_pass_rate": _closed_loop_rate(
                    results, "evidence_gate_passed"
                ),
                "hypothesis_accept_rate": _closed_loop_rate(
                    results, "hypothesis_accepted"
                ),
                "review_pass_rate": _closed_loop_rate(
                    results, "root_cause_review_passed"
                ),
                "patch_binding_rate": _closed_loop_rate(
                    results, "patch_bound_to_accepted_set"
                ),
                "validation_pass_rate": _closed_loop_rate(
                    results, "validation_passed"
                ),
            },
            "exception_recovery": {
                "worker_retry_count": _closed_loop_sum(
                    results, "worker_retry_count"
                ),
                "contract_rejection_count": _closed_loop_sum(
                    results, "contract_rejection_count"
                ),
                "node_rebuild_count": _closed_loop_sum(
                    results, "node_rebuild_count"
                ),
                "supervisor_format_repair_count": _closed_loop_sum(
                    results, "supervisor_format_repair_count"
                ),
                "replan_attempt_count": _closed_loop_sum(
                    results, "replan_attempt_count"
                ),
                "replan_success_rate": _conditional_closed_loop_rate(
                    results,
                    attempted="replan_attempt_count",
                    succeeded="replan_succeeded",
                ),
            },
            "routing_and_cost": {
                "route_attempts_by_model": _merge_counters(
                    results, "route_attempts_by_model"
                ),
                "route_successes_by_model": _merge_counters(
                    results, "route_successes_by_model"
                ),
                "route_exhausted_by_model": _merge_counters(
                    results, "route_exhausted_by_model"
                ),
                "supervisor_tokens": sum(
                    int(item.get("usage", {}).get("supervisor_tokens", 0))
                    for item in results
                ),
                "worker_tokens": sum(
                    int(item.get("usage", {}).get("worker_tokens", 0))
                    for item in results
                ),
                "total_tokens": totals["total_tokens"],
                "latency_by_stage": _merge_latency_by_stage(results),
                "estimated_cost_cny": total_cost,
            },
        },
        "task_result_paths": [str(item["result_path"]) for item in results],
    }


def write_closed_loop_results_csv(
    path: str | Path,
    results: Sequence[Mapping[str, Any]],
) -> None:
    """Write one flat task-level comparison table."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "task_id",
        "status",
        "reason",
        "hypothesis_accepted",
        "root_cause_review_passed",
        "patch_bound_to_accepted_set",
        "patch_validation_passed",
        "artifact_rejection_count",
        "worker_failure_count",
        "isolated_worker_failure_count",
        "recovery_attempted",
        "recovery_succeeded",
        "workspace_clean",
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
    )
    with target.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for item in results:
            validation = item.get("validation", {})
            usage = item.get("usage", {})
            mechanism = item.get("mechanism", {})
            closed_loop = item.get("closed_loop", {})
            writer.writerow(
                {
                    "task_id": item.get("task_id"),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                    "hypothesis_accepted": closed_loop.get(
                        "hypothesis_accepted",
                        False,
                    ),
                    "root_cause_review_passed": closed_loop.get(
                        "root_cause_review_passed",
                        False,
                    ),
                    "patch_bound_to_accepted_set": closed_loop.get(
                        "patch_bound_to_accepted_set",
                        False,
                    ),
                    "patch_validation_passed": closed_loop.get(
                        "patch_validation_passed",
                        False,
                    ),
                    "artifact_rejection_count": closed_loop.get(
                        "artifact_rejection_count",
                        0,
                    ),
                    "worker_failure_count": closed_loop.get(
                        "worker_failure_count",
                        0,
                    ),
                    "isolated_worker_failure_count": closed_loop.get(
                        "isolated_worker_failure_count",
                        0,
                    ),
                    "recovery_attempted": closed_loop.get(
                        "recovery_attempted",
                        False,
                    ),
                    "recovery_succeeded": closed_loop.get(
                        "recovery_succeeded",
                        False,
                    ),
                    "workspace_clean": closed_loop.get(
                        "workspace_clean",
                        False,
                    ),
                    "target_test_passed": validation.get(
                        "target_test_passed",
                        False,
                    ),
                    "regression_passed": validation.get(
                        "regression_passed",
                        False,
                    ),
                    "syntax_valid": validation.get("syntax_valid", False),
                    "model_calls": usage.get("model_calls", 0),
                    "tool_calls": usage.get("tool_calls", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                    "duration_ms": usage.get("duration_ms", 0),
                    "estimated_api_cost_cny": usage.get(
                        "estimated_api_cost_cny",
                        0.0,
                    ),
                    "execution_path": mechanism.get(
                        "execution_path",
                        "unknown",
                    ),
                    "result_path": item.get("result_path"),
                }
            )


def write_closed_loop_final_report(
    path: str | Path,
    summary: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
) -> None:
    """Write a human-readable final report without hiding failed tasks."""

    isolation = summary.get("worker_failure_isolation_rate")
    recovery = summary.get("recovery_success_rate")
    lines = [
        "# RepoPilot-MAS 最终闭环评测报告",
        "",
        "## 总体结果",
        "",
        "| 指标 | 结果 |",
        "| --- | ---: |",
        f"| 任务解决率 | {summary['task_resolution_rate']:.1%} |",
        f"| 根因接受率 | {summary['hypothesis_accept_rate']:.1%} |",
        f"| 根因审查通过率 | {summary['review_pass_rate']:.1%} |",
        f"| Patch 根因集合绑定率 | {summary['patch_binding_rate']:.1%} |",
        f"| Patch 完整验证通过率 | {summary['patch_success_rate']:.1%} |",
        f"| Worker 失败隔离率 | {_format_optional_rate(isolation)} |",
        f"| Recovery 成功率 | {_format_optional_rate(recovery)} |",
        f"| Workspace 清理率 | {summary['workspace_cleanup_rate']:.1%} |",
        f"| 平均 Token | {summary['average_tokens']:.1f} |",
        f"| 平均时延(ms) | {summary['average_latency_ms']:.1f} |",
        "",
        "## 逐任务结果",
        "",
        "| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in results:
        closed_loop = item.get("closed_loop", {})
        usage = item.get("usage", {})
        lines.append(
            "| {task} | {status} | {hypothesis} | {review} | {patch} | "
            "{recovery} | {tokens} | {latency} |".format(
                task=item.get("task_id"),
                status=item.get("status"),
                hypothesis=_yes_no(
                    closed_loop.get("hypothesis_accepted", False)
                ),
                review=_yes_no(
                    closed_loop.get("root_cause_review_passed", False)
                ),
                patch=_yes_no(
                    closed_loop.get("patch_validation_passed", False)
                ),
                recovery=_yes_no(
                    closed_loop.get("recovery_succeeded", False)
                ) if closed_loop.get("recovery_attempted") else "N/A",
                tokens=usage.get("total_tokens", 0),
                latency=usage.get("duration_ms", 0),
            )
        )

    failures = [item for item in results if item.get("status") != "succeeded"]
    lines.extend(["", "## 失败任务", ""])
    if not failures:
        lines.append("本次运行没有失败任务。")
    else:
        for item in failures:
            lines.append(
                f"- `{item.get('task_id')}`: {item.get('reason', 'unknown')}"
            )
    lines.extend(
        [
            "",
            "> 失败、超时、预算违规和 Runner 异常均保留在分母中。",
            "",
        ]
    )
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def _is_acceptable_root_cause_review(
    artifact: Mapping[str, Any] | None,
) -> bool:
    if artifact is None or artifact.get("artifact_type") != "review":
        return False
    content = _content(artifact)
    return bool(
        content.get("mode") in _ROOT_CAUSE_REVIEW_MODES
        and content.get("verdict") in _ACCEPTABLE_ROOT_CAUSE_VERDICTS
        and content.get("failure_explained") is True
        and content.get("causal_chain_complete") is True
        and (
            bool(content.get("alternative_causes"))
            or content.get("counterexample_checked") is True
        )
        and bool(content.get("verification_steps_executed"))
        and not content.get("remaining_uncertainty")
    )


def _patch_refs(item: Mapping[str, Any]) -> tuple[str, ...]:
    content = _content(item)
    return _unique_strings(content.get("based_on_hypothesis_refs", ()))


def build_artifact_ref(item: Mapping[str, Any]) -> str:
    """Build the canonical ref from Artifact identity fields."""

    artifact_id = str(item.get("artifact_id", ""))
    version = item.get("version")
    if artifact_id and isinstance(version, int) and not isinstance(version, bool):
        return f"{artifact_id}@v{version}"
    fallback = str(item.get("artifact_ref", ""))
    if fallback:
        return fallback
    raise ValueError("artifact requires artifact_id/version or artifact_ref")


def _has_artifact_identity(item: Mapping[str, Any]) -> bool:
    return bool(
        (item.get("artifact_id") and isinstance(item.get("version"), int))
        or item.get("artifact_ref")
    )


def _same_ref_set(left: Sequence[str], right: Sequence[str]) -> bool:
    return len(left) == len(right) and set(left) == set(right)


def _content(item: Mapping[str, Any]) -> Mapping[str, Any]:
    value = item.get("content", {})
    return value if isinstance(value, Mapping) else {}


def _unique_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        return (str(value),) if str(value) else ()
    if not isinstance(value, Sequence):
        return ()
    return tuple(dict.fromkeys(str(item) for item in value if str(item)))


def _validation_rate(
    results: Sequence[Mapping[str, Any]],
    field: str,
) -> float:
    return sum(
        bool(item.get("validation", {}).get(field)) for item in results
    ) / len(results)


def _closed_loop_rate(
    results: Sequence[Mapping[str, Any]],
    field: str,
) -> float:
    return sum(
        bool(item.get("closed_loop", {}).get(field)) for item in results
    ) / len(results)


def _closed_loop_sum(
    results: Sequence[Mapping[str, Any]],
    field: str,
) -> int:
    return sum(int(item.get("closed_loop", {}).get(field, 0)) for item in results)


def _conditional_closed_loop_rate(
    results: Sequence[Mapping[str, Any]],
    *,
    attempted: str,
    succeeded: str,
) -> float | None:
    attempted_tasks = [
        item
        for item in results
        if int(item.get("closed_loop", {}).get(attempted, 0)) > 0
    ]
    if not attempted_tasks:
        return None
    return sum(
        bool(item.get("closed_loop", {}).get(succeeded))
        for item in attempted_tasks
    ) / len(attempted_tasks)


def _merge_counters(
    results: Sequence[Mapping[str, Any]],
    field: str,
) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for item in results:
        values = item.get("closed_loop", {}).get(field, {})
        if isinstance(values, Mapping):
            counter.update({str(key): int(value) for key, value in values.items()})
    return dict(sorted(counter.items()))


def _merge_latency_by_stage(
    results: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, float | int]]:
    calls: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    for item in results:
        values = item.get("closed_loop", {}).get("latency_by_stage", {})
        if not isinstance(values, Mapping):
            continue
        for stage, raw in values.items():
            if not isinstance(raw, Mapping):
                continue
            calls[str(stage)] += int(raw.get("calls", 0))
            totals[str(stage)] += int(raw.get("total_ms", 0))
    return {
        stage: {
            "calls": calls[stage],
            "total_ms": total_ms,
            "average_ms": total_ms / calls[stage],
        }
        for stage, total_ms in sorted(totals.items())
        if calls[stage]
    }


def _format_optional_rate(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.1%}"


def _yes_no(value: Any) -> str:
    return "是" if bool(value) else "否"


__all__ = [
    "aggregate_closed_loop_results",
    "build_artifact_ref",
    "closed_loop_task_mechanism",
    "save_json",
    "write_closed_loop_final_report",
    "write_closed_loop_results_csv",
]
