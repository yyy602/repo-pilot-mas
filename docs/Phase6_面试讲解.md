# RepoPilot-MAS 面试讲解与简历表述

## 1. 两分钟讲解

我做的是一个面向代码修复的层级式多智能体系统。它不是把十几个角色串成固定 Prompt 流水线，而是把语义决策和工程约束拆开：阿里云强模型驱动的 SupervisorAgent 看到压缩后的全局状态，决定是否补调查、接受根因、生成补丁或重规划；OrchestrationEngine 不做语义推理，只负责 Schema、动态任务图、预算、Gate 和安全状态迁移；本地 Qwen3-8B Worker 负责调查、诊断、审查和补丁生成；LangGraph 承载异步循环、SQLite Checkpoint 和恢复。

Worker 不能直接改任务图，只能返回结构化 Artifact。补丁只能进入隔离候选工作区，最终成功必须绑定真实目标测试、完整回归、静态检查和受保护路径检查。API Supervisor 输出错误 Worker 名称、重复决策或非法引用时，Engine 会拒绝，而不是猜测模型意图。

我用冻结的 10 个 QuixBugs Python 测试任务评测了三个主系统和两个消融。单 seed 下，本地 Single-Agent 完成 1/10，固定 Hybrid 完成 2/10，动态 Hybrid 完成 7/10；动态系统等价 API 成本约 8.94 元，中位时延约 245 秒，所以它提升了这个小样本协议内的完成数，但也明显更贵、更慢。50 个结果全部保留，失败和预算耗尽都计入分母。

我还专门区分了机制可达性和真实效果。Phase 5 的确定性案例证明 Challenge/Rebuttal、双补丁竞争和定向重规划能闭环；但 Phase 6 真实运行没有触发有效 Challenge，3 次重规划也没有恢复，因此我不会声称这些机制已经带来统计收益。这也是项目当前最需要扩展的数据和评测方向。

## 2. 推荐简历描述

项目标题：

> RepoPilot-MAS：基于 API Supervisor、动态任务图与确定性验证的多智能体代码修复系统

三条描述：

- 设计 SupervisorAgent / OrchestrationEngine 分层架构：由阿里云 Qwen API 负责全局语义规划，本地 Qwen3-8B 专业 Worker 执行调查、诊断、审查与补丁任务，使用 LangGraph + SQLite 实现异步编排、断点恢复和可审计状态。
- 实现结构化 Artifact、动态 Gate、预算/循环检测、候选工作区隔离及目标测试、完整回归、静态检查、Protected Path 的 fail-closed 验证；真实处理错误 Worker/mode、决策重复、Gate 死角、跨系统 CUDA OOM 和 API 配额故障转移。
- 在固定 seed 的 10 个 QuixBugs Python 测试任务上，Dynamic Hybrid 完成 7/10，对比同预算上限的 Fixed Hybrid 2/10 与本地 Single-Agent 1/10；保留 50 条任务结果与 Trace，并报告 887,520 Token、245 秒中位时延和约 ¥8.94 等价 API 成本。

如果简历篇幅有限，第三条必须保留“10 个任务、固定 seed、等价成本”这些限定，不能只写百分比提升。

## 3. 高频追问

### 为什么还需要 SupervisorAgent，Engine 不能直接规则路由吗？

Engine 擅长判断一个动作是否合法，却不能可靠判断证据是否充分、两个根因是否真正冲突、验证失败应回到哪个语义阶段。Supervisor 负责开放式语义决策，Engine 把动作限制在安全、可追踪的协议内。二者分别解决“该做什么”和“是否允许这样做”。

### 为什么使用 LangGraph？

LangGraph 负责固定的系统执行循环、异步节点调度、SQLite Checkpoint、恢复和人工介入。领域 TaskGraph 仍由 Supervisor 动态创建，Engine 是其唯一状态权威。这样没有用 LangGraph 的静态图替代动态任务图，也没有维护两份相互冲突的业务状态。

### 最大的工程问题是什么？

最典型的是 API Supervisor 输出了自然语言上合理、协议上错误的 Worker/mode。继续加 Prompt 不够，我最终做了三层约束：Prompt 精确映射、JSON Schema 分支枚举、Engine 引用与状态校验。另一个深层问题是多系统顺序评测时 CUDA OOM；`empty_cache()` 没用，因为模型仍被对象引用，最后通过 ModelAdapter 的显式 close 生命周期解决。

### 7/10 是否证明多 Agent 一定更好？

不能。它只说明在这个 10 任务、单 seed、固定配置中 Dynamic Hybrid 完成更多任务。与 Single-Agent 的比较还包含强 API Supervisor 的模型能力差异；与 Fixed Hybrid 的比较更接近机制对照，但样本仍小，且两者实际 API 调用数差异很大。需要更多任务、多 seed 和显著性分析后才能做普遍结论。

### 两个消融各少一个成功，能否证明对应机制有效？

不能。Dynamic 主系统本次也没有执行第二诊断者或有效 Challenge，所以两个消融没有真正移除已触发的机制。1 个任务差异更可能混有 API 槽位和运行波动。当前只能证明消融约束入口可执行。

### 为什么保留失败结果？

Agent 系统最容易通过删超时、删预算耗尽样本虚高成功率。我的 runner 把每个系统—任务都写入结果矩阵，预算外结果只能失败关闭。独立审计还检查 50 个结果、Trace、Checkpoint、Source Digest 和成功验证门，失败本身也是框架可靠性的证据。

## 4. 不应使用的表述

- 不说“成功率提升 60%”，改说“在 10 个冻结任务中由 1/10 到 7/10”；
- 不说“Challenge 提升成功率”，因为真实评测有效 Challenge 为 0；
- 不说“重规划可以恢复失败”，因为本次 3 次尝试、0 次恢复；
- 不说“并行加速明显”，因为 Dynamic 只记录 1 对 Worker 重叠；
- 不说“支持 SWE-bench/Java/C”，因为没有运行证据；
- 不说“成本为零”，免费额度不等于资源成本为零，应报告等价公开单价成本。

## 5. 下一步方向

优先扩展到有真实 Issue、多文件上下文和容器环境的 SWE-Gym Lite 小子集；在不改测试协议的前提下增加多个 seed，预标需要冲突消解和重规划的任务，分别测机制触发率、恢复率和成本。只有这些证据齐备后，再更新简历中的因果表述。
