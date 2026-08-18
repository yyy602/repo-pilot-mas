"""阶段二服务入口：常驻运行模式（FastAPI + WebTaskRunner）。

启动：
    python -m repo_pilot_mas.visualization.server

服务启动后通过 POST /runs 提交受信任务、GET /runs/{id}/status 轮询进度；
关闭时释放常驻 Worker 模型池。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from repo_pilot_mas.visualization.runner import WebTaskRunner
from repo_pilot_mas.visualization.service import build_app


def create_app(
    *,
    model_config: str = "configs/model.yaml",
    supervisor_config: str = "configs/supervisor.yaml",
    runtime_config: str = "configs/runtime.yaml",
    phase6_config: str = "configs/phase6.yaml",
) -> FastAPI:
    runner = WebTaskRunner(
        model_config=model_config,
        supervisor_config=supervisor_config,
        runtime_config=runtime_config,
        phase6_config=phase6_config,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        runner.close()

    return build_app(runner=runner, lifespan=lifespan)


def main() -> None:
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
