"""Model adapters exposed by RepoPilot-MAS."""

from repo_pilot_mas.models.base import (
    GenerationConfig,
    Message,
    ModelAdapter,
    ModelAdapterError,
    ModelResponse,
    TokenUsage,
)
from repo_pilot_mas.models.dashscope import (
    DashScopeHTTPResponse,
    DashScopeRequestError,
    DashScopeRouterConfig,
    SupervisorModelRouter,
    SupervisorRoute,
    create_supervisor_model_router,
)
from repo_pilot_mas.models.factory import (
    create_model_adapter,
    create_supervisor_model_adapter,
    create_worker_model_pool,
)
from repo_pilot_mas.models.fake import FakeModelAdapter
from repo_pilot_mas.models.local_transformers import LocalTransformersAdapter

__all__ = [
    "DashScopeHTTPResponse",
    "DashScopeRequestError",
    "DashScopeRouterConfig",
    "FakeModelAdapter",
    "GenerationConfig",
    "LocalTransformersAdapter",
    "Message",
    "ModelAdapter",
    "ModelAdapterError",
    "ModelResponse",
    "SupervisorModelRouter",
    "SupervisorRoute",
    "TokenUsage",
    "create_model_adapter",
    "create_supervisor_model_adapter",
    "create_supervisor_model_router",
    "create_worker_model_pool",
]
