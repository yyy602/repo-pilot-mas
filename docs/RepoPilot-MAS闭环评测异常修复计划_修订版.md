# RepoPilot-MAS 闭环评测异常修复计划（修订版）

## 1. 文档说明

本修复计划针对 `agent/hypothesis-patch-closed-loop` 分支中 `reports/closed_loop/` 暴露出的流程回归问题进行系统整改。

本版修订明确：

> 当前正式 Supervisor 路由为 `qwen3.8-max → deepseek-v4-pro-0813 → deepseek-v4-flash-0731`，使用“强模型优先、账号其次”的六槽位自动降级策略。模型自动切换属于正常运行逻辑，不视为缺陷。

因此，模型路由只需要保留完整 Trace、实际调用与 Token 统计，不列入待修复问题。闭环加固 v2 不把人民币价格作为验收指标；调用次数、Token、超时和预算失败关闭仍必须完整记录。历史 Phase 6 v1 的冻结等价成本证据保持不变。

本计划的目标不是简单提高预算、降低 Gate 严格度或回退后续闭环设计，而是修复闭环改造后暴露出的跨模块集成问题，使系统真正形成可运行、可恢复、可验证、可评测的代码修复闭环。

### 当前执行状态（2026-08-16）

- Phase A–G 的代码、Schema、契约、指标和 Trace 改造已完成；在 `multi_agent` 环境中，当前运行时通过 239 项全量测试、Ruff、compileall 与 `git diff --check`。正式预冻结还要求先提交运行时代码，当前记录因此如实为 `passed=false`。
- Smoke 和多轮针对性 Development 使用了真实 API，并持续暴露及修复框架流程问题；完整过程见 `docs/Phase6_真实问题复盘与排障.md`。
- 最后一轮 `dev_repair_v2_routes_classified` 证明两个账号 × 三个模型共六个 Supervisor 路由均返回上游免费额度耗尽；系统已能在一次策略调用后以 `SUPERVISOR_ROUTES_EXHAUSTED` 立即失败关闭，并将 failure_class 记录为 `provider_quota_exhausted`。
- 2026-08-12 15:38 的最小真实 API 复查再次按既定顺序请求六个槽位，全部返回 `AllocationQuota.FreeTierOnly`；该证据保存在 `reports/closed_loop/evaluation/api_route_recheck_20260812_1538/trace.jsonl`。
- 2026-08-16 已迁移到三模型六槽位路由；逐槽位最小真实请求中 5/6 可用，仅 `deepseek-v4-flash-0731 + DASHSCOPE_API_KEY_1` 返回免费额度耗尽，同模型账号 2 及其余四个槽位均成功。脱敏证据保存在 `reports/closed_loop/evaluation/supervisor_route_probe_20260816/`。
- 完整 5 任务 Development 尚未在最终代码指纹上运行，因而尚不具备冻结资格；代码/配置/Prompt/路由尚未冻结，10 任务 Frozen Test 也不得开始。
- 提交当前运行时代码并重新通过预冻结后，必须从本计划第 10 节“第四步：完整 Development”继续，不得把单任务协议门通过当作完整验收。
- A–G 要求、针对性真实运行和正式评测门的逐项证据见 `docs/Phase6_闭环加固v2完成性审计.md`。

---

# 2. 修复目标

修复完成后，系统应稳定执行以下主链路：

```text
Task
→ Investigation
→ Evidence
→ Diagnosis
→ Hypothesis
→ Review
→ HypothesisResolution
→ Patch
→ Validation
→ Finalization
```

同时满足：

1. Engine、Runner 和 Report 使用同一套有效预算。
2. “Agent 执行失败”和“成功复现被测程序缺陷”语义分离。
3. Agent 节点在进入 WorkerPool 前完成确定性输入契约校验。
4. Reviewer 不再因缺少 Evidence 被错误调度和重复重试。
5. Supervisor 结构化输出错误不再直接摧毁整个业务任务。
6. Hypothesis 必须由执行证据支持，而不是只依据源码片段推测。
7. Reviewer 必须进行独立根因验证，而不是复述 Diagnostician。
8. Patch 必须绑定完整 accepted Hypothesis 集合，并经过确定性 Validation。
9. Trace、Checkpoint、Artifact 和评测指标保持一致。
10. 所有修复均为通用系统逻辑，不针对具体 QuixBugs 答案写特例。

---

# 3. 当前问题概况

## 3.1 当前运行结果

| 运行范围 | 当前结果 | 主要问题 |
|---|---:|---|
| Smoke | 未进入完整闭环 | 成功复现 Bug 被误判为 Worker 失败 |
| Development | 1/5 成功 | Token 提前终止、结构化输出错误、错误 Patch |
| Frozen Test | 0/10 成功 | 全部以 `OUTPUT_TOKEN_BUDGET_EXHAUSTED` 结束 |

