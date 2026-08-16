"""Structured global decisions proposed by SupervisorAgent."""

from __future__ import annotations

import copy
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
        if (
            not self.gate_name.strip()
            or not self.reason.strip()
            or not self.budget_effect.strip()
        ):
            raise ValueError("gate record text fields must not be empty")
        object.__setattr__(
            self,
            "trigger_artifact_refs",
            _unique_strings(self.trigger_artifact_refs),
        )
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
            value.strip()
            for value in (
                self.node_type,
                self.agent_type,
                self.mode,
                self.objective,
            )
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
        object.__setattr__(self, "depends_on", _unique_strings(self.depends_on))
        object.__setattr__(
            self,
            "input_artifact_ids",
            _unique_strings(self.input_artifact_ids),
        )
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
        object.__setattr__(
            self,
            "target_task_ids",
            _unique_strings(self.target_task_ids),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _unique_strings(self.evidence_refs),
        )
        object.__setattr__(
            self,
            "hypothesis_refs",
            _unique_strings(self.hypothesis_refs),
        )
        object.__setattr__(
            self,
            "review_refs",
            _unique_strings(self.review_refs),
        )
        self._normalize_hypothesis_selection()
        if self.action is DecisionAction.REQUEST_REPLAN and not self.failure_class:
            defaults = {
                "investigation": "evidence_incomplete",
                "diagnosis": "root_cause_rejected",
                "patch": "target_test_failure",
            }
            object.__setattr__(
                self,
                "failure_class",
                defaults.get(self.next_workflow_stage or ""),
            )
        if self.gate_record is not None and not isinstance(
            self.gate_record,
            GateRecord,
        ):
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
            raise ValueError(
                "hypothesis_ref and primary_hypothesis_ref must match"
            )
        if primary is not None and primary not in refs:
            raise ValueError(
                "primary_hypothesis_ref must belong to hypothesis_refs"
            )
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
            raise ValueError(
                "next_workflow_stage is not valid for this action"
            )
        if (
            self.hypothesis_ref
            or self.hypothesis_refs
            or self.primary_hypothesis_ref
            or self.review_refs
        ) and self.action is not DecisionAction.ACCEPT_HYPOTHESIS:
            raise ValueError(
                "hypothesis selection fields are only valid for "
                "ACCEPT_HYPOTHESIS"
            )
        if (self.patch_ref or self.validation_ref) and self.action not in {
            DecisionAction.SELECT_PATCH,
            DecisionAction.FINALIZE_TASK,
        }:
            raise ValueError(
                "patch selection fields are not valid for this action"
            )
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
            raise ValueError(
                f"{self.action.value} requires next_workflow_stage"
            )
        if self.action is DecisionAction.REQUEST_REPLAN and not self.failure_class:
            raise ValueError("REQUEST_REPLAN requires failure_class")
        if self.action is DecisionAction.ACCEPT_HYPOTHESIS and (
            not self.hypothesis_refs or not self.primary_hypothesis_ref
        ):
            raise ValueError(
                "ACCEPT_HYPOTHESIS requires hypothesis_refs and "
                "primary_hypothesis_ref"
            )
        if self.action in {
            DecisionAction.SELECT_PATCH,
            DecisionAction.FINALIZE_TASK,
        } and (not self.patch_ref or not self.validation_ref):
            raise ValueError(
                f"{self.action.value} requires patch_ref and validation_ref"
            )

    @property
    def fingerprint(self) -> str:
        value = self.to_dict()
        value.pop("decision_id")
        value.pop("reason")
        gate = value.get("gate_record")
        if isinstance(gate, dict):
            gate.pop("reason", None)
            gate.pop("budget_effect", None)
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

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
        if self.hypothesis_refs:
            value["hypothesis_refs"] = list(self.hypothesis_refs)
            value["primary_hypothesis_ref"] = self.primary_hypothesis_ref
            value["hypothesis_ref"] = self.hypothesis_ref
        if self.review_refs:
            value["review_refs"] = list(self.review_refs)
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
            create_tasks=tuple(
                CreateTaskRequest.from_dict(item)
                for item in create_values
            ),
            target_task_ids=_strings(value.get("target_task_ids", ())),
            next_workflow_stage=(
                str(value["next_workflow_stage"])
                if value.get("next_workflow_stage")
                else None
            ),
            evidence_refs=_strings(value.get("evidence_refs", ())),
            hypothesis_ref=(
                str(value["hypothesis_ref"])
                if value.get("hypothesis_ref")
                else None
            ),
            patch_ref=(
                str(value["patch_ref"])
                if value.get("patch_ref")
                else None
            ),
            validation_ref=(
                str(value["validation_ref"])
                if value.get("validation_ref")
                else None
            ),
            gate_record=(
                GateRecord.from_dict(value["gate_record"])
                if isinstance(value.get("gate_record"), Mapping)
                else None
            ),
            failure_class=(
                str(value["failure_class"])
                if value.get("failure_class")
                else None
            ),
            hypothesis_refs=_strings(value.get("hypothesis_refs", ())),
            primary_hypothesis_ref=(
                str(value["primary_hypothesis_ref"])
                if value.get("primary_hypothesis_ref")
                else None
            ),
            review_refs=_strings(value.get("review_refs", ())),
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
        "evidence_refs": {
            "type": "array",
            "items": non_empty,
        },
    }
    worker_contracts = (
        (
            "INVESTIGATION_TASK",
            "InvestigatorAgent",
            INVESTIGATOR_MODES,
        ),
        (
            "DIAGNOSIS_TASK",
            "DiagnosticianAgent",
            DIAGNOSIS_PERSPECTIVES,
        ),
        (
            "CHALLENGE_TASK",
            "DiagnosticianAgent",
            ("challenge",),
        ),
        (
            "REBUTTAL_TASK",
            "DiagnosticianAgent",
            ("rebuttal",),
        ),
        (
            "REVIEW_TASK",
            "ReviewerAgent",
            REVIEWER_MODES,
        ),
        (
            "REVIEW_TASK",
            "PatchAgent",
            ("minimal_critiques_robust", "robust_critiques_minimal"),
        ),
        (
            "PATCH_TASK",
            "PatchAgent",
            PATCH_STRATEGIES,
        ),
        (
            "VALIDATION_TASK",
            "ValidationExecutor",
            ("deterministic",),
        ),
    )
    task_common = {
        "objective": non_empty,
        "depends_on": {
            "type": "array",
            "items": non_empty,
        },
        "input_artifact_ids": {
            "type": "array",
            "items": non_empty,
        },
        "dependency_policy": {
            "type": "string",
            "enum": ["all_succeeded", "all_terminal"],
        },
        "critical": {"type": "boolean"},
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
        },
    }
    task_schema = {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "node_type": {"const": node_type},
                    "agent_type": {"const": agent_type},
                    "mode": {
                        "type": "string",
                        "enum": list(modes),
                    },
                    **task_common,
                },
                "required": [
                    "node_type",
                    "agent_type",
                    "mode",
                    "objective",
                ],
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
            "added_node_count": {
                "type": "integer",
                "minimum": 1,
            },
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
        properties = {
            **common,
            "action": {"const": action.value},
            **dict(extra or {}),
        }
        return {
            "type": "object",
            "properties": properties,
            "required": [
                "decision_id",
                "action",
                "reason",
                *required,
            ],
            "additionalProperties": False,
        }

    targets = {
        "target_task_ids": {
            "type": "array",
            "items": non_empty,
            "minItems": 1,
        }
    }
    stage = {
        "next_workflow_stage": {
            "type": "string",
            "enum": list(_WORKFLOW_STAGES),
        }
    }
    patch_selection = {
        "patch_ref": non_empty,
        "validation_ref": non_empty,
    }
    hypothesis_selection = {
        "hypothesis_refs": {
            "type": "array",
            "items": non_empty,
            "minItems": 1,
        },
        "primary_hypothesis_ref": non_empty,
        "review_refs": {
            "type": "array",
            "items": non_empty,
            "minItems": 1,
        },
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
                    "next_workflow_stage": stage[
                        "next_workflow_stage"
                    ],
                    "gate_record": gate_schema,
                },
                ("create_tasks",),
            ),
            variant(
                DecisionAction.CANCEL_TASK,
                targets,
                ("target_task_ids",),
            ),
            variant(
                DecisionAction.PAUSE_TASK,
                targets,
                ("target_task_ids",),
            ),
            variant(
                DecisionAction.RESUME_TASK,
                targets,
                ("target_task_ids",),
            ),
            variant(
                DecisionAction.CHANGE_WORKFLOW_STAGE,
                stage,
                ("next_workflow_stage",),
            ),
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
                (
                    "next_workflow_stage",
                    "failure_class",
                ),
            ),
            variant(
                DecisionAction.ACCEPT_HYPOTHESIS,
                hypothesis_selection,
                (
                    "hypothesis_refs",
                    "primary_hypothesis_ref",
                    "review_refs",
                ),
            ),
            variant(
                DecisionAction.SELECT_PATCH,
                patch_selection,
                ("patch_ref", "validation_ref"),
            ),
            variant(
                DecisionAction.FINALIZE_TASK,
                patch_selection,
                ("patch_ref", "validation_ref"),
            ),
            variant(DecisionAction.TERMINATE_TASK),
        ]
    }


