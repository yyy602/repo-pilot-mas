# RepoPilot-MAS 完整计划（唯一基线 v2.5）

> 项目名称：RepoPilot-MAS
> 中文定位：基于 Supervisor 主导、动态任务图与对抗审查的多智能体代码修复系统
> 英文名称：RepoPilot-MAS: A Multi-Agent Code Repair System with Dynamic Task Graphs and Adversarial Review
> 计划版本：v2.5
> 文档状态：唯一权威计划基线
> 当前进度：Phase 0～Phase 5 与 Phase 6 历史基线 v1 已完成；闭环加固 v2 实现完成，Review 选择 Gate 修复等待提交、重新预冻结和正式验收

---

## 0. 文档权威性与编号规则

本文件是 RepoPilot-MAS 唯一的整体规划、架构和阶段验收依据。

- README、阶段说明、代码注释与本文件冲突时，以本文件为准；
- 历史方案只保留在 Git 历史中，不在当前仓库维护第二份整体计划；
- 开发进度只使用 `Phase 0` 至 `Phase 6`；
- Investigation、Diagnosis、Review、Patch、Validation 等称为“工作流阶段”，不再使用“阶段 0～9”，避免与开发 Phase 混淆；
- 每个 Phase 必须满足验收标准后才能标记完成；
- 日历天数只作为节奏建议，不作为完成证据。

本计划整合并替代早期方案，核心调整如下：

1. SupervisorAgent 是唯一拥有全局语义决策权的 Agent；
2. OrchestrationEngine 是确定性执行内核，不是 Agent；
3. 逻辑角色收敛为四类可复用 Worker Agent；
4. 系统从最小任务图开始，按不确定性与风险逐节点扩展；
5. 快速、标准、深度仅是执行 Trace 的结果分类，不是三套写死的 Pipeline；
6. 保留结构化证据、独立根因分析、Challenge/Rebuttal、双补丁竞争、真实测试和定向重规划；
7. 所有简历指标必须来自可复现的 Trace 和评测结果。
8. 默认部署采用阿里云强模型 API 作为 Supervisor、本地 Qwen3-8B 作为 Worker，确定性 Engine 负责约束两者。
9. LangGraph 只承载固定的系统执行循环、持久化恢复和人工介入，不替代 SupervisorAgent，也不复制动态 TaskGraph。

---

## 1. 项目定位与目标

### 1.1 要解决的问题

输入一个代码缺陷任务，包括：

- Bug 描述或 Issue；
- 待修复代码仓库；
- 失败测试、错误堆栈或运行日志；
- 可选的目标文件和验收条件；
- 由任务清单提供的受信测试命令与资源预算。

系统自动完成：

1. 解析任务、验收条件和安全边界；
2. 调查相关代码、复现失败并分析依赖；
3. 审计证据的真实性、相关性与充分性；
4. 根据不确定性决定是否增加调查或根因实例；
5. 在需要时执行独立根因推理和 Challenge/Rebuttal；
6. 由 Reviewer 给出专业建议，Supervisor 作最终根因决策；
7. 根据风险生成一个或两个候选补丁；
8. 在隔离工作区中应用补丁并运行真实测试；
9. 根据失败类型定向返回 Investigation、Diagnosis 或 Patch；
10. 输出最终补丁、证据链、验证结果、任务图和完整 Trace。

### 1.2 核心价值

项目的核心卖点不是 Agent 数量，而是：

> SupervisorAgent 根据结构化证据、根因冲突、补丁风险和真实验证结果，动态创建、暂停、恢复、取消、重试和回退任务；OrchestrationEngine 保证所有决定被合法、安全、可追踪地执行。

需要重点证明的工程能力：

- 动态任务图、条件边、Fork–Join 和局部回退；
- Agent 独立上下文与结构化共享黑板；
- Supervisor 语义决策与 Engine 工程执行分离；
- Challenge–Rebuttal 与候选补丁竞争；
- 工具超时、预算、重试、循环检测和失败降级；
- 测试驱动的真实验证，而不是模型自评；
- Single-Agent、固定 Multi-Agent 和动态系统的公平对照。

### 1.3 系统输出

每个任务至少输出：

- `FinalReport`；
- 根因结论及证据引用；
- 最终 Patch 和实际 Diff；
- 目标测试、回归测试和静态检查结果；
- 未选择候选及其淘汰原因；
- Agent 协作与 Reviewer 建议；
- 动态任务图与状态变化；
- 预算使用、重规划记录和终止原因；
- JSONL Trace 与汇总指标。

---

## 2. MVP 范围边界

### 2.1 简历 MVP 必须具备

- 一个 Single-Agent ReAct 基线；
- 一个 SupervisorAgent；
- 一个确定性的 OrchestrationEngine；
- 四类 Worker Agent：Investigator、Diagnostician、Reviewer、Patch；
- 动态 TaskGraph、节点状态机和结构化 Blackboard；
- 至少一个任务出现并行 Investigation；
- 至少一个任务出现两个独立 Diagnostician；
- 至少一个有效的双向 Challenge/Rebuttal 案例；
- 至少一个双 Patch 竞争和审查案例；
- 真实测试、静态检查和受保护路径检查；
- 最多一次定向重规划；
- 至少 10 个 QuixBugs Python 任务的真实结果；
- Single-Agent、固定 Multi-Agent、动态 Supervisor MAS 三组对照；
- 结构化 Trace、README、架构图和真实指标。

这里的“至少一个案例”用于证明机制真实执行，不代表所有任务都强制走深度路径。

### 2.2 可以降级但不能伪装完成

资源不足时允许：

- Investigator 最大并发从 3 降为 2；
- 只有高风险任务生成双 Patch；
- Reviewer 的不同 mode 使用同一个基础模型；
- SWE-Gym Lite 延后到 MVP 之后；
- 完整消融只保留最关键的 2～3 组。

以下能力不能从简历 MVP 中删除：

- Single-Agent 基线；
- 至少一次真实并行；
- Diagnostician 第一轮上下文隔离；
- Challenge/Rebuttal；
- 真实测试验证；
- 动态扩展或定向重规划；
- 统一预算下的对照结果。

### 2.3 本阶段明确不做

- Qwen3-8B 的 LoRA、DPO 或强化学习；
- 大规模 Agent 集群；
- Redis、Kafka、Celery、Kubernetes；
- 完整 SWE-bench Verified 评测；
- 自动构建大型全局代码索引；
- 多轮无限辩论；
- 每个工具动作都封装成 Agent；
- 分布式状态管理；
- 前端可视化平台；
- 多用户队列和生产级部署；
- 没有真实实验支撑的性能或准确率宣传。

---

## 3. 总体架构

```text
                              User Task
                                  │
                                  ▼
                    ┌────────────────────────┐
                    │ LangGraph Runtime Loop │
                    │ 持久化、恢复、流式事件、HITL │
                    └────────────┬───────────┘
                                 │ 唤醒决策节点
                                 ▼
                    ┌────────────────────────┐
                    │    SupervisorAgent     │
                    │ 阿里云强模型 API 全局决策 │
                    └────────────┬───────────┘
                                 │ SupervisorDecision
                                 ▼
                    ┌────────────────────────┐
                    │  OrchestrationEngine   │
                    │ Schema、TaskGraph 与硬约束 │
                    └────────────┬───────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
              ▼                  ▼                  ▼
     InvestigatorAgent   DiagnosticianAgent   ReviewerAgent
       本地 Qwen3-8B       本地 Qwen3-8B        本地 Qwen3-8B
              │                  │                  │
              └──────────────────┼──────────────────┘
                                 ▼
                            PatchAgent
                                 │
                                 ▼
                    Deterministic Tool Layer
                                 │
                                 ▼
                  Isolated Validation Workspace
                                 │
                                 ▼
              Blackboard + Artifact Store + Trace
                                 │
                                 └────→ LangGraph 唤醒 SupervisorAgent
```

系统分为七层：

1. LangGraph 持久化运行时层；
2. Supervisor 决策层；
3. OrchestrationEngine 确定性执行层；
4. Worker Agent 推理层；
5. Blackboard 与 Artifact 状态层；
6. 确定性工具层；
7. 隔离运行环境层。

### 3.1 架构原则

- Agent 类型固定，运行时实例按需创建；
- LangGraph 控制图是固定的系统执行循环，TaskGraph 是 Supervisor 动态生成的领域任务 DAG，两者不得合并或重复维护同一状态；
- Worker 只能提交 Artifact 和建议，不能修改全局任务图；
- Supervisor 只能输出结构化决策，不能直接操作进程和文件；
- Engine 执行状态迁移、预算和安全约束；
- LangGraph Dispatcher 只调用 Engine 判定为 READY 的节点，并把 Worker 返回值交回 Engine；
- LangGraph Checkpointer 是执行位置恢复的权威来源；自研 CheckpointStore 只负责可审计导出，不参与决定下一执行节点；
- 每个运行必须绑定相同的 `task_id`、LangGraph `thread_id` 和 Engine `state_version`，不一致时拒绝恢复；
- Agent 之间不转发完整聊天历史；
- 每次 Worker 调用使用任务所需的最小上下文；
- Supervisor 只接收压缩后的全局状态、Artifact 和证据引用，不默认上传完整仓库或聊天历史；
- API Key 只从本机忽略文件加载，不得进入配置、日志、Trace、检查点或模型上下文；
- 任何成功结论都必须引用 Engine 产生的 ValidationResult；
- 原始仓库不作为补丁写入目标。

