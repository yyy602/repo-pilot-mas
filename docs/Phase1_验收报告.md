# Phase 1 验收报告

> 验收日期：2026-08-01  
> 计划依据：`docs/RepoPilot-MAS_完整计划.md` 第 10.3、10.4 和 14 节  
> 结论：Phase 1 已通过全部关闭门槛，可以进入 Phase 2。

## 1. 验收环境与命令

- Conda 环境：`multi_agent`
- Python：3.10.20
- pytest：9.1.1
- Ruff：0.16.1

```bash
conda activate multi_agent
python -m pytest
python -m ruff check .
git diff --check
```

最终结果：48 项测试全部通过，Ruff 无告警，Git Diff 空白检查通过。

## 2. 关闭门槛证据

| 验收项 | 结果与主要证据 |
| --- | --- |
| 测试成功、普通失败、超时 | 已覆盖三种返回分支，并验证超时会终止子进程组 |
| 受信可执行文件 | 命令绑定到受信绝对路径，伪造同名解释器不能产生假通过 |
| 路径边界 | 拒绝绝对路径、目录穿越、`.git` 和越界符号链接 |
| Patch 分支 | 覆盖新增、修改、删除、二进制拒绝和非法路径拒绝 |
| Patch 原子性 | `git apply --check` 失败时工作树内容不变 |
| 候选隔离 | 两个候选工作树和原始仓库互不污染 |
| 工作区身份 | 回滚和删除前验证受管布局；拒绝工作树 symlink 替换 |
| baseline 恢复 | baseline 去除写权限并记录摘要；摘要变化时拒绝回滚 |
| 输出资源边界 | stdout、stderr 在读取时仅保留有界头尾内容 |
| Diff 资源边界 | 文件流式比较；超大文件不读入 Diff；文本 Diff 使用有界缓冲区 |
| protected paths | Patch 前置阻断，并在 Diff 后置检查中淘汰直接修改 |
| 九工具返回约定 | 九个工具均验证 Trace ID、结构化错误和 JSON 序列化 |
| 工程检查 | 48 项 pytest 全绿，Ruff 全绿，`git diff --check` 通过 |

## 3. 安全边界说明

Phase 1 提供的是应用层路径、命令、资源和工作区约束，当前只用于受信的 QuixBugs Python 数据。它不等同于操作系统沙箱；接入真实第三方仓库前，仍必须使用容器或等价隔离，并配置只读挂载、资源限制和禁网策略。
