# Phase 6：评测、观测与简历交付

## 1. 阶段目标

Phase 6 不再增加新的 Agent 角色，而是回答三个问题：

1. RepoPilot-MAS 是否能在冻结公开任务上完整运行；
2. Dynamic Hybrid 相比 Single-Agent 和 Fixed Hybrid 的效果、成本与失败形态是什么；
3. 哪些机制只证明“实现存在”，哪些机制已经获得真实模型运行证据。

本阶段使用 QuixBugs Python，固定上游提交 `4257f44b0ff1181dedaedee6a447e133219fcebf`。5 个 Phase 2 任务只作 development split，新增 10 个任务组成冻结 test split，二者不交叉。

## 2. 评测协议

协议文件：

- 套件：`data/quixbugs/phase6_suite.json`；
- 预算与计价快照：`configs/phase6.yaml`；
- 批量入口：`scripts/run_phase6_evaluation.py`；
- 独立审计：`scripts/audit_phase6_evaluation.py`。

正式运行固定 `seed=0`、temperature=0，并执行五个系统：

| 系统 | 作用 |
| --- | --- |
| `local_single_agent` | 本地 Qwen3-8B 低成本部署基线 |
| `fixed_hybrid` | 固定拓扑；与 Proposed 共享 Supervisor/Worker 池及最大预算 |
| `dynamic_hybrid` | API Supervisor + Engine + 本地 Worker 的目标系统 |
| `no_second_diagnostician` | 最多创建一个 Diagnosis 节点 |
| `no_challenge_rebuttal` | 禁止 Challenge/Rebuttal 节点 |

失败、超时和预算耗尽均保留并计入分母。预算耗尽不等于评测无效：只要它被标记为失败、没有越过安全门，即为有效的 fail-closed 结果。

## 3. 可复现入口

开发集单任务验证：

```bash
conda activate multi_agent
python scripts/run_phase6_evaluation.py \
  --split development \
  --systems dynamic_hybrid \
  --task-id quixbugs_gcd
```

正式冻结评测：

```bash
conda activate multi_agent
python scripts/run_phase6_evaluation.py \
  --run-id phase6_quixbugs_final_v1
```

独立复核并导出精简证据：

```bash
conda activate multi_agent
python scripts/audit_phase6_evaluation.py \
  --run-root reports/phase6/evaluation/phase6_quixbugs_final_v1
```

真实 Key 由 `.env.supervisor` 自动加载。正式运行已在显式移除两个同名进程环境变量后完成，证明入口读取的是被 Git 忽略的凭据文件；报告只记录环境变量名。

## 4. 冻结结果

运行 ID：`phase6_quixbugs_final_v1`。

| 系统 | 解决数 | 成功率 | Token | API 调用 | 工具调用 | 中位时延 | 等价 API 成本 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Local Single-Agent | 1/10 | 10% | 389,473 | 0 | 118 | 47,893 ms | ¥0 |
| Fixed Hybrid | 2/10 | 20% | 306,831 | 5 | 105 | 99,339 ms | ¥0.503904 |
| Dynamic Hybrid | 7/10 | 70% | 887,520 | 75 | 133 | 245,337 ms | ¥8.940828 |
| No Second Diagnostician | 6/10 | 60% | 978,324 | 79 | 139 | 276,847 ms | ¥9.471288 |
| No Challenge/Rebuttal | 6/10 | 60% | 1,020,534 | 79 | 139 | 255,327 ms | ¥8.985158 |

动态系统在这个固定的 10 任务、单 seed 协议上比 Fixed Hybrid 多解决 5 个任务，比本地 Single-Agent 多解决 6 个任务。它同时付出了明显更高的 Token、API 成本和端到端时延，因此不能只报告成功率而忽略代价。

成本使用 2026-08-02 保存的阿里云百炼公开单价快照估算。免费额度下账单可能为零，表中数字只用于可比较的等价成本。

## 5. 路径与机制观测

Dynamic Hybrid 的后验路径为 `fast=6`、`standard=1`、`deep=3`。10 个任务均预标为小型简单任务，其中 4 个仍发生非 fast 扩展，因此不必要扩展为 4/10；这说明动态门不是“永不浪费”，只是没有像固定流水线那样在所有任务上走完整路径。

Fixed Hybrid 记录到 23 对 Worker 时间区间重叠，证明固定流水线存在真实并行；Dynamic Hybrid 只有 1 对重叠，其当前真实路径主要是串行按需扩展，不能宣称获得显著并行加速。

API 路由 Trace 真实记录了账号 1 槽位耗尽后切换账号 2，随后切换 Flash 的过程。未发生明文 Key 记录。

## 6. 消融该如何解释

两个消融都按预注册约束运行完毕，但本次样本不能给出强因果结论：

- Dynamic Hybrid 自身的 10 个任务也都只创建了一个 Diagnostician，因此 `no_second_diagnostician` 没有真正移除已执行机制；7/10 与 6/10 的差值不能归因于第二诊断者。
- Dynamic Hybrid 没有产生有效 Challenge/Rebuttal，因此 `no_challenge_rebuttal` 同样没有移除实际触发的机制；7/10 与 6/10 不能证明 Challenge 带来一个任务的提升。
- 两个消融走到了不同 API 账号/模型槽位，且只运行一个 seed，随机和服务端差异足以影响单任务结果。

所以本项目只声称“消融入口和约束真实执行”，不声称两个机制获得统计意义上的效果证明。

## 7. Phase 5 与 Phase 6 证据边界

Phase 5 确定性机制案例证明：双向 Challenge、Rebuttal、Hypothesis 修订、双 Patch 竞争、回归测试淘汰错误补丁、定向重规划和无进展终止均能闭环执行。

Phase 6 真实模型评测证明：API Supervisor 确实参与动态规划，Worker 与工具真实运行，冻结任务结果与成本可追溯。但真实测试集中有效 Challenge 数为 0，3 次动态重规划恢复数为 0。因此不能把 Phase 5 的机制可达性写成 Phase 6 的真实收益。

## 8. 交付证据

- 独立审计：`reports/phase6/final_audit.json`；
- 系统汇总：`reports/phase6/results_summary.json`；
- 50 个任务结果索引：`reports/phase6/task_results.csv`；
- 可读结果表：`reports/phase6/results.md`；
- 演示 Trace 索引：`reports/phase6/trace_index.json`；
- 框架排障复盘：`docs/Phase6_真实问题复盘与排障.md`；
- 面试与简历材料：`docs/Phase6_面试讲解.md`。

完整运行目录约 44 MB，默认不提交，其中包含每任务结果、原始模型响应引用、LangGraph SQLite Checkpoint、隔离工作区与完整 Trace。仓库只保存审计摘要和四条经过凭据扫描的真实演示 Trace。

## 9. 当前限制

- 只有 10 个小型 Python 算法任务，没有多文件真实 Issue；
- 只运行一个随机种子，没有置信区间或显著性检验；
- Local Single-Agent 与 Dynamic Hybrid 使用的决策模型不同，二者是部署对比，不是纯架构消融；
- Fixed Hybrid 的固定控制步骤多由确定性策略执行，API 只负责语义选择，因此实际 API 调用量明显低于 Dynamic Hybrid；
- 有效对抗与重规划恢复尚未在冻结真实模型运行中出现；
- 动态系统时延和等价成本高于两个基线；
- 尚未评测 SWE-bench Verified、SWE-Gym Lite、Java 或 C 项目。
