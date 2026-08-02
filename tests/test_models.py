from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from repo_pilot_mas.models import (
    FakeModelAdapter,
    GenerationConfig,
    LocalTransformersAdapter,
    Message,
    ModelAdapterError,
)

_SIMPLE_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def test_fake_model_repairs_one_invalid_structured_response(tmp_path: Path) -> None:
    adapter = FakeModelAdapter(
        ["not-json", {"answer": "fixed"}],
        raw_log_dir=tmp_path / "raw",
    )

    response = adapter.generate(
        [Message("system", "Return data"), Message("user", "Give an answer")],
        response_schema=_SIMPLE_SCHEMA,
        config=GenerationConfig(max_retries=1),
    )

    assert response.structured_output == {"answer": "fixed"}
    assert response.attempts == 2
    assert response.usage.input_tokens > 0
    assert response.usage.output_tokens > 0
    assert Path(response.raw_response_ref).is_file()
    assert len(list((tmp_path / "raw").glob("*.json"))) == 2


def test_fake_model_returns_structured_error_after_retry_limit() -> None:
    adapter = FakeModelAdapter(["bad", "still bad"])

    with pytest.raises(ModelAdapterError) as captured:
        adapter.generate(
            [Message("user", "Give an answer")],
            response_schema=_SIMPLE_SCHEMA,
            config=GenerationConfig(max_retries=1),
        )

    assert captured.value.code == "STRUCTURED_OUTPUT_ERROR"
    assert captured.value.attempts == 2
    assert captured.value.usage.total_tokens > 0


def test_fake_model_async_generation() -> None:
    adapter = FakeModelAdapter([{"answer": "async"}])

    response = asyncio.run(
        adapter.agenerate(
            [Message("user", "Give an answer")],
            response_schema=_SIMPLE_SCHEMA,
        )
    )

    assert response.structured_output == {"answer": "async"}


def test_local_adapter_close_drops_loaded_resources(tmp_path: Path) -> None:
    adapter = LocalTransformersAdapter(tmp_path)
    adapter._model = object()
    adapter._tokenizer = object()

    adapter.close()

    assert adapter._model is None
    assert adapter._tokenizer is None
