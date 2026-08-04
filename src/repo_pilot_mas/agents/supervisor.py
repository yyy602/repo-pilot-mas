"""Global SupervisorAgent and deterministic scripted replacement."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from repo_pilot_mas.models import (
    GenerationConfig,
    Message,
    ModelAdapter,
    ModelAdapterError,
    TokenUsage,
)
from repo_pilot_mas.runtime.trace import TraceWriter, redact_secrets
from repo_pilot_mas.schemas.json_schema import validate_json_schema
from repo_pilot_mas.schemas.supervisor_decision import (
    SupervisorDecision,
    supervisor_decision_schema,
)

DYNAMIC_SUPERVISOR_PROMPT = """你是 RepoPilot-MAS 的全局 SupervisorAgent。
你的职责是从完整状态快照中做全局编排决策，而不是执行代码修改。

硬约束：
1. 每次只能选择一个 action，并且只输出符合给定 Schema 的 JSON 对象。
2. 不得引用快照中不存在的任务或 Artifact；不得绕过依赖、预算和阶段约束。
   N1、N2 等是 node ID，只能用于 depends_on/target_task_ids；E1@v1 等带版本号的是
   Artifact ref，只能用于 input_artifact_ids/evidence_refs/各类选择字段，二者不得混用。
3. 初始化且任务图为空时，创建至少一个 INVESTIGATION_TASK，并将阶段推进到 investigation。
4. 子任务目标必须具体、可验证；本地 Worker 只负责执行已规划任务。
5. 只有补丁验证通过后才可 FINALIZE_TASK；无法安全继续时使用 TERMINATE_TASK。
6. decision_id 必须唯一，只使用字母、数字、点、下划线或连字符。
7. 从一个 Investigator、一个 Diagnostician 和一个 Patch 的最小路径开始；只有 Artifact
   显示证据不足、实质根因冲突或补丁取舍时才动态扩展。扩展必须填写 gate_record，记录
   触发 Artifact、语义理由、新增节点数和预算影响，并在 evidence_refs 中重复列出触发引用。
8. 两个 Hypothesis 只有在根因或因果链实质冲突时才进入一次双向 Challenge/Rebuttal；
   Challenge 必须包含具体证据缺口、反例、替代因果链和所需证据。Reviewer 只给建议，
   最终 ACCEPT_HYPOTHESIS 必须引用 Evidence、Challenge、Rebuttal 和 Review。
9. 仅在作用域、稳健性或公共契约存在真实取舍时创建 Minimal/Robust 双 Patch；每个候选
   都要经过 Patch Review 和独立 ValidationTask，真实目标测试与完整回归拥有最终否决权。
10. Validation 失败时按 failure_class 定向重规划：复现/位置/证据不足回 investigation，
    根因被推翻或两个 Patch 均失败回 diagnosis，单个 Patch 的应用/语法/目标/回归失败回 patch。
    MVP 最多重规划一次，不得用重复决策绕过预算。
11. execution_path_class 是 Engine 根据已创建节点计算的后验只读值，不得预先选择路径标签。
12. 创建节点时必须严格使用以下 Worker 契约，禁止自造名称或 mode：
    INVESTIGATION_TASK -> InvestigatorAgent -> code_retrieval/failure_reproduction/
    dependency_trace/evidence_completion/regression_scope；
    DIAGNOSIS_TASK -> DiagnosticianAgent -> control_flow/data_flow；
    CHALLENGE_TASK -> DiagnosticianAgent -> challenge；
    REBUTTAL_TASK -> DiagnosticianAgent -> rebuttal；
    REVIEW_TASK -> ReviewerAgent -> evidence_review/hypothesis_comparison/challenge_quality/
    root_cause_recommendation/patch_review/final_risk_review；
    PATCH_TASK -> PatchAgent -> minimal/robust；
    VALIDATION_TASK -> ValidationExecutor -> deterministic。
    不得创建 REPLAN_TASK 或 FINALIZATION_TASK，改用 REQUEST_REPLAN 或 FINALIZE_TASK action。
