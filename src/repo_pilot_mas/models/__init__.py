"""Model adapters exposed by RepoPilot-MAS."""

from repo_pilot_mas.models.base import (
    GenerationConfig,
    Message,
    ModelAdapter,
    ModelAdapterError,
    ModelResponse,
    TokenUsage,
)
from repo_pilot_mas.models.factory import create_model_adapter
from repo_pilot_mas.models.fake import FakeModelAdapter
from repo_pilot_mas.models.local_transformers import LocalTransformersAdapter

__all__ = [
    "FakeModelAdapter",
    "GenerationConfig",
    "LocalTransformersAdapter",
    "Message",
    "ModelAdapter",
    "ModelAdapterError",
    "ModelResponse",
    "TokenUsage",
    "create_model_adapter",
]
