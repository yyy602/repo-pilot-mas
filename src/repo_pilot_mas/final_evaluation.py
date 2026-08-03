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
) -> dict[str, Any]:
    """Derive auditable closed-loop mechanism metrics for one task."""

    artifact_by_ref = {
        str(item.get("artifact_ref", "")): item
        for item in artifacts
        if item.get("artifact_ref")
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

    patch_candidates = [
        item
        for item in artifacts
        if item.get("artifact_type") == "patch_candidate"
    ]
    patch_bound_to_accepted_set = any(
        _patch_refs(item) == accepted_refs for item in patch_candidates
    ) if accepted_refs else False
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

    return {
        "hypothesis_accepted": hypothesis_accepted,
        "accepted_hypothesis_count": len(accepted_refs),
        "root_cause_review_passed": root_cause_review_passed,
        "accepted_review_count": len(review_refs),
        "patch_bound_to_accepted_set": patch_bound_to_accepted_set,
        "patch_validation_passed": patch_validation_passed,
        "artifact_rejection_count": len(rejection_items),
        "worker_failure_count": len(failed_worker_ids),
        "isolated_worker_failure_count": len(isolated_worker_ids),
        "replan_record_count": replan_records,
        "retry_node_count": retry_nodes,
        "recovery_attempted": recovery_attempted,
        "recovery_succeeded": bool(recovery_attempted and engine_succeeded),
        "workspace_clean": bool(workspace_clean),
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
    )


def _patch_refs(item: Mapping[str, Any]) -> tuple[str, ...]:
    content = _content(item)
    raw_refs = content.get("based_on_hypothesis_refs")
    if raw_refs is None:
        legacy = content.get("based_on_hypothesis")
        raw_refs = (legacy,) if legacy else ()
    return _unique_strings(raw_refs)


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


def _format_optional_rate(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.1%}"


def _yes_no(value: Any) -> str:
    return "是" if bool(value) else "否"


__all__ = [
    "aggregate_closed_loop_results",
    "closed_loop_task_mechanism",
    "save_json",
    "write_closed_loop_final_report",
    "write_closed_loop_results_csv",
]