当前结果不能直接作为最终闭环架构能力结论，因为大量任务不是“完成完整修复后测试失败”，而是在 Hypothesis 接受、Patch 创建或 Validation 前被框架机制提前终止。

---

# 4. 根因分级

# 4.1 P0：阻断系统正确运行的问题

## P0-1 预算存在双重事实源

当前存在两套预算：

### `configs/runtime.yaml`

```yaml
orchestration:
  max_input_tokens: 200000
  max_output_tokens: 20000
  max_tool_calls: 30
```

### `configs/phase6.yaml`

```yaml
hybrid_limits:
  max_input_tokens: 600000
  max_output_tokens: 100000
  max_tool_calls: 120
```

Runner 创建 `EngineBudget` 时主要继承 `runtime.yaml`，但最终报告依据 `phase6.yaml` 判断是否超预算，因此会出现：

```text
Engine termination_reason = OUTPUT_TOKEN_BUDGET_EXHAUSTED
Report budget.within_budget = true
Summary budget_violations = 0
```

这既影响任务执行，也破坏评测可信度。

---

## P0-2 `failure_reproduction` 成功语义错误

在故障复现任务中：

```text
测试失败
```

通常意味着：

```text
目标缺陷被成功复现
```

而不是：

```text
Investigator 执行失败
```

当前模型一旦输出 `action.status=failed`，Investigator 就直接抛出 `WorkerAgentError`，继而产生：

```text
WORKER_DIED
→ ArtifactRejection
→ Retry
→ Replan
```

这使系统无法把测试异常转换为 Evidence，也无法顺利进入 Diagnosis。

---

## P0-3 Agent 输入契约缺少前置校验

Reviewer 的 `root_cause_recommendation` 模式要求：

```text
至少一个 Hypothesis
+
至少一个直接 Evidence
```

但 Supervisor 多次只传入 Hypothesis。

当前错误发现位置是在 Reviewer 真正执行之后，因此会额外消耗：

- Worker 模型调用；
- Retry；
- ArtifactRejection；
- Supervisor 决策；
- Token；
- Replan 预算。

这类确定性输入错误应在创建节点前被 Engine 拒绝。

---

## P0-4 不可修复错误与可修复错误混用同一 Retry

当前部分错误会直接原地重试：

```text
缺少输入 Artifact
输入 Artifact 类型错误
Agent mode 与输入不匹配
```

但原地重试不会改变原节点的输入，因此必然再次失败。

应区分：

| 错误类型 | 处理方式 |
|---|---|
| 模型超时、临时工具失败 | 原地 Retry |
| 输入 Artifact 缺失 | 拒绝创建或重建节点 |
| 输入类型不匹配 | Supervisor 修正后新建节点 |
| Schema 不合法 | 格式修复或重试模型 |
| 业务证据不足 | 回到 Investigation 补证据 |

---

## P0-5 Supervisor 结构化输出错误会直接终止任务

当前一次 `STRUCTURED_OUTPUT_ERROR` 在格式修复仍失败后，会让 Engine 直接进入 Failed。

这意味着：

```text
一个控制面 JSON 格式错误
```

会摧毁：

```text
已经完成的 Investigation
已经完成的 Diagnosis
已经生成的 Evidence 和 Hypothesis
```

缺少阶段 Schema 收缩、模型级重试、安全 Fallback 和可恢复 Decision Rejection。

---

## P0-6 Review 指标索引实现错误

评测逻辑按：

```text
artifact_ref
```

建立 Review Artifact 索引，但 Artifact 序列化结果主要包含：

```text
artifact_id
version
```

如果没有显式生成：

```text
{artifact_id}@v{version}
```

则 Review 永远无法被找到。

因此即使：

```text
Review verdict = supported
Hypothesis status = accepted
Validation passed
```

仍可能得到：

```text
root_cause_review_passed = false
```

---

# 4.2 P1：流程走通后仍会影响正确率的问题

## P1-1 Evidence Gate 过弱

部分任务只依据一段源码就进入 Diagnosis 和 Review，缺少：

- 失败测试；
- 失败输入；
- 实际输出；
- 异常类型；
- 执行路径；
- 对替代原因的排除。

结果是 Diagnostician 和 Reviewer可能只围绕相同源码片段进行语言推测。

---

## P1-2 Reviewer 独立性不足

当前 Reviewer 经常表现为：

```text
Diagnostician 提出根因 X
Reviewer 复述并支持根因 X
```

缺少真正的验证动作：

- 根因是否完整解释失败输入；
- 失败输出是否与根因一致；
- 是否存在替代根因；
- 因果链是否闭合；
- verification plan 是否实际执行；
- 是否能构造反例；
- 修复点是否与数据流一致。

---

## P1-3 PatchAgent 缺少局部语义推演

PatchAgent 可能生成语法合法但语义错误的 Patch，例如：