---

## 4. SupervisorAgent 与 OrchestrationEngine

### 4.1 SupervisorAgent 的职责

SupervisorAgent 是唯一全局主 Agent，默认由阿里云强模型 API 驱动，负责：

- 理解 Issue 和验收条件；
- 制定初始最小计划；
- 创建、暂停、恢复或取消 Worker 任务；
- 判断是否需要补充证据；
- 判断根因是否存在实质冲突；
- 决定是否进入 Challenge/Rebuttal；
- 决定生成一个还是两个 Patch；
- 根据真实测试结果选择回退位置；
- 在预算内选择最终根因和补丁；
- 宣布成功、失败或需要人工处理。

只有 Supervisor 可以提出以下全局动作：

```text
CREATE_TASK
CANCEL_TASK
PAUSE_TASK
RESUME_TASK
CHANGE_WORKFLOW_STAGE
REQUEST_REPLAN
ACCEPT_HYPOTHESIS
SELECT_PATCH
FINALIZE_TASK
TERMINATE_TASK
```

Supervisor 不负责：

- 遍历全部代码；
- 直接执行测试；
- 管理线程和进程；
- 直接写入工作区；
- 自己生成并审查同一个补丁；
- 绕过 Engine 修改全局状态。

### 4.2 SupervisorDecision

Supervisor 每次读取压缩后的状态快照，并输出结构化决定：

```json
{
  "decision_id": "D12",
  "action": "CREATE_TASK",
  "reason": "两个根因均依赖尚未验证的缓存失效路径。",
  "create_tasks": [
    {
      "agent_type": "investigator",
      "mode": "dependency_trace",
      "objective": "确认缓存写入与失效路径",
      "input_artifact_ids": ["H1", "H2", "E4"]
    }
  ],
  "cancel_task_ids": [],
  "next_workflow_stage": "investigation",
  "evidence_refs": ["E4", "H1", "H2"]
}
```

模型自报 confidence 只作为辅助 Trace，不是 Engine 接受决定或任务成功的依据。

### 4.3 OrchestrationEngine 的职责

OrchestrationEngine 不是 Agent。它负责：

- 校验 SupervisorDecision Schema；
- 验证动作是否适用于当前状态；
- 创建和更新 TaskNode；
- 维护依赖、条件边和 Join Barrier；
- 将节点从 PENDING 转为 READY；
- 判定可调度 Worker 节点，由 LangGraph Dispatcher 执行实际调用；
- 控制最大并发、超时、重试和重规划次数；
- 保存 Artifact、状态版本、检查点和 Trace；
- 检测重复节点、无进展循环和预算耗尽；
- 在预定决策点唤醒 Supervisor。

Supervisor 只在以下决策点调用，Engine 不为普通状态更新反复调用模型：

1. 初始任务解析完成；
2. 一组 Investigation Join 完成；
3. Diagnosis 或 Review 产生冲突；
4. Patch 验证完成或失败；
5. 需要重规划或最终结束。

### 4.4 LangGraph Runtime 的职责

LangGraph Runtime 不是 Agent，也不拥有全局语义决策权。它负责：

- 固定连接 `Supervisor → Engine → Dispatcher → Collector → Supervisor` 执行循环；
- 以 `thread_id` 持久化每一步运行状态，并在进程重启后从未完成位置继续；
- 保留同一执行步中已成功节点的结果，恢复时不重复执行已完成 Worker；
- 对外输出 Supervisor、Engine、Worker、Artifact 和终止事件流；
- 在需要人工确认时触发可恢复中断，并通过同一 `thread_id` 接收恢复命令；
- 在终态结束图执行，不用固定 Pipeline 替代 Supervisor 的动态决策。

### 4.5 Engine 硬约束

Engine 必须拒绝：

- 不符合 Schema 的 Supervisor 决策；
- 超过 Agent、工具、时间、Token 或重规划预算的动作；
- 创建与现有活动节点等价的重复任务；
- 依赖未满足时运行节点；
- Worker 直接修改 TaskGraph 或 Blackboard 的行为；
- Agent 提供或改写任务级测试命令；
- 修改 `protected_paths` 的 Patch；
- 选择不存在、未应用或未验证的 Patch；
- 没有目标测试和回归测试结果就宣布成功；
- 仍存在未处理 Blocking Review 时 Finalize；
- 将 Agent 自报的“测试通过”视为 ValidationResult。

最终成功必须绑定到具体 Patch 哈希、实际 Diff、测试命令、退出码和 Trace ID。

---

## 5. Worker Agent 类型

系统只保留四类 Worker 实现。mode 和 strategy 表示逻辑任务，不意味着共享上下文。

### 5.1 InvestigatorAgent

负责调查和证据收集，支持：

```text
mode=code_retrieval
mode=failure_reproduction
mode=dependency_trace
mode=evidence_completion
mode=regression_scope
```

主要工具权限：

- `list_files`
- `search_code`
- `inspect_code`
- `find_references`
- `run_tests`
- `static_check`

每个 Investigation 任务返回 EvidenceArtifact，必须区分：

- 工具直接观察；
- 从代码推导的结论；
- 尚未验证的假设；
- 建议补充的证据。

### 5.2 DiagnosticianAgent

负责根因分析。需要时创建两个独立实例：

```text
Diagnostician A：控制流、边界条件、异常路径和运行行为
Diagnostician B：数据流、状态变化、接口契约和依赖影响
```

第一轮必须满足：

- 上下文相互隔离；
- 不读取对方 Hypothesis；
- 引用 Evidence ID；
- 区分直接原因和根本原因；
- 给出可执行验证计划；
- 主动列出反例和缺失证据。

Supervisor 授权后，同一 Agent 类型可以使用：

```text
mode=challenge
mode=rebuttal
```

Challenge 与 Rebuttal 必须是新的任务调用，不复用对方的私有推理历史。

### 5.3 ReviewerAgent

Reviewer 提供专业审查建议，不拥有最终决策权。支持：

```text
mode=evidence_review
mode=hypothesis_comparison
mode=challenge_quality
mode=root_cause_recommendation
mode=patch_review
mode=final_risk_review
```

不同 mode 使用：

- 独立输入视图；
- 独立 Prompt；
- 独立输出 Schema；
- 新的模型上下文。

Reviewer 可以推荐根因或 Patch，但最终 `ACCEPT_HYPOTHESIS` 和 `SELECT_PATCH` 只能由 Supervisor 提出，并由 Engine 校验。

### 5.4 PatchAgent

PatchAgent 根据已接受根因和 Patch Constraints 生成 Unified Diff：

```text
strategy=minimal
strategy=robust
```

Minimal：

- 优先最小修改范围；
- 直接修复已确认根因；
- 避免无关重构和公开接口变化。

Robust：

- 允许必要的输入校验和状态一致性处理；
- 允许处理已被证据支持的相邻边界情况；
- 必须解释相对 Minimal 增加的修改和风险。

PatchAgent 不得修改任务自带的测试和其他 `protected_paths`。它可以提出额外回归测试，但只能作为独立 GeneratedTestArtifact 或写入临时验证区域；额外测试不能替代官方目标测试和回归测试。

### 5.5 运行时实例

| 类型 | 常见实例数 | 最大 LLM 并发 |
| --- | ---: | ---: |
| SupervisorAgent | 1 | 1 |
| InvestigatorAgent | 1～3 | 2～3 |
| DiagnosticianAgent | 1～2 | 2 |
| ReviewerAgent | 0～1 | 1 |
| PatchAgent | 1～2 | 2 |

“实例”表示一次隔离任务上下文，不要求常驻一个模型进程。

---

## 6. Agent 协议、Artifact 与共享状态

### 6.1 消息类型

```text
PROPOSE
CHALLENGE
REBUTTAL
REQUEST_EVIDENCE
RECOMMEND
VERDICT
REPLAN
```

统一消息结构：

```json
{
  "message_id": "M12",
  "message_type": "CHALLENGE",
  "sender": "diagnostician_b",
  "receiver": "diagnostician_a",
  "target_id": "H1",
  "content": {},
  "created_at": "...",
  "trace_id": "..."
}
```

协议要求：

- Agent 不能直接修改其他 Agent 的 Artifact；
- 所有质疑必须引用目标 ID 和具体 Claim；
- 结论必须引用 Evidence ID；
- Blocking 质疑必须在 Finalize 前解决或明确拒绝理由；
- 所有消息写入 Trace；
- 自然语言输出不能替代结构化 Artifact。

### 6.2 Artifact 基础字段

每个 Artifact 至少包含：

```json
{
  "artifact_id": "H1",
  "artifact_type": "hypothesis",
  "version": 2,
  "status": "revised",
  "created_by": "diagnostician_a",
  "created_at": "...",
  "supersedes": "H1@v1",
  "input_refs": ["E3", "E7"],
  "trace_id": "..."
}
```

核心对象：

```text
TaskSpec
TaskNode
SupervisorDecision
EvidenceArtifact
EvidenceReview
HypothesisArtifact
ChallengeArtifact
RebuttalArtifact
ReviewArtifact
PatchCandidate
GeneratedTestArtifact
ValidationResult
ReplanRecord
FinalReport
```

### 6.3 核心 Artifact 要求

**EvidenceArtifact**

- `claim`；
- `source`；
- `content`；
- `observation_type`：direct / derived / hypothesis；
- `confidence`；
- `status`：unverified / verified / rejected；
- 工具调用 Trace ID。

