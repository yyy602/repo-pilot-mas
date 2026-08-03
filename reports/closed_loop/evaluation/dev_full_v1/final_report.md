# RepoPilot-MAS 最终闭环评测报告

## 总体结果

| 指标 | 结果 |
| --- | ---: |
| 任务解决率 | 20.0% |
| 根因接受率 | 80.0% |
| 根因审查通过率 | 0.0% |
| Patch 根因集合绑定率 | 60.0% |
| Patch 完整验证通过率 | 20.0% |
| Worker 失败隔离率 | N/A |
| Recovery 成功率 | 0.0% |
| Workspace 清理率 | 100.0% |
| 平均 Token | 71283.6 |
| 平均时延(ms) | 206026.4 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_find_first_in_sorted | failed | 是 | 否 | 否 | 否 | 86939 | 238442 |
| quixbugs_gcd | succeeded | 是 | 否 | 是 | N/A | 74335 | 207416 |
| quixbugs_is_valid_parenthesization | failed | 否 | 否 | 否 | N/A | 33507 | 111923 |
| quixbugs_max_sublist_sum | failed | 是 | 否 | 否 | N/A | 96652 | 253919 |
| quixbugs_powerset | failed | 是 | 否 | 否 | N/A | 64985 | 218432 |

## 失败任务

- `quixbugs_find_first_in_sorted`: Validation failed with regression_failure. The remaining replan budget is exhausted (1/1). Per constraint 10 and 5, unable to safely continue or retry the patch stage.
- `quixbugs_is_valid_parenthesization`: STRUCTURED_OUTPUT_ERROR
- `quixbugs_max_sublist_sum`: OUTPUT_TOKEN_BUDGET_EXHAUSTED
- `quixbugs_powerset`: OUTPUT_TOKEN_BUDGET_EXHAUSTED

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
