from __future__ import annotations

import hashlib
from pathlib import Path

import repo_pilot_mas.orchestration.validation as validation_module
from repo_pilot_mas.agents import DiagnosticianAgent, WorkerPool
from repo_pilot_mas.models import FakeModelAdapter
from repo_pilot_mas.orchestration import NodeStatus, NodeType, TaskNode
from repo_pilot_mas.orchestration.validation import ValidationExecutor
from repo_pilot_mas.runtime import WorkspaceManager
from repo_pilot_mas.schemas import Artifact, ArtifactType, TaskSpec, validate_worker_artifact
from repo_pilot_mas.tools import collect_diff


def _task(source: Path) -> TaskSpec:
    return TaskSpec(
        "phase5-workers",
        source,
        "除数为零时应返回零，同时保留正常除法",
        failing_tests=("tests/test_target.py",),
        protected_paths=("tests",),
        test_command=("python", "-m", "pytest", "-q"),
    )


def _evidence() -> Artifact:
    return Artifact(
        "N1.evidence",
        ArtifactType.EVIDENCE,
        "N1",
        {
            "mode": "code_retrieval",
            "evidence_kind": "source",
            "claim": "safe_divide 未处理零除数",
            "supports_claims": ["safe_divide 未处理零除数"],
            "contradicts_claims": [],
            "verified": True,
            "source": {"path": "calculator.py", "line_start": 1, "line_end": 2},
            "content": "函数直接执行 a / b。",
            "observation_type": "direct",
            "confidence": 1.0,
            "status": "verified",
            "tool_trace_ids": ["inspect-1"],
            "missing_evidence": [],
        },
    )


def _hypothesis(evidence: Artifact) -> Artifact:
    return Artifact(
        "N2.hypothesis",
        ArtifactType.HYPOTHESIS,
        "N2",
        {
            "perspective": "control_flow",
            "root_cause": "缺少零除数分支",
            "direct_cause": "所有输入都执行除法表达式",
            "supporting_evidence": [evidence.ref],
            "counter_evidence": [],
            "affected_symbols": ["safe_divide"],
            "verification_plan": ["运行零除数和正常除法测试"],
            "missing_evidence": [],
            "confidence": 0.8,
        },
        input_refs=(evidence.ref,),
    )


def test_challenge_and_rebuttal_create_versioned_hypothesis(tmp_path: Path) -> None:
    evidence = _evidence()
    hypothesis = _hypothesis(evidence)
    challenge_model = FakeModelAdapter(
        [
            {
                "challenged_hypothesis_ref": hypothesis.ref,
                "challenged_claim": "只需增加零除数分支",
                "insufficiency_reason": "尚未证明正常除法不会被改坏",
                "counterexample": "直接返回零会让 6/2 错误",
                "alternative_causal_chain": "过宽修复破坏非零输入语义",
                "required_evidence": ["正常除法回归测试"],
                "severity": "blocking",
            }
        ]
    )
    challenge = DiagnosticianAgent(challenge_model).challenge(
        _task(tmp_path), "N3", "验证根因边界", (hypothesis, evidence)
    )
    validate_worker_artifact(
        challenge,
        expected_type=ArtifactType.CHALLENGE,
        allowed_input_refs=(hypothesis.ref, evidence.ref),
    )

    revised_content = hypothesis.to_dict()["content"]
    revised_content["root_cause"] = "缺少仅针对零除数的窄化分支"
    revised_content["verification_plan"] = ["同时运行零除数目标测试和正常除法回归"]
    rebuttal_model = FakeModelAdapter(
        [
            {
                "response_to": challenge.ref,
                "defended_hypothesis_ref": hypothesis.ref,
                "decision": "partial_accept",
                "new_evidence_refs": [evidence.ref],
                "counterexample_explanation": "反例成立，补丁不能改写非零路径",
                "revision_summary": "将根因和验证计划窄化到零除数分支",
                "resulting_hypothesis": revised_content,
            }
        ]
    )
    outputs = DiagnosticianAgent(rebuttal_model).rebuttal(
        _task(tmp_path), "N4", "回应质疑", (hypothesis, challenge, evidence)
    )
    assert [item.artifact_type for item in outputs] == [
        ArtifactType.HYPOTHESIS,
        ArtifactType.REBUTTAL,
    ]
    revised, rebuttal = outputs
    assert revised.version == 2
    assert revised.supersedes == hypothesis.ref
    assert rebuttal.content["resulting_hypothesis_ref"] == revised.ref


