"""Deterministic Phase 5 validation of one isolated PatchCandidate workspace."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from repo_pilot_mas.runtime import TraceWriter, WorkspaceManager
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.schemas.worker_artifact import validate_worker_artifact
from repo_pilot_mas.tools import collect_diff, run_tests, static_check


class ValidationExecutor:
    """Run trusted TaskSpec commands; no model can choose executable commands here."""

    def __init__(
        self,
        workspace_root: str,
        *,
        trace_writer: TraceWriter | None = None,
    ) -> None:
        self.workspace_root = workspace_root
        self.trace_writer = trace_writer

    def run(
        self,
        task: TaskSpec,
        node_id: str,
        artifacts: Sequence[Artifact],
    ) -> Artifact:
        patches = [item for item in artifacts if item.artifact_type is ArtifactType.PATCH_CANDIDATE]
        if len(patches) != 1:
            raise ValueError("ValidationTask 必须且只能引用一个 PatchCandidate")
        patch = patches[0]
        patch_reviews = [
            item
            for item in artifacts
            if item.artifact_type is ArtifactType.REVIEW
            and item.content.get("mode") == "patch_review"
            and item.content.get("target_artifact_ref")
            in {patch.ref, patch.artifact_id}
        ]
        if not patch_reviews:
            raise ValueError(
                "ValidationTask 必须引用目标 PatchCandidate 的 patch_review"
            )
        if any(
            item.content.get("verdict")
            not in {"approved", "compatible", "supported"}
            for item in patch_reviews
        ):
            raise ValueError("ValidationTask 不能执行带阻塞 Patch Review 的补丁")

        manager = WorkspaceManager(task.repository_path, self.workspace_root)
        workspace = manager.open(task.task_id, str(patch.content["workspace_id"]))

        diff_result = collect_diff(workspace, protected_paths=task.protected_paths)
        actual_diff = str(diff_result.data.get("diff", ""))
        actual_hash = hashlib.sha256(actual_diff.encode()).hexdigest()
        integrity_ok = bool(
            diff_result.data.get("changed_count")
            and actual_diff == patch.content["diff"]
            and actual_hash == patch.content["diff_sha256"]
        )
        protected_ok = diff_result.ok and not diff_result.data.get("protected_path_violations")

        target_command = tuple(task.test_command) + tuple(task.failing_tests)
        target = run_tests(
            workspace.root,
            command=target_command,
            timeout_seconds=task.max_runtime_seconds,
        )
        regression = run_tests(
            workspace.root,
            command=task.test_command,
            timeout_seconds=task.max_runtime_seconds,
        )
        static = static_check(
            workspace.root,
            timeout_seconds=min(task.max_runtime_seconds, 60.0),
        )
        for result in (diff_result, target, regression, static):
            self._trace_tool(node_id, result)

        failure_class = "none"
        if not integrity_ok:
            failure_class = "patch_integrity_failure"
        elif not protected_ok:
            failure_class = "protected_path_violation"
        elif not target.ok:
            failure_class = "target_test_failure"
        elif not regression.ok:
            failure_class = "regression_failure"
        elif not static.ok:
            failure_class = "syntax_failure"
        passed = failure_class == "none"
        changed_files = [str(item["path"]) for item in diff_result.data.get("files", ())]
        changed_lines = _changed_lines(actual_diff)
        content = {
            "patch_ref": patch.ref,
            "patch_review_refs": [item.ref for item in patch_reviews],
            "patch_sha256": str(patch.content["diff_sha256"]),
            "workspace_id": workspace.workspace_id,
            "applied": integrity_ok,
            "target_test": _command_evidence(target),
            "regression_test": _command_evidence(regression),
            "static_check": _command_evidence(static, fallback_command=("static_check",)),
            "protected_path_check": protected_ok,
            "changed_files": changed_files,
            "changed_lines": changed_lines,
            "passed": passed,
            "failure_class": failure_class,
            "tool_trace_ids": [
                diff_result.trace_id,
                target.trace_id,
                regression.trace_id,
                static.trace_id,
            ],
        }
        artifact = Artifact(
            artifact_id=f"{node_id}.validation",
            artifact_type=ArtifactType.VALIDATION_RESULT,
            created_by=node_id,
            content=content,
            input_refs=(patch.ref, *(item.ref for item in patch_reviews)),
        )
        validate_worker_artifact(
            artifact,
            expected_type=ArtifactType.VALIDATION_RESULT,
            allowed_input_refs=tuple(item.ref for item in artifacts),
        )
        if self.trace_writer is not None:
            self.trace_writer.write(
                "worker_artifact_created",
                {"artifact": artifact.to_dict(), "executor": "deterministic_validation"},
            )
        manager.delete(workspace)
        if self.trace_writer is not None:
            self.trace_writer.write(
                "validation_workspace_deleted",
                {"node_id": node_id, "workspace_id": workspace.workspace_id},
            )
        return artifact

    def _trace_tool(self, node_id: str, result: ToolResult) -> None:
        if self.trace_writer is not None:
            self.trace_writer.write(
                "tool_call",
                {"phase": "validation", "node_id": node_id, "result": result.to_dict()},
                trace_id=result.trace_id,
            )


def _command_evidence(
    result: ToolResult,
    *,
    fallback_command: tuple[str, ...] = ("run_tests",),
) -> dict[str, Any]:
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return {
        "command": list(result.command or fallback_command),
        "exit_code": int(result.exit_code if result.exit_code is not None else (0 if result.ok else 1)),
        "trace_id": result.trace_id,
        "duration_ms": result.duration_ms,
        "output_tail": output[-4000:],
    }


def _changed_lines(diff: str) -> int:
    return sum(
        1
        for line in diff.splitlines()
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )
