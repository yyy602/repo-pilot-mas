# RepoPilot-MAS 根因确认与 Patch 闭环改造计划

## 1. 文档目的

本文档用于指导 RepoPilot-MAS 在分支 `agent/hypothesis-patch-closed-loop` 上完成根因确认、Reviewer 审查、Supervisor 接受、Patch 生成、Artifact 拒绝与恢复流程的闭环改造。

本次改造不是简单增加若干异常判断，而是建立一条可恢复、可审计、可持久化的完整执行链：

```text
Evidence
   ↓
Candidate Hypothesis
   ↓
Reviewer 技术审查
   ↓
Supervisor 接受一个或多个 Hypothesis
   ↓
Engine 记录 HypothesisResolution
   ↓
PatchAgent 基于已接受根因生成 Patch
   ↓
Patch Policy Gate
   ├── 合法 → Validation
   └── 不合法 → 结构化拒绝 → Supervisor 重新规划
```

现有 `main` 分支及 `phase6_quixbugs_final_v1` 冻结评测结果保持不变。新逻辑仅在独立分支上开发和验证。

---

## 2. 改造目标

本次改造需要保证以下系统不变量：

1. 未经 Reviewer 审查的 Hypothesis 不能被 Supervisor 接受。
2. 未经 Supervisor 接受的 Hypothesis 不能用于创建 PatchTask。
3. PatchCandidate 必须准确绑定当前有效的已接受 Hypothesis 集合。
4. 非法 Supervisor 决策不能导致 Runtime 直接崩溃。
5. 非法 Worker Artifact 不能导致 Collector 整批失败。
6. 根因被修订或推翻后，依赖旧根因的 Patch 与 Validation 必须失效。
7. 所有拒绝、恢复和状态迁移必须进入 Blackboard、Checkpoint 和 Trace。
8. Engine 只负责确定性约束，不替代 Reviewer 或 Supervisor 进行语义判断。

---

## 3. 角色职责边界

### 3.1 DiagnosticianAgent

负责：

- 基于 Evidence 生成候选根因；
- 根据新增 Evidence 修订根因；
- 在冲突路径中生成 Challenge 和 Rebuttal；
- 输出结构化 Hypothesis、Challenge、Rebuttal Artifact。

不负责：

- 接受最终根因；
- 创建或修改 TaskGraph；
- 直接创建 Patch；
- 选择最终 Patch。

### 3.2 ReviewerAgent

负责：

- 审查单个根因的证据链；
- 比较多个候选根因；
- 判断是否缺少证据；
- 判断根因是否兼容、冲突或需要修订；
- 给出推荐接受的根因集合；
- 审查 Patch 风险与覆盖范围。

不负责：

- 修改 TaskGraph；
- 直接接受根因；
- 直接创建 PatchTask；
- 选择最终 Patch；
- 宣布任务成功。

### 3.3 SupervisorAgent

负责：

- 根据 Reviewer 结论决定下一步系统动作；
- 决定补充 Evidence、重新诊断、进入 Challenge/Rebuttal、接受根因或终止；
- 通过 `ACCEPT_HYPOTHESIS` 接受一个或多个根因；
- 创建 PatchTask、ValidationTask 或重规划任务；
- 在验证通过后选择 Patch 并完成任务。

不负责：

- 直接修改文件；
- 直接执行工具；
- 绕过 Reviewer 和 Engine；
- 替代 Reviewer 完成完整技术审查。

### 3.4 OrchestrationEngine

负责：

- 强制 Reviewer Gate；
- 强制 Hypothesis Acceptance Gate；
- 强制 Patch 与已接受根因绑定；
- 拒绝非法 SupervisorDecision；
- 输出结构化恢复信息；
- 维护根因确认状态；
- 失效过期根因及其下游节点；
- 强制预算、阶段、引用、状态和安全约束。

不负责：

- 判断哪个根因在技术上更正确；
- 判断两个根因在语义上是否冲突；
- 自动生成新的自然语言根因。

