from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

import pytest

from repo_pilot_mas.config import load_env_file, load_yaml
from repo_pilot_mas.models import (
    DashScopeHTTPResponse,
    DashScopeRequestError,
    DashScopeRouterConfig,
    GenerationConfig,
    Message,
    ModelAdapterError,
    SupervisorModelRouter,
)
from repo_pilot_mas.models.dashscope import (
    _request_dashscope,
    create_supervisor_model_router,
)
from repo_pilot_mas.runtime import TraceWriter

_MODELS = ("max", "flash", "flash-versioned")
_KEY_ENVS = ("DASHSCOPE_TEST_KEY_1", "DASHSCOPE_TEST_KEY_2")


def _config(*, retries: int = 1) -> DashScopeRouterConfig:
    return DashScopeRouterConfig(
        base_url="https://example.test/v1",
        model_order=_MODELS,
        api_key_envs=_KEY_ENVS,
        max_retries_per_route=retries,
        initial_backoff_seconds=0,
    )


def _set_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_KEY_ENVS[0], "secret-account-1")
    monkeypatch.setenv(_KEY_ENVS[1], "secret-account-2")


def _generate(router: SupervisorModelRouter) -> None:
    router.generate(
        [Message("user", "decide")],
        config=GenerationConfig(max_retries=0),
    )


def test_routes_are_model_major_then_account_minor() -> None:
    routes = _config().routes
    assert [(route.model_id, route.api_key_env) for route in routes] == [
        ("max", _KEY_ENVS[0]),
        ("max", _KEY_ENVS[1]),
        ("flash", _KEY_ENVS[0]),
        ("flash", _KEY_ENVS[1]),
        ("flash-versioned", _KEY_ENVS[0]),
        ("flash-versioned", _KEY_ENVS[1]),
    ]


def test_repository_config_matches_current_six_route_policy() -> None:
    config = DashScopeRouterConfig.from_mapping(
        load_yaml("configs/supervisor.yaml")["supervisor"]
    )

    assert [(route.model_id, route.api_key_env) for route in config.routes] == [
        ("deepseek-v4-pro-0813", "DASHSCOPE_API_KEY_1"),
        ("deepseek-v4-pro-0813", "DASHSCOPE_API_KEY_2"),
        ("deepseek-v4-flash-0731", "DASHSCOPE_API_KEY_1"),
        ("deepseek-v4-flash-0731", "DASHSCOPE_API_KEY_2"),
        ("qwen3.8-max", "DASHSCOPE_API_KEY_1"),
        ("qwen3.8-max", "DASHSCOPE_API_KEY_2"),
    ]
    assert config.per_route_timeout_seconds == 60


def test_repository_supervisor_disables_thinking_for_structured_json(
    tmp_path: Path,
) -> None:
    router, generation = create_supervisor_model_router(
        "configs/supervisor.yaml",
        raw_log_dir=tmp_path / "raw",
    )
    try:
        assert generation.enable_thinking is False
    finally:
        router.close()


def test_raw_dashscope_request_sends_enable_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            del args

        @staticmethod
        def read() -> bytes:
            return json.dumps(
                {
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 2,
                    },
                }
            ).encode()

    def urlopen(request, timeout):  # type: ignore[no-untyped-def]
        assert isinstance(request, urllib.request.Request)
        captured.update(json.loads(request.data.decode()))
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    response = _request_dashscope(
        "https://example.test/v1",
        _config().routes[0],
        "secret",
        [Message("user", "decide")],
        GenerationConfig(enable_thinking=False),
    )

    assert response.text == "{}"
    assert captured["enable_thinking"] is False


def test_quota_exhaustion_switches_to_same_model_other_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_keys(monkeypatch)
    calls: list[tuple[str, str]] = []

    def requester(base_url, route, api_key, messages, config):  # type: ignore[no-untyped-def]
        del base_url, api_key, messages, config
        calls.append((route.model_id, route.api_key_env))
        if route.api_key_env == _KEY_ENVS[0]:
            raise DashScopeRequestError(
                403,
                "AllocationQuota.FreeTierOnly",
                "Free allocated quota exceeded",
            )
        return DashScopeHTTPResponse("ok", 2, 1, "request-2")

    router = SupervisorModelRouter(_config(), requester=requester, sleeper=lambda _: None)
    _generate(router)

    assert calls == [("max", _KEY_ENVS[0]), ("max", _KEY_ENVS[1])]
    assert router.exhausted_routes == (f"max|{_KEY_ENVS[0]}",)


def test_model_fallback_is_recorded_not_treated_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_keys(monkeypatch)
    calls: list[tuple[str, str]] = []

    def requester(base_url, route, api_key, messages, config):  # type: ignore[no-untyped-def]
        del base_url, api_key, messages, config
        calls.append((route.model_id, route.api_key_env))
        if route.model_id == "max":
            raise DashScopeRequestError(403, "AllocationQuota.FreeTierOnly", "quota")
        return DashScopeHTTPResponse("ok", 2, 1)

    router = SupervisorModelRouter(_config(), requester=requester, sleeper=lambda _: None)
    _generate(router)

    assert calls == [
        ("max", _KEY_ENVS[0]),
        ("max", _KEY_ENVS[1]),
        ("flash", _KEY_ENVS[0]),
    ]


