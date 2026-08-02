from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from repo_pilot_mas.agents import (
    DiagnosticianAgent,
    InvestigatorAgent,
    PatchAgent,
    ReviewerAgent,
    WorkerAgentError,
    WorkerPool,
)
from repo_pilot_mas.models import (
    FakeModelAdapter,
    GenerationConfig,
    Message,
    ModelAdapter,
)
from repo_pilot_mas.models.base import RawGeneration
from repo_pilot_mas.orchestration import NodeStatus, NodeType, TaskNode
from repo_pilot_mas.runtime import WorkspaceManager
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec, validate_worker_artifact
from repo_pilot_mas.schemas.tool_result import ToolResult
from repo_pilot_mas.tools import build_workspace_tool_registry
from repo_pilot_mas.tools.registry import ToolDefinition, ToolRegistry


def _task(repository: Path) -> TaskSpec:
    return TaskSpec(
        "phase4-workers",
        repository,
        "函数错误地接受未闭合左括号",
        failing_tests=("tests/test_target.py",),
        protected_paths=("tests",),
    )


def _evidence() -> Artifact:
    artifact = Artifact(
        "N1.evidence",
        ArtifactType.EVIDENCE,
        "N1",
        {
            "mode": "code_retrieval",
            "claim": "函数结束时没有检查 depth 是否回到零",
            "source": {"path": "target.py", "line_start": 1, "line_end": 9},
            "content": "未闭合左括号会留下正 depth，但函数固定返回 True。",
            "observation_type": "direct",
            "confidence": 0.99,
            "status": "verified",
            "tool_trace_ids": ["inspect-trace"],
            "missing_evidence": [],
        },
    )
    validate_worker_artifact(artifact)
    return artifact


def _hypothesis(node_id: str = "N3") -> Artifact:
    evidence = _evidence()
    artifact = Artifact(
        f"{node_id}.hypothesis",
        ArtifactType.HYPOTHESIS,
        node_id,
        {
            "perspective": "control_flow",
            "root_cause": "终态不变量没有编码到返回条件",
            "direct_cause": "循环结束后无条件 return True",
            "supporting_evidence": [evidence.ref],
            "counter_evidence": [],
            "affected_symbols": ["is_valid_parenthesization"],
            "verification_plan": ["运行未闭合左括号用例"],
            "missing_evidence": [],
            "confidence": 0.98,
        },
        input_refs=(evidence.ref,),
    )
    validate_worker_artifact(artifact, allowed_input_refs=(evidence.ref,))
    return artifact


def _dummy_tools() -> ToolRegistry:
    registry = ToolRegistry()
    schema = {"type": "object", "additionalProperties": True}
    for name in ("list_files", "search_code", "inspect_code", "find_references"):
        registry.register(
            ToolDefinition(
                name,
                name,
                schema,
                lambda _args, tool=name: ToolResult.success(
                    tool,
                    data={"text": "target.py:1-9"},
                    trace_id="inspect-trace" if tool == "inspect_code" else f"{tool}-trace",
                ),
            )
        )
    return registry


def test_investigator_uses_mode_tools_and_returns_schema_valid_evidence(tmp_path: Path) -> None:
    model = FakeModelAdapter(
        [
            {
                "thought_summary": "读取目标函数",
                "action": {
                    "type": "tool",
                    "tool_name": "inspect_code",
                    "arguments": {"file_path": "target.py"},
                },
            },
            {
                "thought_summary": "形成直接证据",
                "action": {
                    "type": "final",
                    "status": "success",
                    "reason": "已定位代码证据",
                    "artifact": _evidence().to_dict()["content"],
                },
            },
        ]
    )
    artifact = InvestigatorAgent(model, _dummy_tools()).run(
        _task(tmp_path), "N1", "code_retrieval", "读取缺陷函数"
    )
    validate_worker_artifact(artifact, expected_type=ArtifactType.EVIDENCE)
    assert artifact.created_by == "N1"


def test_two_diagnosticians_receive_only_evidence_and_not_each_other(tmp_path: Path) -> None:
    evidence = _evidence()
    outputs = []
    for node_id, perspective in (("N3", "control_flow"), ("N4", "data_flow")):
        model = FakeModelAdapter(
            [
                {
                    "perspective": perspective,
                    "root_cause": "终态不变量缺失",
                    "direct_cause": "循环结束后固定返回 True",
                    "supporting_evidence": [
                        evidence.artifact_id if perspective == "data_flow" else evidence.ref
                    ],
                    "counter_evidence": [],
                    "affected_symbols": ["is_valid_parenthesization"],
                    "verification_plan": ["运行未闭合输入"],
                    "missing_evidence": [],
                    "confidence": 0.9,
                }
            ]
        )
        outputs.append(
            DiagnosticianAgent(model).run(
                _task(tmp_path), node_id, perspective, "独立诊断", (evidence,)
            )
        )
    assert {item.content["perspective"] for item in outputs} == {
        "control_flow",
        "data_flow",
    }
    assert outputs[1].content["supporting_evidence"] == (evidence.ref,)
    with pytest.raises(WorkerAgentError, match="只能读取 Evidence"):
        DiagnosticianAgent(FakeModelAdapter([])).run(
            _task(tmp_path), "N5", "control_flow", "错误输入", (outputs[1],)
        )


