"""运行 Phase 5 确定性机制案例，并保存动态协作与真实测试证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repo_pilot_mas.agents import ScriptedSupervisor
from repo_pilot_mas.orchestration import (
    EngineBudget,
    EngineStatus,
    ExecutionPath,
    LangGraphRuntime,
    NodeStatus,
    NodeType,
    OrchestrationEngine,
    WorkerOutcome,
    classify_execution_path,
    hypotheses_materially_different,
    restore_engine_from_runtime_state,
)
from repo_pilot_mas.orchestration.validation import ValidationExecutor
from repo_pilot_mas.runtime import TraceWriter, WorkspaceManager
from repo_pilot_mas.schemas import (
    Artifact,
    ArtifactType,
    CreateTaskRequest,
    DecisionAction,
    GateRecord,
    SupervisorDecision,
    TaskSpec,
    validate_worker_artifact,
)
from repo_pilot_mas.tools import collect_diff


class MechanismWorker:
    """确定性 Worker 替身：固定认知输出，Patch 仍通过真实隔离工作区和测试验证。"""

    def __init__(self, workspace_root: Path, trace_writer: TraceWriter) -> None:
        self.workspace_root = workspace_root
        self.trace_writer = trace_writer

    def execute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        task_spec = TaskSpec.from_dict(task)
        inputs = tuple(Artifact.from_dict(item) for item in artifacts)
        node_id = str(node["node_id"])
        node_type = NodeType(str(node["node_type"]))
        mode = str(node["mode"])
        if node_type is NodeType.INVESTIGATION_TASK:
            outputs = (self._evidence(node_id, mode),)
        elif node_type is NodeType.DIAGNOSIS_TASK:
            outputs = (self._hypothesis(node_id, mode, inputs),)
        elif node_type is NodeType.CHALLENGE_TASK:
            outputs = (self._challenge(node_id, inputs),)
        elif node_type is NodeType.REBUTTAL_TASK:
            outputs = self._rebuttal(node_id, inputs)
        elif node_type is NodeType.REVIEW_TASK:
            outputs = (self._review(node_id, mode, inputs),)
        elif node_type is NodeType.PATCH_TASK:
            outputs = (self._patch(task_spec, node_id, mode, inputs),)
        elif node_type is NodeType.VALIDATION_TASK:
            outputs = (
                ValidationExecutor(
                    str(self.workspace_root), trace_writer=self.trace_writer
                ).run(task_spec, node_id, inputs),
            )
        else:
            return WorkerOutcome(
                node_id,
                NodeStatus.FAILED,
                reason=f"UNSUPPORTED_MECHANISM_NODE:{node_type.value}",
            )
        return WorkerOutcome(node_id, NodeStatus.SUCCEEDED, outputs)

    async def aexecute(
        self,
        *,
        task: Mapping[str, Any],
        node: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> WorkerOutcome:
        return self.execute(task=task, node=node, artifacts=artifacts)

    @staticmethod
    def _evidence(node_id: str, mode: str) -> Artifact:
        reproduction = mode == "failure_reproduction"
        return Artifact(
            f"{node_id}.evidence",
            ArtifactType.EVIDENCE,
            node_id,
            {
                "mode": mode,
                "claim": (
                    "零除数目标测试稳定触发 ZeroDivisionError"
                    if reproduction
                    else "safe_divide 对所有输入直接执行 a / b"
                ),
                "source": {
                    "path": "tests/test_target.py" if reproduction else "calculator.py",
                    "line_start": 1,
                    "line_end": 5 if reproduction else 2,
                },
                "content": (
                    "pytest 目标用例退出码非零，异常来自 calculator.safe_divide。"
                    if reproduction
                    else "函数没有 b == 0 的控制流分支。"
                ),
                "observation_type": "direct",
                "confidence": 1.0,
                "status": "verified",
                "tool_trace_ids": [f"{node_id}-tool-trace"],
                "missing_evidence": [],
            },
        )

    @staticmethod
    def _hypothesis(node_id: str, perspective: str, inputs: Sequence[Artifact]) -> Artifact:
        evidence_refs = [item.ref for item in inputs if item.artifact_type is ArtifactType.EVIDENCE]
        data_flow = perspective == "data_flow"
        return Artifact(
            f"{node_id}.hypothesis",
            ArtifactType.HYPOTHESIS,
            node_id,
            {
                "perspective": perspective,
                "root_cause": (
                    "接口契约没有把零除数建模为可恢复结果"
                    if data_flow
                    else "控制流缺少仅处理零除数的窄化分支"
                ),
                "direct_cause": (
                    "异常值没有在除法前转换为领域返回值"
                    if data_flow
                    else "b == 0 时仍进入除法表达式"
                ),
                "supporting_evidence": evidence_refs,
                "counter_evidence": ["非零输入必须继续执行真实除法"],
                "affected_symbols": ["safe_divide.contract" if data_flow else "safe_divide"],
                "verification_plan": ["同时运行零除数目标测试和正常除法回归"],
                "missing_evidence": [],
                "confidence": 0.78 if data_flow else 0.86,
            },
            input_refs=tuple(item.ref for item in inputs),
        )

    @staticmethod
    def _challenge(node_id: str, inputs: Sequence[Artifact]) -> Artifact:
        hypothesis = next(
            item for item in inputs if item.artifact_type is ArtifactType.HYPOTHESIS
        )
        return Artifact(
            f"{node_id}.challenge",
            ArtifactType.CHALLENGE,
            node_id,
            {
                "challenged_hypothesis_ref": hypothesis.ref,
                "challenged_claim": str(hypothesis.content["root_cause"]),
                "insufficiency_reason": "当前因果链没有证明修复范围不会覆盖正常除法路径",
                "counterexample": "无条件返回零可通过目标测试，却会令 safe_divide(6, 2) 失败",
                "alternative_causal_chain": "真正风险来自补丁作用域过宽，而不只是异常未捕获",
                "required_evidence": ["正常除法完整回归结果", "Patch changed-lines 证据"],
                "severity": "blocking",
            },
            input_refs=tuple(item.ref for item in inputs),
        )

    @staticmethod
    def _rebuttal(node_id: str, inputs: Sequence[Artifact]) -> tuple[Artifact, ...]:
        hypothesis = next(
            item for item in inputs if item.artifact_type is ArtifactType.HYPOTHESIS
        )
        challenge = next(item for item in inputs if item.artifact_type is ArtifactType.CHALLENGE)
        evidence_refs = [item.ref for item in inputs if item.artifact_type is ArtifactType.EVIDENCE]
        revised_content = hypothesis.to_dict()["content"]
        revised_content["root_cause"] = (
            f"{revised_content['root_cause']}，且修复必须保持非零路径语义"
        )
        revised_content["verification_plan"] = [
            "运行零除数目标测试",
            "运行包含正常除法的完整回归",
            "核对补丁只增加 b == 0 分支",
        ]
        revised_content["confidence"] = 0.92
        revised = Artifact(
            hypothesis.artifact_id,
            ArtifactType.HYPOTHESIS,
            node_id,
            revised_content,
            version=hypothesis.version + 1,
            supersedes=hypothesis.ref,
            input_refs=tuple(item.ref for item in inputs),
        )
        rebuttal = Artifact(
            f"{node_id}.rebuttal",
            ArtifactType.REBUTTAL,
            node_id,
            {
                "response_to": challenge.ref,
                "defended_hypothesis_ref": hypothesis.ref,
                "decision": "partial_accept",
                "new_evidence_refs": evidence_refs,
                "resulting_hypothesis_ref": revised.ref,
                "counterexample_explanation": "反例成立，目标测试不能替代正常路径回归",
                "revision_summary": "接受作用域质疑并增加非零路径不变量",
            },
            input_refs=tuple(item.ref for item in inputs) + (revised.ref,),
        )
        return revised, rebuttal

    @staticmethod
    def _review(node_id: str, mode: str, inputs: Sequence[Artifact]) -> Artifact:
        evidence_refs = [item.ref for item in inputs if item.artifact_type is ArtifactType.EVIDENCE]
        targets = [
            item
            for item in inputs
            if item.artifact_type in {ArtifactType.HYPOTHESIS, ArtifactType.PATCH_CANDIDATE}
        ]
        if mode == "minimal_critiques_robust":
            target = next(item for item in targets if item.content["strategy"] == "robust")
        elif mode == "robust_critiques_minimal":
            target = next(item for item in targets if item.content["strategy"] == "minimal")
        else:
            target = targets[0]
        is_cross_review = mode in {
            "minimal_critiques_robust",
            "robust_critiques_minimal",
        }
        is_consolidation = mode == "patch_review" and len(targets) == 2
        return Artifact(
            f"{node_id}.review",
            ArtifactType.REVIEW,
            node_id,
            {
                "mode": "patch_review" if is_cross_review else mode,
                "target_artifact_ref": target.ref,
                "evidence_refs": evidence_refs,
                "verdict": "approved" if mode == "patch_review" else "supported",
                "findings": [
                    (
                        "Minimal 侧确认 Robust 没有无证据重构或公共接口变化"
                        if mode == "minimal_critiques_robust"
                        else (
                            "Robust 侧指出 Minimal 可能只硬编码零除数目标并破坏正常路径"
                            if mode == "robust_critiques_minimal"
                            else (
                                "Reviewer 汇总双方 Patch 交叉审查，真实测试保留最终否决权"
                                if is_consolidation
                                else "修订根因回应了双方 Challenge，并与直接 Evidence 一致"
                            )
                        )
                    )
                ],
                "risk_notes": ["仍需完整回归排除补丁作用域过宽"],
                "recommendation": (
                    "允许进入 Reviewer 汇总与真实验证"
                    if is_cross_review
                    else (
                        "双方意见已汇总；允许验证但不得跳过完整回归"
                        if is_consolidation
                        else "建议 Supervisor 接受修订后的控制流 Hypothesis"
                    )
                ),
            },
            input_refs=tuple(item.ref for item in inputs),
        )

    def _patch(
        self,
        task: TaskSpec,
        node_id: str,
        strategy: str,
        inputs: Sequence[Artifact],
    ) -> Artifact:
        hypothesis = next(
            item for item in inputs if item.artifact_type is ArtifactType.HYPOTHESIS
        )
        workspace = WorkspaceManager(task.repository_path, self.workspace_root).create(
            task.task_id, f"{node_id}-{strategy}"
        )
        replacement = (
            "def safe_divide(a, b):\n    return 0\n"
            if strategy == "minimal"
            else "def safe_divide(a, b):\n    if b == 0:\n        return 0\n    return a / b\n"
        )
        (workspace.root / "calculator.py").write_text(replacement, encoding="utf-8")
        collected = collect_diff(workspace, protected_paths=task.protected_paths)
        diff = str(collected.data["diff"])
        return Artifact(
            f"{node_id}.patch",
            ArtifactType.PATCH_CANDIDATE,
            node_id,
            {
                "strategy": strategy,
                "based_on_hypothesis": hypothesis.ref,
                "diff": diff,
                "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
                "changed_files": ["calculator.py"],
                "rationale": (
                    "仅满足当前零除数目标用例"
                    if strategy == "minimal"
                    else "增加窄化零除数分支并保留非零除法"
                ),
                "risk_notes": (
                    ["可能覆盖正常除法语义"] if strategy == "minimal" else []
                ),
                "protected_path_check": collected.ok,
                "workspace_id": workspace.workspace_id,
            },
            input_refs=tuple(item.ref for item in inputs),
        )


def _request(
    node_type: str,
    agent: str,
    mode: str,
    objective: str,
    *,
    depends_on: Sequence[str] = (),
    inputs: Sequence[str] = (),
) -> CreateTaskRequest:
    return CreateTaskRequest(
        node_type,
        agent,
        mode,
        objective,
        depends_on=tuple(depends_on),
        input_artifact_ids=tuple(inputs),
        timeout_seconds=120,
    )


def _gate(name: str, refs: Sequence[str], count: int, reason: str) -> GateRecord:
    return GateRecord(
        name,
        tuple(refs),
        reason,
        count,
        f"新增 {count} 个节点并占用 {count} 次 Worker 调用",
    )


def _deep_decisions() -> tuple[SupervisorDecision, ...]:
    e1, e2 = "N1.evidence@v1", "N2.evidence@v1"
    h1, h2 = "N3.hypothesis@v1", "N4.hypothesis@v1"
    c2, c1 = "N5.challenge@v1", "N6.challenge@v1"
    h1v2, h2v2 = "N3.hypothesis@v2", "N4.hypothesis@v2"
    r1, r2, review = "N7.rebuttal@v1", "N8.rebuttal@v1", "N9.review@v1"
    p1, p2 = "N10.patch@v1", "N11.patch@v1"
    v1, v2 = "N15.validation@v1", "N16.validation@v1"
    return (
        SupervisorDecision(
            "P5-D1-investigate",
            DecisionAction.CREATE_TASK,
            "从最小种子图开始，只创建一个代码调查节点",
            create_tasks=(
                _request(
                    "INVESTIGATION_TASK",
                    "InvestigatorAgent",
                    "code_retrieval",
                    "定位 safe_divide 的真实控制流",
                ),
            ),
            next_workflow_stage="investigation",
        ),
        SupervisorDecision(
            "P5-D2-expand-investigation",
            DecisionAction.CREATE_TASK,
            "单一代码来源不足以证明运行时失败，按门增加复现调查",
            create_tasks=(
                _request(
                    "INVESTIGATION_TASK",
                    "InvestigatorAgent",
                    "failure_reproduction",
                    "运行受信目标测试并记录稳定失败",
                    depends_on=("N1",),
                ),
            ),
            evidence_refs=(e1,),
            gate_record=_gate("additional_investigation", (e1,), 1, "只有静态代码来源"),
        ),
        SupervisorDecision(
            "P5-D3-first-diagnosis",
            DecisionAction.CREATE_TASK,
            "先形成控制流根因",
            create_tasks=(
                _request(
                    "DIAGNOSIS_TASK",
                    "DiagnosticianAgent",
                    "control_flow",
                    "基于两份 Evidence 独立分析控制流根因",
                    depends_on=("N2",),
                    inputs=(e1, e2),
                ),
            ),
            next_workflow_stage="diagnosis",
        ),
        SupervisorDecision(
            "P5-D4-expand-diagnosis",
            DecisionAction.CREATE_TASK,
            "控制流假设仍含接口契约反例，增加独立数据流诊断",
            create_tasks=(
                _request(
                    "DIAGNOSIS_TASK",
                    "DiagnosticianAgent",
                    "data_flow",
                    "从接口契约和数据流独立形成第二根因",
                    depends_on=("N3",),
                    inputs=(e1, e2),
                ),
            ),
            evidence_refs=(e1, e2, h1),
            gate_record=_gate(
                "additional_diagnosis", (e1, e2, h1), 1, "存在控制流与契约两条因果链"
            ),
        ),
        SupervisorDecision(
            "P5-D5-bidirectional-challenge",
            DecisionAction.CREATE_TASK,
            "两个实质不同根因需要双向质询",
            create_tasks=(
                _request(
                    "CHALLENGE_TASK",
                    "DiagnosticianAgent",
                    "challenge",
                    "控制流诊断者质疑数据流 Hypothesis",
                    depends_on=("N4",),
                    inputs=(h2, e1, e2),
                ),
                _request(
                    "CHALLENGE_TASK",
                    "DiagnosticianAgent",
                    "challenge",
                    "数据流诊断者质疑控制流 Hypothesis",
                    depends_on=("N4",),
                    inputs=(h1, e1, e2),
                ),
            ),
            evidence_refs=(h1, h2),
            gate_record=_gate("adversarial_review", (h1, h2), 2, "根因和受影响符号均不同"),
        ),
        SupervisorDecision(
            "P5-D6-one-round-rebuttal",
            DecisionAction.CREATE_TASK,
            "双方各进行一次回应并允许版本化修订",
            create_tasks=(
                _request(
                    "REBUTTAL_TASK",
                    "DiagnosticianAgent",
                    "rebuttal",
                    "控制流诊断者回应针对自身的 Challenge",
                    depends_on=("N5", "N6"),
                    inputs=(h1, c1, e1, e2),
                ),
                _request(
                    "REBUTTAL_TASK",
                    "DiagnosticianAgent",
                    "rebuttal",
                    "数据流诊断者回应针对自身的 Challenge",
                    depends_on=("N5", "N6"),
                    inputs=(h2, c2, e1, e2),
                ),
            ),
            evidence_refs=(c1, c2),
            gate_record=_gate("adversarial_review", (c1, c2), 2, "两份 Challenge 均为 blocking"),
        ),
        SupervisorDecision(
            "P5-D7-root-review",
            DecisionAction.CREATE_TASK,
            "Reviewer 汇总证据、质询和回应，但不替代 Supervisor 决策",
            create_tasks=(
                _request(
                    "REVIEW_TASK",
                    "ReviewerAgent",
                    "root_cause_recommendation",
                    "审查修订根因及双方质询回应",
                    depends_on=("N7", "N8"),
                    inputs=(h1v2, h2v2, c1, c2, r1, r2, e1, e2),
                ),
            ),
        ),
        SupervisorDecision(
            "P5-D8-accept-root",
            DecisionAction.ACCEPT_HYPOTHESIS,
            "综合直接 Evidence、双方 Challenge/Rebuttal 与 Reviewer 建议，接受窄化控制流根因",
            evidence_refs=(e1, e2, c1, c2, r1, r2, review),
            hypothesis_ref=h1v2,
        ),
        SupervisorDecision(
            "P5-D9-dual-patch",
            DecisionAction.CREATE_TASK,
            "根因可信但存在最小改动与语义保持的取舍，生成两个隔离候选",
            create_tasks=(
                _request(
                    "PATCH_TASK",
                    "PatchAgent",
                    "minimal",
                    "生成只满足目标用例的最小候选以检验回归门",
                    depends_on=("N9",),
                    inputs=(h1v2, e1, e2),
                ),
                _request(
                    "PATCH_TASK",
                    "PatchAgent",
                    "robust",
                    "生成保持非零路径语义的稳健候选",
                    depends_on=("N9",),
                    inputs=(h1v2, e1, e2),
                ),
            ),
            next_workflow_stage="patch",
            evidence_refs=(h1v2, review),
            gate_record=_gate("dual_patch", (h1v2, review), 2, "存在作用域与稳健性取舍"),
        ),
        SupervisorDecision(
            "P5-D10-patch-review",
            DecisionAction.CREATE_TASK,
            "两个候选分别接受 Patch Review，真实测试保留最终否决权",
            create_tasks=(
                _request(
                    "REVIEW_TASK",
                    "PatchAgent",
                    "minimal_critiques_robust",
                    "Minimal 侧审查 Robust Patch 是否过度修改",
                    depends_on=("N10", "N11"),
                    inputs=(p1, p2, e1, e2),
                ),
                _request(
                    "REVIEW_TASK",
                    "PatchAgent",
                    "robust_critiques_minimal",
                    "Robust 侧审查 Minimal Patch 是否只修表面",
                    depends_on=("N10", "N11"),
                    inputs=(p1, p2, e1, e2),
                ),
            ),
        ),
        SupervisorDecision(
            "P5-D11-consolidate-patch-review",
            DecisionAction.CREATE_TASK,
            "Reviewer 汇总两侧 Patch 交叉审查中的 Blocking 风险",
            create_tasks=(
                _request(
                    "REVIEW_TASK",
                    "ReviewerAgent",
                    "patch_review",
                    "汇总 Minimal 与 Robust 双向审查",
                    depends_on=("N12", "N13"),
                    inputs=(p1, p2, "N12.review@v1", "N13.review@v1", e1, e2),
                ),
            ),
        ),
        SupervisorDecision(
            "P5-D12-validate",
            DecisionAction.CREATE_TASK,
            "对两个独立工作区运行目标测试、完整回归、静态检查和保护路径检查",
            create_tasks=(
                _request(
                    "VALIDATION_TASK",
                    "ValidationExecutor",
                    "full_validation",
                    "真实验证 Minimal Patch",
                    depends_on=("N14",),
                    inputs=(p1,),
                ),
                _request(
                    "VALIDATION_TASK",
                    "ValidationExecutor",
                    "full_validation",
                    "真实验证 Robust Patch",
                    depends_on=("N14",),
                    inputs=(p2,),
                ),
            ),
            next_workflow_stage="validation",
        ),
        SupervisorDecision(
            "P5-D13-select-patch",
            DecisionAction.SELECT_PATCH,
            "Minimal 虽通过目标测试但完整回归失败；选择所有真实门均通过的 Robust Patch",
            evidence_refs=(v1, v2),
            patch_ref=p2,
            validation_ref=v2,
        ),
        SupervisorDecision(
            "P5-D14-finalize",
            DecisionAction.FINALIZE_TASK,
            "所选 Robust Patch 已通过真实目标测试、完整回归、静态和保护路径检查",
            evidence_refs=(v2,),
            patch_ref=p2,
            validation_ref=v2,
        ),
    )


def _simple_decisions() -> tuple[SupervisorDecision, ...]:
    e1, h1, patch, validation = (
        "N1.evidence@v1",
        "N2.hypothesis@v1",
        "N3.patch@v1",
        "N4.validation@v1",
    )
    return (
        SupervisorDecision(
            "P5-S1-investigate",
            DecisionAction.CREATE_TASK,
            "简单任务只创建一个调查节点",
            create_tasks=(
                _request(
                    "INVESTIGATION_TASK",
                    "InvestigatorAgent",
                    "code_retrieval",
                    "定位缺少的零除数分支",
                ),
            ),
            next_workflow_stage="investigation",
        ),
        SupervisorDecision(
            "P5-S2-diagnose",
            DecisionAction.CREATE_TASK,
            "直接证据充分，不增加额外 Investigator",
            create_tasks=(
                _request(
                    "DIAGNOSIS_TASK",
                    "DiagnosticianAgent",
                    "control_flow",
                    "形成单一控制流根因",
                    depends_on=("N1",),
                    inputs=(e1,),
                ),
            ),
            next_workflow_stage="diagnosis",
        ),
        SupervisorDecision(
            "P5-S3-accept",
            DecisionAction.ACCEPT_HYPOTHESIS,
            "单一根因与直接证据一致",
            evidence_refs=(e1,),
            hypothesis_ref=h1,
        ),
        SupervisorDecision(
            "P5-S4-patch",
            DecisionAction.CREATE_TASK,
            "没有补丁取舍，只生成一个稳健候选",
            create_tasks=(
                _request(
                    "PATCH_TASK",
                    "PatchAgent",
                    "robust",
                    "增加窄化零除数分支",
                    depends_on=("N2",),
                    inputs=(h1, e1),
                ),
            ),
            next_workflow_stage="patch",
        ),
        SupervisorDecision(
            "P5-S5-validate",
            DecisionAction.CREATE_TASK,
            "运行完整确定性验证",
            create_tasks=(
                _request(
                    "VALIDATION_TASK",
                    "ValidationExecutor",
                    "full_validation",
                    "验证唯一 Patch",
                    depends_on=("N3",),
                    inputs=(patch,),
                ),
            ),
            next_workflow_stage="validation",
        ),
        SupervisorDecision(
            "P5-S6-finalize",
            DecisionAction.FINALIZE_TASK,
            "唯一候选通过全部真实验证门",
            evidence_refs=(validation,),
            patch_ref=patch,
            validation_ref=validation,
        ),
    )


def _write_fixture(root: Path) -> Path:
    source = root / "fixture_source"
    source.mkdir(parents=True)
    (source / "calculator.py").write_text(
        "def safe_divide(a, b):\n    return a / b\n", encoding="utf-8"
    )
    tests = source / "tests"
    tests.mkdir()
    (tests / "test_target.py").write_text(
        "from calculator import safe_divide\n\n\ndef test_zero():\n"
        "    assert safe_divide(1, 0) == 0\n",
        encoding="utf-8",
    )
    (tests / "test_regression.py").write_text(
        "from calculator import safe_divide\n\n\ndef test_nonzero():\n"
        "    assert safe_divide(6, 2) == 3\n",
        encoding="utf-8",
    )
    return source


def _task(task_id: str, source: Path) -> TaskSpec:
    return TaskSpec(
        task_id,
        source,
        "safe_divide 在除数为零时应返回 0，同时必须保留正常除法语义",
        failing_tests=("tests/test_target.py",),
        acceptance_criteria=("零除数返回 0", "非零除法结果不变"),
        target_files=("calculator.py",),
        protected_paths=("tests",),
        test_command=("python", "-m", "pytest", "-q"),
        max_runtime_seconds=30,
    )


def _run_runtime(
    task: TaskSpec,
    decisions: Sequence[SupervisorDecision],
    run_root: Path,
    label: str,
) -> tuple[OrchestrationEngine, TraceWriter]:
    trace = TraceWriter(run_root / f"{label}_trace.jsonl")
    engine = OrchestrationEngine(task, trace_writer=trace)
    runtime = LangGraphRuntime(
        ScriptedSupervisor(decisions),
        MechanismWorker(run_root / "workspaces", trace),
        run_root / f"{label}.sqlite3",
        trace_writer=trace,
    )
    with runtime:
        state = runtime.run(engine, thread_id=f"{task.task_id}-{label}")
    return restore_engine_from_runtime_state(state), trace


def _run_replan_cases(source: Path, run_root: Path) -> dict[str, Any]:
    trace = TraceWriter(run_root / "replan_trace.jsonl")
    engine = OrchestrationEngine(
        _task("phase5-replan", source),
        budget=EngineBudget(max_replans=1),
        trace_writer=trace,
    )
    evidence = MechanismWorker._evidence("R1", "failure_reproduction")
    engine.add_artifact(evidence)
    engine.blackboard.set_stage("validation")
    first = engine.apply_decision(
        SupervisorDecision(
            "P5-R1-target-diagnosis",
            DecisionAction.REQUEST_REPLAN,
            "两个候选均未解释同一目标失败，说明根因层级错误",
            next_workflow_stage="diagnosis",
            evidence_refs=(evidence.ref,),
            failure_class="both_patches_fail_target",
        )
    )
    engine.apply_decision(
        SupervisorDecision(
            "P5-R2-to-patch",
            DecisionAction.CHANGE_WORKFLOW_STAGE,
            "构造重规划预算边界场景",
            next_workflow_stage="patch",
        )
    )
    engine.apply_decision(
        SupervisorDecision(
            "P5-R3-to-validation",
            DecisionAction.CHANGE_WORKFLOW_STAGE,
            "返回验证阶段检查第二次重规划",
            next_workflow_stage="validation",
        )
    )
    exhausted = engine.apply_decision(
        SupervisorDecision(
            "P5-R4-exhausted",
            DecisionAction.REQUEST_REPLAN,
            "第二次回退超过 MVP 预算",
            next_workflow_stage="patch",
            evidence_refs=(evidence.ref,),
            failure_class="regression_failure",
        )
    )

    loop_trace = TraceWriter(run_root / "no_progress_trace.jsonl")
    loop = OrchestrationEngine(
        _task("phase5-no-progress", source),
        budget=EngineBudget(max_no_progress_decisions=2),
        trace_writer=loop_trace,
    )
    repeated = _request(
        "INVESTIGATION_TASK",
        "InvestigatorAgent",
        "code_retrieval",
        "重复且不产生新进展的调查",
    )
    loop_results = []
    for index in range(1, 4):
        loop_results.append(
            loop.apply_decision(
                SupervisorDecision(
                    f"P5-L{index}",
                    DecisionAction.CREATE_TASK,
                    "重复决策",
                    create_tasks=(repeated,),
                    next_workflow_stage="investigation",
                )
            )
        )
    return {
        "targeted_replan_ok": first.ok,
        "replan_record": engine.blackboard.artifacts.get("replan.1").to_dict(),
        "replan_path_class": classify_execution_path(engine.graph.nodes).value,
        "budget_exhaustion_code": exhausted.code,
        "budget_exhaustion_status": engine.status.value,
        "no_progress_codes": [item.code for item in loop_results],
        "no_progress_status": loop.status.value,
        "trace_path": str(trace.path),
        "no_progress_trace_path": str(loop_trace.path),
    }


def _events(trace: TraceWriter) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in trace.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def run(report_root: Path) -> tuple[dict[str, Any], bool]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_root = report_root.expanduser().resolve(strict=False) / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    source = _write_fixture(run_root)
    source_digest = _tree_digest(source)
    deep_decisions = _deep_decisions()
    deep, deep_trace = _run_runtime(
        _task("phase5-deep", source), deep_decisions, run_root, "deep"
    )
    simple, simple_trace = _run_runtime(
        _task("phase5-simple", source), _simple_decisions(), run_root, "simple"
    )
    replan = _run_replan_cases(source, run_root)

    deep_artifacts = deep.blackboard.artifacts.values()
    schema_errors: list[str] = []
    for artifact in (*deep_artifacts, *simple.blackboard.artifacts.values()):
        try:
            validate_worker_artifact(artifact)
        except (TypeError, ValueError) as exc:
            schema_errors.append(f"{artifact.ref}: {exc}")
    hypotheses_v1 = [
        item
        for item in deep_artifacts
        if item.artifact_type is ArtifactType.HYPOTHESIS and item.version == 1
    ]
    challenges = [item for item in deep_artifacts if item.artifact_type is ArtifactType.CHALLENGE]
    revisions = [
        item
        for item in deep_artifacts
        if item.artifact_type is ArtifactType.HYPOTHESIS and item.version > 1
    ]
    patches = [
        item for item in deep_artifacts if item.artifact_type is ArtifactType.PATCH_CANDIDATE
    ]
    validations = {
        str(item.content["patch_ref"]): item
        for item in deep_artifacts
        if item.artifact_type is ArtifactType.VALIDATION_RESULT
    }
    minimal = next(item for item in patches if item.content["strategy"] == "minimal")
    robust = next(item for item in patches if item.content["strategy"] == "robust")
    minimal_validation = validations[minimal.ref]
    robust_validation = validations[robust.ref]
    accept_decision = deep_decisions[7]
    cited_types = {
        deep.blackboard.artifacts.get(ref).artifact_type for ref in accept_decision.evidence_refs
    }
    challenge_targets = {str(item.content["challenged_hypothesis_ref"]) for item in challenges}
    simple_types = [node.node_type for node in simple.graph.nodes]
    review_topology = {
        (node.agent_type, node.mode)
        for node in deep.graph.nodes
        if node.node_type is NodeType.REVIEW_TASK
    }
    deep_events = _events(deep_trace)
    gate_events = [
        event
        for event in deep_events
        if event["event_type"] == "supervisor_decision_applied"
        and isinstance(event.get("data", {}).get("decision"), Mapping)
        and event["data"]["decision"].get("gate_record")
    ]
    gates = {
        "simple_task_not_unconditionally_expanded": (
            classify_execution_path(simple.graph.nodes) is ExecutionPath.FAST
            and simple_types.count(NodeType.INVESTIGATION_TASK) == 1
            and simple_types.count(NodeType.DIAGNOSIS_TASK) == 1
            and NodeType.CHALLENGE_TASK not in simple_types
            and NodeType.REVIEW_TASK not in simple_types
        ),
        "two_materially_different_hypotheses": (
            len(hypotheses_v1) == 2
            and hypotheses_materially_different(
                hypotheses_v1[0].content, hypotheses_v1[1].content
            )
        ),
        "bidirectional_effective_challenge": (
            len(challenges) == 2
            and challenge_targets == {item.ref for item in hypotheses_v1}
            and all(item.content["severity"] == "blocking" for item in challenges)
        ),
        "diagnostician_revised_after_challenge": len(revisions) == 2
        and all(item.supersedes for item in revisions),
        "supervisor_cites_full_adversarial_chain": {
            ArtifactType.EVIDENCE,
            ArtifactType.CHALLENGE,
            ArtifactType.REBUTTAL,
            ArtifactType.REVIEW,
        }.issubset(cited_types),
        "dual_patches_are_materially_different": len(patches) == 2
        and minimal.content["diff_sha256"] != robust.content["diff_sha256"],
        "patch_review_is_crossed_and_consolidated": {
            ("PatchAgent", "minimal_critiques_robust"),
            ("PatchAgent", "robust_critiques_minimal"),
            ("ReviewerAgent", "patch_review"),
        }.issubset(review_topology),
        "real_tests_overturn_wrong_patch": (
            minimal_validation.content["target_test"]["exit_code"] == 0
            and minimal_validation.content["regression_test"]["exit_code"] != 0
            and minimal_validation.content["passed"] is False
            and robust_validation.content["passed"] is True
        ),
        "targeted_replan_returns_to_diagnosis": (
            replan["targeted_replan_ok"]
            and replan["replan_record"]["content"]["target_stage"] == "diagnosis"
            and replan["replan_path_class"] == "deep"
        ),
        "termination_is_deterministic": (
            replan["budget_exhaustion_status"] == EngineStatus.FAILED.value
            and replan["no_progress_status"] == EngineStatus.FAILED.value
            and replan["no_progress_codes"][-2:] == ["NO_PROGRESS_LOOP", "NO_PROGRESS_LOOP"]
        ),
        "path_class_uses_actual_nodes": (
            classify_execution_path(deep.graph.nodes) is ExecutionPath.DEEP
            and classify_execution_path(simple.graph.nodes) is ExecutionPath.FAST
        ),
        "dynamic_gate_records_are_auditable": len(gate_events) == 5,
        "deep_and_simple_tasks_succeeded": (
            deep.status is EngineStatus.SUCCEEDED and simple.status is EngineStatus.SUCCEEDED
        ),
        "artifacts_are_schema_valid": not schema_errors,
        "source_repository_unchanged": source_digest == _tree_digest(source),
    }
    report = {
        "phase": 5,
        "run_id": run_id,
        "case_type": "deterministic_mechanism_acceptance",
        "scope_note": "用于验证 Phase 5 机制与真实测试门，不属于 Phase 6 模型效果评测。",
        "passed": all(gates.values()),
        "gates": gates,
        "deep": {
            "status": deep.status.value,
            "path_class": classify_execution_path(deep.graph.nodes).value,
            "nodes": [node.to_dict() for node in deep.graph.nodes],
            "artifacts": [item.to_dict() for item in deep_artifacts],
            "selected_hypothesis_ref": deep.blackboard.selected_hypothesis_ref,
            "selected_patch_ref": deep.blackboard.selected_patch_ref,
            "validation_ref": deep.blackboard.validation_ref,
            "trace_path": str(deep_trace.path),
        },
        "simple": {
            "status": simple.status.value,
            "path_class": classify_execution_path(simple.graph.nodes).value,
            "nodes": [node.to_dict() for node in simple.graph.nodes],
            "trace_path": str(simple_trace.path),
        },
        "replan": replan,
        "schema_errors": schema_errors,
    }
    report_path = run_root / "acceptance.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        "phase": 5,
        "run_id": run_id,
        "passed": report["passed"],
        "gates": gates,
        "report_path": str(report_path),
    }
    (report_root / "latest_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report, bool(report["passed"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-root", default="reports/phase5/acceptance")
    args = parser.parse_args()
    report, passed = run(Path(args.report_root))
    print(json.dumps({"passed": passed, "gates": report["gates"]}, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
