"""Model adapter protocol with structured-output repair and bounded retries."""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from repo_pilot_mas.schemas.json_schema import SchemaValidationError, validate_json_schema

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError(f"unsupported message role: {self.role}")
        if not self.content.strip():
            raise ValueError("message content must not be empty")

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    temperature: float = 0.0
    max_output_tokens: int = 512
    stop: tuple[str, ...] = ()
    timeout_seconds: float = 120.0
    max_retries: int = 1

    def __post_init__(self) -> None:
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        object.__setattr__(self, "stop", tuple(str(item) for item in self.stop))


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class RawGeneration:
    text: str
    input_tokens: int
    output_tokens: int
    model_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    structured_output: Mapping[str, Any] | None
    usage: TokenUsage
    latency_ms: int
    model_id: str
    raw_response_ref: str
    trace_id: str
    attempts: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)


class ModelAdapterError(RuntimeError):
    """Structured model failure retaining accounting from attempted calls."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        attempts: int = 0,
        usage: TokenUsage | None = None,
        latency_ms: int = 0,
        raw_response_refs: Sequence[str] = (),
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.attempts = attempts
        self.usage = usage or TokenUsage()
        self.latency_ms = latency_ms
        self.raw_response_refs = tuple(raw_response_refs)
        self.details = dict(details or {})


class ModelAdapter(ABC):
    """Provider-neutral generation API used by agents."""

    def __init__(self, model_id: str, *, raw_log_dir: str | Path | None = None) -> None:
        if not model_id.strip():
            raise ValueError("model_id must not be empty")
        self.model_id = model_id.strip()
        self.raw_log_dir = Path(raw_log_dir).expanduser() if raw_log_dir else None

    def generate(
        self,
        messages: Sequence[Message],
        *,
        response_schema: Mapping[str, Any] | None = None,
        config: GenerationConfig | None = None,
    ) -> ModelResponse:
        active_config = config or GenerationConfig()
        active_messages = tuple(messages)
        if not active_messages:
            raise ValueError("messages must not be empty")
        if response_schema is not None:
            active_messages += (
                Message(
                    "user",
                    "只返回一个符合以下 JSON Schema 的 JSON 对象，不要使用 Markdown：\n"
                    + json.dumps(response_schema, ensure_ascii=False, separators=(",", ":")),
                ),
            )

        started = time.perf_counter()
        deadline = started + active_config.timeout_seconds
        input_tokens = 0
        output_tokens = 0
        raw_refs: list[str] = []
        last_error = ""
        for attempt in range(1, active_config.max_retries + 2):
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise ModelAdapterError(
                    "MODEL_TIMEOUT",
                    f"model generation exceeded {active_config.timeout_seconds} seconds",
                    attempts=attempt - 1,
                    usage=TokenUsage(input_tokens, output_tokens),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    raw_response_refs=raw_refs,
                )
            try:
                raw = self._generate_once(
                    active_messages,
                    replace(active_config, timeout_seconds=remaining),
                )
            except ModelAdapterError:
                raise
            except Exception as exc:
                raise ModelAdapterError(
                    "MODEL_GENERATION_ERROR",
                    str(exc),
                    attempts=attempt,
                    usage=TokenUsage(input_tokens, output_tokens),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    raw_response_refs=raw_refs,
                ) from exc
            input_tokens += raw.input_tokens
            output_tokens += raw.output_tokens
            content = _apply_stop(raw.text, active_config.stop)
            trace_id = uuid4().hex
            response_model_id = raw.model_id or self.model_id
            raw_ref = self._write_raw_response(
                trace_id,
                attempt=attempt,
                messages=active_messages,
                content=content,
                usage=TokenUsage(raw.input_tokens, raw.output_tokens),
                model_id=response_model_id,
                metadata=raw.metadata,
            )
            raw_refs.append(raw_ref)

            structured_output: Mapping[str, Any] | None = None
            try:
                if response_schema is not None:
                    structured_output = _parse_json_object(content)
                    validate_json_schema(structured_output, response_schema)
                return ModelResponse(
                    content=content,
                    structured_output=structured_output,
                    usage=TokenUsage(input_tokens, output_tokens),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    model_id=response_model_id,
                    raw_response_ref=raw_ref,
                    trace_id=trace_id,
                    attempts=attempt,
                    metadata=dict(raw.metadata),
                )
            except (ValueError, SchemaValidationError) as exc:
                last_error = str(exc)
                if attempt > active_config.max_retries:
                    break
                active_messages += (
                    Message("assistant", content),
                    Message(
                        "user",
                        f"上一个响应不符合 Schema：{last_error}。请只返回修正后的 JSON 对象。",
                    ),
                )

        raise ModelAdapterError(
            "STRUCTURED_OUTPUT_ERROR",
            last_error or "model did not return valid structured output",
            attempts=active_config.max_retries + 1,
            usage=TokenUsage(input_tokens, output_tokens),
            latency_ms=int((time.perf_counter() - started) * 1000),
            raw_response_refs=raw_refs,
        )

    async def agenerate(
        self,
        messages: Sequence[Message],
        *,
        response_schema: Mapping[str, Any] | None = None,
        config: GenerationConfig | None = None,
    ) -> ModelResponse:
        return await asyncio.to_thread(
            self.generate,
            messages,
            response_schema=response_schema,
            config=config,
        )

    @abstractmethod
    def _generate_once(
        self,
        messages: Sequence[Message],
        config: GenerationConfig,
    ) -> RawGeneration:
        """Return one raw provider generation."""

    def _write_raw_response(
        self,
        trace_id: str,
        *,
        attempt: int,
        messages: Sequence[Message],
        content: str,
        usage: TokenUsage,
        model_id: str,
        metadata: Mapping[str, Any],
    ) -> str:
        if self.raw_log_dir is None:
            return f"memory://{model_id}/{trace_id}"
        self.raw_log_dir.mkdir(parents=True, exist_ok=True)
        path = self.raw_log_dir / f"{trace_id}.json"
        payload = {
            "trace_id": trace_id,
            "model_id": model_id,
            "attempt": attempt,
            "messages": [message.to_dict() for message in messages],
            "content": content,
            "usage": usage.to_dict(),
            "metadata": dict(metadata),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return str(path)


def _parse_json_object(content: str) -> Mapping[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```json"):
        stripped = stripped[7:]
    elif stripped.startswith("```"):
        stripped = stripped[3:]
    stripped = stripped.removesuffix("```")
    decoder = json.JSONDecoder()
    for index, character in enumerate(stripped):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            return value
    raise ValueError("response does not contain a JSON object")


def _apply_stop(value: str, stops: Sequence[str]) -> str:
    positions = [value.find(stop) for stop in stops if stop and value.find(stop) >= 0]
    return value[: min(positions)] if positions else value