def test_structured_output_recovery_advances_route_without_exhausting_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_keys(monkeypatch)
    calls: list[tuple[str, str]] = []

    def requester(base_url, route, api_key, messages, config):  # type: ignore[no-untyped-def]
        del base_url, api_key, messages, config
        calls.append((route.model_id, route.api_key_env))
        text = '{"wrong":true}' if route.api_key_env == _KEY_ENVS[0] else '{"ok":true}'
        return DashScopeHTTPResponse(text, 2, 1)

    trace_path = tmp_path / "route-format-recovery.jsonl"
    router = SupervisorModelRouter(
        _config(retries=0),
        requester=requester,
        sleeper=lambda _: None,
        trace_writer=TraceWriter(trace_path),
    )
    schema = {
        "type": "object",
        "properties": {"ok": {"const": True}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    router.begin_structured_recovery()

    response = router.generate(
        [Message("user", "decide")],
        response_schema=schema,
        config=GenerationConfig(max_retries=1),
    )

    assert response.structured_output == {"ok": True}
    assert calls == [
        ("max", _KEY_ENVS[0]),
        ("max", _KEY_ENVS[1]),
    ]
    assert router.exhausted_routes == ()
    events = [
        json.loads(line)["event_type"]
        for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events.count("supervisor_route_format_rejected") == 1

    router.begin_structured_recovery()
    router.generate(
        [Message("user", "decide again")],
        response_schema=schema,
        config=GenerationConfig(max_retries=1),
    )
    assert calls[2] == ("max", _KEY_ENVS[0])


def test_all_routes_exhausted_is_structured_and_restorable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_keys(monkeypatch)

    def requester(base_url, route, api_key, messages, config):  # type: ignore[no-untyped-def]
        del base_url, route, api_key, messages, config
        raise DashScopeRequestError(403, "AllocationQuota.FreeTierOnly", "quota")

    router = SupervisorModelRouter(_config(), requester=requester, sleeper=lambda _: None)
    with pytest.raises(ModelAdapterError) as captured:
        _generate(router)

    assert captured.value.code == "SUPERVISOR_ROUTES_EXHAUSTED"
    assert len(router.exhausted_routes) == 6
    restored = SupervisorModelRouter(_config(), requester=requester, sleeper=lambda _: None)
    restored.restore_state(router.to_state_dict())
    assert restored.exhausted_routes == router.exhausted_routes


def test_transient_throttle_retries_then_fails_over_without_persisting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_keys(monkeypatch)
    calls: list[tuple[str, str]] = []

    def requester(base_url, route, api_key, messages, config):  # type: ignore[no-untyped-def]
        del base_url, api_key, messages, config
        calls.append((route.model_id, route.api_key_env))
        if route.api_key_env == _KEY_ENVS[0]:
            raise DashScopeRequestError(429, "Throttling.RateQuota", "slow down")
        return DashScopeHTTPResponse("ok", 1, 1)

    router = SupervisorModelRouter(_config(retries=1), requester=requester, sleeper=lambda _: None)
    _generate(router)

    assert calls == [
        ("max", _KEY_ENVS[0]),
        ("max", _KEY_ENVS[0]),
        ("max", _KEY_ENVS[1]),
    ]
    assert router.to_state_dict() == {"exhausted_routes": []}


def test_router_honors_one_deadline_across_internal_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_keys(monkeypatch)

    def requester(base_url, route, api_key, messages, config):  # type: ignore[no-untyped-def]
        del base_url, route, api_key, messages, config
        time.sleep(0.01)
        raise DashScopeRequestError(429, "Throttling.RateQuota", "slow down")

    router = SupervisorModelRouter(_config(retries=1), requester=requester, sleeper=lambda _: None)
    with pytest.raises(ModelAdapterError) as captured:
        router.generate(
            [Message("user", "decide")],
            config=GenerationConfig(timeout_seconds=0.001, max_retries=0),
        )

    assert captured.value.code == "MODEL_TIMEOUT"
    assert router.exhausted_routes == ()


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "InvalidApiKey"), (400, "InvalidParameter")],
)
def test_auth_and_model_errors_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    code: str,
) -> None:
    _set_keys(monkeypatch)
    calls = 0

    def requester(base_url, route, api_key, messages, config):  # type: ignore[no-untyped-def]
        nonlocal calls
        del base_url, route, api_key, messages, config
        calls += 1
        raise DashScopeRequestError(status, code, "invalid request")

    router = SupervisorModelRouter(_config(), requester=requester, sleeper=lambda _: None)
    with pytest.raises(ModelAdapterError) as captured:
        _generate(router)

    assert captured.value.code == "SUPERVISOR_CONFIGURATION_ERROR"
    assert calls == 1
    assert router.exhausted_routes == ()


def test_secret_file_loader_requires_private_permissions_and_respects_process_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env.supervisor"
    env_file.write_text(
        "export DASHSCOPE_TEST_KEY_1=file-one\nDASHSCOPE_TEST_KEY_2='file-two'\n",
        encoding="utf-8",
    )
    env_file.chmod(0o644)
    for name in _KEY_ENVS:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(PermissionError):
        load_env_file(env_file, required_names=_KEY_ENVS)

    env_file.chmod(0o600)
    monkeypatch.setenv(_KEY_ENVS[0], "explicit-one")
    loaded = load_env_file(env_file, required_names=_KEY_ENVS)

    assert loaded == {
        _KEY_ENVS[0]: "explicit-one",
        _KEY_ENVS[1]: "file-two",
    }
    assert os.environ[_KEY_ENVS[0]] == "explicit-one"


def test_secret_file_loader_rejects_symbolic_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "credentials"
    target.write_text(
        "DASHSCOPE_TEST_KEY_1=one\nDASHSCOPE_TEST_KEY_2=two\n",
        encoding="utf-8",
    )
    target.chmod(0o600)
    link = tmp_path / ".env.supervisor"
    link.symlink_to(target)
    for name in _KEY_ENVS:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="symbolic link"):
        load_env_file(link, required_names=_KEY_ENVS)
