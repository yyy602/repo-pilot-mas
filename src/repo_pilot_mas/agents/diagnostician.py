"""Independent diagnosis, challenge, and rebuttal Worker."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from repo_pilot_mas.agents.worker_common import (
    WorkerAgentError,
    generate_artifact,
    worker_task_view,
)
from repo_pilot_mas.models import GenerationConfig, Message, ModelAdapter
from repo_pilot_mas.runtime import TraceWriter
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec
from repo_pilot_mas.schemas.json_schema import validate_json_schema
from repo_pilot_mas.schemas.worker_artifact import (
    CHALLENGE_CONTENT_SCHEMA,
    DIAGNOSIS_PERSPECTIVES,
    HYPOTHESIS_CONTENT_SCHEMA,
    validate_worker_artifact,
)

_REBUTTAL_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "response_to": {"type": "string", "minLength": 1},
        "defended_hypothesis_ref": {"type": "string", "minLength": 1},
        "decision": {
            "type": "string",
            "enum": ["accept", "partial_accept", "reject"],
        },
        "new_evidence_refs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "counterexample_explanation": {"type": "string", "minLength": 1},
        "revision_summary": {"type": "string", "minLength": 1},
        "resulting_hypothesis": HYPOTHESIS_CONTENT_SCHEMA,
    },
    "required": [
        "response_to",
        "defended_hypothesis_ref",
        "decision",
        "new_evidence_refs",
        "counterexample_explanation",
        "revision_summary",
        "resulting_hypothesis",
    ],
    "additionalProperties": False,
}


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

    def challenge(
        self,
        task: TaskSpec,
        node_id: str,
        objective: str,
        artifacts: Sequence[Artifact],
    ) -> Artifact:
        hypotheses = [item for item in artifacts if item.artifact_type is ArtifactType.HYPOTHESIS]
        evidence = [item for item in artifacts if item.artifact_type is ArtifactType.EVIDENCE]
        if len(hypotheses) != 1 or not evidence or len(hypotheses) + len(evidence) != len(artifacts):
            raise WorkerAgentError("Challenge 只能读取一个对方 Hypothesis 及其直接 Evidence")
        return generate_artifact(
            self.model,
            task=task,
            node_id=node_id,
            mode="challenge",
            objective=objective,
            artifacts=artifacts,
            artifact_type=ArtifactType.CHALLENGE,
            content_schema=CHALLENGE_CONTENT_SCHEMA,
            system_prompt=(
                "你是独立 DiagnosticianAgent，正在质疑对方的根因假设。"
                "challenged_hypothesis_ref 必须引用给定 Hypothesis。"
                "只有指出具体主张、证据或因果链缺口，并给出可检验反例、替代因果链和所需证据，"
                "才是有效 Challenge；不能只改写原结论，也不能读取对方私有推理。"
            ),
            generation_config=self.generation_config,
            trace_writer=self.trace_writer,
        )

    def rebuttal(
        self,
        task: TaskSpec,
        node_id: str,
        objective: str,
        artifacts: Sequence[Artifact],
    ) -> tuple[Artifact, ...]:
        challenges = [item for item in artifacts if item.artifact_type is ArtifactType.CHALLENGE]
        hypotheses = [item for item in artifacts if item.artifact_type is ArtifactType.HYPOTHESIS]
        evidence = [item for item in artifacts if item.artifact_type is ArtifactType.EVIDENCE]
        allowed_types = {ArtifactType.CHALLENGE, ArtifactType.HYPOTHESIS, ArtifactType.EVIDENCE}
        if (
            len(challenges) != 1
            or len(hypotheses) != 1
            or not evidence
            or any(item.artifact_type not in allowed_types for item in artifacts)
        ):
            raise WorkerAgentError("Rebuttal 需要一个自身 Hypothesis、一个针对它的 Challenge 和 Evidence")
        challenge = challenges[0]
        hypothesis = hypotheses[0]
        if challenge.content["challenged_hypothesis_ref"] != hypothesis.ref:
            raise WorkerAgentError("Challenge 并非针对给定的自身 Hypothesis")

        response = self.model.generate(
            (
                Message(
                    "system",
                    "你是收到质疑的 DiagnosticianAgent。必须逐项回应反例并选择 accept、"
                    "partial_accept 或 reject。accept/partial_accept 时要真正修订根因或因果链；"
                    "reject 时 resulting_hypothesis 原样保留。所有引用必须来自输入 Artifact。",
                ),
                Message(
                    "user",
                    json.dumps(
                        {
                            "task": worker_task_view(task),
                            "worker": {"node_id": node_id, "mode": "rebuttal", "objective": objective},
                            "input_artifacts": [item.to_dict() for item in artifacts],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            ),
            response_schema=_REBUTTAL_DRAFT_SCHEMA,
            config=self.generation_config,
        )
        if response.structured_output is None:
            raise WorkerAgentError("模型没有返回结构化 Rebuttal")
        draft = dict(response.structured_output)
        validate_json_schema(draft, _REBUTTAL_DRAFT_SCHEMA)
        refs_by_id = {item.artifact_id: item.ref for item in artifacts}
        refs_by_id.update({item.ref: item.ref for item in artifacts})

        def canonical(value: Any) -> str:
            return refs_by_id.get(str(value), str(value))

        if canonical(draft["response_to"]) != challenge.ref:
            raise WorkerAgentError("Rebuttal response_to 必须指向输入 Challenge")
        if canonical(draft["defended_hypothesis_ref"]) != hypothesis.ref:
            raise WorkerAgentError("Rebuttal defended_hypothesis_ref 必须指向自身 Hypothesis")
        new_evidence_refs = tuple(canonical(item) for item in draft["new_evidence_refs"])
        evidence_refs = {item.ref for item in evidence}
        if not set(new_evidence_refs).issubset(evidence_refs):
            raise WorkerAgentError("Rebuttal new_evidence_refs 只能引用输入 Evidence")

        decision = str(draft["decision"])
        resulting_ref = hypothesis.ref
        outputs: list[Artifact] = []
        if decision in {"accept", "partial_accept"}:
            revised_content = dict(draft["resulting_hypothesis"])
            revised_content["perspective"] = hypothesis.content["perspective"]
            revised_content["supporting_evidence"] = [
                canonical(item) for item in revised_content["supporting_evidence"]
            ]
            if revised_content == hypothesis.to_dict()["content"]:
                raise WorkerAgentError("接受 Challenge 时必须产生实质修订的 Hypothesis")
            revised = Artifact(
                artifact_id=hypothesis.artifact_id,
                artifact_type=ArtifactType.HYPOTHESIS,
                created_by=node_id,
                content=revised_content,
                version=hypothesis.version + 1,
                supersedes=hypothesis.ref,
                input_refs=tuple(item.ref for item in artifacts),
                trace_id=response.trace_id,
            )
            validate_worker_artifact(
                revised,
                expected_type=ArtifactType.HYPOTHESIS,
                allowed_input_refs=tuple(item.ref for item in artifacts),
            )
            outputs.append(revised)
            resulting_ref = revised.ref
        else:
            proposed = dict(draft["resulting_hypothesis"])
            if proposed != hypothesis.to_dict()["content"]:
                raise WorkerAgentError("拒绝 Challenge 时不得静默修改 Hypothesis")

        rebuttal_inputs = tuple(item.ref for item in artifacts) + tuple(item.ref for item in outputs)
        rebuttal = Artifact(
            artifact_id=f"{node_id}.rebuttal",
            artifact_type=ArtifactType.REBUTTAL,
            created_by=node_id,
            content={
                "response_to": challenge.ref,
                "defended_hypothesis_ref": hypothesis.ref,
                "decision": decision,
                "new_evidence_refs": list(new_evidence_refs),
                "resulting_hypothesis_ref": resulting_ref,
                "counterexample_explanation": str(draft["counterexample_explanation"]),
                "revision_summary": str(draft["revision_summary"]),
            },
            input_refs=rebuttal_inputs,
            trace_id=response.trace_id,
        )
        validate_worker_artifact(
            rebuttal,
            expected_type=ArtifactType.REBUTTAL,
            allowed_input_refs=rebuttal_inputs,
        )
        outputs.append(rebuttal)
        if self.trace_writer is not None:
            for artifact in outputs:
                self.trace_writer.write(
                    "worker_artifact_created",
                    {"artifact": artifact.to_dict(), "model_id": response.model_id},
                    trace_id=response.trace_id,
                )
        return tuple(outputs)
