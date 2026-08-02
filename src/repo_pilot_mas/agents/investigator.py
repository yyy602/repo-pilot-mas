"""Evidence-producing Investigator Worker."""

from __future__ import annotations

import json
from collections.abc import Sequence

from repo_pilot_mas.agents.worker_common import WorkerAgentError, worker_task_view
from repo_pilot_mas.models import GenerationConfig, Message, ModelAdapter
from repo_pilot_mas.orchestration.react_loop import ReactBudget, ReactLoop
from repo_pilot_mas.runtime import TraceWriter
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec
from repo_pilot_mas.schemas.worker_artifact import (
    EVIDENCE_CONTENT_SCHEMA,
    INVESTIGATOR_MODES,
    validate_worker_artifact,
)
from repo_pilot_mas.tools.registry import ToolRegistry

_MODE_TOOLS = {
    "code_retrieval": ("list_files", "search_code", "inspect_code", "find_references"),
    "failure_reproduction": ("list_files", "search_code", "inspect_code", "run_tests"),
    "dependency_trace": ("search_code", "inspect_code", "find_references"),
    "evidence_completion": (
        "list_files",
        "search_code",
        "inspect_code",
        "find_references",
        "run_tests",
        "static_check",
    ),
    "regression_scope": ("search_code", "inspect_code", "find_references", "run_tests"),
}
_REQUIRED_TOOLS = {
    "code_retrieval": frozenset({"search_code", "inspect_code"}),
    "failure_reproduction": frozenset({"run_tests"}),
    "dependency_trace": frozenset({"find_references", "search_code"}),
    "evidence_completion": frozenset({"inspect_code", "run_tests"}),
    "regression_scope": frozenset({"find_references", "run_tests"}),
}


class InvestigatorAgent:
    def __init__(
        self,
        model: ModelAdapter,
        tools: ToolRegistry,
        *,
        trace_writer: TraceWriter | None = None,
        budget: ReactBudget | None = None,
        generation_config: GenerationConfig | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.trace_writer = trace_writer
        self.budget = budget or ReactBudget(max_steps=8, max_model_calls=10, max_tool_calls=8)
        self.generation_config = generation_config or GenerationConfig(max_output_tokens=768)

    def run(self, task: TaskSpec, node_id: str, mode: str, objective: str) -> Artifact:
        if mode not in INVESTIGATOR_MODES:
            raise WorkerAgentError(f"不支持的 Investigator mode: {mode}")
        tools = self.tools.subset(_MODE_TOOLS[mode])
        loop = ReactLoop(
            self.model,
            tools,
            budget=self.budget,
            generation_config=self.generation_config,
            trace_writer=self.trace_writer,
            final_payload_schema=EVIDENCE_CONTENT_SCHEMA,
        )
        messages = (
            Message(
                "system",
                "你是 RepoPilot-MAS InvestigatorAgent。只调查，不修改代码。"
                "必须先调用工具取得证据；直接观察、推导和假设要明确区分。"
                "所有工具 path 都必须是候选工作区相对路径，仓库根目录只能写 '.'，绝不能写绝对路径。"
                "如果目标文件未知，先 list_files(path='.')，再 inspect_code。"
                "source 行号必须来自工具输出，tool_trace_ids 必须引用成功工具结果的 trace_id。"
                "最终 action.status=success，并在 action.artifact 返回 Evidence 内容。"
                f"当前 mode={mode}。可用工具："
                + json.dumps(tools.definitions_for_model(), ensure_ascii=False),
            ),
            Message(
                "user",
                json.dumps(
                    {
                        "task": worker_task_view(task),
                        "node_id": node_id,
                        "objective": objective,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        result = loop.run(messages)
        if result.status != "completed" or result.final_action is None:
            raise WorkerAgentError(f"Investigator 未完成：{result.reason}")
        if result.final_action["status"] != "success":
            raise WorkerAgentError(f"Investigator 报告失败：{result.final_action['reason']}")
        if not _REQUIRED_TOOLS[mode].intersection(_called_tools(result.messages)):
            raise WorkerAgentError(f"Investigator mode={mode} 缺少必要工具证据")
        content = dict(result.final_action["artifact"])
        artifact = Artifact(
            artifact_id=f"{node_id}.evidence",
            artifact_type=ArtifactType.EVIDENCE,
            created_by=node_id,
            content=content,
        )
        validate_worker_artifact(artifact, expected_type=ArtifactType.EVIDENCE)
        if not set(content["tool_trace_ids"]).issubset(
            _context_trace_ids(result.messages)
        ):
            raise WorkerAgentError("Evidence 引用了当前上下文中不存在的工具 trace_id")
        if self.trace_writer is not None:
            self.trace_writer.write("worker_artifact_created", {"artifact": artifact.to_dict()})
        return artifact


def _context_trace_ids(messages: Sequence[Message]) -> set[str]:
    trace_ids: set[str] = set()
    for message in messages:
        if message.role != "user" or not message.content.startswith("工具执行结果："):
            continue
        try:
            result = json.loads(message.content.removeprefix("工具执行结果："))
        except json.JSONDecodeError:
            continue
        if isinstance(result.get("trace_id"), str):
            trace_ids.add(result["trace_id"])
    return trace_ids


def _called_tools(messages: Sequence[Message]) -> set[str]:
    tools: set[str] = set()
    for message in messages:
        if message.role != "user" or not message.content.startswith("工具执行结果："):
            continue
        try:
            result = json.loads(message.content.removeprefix("工具执行结果："))
        except json.JSONDecodeError:
            continue
        if isinstance(result.get("tool"), str):
            tools.add(result["tool"])
    return tools
