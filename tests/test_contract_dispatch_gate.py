from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from repo_pilot_mas.orchestration import (
    LangGraphRuntime,
    NodeStatus,
    NodeType,
    OrchestrationEngine,
    TaskNode,
    WorkerOutcome,
    restore_engine_from_runtime_state,
)
from repo_pilot_mas.runtime import TraceWriter
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec


class _NeverCalledSupervisor:
    def decide(self, snapshot: Mapping[str, Any]) -> Any:
        del snapshot
        raise AssertionError("dispatch-gate tests must not invoke Supervisor")


class _RecordingWorker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        del task
        node_id = str(node["node_id"])
        self.calls.append(node_id)
        refs = tuple(_artifact_ref(item) for item in artifacts)
        by_type = {str(item["artifact_type"]): item for item in artifacts}
        node_type = NodeType(str(node["node_type"]))

        if node_type is NodeType.REVIEW_TASK:
            hypothesis_ref = _artifact_ref(by_type[ArtifactType.HYPOTHESIS.value])
            evidence_ref = _artifact_ref(by_type[ArtifactType.EVIDENCE.value])
            artifact = Artifact(
                artifact_id=f"{node_id}.review",
                artifact_type=ArtifactType.REVIEW,
                created_by=node_id,
                input_refs=refs,
                content={
                    "mode": str(node["mode"]),
                    "target_artifact_ref": hypothesis_ref,
                    "evidence_refs": [evidence_ref],
                    "verdict": "supported",
                    "findings": ["输入证据支持当前根因假设"],
                    "risk_notes": [],
                    "recommendation": "进入后续修复阶段",
                },
            )
        elif node_type is NodeType.DIAGNOSIS_TASK:
            evidence_ref = _artifact_ref(by_type[ArtifactType.EVIDENCE.value])
            artifact = Artifact(
                artifact_id=f"{node_id}.hypothesis",
                artifact_type=ArtifactType.HYPOTHESIS,
                created_by=node_id,
                input_refs=refs,
                content={
                    "perspective": "control_flow",
                    "root_cause": "目标分支使用了错误的边界判断",
                    "direct_cause": "条件表达式未覆盖边界输入",
                    "supporting_evidence": [evidence_ref],
                    "counter_evidence": [],
                    "affected_symbols": ["target_function"],
                    "verification_plan": ["重新运行目标测试"],
                    "missing_evidence": [],
                    "confidence": 0.9,
                },
            )
        else:
            raise AssertionError(f"unexpected node type: {node_type.value}")

        return WorkerOutcome(node_id, NodeStatus.SUCCEEDED, (artifact,))


def _task(tmp_path: Path, task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        repository_path=tmp_path,
        issue="验证 Agent 输入契约在 Worker 调用前生效",
        acceptance_criteria=("非法输入不得调用 Worker",),
    )


def _artifact_ref(value: Mapping[str, Any]) -> str:
    return f"{value['artifact_id']}@v{value['version']}"


def _add_hypothesis(engine: OrchestrationEngine) -> str:
    return engine.add_artifact(
        Artifact(
            artifact_id="seed.hypothesis",
            artifact_type=ArtifactType.HYPOTHESIS,
            created_by="seed",
            content={"summary": "候选根因"},
        )
    )


def _add_evidence(engine: OrchestrationEngine) -> str:
    return engine.add_artifact(
        Artifact(
            artifact_id="seed.evidence",
            artifact_type=ArtifactType.EVIDENCE,
            created_by="seed",
            content={"summary": "直接代码证据"},
        )
    )


def _add_review_node(
    engine: OrchestrationEngine,
    *,
    node_id: str,
    input_refs: tuple[str, ...],
) -> None:
    engine.graph.add_node(
        TaskNode(
            node_id=node_id,
            node_type=NodeType.REVIEW_TASK,
            agent_type="ReviewerAgent",
            mode="root_cause_recommendation",
            objective="审查根因假设与直接证据是否一致",
            input_artifact_ids=input_refs,
        )
    )


