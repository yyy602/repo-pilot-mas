# Phase 5 验收报告

## 1. 验收结论

Phase 5 已于 2026-08-02 完成。最终确定性机制运行的 15 项门禁全部通过，覆盖动态扩展记录、实质不同根因、双向 Challenge、一轮 Rebuttal、版本化结论修订、PatchAgent 双向审查与 Reviewer 汇总、双 Patch 真实回归淘汰、定向重规划、预算和无进展终止，以及 fast/deep 后验分类。

本结论只表示机制闭环已实现，不表示 Phase 6 的真实模型批量评测已经完成。Scripted Supervisor 和确定性认知输出仅用于排除模型随机性；Patch 的目标测试、完整回归、静态检查、Diff、哈希和保护路径检查均是真实执行。

## 2. 最终运行

最终 Run ID：`20260802T033236637083Z`。

| 场景 | 节点 | Artifact | 路径 | 结果 |
| --- | ---: | ---: | --- | --- |
| 深路径对抗闭环 | 16 | 18 | `deep` | 成功并选择 Robust Patch |
| 简单任务未扩展 | 4 | 4 | `fast` | 成功，无 Challenge/Reviewer/双 Patch |
| 定向重规划 | 1 个 Replan 节点 | 1 个 ReplanRecord | `deep` | `both_patches_fail_target` 返回 Diagnosis |
| 无进展终止 | 1 个实际任务节点 | 无新增 Artifact | — | 两次 `NO_PROGRESS_LOOP` 后失败终止 |

深路径中，Supervisor 的根因接受决定同时引用 Evidence、两份 Challenge、两份 Rebuttal 和 Reviewer 建议；两个 Diagnostician 均从 v1 修订为 v2。Minimal 与 Robust 两侧 PatchAgent 分别交叉审查对方候选，N14 Reviewer 再汇总风险。五次动态图扩展均保存触发 Artifact、理由、新增节点数和预算影响。

## 3. 真实测试推翻错误 Patch

两个 PatchCandidate 的 Diff SHA-256 不同，并位于不同受管工作区：

| Patch | 目标测试 | 完整回归 | 静态检查 | 结论 |
| --- | ---: | ---: | ---: | --- |
| Minimal | 0 | 1 | 0 | `regression_failure`，淘汰 |
| Robust | 0 | 0 | 0 | 全部门通过，最终选择 |

这证明目标测试通过不能替代完整回归，也证明 Reviewer 建议不能覆盖真实测试否决。最终 Engine 绑定 `N11.patch@v1` 与 `N16.validation@v1`，原始机制仓库摘要保持不变，三个候选工作区均在验证后销毁。

## 4. 重规划与终止

- `both_patches_fail_target` 只能返回 Diagnosis；错误返回 Patch 会被 Engine 拒绝。
- 第一次合法重规划生成 `replan.1@v1`，记录来源阶段、目标阶段、失败类别、触发引用和剩余预算。
- 第二次重规划超过 `max_replans=1` 后，Engine 以 failed 状态终止。
- 相同决策指纹连续两次无进展后，Engine 以 failed 状态终止。

## 5. 自动化验证

```bash
/home/user50305/.conda/envs/multi_agent/bin/python scripts/run_phase5_acceptance.py
/home/user50305/.conda/envs/multi_agent/bin/python -m pytest
/home/user50305/.conda/envs/multi_agent/bin/python -m ruff check .
/home/user50305/.conda/envs/multi_agent/bin/python -m compileall -q src scripts tests
/home/user50305/.conda/envs/multi_agent/bin/python -m pip check
git diff --check
```

最终全量 118 项 pytest 通过，其中 Phase 5 专项 8 项；Ruff 无告警。

## 6. 证据索引

- 汇总：`reports/phase5/acceptance/latest_summary.json`
- 完整报告：`reports/phase5/acceptance/20260802T033236637083Z/acceptance.json`
- Challenge–Rebuttal 与双 Patch Trace：`reports/phase5/acceptance/20260802T033236637083Z/deep_trace.jsonl`
- 简单任务未扩展 Trace：`reports/phase5/acceptance/20260802T033236637083Z/simple_trace.jsonl`
- 定向重规划 Trace：`reports/phase5/acceptance/20260802T033236637083Z/replan_trace.jsonl`
- 无进展终止 Trace：`reports/phase5/acceptance/20260802T033236637083Z/no_progress_trace.jsonl`
- 可重复入口：`scripts/run_phase5_acceptance.py`