_STAGE_CREATE_NODE_TYPES = {
    "initialization": frozenset({"INVESTIGATION_TASK"}),
    "investigation": frozenset(
        {"INVESTIGATION_TASK", "DIAGNOSIS_TASK", "REVIEW_TASK"}
    ),
    "diagnosis": frozenset(
        {
            "INVESTIGATION_TASK",
            "DIAGNOSIS_TASK",
            "CHALLENGE_TASK",
            "REBUTTAL_TASK",
            "REVIEW_TASK",
        }
    ),
    "review": frozenset(
        {
            "INVESTIGATION_TASK",
            "DIAGNOSIS_TASK",
            "CHALLENGE_TASK",
            "REBUTTAL_TASK",
            "REVIEW_TASK",
        }
    ),
    "patch": frozenset({"PATCH_TASK", "REVIEW_TASK", "VALIDATION_TASK"}),
    "validation": frozenset(
        {"PATCH_TASK", "REVIEW_TASK", "VALIDATION_TASK"}
    ),
    "replan": frozenset({"INVESTIGATION_TASK", "DIAGNOSIS_TASK", "PATCH_TASK"}),
}

_LEGAL_STAGE_TRANSITIONS = {
    "initialization": ("investigation",),
    "investigation": ("diagnosis",),
    "diagnosis": ("investigation", "review", "patch"),
    "review": ("investigation", "diagnosis", "patch"),
    "patch": ("diagnosis", "validation"),
    "validation": ("investigation", "diagnosis", "patch", "finalization"),
    "replan": ("investigation", "diagnosis", "patch"),
    "finalization": ("completed",),
}

