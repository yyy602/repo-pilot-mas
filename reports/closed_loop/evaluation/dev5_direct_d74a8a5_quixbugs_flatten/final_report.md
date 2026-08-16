# RepoPilot-MAS 最终闭环评测报告

## 总体结果

| 指标 | 结果 |
| --- | ---: |
| 任务解决率 | 0.0% |
| 根因接受率 | 0.0% |
| 根因审查通过率 | 0.0% |
| Patch 根因集合绑定率 | 0.0% |
| Patch 完整验证通过率 | 0.0% |
| Worker 失败隔离率 | N/A |
| Recovery 成功率 | N/A |
| Workspace 清理率 | 100.0% |
| 平均 Token | 86765.0 |
| 平均时延(ms) | 96126.0 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_flatten | failed | 否 | 否 | 否 | N/A | 86765 | 96126 |

## 失败任务

- `quixbugs_flatten`: NO_PROGRESS_LOOP

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