### 3.5 Collector

负责：

- 校验 WorkerOutcome；
- 原子提交合法 Artifact；
- 将业务层 Artifact 违规转换为结构化拒绝记录；
- 将对应 Worker 节点标记为 FAILED；
- 继续收集同批次其他 Worker 的结果；
- 完成后重新进入 Supervisor 决策。

仅以下基础设施级错误可以导致 Collector 整体失败：

- `dispatch_id` 不一致；
- `state_version` 不一致；
- WorkerOutcome 属于其他节点；
- Checkpoint 或 Engine 状态损坏。

---

## 4. 根因确认状态模型

新增 `HypothesisResolution` 结构，由 Engine 和 Blackboard 共同维护。

建议新增文件：

```text
src/repo_pilot_mas/schemas/hypothesis_resolution.py
```

### 4.1 状态枚举

```python
class HypothesisResolutionStatus(str, Enum):
    UNRESOLVED = "unresolved"
    UNDER_REVIEW = "under_review"
    NEEDS_EVIDENCE = "needs_evidence"
    NEEDS_REVISION = "needs_revision"
    CONFLICT = "conflict"
    ACCEPTED = "accepted"
    INVALIDATED = "invalidated"
    REJECTED = "rejected"
```

### 4.2 数据结构

```python
@dataclass(slots=True)
class HypothesisResolution:
    status: HypothesisResolutionStatus
    candidate_refs: tuple[str, ...] = ()
    accepted_refs: tuple[str, ...] = ()
    primary_ref: str | None = None
    review_refs: tuple[str, ...] = ()
    accepted_by_decision_id: str | None = None
    accepted_at_state_version: int | None = None
    invalidation_reason: str | None = None
```

### 4.3 状态迁移

```text
无 Hypothesis
    ↓ Diagnostician 输出
UNRESOLVED
    ↓ Reviewer 节点创建
UNDER_REVIEW
    ├── needs_more_evidence → NEEDS_EVIDENCE
    ├── changes_requested   → NEEDS_REVISION
    ├── unsupported         → REJECTED
    ├── conflict            → CONFLICT
    └── supported/compatible
            ↓ Supervisor ACCEPT_HYPOTHESIS
         ACCEPTED
```

当已接受根因被修订或替代：

```text
ACCEPTED
    ↓ Hypothesis superseded / 新证据推翻
INVALIDATED
```

进入 `INVALIDATED` 后必须同步清理当前有效选择，并失效依赖旧根因的下游节点。

---

## 5. 单根因与多根因支持

### 5.1 单根因

```text
H-A
 ↓
Reviewer(root_cause_recommendation)
 ↓ supported
Supervisor ACCEPT_HYPOTHESIS
 ↓
accepted_refs = [H-A]
primary_ref = H-A
```

### 5.2 多个互补根因

例如：

```text
H-A：缓存键缺少 tenant_id
H-B：异步清理路径没有覆盖
```

Reviewer 可以输出：

```text
verdict = compatible
recommended_hypothesis_refs = [H-A, H-B]
```

Supervisor 可以同时接受：

```text
accepted_refs = [H-A, H-B]
primary_ref = H-A
```

后续 Patch 必须完整覆盖两个已接受根因。

### 5.3 多个竞争根因

例如：

```text
H-A：错误来自缓存键作用域
H-B：错误来自并发竞态
```

Reviewer 输出：

```text
verdict = conflict
```

此时不得进入 Patch，应执行：

```text
Challenge
→ Rebuttal
→ Reviewer(root_cause_recommendation)
→ Supervisor 接受最终根因集合
```

---

## 6. SupervisorDecision Schema 改造

当前 `ACCEPT_HYPOTHESIS` 只支持单个 `hypothesis_ref`。本次升级为多根因结构：

```json
{
  "action": "ACCEPT_HYPOTHESIS",
  "hypothesis_refs": [
    "H-A@v2",
    "H-B@v1"
  ],
  "primary_hypothesis_ref": "H-A@v2",
  "evidence_refs": [
    "E1@v1",
    "E2@v1",
    "R3@v1"
  ]
}
```