_REPLAN_TARGET_BY_FAILURE_CLASS = {
    "reproduction_failure": "investigation",
    "wrong_location": "investigation",
    "evidence_incomplete": "investigation",
    "root_cause_rejected": "diagnosis",
    "both_patches_fail_target": "diagnosis",
    "counterexample_overturns": "diagnosis",
    "patch_apply_failure": "patch",
    "syntax_failure": "patch",
    "target_test_failure": "patch",
    "regression_failure": "patch",
    "blocking_patch_review": "patch",
}


def additional_investigation_required(
    artifacts: Sequence[Mapping[str, Any]],
) -> bool:
    """Return whether the current Artifact set still justifies Investigation."""

    source_evidence_indexes: list[int] = []
    failure_evidence_indexes: list[int] = []
    verified_evidence_indexes: list[int] = []
    latest_explicit_gap_index: int | None = None

    for index, item in enumerate(artifacts):
        if not isinstance(item, Mapping):
            continue
        artifact_type = str(item.get("artifact_type", ""))
        content = item.get("content", {})
        if not isinstance(content, Mapping):
            continue

        if (
            artifact_type == "hypothesis" and content.get("missing_evidence")
        ) or (
            artifact_type == "review"
            and (
                content.get("verdict") == "needs_more_evidence"
                or content.get("remaining_uncertainty")
            )
        ):
            latest_explicit_gap_index = index

        if artifact_type != "evidence":
            continue
        if (
            content.get("verified") is not True
            or content.get("status") != "verified"
            or not content.get("tool_trace_ids")
            or not isinstance(content.get("source"), Mapping)
            or not content.get("source", {}).get("path")
        ):
            continue

        verified_evidence_indexes.append(index)
        source_evidence_indexes.append(index)
        reproduction = content.get("reproduction")
        reproduced_failure = (
            isinstance(reproduction, Mapping)
            and reproduction.get("attempted") is True
            and reproduction.get("succeeded") is True
            and bool(reproduction.get("failure_output"))
        )
        verified_execution = (
            content.get("evidence_kind") == "execution"
            and bool(content.get("content"))
        )
        if reproduced_failure or verified_execution:
            failure_evidence_indexes.append(index)

    if not source_evidence_indexes or not failure_evidence_indexes:
        return True
    if latest_explicit_gap_index is None:
        return False
    return not any(
        index > latest_explicit_gap_index
        and not artifacts[index].get("content", {}).get("missing_evidence")
        for index in verified_evidence_indexes
    )


