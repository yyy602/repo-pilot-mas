"""Targeted review Worker without routing authority."""

from __future__ import annotations

import copy
from collections.abc import Sequence

from repo_pilot_mas.agents.worker_common import WorkerAgentError, generate_artifact
from repo_pilot_mas.models import GenerationConfig, ModelAdapter
from repo_pilot_mas.runtime import TraceWriter
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec
from repo_pilot_mas.schemas.worker_artifact import REVIEW_CONTENT_SCHEMA, REVIEWER_MODES

_ACCEPTABLE_REVIEW_VERDICTS = frozenset({"approved", "compatible", "supported"})
_ROOT_CAUSE_REVIEW_MODES = frozenset(
    {"root_cause_recommendation", "hypothesis_comparison"}
)


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
        evidence = tuple(
            item
            for item in artifacts
            if item.artifact_type is ArtifactType.EVIDENCE
        )
        if not evidence:
            raise WorkerAgentError("Reviewer 必须获得目标 Artifact 的直接 Evidence")
        hypotheses = tuple(
            item
            for item in artifacts
            if item.artifact_type is ArtifactType.HYPOTHESIS
        )

        target = None
        if mode == "patch_review":
            target = _validate_patch_review_inputs(artifacts)
        elif mode == "root_cause_recommendation":
            if len(hypotheses) != 1:
                raise WorkerAgentError(
                    "root_cause_recommendation 必须且只能审查一个 Hypothesis"
                )
            target = hypotheses[0]
        elif mode == "hypothesis_comparison" and len(hypotheses) < 2:
            raise WorkerAgentError(
                "hypothesis_comparison 必须比较至少两个 Hypothesis"
            )

        normalization: dict[str, str] = {}

        def normalize_review(content: dict[str, object]) -> dict[str, object]:
            if (
                content.get("verdict") in _ACCEPTABLE_REVIEW_VERDICTS
                and not _review_quality_complete(content)
            ):
                normalization["original_verdict"] = str(content["verdict"])
                content["verdict"] = "needs_more_evidence"
                normalization["normalized_verdict"] = "needs_more_evidence"
            return content

        review = generate_artifact(
            self.model,
            task=task,
            node_id=node_id,
            mode=mode,
            objective=objective,
            artifacts=artifacts,
            artifact_type=ArtifactType.REVIEW,
            content_schema=_review_content_schema(
                mode,
                target=target,
                evidence=evidence,
                hypotheses=hypotheses,
            ),
            system_prompt=_review_prompt(mode, target, hypotheses),
            generation_config=self.generation_config,
            trace_writer=self.trace_writer,
            content_transform=normalize_review,
        )
        _validate_review_output(review, mode, evidence, target, hypotheses)

        if self.trace_writer is not None:
            if normalization:
                self.trace_writer.write(
                    "review_verdict_normalized",
                    {
                        "node_id": node_id,
                        "review_ref": review.ref,
                        "mode": mode,
                        **normalization,
                        "reason": "acceptable verdict did not satisfy quality gate",
                    },
                    trace_id=review.trace_id,
                )
            self.trace_writer.write(
                "review_independent_verification",
                {
                    "node_id": node_id,
                    "review_ref": review.ref,
                    "mode": mode,
                    "verdict": review.content["verdict"],
                    "failure_explained": review.content["failure_explained"],
                    "causal_chain_complete": review.content[
                        "causal_chain_complete"
                    ],
                    "alternative_causes": list(review.content["alternative_causes"]),
                    "counterexample_checked": review.content[
                        "counterexample_checked"
                    ],
                    "verification_steps_executed": list(
                        review.content["verification_steps_executed"]
                    ),
                    "remaining_uncertainty": list(
                        review.content["remaining_uncertainty"]
                    ),
                },
                trace_id=review.trace_id,
            )

        if mode == "patch_review" and self.trace_writer is not None:
            assert target is not None
            self.trace_writer.write(
                "patch_review_completed",
                {
                    "node_id": node_id,
                    "review_ref": review.ref,
                    "patch_ref": target.ref,
                    "verdict": review.content["verdict"],
                    "evidence_refs": list(review.content["evidence_refs"]),
                    "root_cause_review_refs": [
                        item.ref
                        for item in artifacts
                        if item.artifact_type is ArtifactType.REVIEW
                        and item.content.get("mode") in _ROOT_CAUSE_REVIEW_MODES
                    ],
                },
                trace_id=review.trace_id,
            )
        return review


def _review_quality_complete(content: dict[str, object]) -> bool:
    return bool(
        content.get("failure_explained") is True
        and content.get("causal_chain_complete") is True
        and (
            content.get("alternative_causes")
            or content.get("counterexample_checked") is True
        )
        and content.get("verification_steps_executed")
        and not content.get("remaining_uncertainty")
    )


def _validate_patch_review_inputs(
    artifacts: Sequence[Artifact],
) -> Artifact:
    patches = tuple(
        item
        for item in artifacts
        if item.artifact_type is ArtifactType.PATCH_CANDIDATE
    )
    if len(patches) != 1:
        raise WorkerAgentError("patch_review 必须且只能审查一个 PatchCandidate")
    if not any(
        item.artifact_type is ArtifactType.HYPOTHESIS
        for item in artifacts
    ):
        raise WorkerAgentError("patch_review 缺少已接受的 Hypothesis")
    root_reviews = tuple(
        item
        for item in artifacts
        if item.artifact_type is ArtifactType.REVIEW
        and item.content.get("mode") in _ROOT_CAUSE_REVIEW_MODES
    )
    if not root_reviews:
        raise WorkerAgentError("patch_review 缺少根因 Review 上下文")
    if not any(
        item.content.get("verdict") in _ACCEPTABLE_REVIEW_VERDICTS
        for item in root_reviews
    ):
        raise WorkerAgentError("patch_review 缺少非阻塞的根因 Review")
    return patches[0]


