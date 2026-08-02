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

_SYSTEM_PROMPT = """你是 RepoPilot-MAS 的全局 SupervisorAgent。
你的职责是从完整状态快照中做全局编排决策，而不是执行代码修改。

硬约束：
1. 每次只能选择一个 action，并且只输出符合给定 Schema 的 JSON 对象。
2. 不得引用快照中不存在的任务或 Artifact；不得绕过依赖、预算和阶段约束。
3. 初始化且任务图为空时，创建至少一个 INVESTIGATION_TASK，并将阶段推进到 investigation。
4. 子任务目标必须具体、可验证；本地 Worker 只负责执行已规划任务。
5. 只有补丁验证通过后才可 FINALIZE_TASK；无法安全继续时使用 TERMINATE_TASK。
6. decision_id 必须唯一，只使用字母、数字、点、下划线或连字符。
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
    ) -> None:
        self.model = model
        self.generation_config = generation_config or GenerationConfig(
            temperature=0.0,
            max_output_tokens=2048,
            max_retries=1,
        )
        self.trace_writer = trace_writer

    def decide(self, snapshot: Mapping[str, Any]) -> SupervisorOutcome:
        messages = (
            Message("system", _SYSTEM_PROMPT),
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
