# RepoPilot-MAS：基于动态任务图与对抗审查的多智能体代码修复系统

> 目标：在 3–5 天内完成一个可运行、可演示、可对比、可写入简历的多 Agent 代码修复 MVP。  
> 核心机制：动态任务图、共享状态管理、阶段内并行调度、Agent 交叉质疑、Challenge–Rebuttal–Arbitration、候选补丁竞争、真实测试验证与失败驱动重规划。  
> 原则：不以“Agent 数量”作为卖点，而是证明系统如何根据当前状态动态创建、暂停、恢复、取消和重试任务，并让多个 Agent 围绕同一个问题相互检查、发现错误和共同完成修复。

---

## 1. 项目定位

### 1.1 项目名称

**RepoPilot-MAS：基于动态任务图与对抗审查的多智能体代码修复系统**

英文名称可写为：

**RepoPilot-MAS: A Multi-Agent Code Repair System with Dynamic Task Graphs and Adversarial Review**

### 1.2 项目要解决的问题

输入一个程序缺陷任务，包括：

- Bug 描述或 Issue；
- 待修复代码仓库；
- 失败测试或错误日志；
- 可选的验收要求。

系统自动完成：

1. 解析任务与验收条件；
2. 并行检索代码、复现错误和分析依赖；
3. 检查证据是否真实、完整和一致；
4. 多个根因 Agent 独立提出根因假设；
5. 根因 Agent 相互质疑并进行答辩；
6. Arbiter 基于证据、质疑和答辩完成仲裁；
7. 多个 Patch Agent 并行生成不同修复方案；
8. Patch Agent 相互检查表面修复、过度修改和回归风险；
9. 在隔离工作区中运行真实测试；
10. 根据测试失败类型定向返回根因阶段或补丁阶段；
11. 输出最终补丁、根因报告、测试结果和完整执行 Trace。

### 1.3 项目的核心价值

本项目重点体现以下 Agent 工程能力：

- Supervisor 驱动的动态任务图；
- 基于任务状态的节点创建、激活、暂停、恢复、取消和重试；
- Fork–Join、条件分支、局部回退和动态重规划；
- 多 Agent 并行与异步调度；
- 独立上下文、共享黑板和结构化状态管理；
- Agent 之间的 Challenge、Rebuttal 和 Request Evidence；
- Proposer–Critic–Arbiter 对抗协作协议；
- 候选补丁竞争和相互审查；
- 工具超时、局部失败、预算耗尽和循环检测；
- 测试驱动的失败恢复与定向回退；
- 单 Agent、固定流水线和动态任务图系统的量化对照评测。

---

## 2. 3–5 天 MVP 的范围边界

### 2.1 必须实现的能力

5 天版本必须实现：

- 一个 Single-Agent ReAct 基线；
- 一个 Supervisor；
- 3 个并行 Evidence Agent；
- 一个 Evidence Critic；
- 2 个独立 RootCause Agent；
- RootCause Agent 双向交叉质疑；
- 一轮 Rebuttal；
- 一个 Arbiter；
- 2 个候选 Patch Agent；
- Patch Agent 相互审查；
- 真实测试执行；
- 最多一次定向重规划；
- Single-Agent 与 Multi-Agent 对照实验；
- 结构化 Trace、README 和架构图。

### 2.2 3 天版本可缩减内容

如果只有 3 天，可以缩减为：

- Evidence Agent 只保留 Retrieval 和 Reproduction；
- 不实现独立 Dependency Agent；
- 只保留一个 Patch Agent；
- RootCause A/B 双向质疑仍必须保留；
- Critic 和 Arbiter 可以由同一个模型实例承担，但输出结构必须分开；
- 只运行 QuixBugs 任务；
- 只实现一次补丁失败后的重新生成。

### 2.3 本阶段明确不做

为了保证 3–5 天内完成，本阶段不做：

- Qwen3-8B 的 LoRA、DPO 或强化学习；
- 64 个 Agent 或大规模 Agent 集群；
- Redis、Kafka、Celery、Kubernetes；
- 完整 SWE-bench Verified 500 任务评测；
- 自动构建大规模仓库索引；
- 多轮无限辩论；
- 每个动作都封装成 Agent；
- 完整分布式状态管理；
- 前端可视化平台；
- 复杂权限系统和生产级部署。

这些内容可以写入后续扩展计划，但不能影响 MVP 交付。

---

## 3. 动态任务图与状态管理

动态任务图是本项目区别于固定多 Agent 流水线的核心。

固定流水线通常是：

```text
Evidence → RootCause → Patch → Test → Review
```

而本项目中的任务图不是预先写死的。Supervisor 会根据当前任务状态、中间证据、审查结果、测试结果和剩余预算，动态决定：

- 创建哪些任务节点；
- 哪些节点可以并行执行；
- 哪些节点必须等待前置条件；
- 哪些节点需要暂停；
- 哪些节点已经失效并应取消；
- 哪些节点需要重新激活；
- 失败后应回退到 Evidence、RootCause 还是 Patch 阶段；
- 当前任务是否应降级、终止或转人工。

### 3.1 任务图中的节点类型

| 节点类型           | 作用                           |
| ------------------ | ------------------------------ |
| `EVIDENCE_TASK`    | 检索、复现、依赖分析和补充证据 |
| `CRITIQUE_TASK`    | 检查证据、根因或补丁           |
| `ROOT_CAUSE_TASK`  | 独立提出根因假设               |
| `REBUTTAL_TASK`    | 回应质疑或修正假设             |
| `ARBITRATION_TASK` | 综合证据、质疑和答辩           |
| `PATCH_TASK`       | 生成候选补丁                   |
| `VALIDATION_TASK`  | 运行测试和静态检查             |
| `REPLAN_TASK`      | 根据失败结果修改任务图         |

### 3.2 任务节点状态

```text
PENDING
  ↓
READY
  ↓
RUNNING
  ├──→ SUCCEEDED
  ├──→ FAILED
  ├──→ TIMED_OUT
  ├──→ BLOCKED
  ├──→ PAUSED
  └──→ CANCELLED
```

节点状态含义：

- `PENDING`：已经创建，但前置依赖尚未满足；
- `READY`：满足执行条件，可以进入调度队列；
- `RUNNING`：Agent 或工具正在执行；
- `SUCCEEDED`：产生有效结构化结果；
- `FAILED`：执行失败，但可能重试或降级；
- `TIMED_OUT`：超过最大执行时间；
- `BLOCKED`：缺少关键证据或依赖；
- `PAUSED`：等待其他 Agent 的质疑、答辩或补充结果；
- `CANCELLED`：由于任务图变化，当前节点不再需要。

### 3.3 条件边

任务图中的边不只是固定的先后关系，还包含条件：

