"""Budget resolution shared by closed-loop runners and reports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_EVALUATION_TO_ENGINE_KEYS = {
    "max_supervisor_api_calls": "max_supervisor_calls",
    "max_tool_calls": "max_tool_calls",
    "max_input_tokens": "max_input_tokens",
    "max_output_tokens": "max_output_tokens",
    "max_runtime_seconds": "max_runtime_seconds",
}


def effective_engine_budget(
    orchestration_defaults: Mapping[str, Any],
    evaluation_limits: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve the exact EngineBudget values used by final evaluation.

    Structural orchestration limits such as node concurrency and replan counts
    come from the runtime configuration. Resource limits come from the frozen
    evaluation protocol so execution and reporting cannot silently use
    different token, tool, call, or runtime ceilings.
    """

    resolved = dict(orchestration_defaults)
    for evaluation_key, engine_key in _EVALUATION_TO_ENGINE_KEYS.items():
        if evaluation_key not in evaluation_limits:
            raise KeyError(
                f"evaluation limits are missing required key: {evaluation_key}"
            )
        resolved[engine_key] = evaluation_limits[evaluation_key]
    return resolved


def effective_engine_limit_view(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return only immutable EngineBudget limit fields for reports and traces."""

    keys = (
        "max_nodes",
        "max_concurrent_nodes",
        "max_supervisor_calls",
        "max_tool_calls",
        "max_input_tokens",
        "max_output_tokens",
        "max_runtime_seconds",
        "max_replans",
        "max_retries_per_node",
        "max_no_progress_decisions",
    )
    return {key: values[key] for key in keys}