def supervisor_decision_schema_for_state(
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Return only actions and Worker contracts that are legal for this state."""

    stage = str(snapshot.get("workflow_stage", "initialization"))
    selections = snapshot.get("selections", {})
    selections = selections if isinstance(selections, Mapping) else {}
    resolution = selections.get("hypothesis_resolution", {})
    resolution = resolution if isinstance(resolution, Mapping) else {}
    accepted = resolution.get("status") == "accepted"
    nodes = snapshot.get("nodes", ())
    nodes = (
        nodes
        if isinstance(nodes, Sequence) and not isinstance(nodes, (str, bytes))
        else ()
    )
    artifacts = snapshot.get("artifacts", ())
    artifacts = (
        artifacts
        if isinstance(artifacts, Sequence)
        and not isinstance(artifacts, (str, bytes))
        else ()
    )
    task = snapshot.get("task", {})
    task = task if isinstance(task, Mapping) else {}
    reproduction_pending = bool(task.get("requires_failure_reproduction")) and not bool(
        task.get("confirmed_failure_reproduction_refs")
    )
    artifact_types = {
        str(item.get("artifact_type", ""))
        for item in artifacts
        if isinstance(item, Mapping)
    }
    artifact_refs_by_type: dict[str, list[str]] = {}
    for item in artifacts:
        if not isinstance(item, Mapping) or not item.get("artifact_ref"):
            continue
        artifact_refs_by_type.setdefault(
            str(item.get("artifact_type", "")), []
        ).append(str(item["artifact_ref"]))
    hypothesis_ready_refs = {
        str(item["artifact_ref"])
        for item in artifacts
        if isinstance(item, Mapping)
        and item.get("artifact_type") == "hypothesis"
        and item.get("artifact_ref")
        and isinstance(item.get("content"), Mapping)
        and not item.get("content", {}).get("missing_evidence")
    }
    acceptable_review_refs: set[str] = set()
    reviewed_ready_refs: set[str] = set()
    for item in artifacts:
        if (
            not isinstance(item, Mapping)
            or item.get("artifact_type") != "review"
            or not item.get("artifact_ref")
            or not isinstance(item.get("content"), Mapping)
        ):
            continue
        content = item["content"]
        if (
            content.get("mode")
            not in {"root_cause_recommendation", "hypothesis_comparison"}
            or content.get("verdict")
            not in {"approved", "compatible", "supported"}
        ):
            continue
        raw_targets = content.get("target_artifact_refs")
        if raw_targets is None:
            raw_targets = (content.get("target_artifact_ref"),)
        if isinstance(raw_targets, (str, bytes)):
            raw_targets = (raw_targets,)
        covered = {
            str(ref) for ref in raw_targets if str(ref) in hypothesis_ready_refs
        }
        if covered:
            acceptable_review_refs.add(str(item["artifact_ref"]))
            reviewed_ready_refs.update(covered)
    recovery = snapshot.get("recovery", {})
    recovery = recovery if isinstance(recovery, Mapping) else {}
    pending_replan = recovery.get("pending_replan") is True
    remaining_replans = int(recovery.get("remaining_replans", 1) or 0)
    patch_refs = set(artifact_refs_by_type.get("patch_candidate", ()))
    validations_by_patch = {
        str(item.get("content", {}).get("patch_ref", "")): item
        for item in artifacts
        if isinstance(item, Mapping)
        and item.get("artifact_type") == "validation_result"
        and isinstance(item.get("content"), Mapping)
        and item.get("content", {}).get("patch_ref")
    }
    unvalidated_patch_refs = patch_refs.difference(validations_by_patch)
    passed_validation_exists = any(
        item.get("content", {}).get("passed") is True
        for item in validations_by_patch.values()
    )
    investigation_required = additional_investigation_required(artifacts)

    allowed_actions = {
        DecisionAction.CREATE_TASK.value,
        DecisionAction.TERMINATE_TASK.value,
    }
    if stage not in {"initialization", "finalization", "completed", "terminated"}:
        allowed_actions.add(DecisionAction.CHANGE_WORKFLOW_STAGE.value)
    if stage == "investigation" and reproduction_pending:
        allowed_actions.discard(DecisionAction.CHANGE_WORKFLOW_STAGE.value)
    if stage in {"review", "patch", "validation", "replan"}:
        allowed_actions.add(DecisionAction.REQUEST_REPLAN.value)
    if remaining_replans <= 0 and not pending_replan:
        allowed_actions.discard(DecisionAction.REQUEST_REPLAN.value)
    if (
        stage in {"diagnosis", "review"}
        and not accepted
        and reviewed_ready_refs
        and acceptable_review_refs
    ):
        allowed_actions.add(DecisionAction.ACCEPT_HYPOTHESIS.value)
    if stage == "validation" and "validation_result" in artifact_types:
        allowed_actions.update(
            {
                DecisionAction.SELECT_PATCH.value,
                DecisionAction.FINALIZE_TASK.value,
            }
        )
    if stage == "finalization" and "validation_result" in artifact_types:
        allowed_actions.add(DecisionAction.FINALIZE_TASK.value)
    if any(
        isinstance(item, Mapping)
        and str(item.get("status", "")) in {"PENDING", "READY", "RUNNING"}
        for item in nodes
    ):
        allowed_actions.update(
            {
                DecisionAction.CANCEL_TASK.value,
                DecisionAction.PAUSE_TASK.value,
            }
        )
    if any(
        isinstance(item, Mapping) and str(item.get("status", "")) == "PAUSED"
        for item in nodes
    ):
        allowed_actions.add(DecisionAction.RESUME_TASK.value)

    allowed_node_types = set(_STAGE_CREATE_NODE_TYPES.get(stage, ()))
    if reproduction_pending:
        allowed_node_types.discard("DIAGNOSIS_TASK")
        allowed_node_types.discard("REVIEW_TASK")
    if pending_replan:
        allowed_actions = {DecisionAction.CREATE_TASK.value}
        target_node_type = str(recovery.get("target_node_type", ""))
        allowed_node_types = {target_node_type} if target_node_type else set()
    if accepted and stage in {"diagnosis", "review"}:
        allowed_node_types.add("PATCH_TASK")
    if not accepted:
        allowed_node_types.discard("PATCH_TASK")
        allowed_node_types.discard("VALIDATION_TASK")
    if patch_refs and not pending_replan:
        allowed_node_types.discard("PATCH_TASK")
    if not pending_replan and not investigation_required:
        allowed_node_types.discard("INVESTIGATION_TASK")
    if not unvalidated_patch_refs:
        allowed_node_types.discard("VALIDATION_TASK")
    if (
        stage in {"patch", "validation"}
        and patch_refs
        and not unvalidated_patch_refs
        and not passed_validation_exists
        and remaining_replans <= 0
        and not pending_replan
    ):
        allowed_actions = {DecisionAction.TERMINATE_TASK.value}
        allowed_node_types.clear()
    if not allowed_node_types:
        allowed_actions.discard(DecisionAction.CREATE_TASK.value)
    gate_required_node_types = {"CHALLENGE_TASK", "REBUTTAL_TASK"}
    active_statuses = {"PENDING", "READY", "RUNNING", "PAUSED", "SUCCEEDED"}
    for node_type in ("INVESTIGATION_TASK", "DIAGNOSIS_TASK", "PATCH_TASK"):
        if any(
            isinstance(item, Mapping)
            and str(item.get("node_type", "")) == node_type
            and str(item.get("status", "")) in active_statuses
            for item in nodes
        ):
            gate_required_node_types.add(node_type)

    schema = supervisor_decision_schema()
    filtered_variants = [
        variant
        for variant in schema["oneOf"]
        if variant["properties"]["action"]["const"] in allowed_actions
    ]
    variants: list[dict[str, Any]] = []
    for variant in filtered_variants:
        action = variant["properties"]["action"]["const"]
        if action == DecisionAction.REQUEST_REPLAN.value:
            for failure_class, target_stage in _REPLAN_TARGET_BY_FAILURE_CLASS.items():
                constrained = copy.deepcopy(variant)
                constrained["properties"]["failure_class"] = {
                    "const": failure_class
                }
                constrained["properties"]["next_workflow_stage"] = {
                    "const": target_stage
                }
                variants.append(constrained)
            continue
        constrained = copy.deepcopy(variant)
        if action == DecisionAction.ACCEPT_HYPOTHESIS.value:
            allowed_hypotheses = sorted(reviewed_ready_refs)
            allowed_reviews = sorted(acceptable_review_refs)
            constrained["properties"]["hypothesis_refs"]["items"] = {
                "type": "string",
                "enum": allowed_hypotheses,
            }
            constrained["properties"]["primary_hypothesis_ref"] = {
                "type": "string",
                "enum": allowed_hypotheses,
            }
            constrained["properties"]["review_refs"]["items"] = {
                "type": "string",
                "enum": allowed_reviews,
            }
        if action == DecisionAction.CREATE_TASK.value:
            allowed_next_stages = tuple(
                dict.fromkeys((stage, *_LEGAL_STAGE_TRANSITIONS.get(stage, ())))
            )
            if not accepted:
                allowed_next_stages = tuple(
                    item
                    for item in allowed_next_stages
                    if item not in {"patch", "validation", "finalization", "completed"}
                )
            constrained["properties"]["next_workflow_stage"] = {
                "type": "string",
                "enum": list(allowed_next_stages),
            }
            if stage == "initialization":
                constrained["properties"]["next_workflow_stage"] = {
                    "const": "investigation"
                }
                if "next_workflow_stage" not in constrained["required"]:
                    constrained["required"].append("next_workflow_stage")
            if pending_replan:
                trigger_refs = tuple(
                    dict.fromkeys(
                        (
                            str(recovery.get("replan_ref", "")),
                            *(
                                str(item)
                                for item in recovery.get("trigger_refs", ())
                                if str(item)
                            ),
                        )
                    )
                )
                trigger_refs = tuple(ref for ref in trigger_refs if ref)
                exact_triggers = {
                    "type": "array",
                    "items": {"type": "string", "enum": list(trigger_refs)},
                    "minItems": len(trigger_refs),
                    "maxItems": len(trigger_refs),
                    "uniqueItems": True,
                }
                constrained["properties"]["evidence_refs"] = exact_triggers
                constrained["properties"]["gate_record"]["properties"][
                    "trigger_artifact_refs"
                ] = copy.deepcopy(exact_triggers)
                for required_field in ("evidence_refs", "gate_record"):
                    if required_field not in constrained["required"]:
                        constrained["required"].append(required_field)
        elif action == DecisionAction.CHANGE_WORKFLOW_STAGE.value:
            allowed_next_stages = _LEGAL_STAGE_TRANSITIONS.get(stage, ())
            if not accepted:
                allowed_next_stages = tuple(
                    item
                    for item in allowed_next_stages
                    if item not in {"patch", "validation", "finalization", "completed"}
                )
            constrained["properties"]["next_workflow_stage"] = {
                "type": "string",
                "enum": list(allowed_next_stages),
            }
        variants.append(constrained)

    for variant in variants:
        if (
            variant["properties"]["action"]["const"]
            != DecisionAction.CREATE_TASK.value
        ):
            continue
        task_variants = variant["properties"]["create_tasks"]["items"]["oneOf"]
        allowed_task_variants = [
            task_variant
            for task_variant in task_variants
            if task_variant["properties"]["node_type"]["const"]
            in allowed_node_types
        ]
        contracted_task_variants = [
            constrained
            for task_variant in allowed_task_variants
            for constrained in _task_contract_variants(
                task_variant,
                artifact_refs_by_type,
            )
        ]
        if not investigation_required:
            contracted_task_variants = [
                item
                for item in contracted_task_variants
                if not (
                    item["properties"]["agent_type"]["const"]
                    == "ReviewerAgent"
                    and item["properties"]["mode"]["const"]
                    == "evidence_review"
                )
            ]
        variant["properties"]["create_tasks"]["items"]["oneOf"] = (
            contracted_task_variants
        )

    final_variants: list[dict[str, Any]] = []
    for variant in variants:
        if (
            pending_replan
            or variant["properties"]["action"]["const"]
            != DecisionAction.CREATE_TASK.value
        ):
            final_variants.append(variant)
            continue
        task_variants = variant["properties"]["create_tasks"]["items"]["oneOf"]
        gated_types = {
            str(item["properties"]["node_type"]["const"])
            for item in task_variants
            if str(item["properties"]["node_type"]["const"])
            in gate_required_node_types
        }
        if not gated_types:
            expandable_types = {
                "INVESTIGATION_TASK",
                "DIAGNOSIS_TASK",
                "PATCH_TASK",
            }
            available_types = {
                str(item["properties"]["node_type"]["const"])
                for item in task_variants
            }
            if (
                available_types.intersection(expandable_types)
                and artifact_refs_by_type
            ):
                gated = copy.deepcopy(variant)
                gated["properties"]["create_tasks"]["minItems"] = 2
                for required_field in ("evidence_refs", "gate_record"):
                    if required_field not in gated["required"]:
                        gated["required"].append(required_field)
                gated["properties"]["evidence_refs"]["minItems"] = 1
                final_variants.append(gated)
                variant["properties"]["create_tasks"]["maxItems"] = 1
            elif available_types.intersection(expandable_types):
                variant["properties"]["create_tasks"]["maxItems"] = 1
            variant["properties"].pop("gate_record", None)
            final_variants.append(variant)
            continue
        ungated_task_variants = [
            item
            for item in task_variants
            if str(item["properties"]["node_type"]["const"])
            not in gated_types
        ]
        if ungated_task_variants:
            ungated = copy.deepcopy(variant)
            ungated["properties"]["create_tasks"]["items"][
                "oneOf"
            ] = ungated_task_variants
            ungated["properties"].pop("gate_record", None)
            final_variants.append(ungated)
        gated = copy.deepcopy(variant)
        gated["properties"]["create_tasks"]["items"]["oneOf"] = [
            item
            for item in task_variants
            if str(item["properties"]["node_type"]["const"])
            in gated_types
        ]
        for required_field in ("evidence_refs", "gate_record"):
            if required_field not in gated["required"]:
                gated["required"].append(required_field)
        gated["properties"]["evidence_refs"]["minItems"] = 1
        final_variants.append(gated)
    return {"oneOf": final_variants}


_TASK_MODE_ARTIFACT_COUNTS: dict[
    tuple[str, str], dict[str, tuple[int, int | None]]
] = {
    ("DiagnosticianAgent", "control_flow"): {"evidence": (1, None)},
    ("DiagnosticianAgent", "data_flow"): {"evidence": (1, None)},
    ("DiagnosticianAgent", "challenge"): {
        "evidence": (1, None),
        "hypothesis": (1, 1),
    },
    ("DiagnosticianAgent", "rebuttal"): {
        "evidence": (1, None),
        "hypothesis": (1, 1),
        "challenge": (1, 1),
    },
    ("ReviewerAgent", "evidence_review"): {"evidence": (1, None)},
    ("ReviewerAgent", "root_cause_recommendation"): {
        "evidence": (1, None),
        "hypothesis": (1, 1),
    },
    ("ReviewerAgent", "hypothesis_comparison"): {
        "evidence": (1, None),
        "hypothesis": (2, None),
    },
    ("ReviewerAgent", "challenge_quality"): {
        "evidence": (1, None),
        "hypothesis": (1, None),
        "challenge": (1, None),
    },
    ("ReviewerAgent", "patch_review"): {
        "evidence": (1, None),
        "hypothesis": (1, None),
        "patch_candidate": (1, 1),
        "review": (1, None),
    },
    ("ReviewerAgent", "final_risk_review"): {
        "evidence": (1, None),
        "patch_candidate": (1, 1),
        "validation_result": (1, 1),
        "review": (1, None),
    },
    ("PatchAgent", "minimal"): {"hypothesis": (1, None), "review": (1, None)},
    ("PatchAgent", "robust"): {"hypothesis": (1, None), "review": (1, None)},
    ("PatchAgent", "minimal_critiques_robust"): {
        "evidence": (1, None),
        "patch_candidate": (2, 2),
    },
    ("PatchAgent", "robust_critiques_minimal"): {
        "evidence": (1, None),
        "patch_candidate": (2, 2),
    },
    ("ValidationExecutor", "deterministic"): {
        "patch_candidate": (1, 1),
        "review": (1, None),
    },
}

_TASK_MODE_ALLOWED_ARTIFACT_TYPES: dict[tuple[str, str], frozenset[str]] = {
    ("DiagnosticianAgent", "control_flow"): frozenset({"evidence"}),
    ("DiagnosticianAgent", "data_flow"): frozenset({"evidence"}),
    ("DiagnosticianAgent", "challenge"): frozenset(
        {"evidence", "hypothesis"}
    ),
    ("DiagnosticianAgent", "rebuttal"): frozenset(
        {"evidence", "hypothesis", "challenge"}
    ),
}


def _task_contract_variants(
    task_variant: Mapping[str, Any],
    refs_by_type: Mapping[str, Sequence[str]],
) -> list[dict[str, Any]]:
    """Split Worker modes and encode their minimum Artifact input contracts."""

    properties = task_variant["properties"]
    agent_type = str(properties["agent_type"]["const"])
    mode_schema = properties["mode"]
    modes = (
        (str(mode_schema["const"]),)
        if "const" in mode_schema
        else tuple(str(item) for item in mode_schema.get("enum", ()))
    )
    variants: list[dict[str, Any]] = []
    for mode in modes:
        counts = _TASK_MODE_ARTIFACT_COUNTS.get((agent_type, mode), {})
        if any(len(refs_by_type.get(kind, ())) < minimum for kind, (minimum, _) in counts.items()):
            continue
        allowed_types = _TASK_MODE_ALLOWED_ARTIFACT_TYPES.get((agent_type, mode))
        all_refs = [
            ref
            for artifact_type, refs in refs_by_type.items()
            if allowed_types is None or artifact_type in allowed_types
            for ref in refs
        ]
        constrained = copy.deepcopy(task_variant)
        constrained["properties"]["mode"] = {"const": mode}
        input_schema: dict[str, Any] = {
            "type": "array",
            "items": {"type": "string", "enum": all_refs},
            "uniqueItems": True,
        }
        input_schema["allOf"] = []
        for artifact_type, (minimum, maximum) in counts.items():
            requirement: dict[str, Any] = {
                "type": "array",
                "contains": {
                    "type": "string",
                    "enum": list(refs_by_type.get(artifact_type, ())),
                },
                "minContains": minimum,
            }
            if maximum is not None:
                requirement["maxContains"] = maximum
            input_schema["allOf"].append(requirement)
        constrained["properties"]["input_artifact_ids"] = input_schema
        if counts and "input_artifact_ids" not in constrained["required"]:
            constrained["required"].append("input_artifact_ids")
        variants.append(constrained)
    return variants


def _strings(value: Sequence[Any]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError("expected a sequence, not a string")
    return tuple(str(item) for item in value)


def _unique_strings(value: Sequence[Any]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError("expected a sequence, not a string")
    return tuple(
        dict.fromkeys(
            str(item)
            for item in value
            if str(item)
        )
    )
