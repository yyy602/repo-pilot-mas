# RepoPilot-MAS 最终闭环评测报告

## 总体结果

| 指标 | 结果 |
| --- | ---: |
| 任务解决率 | 60.0% |
| 根因接受率 | 80.0% |
| 根因审查通过率 | 80.0% |
| Patch 根因集合绑定率 | 80.0% |
| Patch 完整验证通过率 | 60.0% |
| Worker 失败隔离率 | 100.0% |
| Recovery 成功率 | 33.3% |
| Workspace 清理率 | 100.0% |
| 平均 Token | 184025.2 |
| 平均时延(ms) | 460247.8 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_find_first_in_sorted | succeeded | 是 | 是 | 是 | 是 | 123734 | 269319 |
| quixbugs_gcd | succeeded | 是 | 是 | 是 | N/A | 119438 | 241883 |
| quixbugs_is_valid_parenthesization | succeeded | 是 | 是 | 是 | N/A | 117924 | 285449 |
| quixbugs_bucketsort | failed | 否 | 否 | 否 | 否 | 350493 | 927814 |
| quixbugs_flatten | failed | 是 | 是 | 否 | 否 | 208537 | 576774 |

## 失败任务

- `quixbugs_bucketsort`: BUDGET_VIOLATION
- `quixbugs_flatten`: BUDGET_VIOLATION

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
