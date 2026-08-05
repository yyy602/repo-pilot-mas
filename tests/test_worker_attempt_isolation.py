from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from repo_pilot_mas.agents.worker_pool import (
    WorkerAttemptRevokedError,
    _workspace_id,
)
from repo_pilot_mas.orchestration import (
    AsyncLangGraphRuntime,
    LangGraphRuntime,
    NodeStatus,
    NodeType,
    OrchestrationEngine,
    TaskNode,
    WorkerOutcome,
)
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.tools import ToolDefinition, ToolRegistry


class _NeverCalledSupervisor:
    def decide(self, snapshot: Mapping[str, Any]) -> Any:
        del snapshot
        raise AssertionError("deterministic retry must not call Supervisor")


class _NoopExecutor:
    def execute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        del task, node, artifacts
        raise AssertionError("direct routing tests must not execute Worker")

    async def aexecute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        return self.execute(task=task, node=node, artifacts=artifacts)


class _SlowRevocableExecutor:
    def __init__(self) -> None:
        self.revocations: list[dict[str, Any]] = []

    def execute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        del task, node, artifacts
        raise AssertionError("async timeout test must use aexecute")

    async def aexecute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        del task, artifacts
        await asyncio.sleep(0.2)
        node_id = str(node["node_id"])
        artifact = Artifact(
            artifact_id=f"{node_id}.late",
            artifact_type=ArtifactType.HYPOTHESIS,
            created_by=node_id,
            content={"summary": "late result must not be collected"},
        )
        return WorkerOutcome(node_id, NodeStatus.SUCCEEDED, (artifact,))

    def revoke_attempt(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        attempt_id: str | None = None,
        reason: str = "runtime_timeout",
    ) -> bool:
        self.revocations.append(
            {
                "task_id": task["task_id"],
                "node_id": node["node_id"],
                "attempt": node["attempt"],
                "attempt_id": attempt_id,
                "reason": reason,
            }
        )
        return True


def _task(tmp_path: Path, task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        repository_path=tmp_path,
        issue="验证 Phase C Worker Attempt 隔离和确定性重试",
        acceptance_criteria=("旧 Attempt 不得污染新 Attempt",),
    )


def _retry_engine(tmp_path: Path, task_id: str) -> OrchestrationEngine:
    engine = OrchestrationEngine(_task(tmp_path, task_id))
    evidence_ref = engine.add_artifact(
        Artifact(
            artifact_id="seed.evidence",
            artifact_type=ArtifactType.EVIDENCE,
            created_by="seed",
            content={"summary": "direct evidence"},
        )
    )
    node_id = engine._next_node_id()
    assert node_id == "N1"
    engine.graph.add_node(
        TaskNode(
            node_id=node_id,
            node_type=NodeType.DIAGNOSIS_TASK,
            agent_type="DiagnosticianAgent",
            mode="control_flow",
            objective="基于直接证据重新生成根因假设",
            input_artifact_ids=(evidence_ref,),
            timeout_seconds=5.0,
            retry_count=1,
        )
    )
    assert engine.graph.get(node_id).status is NodeStatus.READY
    return engine


def _retry_state(
    runtime: LangGraphRuntime,
    engine: OrchestrationEngine,
    thread_id: str,
) -> dict[str, Any]:
    state = runtime._initial_state(engine, thread_id, False)
    state["last_event"] = {
        "event_type": "worker_artifacts_collected",
        "collection_results": {
            "N1": {
                "retry_scheduled": True,
                "attempt": 1,
                "max_attempts": 2,
            }
        },
    }
    return state


def test_guarded_tool_registry_blocks_handler_after_revocation() -> None:
    calls: list[dict[str, Any]] = []
    revoked = False

    def guard() -> None:
        if revoked:
            raise WorkerAttemptRevokedError("attempt revoked")

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            "mutate",
            "test side effect",
            {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            lambda arguments: (
                calls.append(dict(arguments))
                or ToolResult.success("mutate")
            ),
        )
    )
    guarded = registry.guarded(guard)

    assert guarded.invoke("mutate", {}).ok is True
    revoked = True
    with pytest.raises(WorkerAttemptRevokedError, match="attempt revoked"):
        guarded.invoke("mutate", {})
    assert calls == [{}]


