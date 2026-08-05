"""Evidence-producing Investigator Worker."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

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
_FAILURE_TYPE_PATTERN = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\b"
)


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
            final_payload_schema=_investigator_payload_schema(mode),
        )
        messages = (
            Message(
                "system",
                "你是 RepoPilot-MAS InvestigatorAgent。只调查，不修改代码。"
                "必须先调用工具取得证据；直接观察、推导和假设要明确区分。"
                "所有工具 path 都必须是候选工作区相对路径，仓库根目录只能写 '.'，绝不能写绝对路径。"
                "如果目标文件未知，先 list_files(path='.')，再 inspect_code。"
                "source 行号必须来自工具输出，tool_trace_ids 必须引用当前上下文中的工具结果 trace_id。"
                "模型输出不得包含 reproduction；该字段仅在 failure_reproduction 模式下由 Agent "
                "根据真实 run_tests Observation 注入。"
                "failure_reproduction 模式下，目标测试以非零退出并产生明确失败输出表示缺陷复现成功，"
                "此时仍应返回 action.status=success；只有工具调用或调查过程本身无法完成时才返回 failure。"
                "最终在 action.artifact 返回 Evidence 内容。"
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
        if not _REQUIRED_TOOLS[mode].intersection(_called_tools(result.messages)):
            raise WorkerAgentError(f"Investigator mode={mode} 缺少必要工具证据")

        content = dict(result.final_action["artifact"])
        # The model-facing schema no longer advertises reproduction, but accepts
        # unknown fields so stale model behavior does not fail inside ReactLoop.
        # Discard any model-authored value before strict Artifact validation.
        content.pop("reproduction", None)
        if content.get("mode") != mode:
            raise WorkerAgentError(
                f"Investigator Artifact mode 不一致：expected={mode}, actual={content.get('mode')}"
            )

        reproduction: dict[str, Any] | None = None
        if mode == "failure_reproduction":
            reproduction = _reproduction_observation(result.messages)
            if reproduction is None:
                raise WorkerAgentError("failure_reproduction 缺少可解析的 run_tests 结果")
            # Tool Observation is the source of truth. Never trust a
            # model-authored reproduction object.
            content["reproduction"] = reproduction
            if (
                result.final_action["status"] != "success"
                and not reproduction["succeeded"]
            ):
                raise WorkerAgentError(
                    f"Investigator 报告失败：{result.final_action['reason']}"
                )
        elif result.final_action["status"] != "success":
            raise WorkerAgentError(
                f"Investigator 报告失败：{result.final_action['reason']}"
            )

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
            if reproduction is not None and reproduction["succeeded"]:
                self.trace_writer.write(
                    "bug_reproduction_confirmed",
                    {
                        "node_id": node_id,
                        "failure_type": reproduction["failure_type"],
                        "test_exit_code": reproduction["exit_code"],
                        "failing_command": reproduction["command"],
                        "tool_trace_ids": list(content["tool_trace_ids"]),
                    },
                )
            self.trace_writer.write(
                "worker_artifact_created",
                {"artifact": artifact.to_dict()},
            )
        return artifact


def _investigator_payload_schema(mode: str) -> dict[str, Any]:
    """Return the model-facing Evidence schema for one Investigator mode.

    ``reproduction`` is intentionally omitted because it is deterministic data
    derived from ``run_tests``. ``additionalProperties`` remains permissive only
    at the model boundary so an older model response containing reproduction can
    be normalized before the final strict Artifact validation.
    """

    if mode not in INVESTIGATOR_MODES:
        raise ValueError(f"unsupported Investigator mode: {mode}")
    schema = deepcopy(EVIDENCE_CONTENT_SCHEMA)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise TypeError("Evidence schema properties must be a dictionary")
    properties["mode"] = {"const": mode}
    properties.pop("reproduction", None)
    schema["additionalProperties"] = True
    return schema


def _reproduction_observation(messages: Sequence[Message]) -> dict[str, Any] | None:
    for result in reversed(_tool_results(messages)):
        if result.get("tool") != "run_tests":
            continue
        exit_code = result.get("exit_code")
        command = result.get("command")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            return None
        if not isinstance(command, Sequence) or isinstance(command, (str, bytes)):
            return None
        normalized_command = [str(item) for item in command if str(item)]
        if not normalized_command:
            return None
        data = result.get("data")
        data_mapping = data if isinstance(data, Mapping) else {}
        error = result.get("error")
        error_mapping = error if isinstance(error, Mapping) else {}
        timed_out = bool(data_mapping.get("timed_out", False))
        error_code = str(error_mapping.get("code", ""))
        reproduction_succeeded = bool(
            error_code == "TEST_FAILED" and exit_code != 0 and not timed_out
        )
        stdout = str(result.get("stdout", ""))
        stderr = str(result.get("stderr", ""))
        failure_output = "\n".join(
            part for part in (stdout, stderr) if part
        )[-4000:]
        matches = _FAILURE_TYPE_PATTERN.findall(failure_output)
        failure_type = matches[-1] if matches else (
            error_code if reproduction_succeeded else ""
        )
        return {
            "attempted": True,
            "succeeded": reproduction_succeeded,
            "exit_code": exit_code,
            "failure_type": failure_type,
            "failure_output": failure_output,
            "command": normalized_command,
        }
    return None


def _tool_results(messages: Sequence[Message]) -> tuple[dict[str, Any], ...]:
    results: list[dict[str, Any]] = []
    for message in messages:
        if message.role != "user" or not message.content.startswith("工具执行结果："):
            continue
        try:
            value = json.loads(message.content.removeprefix("工具执行结果："))
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            results.append(dict(value))
    return tuple(results)


def _context_trace_ids(messages: Sequence[Message]) -> set[str]:
    return {
        str(result["trace_id"])
        for result in _tool_results(messages)
        if isinstance(result.get("trace_id"), str)
    }


def _called_tools(messages: Sequence[Message]) -> set[str]:
    return {
        str(result["tool"])
        for result in _tool_results(messages)
        if isinstance(result.get("tool"), str)
    }