- 修改了错误变量；
- 改变生成器控制流；
- 提前 `return`；
- 修复局部测试但破坏后续元素；
- 与 accepted Hypothesis 的因果链不一致。

Validation 可以最终否决，但前置语义校验不足会增加：

- 错误 Patch 数量；
- Token；
- Replan；
- 总时延。

---

## P1-4 Hypothesis 接受条件缺少证据覆盖检查

当前应进一步校验：

```text
accepted Hypothesis 的 supporting_evidence
是否都存在
是否经过 Review
是否覆盖 direct_cause
是否存在 unresolved missing_evidence
```

不能只因为 Reviewer 输出 `supported` 就直接接受。

---

## P1-5 Validation 失败后的定向回退不够精细

不同错误应回退到不同阶段：

| failure_class | 推荐回退阶段 |
|---|---|
| `patch_apply_failure` | Patch |
| `syntax_failure` | Patch |
| `target_test_failure` | Patch 或 Diagnosis |
| `regression_failure` | Patch |
| `root_cause_invalidated` | Diagnosis |
| `insufficient_reproduction` | Investigation |
| `protected_path_violation` | Patch，并拒绝原 Patch |

当前部分任务在失败后只使用统一 Replan，容易浪费唯一一次重规划机会。

---

# 4.3 P2：Token、状态体积和可观测性问题

## P2-1 Supervisor 状态快照过长

每轮 Supervisor 请求可能包含：

- 完整 TaskSpec；
- 全部 Node；
- 全部 Artifact 内容；
- 根因状态；
- Recovery 状态；
- 完整决策历史；
- 长系统 Prompt；
- 全部 Action 的大型 `oneOf` Schema。

随着节点和 Artifact 增加，后续每轮请求持续膨胀。

这会增加：

- 输入 Token；
- 输出 Token；
- 格式错误概率；
- 响应时延；
- API 资源消耗。

---

## P2-2 模型路由记录应保持完整，但不属于故障

当前 Supervisor 按配置顺序尝试模型和账号：

```text
模型优先
→ 账号轮转
→ 配额或路由不可用时自动降级
```

这是正常设计，不需要修改为固定模型，也不需要把某个模型定义为唯一计划模型。

只要求 Trace 和评测报告完整记录：

```text
每次 route_attempt
每次 route_succeeded
每次 route_exhausted
最终每次决策实际使用的 model_id
各模型调用次数和 Token
```

此项仅属于可观测性要求，不作为性能退化根因，也不列入阻断修复项。

---

## P2-3 Recovery 指标粒度不足

当前 `recovery_attempted` 无法区分：

- Worker 原地 Retry；
- 修正输入后重建节点；
- 阶段 Replan；
- Supervisor 格式修复；
- 模型路由自动降级；
- Patch 再生成；
- Checkpoint 恢复。

应拆分不同恢复类型，才能判断具体机制是否有效。

---

## P2-4 终态原因不够结构化

部分任务只有自然语言 `reason`，应统一为：

```json
{
  "termination_code": "OUTPUT_TOKEN_BUDGET_EXHAUSTED",
  "termination_stage": "review",
  "trigger_ref": "N5.review@v1",
  "recoverable": false,
  "details": {}
}
```

便于后续统计失败分布。

---

# 5. 修复原则

## 5.1 不回退完整闭环设计

以下机制应继续保留：

- HypothesisResolution；
- Reviewer Gate；
- Patch 与 accepted Hypothesis 集合绑定；
- ArtifactRejection；
- Worker 隔离；
- Workspace 生命周期；
- Validation 最终否决；
- Checkpoint 恢复。

问题不是闭环过多，而是配套的预算、契约、恢复和评测没有同步升级。

---

## 5.2 不使用“单纯调大 Token”作为最终修复

扩大预算只会让流程继续向后运行，但不能解决：

- Reviewer 输入错误；
- 故障复现语义错误；
- 错误 Hypothesis；
- 错误 Patch；
- Review 指标错误；
- Supervisor 格式错误。

预算修复只是 P0 的第一步。

---

## 5.3 确定性规则必须由代码校验

以下规则不得只依赖 Prompt：

```text
Reviewer 必须有 Hypothesis 和 Evidence
PatchAgent 必须输入 accepted Hypothesis 集合
PatchCandidate 必须绑定 accepted_refs
Validation 必须引用有效 Patch
FINALIZE 必须存在 passed ValidationResult
```

---

## 5.4 Fail-Closed 与可恢复必须同时存在

Fail-Closed 表示非法状态不能进入下游，不代表任何错误都应立即终止整个任务。

正确行为：

```text
非法 Patch
→ 拒绝并阻断

Reviewer 输入不完整
→ 调度前拒绝，要求修正

Supervisor 格式错误
→ 格式修复或安全重试

测试成功复现缺陷
→ 生成 Evidence

Validation 失败
→ 按 failure_class 定向回退
```

