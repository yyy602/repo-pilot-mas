"""运行真实本地 Qwen3-8B Phase 4 Worker 池并保存验收证据。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repo_pilot_mas.agents import SupervisorOutcome, WorkerPool
from repo_pilot_mas.config import load_yaml
from repo_pilot_mas.models import create_worker_model_pool
from repo_pilot_mas.orchestration import (
    AsyncLangGraphRuntime,
    EngineBudget,
    NodeStatus,
    OrchestrationEngine,
    restore_engine_from_runtime_state,
)
from repo_pilot_mas.runtime import TraceWriter, WorkspaceManager
from repo_pilot_mas.schemas import (
    CreateTaskRequest,
    DecisionAction,
    SupervisorDecision,
    validate_worker_artifact,
)
from repo_pilot_mas.tasks import load_task
from repo_pilot_mas.tools import run_tests
from repo_pilot_mas.tools.registry import task_test_command


class Phase4AcceptanceSupervisor:
    """Deterministic topology so Phase 4 evaluates Workers rather than planner variance."""

    def decide(self, snapshot: Mapping[str, Any]) -> SupervisorOutcome:
        nodes = snapshot["nodes"]
        artifacts = snapshot["artifacts"]
        failed = [node for node in nodes if node["status"] != NodeStatus.SUCCEEDED.value]
        if nodes and failed:
            decision = SupervisorDecision(
                f"P4-D-failed-{len(nodes)}",
                DecisionAction.TERMINATE_TASK,
                "Phase 4 Worker 出现非成功终态，停止扩展任务图并保留失败证据",
            )
        elif not nodes:
            decision = SupervisorDecision(
                "P4-D1-investigate",
                DecisionAction.CREATE_TASK,
                "并行获取代码与失败复现证据",
                create_tasks=(
                    CreateTaskRequest(
                        "INVESTIGATION_TASK",
                        "InvestigatorAgent",
                        "code_retrieval",
                        "定位缺陷实现、返回条件及对应行号",
                        timeout_seconds=360,
                    ),
                    CreateTaskRequest(
                        "INVESTIGATION_TASK",
                        "InvestigatorAgent",
                        "failure_reproduction",
                        "运行固定目标测试并提取可复现失败证据",
                        timeout_seconds=360,
                    ),
                ),
                next_workflow_stage="investigation",
            )
        elif len(nodes) == 2:
            evidence_refs = _refs(artifacts, "evidence")
            decision = SupervisorDecision(
                "P4-D2-diagnose",
                DecisionAction.CREATE_TASK,
                "基于同一组 Evidence 启动两个互不读取对方结论的诊断实例",
                create_tasks=(
                    CreateTaskRequest(
                        "DIAGNOSIS_TASK",
                        "DiagnosticianAgent",
                        "control_flow",
                        "从控制流和边界条件独立形成根因假设",
                        depends_on=("N1", "N2"),
                        input_artifact_ids=evidence_refs,
                        timeout_seconds=300,
                    ),
                    CreateTaskRequest(
                        "DIAGNOSIS_TASK",
                        "DiagnosticianAgent",
                        "data_flow",
                        "从数据流、状态不变量和接口契约独立形成根因假设",
                        depends_on=("N1", "N2"),
                        input_artifact_ids=evidence_refs,
                        timeout_seconds=300,
                    ),
                ),
                next_workflow_stage="diagnosis",
            )
        elif len(nodes) == 4:
            evidence_refs = _refs(artifacts, "evidence")
            hypothesis_refs = _refs(artifacts, "hypothesis")
            decision = SupervisorDecision(
                "P4-D3-review",
                DecisionAction.CREATE_TASK,
                "审查目标根因与直接证据的一致性",
                create_tasks=(
                    CreateTaskRequest(
                        "REVIEW_TASK",
                        "ReviewerAgent",
                        "root_cause_recommendation",
                        "引用目标 Hypothesis 和 Evidence 给出根因建议",
                        depends_on=("N3", "N4"),
                        input_artifact_ids=(hypothesis_refs[0], *evidence_refs),
                        timeout_seconds=300,
                    ),
                ),
                next_workflow_stage="review",
            )
        elif len(nodes) == 5:
            evidence_refs = _refs(artifacts, "evidence")
            hypothesis_refs = _refs(artifacts, "hypothesis")
            decision = SupervisorDecision(
                "P4-D4-patch",
                DecisionAction.CREATE_TASK,
                "在两个隔离工作区生成 Minimal 与 Robust 候选补丁",
                create_tasks=(
                    CreateTaskRequest(
                        "PATCH_TASK",
                        "PatchAgent",
                        "minimal",
                        "根据证据支持的根因生成最小候选补丁",
                        depends_on=("N5",),
                        input_artifact_ids=(hypothesis_refs[0], *evidence_refs),
                        timeout_seconds=420,
                    ),
                    CreateTaskRequest(
                        "PATCH_TASK",
                        "PatchAgent",
                        "robust",
                        "根据证据支持的根因生成稳健候选补丁",
                        depends_on=("N5",),
                        input_artifact_ids=(*hypothesis_refs, *evidence_refs),
                        timeout_seconds=420,
                    ),
                ),
                next_workflow_stage="patch",
            )
        else:
            decision = SupervisorDecision(
                "P4-D5-stop",
                DecisionAction.TERMINATE_TASK,
                "Phase 4 Worker 验收拓扑已执行完成",
            )
        return SupervisorOutcome(decision, model_id="phase4-acceptance-supervisor")


def _refs(artifacts: Any, artifact_type: str) -> tuple[str, ...]:
    return tuple(
        str(item["artifact_ref"])
        for item in artifacts
        if item["artifact_type"] == artifact_type
    )


async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    task = load_task(args.task)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_root = Path(args.report_root).expanduser().resolve(strict=False) / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    trace = TraceWriter(run_root / "trace.jsonl")
    model_config = load_yaml(args.model_config)
    models, generation = create_worker_model_pool(
        model_config,
        raw_log_dir=run_root / "raw_model_responses",
    )
    if len(models) < 2:
        raise RuntimeError("Phase 4 并发验收至少需要两个本地模型槽位")
    runtime_config = load_yaml(args.runtime_config)
    orchestration = runtime_config.get("orchestration", {})
    langgraph = runtime_config.get("langgraph", {})
    if not isinstance(orchestration, Mapping) or not isinstance(langgraph, Mapping):
        raise TypeError("runtime 配置缺少 orchestration 或 langgraph mapping")
    budget_values = dict(orchestration)
    budget_values["max_runtime_seconds"] = max(
        float(budget_values.get("max_runtime_seconds", 900)), 1800
    )
    source_digest_before = _tree_digest(task.repository_path)
    workspace_root = run_root / "workspaces"
    pool = WorkerPool(
        models,
        workspace_root=workspace_root,
        trace_writer=trace,
        generation_config=generation,
    )
    pool.prepare()
    engine = OrchestrationEngine(task, budget=EngineBudget(**budget_values), trace_writer=trace)
    thread_id = f"{task.task_id}-{run_id}"
    runtime = await AsyncLangGraphRuntime.create(
        Phase4AcceptanceSupervisor(),
        pool,
        run_root / "langgraph.sqlite3",
        trace_writer=trace,
        recursion_limit=int(langgraph.get("recursion_limit", 128)),
    )
    async with runtime:
        state = await runtime.arun(engine, thread_id=thread_id)
        history_length = len(
            await runtime.astate_history(thread_id=thread_id, task_id=task.task_id)
        )
    restored = restore_engine_from_runtime_state(state)
    all_artifacts = tuple(restored.blackboard.artifacts.values())
    schema_errors: list[str] = []
    for artifact in all_artifacts:
        try:
            validate_worker_artifact(artifact)
        except (TypeError, ValueError) as exc:
            schema_errors.append(f"{artifact.ref}: {exc}")

    events = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    model_intervals = _model_intervals(events)
    investigator_overlap = _overlap(model_intervals.get("N1"), model_intervals.get("N2"))
    contexts = {
        event["data"]["node_id"]: event["data"]
        for event in events
        if event["event_type"] == "worker_context_built"
    }
    diagnosis_isolated = all(
        contexts.get(node_id, {}).get("input_types") == ["evidence", "evidence"]
        for node_id in ("N3", "N4")
    )
    review = next(
        (item for item in all_artifacts if item.artifact_type.value == "review"),
        None,
    )
    reviewer_citations = bool(
        review
        and review.content.get("target_artifact_ref") in review.input_refs
        and set(review.content.get("evidence_refs", ())).issubset(review.input_refs)
    )
    patches = [item for item in all_artifacts if item.artifact_type.value == "patch_candidate"]
    patch_validations = []
    manager = WorkspaceManager(task.repository_path, workspace_root)
    for patch in patches:
        workspace = manager.open(task.task_id, str(patch.content["workspace_id"]))
        result = run_tests(
            workspace.root,
            command=task_test_command(task, target=True),
            timeout_seconds=task.max_runtime_seconds,
        )
        patch_validations.append(
            {
                "patch_ref": patch.ref,
                "workspace_id": workspace.workspace_id,
                "workspace_root": str(workspace.root),
                "baseline_digest": workspace.baseline_digest,
                "target_test": result.to_dict(),
            }
        )
    workspace_ids = [str(item.content["workspace_id"]) for item in patches]
    workspace_isolation = bool(
        len(patches) == 2
        and len(set(workspace_ids)) == 2
        and len({item["workspace_root"] for item in patch_validations}) == 2
        and len({item["baseline_digest"] for item in patch_validations}) == 1
        and source_digest_before == _tree_digest(task.repository_path)
    )
    statuses = {node.node_id: node.status.value for node in restored.graph.nodes}
    collector_before_supervisor = _collector_before_last_supervisor(events)
    gates = {
        "seven_workers_succeeded": len(statuses) == 7
        and set(statuses.values()) == {NodeStatus.SUCCEEDED.value},
        "investigator_real_overlap": investigator_overlap,
        "diagnosis_first_round_isolated": diagnosis_isolated,
        "reviewer_cites_target_and_evidence": reviewer_citations,
        "two_patch_workspaces_isolated": workspace_isolation,
        "all_artifacts_schema_valid": len(all_artifacts) == 7 and not schema_errors,
        "collector_wakes_supervisor": collector_before_supervisor,
        "patch_target_tests_pass": len(patch_validations) == 2
        and all(item["target_test"]["ok"] for item in patch_validations),
    }
    report = {
        "phase": 4,
        "run_id": run_id,
        "task_id": task.task_id,
        "runtime_status": state["runtime_status"],
        "thread_id": thread_id,
        "history_length": history_length,
        "worker_models": [
            {
                "model_id": model.model_id,
                "device": getattr(model, "device", None),
            }
            for model in models
        ],
        "nodes": [node.to_dict() for node in restored.graph.nodes],
        "artifacts": [artifact.to_dict() for artifact in all_artifacts],
        "model_intervals": model_intervals,
        "diagnosis_contexts": {key: contexts.get(key) for key in ("N3", "N4")},
        "patch_validations": patch_validations,
        "schema_errors": schema_errors,
        "gates": gates,
        "trace_path": str(trace.path),
        "checkpoint_db": str(run_root / "langgraph.sqlite3"),
    }
    report_path = run_root / "acceptance.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary_path = Path(args.report_root).expanduser().resolve(strict=False) / "latest_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "phase": 4,
                "run_id": run_id,
                "passed": all(gates.values()),
                "gates": gates,
                "report_path": str(report_path),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return report, all(gates.values())


def _model_intervals(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for event in events:
        data = event.get("data", {})
        node_id = data.get("node_id")
        if not isinstance(node_id, str):
            continue
        if event["event_type"] == "worker_model_slot_acquired":
            values[node_id] = {
                "started_at": data["started_at"],
                "slot": data["slot"],
                "device": data["device"],
            }
        elif event["event_type"] == "worker_model_slot_released" and node_id in values:
            values[node_id]["finished_at"] = data["finished_at"]
            values[node_id]["duration_ms"] = data["duration_ms"]
    return values


def _overlap(first: Mapping[str, Any] | None, second: Mapping[str, Any] | None) -> bool:
    if not first or not second or "finished_at" not in first or "finished_at" not in second:
        return False
    first_start = datetime.fromisoformat(str(first["started_at"]))
    first_end = datetime.fromisoformat(str(first["finished_at"]))
    second_start = datetime.fromisoformat(str(second["started_at"]))
    second_end = datetime.fromisoformat(str(second["finished_at"]))
    return max(first_start, second_start) < min(first_end, second_end)


def _collector_before_last_supervisor(events: list[dict[str, Any]]) -> bool:
    collector_indices = [
        index for index, event in enumerate(events) if event["event_type"] == "worker_artifacts_collected"
    ]
    supervisor_indices = [
        index for index, event in enumerate(events) if event["event_type"] == "supervisor_decision_applied"
    ]
    return bool(collector_indices and supervisor_indices and collector_indices[-1] < supervisor_indices[-1])


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task",
        default="data/quixbugs/tasks/quixbugs_is_valid_parenthesization.json",
    )
    parser.add_argument("--model-config", default="configs/model.yaml")
    parser.add_argument("--runtime-config", default="configs/runtime.yaml")
    parser.add_argument("--report-root", default="reports/phase4/acceptance")
    args = parser.parse_args()
    report, passed = asyncio.run(_run(args))
    print(json.dumps({"passed": passed, "gates": report["gates"]}, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
