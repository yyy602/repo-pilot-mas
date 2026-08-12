"""Append-only Artifact Store and versioned Blackboard."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from repo_pilot_mas.schemas.artifact import Artifact, ArtifactType
from repo_pilot_mas.schemas.hypothesis_resolution import (
    HypothesisResolution,
    HypothesisResolutionStatus,
)

_ROOT_CAUSE_REVIEW_MODES = {
    "hypothesis_comparison",
    "root_cause_recommendation",
}


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
    hypothesis_resolution: HypothesisResolution = field(default_factory=HypothesisResolution)

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("blackboard task_id must not be empty")
        if self.state_version < 0:
            raise ValueError("state_version must be non-negative")
        self.task_spec = dict(self.task_spec)
        self.messages = [dict(item) for item in self.messages]
        self.trace_refs = [str(item) for item in self.trace_refs]
        if isinstance(self.hypothesis_resolution, Mapping):
            self.hypothesis_resolution = HypothesisResolution.from_dict(
                self.hypothesis_resolution
            )
        elif not isinstance(self.hypothesis_resolution, HypothesisResolution):
            raise TypeError("hypothesis_resolution must be HypothesisResolution")
        self._reconcile_hypothesis_compatibility()

    def _reconcile_hypothesis_compatibility(self) -> None:
        legacy_ref = self.selected_hypothesis_ref
        resolution = self.hypothesis_resolution
        if legacy_ref is not None:
            artifact = self.artifacts.get(legacy_ref)
            if artifact.artifact_type is not ArtifactType.HYPOTHESIS:
                raise ValueError("selected hypothesis must reference a hypothesis artifact")
            canonical_ref = artifact.ref
            if resolution.status is HypothesisResolutionStatus.ACCEPTED:
                if resolution.primary_ref != canonical_ref:
                    raise ValueError(
                        "selected_hypothesis_ref conflicts with hypothesis_resolution"
                    )
            elif resolution.candidate_refs:
                raise ValueError(
                    "legacy selected_hypothesis_ref cannot override an unresolved resolution"
                )
            else:
                self.hypothesis_resolution = HypothesisResolution.legacy_accepted(
                    canonical_ref
                )
            self.selected_hypothesis_ref = canonical_ref
            return
        if resolution.status is HypothesisResolutionStatus.ACCEPTED:
            self.selected_hypothesis_ref = resolution.primary_ref

    def bump(self) -> int:
        self.state_version += 1
        return self.state_version

    def add_artifact(self, artifact: Artifact) -> str:
        if (
            artifact.artifact_type is ArtifactType.REVIEW
            and str(artifact.content.get("mode", "")) in _ROOT_CAUSE_REVIEW_MODES
        ):
            self._review_target_refs(artifact)
        ref = self.artifacts.add(artifact)
        self._update_hypothesis_resolution_for_artifact(artifact)
        self.bump()
        return ref

    def _update_hypothesis_resolution_for_artifact(self, artifact: Artifact) -> None:
        if artifact.artifact_type is ArtifactType.HYPOTHESIS:
            if (
                artifact.supersedes
                and artifact.supersedes in self.hypothesis_resolution.accepted_refs
            ):
                self._invalidate_hypothesis_selection(
                    f"accepted hypothesis superseded by {artifact.ref}"
                )
            self.hypothesis_resolution.observe_candidates((artifact.ref,))
            return
        if artifact.artifact_type is not ArtifactType.REVIEW:
            return
        mode = str(artifact.content.get("mode", ""))
        if mode not in _ROOT_CAUSE_REVIEW_MODES:
            return
        targets = self._review_target_refs(artifact)
        self.hypothesis_resolution.mark_reviewed(
            artifact.ref,
            str(artifact.content.get("verdict", "")),
            target_refs=targets,
            metadata={"latest_root_cause_review_mode": mode},
        )
        self.selected_hypothesis_ref = None
        self.selected_patch_ref = None
        self.validation_ref = None

    def _review_target_refs(self, artifact: Artifact) -> tuple[str, ...]:
        raw_targets = artifact.content.get("target_artifact_refs")
        if raw_targets is None:
            target = artifact.content.get("target_artifact_ref")
            raw_targets = (target,) if target else ()
        if isinstance(raw_targets, (str, bytes)):
            raw_targets = (raw_targets,)
        targets: list[str] = []
        for value in raw_targets:
            target = self.artifacts.get(str(value))
            if target.artifact_type is not ArtifactType.HYPOTHESIS:
                raise ValueError("root-cause Review may target only Hypothesis artifacts")
            targets.append(target.ref)
        return tuple(dict.fromkeys(targets))

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

    def set_hypotheses(
        self,
        refs: Sequence[str],
        *,
        primary_ref: str,
        review_refs: Sequence[str],
        decision_id: str,
    ) -> None:
        normalized_refs = self._latest_refs(refs, ArtifactType.HYPOTHESIS)
        normalized_reviews = self._latest_refs(review_refs, ArtifactType.REVIEW)
        primary = self.artifacts.get(primary_ref)
        if primary.artifact_type is not ArtifactType.HYPOTHESIS:
            raise ValueError("primary hypothesis must reference a Hypothesis artifact")
        if primary.ref not in normalized_refs:
            raise ValueError("primary hypothesis must belong to accepted hypothesis set")
        previous = self.hypothesis_resolution.accepted_refs
        self.hypothesis_resolution.accept(
            normalized_refs,
            primary_ref=primary.ref,
            review_refs=normalized_reviews,
            decision_id=decision_id,
            state_version=self.state_version + 1,
        )
        self.selected_hypothesis_ref = primary.ref
        if previous != normalized_refs:
            self.selected_patch_ref = None
            self.validation_ref = None
        self.bump()

    def set_hypothesis(self, ref: str) -> None:
        """Legacy single-root selection retained for old checkpoints and tests."""

        artifact = self.artifacts.get(ref)
        if artifact.artifact_type is not ArtifactType.HYPOTHESIS:
            raise ValueError("selected hypothesis must reference a hypothesis artifact")
        if not self.artifacts.is_latest(artifact.ref):
            raise ValueError("selected hypothesis is not the latest revision")
        self.hypothesis_resolution = HypothesisResolution.legacy_accepted(artifact.ref)
        self.selected_hypothesis_ref = artifact.ref
        self.selected_patch_ref = None
        self.validation_ref = None
        self.bump()

    def invalidate_hypotheses(self, reason: str) -> None:
        self._invalidate_hypothesis_selection(reason)
        self.bump()

    def _invalidate_hypothesis_selection(self, reason: str) -> None:
        self.hypothesis_resolution.invalidate(reason)
        self.selected_hypothesis_ref = None
        self.selected_patch_ref = None
        self.validation_ref = None

    def _latest_refs(
        self,
        refs: Sequence[str],
        expected_type: ArtifactType,
    ) -> tuple[str, ...]:
        normalized: list[str] = []
        for ref in refs:
            artifact = self.artifacts.get(ref)
            if artifact.artifact_type is not expected_type:
                raise ValueError(
                    f"expected {expected_type.value} Artifact, got {artifact.artifact_type.value}"
                )
            if not self.artifacts.is_latest(artifact.ref):
                raise ValueError(f"artifact is not the latest revision: {artifact.ref}")
            normalized.append(artifact.ref)
        result = tuple(dict.fromkeys(normalized))
        if not result:
            raise ValueError(f"at least one {expected_type.value} Artifact is required")
        return result

    def set_patch(self, patch_ref: str, validation_ref: str) -> None:
        patch = self.artifacts.get(patch_ref)
        validation = self.artifacts.get(validation_ref)
        if patch.artifact_type is not ArtifactType.PATCH_CANDIDATE:
            raise ValueError("selected patch must reference a patch candidate")
        if validation.artifact_type is not ArtifactType.VALIDATION_RESULT:
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
            "hypothesis_resolution": self.hypothesis_resolution.to_dict(),
            "selected_patch_ref": self.selected_patch_ref,
            "validation_ref": self.validation_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Blackboard:
        artifact_values = value.get("artifacts", ())
        if isinstance(artifact_values, (str, bytes)):
            raise TypeError("blackboard artifacts must be a sequence")
        artifacts = ArtifactStore.from_list(artifact_values)
        resolution_value = value.get("hypothesis_resolution")
        selected_hypothesis_ref = value.get("selected_hypothesis_ref")
        if isinstance(resolution_value, Mapping):
            resolution = HypothesisResolution.from_dict(resolution_value)
        elif selected_hypothesis_ref:
            selected = artifacts.get(str(selected_hypothesis_ref))
            resolution = HypothesisResolution.legacy_accepted(selected.ref)
        else:
            candidates = tuple(
                artifact.ref
                for artifact in artifacts.latest_values()
                if artifact.artifact_type is ArtifactType.HYPOTHESIS
            )
            resolution = HypothesisResolution.unresolved(candidates)
        return cls(
            task_id=str(value["task_id"]),
            task_spec=dict(value["task_spec"]),
            workflow_stage=str(value.get("workflow_stage", "initialization")),
            state_version=int(value.get("state_version", 0)),
            artifacts=artifacts,
            messages=[dict(item) for item in value.get("messages", ())],
            trace_refs=[str(item) for item in value.get("trace_refs", ())],
            selected_hypothesis_ref=(
                str(selected_hypothesis_ref) if selected_hypothesis_ref else None
            ),
            selected_patch_ref=(
                str(value["selected_patch_ref"])
                if value.get("selected_patch_ref")
                else None
            ),
            validation_ref=(str(value["validation_ref"]) if value.get("validation_ref") else None),
            hypothesis_resolution=resolution,
        )
