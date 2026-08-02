# Phase 3 验收报告

## 1. 验收结论

Phase 3 已于 2026-08-01 完成。真实 `SupervisorAgent` 是唯一全局主 Agent；LangGraph 负责固定执行循环、SQLite 持久化、恢复、流式事件和人工介入；`OrchestrationEngine` 是唯一确定性状态入口；动态 `TaskGraph` 继续表达 Supervisor 创建的 Worker 任务 DAG。

本阶段已经证明 Supervisor 主导的运行闭环和失败恢复边界。专业 Worker Agent 与完整代码修复闭环属于 Phase 4；Phase 3 的 Fake Worker 只用于运行时验收，当前结果不作为多 Agent 修复率或 Worker 能力结论。

## 2. 验收环境

| 项目 | 实际值 |
| --- | --- |
| Conda 环境 | `multi_agent` |
| Python | 3.10.20 |
| LangGraph | 1.2.10 |
| LangGraph SQLite Checkpointer | 3.1.1 |
| pytest | 9.1.1 |
| Ruff | 0.16.1 |
| Supervisor Provider | 阿里云百炼 OpenAI 兼容接口 |
| 真实验收模型 | `qwen3.7-max-2026-06-08` |

## 3. 验收门结果

| 计划验收项 | 结果 | 主要证据 |
| --- | --- | --- |
| 节点状态迁移与非法迁移拒绝 | 通过 | `tests/test_phase3_task_graph.py` 覆盖 PENDING、READY、RUNNING、PAUSED 和全部终态 |
| 动态创建、暂停、恢复和取消 | 通过 | `tests/test_phase3_engine.py` |
| Engine 预算与安全硬约束 | 通过 | 未满足依赖、并发、节点、工具、Token、时间和重规划预算均有测试 |
| Join 与证据版本传播 | 通过 | 混合终态可 Join；`E1@v2` 会取消依赖 `E1@v1` 的活动节点及后继 |
| Supervisor 格式修复与失败终止 | 通过 | 坏格式可修复一次；再次失败返回 `STRUCTURED_OUTPUT_ERROR` |
| DashScope 六槽位路由 | 通过 | 模型优先、账号其次；覆盖额度耗尽、瞬时限流、鉴权错误、模型错误和全部耗尽 |
| Supervisor 主导自动闭环 | 通过 | LangGraph 自动执行 Supervisor、Dispatcher、Fake Worker、Collector，再次唤醒 Supervisor |
| SQLite 重启恢复与去重 | 通过 | 并行 `N1` 成功、`N2` 崩溃后，新 Runtime 只重试 `N2`，不重复执行 `N1` |
| 可恢复人工介入 | 通过 | `interrupt` 在派发前暂停，使用同一 `thread_id` 和 `Command` 跨 Runtime 恢复 |
| 流式事件 | 通过 | `updates` 流覆盖 Supervisor、Dispatcher、Worker、Collector 和终止更新 |
| 身份与版本绑定 | 通过 | 错误 `task_id`、`thread_id`、`state_version` 及审计快照绑定全部被拒绝 |
| 检查点职责分离 | 通过 | LangGraph SQLite 决定执行恢复；自研 CheckpointStore 只导出带校验和的审计快照 |
| 凭据自动加载且不泄漏 | 通过 | `.env.supervisor` 为 `0600`、被 Git 忽略且未跟踪；凭据值在其他文件中匹配数为 0 |
| 真实 Supervisor 经 LangGraph 调用 | 通过 | 真实模型产生合法 `CREATE_TASK`，LangGraph 随后进入人工中断，Fake Worker 未运行 |

## 4. LangGraph 运行时证据

最终 Scripted 验收运行 ID 为 `20260801T155803300236Z`。

自动闭环按以下节点顺序执行：

```text
validate
→ supervisor
→ prepare_dispatch
→ worker
→ collector
→ supervisor
→ END
```

验收结果：