### 6.1 向后兼容

旧字段继续支持读取：

```text
hypothesis_ref
```

读取时归一化为：

```text
hypothesis_refs = [hypothesis_ref]
primary_hypothesis_ref = hypothesis_ref
```

### 6.2 Engine 接受根因时的校验

必须满足：

1. 所有引用都是当前最新 Hypothesis；
2. 至少存在一份覆盖待接受根因的 Reviewer 输出；
3. Reviewer 引用了直接 Evidence；
4. Reviewer verdict 允许接受；
5. `primary_hypothesis_ref` 属于 `hypothesis_refs`；
6. 决策 `evidence_refs` 包含所依赖的 Review；
7. 存在冲突时，Challenge、Rebuttal 和最终 Review 已完成；
8. 不存在未解决的 blocking Review。

---

## 7. Reviewer Artifact Schema 改造

Reviewer 需要同时支持单目标审查和多目标根因比较。

### 7.1 单目标审查

适用于：

- `evidence_review`；
- `patch_review`；
- `final_risk_review`。

继续使用：

```json
{
  "target_artifact_ref": "P1@v1"
}
```

### 7.2 多目标根因审查

适用于：

- `hypothesis_comparison`；
- `root_cause_recommendation`。

建议结构：

```json
{
  "mode": "hypothesis_comparison",
  "target_artifact_refs": [
    "H-A@v1",
    "H-B@v1"
  ],
  "recommended_hypothesis_refs": [
    "H-A@v1"
  ],
  "evidence_refs": [
    "E1@v1",
    "E2@v1"
  ],
  "verdict": "conflict",
  "findings": [
    "两份根因对应的因果链和修复位置不一致"
  ],
  "risk_notes": [],
  "recommendation": "进入定向 Challenge"
}
```

### 7.3 Verdict 与后续动作

| Verdict | Supervisor 后续动作 |
| --- | --- |
| `supported` | 接受单个根因 |
| `compatible` | 接受一个或多个互补根因 |
| `needs_more_evidence` | 创建 Investigator |
| `changes_requested` | 创建 Diagnostician 修订任务 |
| `unsupported` | 生成新假设或终止 |
| `conflict` | 创建 Challenge/Rebuttal |
| `approved` | Patch 或最终风险审查通过 |

---

## 8. PatchTask 前置门

在 Engine 中新增统一校验函数：

```python
def _validate_patch_eligibility(request: CreateTaskRequest) -> None:
    ...
```

检查内容：

1. `HypothesisResolution.status == ACCEPTED`；
2. `accepted_refs` 非空；
3. 所有 `accepted_refs` 都是最新版本；
4. 所有根因 Review 仍然有效；
5. PatchTask 输入包含完整 `accepted_refs`；
6. PatchTask 不得引用未接受的 Hypothesis；
7. PatchTask 输入包含必要 Review；
8. 不存在未解决的 conflict；
9. 不存在已失效根因；
10. 当前工作流阶段允许创建 PatchTask。

### 8.1 不满足条件时的处理

不直接使用普通 `ValueError` 作为系统最终处理，而是新增结构化策略异常，例如：

```python
DecisionPolicyViolation(
    code="HYPOTHESIS_NOT_RESOLVED",
    message="Patch requires reviewed and accepted hypotheses",
    recovery={
        "recommended_stage": "review",
        "allowed_next_actions": [
            "CREATE_REVIEW_TASK",
            "CREATE_INVESTIGATION_TASK",
            "CREATE_DIAGNOSIS_TASK",
            "TERMINATE_TASK"
        ],
        "candidate_hypothesis_refs": ["H-A@v1", "H-B@v1"]
    },
)
```

`apply_decision()` 捕获后返回结构化 `DecisionResult`，Engine 保持 `ACTIVE`，LangGraph 再次进入 Supervisor。

---

## 9. DecisionResult 扩展

