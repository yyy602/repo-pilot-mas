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
| 平均 Token | 217057.0 |
| 平均时延(ms) | 343524.0 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_find_first_in_sorted | failed | 否 | 否 | 否 | 否 | 217057 | 343524 |

## 失败任务

- `quixbugs_find_first_in_sorted`: N5.review@v1 裁决为 needs_more_evidence，补证节点 N6 与 N8 均已耗尽重试预算并 TIMED_OUT，replan.1@v1 已批准但恢复节点仍失败，remaining_replans=0 且 pending_replan=false，无法再创建新的补证节点或再次请求重规划。证据缺口无法消除，根因状态机停留在 needs_evidence，无法安全推进到 diagnosis/patch，故终止任务。

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
