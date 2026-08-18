"""Read-only FastAPI service + 阶段二运行接口 for the Phase 7 dashboard.

阶段一：只读接口，暴露 reports/ 产物；
阶段二：可选注入 WebTaskRunner 后提供 POST /runs（提交受信任务）与
GET /runs/{id}/status（进度轮询）。运行产物写入 reports/web_runs/。
控制接口默认关闭；运行接口仅在显式传入 runner 时启用。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from repo_pilot_mas.tasks.loader import load_task as load_task_manifest
from repo_pilot_mas.visualization.parsers import (
    TaskView,
    build_decision_events,
    build_model_call_events,
    build_node_timeline,
    discover_runs,
    load_run,
    load_task,
    rejection_summary,
)
from repo_pilot_mas.visualization.runner import WebTaskRunner


class SubmitRunRequest(BaseModel):
    task_id: str
    run_id: str | None = None
    require_human_approval: bool = False


class ApprovalRequest(BaseModel):
    approved: bool


def build_app(
    evaluation_root: str | Path | None = None,
    *,
    demo_root: str | Path | None = None,
    web_root: str | Path | None = None,
    runner: WebTaskRunner | None = None,
    lifespan: Any | None = None,
    cors_origins: Sequence[str] = ("http://localhost:8501",),
) -> FastAPI:
    """Build the dashboard API (read-only + optional stage-2 run endpoints)."""

    app = FastAPI(
        title="RepoPilot-MAS Dashboard",
        description="阶段一/二：只读驾驶舱 + 常驻运行模式",
        version="0.2.0",
        lifespan=lifespan,
    )
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/runs")
    def runs() -> dict[str, Any]:
        return {
            "runs": [
                run.to_dict()
                for run in discover_runs(
                    evaluation_root, demo_root=demo_root, web_root=web_root
                )
            ]
        }

    @app.get("/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, Any]:
        try:
            run = load_run(run_id, evaluation_root, web_root=web_root)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        payload = run.to_dict()
        payload["summary"] = run.summary
        payload["acceptance"] = run.acceptance
        payload["gates"] = run.acceptance.get("gates") if run.acceptance else None
        payload["termination_codes"] = (
            run.summary.get("termination_codes") if run.summary else None
        )
        payload["termination_failure_classes"] = (
            run.summary.get("termination_failure_classes")
            if run.summary
            else None
        )
        payload["route_attempts_by_model"] = (
            run.summary.get("route_attempts_by_model") if run.summary else None
        )
        return payload

    @app.get("/runs/{run_id}/tasks/{task_id}")
    def task_detail(run_id: str, task_id: str) -> dict[str, Any]:
        try:
            task: TaskView = load_task(
                run_id, task_id, evaluation_root, web_root=web_root
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        payload = task.to_dict()
        payload["node_timeline"] = build_node_timeline(task.events)
        payload["decisions"] = [
            dict(event) for event in build_decision_events(task.events)
        ]
        payload["model_calls"] = [
            dict(event) for event in build_model_call_events(task.events)
        ]
        payload["rejections"] = list(rejection_summary(task.artifacts))
        return payload

    @app.get("/runs/{run_id}/compare")
    def compare(run_id: str) -> dict[str, Any]:
        try:
            run = load_run(run_id, evaluation_root, web_root=web_root)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        rows: list[dict[str, Any]] = []
        for task_id in run.task_ids:
            try:
                task = load_task(
                    run_id, task_id, evaluation_root, web_root=web_root
                )
            except KeyError:
                continue
            rows.append(
                {
                    "task_id": task_id,
                    "status": task.status,
                    "failure_class": task.termination.get("failure_class"),
                    "termination_code": task.termination.get("code"),
                    "stage": task.termination.get("stage"),
                    "message": str(task.termination.get("message", ""))[:200],
                    "validation": (task.result or {}).get("validation"),
                    "usage": (task.result or {}).get("usage"),
                }
            )
        return {"run_id": run_id, "tasks": rows}

    # ---- 阶段二：常驻运行模式（仅注入 runner 时启用） ----

    @app.get("/runs/{run_id}/status")
    def run_status(run_id: str) -> dict[str, Any]:
        if runner is None:
            raise HTTPException(status_code=503, detail="运行模式未启用")
        record = runner.status(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"未知 web run：{run_id}")
        return record

    @app.post("/runs")
    def submit_run(request: SubmitRunRequest) -> dict[str, Any]:
        if runner is None:
            raise HTTPException(status_code=503, detail="运行模式未启用")
        task_path = (
            Path("data/quixbugs/tasks") / f"{request.task_id}.json"
        )
        if not task_path.is_file():
            raise HTTPException(status_code=400, detail=f"未知任务：{request.task_id}")
        try:
            task = load_task_manifest(task_path)
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            run_id = runner.submit(
                task,
                run_id=request.run_id,
                require_human_approval=request.require_human_approval,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"run_id": run_id, "status": runner.status(run_id)}

    # ---- 阶段三：人工审批控制面（仅注入 runner 时启用） ----

    @app.get("/approvals")
    def approvals() -> dict[str, Any]:
        if runner is None:
            raise HTTPException(status_code=503, detail="运行模式未启用")
        return {"approvals": [dict(item) for item in runner.approvals_all()]}

    @app.get("/runs/{run_id}/pending")
    def run_pending(run_id: str) -> dict[str, Any]:
        if runner is None:
            raise HTTPException(status_code=503, detail="运行模式未启用")
        item = runner.pending_approval(run_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"无挂起审批：{run_id}")
        return item

    @app.post("/runs/{run_id}/approve")
    def run_approve(run_id: str, request: ApprovalRequest) -> dict[str, Any]:
        if runner is None:
            raise HTTPException(status_code=503, detail="运行模式未启用")
        applied = runner.decide(run_id, approved=request.approved)
        if not applied:
            raise HTTPException(status_code=409, detail=f"无挂起审批：{run_id}")
        return {"run_id": run_id, "approved": request.approved}

    return app


app = build_app()