当前 `DecisionResult` 增加以下字段：

```python
@dataclass(frozen=True, slots=True)
class DecisionResult:
    ok: bool
    code: str
    message: str
    decision_id: str | None
    state_version: int
    mutated_node_ids: tuple[str, ...] = ()
    recoverable: bool = False
    recommended_stage: str | None = None
    allowed_next_actions: tuple[str, ...] = ()
    trigger_artifact_refs: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)
```

示例：

```json
{
  "ok": false,
  "code": "HYPOTHESIS_REVIEW_REQUIRED",
  "message": "Hypothesis must be reviewed before acceptance",
  "recoverable": true,
  "recommended_stage": "review",
  "allowed_next_actions": [
    "CREATE_TASK"
  ],
  "details": {
    "required_reviewer_mode": "root_cause_recommendation"
  }
}
```

Supervisor 下一轮必须根据 `allowed_next_actions` 和 `recommended_stage` 修正决策。

---

## 10. PatchCandidate Schema 改造

当前单根因字段：

```text
based_on_hypothesis
```

升级为：

```json
{
  "based_on_hypothesis_refs": [
    "H-A@v2",
    "H-B@v1"
  ],
  "primary_hypothesis_ref": "H-A@v2",
  "covered_root_causes": {
    "H-A@v2": "修复缓存键作用域",
    "H-B@v1": "补充异步清理逻辑"
  }
}
```

### 10.1 向后兼容

旧字段：

```text
based_on_hypothesis
```

读取时迁移为：

```text
based_on_hypothesis_refs = [based_on_hypothesis]
primary_hypothesis_ref = based_on_hypothesis
```

### 10.2 PatchCandidate Gate

必须满足：

```text
PatchCandidate.based_on_hypothesis_refs
==
HypothesisResolution.accepted_refs
```

不能只包含子集，也不能包含未被接受的额外 Hypothesis。

---

## 11. Worker Artifact 非法时的恢复闭环

当前 Collector 逐个调用 `engine.add_artifact()`。本次应增加统一原子处理接口：

```python
def collect_worker_outcome(
    self,
    outcome: WorkerOutcome,
) -> WorkerCollectionResult:
    ...
```

### 11.1 原子处理顺序

```text
1. 校验 WorkerOutcome
2. 校验全部 Artifact Schema
3. 校验全部引用
4. 校验 Patch 与已接受根因绑定
5. 校验 Artifact 版本关系
6. 全部成功后统一写入 Blackboard
```

禁止出现半提交状态：

```text
第一个 Artifact 已写入
第二个 Artifact 校验失败
Blackboard 留下不完整结果
```

---

## 12. ArtifactRejection 结构化记录

新增 Artifact 类型：

```text
ARTIFACT_REJECTION
```

示例：

```json
{
  "artifact_type": "artifact_rejection",
  "created_by": "OrchestrationEngine",
  "content": {
    "node_id": "N8",
    "code": "PATCH_HYPOTHESIS_SET_MISMATCH",
    "recoverable": true,
    "recommended_stage": "patch",
    "expected_hypothesis_refs": [
      "H-A@v2"
    ],
    "actual_hypothesis_refs": [
      "H-B@v1"
    ],
    "allowed_next_actions": [
      "RETRY_PATCH",
      "CREATE_PATCH_TASK",
      "REQUEST_REPLAN"
    ]
  }
}
```

处理方式：

```text
非法 PatchCandidate
        ↓
不加入有效 PatchCandidate 集合
        ↓
生成 ArtifactRejection
        ↓
Patch 节点标记 FAILED
        ↓
清理候选 Workspace
        ↓
Collector 继续处理其他 Worker
        ↓
返回 Supervisor
```

---

## 13. Collector 容错改造

Collector 对同一批 Worker 分别处理：

```text
Worker A 合法
→ Artifact 写入
→ Node A SUCCEEDED

Worker B Patch 非法
→ ArtifactRejection 写入
→ Node B FAILED

Worker C 超时
→ Node C TIMED_OUT
```