```text
Evidence Critic = PASS
    → 激活 RootCause A/B

Evidence Critic = NEEDS_MORE_EVIDENCE
    → 新建定向 Evidence 节点

RootCause 存在冲突
    → 激活 Challenge/Rebuttal

Arbiter = REQUEST_EVIDENCE
    → 返回 Evidence 阶段

Patch 测试失败且根因可信
    → 返回 Patch 阶段

两个 Patch 都失败且无法解释
    → 返回 RootCause 阶段

预算耗尽
    → 终止任务
```

### 3.4 Fork–Join 与并行

并行探索是动态任务图中的一种调度模式，而不是项目的最高层定位。

典型 Fork–Join：

```text
Supervisor
    ├── Retrieval
    ├── Reproduction
    └── Dependency
          ↓
       Join Barrier
          ↓
    Evidence Critic
```

另一个 Fork–Join：

```text
Arbiter 确认根因
    ├── Minimal Patch
    └── Robust Patch
          ↓
       Join Barrier
          ↓
     Cross Review/Test
```

### 3.5 共享状态与版本管理

动态任务图需要依赖共享状态，而不是依赖完整聊天记录。

状态至少包括：

- 当前任务阶段；
- 活跃节点；
- 已完成节点；
- 节点依赖；
- Evidence 版本；
- Hypothesis 版本；
- Critique 和 Rebuttal；
- Patch 版本；
- 测试结果；
- 预算消耗；
- 重规划历史；
- 终止原因。

每个可修改对象都带版本号：

```json
{
  "hypothesis_id": "H1",
  "version": 2,
  "supersedes": "H1@v1",
  "status": "revised",
  "updated_by": "root_cause_a",
  "triggered_by": "challenge_C2"
}
```

这样可以回答：

- 哪个质疑导致了根因修订；
- 哪个根因版本指导了当前补丁；
- 哪次测试失败触发了任务图回退；
- 为什么某个节点被取消；
- 当前状态是否可以从检查点恢复。

### 3.6 状态机与检查点

每个阶段完成后保存检查点：

```text
INIT
  → EVIDENCE_COLLECTION
  → EVIDENCE_REVIEW
  → ROOT_CAUSE_PROPOSAL
  → CROSS_EXAMINATION
  → ARBITRATION
  → PATCH_GENERATION
  → PATCH_REVIEW
  → VALIDATION
  → COMPLETED / REPLAN / FAILED
```

检查点至少保存：

- `task_state.json`；
- `task_graph.json`；
- `blackboard.json`；
- `trace.jsonl`；
- 当前工作区和 Patch 引用。

发生模型服务中断或进程退出后，可以从最近检查点继续，而不必重新执行全部 Agent。

---

## 4. 系统总体架构

```text
                                  Bug / Issue
                                       │
                                       ▼
                              Supervisor Agent
                     任务解析、预算控制、状态管理、重规划
                                       │
             ┌─────────────────────────┼─────────────────────────┐
             │                         │                         │
             ▼                         ▼                         ▼
      Retrieval Agent         Reproduction Agent        Dependency Agent
       代码与测试检索             错误复现与日志             调用链与影响范围
             │                         │                         │
             └─────────────────────────┼─────────────────────────┘
                                       ▼
                              Evidence Critic
                    检查证据真实性、相关性、冲突和缺失项
                                       │
                          证据不足时定向请求补充
                                       ▼
                    ┌──────────────────┴──────────────────┐
                    │                                     │
                    ▼                                     ▼
            RootCause Agent A                     RootCause Agent B
             运行行为视角                            代码语义视角
                    │                                     │
                    ├──── A Challenge B ─────────────────►│
                    │◄─── B Challenge A ──────────────────┤
                    │                                     │
                    ▼                                     ▼
              Rebuttal / Revise                      Rebuttal / Revise
                    │                                     │
                    └──────────────────┬──────────────────┘
                                       ▼
                                Arbiter Agent
                   综合证据、假设、质疑和答辩确定根因
                                       │
                    ┌──────────────────┴──────────────────┐
                    │                                     │
                    ▼                                     ▼
           Minimal Patch Agent                    Robust Patch Agent
             最小范围修改                            根因级鲁棒修复
                    │                                     │
                    ├──── Cross Review / Challenge ───────┤
                    │                                     │
                    └──────────────────┬──────────────────┘
                                       ▼
                          Test Runner / Validator
                      独立工作区、真实测试、静态检查
                                       │
                           ┌───────────┴───────────┐
                           │                       │
                         通过                     失败
                           │                       │
                           ▼                       ▼
                      输出最佳补丁        Supervisor 判断失败类型
                                                   │
                               ┌───────────────────┴───────────────────┐
                               │                                       │
                        根因或证据不足                           补丁实现错误
                               │                                       │
                     返回 RootCause/Evidence                     返回 Patch 阶段
```

### 3.1 架构原则

整个系统不是所有 Agent 永久同时运行，而是：

- 阶段之间存在必要依赖；
- 阶段内部进行并行；
- 关键结论必须经过交叉审查；
- Agent 之间通过共享黑板交换结构化信息；
- 测试失败后只重新执行相关阶段；
- 所有写操作必须发生在隔离工作区。

---

## 5. Agent 划分

## 4.1 Supervisor Agent

### 职责

- 解析 Bug 描述；
- 提取期望行为和当前异常行为；
- 生成验收条件；
- 创建任务图；
- 并行调度 Agent；
- 维护最大模型调用数、工具调用数和重规划次数；
- 判断是否需要补充证据；
- 判断测试失败属于根因错误还是补丁实现错误；
- 选择继续、降级、终止或输出结果。

### 不负责

- 不直接检索全部代码；
- 不直接生成最终补丁；
- 不替代 Critic 和 Arbiter；
- 不模拟执行测试。

### 输出示例

```json
{
  "task_id": "qb_binary_search",
  "status": "evidence_collection",
  "acceptance_criteria": [
    "修复错误返回结果",
    "全部已有测试通过",
    "不修改测试文件"
  ],
  "budget": {
    "max_model_calls": 18,
    "max_tool_calls": 30,
    "max_replans": 1
  }
}
```

---

## 4.2 Evidence Agent Pool

Evidence Agent 使用统一基类，通过不同模式运行。

### Retrieval Agent

负责：

- 根据 Bug 描述检索相关文件；
- 定位函数、类、变量和测试；
- 返回代码片段及相关原因；
- 标记直接证据和推测。

输出内容：

- 文件路径；
- 符号名；
- 行号；
- 相关代码；
- 与 Issue 的关联说明；
- 置信度。

### Reproduction Agent

负责：

- 执行已有测试；
- 提取失败用例；
- 收集异常栈；
- 记录运行命令和环境信息；
- 尝试构建最小复现。

输出内容：

- 失败测试名；
- 错误类型；
- 失败输入；
- 实际输出；
- 期望输出；
- 异常栈；
- 复现命令。

