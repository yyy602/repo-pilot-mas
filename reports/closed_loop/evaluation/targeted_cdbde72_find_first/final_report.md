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
| 平均 Token | 235141.0 |
| 平均时延(ms) | 334832.0 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_find_first_in_sorted | failed | 是 | 是 | 否 | 否 | 235141 | 334832 |

## 失败任务

- `quixbugs_find_first_in_sorted`: SUPERVISOR_CALL_BUDGET_EXHAUSTED

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