业务层输出错误不能升级为 Runtime 崩溃。

Collector 完成后仍返回：

```text
collector → supervisor
```

Supervisor 读取新的 ArtifactRejection、节点状态和恢复建议后重新规划。

---

## 14. Patch Workspace 清理

非法 Patch 可能已经创建隔离工作区，因此必须增加显式清理接口。

候选方案：

```python
worker_executor.cleanup_rejected_outcome(...)
```

或：

```python
worker_pool.discard_workspace(workspace_id)
```

清理规则：

| 情况 | Workspace 处理 |
| --- | --- |
| PatchCandidate 合法且待验证 | 保留 |
| Patch Schema 非法 | 删除 |
| 根因绑定不一致 | 删除 |
| 修改受保护路径 | 删除 |
| Validation 完成 | 删除 |
| Runtime 终止 | 清理剩余候选 |

不能依赖 Python 垃圾回收清理工作区。

---

## 15. 根因失效传播

以下事件触发根因失效：

- 已接受 Hypothesis 被 `supersedes`；
- 关键 Review 被替代；
- 新 Evidence 推翻已接受根因；
- Supervisor 请求重新诊断；
- 对抗流程产生修订版 Hypothesis。

失效动作：

```text
HypothesisResolution → INVALIDATED
accepted hypothesis refs → 清空
selected patch → 清空
validation ref → 清空
```

同时取消：

- 读取旧根因且尚未结束的 PatchTask；
- 读取旧 Patch 的 ValidationTask；
- 依赖旧 Validation 的 FinalizationTask。

已经结束的 Artifact 不删除，继续保留审计历史，但不再属于当前有效状态。

---

## 16. Supervisor Prompt 修改

Supervisor Prompt 增加以下硬约束：

1. 所有 Hypothesis 在接受前必须经过 Reviewer；
2. 单根因使用 `root_cause_recommendation`；
3. 多根因使用 `hypothesis_comparison`；
4. Review verdict 为 `needs_more_evidence` 时必须补证据；
5. Review verdict 为 `changes_requested` 时必须修订根因；
6. Review verdict 为 `conflict` 时不得创建 Patch；
7. 只有 `hypothesis_resolution.status=accepted` 才可创建 PatchTask；
8. 上一次决策被策略门拒绝时，必须按照恢复信息修正；
9. PatchTask 必须引用完整 accepted Hypothesis 和 Review；
10. 不得重复创建等价 Review、Diagnosis 或 Patch 节点。

Supervisor 仍负责最终决策，但不再替代 Reviewer 完成技术审查。

---

## 17. 执行路径定义

由于 Patch 前必须经过 Reviewer，执行路径重新定义如下。

### 17.1 Fast Path

```text
Investigator
→ Diagnostician
→ Reviewer
→ Supervisor Accept
→ PatchAgent
→ Validation
```

### 17.2 Standard Path

```text
Investigator A/B
→ Diagnostician A/B
→ Reviewer Comparison
→ Supervisor Accept
→ PatchAgent
→ Patch Review（按风险）
→ Validation
```

### 17.3 Deep Path

```text
Investigator A/B
→ Diagnostician A/B
→ Reviewer 判断 conflict
→ Challenge
→ Rebuttal
→ Reviewer Final Recommendation
→ Supervisor Accept
→ Minimal/Robust Patch
→ Patch Review
→ Validation
→ 必要时 Replan
```

`phase5_policy.py` 中的后验路径分类规则必须同步更新，避免 Reviewer 成为必经节点后所有任务都被误分类为 standard 或 deep。

---

## 18. 文件级修改清单

### 18.1 新增文件

```text
src/repo_pilot_mas/schemas/hypothesis_resolution.py
src/repo_pilot_mas/orchestration/policy_violation.py
```

### 18.2 修改 Schema 层

```text
src/repo_pilot_mas/schemas/artifact.py
src/repo_pilot_mas/schemas/worker_artifact.py
src/repo_pilot_mas/schemas/supervisor_decision.py
src/repo_pilot_mas/schemas/__init__.py
```