### Dependency Agent

负责：

- 分析目标符号的调用方和被调用方；
- 分析数据流和输入来源；
- 估计修改影响范围；
- 返回可能受影响的测试和模块。

MVP 中优先使用 Python AST 和简单引用搜索，不构建复杂全局图数据库。

### Evidence Agent 统一输出

```json
{
  "evidence_id": "E3",
  "producer": "reproduction_agent",
  "type": "runtime_failure",
  "claim": "binary_search 在目标不存在时返回错误索引",
  "source": {
    "test": "test_binary_search_not_found",
    "command": "pytest -q"
  },
  "content": {
    "expected": -1,
    "actual": 4
  },
  "confidence": 0.98,
  "status": "unverified"
}
```

---

## 4.3 Evidence Critic

### 职责

Evidence Critic 不负责推断最终根因，只检查证据。

检查项：

- 证据是否来自真实工具结果；
- 代码路径和行号是否存在；
- 测试是否真的失败；
- Claim 是否超出了 Source 能支持的范围；
- 检索到的位置是否只是异常发生点而非根因；
- 不同 Agent 的证据是否冲突；
- 是否缺少关键调用方、失败输入或环境信息；
- 是否存在模型虚构的证据。

### 质疑示例

```json
{
  "message_type": "CHALLENGE",
  "sender": "evidence_critic",
  "receiver": "retrieval_agent",
  "target_id": "E5",
  "objection": "该代码位置只是返回错误结果的位置，尚未证明循环边界计算是根因。",
  "required_evidence": [
    "目标函数的边界变量变化轨迹",
    "失败输入对应的循环执行过程"
  ],
  "severity": "blocking"
}
```

### 处理逻辑

- 非关键证据不足：标记为低置信度，继续流程；
- 关键证据不足：Supervisor 只重新激活对应 Evidence Agent；
- Agent 超时：保留其他证据，允许降级继续；
- 证据冲突：记录冲突，交给 RootCause Agent 解释。

---

## 4.4 RootCause Agent A 和 B

两个 RootCause Agent 使用同一个基础模型，但必须拥有：

- 独立上下文；
- 独立推理历史；
- 不同分析视角；
- 第一轮互不可见；
- 统一输出 Schema。

### RootCause Agent A：运行行为视角

重点分析：

- 失败输入如何触发错误；
- 程序执行路径；
- 变量如何变化；
- 异常或错误结果在哪一步产生；
- 哪个条件判断最可能失效。

### RootCause Agent B：代码语义与依赖视角

重点分析：

- 函数契约；
- 边界条件；
- 调用链；
- 上游输入来源；
- 修改点对其他代码的影响；
- 是否存在更上游的根本原因。

### 根因输出 Schema

```json
{
  "hypothesis_id": "H1",
  "producer": "root_cause_a",
  "root_cause": "循环更新右边界时使用了 mid + 1，导致搜索区间无法正确收缩",
  "direct_cause": "目标不存在时仍返回当前索引",
  "supporting_evidence": ["E1", "E3", "E6"],
  "counter_evidence": [],
  "affected_symbols": ["binary_search"],
  "verification_plan": [
    "记录每轮 left、right 和 mid",
    "使用不存在的目标值运行测试"
  ],
  "confidence": 0.83
}
```

---

## 4.5 RootCause 交叉质疑机制

### 执行流程

1. RootCause A 和 B 独立生成 H1 和 H2；
2. A 获得 H2，但不能修改 H2；
3. B 获得 H1，但不能修改 H1；
4. A 对 H2 发起 Challenge；
5. B 对 H1 发起 Challenge；
6. A、B 分别收到对方质疑；
7. 每个 Agent进行一次 Rebuttal；
8. Agent 可以接受质疑、补充证据或修改假设；
9. 结果交给 Arbiter。

### Challenge 必须包含

- 被质疑的具体 Claim；
- 为什么现有证据不足；
- 可能存在的反例；
- 需要什么证据才能验证；
- 严重程度。

```json
{
  "message_type": "CHALLENGE",
  "sender": "root_cause_b",
  "receiver": "root_cause_a",
  "target_id": "H1",
  "objections": [
    {
      "claim": "错误仅由返回值处理引起",
      "problem": "该假设无法解释循环提前退出的现象",
      "required_evidence": "失败输入下的边界变量变化轨迹",
      "severity": "blocking"
    }
  ]
}
```

### Rebuttal 必须包含

- 接受、部分接受或拒绝质疑；
- 新增证据；
- 修订后的假设；
- 尚未解决的问题。

```json
{
  "message_type": "REBUTTAL",
  "sender": "root_cause_a",
  "target_challenge": "C2",
  "decision": "revise",
  "revised_hypothesis": "错误由区间收缩逻辑和最终返回值共同导致，其中区间边界更新是根本原因。",
  "new_evidence": ["E8"],
  "remaining_uncertainty": []
}
```

### 停止条件

MVP 只进行一轮 Challenge 和一轮 Rebuttal，防止无限争论。

---

## 4.6 Arbiter Agent

### 职责

Arbiter 只做综合判断，不负责发起质疑。

输入包括：

- 原始任务；
- 经过 Evidence Critic 检查的证据；
- H1 和 H2；
- 双方 Challenge；
- 双方 Rebuttal；
- 新增工具结果。

输出三种决策：

1. `SELECT_HYPOTHESIS`：选择或合并根因；
2. `REQUEST_EVIDENCE`：证据不足，定向补充；
3. `UNRESOLVED`：预算不足或无法确定，终止或降级。

### 输出示例

```json
{
  "decision": "SELECT_HYPOTHESIS",
  "selected_hypothesis": "H1_REVISED",
  "reason": "修订后的 H1 能同时解释区间更新异常、错误返回值和全部失败测试。",
  "supporting_evidence": ["E3", "E6", "E8"],
  "rejected_hypotheses": [
    {
      "id": "H2",
      "reason": "H2 仅解释了返回值现象，无法解释循环区间异常。"
    }
  ],
  "patch_constraints": [
    "不得修改测试文件",
    "优先修改目标函数",
    "必须覆盖目标不存在和边界位置两类输入"
  ]
}
```

---

## 4.7 Patch Agent Pool

### Minimal Patch Agent

目标：

- 尽可能少改文件；
- 尽可能少改代码；
- 不进行无关重构；
- 不改变公开接口；
- 优先降低回归风险。

### Robust Patch Agent

目标：

- 从仲裁后的根因修复；
- 覆盖同类边界条件；
- 避免仅针对当前测试硬编码；
- 必要时进行有限局部重构；
- 显式说明潜在影响。

### Patch 输出 Schema