def test_async_timeout_revokes_attempt_before_returning_timeout(
    tmp_path: Path,
) -> None:
    executor = _SlowRevocableExecutor()
    runtime = object.__new__(AsyncLangGraphRuntime)
    runtime.worker_executor = executor
    runtime.trace_writer = None

    task = _task(tmp_path, "timeout-attempt").to_dict()
    node = TaskNode(
        node_id="N1",
        node_type=NodeType.DIAGNOSIS_TASK,
        agent_type="DiagnosticianAgent",
        mode="control_flow",
        objective="模拟超时 Worker",
        status=NodeStatus.RUNNING,
        timeout_seconds=0.01,
    ).to_dict()
    node.update(
        {
            "attempt": 1,
            "attempt_id": "timeout-attempt:N1:a1",
            "requested_timeout_seconds": 0.01,
            "timeout_seconds": 0.01,
        }
    )
    state = {
        "task_id": "timeout-attempt",
        "thread_id": "timeout-attempt-thread",
        "dispatch_id": "timeout-attempt-thread:1:N1",
        "engine_state_version": 1,
        "task": task,
        "node": node,
        "artifacts": [],
    }

    update = asyncio.run(runtime._aworker_node(state))
    payload = update["worker_results"]["N1"]

    assert payload["status"] == NodeStatus.TIMED_OUT.value
    assert payload["attempt"] == 1
    assert payload["attempt_id"] == "timeout-attempt:N1:a1"
    assert executor.revocations == [
        {
            "task_id": "timeout-attempt",
            "node_id": "N1",
            "attempt": 1,
            "attempt_id": "timeout-attempt:N1:a1",
            "reason": "worker_timeout:0.01",
        }
    ]


def test_collector_retry_routes_directly_to_attempt_two(tmp_path: Path) -> None:
    engine = _retry_engine(tmp_path, "direct-retry")

    with LangGraphRuntime(
        _NeverCalledSupervisor(),
        _NoopExecutor(),
        tmp_path / "direct-retry.sqlite3",
    ) as runtime:
        state = _retry_state(runtime, engine, "direct-retry-thread")
        config = runtime._config("direct-retry-thread")

        assert runtime._route_after_collector(state) == "prepare_dispatch"
        prepared = dict(state)
        prepared.update(runtime._prepare_dispatch_node(state, config))
        sends = runtime._route_prepared_dispatch(prepared)

    assert isinstance(sends, list)
    assert len(sends) == 1
    worker_input = sends[0].arg
    assert worker_input["node"]["attempt"] == 2
    assert worker_input["node"]["attempt_id"] == "direct-retry:N1:a2"
    assert worker_input["node"]["requested_timeout_seconds"] == 5.0
    assert worker_input["node"]["timeout_seconds"] == 60.0


def test_collector_rejects_stale_attempt_result(tmp_path: Path) -> None:
    engine = _retry_engine(tmp_path, "stale-attempt")

    with LangGraphRuntime(
        _NeverCalledSupervisor(),
        _NoopExecutor(),
        tmp_path / "stale-attempt.sqlite3",
    ) as runtime:
        state = _retry_state(runtime, engine, "stale-attempt-thread")
        config = runtime._config("stale-attempt-thread")
        prepared = dict(state)
        prepared.update(runtime._prepare_dispatch_node(state, config))
        prepared["worker_results"] = {
            "N1": {
                "node_id": "N1",
                "status": NodeStatus.FAILED.value,
                "artifacts": [],
                "reason": "late attempt one result",
                "dispatch_id": prepared["dispatch_id"],
                "engine_state_version": prepared["engine_state_version"],
                "attempt": 1,
                "attempt_id": "stale-attempt:N1:a1",
            }
        }

        with pytest.raises(ValueError, match="attempt is inconsistent"):
            runtime._collector_node(prepared, config)


def test_retry_workspace_identity_is_attempt_scoped() -> None:
    attempt_one = _workspace_id("N4", "2026-08-05T00:00:00+00:00", 1)
    attempt_two = _workspace_id("N4", "2026-08-05T00:00:00+00:00", 2)

    assert attempt_one != attempt_two
    assert "-a1-" in attempt_one
    assert "-a2-" in attempt_two