def _add_diagnosis_node(
    engine: OrchestrationEngine,
    *,
    node_id: str,
    evidence_ref: str,
) -> None:
    engine.graph.add_node(
        TaskNode(
            node_id=node_id,
            node_type=NodeType.DIAGNOSIS_TASK,
            agent_type="DiagnosticianAgent",
            mode="control_flow",
            objective="基于直接证据生成控制流根因假设",
            input_artifact_ids=(evidence_ref,),
        )
    )


def _events(path: Path, event_type: str) -> list[Mapping[str, Any]]:
    return [
        payload["data"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if (payload := json.loads(line))["event_type"] == event_type
    ]


def _merge_runtime_state(
    state: Mapping[str, Any],
    update: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply a LangGraph node update to the full state used by direct-node tests."""

    merged = dict(state)
    for key, value in update.items():
        if key == "worker_results":
            existing = merged.get("worker_results", {})
            if not isinstance(existing, Mapping) or not isinstance(value, Mapping):
                raise TypeError("worker_results updates must be mappings")
            worker_results = dict(existing)
            worker_results.update(value)
            merged[key] = worker_results
        else:
            merged[key] = value
    return merged


def _prepare_dispatch_state(
    runtime: LangGraphRuntime,
    state: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    update = runtime._prepare_dispatch_node(state, config)
    return _merge_runtime_state(state, update)


def _collect_single_worker(
    runtime: LangGraphRuntime,
    prepared: Mapping[str, Any],
    config: Mapping[str, Any],
) -> Mapping[str, Any]:
    sends = runtime._route_prepared_dispatch(prepared)
    assert isinstance(sends, list)
    assert len(sends) == 1
    worker_update = runtime._worker_node(sends[0].arg)
    collector_state = _merge_runtime_state(prepared, worker_update)
    collector_update = runtime._collector_node(collector_state, config)
    return _merge_runtime_state(collector_state, collector_update)


def test_invalid_reviewer_is_rejected_before_worker_execution(tmp_path: Path) -> None:
    engine = OrchestrationEngine(_task(tmp_path, "contract-invalid"))
    hypothesis_ref = _add_hypothesis(engine)
    _add_review_node(
        engine,
        node_id="review-missing-evidence",
        input_refs=(hypothesis_ref,),
    )
    worker = _RecordingWorker()
    trace_path = tmp_path / "contract-invalid.jsonl"

    with LangGraphRuntime(
        _NeverCalledSupervisor(),
        worker,
        tmp_path / "contract-invalid.sqlite3",
        trace_writer=TraceWriter(trace_path),
    ) as runtime:
        state = runtime._initial_state(engine, "contract-invalid-thread", False)
        config = runtime._config("contract-invalid-thread")
        prepared = _prepare_dispatch_state(runtime, state, config)
        route = runtime._route_prepared_dispatch(prepared)

    assert route == "supervisor"
    assert worker.calls == []
    assert prepared["pending_worker_ids"] == []
    assert prepared["dispatch_id"] is None

    restored = restore_engine_from_runtime_state(prepared)
    node = restored.graph.get("review-missing-evidence")
    assert node.status is NodeStatus.FAILED
    assert node.retry_count == 0
    assert len(node.output_artifact_ids) == 1
    rejection = restored.blackboard.artifacts.get(node.output_artifact_ids[0])
    assert rejection.artifact_type is ArtifactType.ARTIFACT_REJECTION
    assert rejection.content["code"] == "AGENT_INPUT_CONTRACT_VIOLATION"
    assert set(rejection.content["allowed_next_actions"]) == {
        "CREATE_TASK",
        "REQUEST_REPLAN",
    }

    checked = _events(trace_path, "agent_input_contract_checked")
    rejected = _events(trace_path, "agent_input_contract_rejected")
    skipped = _events(trace_path, "worker_dispatch_skipped")
    assert len(checked) == 1
    assert checked[0]["passed"] is False
    assert checked[0]["input_artifact_types"] == ["hypothesis"]
    assert len(rejected) == 1
    assert rejected[0]["recovery_class"] == "input_contract"
    assert rejected[0]["recovery_action"] == "return_to_supervisor"
    assert rejected[0]["retry_scheduled"] is False
    assert rejected[0]["rejection_ref"] == rejection.ref
    assert len(skipped) == 1


def test_valid_reviewer_is_dispatched_and_collected(tmp_path: Path) -> None:
    engine = OrchestrationEngine(_task(tmp_path, "contract-valid"))
    hypothesis_ref = _add_hypothesis(engine)
    evidence_ref = _add_evidence(engine)
    _add_review_node(
        engine,
        node_id="review-complete-inputs",
        input_refs=(hypothesis_ref, evidence_ref),
    )
    worker = _RecordingWorker()
    trace_path = tmp_path / "contract-valid.jsonl"

    with LangGraphRuntime(
        _NeverCalledSupervisor(),
        worker,
        tmp_path / "contract-valid.sqlite3",
        trace_writer=TraceWriter(trace_path),
    ) as runtime:
        state = runtime._initial_state(engine, "contract-valid-thread", False)
        config = runtime._config("contract-valid-thread")
        prepared = _prepare_dispatch_state(runtime, state, config)
        collected = _collect_single_worker(runtime, prepared, config)

    assert worker.calls == ["review-complete-inputs"]
    assert prepared["pending_worker_ids"] == ["review-complete-inputs"]
    restored = restore_engine_from_runtime_state(collected)
    node = restored.graph.get("review-complete-inputs")
    assert node.status is NodeStatus.SUCCEEDED
    assert len(node.output_artifact_ids) == 1
    review = restored.blackboard.artifacts.get(node.output_artifact_ids[0])
    assert review.artifact_type is ArtifactType.REVIEW

    checked = _events(trace_path, "agent_input_contract_checked")
    rejected = _events(trace_path, "agent_input_contract_rejected")
    assert len(checked) == 1
    assert checked[0]["passed"] is True
    assert set(checked[0]["input_artifact_types"]) == {
        "evidence",
        "hypothesis",
    }
    assert rejected == []


def test_mixed_dispatch_rejects_invalid_node_without_blocking_valid_node(
    tmp_path: Path,
) -> None:
    engine = OrchestrationEngine(_task(tmp_path, "contract-mixed"))
    hypothesis_ref = _add_hypothesis(engine)
    evidence_ref = _add_evidence(engine)
    _add_review_node(
        engine,
        node_id="review-invalid",
        input_refs=(hypothesis_ref,),
    )
    _add_diagnosis_node(
        engine,
        node_id="diagnosis-valid",
        evidence_ref=evidence_ref,
    )
    worker = _RecordingWorker()
    trace_path = tmp_path / "contract-mixed.jsonl"

    with LangGraphRuntime(
        _NeverCalledSupervisor(),
        worker,
        tmp_path / "contract-mixed.sqlite3",
        trace_writer=TraceWriter(trace_path),
    ) as runtime:
        state = runtime._initial_state(engine, "contract-mixed-thread", False)
        config = runtime._config("contract-mixed-thread")
        prepared = _prepare_dispatch_state(runtime, state, config)
        collected = _collect_single_worker(runtime, prepared, config)

    assert prepared["pending_worker_ids"] == ["diagnosis-valid"]
    assert worker.calls == ["diagnosis-valid"]
    restored = restore_engine_from_runtime_state(collected)
    invalid = restored.graph.get("review-invalid")
    valid = restored.graph.get("diagnosis-valid")
    assert invalid.status is NodeStatus.FAILED
    assert invalid.retry_count == 0
    assert valid.status is NodeStatus.SUCCEEDED

    checked = _events(trace_path, "agent_input_contract_checked")
    rejected = _events(trace_path, "agent_input_contract_rejected")
    assert len(checked) == 2
    assert {event["node_id"]: event["passed"] for event in checked} == {
        "review-invalid": False,
        "diagnosis-valid": True,
    }
    assert [event["node_id"] for event in rejected] == ["review-invalid"]
