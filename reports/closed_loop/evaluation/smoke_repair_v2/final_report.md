# RepoPilot-MAS 最终闭环评测报告

## 总体结果

| 指标 | 结果 |
| --- | ---: |
| 任务解决率 | 0.0% |
| 根因接受率 | 100.0% |
| 根因审查通过率 | 100.0% |
| Patch 根因集合绑定率 | 100.0% |
| Patch 完整验证通过率 | 0.0% |
| Worker 失败隔离率 | N/A |
| Recovery 成功率 | 0.0% |
| Workspace 清理率 | 100.0% |
| 平均 Token | 87998.0 |
| 平均时延(ms) | 309342.0 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_find_first_in_sorted | failed | 是 | 是 | 否 | 否 | 87998 | 309342 |

## 失败任务

- `quixbugs_find_first_in_sorted`: Validation N6 failed with regression_failure. The maximum allowed replans (1) have been fully consumed (remaining: 0). Per constraint 10 and 5, further replanning is blocked by budget limits and the task cannot be safely continued or finalized. Terminating execution.

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