13. 快照 decision_history.last_supervisor_call.decision_result 若显示上一决定被拒绝，下一次必须
    修正其中的 code/message；decision_id 不得出现在 processed_decision_ids 中，也不得重复同类
    无效决定。已有同类成功或活动节点之外再增加 Investigation、Diagnosis、Patch，
    以及任何 Challenge/Rebuttal，都属于动态扩图：必须引用现有 Artifact，并同时填写 evidence_refs
    和字段完全一致的 gate_record；单纯重试 FAILED/TIMED_OUT/BLOCKED 节点不算动态扩图。
14. selections.hypothesis_ref 非空表示根因已经被接受，禁止再次 ACCEPT_HYPOTHESIS，应据此创建
    PATCH_TASK；selections.patch_ref 与 validation_ref 非空表示补丁已经选择，禁止重复 SELECT_PATCH，
    验证完整时使用 FINALIZE_TASK。
15. Phase C 根因审查是强制门禁。单个候选 Hypothesis 必须先创建 ReviewerAgent 的
    root_cause_recommendation；多个候选必须创建 hypothesis_comparison。Review 必须同时输入直接
    Evidence 和目标 Hypothesis。不得从未审查的 Hypothesis 直接创建 PATCH_TASK。
16. 必须根据 Review verdict 路由：supported/approved/compatible 才可 ACCEPT_HYPOTHESIS；
    needs_more_evidence 回 investigation 补证；changes_requested 或 unsupported 回 diagnosis 修订；
    conflict 留在 review，并按实质冲突决定是否创建 Challenge/Rebuttal。不得忽略阻塞性 Review。
17. ACCEPT_HYPOTHESIS 后创建 PATCH_TASK 时，input_artifact_ids 必须完整包含已接受 Hypothesis
    和用于接受它们的根因 Review。PatchCandidate 生成后必须创建 ReviewerAgent/patch_review，
    输入直接 Evidence、已接受 Hypothesis、根因 Review 和该 PatchCandidate。
18. 只有 verdict 为 supported/approved/compatible 且 target_artifact_ref 指向当前 PatchCandidate
    的 patch_review 才允许创建 ValidationExecutor/deterministic；ValidationTask 输入必须包含该
    PatchCandidate 和 patch_review。ValidationResult.passed=true 后才可 SELECT_PATCH 或 FINALIZE_TASK。
19. selections.hypothesis_resolution 是根因状态机的只读真相：unresolved 表示需要 Review；
    under_review 表示可考虑 ACCEPT_HYPOTHESIS；needs_evidence/needs_revision/conflict/rejected 表示必须
    按第16条回退或扩展；accepted 才允许进入 Patch。不得仅凭模型 confidence 越过该状态机。
"""


@dataclass(frozen=True, slots=True)
class SupervisorOutcome:
    decision: SupervisorDecision
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    model_id: str = "scripted-supervisor"
    raw_response_ref: str | None = None
    trace_id: str | None = None
    attempts: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SupervisorAgentError(RuntimeError):
    """Structured Supervisor failure safe for Engine handling and tracing."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        usage: TokenUsage | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        safe_message = str(redact_secrets(message))
        safe_details = redact_secrets(dict(details or {}))
        super().__init__(safe_message)
        self.code = code
        self.usage = usage or TokenUsage()
        self.details = dict(safe_details)