### 18.3 修改状态层

```text
src/repo_pilot_mas/state/blackboard.py
```

### 18.4 修改编排层

```text
src/repo_pilot_mas/orchestration/engine.py
src/repo_pilot_mas/orchestration/phase5_policy.py
src/repo_pilot_mas/orchestration/langgraph_runtime.py
```

实际 LangGraph Runtime 文件路径以仓库当前结构为准。

### 18.5 修改 Agent 层

```text
src/repo_pilot_mas/agents/supervisor.py
src/repo_pilot_mas/agents/reviewer.py
src/repo_pilot_mas/agents/patch_agent.py
src/repo_pilot_mas/agents/worker_pool.py
```

### 18.6 修改评测与验收

```text
src/repo_pilot_mas/evaluation.py
scripts/run_phase5_acceptance.py
scripts/run_phase6_evaluation.py
```

---

## 19. 兼容策略

### 19.1 Blackboard 兼容

旧状态：

```json
{
  "selected_hypothesis_ref": "H-A@v1"
}
```

读取时迁移为：

```json
{
  "hypothesis_resolution": {
    "status": "accepted",
    "accepted_refs": ["H-A@v1"],
    "primary_ref": "H-A@v1"
  }
}
```

保留只读兼容属性：

```python
selected_hypothesis_ref
```

该属性返回当前 `primary_ref`。

### 19.2 Patch 兼容

旧字段：

```text
based_on_hypothesis
```

读取时迁移为：

```text
based_on_hypothesis_refs
primary_hypothesis_ref
```

### 19.3 SupervisorDecision 兼容

旧字段：

```text
hypothesis_ref
```

读取时迁移为：

```text
hypothesis_refs
primary_hypothesis_ref
```

### 19.4 Phase 6 结果兼容

原冻结结果：

```text
phase6_quixbugs_final_v1
```

保持不变，不覆盖、不重算、不与新结果混合。

新逻辑评测使用新协议版本，例如：

```text
phase6_v2_closed_loop
```

---

## 20. 测试计划

### 20.1 Schema 测试

1. 单根因接受格式通过；
2. 多根因接受格式通过；
3. primary 不属于 accepted 集合时拒绝；
4. comparison Review 缺少两个目标时拒绝；
5. Patch 缺少完整 accepted refs 时拒绝；
6. 旧格式能够兼容迁移；
7. ArtifactRejection Schema 校验通过。

### 20.2 Engine 测试

1. 无 Review 时 `ACCEPT_HYPOTHESIS` 被结构化拒绝；
2. 拒绝后 Engine 保持 ACTIVE；
3. 拒绝结果包含恢复信息；
4. Review 为 `needs_more_evidence` 时不能接受；
5. Review 为 `changes_requested` 时不能接受；
6. Review 为 `conflict` 且无 Challenge/Rebuttal 时不能接受；
7. 完整对抗链后可以接受；
8. 未接受根因时创建 PatchTask 被结构化拒绝；
9. PatchTask 引用错误根因被拒绝；
10. PatchTask 引用根因不完整被拒绝；
11. 合法 PatchTask 可以创建；
12. 根因被 supersede 后 Resolution 自动失效；
13. 下游 Patch 和 Validation 自动取消。

### 20.3 Collector 测试

1. 非法 PatchCandidate 不导致 Collector 崩溃；
2. 非法 Patch 节点变为 FAILED；
3. 生成 ArtifactRejection；
4. 同批合法 Worker 仍正常写入；
5. Artifact 写入保持原子性；
6. Workspace 清理被调用；
7. Collector 完成后重新进入 Supervisor。

### 20.4 Runtime 测试

1. 非法 Patch 决策后重新进入 Supervisor；
2. Supervisor 根据 recovery 创建 Reviewer；
3. Reviewer 完成后重新进入 Supervisor；
4. Supervisor 接受根因；
5. Patch 与 Validation 最终完成；
6. SQLite Checkpoint 恢复后 Resolution 状态一致。

