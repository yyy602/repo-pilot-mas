"""阶段二：常驻运行模式。

WebTaskRunner 持有一个常驻的本地 Worker 模型池（只加载一次），
通过 submit() 在服务进程内串行执行受信 QuixBugs 单任务，产物写入
reports/web_runs/<run_id>/，与正式评测目录完全隔离。

本文件不修改任何 Engine/Supervisor/Worker 行为，只复用现有组件：
create_worker_model_pool / create_supervisor_model_adapter /
OrchestrationEngine / AsyncLangGraphRuntime / restore_engine_from_runtime_state。
"""

from __future__ import annotations

import asyncio
import gc
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repo_pilot_mas.agents import DYNAMIC_SUPERVISOR_PROMPT, SupervisorAgent, WorkerPool
from repo_pilot_mas.config import load_yaml
from repo_pilot_mas.evaluation import tree_digest
from repo_pilot_mas.evaluation_budget import effective_engine_budget
from repo_pilot_mas.models import create_supervisor_model_adapter, create_worker_model_pool
from repo_pilot_mas.orchestration import (
    AsyncLangGraphRuntime,
    EngineBudget,
    EngineStatus,
    LangGraphRuntime,
    OrchestrationEngine,
    ReactBudget,
    restore_engine_from_runtime_state,
)
from repo_pilot_mas.runtime import TraceWriter
from repo_pilot_mas.schemas import TaskSpec
from repo_pilot_mas.tasks.loader import load_task as load_task_manifest

_WEB_RUNS_ROOT = Path("reports/web_runs")
_FINISHED_STATUSES = frozenset({"done", "failed"})


