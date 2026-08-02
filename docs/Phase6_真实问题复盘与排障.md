# Phase 6 真实框架问题复盘与排障

## 1. 文档边界

本文只记录 RepoPilot-MAS 框架、编排和评测基础设施中真实遇到的问题，不记录 QuixBugs 某个算法的 Bug 定位过程。这里关注的是 Supervisor、Engine、Worker、LangGraph、Schema、Trace、预算、GPU 生命周期和评测协议为什么会出错，以及系统如何修复。

每个问题均按“现象—根因—排查—修复—验证—面试要点”记录。路径中的 `v1`～`v9` 是 Phase 6 development pilot，不是正式测试集结果。

## 2. API Supervisor 输出错误的 Worker 名称和 mode

**现象**

真实 Supervisor 曾创建 `worker_type="Investigator"`、`mode="default"`。系统实际只接受 `InvestigatorAgent`，且 Investigation 只能使用 `code_retrieval`、`failure_reproduction` 或 `dependency_analysis`。

**根因**

Prompt 只描述了逻辑角色，Schema 对 `worker_type` 和 `mode` 仅要求非空字符串。模型输出在自然语言上合理，但在协议上不可执行。只在 Prompt 中说“输出合法 JSON”不能替代枚举级约束。

**排查**

从 `supervisor_decision_rejected` Trace 向前定位对应原始响应，确认 JSON 语法正确，错误发生在语义契约而非解析层；再对照 WorkerPool 的真实映射表，发现名称和 mode 都没有单一权威枚举。

**修复**

- 在 Supervisor Prompt 中写入节点类型、Worker 类和 mode 的精确映射；
- 把 `SupervisorDecision` Schema 改为按 `node_type` 分支的嵌套 `oneOf`，每个分支只允许对应 Worker 与 mode；
- 增加真实错误样例的回归测试。

**验证**

`tests/test_supervisor_agent.py` 校验 Prompt，Schema 测试拒绝错误 Worker/mode。正式运行中的非法决定仍由 Engine fail-closed，而不是猜测模型意图。

**面试要点**

LLM 的“结构化输出”不等于“业务协议正确”。可靠系统要同时有 Prompt 约束、JSON Schema 约束和 Engine 状态约束。

## 3. Worker 失败后扩图被错误要求 Gate

**现象**

Investigation Worker 没有生成 Artifact 而进入 FAILED；Supervisor 尝试创建替代 Investigation 时，Engine 却以“扩图缺少 GateRecord”拒绝。此时没有可引用的成功 Artifact，形成无法恢复的状态。

**根因**

Engine 用“同类型节点总数”判断是不是第二次扩图，把 FAILED、TIMED_OUT、BLOCKED、CANCELLED 节点也计入有效扩展次数。失败重试被误认为基于新证据的语义扩图。

**排查**

对照节点状态、Artifact Store 和 Gate 校验输入，发现计数增长但 Artifact 数没有增长；用最小状态图复现“一个 FAILED Investigation + 一个替代 CREATE_TASK”。

**修复**

Gate 计数排除失败、超时、阻塞和取消节点。只有已有可用同类工作后继续扩展，才必须携带引用 Artifact、理由、预算影响和新增节点数的 GateRecord。

**验证**

`tests/test_phase3_engine.py` 覆盖失败替代无需伪造 Gate、真正扩图仍强制 Gate 两条路径。

**面试要点**

重试与扩图语义不同：前者恢复执行可靠性，后者增加推理宽度。状态机若只数节点、不看终态和产物，很容易制造不可恢复死角。

## 4. Supervisor 混淆 Node ID 与 Artifact Ref

**现象**

Supervisor 把 `N3` 这类节点 ID 填进 `hypothesis_ref`，或把 `N3.hypothesis@v1` 当作依赖节点 ID。JSON 字段类型都是字符串，基础 Schema 无法发现。

**根因**

