# Phase 5 动态门控与对抗协作

## 1. 实现边界

Phase 5 在现有 Supervisor—LangGraph—Engine—Worker 架构上补齐动态协作，不增加新的 Agent 角色。Supervisor 继续负责是否扩展、接受哪个根因和选择哪个 Patch；Engine 只校验阶段、安全、预算、引用和终止条件；Diagnostician 通过不同 mode 完成首轮诊断、Challenge 与 Rebuttal；ValidationExecutor 不调用模型，只运行受信 `TaskSpec` 命令。

## 2. 动态扩展门

创建额外 Investigator、Diagnostician、Challenge/Rebuttal 或双 Patch 时，`SupervisorDecision` 必须携带 `GateRecord`：

- `trigger_artifact_refs`：触发扩展的 Artifact；
- `reason`：证据不足、因果冲突或实现取舍；
- `added_node_count`：本次增加的节点数；
- `budget_effect`：额外 Worker 调用和节点预算影响。

Engine 校验触发引用真实存在、同时出现在 `evidence_refs` 中，并与实际创建节点数一致。初始化的最小调查节点不要求动态门记录。

## 3. Challenge、Rebuttal 与根因决策

两个 Diagnostician 第一轮仍只读取 Evidence。存在实质冲突时，Supervisor 创建双向 Challenge：每个实例只读取对方 Hypothesis 及其直接 Evidence。有效 Challenge 必须包含具体被质疑主张、证据或因果链缺口、可检验反例、替代因果链、所需证据和阻塞级别。

每方最多回应一次。`accept` 或 `partial_accept` 必须产生同一 Hypothesis 的新版本，并以 `supersedes` 连接旧版本；`reject` 不允许静默修改结论。Reviewer 随后输出 `root_cause_recommendation`，但最终 `ACCEPT_HYPOTHESIS` 仍由 Supervisor 发出。只要本轮存在 Challenge/Rebuttal，Engine 就要求该决定同时引用 Evidence、Challenge、Rebuttal 和 Review。

## 4. 双 Patch、审查与真实验证

Minimal 与 Robust Patch 使用不同受管工作区。Minimal 侧先审查 Robust 是否过度修改，Robust 侧审查 Minimal 是否只修表面症状，随后 Reviewer 汇总双方 Blocking 风险。Patch Review 只能给出建议；每个 PatchCandidate 最多修订一次，真正的淘汰依据来自 `ValidationExecutor`：

1. 重新收集实际 Diff，并核对 Patch SHA-256；
2. 检查 `protected_paths`；
3. 运行固定目标测试；
4. 运行完整回归；
5. 运行 Python 语法与 Ruff 静态检查；
6. 生成包含四个工具 Trace ID 的 `ValidationResult`。

Engine 只允许选择真实通过全部门禁、哈希与 Patch 一致且没有阻塞 Patch Review 的候选。

## 5. 失败分类、重规划与终止

重规划目标由失败类别唯一约束：

| 返回阶段 | 失败类别 |
| --- | --- |
| Investigation | `reproduction_failure`、`wrong_location`、`evidence_incomplete` |
| Diagnosis | `root_cause_rejected`、`both_patches_fail_target`、`counterexample_overturns` |
| Patch | `patch_apply_failure`、`syntax_failure`、`target_test_failure`、`regression_failure`、`blocking_patch_review` |

每次重规划生成 `ReplanRecord` 和实际 `REPLAN_TASK` 节点。MVP 最多重规划一次；超过预算时 Engine 确定性失败终止。语义相同的决定连续两次不产生进展时，同样确定性终止。

## 6. 后验路径分类

路径标签只根据实际 TaskGraph 节点计算：

- `fast`：单调查、单诊断、单 Patch，且没有 Review；
- `standard`：出现并行调查、双诊断或 Review；
- `deep`：出现 Challenge/Rebuttal、双 Patch 或 Replan。

Supervisor 只能读取 `execution_path_class`，不能预先指定路径。

## 7. 可重复验收

```bash
conda activate multi_agent
python scripts/run_phase5_acceptance.py
```

该入口使用 Scripted Supervisor 和确定性认知输出固定机制拓扑，同时通过真实隔离工作区、pytest、完整回归、Ruff、Diff 和保护路径检查验证 Patch。它只证明 Phase 5 机制，不代表真实模型成功率，也不纳入 Phase 6 的 QuixBugs 对照指标。
