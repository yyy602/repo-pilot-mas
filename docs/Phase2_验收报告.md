# Phase 2 验收报告

## 1. 验收结论

Phase 2 已于 2026-08-01 完成。模型适配层、Tool Registry、有预算的 ReAct Loop、SingleAgent、TraceWriter、任务加载器和统一 CLI 均已实现；5 个 QuixBugs 任务完成本地 Qwen3-8B 真实端到端运行，并保存成功或失败报告及完整 Trace。

本次五任务初步基线为 **1/5 成功（20%）**。该数字只证明当前 Single-Agent 闭环可运行并提供后续同预算对照，不是项目最终修复率。

## 2. 验收环境

| 项目 | 实际值 |
| --- | --- |
| Conda 环境 | `multi_agent` |
| Python | 3.10.20 |
| pytest | 9.1.1 |
| Ruff | 0.16.1 |
| PyTorch | 2.5.1+cu118 |
| Transformers | 4.51.3 |
| 模型 | 本地 Qwen3-8B |
| GPU | 2 × NVIDIA GeForce RTX 3090，单次模型实例使用一张卡 |

## 3. 验收门结果

| 计划验收项 | 结果 | 证据 |
| --- | --- | --- |
| FakeModelAdapter 单元测试不依赖 GPU | 通过 | 结构化修复、失败上限和异步调用测试 |
| Qwen3-8B 至少一次结构化工具调用 | 通过 | `reports/phase2/model_smoke/result.json` |
| 至少 5 个任务一条命令端到端运行 | 通过 | 5 个独立 TaskSpec 和选定运行报告 |
| 成功和失败任务均产生 FinalReport | 通过 | 1 个成功报告、4 个失败报告 |
| Agent Patch 可应用、测试和回滚 | 通过 | 成功任务的 Diff、目标测试、回归、静态检查和 cleanup 均通过 |
| 至少 5 条含模型、工具、Token 和耗时的 Trace | 通过 | 5 条选定 JSONL Trace，原始模型响应引用存在 |
| 可作为后续统一预算基线 | 通过 | 汇总文件固定模型、预算、任务、调用、Token 和耗时 |

## 4. 五任务真实结果

| 任务 | 状态 | 终止原因 | 模型调用 | 工具调用 | Token | 耗时 |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `gcd` | 失败 | Token/模型预算耗尽 | 10 | 12 | 46,571 | 62.206 s |
| `is_valid_parenthesization` | 成功 | `VALIDATION_PASSED` | 4 | 9 | 8,415 | 21.633 s |
| `powerset` | 失败 | Token/模型预算耗尽 | 11 | 12 | 42,914 | 86.914 s |
| `max_sublist_sum` | 失败 | 未形成已应用补丁 | 4 | 6 | 13,045 | 23.868 s |
| `find_first_in_sorted` | 失败 | 最大步数耗尽 | 10 | 13 | 40,693 | 82.377 s |
| **合计** | **1 成功 / 4 失败** | — | **39** | **52** | **151,638** | **276.998 s** |

工具调用统计包含初始复现、ReAct 内调用、终检和清理；ReAct 的工具预算只约束循环内部调用，因此 FinalReport 的总工具调用数可能高于循环预算。

成功任务将 `is_valid_parenthesization.py` 的最终返回条件改为 `depth == 0`。目标测试、完整回归、静态检查、受保护路径检查和回滚全部通过，原始数据目录保持缺陷版本不变。

四个失败是有效基线结果：系统按预算终止、没有接受空补丁或模型自报成功，并且均完成工作区回滚与删除。它们暴露了后续应通过编排、上下文控制和专门角色改善的问题，而不是 Phase 2 基础设施缺失。

## 5. 自动化验证

在仓库根目录执行：

```bash
/home/user50305/.conda/envs/multi_agent/bin/python -m pytest
/home/user50305/.conda/envs/multi_agent/bin/python -m ruff check .
git diff --check
```

最终结果为 60 项 pytest 全部通过、Ruff 无告警、Diff 空白检查通过。测试覆盖模型结构化响应与 Fake 异步接口、工具注册和安全参数、Single-Agent 成功/失败/受保护路径、CLI 端到端、五任务失败复现，以及 Phase 1 全量回归。

## 6. 证据索引

- 汇总：`reports/phase2/acceptance_summary.json`
- Qwen 结构化调用：`reports/phase2/model_smoke/result.json`
- 五条选定 FinalReport 与 Trace：见汇总文件中的 `selected_runs`
- 数据来源与固定提交：`data/quixbugs/SOURCE.md`
- 实现说明：`docs/Phase2_模型适配层与单智能体基线.md`

`reports/phase2/acceptance/` 中还保留了开发期间的失败尝试，用于说明补丁格式问题如何被发现并修复；正式五任务统计只采用汇总文件明确列出的五个不同任务运行。
