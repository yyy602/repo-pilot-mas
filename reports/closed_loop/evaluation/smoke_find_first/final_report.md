# RepoPilot-MAS 最终闭环评测报告

## 总体结果

| 指标 | 结果 |
| --- | ---: |
| 任务解决率 | 0.0% |
| 根因接受率 | 0.0% |
| 根因审查通过率 | 0.0% |
| Patch 根因集合绑定率 | 0.0% |
| Patch 完整验证通过率 | 0.0% |
| Worker 失败隔离率 | 100.0% |
| Recovery 成功率 | 0.0% |
| Workspace 清理率 | 100.0% |
| 平均 Token | 129075.0 |
| 平均时延(ms) | 359399.0 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_find_first_in_sorted | failed | 否 | 否 | 否 | 否 | 129075 | 359399 |

## 失败任务

- `quixbugs_find_first_in_sorted`: OUTPUT_TOKEN_BUDGET_EXHAUSTED

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