**HypothesisArtifact**

- `root_cause`；
- `direct_cause`；
- `supporting_evidence`；
- `counter_evidence`；
- `affected_symbols`；
- `verification_plan`；
- `missing_evidence`；
- `confidence`。

**ChallengeArtifact**

- 被质疑的具体 Claim；
- 证据不足或因果链错误的原因；
- 反例；
- `required_evidence`；
- `severity`：blocking / non_blocking。

**RebuttalArtifact**

- `response_to`；
- `decision`：accept / partial_accept / reject；
- 新证据引用；
- 修订后的 Hypothesis 引用；
- 对反例的解释。

**PatchCandidate**

- `strategy`；
- `based_on_hypothesis`；
- `diff`；
- `changed_files`；
- `rationale`；
- `risk_notes`；
- `protected_path_check`；
- 工作区 ID。

**ValidationResult**

- Patch ID 和内容哈希；
- 实际应用结果；
- 目标测试命令、退出码与日志引用；
- 回归测试命令、退出码与日志引用；
- 静态检查结果；
- protected path 结果；
- 修改文件和行数；
- 最终 `passed` 布尔值。

### 6.4 Blackboard 与版本管理

Blackboard 只保存结构化状态和 Artifact 引用，不保存所有完整聊天历史。

```text
TaskState
├── TaskSpec
├── Budget
├── TaskGraph
├── Active / Completed / Blocked Node IDs
├── EvidenceArtifact[]
├── HypothesisArtifact[]
├── ChallengeArtifact[]
├── RebuttalArtifact[]
├── ReviewArtifact[]
├── PatchCandidate[]
├── ValidationResult[]
├── ReplanRecord[]
└── Trace References[]
```

每次状态修改必须增加 `state_version`。Artifact 修订通过 `version` 和 `supersedes` 建立关系，禁止原地覆盖历史结论。

上下文最小化规则：

- Investigator：TaskSpec、目标范围和必要工具结果；
- Diagnostician：经过审计的 Evidence；
- Reviewer：目标 Artifact 与其直接证据；
- PatchAgent：已接受 Hypothesis、约束和相关代码；
- Supervisor：压缩状态快照和 Artifact 摘要；
- Engine：完整结构化状态，不读取模型私有推理。

---

## 7. 动态任务图

### 7.1 节点类型

```text
INVESTIGATION_TASK
DIAGNOSIS_TASK
CHALLENGE_TASK
REBUTTAL_TASK
REVIEW_TASK
PATCH_TASK
VALIDATION_TASK
REPLAN_TASK
FINALIZATION_TASK
```

### 7.2 节点状态

```text
PENDING → READY → RUNNING
                    ├── SUCCEEDED
                    ├── FAILED
                    ├── TIMED_OUT
                    ├── BLOCKED
                    ├── PAUSED
                    └── CANCELLED
```

状态含义：

- `PENDING`：已经创建但依赖未满足；
- `READY`：满足条件并等待调度；
- `RUNNING`：Worker 或工具正在执行；
- `SUCCEEDED`：产生通过 Schema 校验的结果；
- `FAILED`：执行失败，可按策略重试或降级；
- `TIMED_OUT`：超过节点超时；
- `BLOCKED`：缺少必要输入或环境；
- `PAUSED`：等待质疑、答辩或补充证据；
- `CANCELLED`：状态变化后节点已失效。

### 7.3 最小种子图与按需扩展

系统不在开始时选择一条固定 Pipeline，而是创建最小种子图：

```text
Investigation → Diagnosis → Patch → Validation → Finalization
```

Supervisor 根据中间状态插入或取消节点：

```text
证据不足
  → 插入第二个 Investigation 或 Evidence Review

存在多个可解释故障链
  → 插入第二个 Diagnosis

根因实质冲突
  → 插入 Challenge → Rebuttal → Root Cause Recommendation

修复策略存在真实取舍
  → Fork Minimal / Robust Patch → Join Review

Patch 失败且根因可信
  → 创建新 Patch，取消失效 Patch 后继节点

新失败无法由根因解释
  → 回退 Diagnosis 或 Investigation
```

执行结束后，根据实际激活节点将 Trace 标记为：

- `fast`：单路调查、单路诊断、单 Patch；
- `standard`：出现并行调查、双诊断或 Reviewer；
- `deep`：出现 Challenge/Rebuttal、双 Patch 或重规划。

路径标签用于分析成本和扩展行为，不参与路由实现。

### 7.4 Fork–Join

典型 Investigation Fork–Join：

```text
             ┌── code_retrieval ──────┐
Supervisor ──┼── failure_reproduction ┼── Join → Review/Diagnosis
             └── dependency_trace ────┘
```

典型 Patch Fork–Join：

```text
Accepted Hypothesis
       ├── minimal Patch ── independent workspace ── Validation
       └── robust Patch ─── independent workspace ── Validation
                                                   ↓
                                              Join / Review
```

Join 等待所有节点进入终态，不要求所有节点成功。单个非关键 Worker 失败不得使整个流程直接崩溃。

### 7.5 动态门控

启动第二个 Investigator 的依据：

- 错误堆栈与检索位置不一致；
- 失败无法稳定复现；
- 涉及多个模块或调用链；
- 关键 Claim 只有单一来源；
- Reviewer 请求独立验证。

启动第二个 Diagnostician 的依据：

- 首个结论依赖未验证假设；
- 多个故障链都能解释现象；
- 根因涉及状态、缓存、并发或生命周期；
- 首轮 Patch 失败且实现本身无明显错误。

进入 Challenge/Rebuttal 的依据：

- H1 与 H2 对根因位置、机制或修复方向存在实质冲突；
- 两个表述只是同一根因的不同粒度时直接合并，不发起辩论；
- Challenge 必须满足结构化有效性要求，否则只记录而不阻塞。

生成第二个 Patch 的依据：

- 修改范围与鲁棒性存在明显取舍；
- 涉及公共接口或兼容性风险；
- Minimal 可能只修复表面症状；
- Reviewer 指出可验证的回归风险。

门控决定必须记录触发 Artifact、理由和预算影响，不能只记录模型 confidence。

### 7.6 检查点与恢复

运行时使用两类互补但职责唯一的持久化：

1. LangGraph SQLite Checkpointer：执行恢复权威，保存每个图步骤和中断位置；
2. 自研 CheckpointStore：审计导出，保存可阅读、带校验和的 Engine 快照与路由状态。

关键 Join、根因接受、Patch 生成和 Validation 后可导出：

```text
task_state.json
task_graph.json
blackboard.json
trace.jsonl
workspace_refs.json
```

审计快照必须记录对应的 `task_id`、`thread_id` 和 `state_version`。载入时若三者与 LangGraph 当前线程状态不一致，必须拒绝，不能自行选择“较新”的一份覆盖另一份。

MVP 必须支持进程中断后使用同一 SQLite 文件和 `thread_id` 自动恢复执行，并证明已经完成的 Worker 不会重复运行。

---

## 8. Challenge、Rebuttal 与 Patch 审查

### 8.1 根因交叉质疑

仅在两个 Hypothesis 实质冲突时执行：

1. A、B 独立生成 H1、H2；
2. A 只读取 H2 及其证据，生成对 H2 的 Challenge；
3. B 只读取 H1 及其证据，生成对 H1 的 Challenge；
4. A、B 分别收到针对自己的 Challenge；
5. 各进行一次 Rebuttal，可接受、部分接受或拒绝；
6. 接受质疑时生成新 Hypothesis 版本；
7. Reviewer 输出 RootCauseRecommendation；
8. Supervisor 作最终 ACCEPT_HYPOTHESIS 或 REQUEST_EVIDENCE 决定。

MVP 只允许一轮 Challenge/Rebuttal。

有效 Challenge 必须：

- 指向具体 Claim；
- 解释现有证据为何不足；
- 提供反例或替代因果链；
- 提出可执行的证据请求；
- 标明严重程度。

### 8.2 Patch 审查

生成双 Patch 时：

- Minimal 侧重点审查 Robust 是否过度修改；
- Robust 侧重点审查 Minimal 是否只修复表面症状；
- Reviewer 汇总 Blocking 风险；
- PatchAgent 最多各修订一次；
- 最终选择仍以真实验证为第一依据。

Blocking 问题包括：

- 修改受保护测试；
- 明显硬编码具体用例；
- 吞掉异常或破坏错误语义；
- 修改与根因无关；
- 无必要地改变公开接口；
- 引入语法错误或确定性回归；
- Diff 与声明的修改范围不一致。

---

## 9. 完整工作流与失败恢复

### 9.1 初始化

Engine：

1. 加载并校验 TaskSpec；
2. 确认仓库、测试命令和受保护路径；
3. 建立只读语义基线和候选工作区；
4. 初始化预算、Blackboard、TaskGraph 和 Trace；
5. 调用 Supervisor 生成最小调查任务。

任务为空、仓库不存在或环境无法启动时，输出结构化 environment/input failure，不进入 Agent 流程。

### 9.2 Investigation

- 一个或多个 Investigator 收集代码、复现和依赖证据；
- 并行节点记录真实起止时间；
- Reproduction 失败通常为关键问题；
- Dependency 失败可以降级为 AST 引用搜索；
- Join 后由 Reviewer 或 Supervisor 判断证据是否足够。

证据结果：

