# RepoPilot-MAS 最终闭环评测报告

## 总体结果

| 指标 | 结果 |
| --- | ---: |
| 任务解决率 | 0.0% |
| 根因接受率 | 30.0% |
| 根因审查通过率 | 0.0% |
| Patch 根因集合绑定率 | 30.0% |
| Patch 完整验证通过率 | 0.0% |
| Worker 失败隔离率 | 100.0% |
| Recovery 成功率 | 0.0% |
| Workspace 清理率 | 100.0% |
| 平均 Token | 86393.2 |
| 平均时延(ms) | 260936.2 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_bucketsort | failed | 否 | 否 | 否 | 否 | 72159 | 254712 |
| quixbugs_flatten | failed | 是 | 否 | 否 | 否 | 69308 | 216715 |
| quixbugs_get_factors | failed | 否 | 否 | 否 | 否 | 110877 | 330652 |
| quixbugs_hanoi | failed | 否 | 否 | 否 | N/A | 109492 | 291179 |
| quixbugs_knapsack | failed | 是 | 否 | 否 | N/A | 70474 | 214533 |
| quixbugs_lcs_length | failed | 否 | 否 | 否 | 否 | 112864 | 312967 |
| quixbugs_lis | failed | 否 | 否 | 否 | 否 | 110195 | 307538 |
| quixbugs_next_permutation | failed | 否 | 否 | 否 | 否 | 67640 | 240545 |
| quixbugs_quicksort | failed | 是 | 否 | 否 | N/A | 85798 | 244448 |
| quixbugs_sieve | failed | 否 | 否 | 否 | 否 | 55125 | 196073 |

## 失败任务

- `quixbugs_bucketsort`: OUTPUT_TOKEN_BUDGET_EXHAUSTED
- `quixbugs_flatten`: OUTPUT_TOKEN_BUDGET_EXHAUSTED
- `quixbugs_get_factors`: OUTPUT_TOKEN_BUDGET_EXHAUSTED
- `quixbugs_hanoi`: OUTPUT_TOKEN_BUDGET_EXHAUSTED
- `quixbugs_knapsack`: OUTPUT_TOKEN_BUDGET_EXHAUSTED
- `quixbugs_lcs_length`: OUTPUT_TOKEN_BUDGET_EXHAUSTED
- `quixbugs_lis`: OUTPUT_TOKEN_BUDGET_EXHAUSTED
- `quixbugs_next_permutation`: OUTPUT_TOKEN_BUDGET_EXHAUSTED
- `quixbugs_quicksort`: OUTPUT_TOKEN_BUDGET_EXHAUSTED
- `quixbugs_sieve`: OUTPUT_TOKEN_BUDGET_EXHAUSTED

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
