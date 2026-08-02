"""Phase 5 deterministic policy checks around semantic Supervisor decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from repo_pilot_mas.orchestration.task_graph import NodeType, TaskNode


class ExecutionPath(str, Enum):
    FAST = "fast"
    STANDARD = "standard"
    DEEP = "deep"


class FailureClass(str, Enum):
    REPRODUCTION_FAILURE = "reproduction_failure"
    WRONG_LOCATION = "wrong_location"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"
    ROOT_CAUSE_REJECTED = "root_cause_rejected"
    BOTH_PATCHES_FAIL_TARGET = "both_patches_fail_target"
    COUNTEREXAMPLE_OVERTURNS = "counterexample_overturns"
    PATCH_APPLY_FAILURE = "patch_apply_failure"
    SYNTAX_FAILURE = "syntax_failure"
    TARGET_TEST_FAILURE = "target_test_failure"
    REGRESSION_FAILURE = "regression_failure"
    BLOCKING_PATCH_REVIEW = "blocking_patch_review"


_REPLAN_STAGE = {
    FailureClass.REPRODUCTION_FAILURE: "investigation",
    FailureClass.WRONG_LOCATION: "investigation",
    FailureClass.EVIDENCE_INCOMPLETE: "investigation",
    FailureClass.ROOT_CAUSE_REJECTED: "diagnosis",
    FailureClass.BOTH_PATCHES_FAIL_TARGET: "diagnosis",
    FailureClass.COUNTEREXAMPLE_OVERTURNS: "diagnosis",
    FailureClass.PATCH_APPLY_FAILURE: "patch",
    FailureClass.SYNTAX_FAILURE: "patch",
    FailureClass.TARGET_TEST_FAILURE: "patch",
    FailureClass.REGRESSION_FAILURE: "patch",
    FailureClass.BLOCKING_PATCH_REVIEW: "patch",
}


def required_replan_stage(failure_class: FailureClass | str) -> str:
    """Return the only workflow stage compatible with a failure class."""

    return _REPLAN_STAGE[FailureClass(failure_class)]


def classify_execution_path(nodes: Sequence[TaskNode | Mapping[str, Any]]) -> ExecutionPath:
    """Classify a completed path from nodes that actually existed in the graph."""

    node_types = tuple(_node_type(node) for node in nodes)
    if (
        NodeType.CHALLENGE_TASK in node_types
        or NodeType.REBUTTAL_TASK in node_types
        or NodeType.REPLAN_TASK in node_types
        or node_types.count(NodeType.PATCH_TASK) >= 2
    ):
        return ExecutionPath.DEEP
    if (
        node_types.count(NodeType.INVESTIGATION_TASK) >= 2
        or node_types.count(NodeType.DIAGNOSIS_TASK) >= 2
        or NodeType.REVIEW_TASK in node_types
    ):
        return ExecutionPath.STANDARD
    return ExecutionPath.FAST


def hypotheses_materially_different(
    first: Mapping[str, Any], second: Mapping[str, Any]
) -> bool:
    """Provide a conservative structural signal; Supervisor still decides semantics."""

    first_symbols = {str(value).strip() for value in first.get("affected_symbols", ())}
    second_symbols = {str(value).strip() for value in second.get("affected_symbols", ())}
    root_differs = _normalized(first.get("root_cause")) != _normalized(second.get("root_cause"))
    direct_differs = _normalized(first.get("direct_cause")) != _normalized(
        second.get("direct_cause")
    )
    return root_differs and (direct_differs or first_symbols != second_symbols)


def _node_type(node: TaskNode | Mapping[str, Any]) -> NodeType:
    if isinstance(node, TaskNode):
        return node.node_type
    return NodeType(str(node["node_type"]))


def _normalized(value: Any) -> str:
    return "".join(str(value or "").lower().split())
