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
    CreateTaskRequest,
    GateRecord,
    SupervisorDecision,
    additional_investigation_required,
    evidence_gap_completion_refs,
    evidence_gap_recovery_stage,
    supervisor_decision_schema,
    supervisor_decision_schema_for_state,
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
20. recovery.pending_replan=true 表示一次 REQUEST_REPLAN 已获准但恢复节点尚未创建。此时
    remaining_replans=0 只禁止再次 REQUEST_REPLAN，不禁止执行本次恢复；必须按 target_stage 创建
    对应 Worker 节点，并引用 replan_ref 与 trigger_refs 填写 gate_record，禁止直接 TERMINATE_TASK。
    Patch 恢复还应把失败的 ValidationResult 和原 PatchCandidate 加入 Worker 输入，使新候选能针对
    真实失败修正，而不是重复生成同一补丁。
21. Hypothesis.missing_evidence 非空时禁止 ACCEPT_HYPOTHESIS。补证完成后必须创建新的
    DIAGNOSIS_TASK；该任务只能输入 Evidence，必须包含新增 Evidence，不得输入旧 Hypothesis、Review
    或 ArtifactRejection。使新 Hypothesis 引用新增 Evidence 并清空已满足的 missing_evidence，随后
    针对新 Hypothesis 重新 Review；重复接受或重复审查旧 Hypothesis 都不能消除证据缺口。
22. 已有带成功工具 Trace 的源码定位与失败复现/执行 Evidence，且 Hypothesis 或 Review 没有在其后
    提出新的明确证据缺口时，禁止继续创建 INVESTIGATION_TASK，应立即进入 Diagnosis。只有
    Hypothesis.missing_evidence、Review.needs_more_evidence/remaining_uncertainty 明确提出新缺口时才可
    重新补证；新增 verified Evidence 回应缺口后必须再次关闭 Investigation。
23. HypothesisResolution.status 不是 accepted 时，next_workflow_stage 不得为 patch、validation 或
    finalization；若快照已处于这些阶段但根因未接受，必须先退回 review/diagnosis 修复状态。
24. 补充 verified Evidence 只表示证据缺口已回应，不会修改旧 Hypothesis。补证后必须先创建新的
    DIAGNOSIS_TASK，并且只能审查这个新 Hypothesis；不得继续 Review 或接受仍带 missing_evidence 的旧版本。
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
        recoverable: bool = True,
    ) -> None:
        safe_message = str(redact_secrets(message))
        safe_details = redact_secrets(dict(details or {}))
        super().__init__(safe_message)
        self.code = code
        self.usage = usage or TokenUsage()
        self.details = dict(safe_details)
        self.recoverable = bool(recoverable)


