# Phase 4 验收报告

## 1. 验收结论

Phase 4 已于 2026-08-02 完成。最终真实运行通过全部 8 项 Worker 门禁；独立超时运行通过全部 4 项 Runtime 门禁。四类 Worker、双 GPU 异步执行、独立诊断上下文、引用审查、双 Patch 工作区和 Schema 契约均已有代码、测试、Trace、SQLite 检查点及机器可读报告。

本结论不代表 Phase 5 已完成。最终两个 Patch 虽来自独立实例和工作区，但产生了相同修复；“实质不同 Patch 竞争、Challenge/Rebuttal 与动态门控”仍属于 Phase 5。

## 2. 验收环境

| 项目 | 实际值 |
| --- | --- |
| Conda 环境 | `multi_agent` |
| Python | 3.10.20 |
| 本地 Worker 模型 | Qwen3-8B |
| 模型设备 | `cuda:0`、`cuda:1` |
| LangGraph | 1.2.10 |
| 异步检查点 | AsyncSqliteSaver / aiosqlite 0.22.1 |
| pytest | 9.1.1 |
| Ruff | 0.16.1 |

## 3. 真实 Worker 运行

最终 Run ID：`20260802T010050479360Z`，任务：`quixbugs_is_valid_parenthesization`。

| 节点 | Worker / mode | 设备 | 模型耗时 | 结果 |
| --- | --- | --- | ---: | --- |
| N1 | Investigator / code_retrieval | cuda:0 | 22039 ms | Evidence 成功 |
| N2 | Investigator / failure_reproduction | cuda:1 | 23979 ms | Evidence 成功 |
| N3 | Diagnostician / control_flow | cuda:0 | 8086 ms | Hypothesis 成功 |
| N4 | Diagnostician / data_flow | cuda:1 | 9082 ms | Hypothesis 成功 |
| N5 | Reviewer / root_cause_recommendation | cuda:0 | 5410 ms | Review 成功 |
| N6 | Patch / minimal | cuda:1 | 28751 ms | PatchCandidate 成功 |
| N7 | Patch / robust | cuda:0 | 24038 ms | PatchCandidate 成功 |

N1 从 `01:00:59.787161Z` 运行到 `01:01:21.826588Z`，N2 从 `01:00:59.787532Z` 运行到 `01:01:23.767112Z`，有约 22 秒真实模型时间重叠。N3/N4 与 N6/N7 也分别并发执行。

两个 Patch 工作区 ID 分别为 `N6-3c498ea01e`、`N7-bccf1f3c89`，只读基线摘要一致但 worktree 路径不同。两者均只修改 `is_valid_parenthesization.py`，受保护路径检查为 true，目标测试退出码均为 0。Patch Diff SHA-256 均为 `1b021c40d20910e6404ce2d2631e3164dd9f171ff6b6cd2238c972933b7c48e7`。

## 4. 验收门

| 计划验收项 | 结果 | 证据 |
| --- | --- | --- |
| 两个 Investigator 真实重叠 | 通过 | 模型槽位 acquire/release UTC 时间窗 |
| 单 Worker 超时不使 Engine 崩溃 | 通过 | 独立运行 N1=SUCCEEDED、N2=TIMED_OUT、Runtime=terminated |
| Worker 只返回 Artifact | 通过 | WorkerPool 只接收序列化输入并返回 WorkerOutcome；不可变测试通过 |
| Collector 后再次唤醒 Supervisor | 通过 | 4 次 `worker_artifacts_collected` 后均继续 Supervisor；超时场景同样通过 |
| 两个 Diagnostician 第一轮隔离 | 通过 | N3/N4 context 的 input_types 均只有两项 `evidence` |
| Reviewer 引用目标与 Evidence | 通过 | N5 Review 的引用均属于 input_refs |
| 两个 Patch 工作区互不污染 | 通过 | 两个 workspace ID、worktree 不同，基线摘要一致，源目录摘要未变 |
| 所有输出 Schema 合法 | 通过 | 7 个节点成功、7 个 Artifact 通过类型 Schema 与跨引用校验 |

## 5. 超时隔离运行

`reports/phase4/timeout_acceptance/acceptance.json` 记录一个 0.01 秒 Worker 和一个 0.20 秒 Worker 在 0.05 秒节点上限下的结果。慢节点被 Runtime 转换为 `TIMED_OUT`；快节点的 Evidence 正常进入 Blackboard；Collector 处理混合终态并唤醒 Supervisor，Engine 没有异常退出。

## 6. 自动化验证

```bash
/home/user50305/.conda/envs/multi_agent/bin/python -m pip check
/home/user50305/.conda/envs/multi_agent/bin/python -m pytest -q
/home/user50305/.conda/envs/multi_agent/bin/python -m ruff check .
git diff --check
/home/user50305/.conda/envs/multi_agent/bin/python -m compileall -q src scripts tests
```

最终全量 110 项 pytest 通过，其中 Phase 4 专项 8 项；Ruff、编译、Diff 空白、依赖完整性均通过。最新 7 个 Artifact 重新按当前 Schema 校验为 7/7，两个 PatchCandidate 与 Engine 的哈希、受保护路径契约校验为 2/2；真实凭据在 `.env.supervisor` 之外的匹配数为 0。

## 7. 证据索引

- 总汇：`reports/phase4/acceptance_summary.json`
- 真实 Worker 汇总：`reports/phase4/acceptance/latest_summary.json`
- 最终真实运行：`reports/phase4/acceptance/20260802T010050479360Z/`
- 超时隔离：`reports/phase4/timeout_acceptance/`
- 可重复入口：`scripts/run_phase4_acceptance.py`、`scripts/run_phase4_timeout_acceptance.py`
