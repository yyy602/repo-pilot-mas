"""Isolated minimal/robust Patch Worker."""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

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
        "pre_patch_behavior": {"type": "string", "minLength": 1},
        "post_patch_expected_behavior": {"type": "string", "minLength": 1},
        "failure_input_walkthrough": {"type": "string", "minLength": 1},
        "semantic_rationale": {"type": "string", "minLength": 1},
        "risk_notes": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
    "required": [
        "file_path",
        "old_text",
        "new_text",
        "rationale",
        "pre_patch_behavior",
        "post_patch_expected_behavior",
        "failure_input_walkthrough",
        "semantic_rationale",
        "risk_notes",
    ],
    "additionalProperties": False,
}
_PATCH_TOOLS = (
    "list_files",
    "search_code",
    "inspect_code",
    "find_references",
)


@dataclass(frozen=True, slots=True)
class _ReplacementDiff:
    patch_text: str
    matched_old_text: str
    replacement_new_text: str
    reduced_to_changed_hunk: bool


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
            "绝不能修改 protected_paths；所有工具 file_path/path 都必须是候选工作区相对路径，"
            "根目录写 '.'，绝不能写绝对路径。"
            "最终 action.artifact 的 file_path 必须是相对路径；old_text 必须逐字复制当前文件中"
            "唯一存在的最小片段（通常只包含必须变化的 1-3 行），new_text 是替换后的片段。"
            "不要输出 Unified Diff；确定性控制器会把该替换转换成 Unified Diff 并应用。"
            "根因落实：先逐字阅读输入中全部 accepted Hypothesis 的 root_cause、direct_cause 与"
            " affected_symbols，定位真正产生失败输出的那一行代码，只修改该行及其必要相邻行。"
            "禁止修改与根因无关的 return 条件、禁止等价改写（例如给已经短路的 or 分支补冗余"
            "条件）、禁止把失败输入走不到的分支当修复点。若推演发现 Hypothesis 指向的代码本身"
            "正确，必须沿 direct_cause 继续定位真正出错的位置，而不是硬改一个无关行。"
            "文件状态：每次替换失败后控制器会把工作区回滚到原始状态，因此 old_text 必须始终"
            "基于当前（原始）文件内容逐字复制，绝不能把上一轮 new_text 当作 old_text。"
            "工具克制：只有上下文中确实没有目标代码时才调用 inspect_code；已经读过的文件区间"
            "禁止用相同参数重复读取。整个 attempt 内工具调用尽量少（一般 1-3 次），拿到文件"
            "内容后直接构造替换并返回 final。"
            "返回补丁前必须用失败输入按实际求值或执行顺序推演旧代码和新代码；"
            "如果要保护一个可能失败的操作，保护条件必须在该操作之前生效，不能追加在"
            "已经执行的访问之后。不能用变量改名、无效条件或只改注释冒充语义修复。"
            "pre_patch_behavior、post_patch_expected_behavior、failure_input_walkthrough 和"
            " semantic_rationale 必须记录这次具体推演，不能照抄 Hypothesis。"
            "若输入包含 ArtifactRejection，必须读取其中的失败候选和真实输出，且不得重复"
            "已失败的 new_text。"
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
        failed_candidates: list[dict[str, str]] = []
        for attempt_index in range(3):
            if (
                result.status == "completed"
                and result.final_action is not None
                and result.final_action["status"] == "success"
                and "inspect_code" in _successful_tools(result.messages)
            ):
                draft = result.final_action["artifact"]
                try:
                    replacement = _replacement_diff(
                        self.workspace,
                        str(draft["file_path"]),
                        str(draft["old_text"]),
                        str(draft["new_text"]),
                    )
                    applied = self.all_tools.invoke(
                        "apply_patch",
                        {"patch_text": replacement.patch_text},
                    )
                    if (
                        replacement.reduced_to_changed_hunk
                        and self.trace_writer is not None
                    ):
                        self.trace_writer.write(
                            "patch_replacement_reduced",
                            {
                                "node_id": node_id,
                                "attempt": attempt_index + 1,
                                "file_path": str(draft["file_path"]),
                                "matched_old_text": replacement.matched_old_text,
                                "replacement_new_text": replacement.replacement_new_text,
                                "original_old_sha256": hashlib.sha256(
                                    str(draft["old_text"]).encode()
                                ).hexdigest(),
                                "original_new_sha256": hashlib.sha256(
                                    str(draft["new_text"]).encode()
                                ).hexdigest(),
                            },
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
                        failed_candidates.append(
                            _failed_candidate_summary(
                                draft,
                                code=last_failure_code,
                                failure=failure,
                            )
                        )
                        restore_workspace(self.workspace)
                        applied = None
                except (OSError, ValueError) as exc:
                    last_failure_code = "MODEL_FORMAT_ERROR"
                    failure = str(exc)
                    if draft is not None:
                        failed_candidates.append(
                            _failed_candidate_summary(
                                draft,
                                code=last_failure_code,
                                failure=failure,
                            )
                        )
            else:
                last_failure_code = "MODEL_FORMAT_ERROR"
                failure = "尚未成功 inspect_code 并返回结构化文本替换"
            if attempt_index == 2:
                break
            has_observation = _has_tool_observation(result.messages)
            if last_failure_code == "PATCH_TARGET_TEST_FAILED":
                feedback = (
                    "上一轮替换已应用但目标测试仍失败，控制器已把工作区回滚到原始状态。"
                    "先根据 traceback 定位最先失败的操作，回到输入 Hypothesis 的 direct_cause，"
                    "检查是否改错了位置（例如改了 return 条件而根因在循环边界初始化），"
                    "并按原始代码重新推演。保护条件必须在危险操作之前生效。"
                    "不得重复上一候选的 new_text，也绝不能把上一轮的 new_text 当成 old_text。"
                    "失败候选与输出："
                    + json.dumps(
                        failed_candidates[-1],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            else:
                feedback = (
                    f"候选替换不可应用：{failure}。工作区已恢复原始状态；old_text 必须从当前"
                    "（原始）文件逐字复制，只包含必须变化的最小连续行（1-3 行），且整个片段在"
                    "文件中只出现一次。"
                )
            if has_observation:
                feedback += (
                    "目标文件内容已经在上下文中，无需再次调用 inspect_code；"
                    "直接基于已有的文件观察构造修正后的 final artifact。"
                )
            else:
                feedback += "上下文中还没有文件内容，请先用 inspect_code 读取目标文件。"
            result = loop.run((*result.messages, Message("user", feedback)))
        if result.status != "completed" or result.final_action is None:
            failure_context = (
                ";failed_candidates="
                + json.dumps(
                    failed_candidates,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if failed_candidates
                else ""
            )
            raise WorkerAgentError(
                f"PatchAgent 未完成：{result.reason}{failure_context}",
                code=last_failure_code,
            )
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
                failure_details = {
                    "failed_candidates": failed_candidates,
                    "last_test_output_tail": output,
                }
                raise WorkerAgentError(
                    "目标测试预检查失败；失败候选与真实输出："
                    + json.dumps(
                        failure_details,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
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
        content = {
            "strategy": strategy,
            "based_on_hypothesis_refs": list(hypothesis_refs),
            "primary_hypothesis_ref": primary_hypothesis_ref,
            "covered_root_causes": covered_root_causes,
            "diff": diff_text,
            "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
            "changed_files": files,
            "rationale": str(draft["rationale"]),
            "semantic_rationale": str(draft["semantic_rationale"]),
            "pre_patch_behavior": str(draft["pre_patch_behavior"]),
            "post_patch_expected_behavior": str(
                draft["post_patch_expected_behavior"]
            ),
            "failure_input_walkthrough": str(draft["failure_input_walkthrough"]),
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


def _has_tool_observation(messages: Sequence[Message]) -> bool:
    """Return whether the current context already contains any tool observation."""

    return any(
        message.role == "user"
        and message.content.startswith("工具执行结果：")
        for message in messages
    )


def _replacement_diff(
    workspace: Workspace,
    file_path: str,
    old_text: str,
    new_text: str,
) -> _ReplacementDiff:
    target = workspace.guard().resolve(file_path, allow_root=False)
    if not target.is_file() or target.is_symlink():
        raise ValueError("file_path 必须指向工作区内普通文件")
    if old_text == new_text:
        raise ValueError("old_text 与 new_text 不能相同")
    source = target.read_text(encoding="utf-8")
    matched_old_text = old_text
    replacement_new_text = new_text
    reduced = False
    occurrences = source.count(matched_old_text)
    if occurrences == 0:
        reduced_pair = _changed_line_hunk(old_text, new_text)
        if reduced_pair is not None:
            candidate_old, candidate_new = reduced_pair
            candidate_occurrences = source.count(candidate_old)
            if candidate_occurrences == 1:
                matched_old_text = candidate_old
                replacement_new_text = candidate_new
                occurrences = 1
                reduced = True
    if occurrences != 1:
        raise ValueError(
            f"old_text 必须在目标文件中唯一出现，实际为 {occurrences} 次"
        )
    updated = source.replace(matched_old_text, replacement_new_text, 1)
    relative = target.relative_to(workspace.root).as_posix()
    return _ReplacementDiff(
        patch_text="".join(
            difflib.unified_diff(
                source.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
                lineterm="\n",
            )
        ),
        matched_old_text=matched_old_text,
        replacement_new_text=replacement_new_text,
        reduced_to_changed_hunk=reduced,
    )


def _changed_line_hunk(old_text: str, new_text: str) -> tuple[str, str] | None:
    """Drop identical model-supplied context around one contiguous changed hunk."""

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    prefix = 0
    while (
        prefix < len(old_lines)
        and prefix < len(new_lines)
        and old_lines[prefix] == new_lines[prefix]
    ):
        prefix += 1

    suffix = 0
    max_suffix = min(len(old_lines) - prefix, len(new_lines) - prefix)
    while (
        suffix < max_suffix
        and old_lines[-(suffix + 1)] == new_lines[-(suffix + 1)]
    ):
        suffix += 1

    old_end = len(old_lines) - suffix if suffix else len(old_lines)
    new_end = len(new_lines) - suffix if suffix else len(new_lines)
    old_hunk = "".join(old_lines[prefix:old_end])
    new_hunk = "".join(new_lines[prefix:new_end])
    if not old_hunk or not new_hunk or old_hunk == old_text:
        return None
    return old_hunk, new_hunk


def _failed_candidate_summary(
    draft: object,
    *,
    code: str,
    failure: str,
) -> dict[str, str]:
    value = draft if isinstance(draft, dict) else {}

    def clipped(key: str, limit: int = 1200) -> str:
        return str(value.get(key, ""))[-limit:]

    return {
        "code": code,
        "file_path": clipped("file_path", 300),
        "old_text": clipped("old_text"),
        "new_text": clipped("new_text"),
        "semantic_rationale": clipped("semantic_rationale"),
        "failure_tail": str(failure)[-1800:],
    }