```json
{
  "patch_id": "P1",
  "producer": "minimal_patch_agent",
  "strategy": "minimal",
  "resolved_hypothesis": "H1_REVISED",
  "modified_files": ["python_programs/binary_search.py"],
  "diff": "...",
  "expected_effect": [
    "正确收缩搜索区间",
    "目标不存在时返回 -1"
  ],
  "known_risks": [],
  "required_tests": [
    "test_not_found",
    "test_first_element",
    "test_last_element"
  ]
}
```

---

## 4.8 Patch 相互审查

### Minimal Patch Agent 审查 Robust Patch

重点检查：

- 是否过度修改；
- 是否引入不必要重构；
- 是否改变公开接口；
- 是否增加不相关逻辑；
- 是否扩大回归风险。

### Robust Patch Agent 审查 Minimal Patch

重点检查：

- 是否只修复表面现象；
- 是否存在测试硬编码；
- 是否吞掉异常；
- 是否遗漏同类边界；
- 是否真正对应仲裁后的根因。

### Patch Critique 示例

```json
{
  "reviewed_patch": "P1",
  "reviewer": "robust_patch_agent",
  "verdict": "needs_revision",
  "objections": [
    {
      "severity": "blocking",
      "problem": "补丁只修改最终返回值，没有修复搜索区间更新错误。",
      "counterexample": "目标位于最后一个位置时仍会失败。"
    }
  ]
}
```

Patch 相互审查不是最终裁决，最终结果仍由真实测试决定。

---

## 4.9 Test Runner 与 Validator

### Test Runner 是工具，不是大模型

负责：

- 创建隔离工作区；
- 应用候选补丁；
- 运行目标测试；
- 运行全部回归测试；
- 收集退出码、日志和耗时；
- 检查是否修改测试文件；
- 执行简单静态检查；
- 回滚工作区。

### Validator Agent

负责解释测试结果：

- 补丁是否可应用；
- 原失败测试是否通过；
- 原有测试是否仍然通过；
- 是否修改测试；
- 是否存在明显硬编码；
- 是否有 Blocking Critique；
- 哪个候选补丁更合适；
- 失败应该回退到哪个阶段。

### 验证硬约束

最终补丁至少满足：

1. 补丁成功应用；
2. 目标失败测试通过；
3. 原有测试不退化；
4. 不修改受保护测试；
5. 不出现明显语法错误；
6. 不存在未处理的 Blocking Critique。

---

## 6. Agent 交互协议

MVP 定义以下消息类型：

| 消息类型           | 用途                         |
| ------------------ | ---------------------------- |
| `PROPOSE`          | 提出证据、根因或补丁         |
| `CHALLENGE`        | 质疑另一个 Agent 的结论      |
| `REBUTTAL`         | 回应质疑或修正结论           |
| `REQUEST_EVIDENCE` | 请求补充工具或证据           |
| `VERDICT`          | 给出仲裁或验证结果           |
| `REPLAN`           | 请求 Supervisor 修改执行路径 |

统一消息格式：

```json
{
  "message_id": "M12",
  "message_type": "CHALLENGE",
  "sender": "root_cause_b",
  "receiver": "root_cause_a",
  "target_id": "H1",
  "content": {},
  "created_at": "2026-07-31T23:00:00+08:00"
}
```

### 协议要求

- Agent 不直接修改其他 Agent 的输出；
- 所有质疑必须引用具体目标 ID；
- 所有结论必须引用证据 ID；
- Agent 不能通过自然语言模糊表达“我不同意”；
- Blocking 质疑必须在进入下一阶段前处理；
- 所有消息写入 Trace。

---

## 7. 共享黑板与状态存储设计

共享黑板只保存结构化结果，不保存所有 Agent 的完整聊天历史。它同时作为动态任务图的状态源，为节点调度、条件判断、版本追踪、重规划和断点恢复提供依据。

```text
TaskState
├── IssueSpec
├── AcceptanceCriteria
├── Budget
├── TaskGraph
├── Evidence[]
├── EvidenceCritiques[]
├── Hypotheses[]
├── Challenges[]
├── Rebuttals[]
├── ArbiterDecision
├── Patches[]
├── PatchCritiques[]
├── TestResults[]
├── ReplanHistory[]
└── ExecutionTrace[]
```

### TaskState 建议字段

```python
class TaskState:
    task_id: str
    status: str
    issue_spec: dict
    acceptance_criteria: list[str]

    task_graph: dict
    active_node_ids: list[str]
    completed_node_ids: list[str]
    blocked_node_ids: list[str]
    state_version: int
    checkpoint_id: str | None

    evidence: list[dict]
    evidence_critiques: list[dict]
    hypotheses: list[dict]
    challenges: list[dict]
    rebuttals: list[dict]
    arbiter_decision: dict | None

    patches: list[dict]
    patch_critiques: list[dict]
    test_results: list[dict]

    model_calls: int
    tool_calls: int
    replan_count: int
    token_usage: int
    start_time: str
    end_time: str | None
    termination_reason: str | None
```

### 上下文隔离

不同 Agent 只读取必要信息：

- Evidence Agent：Issue、仓库和工具结果；
- Evidence Critic：全部证据，但不看根因结论；
- RootCause Agent：经过审计的证据；
- Critic：目标假设和证据；
- Arbiter：证据、假设、Challenge 和 Rebuttal；
- Patch Agent：确认根因和修改约束；
- Validator：Patch、Critique 和测试结果。

---

## 8. 工具层设计

### 7.1 必须工具

| 工具                 | 功能                   |
| -------------------- | ---------------------- |
| `list_files`         | 列出仓库目录           |
| `search_code`        | 关键词检索代码         |
| `inspect_code`       | 查看文件或符号上下文   |
| `find_references`    | 查找函数或变量引用     |
| `run_tests`          | 执行 pytest 或指定测试 |
| `apply_patch`        | 在工作区应用 Diff      |
| `collect_diff`       | 获取修改内容           |
| `rollback_workspace` | 回滚候选补丁           |
| `static_check`       | 执行语法和基础静态检查 |

### 7.2 MVP 可选工具

- `git_history`：查看文件历史；
- `trace_variables`：插桩记录变量变化；
- `ast_dependency`：构建简单 AST 调用关系。

### 7.3 工具安全约束

- 所有命令必须有超时；
- 禁止任意网络访问；
- 禁止修改宿主机项目；
- 每个 Patch 使用独立临时工作区；
- 只允许白名单命令；
- 保存标准输出、标准错误和退出码；
- 超时只重试一次；
- 写操作必须可回滚。

---

## 9. 数据集与任务准备

## 8.1 5 天版本推荐数据

### 主开发与评测集：QuixBugs Python

建议选择 10–15 个任务，覆盖：

- 边界条件错误；
- 循环更新错误；
- 比较符错误；
- 递归终止错误；
- 错误返回值；
- 数据结构操作错误。

用途：

- 工具链验证；
- Single-Agent 与 Multi-Agent 对比；
- Critic 纠错演示；
- 重规划演示。

