"""Budgeted ReAct loop that only executes registered structured tools."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from repo_pilot_mas.models import (
    GenerationConfig,
    Message,
    ModelAdapter,
    ModelAdapterError,
)
from repo_pilot_mas.runtime.trace import TraceWriter
from repo_pilot_mas.schemas import ToolResult
from repo_pilot_mas.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ReactBudget:
    max_steps: int = 10
    max_model_calls: int = 12
    max_tool_calls: int = 12
    max_input_tokens: int = 40_000
    max_output_tokens: int = 8_000
    max_runtime_seconds: float = 120.0

    def __post_init__(self) -> None:
        values = (
            self.max_steps,
            self.max_model_calls,
            self.max_tool_calls,
            self.max_input_tokens,
            self.max_output_tokens,
        )
        if any(value <= 0 for value in values) or self.max_runtime_seconds <= 0:
            raise ValueError("all ReAct budgets must be positive")


@dataclass(frozen=True, slots=True)
class ReactResult:
    status: Literal["completed", "failed"]
    reason: str
    final_action: Mapping[str, Any] | None
    steps: int
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    duration_ms: int
    successful_tools: tuple[str, ...]
    messages: tuple[Message, ...]


class ReactLoop:
    def __init__(
        self,
        model: ModelAdapter,
        tools: ToolRegistry,
        *,
        budget: ReactBudget,
        generation_config: GenerationConfig | None = None,
        trace_writer: TraceWriter | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.budget = budget
        self.generation_config = generation_config or GenerationConfig()
        self.trace_writer = trace_writer

    def run(self, messages: Sequence[Message]) -> ReactResult:
        history = list(messages)
        started = time.perf_counter()
        model_calls = 0
        tool_calls = 0
        input_tokens = 0
        output_tokens = 0
        successful_tools: list[str] = []
        failed_action_signatures: set[str] = set()
        response_schema = _response_schema(self.tools.names)

        for step in range(1, self.budget.max_steps + 1):
            elapsed = time.perf_counter() - started
            remaining = self.budget.max_runtime_seconds - elapsed
            if remaining <= 0:
                return self._failure(
                    "RUNTIME_BUDGET_EXHAUSTED",
                    step - 1,
                    model_calls,
                    tool_calls,
                    input_tokens,
                    output_tokens,
                    started,
                    successful_tools,
                    history,
                )
            if model_calls >= self.budget.max_model_calls:
                return self._failure(
                    "MODEL_CALL_BUDGET_EXHAUSTED",
                    step - 1,
                    model_calls,
                    tool_calls,
                    input_tokens,
                    output_tokens,
                    started,
                    successful_tools,
                    history,
                )

            config = replace(
                self.generation_config,
                timeout_seconds=min(self.generation_config.timeout_seconds, remaining),
            )
            try:
                response = self.model.generate(
                    history,
                    response_schema=response_schema,
                    config=config,
                )
            except ModelAdapterError as exc:
                model_calls += exc.attempts
                input_tokens += exc.usage.input_tokens
                output_tokens += exc.usage.output_tokens
                self._trace(
                    "model_error",
                    {
                        "step": step,
                        "model_id": self.model.model_id,
                        "code": exc.code,
                        "message": str(exc),
                        "attempts": exc.attempts,
                        "usage": exc.usage.to_dict(),
                        "latency_ms": exc.latency_ms,
                        "raw_response_refs": list(exc.raw_response_refs),
                    },
                )
                return self._failure(
                    exc.code,
                    step,
                    model_calls,
                    tool_calls,
                    input_tokens,
                    output_tokens,
                    started,
                    successful_tools,
                    history,
                )

            model_calls += response.attempts
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens
            self._trace(
                "model_call",
                {
                    "step": step,
                    "model_id": response.model_id,
                    "attempts": response.attempts,
                    "usage": response.usage.to_dict(),
                    "latency_ms": response.latency_ms,
                    "raw_response_ref": response.raw_response_ref,
                },
                trace_id=response.trace_id,
            )
            if (
                model_calls > self.budget.max_model_calls
                or input_tokens > self.budget.max_input_tokens
                or output_tokens > self.budget.max_output_tokens
            ):
                return self._failure(
                    "TOKEN_OR_MODEL_BUDGET_EXHAUSTED",
                    step,
                    model_calls,
                    tool_calls,
                    input_tokens,
                    output_tokens,
                    started,
                    successful_tools,
                    history,
                )

            assert response.structured_output is not None
            action = response.structured_output["action"]
            history.append(Message("assistant", response.content))
            if action["type"] == "final":
                self._trace("react_final_action", {"step": step, "action": dict(action)})
                return ReactResult(
                    status="completed",
                    reason=str(action["reason"]),
                    final_action=dict(action),
                    steps=step,
                    model_calls=model_calls,
                    tool_calls=tool_calls,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    successful_tools=tuple(successful_tools),
                    messages=tuple(history),
                )

            if tool_calls >= self.budget.max_tool_calls:
                return self._failure(
                    "TOOL_CALL_BUDGET_EXHAUSTED",
                    step,
                    model_calls,
                    tool_calls,
                    input_tokens,
                    output_tokens,
                    started,
                    successful_tools,
                    history,
                )
            tool_name = str(action["tool_name"])
            arguments = action["arguments"]
            action_signature = json.dumps(
                {"tool_name": tool_name, "arguments": arguments},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if action_signature in failed_action_signatures:
                tool_result = ToolResult.failure(
                    tool_name,
                    code="REPEATED_NO_PROGRESS_ACTION",
                    message=(
                        "相同工具参数已经失败，禁止原样重试。对于 apply_patch，请重新 inspect_code，"
                        "删除虚构 index 行，并使用精确的 -旧行/+新行生成最小 hunk。"
                    ),
                )
            else:
                tool_result = self.tools.invoke(tool_name, arguments)
            tool_calls += 1
            if tool_result.ok:
                successful_tools.append(tool_name)
            else:
                failed_action_signatures.add(action_signature)
            self._trace(
                "tool_call",
                {
                    "step": step,
                    "tool": tool_name,
                    "arguments": dict(arguments),
                    "result": tool_result.to_dict(),
                },
                trace_id=tool_result.trace_id,
            )
            history.append(
                Message(
                    "user",
                    "工具执行结果："
                    + json.dumps(tool_result.to_dict(), ensure_ascii=False, separators=(",", ":")),
                )
            )

        return self._failure(
            "MAX_STEPS_EXHAUSTED",
            self.budget.max_steps,
            model_calls,
            tool_calls,
            input_tokens,
            output_tokens,
            started,
            successful_tools,
            history,
        )

    def _failure(
        self,
        reason: str,
        steps: int,
        model_calls: int,
        tool_calls: int,
        input_tokens: int,
        output_tokens: int,
        started: float,
        successful_tools: Sequence[str],
        history: Sequence[Message],
    ) -> ReactResult:
        self._trace("react_failed", {"reason": reason, "steps": steps})
        return ReactResult(
            status="failed",
            reason=reason,
            final_action=None,
            steps=steps,
            model_calls=model_calls,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=int((time.perf_counter() - started) * 1000),
            successful_tools=tuple(successful_tools),
            messages=tuple(history),
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


def _response_schema(tool_names: Sequence[str]) -> dict[str, Any]:
    thought = {"type": "string"}
    return {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "thought_summary": thought,
                    "action": {
                        "type": "object",
                        "properties": {
                            "type": {"const": "tool"},
                            "tool_name": {"type": "string", "enum": list(tool_names)},
                            "arguments": {"type": "object"},
                        },
                        "required": ["type", "tool_name", "arguments"],
                        "additionalProperties": False,
                    },
                },
                "required": ["thought_summary", "action"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "thought_summary": thought,
                    "action": {
                        "type": "object",
                        "properties": {
                            "type": {"const": "final"},
                            "status": {"type": "string", "enum": ["success", "failure"]},
                            "reason": {"type": "string"},
                        },
                        "required": ["type", "status", "reason"],
                        "additionalProperties": False,
                    },
                },
                "required": ["thought_summary", "action"],
                "additionalProperties": False,
            },
        ]
    }
