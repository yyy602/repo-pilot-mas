"""Isolated minimal/robust Patch Worker."""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Sequence

from repo_pilot_mas.agents.worker_common import (
    WorkerAgentError,
    generate_artifact,
    worker_task_view,
)
from repo_pilot_mas.models import GenerationConfig, Message, ModelAdapter
from repo_pilot_mas.orchestration.react_loop import ReactBudget, ReactLoop
from repo_pilot_mas.runtime import TraceWriter, Workspace, restore_workspace
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec
from repo_pilot_mas.schemas.worker_artifact import (
    PATCH_STRATEGIES,
    REVIEW_CONTENT_SCHEMA,
    validate_worker_artifact,
)
from repo_pilot_mas.tools import collect_diff, run_tests, static_check
from repo_pilot_mas.tools.registry import ToolRegistry, task_test_command

_PATCH_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string", "minLength": 1},
        "old_text": {"type": "string", "minLength": 1},
        "new_text": {"type": "string", "minLength": 1},
        "rationale": {"type": "string", "minLength": 1},
        "risk_notes": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
    "required": ["file_path", "old_text", "new_text", "rationale", "risk_notes"],
    "additionalProperties": False,
}
_PATCH_TOOLS = (
    "list_files",
    "search_code",
    "inspect_code",
    "find_references",
)


