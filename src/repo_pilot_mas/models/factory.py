"""Construct the explicitly supported Phase 2 model providers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from repo_pilot_mas.config import load_yaml
from repo_pilot_mas.models.base import GenerationConfig, ModelAdapter
from repo_pilot_mas.models.dashscope import create_supervisor_model_router
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


def create_worker_model_pool(
    config: Mapping[str, Any],
    *,
    raw_log_dir: str | Path,
) -> tuple[tuple[ModelAdapter, ...], GenerationConfig]:
    """Create the configured independent local Worker model slots."""

    raw_models = config.get("worker_models")
    if not isinstance(raw_models, list) or not raw_models:
        fallback = config.get("model")
        if not isinstance(fallback, Mapping):
            raise TypeError("model configuration requires worker_models or model")
        raw_models = [fallback]
    models: list[ModelAdapter] = []
    for index, item in enumerate(raw_models):
        if not isinstance(item, Mapping):
            raise TypeError("each worker_models entry must be a mapping")
        models.append(
            create_model_adapter(
                item,
                raw_log_dir=Path(raw_log_dir) / f"slot-{index}",
            )
        )
    raw_generation = config.get("generation", {})
    if not isinstance(raw_generation, Mapping):
        raise TypeError("generation configuration must be a mapping")
    generation = GenerationConfig(
        temperature=float(raw_generation.get("temperature", 0.0)),
        max_output_tokens=int(raw_generation.get("max_output_tokens", 1024)),
        stop=tuple(str(item) for item in raw_generation.get("stop", ())),
        timeout_seconds=float(raw_generation.get("timeout_seconds", 180.0)),
        max_retries=int(raw_generation.get("max_retries", 1)),
    )
    return tuple(models), generation


def create_supervisor_model_adapter(
    config_path: str | Path,
    *,
    raw_log_dir: str | Path,
    trace_writer: Any | None = None,
    shared_worker_models: tuple[ModelAdapter, ...] = (),
) -> tuple[ModelAdapter, GenerationConfig]:
    """Create the explicitly configured API or local-debug Supervisor model."""

    path = Path(config_path).expanduser().resolve(strict=True)
    values = load_yaml(path)
    supervisor = values.get("supervisor")
    if not isinstance(supervisor, Mapping):
        raise TypeError("supervisor configuration must be a mapping")
    provider = str(supervisor.get("provider", "")).strip()
    if provider == "dashscope_openai_compatible":
        return create_supervisor_model_router(
            path,
            raw_log_dir=raw_log_dir,
            trace_writer=trace_writer,
        )
    if provider != "local_transformers":
        raise ValueError(f"unsupported supervisor provider: {provider}")

    generation_values = supervisor.get("generation", {})
    if not isinstance(generation_values, Mapping):
        raise TypeError("supervisor generation configuration must be a mapping")
    generation = GenerationConfig(
        temperature=float(generation_values.get("temperature", 0.0)),
        max_output_tokens=int(generation_values.get("max_output_tokens", 2048)),
        timeout_seconds=float(generation_values.get("timeout_seconds", 180.0)),
        max_retries=int(generation_values.get("max_format_repairs", 1)),
        enable_thinking=generation_values.get("enable_thinking"),
    )

    reuse_slot = supervisor.get("reuse_worker_slot")
    if reuse_slot is None:
        return create_model_adapter(supervisor, raw_log_dir=raw_log_dir), generation
    slot = int(reuse_slot)
    if slot < 0 or slot >= len(shared_worker_models):
        raise ValueError("reuse_worker_slot does not reference a Worker model")
    model = shared_worker_models[slot]
    if not isinstance(model, LocalTransformersAdapter):
        raise TypeError("local Supervisor may only reuse a local Transformers Worker")
    configured_path = Path(str(supervisor["model_path"])).expanduser().resolve(strict=True)
    if (
        model.model_path != configured_path
        or model.device != str(supervisor.get("device", "cuda:0"))
        or model.dtype != str(supervisor.get("dtype", "bfloat16"))
    ):
        raise ValueError("local Supervisor and reused Worker model configurations differ")
    return model, generation
