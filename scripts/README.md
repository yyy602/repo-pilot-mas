# 脚本目录

- `run_task.py`：Phase 2 单任务 Single-Agent 入口。
- `run_phase3_acceptance.py`：生成 Phase 3 动态图、降级、LangGraph 自动闭环、重启恢复和人工中断证据。
- `run_phase3_supervisor.py`：经 LangGraph 调用真实 DashScope Supervisor，并在 Worker 派发前保存脱敏中断证据。
- `run_phase4_acceptance.py`：运行双 GPU 本地 Qwen3-8B 四类 Worker 的完整真实验收链。
- `run_phase4_timeout_acceptance.py`：生成单 Worker 超时不击穿 Engine 的确定性证据。
- `run_phase5_acceptance.py`：生成动态门控、Challenge/Rebuttal、双 Patch 真实验证、定向重规划与简单路径的确定性机制证据。
- `run_phase6_evaluation.py`：运行 development 单任务，或运行冻结 test split 的三个主系统与两个消融；完整批次会真实调用 API 和本地模型。
- `run_closed_loop_final_evaluation.py`：运行 Phase 6 修复后的完整闭环；完整 Development 必须提供 `--verification-record`，完整 Frozen Test 必须用 `--freeze-manifest` 绑定已通过门禁的 Development 身份。
- `run_phase6_pre_freeze_checks.py`：在已提交的运行时树上统一执行 pytest、Ruff、compileall、diff 与 clean-tree 检查，生成完整 Development 必需的预冻结记录；使用 `python -m scripts.run_phase6_pre_freeze_checks` 调用。
- `audit_closed_loop_final_evaluation.py`：独立复算修复后 Development 与 Frozen Test 的划分、身份、预算、终态、Trace、Checkpoint、安全及汇总门禁。
- `audit_phase6_evaluation.py`：独立复核 Phase 6 结果矩阵、Trace、Checkpoint、预算、安全门和消融约束，并导出可提交的精简证据。

从仓库根目录运行，具体命令见主 `README.md`。
