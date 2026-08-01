# 配置目录

- `model.yaml`：Phase 2 本地模型路径、设备、数据类型和生成限制；
- `runtime.yaml`：Single-Agent 的步数、模型调用、工具调用、Token 和总耗时预算。

模型路径只允许通过配置注入，Agent 实现中不得硬编码。
