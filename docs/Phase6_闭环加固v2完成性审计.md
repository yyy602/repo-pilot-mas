# Phase 6 闭环加固 v2 完成性审计

## 1. 审计结论

审计分支为 `agent/hypothesis-patch-closed-loop`，依据 `docs/RepoPilot-MAS闭环评测异常修复计划_修订版.md` 逐项检查。

当前结论不是“Phase 6 v2 已完成”，而是：

- Phase A–G 的代码实现与单元/集成测试已经完成；完整离线协议清单已预检，但正式预冻结还要求运行时代码先提交；
- 多轮真实 API development pilot 已证明 Supervisor、Engine、Worker、Validation 和 Trace 确实参与运行，但这些运行早于最终代码指纹或只覆盖单任务，不能替代完整 Development；
- 完整 5 任务 Development 首轮在前两题 0/2 后中止，冻结身份、10 任务 Frozen Test 和独立最终审计尚未执行；
- 旧的两个账号 × 三个 qwen3.7 路由均已额度耗尽；当前三模型六槽位路由已提交且 5/6 槽位可用，API 可用性不再是阻塞项；当前待提交的是真实 Development 暴露的 Review 选择 Gate 修复。

2026-08-12 15:38（Asia/Shanghai）的最小真实 API 复查保存在 `reports/closed_loop/evaluation/api_route_recheck_20260812_1538/trace.jsonl`：六个槽位按既定顺序各尝试一次，均返回 `AllocationQuota.FreeTierOnly`，终态为 `SUPERVISOR_ROUTES_EXHAUSTED`。因此没有启动已知必然无效的正式 5 任务 Development。

2026-08-16 的三模型逐槽位真实探测中 5/6 可用：仅 `deepseek-v4-flash-0731 + DASHSCOPE_API_KEY_1` 免费额度耗尽；同模型账号 2 与 `qwen3.8-max`、`deepseek-v4-pro-0813` 的两个账号槽位均返回合法 JSON。脱敏证据见 `reports/closed_loop/evaluation/supervisor_route_probe_20260816/summary.json` 和同目录 `trace.jsonl`。

## 2. Phase A–G 要求与证据

| Phase | 核心要求 | 当前证据 | 判定 |
| --- | --- | --- | --- |
| A | 唯一预算事实源，区分策略调用、API 路由、Worker 和总预算 | `configs/closed_loop_evaluation.yaml`、`evaluation_budget.py`、`test_evaluation_budget.py`、Result 的 configured/effective/actual 字段 | 实现通过 |
| B | 目标测试失败是成功复现；工具异常是 Worker 失败 | `investigator.py`、`test_reproduction_schema_and_recovery.py`、`bug_reproduction_confirmed` Trace | 实现通过 |
| C | 调度前输入契约与分类 Retry | `agent_contracts.py`、`worker_recovery.py`、`test_agent_contracts.py`、`test_contract_dispatch_gate.py` | 实现通过 |
| D | 阶段化最小 Schema、格式恢复、安全 Fallback、六槽位路由 | `supervisor.py`、`supervisor_decision.py`、`dashscope.py`、`test_supervisor_agent.py`、`test_dashscope_router.py`；非法结构在同一决策内轮换路由且不污染永久额度状态 | 实现通过 |
| E | 类型化 Evidence、独立 Reviewer、Evidence/HypothesisResolution Gate | `worker_artifact.py`、`reviewer.py`、`test_phase_c_review_gate.py`、`test_phase4_workers.py` | 实现通过 |
| F | Patch 语义预检、完整 accepted set 绑定、Validation 定向回退 | `patch_agent.py`、`validation.py`、`test_patch_multi_hypothesis_binding.py`、`test_phase5_policy.py` | 实现通过 |
| G | Artifact Ref、四层指标、完整 Trace 事件 | `final_evaluation.py`、`evaluation.py`、`test_final_evaluation_metrics.py`、`test_phase6_evaluation.py` | 实现通过 |

当前运行时通过 241 项 pytest、Ruff、compileall 和 `git diff --check`。`reports/closed_loop/verification/pre_freeze_checks.json` 曾在路由提交 `48d0bcc` 上通过全部 7 个 Gate，但随后真实 Development 发现并修复了 Review 选择 Gate 错误，因此该记录已被新运行时改动作废。新修复提交后必须重新生成预冻结记录。

## 3. 真实 API 针对性验证边界

