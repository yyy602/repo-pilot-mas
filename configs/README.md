# 配置目录

- `model.yaml`：Phase 2 单模型配置，以及 Phase 4 双 GPU Worker 模型槽位、数据类型和生成限制；
- `supervisor.yaml`：正式阿里云 Supervisor，固定使用 Pro → Flash → Max、模型内账号其次的六槽位路由；
- `supervisor.local.yaml`：仅供单任务闭环调试的本地 Qwen3-8B Supervisor，复用 Worker 槽位 0，不得用于完整 Development 或 Frozen Test；
- `runtime.yaml`：Single-Agent 与 OrchestrationEngine 预算，以及 LangGraph SQLite 路径和递归上限。
- `phase6.yaml`：冻结评测协议、三系统/消融清单、公平预算，以及仅供历史 v1 结果复核的价格快照。

模型路径和 API 地址只允许通过配置注入，Agent 实现中不得硬编码。真实 API Key 仅保存在仓库根目录 `.env.supervisor`，配置中只允许出现 `DASHSCOPE_API_KEY_1`、`DASHSCOPE_API_KEY_2` 两个环境变量名。
