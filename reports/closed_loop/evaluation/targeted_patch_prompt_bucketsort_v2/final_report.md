# RepoPilot-MAS 最终闭环评测报告

## 总体结果

| 指标 | 结果 |
| --- | ---: |
| 任务解决率 | 0.0% |
| 根因接受率 | 100.0% |
| 根因审查通过率 | 100.0% |
| Patch 根因集合绑定率 | 0.0% |
| Patch 完整验证通过率 | 0.0% |
| Worker 失败隔离率 | 100.0% |
| Recovery 成功率 | 0.0% |
| Workspace 清理率 | 100.0% |
| 平均 Token | 227463.0 |
| 平均时延(ms) | 627098.0 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_bucketsort | failed | 是 | 是 | 否 | 否 | 227463 | 627098 |

## 失败任务

- `quixbugs_bucketsort`: N8 与 N10 两个 PatchAgent 恢复节点均已耗尽重试预算并 TIMED_OUT，remaining_replans=0 且 recovery.pending_replan=false，已无可用重规划预算。根因假设 N6.hypothesis@v1 已被接受且审查通过，但补丁生成阶段连续超时，无法在预算内产出 PatchCandidate，无法安全继续推进到验证与最终化阶段。

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
