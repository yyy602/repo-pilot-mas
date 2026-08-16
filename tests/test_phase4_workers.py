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
from repo_pilot_mas.runtime import TraceWriter, WorkspaceManager
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
            "evidence_kind": "source",
            "claim": "函数结束时没有检查 depth 是否回到零",
            "supports_claims": ["函数结束时没有检查 depth 是否回到零"],
            "contradicts_claims": [],
            "verified": True,
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


def _patch_draft(
    new_text: str,
    rationale: str,
    *,
    file_path: str = "target.py",
    old_text: str = "    return True",
) -> dict[str, object]:
    return {
        "file_path": file_path,
        "old_text": old_text,
        "new_text": new_text,
        "rationale": rationale,
        "pre_patch_behavior": "失败输入执行旧返回语句并得到错误结果",
        "post_patch_expected_behavior": "失败输入执行新返回语句并得到正确结果",
        "failure_input_walkthrough": "失败输入走到返回语句，旧条件恒真；新条件检查终态不变量",
        "semantic_rationale": rationale,
        "risk_notes": [],
    }


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


def test_evidence_review_target_is_constrained_to_input_evidence(
    tmp_path: Path,
) -> None:
    evidence = _evidence()
    model = FakeModelAdapter(
        [
            {
                "mode": "evidence_review",
                "target_artifact_ref": evidence.ref,
                "evidence_refs": [evidence.ref],
                "verdict": "needs_more_evidence",
                "findings": ["源码证据存在，但尚未复现真实失败"],
                "risk_notes": ["单独源码观察不足以接受根因"],
                "recommendation": "先补充失败复现 Evidence",
                "failure_explained": False,
                "causal_chain_complete": False,
                "alternative_causes": ["测试配置可能影响观察"],
                "counterexample_checked": False,
                "verification_steps_executed": ["核对源码定位"],
                "remaining_uncertainty": ["真实失败输出未知"],
            }
        ]
    )

    review = ReviewerAgent(model).run(
        _task(tmp_path),
        "N2",
        "evidence_review",
        "检查现有证据是否充分",
        (evidence,),
    )

    assert review.content["target_artifact_ref"] == evidence.ref


