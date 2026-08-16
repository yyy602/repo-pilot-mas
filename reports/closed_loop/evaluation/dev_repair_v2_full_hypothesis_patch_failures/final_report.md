# RepoPilot-MAS 最终闭环评测报告

## 总体结果

| 指标 | 结果 |
| --- | ---: |
| 任务解决率 | 60.0% |
| 根因接受率 | 80.0% |
| 根因审查通过率 | 80.0% |
| Patch 根因集合绑定率 | 60.0% |
| Patch 完整验证通过率 | 60.0% |
| Worker 失败隔离率 | 100.0% |
| Recovery 成功率 | 50.0% |
| Workspace 清理率 | 100.0% |
| 平均 Token | 185559.4 |
| 平均时延(ms) | 256515.8 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_find_first_in_sorted | succeeded | 是 | 是 | 是 | 是 | 211141 | 325398 |
| quixbugs_gcd | succeeded | 是 | 是 | 是 | N/A | 163867 | 256855 |
| quixbugs_is_valid_parenthesization | succeeded | 是 | 是 | 是 | N/A | 76995 | 129931 |
| quixbugs_bucketsort | failed | 是 | 是 | 否 | 否 | 169440 | 259070 |
| quixbugs_flatten | failed | 否 | 否 | 否 | N/A | 306354 | 311325 |

## 失败任务

- `quixbugs_bucketsort`: N5 和 N7 两个 PatchAgent/minimal 节点均因 MODEL_FORMAT_ERROR 失败且已达 max_retries_per_node=1，replan.1@v1 已消耗唯一允许的 replan 配额（remaining_replans=0），无法再创建新的恢复节点或重试。根据约束10与20，在补丁阶段恢复资源耗尽且无其他可行路径时，应使用 TERMINATE_TASK 安全终止。
- `quixbugs_flatten`: SUPERVISOR_CALL_BUDGET_EXHAUSTED

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