def _validate_review_output(
    review: Artifact,
    mode: str,
    evidence: Sequence[Artifact],
    target: Artifact | None,
    hypotheses: Sequence[Artifact],
) -> None:
    if review.content.get("mode") != mode:
        raise WorkerAgentError(
            f"Reviewer 输出 mode 与任务不一致: expected={mode}, "
            f"actual={review.content.get('mode')}"
        )
    allowed_evidence_refs = {item.ref for item in evidence}
    actual_evidence_refs = {
        str(item) for item in review.content.get("evidence_refs", ())
    }
    if not actual_evidence_refs or not actual_evidence_refs.issubset(
        allowed_evidence_refs
    ):
        raise WorkerAgentError("Reviewer evidence_refs 只能引用直接 Evidence")
    if target is not None and review.content.get("target_artifact_ref") != target.ref:
        raise WorkerAgentError(
            "patch_review 的 target_artifact_ref 必须指向唯一 PatchCandidate"
        )
    if (
        mode == "evidence_review"
        and review.content.get("target_artifact_ref") not in allowed_evidence_refs
    ):
        raise WorkerAgentError(
            "evidence_review 的 target_artifact_ref 必须指向输入 Evidence",
            code="ARTIFACT_SCHEMA_ERROR",
        )
    if mode == "hypothesis_comparison":
        expected = {item.ref for item in hypotheses}
        actual = {
            str(item) for item in review.content.get("target_artifact_refs", ())
        }
        if actual != expected:
            raise WorkerAgentError(
                "hypothesis_comparison 必须在 target_artifact_refs 覆盖全部候选 Hypothesis",
                code="ARTIFACT_SCHEMA_ERROR",
            )
        if review.content.get("target_artifact_ref") not in expected:
            raise WorkerAgentError(
                "hypothesis_comparison 的推荐目标必须是候选 Hypothesis",
                code="ARTIFACT_SCHEMA_ERROR",
            )


def _review_content_schema(
    mode: str,
    *,
    target: Artifact | None,
    evidence: Sequence[Artifact],
    hypotheses: Sequence[Artifact],
) -> dict[str, object]:
    schema = copy.deepcopy(REVIEW_CONTENT_SCHEMA)
    properties = schema["properties"]
    properties["mode"] = {"const": mode}
    properties["evidence_refs"] = {
        "type": "array",
        "items": {"type": "string", "enum": [item.ref for item in evidence]},
        "minItems": 1,
        "uniqueItems": True,
    }
    if target is not None:
        properties["target_artifact_ref"] = {"const": target.ref}
    if mode == "evidence_review":
        properties["target_artifact_ref"] = {
            "type": "string",
            "enum": [item.ref for item in evidence],
        }
    if mode == "hypothesis_comparison":
        refs = [item.ref for item in hypotheses]
        properties["target_artifact_ref"] = {
            "type": "string",
            "enum": refs,
        }
        properties["target_artifact_refs"] = {
            "type": "array",
            "items": {"type": "string", "enum": refs},
            "minItems": len(refs),
            "maxItems": len(refs),
            "uniqueItems": True,
        }
        schema["required"].append("target_artifact_refs")
    return schema


def _review_prompt(
    mode: str,
    target: Artifact | None,
    hypotheses: Sequence[Artifact],
) -> str:
    common = (
        "你是 RepoPilot-MAS ReviewerAgent，只提供审查建议，不决定路由、根因接受或 Patch 选择。"
        "target_artifact_ref 必须指向给定目标，evidence_refs 必须逐项引用给定的直接 Evidence。"
        "结论不得超出直接证据；说明发现、风险和建议。"
        "必须独立填写 failure_explained、causal_chain_complete、alternative_causes、"
        "counterexample_checked、verification_steps_executed 和 remaining_uncertainty。"
        "只有真实失败得到解释、因果链完整、至少检查一个替代原因或反例、至少执行一项"
        "verification plan 且无剩余关键不确定性时，才可给 supported/approved/compatible。"
        f"mode 必须为 {mode}。"
    )
    if mode == "hypothesis_comparison":
        refs = "、".join(item.ref for item in hypotheses)
        return (
            common
            + f"本次必须完整比较这些 Hypothesis：{refs}。target_artifact_refs 必须逐项且仅包含"
            "上述全部引用；target_artifact_ref 必须指向你最终推荐的其中一个 Hypothesis，禁止"
            "指向 Evidence。"
        )
    if mode != "patch_review":
        return common
    assert target is not None
    return (
        common
        + f"本次唯一审查目标是 PatchCandidate {target.ref}，target_artifact_ref 必须精确输出该引用。"
        "审查 diff 是否直接覆盖已接受根因，是否违反 protected_paths，是否引入无证据的扩大修改，"
        "是否保持公开契约，以及是否存在目标测试、回归或静态检查风险。"
        "若补丁未覆盖根因、只硬编码目标用例、改变无关行为或缺少必要上下文，使用"
        " changes_requested/unsupported/needs_more_evidence；只有证据支持且风险可控时才使用"
        " supported/approved/compatible。不要把根因 Review 当作直接 Evidence 引用。"
    )
