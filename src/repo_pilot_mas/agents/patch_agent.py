"""Isolated minimal/robust Patch Worker."""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Sequence

from repo_pilot_mas.agents.worker_common import WorkerAgentError, worker_task_view
from repo_pilot_mas.models import GenerationConfig, Message, ModelAdapter
from repo_pilot_mas.orchestration.react_loop import ReactBudget, ReactLoop
from repo_pilot_mas.runtime import TraceWriter, Workspace
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec
from repo_pilot_mas.schemas.worker_artifact import PATCH_STRATEGIES, validate_worker_artifact
from repo_pilot_mas.tools import collect_diff
from repo_pilot_mas.tools.registry import ToolRegistry

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
        hypotheses = [item for item in artifacts if item.artifact_type is ArtifactType.HYPOTHESIS]
        if not hypotheses:
            raise WorkerAgentError("PatchAgent 缺少 Hypothesis Artifact")
        prompt = (
            "你是 RepoPilot-MAS PatchAgent，只能为当前候选工作区提出一个精确文本替换。"
            "先用 inspect_code 读取真实代码；绝不能修改 protected_paths。"
            "所有工具 file_path/path 都必须是候选工作区相对路径，根目录写 '.'，绝不能写绝对路径。"
            "最终 action.artifact 的 file_path 必须是相对路径；old_text 必须逐字复制当前文件中"
            "唯一存在的最小片段，new_text 是替换后的片段。不要输出 Unified Diff。"
            "确定性控制器会把该替换转换成 Unified Diff，并通过受保护路径检查后应用。"
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
                        "allowed_hypothesis_refs": [item.ref for item in hypotheses],
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
        for _ in range(3):
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
                    applied = self.all_tools.invoke("apply_patch", {"patch_text": patch_text})
                    if applied.ok:
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
                        break
                except (OSError, ValueError) as exc:
                    failure = str(exc)
                else:
                    failure = applied.error.message if applied.error else "补丁应用失败"
            else:
                failure = "尚未成功 inspect_code 并返回结构化文本替换"
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
            raise WorkerAgentError(f"PatchAgent 报告失败：{result.final_action['reason']}")
        if draft is None or applied is None or not applied.ok:
            raise WorkerAgentError("PatchAgent 未能生成可应用的结构化文本替换")
        collected = collect_diff(self.workspace, protected_paths=task.protected_paths)
        if not collected.ok or collected.data.get("is_clean"):
            code = collected.error.code if collected.error else "EMPTY_PATCH"
            raise WorkerAgentError(f"Patch 收集失败：{code}")
        if collected.truncated:
            raise WorkerAgentError("Patch diff 超出可审计大小限制")
        files = [str(item["path"]) for item in collected.data["files"]]
        diff_text = str(collected.data["diff"])
        content = {
            "strategy": strategy,
            "based_on_hypothesis": hypotheses[0].ref,
            "diff": diff_text,
            "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
            "changed_files": files,
            "rationale": str(draft["rationale"]),
            "risk_notes": list(draft["risk_notes"]),
            "protected_path_check": not collected.data["protected_path_violations"],
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
            self.trace_writer.write("worker_artifact_created", {"artifact": artifact.to_dict()})
        return artifact


def _successful_tools(messages: Sequence[Message]) -> set[str]:
    names: set[str] = set()
    for message in messages:
        if message.role != "user" or not message.content.startswith("工具执行结果："):
            continue
        try:
            result = json.loads(message.content.removeprefix("工具执行结果："))
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
        raise ValueError(f"old_text 必须在目标文件中唯一出现，实际为 {occurrences} 次")
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