---

## 5.5 禁止针对测试答案写特例

不能加入：

```text
如果任务是 flatten 就使用 yield x
如果任务是 bucketsort 就修改 enumerate(counts)
```

所有修复必须适用于一般代码修复任务。

---

# 6. 分阶段实施计划

# Phase A：统一预算与评测口径

## A1. 建立唯一有效预算

### 涉及文件

```text
configs/runtime.yaml
configs/phase6.yaml
configs/closed_loop_evaluation.yaml
scripts/run_closed_loop_final_evaluation.py
src/repo_pilot_mas/orchestration/engine.py
src/repo_pilot_mas/final_evaluation.py
```

### 实施内容

1. `closed_loop_evaluation.yaml` 明确指定正式评测预算来源。
2. Runner 创建 `EngineBudget` 时显式映射所有关键限制。
3. 不再只覆盖：
   - `max_supervisor_calls`
   - `max_runtime_seconds`
4. 同时覆盖：
   - `max_tool_calls`
   - `max_input_tokens`
   - `max_output_tokens`
5. `result.json` 同时记录：
   - configured budget；
   - effective Engine budget；
   - actual usage。
6. Summary 依据 effective budget 计算违规。

### 建议实现

```python
budget_values.update(
    max_supervisor_calls=int(limits["max_supervisor_api_calls"]),
    max_tool_calls=int(limits["max_tool_calls"]),
    max_input_tokens=int(limits["max_input_tokens"]),
    max_output_tokens=int(limits["max_output_tokens"]),
    max_runtime_seconds=float(limits["max_runtime_seconds"]),
)
```

### 验收

- Engine Snapshot 与 Report 中的预算一致。
- 不再出现：
  ```text
  OUTPUT_TOKEN_BUDGET_EXHAUSTED
  within_budget = true
  ```
- 增加 Runner 到 Engine 的预算映射单元测试。

---

## A2. 区分 Supervisor 预算与评测总预算

推荐分为：

```text
Supervisor Decision Budget
Worker Execution Budget
Evaluation Total Budget
```

结果结构建议：

```json
{
  "budget": {
    "engine": {},
    "workers": {},
    "evaluation": {}
  },
  "usage": {
    "supervisor": {},
    "workers": {},
    "total": {}
  }
}
```

避免将 Supervisor 累计 Token 与所有模型总 Token 混为一个阈值。

---

# Phase B：修复 Investigator 语义

## B1. 重构 `failure_reproduction`

### 涉及文件

```text
src/repo_pilot_mas/agents/investigator.py
src/repo_pilot_mas/schemas/worker_artifact.py
src/repo_pilot_mas/orchestration/collector.py
tests/
```

### 新 Evidence 字段

```json
{
  "mode": "failure_reproduction",
  "reproduction_attempted": true,
  "reproduction_succeeded": true,
  "test_exit_code": 1,
  "failure_type": "IndexError",
  "failure_output": "...",
  "failing_command": ["python", "-m", "pytest", "..."],
  "tool_trace_ids": []
}
```

### 语义规则

```text
Agent 执行成功 + 目标测试失败
= 成功复现 Bug

Agent 未完成 ReAct / 工具异常 / Schema 错误
= Worker 执行失败
```

### 验收

- `find_first_in_sorted` 的 IndexError 生成 Evidence。
- 不再生成 `WORKER_DIED`。
- 后续能够进入 Diagnosis。

---

## B2. 强化 Investigator Prompt

Prompt 中明确：

```text
failure_reproduction 模式下，测试出现预期失败不代表 Agent 失败。
只要成功获得可复现的失败命令、退出码和错误输出，应返回 action.status=success。
```

Prompt 只是补充，最终仍由代码解析工具结果决定。

---

# Phase C：新增 Agent 输入契约层

## C1. 新增 `agent_contracts.py`

建议新增：

```text
src/repo_pilot_mas/orchestration/agent_contracts.py
```

### 契约矩阵

| Agent / Mode | 必需输入 |
|---|---|
| Diagnostician `control_flow` | 至少 1 个 Evidence |
| Diagnostician `data_flow` | 至少 1 个 Evidence |
| Reviewer `root_cause_recommendation` | Hypothesis + Evidence |
| Reviewer `hypothesis_comparison` | 至少 2 个 Hypothesis + Evidence |
| Reviewer `patch_review` | PatchCandidate + accepted Hypothesis |
| PatchAgent | accepted Hypothesis 集合 + 根因 Review |
| ValidationExecutor | 1 个 PatchCandidate |
| Finalization | passed ValidationResult |

### 调用位置

在：

```text
OrchestrationEngine._validate_decision()
```

创建节点前解析输入 Artifact 并校验。

### 契约错误返回

