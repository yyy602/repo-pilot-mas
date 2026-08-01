# 配置目录

- `model.yaml`：Phase 2 本地模型路径、设备、数据类型和生成限制；
- `supervisor.yaml`：Phase 3 阿里云 Supervisor、双账号三模型顺序路由和故障转移规则；
- `runtime.yaml`：Single-Agent 的步数、模型调用、工具调用、Token 和总耗时预算。

模型路径和 API 地址只允许通过配置注入，Agent 实现中不得硬编码。真实 API Key 仅保存在仓库根目录 `.env.supervisor`，配置中只允许出现 `DASHSCOPE_API_KEY_1`、`DASHSCOPE_API_KEY_2` 两个环境变量名。