| 任务/运行 | 已证明内容 | 不能证明的内容 |
| --- | --- | --- |
| `dev_repair_v2_gcd` | 标准 Evidence→Hypothesis→Review→Patch→Validation 闭环成功 | 不是最终指纹上的完整 Development |
| `dev_repair_v2_find_first_*` | 真实失败复现、Evidence 充分性门和多轮 Schema 修复确实触发 | 后续运行被新问题或额度耗尽中止，没有最终成功 Result |
| `dev_repair_v2_flatten` | 错误 Patch 被 Validation 否决并触发 Replan | 该历史运行仍受旧静态检查语义影响，修复后尚未真实重跑 |
| `dev_repair_v2_bucketsort` | Worker 失败隔离、Replan、替代 Patch 和最终 Validation 成功 | 单题成功不能代替 4/5 Development 门 |
| `dev_repair_v2_parenthesization` | Supervisor 格式恢复后保留状态并完成闭环 | 不是冻结 Test 结果 |
| `dev_repair_v2_routes_classified` | 六槽位真实尝试后立即 fail-closed，终态为 `provider_quota_exhausted` | Protocol Acceptance 通过不代表业务成功；实际为 0/1 solved |

上述运行全部属于 development/诊断证据，不与 Frozen Test 指标合并。

## 4. 正式评测门状态

| 顺序 | 门禁 | 状态 | 证据或缺口 |
| ---: | --- | --- | --- |
| 1 | 预冻结代码检查 | 代码检查通过，clean gate 待新提交 | 241 tests、Ruff、compileall、diff 均通过；Review Gate 修复尚未提交 |
| 2 | 5 任务 Development 清单预检 | 通过 | `preflight_closed_loop_dev_v2`，仅 dry-run |
| 3 | 10 任务 Frozen Test 清单预检 | 通过 | `preflight_closed_loop_test_v2`，仅 dry-run，未执行测试任务 |
| 4 | 完整 5 任务 Development ≥4/5 | 修复后待重跑 | 首轮前两题 0/2 后中止；针对性 GCD 修复后 1/1 solved |
| 5 | 冻结 commit/config/Prompt/route 身份 | 待执行 | 必须绑定已通过的完整 Development |
| 6 | 一次性 10 任务 Frozen Test | 待执行 | 不得在 Development 通过前运行 |
| 7 | 独立最终审计 | 待执行 | 需要完整 Development 与 Frozen Test 两套产物 |

审计器已修复为从与 Runner 相同的 `configs/closed_loop_evaluation.yaml` 解析 Suite，并独立重算 Development 4/5、框架终态、路由成功、Snapshot 压缩、Frozen Test 非统一框架失败、预算、Usage、Validation、闭环机制、源码摘要、工作区和证据路径等门禁；单任务 Result 与批量条目也必须逐字段一致，Development/Test commit 还必须相同且与当前 HEAD 一致。缺失冻结 Gate、dirty runtime、预冻结记录哈希不匹配、缺少任一必需检查、dry-run、身份路径解绑、Development 文件哈希变化或篡改原始证据派生指标均会失败关闭。完整 5+10 临时证据链的端到端审计测试已经通过。

## 5. 恢复后的唯一执行顺序

先提交当前运行时代码，再用统一入口生成不可手工冒充的预冻结记录：

```bash
/home/user50305/.conda/envs/multi_agent/bin/python \
  -m scripts.run_phase6_pre_freeze_checks
```

只有记录 `passed=true`，并且恢复任意可用 DashScope Supervisor 路由后，才能执行：

```bash
/home/user50305/.conda/envs/multi_agent/bin/python \
  scripts/run_closed_loop_final_evaluation.py \
  --split development \
  --run-id dev_repair_v2_full \
  --verification-record reports/closed_loop/verification/pre_freeze_checks.json
```

只有 `dev_repair_v2_full/acceptance.json` 的完整 Development 门全部通过，才能执行：

```bash
/home/user50305/.conda/envs/multi_agent/bin/python \
  scripts/run_closed_loop_final_evaluation.py \
  --split test \
  --run-id closed_loop_repair_test_v2 \
  --freeze-manifest reports/closed_loop/evaluation/dev_repair_v2_full/manifest.json
```

最后独立重算：

```bash
/home/user50305/.conda/envs/multi_agent/bin/python \
  scripts/audit_closed_loop_final_evaluation.py \
  --development-root reports/closed_loop/evaluation/dev_repair_v2_full \
  --test-root reports/closed_loop/evaluation/closed_loop_repair_test_v2 \
  --output reports/closed_loop/audit/final_audit_v2.json
```

任一步未通过都必须保留失败结果，不得修改 Frozen Test Prompt、规则或任务答案后重跑并覆盖原结果。