def test_reviewer_conservatively_downgrades_inconsistent_acceptable_verdict(
    tmp_path: Path,
) -> None:
    evidence = _evidence()
    hypothesis = _hypothesis()
    model = FakeModelAdapter(
        [
            {
                "mode": "root_cause_recommendation",
                "target_artifact_ref": hypothesis.ref,
                "evidence_refs": [evidence.ref],
                "verdict": "supported",
                "findings": ["根因大体符合证据"],
                "risk_notes": ["仍有未解决的边界问题"],
                "recommendation": "先补证再接受",
                "failure_explained": True,
                "causal_chain_complete": True,
                "alternative_causes": ["可能存在第二个边界错误"],
                "counterexample_checked": True,
                "verification_steps_executed": ["核对失败路径"],
                "remaining_uncertainty": ["空数组路径尚未确认"],
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

    assert review.content["verdict"] == "needs_more_evidence"
    assert review.content["remaining_uncertainty"] == ("空数组路径尚未确认",)


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
                "failure_explained": True,
                "causal_chain_complete": True,
                "alternative_causes": ["排除了字符分类错误"],
                "counterexample_checked": True,
                "verification_steps_executed": ["核对未闭合输入的控制流"],
                "remaining_uncertainty": [],
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


def test_hypothesis_comparison_schema_repairs_non_hypothesis_target(
    tmp_path: Path,
) -> None:
    evidence = _evidence()
    first = _hypothesis("N3")
    second = _hypothesis("N4")
    common = {
        "mode": "hypothesis_comparison",
        "evidence_refs": [evidence.ref],
        "verdict": "supported",
        "findings": ["第二个假设更完整地解释边界失败"],
        "risk_notes": [],
        "recommendation": "推荐第二个假设",
        "failure_explained": True,
        "causal_chain_complete": True,
        "alternative_causes": ["已排除测试配置问题"],
        "counterexample_checked": True,
        "verification_steps_executed": ["逐项核对两个因果链"],
        "remaining_uncertainty": [],
    }
    invalid = {
        **common,
        "target_artifact_ref": evidence.ref,
    }
    valid = {
        **common,
        "target_artifact_ref": second.ref,
        "target_artifact_refs": [first.ref, second.ref],
    }

    review = ReviewerAgent(
        FakeModelAdapter([invalid, valid]),
        generation_config=GenerationConfig(max_retries=1),
    ).run(
        _task(tmp_path),
        "N5",
        "hypothesis_comparison",
        "比较两个候选根因",
        (evidence, first, second),
    )

    assert review.content["target_artifact_ref"] == second.ref
    assert set(review.content["target_artifact_refs"]) == {first.ref, second.ref}


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
    (source / "tests" / "test_target.py").write_text(
        "from target import is_valid_parenthesization\n\n"
        "def test_balanced_is_accepted():\n"
        "    assert is_valid_parenthesization('()') is True\n",
        encoding="utf-8",
    )
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
                        "artifact": _patch_draft(
                            replacements[strategy],
                            f"{strategy} 修复终态检查",
                        ),
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
    assert "test_balanced_is_accepted" in (
        source / "tests" / "test_target.py"
    ).read_text()
    for patch, _workspace in patches:
        validate_worker_artifact(patch, expected_type=ArtifactType.PATCH_CANDIDATE)
        assert patch.content["protected_path_check"] is True
        assert len(patch.content["diff_sha256"]) == 64


def test_patch_agent_uses_target_precheck_to_correct_failed_draft(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.py").write_text(
        "def is_valid_parenthesization(parens):\n"
        "    depth = 0\n"
        "    for paren in parens:\n"
        "        depth += 1 if paren == '(' else -1\n"
        "    return True\n",
        encoding="utf-8",
    )
    tests = source / "tests"
    tests.mkdir()
    (tests / "test_target.py").write_text(
        "from target import is_valid_parenthesization\n\n"
        "def test_unclosed_is_rejected():\n"
        "    assert is_valid_parenthesization('(') is False\n",
        encoding="utf-8",
    )
    task = _task(source)
    workspace = WorkspaceManager(
        source,
        tmp_path / "workspaces",
    ).create(task.task_id, "target-precheck")
    model = FakeModelAdapter(
        [
            {
                "thought_summary": "检查首个候选",
                "action": {
                    "type": "tool",
                    "tool_name": "inspect_code",
                    "arguments": {"file_path": "target.py"},
                }
            },
            {
                "thought_summary": "提交首个候选",
                "action": {
                    "type": "final",
                    "status": "success",
                    "reason": "首个候选",
                    "artifact": _patch_draft(
                        "    return depth >= 0",
                        "尝试检查深度",
                    ),
                }
            },
            {
                "thought_summary": "根据测试失败重新检查",
                "action": {
                    "type": "tool",
                    "tool_name": "inspect_code",
                    "arguments": {"file_path": "target.py"},
                }
            },
            {
                "thought_summary": "提交修正候选",
                "action": {
                    "type": "final",
                    "status": "success",
                    "reason": "根据失败测试修正",
                    "artifact": _patch_draft(
                        "    return depth == 0",
                        "未闭合括号必须留下非零深度",
                    ),
                }
            },
        ]
    )

    patch = PatchAgent(
        model,
        build_workspace_tool_registry(task, workspace),
        workspace,
    ).run(
        task,
        "N6",
        "minimal",
        "生成并预检候选修复",
        (_hypothesis(), _evidence()),
    )

    assert patch.content["precheck_result"]["ok"] is True
    assert patch.content["precheck_command"][-1] == "tests/test_target.py"
    assert "return depth == 0" in (
        workspace.root / "target.py"
    ).read_text(encoding="utf-8")


def test_patch_agent_reduces_unmatched_context_to_unique_changed_hunk(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.py").write_text(
        "def find_first(values, target):\n"
        "    lo = 0\n"
        "    hi = len(values)\n"
        "\n"
        "    while lo <= hi:\n"
        "        mid = (lo + hi) // 2\n"
        "\n"
        "        if values[mid] == target:\n"
        "            return mid\n"
        "        lo = mid + 1\n"
        "\n"
        "    return -1\n",
        encoding="utf-8",
    )
    tests = source / "tests"
    tests.mkdir()
    (tests / "test_target.py").write_text(
        "from target import find_first\n\n"
        "def test_above_range_returns_minus_one():\n"
        "    assert find_first([1, 2, 3], 4) == -1\n",
        encoding="utf-8",
    )
    task = TaskSpec(
        "patch-reduced-hunk",
        source,
        "目标高于数组范围时越界",
        failing_tests=("tests/test_target.py",),
        protected_paths=("tests",),
    )
    workspace = WorkspaceManager(source, tmp_path / "workspaces").create(
        task.task_id,
        "reduced-hunk",
    )
    old_text = (
        "    while lo <= hi:\n"
        "        mid = (lo + hi) // 2\n"
        "        if values[mid] == target:\n"
        "            return mid\n"
        "        lo = mid + 1"
    )
    new_text = old_text.replace("while lo <= hi:", "while lo < hi:")
    trace_path = tmp_path / "trace.jsonl"

    patch = PatchAgent(
        FakeModelAdapter(
            [
                {
                    "thought_summary": "读取完整函数",
                    "action": {
                        "type": "tool",
                        "tool_name": "inspect_code",
                        "arguments": {"file_path": "target.py"},
                    },
                },
                {
                    "thought_summary": "修复半开区间边界",
                    "action": {
                        "type": "final",
                        "status": "success",
                        "reason": "候选完成",
                        "artifact": _patch_draft(
                            new_text,
                            "保持 hi 为排他上界，避免 mid 等于 len(values)",
                            old_text=old_text,
                        ),
                    },
                },
            ]
        ),
        build_workspace_tool_registry(task, workspace),
        workspace,
        trace_writer=TraceWriter(trace_path),
    ).run(
        task,
        "N6",
        "minimal",
        "修复边界越界",
        (_hypothesis(), _evidence()),
    )

    assert patch.content["precheck_result"]["ok"] is True
    assert "while lo < hi:" in (workspace.root / "target.py").read_text(
        encoding="utf-8"
    )
    assert "patch_replacement_reduced" in trace_path.read_text(encoding="utf-8")


def test_patch_agent_preserves_target_test_failure_class(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.py").write_text(
        "def is_valid_parenthesization(parens):\n"
        "    depth = 0\n"
        "    for paren in parens:\n"
        "        depth += 1 if paren == '(' else -1\n"
        "    return True\n",
        encoding="utf-8",
    )
    tests = source / "tests"
    tests.mkdir()
    (tests / "test_target.py").write_text(
        "from target import is_valid_parenthesization\n\n"
        "def test_unclosed_is_rejected():\n"
        "    assert is_valid_parenthesization('(') is False\n",
        encoding="utf-8",
    )
    task = _task(source)
    workspace = WorkspaceManager(
        source,
        tmp_path / "workspaces",
    ).create(task.task_id, "target-precheck-failure")
    responses: list[dict[str, object]] = []
    for _ in range(3):
        responses.extend(
            [
                {
                    "thought_summary": "检查候选",
                    "action": {
                        "type": "tool",
                        "tool_name": "inspect_code",
                        "arguments": {"file_path": "target.py"},
                    },
                },
                {
                    "thought_summary": "提交仍不完整的候选",
                    "action": {
                        "type": "final",
                        "status": "success",
                        "reason": "候选可应用但目标测试仍失败",
                        "artifact": _patch_draft(
                            "    return depth >= 0",
                            "尝试检查深度",
                        ),
                    },
                },
            ]
        )

    with pytest.raises(WorkerAgentError) as caught:
        PatchAgent(
            FakeModelAdapter(responses),
            build_workspace_tool_registry(task, workspace),
            workspace,
        ).run(
            task,
            "N7",
            "minimal",
            "生成并预检候选修复",
            (_hypothesis(), _evidence()),
        )

    assert caught.value.code == "PATCH_TARGET_TEST_FAILED"
    assert "目标测试预检查失败" in str(caught.value)
    assert '"new_text":"    return depth >= 0"' in str(caught.value)
    assert "return True" in (workspace.root / "target.py").read_text(
        encoding="utf-8"
    )


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