class SupervisorAgent:
    def __init__(
        self,
        model: ModelAdapter,
        *,
        generation_config: GenerationConfig | None = None,
        trace_writer: TraceWriter | None = None,
        system_prompt: str = DYNAMIC_SUPERVISOR_PROMPT,
    ) -> None:
        if not system_prompt.strip():
            raise ValueError("system_prompt must not be empty")
        self.model = model
        self.generation_config = generation_config or GenerationConfig(
            temperature=0.0,
            max_output_tokens=2048,
            max_retries=1,
        )
        self.trace_writer = trace_writer
        self.system_prompt = system_prompt

    def decide(self, snapshot: Mapping[str, Any]) -> SupervisorOutcome:
        messages = (
            Message("system", self.system_prompt),
            Message(
                "user",
                "请基于以下只读状态快照给出下一步唯一决策：\n"
                + json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            ),
        )
        try:
            response = self.model.generate(
                messages,
                response_schema=supervisor_decision_schema(),
                config=self.generation_config,
            )
            if response.structured_output is None:
                raise SupervisorAgentError(
                    "SUPERVISOR_EMPTY_DECISION",
                    "supervisor response has no structured decision",
                    usage=response.usage,
                )
            decision = SupervisorDecision.from_dict(response.structured_output)
        except SupervisorAgentError as exc:
            self._trace_failure(exc.code, str(exc), exc.usage, exc.details)
            raise
        except ModelAdapterError as exc:
            details = {
                "model_error_code": exc.code,
                "attempts": exc.attempts,
                "raw_response_refs": list(exc.raw_response_refs),
                **exc.details,
            }
            self._trace_failure(exc.code, str(exc), exc.usage, details)
            raise SupervisorAgentError(
                exc.code,
                str(exc),
                usage=exc.usage,
                details=details,
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            usage = response.usage if "response" in locals() else TokenUsage()
            details = (
                {
                    "attempts": response.attempts,
                    "raw_response_refs": [response.raw_response_ref],
                }
                if "response" in locals()
                else {}
            )
            self._trace_failure("SUPERVISOR_DECISION_ERROR", str(exc), usage, details)
            raise SupervisorAgentError(
                "SUPERVISOR_DECISION_ERROR",
                str(exc),
                usage=usage,
                details=details,
            ) from exc

        metadata = _safe_metadata(response.metadata)
        self._trace(
            "supervisor_decision_generated",
            {
                "decision_id": decision.decision_id,
                "action": decision.action.value,
                "model_id": response.model_id,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "latency_ms": response.latency_ms,
                "attempts": response.attempts,
                "raw_response_ref": response.raw_response_ref,
                **metadata,
            },
            trace_id=response.trace_id,
        )
        return SupervisorOutcome(
            decision=decision,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=response.latency_ms,
            model_id=response.model_id,
            raw_response_ref=response.raw_response_ref,
            trace_id=response.trace_id,
            attempts=response.attempts,
            metadata=metadata,
        )

    def _trace_failure(
        self,
        code: str,
        message: str,
        usage: TokenUsage,
        details: Mapping[str, Any],
    ) -> None:
        self._trace(
            "supervisor_generation_failed",
            {
                "code": code,
                "message": message,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                **_safe_metadata(details),
            },
        )

    def _trace(
        self,
        event_type: str,
        data: Mapping[str, Any],
        *,
        trace_id: str | None = None,
    ) -> None:
        if self.trace_writer is not None:
            self.trace_writer.write(event_type, data, trace_id=trace_id)


class ScriptedSupervisor:
    """Return a fixed decision sequence for deterministic orchestration tests."""

    def __init__(self, decisions: Sequence[SupervisorDecision | Mapping[str, Any]]) -> None:
        parsed: list[SupervisorDecision] = []
        for item in decisions:
            if isinstance(item, SupervisorDecision):
                parsed.append(item)
                continue
            validate_json_schema(item, supervisor_decision_schema())
            parsed.append(SupervisorDecision.from_dict(item))
        self._decisions = tuple(parsed)
        self.calls = 0

    def decide(self, snapshot: Mapping[str, Any]) -> SupervisorOutcome:
        del snapshot
        if self.calls >= len(self._decisions):
            raise SupervisorAgentError(
                "SCRIPTED_DECISIONS_EXHAUSTED",
                "scripted supervisor has no remaining decisions",
            )
        decision = self._decisions[self.calls]
        self.calls += 1
        return SupervisorOutcome(decision=decision)


def _safe_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "api_key_env",
        "attempts",
        "error_code",
        "exhausted_routes",
        "http_status",
        "model_error_code",
        "model_provider",
        "request_id",
        "route_attempts",
        "raw_response_refs",
        "transient_failures",
    }
    return {key: item for key, item in value.items() if key in allowed}
