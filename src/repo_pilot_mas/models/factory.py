"""Construct the explicitly supported Phase 2 model providers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from repo_pilot_mas.models.base import ModelAdapter
from repo_pilot_mas.models.fake import FakeModelAdapter
from repo_pilot_mas.models.local_transformers import LocalTransformersAdapter


def create_model_adapter(
    config: Mapping[str, Any],
    *,
    raw_log_dir: str | Path,
) -> ModelAdapter:
    provider = str(config.get("provider", "")).strip()
    if provider == "local_transformers":
        return LocalTransformersAdapter(
            config["model_path"],
            device=str(config.get("device", "cuda:0")),
            dtype=str(config.get("dtype", "bfloat16")),
            raw_log_dir=raw_log_dir,
        )
    if provider == "fake":
        responses_path = Path(config["responses_path"]).expanduser().resolve(strict=True)
        responses = json.loads(responses_path.read_text(encoding="utf-8"))
        if not isinstance(responses, list):
            raise TypeError("fake responses file must contain one JSON array")
        return FakeModelAdapter(responses, raw_log_dir=raw_log_dir)
    raise ValueError(f"unsupported model provider: {provider}")