```json
{
  "code": "AGENT_INPUT_CONTRACT_VIOLATION",
  "recoverable": true,
  "recommended_stage": "review",
  "allowed_next_actions": ["CREATE_TASK"]
}
```

### 验收

- 缺 Evidence 的 Reviewer 不会进入 WorkerPool。
- 不再消耗本地模型调用。
- Supervisor 能在下一轮修正输入。

---

## C2. 重试分类

新增错误分类：

```text
TRANSIENT_WORKER_ERROR
AGENT_INPUT_CONTRACT_VIOLATION
ARTIFACT_SCHEMA_ERROR
TOOL_EXECUTION_ERROR
MODEL_TIMEOUT
MODEL_FORMAT_ERROR
BUSINESS_EVIDENCE_INSUFFICIENT
```

对应处理：

| 分类 | 处理 |
|---|---|
| `MODEL_TIMEOUT` | 原地 Retry |
| `TOOL_EXECUTION_ERROR` | 限次 Retry |
| `AGENT_INPUT_CONTRACT_VIOLATION` | 修正输入后重建节点 |
| `ARTIFACT_SCHEMA_ERROR` | 模型格式修复或重试 |
| `BUSINESS_EVIDENCE_INSUFFICIENT` | 新建 Investigation |
| `MODEL_FORMAT_ERROR` | 格式恢复链路 |

---

# Phase D：增强 Supervisor 稳定性

## D1. 使用阶段化最小 Schema

新增：

```text
supervisor_decision_schema_for_state(snapshot)
```

不同阶段只提供当前合法动作。

### 示例

Diagnosis 阶段：

```text
CREATE_REVIEW_TASK
CREATE_INVESTIGATION_TASK
ACCEPT_HYPOTHESIS
TERMINATE_TASK
```

Patch 阶段：

```text
CREATE_PATCH_REVIEW
CREATE_VALIDATION_TASK
REQUEST_REPLAN
TERMINATE_TASK
```

这样减少 `oneOf` 分支数量和格式歧义。

---

## D2. Supervisor 格式错误恢复链路

```text
第一次失败
→ 同模型格式修复

仍失败
→ 使用当前阶段最小 Schema 重试

仍失败
→ 按既定模型与账号顺序继续路由

仍失败
→ 生成可恢复的 DecisionRejection
```

不得在首次结构化输出错误后直接终止任务。

---

## D3. 确定性安全 Fallback

仅允许低风险动作：

```text
已有 Hypothesis、缺 Review
→ 创建 Reviewer，并自动补齐 supporting Evidence

已有 accepted Hypothesis、缺 Patch
→ 创建 PatchTask

已有 Patch、缺 Validation
→ 创建 ValidationTask
```

禁止 Fallback 自动执行：

```text
ACCEPT_HYPOTHESIS
SELECT_PATCH
FINALIZE_TASK
```

这些仍需满足真实 Artifact 条件。

---

## D4. 保留现有模型路由策略

模型路由继续按照当前配置工作，不固定某个模型。

只增加报告字段：

```json
{
  "route_usage": {
    "attempts_by_model": {},
    "succeeded_by_model": {},
    "exhausted_by_model": {}
  }
}
```

不将 Fallback 视为错误或实验污染。

---

# Phase E：强化 Evidence 与 Reviewer

## E1. 最低 Evidence Gate

接受 Hypothesis 前至少要求：

```text
源码定位 Evidence
+
失败复现或明确错误行为 Evidence
+
失败输出或执行路径 Evidence
```

简单任务允许同一个 Evidence 覆盖多个类别，但必须有成功工具 Trace 支撑。

---

## E2. Evidence 类型化

增加：

```json
{
  "evidence_kind": "source|reproduction|execution|dependency|counterexample",
  "supports_claims": [],
  "contradicts_claims": [],
  "verified": true
}
```

避免所有 Evidence 都只是一个笼统的 `claim`。

---

## E3. Reviewer 独立验证字段

Review Artifact 增加：

```json
{
  "failure_explained": true,
  "causal_chain_complete": true,
  "alternative_causes": [],
  "counterexample_checked": true,
  "verification_steps_executed": [],
  "remaining_uncertainty": []
}
```

### `supported` 的最低条件

1. 能解释真实失败输入和输出；
2. 根因与源码位置、执行证据一致；
3. 至少检查一个替代原因或反例；
4. verification plan 至少执行一项；
5. 没有未解决的关键 Evidence 缺口。

---

## E4. HypothesisResolution Gate

接受前确定性校验：

```text
candidate_ref 存在
supporting_evidence 全部存在
Review target 指向该 Hypothesis
Review verdict 可接受
Review evidence_refs 覆盖必要 Evidence
missing_evidence 不包含阻断项
primary_ref 属于 accepted_refs
```

---

# Phase F：强化 Patch 与 Validation

## F1. Patch 前置语义检查