def test_reviewer_cites_target_and_direct_evidence(tmp_path: Path) -> None:
    evidence = _evidence()
    hypothesis = _hypothesis()
    model = FakeModelAdapter(
        [
            {
                "mode": "root_cause_recommendation",
                "target_artifact_ref": hypothesis.ref,
                "evidence_refs": [evidence.ref],
                "verdict": "supported",
                "findings": ["根因与直接代码观察一致"],
                "risk_notes": ["仍需目标测试验证"],
                "recommendation": "建议 Supervisor 考虑接受该根因",
            }
        ]
    )
    review = ReviewerAgent(model).run(
        _task(tmp_path),
        "N5",
        "root_cause_recommendation",
        "审查根因",
        (hypothesis, evidence),
    )
    validate_worker_artifact(
        review,
        expected_type=ArtifactType.REVIEW,
        allowed_input_refs=(hypothesis.ref, evidence.ref),
    )
    assert review.content["target_artifact_ref"] == hypothesis.ref
    assert review.content["evidence_refs"] == (evidence.ref,)


def test_two_patch_strategies_use_independent_workspaces(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.py").write_text(
        "def is_valid_parenthesization(parens):\n"
        "    depth = 0\n"
        "    for paren in parens:\n"
        "        if paren == '(':\n"
        "            depth += 1\n"
        "        else:\n"
        "            depth -= 1\n"
        "            if depth < 0:\n"
        "                return False\n"
        "    return True\n",
        encoding="utf-8",
    )
    (source / "tests").mkdir()
    (source / "tests" / "test_target.py").write_text("# protected\n", encoding="utf-8")
    task = _task(source)
    manager = WorkspaceManager(source, tmp_path / "workspaces")
    hypothesis = _hypothesis()
    artifacts = (hypothesis, _evidence())
    patches = []
    replacements = {
        "minimal": "    return depth == 0",
        "robust": "    return depth == 0  # all opens must be closed",
    }
    for index, strategy in enumerate(("minimal", "robust"), start=1):
        workspace = manager.create(task.task_id, f"patch-{index}")
        model = FakeModelAdapter(
            [
                {
                    "thought_summary": "检查目标代码",
                    "action": {
                        "type": "tool",
                        "tool_name": "inspect_code",
                        "arguments": {"file_path": "target.py"},
                    },
                },
                {
                    "thought_summary": "完成候选补丁",
                    "action": {
                        "type": "final",
                        "status": "success",
                        "reason": "补丁已应用",
                        "artifact": {
                            "file_path": "target.py",
                            "old_text": "    return True",
                            "new_text": replacements[strategy],
                            "rationale": f"{strategy} 修复终态检查",
                            "risk_notes": [],
                        },
                    },
                },
            ]
        )
        patch = PatchAgent(
            model,
            build_workspace_tool_registry(task, workspace),
            workspace,
        ).run(task, f"N{index + 5}", strategy, "生成候选修复", artifacts)
        patches.append((patch, workspace))

    assert patches[0][0].content["workspace_id"] != patches[1][0].content["workspace_id"]
    assert "# all opens" not in (patches[0][1].root / "target.py").read_text()
    assert "# all opens" in (patches[1][1].root / "target.py").read_text()
    assert "return True" in (source / "target.py").read_text()
    assert (source / "tests" / "test_target.py").read_text() == "# protected\n"
    for patch, _workspace in patches:
        validate_worker_artifact(patch, expected_type=ArtifactType.PATCH_CANDIDATE)
        assert patch.content["protected_path_check"] is True
        assert len(patch.content["diff_sha256"]) == 64


class _AdaptiveInvestigatorModel(ModelAdapter):
    def __init__(self) -> None:
        super().__init__("adaptive-investigator")
        self.calls = 0

    def _generate_once(
        self,
        messages: Sequence[Message],
        config: GenerationConfig,
    ) -> RawGeneration:
        del config
        self.calls += 1
        if self.calls == 1:
            value = {
                "thought_summary": "检查文件",
                "action": {
                    "type": "tool",
                    "tool_name": "inspect_code",
                    "arguments": {"file_path": "target.py"},
                },
            }
        else:
            tool_message = next(
                item.content
                for item in reversed(messages)
                if item.content.startswith("工具执行结果：")
            )
            trace_id = json.loads(tool_message.removeprefix("工具执行结果："))["trace_id"]
            value = {
                "thought_summary": "返回证据",
                "action": {
                    "type": "final",
                    "status": "success",
                    "reason": "完成",
                    "artifact": {
                        **_evidence().to_dict()["content"],
                        "tool_trace_ids": [trace_id],
                    },
                },
            }
        text = json.dumps(value, ensure_ascii=False)
        return RawGeneration(text, 10, max(len(text) // 4, 1))


def test_worker_pool_returns_only_artifact_without_mutating_node_or_repository(
    tmp_path: Path,
) -> None:
    source = tmp_path / "pool-source"
    source.mkdir()
    original = "def target():\n    return True\n"
    (source / "target.py").write_text(original, encoding="utf-8")
    task = _task(source)
    node = TaskNode(
        "N1",
        NodeType.INVESTIGATION_TASK,
        "InvestigatorAgent",
        "code_retrieval",
        "读取目标实现",
        status=NodeStatus.RUNNING,
    )
    node_before = node.to_dict()
    pool = WorkerPool(
        (_AdaptiveInvestigatorModel(),),
        workspace_root=tmp_path / "pool-workspaces",
    )

    outcome = pool.execute(task=task.to_dict(), node=node.to_dict(), artifacts=())

    assert outcome.status is NodeStatus.SUCCEEDED
    assert len(outcome.artifacts) == 1
    assert outcome.artifacts[0].artifact_type is ArtifactType.EVIDENCE
    assert node.to_dict() == node_before
    assert (source / "target.py").read_text(encoding="utf-8") == original
    task_workspace_root = tmp_path / "pool-workspaces" / task.task_id
    assert not task_workspace_root.exists() or not any(task_workspace_root.iterdir())