- Supervisor 首次创建 `N1`，Fake Worker 返回 `N1-evidence@v1`，Collector 通过 Engine 写入 Artifact 并完成节点；
- Supervisor 第二次读取包含该 Artifact 的压缩快照并结束验收循环；
- 并行恢复场景首次调用顺序为 `N1、N2`，其中 `N2` 按计划崩溃；新 Runtime 恢复时调用序列只增加一个 `N2`，证明 `N1` 没有重复执行；
- 人工介入场景在恢复前 Worker 调用数为 0，跨 Runtime 批准后仅执行 `N1`；
- SQLite 保存同一线程的状态历史，自研审计检查点继续保存可读 JSON、Trace、Router 状态和校验和。

证据位于 `reports/phase3/acceptance/scripted/20260801T155803300236Z/`。

## 5. 真实 Supervisor 调用

最终真实验收使用任务 `quixbugs_is_valid_parenthesization`，通过 LangGraph 入口命中第一槽位：

| 字段 | 实际值 |
| --- | --- |
| Run ID | `20260801T155922622926Z` |
| 模型 | `qwen3.7-max-2026-06-08` |
| 账号标识 | `DASHSCOPE_API_KEY_1` |
| 决策 | `CREATE_TASK` / `init_investigation_1` |
| 输入 Token | 1857 |
| 输出 Token | 720 |
| 延迟 | 14364 ms |
| 格式修复次数 | 0（首次响应合法） |
| 请求 ID | `chatcmpl-07a2d9b8-f23c-9e84-ac20-d783fd3d4343` |
| Engine 结果 | `DECISION_APPLIED` |
| LangGraph 状态历史 | 4 个检查点快照 |
| 下一节点 | `human_review` |
| Fake Worker 调用数 | 0 |

这次运行证明真实 Supervisor 位于 LangGraph 控制图内。运行时在 Worker 派发前触发人工中断，因此没有用 Fake Worker 冒充 Phase 4 的真实 Worker。真实响应、Trace、SQLite 数据库、报告和审计快照位于 `reports/phase3/acceptance/real_supervisor/20260801T155922622926Z/`。

## 6. 动态图与降级证据

同一最终 Scripted 运行还验证：

- 两个 Investigation 节点形成 Fork，其中一个成功、一个超时；`all_terminal` Join 正常进入 READY 并完成；
- 新版证据 `E1@v2` 追加后，依赖旧版 `E1@v1` 的 Diagnosis 节点被标记为 `CANCELLED`；
- 非法依赖 `N404` 返回 `INVALID_SUPERVISOR_DECISION`，没有污染任务图；
- 运行节点超时后进入 `TIMED_OUT`，Engine 保持活动，可由后续决策降级或重规划；
- 审计快照重新加载后的 TaskState、TaskGraph、Blackboard 和 Router 耗尽状态一致。

## 7. 自动化验证

在仓库根目录执行：

```bash
/home/user50305/.conda/envs/multi_agent/bin/python -m pytest -q
/home/user50305/.conda/envs/multi_agent/bin/python -m ruff check .
git diff --check
/home/user50305/.conda/envs/multi_agent/bin/python -m compileall -q src scripts tests
```

最终结果为全量 102 项 pytest 通过，其中 42 项为 Phase 3 专项测试、7 项专门覆盖 LangGraph 运行时；Ruff、Diff 空白检查和编译检查通过。真实凭据扫描确认：除被忽略的 `.env.supervisor` 外没有匹配文件。

## 8. 证据索引

- 机器可读汇总：`reports/phase3/acceptance_summary.json`
- 六槽位预检：`reports/phase3/preflight.json`
- 最终 Scripted 与 LangGraph 验收：`reports/phase3/acceptance/scripted/20260801T155803300236Z/`
- 最终真实 API 与 LangGraph 验收：`reports/phase3/acceptance/real_supervisor/20260801T155922622926Z/`
- 可重复入口：`scripts/run_phase3_acceptance.py`、`scripts/run_phase3_supervisor.py`
