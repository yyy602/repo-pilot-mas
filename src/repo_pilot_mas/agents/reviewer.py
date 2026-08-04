"""Targeted review Worker without routing authority."""

from __future__ import annotations

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

        target = None
        if mode == "patch_review":
            target = _validate_patch_review_inputs(artifacts)

        review = generate_artifact(
            self.model,
            task=task,
            node_id=node_id,
            mode=mode,
            objective=objective,
            artifacts=artifacts,
            artifact_type=ArtifactType.REVIEW,
            content_schema=REVIEW_CONTENT_SCHEMA,
            system_prompt=_review_prompt(mode, target),
            generation_config=self.generation_config,
            trace_writer=self.trace_writer,
        )
        _validate_review_output(review, mode, evidence, target)

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


def _review_prompt(mode: str, target: Artifact | None) -> str:
    common = (
        "你是 RepoPilot-MAS ReviewerAgent，只提供审查建议，不决定路由、根因接受或 Patch 选择。"
        "target_artifact_ref 必须指向给定目标，evidence_refs 必须逐项引用给定的直接 Evidence。"
        "结论不得超出直接证据；说明发现、风险和建议。"
        f"mode 必须为 {mode}。"
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