PatchAgent 在返回 Patch 前必须提供：

```json
{
  "pre_patch_behavior": "...",
  "post_patch_expected_behavior": "...",
  "failure_input_walkthrough": "...",
  "semantic_rationale": "...",
  "precheck_command": [],
  "precheck_result": {}
}
```

至少完成：

1. 读取目标函数完整上下文；
2. 推演控制流或数据流；
3. 说明修改如何消除 direct cause；
4. 执行最小检查；
5. 检查受保护路径；
6. 绑定全部 `accepted_refs`。

---

## F2. Patch Binding Gate

确定性要求：

```text
set(Patch.based_on_hypothesis_refs)
==
set(HypothesisResolution.accepted_refs)
```

同时：

```text
primary_hypothesis_ref
必须属于 accepted_refs
```

不满足则生成 `ArtifactRejection`，不得进入 Validation。

---

## F3. Validation 定向回退

ValidationResult 增加标准化：

```json
{
  "failure_class": "regression_failure",
  "recommended_stage": "patch",
  "invalidated_refs": [],
  "recoverable": true
}
```

Supervisor 按 failure_class 决定回退，不再消耗无意义的全局 Replan。

---

# Phase G：修复评测与 Trace

## G1. 修复 Artifact Ref 索引

统一使用：

```python
artifact_ref = f"{artifact_id}@v{version}"
```

构建索引。

### 验收

GCD 这类任务必须统计为：

```text
hypothesis_accepted = true
root_cause_review_passed = true
patch_bound_to_accepted_set = true
patch_validation_passed = true
```

---

## G2. 指标分层

### 业务结果

- task_resolution_rate
- target_test_pass_rate
- regression_pass_rate
- patch_success_rate

### 闭环机制

- evidence_gate_pass_rate
- hypothesis_accept_rate
- review_pass_rate
- patch_binding_rate
- validation_pass_rate

### 异常恢复

- worker_retry_count
- contract_rejection_count
- node_rebuild_count
- supervisor_format_repair_count
- replan_attempt_count
- replan_success_rate

### 路由与资源

- route_attempts_by_model
- route_successes_by_model
- route_exhausted_by_model
- supervisor_tokens
- worker_tokens
- total_tokens
- latency_by_stage

模型自动 Fallback 只作为路由统计，不作为失败项。

---

## G3. 新增 Trace 事件

```text
effective_budget_loaded
agent_input_contract_checked
agent_input_contract_rejected
bug_reproduction_confirmed
supervisor_schema_reduced
supervisor_format_repair_attempted
supervisor_decision_recovery
evidence_gate_checked
review_independent_verification
patch_precheck_completed
validation_replan_classified
metric_artifact_ref_built
```

---

# 7. 文件级修改清单

| 文件 | 修改内容 |
|---|---|
| `configs/runtime.yaml` | 明确默认运行时预算，避免与正式评测冲突 |
| `configs/phase6.yaml` | 保留冻结评测预算 |
| `configs/closed_loop_evaluation.yaml` | 声明有效预算来源与协议版本 |
| `configs/supervisor.yaml` | 保留现有模型路由，增加格式恢复设置 |
| `scripts/run_closed_loop_final_evaluation.py` | 显式映射预算、统一实际使用口径，并拒绝 dirty runtime 与未绑定 commit 的冻结运行 |
| `scripts/run_phase6_pre_freeze_checks.py` | 统一生成 pytest、Ruff、compileall、diff 和 clean-tree 预冻结记录 |
| `scripts/audit_closed_loop_final_evaluation.py` | 独立重算 Suite、冻结身份、原始证据及高层指标 |
| `src/repo_pilot_mas/orchestration/engine.py` | 契约校验、错误分类、可恢复 Decision |
| `src/repo_pilot_mas/orchestration/agent_contracts.py` | 新增 Agent 输入契约 |
| `src/repo_pilot_mas/orchestration/worker_recovery.py` | 收集 WorkerOutcome，区分可重试错误、需重建错误与 ArtifactRejection |
| `src/repo_pilot_mas/agents/investigator.py` | 修正复现任务语义 |
| `src/repo_pilot_mas/agents/reviewer.py` | 独立验证规则 |
| `src/repo_pilot_mas/agents/patch_agent.py` | 语义推演和预检查 |
| `src/repo_pilot_mas/agents/supervisor.py` | 阶段化 Schema、格式错误恢复 |
| `src/repo_pilot_mas/schemas/worker_artifact.py` | 扩展 Evidence、Review、Patch Schema |
| `src/repo_pilot_mas/final_evaluation.py` | 修复 Ref 索引、指标分层 |
| `tests/` | 新增预算、契约、复现、恢复和指标测试 |

---

# 8. 测试计划

## 8.1 基础检查

```bash
python -m compileall -q src tests
ruff check .
pytest -q
```

---

