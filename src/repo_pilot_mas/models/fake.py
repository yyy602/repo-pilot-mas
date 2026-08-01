"""Deterministic model adapter for unit and integration tests."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from repo_pilot_mas.models.base import (
    GenerationConfig,
    Message,
    ModelAdapter,
    ModelAdapterError,
    RawGeneration,
)


class FakeModelAdapter(ModelAdapter):
    def __init__(
        self,
        responses: Sequence[str | Mapping[str, Any]],
        *,
        model_id: str = "fake-model",
        raw_log_dir: str | Path | None = None,
    ) -> None:
        super().__init__(model_id, raw_log_dir=raw_log_dir)
        self._responses = list(responses)
        self.calls = 0

    def _generate_once(
        self,
        messages: Sequence[Message],
        config: GenerationConfig,
    ) -> RawGeneration:
        del config
        if self.calls >= len(self._responses):
            raise ModelAdapterError(
                "FAKE_RESPONSES_EXHAUSTED",
                "FakeModelAdapter has no remaining scripted responses",
                attempts=self.calls,
            )
        response = self._responses[self.calls]
        self.calls += 1
        text = (
            json.dumps(response, ensure_ascii=False)
            if isinstance(response, Mapping)
            else str(response)
        )
        input_text = "\n".join(message.content for message in messages)
        return RawGeneration(
            text=text,
            input_tokens=max(len(input_text) // 4, 1),
            output_tokens=max(len(text) // 4, 1),
        )