### 真实仓库演示：SWE-Gym Lite

时间允许时选择 3–5 个环境容易启动的 Python 任务。

用途：

- 证明系统不仅能处理单文件算法错误；
- 展示多文件检索和依赖分析；
- 演示真实 Issue 输入。

如果 5 天内无法稳定运行，不在简历中声称已完成 SWE-Gym 评测。

### 人工构造对抗案例

准备 2 个专门用于演示质疑机制的案例：

1. 表面根因与真正根因不同；
2. Minimal Patch 通过目标测试但导致回归失败。

人工构造案例只能作为机制演示，不能与标准数据集结果混为同一指标。

## 8.2 统一任务格式

```json
{
  "task_id": "quixbugs_binary_search",
  "repo_path": "data/quixbugs",
  "problem_statement": "binary_search returns an incorrect index when the target is absent.",
  "test_command": "pytest -q",
  "target_files": [],
  "protected_paths": ["tests"],
  "max_runtime_seconds": 60
}
```

---

## 10. 完整执行流程

## 9.1 阶段 0：任务初始化

Supervisor：

1. 读取任务；
2. 提取异常行为；
3. 提取期望行为；
4. 生成验收条件；
5. 设置预算；
6. 初始化共享黑板；
7. 创建 Evidence 任务。

失败处理：

- 任务描述为空：终止；
- 仓库不存在：终止；
- 测试命令缺失：使用默认配置或终止；
- 环境无法启动：记录为 environment failure。

---

## 9.2 阶段 1：并行证据收集

通过 `asyncio.gather()` 并行启动：

- Retrieval Agent；
- Reproduction Agent；
- Dependency Agent。

要求记录：

- 每个 Agent 的开始时间；
- 每个 Agent 的结束时间；
- Agent 使用的工具；
- Agent 输出证据；
- 超时和失败信息。

Fork–Join 逻辑：

```text
fork:
  Retrieval
  Reproduction
  Dependency

join:
  等待全部完成、失败或超时
```

允许部分失败：

- 一个 Agent 超时，其他两个证据足够时继续；
- Reproduction 失败通常为关键失败，应优先处理；
- Dependency Agent 失败可以降级为简单引用搜索。

---

## 9.3 阶段 2：证据审计

Evidence Critic 检查全部证据。

结果：

- `PASS`：进入根因阶段；
- `NEEDS_MORE_EVIDENCE`：定向补充一次；
- `CONFLICT`：保留冲突，交给 RootCause Agent；
- `BLOCKED`：没有可靠复现或关键证据，终止。

MVP 最多补充一次证据。

---

## 9.4 阶段 3：独立根因推理

并行启动 RootCause A 和 B。

要求：

- 不共享第一轮结果；
- 使用不同 Prompt 视角；
- 引用证据 ID；
- 给出可验证实验；
- 明确区分直接原因和根本原因。

---

## 9.5 阶段 4：交叉质疑与答辩

执行：

```text
A reviews H2
B reviews H1
A rebuts B's challenge
B rebuts A's challenge
```

MVP 只执行一轮。

质疑无效的判定：

- 没有引用具体 Claim；
- 没有说明证据不足原因；
- 仅表达偏好；
- 没有反例或验证要求；
- 与任务无关。

无效质疑仍保存，但不影响下一阶段。

---

## 9.6 阶段 5：根因仲裁

Arbiter：

- 比较 H1、H2；
- 检查双方是否回应 Blocking Challenge；
- 判断是否选择、合并或退回；
- 生成 Patch Constraints。

若需要补充证据：

- 最多触发一次 `REQUEST_EVIDENCE`；
- 超过预算则选择低风险假设或终止。

---

## 9.7 阶段 6：候选补丁生成

并行启动：

- Minimal Patch Agent；
- Robust Patch Agent。

要求：

- 基于同一根因和约束；
- 不查看对方补丁；
- 输出统一 Diff；
- 明确修改原因和风险；
- 不允许直接修改测试文件。

---

## 9.8 阶段 7：Patch 交叉审查

Minimal 审查 Robust，Robust 审查 Minimal。

Blocking 问题包括：

- 修改测试以通过；
- 明显硬编码；
- 吞掉异常；
- 修改与根因无关；
- 改变公开接口且无必要；
- 引入明显语法或逻辑错误。

审查后允许 Patch Agent 各修改一次补丁，时间不足时可跳过修改，仅将 Critique 交给 Validator。

---

## 9.9 阶段 8：隔离验证

为 P1、P2 创建独立工作区。

每个候选补丁执行：

1. 应用补丁；
2. 运行目标测试；
3. 运行全部测试；
4. 运行基础静态检查；
5. 检查测试文件是否修改；
6. 统计修改文件和行数；
7. 保存日志；
8. 回滚或销毁工作区。

候选补丁选择优先级：

1. 全部目标测试通过；
2. 全部回归测试通过；
3. 无 Blocking Critique；
4. 不修改测试；
5. 修改范围更小；
6. 风险更低；
7. 执行结果更稳定。

---

## 9.10 阶段 9：失败驱动重规划

最多允许一次重规划。

### 回退到 Evidence 阶段

触发条件：

- 找错文件；
- 测试无法复现；
- 新错误指向未分析模块；
- 根因与执行结果明显矛盾。

### 回退到 RootCause 阶段

触发条件：

- 两个 Patch 都无法解决目标测试；
- 新失败现象无法由选定根因解释；
- Patch Critique 指出根因层级错误。

### 回退到 Patch 阶段

触发条件：

- 根因可信，但补丁实现错误；
- 单个候选补丁回归失败；
- Patch 可应用但语法错误；
- 存在可局部修正的 Blocking Critique。

### 终止条件

- 超过最大重规划次数；
- 超过最大模型调用数；
- 超过最大工具调用数；
- 连续两轮没有新增证据；
- 环境不可用；
- 所有候选补丁失败。

---

## 11. 推荐项目目录

