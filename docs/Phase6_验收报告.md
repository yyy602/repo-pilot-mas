# Phase 6 验收报告

## 1. 验收结论

Phase 6 已于 2026-08-02 完成。正式运行 `phase6_quixbugs_final_v1` 生成 5 个系统 × 10 个冻结任务共 50 个结果；独立审计的 15 项门禁全部通过。

这里的“通过”表示评测过程完整、预算与安全语义正确、结果可追溯，不表示所有修复任务成功。50 个结果中 22 个成功、28 个失败；失败均保留在分母中。

## 2. 交付物检查

| 交付物 | 结果 | 证据 |
| --- | --- | --- |
| 10 个冻结测试任务 | 通过 | `data/quixbugs/phase6_suite.json` |
| development/test 隔离 | 通过 | 5 个开发任务与 10 个测试任务无交集 |
| 三个主系统 | 通过 | Local、Fixed、Dynamic 各 10 个结果 |
| 两个关键消融 | 通过 | 两个消融各 10 个结果，节点约束经机器检查 |
| 单任务与批量入口 | 通过 | `scripts/run_phase6_evaluation.py` |
| 独立结果审计 | 通过 | `scripts/audit_phase6_evaluation.py` |
| Trace 与 Checkpoint | 通过 | 50 条 Trace 可解析，40 个 Hybrid Checkpoint 存在 |
| 原始仓库完整性 | 通过 | 50 个结果的前后摘要一致 |
| 安全成功条件 | 通过 | 22 个成功结果均通过目标、回归、静态和 protected-path 门 |
| 预算失败关闭 | 通过 | 7 个越预算结果全部标记失败 |
| README、架构图、结果图 | 通过 | `README.md`、`docs/images/` |
| 框架问题复盘 | 通过 | `docs/Phase6_真实问题复盘与排障.md` |
| 面试与简历材料 | 通过 | `docs/Phase6_面试讲解.md` |

机器可读总门禁见 `reports/phase6/final_audit.json`。

## 3. 结果核对

| 系统 | 成功 | 预算违规 | Protected Path 违规 | 路径分布 |
| --- | ---: | ---: | ---: | --- |
| Local Single-Agent | 1/10 | 7 | 0 | single=10 |
| Fixed Hybrid | 2/10 | 0 | 0 | standard=7, deep=3 |
| Dynamic Hybrid | 7/10 | 0 | 0 | fast=6, standard=1, deep=3 |
| No Second Diagnostician | 6/10 | 0 | 0 | fast=5, standard=1, deep=4 |
| No Challenge/Rebuttal | 6/10 | 0 | 0 | fast=5, standard=2, deep=3 |

Local 的 7 个预算耗尽任务正确计为失败。评测器初版曾错误要求“所有任务均不得耗尽预算”，导致整场运行的原始 `acceptance.json` 为 false；独立审计保留该原始文件，并按“越预算必须失败关闭”的正确语义重新验收，没有修改任何任务结果。

## 4. 真实 API 与路由证据

Dynamic Hybrid 共记录 75 次 Supervisor API 尝试且全部成功。两个消融运行中实际发生账号槽位耗尽与切换：

- `no_second_diagnostician`：账号 1 Max 槽位耗尽后转向账号 2；
- `no_challenge_rebuttal`：账号 1、账号 2 Max 槽位先后耗尽，随后转向 Flash。

这些记录证明 Supervisor 不是本地假实现，也证明六槽位路由能在真实长批次中工作。Trace 只包含 `DASHSCOPE_API_KEY_1/2` 名称，不包含 Key 值。

## 5. 机制验收边界

最终成功标准中的 Challenge/Rebuttal、Critique 修订、错误 Patch 淘汰、定向重规划和简单任务不扩展，由 Phase 5 运行 `20260802T033236637083Z` 提供确定性机制证据。Phase 6 独立审计同时检查该运行仍通过。

Phase 6 的真实模型运行没有触发有效 Challenge，重规划也没有恢复成功。因此本报告确认机制存在和真实评测完成，但不声称对抗机制或重规划已经带来可量化提升。

## 6. 验收命令

```bash
/home/user50305/.conda/envs/multi_agent/bin/python scripts/audit_phase6_evaluation.py
/home/user50305/.conda/envs/multi_agent/bin/python -m pytest
/home/user50305/.conda/envs/multi_agent/bin/python -m ruff check .
git diff --check
```

最终结果：全量 `130 passed in 22.00s`，Ruff 输出 `All checks passed!`，`git diff --check` 通过。

## 7. 可写入与不可写入简历的结论

可以写：

- 实现 API Supervisor、确定性 Engine、本地专业 Worker 和 LangGraph Checkpoint 的分层架构；
- 在 10 个冻结 QuixBugs Python 任务上，Dynamic Hybrid 完成 7/10，Fixed Hybrid 为 2/10，本地 Single-Agent 为 1/10；
- 50 个系统—任务结果均保留 Trace，成功绑定完整真实验证，失败和预算耗尽不删样本；
- 实现真实账号/模型故障转移、结构化非法决策拒绝和 fail-closed 预算控制。

不可写：

- “成功率提升 60%”而不说明 10 个任务和基线配置；
- “第二 Diagnostician 或 Challenge 使成功率提升 10%”；
- “重规划提高恢复率”；
- “达到 SWE-bench、SWE-Gym、Java 或生产级效果”；
- “并行显著降低时延”。
