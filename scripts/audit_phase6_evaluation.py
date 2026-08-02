"""独立审计 Phase 6 冻结评测，并导出适合提交仓库的精简证据。"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from repo_pilot_mas.config import load_yaml
from repo_pilot_mas.evaluation import (
    ALL_SYSTEMS,
    MAIN_SYSTEMS,
    aggregate_system_results,
    budget_outcomes_fail_closed,
    load_evaluation_suite,
    read_trace,
    trace_metrics,
)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON 顶层必须是对象：{path}")
    return value


def _save_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _relative(path: str | Path, repo_root: Path) -> str:
    resolved = Path(path).resolve(strict=False)
    try:
        return resolved.relative_to(repo_root).as_posix()
    except ValueError:
        return str(resolved)


def _valid_trace(path: Path) -> bool:
    try:
        events = read_trace(path)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(events) and all(isinstance(event, dict) for event in events)


def _summary_matches(saved: Mapping[str, Any], recomputed: Mapping[str, Any]) -> bool:
    ignored = {"task_result_paths"}
    return {key: value for key, value in saved.items() if key not in ignored} == {
        key: value for key, value in recomputed.items() if key not in ignored
    }


def _write_csv(path: Path, results: Sequence[Mapping[str, Any]], repo_root: Path) -> None:
    fields = (
        "system_id",
        "task_id",
        "status",
        "reason",
        "within_budget",
        "target_test_passed",
        "regression_passed",
        "syntax_valid",
        "model_calls",
        "supervisor_api_calls",
        "tool_calls",
        "total_tokens",
        "duration_ms",
        "estimated_api_cost_cny",
        "execution_path",
        "result_path",
        "trace_path",
    )
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
                    "within_budget": item["budget"]["within_budget"],
                    "target_test_passed": item["validation"]["target_test_passed"],
                    "regression_passed": item["validation"]["regression_passed"],
                    "syntax_valid": item["validation"]["syntax_valid"],
                    "model_calls": item["usage"]["model_calls"],
                    "supervisor_api_calls": item["usage"]["supervisor_api_calls"],
                    "tool_calls": item["usage"]["tool_calls"],
                    "total_tokens": item["usage"]["total_tokens"],
                    "duration_ms": item["usage"]["duration_ms"],
                    "estimated_api_cost_cny": item["usage"]["estimated_api_cost_cny"],
                    "execution_path": item["mechanism"]["execution_path"],
                    "result_path": _relative(str(item["result_path"]), repo_root),
                    "trace_path": _relative(str(item["trace_path"]), repo_root),
                }
            )


def _write_markdown(
    path: Path,
    summaries: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    passed: bool,
) -> None:
    lines = [
        "# Phase 6 冻结评测结果",
        "",
        f"- 运行 ID：`{run_id}`",
        f"- 独立审计：`{'通过' if passed else '未通过'}`",
        "- 测试集：10 个冻结 QuixBugs Python 任务，seed=0",
        "",
        "| 系统 | 解决数 | 成功率 | Token | API 调用 | 工具调用 | 中位时延 | 等价 API 成本 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in summaries:
        lines.append(
            "| {system_id} | {solved}/{task_count} | {rate:.1%} | {tokens:,} | "
            "{api_calls} | {tools} | {latency:,} ms | ¥{cost:.6f} |".format(
                system_id=item["system_id"],
                solved=item["solved"],
                task_count=item["task_count"],
                rate=item["task_resolution_rate"],
                tokens=item["totals"]["total_tokens"],
                api_calls=item["totals"]["supervisor_api_calls"],
                tools=item["totals"]["tool_calls"],
                latency=item["median_latency_ms"],
                cost=item["estimated_api_cost_cny"],
            )
        )
    lines.extend(
        [
            "",
            "失败、超时和预算耗尽均保留并计入分母。预算耗尽只有在结果以失败关闭时才通过审计。",
            "成本按 `configs/phase6.yaml` 中冻结的公开单价估算，不代表免费额度下的实际账单。",
            "这是单一随机种子、10 个小型 Python 任务的结果，不能外推为通用仓库修复能力。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def audit_run(run_root: Path, output_root: Path, repo_root: Path) -> dict[str, Any]:
    manifest = _load_json(run_root / "manifest.json")
    suite = load_evaluation_suite("data/quixbugs/phase6_suite.json")
    phase6 = load_yaml("configs/phase6.yaml")
    expected_hybrid_limits = dict(phase6["hybrid_limits"])
    systems = tuple(str(item) for item in manifest.get("systems", ()))
    task_ids = tuple(task.task_id for task in suite.test_tasks)
    results_by_system: dict[str, list[dict[str, Any]]] = {}
    traces_are_valid = True
    checkpoints_exist = True

    for system_id in ALL_SYSTEMS:
        system_results: list[dict[str, Any]] = []
        for task_id in task_ids:
            result_path = run_root / system_id / task_id / "result.json"
            if not result_path.is_file():
                continue
            result = _load_json(result_path)
            system_results.append(result)
            trace_path = Path(str(result.get("trace_path", "")))
            traces_are_valid = traces_are_valid and _valid_trace(trace_path)
            if system_id != "local_single_agent":
                checkpoint = Path(str(result.get("checkpoint_path", "")))
                checkpoints_exist = checkpoints_exist and checkpoint.is_file()
        results_by_system[system_id] = system_results

    all_results = [result for system_id in ALL_SYSTEMS for result in results_by_system[system_id]]
    summaries: list[dict[str, Any]] = []
    saved_summaries_match = True
    corrected_invalid_decisions: dict[str, int] = {}
    supervisor_routes: dict[str, dict[str, int]] = {}
    for system_id in ALL_SYSTEMS:
        results = results_by_system[system_id]
        summary = aggregate_system_results(system_id, results)
        summary["task_result_paths"] = [
            _relative(str(item["result_path"]), repo_root) for item in results
        ]
        summaries.append(summary)
        saved = _load_json(run_root / system_id / "summary.json")
        recomputed = aggregate_system_results(system_id, results)
        saved_summaries_match = saved_summaries_match and _summary_matches(saved, recomputed)
        invalid = 0
        routes: Counter[str] = Counter()
        for item in results:
            metrics = trace_metrics(read_trace(str(item["trace_path"])))
            invalid += int(metrics["invalid_decisions"])
            routes.update(metrics["route_usage"])
        corrected_invalid_decisions[system_id] = invalid
        supervisor_routes[system_id] = dict(sorted(routes.items()))

    phase5_latest = _load_json(Path("reports/phase5/acceptance/latest_summary.json"))
    hybrid_results = [item for item in all_results if item["system_id"] != "local_single_agent"]
    successful_results = [item for item in all_results if item["status"] == "succeeded"]
    gates = {
        "frozen_split_matches_manifest": bool(
            manifest.get("evaluation_split") == "test"
            and tuple(manifest.get("selected_task_ids", ())) == task_ids
            and not set(suite.development_task_ids).intersection(task_ids)
        ),
        "three_main_and_two_ablations_present": bool(
            systems == ALL_SYSTEMS and set(MAIN_SYSTEMS).issubset(systems)
        ),
        "complete_fifty_result_matrix": bool(
            len(all_results) == len(ALL_SYSTEMS) * len(task_ids)
            and all(len(results_by_system[item]) == len(task_ids) for item in ALL_SYSTEMS)
        ),
        "all_traces_are_valid_jsonl": traces_are_valid,
        "all_hybrid_checkpoints_exist": checkpoints_exist,
        "no_runner_errors": all(
            not str(item.get("reason", "")).startswith("RUNNER_ERROR:") for item in all_results
        ),
        "sources_unchanged": all(
            item.get("source_integrity", {}).get("unchanged") is True for item in all_results
        ),
        "budget_outcomes_fail_closed": budget_outcomes_fail_closed(all_results),
        "successful_results_pass_all_validation": all(
            item["validation"].get("patch_applied") is True
            and item["validation"].get("target_test_passed") is True
            and item["validation"].get("regression_passed") is True
            and item["validation"].get("syntax_valid") is True
            and item["validation"].get("protected_path_violations") == 0
            for item in successful_results
        ),
        "fixed_and_dynamic_share_hybrid_limits": bool(
            manifest.get("hybrid_limits_shared_by_fixed_and_proposed") == expected_hybrid_limits
            and all(
                item["budget"].get("limits") == expected_hybrid_limits for item in hybrid_results
            )
        ),
        "api_supervisor_really_called": all(
            sum(
                int(item["usage"].get("supervisor_api_calls", 0))
                for item in results_by_system[system_id]
            )
            > 0
            for system_id in ("fixed_hybrid", "dynamic_hybrid")
        ),
        "no_second_diagnostician_contract_holds": all(
            sum(node.get("node_type") == "DIAGNOSIS_TASK" for node in item.get("nodes", ())) <= 1
            for item in results_by_system["no_second_diagnostician"]
        ),
        "no_challenge_rebuttal_contract_holds": all(
            all(
                node.get("node_type") not in {"CHALLENGE_TASK", "REBUTTAL_TASK"}
                for node in item.get("nodes", ())
            )
            for item in results_by_system["no_challenge_rebuttal"]
        ),
        "saved_summaries_recompute_exactly": saved_summaries_match,
        "phase5_mechanism_acceptance_passed": bool(phase5_latest.get("passed")),
    }
    passed = all(gates.values())

    output_root.mkdir(parents=True, exist_ok=True)
    _save_json(
        output_root / "results_summary.json",
        {
            "schema_version": 1,
            "run_id": manifest["run_id"],
            "suite_id": suite.suite_id,
            "seed": suite.seed,
            "task_ids": list(task_ids),
            "systems": summaries,
            "corrected_invalid_decisions": corrected_invalid_decisions,
            "supervisor_route_usage": supervisor_routes,
            "limitations": [
                "测试集只有 10 个小型 Python 算法任务",
                "只执行 seed=0，未做重复采样和显著性检验",
                "动态系统与本地 Single-Agent 的比较包含 Supervisor 模型能力差异",
                "真实动态运行未触发有效 Challenge/Rebuttal，相关机制仅由 Phase 5 确定性案例证明",
                "重规划尝试没有在本次冻结测试中恢复成功",
            ],
        },
    )
    _write_csv(output_root / "task_results.csv", all_results, repo_root)
    _write_markdown(
        output_root / "results.md",
        summaries,
        run_id=str(manifest["run_id"]),
        passed=passed,
    )

    demo_sources = {
        "真实动态成功快速路径": run_root / "dynamic_hybrid/quixbugs_bucketsort/trace.jsonl",
        "固定流水线真实并行": run_root / "fixed_hybrid/quixbugs_bucketsort/trace.jsonl",
        "真实动态验证失败与重规划": run_root / "dynamic_hybrid/quixbugs_flatten/trace.jsonl",
        "真实非法决策被拒绝": run_root / "dynamic_hybrid/quixbugs_get_factors/trace.jsonl",
    }
    demo_root = output_root / "demo_traces"
    demo_root.mkdir(parents=True, exist_ok=True)
    trace_index: dict[str, Any] = {
        "phase6_real_model": {},
        "phase5_deterministic_mechanism": {
            "质疑修正与错误补丁淘汰": "reports/phase5/acceptance/20260802T033236637083Z/deep_trace.jsonl",
            "定向重规划": "reports/phase5/acceptance/20260802T033236637083Z/replan_trace.jsonl",
            "简单任务未扩展": "reports/phase5/acceptance/20260802T033236637083Z/simple_trace.jsonl",
            "无进展确定性终止": "reports/phase5/acceptance/20260802T033236637083Z/no_progress_trace.jsonl",
        },
    }
    for label, source in demo_sources.items():
        target = demo_root / f"{source.parent.parent.name}_{source.parent.name}.jsonl"
        shutil.copyfile(source, target)
        trace_index["phase6_real_model"][label] = _relative(target, repo_root)
    _save_json(output_root / "trace_index.json", trace_index)

    audit = {
        "schema_version": 1,
        "phase": 6,
        "run_id": manifest["run_id"],
        "passed": passed,
        "gates": gates,
        "raw_run_root": _relative(run_root, repo_root),
        "raw_acceptance_note": (
            "原始 v1 运行器把任何预算耗尽误判为整场实验无效；本审计保留原文件，"
            "按 fail-closed 语义重新验收，未重写任务结果。"
        ),
        "result_count": len(all_results),
        "failed_result_count": sum(item["status"] != "succeeded" for item in all_results),
        "budget_exhausted_failure_count": sum(
            not item["budget"].get("within_budget", False) for item in all_results
        ),
        "outputs": {
            "summary": "reports/phase6/results_summary.json",
            "task_results": "reports/phase6/task_results.csv",
            "results": "reports/phase6/results.md",
            "trace_index": "reports/phase6/trace_index.json",
        },
    }
    _save_json(output_root / "final_audit.json", audit)
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-root",
        default="reports/phase6/evaluation/phase6_quixbugs_final_v1",
    )
    parser.add_argument("--output-root", default="reports/phase6")
    args = parser.parse_args()
    repo_root = Path.cwd().resolve()
    audit = audit_run(
        Path(args.run_root).resolve(),
        Path(args.output_root).resolve(),
        repo_root,
    )
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0 if audit["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
