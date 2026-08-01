# RepoPilot-MAS

基于动态任务图与对抗审查的多智能体代码修复系统。

## 当前阶段

Phase 0：项目骨架与开发环境初始化。

## 项目目标

面向程序缺陷修复任务，构建具备动态任务编排、状态管理、并行协作、交叉质疑、候选补丁竞争和测试驱动重规划能力的多智能体系统。

## 核心机制

- 动态任务图与条件路由
- 共享黑板与版本化状态管理
- 多 Agent 并行调度
- Challenge–Rebuttal–Arbitration
- 候选补丁竞争与交叉审查
- 测试驱动的失败恢复与动态重规划

## 安装开发依赖

```bash
python -m pip install -e ".[dev]"

```

## 运行测试

```bash
python -m pytest
```

## 代码检查

```bash
python -m ruff check .
```

## 项目计划

完整开发计划见：

`docs/RepoPilot-MAS_完整计划.md`