```text
repo_pilot_mas/
├── README.md
├── requirements.txt
├── configs/
│   ├── agents.yaml
│   ├── runtime.yaml
│   └── evaluation.yaml
│
├── src/
│   ├── orchestrator/
│   │   ├── supervisor.py
│   │   ├── workflow.py
│   │   ├── task_graph.py
│   │   ├── state_machine.py
│   │   ├── scheduler.py
│   │   ├── router.py
│   │   ├── replanner.py
│   │   └── termination.py
│   │
│   ├── agents/
│   │   ├── base_agent.py
│   │   ├── evidence_agent.py
│   │   ├── evidence_critic.py
│   │   ├── root_cause_agent.py
│   │   ├── arbiter_agent.py
│   │   ├── patch_agent.py
│   │   └── validator_agent.py
│   │
│   ├── protocol/
│   │   ├── messages.py
│   │   └── schemas.py
│   │
│   ├── state/
│   │   ├── task_state.py
│   │   ├── node_state.py
│   │   ├── blackboard.py
│   │   ├── version_store.py
│   │   └── checkpoint.py
│   │
│   ├── tools/
│   │   ├── list_files.py
│   │   ├── search_code.py
│   │   ├── inspect_code.py
│   │   ├── find_references.py
│   │   ├── run_tests.py
│   │   ├── apply_patch.py
│   │   ├── rollback.py
│   │   └── static_check.py
│   │
│   ├── runtime/
│   │   ├── model_client.py
│   │   ├── workspace.py
│   │   └── async_executor.py
│   │
│   └── observability/
│       ├── trace.py
│       ├── metrics.py
│       └── logger.py
│
├── data/
│   ├── quixbugs/
│   ├── swe_gym_subset/
│   └── tasks/
│
├── scripts/
│   ├── run_task.py
│   ├── run_eval.py
│   └── summarize_results.py
│
├── tests/
│   ├── test_protocol.py
│   ├── test_blackboard.py
│   ├── test_tools.py
│   └── test_workflow.py
│
└── reports/
    ├── traces/
    ├── patches/
    ├── logs/
    └── eval/
```

---

## 12. 5 天实施计划

## Day 1：工具闭环与 Single-Agent 基线

### 目标

先完成一个可运行的代码修复闭环。

### 工作内容

- 初始化项目；
- 准备 10–15 个 QuixBugs Python 任务；
- 定义任务 JSON；
- 实现 `list_files`；
- 实现 `search_code`；
- 实现 `inspect_code`；
- 实现 `run_tests`；
- 实现 `apply_patch`；
- 实现 `rollback_workspace`；
- 实现模型客户端；
- 完成 Single-Agent ReAct 流程；
- 保存基础 Trace。

### Day 1 验收标准

- 至少 5 个任务可以端到端运行；
- Agent 可以生成补丁；
- 补丁可以应用；
- 真实测试可以执行；
- 工作区可以回滚；
- 无论修复成功或失败，都能输出结构化结果。

### Day 1 输出物

- `single_agent.py` 或对应工作流；
- 5 条运行 Trace；
- 基础工具单元测试；
- 一条运行命令。

---

## Day 2：并行 Evidence Agent 与证据质疑

### 目标

实现第一层真正的多 Agent 并行与审查。

### 工作内容

- 实现 `BaseAgent`；
- 实现 Retrieval Agent；
- 实现 Reproduction Agent；
- 实现 Dependency Agent；
- 使用 `asyncio.gather()` 并行调度；
- 实现共享黑板；
- 实现 TaskGraph、TaskNode 和节点状态流转；
- 实现 Fork–Join 和条件边；
- 实现基础检查点持久化；
- 实现 Evidence Schema；
- 实现 Evidence Critic；
- 实现 `CHALLENGE`；
- 实现一次 `REQUEST_EVIDENCE`；
- 记录并发开始和结束时间。

### Day 2 验收标准

- 三个 Evidence Agent 具有独立上下文；
- Trace 中显示时间重叠；
- 动态任务图可以记录节点从 PENDING、READY、RUNNING 到终态的变化；
- 所有输出符合统一 Schema；
- Evidence Critic 能指出至少一种证据不足；
- 能定向重新调用一个 Evidence Agent；
- 单个 Agent 超时不会导致整个流程崩溃。

### Day 2 输出物

- 并行 Evidence 流程；
- Evidence Critic Trace；
- Agent 超时降级案例。

---

## Day 3：根因竞争、交叉质疑、答辩与仲裁

### 目标

完成项目最核心的对抗协作协议。

### 工作内容

- 实现 RootCause Agent A；
- 实现 RootCause Agent B；
- 设置不同分析视角；
- 第一轮独立推理；
- A Challenge B；
- B Challenge A；
- A/B 各进行一次 Rebuttal；
- 实现 Arbiter；
- 支持选择、合并和请求补充证据；
- 记录完整根因演化链路。

### Day 3 验收标准

- 两个 RootCause Agent 不能互相看到第一轮结果；
- 至少一个任务产生两个不同根因；
- 至少出现一次有效 Challenge；
- 被质疑 Agent 能修改或补充自己的结论；
- Arbiter 的结论引用 Evidence、Challenge 和 Rebuttal；
- 不以 Agent 自报的 confidence 作为唯一选择依据。

### Day 3 输出物

- 一条完整 Challenge–Rebuttal–Arbitration Trace；
- 一个被质疑后修正根因的案例；
- 根因 Schema 和协议测试。

---

## Day 4：双候选补丁、相互审查、真实测试与重规划

### 目标

形成完整自动修复闭环。

### 工作内容

- 实现 Minimal Patch Agent；
- 实现 Robust Patch Agent；
- 并行生成 P1 和 P2；
- 实现 Patch Cross Review；
- 为每个 Patch 创建独立工作区；
- 运行目标测试和回归测试；
- 实现 Validator；
- 根据失败类型修改动态任务图并定向回退；
- 取消已经失效的节点并创建新的 Patch 或 RootCause 节点；
- 保存重规划前后的状态版本；
- 最多执行一次重规划；
- 输出最终补丁和选择原因。

### Day 4 验收标准

- P1 和 P2 不会污染彼此；
- 至少出现一次 Patch Critique；
- Test Runner 可以推翻 Agent 的错误判断；
- 目标测试失败时能返回 Patch 或 RootCause 阶段；
- 输出最终选择和未选择原因；
- 完整流程不会无限循环。

### Day 4 输出物

- 一条双补丁竞争 Trace；
- 一条测试推翻错误补丁的 Trace；
- 一条重规划后成功或失败的 Trace。

---

## Day 5：对照实验、评测、README 与简历材料

### 目标

将项目整理为可展示、可解释和可量化的简历项目。

### 对照版本

| 版本                                    | 并行证据 | 根因交叉质疑 | 双补丁 | 重规划 |
| --------------------------------------- | -------: | -----------: | -----: | -----: |
| Single-Agent                            |       否 |           否 |     否 |     否 |
| Serial Multi-Agent                      |       否 |           否 |     否 |     否 |
| Parallel Multi-Agent                    |       是 |           否 |     是 |     否 |
| Dynamic-Graph Multi-Agent               |       是 |           否 |     是 |     是 |
| Full Dynamic-Graph + Adversarial Review |       是 |           是 |     是 |     是 |

### 工作内容

- 固定任务集；
- 固定模型和生成参数；
- 统一最大调用预算；
- 运行四组实验；
- 统计任务成功率；
- 统计 Agent 和工具调用；
- 统计 Token 和时延；
- 统计 Critic 纠错次数；
- 统计重规划恢复次数；
- 编写 README；
- 绘制架构图；
- 整理一条最佳演示 Trace；
- 准备面试讲解稿；
- 更新简历描述。

