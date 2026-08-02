"""Deterministic task graph, node state machine, and dependency policies."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from repo_pilot_mas.schemas.tool_result import utc_now_iso


class NodeType(str, Enum):
    INVESTIGATION_TASK = "INVESTIGATION_TASK"
    DIAGNOSIS_TASK = "DIAGNOSIS_TASK"
    CHALLENGE_TASK = "CHALLENGE_TASK"
    REBUTTAL_TASK = "REBUTTAL_TASK"
    REVIEW_TASK = "REVIEW_TASK"
    PATCH_TASK = "PATCH_TASK"
    VALIDATION_TASK = "VALIDATION_TASK"
    REPLAN_TASK = "REPLAN_TASK"
    FINALIZATION_TASK = "FINALIZATION_TASK"


class NodeStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


class DependencyPolicy(str, Enum):
    ALL_SUCCEEDED = "all_succeeded"
    ALL_TERMINAL = "all_terminal"


TERMINAL_STATUSES = frozenset(
    {
        NodeStatus.SUCCEEDED,
        NodeStatus.FAILED,
        NodeStatus.TIMED_OUT,
        NodeStatus.BLOCKED,
        NodeStatus.CANCELLED,
    }
)

_ALLOWED_TRANSITIONS = {
    NodeStatus.PENDING: {NodeStatus.READY, NodeStatus.BLOCKED, NodeStatus.PAUSED, NodeStatus.CANCELLED},
    NodeStatus.READY: {NodeStatus.RUNNING, NodeStatus.PAUSED, NodeStatus.CANCELLED},
    NodeStatus.RUNNING: {
        NodeStatus.SUCCEEDED,
        NodeStatus.FAILED,
        NodeStatus.TIMED_OUT,
        NodeStatus.BLOCKED,
        NodeStatus.PAUSED,
        NodeStatus.CANCELLED,
    },
    NodeStatus.PAUSED: {NodeStatus.PENDING, NodeStatus.READY, NodeStatus.CANCELLED},
    NodeStatus.FAILED: {NodeStatus.PENDING},
    NodeStatus.TIMED_OUT: {NodeStatus.PENDING},
}


@dataclass(slots=True)
class TaskNode:
    node_id: str
    node_type: NodeType
    agent_type: str
    mode: str
    objective: str
    dependencies: tuple[str, ...] = ()
    input_artifact_ids: tuple[str, ...] = ()
    dependency_policy: DependencyPolicy = DependencyPolicy.ALL_SUCCEEDED
    critical: bool = True
    timeout_seconds: float = 120.0
    status: NodeStatus = NodeStatus.PENDING
    output_artifact_ids: tuple[str, ...] = ()
    created_by_decision_id: str | None = None
    retry_count: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    failure_reason: str | None = None
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if not self.node_id.strip() or not self.agent_type.strip() or not self.objective.strip():
            raise ValueError("node_id, agent_type, and objective must not be empty")
        if (
            isinstance(self.timeout_seconds, bool)
            or not math.isfinite(float(self.timeout_seconds))
            or float(self.timeout_seconds) <= 0
        ):
            raise ValueError("node timeout_seconds must be positive")
        if self.retry_count < 0:
            raise ValueError("retry_count must be non-negative")
        if not isinstance(self.critical, bool):
            raise TypeError("critical must be a boolean")
        self.node_type = NodeType(self.node_type)
        self.status = NodeStatus(self.status)
        self.dependency_policy = DependencyPolicy(self.dependency_policy)
        self.dependencies = tuple(self.dependencies)
        self.input_artifact_ids = tuple(self.input_artifact_ids)
        self.output_artifact_ids = tuple(self.output_artifact_ids)
        self.timeout_seconds = float(self.timeout_seconds)

    @property
    def
        return self.status in TERMINAL_STATUSES

    def transition(self, target: NodeStatus, *, reason: str | None = None) -> None:
        target = NodeStatus(target)
        if target not in _ALLOWED_TRANSITIONS.get(self.status, set()):
            raise ValueError(f"illegal node transition: {self.status.value} -> {target.value}")
        self.status = target
        if target is NodeStatus.RUNNING:
            self.started_at = utc_now_iso()
            self.finished_at = None
            self.failure_reason = None
        elif target in TERMINAL_STATUSES:
            self.finished_at = utc_now_iso()
            self.failure_reason = reason
        elif target is NodeStatus.PENDING:
            self.started_at = None
            self.finished_at = None
            self.failure_reason = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type.value,
            "agent_type": self.agent_type,
            "mode": self.mode,
            "objective": self.objective,
            "dependencies": list(self.dependencies),
            "input_artifact_ids": list(self.input_artifact_ids),
            "dependency_policy": self.dependency_policy.value,
            "critical": self.critical,
            "timeout_seconds": self.timeout_seconds,
            "status": self.status.value,
            "output_artifact_ids": list(self.output_artifact_ids),
            "created_by_decision_id": self.created_by_decision_id,
            "retry_count": self.retry_count,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "failure_reason": self.failure_reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TaskNode:
        return cls(
            node_id=str(value["node_id"]),
            node_type=NodeType(str(value["node_type"])),
            agent_type=str(value["agent_type"]),
            mode=str(value.get("mode", "default")),
            objective=str(value["objective"]),
            dependencies=_strings(value.get("dependencies", ())),
            input_artifact_ids=_strings(value.get("input_artifact_ids", ())),
            dependency_policy=DependencyPolicy(
                str(value.get("dependency_policy", DependencyPolicy.ALL_SUCCEEDED.value))
            ),
            critical=value.get("critical", True),
            timeout_seconds=float(value.get("timeout_seconds", 120.0)),
            status=NodeStatus(str(value.get("status", NodeStatus.PENDING.value))),
            output_artifact_ids=_strings(value.get("output_artifact_ids", ())),
            created_by_decision_id=value.get("created_by_decision_id"),
            retry_count=int(value.get("retry_count", 0)),
            started_at=value.get("started_at"),
            finished_at=value.get("finished_at"),
            failure_reason=value.get("failure_reason"),
            created_at=str(value.get("created_at", utc_now_iso())),
        )


class TaskGraph:
    def __init__(self, nodes: Sequence[TaskNode] = (), *, version: int = 0) -> None:
        if isinstance(version, bool) or version < 0:
            raise ValueError("task graph version must be non-negative")
        self._nodes: dict[str, TaskNode] = {}
        self.version = 0
        for node in nodes:
            self.add_node(node, refresh=False)
        if version:
            self.version = version

    @property
    def nodes(self) -> tuple[TaskNode, ...]:
        return tuple(self._nodes.values())

    def get(self, node_id: str) -> TaskNode:
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise KeyError(f"unknown task node: {node_id}") from exc

    def add_node(self, node: TaskNode, *, refresh: bool = True) -> tuple[str, ...]:
        if node.node_id in self._nodes:
            raise ValueError(f"duplicate task node: {node.node_id}")
        if node.node_id in node.dependencies:
            raise ValueError("task node cannot depend on itself")
        missing = [item for item in node.dependencies if item not in self._nodes]
        if missing:
            raise ValueError(f"task node has unknown dependencies: {missing}")
        self._nodes[node.node_id] = node
        self.version += 1
        return self.refresh_ready() if refresh else ()

    def transition(
        self,
        node_id: str,
        target: NodeStatus,
        *,
        reason: str | None = None,
    ) -> tuple[str, ...]:
        node = self.get(node_id)
        node.transition(target, reason=reason)
        self.version += 1
        changed = [node_id]
        changed.extend(self.refresh_ready())
        return tuple(dict.fromkeys(changed))

    def refresh_ready(self) -> tuple[str, ...]:
        changed: list[str] = []
        progress = True
        while progress:
            progress = False
            for node in self._nodes.values():
                if node.status is not NodeStatus.PENDING:
                    continue
                dependencies = [self._nodes[item] for item in node.dependencies]
                if not dependencies:
                    node.transition(NodeStatus.READY)
                elif node.dependency_policy is DependencyPolicy.ALL_TERMINAL:
                    if not all(item.terminal for item in dependencies):
                        continue
                    node.transition(NodeStatus.READY)
                else:
                    if all(item.status is NodeStatus.SUCCEEDED for item in dependencies):
                        node.transition(NodeStatus.READY)
                    elif any(item.terminal and item.status is not NodeStatus.SUCCEEDED for item in dependencies):
                        node.transition(NodeStatus.BLOCKED, reason="dependency did not succeed")
                    else:
                        continue
                self.version += 1
                changed.append(node.node_id)
                progress = True
        return tuple(changed)

    def descendants(self, node_id: str) -> tuple[str, ...]:
        self.get(node_id)
        found: list[str] = []
        queue = deque([node_id])
        while queue:
            parent = queue.popleft()
            for node in self._nodes.values():
                if parent in node.dependencies and node.node_id not in found:
                    found.append(node.node_id)
                    queue.append(node.node_id)
        return tuple(found)

    def invalidate_descendants(self, node_id: str, *, reason: str) -> tuple[str, ...]:
        changed: list[str] = []
        for descendant_id in self.descendants(node_id):
            node = self.get(descendant_id)
            if node.terminal:
                continue
            if NodeStatus.CANCELLED in _ALLOWED_TRANSITIONS.get(node.status, set()):
                node.transition(NodeStatus.CANCELLED, reason=reason)
                self.version += 1
                changed.append(descendant_id)
        return tuple(changed)

    def semantic_duplicate(self, candidate: TaskNode) -> str | None:
        for node in self._nodes.values():
            if node.terminal:
                continue
            if (
                node.node_type == candidate.node_type
                and node.agent_type == candidate.agent_type
                and node.mode == candidate.mode
                and node.objective == candidate.objective
                and node.input_artifact_ids == candidate.input_artifact_ids
            ):
                return node.node_id
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "nodes": [node.to_dict() for node in self._nodes.values()]}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TaskGraph:
        node_values = value.get("nodes", ())
        if isinstance(node_values, (str, bytes)):
            raise TypeError("task graph nodes must be a sequence")
        graph = cls(
            [TaskNode.from_dict(item) for item in node_values],
            version=0,
        )
        saved_version = int(value.get("version", graph.version))
        if saved_version < 0:
            raise ValueError("task graph version must be non-negative")
        graph.version = saved_version
        return graph


def _strings(value: Sequence[Any]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError("expected a sequence, not a string")
    return tuple(str(item) for item in value)