## 8.2 必增单元测试

### Budget

```text
test_final_runner_maps_all_hybrid_limits
test_engine_and_report_use_same_effective_budget
test_budget_exhausted_cannot_be_reported_within_budget
```

### Investigator

```text
test_reproduction_failure_is_successful_evidence
test_tool_failure_is_worker_failure
test_reproduction_evidence_contains_exit_code_and_output
```

### Agent Contract

```text
test_root_cause_review_requires_hypothesis_and_evidence
test_patch_requires_accepted_hypothesis_set
test_validation_requires_patch_candidate
test_contract_error_is_rejected_before_dispatch
```

### Retry

```text
test_transient_worker_error_can_retry
test_contract_error_does_not_retry_same_node
test_corrected_inputs_create_new_node
```

### Supervisor

```text
test_stage_specific_schema_only_exposes_legal_actions
test_structured_output_error_is_recoverable
test_safe_fallback_does_not_auto_accept_hypothesis
test_model_fallback_is_recorded_not_treated_as_failure
```

### Metrics

```text
test_artifact_ref_is_built_from_id_and_version
test_supported_review_is_counted
test_rejected_review_is_not_counted
test_route_fallback_does_not_reduce_task_success
```

---

# 9. 集成验证任务

## 9.1 GCD

目的：

```text
验证标准闭环不被新修复破坏
```

要求：

```text
Evidence
→ Hypothesis
→ Review
→ Accept
→ Patch
→ Validation
→ Success
```

---

## 9.2 Find First In Sorted

目的：

```text
验证 failure_reproduction 语义
```

要求：

- IndexError 转化为 reproduction Evidence；
- 不产生错误 `WORKER_DIED`；
- 能进入 Diagnosis。

---

## 9.3 Flatten

目的：

```text
验证 Reviewer 契约与错误 Patch 拦截
```

要求：

- Reviewer 初次创建即包含 Evidence；
- 不重复执行同一错误 Reviewer；
- 错误 Patch 必须被 Validation 否决；
- Patch 回退到正确阶段。

---

## 9.4 Bucketsort

目的：

```text
验证根因审查是否真正基于数据流
```

要求：

- Review 检查 `arr` 与 `counts` 的数据流关系；
- 不能只复述 Hypothesis；
- 必须依据失败输入验证根因。

---

## 9.5 Is Valid Parenthesization

目的：

```text
验证 Supervisor 结构化错误恢复
```

要求：

- 已有 Evidence 和 Hypothesis 不丢失；
- 一次格式错误不终止整个任务；
- 能继续创建 Review 或进入后续阶段。

---

# 10. 重新评测流程

## 第一步：完成 P0 单元测试

必须先通过：

```text
预算一致
复现语义
输入契约
Retry 分类
Review 指标
Supervisor 格式恢复
```

---

## 第二步：Smoke

```bash
/home/user50305/.conda/envs/multi_agent/bin/python \
  scripts/run_closed_loop_final_evaluation.py \
  --split development \
  --task-id quixbugs_find_first_in_sorted \
  --run-id smoke_repair_v2
```

通过条件：

- 复现成功生成 Evidence；
- 不出现错误 Worker Failure；
- 进入 Diagnosis；
- Engine 和报告预算一致。

---

## 第三步：针对性 Development

依次运行：

```text
gcd
flatten
bucketsort
is_valid_parenthesization
```

分别验证：

- 标准闭环；
- Reviewer 契约；
- Patch 质量；
- 根因独立审查；
- Supervisor 恢复。

---

## 第四步：完整 Development

```bash
/home/user50305/.conda/envs/multi_agent/bin/python \
  -m scripts.run_phase6_pre_freeze_checks

/home/user50305/.conda/envs/multi_agent/bin/python \
  scripts/run_closed_loop_final_evaluation.py \
  --split development \
  --run-id dev_repair_v2_full \
  --verification-record reports/closed_loop/verification/pre_freeze_checks.json
```

建议进入 Frozen Test 的最低门槛：

```text
Development 至少 4/5
框架级统一终止错误为 0
Reviewer 输入契约错误为 0
预算口径矛盾为 0
Workspace Cleanup Rate = 100%
Source Integrity Violations = 0
```

---

## 第五步：冻结版本

记录：

```text
Git commit SHA
配置 SHA256
Prompt SHA256
模型路由配置
单元测试结果
Development 结果
```

---

## 第六步：Frozen Test

```bash
/home/user50305/.conda/envs/multi_agent/bin/python \
  scripts/run_closed_loop_final_evaluation.py \
  --split test \
  --run-id closed_loop_repair_test_v2 \
  --freeze-manifest reports/closed_loop/evaluation/dev_repair_v2_full/manifest.json
```

正式运行期间不得依据单个 Test 任务修改规则或 Prompt。

---

## 第七步：独立最终审计

