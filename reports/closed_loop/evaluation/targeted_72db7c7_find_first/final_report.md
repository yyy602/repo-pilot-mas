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
| 平均 Token | 488522.0 |
| 平均时延(ms) | 713576.0 |

## 逐任务结果

| 任务 | 状态 | 根因接受 | 审查通过 | Patch通过 | Recovery | Token | 时延(ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quixbugs_find_first_in_sorted | failed | 是 | 是 | 否 | 否 | 488522 | 713576 |

## 失败任务

- `quixbugs_find_first_in_sorted`: All patch generation attempts (N9, N11, N12, N13) have failed with either MODEL_FORMAT_ERROR or PATCH_TARGET_TEST_FAILED, and each has exhausted its per-node retry budget. The single allowed replan (replan.1@v1) has already been consumed to recover from N9's format failure, but subsequent recovery nodes N12/N13 also failed the target test. No remaining replans or retries are available, and no valid PatchCandidate exists to proceed to validation. Per constraint 10 and budget limits, further patch attempts would violate MVP rules. TERMINATE_TASK is the only safe action.

> 失败、超时、预算违规和 Runner 异常均保留在分母中。