### Day 5 验收标准

- 至少有 10 个任务的真实结果；
- 所有数字来自实际 Trace；
- 可以一条命令运行单个任务；
- 可以一条命令运行批量评测；
- README 中说明当前限制；
- 简历中不出现未完成的数据集和虚构指标。

---

## 13. 3 天压缩计划

### Day 1

- QuixBugs 任务准备；
- 代码工具；
- 测试工具；
- Single-Agent 基线；
- Patch 应用与回滚。

### Day 2

- Retrieval 和 Reproduction 并行；
- Evidence Critic；
- RootCause A/B 独立分析；
- 双向 Challenge；
- 一轮 Rebuttal；
- Critic/Arbiter 可暂时共用一个模型调用。

### Day 3

- 一个 Patch Agent；
- Patch Critique 由 RootCause Agent 或 Validator 承担；
- 真实测试；
- 一次失败重生成；
- Single-Agent 与 Adversarial Multi-Agent 对照；
- README、架构图和简历描述。

### 3 天版本必须保留

- 并行 Evidence；
- RootCause A/B 独立推理；
- A/B 双向质疑；
- 一轮 Rebuttal；
- 测试验证。

否则项目容易退化成串行多 Prompt 流水线。

---

## 14. 评测方案

## 13.1 任务结果指标

| 指标                     | 含义                   |
| ------------------------ | ---------------------- |
| Task Resolution Rate     | 测试全部通过的任务比例 |
| Patch Apply Rate         | 候选补丁成功应用比例   |
| Target Test Pass Rate    | 原失败测试修复比例     |
| Regression Pass Rate     | 原有测试保持通过比例   |
| Syntax Valid Rate        | 补丁无语法错误比例     |
| Protected Test Violation | 修改受保护测试的次数   |

## 13.2 多 Agent 协作指标

| 指标                        | 含义                                      |
| --------------------------- | ----------------------------------------- |
| Valid Challenge Rate        | 有具体证据和问题的质疑比例                |
| Critique Acceptance Rate    | 被审查 Agent 接受或部分接受质疑的比例     |
| Critique-Induced Correction | 质疑导致根因或补丁修正的次数              |
| Arbitration Accuracy        | Arbiter 选择最终成功根因或补丁的比例      |
| Evidence Request Utility    | 补充证据后任务取得进展的比例              |
| Replan Recovery Rate        | 首轮失败后重规划成功比例                  |
| Graph Routing Accuracy      | Supervisor 将任务路由到正确节点类型的比例 |
| Invalidated Node Rate       | 因状态变化而取消的无效节点比例            |
| State Recovery Success      | 从检查点恢复后继续完成任务的比例          |
| Invalid Agent Call Rate     | 无效或重复 Agent 调用比例                 |

## 13.3 系统效率指标

| 指标                   | 含义                       |
| ---------------------- | -------------------------- |
| End-to-End Latency     | 单任务总耗时               |
| Parallel Stage Latency | 并行阶段耗时               |
| Model Calls            | 模型调用次数               |
| Tool Calls             | 工具调用次数               |
| Token Usage            | 输入和输出 Token           |
| Parallel Speedup       | 相对串行执行的时延改善     |
| Cost per Solved Task   | 每个成功任务的平均调用成本 |

## 13.4 实验公平性

所有版本必须尽量保持：

- 相同基础模型；
- 相同任务输入；
- 相同工具权限；
- 相同最大调用预算；
- 相同测试环境；
- 相同随机种子集合；
- 相同评测脚本。

不能给 Full System 无限预算，再与受限 Single-Agent 比较。

---

## 15. Trace 设计

每次运行至少记录：

```json
{
  "event_id": "EVT_001",
  "task_id": "quixbugs_binary_search",
  "stage": "root_cause_challenge",
  "agent": "root_cause_b",
  "event_type": "CHALLENGE",
  "input_refs": ["H1", "E3", "E8"],
  "output_ref": "C2",
  "start_time": "...",
  "end_time": "...",
  "model_calls": 1,
  "tool_calls": 0,
  "token_usage": {
    "input": 1234,
    "output": 356
  },
  "status": "success"
}
```

### 演示 Trace 至少包含

1. 并行 Evidence Agent 开始和结束；
2. Evidence Critic 质疑；
3. RootCause A 和 B 的不同假设；
4. A Challenge B；
5. B Challenge A；
6. 一个 Agent 修正假设；
7. Arbiter 选择根因；
8. 两个候选补丁；
9. Patch 相互审查；
10. 测试结果；
11. Supervisor 最终选择或重规划。

---

## 16. 配置建议

```yaml
runtime:
  max_concurrent_agents: 3
  max_model_calls: 18
  max_tool_calls: 30
  max_replans: 1
  max_critique_rounds: 1
  max_patch_candidates: 2
  agent_timeout_seconds: 120
  tool_timeout_seconds: 60

generation:
  temperature_root_cause_a: 0.3
  temperature_root_cause_b: 0.7
  temperature_critic: 0.2
  temperature_arbiter: 0.1
  temperature_patch: 0.2

validation:
  protect_test_files: true
  run_full_regression: true
  require_syntax_check: true
```

使用不同温度只能增加一定输出差异，真正的多样性主要来自：

- 不同分析视角；
- 不同可见证据；
- 独立上下文；
- 独立工具调用；
- 不同目标约束。

---

## 17. 关键工程风险与应对

## 16.1 Agent 输出无法解析

应对：

- 使用 JSON Schema；
- 增加一次格式修复；
- 修复失败时记录原始输出并降级；
- 不因为单次解析失败导致整个任务崩溃。

## 16.2 两个根因 Agent 输出完全相同

应对：

- 使用不同 System Prompt；
- 为 A 强调运行轨迹；
- 为 B 强调调用链和接口契约；
- 第一轮提供不同证据子集；
- 禁止复制对方结论；
- 相同假设仍要求相互寻找反例。

## 16.3 Critic 只做表面评价

应对：

- 强制引用目标 Claim 和证据；
- 强制给出 required evidence 或 counterexample；
- 没有具体 objection 的质疑标记为 invalid；
- 在指标中统计 Valid Challenge Rate。

## 16.4 Agent 无限争论

应对：

- 只允许一轮 Challenge 和 Rebuttal；
- Arbiter 必须做决定；
- 最大补充证据次数为 1；
- 最大重规划次数为 1；
- 预算耗尽立即终止。

## 16.5 Patch Agent 修改测试

应对：

- 将测试目录设置为受保护路径；
- 应用补丁前检查 Diff；
- 发现修改测试直接淘汰候选补丁。

## 16.6 补丁相互污染

应对：

- 每个候选补丁使用独立临时目录或 Git worktree；
- 测试结束后销毁工作区；
- 不在主仓库直接修改。

