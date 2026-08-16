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
| 平均 Token | 250093.0 |
| 平均时延(ms) | 769694.0 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_find_first_in_sorted | failed | 是 | 是 | 否 | 否 | 250093 | 769694 |

## 失败任务

- `quixbugs_find_first_in_sorted`: N8 和 N10 两个 PATCH_TASK（minimal）均因 PATCH_TARGET_TEST_FAILED 失败且已达 max_retries_per_node=1；replan.1@v1 已消耗唯一允许的 replan 配额（remaining_replans=0），无法再次 REQUEST_REPLAN。当前无剩余预算或合法路径生成新补丁，按约束10与约束20在恢复节点失败后应终止任务。

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