class SupervisorAgent:
    def __init__(
        self,
        model: ModelAdapter,
        *,
        generation_config: GenerationConfig | None = None,
        trace_writer: TraceWriter | None = None,
        system_prompt: str = DYNAMIC_SUPERVISOR_PROMPT,
        additional_schema_retries: int = 1,
        safe_fallback_enabled: bool = True,
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
        if additional_schema_retries < 0:
            raise ValueError("additional_schema_retries must be non-negative")
        self.additional_schema_retries = additional_schema_retries
        self.safe_fallback_enabled = bool(safe_fallback_enabled)

    def decide(self, snapshot: Mapping[str, Any]) -> SupervisorOutcome:
        begin_recovery = getattr(
            self.model,
            "begin_structured_recovery",
            None,
        )
        if callable(begin_recovery):
            begin_recovery()
        snapshot_view = _supervisor_snapshot_view(snapshot)
        processed_decision_ids = set(
            snapshot_view["decision_history"]["processed_decision_ids"]
        )
        original_chars = len(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        )
        compact_chars = len(
            json.dumps(snapshot_view, ensure_ascii=False, sort_keys=True)
        )
        self._trace(
            "supervisor_snapshot_compacted",
            {
                "workflow_stage": str(
                    snapshot.get("workflow_stage", "initialization")
                ),
                "original_chars": original_chars,
                "compact_chars": compact_chars,
                "reduction_ratio": (
                    1.0 - compact_chars / original_chars
                    if original_chars
                    else 0.0
                ),
                "node_count": len(snapshot_view.get("nodes", ())),
                "artifact_count": len(snapshot_view.get("artifacts", ())),
            },
        )
        messages = (
            Message("system", self.system_prompt),
            Message(
                "user",
                "请基于以下只读状态快照给出下一步唯一决策：\n"
                + json.dumps(
                    snapshot_view,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        schema = supervisor_decision_schema_for_state(snapshot)
        self._trace(
            "supervisor_schema_reduced",
            {
                "workflow_stage": str(snapshot.get("workflow_stage", "initialization")),
                "action_count": len(schema["oneOf"]),
                "actions": _schema_actions(schema),
                "create_node_types": _schema_node_types(schema),
            },
        )
        total_usage = TokenUsage()
        total_latency_ms = 0
        total_attempts = 0
        raw_response_refs: list[str] = []
        last_error: SupervisorAgentError | None = None
        response = None
        decision: SupervisorDecision | None = None

        for schema_attempt in range(self.additional_schema_retries + 1):
            try:
                response = self.model.generate(
                    messages,
                    response_schema=schema,
                    config=self.generation_config,
                )
                total_usage = _add_usage(total_usage, response.usage)
                total_latency_ms += response.latency_ms
                total_attempts += response.attempts
                raw_response_refs.append(response.raw_response_ref)
                if response.attempts > 1:
                    self._trace_format_repair(
                        "model_adapter",
                        schema_attempt=schema_attempt,
                        attempts=response.attempts,
                    )
                if response.structured_output is None:
                    raise SupervisorAgentError(
                        "SUPERVISOR_EMPTY_DECISION",
                        "supervisor response has no structured decision",
                        usage=total_usage,
                    )
                raw_evidence_refs = set(
                    _string_list(response.structured_output.get("evidence_refs", ()))
                )
                candidate = SupervisorDecision.from_dict(response.structured_output)
                added_gate_refs = tuple(
                    ref for ref in candidate.evidence_refs if ref not in raw_evidence_refs
                )
                if added_gate_refs:
                    self._trace(
                        "supervisor_gate_evidence_refs_normalized",
                        {
                            "decision_id": candidate.decision_id,
                            "added_evidence_refs": list(added_gate_refs),
                        },
                    )
                if candidate.decision_id in processed_decision_ids:
                    raise ValueError(
                        "decision_id has already been processed: "
                        f"{candidate.decision_id}"
                    )
                decision = candidate
                break
            except ModelAdapterError as exc:
                total_usage = _add_usage(total_usage, exc.usage)
                total_latency_ms += exc.latency_ms
                total_attempts += exc.attempts
                raw_response_refs.extend(exc.raw_response_refs)
                last_error = SupervisorAgentError(
                    exc.code,
                    str(exc),
                    usage=total_usage,
                    details={
                        "model_error_code": exc.code,
                        "attempts": total_attempts,
                        "raw_response_refs": list(raw_response_refs),
                        **exc.details,
                    },
                    recoverable=exc.code
                    not in {
                        "SUPERVISOR_CONFIGURATION_ERROR",
                        "SUPERVISOR_ROUTES_EXHAUSTED",
                    },
                )
                if (
                    exc.code == "STRUCTURED_OUTPUT_ERROR"
                    and schema_attempt < self.additional_schema_retries
                ):
                    self._trace_format_repair(
                        "stage_schema_retry",
                        schema_attempt=schema_attempt + 1,
                        attempts=exc.attempts,
                    )
                    continue
                break
            except SupervisorAgentError as exc:
                last_error = exc
                break
            except (KeyError, TypeError, ValueError) as exc:
                if response is not None:
                    reject_response = getattr(
                        self.model,
                        "reject_structured_response",
                        None,
                    )
                    if callable(reject_response):
                        reject_response(
                            response.model_id,
                            response.metadata,
                            str(exc),
                        )
                last_error = SupervisorAgentError(
                    "SUPERVISOR_DECISION_ERROR",
                    str(exc),
                    usage=total_usage,
                    details={
                        "attempts": total_attempts,
                        "raw_response_refs": list(raw_response_refs),
                    },
                )
                if schema_attempt < self.additional_schema_retries:
                    self._trace_format_repair(
                        "stage_schema_retry",
                        schema_attempt=schema_attempt + 1,
                        attempts=1,
                    )
                    continue
                break
        else:  # pragma: no cover - bounded loop always exits through break
            raise AssertionError("unreachable supervisor recovery state")

        if decision is None:
            if (
                self.safe_fallback_enabled
                and last_error is not None
                and last_error.recoverable
                and (fallback := _safe_fallback_decision(snapshot)) is not None
            ):
                self._trace(
                    "supervisor_decision_recovery",
                    {
                        "recovery": "deterministic_safe_fallback",
                        "source_error_code": last_error.code,
                        "decision_id": fallback.decision_id,
                        "action": fallback.action.value,
                    },
                )
                return SupervisorOutcome(
                    decision=fallback,
                    input_tokens=total_usage.input_tokens,
                    output_tokens=total_usage.output_tokens,
                    latency_ms=total_latency_ms,
                    model_id="deterministic-safe-fallback",
                    raw_response_ref=(raw_response_refs[-1] if raw_response_refs else None),
                    attempts=total_attempts,
                    metadata={
                        "recovery": "deterministic_safe_fallback",
                        "source_error_code": last_error.code,
                    },
                )
            error = last_error or SupervisorAgentError(
                "SUPERVISOR_DECISION_ERROR",
                "supervisor did not produce a decision",
                usage=total_usage,
            )
            self._trace_failure(error.code, str(error), error.usage, error.details)
            raise error

        assert response is not None
        metadata = _safe_metadata(response.metadata)
        self._trace(
            "supervisor_decision_generated",
            {
                "decision_id": decision.decision_id,
                "action": decision.action.value,
                "model_id": response.model_id,
                "input_tokens": total_usage.input_tokens,
                "output_tokens": total_usage.output_tokens,
                "latency_ms": total_latency_ms,
                "attempts": total_attempts,
                "raw_response_ref": response.raw_response_ref,
                **metadata,
            },
            trace_id=response.trace_id,
        )
        return SupervisorOutcome(
            decision=decision,
            input_tokens=total_usage.input_tokens,
            output_tokens=total_usage.output_tokens,
            latency_ms=total_latency_ms,
            model_id=response.model_id,
            raw_response_ref=response.raw_response_ref,
            trace_id=response.trace_id,
            attempts=total_attempts,
            metadata=metadata,
        )

    def _trace_format_repair(
        self,
        layer: str,
        *,
        schema_attempt: int,
        attempts: int,
    ) -> None:
        self._trace(
            "supervisor_format_repair_attempted",
            {
                "layer": layer,
                "schema_attempt": schema_attempt,
                "attempts": attempts,
            },
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
        "recovery",
        "source_error_code",
    }
    return {key: item for key, item in value.items() if key in allowed}


def _supervisor_snapshot_view(
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the minimal semantic state sent to the API Supervisor."""

    task = snapshot.get("task", {})
    task = task if isinstance(task, Mapping) else {}
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
    history = snapshot.get("decision_history", {})
    history = history if isinstance(history, Mapping) else {}
    processed_ids = _string_list(history.get("processed_decision_ids", ()))
    return {
        "task": {
            key: task[key]
            for key in (
                "task_id",
                "issue",
                "acceptance_criteria",
                "protected_paths",
                "failing_tests",
                "target_files",
                "max_runtime_seconds",
                "requires_failure_reproduction",
                "confirmed_failure_reproduction_refs",
            )
            if key in task
        },
        "engine_status": snapshot.get("engine_status"),
        "termination": snapshot.get("termination"),
        "workflow_stage": snapshot.get("workflow_stage"),
        "execution_path_class": snapshot.get("execution_path_class"),
        "state_version": snapshot.get("state_version"),
        "nodes": [
            _compact_node(item) for item in nodes if isinstance(item, Mapping)
        ],
        "artifacts": [
            _compact_artifact(item)
            for item in artifacts
            if isinstance(item, Mapping)
        ],
        "selections": snapshot.get("selections", {}),
        "decision_history": {
            "processed_decision_ids": list(processed_ids),
            "processed_decision_count": len(processed_ids),
            "last_supervisor_call": _compact_last_supervisor_call(
                history.get("last_supervisor_call")
            ),
        },
        "recovery": snapshot.get("recovery", {}),
        "budget": snapshot.get("budget", {}),
    }


def _compact_node(item: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        key: item[key]
        for key in (
            "node_id",
            "node_type",
            "status",
            "agent_type",
            "mode",
            "dependencies",
            "input_artifact_ids",
            "output_artifact_ids",
            "dependency_policy",
            "critical",
            "retry_count",
        )
        if key in item
    }
    if item.get("objective"):
        result["objective"] = _bounded_text(item["objective"], 240)
    if item.get("failure_reason"):
        result["failure_reason"] = _bounded_text(
            item["failure_reason"],
            240,
        )
    return result


def _compact_artifact(item: Mapping[str, Any]) -> dict[str, Any]:
    artifact_type = str(item.get("artifact_type", ""))
    content = item.get("content", {})
    content = content if isinstance(content, Mapping) else {}
    fields_by_type = {
        "evidence": (
            "mode",
            "evidence_kind",
            "claim",
            "supports_claims",
            "contradicts_claims",
            "verified",
            "source",
            "observation_type",
            "confidence",
            "status",
            "tool_trace_ids",
            "missing_evidence",
        ),
        "hypothesis": (
            "perspective",
            "root_cause",
            "direct_cause",
            "supporting_evidence",
            "counter_evidence",
            "affected_symbols",
            "verification_plan",
            "missing_evidence",
            "confidence",
        ),
        "review": (
            "mode",
            "target_artifact_ref",
            "target_artifact_refs",
            "evidence_refs",
            "verdict",
            "findings",
            "risk_notes",
            "recommendation",
            "failure_explained",
            "causal_chain_complete",
            "alternative_causes",
            "counterexample_checked",
            "verification_steps_executed",
            "remaining_uncertainty",
        ),
        "patch_candidate": (
            "strategy",
            "based_on_hypothesis_refs",
            "primary_hypothesis_ref",
            "covered_root_causes",
            "changed_files",
            "rationale",
            "semantic_rationale",
            "pre_patch_behavior",
            "post_patch_expected_behavior",
            "failure_input_walkthrough",
            "risk_notes",
            "protected_path_check",
            "workspace_id",
            "diff_sha256",
        ),
        "validation_result": (
            "patch_ref",
            "patch_review_refs",
            "patch_sha256",
            "applied",
            "protected_path_check",
            "changed_files",
            "changed_lines",
            "passed",
            "failure_class",
            "recommended_stage",
            "invalidated_refs",
            "recoverable",
            "tool_trace_ids",
        ),
        "artifact_rejection": (
            "node_id",
            "code",
            "reason",
            "origin",
            "terminal_status",
            "recoverable",
            "recommended_stage",
            "allowed_next_actions",
            "attempt",
            "max_attempts",
            "expected_artifact_type",
            "actual_artifact_refs",
        ),
        "replan_record": (
            "decision_id",
            "attempt",
            "from_stage",
            "target_stage",
            "failure_class",
            "trigger_refs",
            "reason",
            "remaining_replans",
        ),
    }
    selected = {
        key: _compact_value(content[key])
        for key in fields_by_type.get(artifact_type, tuple(content))
        if key in content
    }
    if artifact_type == "evidence" and isinstance(
        content.get("reproduction"),
        Mapping,
    ):
        reproduction = content["reproduction"]
        selected["reproduction"] = {
            key: _compact_value(reproduction[key])
            for key in (
                "attempted",
                "succeeded",
                "exit_code",
                "failure_type",
            )
            if key in reproduction
        }
    if artifact_type == "validation_result":
        for label in ("target_test", "regression_test", "static_check"):
            command = content.get(label)
            if isinstance(command, Mapping):
                selected[label] = {
                    key: _compact_value(command[key])
                    for key in ("exit_code", "trace_id", "duration_ms")
                    if key in command
                }
    return {
        key: item[key]
        for key in (
            "artifact_ref",
            "artifact_type",
            "status",
            "created_by",
        )
        if key in item
    } | {"content": selected}


def _compact_last_supervisor_call(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result = {
        key: _compact_value(value[key])
        for key in (
            "ok",
            "code",
            "decision_id",
            "action",
            "model_id",
            "recoverable",
            "recommended_stage",
            "allowed_next_actions",
        )
        if key in value
    }
    decision_result = value.get("decision_result")
    if isinstance(decision_result, Mapping):
        result["decision_result"] = {
            key: _compact_value(decision_result[key])
            for key in (
                "ok",
                "code",
                "message",
                "decision_id",
                "recoverable",
                "recommended_stage",
                "allowed_next_actions",
                "trigger_artifact_refs",
            )
            if key in decision_result
        }
    return result


def _compact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _bounded_text(value, 600)
    if isinstance(value, Mapping):
        return {
            str(key): _compact_value(item) for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_compact_value(item) for item in value]
    return value


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _add_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    return TokenUsage(
        left.input_tokens + right.input_tokens,
        left.output_tokens + right.output_tokens,
    )


def _schema_actions(schema: Mapping[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            str(variant["properties"]["action"]["const"])
            for variant in schema.get("oneOf", ())
        )
    )


def _schema_node_types(schema: Mapping[str, Any]) -> list[str]:
    node_types: set[str] = set()
    for variant in schema.get("oneOf", ()):
        properties = variant.get("properties", {})
        if properties.get("action", {}).get("const") != "CREATE_TASK":
            continue
        task_variants = (
            properties.get("create_tasks", {})
            .get("items", {})
            .get("oneOf", ())
        )
        node_types.update(
            str(item["properties"]["node_type"]["const"])
            for item in task_variants
        )
    return sorted(node_types)


def _safe_fallback_decision(
    snapshot: Mapping[str, Any],
) -> SupervisorDecision | None:
    """Build only low-risk task-creation decisions from existing artifacts."""

    artifacts = [
        item
        for item in snapshot.get("artifacts", ())
        if isinstance(item, Mapping) and item.get("artifact_ref")
    ]
    by_type: dict[str, list[Mapping[str, Any]]] = {}
    by_ref = {str(item["artifact_ref"]): item for item in artifacts}
    for item in artifacts:
        by_type.setdefault(str(item.get("artifact_type", "")), []).append(item)
    stage = str(snapshot.get("workflow_stage", "initialization"))
    state_version = int(snapshot.get("state_version", 0))
    nodes = [item for item in snapshot.get("nodes", ()) if isinstance(item, Mapping)]

    hypotheses = by_type.get("hypothesis", [])
    active_diagnosis = any(
        str(item.get("node_type", "")) == "DIAGNOSIS_TASK"
        and str(item.get("status", ""))
        in {"PENDING", "READY", "RUNNING", "PAUSED"}
        for item in nodes
    )
    verified_evidence_refs = tuple(
        str(item["artifact_ref"])
        for item in by_type.get("evidence", [])
        if (
            _artifact_content(item).get("verified") is True
            and _artifact_content(item).get("status") == "verified"
            and bool(_artifact_content(item).get("tool_trace_ids"))
            and isinstance(_artifact_content(item).get("source"), Mapping)
            and bool(_artifact_content(item).get("source", {}).get("path"))
        )
    )
    task = snapshot.get("task", {})
    task = task if isinstance(task, Mapping) else {}
    confirmed_reproduction_refs = {
        ref
        for ref in _string_list(
            task.get("confirmed_failure_reproduction_refs", ())
        )
        if ref in verified_evidence_refs
    }
    reproduction_ready = not task.get(
        "requires_failure_reproduction"
    ) or bool(confirmed_reproduction_refs)
    if (
        not hypotheses
        and not active_diagnosis
        and verified_evidence_refs
        and reproduction_ready
        and not additional_investigation_required(artifacts)
        and stage in {"investigation", "diagnosis"}
    ):
        return _fallback_create(
            decision_id=f"fallback-diagnosis-{state_version}",
            reason="结构化决策恢复：基于已验证且充分的执行证据创建首次根因诊断任务",
            stage="diagnosis",
            node_type="DIAGNOSIS_TASK",
            agent_type="DiagnosticianAgent",
            mode="control_flow",
            objective="基于已验证的源码与失败执行证据分析控制流根因",
            input_refs=verified_evidence_refs,
            include_gate=False,
        )

    root_reviews = [
        item
        for item in by_type.get("review", [])
        if _artifact_content(item).get("mode")
        in {"root_cause_recommendation", "hypothesis_comparison"}
    ]
    active_root_review = any(
        str(item.get("node_type", "")) == "REVIEW_TASK"
        and str(item.get("mode", ""))
        in {"root_cause_recommendation", "hypothesis_comparison"}
        and str(item.get("status", ""))
        not in {"FAILED", "TIMED_OUT", "BLOCKED", "CANCELLED"}
        for item in nodes
    )
    if hypotheses and not root_reviews and not active_root_review and stage in {"diagnosis", "review"}:
        selected = hypotheses if len(hypotheses) > 1 else hypotheses[-1:]
        hypothesis_refs = tuple(str(item["artifact_ref"]) for item in selected)
        evidence_refs = tuple(
            dict.fromkeys(
                ref
                for item in selected
                for ref in _string_list(_artifact_content(item).get("supporting_evidence", ()))
                if ref in by_ref and str(by_ref[ref].get("artifact_type")) == "evidence"
            )
        )
        if evidence_refs:
            inputs = (*evidence_refs, *hypothesis_refs)
            mode = (
                "hypothesis_comparison"
                if len(hypothesis_refs) > 1
                else "root_cause_recommendation"
            )
            return _fallback_create(
                decision_id=f"fallback-review-{state_version}",
                reason="结构化决策恢复：为现有根因假设补建独立审查任务",
                stage="review",
                node_type="REVIEW_TASK",
                agent_type="ReviewerAgent",
                mode=mode,
                objective="基于直接证据独立审查候选根因，并检查替代原因或反例",
                input_refs=inputs,
            )

    selections = snapshot.get("selections", {})
    selections = selections if isinstance(selections, Mapping) else {}
    resolution = selections.get("hypothesis_resolution", {})
    resolution = resolution if isinstance(resolution, Mapping) else {}
    recovery = snapshot.get("recovery", {})
    recovery = recovery if isinstance(recovery, Mapping) else {}
    pending_replan_target = (
        str(recovery.get("target_stage", ""))
        if recovery.get("pending_replan") is True
        else ""
    )
    replan_trigger_refs = tuple(
        ref
        for ref in (
            str(recovery.get("replan_ref", "")),
            *_string_list(recovery.get("trigger_refs", ())),
        )
        if ref in by_ref
    )
    replan_patch_context_refs = tuple(
        str(item["artifact_ref"])
        for artifact_type in ("patch_candidate", "validation_result")
        for item in by_type.get(artifact_type, [])[-1:]
    )
    accepted_refs = tuple(
        ref for ref in _string_list(resolution.get("accepted_refs", ())) if ref in by_ref
    )
    review_refs = tuple(
        ref for ref in _string_list(resolution.get("review_refs", ())) if ref in by_ref
    )
    blocking_review_refs = tuple(
        ref
        for ref in review_refs
        if str(by_ref[ref].get("artifact_type", "")) == "review"
        and _artifact_content(by_ref[ref]).get("mode")
        in {"root_cause_recommendation", "hypothesis_comparison"}
        and (
            _artifact_content(by_ref[ref]).get("verdict") == "needs_more_evidence"
            or bool(_artifact_content(by_ref[ref]).get("remaining_uncertainty"))
        )
    )
    candidate_refs = tuple(
        ref
        for ref in _string_list(resolution.get("candidate_refs", ()))
        if ref in by_ref
    )
    blocking_hypothesis_refs = tuple(
        ref
        for ref in candidate_refs
        if str(by_ref[ref].get("artifact_type", "")) == "hypothesis"
        and bool(_artifact_content(by_ref[ref]).get("missing_evidence"))
    )
    gap_refs = tuple(
        dict.fromkeys((*blocking_review_refs, *blocking_hypothesis_refs))
    )
    gap_stage = evidence_gap_recovery_stage(artifacts)
    gap_completion_refs = evidence_gap_completion_refs(artifacts)
    evidence_completion_attempted_for_gap = any(
        str(item.get("node_type", "")) == "INVESTIGATION_TASK"
        and str(item.get("mode", "")) == "evidence_completion"
        and str(item.get("status", "")) != "CANCELLED"
        and set(gap_refs).issubset(
            set(_string_list(item.get("input_artifact_ids", ())))
        )
        for item in nodes
    )
    if (
        resolution.get("status") == "needs_evidence"
        and gap_refs
        and gap_stage == "diagnosis"
        and gap_completion_refs
        and not active_diagnosis
        and stage in {"investigation", "diagnosis", "review"}
    ):
        return _fallback_create(
            decision_id=f"fallback-rediagnosis-{state_version}",
            reason="结构化决策恢复：补证完成后基于新旧验证证据重新诊断根因",
            stage="diagnosis",
            node_type="DIAGNOSIS_TASK",
            agent_type="DiagnosticianAgent",
            mode="control_flow",
            objective="基于失败复现和新增验证证据重新分析控制流根因",
            input_refs=verified_evidence_refs,
            trigger_refs=(*gap_refs, *gap_completion_refs),
        )
    if (
        pending_replan_target == "investigation"
        and blocking_review_refs
        and verified_evidence_refs
        and replan_trigger_refs
        and stage == "investigation"
    ):
        return _fallback_create(
            decision_id=f"fallback-replan-investigation-{state_version}",
            reason="结构化决策恢复：执行已经获准的定向 Investigation 重规划",
            stage="investigation",
            node_type="INVESTIGATION_TASK",
            agent_type="InvestigatorAgent",
            mode="evidence_completion",
            objective="根据阻塞性根因审查重新收集可验证执行证据",
            input_refs=(*verified_evidence_refs, *blocking_review_refs),
            trigger_refs=replan_trigger_refs,
            decision_evidence_refs=replan_trigger_refs,
        )
    if (
        resolution.get("status") == "needs_evidence"
        and gap_refs
        and gap_stage == "investigation"
        and not evidence_completion_attempted_for_gap
        and blocking_review_refs
        and stage in {"investigation", "diagnosis", "review"}
    ):
        evidence_refs = tuple(
            str(item["artifact_ref"])
            for item in by_type.get("evidence", [])
        )
        # evidence_completion 的输入契约只允许 Evidence/Review/ArtifactRejection；
        # 带 missing_evidence 的 Hypothesis 只能作为 gate 触发引用，不能进入 Worker 输入，
        # 否则确定性输入契约会拒绝该恢复决策，使安全回退陷入 NO_PROGRESS_LOOP。
        return _fallback_create(
            decision_id=f"fallback-evidence-{state_version}",
            reason="结构化决策恢复：根据阻塞性根因审查补充缺失的执行证据",
            stage="investigation",
            node_type="INVESTIGATION_TASK",
            agent_type="InvestigatorAgent",
            mode="evidence_completion",
            objective="针对根因审查指出的剩余不确定性补充可验证执行证据",
            input_refs=(*evidence_refs, *blocking_review_refs),
            trigger_refs=gap_refs,
        )
    active_patch = any(
        str(item.get("node_type", "")) == "PATCH_TASK"
        and str(item.get("status", ""))
        not in {"FAILED", "TIMED_OUT", "BLOCKED", "CANCELLED"}
        for item in nodes
    )
    if (
        resolution.get("status") == "accepted"
        and accepted_refs
        and review_refs
        and (
            pending_replan_target == "patch"
            or (not by_type.get("patch_candidate") and not active_patch)
        )
        and stage in {"review", "patch"}
    ):
        return _fallback_create(
            decision_id=(
                f"fallback-replan-patch-{state_version}"
                if pending_replan_target == "patch"
                else f"fallback-patch-{state_version}"
            ),
            reason=(
                "结构化决策恢复：执行已经获准的定向 Patch 重规划"
                if pending_replan_target == "patch"
                else "结构化决策恢复：根据已接受根因集合创建最小补丁任务"
            ),
            stage="patch",
            node_type="PATCH_TASK",
            agent_type="PatchAgent",
            mode="minimal",
            objective=(
                "针对上一 Validation 失败生成新的最小补丁，并保持与完整 accepted Hypothesis 集合严格绑定"
                if pending_replan_target == "patch"
                else "生成与完整 accepted Hypothesis 集合严格绑定的最小补丁"
            ),
            input_refs=(
                *accepted_refs,
                *review_refs,
                *(replan_patch_context_refs if pending_replan_target == "patch" else ()),
            ),
            trigger_refs=replan_trigger_refs or None,
            decision_evidence_refs=(
                replan_trigger_refs if pending_replan_target == "patch" else None
            ),
        )

    patch_reviewed_refs = {
        str(_artifact_content(item).get("target_artifact_ref", ""))
        for item in by_type.get("review", [])
        if _artifact_content(item).get("mode") == "patch_review"
    }
    patch_review_attempted_refs = {
        ref
        for item in nodes
        if str(item.get("node_type", "")) == "REVIEW_TASK"
        and str(item.get("mode", "")) == "patch_review"
        and str(item.get("status", "")) != "CANCELLED"
        for ref in _string_list(item.get("input_artifact_ids", ()))
        if ref in by_ref
        and str(by_ref[ref].get("artifact_type", "")) == "patch_candidate"
    }
    if (
        resolution.get("status") == "accepted"
        and accepted_refs
        and review_refs
        and verified_evidence_refs
        and stage in {"patch", "validation"}
    ):
        for patch in reversed(by_type.get("patch_candidate", [])):
            patch_ref = str(patch["artifact_ref"])
            if patch_ref in patch_reviewed_refs or patch_ref in patch_review_attempted_refs:
                continue
            return _fallback_create(
                decision_id=f"fallback-patch-review-{state_version}",
                reason="结构化决策恢复：为尚未审查的补丁候选创建独立 Patch Review",
                stage="patch",
                node_type="REVIEW_TASK",
                agent_type="ReviewerAgent",
                mode="patch_review",
                objective="基于直接证据和已接受根因独立审查当前补丁候选",
                input_refs=(
                    *verified_evidence_refs,
                    *accepted_refs,
                    *review_refs,
                    patch_ref,
                ),
                include_gate=False,
            )

    active_validation = any(
        str(item.get("node_type", "")) == "VALIDATION_TASK"
        and str(item.get("status", ""))
        not in {"FAILED", "TIMED_OUT", "BLOCKED", "CANCELLED"}
        for item in nodes
    )
    if not active_validation and stage in {"patch", "validation"}:
        validated_patch_refs = {
            str(_artifact_content(item).get("patch_ref", ""))
            for item in by_type.get("validation_result", [])
        }
        for patch in reversed(by_type.get("patch_candidate", [])):
            patch_ref = str(patch["artifact_ref"])
            if patch_ref in validated_patch_refs:
                continue
            reviews = [
                item
                for item in by_type.get("review", [])
                if _artifact_content(item).get("mode") == "patch_review"
                and _artifact_content(item).get("verdict")
                in {"approved", "compatible", "supported"}
                and _artifact_content(item).get("target_artifact_ref") == patch_ref
            ]
            if reviews:
                review_ref = str(reviews[-1]["artifact_ref"])
                return _fallback_create(
                    decision_id=f"fallback-validation-{state_version}",
                    reason="结构化决策恢复：验证已通过独立审查的补丁候选",
                    stage="validation",
                    node_type="VALIDATION_TASK",
                    agent_type="ValidationExecutor",
                    mode="deterministic",
                    objective="应用当前补丁并执行目标测试与完整回归",
                    input_refs=(patch_ref, review_ref),
                )
    return None


def _fallback_create(
    *,
    decision_id: str,
    reason: str,
    stage: str,
    node_type: str,
    agent_type: str,
    mode: str,
    objective: str,
    input_refs: Sequence[str],
    trigger_refs: Sequence[str] | None = None,
    include_gate: bool = True,
    decision_evidence_refs: Sequence[str] | None = None,
) -> SupervisorDecision:
    refs = tuple(dict.fromkeys(input_refs))
    triggers = tuple(dict.fromkeys(trigger_refs or refs))
    evidence_refs = tuple(
        dict.fromkeys(
            decision_evidence_refs
            if decision_evidence_refs is not None
            else (*refs, *triggers)
        )
    )
    return SupervisorDecision(
        decision_id=decision_id,
        action="CREATE_TASK",
        reason=reason,
        create_tasks=(
            CreateTaskRequest(
                node_type=node_type,
                agent_type=agent_type,
                mode=mode,
                objective=objective,
                input_artifact_ids=refs,
            ),
        ),
        next_workflow_stage=stage,
        evidence_refs=evidence_refs,
        gate_record=(
            GateRecord(
                gate_name="supervisor_safe_recovery",
                trigger_artifact_refs=triggers,
                reason=reason,
                added_node_count=1,
                budget_effect="新增 1 个受限恢复节点，不执行语义接受或最终选择",
            )
            if include_gate
            else None
        ),
    )


def _artifact_content(item: Mapping[str, Any]) -> Mapping[str, Any]:
    content = item.get("content", {})
    return content if isinstance(content, Mapping) else {}


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item) for item in value if str(item))
