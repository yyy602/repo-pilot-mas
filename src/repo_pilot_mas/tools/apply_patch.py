"""Apply a unified diff inside an isolated workspace."""

from __future__ import annotations

from pathlib import Path

from repo_pilot_mas.runtime.command_runner import CommandPolicyError, CommandRunner
from repo_pilot_mas.runtime.path_guard import PathGuard, PathSecurityError
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.tools._common import ToolContext

_HEADER_PREFIXES = ("--- ", "+++ ", "rename from ", "rename to ", "copy from ", "copy to ")


def apply_patch(
    root: str | Path,
    patch_text: str,
    *,
    timeout_seconds: float = 30.0,
    max_patch_chars: int = 500_000,
    max_output_chars: int = 20_000,
) -> ToolResult:
    """Validate and atomically apply a textual unified diff with ``git apply``."""

    context = ToolContext.start("apply_patch")
    try:
        if not patch_text.strip():
            raise ValueError("patch_text must not be empty")
        if len(patch_text) > max_patch_chars:
            raise ValueError(
                f"patch exceeds size limit ({len(patch_text)} > {max_patch_chars} characters)"
            )
        if "GIT binary patch" in patch_text or "Binary files " in patch_text:
            raise ValueError("binary patches are not supported")

        guard = PathGuard(root)
        changed_paths = _validate_patch_paths(guard, patch_text)
        if not changed_paths:
            raise ValueError("patch does not contain any supported file headers")

        runner = CommandRunner(root)
        check_execution = runner.run(
            ("git", "apply", "--check", "--whitespace=nowarn", "-"),
            input_text=patch_text,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )
        if check_execution.timed_out:
            return ToolResult.failure(
                "apply_patch",
                code="PATCH_TIMEOUT",
                message=f"patch validation exceeded {timeout_seconds} seconds",
                data={"changed_paths": changed_paths, "stage": "check"},
                stdout=check_execution.stdout,
                stderr=check_execution.stderr,
                exit_code=check_execution.exit_code,
                command=check_execution.command,
                duration_ms=check_execution.duration_ms,
                truncated=check_execution.truncated,
                trace_id=context.trace_id,
                started_at=context.started_at,
            )
        if check_execution.exit_code != 0:
            return ToolResult.failure(
                "apply_patch",
                code="PATCH_CHECK_FAILED",
                message="patch cannot be applied cleanly",
                data={"changed_paths": changed_paths, "stage": "check"},
                stdout=check_execution.stdout,
                stderr=check_execution.stderr,
                exit_code=check_execution.exit_code,
                command=check_execution.command,
                duration_ms=check_execution.duration_ms,
                truncated=check_execution.truncated,
                trace_id=context.trace_id,
                started_at=context.started_at,
            )

        apply_execution = runner.run(
            ("git", "apply", "--whitespace=nowarn", "-"),
            input_text=patch_text,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )
        if apply_execution.timed_out or apply_execution.exit_code != 0:
            return ToolResult.failure(
                "apply_patch",
                code="PATCH_APPLY_FAILED",
                message=(
                    f"patch application exceeded {timeout_seconds} seconds"
                    if apply_execution.timed_out
                    else f"git apply exited with code {apply_execution.exit_code}"
                ),
                data={"changed_paths": changed_paths, "stage": "apply"},
                stdout=apply_execution.stdout,
                stderr=apply_execution.stderr,
                exit_code=apply_execution.exit_code,
                command=apply_execution.command,
                duration_ms=apply_execution.duration_ms,
                truncated=apply_execution.truncated,
                trace_id=context.trace_id,
                started_at=context.started_at,
            )
        return ToolResult.success(
            "apply_patch",
            data={"changed_paths": changed_paths, "applied": True},
            stdout=apply_execution.stdout,
            stderr=apply_execution.stderr,
            exit_code=apply_execution.exit_code,
            command=apply_execution.command,
            duration_ms=apply_execution.duration_ms,
            truncated=apply_execution.truncated,
            trace_id=context.trace_id,
            started_at=context.started_at,
        )
    except (OSError, ValueError, PathSecurityError, CommandPolicyError) as exc:
        return context.failure(code="APPLY_PATCH_ERROR", message=str(exc))


def _validate_patch_paths(guard: PathGuard, patch_text: str) -> list[str]:
    paths: set[str] = set()
    for line in patch_text.splitlines():
        prefix = next((item for item in _HEADER_PREFIXES if line.startswith(item)), None)
        if prefix is None:
            continue
        raw_path = line[len(prefix) :].split("\t", 1)[0].strip()
        if raw_path == "/dev/null":
            continue
        if raw_path.startswith(("a/", "b/")):
            raw_path = raw_path[2:]
        if not raw_path:
            raise ValueError("patch contains an empty file path")
        guard.resolve(raw_path, must_exist=False, allow_root=False)
        paths.add(Path(raw_path).as_posix())
    return sorted(paths)