节点与 Artifact 都使用可读字符串标识，但生命周期和可引用位置不同；快照没有把二者的使用规则强调给模型。

**排查**

非法选择在 Engine 的引用解析阶段失败。检查快照发现节点列表和 Artifact 列表同时出现，却没有紧邻字段的正反例。

**修复**

Prompt 明确：`depends_on` 只能是 Node ID，所有 `*_ref` 必须是带版本的 Artifact Ref；Engine 继续校验引用存在性和类型，不因字符串形似就接受。

**验证**

真实 pilot 后未再出现同类引用混淆；已有非法依赖和 Artifact 引用测试继续通过。

**面试要点**

LLM 协议设计应减少“同形不同义”的标识符；无法改类型时，至少要在上下文、Schema 描述和确定性解析器三处固定语义。

## 5. 同工作流阶段重试被误判为非法回退

**现象**

在 Investigation 内补建一个节点时，Supervisor 同时返回 `next_workflow_stage="investigation"`。Engine 把它当作阶段迁移，拒绝为非法操作。

**根因**

模型常会重复陈述当前阶段；Engine 只区分“有值/无值”，没有区分同阶段 no-op 与真正的前进/回退。

**排查**

Trace 显示 CREATE_TASK 本身合法，唯一失败字段是与当前阶段相同的 `next_workflow_stage`。

**修复**

CREATE_TASK 上的同阶段值按 no-op 处理；跨阶段仍必须满足工作流转换和 Gate 规则，不能借此绕过状态机。

**验证**

加入同阶段 CREATE 接受、非法跨阶段仍拒绝的回归测试。

**面试要点**

面向 LLM 的协议要容忍无害冗余，但不能放宽安全约束。好的做法是归一化 no-op，而不是把所有重复字段都当错误或全部忽略。

## 6. 决策历史没有进入 Snapshot，导致无进展循环

**现象**

一个 API Supervisor 重复输出已经处理过的 `ACCEPT_HYPOTHESIS` 和相同 `decision_id`，最终触发 `NO_PROGRESS_LOOP`。

**根因**

Engine 内部知道哪些 decision_id 已处理，也记录最近一次拒绝；但给 Supervisor 的 Snapshot 没有暴露这些信息。模型看见的世界没有变化，自然可能重复同一个决定。

**排查**

对比 Engine 内存状态、Checkpoint 和每轮 Supervisor 输入，确认状态变化存在于内部集合，却没有序列化进模型上下文。

**修复**

Snapshot 增加已处理 decision_id、最近一次 Supervisor 调用及应用/拒绝结果；Prompt 要求根据最近结果推进，不能重复已处理决定。

**验证**

development pilot v7 在相同消融配置下完成；Engine Snapshot 回归测试检查决策历史字段。

**面试要点**

循环检测只能止损，不能替代可观测状态。让决策者看到“上一个动作发生了什么”是闭环控制系统的基本要求。

## 7. Worker 自报调查完成，但没有调用受信测试工具

**现象**

`failure_reproduction` Worker 有时根据代码推断直接输出 Evidence，却没有调用 `run_tests`。Artifact 因缺少工具 Trace 被拒绝，固定流水线随上游失败而终止。

**根因**

本地模型倾向用语言解释替代真实执行。若框架只检查自然语言字段是否齐全，会把猜测当成复现证据。

**排查**

检查 Worker 原始响应、工具调用事件和 Artifact Schema，确认不是测试命令失败，而是根本没有执行测试。

**修复**

不降低证据门：`failure_reproduction` 必须引用真实 `run_tests` Trace。Worker 未执行时节点结构化 FAILED，Supervisor 可按预算替代或终止。

**验证**

正式评测中这类失败被保留，没有被人工补写成成功。独立审计要求所有成功任务绑定真实 ValidationResult。

**面试要点**

这是预期的 fail-closed，不是为了提高数字就应该“修掉”的错误。Agentic 系统的关键不是模型永不犯错，而是模型犯错时证据门不失效。

