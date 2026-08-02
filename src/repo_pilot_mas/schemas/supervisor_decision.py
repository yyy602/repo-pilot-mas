"""Structured global decisions proposed by SupervisorAgent."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from repo_pilot_mas.schemas.worker_artifact import (
    DIAGNOSIS_PERSPECTIVES,
    INVESTIGATOR_MODES,
    PATCH_STRATEGIES,
    REVIEWER_MODES,
)

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_WORKFLOW_STAGES = (
    "initialization",
    "investigation",
    "diagnosis",
    "review",
    "patch",
    "validation",
    "replan",
    "finalization",
    "completed",
    "terminated",
)


class DecisionAction(str, Enum):
    CREATE_TASK = "CREATE_TASK"
    CANCEL_TASK = "CANCEL_TASK"
    PAUSE_TASK = "PAUSE_TASK"
    RESUME_TASK = "RESUME_TASK"
    CHANGE_WORKFLOW_STAGE = "CHANGE_WORKFLOW_STAGE"
    REQUEST_REPLAN = "REQUEST_REPLAN"
    ACCEPT_HYPOTHESIS = "ACCEPT_HYPOTHESIS"
    SELECT_PATCH = "SELECT_PATCH"
    FINALIZE_TASK = "FINALIZE_TASK"
    TERMINATE_TASK = "TERMINATE_TASK"


@dataclass(frozen=True, slots=True)
class GateRecord:
    """Auditable reason and budget effect for a dynamic graph expansion."""

    gate_name: str
    trigger_artifact_refs: tuple[str, ...]
    reason: str
    added_node_count: int
    budget_effect: str

    def __post_init__(self) -> None:
        if not self.gate_name.strip() or not self.reason.strip() or not self.budget_effect.strip():
            raise ValueError("gate record text fields must not be empty")
        object.__setattr__(self, "trigger_artifact_refs", tuple(self.trigger_artifact_refs))
        if not self.trigger_artifact_refs:
            raise ValueError("gate record requires triggering artifacts")
        if isinstance(self.added_node_count, bool) or self.added_node_count <= 0:
            raise ValueError("gate added_node_count must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_name": self.gate_name,
            "trigger_artifact_refs": list(self.trigger_artifact_refs),
            "reason": self.reason,
            "added_node_count": self.added_node_count,
            "budget_effect": self.budget_effect,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GateRecord:
        return cls(
            gate_name=str(value["gate_name"]),
            trigger_artifact_refs=_strings(value["trigger_artifact_refs"]),
            reason=str(value["reason"]),
            added_node_count=int(value["added_node_count"]),
            budget_effect=str(value["budget_effect"]),
        )


@dataclass(frozen=True, slots=True)
class CreateTaskRequest:
    node_type: str
    agent_type: str
    mode: str
    objective: str
    depends_on: tuple[str, ...] = ()
    input_artifact_ids: tuple[str, ...] = ()
    dependency_policy: str = "all_succeeded"
    critical: bool = True
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.node_type, self.agent_type, self.mode, self.objective)):
            raise ValueError("create task fields must not be empty")
        if self.dependency_policy not in {"all_succeeded", "all_terminal"}:
            raise ValueError("unsupported dependency_policy")
        if not isinstance(self.critical, bool):
            raise TypeError("critical must be a boolean")
        if (
            isinstance(self.timeout_seconds, bool)
            or not math.isfinite(float(self.timeout_seconds))
            or float(self.timeout_seconds) <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        object.__setattr__(self, "input_artifact_ids", tuple(self.input_artifact_ids))
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_type": self.node_type,
            "agent_type": self.agent_type,
            "mode": self.mode,
            "objective": self.objective,
            "depends_on": list(self.depends_on),
            "input_artifact_ids": list(self.input_artifact_ids),
            "dependency_policy": self.dependency_policy,
            "critical": self.critical,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CreateTaskRequest:
        return cls(
            node_type=str(value["node_type"]),
            agent_type=str(value["agent_type"]),
            mode=str(value["mode"]),
            objective=str(value["objective"]),
            depends_on=_strings(value.get("depends_on", ())),
            input_artifact_ids=_strings(value.get("input_artifact_ids", ())),
            dependency_policy=str(value.get("dependency_policy", "all_succeeded")),
            critical=value.get("critical", True),
            timeout_seconds=float(value.get("timeout_seconds", 120.0)),
        )


@dataclass(frozen=True, slots=True)
class SupervisorDecision:
    decision_id: str
    action: DecisionAction
    reason: str
    create_tasks: tuple[CreateTaskRequest, ...] = ()
    target_task_ids: tuple[str, ...] = ()
    next_workflow_stage: str | None = None
    evidence_refs: tuple[str, ...] = ()
    hypothesis_ref: str | None = None
    patch_ref: str | None = None
    validation_ref: str | None = None
    gate_record: GateRecord | None = None
    failure_class: str | None = None
    hypothesis_refs: tuple[str, ...] = ()
    primary_hypothesis_ref: str | None = None
    review_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _ID_PATTERN.fullmatch(self.decision_id) or ".." in self.decision_id:
            raise ValueError(f"invalid decision_id: {self.decision_id!r}")
        if not self.reason.strip():
            raise ValueError("decision reason must not be empty")
        object.__setattr__(self, "action", DecisionAction(self.action))
        object.__setattr__(self, "create_tasks", tuple(self.create_tasks))
        object.__setattr__(self, "target_task_ids", tuple(self.target_task_ids))
        object.__setattr__(self, "evidence_refs", _unique_strings(self.evidence_refs))
        object.__setattr__(self, "hypothesis_refs", _unique_strings(self.hypothesis_refs))
        object.__setattr__(self, "review_refs", _unique_strings(self.review_refs))
        self._normalize_hypothesis_selection()
        if self.action is DecisionAction.REQUEST_REPLAN and not self.failure_class:
            defaults = {
                "investigation": "evidence_incomplete",
                "diagnosis": "root_cause_rejected",
                "patch": "target_test_failure",
            }
            object.__setattr__(self, "failure_class", defaults.get(self.next_workflow_stage or ""))
        if self.gate_record is not None and not isinstance(self.gate_record, GateRecord):
            raise TypeError("gate_record must be a GateRecord")
        self._validate_action_fields()

    def _normalize_hypothesis_selection(self) -> None:
        refs = self.hypothesis_refs
        primary = self.primary_hypothesis_ref
        legacy = self.hypothesis_ref
        if legacy and not refs:
            refs = (legacy,)
        if primary and not refs:
            refs = (primary,)
        if refs and primary is None:
            primary = legacy or refs[0]
        if refs and legacy is None:
            legacy = primary
        if legacy and primary and legacy != primary:
            raise ValueError("hypothesis_ref and primary_hypothesis_ref must match")
        if primary is not None and primary not in refs:
            raise ValueError("primary_hypothesis_ref must belong to hypothesis_refs")
        object.__setattr__(self, "hypothesis_refs", refs)
        object.__setattr__(self, "primary_hypothesis_ref", primary)
        object.__setattr__(self, "hypothesis_ref", legacy)

    def _validate_action_fields(self) -> None:
        if self.create_tasks and self.action is not DecisionAction.CREATE_TASK:
            raise ValueError("create_tasks is only valid for CREATE_TASK")
        if self.target_task_ids and self.action not in {
            DecisionAction.CANCEL_TASK,
            DecisionAction.PAUSE_TASK,
            DecisionAction.RESUME_TASK,
        }:
            raise ValueError("target_task_ids is not valid for this action")
        if self.next_workflow_stage and self.action not in {
            DecisionAction.CREATE_TASK,
            DecisionAction.CHANGE_WORKFLOW_STAGE,
            DecisionAction.REQUEST_REPLAN,
        }:
            raise ValueError("next_workflow_stage is not valid for this action")
        if (
            self.hypothesis_ref
            or self.hypothesis_refs
            or self.primary_hypothesis_ref
            or self.review_refs
        ) and self.action is not DecisionAction.ACCEPT_HYPOTHESIS:
            raise ValueError("hypothesis selection fields are only valid for ACCEPT_HYPOTHESIS")
        if (self.patch_ref or self.validation_ref) and self.action not in {
            DecisionAction.SELECT_PATCH,
            DecisionAction.FINALIZE_TASK,
        }:
            raise ValueError("patch selection fields are not valid for this action")
        if self.gate_record and self.action is not DecisionAction.CREATE_TASK:
            raise ValueError("gate_record is only valid for CREATE_TASK")
        if self.failure_class and self.action is not DecisionAction.REQUEST_REPLAN:
            raise ValueError("failure_class is only valid for REQUEST_REPLAN")
        if self.action is DecisionAction.CREATE_TASK and not self.create_tasks:
            raise ValueError("CREATE_TASK requires create_tasks")
        if self.action in {
            DecisionAction.CANCEL_TASK,
            DecisionAction.PAUSE_TASK,
            DecisionAction.RESUME_TASK,
        } and not self.target_task_ids:
            raise ValueError(f"{self.action.value} requires target_task_ids")
        if self.action in {
            DecisionAction.CHANGE_WORKFLOW_STAGE,
            DecisionAction.REQUEST_REPLAN,
        } and not self.next_workflow_stage:
            raise ValueError(f"{self.action.value} requires next_workflow_stage")
        if self.action is DecisionAction.REQUEST_REPLAN and not self.failure_class:
            raise ValueError("REQUEST_REPLAN requires failure_class")
        if self.action is DecisionAction.ACCEPT_HYPOTHESIS and (
            not self.hypothesis_refs or not self.primary_hypothesis_ref
        ):
            raise ValueError("ACCEPT_HYPOTHESIS requires hypothesis_refs and primary_hypothesis_ref")
        if self.action in {DecisionAction.SELECT_PATCH, DecisionAction.FINALIZE_TASK} and (
            not self.patch_ref or not self.validation_ref
        ):
            raise ValueError(f"{self.action.value} requires patch_ref and validation_ref")

    @property
    def fingerprint(self) -> str:
        value = self.to_dict()
        value.pop("decision_id")
        value.pop("reason")
        gate = value.get("gate_record")
        if isinstance(gate, dict):
            gate.pop("reason", None)
            gate.pop("budget_effect", None)
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
