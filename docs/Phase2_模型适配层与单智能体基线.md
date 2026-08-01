# Phase 2：模型适配层与 Single-Agent 基线

## 1. 阶段目标

Phase 2 在 Phase 1 的确定性工具层上形成首个完整代码修复闭环：读取 `TaskSpec`，由模型按结构化协议选择工具，在隔离工作区调查和应用补丁，再由系统执行目标测试、完整回归、静态检查、Diff 校验和回滚，最终保存 `FinalReport` 与 JSONL Trace。

本阶段只实现实际使用的本地 Transformers Provider 和 Fake Provider，不提前引入远程模型、Supervisor 或 TaskGraph 抽象。

## 2. 运行链路

```text
TaskSpec JSON
    │
    ▼
任务加载器 ──→ SingleAgent ──→ 有预算的 ReAct Loop
                    │                    │
                    │                    └──→ 结构化 Tool Registry
                    │                              │
                    ▼                              ▼
              TraceWriter              Phase 1 九个确定性工具
                    │                              │
                    └────────→ FinalReport ←──────┘
                                      │
                                      ▼
                           回滚并删除候选工作区
```

关键边界如下：

- Agent 只能输出 `tool` 或 `final` 两类结构化动作；
- 工具名必须已注册，参数必须通过 Agent 可见 JSON Schema；
- `repository_path`、测试命令、超时和 `protected_paths` 在 Registry 构建时绑定，模型不能传入或覆盖；
- 补丁只能写入候选工作区，并经 `git apply --check`、受保护路径检查和最终真实测试验证；
- 模型声称成功不会直接决定任务成功，只有确定性终检全部通过才返回 `VALIDATION_PASSED`；
- 达到步数、调用、Token 或耗时预算时输出结构化失败，并继续执行 Diff 收集和清理。

## 3. 主要实现

| 模块 | 职责 |
| --- | --- |
| `models/base.py` | 同步/异步统一接口、消息与生成配置、结构化输出、格式修复、Token/耗时和原始响应引用 |
| `models/local_transformers.py` | 延迟加载本地 Qwen3-8B，使用本地权重完成受限生成 |
| `models/fake.py` | 无 GPU 的确定性单元测试模型 |
| `tools/registry.py` | 九工具注册、参数 Schema 校验及任务安全参数绑定 |
| `orchestration/react_loop.py` | 最大步数、模型/工具/Token/耗时预算和重复失败动作拦截 |
| `agents/single_agent.py` | 失败复现、ReAct、终检、FinalReport 和工作区清理 |
| `runtime/trace.py` | 追加写入 JSONL 模型、工具、终止和报告事件 |
| `tasks/loader.py` | 从 JSON 清单解析并校验 TaskSpec 和相对仓库路径 |
| `scripts/run_task.py` | 单任务统一运行入口 |

## 4. 数据与配置

`data/quixbugs/` 保存 5 个相互隔离的 Python 缺陷任务和公开测试。数据固定到上游提交 `4257f44b0ff1181dedaedee6a447e133219fcebf`，只包含缺陷程序与由公开用例改写的测试，不向 Agent 提供参考修复。

默认配置分为：

- `configs/model.yaml`：模型 Provider、权重路径、设备、精度和生成参数；
- `configs/runtime.yaml`：Single-Agent 的步数、调用、Token 和总耗时预算。

## 5. 运行方式

```bash
conda activate multi_agent
python scripts/run_task.py \
  --task data/quixbugs/tasks/quixbugs_is_valid_parenthesization.json
```

命令退出码为 `0` 表示确定性终检成功，`1` 表示任务已正常结束但修复失败。两种情况都会输出并保存 `FinalReport`；运行时异常仍使用非零退出码和异常信息，不能伪装成任务级失败。

## 6. Phase 3 的复用边界

Phase 3 可以直接把 `SingleAgent` 作为统一预算下的 Baseline A，并复用 ModelAdapter、Tool Registry、TraceWriter、TaskSpec 和 FinalReport。Phase 3 新增的 Supervisor 与 Engine 不应修改本阶段工具安全边界，也不能把本阶段 1/5 的初步成功率包装为最终性能结论。
