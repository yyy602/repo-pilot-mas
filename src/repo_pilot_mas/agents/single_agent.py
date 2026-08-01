"""Single-Agent code repair baseline with deterministic final validation."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from repo_pilot_mas.models import GenerationConfig, Message, ModelAdapter
from repo_pilot_mas.orchestration import ReactBudget, ReactLoop, ReactResult
from repo_pilot_mas.runtime import TraceWriter, Workspace, WorkspaceManager
from repo_pilot_mas.schemas import FinalReport, TaskSpec, ToolResult
from repo_pilot_mas.schemas.tool_result import utc_now_iso
from repo_pilot_mas.tools import (
    build_workspace_tool_registry,
    collect_diff,
    rollback_workspace,
    run_tests,
    static_check,
)
from repo_pilot_mas.tools.registry import task_test_command


class SingleAgent:
    def __init__(
        self,
        model: ModelAdapter,
        *,
        workspace_root: str | Path,
        report_root: str | Path,
        budget: ReactBudget | None = None,
        generation_config: GenerationConfig | None = None,
    ) -> None:
        self.model = model
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.report_root = Path(report_root).expanduser().resolve(strict=False)
        self.budget = budget or ReactBudget()
        self.generation_config = generation_config or GenerationConfig()

    def run(self, task: TaskSpec) -> FinalReport:
        started_perf = time.perf_counter()
        started_at = utc_now_iso()
        run_id = uuid4().hex[:12]
        run_root = self.report_root / task.task_id / run_id
        trace_path = run_root / "trace.jsonl"
        report_path = run_root / "final_report.json"
        trace = TraceWriter(trace_path)
        trace.write(
            "task_started",
            {
                "task": task.to_dict(),
                "model_id": self.model.model_id,
                "run_id": run_id,
            },
        )

        manager = WorkspaceManager(task.repository_path, self.workspace_root)
        workspace = manager.create(task.task_id, run_id)
        registry = build_workspace_tool_registry(task, workspace)
        tool_calls = 0
        validations: dict[str, object] = {}

        initial_test = run_tests(
            workspace.root,
            command=task_test_command(task, target=True),
            timeout_seconds=task.max_runtime_seconds,
        )
        tool_calls += 1
        validations["initial_target_test"] = initial_test.to_dict()
        _trace_tool(trace, "initial_validation", initial_test)

        reproducible_failure_codes = {"TEST_FAILED", "TEST_TIMEOUT"}
        if (
            initial_test.ok
            or initial_test.error is None
            or initial_test.error.code not in reproducible_failure_codes
        ):
            reason = (
                "TARGET_TEST_ALREADY_PASSES"
                if initial_test.ok
                else f"INITIAL_TEST_UNUSABLE:{initial_test.error.code if initial_test.error else 'UNKNOWN'}"
            )
            cleanup = _cleanup_workspace(manager, workspace, trace)
            tool_calls += 1
            validations["cleanup"] = cleanup.to_dict()
            return self._save_report(
                task=task,
                workspace=workspace,
                report_path=report_path,
                trace_path=trace_path,
                started_at=started_at,
                started_perf=started_perf,
                status="failed",
                reason=reason,
                validations=validations,
                react=None,
                tool_calls=tool_calls,
            )

        loop = ReactLoop(
            self.model,
            registry,
            budget=self.budget,
            generation_config=self.generation_config,
            trace_writer=trace,
        )
        react = loop.run(_initial_messages(task, registry.definitions_for_model(), initial_test))
        tool_calls += react.tool_calls

        diff_result = collect_diff(workspace, protected_paths=task.protected_paths)
        tool_calls += 1
        validations["diff"] = diff_result.to_dict()
        _trace_tool(trace, "final_validation", diff_result)
        changed_files = tuple(
            str(item["path"]) for item in diff_result.data.get("files", ())
        )
        diff_text = str(diff_result.data.get("diff", ""))
        patch_sha256 = hashlib.sha256(diff_text.encode()).hexdigest() if diff_text else None

        target_test: ToolResult | None = None
        regression_test: ToolResult | None = None
        static_result: ToolResult | None = None
        patch_applied = "apply_patch" in react.successful_tools
        if diff_result.ok and changed_files and patch_applied:
            target_test = run_tests(
                workspace.root,
                command=task_test_command(task, target=True),
                timeout_seconds=task.max_runtime_seconds,
            )
            regression_test = run_tests(
                workspace.root,
                command=task_test_command(task, target=False),
                timeout_seconds=task.max_runtime_seconds,
            )
            static_result = static_check(
                workspace.root,
                timeout_seconds=task.max_runtime_seconds,
            )
            for label, result in (
                ("target_test", target_test),
                ("regression_test", regression_test),
                ("static_check", static_result),
            ):
                tool_calls += 1
                validations[label] = result.to_dict()
                _trace_tool(trace, "final_validation", result)

        passed = bool(
            react.status == "completed"
            and patch_applied
            and diff_result.ok
            and changed_files
            and target_test is not None
            and target_test.ok
            and regression_test is not None
            and regression_test.ok
            and static_result is not None
            and static_result.ok
        )
        reason = _final_reason(
            react,
            patch_applied=patch_applied,
            diff_result=diff_result,
            changed_files=changed_files,
            target_test=target_test,
            regression_test=regression_test,
            static_result=static_result,
        )

        cleanup = _cleanup_workspace(manager, workspace, trace)
        tool_calls += 1
        validations["cleanup"] = cleanup.to_dict()
        if not cleanup.ok:
            passed = False
            reason = "WORKSPACE_CLEANUP_FAILED"

        return self._save_report(
            task=task,
            workspace=workspace,
            report_path=report_path,
            trace_path=trace_path,
            started_at=started_at,
            started_perf=started_perf,
            status="succeeded" if passed else "failed",
            reason=reason,
            validations=validations,
            react=react,
            tool_calls=tool_calls,
            patch_sha256=patch_sha256,
            changed_files=changed_files,
        )

    def _save_report(
        self,
        *,
        task: TaskSpec,
        workspace: Workspace,
        report_path: Path,
        trace_path: Path,
        started_at: str,
        started_perf: float,
        status: str,
        reason: str,
        validations: dict[str, object],
        react: ReactResult | None,
        tool_calls: int,
        patch_sha256: str | None = None,
        changed_files: tuple[str, ...] = (),
    ) -> FinalReport:
        report = FinalReport(
            task_id=task.task_id,
            status=status,
            reason=reason,
            model_id=self.model.model_id,
            workspace_id=workspace.workspace_id,
            patch_sha256=patch_sha256,
            changed_files=changed_files,
            validations=validations,
            model_calls=react.model_calls if react else 0,
            tool_calls=tool_calls,
            input_tokens=react.input_tokens if react else 0,
            output_tokens=react.output_tokens if react else 0,
            duration_ms=int((time.perf_counter() - started_perf) * 1000),
            trace_path=str(trace_path),
            started_at=started_at,
            finished_at=utc_now_iso(),
            report_path=str(report_path),
            metadata={"dataset": task.metadata.get("dataset")},
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        TraceWriter(trace_path).write("final_report", report.to_dict())
        return report


def _initial_messages(
    task: TaskSpec,
    tool_definitions: list[dict[str, object]],
    initial_test: ToolResult,
) -> tuple[Message, ...]:
    system = (
        "你是 RepoPilot-MAS 的 Single-Agent 代码修复基线。只通过给定工具调查和修改候选工作区。"
        "不得修改 protected_paths，不得自拟测试命令，不得声称工具没有验证的结果。"
        "先读取证据，再生成最小 Unified Diff 并调用 apply_patch；需要时运行目标测试。"
        "生成 Unified Diff 时省略虚构的 index 行；hunk 中的空格、删除行和上下文必须与 inspect_code "
        "看到的原文件逐字一致。修改已有行应使用 -旧行/+新行，不要在 return 后添加不可达代码。"
        "单行替换的合法格式示例为：\n--- a/example.py\n+++ b/example.py\n@@ -7 +7 @@\n"
        "-    return old\n+    return new\n。不要在 @@ 行附加函数文本，不要重复旧 return。"
        "PATCH_CHECK_FAILED 后不得原样重试，必须重新检查并改变 Patch。"
        "完成后输出 final 动作，最终成功与否由确定性验证决定。\n"
        "可用工具："
        + json.dumps(tool_definitions, ensure_ascii=False, separators=(",", ":"))
    )
    user = {
        "task": _agent_task_view(task),
        "initial_target_test": initial_test.to_dict(),
    }
    return (
        Message("system", system),
        Message("user", json.dumps(user, ensure_ascii=False, separators=(",", ":"))),
    )


def _agent_task_view(task: TaskSpec) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "issue": task.issue,
        "failing_tests": list(task.failing_tests),
        "acceptance_criteria": list(task.acceptance_criteria),
        "target_files": list(task.target_files),
        "protected_paths": list(task.protected_paths),
        "metadata": dict(task.metadata),
    }


def _trace_tool(trace: TraceWriter, phase: str, result: ToolResult) -> None:
    trace.write(
        "tool_call",
        {"phase": phase, "tool": result.tool, "result": result.to_dict()},
        trace_id=result.trace_id,
    )


def _cleanup_workspace(
    manager: WorkspaceManager,
    workspace: Workspace,
    trace: TraceWriter,
) -> ToolResult:
    rollback = rollback_workspace(workspace)
    _trace_tool(trace, "cleanup", rollback)
    if not rollback.ok:
        return rollback
    try:
        manager.delete(workspace)
    except (OSError, ValueError) as exc:
        return ToolResult.failure(
            "rollback_workspace",
            code="WORKSPACE_DELETE_ERROR",
            message=str(exc),
            data={"rolled_back": True, "deleted": False},
        )
    return replace(
        rollback,
        data={**rollback.data, "deleted": True},
    )


def _final_reason(
    react: ReactResult,
    *,
    patch_applied: bool,
    diff_result: ToolResult,
    changed_files: tuple[str, ...],
    target_test: ToolResult | None,
    regression_test: ToolResult | None,
    static_result: ToolResult | None,
) -> str:
    if react.status != "completed":
        return react.reason
    if not patch_applied:
        return "NO_APPLIED_PATCH"
    if not diff_result.ok:
        return diff_result.error.code if diff_result.error else "DIFF_VALIDATION_FAILED"
    if not changed_files:
        return "EMPTY_PATCH"
    if target_test is None or not target_test.ok:
        return "TARGET_TEST_FAILED"
    if regression_test is None or not regression_test.ok:
        return "REGRESSION_TEST_FAILED"
    if static_result is None or not static_result.ok:
        return "STATIC_CHECK_FAILED"
    return "VALIDATION_PASSED"
