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
        if not all(
            value.strip() for value in (self.node_type, self.agent_type, self.mode, self.objective)
        ):
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

    def __post_init__(self) -> None:
        if not _ID_PATTERN.fullmatch(self.decision_id) or ".." in self.decision_id:
            raise ValueError(f"invalid decision_id: {self.decision_id!r}")
        if not self.reason.strip():
            raise ValueError("decision reason must not be empty")
        object.__setattr__(self, "action", DecisionAction(self.action))
        object.__setattr__(self, "create_tasks", tuple(self.create_tasks))
        object.__setattr__(self, "target_task_ids", tuple(self.target_task_ids))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
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
        if self.hypothesis_ref and self.action is not DecisionAction.ACCEPT_HYPOTHESIS:
            raise ValueError("hypothesis_ref is only valid for ACCEPT_HYPOTHESIS")
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
        if self.action is DecisionAction.ACCEPT_HYPOTHESIS and not self.hypothesis_ref:
            raise ValueError("ACCEPT_HYPOTHESIS requires hypothesis_ref")
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

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "decision_id": self.decision_id,
            "action": self.action.value,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
        }
        if self.create_tasks:
            value["create_tasks"] = [task.to_dict() for task in self.create_tasks]
        if self.target_task_ids:
            value["target_task_ids"] = list(self.target_task_ids)
        if self.next_workflow_stage:
            value["next_workflow_stage"] = self.next_workflow_stage
        if self.hypothesis_ref:
            value["hypothesis_ref"] = self.hypothesis_ref
        if self.patch_ref:
            value["patch_ref"] = self.patch_ref
        if self.validation_ref:
            value["validation_ref"] = self.validation_ref
        if self.gate_record:
            value["gate_record"] = self.gate_record.to_dict()
        if self.failure_class:
            value["failure_class"] = self.failure_class
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SupervisorDecision:
        create_values = value.get("create_tasks", ())
        if isinstance(create_values, (str, bytes)):
            raise TypeError("create_tasks must be a sequence")
        return cls(
            decision_id=str(value["decision_id"]),
            action=DecisionAction(str(value["action"])),
            reason=str(value["reason"]),
            create_tasks=tuple(CreateTaskRequest.from_dict(item) for item in create_values),
            target_task_ids=_strings(value.get("target_task_ids", ())),
            next_workflow_stage=(
                str(value["next_workflow_stage"])
                if value.get("next_workflow_stage")
                else None
            ),
            evidence_refs=_strings(value.get("evidence_refs", ())),
            hypothesis_ref=(str(value["hypothesis_ref"]) if value.get("hypothesis_ref") else None),
            patch_ref=str(value["patch_ref"]) if value.get("patch_ref") else None,
            validation_ref=(str(value["validation_ref"]) if value.get("validation_ref") else None),
            gate_record=(
                GateRecord.from_dict(value["gate_record"])
                if isinstance(value.get("gate_record"), Mapping)
                else None
            ),
            failure_class=str(value["failure_class"]) if value.get("failure_class") else None,
        )


