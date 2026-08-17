# RepoPilot-MAS 最终闭环评测报告

## 总体结果

| 指标 | 结果 |
| --- | ---: |
| 任务解决率 | 60.0% |
| 根因接受率 | 100.0% |
| 根因审查通过率 | 100.0% |
| Patch 根因集合绑定率 | 60.0% |
| Patch 完整验证通过率 | 60.0% |
| Worker 失败隔离率 | 100.0% |
| Recovery 成功率 | 0.0% |
| Workspace 清理率 | 100.0% |
| 平均 Token | 204660.8 |
| 平均时延(ms) | 260718.4 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_find_first_in_sorted | failed | 是 | 是 | 否 | 否 | 190232 | 313116 |
| quixbugs_gcd | succeeded | 是 | 是 | 是 | N/A | 87241 | 118969 |
| quixbugs_is_valid_parenthesization | succeeded | 是 | 是 | 是 | N/A | 78639 | 131856 |
| quixbugs_bucketsort | failed | 是 | 是 | 否 | 否 | 483633 | 485185 |
| quixbugs_flatten | succeeded | 是 | 是 | 是 | N/A | 183559 | 254466 |

## 失败任务

- `quixbugs_find_first_in_sorted`: 根因已被接受且 Review 为 supported，但补丁阶段已耗尽重试与重规划预算：N5 两次 MODEL_FORMAT_ERROR 失败，N7 一次 PATCH_PRIMARY_HYPOTHESIS_MISMATCH 后重试又因 TOKEN_OR_MODEL_BUDGET_EXHAUSTED 失败，且上次恢复决策被拒绝（LOGICAL_TASK_RETRY_EXHAUSTED），remaining_replans=0，无法再创建新的 PATCH_TASK。继续执行无法安全推进，按约束终止任务。
- `quixbugs_bucketsort`: Patch 阶段已耗尽重试与重规划预算：N5 因 TOKEN_OR_MODEL_BUDGET_EXHAUSTED 且 old_text 未唯一匹配而 FAILED，N7 恢复节点两次 MODEL_TIMEOUT 后 TIMED_OUT；recovery.remaining_replans=0 且 pending_replan=false，上一决定 create_patch_recovery_005 已被拒绝，无法再创建逻辑等价的 PATCH_TASK 或再次 REQUEST_REPLAN。在预算约束下无法安全继续生成并验证补丁，因此终止任务。

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
