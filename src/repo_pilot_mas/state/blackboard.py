"""Append-only Artifact Store and versioned Blackboard."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from repo_pilot_mas.schemas.artifact import Artifact


class ArtifactStore:
    def __init__(self, artifacts: Sequence[Artifact] = ()) -> None:
        self._by_ref: dict[str, Artifact] = {}
        self._latest: dict[str, str] = {}
        for artifact in artifacts:
            self.add(artifact)

    def add(self, artifact: Artifact) -> str:
        if artifact.ref in self._by_ref:
            raise ValueError(f"artifact version already exists: {artifact.ref}")
        latest_ref = self._latest.get(artifact.artifact_id)
        if latest_ref is None:
            if artifact.version != 1 or artifact.supersedes is not None:
                raise ValueError("new artifact must start at version 1 without supersedes")
        else:
            latest = self._by_ref[latest_ref]
            if artifact.artifact_type is not latest.artifact_type:
                raise ValueError("artifact revision cannot change artifact_type")
            if artifact.version != latest.version + 1:
                raise ValueError("artifact revision must increment the latest version by one")
            if artifact.supersedes != latest.ref:
                raise ValueError("artifact revision must supersede the latest version")
        for input_ref in artifact.input_refs:
            if input_ref not in self._by_ref:
                raise ValueError(f"artifact references an unknown input: {input_ref}")
        self._by_ref[artifact.ref] = artifact
        self._latest[artifact.artifact_id] = artifact.ref
        return artifact.ref

    def get(self, ref_or_id: str) -> Artifact:
        ref = self._latest.get(ref_or_id, ref_or_id)
        try:
            return self._by_ref[ref]
        except KeyError as exc:
            raise KeyError(f"unknown artifact: {ref_or_id}") from exc

    def contains(self, ref_or_id: str) -> bool:
        return ref_or_id in self._latest or ref_or_id in self._by_ref

    def is_latest(self, ref: str) -> bool:
        artifact = self.get(ref)
        return self._latest[artifact.artifact_id] == artifact.ref

    def values(self) -> tuple[Artifact, ...]:
        return tuple(self._by_ref.values())

    def latest_values(self) -> tuple[Artifact, ...]:
        return tuple(self._by_ref[ref] for ref in self._latest.values())

    def to_list(self) -> list[dict[str, Any]]:
        return [artifact.to_dict() for artifact in self._by_ref.values()]

    @classmethod
    def from_list(cls, values: Sequence[Mapping[str, Any]]) -> ArtifactStore:
        return cls([Artifact.from_dict(value) for value in values])


@dataclass(slots=True)
class Blackboard:
    task_id: str
    task_spec: Mapping[str, Any]
    workflow_stage: str = "initialization"
    state_version: int = 0
    artifacts: ArtifactStore = field(default_factory=ArtifactStore)
    messages: list[dict[str, Any]] = field(default_factory=list)
    trace_refs: list[str] = field(default_factory=list)
    selected_hypothesis_ref: str | None = None
    selected_patch_ref: str | None = None
    validation_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("blackboard task_id must not be empty")
        if self.state_version < 0:
            raise ValueError("state_version must be non-negative")
        self.task_spec = dict(self.task_spec)

    def bump(self) -> int:
        self.state_version += 1
        return self.state_version

    def add_artifact(self, artifact: Artifact) -> str:
        ref = self.artifacts.add(artifact)
        self.bump()
        return ref

    def record_message(self, message: Mapping[str, Any]) -> None:
        self.messages.append(dict(message))
        self.bump()

    def add_trace_ref(self, trace_ref: str) -> None:
        if trace_ref not in self.trace_refs:
            self.trace_refs.append(trace_ref)
            self.bump()

    def set_stage(self, workflow_stage: str) -> None:
        if not workflow_stage.strip():
            raise ValueError("workflow_stage must not be empty")
        if workflow_stage != self.workflow_stage:
            self.workflow_stage = workflow_stage
            self.bump()

    def set_hypothesis(self, ref: str) -> None:
        artifact = self.artifacts.get(ref)
        if artifact.artifact_type.value != "hypothesis":
            raise ValueError("selected hypothesis must reference a hypothesis artifact")
        if not self.artifacts.is_latest(artifact.ref):
            raise ValueError("selected hypothesis is not the latest revision")
        self.selected_hypothesis_ref = artifact.ref
        self.bump()

    def set_patch(self, patch_ref: str, validation_ref: str) -> None:
        patch = self.artifacts.get(patch_ref)
        validation = self.artifacts.get(validation_ref)
        if patch.artifact_type.value != "patch_candidate":
            raise ValueError("selected patch must reference a patch candidate")
        if validation.artifact_type.value != "validation_result":
            raise ValueError("validation_ref must reference a validation result")
        if not self.artifacts.is_latest(patch.ref) or not self.artifacts.is_latest(validation.ref):
            raise ValueError("patch selection must use latest artifact revisions")
        if not validation.content.get("passed"):
            raise ValueError("selected patch validation did not pass")
        if validation.content.get("patch_ref") not in {patch.ref, patch.artifact_id}:
            raise ValueError("validation result belongs to another patch")
        self.selected_patch_ref = patch.ref
        self.validation_ref = validation.ref
        self.bump()

    def artifact_summaries(self) -> list[dict[str, Any]]:
        return [
            {
                "artifact_ref": artifact.ref,
                "artifact_type": artifact.artifact_type.value,
                "status": artifact.status,
                "created_by": artifact.created_by,
                "content": artifact.to_dict()["content"],
            }
            for artifact in self.artifacts.latest_values()
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_spec": dict(self.task_spec),
            "workflow_stage": self.workflow_stage,
            "state_version": self.state_version,
            "artifacts": self.artifacts.to_list(),
            "messages": list(self.messages),
            "trace_refs": list(self.trace_refs),
            "selected_hypothesis_ref": self.selected_hypothesis_ref,
            "selected_patch_ref": self.selected_patch_ref,
            "validation_ref": self.validation_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Blackboard:
        artifact_values = value.get("artifacts", ())
        if isinstance(artifact_values, (str, bytes)):
            raise TypeError("blackboard artifacts must be a sequence")
        return cls(
            task_id=str(value["task_id"]),
            task_spec=dict(value["task_spec"]),
            workflow_stage=str(value.get("workflow_stage", "initialization")),
            state_version=int(value.get("state_version", 0)),
            artifacts=ArtifactStore.from_list(artifact_values),
            messages=[dict(item) for item in value.get("messages", ())],
            trace_refs=[str(item) for item in value.get("trace_refs", ())],
            selected_hypothesis_ref=value.get("selected_hypothesis_ref"),
            selected_patch_ref=value.get("selected_patch_ref"),
            validation_ref=value.get("validation_ref"),
        )