- `PASS`：进入 Diagnosis；
- `NEEDS_MORE_EVIDENCE`：最多补充一次定向 Investigation；
- `CONFLICT`：保留冲突供 Diagnostician 解释；
- `BLOCKED`：没有可靠复现且预算不足，终止。

### 9.3 Diagnosis 与根因决定

- 默认一个 Diagnostician；
- 触发门控时创建第二个独立实例；
- 无实质冲突时 Reviewer 比较或 Supervisor 直接接受；
- 有实质冲突时执行一次 Challenge/Rebuttal；
- Reviewer 只推荐，Supervisor 决定选择、合并或补充证据。

### 9.4 Patch 与 Validation

每个 Patch 使用独立工作区，并按顺序执行：

1. 校验 protected paths；
2. `git apply --check`；
3. 应用 Patch；
4. 收集实际 Diff；
5. 运行目标测试；
6. 运行全部回归测试；
7. 运行语法和静态检查；
8. 再次检查 protected paths；
9. 保存 ValidationResult；
10. 回滚或销毁工作区。

候选选择优先级：

1. 受保护路径无违规；
2. Patch 成功应用；
3. 目标测试通过；
4. 全部回归测试通过；
5. 静态检查通过；
6. 无未处理 Blocking Review；
7. 在前述条件相同时，改动更小、风险更低。

### 9.5 定向重规划

MVP 最多允许一次重规划。

回退 Investigation：

- 测试无法复现；
- 找错文件；
- 新错误指向未分析模块；
- 工具结果不完整；
- 根因与运行证据明显矛盾。

回退 Diagnosis：

- 两个 Patch 都无法解决目标测试；
- 新失败无法由当前根因解释；
- Reviewer 指出根因层级错误；
- 反例推翻核心因果链。

回退 Patch：

- 根因可信但 Diff 无法应用；
- Patch 有语法或局部实现错误；
- 单个候选产生回归；
- 存在可局部修正的 Blocking Review。

终止条件：

- 超过最大重规划次数；
- Agent、工具、Token 或时间预算耗尽；
- 连续两个决策点没有新增有效 Artifact；
- 环境不可用；
- 所有候选失败且没有新的可验证方向。

---
## 10. 确定性工具层与安全边界

### 10.1 九个基础工具

| 工具 | 作用 |
| --- | --- |
| `list_files` | 在路径边界内列出文件与目录 |
| `search_code` | 关键词或正则代码检索 |
| `inspect_code` | 查看文件区间或 Python 符号 |
| `find_references` | 基于 AST 查找定义和引用 |
| `run_tests` | 执行 TaskSpec 指定的 pytest/unittest |
| `apply_patch` | 校验并应用 Unified Diff |
| `collect_diff` | 比较候选工作区和基线 |
| `rollback_workspace` | 恢复候选工作区 |
| `static_check` | Python 语法检查和 Ruff |

Test Runner 不是 Agent。测试、静态检查、Diff 和回滚全部由 Engine 调用确定性工具完成。

### 10.2 工具结果

所有工具返回统一 ToolResult：

```text
tool
ok
data
error.code / error.message / error.details
stdout / stderr
exit_code
command
duration_ms
truncated
trace_id
started_at
```

工具可预期失败以结构化结果返回；编程错误可以抛出并由 Engine 记录为内部失败。工具结果必须可 JSON 序列化。

### 10.3 安全硬约束

- 只接受仓库内相对路径；
- 拒绝目录穿越、绝对路径、`.git` 和越界符号链接；
- 测试命令来自受信 TaskSpec，Agent 不能提供任意可执行路径；
- 可执行文件必须解析到 Engine 配置的受信解释器或白名单路径，不能只校验 basename；
- 子进程不使用 Shell 展开；
- 所有命令有超时并终止进程组；
- 标准输出、标准错误和 Diff 必须有实际资源上限，不能在完整读入内存后才截断；
- 每个 Patch 使用独立候选工作区；
- rollback/delete 必须验证受管工作区身份并拒绝 symlink 替换；
- baseline 不得被候选代码当作可写恢复源；
- protected paths 的修改在应用前后都必须检查；
- MVP 只运行受信 QuixBugs 数据；真实第三方仓库必须进入容器或等价 OS 隔离；
- 清除代理变量不等于禁止网络，文档不得将应用层限制描述为 OS 级沙箱。

### 10.4 Phase 1 关闭前必须补齐的验证

- 合法测试命令成功、普通失败和超时；
- 伪造同名可执行文件不能产生假测试通过；
- 路径穿越、越界 symlink 和工作区 symlink 替换；
- Patch 新增、修改、删除、二进制和非法路径；
- Patch 校验失败不污染工作区；
- 两个候选工作区互不污染；
- baseline 不可作为被候选修改后的回滚源；
- 输出和 Diff 上限分支；
- protected paths 违规淘汰；
- 九个工具都包含 Trace ID 和结构化错误；
- pytest 与 Ruff 全部通过。

---

## 11. 数据集与统一任务格式

### 11.1 主开发与评测集：QuixBugs Python

准备 10～15 个任务，覆盖：

- 边界条件错误；
- 循环更新错误；
- 比较符错误；
- 递归终止错误；
- 错误返回值；
- 数据结构操作错误。

用途：

- 工具和 Single-Agent 闭环；
- 固定 Multi-Agent 与动态系统对照；
- 动态扩展和成本评测；
- Critique、双 Patch 和重规划演示。

参考修复和隐藏验收信息不能提供给 Agent。

### 11.2 机制演示案例

单独准备至少两个对抗案例：

1. 表面错误位置与真正根因不同；
2. Minimal Patch 通过目标测试但引入回归。

机制案例只能用于验证 Challenge、Review 和重规划，不与标准 QuixBugs 成功率混为一个指标。

### 11.3 可选真实仓库任务

MVP 完成后再选择 3～5 个环境容易启动的 SWE-Gym Lite Python 任务，用于展示多文件检索和真实 Issue。未稳定运行时不得写入简历成果。

### 11.4 TaskSpec 唯一格式

```json
{
  "task_id": "quixbugs_binary_search",
  "repository_path": "data/quixbugs",
  "issue": "binary_search returns an incorrect index when the target is absent.",
  "failing_tests": ["tests/test_binary_search.py"],
  "acceptance_criteria": [
    "target test passes",
    "full regression passes",
    "protected paths are unchanged"
  ],
  "test_command": ["python", "-m", "pytest", "-q"],
  "target_files": [],
  "protected_paths": ["tests"],
  "max_runtime_seconds": 60,
  "metadata": {
    "dataset": "quixbugs",
    "category": "boundary_condition"
  }
}
```

字段规则：

- `task_id` 是安全、稳定、唯一的标识；
- `repository_path` 由任务加载器解析，Agent 不直接改变；
- `test_command` 是参数数组，不能是 Shell 字符串；
- `protected_paths` 默认至少包含任务自带测试目录；
- `target_files` 可以为空，不能向 Agent 泄露参考修复位置；
- 所有任务必须经过 Schema 校验才能创建工作区。

---

## 12. 模型适配层与配置

### 12.1 按角色分配模型

默认部署使用分层模型配置：

- SupervisorAgent：调用阿里云百炼 OpenAI 兼容 API，使用更强的模型完成全局规划、路由、冲突判断、重规划和最终语义决策；
- Worker Agent：使用服务器本地 Qwen3-8B，在 Supervisor 给定的目标和 Artifact 边界内完成调查、诊断、审查和补丁生成；
- OrchestrationEngine：不调用模型，只执行 Schema、状态机、预算、检查点和安全约束。
- LangGraph Runtime：不调用模型，只负责固定执行循环、SQLite 持久化、恢复、事件流和人工介入。

本地 Worker 模型目录为：

```text
/home/user50305/yjh/models/Qwen3-8B
```

模型路径、远程模型名和 API 地址只能由配置注入，不能硬编码在 Agent 实现中。Supervisor 的“全局视角”是压缩后的 TaskGraph、Artifact、证据、验证结果和剩余预算，不是未经筛选的完整源码或所有 Agent 聊天历史。

### 12.2 ModelAdapter

统一接口至少提供：

- 同步和异步生成；
- System/User 消息输入；
- JSON Schema 或结构化输出约束；
- 温度、最大输出 Token 和停止条件；
- 超时、一次格式修复和有限重试；
- 输入/输出 Token、延迟和模型标识；
- 原始响应日志引用；
- FakeModelAdapter，供无 GPU 单元测试使用。

Phase 2 已实现本地 Transformers Provider 和 FakeModelAdapter。Phase 3 只新增实际采用的阿里云百炼 OpenAI 兼容 Provider，不预先实现其他远程 Provider。

### 12.3 推荐配置

本地 Worker 继续使用 `configs/model.yaml`；Supervisor 使用 `configs/supervisor.yaml`。后者只保存非敏感配置：

```yaml
supervisor:
  provider: dashscope_openai_compatible
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  credential_file: .env.supervisor
  routing:
    strategy: model_first_account_second
    model_order:
      - qwen3.8-max
      - deepseek-v4-pro-0813
      - deepseek-v4-flash-0731
    api_key_envs:
      - DASHSCOPE_API_KEY_1
      - DASHSCOPE_API_KEY_2
  generation:
    temperature: 0.0
    enable_thinking: false
```

`credential_file` 由 Phase 3 运行入口自动读取，但不得写入 Trace；进程环境中已经显式设置的同名变量优先于文件值。

### 12.4 Supervisor 路由顺序