def supervisor_decision_schema() -> dict[str, Any]:
    non_empty = {"type": "string", "minLength": 1}
    common = {
        "decision_id": {
            "type": "string",
            "minLength": 1,
            "pattern": "[A-Za-z0-9_.-]+",
        },
        "reason": non_empty,
        "evidence_refs": {"type": "array", "items": non_empty},
    }
    worker_contracts = (
        ("INVESTIGATION_TASK", "InvestigatorAgent", INVESTIGATOR_MODES),
        ("DIAGNOSIS_TASK", "DiagnosticianAgent", DIAGNOSIS_PERSPECTIVES),
        ("CHALLENGE_TASK", "DiagnosticianAgent", ("challenge",)),
        ("REBUTTAL_TASK", "DiagnosticianAgent", ("rebuttal",)),
        ("REVIEW_TASK", "ReviewerAgent", REVIEWER_MODES),
        (
            "REVIEW_TASK",
            "PatchAgent",
            ("minimal_critiques_robust", "robust_critiques_minimal"),
        ),
        ("PATCH_TASK", "PatchAgent", PATCH_STRATEGIES),
        ("VALIDATION_TASK", "ValidationExecutor", ("deterministic",)),
    )
    task_common = {
        "objective": non_empty,
        "depends_on": {"type": "array", "items": non_empty},
        "input_artifact_ids": {"type": "array", "items": non_empty},
        "dependency_policy": {
            "type": "string",
            "enum": ["all_succeeded", "all_terminal"],
        },
        "critical": {"type": "boolean"},
        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
    }
    task_schema = {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "node_type": {"const": node_type},
                    "agent_type": {"const": agent_type},
                    "mode": {"type": "string", "enum": list(modes)},
                    **task_common,
                },
                "required": ["node_type", "agent_type", "mode", "objective"],
                "additionalProperties": False,
            }
            for node_type, agent_type, modes in worker_contracts
        ]
    }
    gate_schema = {
        "type": "object",
        "properties": {
            "gate_name": non_empty,
            "trigger_artifact_refs": {
                "type": "array",
                "items": non_empty,
                "minItems": 1,
            },
            "reason": non_empty,
            "added_node_count": {"type": "integer", "minimum": 1},
            "budget_effect": non_empty,
        },
        "required": [
            "gate_name",
            "trigger_artifact_refs",
            "reason",
            "added_node_count",
            "budget_effect",
        ],
        "additionalProperties": False,
    }

    def variant(
        action: DecisionAction,
        extra: Mapping[str, Any] | None = None,
        required: Sequence[str] = (),
    ) -> dict[str, Any]:
        properties = {**common, "action": {"const": action.value}, **dict(extra or {})}
        return {
            "type": "object",
            "properties": properties,
            "required": ["decision_id", "action", "reason", *required],
            "additionalProperties": False,
        }

    targets = {
        "target_task_ids": {
            "type": "array",
            "items": non_empty,
            "minItems": 1,
        }
    }
    stage = {"next_workflow_stage": {"type": "string", "enum": list(_WORKFLOW_STAGES)}}
    selection = {
        "patch_ref": non_empty,
        "validation_ref": non_empty,
    }
    return {
        "oneOf": [
            variant(
                DecisionAction.CREATE_TASK,
                {
                    "create_tasks": {
                        "type": "array",
                        "items": task_schema,
                        "minItems": 1,
                    },
                    "next_workflow_stage": stage["next_workflow_stage"],
                    "gate_record": gate_schema,
                },
                ("create_tasks",),
            ),
            variant(DecisionAction.CANCEL_TASK, targets, ("target_task_ids",)),
            variant(DecisionAction.PAUSE_TASK, targets, ("target_task_ids",)),
            variant(DecisionAction.RESUME_TASK, targets, ("target_task_ids",)),
            variant(DecisionAction.CHANGE_WORKFLOW_STAGE, stage, ("next_workflow_stage",)),
            variant(
                DecisionAction.REQUEST_REPLAN,
                {
                    **stage,
                    "failure_class": {
                        "type": "string",
                        "enum": [
                            "reproduction_failure",
                            "wrong_location",
                            "evidence_incomplete",
                            "root_cause_rejected",
                            "both_patches_fail_target",
                            "counterexample_overturns",
                            "patch_apply_failure",
                            "syntax_failure",
                            "target_test_failure",
                            "regression_failure",
                            "blocking_patch_review",
                        ],
                    },
                },
                ("next_workflow_stage", "failure_class"),
            ),
            variant(
                DecisionAction.ACCEPT_HYPOTHESIS,
                {"hypothesis_ref": non_empty},
                ("hypothesis_ref",),
            ),
            variant(DecisionAction.SELECT_PATCH, selection, ("patch_ref", "validation_ref")),
            variant(DecisionAction.FINALIZE_TASK, selection, ("patch_ref", "validation_ref")),
            variant(DecisionAction.TERMINATE_TASK),
        ]
    }


def _strings(value: Sequence[Any]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError("expected a sequence, not a string")
    return tuple(str(item) for item in value)
