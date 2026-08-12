"""Shared, context-isolated primitives for Phase 4 Worker agents."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from repo_pilot_mas.models import GenerationConfig, Message, ModelAdapter
from repo_pilot_mas.runtime import TraceWriter
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec
from repo_pilot_mas.schemas.worker_artifact import validate_worker_artifact


class WorkerAgentError(RuntimeError):
    """A bounded Worker failure that must not mutate orchestration state."""

    def __init__(self, message: str, *, code: str = "MODEL_FORMAT_ERROR") -> None:
        super().__init__(message)
        self.code = code


def generate_artifact(
    model: ModelAdapter,
    *,
    task: TaskSpec,
    node_id: str,
    mode: str,
    objective: str,
    artifacts: Sequence[Artifact],
    artifact_type: ArtifactType,
    content_schema: Mapping[str, Any],
    system_prompt: str,
    generation_config: GenerationConfig,
    trace_writer: TraceWriter | None,
    content_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> Artifact:
    refs = tuple(artifact.ref for artifact in artifacts)
    context = {
        "task": worker_task_view(task),
        "worker": {"node_id": node_id, "mode": mode, "objective": objective},
        "input_artifacts": [artifact.to_dict() for artifact in artifacts],
    }
    _trace_context(trace_writer, node_id, mode, artifacts)
    response = model.generate(
        (
            Message("system", system_prompt),
            Message("user", json.dumps(context, ensure_ascii=False, separators=(",", ":"))),
        ),
        response_schema=content_schema,
        config=generation_config,
    )
    if response.structured_output is None:
        raise WorkerAgentError("模型没有返回结构化 Artifact 内容")
    content = _canonicalize_refs(
        dict(response.structured_output),
        artifact_type,
        artifacts,
    )
    if content_transform is not None:
        content = content_transform(content)
    artifact = Artifact(
        artifact_id=f"{node_id}.{artifact_type.value}",
        artifact_type=artifact_type,
        created_by=node_id,
        content=content,
        input_refs=refs,
        trace_id=response.trace_id,
    )
    validate_worker_artifact(
        artifact,
        expected_type=artifact_type,
        allowed_input_refs=refs,
    )
    _trace_artifact(trace_writer, artifact, response.model_id)
    return artifact


def artifact_inputs(values: Sequence[Mapping[str, Any]]) -> tuple[Artifact, ...]:
    return tuple(Artifact.from_dict(value) for value in values)


def worker_task_view(task: TaskSpec) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "issue": task.issue,
        "failing_tests": list(task.failing_tests),
        "acceptance_criteria": list(task.acceptance_criteria),
        "target_files": list(task.target_files),
        "protected_paths": list(task.protected_paths),
        "metadata": dict(task.metadata),
    }


def _trace_context(
    trace_writer: TraceWriter | None,
    node_id: str,
    mode: str,
    artifacts: Sequence[Artifact],
) -> None:
    if trace_writer is not None:
        trace_writer.write(
            "worker_context_built",
            {
                "node_id": node_id,
                "mode": mode,
                "input_refs": [artifact.ref for artifact in artifacts],
                "input_types": [artifact.artifact_type.value for artifact in artifacts],
            },
        )


def _trace_artifact(
    trace_writer: TraceWriter | None,
    artifact: Artifact,
    model_id: str,
) -> None:
    if trace_writer is not None:
        trace_writer.write(
            "worker_artifact_created",
            {"artifact": artifact.to_dict(), "model_id": model_id},
            trace_id=artifact.trace_id,
        )


def _canonicalize_refs(
    content: dict[str, Any],
    artifact_type: ArtifactType,
    artifacts: Sequence[Artifact],
) -> dict[str, Any]:
    lookup = {artifact.ref: artifact.ref for artifact in artifacts}
    lookup.update({artifact.artifact_id: artifact.ref for artifact in artifacts})

    def canonical(value: Any) -> str:
        text = str(value)
        return lookup.get(text, text)

    if artifact_type is ArtifactType.HYPOTHESIS:
        content["supporting_evidence"] = [
            canonical(item) for item in content.get("supporting_evidence", ())
        ]
    elif artifact_type is ArtifactType.REVIEW:
        content["target_artifact_ref"] = canonical(content.get("target_artifact_ref", ""))
        if "target_artifact_refs" in content:
            content["target_artifact_refs"] = [
                canonical(item) for item in content.get("target_artifact_refs", ())
            ]
        content["evidence_refs"] = [canonical(item) for item in content.get("evidence_refs", ())]
    elif artifact_type is ArtifactType.CHALLENGE:
        content["challenged_hypothesis_ref"] = canonical(
            content.get("challenged_hypothesis_ref", "")
        )
    return content


__all__ = [
    "WorkerAgentError",
    "artifact_inputs",
    "generate_artifact",
    "worker_task_view",
]