路由使用“强模型优先、账号其次”的固定六槽位顺序：

1. `qwen3.8-max` + 账号 1；
2. `qwen3.8-max` + 账号 2；
3. `deepseek-v4-pro-0813` + 账号 1；
4. `deepseek-v4-pro-0813` + 账号 2；
5. `deepseek-v4-flash-0731` + 账号 1；
6. `deepseek-v4-flash-0731` + 账号 2。

三类模型统一使用确定性的非思考 Supervisor 参数，并受相同 Token、调用次数和超时预算约束。

Engine 不根据模型自评动态改变该优先级。只有服务端明确返回免费额度耗尽时，Router 才把当前“模型 + 账号”槽位标记为耗尽，并将状态写入不含密钥的检查点。普通 429 可能是瞬时 RPS/TPM 限流，只执行有限退避并允许当次请求切换槽位，不能永久扣除免费额度。鉴权失败、模型不存在和参数错误属于配置错误，必须结构化失败，不能用切换模型掩盖。

全部六个槽位不可用时返回 `SUPERVISOR_ROUTES_EXHAUSTED`，不得静默改用本地 Qwen3-8B 充当生产 Supervisor。Fake、Scripted 或本地 Supervisor 只能作为测试和消融中的显式配置。

### 12.5 凭据安全

- 两个真实 Key 只保存在仓库根目录 `.env.supervisor`；
- `.gitignore` 的 `.env.*` 规则必须忽略该文件，仓库只提交 `.env.example`；
- 本机文件权限设置为 `0600`；
- 配置、异常、日志、Trace、检查点和 FinalReport 只记录 `api_key_env` 名称，绝不记录 Key 值；
- 启动时缺少任一声明的环境变量必须立即失败；
- 提交前必须扫描 Git 暂存区和全部受跟踪文件，确认没有 `sk-` 凭据。

生成差异主要来自独立任务目标、证据子集和上下文隔离，不把不同 temperature 当作多 Agent 独立性的主要来源。

---

## 13. 推荐项目目录

```text
repo-pilot-mas/
├── .env.example
├── README.md
├── pyproject.toml
├── configs/
│   ├── model.yaml
│   ├── supervisor.yaml
│   ├── runtime.yaml
│   └── phase6.yaml
├── docs/
│   ├── RepoPilot-MAS_完整计划.md
│   └── Phase1_确定性工具层.md
├── src/repo_pilot_mas/
│   ├── agents/
│   │   ├── base.py
│   │   ├── single_agent.py
│   │   ├── supervisor.py
│   │   ├── investigator.py
│   │   ├── diagnostician.py
│   │   ├── reviewer.py
│   │   └── patch_agent.py
│   ├── models/
│   │   ├── base.py
│   │   ├── fake.py
│   │   └── local_transformers.py
│   ├── orchestration/
│   │   ├── engine.py
│   │   ├── langgraph_runtime.py
│   │   ├── task_graph.py
│   │   ├── state_machine.py
│   │   ├── scheduler.py
│   │   ├── decision_policy.py
│   │   └── replanner.py
│   ├── protocol/
│   │   ├── messages.py
│   │   ├── artifacts.py
│   │   └── decisions.py
│   ├── state/
│   │   ├── task_state.py
│   │   ├── blackboard.py
│   │   ├── version_store.py
│   │   └── checkpoint.py
│   ├── runtime/
│   │   ├── command_runner.py
│   │   ├── path_guard.py
│   │   ├── workspace.py
│   │   └── react_loop.py
│   ├── tools/
│   │   ├── registry.py
│   │   └── ... existing deterministic tools
│   ├── observability/
│   │   ├── trace.py
│   │   ├── metrics.py
│   │   └── logger.py
│   └── schemas/
│       ├── task.py
│       └── tool_result.py
├── prompts/
├── data/
│   ├── quixbugs/
│   ├── tasks/
│   └── adversarial_cases/
├── scripts/
│   ├── run_task.py
│   ├── run_phase6_evaluation.py
│   └── audit_phase6_evaluation.py
├── tests/
└── reports/
    ├── traces/
    ├── patches/
    ├── logs/
    └── eval/
```

目录按 Phase 逐步创建，不为未来能力提前生成空模块。

---

## 14. 开发 Phase 与验收门

Phase 是唯一开发进度编号。任何“已完成”都必须由代码、测试和输出物共同证明。

### Phase 0：项目初始化

**状态：已完成。**

内容：

- Python `src` 项目骨架；
- `pyproject.toml`；
- pytest 与 Ruff 配置；
- README；
- Git 仓库和服务器开发环境。

验收：

- 包可以在声明的 Python 版本中安装和导入；
- 测试和静态检查命令写入 README；
- 工作区没有依赖未记录的手工路径操作。

### Phase 1：确定性工具层

**状态：已完成（2026-08-01）。**

已实现：

- TaskSpec、ToolResult；
- PathGuard、CommandRunner、WorkspaceManager；
- 九个确定性工具；
- 超时、错误结构、Trace ID 和回滚基础测试。

验收结果：

- [x] 受信可执行文件绑定，杜绝同名程序伪造测试通过；
- [x] 工作区身份、symlink 与 baseline 恢复边界加固；
- [x] 标准输出、标准错误和 Diff 采用真实有界收集；
- [x] `target_files`、`protected_paths`、`max_runtime_seconds` 纳入 TaskSpec；
- [x] 第 10.4 节全部正常与负向分支已覆盖；
- [x] 在 `multi_agent` 的 Python 3.10.20 环境执行 48 项 pytest，全绿；
- [x] Ruff 0.16.1 全绿；
- [x] README 已记录实际环境和可复现命令。

完整验收证据见 `docs/Phase1_验收报告.md`。

输出物：

- 可靠的九工具 API；
- Phase 1 设计说明；
- 完整工具测试报告。

### Phase 2：模型适配层与 Single-Agent 基线

**状态：已完成（2026-08-01）。**

**目标：先形成一个完整、可运行的修复闭环。**

工作内容：

- ModelAdapter 与 FakeModelAdapter；
- Tool Registry 及每个工具的 Agent 可见 Schema；
- 有最大步数和预算的 ReAct Loop；
- SingleAgent；
- 基础 TraceWriter；
- QuixBugs TaskSpec 和任务加载器；
- `scripts/run_task.py` 单任务入口。

硬约束：

- Agent 只能选择已注册工具和结构化参数；
- Agent 不能创建测试命令或改变 protected paths；
- Patch 必须通过 Phase 1 工作区应用和验证；
- 达到最大步数后输出结构化失败，不能无限循环。

验收标准：

- [x] FakeModelAdapter 单元测试不依赖 GPU；
- [x] 本地 Qwen3-8B 成功完成结构化 `list_files` 工具调用；
- [x] 5 个 QuixBugs 任务均可通过 `scripts/run_task.py` 一条命令端到端运行；
- [x] 1 个成功任务和 4 个失败任务均产生 FinalReport；
- [x] Agent 生成的 Patch 已完成应用、目标测试、完整回归、静态检查和回滚；
- [x] 保存 5 条包含模型、工具、Token、耗时及原始响应引用的真实 Trace；
- [x] 固定任务、模型和预算的 Single-Agent 初步结果为 1/5，可作为后续统一预算基线；
- [x] 在 `multi_agent` 环境执行 60 项 pytest 全绿，Ruff 与 `git diff --check` 通过。

输出物：

- `single_agent.py`；
- `react_loop.py`；
- `model_adapter`；
- `run_task.py`；
- 至少 5 条真实 Trace。

完整实现边界与验收证据分别见 `docs/Phase2_模型适配层与单智能体基线.md`、`docs/Phase2_验收报告.md` 和 `reports/phase2/acceptance_summary.json`。

### Phase 3：OrchestrationEngine、状态层与 SupervisorAgent

**状态：已于 2026-08-01 完成验收。**

**目标：由真实 SupervisorAgent 主导决策，以确定性 Engine 约束状态，以 LangGraph 形成可恢复的完整运行循环。**

工作内容：

1. TaskNode、TaskGraph 和状态迁移；
2. Blackboard、Artifact Store 和状态版本；
3. SupervisorDecision Schema；
4. Engine 预算、超时、重试和循环检测；
5. Scripted/Fake Supervisor 决策测试；
6. 阿里云百炼 OpenAI 兼容 ModelAdapter；
7. 双账号、三模型的确定顺序 SupervisorModelRouter；
8. SupervisorAgent；
9. 基础检查点和 Trace；
10. LangGraph 固定控制图与 SQLite Checkpointer；
11. Supervisor、Engine、Dispatcher、Collector 和人工介入节点；
12. 事件流、进程重启恢复与线程身份一致性校验。

验收标准：

