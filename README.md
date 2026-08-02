# RepoPilot-MAS

基于动态任务图与对抗审查的多智能体代码修复系统。

## 当前阶段

Phase 0、Phase 1、Phase 2、Phase 3、Phase 4 已完成；当前下一阶段为 Phase 5（动态门控与对抗协作），尚未开始。

当前版本已经打通本地 Qwen3-8B Single-Agent 修复闭环：模型只能按结构化 Schema 选择九个确定性工具，测试命令和受保护路径由 TaskSpec 固定；系统在隔离工作区应用 Patch，并以真实目标测试、完整回归、静态检查和 Diff 决定最终结果。阶段状态与后续验收以唯一计划基线为准。

Phase 3 已实现真实 SupervisorAgent 主导的 LangGraph 持久化运行循环，以及确定性的 OrchestrationEngine、动态 TaskGraph、版本化 Blackboard 和审计检查点。Supervisor 使用阿里云百炼强模型 API，LangGraph 负责恢复、流式事件和人工介入，本地 Qwen3-8B 只保留给 Phase 4 Worker；路由按 `qwen3.7-max-2026-06-08`、`qwen3.7-flash`、`qwen3.7-flash-2026-07-15` 的模型顺序，并在每个模型内按账号 1、账号 2 顺序切换。

Phase 4 已实现四类专业 Worker、`AsyncSqliteSaver` 持久化异步调度和双 GPU 本地 Qwen3-8B 模型池。最终真实验收完成两路调查、两路独立诊断、引用审查和两个隔离 Patch 工作区；7 个节点全部成功、7 个 Artifact 全部通过 Schema，两个候选补丁的目标测试均通过。Supervisor 仍负责全局路由，Worker 不直接修改 TaskGraph。

## 已实现工具

| 工具 | 功能 |
| --- | --- |
| `list_files` | 在安全路径边界内列出仓库目录 |
| `search_code` | 关键词或正则检索文本代码 |
| `inspect_code` | 查看文件行区间或 Python 符号上下文 |
| `find_references` | 基于 Python AST 查找符号定义与引用 |
| `run_tests` | 在超时和命令白名单约束下运行 pytest 或 unittest |
| `apply_patch` | 先校验再使用 `git apply` 应用 Unified Diff |
| `collect_diff` | 比较候选工作区与只读基线并生成差异 |
| `rollback_workspace` | 将候选工作区恢复到初始基线 |
| `static_check` | 执行 Python 语法检查，并在可用时运行 Ruff |

## 核心基础设施

- `TaskSpec`：代码修复任务的统一输入结构；
- `ToolResult`：包含状态、错误、标准输出、退出码、耗时、截断标记和 Trace ID；
- `PathGuard`：拒绝绝对路径、目录穿越、`.git` 访问和越界符号链接；
- `WorkspaceManager`：为每个候选 Patch 创建独立的只读校验基线与可写工作树；
- `CommandRunner`：禁用 Shell 展开、绑定受信可执行文件、终止超时进程组并流式限制输出。

## 安装开发依赖

```bash
python -m pip install -e ".[dev]"
```

运行本地模型还需要模型依赖：

```bash
python -m pip install -e ".[dev,model]"
```

## 运行测试

```bash
conda activate multi_agent
python -m pytest
```

## 代码检查

```bash
conda activate multi_agent
python -m ruff check .
```

当前验收环境为 Python 3.10.20、LangGraph 1.2.10、pytest 9.1.1、Ruff 0.16.1；全量 110 项测试通过、Ruff 无告警。详见 `docs/Phase4_验收报告.md`。

## 运行 Single-Agent 任务

```bash
conda activate multi_agent
python scripts/run_task.py \
  --task data/quixbugs/tasks/quixbugs_is_valid_parenthesization.json
```

成功和失败任务都会在 `reports/phase2/runs/` 保存 FinalReport 与 Trace。命令退出码为 `0` 表示确定性终检成功，`1` 表示任务闭环正常结束但没有修复成功。

Phase 2 的 5 个 QuixBugs 初步基线为 1/5 成功。该结果用于证明闭环及固定后续对照基线，不代表最终系统性能；精确运行 ID、Token、工具调用和耗时见 `reports/phase2/acceptance_summary.json`。

## Supervisor API 凭据

仓库只提交占位模板，真实 Key 不得写入 YAML 或文档：

```bash
cp .env.example .env.supervisor
chmod 600 .env.supervisor
```

在 `.env.supervisor` 中填写两个账号的 Key。该文件由 `.gitignore` 的 `.env.*` 规则忽略；Phase 3 运行入口会自动读取它，并保证 Key 不进入日志、Trace、检查点或报告。非敏感路由配置见 `configs/supervisor.yaml`，六个 API 槽位的脱敏预检结果见 `reports/phase3/preflight.json`。

经 LangGraph 运行一次真实 Supervisor 决策，并在 Worker 派发前进入可恢复人工中断：

```bash
conda activate multi_agent
python scripts/run_phase3_supervisor.py \
  --task data/quixbugs/tasks/quixbugs_is_valid_parenthesization.json
```

生成可重复的动态图、降级、自动闭环、重启恢复和人工中断证据：

```bash
conda activate multi_agent
python scripts/run_phase3_acceptance.py
```

运行真实 Phase 4 Worker 池与确定性超时隔离验收：

```bash
conda activate multi_agent
python scripts/run_phase4_acceptance.py
python scripts/run_phase4_timeout_acceptance.py
```

## 项目计划

- 唯一计划基线（v2.4）：`docs/RepoPilot-MAS_完整计划.md`
- Phase 1 从属设计说明：`docs/Phase1_确定性工具层.md`
- Phase 1 验收报告：`docs/Phase1_验收报告.md`
- Phase 2 从属设计说明：`docs/Phase2_模型适配层与单智能体基线.md`
- Phase 2 验收报告：`docs/Phase2_验收报告.md`
- Phase 3 验收报告：`docs/Phase3_验收报告.md`
- Phase 4 从属设计说明：`docs/Phase4_专业Worker池.md`
- Phase 4 验收报告：`docs/Phase4_验收报告.md`
