# RepoPilot-MAS 最终闭环评测报告

## 总体结果

| 指标 | 结果 |
| --- | ---: |
| 任务解决率 | 20.0% |
| 根因接受率 | 30.0% |
| 根因审查通过率 | 30.0% |
| Patch 根因集合绑定率 | 20.0% |
| Patch 完整验证通过率 | 20.0% |
| Worker 失败隔离率 | 100.0% |
| Recovery 成功率 | 25.0% |
| Workspace 清理率 | 100.0% |
| 平均 Token | 155918.1 |
| 平均时延(ms) | 281881.0 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_get_factors | failed | 否 | 否 | 否 | N/A | 85538 | 128999 |
| quixbugs_hanoi | failed | 否 | 否 | 否 | 否 | 194056 | 340352 |
| quixbugs_knapsack | succeeded | 是 | 是 | 是 | N/A | 88706 | 122529 |
| quixbugs_lcs_length | failed | 否 | 否 | 否 | N/A | 376885 | 311450 |
| quixbugs_lis | failed | 否 | 否 | 否 | N/A | 61028 | 109862 |
| quixbugs_max_sublist_sum | failed | 是 | 是 | 否 | 否 | 418935 | 1086804 |
| quixbugs_next_permutation | failed | 否 | 否 | 否 | 否 | 151601 | 321430 |
| quixbugs_powerset | succeeded | 是 | 是 | 是 | 是 | 111638 | 273288 |
| quixbugs_quicksort | failed | 否 | 否 | 否 | N/A | 70794 | 124051 |
| quixbugs_sieve | failed | 否 | 否 | 否 | N/A | 0 | 45 |

## 失败任务

- `quixbugs_get_factors`: NO_PROGRESS_LOOP
- `quixbugs_hanoi`: N4 与 N6 两个补证任务均因 MODEL_TIMEOUT 耗尽重试预算，Hypothesis N3.hypothesis@v1 仍带有 missing_evidence，无法进入根因审查；remaining_replans 已为 0，且上一同类恢复决定被拒绝，无法在不重置预算的前提下继续安全推进。
- `quixbugs_lcs_length`: BUDGET_VIOLATION
- `quixbugs_lis`: NO_PROGRESS_LOOP
- `quixbugs_max_sublist_sum`: NO_PROGRESS_LOOP
- `quixbugs_next_permutation`: N3.hypothesis@v1 的 missing_evidence 仍未得到回应，N4 与 N6 补证任务均已耗尽重试预算，且剩余重规划次数为 0，无法安全进入根因审查或补丁阶段。
- `quixbugs_quicksort`: SUPERVISOR_ROUTES_EXHAUSTED
- `quixbugs_sieve`: SUPERVISOR_ROUTES_EXHAUSTED

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