## 8. 多系统顺序运行时发生跨系统 CUDA OOM

**现象**

单独运行 Fixed 或 Dynamic 都可成功，但在同一 Python 进程按 `local → fixed → dynamic` 顺序运行时，Dynamic 加载本地模型发生 CUDA OOM。简单 `del`、`gc.collect()` 和 `torch.cuda.empty_cache()` 仍会复现。

**根因**

模型适配器、WorkerPool、LangGraph Runtime 和异步执行对象之间存在引用链。仅删除顶层局部变量不保证模型权重立即析构；CUDA allocator 清缓存也不能释放仍被 Python 对象引用的 Tensor。

**排查**

- 分别运行单系统，排除模型本身装不下；
- 用 development pilot v8 稳定复现 Fixed 完成后 Dynamic OOM；
- 比较系统切换前后的进程显存，确认是生命周期问题而非单任务峰值；
- 检查对象引用后确认适配器没有显式关闭协议。

**修复**

- `ModelAdapter` 增加统一 `close()` 生命周期接口；
- `LocalTransformersAdapter.close()` 主动清空 model/tokenizer 引用并释放 CUDA cache；
- 评测器在每个系统结束后关闭全部 Worker 模型，再清理 Runtime/Pool/Supervisor 引用；
- 保留 no-op 基类实现，避免给非 GPU Adapter 增加条件分支。

**验证**

development pilot v9 在同一进程依次完成五个系统；正式 50 任务运行无跨系统 OOM。`tests/test_models.py` 验证 close 后持有对象被清空。

**面试要点**

GPU 服务的资源生命周期必须是显式协议，不能依赖垃圾回收时机。`empty_cache()` 只处理缓存，不处理活引用。

## 9. Development 与冻结 Test 共用入口造成泄漏风险

**现象**

Phase 6 初版 runner 只认识测试任务。为了调通框架而反复运行这些任务，会让 Prompt、Schema 和预算调整间接针对测试集。

**根因**

数据清单虽有历史 5 个任务，却没有在运行器层强制 development/test 边界。

**排查**

审查正式评测协议时发现“调试任务选择”没有可执行约束，只靠开发者自觉。

**修复**

Suite 显式保存两组 ID，CLI 必须选择 `--split development|test`；development 运行永远不能通过正式验收，只有完整 test split × 全系统才可设置 `final_evaluation_executed=true`。

**验证**

单元测试检查两组无交集；独立审计再次对照 manifest、Suite 和 50 个结果矩阵。

**面试要点**

评测泄漏不仅来自训练数据，也来自调 Prompt 和修框架。最可靠的边界应写进入口和 Manifest，而不是写在 README 里提醒。

## 10. 新增 TaskSpec 后测试仍硬编码旧数量

**现象**

加入 10 个冻结任务后，旧测试仍断言目录中只有 5 个 TaskSpec，导致“代码正确但门禁失败”。

**根因**

测试验证的是偶然数量，而不是“清单中的任务完整且原始 Bug 可复现”的业务不变量。

**排查**

比较文件系统任务数、Suite manifest 与失败断言，确认数据扩充是计划内变化。

**修复**

更新测试为 15 个总 TaskSpec，并逐个加载、执行原始目标测试，确保每个冻结 Bug 在修复前确实失败。

**验证**

`tests/test_tasks.py` 覆盖全部 15 个 TaskSpec 和原始失败复现。

**面试要点**

测试应绑定稳定契约。若确实需要固定数量，应由版本化 manifest 提供权威值，避免多处散落 magic number。

## 11. Ruff 自动修复建议与冻结基准冲突

**现象**

Ruff 对 QuixBugs 原始 `flatten.py` 报 `UP028`，建议把 `yield` 改为 `yield from`。直接修复会修改基准 Bug 源，破坏数据完整性。

**根因**

全仓静态检查默认把第三方冻结数据当作产品源码。质量工具没有理解 benchmark provenance。

**排查**

