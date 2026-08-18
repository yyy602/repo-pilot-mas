# RepoPilot-MAS 最终闭环评测报告

## 总体结果

| 指标 | 结果 |
| --- | ---: |
| 任务解决率 | 20.0% |
| 根因接受率 | 60.0% |
| 根因审查通过率 | 60.0% |
| Patch 根因集合绑定率 | 20.0% |
| Patch 完整验证通过率 | 20.0% |
| Worker 失败隔离率 | 100.0% |
| Recovery 成功率 | 25.0% |
| Workspace 清理率 | 100.0% |
| 平均 Token | 252447.6 |
| 平均时延(ms) | 374160.4 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_find_first_in_sorted | failed | 是 | 是 | 否 | 否 | 159244 | 351438 |
| quixbugs_gcd | failed | 否 | 否 | 否 | N/A | 500199 | 377981 |
| quixbugs_is_valid_parenthesization | succeeded | 是 | 是 | 是 | 是 | 158248 | 269589 |
| quixbugs_bucketsort | failed | 是 | 是 | 否 | 否 | 323262 | 723473 |
| quixbugs_flatten | failed | 否 | 否 | 否 | 否 | 121285 | 148321 |

## 失败任务

- `quixbugs_find_first_in_sorted`: 根因已被接受且证据充分，但 PatchAgent 在 N5 和 N7 上均两次因 MODEL_FORMAT_ERROR 未能生成可应用的结构化文本替换，共四次失败。MVP 重规划已用尽（remaining_replans=0，replans=1），且 recovery.pending_replan=false，无法再次 REQUEST_REPLAN。继续创建 PATCH_TASK 只会重复同类失败，无法安全继续，因此终止任务。
- `quixbugs_gcd`: BUDGET_VIOLATION
- `quixbugs_bucketsort`: 根因 N6.hypothesis@v1 已被接受，但补丁阶段连续失败：N8 两次 MODEL_FORMAT_ERROR 后触发 replan.1@v1，恢复节点 N10 又因 MODEL_FORMAT_ERROR 和 PATCH_TARGET_TEST_FAILED 两次失败，重试预算耗尽。remaining_replans=0 且 pending_replan=false，上一决策 create_patch_recovery_n11_1 被拒绝（LOGICAL_TASK_RETRY_EXHAUSTED），无法再创建逻辑等价的 PATCH_TASK 恢复节点，也无法再次 REQUEST_REPLAN。在预算约束下无法安全继续生成可应用补丁，因此终止任务。
- `quixbugs_flatten`: N4.review@v1 的 verdict 为 needs_more_evidence，要求补充验证性证据；但两次证据补全任务 N5 与 N7 均因 BUSINESS_EVIDENCE_INSUFFICIENT 失败，且重试预算已耗尽。replan.1@v1 已批准一次回 investigation 的恢复，恢复节点 N7 仍失败，remaining_replans=0 且 recovery.pending_replan=false，已无可用重规划预算。当前证据不足以接受根因，无法安全继续进入 diagnosis 或 patch 阶段，按硬约束第5条终止任务。

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