- PENDING、READY、RUNNING 到所有终态的迁移有测试；
- Engine 拒绝非法决策、未满足依赖和超预算动作；
- 能动态创建、暂停、恢复、取消节点；
- Join 能处理成功、失败和超时混合结果；
- 至少一次状态变化使后继节点失效并被取消；
- Scripted Supervisor 可确定性复现完整图变化；
- 真实 Supervisor 输出不合法时可格式修复一次，仍失败则结构化终止；
- 检查点可加载到一致状态；
- `.env.supervisor` 可被运行入口自动读取，但 Key 不进入任何 Trace、报告或检查点；
- 六个 Supervisor API 槽位的顺序与 `configs/supervisor.yaml` 完全一致；
- Fake HTTP 测试覆盖额度耗尽切换、瞬时限流、鉴权失败、模型错误和全部槽位耗尽；
- 明确额度耗尽状态可随 Engine 检查点恢复，瞬时限流不会被错误持久化；
- 至少一次真实 Supervisor API 调用产生合法 SupervisorDecision，并记录模型、账号环境变量名、Token、延迟和请求 ID；
- 真实 SupervisorAgent 必须从 LangGraph 入口被调用，不能由独立脚本绕开运行时；
- `Supervisor → Engine → Fake Worker → Artifact → Supervisor` 至少完成一个自动闭环；
- 使用 SQLite Checkpointer 和相同 `thread_id` 在新 Runtime 实例中恢复未完成执行；
- 恢复时不得重新执行已经成功并持久化的 Worker；
- 人工介入使用可恢复 interrupt/Command，而不是仅把节点状态改成 PAUSED；
- 对外可流式观察 Supervisor 决策、Engine 应用、Worker 完成、Artifact 收集和终止事件；
- `task_id`、`thread_id` 或 `state_version` 任一不一致时必须拒绝恢复；
- 原有 Engine、TaskGraph、Router、Supervisor 与凭据安全测试全部回归通过。

输出物：

- Engine 与 TaskGraph；
- SupervisorDecision Schema；
- Blackboard；
- DashScope ModelAdapter 与 SupervisorModelRouter；
- LangGraph Runtime 与 SQLite 检查点数据库；
- 一条动态图状态 Trace；
- 一条超时或非法决策降级 Trace。

已完成的编排核心验收：

- [x] 35 项 Phase 3 专项测试覆盖状态迁移、动态节点、混合终态 Join、预算、重试、循环检测、检查点、格式修复和凭据安全；
- [x] 六槽位 Fake HTTP 测试覆盖额度耗尽、瞬时限流、鉴权错误、模型错误和全部耗尽，持久状态只保存明确额度耗尽槽位；
- [x] Scripted Supervisor 生成可复现动态图，证据修订使依赖旧版本的后继节点取消；非法依赖和节点超时均留下结构化降级 Trace；
- [x] 真实 `qwen3.7-max-2026-06-08` Supervisor 通过账号 1 一次生成合法 `CREATE_TASK`，并记录 Token、延迟、请求 ID 和脱敏账号环境变量名；
- [x] 在 `multi_agent` 环境执行全量 96 项 pytest 全绿，Ruff、`git diff --check` 和凭据泄漏扫描通过。

LangGraph 运行时验收：

- [x] 固定控制图自动完成 `Supervisor → Engine → Fake Worker → Artifact → Supervisor` 闭环，Supervisor 调用两次并读取 Worker 证据；
- [x] SQLite 在新 Runtime 实例中恢复失败的并行 Worker，已完成的 `N1` 不重复执行，仅失败的 `N2` 重试；
- [x] LangGraph `interrupt` 与 `Command` 支持跨 Runtime 人工恢复，拒绝时不派发 Worker；
- [x] `updates` 流覆盖 Supervisor、Dispatcher、Worker、Collector 和终止节点更新；
- [x] `task_id`、`thread_id`、`state_version` 及审计快照绑定均有拒绝测试；
- [x] 真实 Supervisor 经 LangGraph 入口生成合法决策，运行时在 Worker 派发前进入人工中断，未用 Fake Worker 冒充 Phase 4；
- [x] 在 `multi_agent` 环境执行全量 102 项 pytest 全绿，其中 Phase 3 专项 42 项；Ruff、Diff、编译和凭据扫描通过。

原有编排核心证据和新增 LangGraph 运行时证据均保留，见 `docs/Phase3_验收报告.md`、`reports/phase3/acceptance_summary.json` 和 `reports/phase3/acceptance/`。

### Phase 4：专业 Worker Agent 池

**状态：已完成（2026-08-02）。**

**目标：实现真实多 Agent 调查、诊断、审查和补丁任务。**

工作内容：

- InvestigatorAgent 各 mode；
- DiagnosticianAgent 两种视角；
- ReviewerAgent 各 mode；
- PatchAgent 两种 strategy；
- 默认全部 Worker 使用本地 Qwen3-8B；
- mode-specific Prompt 和 Schema；
- 独立上下文构造；
- 通过 LangGraph Dispatcher 执行 asyncio 并发调度；
- Evidence、Hypothesis、Review、Patch Artifact。

验收标准：

- 至少两个 Investigator 在 Trace 中有真实时间重叠；
- 单个 Worker 超时不使 Engine 崩溃；
- Worker 只返回 Artifact，不能修改 TaskGraph；
- Worker 不直接决定下一路由，Collector 提交 Artifact 后由 LangGraph 再次唤醒 Supervisor；
- 两个 Diagnostician 第一轮输入中没有对方 Hypothesis；
- Reviewer 结论引用目标 Artifact 和 Evidence；
- 两个 Patch 使用独立工作区且互不污染；
- 所有输出通过对应 Schema。

输出物：

- 四类 Worker；
- 并行 Investigation Trace；
- 独立 Diagnosis Trace；
- 双工作区 Patch Trace。

完成证据：

- [x] Investigator、Diagnostician、Reviewer、Patch 四类真实 Worker 均已实现，并按 mode/strategy 限定 Prompt、工具和 Schema；
- [x] `AsyncLangGraphRuntime` 使用 `AsyncSqliteSaver` 与异步 Worker `Send`，同步 Phase 3 Runtime 保持兼容；
- [x] 两个本地 Qwen3-8B 槽位分别绑定 `cuda:0`、`cuda:1`，最终真实运行 N1/N2 的模型时间窗发生重叠；
- [x] 两个 Diagnostician 第一轮只接收两份 Evidence，不包含对方 Hypothesis；
- [x] Reviewer 输出同时引用目标 Hypothesis 和直接 Evidence，且不拥有路由决策权；
- [x] Minimal、Robust Patch 使用不同工作区；模型提出精确文本替换，确定性控制器生成并应用 Unified Diff；
- [x] PatchCandidate 包含 `diff_sha256`、`protected_path_check` 和工作区 ID，与后续 Engine 选择门禁一致；
- [x] 最终真实运行 7 个节点全部成功、7 个 Artifact 全部通过 Schema，两个候选工作区目标测试退出码均为 0；
- [x] 独立超时场景中 N1 成功、N2 为 `TIMED_OUT`，Collector 仍提交结果并再次唤醒 Supervisor；
- [x] 在 `multi_agent` 环境完成全量 110 项 pytest（Phase 4 专项 8 项）、Ruff、Diff、编译、依赖和凭据检查。

最终真实运行 ID 为 `20260802T010050479360Z`。设计与证据分别见 `docs/Phase4_专业Worker池.md`、`docs/Phase4_验收报告.md`、`reports/phase4/acceptance/latest_summary.json` 和 `reports/phase4/timeout_acceptance/acceptance.json`。

### Phase 5：动态门控与对抗协作

**状态：已完成。**

**目标：完成项目最有辨识度的动态与对抗闭环。**

工作内容：

- 按需增加 Investigator 和 Diagnostician；
- 根因冲突识别；
- 双向 Challenge 和一轮 Rebuttal；
- Reviewer RootCauseRecommendation；
- Minimal/Robust Patch 竞争；
- Patch Review；
- 测试失败分类与一次定向重规划；
- fast/standard/deep Trace 后验分类。

验收标准：

- 简单任务不会无条件创建全部 Worker；
- 至少一个任务产生两个实质不同 Hypothesis；
- 至少一次有效双向 Challenge；
- 至少一个 Diagnostician 因质疑修订结论；
- Supervisor 最终决定引用 Evidence、Challenge、Rebuttal 和 Reviewer 建议；
- 至少一个错误 Patch 被 Reviewer 或真实测试淘汰；
- 至少一次重规划定向返回正确工作流阶段；
- 达到最大重规划或无进展条件后确定性终止；
- 路径标签由实际节点计算，而不是预先选择。

输出物：

- 完整 Challenge–Rebuttal Trace；
- 双 Patch 竞争 Trace；
- 测试推翻 Patch Trace；
- 重规划 Trace；
- 简单任务未扩展案例。

完成证据：

- [x] 动态扩展必须携带 `GateRecord`，Engine 校验触发 Artifact、理由、新增节点数和预算影响；
- [x] Diagnostician 已接通 `challenge` 与 `rebuttal` mode，接受或部分接受质询时生成版本化 Hypothesis；
- [x] 深路径产生两个实质不同 Hypothesis、两份双向 blocking Challenge、两份 Rebuttal 和两次结论修订；
- [x] Reviewer 输出 RootCauseRecommendation，Supervisor 接受根因时同时引用 Evidence、Challenge、Rebuttal 和 Review；
- [x] Minimal/Robust Patch 使用不同 Diff 和工作区，双方 PatchAgent 交叉审查后由 Reviewer 汇总，再分别进入确定性 ValidationExecutor；
- [x] Minimal Patch 目标测试退出码为 0、完整回归退出码为 1，被标记为 `regression_failure`；Robust Patch 全部门通过并被选择；
- [x] `both_patches_fail_target` 定向返回 Diagnosis；错误阶段映射被拒绝，第二次重规划超过预算后确定性终止；
- [x] 相同决策连续两次无进展后确定性终止；
- [x] Engine 限制一轮 Challenge/Rebuttal 和每个 PatchCandidate 最多一次修订；
- [x] 简单任务只创建 4 个必要节点并后验标记为 `fast`，对抗任务按实际 16 个节点后验标记为 `deep`；
- [x] 最终确定性机制运行 15 项门禁全部通过，所有 Artifact Schema 合法，原始机制仓库未被修改。