确认告警只来自冻结的 buggy source，并检查修改是否会影响任务语义和 source fingerprint。

**修复**

只对该冻结文件精确配置 `UP028` per-file ignore；没有扩大目录级忽略，也没有改原始源文件。

**验证**

Ruff 全仓通过，TaskSpec 原始目标测试仍失败，50 个运行前后 source digest 一致。

**面试要点**

代码质量门也必须尊重数据边界。对第三方基准最小豁免比“为了全绿改数据”更正确。

## 12. 预算耗尽被误判为整场评测失败

**现象**

正式 50 任务全部生成结果，但原始 runner 返回非零，因为 Local Single-Agent 有 7 个任务耗尽预算，`no_budget_violations=false`。

**根因**

验收门把“每个任务是否成功”与“实验是否有效”混为一谈。预算本来就是基线能力的一部分；在预算外继续并声称成功才是协议违规，预算耗尽后失败关闭则是合法观测。

**排查**

逐项检查 7 个结果，确认它们都被标记 `status=failed`、reason 为预算违规，没有纳入成功数，也没有遗漏 Trace。

**修复**

门禁改为 `budget_outcomes_fail_closed`：预算内允许成功或失败，预算外只能失败。新增独立审计脚本，不覆盖原始 `acceptance.json`，确保修正口径不篡改数据。

**验证**

`reports/phase6/final_audit.json` 记录 7 个预算耗尽失败，15 项门禁通过；单元测试拒绝“预算外却成功”的伪结果。

**面试要点**

评测基础设施也需要威胁建模。验收的是数据完整性和协议遵守，不是强迫所有样本都成功。

## 13. 同一次 Supervisor 拒绝在 Trace 中被重复计数

**现象**

`supervisor_decision_rejected` 同时由 Engine 和 Runtime 写入，初版指标按 event_type 直接计数，使 Invalid Decision 数翻倍。

**根因**

事件总线允许不同层记录同一事实，但聚合器没有定义 canonical event。事件类型相同不代表统计实体不同。

**排查**

按 decision_id 对齐相邻事件，发现一条包含 Engine 规范化 `code`，另一条是 Runtime 镜像说明。

**修复**

只把携带 Engine 错误码的拒绝事件作为规范计数来源；保留 Runtime 镜像以便阅读，不删除原始 Trace。

**验证**

回归测试构造同一拒绝的两层事件并断言只计 1 次；独立审计从原 Trace 重算，Dynamic Hybrid 为 5 次，而不是重复值。

**面试要点**

可观测性必须定义事件所有权、幂等键和聚合口径。否则日志越丰富，指标反而越不可信。

## 14. 凭据自动加载被误诊为“环境变量缺失”

**现象**

Shell 中显式检查 `DASHSCOPE_API_KEY_1/2` 时显示不存在，一度怀疑正式运行无法调用 API；但 Router 真正发请求时可正常获得凭据。

**根因**

系统设计为 Router 调用时加载 `.env.supervisor`，而不是启动 Shell 时 export 到全局进程。检查位置早于凭据加载点。

**排查**

显式使用 `env -u` 移除两个变量后运行完整 development pilot 和正式评测；API Trace 仍成功，并只记录环境变量名。

**修复**

不改变安全设计；文档区分“进程环境当前值”和“credential loader 可解析值”，预检必须通过 Router 的脱敏接口，而不是直接打印环境变量。

**验证**

正式运行完成真实 API 调用；凭据扫描确认配置、报告、Trace 和受跟踪文件无真实 Key。

**面试要点**

安全加载往往是惰性的。诊断凭据问题时要在真正的解析边界观察，同时避免用 `printenv` 把密钥带进终端日志。

## 15. 长任务看似卡死，难以区分活跃与死锁

**现象**

动态任务单例耗时可达数分钟，终端长时间没有输出，看起来像 Runtime 死锁或 API 卡住。

**根因**