def test_worker_pool_dispatches_challenge_and_rebuttal_modes(tmp_path: Path) -> None:
    evidence = _evidence()
    hypothesis = _hypothesis(evidence)
    challenge_content = {
        "challenged_hypothesis_ref": hypothesis.ref,
        "challenged_claim": "缺少零除数分支",
        "insufficiency_reason": "未验证正常路径",
        "counterexample": "无条件返回零破坏 6/2",
        "alternative_causal_chain": "补丁范围过宽导致回归",
        "required_evidence": ["正常除法回归"],
        "severity": "blocking",
    }
    pool = WorkerPool(
        (FakeModelAdapter([challenge_content]),),
        workspace_root=tmp_path / "workspaces",
    )
    challenge_node = TaskNode(
        "N3",
        NodeType.CHALLENGE_TASK,
        "DiagnosticianAgent",
        "challenge",
        "质疑根因边界",
        input_artifact_ids=(hypothesis.ref, evidence.ref),
    )
    outcome = pool.execute(
        task=_task(tmp_path).to_dict(),
        node=challenge_node.to_dict(),
        artifacts=(hypothesis.to_dict(), evidence.to_dict()),
    )
    assert outcome.status is NodeStatus.SUCCEEDED
    challenge = outcome.artifacts[0]

    revised_content = hypothesis.to_dict()["content"]
    revised_content["root_cause"] = "缺少只覆盖零除数的分支"
    revised_content["verification_plan"] = ["运行目标测试和完整回归"]
    rebuttal_pool = WorkerPool(
        (
            FakeModelAdapter(
                [
                    {
                        "response_to": challenge.ref,
                        "defended_hypothesis_ref": hypothesis.ref,
                        "decision": "partial_accept",
                        "new_evidence_refs": [evidence.ref],
                        "counterexample_explanation": "接受正常路径反例",
                        "revision_summary": "窄化修复边界",
                        "resulting_hypothesis": revised_content,
                    }
                ]
            ),
        ),
        workspace_root=tmp_path / "workspaces",
    )
    rebuttal_node = TaskNode(
        "N4",
        NodeType.REBUTTAL_TASK,
        "DiagnosticianAgent",
        "rebuttal",
        "回应针对自身根因的质疑",
        input_artifact_ids=(hypothesis.ref, challenge.ref, evidence.ref),
    )
    rebuttal_outcome = rebuttal_pool.execute(
        task=_task(tmp_path).to_dict(),
        node=rebuttal_node.to_dict(),
        artifacts=(hypothesis.to_dict(), challenge.to_dict(), evidence.to_dict()),
    )
    assert rebuttal_outcome.status is NodeStatus.SUCCEEDED
    assert [item.artifact_type for item in rebuttal_outcome.artifacts] == [
        ArtifactType.HYPOTHESIS,
        ArtifactType.REBUTTAL,
    ]


def test_patch_agents_cross_review_the_opposite_strategy(tmp_path: Path) -> None:
    evidence = _evidence()
    hypothesis = _hypothesis(evidence)

    def patch(node_id: str, strategy: str, digest_char: str) -> Artifact:
        return Artifact(
            f"{node_id}.patch",
            ArtifactType.PATCH_CANDIDATE,
            node_id,
            {
                "strategy": strategy,
                "based_on_hypothesis": hypothesis.ref,
                "diff": f"--- a/calculator.py\n+++ b/calculator.py\n+{strategy}\n",
                "diff_sha256": digest_char * 64,
                "changed_files": ["calculator.py"],
                "rationale": f"{strategy} 候选",
                "risk_notes": [],
                "protected_path_check": True,
                "workspace_id": f"{strategy}-workspace",
            },
            input_refs=(hypothesis.ref, evidence.ref),
        )

    minimal = patch("N5", "minimal", "a")
    robust = patch("N6", "robust", "b")
    review_content = {
        "mode": "patch_review",
        "target_artifact_ref": robust.ref,
        "evidence_refs": [evidence.ref],
        "verdict": "approved",
        "findings": ["未发现无证据的接口变化"],
        "risk_notes": ["仍需真实回归"],
        "recommendation": "进入 Reviewer 汇总",
        "failure_explained": True,
        "causal_chain_complete": True,
        "alternative_causes": ["排除了只修目标用例"],
        "counterexample_checked": True,
        "verification_steps_executed": ["对照 diff 与失败行为"],
        "remaining_uncertainty": [],
    }
    pool = WorkerPool(
        (FakeModelAdapter([review_content]),),
        workspace_root=tmp_path / "workspaces",
    )
    node = TaskNode(
        "N7",
        NodeType.REVIEW_TASK,
        "PatchAgent",
        "minimal_critiques_robust",
        "Minimal 侧审查 Robust",
        input_artifact_ids=(minimal.ref, robust.ref, evidence.ref),
    )
    outcome = pool.execute(
        task=_task(tmp_path).to_dict(),
        node=node.to_dict(),
        artifacts=(minimal.to_dict(), robust.to_dict(), evidence.to_dict()),
    )
    assert outcome.status is NodeStatus.SUCCEEDED
    assert outcome.artifacts[0].content["target_artifact_ref"] == robust.ref


