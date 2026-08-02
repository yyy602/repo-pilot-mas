# Phase 4 专业 Worker Agent 池

## 1. 阶段边界

Phase 4 实现调查、诊断、审查和候选补丁四类 Worker。Supervisor 仍是唯一全局语义决策者，LangGraph 负责执行循环和恢复，Engine 负责状态与安全约束；Worker 只接收 Task、当前节点和输入 Artifact，只能返回 `WorkerOutcome` 中的 Artifact，不能获得或修改 TaskGraph、Blackboard。

本阶段不实现 Phase 5 的动态不确定性门控、Challenge/Rebuttal、实质差异 Patch 竞争或选择决策。

## 2. Worker 契约

| Worker | mode/strategy | 输入视图 | 输出 |
| --- | --- | --- | --- |
| Investigator | `code_retrieval`、`failure_reproduction`、`dependency_trace`、`evidence_completion`、`regression_scope` | Task 摘要、目标和只读工具结果 | Evidence |
| Diagnostician | `control_flow`、`data_flow` | 经过 Blackboard 校验的 Evidence | Hypothesis |
| Reviewer | 六种审查 mode | 目标 Artifact 与直接 Evidence | Review |
| Patch | `minimal`、`robust` | 一个明确 Hypothesis、Evidence、受保护路径 | PatchCandidate |

每次 Worker 调用都新建消息上下文。两个第一轮 Diagnostician 的构造器拒绝非 Evidence 输入，因此不会读到对方 Hypothesis。Reviewer 的 Schema 和跨引用校验要求目标引用、Evidence 引用均来自输入集合。

## 3. Artifact 与 Patch 边界

`schemas/worker_artifact.py` 定义四类内容 Schema，并在 Schema 校验后继续检查：

- 置信度范围；
- mode/strategy 枚举；
- Evidence 工具 Trace；
- Hypothesis、Review、Patch 的输入引用归属；
- Patch 的 SHA-256、受保护路径检查和工作区 ID。

本地模型不直接拼接不可靠的 Git patch。PatchAgent 先通过 `inspect_code` 获取真实代码，再返回唯一文本替换的 `file_path`、`old_text`、`new_text`。确定性控制器验证旧文本只出现一次，生成 Unified Diff，最后通过 Phase 1 的 `apply_patch` 安全工具应用。最终 Artifact 中的 Diff 来自 `collect_diff`，不是模型自报。

## 4. 并发与隔离

`AsyncLangGraphRuntime` 使用异步 SQLite Checkpointer 和异步 Worker 节点。Dispatcher 对同一批 READY 节点发送多个 `Send`；WorkerPool 通过 `asyncio.to_thread` 执行本地推理，并按节点编号稳定分配模型槽位：

- 槽位 0：Qwen3-8B / `cuda:0`；
- 槽位 1：Qwen3-8B / `cuda:1`。

每个槽位有独立锁，避免同一模型对象并发推理；不同槽位可以真实重叠。模型在派发前顺序预加载，避免 Transformers 延迟导入竞态。Investigator 的临时工作区在返回后删除；Patch 工作区保留供后续验证和选择，两个候选共享同一只读基线摘要但拥有不同 worktree。

节点超时由异步 Runtime 的 `wait_for` 转换为 `TIMED_OUT` WorkerOutcome。Collector 接受成功、失败、超时等终态，统一经 Engine 写入，再唤醒 Supervisor；单个 Worker 异常或超时不会直接修改或击穿 Engine。

## 5. 可重复入口

```bash
conda activate multi_agent
python scripts/run_phase4_acceptance.py
python scripts/run_phase4_timeout_acceptance.py
```

真实 Worker 入口使用两个本地 Qwen3-8B 槽位；超时入口使用确定性延迟 Worker，只验证 Runtime 的超时隔离语义，不冒充真实模型能力结果。