批量 runner 只在任务开始/结束打印进度；真实工作发生在 API 请求、本地推理和工具子进程中。仅凭 stdout 无法判断活性。

**排查**

以只读方式检查 Trace 尾部时间戳、最近 event_type、模型原始响应文件更新时间和进程/GPU 状态。若 Trace 持续增长则是长尾执行，不是死锁。

**修复**

保留 API 请求超时、Worker ReAct 时间、Engine 总运行时间、Token 和调用次数多层预算；每个事件立即追加 JSONL，使外部监控可以不干预运行地判断进度。

**验证**

正式批次最终完成 50/50，未靠杀进程跳过慢任务；每个失败或终止都有结构化原因。

**面试要点**

“没有终端输出”不是死锁证据。Agent 系统需要心跳式可观测状态和分层 deadline，排查时首先区分慢、阻塞、死锁和资源耗尽。

## 16. 账号/模型槽位在长批次中真实耗尽

**现象**

两个消融批次中，Max 的账号槽位先后返回明确额度耗尽。如果把所有 429 都永久拉黑，或遇错直接回退本地 Supervisor，会破坏路由协议和实验配置。

**根因**

Provider 错误至少包含明确额度耗尽、瞬时限流、鉴权失败、模型错误四类，恢复策略不能共用一个“换模型重试”。

**排查**

检查脱敏路由事件的 error classification、模型 ID 与 api_key_env，确认实际顺序为模型优先、账号其次；没有查看或输出 Key 值。

**修复**

沿用 Phase 3 路由器规则：明确额度耗尽才持久标记槽位；瞬时限流有限退避但不永久扣除；鉴权和模型配置错误结构化失败；六槽位全不可用时终止，不静默改用本地 Supervisor。

**验证**

Phase 6 Route Usage 显示账号 1 Max 切账号 2 Max，再切账号 1 Flash；批次继续完成且配置身份不变。

**面试要点**

故障转移不只是循环 API Key。必须先做错误分类，再决定状态是否持久化，否则一次瞬时限流可能永久污染整个长任务队列。

## 17. Development Pilot 时间线

| Pilot | 主要目的与结果 | 暴露的问题 |
| --- | --- | --- |
| v1 | Local、Fixed 可运行；切换后中断 | 跨系统显存没有可靠释放 |
| v2 | 首次完整 Dynamic 调用 | 错误 Worker/mode、失败后 Gate 死角 |
| v3 | 修正契约后 Dynamic 成功 | 证明 API Supervisor 闭环可继续 |
| v4 | 重试工作流 | 同阶段字段被误判为非法迁移 |
| v5 | No Second 消融 | 消融约束可执行 |
| v6 | No Challenge 消融 | 决策历史不可见，触发无进展循环 |
| v7 | 补充 Snapshot 历史后重跑 | No Challenge 闭环成功 |
| v8 | 五系统同进程顺序运行 | `del + gc + empty_cache` 仍在 Fixed→Dynamic OOM |
| v9 | 引入显式 `ModelAdapter.close()` | 五系统连续完成，成为正式运行前最终预演 |

所有 pilot 都只使用 development split。正式 test split 只在框架和评测协议冻结后运行一次；失败的 pilot 不进入正式成功率表。

## 18. 总结

这些问题共同说明，RepoPilot-MAS 的难点不在“多写几个角色 Prompt”，而在边界协议：

- LLM 决策需要 Schema 和确定性状态机共同约束；
- 重试、扩图、阶段迁移和引用必须有精确语义；
- Worker 证据不能被自然语言自报替代；
- GPU、API 配额和异步 Runtime 都需要显式生命周期；
- Trace 必须可去重、可追溯，评测门必须区分任务失败与实验无效；
- 开发集、测试集和第三方冻结源必须在代码层隔离。

面试时可以从“错误 Worker/mode → Gate 死角 → 决策历史缺失 → GPU OOM → 评测口径错误”这条链说明项目如何从能跑的 Demo 演化为可审计系统。
