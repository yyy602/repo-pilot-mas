"""Independent evidence-only diagnosis Worker."""

from __future__ import annotations

from collections.abc import Sequence

from repo_pilot_mas.agents.worker_common import WorkerAgentError, generate_artifact
from repo_pilot_mas.models import GenerationConfig, ModelAdapter
from repo_pilot_mas.runtime import TraceWriter
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec
from repo_pilot_mas.schemas.worker_artifact import (
    DIAGNOSIS_PERSPECTIVES,
    HYPOTHESIS_CONTENT_SCHEMA,
)


class DiagnosticianAgent:
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
        perspective: str,
        objective: str,
        artifacts: Sequence[Artifact],
    ) -> Artifact:
        if perspective not in DIAGNOSIS_PERSPECTIVES:
            raise WorkerAgentError(f"不支持的诊断视角: {perspective}")
        if not artifacts or any(item.artifact_type is not ArtifactType.EVIDENCE for item in artifacts):
            raise WorkerAgentError("第一轮 Diagnostician 只能读取 Evidence Artifact")
        focus = (
            "控制流、边界条件、异常路径和运行行为"
            if perspective == "control_flow"
            else "数据流、状态变化、接口契约和依赖影响"
        )
        return generate_artifact(
            self.model,
            task=task,
            node_id=node_id,
            mode=perspective,
            objective=objective,
            artifacts=artifacts,
            artifact_type=ArtifactType.HYPOTHESIS,
            content_schema=HYPOTHESIS_CONTENT_SCHEMA,
            system_prompt=(
                "你是 RepoPilot-MAS DiagnosticianAgent 的独立第一轮实例。"
                f"只从给定 Evidence 分析{focus}；不得假定或读取另一诊断实例的 Hypothesis。"
                "supporting_evidence 必须使用完整 Evidence ref（artifact_id@vN）。"
                "区分直接原因和根本原因，给出可执行验证计划，并列出反例与缺失证据。"
                f"perspective 必须为 {perspective}。"
            ),
            generation_config=self.generation_config,
            trace_writer=self.trace_writer,
        )
