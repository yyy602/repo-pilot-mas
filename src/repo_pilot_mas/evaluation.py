"""Phase 6 公开基准协议、固定流水线基线与可追溯指标汇总。"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from repo_pilot_mas.agents import SupervisorAgent, SupervisorAgentError, SupervisorOutcome
from repo_pilot_mas.models import GenerationConfig, ModelAdapter, TokenUsage
from repo_pilot_mas.orchestration import ExecutionPath, classify_execution_path
from repo_pilot_mas.schemas import (
    CreateTaskRequest,
    DecisionAction,
    GateRecord,
    SupervisorDecision,
    TaskSpec,
)
from repo_pilot_mas.tasks import load_task

MAIN_SYSTEMS = ("local_single_agent", "fixed_hybrid", "dynamic_hybrid")
ABLATION_SYSTEMS = ("no_second_diagnostician", "no_challenge_rebuttal")
ALL_SYSTEMS = (*MAIN_SYSTEMS, *ABLATION_SYSTEMS)


@dataclass(frozen=True, slots=True)
class EvaluationSuite:
    suite_id: str
    dataset: str
    upstream_commit: str
    seed: int
    development_tasks: tuple[TaskSpec, ...]
    test_tasks: tuple[TaskSpec, ...]
    task_annotations: Mapping[str, Mapping[str, Any]]
    source_path: Path

    @property
    def development_task_ids(self) -> tuple[str, ...]:
        return tuple(task.task_id for task in self.development_tasks)

    def __post_init__(self) -> None:
        if len(self.test_tasks) < 10:
            raise ValueError("Phase 6 正式测试集至少需要 10 个任务")
        test_ids = [task.task_id for task in self.test_tasks]
        if len(test_ids) != len(set(test_ids)):
            raise ValueError("Phase 6 测试集包含重复 task_id")
        development_ids = self.development_task_ids
        if len(development_ids) != len(set(development_ids)):
            raise ValueError("Phase 6 开发集包含重复 task_id")
        if set(test_ids).intersection(development_ids):
            raise ValueError("开发集与测试集不得重叠")


def load_evaluation_suite(path: str | Path) -> EvaluationSuite:
    suite_path = Path(path).expanduser().resolve(strict=True)
    value = json.loads(suite_path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError("评测套件必须是 JSON 对象")
    manifest_dir = suite_path.parent / str(value["task_manifest_directory"])
    development_tasks = tuple(
        load_task(manifest_dir / f"{task_id}.json")
        for task_id in _strings(value.get("development_task_ids", ()))
    )
    test_tasks = tuple(
        load_task(manifest_dir / f"{task_id}.json") for task_id in _strings(value["test_task_ids"])
    )
    annotations = value.get("task_annotations", {})
    if not isinstance(annotations, Mapping):
        raise TypeError("task_annotations 必须是对象")
    return EvaluationSuite(
        suite_id=str(value["suite_id"]),
        dataset=str(value["dataset"]),
        upstream_commit=str(value["upstream_commit"]),
        seed=int(value["seed"]),
        development_tasks=development_tasks,
        test_tasks=test_tasks,
        task_annotations={str(key): dict(item) for key, item in annotations.items()},
        source_path=suite_path,
    )


class FixedPipelineSupervisor:
    """固定拓扑控制器；强 API 只承担根因和通过补丁的语义选择。"""

    def __init__(
        self,
        model: ModelAdapter,
        *,
        generation_config: GenerationConfig,
        trace_writer: Any = None,
    ) -> None:
        self.model = model
        self.generation_config = generation_config
        self.trace_writer = trace_writer
        self.calls = 0

    def decide(self, snapshot: Mapping[str, Any]) -> SupervisorOutcome:
        self.calls += 1
        nodes = [dict(item) for item in snapshot.get("nodes", ())]
        artifacts = [dict(item) for item in snapshot.get("artifacts", ())]
        selections = dict(snapshot.get("selections", {}))
        node_types = Counter(str(item["node_type"]) for item in nodes)
        refs = _artifact_refs(artifacts)
        decision_id = f"FIXED-{snapshot['state_version']}-{self.calls}"

        if not nodes:
            return self._scripted(
                SupervisorDecision(
                    decision_id,
                    DecisionAction.CREATE_TASK,
                    "固定流水线并行执行代码检索和失败复现",
                    create_tasks=(
                        CreateTaskRequest(
                            "INVESTIGATION_TASK",
                            "InvestigatorAgent",
                            "code_retrieval",
                            "定位缺陷实现、关键控制流和对应代码行",
                            timeout_seconds=300,
                        ),
                        CreateTaskRequest(
                            "INVESTIGATION_TASK",
                            "InvestigatorAgent",
                            "failure_reproduction",
                            "执行受信目标测试并保存直接失败证据",
                            timeout_seconds=300,
                        ),
                    ),
                    next_workflow_stage="investigation",
                )
            )

        blocking = [
            item
            for item in nodes
            if item["node_type"] not in {"PATCH_TASK", "VALIDATION_TASK"}
            and item["status"] in {"FAILED", "TIMED_OUT", "BLOCKED"}
        ]
        if blocking:
            return self._terminate(decision_id, "固定流水线的必要上游节点失败")

        evidence_refs = refs["evidence"]
        if node_types["DIAGNOSIS_TASK"] == 0:
            investigation_ids = _succeeded_node_ids(nodes, "INVESTIGATION_TASK")
            if len(investigation_ids) != 2 or len(evidence_refs) != 2:
                return self._terminate(decision_id, "固定流水线没有获得两份调查证据")
            return self._scripted(
                SupervisorDecision(
                    decision_id,
                    DecisionAction.CREATE_TASK,
                    "固定流水线执行两个首轮上下文隔离的诊断视角",
                    create_tasks=(
                        CreateTaskRequest(
                            "DIAGNOSIS_TASK",
                            "DiagnosticianAgent",
                            "control_flow",
                            "根据 Evidence 从控制流和边界条件形成独立根因",
                            depends_on=tuple(investigation_ids),
                            input_artifact_ids=tuple(evidence_refs),
                            timeout_seconds=300,
                        ),
                        CreateTaskRequest(
                            "DIAGNOSIS_TASK",
                            "DiagnosticianAgent",
                            "data_flow",
                            "根据 Evidence 从数据流和状态不变量形成独立根因",
                            depends_on=tuple(investigation_ids),
                            input_artifact_ids=tuple(evidence_refs),
                            timeout_seconds=300,
                        ),
                    ),
                    next_workflow_stage="diagnosis",
                    evidence_refs=tuple(evidence_refs),
                    gate_record=GateRecord(
                        "fixed_protocol_second_diagnosis",
                        tuple(evidence_refs),
                        "固定基线预声明始终执行两个诊断实例",
                        2,
                        "新增两个本地 Worker 节点",
                    ),
                )
            )

        hypothesis_refs = refs["hypothesis"]
        if node_types["REVIEW_TASK"] == 0:
            diagnosis_ids = _succeeded_node_ids(nodes, "DIAGNOSIS_TASK")
            if len(diagnosis_ids) != 2 or len(hypothesis_refs) != 2:
                return self._terminate(decision_id, "固定流水线没有获得两份根因假设")
            return self._scripted(
                SupervisorDecision(
                    decision_id,
                    DecisionAction.CREATE_TASK,
                    "固定流水线审查两份根因与直接证据的一致性",
                    create_tasks=(
                        CreateTaskRequest(
                            "REVIEW_TASK",
                            "ReviewerAgent",
                            "hypothesis_comparison",
                            "比较两份独立根因并给出有证据约束的建议",
                            depends_on=tuple(diagnosis_ids),
                            input_artifact_ids=(*hypothesis_refs, *evidence_refs),
                            timeout_seconds=300,
                        ),
                    ),
                    next_workflow_stage="review",
                )
            )

        review_refs = refs["review"]
        if not selections.get("hypothesis_ref"):
            if not review_refs:
                return self._terminate(decision_id, "固定流水线没有获得根因审查")
            return self._semantic_selection(
                snapshot,
                expected_action=DecisionAction.ACCEPT_HYPOTHESIS,
                allowed_refs=set(hypothesis_refs),
                instruction=(
                    "这是固定流水线的根因选择步骤。只能输出 ACCEPT_HYPOTHESIS，"
                    "hypothesis_ref 必须从快照现有 Hypothesis 中选择；不得创建、取消或重规划节点。"
                ),
            )

        patch_refs = refs["patch_candidate"]
        if node_types["PATCH_TASK"] == 0:
            selected = str(selections["hypothesis_ref"])
            inputs = (selected, *evidence_refs, *review_refs)
            return self._scripted(
                SupervisorDecision(
                    decision_id,
                    DecisionAction.CREATE_TASK,
                    "固定流水线始终生成 Minimal 和 Robust 两个候选",
                    create_tasks=(
                        CreateTaskRequest(
                            "PATCH_TASK",
                            "PatchAgent",
                            "minimal",
                            "依据已选根因生成最小候选补丁",
                            input_artifact_ids=inputs,
                            timeout_seconds=420,
                        ),
                        CreateTaskRequest(
                            "PATCH_TASK",
                            "PatchAgent",
                            "robust",
                            "依据已选根因生成覆盖相邻边界的稳健候选补丁",
                            input_artifact_ids=inputs,
                            timeout_seconds=420,
                        ),
                    ),
                    next_workflow_stage="patch",
                    evidence_refs=(selected, *review_refs),
                    gate_record=GateRecord(
                        "fixed_protocol_dual_patch",
                        (selected, *review_refs),
                        "固定基线预声明始终生成两个候选补丁",
                        2,
                        "新增两个本地 Patch Worker 节点",
                    ),
                )
            )

        if node_types["VALIDATION_TASK"] == 0:
            patch_nodes = _succeeded_node_ids(nodes, "PATCH_TASK")
            if not patch_refs:
                return self._terminate(decision_id, "固定流水线没有生成可验证补丁")
            requests = tuple(
                CreateTaskRequest(
                    "VALIDATION_TASK",
                    "ValidationExecutor",
                    "deterministic",
                    "在独立候选工作区执行目标、回归、静态和受保护路径检查",
                    depends_on=(patch_node,),
                    input_artifact_ids=(patch_ref,),
                    timeout_seconds=180,
                )
                for patch_node, patch_ref in zip(patch_nodes, patch_refs, strict=True)
            )
            return self._scripted(
                SupervisorDecision(
                    decision_id,
                    DecisionAction.CREATE_TASK,
                    "固定流水线独立验证全部成功生成的候选",
                    create_tasks=requests,
                    next_workflow_stage="validation",
                )
            )

        validation_items = [
            item
            for item in artifacts
            if item.get("artifact_type") == "validation_result"
            and bool(item.get("content", {}).get("passed"))
        ]
        if not validation_items:
            return self._terminate(decision_id, "固定流水线没有通过完整验证的候选")
        allowed = {str(item["artifact_ref"]) for item in validation_items}
        allowed.update(str(item["content"]["patch_ref"]) for item in validation_items)
        return self._semantic_selection(
            snapshot,
            expected_action=DecisionAction.FINALIZE_TASK,
            allowed_refs=allowed,
            instruction=(
                "这是固定流水线的最终补丁选择步骤。只能输出 FINALIZE_TASK，patch_ref 与"
                "validation_ref 必须来自同一个 passed=true 的 ValidationResult；不得创建或重规划节点。"
            ),
        )

    def _semantic_selection(
        self,
        snapshot: Mapping[str, Any],
        *,
        expected_action: DecisionAction,
        allowed_refs: set[str],
        instruction: str,
    ) -> SupervisorOutcome:
        prompt = (
            "你是 Fixed Hybrid Pipeline 的强模型语义选择器，不控制固定拓扑。"
            "只输出符合 SupervisorDecision Schema 的 JSON。" + instruction
        )
        outcome = SupervisorAgent(
            self.model,
            generation_config=self.generation_config,
            trace_writer=self.trace_writer,
            system_prompt=prompt,
        ).decide(snapshot)
        decision = outcome.decision
        selected = {
            ref
            for ref in (decision.hypothesis_ref, decision.patch_ref, decision.validation_ref)
            if ref
        }
        if decision.action is not expected_action or not selected.issubset(allowed_refs):
            raise SupervisorAgentError(
                "FIXED_SELECTION_POLICY_VIOLATION",
                f"固定选择器返回了不允许的决策：{decision.action.value}",
                usage=TokenUsage(outcome.input_tokens, outcome.output_tokens),
            )
        return outcome

    @staticmethod
    def _scripted(decision: SupervisorDecision) -> SupervisorOutcome:
        return SupervisorOutcome(decision, model_id="fixed-policy-controller")

    def _terminate(self, decision_id: str, reason: str) -> SupervisorOutcome:
        return self._scripted(
            SupervisorDecision(decision_id, DecisionAction.TERMINATE_TASK, reason)
        )


def constrained_dynamic_prompt(base_prompt: str, system_id: str) -> str:
    if system_id == "dynamic_hybrid":
        return base_prompt
    if system_id == "no_second_diagnostician":
        return (
            base_prompt
            + "\n消融约束：整个任务最多创建一个 DIAGNOSIS_TASK；不得以其他节点冒充第二诊断。"
        )
    if system_id == "no_challenge_rebuttal":
        return (
            base_prompt
            + "\n消融约束：禁止创建 CHALLENGE_TASK 和 REBUTTAL_TASK；其余动态决策保持不变。"
        )
    raise ValueError(f"不支持的动态系统：{system_id}")


def read_trace(path: str | Path) -> list[dict[str, Any]]:
    trace_path = Path(path)
    if not trace_path.is_file():
        return []
    return [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]


def raw_usage(root: str | Path, pricing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for path in sorted(Path(root).rglob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        usage = value.get("usage", {})
        records.append(
            {
                "model_id": str(value.get("model_id", "unknown")),
                "input_tokens": int(usage.get("input_tokens", 0)),
                "output_tokens": int(usage.get("output_tokens", 0)),
            }
        )
    by_model: dict[str, dict[str, Any]] = {}
    total_cost = 0.0
    for item in records:
        model = item["model_id"]
        entry = by_model.setdefault(
            model,
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_cny": 0.0},
        )
        entry["calls"] += 1
        entry["input_tokens"] += item["input_tokens"]
        entry["output_tokens"] += item["output_tokens"]
        cost = _request_cost(item, pricing or {})
        entry["estimated_cost_cny"] += cost
        total_cost += cost
    for entry in by_model.values():
        entry["estimated_cost_cny"] = round(entry["estimated_cost_cny"], 6)
    return {
        "calls": len(records),
        "input_tokens": sum(item["input_tokens"] for item in records),
        "output_tokens": sum(item["output_tokens"] for item in records),
        "total_tokens": sum(item["input_tokens"] + item["output_tokens"] for item in records),
        "estimated_cost_cny": round(total_cost, 6),
        "by_model": by_model,
    }


def trace_metrics(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    route_usage: Counter[str] = Counter()
    route_attempts_by_model: Counter[str] = Counter()
    route_successes_by_model: Counter[str] = Counter()
    route_exhausted_by_model: Counter[str] = Counter()
    latency_totals: Counter[str] = Counter()
    latency_calls: Counter[str] = Counter()
    node_stages: dict[str, str] = {}
    latest_supervisor_stage = "unknown"
    contract_rejection_pending = False
    contract_rejection_keys: set[tuple[str, str]] = set()
    node_rebuild_count = 0
    snapshot_original_chars = 0
    snapshot_compact_chars = 0
    snapshot_count = 0
    intervals: list[tuple[datetime, datetime]] = []
    for event in events:
        event_type = str(event.get("event_type", ""))
        data = event.get("data", {})
        if not isinstance(data, Mapping):
            continue
        if event_type.startswith("supervisor_route_"):
            route_usage[f"{event_type}:{data.get('model_id')}:{data.get('api_key_env')}"] += 1
            model_id = str(data.get("model_id", "unknown"))
            if event_type == "supervisor_route_attempt":
                route_attempts_by_model[model_id] += 1
            elif event_type == "supervisor_route_succeeded":
                route_successes_by_model[model_id] += 1
            elif event_type == "supervisor_route_exhausted":
                route_exhausted_by_model[model_id] += 1
        if event_type == "supervisor_snapshot_compacted":
            snapshot_count += 1
            snapshot_original_chars += int(data.get("original_chars", 0))
            snapshot_compact_chars += int(data.get("compact_chars", 0))
        if event_type == "supervisor_schema_reduced":
            latest_supervisor_stage = str(data.get("workflow_stage", "unknown"))
        elif event_type == "supervisor_decision_generated":
            latency_totals[latest_supervisor_stage] += int(data.get("latency_ms", 0))
            latency_calls[latest_supervisor_stage] += 1
        if event_type == "agent_input_contract_checked" and data.get("node_id"):
            node_stages[str(data["node_id"])] = _stage_for_node_type(
                str(data.get("node_type", ""))
            )
        if event_type == "agent_input_contract_rejected":
            contract_rejection_pending = True
            identity = str(
                data.get("decision_id")
                or data.get("node_id")
                or event.get("event_id")
                or len(contract_rejection_keys)
            )
            contract_rejection_keys.add(("contract", identity))
        if (
            event_type == "supervisor_decision_rejected"
            and data.get("code") == "AGENT_INPUT_CONTRACT_VIOLATION"
        ):
            identity = str(
                data.get("decision_id")
                or event.get("event_id")
                or len(contract_rejection_keys)
            )
            contract_rejection_keys.add(("contract", identity))
        if event_type == "supervisor_decision_applied" and contract_rejection_pending:
            decision = data.get("decision", {})
            if isinstance(decision, Mapping) and decision.get("action") == "CREATE_TASK":
                tasks = decision.get("create_tasks", ())
                if isinstance(tasks, Sequence) and not isinstance(tasks, (str, bytes)):
                    node_rebuild_count += len(tasks)
                contract_rejection_pending = False
        if event_type == "worker_completed" and data.get("started_at") and data.get("finished_at"):
            intervals.append(
                (
                    datetime.fromisoformat(str(data["started_at"])),
                    datetime.fromisoformat(str(data["finished_at"])),
                )
            )
            stage = node_stages.get(str(data.get("node_id", "")), "unknown")
            latency_totals[stage] += int(data.get("duration_ms", 0))
            latency_calls[stage] += 1
    overlap_pairs = sum(
        max(first[0], second[0]) < min(first[1], second[1])
        for index, first in enumerate(intervals)
        for second in intervals[index + 1 :]
    )
    decisions = [
        event["data"]["decision"]
        for event in events
        if event.get("event_type") == "supervisor_decision_applied"
        and isinstance(event.get("data"), Mapping)
        and isinstance(event["data"].get("decision"), Mapping)
    ]
    gates = [item["gate_record"] for item in decisions if "gate_record" in item]
    return {
        "tool_calls": sum(event.get("event_type") == "tool_call" for event in events),
        "invalid_decisions": sum(
            event.get("event_type") == "supervisor_decision_rejected"
            and isinstance(event.get("data"), Mapping)
            # Engine 与 Runtime 都会为同一次拒绝写事件；只有 Engine 事件携带
            # 规范化错误码，因此以它作为一次决策拒绝的唯一计数来源。
            and "code" in event["data"]
            for event in events
        ),
        "route_usage": dict(sorted(route_usage.items())),
        "route_attempts_by_model": dict(sorted(route_attempts_by_model.items())),
        "route_successes_by_model": dict(sorted(route_successes_by_model.items())),
        "route_exhausted_by_model": dict(sorted(route_exhausted_by_model.items())),
        "worker_retry_count": sum(
            event.get("event_type") == "deterministic_worker_retry_scheduled"
            for event in events
        ),
        "contract_rejection_count": len(contract_rejection_keys),
        "node_rebuild_count": node_rebuild_count,
        "supervisor_format_repair_count": sum(
            event.get("event_type") == "supervisor_format_repair_attempted"
            for event in events
        ),
        "supervisor_snapshot_count": snapshot_count,
        "supervisor_snapshot_original_chars": snapshot_original_chars,
        "supervisor_snapshot_compact_chars": snapshot_compact_chars,
        "supervisor_snapshot_reduction_ratio": (
            1.0 - snapshot_compact_chars / snapshot_original_chars
            if snapshot_original_chars
            else None
        ),
        "latency_by_stage": {
            stage: {
                "calls": latency_calls[stage],
                "total_ms": total_ms,
                "average_ms": total_ms / latency_calls[stage],
            }
            for stage, total_ms in sorted(latency_totals.items())
            if latency_calls[stage]
        },
        "parallel_overlap_pairs": overlap_pairs,
        "gate_records": gates,
    }


def _stage_for_node_type(node_type: str) -> str:
    return {
        "INVESTIGATION_TASK": "investigation",
        "DIAGNOSIS_TASK": "diagnosis",
        "CHALLENGE_TASK": "diagnosis",
        "REBUTTAL_TASK": "diagnosis",
        "REVIEW_TASK": "review",
        "PATCH_TASK": "patch",
        "VALIDATION_TASK": "validation",
    }.get(node_type, "unknown")


def budget_outcomes_fail_closed(results: Sequence[Mapping[str, Any]]) -> bool:
    """预算内可成功，预算外必须作为失败保留，二者都属于有效实验结果。"""

    return all(
        item.get("budget", {}).get("within_budget", False) or item.get("status") != "succeeded"
        for item in results
    )


def hybrid_mechanism_metrics(
    nodes: Sequence[Mapping[str, Any]],
    artifacts: Sequence[Mapping[str, Any]],
    *,
    succeeded: bool,
    expected_complexity: str,
) -> dict[str, Any]:
    path = classify_execution_path(nodes).value
    types = Counter(str(item.get("artifact_type")) for item in artifacts)
    revised = sum(
        item.get("artifact_type") == "hypothesis" and int(item.get("version", 1)) > 1
        for item in artifacts
    )
    replans = types["replan_record"]
    return {
        "execution_path": path,
        "valid_challenges": types["challenge"],
        "challenge_nodes": sum(item.get("node_type") == "CHALLENGE_TASK" for item in nodes),
        "critique_induced_corrections": revised,
        "replan_attempted": replans > 0,
        "replan_recovered": bool(replans and succeeded),
        "unnecessary_expansion": bool(
            expected_complexity == "simple" and path != ExecutionPath.FAST.value
        ),
    }


def aggregate_system_results(
    system_id: str,
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not results:
        raise ValueError("系统汇总至少需要一个任务结果")
    solved = sum(item.get("status") == "succeeded" for item in results)
    total = len(results)
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
        key: sum(int(item.get("usage", {}).get(key, 0)) for item in results) for key in usage_fields
    }
    total_cost = round(
        sum(float(item.get("usage", {}).get("estimated_api_cost_cny", 0.0)) for item in results),
        6,
    )
    paths = Counter(
        str(item.get("mechanism", {}).get("execution_path", "single")) for item in results
    )
    return {
        "system_id": system_id,
        "task_count": total,
        "solved": solved,
        "failed": total - solved,
        "task_resolution_rate": solved / total,
        "target_test_pass_rate": _rate(results, "target_test_passed"),
        "regression_pass_rate": _rate(results, "regression_passed"),
        "patch_apply_rate": _rate(results, "patch_applied"),
        "syntax_valid_rate": _rate(results, "syntax_valid"),
        "protected_path_violations": sum(
            int(item.get("validation", {}).get("protected_path_violations", 0)) for item in results
        ),
        "budget_violations": sum(
            not item.get("budget", {}).get("within_budget", False) for item in results
        ),
        "totals": totals,
        "median_latency_ms": int(
            statistics.median(int(item.get("usage", {}).get("duration_ms", 0)) for item in results)
        ),
        "estimated_api_cost_cny": total_cost,
        "estimated_api_cost_per_solved_cny": round(total_cost / solved, 6) if solved else None,
        "route_distribution": dict(sorted(paths.items())),
        "valid_challenges": sum(
            int(item.get("mechanism", {}).get("valid_challenges", 0)) for item in results
        ),
        "critique_induced_corrections": sum(
            int(item.get("mechanism", {}).get("critique_induced_corrections", 0))
            for item in results
        ),
        "replan_attempts": sum(
            bool(item.get("mechanism", {}).get("replan_attempted")) for item in results
        ),
        "replan_recoveries": sum(
            bool(item.get("mechanism", {}).get("replan_recovered")) for item in results
        ),
        "unnecessary_expansions": sum(
            bool(item.get("mechanism", {}).get("unnecessary_expansion")) for item in results
        ),
        "parallel_overlap_pairs": sum(
            int(item.get("mechanism", {}).get("parallel_overlap_pairs", 0)) for item in results
        ),
        "task_result_paths": [str(item["result_path"]) for item in results],
    }


def tree_digest(root: str | Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _artifact_refs(artifacts: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {
        "evidence": [],
        "hypothesis": [],
        "review": [],
        "patch_candidate": [],
        "validation_result": [],
    }
    for item in artifacts:
        artifact_type = str(item.get("artifact_type"))
        if artifact_type in result:
            result[artifact_type].append(str(item["artifact_ref"]))
    return result


def _succeeded_node_ids(nodes: Sequence[Mapping[str, Any]], node_type: str) -> list[str]:
    return [
        str(item["node_id"])
        for item in nodes
        if item.get("node_type") == node_type and item.get("status") == "SUCCEEDED"
    ]


def _request_cost(item: Mapping[str, Any], pricing: Mapping[str, Any]) -> float:
    model = str(item["model_id"])
    model_prices = pricing.get(model)
    if not isinstance(model_prices, Sequence) or isinstance(model_prices, (str, bytes)):
        return 0.0
    input_tokens = int(item["input_tokens"])
    output_tokens = int(item["output_tokens"])
    for tier in model_prices:
        if not isinstance(tier, Mapping):
            continue
        if input_tokens <= int(tier["max_input_tokens"]):
            return (
                input_tokens * float(tier["input_cny_per_million"])
                + output_tokens * float(tier["output_cny_per_million"])
            ) / 1_000_000
    return 0.0


def _rate(results: Sequence[Mapping[str, Any]], field: str) -> float:
    return sum(bool(item.get("validation", {}).get(field)) for item in results) / len(results)


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("expected a sequence of strings")
    return tuple(str(item) for item in value)


__all__ = [
    "ABLATION_SYSTEMS",
    "ALL_SYSTEMS",
    "MAIN_SYSTEMS",
    "EvaluationSuite",
    "FixedPipelineSupervisor",
    "aggregate_system_results",
    "constrained_dynamic_prompt",
    "hybrid_mechanism_metrics",
    "load_evaluation_suite",
    "raw_usage",
    "read_trace",
    "trace_metrics",
    "tree_digest",
]
