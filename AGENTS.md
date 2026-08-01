# 仓库 Agent 开发指南

本文件适用于整个仓库。新增或修改文档时默认使用中文；命令、代码标识和 Schema 字段保留必要的英文。

## 权威来源

- `docs/RepoPilot-MAS_完整计划.md` 是架构、范围、Phase 状态和验收标准的唯一依据。
- `README.md` 和各 Phase 说明均为从属文档；发生冲突时以唯一计划基线为准。
- 修改代码前必须读取当前 Phase 及其验收门。所有检查和输出物未齐备时，不得标记 Phase 完成。
- 开发进度只使用 `Phase 0`–`Phase 6`；Investigation、Diagnosis、Patch 和 Validation 是工作流阶段，不是开发 Phase。

## 工作规则

- 为当前 Phase 实现最小但完整的改动，不得用空骨架、冒烟测试或仅覆盖正常路径的版本替代完整行为。
- 不提前创建未来 Phase 的空模块，也不增加未经需求支持的抽象层和模型 Provider。
- 保留用户已有改动，避免无关重构、格式化噪声和依赖变更。
- 遵守架构边界：Supervisor 负责语义决策，Engine 执行确定性状态与安全约束，Worker 返回结构化 Artifact，工具提供真实执行证据。
- 只有代码、测试、Trace 和当前 Phase 验收门共同支持时，才能更新 Phase 状态或简历表述。

## 安全与证据

- 不得直接修改原始任务仓库，只能使用受管候选工作区。
- 任务自带测试和所有 `protected_paths` 均不可修改。
- 测试命令来自受信 `TaskSpec`；模型不得自行指定可执行文件，也不能用自报结果替代真实验证。
- 子进程必须限制时间和输出，失败保持结构化，并为工具及模型调用保留 Trace ID。
- API Key 只能从 Git 忽略的本机环境文件读取；配置、日志、Trace、检查点和报告不得包含真实 Key。
- 不得虚构评测结果、性能提升、支持的数据集或简历指标；所有数字必须能追溯到保存的评测产物。

## 验证要求

- 使用 Python 3.10 或更高版本。当前服务器优先使用 `/home/user50305/.conda/envs/multi_agent/bin/python`，不得静默回退到默认 Python 3.9 环境。
- 修改代码后运行：

```bash
/home/user50305/.conda/envs/multi_agent/bin/python -m pytest
/home/user50305/.conda/envs/multi_agent/bin/python -m ruff check .
```

- 补充当前 Phase 要求的正常、失败和边界测试；冒烟测试通过不能替代完整验收测试。
- 仅修改文档时，至少运行 `git diff --check`，并检查链接、标题和代码块。
- 如实报告失败或跳过的验证；必要检查未通过时不得推进 Phase。