最终机制运行 ID 为 `20260802T033236637083Z`。设计、验收和机器可读证据分别见 `docs/Phase5_动态门控与对抗协作.md`、`docs/Phase5_验收报告.md`、`reports/phase5/acceptance/latest_summary.json`。该运行固定认知输出以验证机制，不计入 Phase 6 的真实模型效果评测。

### Phase 6：评测、观测与简历交付

**状态：历史基线 v1 已完成（2026-08-02）；闭环加固 v2 已完成实现，三模型六槽位迁移后等待重新预冻结与正式验收（2026-08-16）。**

**目标：把系统整理为可复现、可对照和可写入简历的项目。**

工作内容：

- 固定至少 10 个 QuixBugs 任务；
- 运行三个主系统；
- 固定各系统的角色模型、工具、任务、随机种子集合和分项预算；
- 汇总任务、Token/调用预算、时延和协作指标；
- 运行关键消融；
- 编写 README、架构图和限制说明；
- 整理演示 Trace 和面试讲解；
- 只根据真实结果撰写简历描述。

验收标准：

- [x] 一条命令运行 development 单任务；
- [x] 一条命令运行冻结批量评测；
- [x] 10 个 test 任务 × 5 个系统共 50 个结构化结果，失败全部保留；
- [x] 所有表格数字可回溯到结果、Trace、原始响应引用和 Checkpoint；
- [x] 三个主系统使用预声明预算，Fixed Hybrid 与 Proposed 使用相同 API/Worker 分项上限；
- [x] 两个关键消融完成且节点约束通过独立机器审计；
- [x] README 明确失败任务、当前限制和环境要求；
- [x] 简历表述只使用真实结果，并明确数据规模、单 seed 和等价成本。

历史基线 v1 完成证据：

- 正式运行 ID 为 `phase6_quixbugs_final_v1`；
- Local Single-Agent、Fixed Hybrid、Dynamic Hybrid 分别完成 1/10、2/10、7/10；
- 两个消融均为 6/10，但因 Proposed 实际也没有触发第二 Diagnostician 或有效 Challenge，不能据此声称因果收益；
- Dynamic Hybrid 使用 887,520 Token、75 次 Supervisor API 调用，中位时延 245,337 ms，按冻结公开单价估算成本为 8.940828 元；
- 50 条 Trace 可解析，40 个 Hybrid SQLite Checkpoint 存在，22 个成功结果全部通过目标、回归、静态和 protected-path 门；
- 7 个预算耗尽结果全部失败关闭，没有删样本或越预算成功；
- 独立审计 15 项门禁全部通过，精简证据见 `reports/phase6/final_audit.json` 与 `reports/phase6/results_summary.json`；
- 真实运行有效 Challenge 为 0、重规划 3 次且恢复 0 次，这些限制已写入 README 和简历说明。
- 在 `multi_agent` 环境执行全量 130 项 pytest 全绿，Ruff 与 `git diff --check` 通过，凭据泄漏扫描无命中。

设计、验收、闭环加固完成性审计、框架问题复盘和面试材料分别见 `docs/Phase6_评测观测与简历交付.md`、`docs/Phase6_验收报告.md`、`docs/Phase6_闭环加固v2完成性审计.md`、`docs/Phase6_真实问题复盘与排障.md` 与 `docs/Phase6_面试讲解.md`。

闭环加固 v2 当前状态：

- 已完成预算唯一事实源、失败复现语义、Agent 输入契约、Retry 分类、阶段化最小 Schema、Evidence/Reviewer Gate、Patch 语义与绑定门、Validation 定向回退、Snapshot 压缩、结构化终态和冻结身份校验；
- 当前运行时在 `/home/user50305/.conda/envs/multi_agent/bin/python` 下通过 241 项 pytest、Ruff、compileall 与 `git diff --check`；新发现的 Review 选择 Gate 修复尚待提交和重新预冻结；
- 多轮真实 API development pilot 已验证约束确实参与运行，并形成框架问题 Trace，而非只依赖 fixture；
- 最新一次诊断运行 `dev_repair_v2_routes_classified` 中，六个配置路由均返回上游免费额度耗尽。系统一次策略调用、六次真实路由尝试后立即失败关闭，实际结果记录 `code=SUPERVISOR_ROUTES_EXHAUSTED`、`failure_class=provider_quota_exhausted`，工作区清理率为 100%，源码完整性违规为 0；
- 2026-08-16 按用户确认迁移到三模型六槽位新基线；逐槽位真实探测 5/6 可用，仅 `deepseek-v4-flash-0731 + DASHSCOPE_API_KEY_1` 免费额度耗尽，同模型账号 2 可接管；脱敏证据保存在 `reports/closed_loop/evaluation/supervisor_route_probe_20260816/`；
- 首次完整 Development 前两题均以 `NO_PROGRESS_LOOP` 失败后中止，证明 Engine 错把“comparison 后选中一个候选”当成“从始至终只有一个候选”，误要求额外 recommendation；
- 修复后的真实针对性运行 `dev_repair_v2_review_selection_gatefix_gcd` 为 1/1 solved，业务、机制、预算和安全门均通过；但完整 5 题 Development 尚未在修复后的最终指纹上重跑；
- 该运行的单任务 Protocol Acceptance 通过不代表业务任务成功：实际 solved 为 0/1，也不满足冻结资格；
- 完整 5 任务 Development、冻结身份写入、一次性 10 任务 Frozen Test 和独立最终审计仍待 Review Gate 修复提交并重新预冻结后执行。完成这四步前，不得把闭环加固 v2 标记为验收完成，也不得用它替换历史 v1 的简历数字。

---

## 15. 评测方案

### 15.1 三个主系统

| 系统 | 说明 |
| --- | --- |
| Baseline A：Local Single-Agent ReAct | 本地 Qwen3-8B 单 Agent、相同工具和明确预算，作为低成本部署基线 |
| Baseline B：Fixed Hybrid Pipeline | 与 Proposed 使用相同阿里云 Supervisor 池和本地 Worker，但固定调查、诊断、审查、Patch 顺序 |
| Proposed：Dynamic Hybrid Supervisor MAS | 阿里云 Supervisor + Engine + 本地按需 Worker + 动态图 + 对抗审查 |

Fixed Hybrid Pipeline 与 Proposed 必须使用相同的 Supervisor 模型池、Worker 模型、工具、候选上限和最大 API/Worker 预算，不能故意削弱基线。Baseline A 与混合系统的对比属于实际部署效果与资源消耗对比，不能单独用于证明多 Agent 架构收益。

`Dynamic Local-Supervisor` 是后续可选消融：保持 Engine、动态图和 Worker 不变，只把 Supervisor 显式替换为本地 Qwen3-8B，用于区分强 Supervisor 模型带来的收益与动态机制本身的收益。Phase 6 MVP 未执行该消融，不得在当前结果中声称已完成。

### 15.2 主要结果指标

| 指标 | 含义 |
| --- | --- |
| Task Resolution Rate | 全部验收测试通过的任务比例 |
| Target Test Pass Rate | 原失败测试修复比例 |
| Regression Pass Rate | 原有测试保持通过比例 |
| Patch Apply Rate | Patch 成功应用比例 |
| Protected Path Violations | 修改受保护路径的次数 |
| Syntax Valid Rate | Patch 无语法错误比例 |
| End-to-End Latency | 单任务总耗时 |
| Agent / Tool Calls | 模型和工具调用次数 |
| Token Usage | 输入与输出 Token |
| Supervisor Route Usage | 各模型和账号槽位的调用、额度切换与失败次数 |

### 15.3 机制指标

| 指标 | 含义 |
| --- | --- |
| Valid Challenge Rate | 满足结构化有效性要求的质疑比例 |
| Critique-Induced Correction | 质疑导致 Hypothesis/Patch 修订次数 |
| Replan Recovery Rate | 首轮失败后定向重规划成功比例 |
| Unnecessary Expansion Rate | 简单任务被无必要扩展的比例 |
| Route Distribution | fast/standard/deep 的实际任务分布 |
| Parallel Overlap | 并行 Worker 的真实时间重叠 |
| Invalid Decision Rate | 被 Engine 拒绝的 Supervisor 决策比例 |
| Evidence Request Utility | 补充证据后产生有效进展的比例 |

Conflict Resolution Accuracy 只在具有人工或参考根因标注的案例上计算，不用“最终测试通过”冒充根因选择正确。

### 15.4 关键消融

按优先级执行：

1. 固定全流程替代动态门控；
2. 去掉第二个 Diagnostician；
3. 去掉 Challenge/Rebuttal；
4. 去掉双 Patch；
5. 规则路由替代 SupervisorAgent；
6. 去掉 Reviewer；
7. 完整聊天历史替代结构化 Blackboard。

MVP 至少完成前 3 项中的 2 项，其余根据资源决定。

### 15.5 公平性

- Fixed Hybrid 与 Proposed 使用相同 Supervisor 模型池及版本；
- 所有涉及 Worker 的系统使用相同本地 Qwen3-8B 版本；
- 相同任务输入和隐藏信息边界；
- 相同工具权限；
- 相同测试环境；
- Fixed Hybrid 与 Proposed 使用相同最大 API、Worker、工具、Token 和时间预算；
- 相同随机种子集合；
- 相同评测脚本；
- 失败和超时均计入结果，不静默删除。