class WebTaskRunner:
    """常驻 Worker 模型池 + 串行单任务执行器。

    task_executor 可注入用于测试；默认执行与正式评测一致的真实闭环。
    """

    def __init__(
        self,
        model_config: str | Path,
        supervisor_config: str | Path,
        runtime_config: str | Path,
        phase6_config: str | Path,
        *,
        web_runs_root: str | Path | None = None,
        task_executor: Callable[..., Any] | None = None,
        runtime_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model_config = Path(model_config).expanduser().resolve()
        self.supervisor_config = Path(supervisor_config).expanduser().resolve()
        self.runtime_config = Path(runtime_config).expanduser().resolve()
        self.phase6_config = Path(phase6_config).expanduser().resolve()
        self.web_runs_root = (
            Path(web_runs_root or _WEB_RUNS_ROOT).expanduser().resolve()
        )
        self.task_executor = task_executor
        # 测试注入：async factory(checkpoint_db, trace) -> LangGraph runtime。
        # 真实路径为 None 时使用默认的正式 Supervisor + WorkerPool runtime。
        self.runtime_factory = runtime_factory
        self._lock = threading.Lock()
        self._prepare_lock = threading.Lock()
        self._worker_models: tuple[Any, ...] = ()
        self._worker_generation: Any = None
        self._pool: WorkerPool | None = None
        self._active_run_id: str | None = None
        self._status: dict[str, dict[str, Any]] = {}
        # 阶段三审批：run_id -> {"event", "response", "payload"}
        self._approvals: dict[str, dict[str, Any]] = {}

    # ---- 公开接口 ----

    def submit(
        self,
        task: TaskSpec,
        *,
        run_id: str | None = None,
        require_human_approval: bool = False,
    ) -> str:
        run_id = run_id or f"web-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        run_root = self.web_runs_root / run_id
        try:
            run_root.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise ValueError(f"web run 已存在：{run_id}") from exc
        (run_root / "tasks" / task.task_id).mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "task_id": task.task_id,
            "evaluation_split": "web",
            "require_human_approval": bool(require_human_approval),
            "status": "queued",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        (run_root / "run.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._status[run_id] = {
            "run_id": run_id,
            "task_id": task.task_id,
            "status": "queued",
            "started_at": manifest["started_at"],
            "finished_at": None,
            "error": None,
            "trace_path": str(run_root / "tasks" / task.task_id / "trace.jsonl"),
            "approval_required": bool(require_human_approval),
        }
        thread = threading.Thread(
            target=self._execute,
            args=(task, run_id, run_root, bool(require_human_approval)),
            name=f"web-run-{run_id}",
            daemon=True,
        )
        thread.start()
        return run_id

    def pending_approval(self, run_id: str) -> dict[str, Any] | None:
        """返回挂起审批信息；无挂起或已响应时返回 None。"""

        with self._lock:
            record = self._approvals.get(run_id)
            if record is None or record["response"] is not None:
                return None
            payload = record.get("payload") or {}
            return {
                "run_id": run_id,
                "task_id": self._status.get(run_id, {}).get("task_id"),
                "decision_id": payload.get("decision_id"),
                "state_version": payload.get("state_version"),
                "ready_worker_ids": list(payload.get("ready_worker_ids", ())),
                "type": payload.get("type"),
            }

    def decide(self, run_id: str, *, approved: bool) -> bool:
        """批准/拒绝当前挂起审批；无挂起时返回 False。"""

        with self._lock:
            record = self._approvals.get(run_id)
            if record is None or record["response"] is not None:
                return False
            record["response"] = bool(approved)
            record["event"].set()
            return True

    def approvals_all(self) -> tuple[dict[str, Any], ...]:
        """返回全部挂起审批（供前端审批队列轮询）。"""

        with self._lock:
            run_ids = tuple(self._status.keys())
        pending: list[dict[str, Any]] = []
        for run_id in run_ids:
            item = self.pending_approval(run_id)
            if item is not None:
                pending.append(item)
        return tuple(pending)

    def status(self, run_id: str) -> dict[str, Any] | None:
        record = self._status.get(run_id)
        if record is None:
            return None
        return dict(record)

    def active_run_id(self) -> str | None:
        return self._active_run_id

    def close(self) -> None:
        """释放常驻模型池，供服务退出时调用。"""

        with self._lock:
            self._pool = None
            models = self._worker_models
            self._worker_models = ()
        for model in models:
            try:
                model.close()
            except Exception:  # noqa: BLE001,S110 - 单个模型释放失败不阻断
                pass
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            return

    # ---- 内部执行 ----

    def _execute(
        self,
        task: TaskSpec,
        run_id: str,
        run_root: Path,
        require_human_approval: bool,
    ) -> None:
        with self._lock:
            if self._active_run_id is not None:
                self._status[run_id]["status"] = "failed"
                self._status[run_id]["error"] = "任务串行执行中，请等待当前任务结束"
                self._mark_manifest(run_root, "failed")
                return
            self._active_run_id = run_id
        try:
            if self.task_executor is not None:
                self._set_status(run_id, "running")
                self.task_executor(task=task, run_id=run_id, run_root=run_root)
            elif require_human_approval:
                # 审批模式：同步 runtime（interrupt 在同步图中可用）
                if self.runtime_factory is None:
                    self._set_status(run_id, "preparing")
                    self._prepare_models()
                self._set_status(run_id, "running")
                self._run_approval_task(
                    task=task,
                    run_id=run_id,
                    run_root=run_root,
                )
            else:
                if self.runtime_factory is None:
                    self._set_status(run_id, "preparing")
                    self._prepare_models()
                self._set_status(run_id, "running")
                asyncio.run(
                    self._run_real_task(
                        task=task,
                        run_id=run_id,
                        run_root=run_root,
                    )
                )
            self._set_status(run_id, "done")
        except Exception as exc:  # noqa: BLE001 - 单任务失败必须结构化记录
            self._status[run_id]["error"] = f"{type(exc).__name__}: {exc}"
            self._set_status(run_id, "failed")
        finally:
            with self._lock:
                self._active_run_id = None
            with self._lock:
                self._approvals.pop(run_id, None)
            self._mark_manifest(run_root, self._status[run_id]["status"])

    def _set_status(self, run_id: str, status: str) -> None:
        record = self._status.get(run_id)
        if record is None:
            return
        record["status"] = status
        if status in _FINISHED_STATUSES:
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
        trace_path = record.get("trace_path")
        if trace_path and Path(trace_path).is_file():
            with Path(trace_path).open(encoding="utf-8") as stream:
                record["trace_lines"] = sum(1 for _ in stream)

    def _mark_manifest(self, run_root: Path, status: str) -> None:
        manifest_path = run_root / "run.json"
        if not manifest_path.is_file():
            return
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        manifest["status"] = status
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        # 保留提交时的 started_at 等原始字段（此前重写会丢失 started_at）
        manifest.setdefault("started_at", datetime.now(timezone.utc).isoformat())
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _prepare_models(self) -> None:
        with self._prepare_lock:
            if self._pool is not None:
                return
            worker_models, worker_generation = create_worker_model_pool(
                load_yaml(self.model_config),
                raw_log_dir=self.web_runs_root / ".initial_raw_model_responses" / "workers",
            )
            pool = WorkerPool(
                worker_models,
                workspace_root=self.web_runs_root / ".workspaces",
            )
            pool.prepare()
            with self._lock:
                self._worker_models = worker_models
                self._worker_generation = worker_generation
                self._pool = pool

    async def _make_runtime(
        self,
        *,
        task_root: Path,
        trace: TraceWriter,
        runtime_config: dict[str, Any],
    ) -> tuple[Any, Any]:
        """构建（或测试注入）LangGraph runtime 与其 Worker 池。

        审批恢复时重建同一 sqlite 的 runtime；返回 (runtime, pool)，
        pool 为 None 表示测试注入（无需清理）。
        """

        if self.runtime_factory is not None:
            runtime = await self.runtime_factory(
                checkpoint_db=task_root / "langgraph.sqlite3",
                trace=trace,
            )
            return runtime, None
        supervisor_values = _mapping(load_yaml(self.supervisor_config), "supervisor")
        supervisor_recovery = _mapping(supervisor_values, "recovery")
        router, supervisor_generation = create_supervisor_model_adapter(
            self.supervisor_config,
            raw_log_dir=task_root / "raw_model_responses" / "supervisor",
            shared_worker_models=self._worker_models,
        )
        router.raw_log_dir = task_root / "raw_model_responses" / "supervisor"
        pool = WorkerPool(
            self._worker_models,
            workspace_root=task_root / "workspaces",
            trace_writer=trace,
            react_budget=ReactBudget(**_mapping(load_yaml(self.phase6_config), "worker_react")),
            generation_config=self._worker_generation,
        )
        runtime = await AsyncLangGraphRuntime.create(
            supervisor=SupervisorAgent(
                router,
                generation_config=supervisor_generation,
                trace_writer=trace,
                system_prompt=DYNAMIC_SUPERVISOR_PROMPT,
                additional_schema_retries=int(
                    supervisor_recovery.get("additional_schema_retries", 1)
                ),
                safe_fallback_enabled=bool(
                    supervisor_recovery.get("safe_fallback_enabled", True)
                ),
            ),
            worker_executor=pool,
            checkpoint_db=task_root / "langgraph.sqlite3",
            trace_writer=trace,
            recursion_limit=int(
                _mapping(runtime_config, "langgraph").get("recursion_limit", 128)
            ),
        )
        return runtime, pool

    async def _run_real_task(
        self,
        *,
        task: TaskSpec,
        run_id: str,
        run_root: Path,
    ) -> None:
        """非审批模式：异步执行（与正式评测同路径）。"""

        task_root = run_root / "tasks" / task.task_id
        trace_path = task_root / "trace.jsonl"
        trace = TraceWriter(trace_path)
        phase6_config = load_yaml(self.phase6_config)
        runtime_config = load_yaml(self.runtime_config)
        orchestration = _mapping(runtime_config, "orchestration")
        limits = _mapping(phase6_config, "hybrid_limits")
        budget_values = effective_engine_budget(orchestration, limits)

        engine = OrchestrationEngine(
            task,
            budget=EngineBudget(**budget_values),
            trace_writer=trace,
        )
        thread_id = f"web-{run_id}"
        before = tree_digest(task.repository_path)
        started = time.perf_counter()
        runtime, pool = await self._make_runtime(
            task_root=task_root,
            trace=trace,
            runtime_config=runtime_config,
        )
        try:
            state = await runtime.arun(
                engine,
                thread_id=thread_id,
                require_human_approval=False,
            )
            restored = restore_engine_from_runtime_state(state)
        finally:
            await runtime.aclose()
            _discard_workspaces(pool, task)
        duration_ms = int((time.perf_counter() - started) * 1000)
        _write_web_result(
            task,
            restored,
            run_root=run_root,
            task_root=task_root,
            trace_path=trace_path,
            duration_ms=duration_ms,
            source_digest_before=before,
            source_digest_after=tree_digest(task.repository_path),
        )

    def _run_approval_task(
        self,
        *,
        task: TaskSpec,
        run_id: str,
        run_root: Path,
    ) -> None:
        """审批模式：同步 LangGraphRuntime（interrupt 在同步图中可用）。"""

        task_root = run_root / "tasks" / task.task_id
        trace_path = task_root / "trace.jsonl"
        trace = TraceWriter(trace_path)
        phase6_config = load_yaml(self.phase6_config)
        runtime_config = load_yaml(self.runtime_config)
        orchestration = _mapping(runtime_config, "orchestration")
        limits = _mapping(phase6_config, "hybrid_limits")
        budget_values = effective_engine_budget(orchestration, limits)

        engine = OrchestrationEngine(
            task,
            budget=EngineBudget(**budget_values),
            trace_writer=trace,
        )
        thread_id = f"web-{run_id}"
        before = tree_digest(task.repository_path)
        started = time.perf_counter()
        runtime, pool = self._make_runtime_sync(
            task_root=task_root,
            trace=trace,
            runtime_config=runtime_config,
        )
        try:
            state = runtime.run(
                engine,
                thread_id=thread_id,
                require_human_approval=True,
            )
            # 阶段三审批循环：任务在 human_review interrupt 处挂起，
            # 等待控制接口批准/拒绝后重建 runtime（同一 sqlite）并 resume。
            while _waiting_for_human_sync(runtime, thread_id):
                response = self._await_approval_sync(run_id, thread_id, runtime)
                runtime.close()
                runtime, pool = self._make_runtime_sync(
                    task_root=task_root,
                    trace=trace,
                    runtime_config=runtime_config,
                )
                state = runtime.resume(
                    thread_id=thread_id,
                    task_id=task.task_id,
                    human_response=response,
                )
            restored = restore_engine_from_runtime_state(state)
        finally:
            runtime.close()
            _discard_workspaces(pool, task)
        duration_ms = int((time.perf_counter() - started) * 1000)
        _write_web_result(
            task,
            restored,
            run_root=run_root,
            task_root=task_root,
            trace_path=trace_path,
            duration_ms=duration_ms,
            source_digest_before=before,
            source_digest_after=tree_digest(task.repository_path),
        )

    def _make_runtime_sync(
        self,
        *,
        task_root: Path,
        trace: TraceWriter,
        runtime_config: dict[str, Any],
    ) -> tuple[Any, Any]:
        if self.runtime_factory is not None:
            runtime = self.runtime_factory(
                checkpoint_db=task_root / "langgraph.sqlite3",
                trace=trace,
            )
            return runtime, None
        supervisor_values = _mapping(load_yaml(self.supervisor_config), "supervisor")
        supervisor_recovery = _mapping(supervisor_values, "recovery")
        router, supervisor_generation = create_supervisor_model_adapter(
            self.supervisor_config,
            raw_log_dir=task_root / "raw_model_responses" / "supervisor",
            shared_worker_models=self._worker_models,
        )
        router.raw_log_dir = task_root / "raw_model_responses" / "supervisor"
        pool = WorkerPool(
            self._worker_models,
            workspace_root=task_root / "workspaces",
            trace_writer=trace,
            react_budget=ReactBudget(**_mapping(load_yaml(self.phase6_config), "worker_react")),
            generation_config=self._worker_generation,
        )
        runtime = LangGraphRuntime(
            supervisor=SupervisorAgent(
                router,
                generation_config=supervisor_generation,
                trace_writer=trace,
                system_prompt=DYNAMIC_SUPERVISOR_PROMPT,
                additional_schema_retries=int(
                    supervisor_recovery.get("additional_schema_retries", 1)
                ),
                safe_fallback_enabled=bool(
                    supervisor_recovery.get("safe_fallback_enabled", True)
                ),
            ),
            worker_executor=pool,
            checkpoint_db=task_root / "langgraph.sqlite3",
            trace_writer=trace,
            recursion_limit=int(
                _mapping(runtime_config, "langgraph").get("recursion_limit", 128)
            ),
        )
        return runtime, pool

    def _await_approval_sync(
        self,
        run_id: str,
        thread_id: str,
        runtime: Any,
    ) -> bool:
        record = {"event": threading.Event(), "response": None, "payload": None}
        snapshot = runtime.graph.get_state(
            {"configurable": {"thread_id": thread_id}}
        )
        record["payload"] = _approval_payload(snapshot)
        with self._lock:
            self._approvals[run_id] = record
        record["event"].wait()
        with self._lock:
            response = record["response"]
        if response is None:
            raise RuntimeError("审批被异常终止，无响应")
        return bool(response)


def _discard_workspaces(pool: Any, task: TaskSpec) -> None:
    if pool is None:
        return
    try:
        pool.discard_task_workspaces(
            task=task.to_dict(),
            reason="web_task_finalizer",
        )
    except Exception:  # noqa: BLE001,S110 - 清理失败不影响结果
        pass


def _waiting_for_human_sync(runtime: Any, thread_id: str) -> bool:
    snapshot = runtime.graph.get_state(
        {"configurable": {"thread_id": thread_id}}
    )
    return "human_review" in (snapshot.next or ())


def _approval_payload(snapshot: Any) -> dict[str, Any]:
    interrupts = snapshot.tasks[0].interrupts if snapshot.tasks else ()
    payload: dict[str, Any] = {}
    if interrupts:
        payload = dict(interrupts[0].value)
    last_event = (snapshot.values or {}).get("last_event") or {}
    event_data = last_event.get("data") if isinstance(last_event, Mapping) else {}
    decision = event_data.get("decision") if isinstance(event_data, Mapping) else None
    if not isinstance(decision, Mapping):
        decision = (
            event_data.get("decision_result")
            if isinstance(event_data, Mapping)
            else None
        )
    if isinstance(decision, Mapping):
        payload["decision"] = {
            "decision_id": decision.get("decision_id"),
            "action": decision.get("action"),
            "reason": str(decision.get("reason", ""))[:300],
        }
    return payload


def _mapping(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    selected = value.get(key, {})
    if not isinstance(selected, Mapping):
        raise TypeError(f"配置段必须是 mapping：{key}")
    return dict(selected)


def _write_web_result(
    task: TaskSpec,
    restored: Any,
    *,
    run_root: Path,
    task_root: Path,
    trace_path: Path,
    duration_ms: int,
    source_digest_before: str,
    source_digest_after: str,
) -> None:
    """以与正式 result.json 兼容的精简 Schema 落盘 web 运行结果。"""

    artifacts = [item.to_dict() for item in restored.blackboard.artifacts.values()]
    nodes = [item.to_dict() for item in restored.graph.nodes]
    source_unchanged = source_digest_before == source_digest_after
    succeeded = restored.status is EngineStatus.SUCCEEDED and source_unchanged
    termination_code = restored.termination_code or "UNKNOWN_TERMINATION"
    if not source_unchanged:
        termination_code = "SOURCE_REPOSITORY_MUTATED"
    failure_class = _web_failure_class(
        engine_status=restored.status.value,
        termination_code=termination_code,
        artifacts=artifacts,
    )
    result_path = task_root / "result.json"
    result = {
        "schema_version": 1,
        "protocol_version": "web_v1",
        "system_id": "closed_loop_dynamic",
        "task_id": task.task_id,
        "seed": 0,
        "status": "succeeded" if succeeded else "failed",
        "reason": restored.termination_reason or restored.status.value,
        "termination": {
            "code": termination_code,
            "stage": restored.termination_stage or restored.blackboard.workflow_stage,
            "message": restored.termination_reason or restored.status.value,
            "failure_class": failure_class,
        },
        "engine_status": restored.status.value,
        "workflow_stage": restored.blackboard.workflow_stage,
        "hypothesis_resolution": restored.blackboard.hypothesis_resolution.to_dict(),
        "selected_hypothesis_ref": restored.blackboard.selected_hypothesis_ref,
        "selected_patch_ref": restored.blackboard.selected_patch_ref,
        "validation_ref": restored.blackboard.validation_ref,
        "validation": _web_validation_metrics(artifacts),
        "usage": {
            "duration_ms": duration_ms,
            "model_calls": sum(
                e.get("event_type") == "model_call"
                for e in _read_trace(trace_path)
            ),
        },
        "nodes": nodes,
        "artifacts": artifacts,
        "source_integrity": {
            "before_sha256": source_digest_before,
            "after_sha256": source_digest_after,
            "unchanged": source_unchanged,
        },
        "trace_path": str(trace_path),
        "checkpoint_path": str(task_root / "langgraph.sqlite3"),
        "result_path": str(result_path),
    }
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # 合并保留 submit 时的元数据（started_at、require_human_approval），再写终态
    final_manifest: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_root.name,
        "task_id": task.task_id,
        "evaluation_split": "web",
        "status": result["status"],
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "repository_commit": None,
    }
    existing_path = run_root / "run.json"
    if existing_path.is_file():
        try:
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
            if isinstance(existing, Mapping):
                for key in ("started_at", "require_human_approval"):
                    if existing.get(key) is not None:
                        final_manifest[key] = existing[key]
        except (OSError, ValueError):
            pass
    existing_path.write_text(
        json.dumps(final_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_trace(trace_path: Path) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    if not trace_path.is_file():
        return ()
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            events.append(dict(value))
    return tuple(events)


def _web_validation_metrics(artifacts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    validations = [
        item
        for item in artifacts
        if item.get("artifact_type") == "validation_result"
    ]
    return {
        "patch_applied": any(
            bool(item.get("content", {}).get("applied")) for item in validations
        ),
        "target_test_passed": any(
            (item.get("content", {}).get("target_test") or {}).get("exit_code") == 0
            for item in validations
        ),
        "regression_passed": any(
            (item.get("content", {}).get("regression_test") or {}).get("exit_code") == 0
            for item in validations
        ),
        "syntax_valid": any(
            (item.get("content", {}).get("static_check") or {}).get("exit_code") == 0
            for item in validations
        ),
        "protected_path_violations": sum(
            item.get("content", {}).get("protected_path_check") is not True
            for item in validations
        ),
        "validation_count": len(validations),
        "passed_validation_count": sum(
            bool(item.get("content", {}).get("passed")) for item in validations
        ),
        "changed_files": sorted(
            {
                str(path)
                for item in validations
                for path in item.get("content", {}).get("changed_files", ())
            }
        ),
    }


def _web_failure_class(
    *,
    engine_status: str,
    termination_code: str,
    artifacts: Sequence[Mapping[str, Any]],
) -> str:
    if engine_status == EngineStatus.SUCCEEDED.value:
        return "none"
    if termination_code.endswith("BUDGET_EXHAUSTED") or termination_code == "BUDGET_VIOLATION":
        return "budget_exhausted"
    if termination_code == "SUPERVISOR_ROUTES_EXHAUSTED":
        return "provider_quota_exhausted"
    for item in reversed(artifacts):
        if item.get("artifact_type") not in {"validation_result", "replan_record"}:
            continue
        content = item.get("content", {})
        failure_class = (
            str(content.get("failure_class", ""))
            if isinstance(content, Mapping)
            else ""
        )
        if failure_class and failure_class != "none":
            return failure_class
    if termination_code == "SUPERVISOR_TERMINATED":
        return "task_terminated"
    return "orchestration_failure"


def available_web_tasks(tasks_root: str | Path | None = None) -> tuple[TaskSpec, ...]:
    """返回受信 QuixBugs 任务清单（供界面下拉选择）。"""

    root = Path(tasks_root or "data/quixbugs/tasks").expanduser().resolve()
    if not root.is_dir():
        return ()
    tasks: list[TaskSpec] = []
    for path in sorted(root.glob("*.json")):
        try:
            tasks.append(load_task_manifest(path))
        except (OSError, ValueError, TypeError):
            continue
    return tuple(tasks)