## 16.7 并行没有降低时延

应对：

- 区分逻辑并发、请求并发和物理并行；
- 记录每个 Agent 的起止时间；
- 单 GPU 下使用异步请求和推理批处理；
- 不夸大并行加速，重点强调探索多样性和质量提升。

## 16.8 5 天内 SWE-Gym 环境无法运行

应对：

- QuixBugs 作为主评测；
- SWE-Gym 只做可选扩展；
- 简历中只写实际完成的数据集；
- README 中写明下一阶段计划。

---

## 18. 最终交付物清单

### 代码

- [ ] Single-Agent 基线；
- [ ] Supervisor；
- [ ] 3 个 Evidence Agent；
- [ ] Evidence Critic；
- [ ] 2 个 RootCause Agent；
- [ ] Challenge/Rebuttal；
- [ ] Arbiter；
- [ ] 2 个 Patch Agent；
- [ ] Patch Cross Review；
- [ ] Test Runner；
- [ ] Validator；
- [ ] 一次重规划；
- [ ] Trace 和 Metrics。

### 数据与实验

- [ ] 10–15 个 QuixBugs 任务；
- [ ] 统一任务 JSON；
- [ ] 四种版本对照；
- [ ] 真实任务结果表；
- [ ] Token 和时延统计；
- [ ] Critic 纠错统计；
- [ ] 重规划恢复统计。

### 展示材料

- [ ] README；
- [ ] 架构图；
- [ ] 一条成功修复 Trace；
- [ ] 一条根因被质疑后修正的 Trace；
- [ ] 一条测试推翻错误补丁的 Trace；
- [ ] 一条重规划 Trace；
- [ ] 运行命令；
- [ ] 简历描述；
- [ ] 2 分钟面试讲解稿。

---

## 19. README 推荐结构

```text
1. 项目背景
2. 为什么需要多 Agent
3. 系统架构
4. Agent 划分
5. Challenge–Rebuttal–Arbitration 协议
6. 共享黑板
7. 工具与沙箱
8. 快速开始
9. 运行示例
10. 实验设置
11. 结果
12. 典型 Trace
13. 消融实验
14. 当前限制
15. 后续计划
```

---

## 20. 简历描述草案

### 项目名称

**RepoPilot-MAS：基于动态任务图与对抗审查的多智能体代码修复系统**

### 项目描述

- 面向程序缺陷修复任务构建 Supervisor 驱动的动态任务图，通过节点依赖、条件边、Fork–Join、状态机和检查点机制，动态创建、暂停、恢复、取消与重试代码检索、错误复现、依赖分析、根因推理和补丁验证任务。
- 设计结构化共享黑板与版本化状态管理，记录 Evidence、Hypothesis、Critique、Rebuttal、Patch、测试结果及其依赖关系，实现 Agent 独立上下文、状态同步、执行追踪和失败后的局部恢复。
- 构建 Evidence Critic 证据审计与双 RootCause Agent 交叉质疑机制，通过 Challenge–Rebuttal–Arbitration 协议完成根因假设的相互反驳、证据补充和冲突仲裁，降低单一路径推理导致的错误归因。
- 实现 Minimal/Robust 候选补丁竞争与相互审查，并在隔离工作区中执行目标测试和回归测试；Supervisor 根据失败类型动态修改任务图，定向回退至证据、根因或补丁节点，形成测试驱动的重规划闭环。
- 在 QuixBugs Python 子集上对比 Single-Agent、固定串行 Multi-Agent、并行 Multi-Agent、动态任务图及完整对抗审查系统，统计任务解决率、Critic 纠错率、重规划恢复率、Token 消耗和端到端时延；实际提升为 **[实验完成后填写]**。

### 注意

最后一条只能填写真实实验结果，不能提前虚构数字。

---

## 21. 面试讲解主线

面试时可以按以下顺序讲：

1. 单 Agent 在仓库级修复中容易沿单一路径形成错误根因；
2. 单纯将流程拆成多个串行角色并不能体现多 Agent 价值；
3. 因此使用动态任务图管理节点依赖、状态流转、条件分支、局部回退和预算；
4. 并行探索只是任务图中的 Fork–Join 执行模式，系统还会根据证据和测试结果动态创建或取消节点；
5. 在 Evidence、RootCause 和 Patch 三个阶段引入并行与交叉审查；
6. RootCause A/B 第一轮独立分析，避免过早趋同；
7. 双方通过 Challenge 和 Rebuttal 检查证据与因果链；
8. Arbiter 不做简单投票，而是依据真实证据和质疑结果仲裁；
9. Minimal 与 Robust Patch 互相检查表面修复和过度修改；
10. 最终由真实测试而不是模型自评决定补丁是否成立；
11. 测试失败后只回退相关阶段，避免整个流程重跑；
12. 通过统一预算下的 Single-Agent 对照验证收益与成本。

---

## 22. 后续扩展计划

MVP 完成后可逐步扩展：

### 阶段 A：真实仓库任务

- 增加 SWE-Gym Lite；
- 增加 Git History Agent；
- 增加多文件依赖分析；
- 使用 Docker 复现真实 Issue。

### 阶段 B：模型训练

- 使用 SWE-Gym 成功和失败轨迹；
- 微调 Critic 或 Verifier；
- 构造错误根因和错误补丁数据；
- 训练 Arbiter 对多个候选方案排序。

### 阶段 C：完整评测

- 运行更大规模 SWE-Gym 子集；
- 运行 SWE-bench Verified 子集；
- 增加仓库隔离和时间隔离；
- 进行完整消融实验。

### 阶段 D：生产能力

- 状态持久化；
- 多用户任务队列；
- 风险操作人工确认；
- 预算和配额控制；
- 可视化 Trace；
- 断点续跑；
- 监控和告警。

---

## 23. 最终成功标准

5 天结束时，项目只有同时满足以下条件，才适合写进简历：

1. 一条命令可以运行单个任务；
2. 至少 10 个任务有真实结果；
3. Single-Agent 和完整 Multi-Agent 使用统一预算；
4. Evidence Agent 确实并行执行；
5. RootCause A/B 确实独立生成第一轮结论；
6. 至少有一个有效的双向质疑案例；
7. 至少有一个 Agent 因质疑修改了结论；
8. 至少有一个错误补丁被 Critic 或真实测试淘汰；
9. 至少有一次定向重规划；
10. 所有指标均来自 Trace；
11. README 明确当前限制；
12. 简历不声称尚未完成的训练或评测。

项目的最终卖点应当是：

> 在统一模型和工具预算下，通过动态任务图、版本化状态管理、Agent 交叉质疑、证据仲裁、候选补丁竞争和测试反馈，使系统能够根据执行状态动态调整协作路径，并让多个 Agent 发现和修正彼此的错误，而不是简单地将单 Agent 流程拆成多个串行角色。