跨模型配置的结果必须标注为“部署对比”，不能把强 API 模型带来的提升全部归因于动态编排。闭环加固 v2 报告各 Provider/模型的调用次数、Token 和延迟，不把人民币价格作为验收指标或简历结论；历史 v1 已冻结的等价成本证据保持原样。

---

## 16. Trace、检查点与可观测性

每个事件至少记录：

```json
{
  "event_id": "EVT_001",
  "task_id": "quixbugs_binary_search",
  "state_version": 7,
  "workflow_stage": "diagnosis",
  "node_id": "N6",
  "actor": "diagnostician_b",
  "event_type": "CHALLENGE",
  "input_refs": ["H1", "E3", "E8"],
  "output_ref": "C2",
  "start_time": "...",
  "end_time": "...",
  "model_calls": 1,
  "model_provider": "dashscope_openai_compatible",
  "model_id": "qwen3.7-max-2026-06-08",
  "api_key_env": "DASHSCOPE_API_KEY_1",
  "tool_calls": 0,
  "token_usage": {"input": 1234, "output": 356},
  "status": "success"
}
```

一条完整演示 Trace 至少展示：

1. 初始最小任务图；
2. 并行 Investigation 起止时间；
3. 证据审查或补充请求；
4. H1、H2 的独立产生；
5. 双向 Challenge；
6. Rebuttal 和至少一次修订；
7. Reviewer 建议与 Supervisor 决策；
8. 一个或两个 Patch；
9. 独立 ValidationResult；
10. 错误候选淘汰；
11. Finalize 或定向重规划；
12. 最终任务图和预算汇总。

日志应区分：

- Agent 原始响应引用；
- 解析后的 Artifact；
- 确定性工具日志；
- Engine 状态事件；
- 评测汇总。

---

## 17. 关键工程风险与应对

### 17.1 Agent 输出无法解析

- JSON Schema 校验；
- 最多一次格式修复；
- 保留原始响应引用；
- 修复仍失败则节点 FAILED，不把自然语言猜测成合法 Artifact。

### 17.2 Supervisor 成为单点幻觉来源

- Engine 执行硬约束；
- 决策必须引用 Artifact；
- 成功必须绑定 ValidationResult；
- 使用 Scripted Supervisor 测试所有关键路由；
- 统计 Invalid Decision Rate。

### 17.3 多 Agent 结论高度同质

- 独立首轮上下文；
- 不同分析目标和证据子集；
- 先独立输出再互相可见；
- 相同结论时要求列出反例，但不强制制造虚假冲突。

### 17.4 Reviewer 角色复用导致上下文污染

- 不同 mode 新建上下文；
- mode-specific Prompt 和 Schema；
- Reviewer 不拥有最终决定权；
- Review 输入仅包含目标 Artifact 和直接证据。

### 17.5 Critique 流于表面

- 强制引用具体 Claim；
- 强制 counterexample 或 required evidence；
- 无具体 objection 标记为 invalid；
- 统计 Valid Challenge Rate 和实际修订次数。

### 17.6 无限争论或循环重规划

- 一轮 Challenge/Rebuttal；
- 一次补充证据；
- 一次重规划；
- 重复节点指纹检测；
- 连续无新 Artifact 时终止。

### 17.7 Agent 修改测试或伪造测试通过

- protected paths；
- 任务测试命令由 Engine 持有；
- Patch 应用前后检查 Diff；
- Agent 自报测试结论不进入 ValidationResult；
- GeneratedTestArtifact 与官方测试分离。

### 17.8 补丁或 baseline 相互污染

- 每个候选独立工作区；
- 恢复源与候选写入区域分离；
- 受管工作区身份和 symlink 检查；
- 测试结束销毁候选；
- 原始仓库不直接修改。

### 17.9 并行没有带来收益

- 区分逻辑并发、请求并发和 GPU 物理并行；
- 记录真实时间重叠；
- 不夸大速度提升；
- 同时评估探索多样性、成功率和 Token 成本。

### 17.10 动态路径退化成固定分支

- 只实现一个 TaskGraph；
- 从最小种子图逐节点扩展；
- fast/standard/deep 后验计算；
- 记录每次扩展触发 Artifact；
- 用 Fixed Pipeline 作为独立基线而不是混入 Proposed。

---

## 18. 最终交付物

### 18.1 代码

- [x] Single-Agent baseline；
- [x] ModelAdapter 与 FakeModelAdapter；
- [x] SupervisorAgent；
- [x] OrchestrationEngine；
- [x] TaskGraph 与状态机；
- [x] Blackboard、Artifact 和检查点；
- [x] LangGraph 持久化执行闭环；
- [x] 四类 Worker Agent；
- [x] Challenge/Rebuttal；
- [x] 一个或双 Patch 及 Review；
- [x] Validation 与一次重规划；
- [x] Trace 和 Metrics；
- [x] 单任务和批量评测入口。

### 18.2 数据与实验

- [x] 15 个 QuixBugs TaskSpec，其中 5 个 development、10 个冻结 test；
- [x] 两个独立机制案例；
- [x] 三个主系统对照；
- [x] 两个关键消融；
- [x] 真实结果表；
- [x] Token、调用数和时延；
- [x] Critique 修订和重规划统计；
- [x] 动态路径分布和不必要扩展统计。

### 18.3 展示材料

- [x] README 与快速开始；
- [x] 架构图；
- [x] 一条成功修复 Trace；
- [x] 一条质疑后修正根因 Trace；
- [x] 一条测试淘汰 Patch Trace；
- [x] 一条重规划 Trace；
- [x] 一条简单任务未扩展 Trace；
- [x] 结果表和消融图；
- [x] 2 分钟面试讲解；
- [x] 基于真实指标的简历描述。

---

## 19. README 与简历叙事

README 推荐结构：

```text
1. 项目问题与目标
2. 为什么不是 Single-Agent 或固定 Multi-Agent
3. Supervisor / Engine 分层架构
4. 四类 Worker 与动态实例
5. 动态任务图和门控
6. Challenge–Rebuttal 与 Patch 竞争
7. 确定性工具和隔离边界
8. 快速开始
9. 数据集与实验设置
10. 对照结果和消融
11. 典型 Trace
12. 当前限制
13. 后续计划
```

面试讲解主线：

1. Single-Agent 容易沿单一路径形成错误归因；
2. 固定多角色流水线又会在简单任务上浪费成本；
3. 因此由 Supervisor 做语义决策、Engine 执行硬约束；
4. 系统从最小图开始，只在证据不足和风险上升时增加 Agent；
5. Worker 通过结构化 Artifact 协作，首轮根因上下文隔离；
6. 实质冲突触发 Challenge/Rebuttal，不为展示而强制辩论；
7. Patch 在独立工作区中由真实测试裁决；
8. 失败只回退相关节点；
9. 使用 Single-Agent 做部署对比，并用同模型池的 Fixed Hybrid 验证动态机制的收益与成本。

Phase 6 已在 `docs/Phase6_面试讲解.md` 中根据真实结果给出简历表述。后续更新仍不得写入未经新评测支持的成功率提升、SWE-Gym 结果或训练成果。

---

## 20. 后续扩展

### 20.1 真实仓库任务

- SWE-Gym Lite；
- 多文件依赖分析；
- Git History 工具；
- Docker/网络隔离；
- 真实 Issue 环境复现。

### 20.2 模型与 Verifier

- 使用成功和失败 Trace 构造数据；
- 微调 Reviewer/Verifier；
- 训练根因或 Patch 排序；
- 在有充分数据后再研究 DPO 或强化学习。

### 20.3 更完整评测

- 更大 SWE-Gym 子集；
- SWE-bench Verified 子集；
- 仓库和时间隔离；
- 更多随机种子；
- 完整消融与显著性分析。

### 20.4 生产能力

- 持久化状态服务；
- 多用户队列；
- 人工审批高风险操作；
- 预算和配额；
- 可视化 Trace；
- 断点续跑、监控和告警。

---

## 21. 最终成功标准

项目只有同时满足以下条件，才可以作为完整 RepoPilot-MAS 简历项目：

1. 一条命令运行单个任务；
2. 至少 10 个标准任务有真实结果；
3. Single-Agent、固定 Pipeline 和动态系统使用预先声明的总预算，混合系统另固定 API 与 Worker 分项预算；
4. 至少一次真实并行 Investigation；
5. Diagnostician A/B 第一轮上下文隔离；
6. 至少一次有效双向 Challenge/Rebuttal；
7. 至少一次 Critique 导致 Artifact 修订；
8. 至少一个错误 Patch 被 Reviewer 或真实测试淘汰；
9. 至少一次定向重规划；
10. 简单任务不会默认运行完整深度流程；
11. Supervisor 的非法决定会被 Engine 拒绝；
12. 最终成功绑定真实 ValidationResult；
13. 所有指标均可追溯到 Trace；
14. README 明确安全边界、失败案例和当前限制；
15. 简历不声称未完成的训练、数据集或虚构指标。

最终项目卖点：

> RepoPilot-MAS 不是十几个角色串行调用的 Prompt 流水线，也不是一个万能 Single-Agent。它是由 SupervisorAgent 统一决策、OrchestrationEngine 负责约束执行、专业 Worker 按不确定性动态协作，并由真实工具和测试结果完成闭环验证的层级式多智能体代码修复系统。
