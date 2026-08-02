"""Targeted review Worker without routing authority."""

from __future__ import annotations

from collections.abc import Sequence

from repo_pilot_mas.agents.worker_common import WorkerAgentError, generate_artifact
from repo_pilot_mas.models import GenerationConfig, ModelAdapter
from repo_pilot_mas.runtime import TraceWriter
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec
from repo_pilot_mas.schemas.worker_artifact import REVIEW_CONTENT_SCHEMA, REVIEWER_MODES


class ReviewerAgent:
    def __init__(
        self,
        model: ModelAdapter,
        *,
        trace_writer: TraceWriter | None = None,
        generation_config: GenerationConfig | None = None,
    ) -> None:
        self.model = model
        self.trace_writer = trace_writer
        self.generation_config = generation_config or GenerationConfig(max_output_tokens=1024)

    def run(
        self,
        task: TaskSpec,
        node_id: str,
        mode: str,
        objective: str,
        artifacts: Sequence[Artifact],
    ) -> Artifact:
        if mode not in REVIEWER_MODES:
            raise WorkerAgentError(f"不支持的 Reviewer mode: {mode}")
        if not artifacts:
            raise WorkerAgentError("Reviewer 缺少目标 Artifact")
        evidence = [item for item in artifacts if item.artifact_type is ArtifactType.EVIDENCE]
        if not evidence:
            raise WorkerAgentError("Reviewer 必须获得目标 Artifact 的直接 Evidence")
        return generate_artifact(
            self.model,
            task=task,
            node_id=node_id,
            mode=mode,
            objective=objective,
            artifacts=artifacts,
            artifact_type=ArtifactType.REVIEW,
            content_schema=REVIEW_CONTENT_SCHEMA,
            system_prompt=(
                "你是 RepoPilot-MAS ReviewerAgent，只提供审查建议，不决定路由、根因接受或 Patch 选择。"
                "target_artifact_ref 必须指向给定目标，evidence_refs 必须逐项引用给定 Evidence。"
                "结论不得超出直接证据；说明发现、风险和建议。"
                f"mode 必须为 {mode}。"
            ),
            generation_config=self.generation_config,
            trace_writer=self.trace_writer,
        )