### 20.5 回归测试

必须保证：

```text
现有全部 pytest
Phase 3 acceptance
Phase 4 acceptance
Phase 5 mechanism acceptance
Ruff
Git diff check
```

全部通过后，才进入 QuixBugs 数据集运行。

---

## 21. 关键闭环验收案例

### Case 1：单根因正常闭环

```text
Evidence
→ Hypothesis
→ Reviewer supported
→ Supervisor Accept
→ Patch
→ Validation passed
```

### Case 2：证据不足闭环

```text
Hypothesis
→ Reviewer needs_more_evidence
→ Supervisor 创建 Investigator
→ 新 Evidence
→ Diagnostician 修订
→ Reviewer supported
→ Accept
→ Patch
```

### Case 3：冲突根因闭环

```text
H-A + H-B
→ Reviewer conflict
→ Challenge
→ Rebuttal
→ Reviewer 推荐 H-A
→ Supervisor Accept H-A
→ Patch
```

### Case 4：非法 Patch 恢复闭环

```text
已接受 H-A
→ PatchAgent 错误输出 based_on=H-B
→ ArtifactRejection
→ Patch 节点 FAILED
→ Supervisor 重新创建 PatchTask
→ 新 Patch based_on=H-A
→ Validation
```

以上四个案例全部通过，才视为闭环机制实现完成。

---

## 22. 实施提交顺序

### Commit 1：状态与 Schema

```text
feat: add reviewed hypothesis resolution state
```

内容：

- HypothesisResolution；
- SupervisorDecision 多根因支持；
- Review/Patch Schema；
- Blackboard 兼容迁移。

### Commit 2：Engine Gate

```text
feat: enforce reviewed hypothesis gates before patching
```

内容：

- Reviewer Gate；
- Accept Gate；
- Patch Eligibility；
- 根因失效传播；
- 结构化 DecisionResult。

### Commit 3：Collector 恢复闭环

```text
feat: recover from rejected worker artifacts
```

内容：

- ArtifactRejection；
- Collector 原子处理；
- Patch 节点失败；
- Workspace 清理；
- 返回 Supervisor。

### Commit 4：Prompt、评测和测试

```text
test: validate hypothesis patch closed loop
```

内容：

- Supervisor Prompt；
- Reviewer/PatchAgent Prompt；
- Phase 5 验收；
- Phase 6 v2 协议；
- 全部新增和回归测试。

---

## 23. 最终验收标准

本次改造只有在以下条件全部满足时才算完成：

```text
[ ] 所有 Hypothesis 接受前均有 Reviewer
[ ] Supervisor 可以接受一个或多个根因
[ ] 未接受根因不能创建 PatchTask
[ ] PatchCandidate 必须覆盖完整已接受根因集合
[ ] 非法 Supervisor 决策不终止 Runtime
[ ] 非法 Patch 不导致 Collector 崩溃
[ ] 非法 Patch 形成结构化 ArtifactRejection
[ ] Supervisor 能根据拒绝结果重新规划
[ ] 根因修订会失效旧 Patch 和 Validation
[ ] 同批其他 Worker 不受一个失败 Worker 影响
[ ] Checkpoint 可以恢复新状态
[ ] 旧状态和旧 Artifact 可以兼容读取
[ ] Phase 1 至 Phase 6 现有测试没有被破坏
[ ] 四类关键闭环案例全部通过
```

---

## 24. 本次不包含的范围

为避免范围失控，本次不同时引入：

- 新 Agent 角色；
- 第三个 Diagnostician；
- 无限轮 Challenge/Rebuttal；
- 无限轮 Replan；
- 新代码检索工具；
- 容器级沙箱；
- 多用户任务队列；
- 20 个 QuixBugs 正式评测结果。

先完成并验证闭环代码，再单独进入数据集评测，避免把代码逻辑问题与模型效果问题混在同一次排查中。