```bash
/home/user50305/.conda/envs/multi_agent/bin/python \
  scripts/audit_closed_loop_final_evaluation.py \
  --development-root reports/closed_loop/evaluation/dev_repair_v2_full \
  --test-root reports/closed_loop/evaluation/closed_loop_repair_test_v2 \
  --output reports/closed_loop/audit/final_audit_v2.json
```

审计器必须从与 Runner 相同的 `configs/closed_loop_evaluation.yaml` 解析 Suite，确认 5 个 Development 与 10 个 Frozen Test 的身份、顺序和互斥关系，并对预冻结检查记录、冻结 Gate、运行时指纹、Trace、Checkpoint、预算、安全验证和凭据泄漏重新计算，不能直接信任 Runner 的 `passed` 字段。

---

# 11. 验收标准

## P0 验收

- [x] Engine 与 Report 使用相同有效预算。
- [x] 不存在隐藏的 20000 Token 提前终止。
- [x] 成功复现 Bug 不再产生 `WORKER_DIED`。
- [x] Reviewer 缺少 Evidence 时在调度前被拒绝。
- [x] 确定性输入错误不会原地重试。
- [x] Supervisor 格式错误具备恢复路径。
- [x] Review 指标与实际 Artifact 一致。

## P1 验收

- [x] Hypothesis 接受前具备源码与复现证据。
- [x] Reviewer 完成替代原因或反例检查。
- [x] Patch 包含修改前后行为推演。
- [x] Patch 始终绑定完整 accepted Hypothesis 集合。
- [x] Validation 失败能够按 failure_class 定向回退。

## P2 验收

- [x] Supervisor 快照具备压缩与压缩率量化；完整 Development 仍需通过压缩率门。
- [x] 路由 Trace 完整记录每个模型与账号尝试。
- [x] 正常模型 Fallback 不被统计为任务失败。
- [x] Recovery 指标能够区分不同恢复方式。
- [x] 终态错误具有标准化 code、stage 和 failure_class。

## 系统验收

- [x] 针对性真实运行 Workspace Cleanup Rate = 100%；完整 Development 仍需复验。
- [x] 针对性真实运行 Source Integrity Violations = 0；完整 Development 仍需复验。
- [x] Trace 可以还原已执行的完整状态转换和失败链路。
- [x] 所有新终态均能定位到具体阶段和 failure_class。
- [ ] Frozen Test 不再出现统一框架性失败。

---

# 12. 实施顺序

```text
1. 统一预算事实源
2. 修复 failure_reproduction 语义
3. 增加 Agent 输入契约
4. 修复 Retry 分类
5. 修复 Review 指标
6. 增加 Supervisor 格式恢复
7. 使用阶段化最小 Schema
8. 强化 Evidence Gate
9. 强化 Reviewer 独立验证
10. 增加 Patch 语义预检查
11. 完善 Validation 定向回退
12. 压缩 Supervisor Snapshot
13. 重跑 Smoke
14. 重跑 Development
15. 冻结代码与配置
16. 重跑 Frozen Test
```

前五项完成前，不建议再次运行完整 Frozen Test。

---

# 13. Commit 拆分建议

```text
fix/closed-loop-budget-source
fix/reproduction-semantics
feat/agent-input-contracts
fix/retry-error-classification
fix/review-metric-index
fix/supervisor-structured-output-recovery
feat/stage-specific-supervisor-schema
feat/evidence-review-quality-gates
feat/patch-semantic-precheck
fix/validation-targeted-replan
perf/supervisor-snapshot-compaction
```

每个 Commit 要求：

- 可独立回滚；
- 不修改冻结测试答案；
- 附带单元测试；
- Commit Message 说明问题、修改范围和验证命令。

---

# 14. 最终目标架构

```text
Supervisor
只负责结构化全局决策
        ↓
TaskGraph
确定依赖与 Ready 节点
        ↓
Agent Contract Validator
在执行前校验输入类型和数量
        ↓
WorkerPool
执行具体 Agent
        ↓
Collector
校验 Artifact、隔离非法结果
        ↓
Blackboard
形成唯一事实源
        ↓
Reviewer
依据真实 Evidence 独立验证根因
        ↓
HypothesisResolution
接受一个或多个根因
        ↓
PatchAgent
绑定完整 accepted set 生成 Patch
        ↓
ValidationExecutor
真实目标测试与回归拥有最终否决权
        ↓
Failure Classifier
按错误类型定向恢复
        ↓
Trace / Checkpoint / Report
保持预算、状态和指标一致
```

修复后的核心亮点应是：

- 结构化通信；
- 确定性输入契约；
- 证据驱动根因确认；
- 独立 Reviewer；
- Patch 与根因强绑定；
- Fail-Closed 与可恢复并存；
- 多模型自动路由；
- 统一预算治理；
- 可追踪、可复现、可审计的完整闭环。