def test_real_validation_eliminates_patch_that_only_passes_target(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    static_calls: list[bool] = []
    real_static_check = validation_module.static_check

    def syntax_only_static_check(*args, **kwargs):  # type: ignore[no-untyped-def]
        static_calls.append(kwargs.get("run_ruff"))
        return real_static_check(*args, **kwargs)

    monkeypatch.setattr(validation_module, "static_check", syntax_only_static_check)
    source = tmp_path / "source"
    source.mkdir()
    (source / "calculator.py").write_text(
        "def safe_divide(a, b):\n    return a / b\n", encoding="utf-8"
    )
    tests = source / "tests"
    tests.mkdir()
    (tests / "test_target.py").write_text(
        "from calculator import safe_divide\n\n\ndef test_zero():\n    assert safe_divide(1, 0) == 0\n",
        encoding="utf-8",
    )
    (tests / "test_regression.py").write_text(
        "from calculator import safe_divide\n\n\ndef test_nonzero():\n    assert safe_divide(6, 2) == 3\n",
        encoding="utf-8",
    )
    task = _task(source)
    evidence = _evidence()
    hypothesis = _hypothesis(evidence)
    manager = WorkspaceManager(source, tmp_path / "workspaces")
    validations = []
    for index, (strategy, replacement) in enumerate(
        (
            ("minimal", "def safe_divide(a, b):\n    return 0\n"),
            (
                "robust",
                "def safe_divide(a, b):\n    if b == 0:\n        return 0\n    return a / b\n",
            ),
        ),
        start=1,
    ):
        workspace = manager.create(task.task_id, f"patch-{index}")
        (workspace.root / "calculator.py").write_text(replacement, encoding="utf-8")
        result = collect_diff(workspace, protected_paths=task.protected_paths)
        diff = str(result.data["diff"])
        patch = Artifact(
            f"N{index + 4}.patch",
            ArtifactType.PATCH_CANDIDATE,
            f"N{index + 4}",
            {
                "strategy": strategy,
                "based_on_hypothesis_refs": [hypothesis.ref],
                "primary_hypothesis_ref": hypothesis.ref,
                "covered_root_causes": {
                    hypothesis.ref: hypothesis.content["root_cause"]
                },
                "diff": diff,
                "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
                "changed_files": ["calculator.py"],
                "rationale": "验证候选修复",
                "semantic_rationale": "零除输入需要显式保护，同时保持非零除法行为",
                "pre_patch_behavior": "零除输入直接执行除法并抛出异常",
                "post_patch_expected_behavior": "零除输入返回约定值，非零输入保持除法语义",
                "failure_input_walkthrough": "safe_divide(1, 0) 进入除法表达式并触发异常",
                "precheck_command": ["static_check", ".", "--no-ruff"],
                "precheck_result": {
                    "ok": True,
                    "trace_id": "precheck-trace",
                    "exit_code": 0,
                    "output_tail": "",
                },
                "risk_notes": [],
                "protected_path_check": True,
                "workspace_id": workspace.workspace_id,
            },
            input_refs=(hypothesis.ref, evidence.ref),
        )
        patch_review = Artifact(
            f"R{index}.review",
            ArtifactType.REVIEW,
            f"R{index}",
            {
                "mode": "patch_review",
                "target_artifact_ref": patch.ref,
                "evidence_refs": [evidence.ref],
                "verdict": "supported",
                "findings": ["补丁覆盖已接受根因"],
                "risk_notes": [],
                "recommendation": "进入确定性验证",
                "failure_explained": True,
                "causal_chain_complete": True,
                "alternative_causes": ["排除了测试配置问题"],
                "counterexample_checked": True,
                "verification_steps_executed": ["核对补丁与失败输入"],
                "remaining_uncertainty": [],
            },
            input_refs=(patch.ref, hypothesis.ref, evidence.ref),
        )
        if strategy == "minimal":
            validation = ValidationExecutor(str(tmp_path / "workspaces")).run(
                task, f"N{index + 6}", (patch, patch_review)
            )
        else:
            pool = WorkerPool(
                (FakeModelAdapter([]),),
                workspace_root=tmp_path / "workspaces",
            )
            validation_node = TaskNode(
                "N9",
                NodeType.VALIDATION_TASK,
                "ValidationExecutor",
                "full_validation",
                "通过 WorkerPool 运行确定性验证",
                input_artifact_ids=(patch.ref, patch_review.ref),
            )
            outcome = pool.execute(
                task=task.to_dict(),
                node=validation_node.to_dict(),
                artifacts=(patch.to_dict(), patch_review.to_dict()),
            )
            assert outcome.status is NodeStatus.SUCCEEDED
            validation = outcome.artifacts[0]
        validations.append(validation)

    wrong, correct = validations
    assert wrong.content["target_test"]["exit_code"] == 0
    assert wrong.content["regression_test"]["exit_code"] != 0
    assert wrong.content["failure_class"] == "regression_failure"
    assert wrong.content["passed"] is False
    assert wrong.content["recommended_stage"] == "patch"
    assert wrong.content["invalidated_refs"]
    assert wrong.content["recoverable"] is True
    assert correct.content["passed"] is True, correct.to_dict()["content"]
    assert correct.content["recommended_stage"] == "completed"
    assert correct.content["invalidated_refs"] == ()
    assert correct.content["recoverable"] is False
    assert static_calls == [False, False]

    assert not (tmp_path / "workspaces" / task.task_id / "patch-1").exists()
    assert not (tmp_path / "workspaces" / task.task_id / "patch-2").exists()