class PatchAgent:
    def __init__(
        self,
        model: ModelAdapter,
        tools: ToolRegistry,
        workspace: Workspace,
        *,
        trace_writer: TraceWriter | None = None,
        budget: ReactBudget | None = None,
        generation_config: GenerationConfig | None = None,
    ) -> None:
        self.model = model
        self.all_tools = tools
        self.tools = tools.subset(_PATCH_TOOLS)
        self.workspace = workspace
        self.trace_writer = trace_writer
        self.budget = budget or ReactBudget(max_steps=6, max_model_calls=8, max_tool_calls=6)
        self.generation_config = generation_config or GenerationConfig(max_output_tokens=1024)

    def run(
        self,
        task: TaskSpec,
        node_id: str,
        strategy: str,
        objective: str,
        artifacts: Sequence[Artifact],
    ) -> Artifact:
        if strategy not in PATCH_STRATEGIES:
            raise WorkerAgentError(f"不支持的 Patch strategy: {strategy}")
        hypotheses = [
            item
            for item in artifacts
            if item.artifact_type is ArtifactType.HYPOTHESIS
        ]
        if not hypotheses:
            raise WorkerAgentError("PatchAgent 缺少 Hypothesis Artifact")
        (
            hypothesis_refs,
            primary_hypothesis_ref,
            covered_root_causes,
        ) = _hypothesis_binding(hypotheses)
        prompt = (
            "你是 RepoPilot-MAS PatchAgent，只能为当前候选工作区提出一个精确文本替换。"
            "先用 inspect_code 读取真实代码；绝不能修改 protected_paths。"
            "所有工具 file_path/path 都必须是候选工作区相对路径，根目录写 '.'，绝不能写绝对路径。"
            "最终 action.artifact 的 file_path 必须是相对路径；old_text 必须逐字复制当前文件中"
            "唯一存在的最小片段，new_text 是替换后的片段。不要输出 Unified Diff。"
            "确定性控制器会把该替换转换成 Unified Diff，并通过受保护路径检查后应用。"
            "补丁必须同时覆盖输入中的全部已接受根因，不能只处理 primary 根因。"
            + (
                "Minimal 策略只做修复根因所需的最小改动，避免重构和接口变化。"
                if strategy == "minimal"
                else "Robust 策略可处理证据支持的相邻边界，但必须控制修改范围并解释风险。"
            )
            + "可用工具："
            + json.dumps(self.tools.definitions_for_model(), ensure_ascii=False)
        )
        loop = ReactLoop(
            self.model,
            self.tools,
            budget=self.budget,
            generation_config=self.generation_config,
            trace_writer=self.trace_writer,
            final_payload_schema=_PATCH_DRAFT_SCHEMA,
        )
        history: tuple[Message, ...] = (
            Message("system", prompt),
            Message(
                "user",
                json.dumps(
                    {
                        "task": worker_task_view(task),
                        "node_id": node_id,
                        "strategy": strategy,
                        "objective": objective,
                        "accepted_hypothesis_refs": list(hypothesis_refs),
                        "primary_hypothesis_ref": primary_hypothesis_ref,
                        "covered_root_causes": covered_root_causes,
                        "input_artifacts": [item.to_dict() for item in artifacts],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        result = loop.run(history)
        applied = None
        draft = None
        target_precheck = None
        last_failure_code = "MODEL_FORMAT_ERROR"
        for attempt_index in range(3):
            if (
                result.status == "completed"
                and result.final_action is not None
                and result.final_action["status"] == "success"
                and "inspect_code" in _successful_tools(result.messages)
            ):
                draft = result.final_action["artifact"]
                try:
                    patch_text = _replacement_diff(
                        self.workspace,
                        str(draft["file_path"]),
                        str(draft["old_text"]),
                        str(draft["new_text"]),
                    )
                    applied = self.all_tools.invoke(
                        "apply_patch",
                        {"patch_text": patch_text},
                    )
                    if not applied.ok:
                        last_failure_code = "MODEL_FORMAT_ERROR"
                        failure = (
                            applied.error.message
                            if applied.error
                            else "补丁应用失败"
                        )
                    else:
                        if self.trace_writer is not None:
                            self.trace_writer.write(
                                "tool_call",
                                {
                                    "phase": "patch_controller",
                                    "tool": "apply_patch",
                                    "result": applied.to_dict(),
                                },
                                trace_id=applied.trace_id,
                            )
                        if not task.failing_tests:
                            break
                        target_precheck = run_tests(
                            self.workspace.root,
                            command=task_test_command(task, target=True),
                            timeout_seconds=min(
                                float(task.max_runtime_seconds),
                                60.0,
                            ),
                        )
                        if self.trace_writer is not None:
                            self.trace_writer.write(
                                "tool_call",
                                {
                                    "phase": "patch_target_precheck",
                                    "node_id": node_id,
                                    "attempt": attempt_index + 1,
                                    "tool": "run_tests",
                                    "result": target_precheck.to_dict(),
                                },
                                trace_id=target_precheck.trace_id,
                            )
                            self.trace_writer.write(
                                "patch_target_precheck_completed",
                                {
                                    "node_id": node_id,
                                    "attempt": attempt_index + 1,
                                    "workspace_id": self.workspace.workspace_id,
                                    "result": target_precheck.to_dict(),
                                },
                                trace_id=target_precheck.trace_id,
                            )
                        if target_precheck.ok:
                            break
                        last_failure_code = "PATCH_TARGET_TEST_FAILED"
                        output = "\n".join(
                            part
                            for part in (
                                target_precheck.stdout,
                                target_precheck.stderr,
                            )
                            if part
                        )[-4000:]
                        failure = (
                            "目标测试仍失败，必须根据真实输出修改根因对应逻辑，"
                            f"不能重复相同替换：{output}"
                        )
                        restore_workspace(self.workspace)
                        applied = None
                except (OSError, ValueError) as exc:
                    last_failure_code = "MODEL_FORMAT_ERROR"
                    failure = str(exc)
            else:
                last_failure_code = "MODEL_FORMAT_ERROR"
                failure = "尚未成功 inspect_code 并返回结构化文本替换"
            if attempt_index == 2:
                break
            result = loop.run(
                (
                    *result.messages,
                    Message(
                        "user",
                        f"候选替换不可应用：{failure}。重新 inspect_code，确保 old_text 在目标文件中"
                        "逐字且只出现一次，然后返回修正后的 final artifact。",
                    ),
                )
            )
        if result.status != "completed" or result.final_action is None:
            raise WorkerAgentError(f"PatchAgent 未完成：{result.reason}")
        if result.final_action["status"] != "success":
            raise WorkerAgentError(
                f"PatchAgent 报告失败：{result.final_action['reason']}"
            )
        if draft is None or applied is None or not applied.ok:
            if (
                last_failure_code == "PATCH_TARGET_TEST_FAILED"
                and target_precheck is not None
                and not target_precheck.ok
            ):
                output = "\n".join(
                    part
                    for part in (target_precheck.stdout, target_precheck.stderr)
                    if part
                )[-2000:]
                raise WorkerAgentError(
                    f"目标测试预检查失败：{output}",
                    code="PATCH_TARGET_TEST_FAILED",
                )
            raise WorkerAgentError("PatchAgent 未能生成可应用的结构化文本替换")
        collected = collect_diff(
            self.workspace,
            protected_paths=task.protected_paths,
        )
        if not collected.ok or collected.data.get("is_clean"):
            code = collected.error.code if collected.error else "EMPTY_PATCH"
            raise WorkerAgentError(f"Patch 收集失败：{code}")
        if collected.truncated:
            raise WorkerAgentError("Patch diff 超出可审计大小限制")
        files = [str(item["path"]) for item in collected.data["files"]]
        diff_text = str(collected.data["diff"])
        protected_ok = not collected.data["protected_path_violations"]
        precheck = static_check(
            self.workspace.root,
            run_ruff=False,
            timeout_seconds=task.max_runtime_seconds,
        )
        precheck_output = "\n".join(
            part for part in (precheck.stdout, precheck.stderr) if part
        )[-4000:]
        static_result = {
            "ok": precheck.ok,
            "trace_id": precheck.trace_id,
            "exit_code": int(
                precheck.exit_code
                if precheck.exit_code is not None
                else (0 if precheck.ok else 1)
            ),
            "output_tail": precheck_output,
        }
        if self.trace_writer is not None:
            self.trace_writer.write(
                "tool_call",
                {
                    "phase": "patch_precheck",
                    "node_id": node_id,
                    "result": precheck.to_dict(),
                },
                trace_id=precheck.trace_id,
            )
            self.trace_writer.write(
                "patch_precheck_completed",
                {
                    "node_id": node_id,
                    "workspace_id": self.workspace.workspace_id,
                    "ok": precheck.ok,
                    "protected_path_check": protected_ok,
                    "accepted_hypothesis_refs": list(hypothesis_refs),
                    "primary_hypothesis_ref": primary_hypothesis_ref,
                    "trace_id": precheck.trace_id,
                },
                trace_id=precheck.trace_id,
            )
        if not protected_ok:
            raise WorkerAgentError(
                "Patch 修改了受保护路径",
                code="ARTIFACT_SCHEMA_ERROR",
            )
        if not precheck.ok:
            raise WorkerAgentError(
                "Patch 最小语义预检查失败",
                code="TOOL_EXECUTION_ERROR",
            )
        pre_patch_behavior = "；".join(
            str(item.content.get("direct_cause") or item.content.get("root_cause"))
            for item in hypotheses
        )
        failure_input_walkthrough = "；".join(
            f"{item.ref}: {item.content.get('root_cause')} -> "
            f"{item.content.get('direct_cause')}"
            for item in hypotheses
        )
        content = {
            "strategy": strategy,
            "based_on_hypothesis_refs": list(hypothesis_refs),
            "primary_hypothesis_ref": primary_hypothesis_ref,
            "covered_root_causes": covered_root_causes,
            "diff": diff_text,
            "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
            "changed_files": files,
            "rationale": str(draft["rationale"]),
            "semantic_rationale": str(draft["rationale"]),
            "pre_patch_behavior": pre_patch_behavior,
            "post_patch_expected_behavior": (
                f"应用 {strategy} 替换后，{draft['rationale']}，失败输入不再触发直接原因"
            ),
            "failure_input_walkthrough": failure_input_walkthrough,
            "precheck_command": (
                list(target_precheck.command)
                if target_precheck is not None
                and target_precheck.command is not None
                else ["static_check", ".", "--no-ruff"]
            ),
            "precheck_result": (
                {
                    "ok": target_precheck.ok,
                    "trace_id": target_precheck.trace_id,
                    "exit_code": int(
                        target_precheck.exit_code
                        if target_precheck.exit_code is not None
                        else (0 if target_precheck.ok else 1)
                    ),
                    "output_tail": "\n".join(
                        part
                        for part in (
                            target_precheck.stdout,
                            target_precheck.stderr,
                        )
                        if part
                    )[-4000:],
                }
                if target_precheck is not None
                else static_result
            ),
            "risk_notes": list(draft["risk_notes"]),
            "protected_path_check": protected_ok,
            "workspace_id": self.workspace.workspace_id,
        }
        refs = tuple(item.ref for item in artifacts)
        artifact = Artifact(
            artifact_id=f"{node_id}.patch",
            artifact_type=ArtifactType.PATCH_CANDIDATE,
            created_by=node_id,
            content=content,
            input_refs=refs,
        )
        validate_worker_artifact(
            artifact,
            expected_type=ArtifactType.PATCH_CANDIDATE,
            allowed_input_refs=refs,
        )
        if self.trace_writer is not None:
            self.trace_writer.write(
                "worker_artifact_created",
                {"artifact": artifact.to_dict()},
            )
        return artifact

    @staticmethod
    def review_peer(
        model: ModelAdapter,
        task: TaskSpec,
        node_id: str,
        mode: str,
        objective: str,
        artifacts: Sequence[Artifact],
        *,
        trace_writer: TraceWriter | None = None,
        generation_config: GenerationConfig | None = None,
    ) -> Artifact:
        side_by_mode = {
            "minimal_critiques_robust": ("minimal", "robust"),
            "robust_critiques_minimal": ("robust", "minimal"),
        }
        try:
            own_strategy, target_strategy = side_by_mode[mode]
        except KeyError as exc:
            raise WorkerAgentError(
                f"不支持的 Patch critique mode: {mode}"
            ) from exc
        patches = [
            item
            for item in artifacts
            if item.artifact_type is ArtifactType.PATCH_CANDIDATE
        ]
        evidence = [
            item
            for item in artifacts
            if item.artifact_type is ArtifactType.EVIDENCE
        ]
        if len(patches) != 2 or not evidence:
            raise WorkerAgentError(
                "Patch 交叉审查需要两个 PatchCandidate 和直接 Evidence"
            )
        own = next(
            (
                item
                for item in patches
                if item.content["strategy"] == own_strategy
            ),
            None,
        )
        target = next(
            (
                item
                for item in patches
                if item.content["strategy"] == target_strategy
            ),
            None,
        )
        if own is None or target is None:
            raise WorkerAgentError("Patch 交叉审查缺少 Minimal 或 Robust 候选")
        review = generate_artifact(
            model,
            task=task,
            node_id=node_id,
            mode=mode,
            objective=objective,
            artifacts=artifacts,
            artifact_type=ArtifactType.REVIEW,
            content_schema=REVIEW_CONTENT_SCHEMA,
            system_prompt=(
                f"你是 {own_strategy} 策略 PatchAgent 的全新审查实例，只审查对方的"
                f" {target_strategy} Patch。mode 必须输出 patch_review，"
                f"target_artifact_ref 必须为 {target.ref}。"
                + (
                    "重点指出 Robust 是否过度修改、改变公开契约或引入无证据复杂度。"
                    if own_strategy == "minimal"
                    else "重点指出 Minimal 是否硬编码目标用例、只修表面症状或遗漏相邻边界。"
                )
                + "evidence_refs 只能引用输入 Evidence；你只给审查意见，不选择 Patch。"
            ),
            generation_config=(
                generation_config
                or GenerationConfig(max_output_tokens=1024)
            ),
            trace_writer=trace_writer,
        )
        if review.content["target_artifact_ref"] != target.ref:
            raise WorkerAgentError("Patch 交叉审查必须指向对方候选")
        return review


def _hypothesis_binding(
    hypotheses: Sequence[Artifact],
) -> tuple[tuple[str, ...], str, dict[str, str]]:
    """Bind one Patch to every accepted root cause in deterministic input order."""

    ordered: dict[str, Artifact] = {}
    for artifact in hypotheses:
        if artifact.artifact_type is not ArtifactType.HYPOTHESIS:
            raise WorkerAgentError("Patch 根因绑定包含非 Hypothesis Artifact")
        ordered.setdefault(artifact.ref, artifact)
    if not ordered:
        raise WorkerAgentError("Patch 根因绑定不能为空")

    refs = tuple(ordered)
    covered_root_causes: dict[str, str] = {}
    for ref, artifact in ordered.items():
        root_cause = str(artifact.content.get("root_cause", "")).strip()
        if not root_cause:
            raise WorkerAgentError(
                f"Hypothesis {ref} 缺少可审计的 root_cause"
            )
        covered_root_causes[ref] = root_cause
    return refs, refs[0], covered_root_causes


def _successful_tools(messages: Sequence[Message]) -> set[str]:
    names: set[str] = set()
    for message in messages:
        if (
            message.role != "user"
            or not message.content.startswith("工具执行结果：")
        ):
            continue
        try:
            result = json.loads(
                message.content.removeprefix("工具执行结果：")
            )
        except json.JSONDecodeError:
            continue
        if result.get("ok") is True and isinstance(result.get("tool"), str):
            names.add(result["tool"])
    return names


def _replacement_diff(
    workspace: Workspace,
    file_path: str,
    old_text: str,
    new_text: str,
) -> str:
    target = workspace.guard().resolve(file_path, allow_root=False)
    if not target.is_file() or target.is_symlink():
        raise ValueError("file_path 必须指向工作区内普通文件")
    if old_text == new_text:
        raise ValueError("old_text 与 new_text 不能相同")
    source = target.read_text(encoding="utf-8")
    occurrences = source.count(old_text)
    if occurrences != 1:
        raise ValueError(
            f"old_text 必须在目标文件中唯一出现，实际为 {occurrences} 次"
        )
    updated = source.replace(old_text, new_text, 1)
    relative = target.relative_to(workspace.root).as_posix()
    return "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
            lineterm="\n",
        )
    )
