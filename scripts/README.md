# 脚本目录

- `run_task.py`：Phase 2 单任务 Single-Agent 入口。
- `run_phase3_acceptance.py`：生成 Phase 3 动态图、降级、LangGraph 自动闭环、重启恢复和人工中断证据。
- `run_phase3_supervisor.py`：经 LangGraph 调用真实 DashScope Supervisor，并在 Worker 派发前保存脱敏中断证据。
- `run_phase4_acceptance.py`：运行双 GPU 本地 Qwen3-8B 四类 Worker 的完整真实验收链。
- `run_phase4_timeout_acceptance.py`：生成单 Worker 超时不击穿 Engine 的确定性证据。
- `run_phase5_acceptance.py`：生成动态门控、Challenge/Rebuttal、双 Patch 真实验证、定向重规划与简单路径的确定性机制证据。

从仓库根目录运行，具体命令见主 `README.md`。
