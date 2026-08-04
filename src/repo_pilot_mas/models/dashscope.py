"""DashScope OpenAI-compatible Supervisor router with quota-aware failover."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from repo_pilot_mas.config import load_env_file, load_yaml
from repo_pilot_mas.models.base import (
    GenerationConfig,
    Message,
    ModelAdapter,
    ModelAdapterError,
    RawGeneration,
)
from repo_pilot_mas.runtime.trace import TraceWriter


@dataclass(frozen=True, slots=True)
class SupervisorRoute:
    model_id: str
    api_key_env: str

    @property
    def key(self) -> str:
        return f"{self.model_id}|{self.api_key_env}"


@dataclass(frozen=True, slots=True)
class DashScopeRouterConfig:
    base_url: str
    model_order: tuple[str, ...]
    api_key_envs: tuple[str, ...]
    max_retries_per_route: int = 1
    initial_backoff_seconds: float = 2.0
    per_route_timeout_seconds: float = 30.0
    quota_exhausted_error_codes: tuple[str, ...] = ("AllocationQuota.FreeTierOnly",)
    quota_exhausted_message_patterns: tuple[str, ...] = ("Free allocated quota exceeded",)
    transient_error_codes: tuple[str, ...] = (
        "Throttling",
        "Throttling.RateQuota",
        "Throttling.AllocationQuota",
        "ModelUnavailable",
    )

    def __post_init__(self) -> None:
        if not self.base_url.startswith("https://"):
            raise ValueError("DashScope base_url must use HTTPS")
        if not self.model_order or not self.api_key_envs:
            raise ValueError("model_order and api_key_envs must not be empty")
        if len(set(self.model_order)) != len(self.model_order):
            raise ValueError("model_order contains duplicates")
        if len(set(self.api_key_envs)) != len(self.api_key_envs):
            raise ValueError("api_key_envs contains duplicates")
        if self.max_retries_per_route < 0 or self.initial_backoff_seconds < 0:
            raise ValueError("retry count and backoff must be non-negative")
        if (
            not math.isfinite(self.per_route_timeout_seconds)
            or self.per_route_timeout_seconds <= 0
        ):
            raise ValueError("per_route_timeout_seconds must be positive")

    @property
    def routes(self) -> tuple[SupervisorRoute, ...]:
        return tuple(
            SupervisorRoute(model, api_key_env)
            for model in self.model_order
            for api_key_env in self.api_key_envs
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DashScopeRouterConfig:
        routing = _mapping(value, "routing")
        failover = _mapping(value, "failover")
        if routing.get("strategy") != "model_first_account_second":
            raise ValueError("unsupported supervisor routing strategy")
        return cls(
            base_url=str(value["base_url"]),
            model_order=_strings(routing["model_order"]),
            api_key_envs=_strings(routing["api_key_envs"]),
            max_retries_per_route=int(failover.get("max_retries_per_route", 1)),
            initial_backoff_seconds=float(failover.get("initial_backoff_seconds", 2)),
            per_route_timeout_seconds=float(
                failover.get("per_route_timeout_seconds", 30)
            ),
            quota_exhausted_error_codes=_strings(
                failover.get("quota_exhausted_error_codes", ("AllocationQuota.FreeTierOnly",))
            ),
            quota_exhausted_message_patterns=_strings(
                failover.get("quota_exhausted_message_patterns", ("Free allocated quota exceeded",))
            ),
            transient_error_codes=_strings(failover.get("transient_error_codes", ("Throttling",))),
        )


@dataclass(frozen=True, slots=True)
class DashScopeHTTPResponse:
    text: str
    input_tokens: int
    output_tokens: int
    request_id: str | None = None


class DashScopeRequestError(RuntimeError):
    def __init__(
        self,
        status_code: int | None,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id


Requester = Callable[
    [str, SupervisorRoute, str, Sequence[Message], GenerationConfig],
    DashScopeHTTPResponse,
]


class SupervisorModelRouter(ModelAdapter):
    """Try model-major/account-minor routes without leaking credential values."""

    def __init__(
        self,
        config: DashScopeRouterConfig,
        *,
        raw_log_dir: str | Path | None = None,
        trace_writer: TraceWriter | None = None,
        requester: Requester | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__("dashscope-supervisor-router", raw_log_dir=raw_log_dir)
        self.config = config
        self.trace_writer = trace_writer
        self._requester = requester or _request_dashscope
        self._sleeper = sleeper
        self._exhausted_routes: set[str] = set()
        self._lock = threading.Lock()

    @property
    def routes(self) -> tuple[SupervisorRoute, ...]:
        return self.config.routes

    @property
    def exhausted_routes(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(route.key for route in self.routes if route.key in self._exhausted_routes)

    def to_state_dict(self) -> dict[str, Any]:
        return {"exhausted_routes": list(self.exhausted_routes)}

    def restore_state(self, value: Mapping[str, Any]) -> None:
        known = {route.key for route in self.routes}
        restored = set(_strings(value.get("exhausted_routes", ())))
        unknown = restored - known
        if unknown:
            raise ValueError(f"router checkpoint contains unknown routes: {sorted(unknown)}")
        with self._lock:
            self._exhausted_routes = restored

    def _generate_once(
        self,
        messages: Sequence[Message],
        config: GenerationConfig,
    ) -> RawGeneration:
        overall_deadline = time.perf_counter() + config.timeout_seconds
        request_count = 0
        transient_failures = 0
        route_timeouts = 0
        for route in self.routes:
            with self._lock:
                if route.key in self._exhausted_routes:
                    continue
            api_key = os.environ.get(route.api_key_env)
            if not api_key:
                raise ModelAdapterError(
                    "SUPERVISOR_CONFIGURATION_ERROR",
                    f"required credential is missing: {route.api_key_env}",
                    details={"model_id": route.model_id, "api_key_env": route.api_key_env},
                )

            route_started = time.perf_counter()
            route_deadline = min(
                overall_deadline,
                route_started + self.config.per_route_timeout_seconds,
            )
            for retry_index in range(self.config.max_retries_per_route + 1):
                now = time.perf_counter()
                overall_remaining = overall_deadline - now
                if overall_remaining <= 0:
                    raise self._timeout_error(
                        request_count,
                        route_timeouts,
                        "supervisor overall deadline was exhausted",
                    )
                route_remaining = route_deadline - now
                if route_remaining <= 0:
                    route_timeouts += 1
                    self._trace_route(
                        "supervisor_route_timeout",
                        route,
                        retry_index=retry_index,
                        timeout_seconds=self.config.per_route_timeout_seconds,
                        elapsed_ms=int((now - route_started) * 1000),
                    )
                    break

                request_count += 1
                request_timeout = min(overall_remaining, route_remaining)
                self._trace_route(
                    "supervisor_route_attempt",
                    route,
                    retry_index=retry_index,
                    timeout_seconds=request_timeout,
                )
                try:
                    response = self._requester(
                        self.config.base_url,
                        route,
                        api_key,
                        messages,
                        replace(config, timeout_seconds=request_timeout),
                    )
                except DashScopeRequestError as exc:
                    if self._is_quota_exhausted(exc):
                        with self._lock:
                            self._exhausted_routes.add(route.key)
                        self._trace_route(
                            "supervisor_route_exhausted",
                            route,
                            retry_index=retry_index,
                            error=exc,
                        )
                        break
                    if self._is_configuration_error(exc):
                        self._trace_route(
                            "supervisor_route_configuration_error",
                            route,
                            retry_index=retry_index,
                            error=exc,
                        )
                        raise ModelAdapterError(
                            "SUPERVISOR_CONFIGURATION_ERROR",
                            str(exc),
                            attempts=request_count,
                            details=_error_details(route, exc),
                        ) from exc
                    if not self._is_transient(exc):
                        self._trace_route(
                            "supervisor_route_failed",
                            route,
                            retry_index=retry_index,
                            error=exc,
                        )
                        raise ModelAdapterError(
                            "SUPERVISOR_API_ERROR",
                            str(exc),
                            attempts=request_count,
                            details=_error_details(route, exc),
                        ) from exc
                    transient_failures += 1
                    self._trace_route(
                        "supervisor_route_transient_error",
                        route,
                        retry_index=retry_index,
                        error=exc,
                    )
                    if retry_index < self.config.max_retries_per_route:
                        self._sleep_within_deadlines(
                            retry_index,
                            route_deadline,
                            overall_deadline,
                        )
                        continue
                    break
                except TimeoutError as exc:
                    transient_failures += 1
                    route_timeouts += 1
                    self._trace_route(
                        "supervisor_route_timeout",
                        route,
                        retry_index=retry_index,
                        error=DashScopeRequestError(
                            None,
                            type(exc).__name__,
                            str(exc),
                        ),
                        timeout_seconds=request_timeout,
                        elapsed_ms=int(
                            (time.perf_counter() - route_started) * 1000
                        ),
                    )
                    break
                except OSError as exc:
                    transient_failures += 1
                    self._trace_route(
                        "supervisor_route_transient_error",
                        route,
                        retry_index=retry_index,
                        error=DashScopeRequestError(
                            None,
                            type(exc).__name__,
                            str(exc),
                        ),
                    )
                    if retry_index < self.config.max_retries_per_route:
                        self._sleep_within_deadlines(
                            retry_index,
                            route_deadline,
                            overall_deadline,
                        )
                        continue
                    break
                else:
                    metadata = {
                        "model_provider": "dashscope_openai_compatible",
                        "api_key_env": route.api_key_env,
                        "request_id": response.request_id,
                        "route_attempts": request_count,
                        "route_timeouts": route_timeouts,
                        "per_route_timeout_seconds": (
                            self.config.per_route_timeout_seconds
                        ),
                    }
                    self._trace_route(
                        "supervisor_route_succeeded",
                        route,
                        retry_index=retry_index,
                        request_id=response.request_id,
                    )
                    return RawGeneration(
                        response.text,
                        response.input_tokens,
                        response.output_tokens,
                        model_id=route.model_id,
                        metadata=metadata,
                    )

        if time.perf_counter() >= overall_deadline:
            raise self._timeout_error(
                request_count,
                route_timeouts,
                "supervisor overall deadline was exhausted",
            )

        with self._lock:
            all_exhausted = len(self._exhausted_routes) == len(self.routes)
        code = "SUPERVISOR_ROUTES_EXHAUSTED" if all_exhausted else "SUPERVISOR_ROUTES_UNAVAILABLE"
        message = (
            "all configured supervisor routes have exhausted their free quota"
            if all_exhausted
            else "all eligible supervisor routes failed with transient errors"
        )
        raise ModelAdapterError(
            code,
            message,
            attempts=request_count,
            details={
                "exhausted_routes": list(self.exhausted_routes),
                "transient_failures": transient_failures,
                "route_timeouts": route_timeouts,
                "per_route_timeout_seconds": self.config.per_route_timeout_seconds,
            },
        )

    def _timeout_error(
        self,
        request_count: int,
        route_timeouts: int,
        message: str,
    ) -> ModelAdapterError:
        return ModelAdapterError(
            "MODEL_TIMEOUT",
            message,
            attempts=request_count,
            details={
                "exhausted_routes": list(self.exhausted_routes),
                "route_timeouts": route_timeouts,
                "per_route_timeout_seconds": self.config.per_route_timeout_seconds,
            },
        )

    def _sleep_within_deadlines(
        self,
        retry_index: int,
        route_deadline: float,
        overall_deadline: float,
    ) -> None:
        backoff = self.config.initial_backoff_seconds * (2**retry_index)
        remaining = max(
            min(route_deadline, overall_deadline) - time.perf_counter(),
            0,
        )
        self._sleeper(min(backoff, remaining))

    def _is_quota_exhausted(self, error: DashScopeRequestError) -> bool:
        if error.code in self.config.quota_exhausted_error_codes:
            return True
        message = str(error).casefold()
        return any(
            pattern.casefold() in message
            for pattern in self.config.quota_exhausted_message_patterns
        )

    def _is_configuration_error(self, error: DashScopeRequestError) -> bool:
        return error.status_code in {400, 401, 403, 404}

    def _is_transient(self, error: DashScopeRequestError) -> bool:
        if error.status_code in {429, 500, 502, 503, 504}:
            return True
        return any(
            error.code == code or error.code.startswith(f"{code}.")
            for code in self.config.transient_error_codes
        )

    def _trace_route(
        self,
        event_type: str,
        route: SupervisorRoute,
        *,
        retry_index: int,
        error: DashScopeRequestError | None = None,
        request_id: str | None = None,
        timeout_seconds: float | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        if self.trace_writer is None:
            return
        data: dict[str, Any] = {
            "model_provider": "dashscope_openai_compatible",
            "model_id": route.model_id,
            "api_key_env": route.api_key_env,
            "retry_index": retry_index,
            "request_id": request_id or (error.request_id if error else None),
        }
        if timeout_seconds is not None:
            data["timeout_seconds"] = timeout_seconds
        if elapsed_ms is not None:
            data["elapsed_ms"] = elapsed_ms
        if error is not None:
            data.update(
                http_status=error.status_code,
                error_code=error.code,
                error_message=str(error),
            )
        self.trace_writer.write(event_type, data)


def create_supervisor_model_router(
    config_path: str | Path,
    *,
    raw_log_dir: str | Path,
    trace_writer: TraceWriter | None = None,
    requester: Requester | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[SupervisorModelRouter, GenerationConfig]:
    path = Path(config_path).expanduser().resolve(strict=True)
    config_value = load_yaml(path)
    supervisor_value = _mapping(config_value, "supervisor")
    if supervisor_value.get("provider") != "dashscope_openai_compatible":
        raise ValueError("unsupported supervisor provider")
    router_config = DashScopeRouterConfig.from_mapping(supervisor_value)
    credential_path = Path(str(supervisor_value["credential_file"])).expanduser()
    if not credential_path.is_absolute():
        credential_path = path.parent.parent / credential_path
    load_env_file(credential_path, required_names=router_config.api_key_envs)
    generation_value = _mapping(supervisor_value, "generation")
    generation = GenerationConfig(
        temperature=float(generation_value.get("temperature", 0.0)),
        max_output_tokens=int(generation_value.get("max_output_tokens", 2048)),
        timeout_seconds=float(generation_value.get("timeout_seconds", 120)),
        max_retries=int(generation_value.get("max_format_repairs", 1)),
    )
    return (
        SupervisorModelRouter(
            router_config,
            raw_log_dir=raw_log_dir,
            trace_writer=trace_writer,
            requester=requester,
            sleeper=sleeper,
        ),
        generation,
    )


def _request_dashscope(
    base_url: str,
    route: SupervisorRoute,
    api_key: str,
    messages: Sequence[Message],
    config: GenerationConfig,
) -> DashScopeHTTPResponse:
    payload: dict[str, Any] = {
        "model": route.model_id,
        "messages": [message.to_dict() for message in messages],
        "temperature": config.temperature,
        "max_tokens": config.max_output_tokens,
    }
    if config.stop:
        payload["stop"] = list(config.stop)
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            response_headers = response.headers
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        error = body.get("error", body)
        if not isinstance(error, Mapping):
            error = {}
        request_id = _request_id(body, exc.headers)
        raise DashScopeRequestError(
            exc.code,
            str(error.get("code", f"HTTP_{exc.code}")),
            str(error.get("message", exc.reason)),
            request_id=request_id,
        ) from exc
    except urllib.error.URLError as exc:
        raise OSError(str(exc.reason)) from exc

    if not isinstance(body, Mapping):
        raise DashScopeRequestError(None, "INVALID_RESPONSE", "response body is not an object")
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DashScopeRequestError(
            None,
            "INVALID_RESPONSE",
            "response does not contain message content",
            request_id=_request_id(body, response_headers),
        ) from exc
    if not isinstance(text, str):
        raise DashScopeRequestError(None, "INVALID_RESPONSE", "message content is not text")
    usage = body.get("usage", {})
    if not isinstance(usage, Mapping):
        usage = {}
    return DashScopeHTTPResponse(
        text=text,
        input_tokens=int(usage.get("prompt_tokens", 0)),
        output_tokens=int(usage.get("completion_tokens", 0)),
        request_id=_request_id(body, response_headers),
    )


def _request_id(body: Mapping[str, Any], headers: Any) -> str | None:
    return (
        str(body.get("request_id") or body.get("id"))
        if body.get("request_id") or body.get("id")
        else headers.get("x-request-id")
    )


def _error_details(route: SupervisorRoute, error: DashScopeRequestError) -> dict[str, Any]:
    return {
        "model_id": route.model_id,
        "api_key_env": route.api_key_env,
        "http_status": error.status_code,
        "error_code": error.code,
        "request_id": error.request_id,
    }


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    selected = value.get(key, {})
    if not isinstance(selected, Mapping):
        raise TypeError(f"configuration section must be a mapping: {key}")
    return selected


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("expected a sequence of strings")
    return tuple(str(item) for item in value)
