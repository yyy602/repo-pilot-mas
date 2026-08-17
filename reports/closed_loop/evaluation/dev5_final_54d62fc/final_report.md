# RepoPilot-MAS 最终闭环评测报告

## 总体结果

| 指标 | 结果 |
| --- | ---: |
| 任务解决率 | 0.0% |
| 根因接受率 | 20.0% |
| 根因审查通过率 | 20.0% |
| Patch 根因集合绑定率 | 20.0% |
| Patch 完整验证通过率 | 0.0% |
| Worker 失败隔离率 | 80.0% |
| Recovery 成功率 | 0.0% |
| Workspace 清理率 | 100.0% |
| 平均 Token | 179401.6 |
| 平均时延(ms) | 252817.4 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_find_first_in_sorted | failed | 是 | 是 | 否 | 否 | 238595 | 357648 |
| quixbugs_gcd | failed | 否 | 否 | 否 | 否 | 169083 | 200552 |
| quixbugs_is_valid_parenthesization | failed | 否 | 否 | 否 | 否 | 160002 | 246149 |
| quixbugs_bucketsort | failed | 否 | 否 | 否 | 否 | 201783 | 231094 |
| quixbugs_flatten | failed | 否 | 否 | 否 | 否 | 127545 | 228644 |

## 失败任务

- `quixbugs_find_first_in_sorted`: Validation N12.validation@v1 failed with failure_class=regression_failure, invalidating patch N10.patch@v1. Per rule 10, a single patch failure with regression failure class routes back to patch stage, but remaining_replans=0 and the MVP has already been replanned once (replan.1@v1). The budget shows max_replans=1 already consumed, and recovery.pending_replan=false. Creating another patch would require a new replan which is prohibited. The validation failure is a regression failure, not a root-cause or evidence issue, so returning to investigation/diagnosis is not appropriate. With no replans remaining and the only patch candidate invalidated by regression, the workflow cannot safely continue within constraints.
- `quixbugs_gcd`: The investigation stage has exhausted all recovery options. The replan budget is fully consumed (remaining_replans=0) and the pending replan recovery node N7 has already been created and failed with BUSINESS_EVIDENCE_INSUFFICIENT. Both evidence completion attempts (N5 and N7) failed to produce verified evidence addressing the remaining uncertainty about b==0 test coverage identified in N4.review@v1. The retry budget for evidence completion is exhausted, and no further REQUEST_REPLAN is permitted. The existing evidence N1.evidence@v1 already confirms the reproduction failure, and the review N4.review@v1 indicates needs_more_evidence, but the system cannot produce the required additional evidence. Continuing would violate the budget constraints and the no-progress limit. The only safe action is to terminate the task as the workflow cannot proceed to diagnosis or patch without the required evidence closure.
- `quixbugs_is_valid_parenthesization`: The investigation has reached a dead end. Review N8 returned needs_more_evidence with remaining uncertainties, and the evidence_completion task N9 failed with BUSINESS_EVIDENCE_INSUFFICIENT, exhausting its retry budget. A replan was approved (replan.1@v1) but the recovery node N11 is BLOCKED because its dependency N9 failed, and remaining_replans=0 prevents any further replan. The only allowed next actions are REQUEST_REPLAN or TERMINATE_TASK, but REQUEST_REPLAN is prohibited by the exhausted replan budget. No further investigation, diagnosis, or patch work can proceed without the missing evidence, and the retry budget for evidence completion is exhausted. Continuing would only repeat logically equivalent tasks, which is disallowed. Therefore, the task cannot be safely continued and must be terminated.
- `quixbugs_bucketsort`: N4.review@v1 的 verdict 为 needs_more_evidence，明确指出 remaining_uncertainty 需要确认 counts 是否正确计算。N5 的两次证据补全尝试均因 TOKEN_OR_MODEL_BUDGET_EXHAUSTED 失败，N7 的补证尝试也因 BUSINESS_EVIDENCE_INSUFFICIENT 失败。recovery.pending_replan=false 且 remaining_replans=0，已无重规划预算。根据规则 10，MVP 最多重规划一次，不得用重复决策绕过预算。当前无法安全继续，必须终止任务。
- `quixbugs_flatten`: The investigation stage has exhausted all recovery options. The replan budget is fully consumed (remaining_replans=0, replans=1), and the pending replan recovery node N8 is BLOCKED because its dependency N6 failed with BUSINESS_EVIDENCE_INSUFFICIENT and cannot be retried (retry budget exhausted). The review N5.review@v1 returned verdict 'needs_more_evidence' with remaining uncertainty about whether all non-list leaf node paths use yield, but the evidence_completion task cannot be recreated (LOGICAL_TASK_RETRY_EXHAUSTED) and no further replan is permitted. The hypothesis resolution status is 'needs_evidence', preventing progression to diagnosis acceptance or patch creation. With no valid path to complete the required evidence within budget constraints, the workflow cannot safely continue and must terminate.

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
