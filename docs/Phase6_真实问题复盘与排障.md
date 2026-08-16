# Phase 6 真实框架问题复盘与排障

## 1. 文档边界

本文只记录 RepoPilot-MAS 框架、编排和评测基础设施中真实遇到的问题，不记录 QuixBugs 某个算法的 Bug 定位过程。这里关注的是 Supervisor、Engine、Worker、LangGraph、Schema、Trace、预算、GPU 生命周期和评测协议为什么会出错，以及系统如何修复。

每个问题均按“现象—根因—排查—修复—验证—面试要点”记录。路径中的 `v1`～`v9` 是 Phase 6 development pilot，不是正式测试集结果。

## 2. API Supervisor 输出错误的 Worker 名称和 mode

**现象**

真实 Supervisor 曾创建 `worker_type="Investigator"`、`mode="default"`。系统实际只接受 `InvestigatorAgent`，且 Investigation 只能使用 `code_retrieval`、`failure_reproduction`、`dependency_trace` 或 `evidence_completion`。

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

早期两个消融批次中，Max 的账号槽位先后返回明确额度耗尽；闭环修复过程中普通 Flash 和带日期 Flash 也依次耗尽，最终六个槽位全部不可用。如果把所有 429 都永久拉黑，或遇错直接回退本地 Supervisor，会破坏路由协议和实验配置。

**根因**

Provider 错误至少包含明确额度耗尽、瞬时限流、鉴权失败、模型错误四类，恢复策略不能共用一个“换模型重试”。

**排查**

检查脱敏路由事件的 error classification、模型 ID 与 api_key_env，确认实际顺序为模型优先、账号其次；没有查看或输出 Key 值。

**修复**

沿用 Phase 3 路由器规则：明确额度耗尽才持久标记槽位；瞬时限流有限退避但不永久扣除；鉴权和模型配置错误结构化失败；六槽位全不可用时终止，不静默改用本地 Supervisor。2026-08-16 路由迁移后仍保持三模型六槽位。

**验证**

Phase 6 Route Usage 显示账号 1 Max 切账号 2 Max，再切普通 Flash，闭环修复运行继续切到带日期的 Flash；最终诊断 Trace 完整记录六槽位全部耗尽。各次批次都保持相同配置身份，且只把 Provider 明确返回的免费额度耗尽持久化。

**面试要点**

故障转移不只是循环 API Key。必须先做错误分类，再决定状态是否持久化，否则一次瞬时限流可能永久污染整个长任务队列。

## 17. Supervisor 模型路由配置与计划基线发生漂移

**现象**

早期计划和用户约定的模型顺序是 `qwen3.7-max-2026-06-08 → qwen3.7-flash → qwen3.7-flash-2026-07-15`，但实际配置一度出现不存在于当时约定中的 `qwen3.7-plus`、`qwen3.8-max`。如果不检查真实 Route Trace，可能误以为系统正在使用计划中的强模型。2026-08-16 用户明确发起模型迁移并确认最终三模型基线后，`qwen3.8-max` 成为新基线的一部分，不再属于配置漂移。

**根因**

模型路由同时出现在计划、配置和测试描述中，但测试只验证“能加载若干 route”，没有锁定模型顺序和两个账号展开后的六个槽位。

**排查**

对照唯一计划基线、`configs/supervisor.yaml` 和脱敏 `supervisor_route_attempt` 事件，逐项核对 `model_id + api_key_env`，确认是配置漂移而非 Provider 返回了别名。

**修复**

- 当时恢复三个约定模型和“模型优先、账号其次”的顺序；
- 测试精确断言当时的六个路由槽位；
- Development manifest 保存脱敏后的模型顺序与环境变量名，不保存 Key 值。

新基线迁移时同步修改唯一计划、配置和契约测试。2026-08-16 闭环修复期进一步调整为 `deepseek-v4-flash-0731 → deepseek-v4-pro-0813 → qwen3.8-max`，仍按模型内账号 1、账号 2 的顺序展开六个槽位，以保留强模型免费额度处理前序模型无法完成的决策。

**验证**

历史真实调用先尝试两个 Max 槽位，再尝试两个 Flash 槽位，最后进入带日期的 Flash；额度耗尽和瞬时超时分别记录，不混为同一状态。2026-08-16 新六槽位逐项探测有 5 个成功，仅 Flash 的账号 1 免费额度耗尽，账号 2 可正常接管。脱敏汇总与路由 Trace 保存在 `reports/closed_loop/evaluation/supervisor_route_probe_20260816/`。

**面试要点**

模型名称也是实验配置的一部分。路由“能工作”不代表实验身份正确，模型 ID、账号槽位、顺序和错误分类都应进入可审计 Manifest。

## 18. Development 与 Frozen Test 清单和任务资产不一致

**现象**

修复计划要求用 `find_first_in_sorted`、`gcd`、`flatten`、`bucketsort`、`is_valid_parenthesization` 做 Development，但套件一度把其中两个任务放在 Test；闭环套件还声明了 20 个任务，仓库却只有 15 份真实 TaskSpec。

**根因**

文档、Suite ID 列表、TaskSpec 文件和真实仓库目录没有由同一测试共同约束，导致“清单写了”与“入口可执行”分离。

**排查**

从 runner 的 `selected_task_ids` 反向检查 Suite，再逐个解析 TaskSpec、解析仓库路径并执行原始目标测试，发现清单和资产数量不闭合。

**修复**

- Development 固定为计划规定的五题，Frozen Test 保持十题且两者无交集；
- 补齐五个真实任务仓库与 TaskSpec；
- 测试同时锁定 5/10 划分、总资产数、路径存在和原始 Bug 可复现。

**验证**

runner 只能从所选 split 取任务；Development 单题不能越界选择 Test ID，完整 Test 仍严格要求十题。

**面试要点**

数据集隔离必须是“清单、资产、入口、Manifest”四层一致，不是只在文档中列两个名称列表。

## 19. 已批准 Replan 被错误当作“重规划额度耗尽”

**现象**

Validation 失败后，Supervisor 成功发出一次 `REQUEST_REPLAN`。Engine 已创建 ReplanRecord 并把剩余新建额度降为零，但下一轮在真正创建恢复 Patch 前就终止，理由是“最大重规划次数已用完”。

**根因**

系统把“还能否批准新的 Replan”与“是否必须执行已经批准的 Replan”混成一个布尔判断。计数在批准时增加，却没有显式保存 pending recovery。

**排查**

按时间对齐 ReplanRecord、预算计数、工作流阶段和下一次 Supervisor Snapshot，发现记录已存在但图中尚无目标恢复节点。

**修复**

- Snapshot 增加 `recovery.pending_replan`、目标阶段、目标节点类型、Replan Ref 和触发引用；
- pending 状态下最小 Schema 只允许创建对应恢复节点；
- Engine 禁止在恢复节点创建前终止；
- 安全 Fallback 在模型再次失败时也能执行已经批准的恢复。

**验证**

针对性 `bucketsort` 运行中，补丁 Worker 超时后成功完成 Replan、创建替代 Patch 并最终通过 Validation。

**面试要点**

额度是对“新授权”的限制，不应撤销已经提交的状态转换。可靠状态机要把授权和执行拆成两个可恢复状态。

## 20. failure_class、回退阶段和工作流转换没有绑定

**现象**

API Supervisor 曾把 `evidence_incomplete` 回退到 Patch，也曾从 Investigation 直接声明非法阶段。字段各自都在枚举中，因此普通 JSON Schema 仍会放行这个组合。

**根因**

Schema 只约束单字段取值，没有表达跨字段关系；Engine 到应用阶段才发现语义组合不合法，增加无效调用和恢复复杂度。

**排查**

把被拒决定按 `failure_class + next_workflow_stage + current_stage` 聚合，确认错误集中在合法值的非法组合，而不是拼写错误。

**修复**

- 阶段 Schema 只暴露当前状态可达的下一阶段；
- 每个 `failure_class` 用独立分支绑定唯一或有限目标阶段；
- pending Replan 进一步绑定目标 Worker 节点类型。

**验证**

Schema 回归测试拒绝错误组合；真实 Supervisor 不再依赖 Engine 末端猜测回退意图。

**面试要点**

业务 Schema 的难点常在字段关系，不在字段类型。`enum + enum` 并不能表达有效笛卡尔子集。

## 21. Hypothesis Comparison Review 引用错误导致 Runner 崩溃

**现象**

Supervisor 创建 `hypothesis_comparison` Review 时把 Evidence 当成比较目标。Collector 把结果加入 Blackboard 后，根因解析访问错误类型并抛异常，整题被标为 `RUNNER_ERROR`。

**根因**

Review 只有单个 `target_artifact_ref`，比较模式没有强制“全部候选 Hypothesis”；同时 Blackboard 在完整语义校验前先修改 Artifact Store，违反原子性。

**排查**

从 Runner 异常栈回溯到 Review target，再检查 Store 状态，确认错误既包含输入契约缺口，也包含“先写入、后校验”的事务边界问题。

**修复**

- Review 增加 `target_artifact_refs`；比较模式必须覆盖全部候选 Hypothesis，并选择其中一个作为主目标；
- 阶段最小 Schema 把候选引用写成精确枚举与数量约束；
- Collector 在进入 Blackboard 前预检目标类型；
- Blackboard 先完成所有根因 Review 校验，再修改 Store 和 Resolution。

**验证**

错误引用被转成结构化、可恢复的 `ARTIFACT_SCHEMA_ERROR`，不会再击穿 Runner；合法比较 Review 能更新 HypothesisResolution。

**面试要点**

Agent Artifact 写入也需要事务语义。校验失败时若状态已部分提交，Checkpoint 会把一次模型错误放大为持久状态损坏。

## 22. Patch Review 缺 Evidence，但报告错误地显示零契约拒绝

**现象**

真实 Supervisor 创建 Patch Review 时只传 Patch 和 Hypothesis，没有直接 Evidence。创建时被正确拒绝，但聚合报告中的 `contract_rejection_count` 仍为 0。

**根因**

一方面，阶段 Schema 只约束 Artifact Ref 是字符串，没有表达各种 Artifact 类型的数量；另一方面，指标只统计派发期事件，漏掉创建期 Engine 拒绝。

**排查**

同时读取原始 Decision、Engine 拒绝 Trace、派发 Trace 和最终 summary，确认控制面已经 fail-closed，但观测面漏计。

**修复**

- JSON Schema 校验器补齐 `allOf`、`contains`、`minContains`、`maxContains`、`maxItems` 和 `uniqueItems`；
- 每个 Agent/mode 在创建期编码所需 Artifact 类型与数量；
- 指标对创建期和派发期同一拒绝按规范键去重计数。

**验证**

历史 Trace 重算后能得到 1 次真实契约拒绝；修复后的完整 Development 把契约拒绝为 0 作为 Frozen Test 前硬门禁。

**面试要点**

“安全规则生效”与“指标反映规则生效”是两个独立验收面。错误指标会让正确的 fail-closed 机制看起来从未触发。

## 23. 只在 Prompt 要求 Gate，动态扩图仍会漏 Gate

**现象**

完整 Development 中，API Supervisor 在多轮补证据时仍生成缺少 GateRecord 的 Investigation 扩展；如果只靠 Engine 拒绝，会浪费调用并形成重复决策。

**根因**

“已有同类有效节点后继续扩图必须带 Gate”是依赖当前图状态的条件约束。基础 Schema 始终把 `gate_record` 设为可选，Prompt 文字无法保证模型每次遵守。

**排查**

比较首次创建、失败替代和基于成功 Artifact 再扩展三类 Trace，确认只有第三类需要 Gate；不能简单把 Gate 对所有 CREATE_TASK 设为必填。

**修复**

阶段 Schema 在请求发送前拆成 gated/ungated 分支：已有有效 Investigation、Diagnosis、Patch 或创建 Challenge/Rebuttal 时，必须同时携带 GateRecord 和触发 Evidence；首次创建和失败替代禁止伪造 Gate。

**验证**

修复后的真实 `bucketsort` 运行中，多次扩展均携带 Gate；Worker 失败替代路径仍可执行，不再重现早期 Gate 死角。

**面试要点**

用户看到的“API 输出错误”不能只在响应后补救。能由当前状态确定的约束，应在请求的动态 Schema 中提前裁剪输出空间。

## 24. Validation 把基准已有 Ruff 告警误判成补丁语法错误

**现象**

`flatten` 的补丁已通过目标测试和完整回归，却因冻结源原本存在的 Ruff `UP028` 被标记为 `syntax_failure`，触发无效 Patch 重生成。

**根因**

Validation 把 AST 语法检查和仓库风格检查合并为一个静态门。候选工作区的相对路径又无法命中主仓库对冻结源设置的精确 Ruff 豁免。

**排查**

核对补丁前后 Ruff 输出、目标/回归测试和 diff，确认告警位于未由补丁引入的原始行，且代码可被 AST 正常解析。

**修复**

候选补丁的确定性 Validation 使用 syntax-only 静态检查；产品仓库自身仍运行全仓 Ruff。补丁是否正确继续由目标测试、回归测试、受保护路径和 diff 完整性共同判定。

**验证**

单元测试锁定 Validation 以 `run_ruff=False` 调用静态检查；全仓 Ruff 仍独立通过。

**面试要点**

外部仓库不一定符合本项目的 Linter。把风格告警当成补丁语法错误会制造假阴性，应区分语法、增量质量和项目级风格门。

## 25. 闭环报告把 Phase 6 写成 Phase 8，终止原因只有自然语言

**现象**

闭环 runner 的 Manifest 和 Acceptance 一度固定写入 `phase: 8`；任务失败时只有自由文本 `reason`，无法稳定聚合错误码和发生阶段。

**根因**

脚本从一份实验草案沿用了内部编号，没有引用仓库唯一 Phase 0–6 规则；Engine 又把 Supervisor 的自然语言终止理由直接当作机器字段。

**排查**

全仓搜索阶段号并对照 AGENTS 与唯一计划，确认只有闭环 runner 出现 Phase 8；再检查失败结果，发现同类错误无法按 code 聚合。

**修复**

- runner 统一使用 `_PHASE = 6`；
- Engine 保存 `termination_code`、`termination_stage` 和可读 message；
- Result 补充标准化 `failure_class`，Summary 聚合 code、stage 和 failure class；
- 旧 Checkpoint 恢复时采用保守兼容映射。

**验证**

单元测试覆盖预算终止、无进展终止、Supervisor 主动终止和 Validation 成功；Development 门禁拒绝任何框架类终态错误。

**面试要点**

可读日志不能替代机器状态。没有稳定 code 和 stage，跨任务统计会退化为脆弱的字符串匹配。

## 26. Frozen Test 没有自动绑定已通过的 Development 身份

**现象**

早期流程虽然写着“先 Development、再冻结、最后 Test”，但 Test 入口没有验证当前代码、配置、Prompt 和模型路由是否仍与通过门禁的 Development 相同。

**根因**

冻结停留在人工步骤；Git SHA 只能描述已提交基线，无法独立描述当前未提交但确定的运行时代码内容。

**排查**

检查 runner 参数和 Manifest，确认 Test 可以在没有任何 Development 证据的情况下启动。

**修复**

- Development Manifest 保存 Git 基线 SHA、运行时内容 fingerprint、配置 SHA256、Supervisor Prompt SHA256 和脱敏模型路由；
- 运行时 fingerprint 只覆盖 `src/`、`scripts/`、`configs/`、`data/`、`tests/` 与 `pyproject.toml`，避免测试后的中文报告更新改变执行身份；
- 完整 Test 强制传入 `--freeze-manifest`，并读取同目录 Acceptance 验证 Development 已通过全部门禁；
- 任一身份不一致都在加载模型前 fail-closed。

**验证**

单元测试同时覆盖完全匹配和 Prompt 变化；正式 Frozen Test 的 Acceptance 还包含 `frozen_identity_verified` 门禁。

**面试要点**

“我没有改代码”不是可审计证据。冻结协议应该让入口自己证明 Test 与 Development 使用的是同一执行身份。

## 27. 单路由 30 秒超时低于真实长尾延迟

**现象**

完整 Development 后半程多次出现两个账号连续在 30 秒边界超时；同批早期成功响应已经接近 30 秒，路由总延迟还出现过 37～79 秒。这会把仍在正常生成的大 Schema 响应误判为瞬时故障。

**根因**

全局 Supervisor generation timeout 为 120 秒，但单路由 timeout 固定为 30 秒，没有根据真实 P95 长尾校准。任务状态变大后，模型生成合法结构化决定需要更长时间。

**排查**

从 Trace 分离 `supervisor_route_timeout`、单次成功延迟和包含账号切换的总延迟，确认超时集中在固定边界，而非立即返回的额度或鉴权错误。

**修复**

把单路由 timeout 调整为 60 秒，仍受 120 秒全局调用上限约束；保留账号切换和结构化超时事件，不把超时槽位永久标记为额度耗尽。

**验证**

配置测试锁定 60 秒；最终 Development 和 Frozen Test 从 Route Trace 复核超时、成功与账号切换，不用“最终任务失败”反推 API 原因。

**面试要点**

超时不是越小越安全。分层 deadline 要用真实延迟分布校准，并区分单次尝试上限与整次策略调用上限。

## 28. 结构化 Supervisor 请求未显式关闭思考模式

**现象**

剩余免费槽位的一次初始化决策输入约 6.3k Token，却累计输出约 7.0k Token，超过配置中的 2,048 `max_tokens`，并因格式修复产生两次请求。响应延迟也达到约 60 秒。

**根因**

Qwen3.7 是混合思考模型，HTTP 请求没有传 `enable_thinking=false`。Provider 会把推理过程计入输出用量；而本项目只需要严格 JSON Decision，默认思考既增加 Token 和延迟，也提高结构化输出修复概率。

**排查**

对照原始响应 usage、`supervisor_decision_generated.attempts` 和 DashScope 官方 Chat Completions 参数，确认本项目的原生 HTTP payload 缺少该顶层字段。阿里云错误码文档也明确建议：结构化输出时关闭 thinking。

**修复**

- `GenerationConfig` 增加可选 `enable_thinking`；
- `configs/supervisor.yaml` 对结构化 Supervisor 固定为 `false`；
- 原生 HTTP requester 只在配置非空时把参数写到 payload 顶层；
- 本地 Worker 的思考配置不受影响。

**验证**

单元测试截获真实 HTTP Request body 并断言 `enable_thinking=false`。真实 `supervisor_protocol_probe_v3` 一次生成即被 Engine 应用：输入 2,787、输出 335 Token、延迟 4.743 秒；对照缺项探针的输入 6,249、输出 816 Token、延迟 8.958 秒且决定被拒绝，说明参数和 Schema 修复均实际生效。

**面试要点**

Provider 的默认推理模式也是协议的一部分。不能只看 `max_tokens` 就假定账单输出受同一上限约束，尤其在 reasoning Token 独立计量时。

## 29. Snapshot“逻辑上裁剪”但仍发送完整 Artifact 内容

**现象**

修复计划要求明显缩短 Supervisor Snapshot，但真实请求仍携带每个 Artifact 的完整 `failure_output`、Validation `output_tail` 和 Patch `diff`。节点增多后，同一大段工具输出在每轮重复发送。

**根因**

Engine Snapshot 虽然删掉了一些顶层字段，`Blackboard.artifact_summaries()` 却原样包含完整 content；没有专门面向 Supervisor 的传输视图，也没有量化压缩比例。

**排查**

比较 Engine 内部 Snapshot JSON、发给模型的 user message 和阶段化 Schema 大小，确认主要膨胀来自 Artifact bulk content，而不是节点 ID 或预算字段。

**修复**

- 保留 Engine/Checkpoint 的完整状态，只在 API 边界生成 `_supervisor_snapshot_view`；
- Evidence 保留 claim、来源、验证状态和 Trace ID，删除长失败输出；
- Patch 保留语义摘要、文件和 diff hash，删除 diff 正文；
- Validation 保留 exit code、Trace ID、failure class 和通过状态，删除 output tail；
- 决策历史只保留计数、最近 12 个 ID 和上次规范结果；
- 每次调用写入原始字符数、压缩后字符数和 reduction ratio。

**验证**

单元测试用 10k diff 和长测试输出构造 Snapshot，压缩后小于原始四分之一且关键路由语义完整；空 Artifact 的真实初始化探针压缩约 1.2%，符合“此时没有 bulk content”的预期；完整 Development 要求累计压缩比例至少 20%。

**面试要点**

内部事实源与模型上下文不应是同一个序列化对象。Checkpoint 要完整，控制面输入要最小、稳定、可量化。

## 30. 首次创建不需要 Gate，但 Schema 仍把 Gate 暴露为可选字段

**现象**

关闭 thinking 后的真实协议探针能快速生成合法 Worker/mode，却在初始化决定中主动附加 GateRecord。由于此时没有 Artifact，第一次格式修复又把 Task ID 伪装成触发 Artifact，最终被 Engine 拒绝。

**根因**

动态 Schema 已对“必须 Gate”的扩图分支做了约束，却在完全不需要 Gate 的首次创建分支中保留了可选 `gate_record`。模型看到字段存在，就可能出于“更完整”的倾向填写它。

**排查**

探针原始响应第一版为空 trigger，第二版变成 Task ID；对照初始化 Schema，确认不是 Prompt 映射错误，而是输出空间仍包含一个当下无法合法填写的字段。

**修复**

对所有纯 ungated CREATE 分支从 `properties` 中删除 `gate_record`；只有 gated 扩图和 pending Replan 分支才暴露且强制 Gate。Engine 仍保留最终防线。

**验证**

单元测试在初始化状态注入伪 Gate 并断言 Schema 以 additional property 拒绝；真实 `supervisor_protocol_probe_v3` 仅用一次响应生成无伪 Gate 的初始化决定，并由 Engine 返回 `DECISION_APPLIED`。

**面试要点**

动态 Schema 不只要“必填该填的”，还要“隐藏不该填的”。对 LLM 来说，可选字段本身就是一种行动暗示。

## 31. Evidence 已充分后，Supervisor 仍重复创建 Investigation

**现象**

真实运行已经获得源码定位证据和一次受信失败复现，Supervisor 仍继续创建 `InvestigatorAgent`，导致 Token、时间和工具预算被重复消耗。

**原因**

旧实现只检查“是否存在 Evidence”，没有把“源码证据 + 已验证执行 + 失败复现”编码为一个可判定的充分性状态；动态 Schema 因此持续暴露 `INVESTIGATION_TASK`。

**排查**

在 `dev_repair_v2_find_first_evidencegate` 中按状态版本核对 Artifact，确认不是 Worker 没有产出，而是 Schema 在证据已经闭合后仍允许相同动作。

**修复**

新增 `additional_investigation_required()`：只有证据不足或 Reviewer 明确指出新缺口时才允许继续调查。证据充分后，Schema 移除 Investigation；即使模型绕过 Schema，Engine 也以 `EVIDENCE_COLLECTION_ALREADY_SUFFICIENT` 拒绝。

**验证**

单元测试同时覆盖证据充分时关闭入口，以及 Reviewer 给出 `needs_more_evidence` 后重新开放入口；真实运行观察到流程从复现直接进入 Diagnosis。

**面试要点**

“存在证据”和“证据充分”不是同一个布尔值。LLM 工作流需要显式的充分性谓词，否则动态路由会退化成重复调用。

## 32. Engine 契约与 Diagnostician 自身契约发生漂移

**现象**

补充 Evidence 后，Engine 允许新版 Diagnosis 同时读取 Evidence、旧 Hypothesis 和 Review，但 Diagnostician 仍按旧规则要求“第一轮只能读取 Evidence”，最终报成 `MODEL_FORMAT_ERROR` 并进行了无意义重试。

**原因**

同一输入协议分别存在于 Schema、Engine 和 Worker Prompt/代码中，修改其中两处后遗漏了第三处；同时异常分类把确定性的输入契约错误错误包装成模型格式错误。

**排查**

`dev_repair_v2_find_first_evidencegate` 的 Trace 显示模型尚未生成输出，失败就发生在 Worker 输入解析阶段，由此排除真正的 JSON 格式问题。

**修复**

统一 Diagnostician 三种 mode 的矩阵：`control_flow/data_flow` 只接收 Evidence；`challenge` 接收一个 Hypothesis 与 Evidence；`rebuttal` 接收一个 Hypothesis、一个 Challenge 与 Evidence。确定性不匹配统一返回 `AGENT_INPUT_CONTRACT_VIOLATION`，不再原地重试。

**验证**

Agent Contract、动态 Schema、Worker 调度三层测试均覆盖合法数量、非法类型和不可恢复分类。

**面试要点**

多 Agent 系统最容易发生的不是类名错误，而是“多份协议实现逐渐不一致”。契约需要矩阵化并在调度前单点校验。

## 33. 多任务 Gate Schema 在初始化阶段形成不可满足分支

**现象**

支持一次决策扩展多个节点后，初始化 Schema 意外要求 Gate；此时又没有任何可作为 `trigger_artifact_ref` 的 Artifact，模型只好编造 `task:...` 伪引用。

**原因**

Schema 生成器只关注“多节点扩图需要 Gate”，没有考虑初始种子图尚无 Artifact 这一状态不变量，形成了语法可选但语义不可满足的输出空间。

**排查**

`dev_repair_v2_find_first_contractsync` 的第一次 API 响应直接包含伪 trigger；将同一 Prompt 与初始化 Schema 对照后，定位到分支生成规则，而不是模型随机幻觉。

**修复**

初始化且无 Artifact 时只暴露单节点、无 Gate 的 CREATE 分支；只有存在真实触发 Artifact 后才允许多节点 gated expansion。

**验证**

单元测试断言初始化 `maxItems=1` 且不暴露 `gate_record`；后续真实运行能够正常创建首个 Investigation。

**面试要点**

结构化输出并不自动保证语义可满足。动态 Schema 必须由当前状态的不变量生成，而不是只按动作类型拼装。

## 34. Gated 分支污染普通 Review，诱导模型填写伪 Gate

**现象**

某个状态只要存在一种需要 Gate 的扩图动作，旧 Schema 就把所有可创建节点都放进 gated variant，普通 `evidence_review` 因而也携带无意义 Gate。

**原因**

Schema 按“当前状态是否存在 gated node type”分组，而不是按“每个 node type 是否 gated”分组，导致互斥动作的约束被合并。

**排查**

`dev_repair_v2_find_first_initialgate` 中的真实响应表明普通 Review 主动填写了 Gate；检查 `oneOf` 分支后确认这是 Schema 暗示，而非 Prompt 未强调。

**修复**

将 gated 与 ungated node type 拆成不同 CREATE variants，普通节点分支完全删除 `gate_record`；有真实 Gate 需求的分支继续强制该字段。

**验证**

Schema 测试遍历所有 CREATE variants，确保 gated 类型只能出现在带 Gate 的分支，普通类型只能出现在无 Gate 分支。

**面试要点**

对 LLM 而言，“字段存在但可选”仍是一种行为暗示。最小 Schema 的目标不仅是拒绝非法输出，也要减少模型产生非法输出的机会。

## 35. Evidence Review 的输入输出引用没有绑定

**现象**

Supervisor 创建 `evidence_review` 时，输出目标被填写成普通路径或不存在的引用，Worker 失败后又原样重试。

**原因**

旧 Schema 只约束 target 是字符串，没有限定它必须来自当前输入 Evidence；Worker 输出校验也没有再次验证引用绑定。

**排查**

在 `dev_repair_v2_find_first_initialgate` 中对比节点的 `input_artifact_refs`、Reviewer target 与 Blackboard，发现引用在进入 Worker 前已经失真。

**修复**

动态 Schema 将 review target 枚举绑定到输入 Evidence；Worker Artifact 校验再次检查 target 确实属于输入集合，形成调度前后双重防线。

**验证**

测试覆盖合法 Evidence ref、路径字符串和越权 Artifact ref，后两者均被确定性拒绝且不消耗模型重试。

**面试要点**

Artifact Ref 是数据依赖，不是自由文本。引用完整性应像数据库外键一样处理。

## 36. 失败复现门只在 Engine，导致无效 API 决策

**现象**

源码 Evidence 已存在但失败尚未复现时，Schema 仍允许进入 Diagnosis。Supervisor 先生成一次看似合法的 Diagnosis 决策，再被 Engine 以 `FAILURE_REPRODUCTION_REQUIRED` 拒绝。

**原因**

Engine 有正确的最终防线，但动态 Schema 没有读取 `requires_failure_reproduction` 和已确认复现引用，约束没有前移。

**排查**

`dev_repair_v2_find_first_reviewgate` 的首个拒绝发生在 Supervisor API 已经返回之后，说明正确性没被绕过，但额度被无意义消耗。

**修复**

复现未确认时，Schema 移除 Diagnosis、Review 和离开 Investigation 的阶段转换；Engine 门保持不变，防止手工构造或旧 Checkpoint 绕过。

**验证**

单元测试检查 pending/confirmed 两种快照下可用动作集合；真实流程在复现成功后才进入 Diagnosis。

**面试要点**

安全约束要有最终防线，效率约束要尽量前移。Schema 减少错误请求，Engine 保证错误请求也不能改变状态。

## 37. Reviewer 输出自相矛盾：`supported` 与未解决不确定性并存

**现象**

本地 Reviewer 多次返回 `supported`，同时又保留 `remaining_uncertainty`，违反“接受结论必须完成反例和验证检查”的语义。

**原因**

生成模型倾向于在 verdict 上给出肯定答案，同时在解释中保留谨慎措辞；仅靠 Prompt 无法保证跨字段一致性。

**排查**

`dev_repair_v2_find_first_reviewgate` 连续出现相同组合，Worker Schema 校验均拒绝，证明问题不是偶发 JSON 损坏，而是 verdict 与质量字段的联合约束缺失。

**修复**

Reviewer 在入库前执行确定性归一化：只要失败机制、替代原因、反例、验证步骤或剩余不确定性未闭合，就把表面上的肯定 verdict 降级为 `needs_more_evidence`，并写入 `review_verdict_normalized` Trace。Artifact 校验同时要求可接受 Review 至少包含一个验证步骤。

**验证**

测试覆盖矛盾输出的降级、完整输出的保留和 Trace 事件；归一化只会收紧结论，不会把否定结果提升为成功。

**面试要点**

LLM 的自然语言“谨慎”可能与结构化 verdict 冲突。确定性后处理必须 fail-closed，只允许降级，不允许凭空升级结论。

## 38. 更换 Node ID 绕过同一逻辑任务的重试上限

**现象**

某个 Reviewer 节点达到重试上限后，Supervisor 创建一个新 Node ID，但 Agent、mode 和输入完全相同，相当于无限重置重试预算。

**原因**

旧预算只绑定物理 Node ID，没有定义逻辑任务身份；动态扩图因此可以把 retry 伪装成新任务。

**排查**

按 Trace 比较失败节点和新节点的 `(node_type, agent, mode, input_artifact_refs)`，发现只有 ID 变化，工作内容完全一致。

**修复**

Engine 新增逻辑任务指纹。若相同指纹已有节点在达到重试上限后失败或超时，新建节点以 `LOGICAL_TASK_RETRY_EXHAUSTED` 拒绝，只允许重规划或终止；输入 Evidence 或 mode 真正变化时则视为新任务。

**验证**

测试覆盖“只换 ID 被拒绝”和“新增 Evidence 后允许重建”两条路径。

**面试要点**

动态 DAG 的预算必须绑定语义身份，而不是随机生成的节点标识，否则任何节点上限都可以被扩图绕过。

## 39. 全路由额度耗尽被当作可恢复错误反复进入策略循环

**现象**

两个账号的三个模型共六个路由均返回阿里云 403 免费额度耗尽后，旧 Engine 仍把 `SUPERVISOR_ROUTES_EXHAUSTED` 当作可恢复错误，继续进行 24 次语义决策，最终才以 `SUPERVISOR_CALL_BUDGET_EXHAUSTED` 终止。

**原因**

“单路由失败可切换”与“路由池已经全部耗尽”共用了同一个恢复策略。Router 已经知道没有候选路由，Engine 却丢失了这个终态语义。

**排查**

`dev_repair_v2_find_first_retrybudget` 显示只有首轮发生六次真实 HTTP 请求，后续策略循环没有任何新的可用路由；表面终止码因此掩盖了真正原因。

**修复**

将 `SUPERVISOR_ROUTES_EXHAUSTED` 标记为不可恢复，首次路由池耗尽就 fail-closed；报告将其分类为 `provider_quota_exhausted`，而不是笼统的 `orchestration_failure`。单个账号/模型失败仍由 Router 在同一次调用内按既定顺序切换。

**验证**

`dev_repair_v2_routes_fail_closed` 先证明一次策略调用后立即终止；分类补丁后的 `dev_repair_v2_routes_classified` 再次真实请求六个路由，并在实际结果中记录 `provider_quota_exhausted`。六个槽位的 Trace 顺序完整，Workspace Cleanup Rate 为 100%，原始源码摘要前后一致。对应单元测试验证不会进入 24 次策略循环。

**面试要点**

恢复策略必须区分“还有候选项”和“候选集合为空”。后者继续 retry 只会浪费预算并污染根因统计。

## 40. 单任务协议门通过不等于业务修复成功

**现象**

额度耗尽诊断运行的 `acceptance.json` 显示 `passed: true`，但任务结果明确是 0/1 solved，容易被误读为系统成功。

**原因**

单任务/针对性运行的 acceptance 只验证结果文件、Trace、源码完整性、预算失败关闭和工作区清理等“运行协议”；只有完整 5 任务 Development 才启用 `4/5`、无框架终止、Supervisor 路由成功等冻结门。

**排查**

交叉核对 `acceptance.json`、`summary.json` 和任务 `result.json`，确认三个文件描述的是不同层级的结论，并非统计矛盾。

**修复**

报告与文档明确区分 Protocol Acceptance、Business Result 和 Freeze Eligibility；完整 Development 强制提供与当前运行时指纹一致的预冻结检查记录，Frozen Test 又强制绑定已通过 Development 的 commit、配置、Prompt 和模型路由身份。

**验证**

测试证明针对性运行不能生成可供 Frozen Test 使用的冻结身份；当前额度耗尽运行仅作为 fail-closed 机制证据，不计作任务成功，也不满足冻结资格。

**面试要点**

评测系统至少有三种“通过”：程序完整运行、任务修复成功、实验具备发布资格。把三者混成一个布尔值会制造最危险的指标幻觉。

## 41. 独立审计器与 Runner 读取了不同的任务 Suite

**现象**

最终 Runner 默认使用 `phase6_suite.json` 的 5 个 Development 和 10 个 Frozen Test，独立审计器却硬编码读取 `closed_loop_suite.json` 的 20 个任务。即使两轮正式运行完全正确，最终审计也会按错误的集合和顺序判失败。

**原因**

Runner 已把 Suite 放进 `configs/closed_loop_evaluation.yaml`，但审计脚本仍保留早期实验清单的硬编码路径；原有审计单元测试只覆盖两个局部 helper，没有执行 Suite 解析。

**排查**

逐项对照 Runner 配置、两份 Suite JSON 和审计入口，确认前者是 `5+10` 且互斥，后者是单一 20 任务列表，不具备 Development 划分。

**修复**

审计器新增 `--config/--suite`，默认从与 Runner 相同的 `protocol.suite` 解析任务集；审计门同时核对 Suite ID、数据集、上游 commit、seed、Development/Test 清单及顺序。

**验证**

回归测试锁定默认审计 Suite 为 5 个 Development + 10 个 Test 且两者无交集；`preflight_closed_loop_dev_v2` 和 `preflight_closed_loop_test_v2` 的 dry-run 分别生成精确的 5/10 待执行清单。

**面试要点**

独立审计不等于另写一套配置。执行器和审计器必须共享协议事实源，但审计器要独立重算结果，避免“共享结论”与“共享输入”混淆。

## 42. 空 Frozen Gate 集合被 `all()` 错误判为通过

**现象**

独立审计旧代码直接执行 `all(freeze_gates.values())`。当 `freeze_verification` 或 `gates` 完全缺失时，Python 对空序列返回 `True`，手工拼装或损坏的 Test Manifest 反而能通过冻结门。

**原因**

审计器只验证“现有值是否全真”，没有验证“必需 Gate 是否完整存在”；同时只相信 Development Manifest 中的 `pre_freeze_verification.passed`，没有复核记录文件哈希和 pytest、Ruff、compileall、diff 四类检查。

**排查**

按 fail-closed 原则枚举缺字段、空字典、路径解绑和记录被篡改四种输入，确认空字典是最直接的绕过路径。

**修复**

- 冻结 Gate 必须与九个预定义键精确相等且全部为真；
- Test Manifest 必须绑定传入的 Development manifest/acceptance 绝对路径及各自 SHA256，防止 Test 后替换 Development 证据；
- Runner 在完整 Development 前直接验证预冻结记录与运行时指纹一致，并含退出码为 0 的 pytest、Ruff、compileall、`git diff --check`；独立审计再复核记录文件 SHA256 和同一组精确 Gate；
- 两份 Manifest 都必须声明 `execution_mode=runtime` 和 `final_evaluation_executed=true`，dry-run 不能参与正式审计。

**验证**

新增测试覆盖完整 Gate 通过、空 Gate 失败、记录缺少任一检查失败、记录哈希匹配和 Development 文件被替换；完整 5+10 临时证据链由独立审计端到端重算并通过。

**面试要点**

安全审计中“没有失败项”不等于“必需项全部存在”。所有 `all()`/`any()` 都要考虑空集合语义，关键清单应同时校验键集合和值。

## 43. `run_tests` 基础设施失败可能被保存成 Evidence

**现象**

`failure_reproduction` 已能把 `TEST_FAILED` 正确解释为成功复现，但完成性审计发现：如果 `run_tests` 返回 `RUN_TESTS_ERROR` 等工具异常，而模型仍自报 success，旧代码只会得到 `reproduction.succeeded=false`，仍可能继续构造 Evidence。

**原因**

复现解析只保留退出码和输出，没有把“测试按预期失败”“测试正常通过”“工具根本没有可靠执行”三种结果分开；最终分支又部分依赖模型的 action status。

**排查**

构造“pytest 无法启动但模型声称完成”的 Worker 边界用例，确认异常来自工具执行而非被测程序，不应进入 Evidence Store。

**修复**

确定性解析保留内部 `tool_error_code` 与 timeout 状态：只有 `TEST_FAILED + 非零退出 + 未超时` 能生成成功复现 Evidence；测试正常通过返回 `BUSINESS_EVIDENCE_INSUFFICIENT`；工具错误或超时返回 `TOOL_EXECUTION_ERROR`。内部分类字段在 Artifact 校验前移除，不污染公开 Evidence Schema。

**验证**

新增 `test_tool_failure_is_worker_failure`，即使模型返回 success，`RUN_TESTS_ERROR` 仍被确定性归类为 Worker 工具失败；原有 IndexError 复现测试继续通过。

**面试要点**

被测程序失败与测试基础设施失败共享非零退出码，不能只看 exit code。可靠评测需要同时检查命令是否执行、错误码类别、超时状态和失败输出来源。

## 44. 汇总、单任务结果、Trace 和原始响应没有交叉校验

**现象**

独立审计初版确认 `result.json`、Trace 和 Checkpoint 文件存在，也会重新聚合 `task_results.json`，但没有确认批量条目与对应 `result.json` 内容相同；Token 统计、重试数、契约拒绝数、Snapshot 压缩率和 Validation 摘要仍可能在某一层被修改后保持“自洽”。

**原因**

审计停留在“文件存在 + 汇总可重算”，没有沿证据链从原始模型响应、Trace、Artifact 和当前源仓库逐层反算到 Task Result。只要同时修改汇总和批量结果，旧门禁无法发现漂移。

**排查**

在完整 5+10 临时证据链中分别篡改单任务 Token、批量条目和冻结文件，观察哪些现有 Gate 仍为真；由此确定需要增加逐层等值校验，而不是再增加一个总 `passed` 字段。

**修复**

- `task_results.json` 的每个条目必须与固定任务目录中的 `result.json` 完全一致；
- result、Trace、Checkpoint 必须是对应运行根目录下的精确路径，符号链接和外部路径均拒绝；
- 从 Trace 重算路由、Retry、契约拒绝、节点重建、格式恢复和 Snapshot 指标；
- 从原始 Supervisor/Worker 响应重算模型调用、Token、按模型统计和 API 成本，并与 Trace 中的路由/工具调用合并核对 Usage；
- 从 Artifact 重算 Validation 与闭环机制指标；
- 对原始任务仓库重新计算 tree digest，并检查候选工作区目录实际为空。

**验证**

完整 5+10 临时证据链可以通过所有独立门；在同步修改单任务文件、批量文件和 Summary 后篡改 Token，普通自洽门仍为真，但 `raw_usage_evidence_valid` 会失败并使总审计失败。

**面试要点**

可审计性不是保存很多 JSON，而是让高层指标能够由更低层、较难伪造的证据重新推导。真正的独立审计应验证证据之间的函数关系。

## 45. Supervisor 格式恢复反复命中同一路由

**现象**

Supervisor 的 JSON/Schema 恢复虽然会重试，但模型路由器每次仍从首选账号和模型开始；若该路由持续输出错误 Worker、mode 或不符合阶段 Schema 的结构，格式预算可能全部消耗在同一路由，后续五个槽位根本没有机会纠错。

**原因**

原先只把“额度耗尽”记为永久路由状态。结构化输出失败发生在通用 `ModelAdapter` 校验层，路由器不知道刚才哪条已成功返回 HTTP、但业务结构不可用；Supervisor 的阶段语义校验失败也没有回传路由拒绝信号。因此，API 成功与决策可用性之间存在状态断层。

**排查**

构造账号 1 返回非法 Schema、账号 2 返回合法 Schema 的双路由请求，记录实际调用顺序和 Trace。旧流程会再次请求账号 1；这说明问题不在 Prompt 是否写了约束，而在恢复策略没有把校验结果反馈给路由层。

**修复**

为模型适配器增加结构化恢复生命周期：每次 Supervisor 决策开始时清空临时拒绝集合；JSON Schema 或阶段语义校验失败后，把本次 `model_id + api_key_env` 标记为“仅本决策暂时拒绝”，记录 `supervisor_route_format_rejected`，下一次格式尝试转向后续槽位。该状态不写入额度耗尽集合和 Checkpoint，所以下一次独立决策仍按既定优先级从首选路由开始；所有可用路由都返回非法结构时以 `STRUCTURED_OUTPUT_ERROR` 失败关闭。

**验证**

回归测试确认调用顺序为账号 1 的 max → 账号 2 的 max，账号 1 不进入永久 exhausted 状态；下一次 Supervisor 决策重新从账号 1 开始。现有 Supervisor 测试中的轻量测试适配器不实现该可选 hook 时仍保持兼容。

**面试要点**

“API 请求成功”只说明传输和供应商服务正常，不代表路由决策有效。多模型容错既要处理 429/额度/超时，也要让下游 Schema 与语义校验结果反向影响同一决策内的路由选择。

## 46. 未提交运行时代码可能被错误绑定到 HEAD commit

**现象**

冻结 Manifest 同时记录 `repository_commit` 和工作区指纹，但旧门禁只比较指纹、配置、Prompt 与路由。若正式 Development 在 dirty tree 上运行，Test 只要沿用同一 dirty tree 仍可能通过；Manifest 中的 commit 却不包含这些实际执行代码，无法由仓库复现。

**原因**

`git diff --check` 只检查空白错误，不代表工作区干净；独立审计也没有把 `repository_commit` 放进 Development/Test 身份等值字段。设计上记录了 commit，执行上却没有真正冻结 commit。

**排查**

在当前分支保留运行时代码改动，确认 pytest、Ruff、compileall、`git diff --check` 全部通过，而 `git status --short -- pyproject.toml configs data scripts src tests` 仍有输出。由此证明原六项 Gate 可以在不可复现的 dirty tree 上全部为真。

**修复**

- 新增统一入口 `scripts/run_phase6_pre_freeze_checks.py`，顺序执行四类代码检查并检查运行时范围是否与 HEAD 一致；
- 完整 Development 在加载模型和 Key 前同时验证记录中的 clean 结果与当前实际 clean 状态；
- Frozen Test 增加 commit 精确匹配和当前运行时树 clean 两个 Gate；
- 独立审计把 `repository_commit` 纳入 Development/Test 身份等值与当前 HEAD 复核。

**验证**

当时 239 项测试、Ruff、compileall 和 diff 均通过，但由于代码尚未提交，预冻结记录如实为 `passed=false`，仅 `preflight_declared_passed` 与 `preflight_runtime_tree_clean` 为假。提交后必须重新运行统一入口，不能手改记录为通过。

**面试要点**

“保存了 commit SHA”不等于“实验来自这个 commit”。可复现实验必须同时保证代码树干净、运行时输入被纳入作用域，并在 Development、Test 和独立审计三处验证同一身份。

## 47. 多候选 Hypothesis 选出单一根因后被 Review Gate 误拒

**现象**

在首次完整 Development 中，`find_first_in_sorted` 和 GCD 都完成了失败复现、两个 Hypothesis 以及 `hypothesis_comparison` Review；Review 覆盖完整候选集并推荐其中一个根因。Supervisor 随后仅接受被推荐的 Hypothesis，Engine 却返回 `ROOT_CAUSE_RECOMMENDATION_REQUIRED`，后续相同语义决策又被判定为 `NO_PROGRESS_LOOP`。两题均失败后，当次批次已不可能达到 4/5 冻结门。

**原因**

Engine 用“最终接受的 Hypothesis 数量”决定 Review 类型：只接受一个就强制要求 `root_cause_recommendation`，接受多个才认可 `hypothesis_comparison`。这混淆了“待比较的候选池”和“比较后的最终选择”：两个候选经完整比较选出一个，本来就是 comparison 的正常结果，不应再无条件增加一次 Review。

**排查**

- 从两题 Trace 对齐 `supervisor_decision_generated`、`hypothesis_resolution_updated`和 `supervisor_decision_rejected`；
- 确认 comparison Review 的 `target_artifact_refs` 覆盖两个候选，`target_artifact_ref` 指向被推荐者；
- 确认失败发生在 Worker 成功、Review 产物已入库之后，不是 API Schema、本地模型或 Evidence 引用故障；
- 首版修复又过度限制为“多候选只认 comparison”，针对性运行随即证明“补证后为最终单候选新建 recommendation”也是合法路径。

**修复**

确定性 Gate 同时允许两种有证据的收敛方式：

1. 合格 `hypothesis_comparison` 覆盖完整候选池，可接受其推荐的单一候选；
2. 合格 `root_cause_recommendation` 直接覆盖最终接受的单一候选。

接受多个 Hypothesis 时仍必须有覆盖完整候选集的 comparison；Review verdict、独立验证字段、Evidence 覆盖和最小 Evidence Gate 均未放宽。

**验证**

回归测试分别覆盖“comparison 选出一个”和“多候选后的单候选 recommendation”。真实针对性运行 `dev_repair_v2_review_selection_gatefix_gcd` 最终 1/1 solved，Hypothesis、Review、Patch Binding、目标测试、完整回归和 Validation 全部通过，终态为 `VALIDATION_PASSED`，Workspace Cleanup Rate 为 100%，Source Integrity Violations 为 0。

**面试要点**

多 Agent 状态机最难的问题常不是“缺一个规则”，而是规则绑错了语义对象。候选池大小决定需要哪种比较证据，最终接受集合决定后续 Patch 绑定；两者不能用同一个数量条件代替。

## 48. Development Pilot 时间线

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
| repair-v2 evidencegate | 验证 Evidence 充分性门 | 暴露 Diagnostician 三层契约漂移 |
| repair-v2 contractsync | 同步 Diagnostician 契约后重跑 | 暴露初始化多任务 Gate 不可满足 |
| repair-v2 initialgate | 修复初始化 Gate 后重跑 | 暴露普通 Review 被 gated variant 污染 |
| repair-v2 reviewgate | 前移复现门并验证 Review | 暴露矛盾 verdict 与换 Node ID 绕过重试 |
| repair-v2 retrybudget | 增加逻辑任务重试指纹后重跑 | 六个 Supervisor API 路由真实额度全部耗尽 |
| repair-v2 routes-fail-closed | 验证全路由耗尽终态 | 一次策略调用、六次路由尝试后立即失败关闭 |
| repair-v2 routes-classified | 验证终态报告分类 | 实际结果记录 `provider_quota_exhausted`，未再退化为通用编排故障 |
| API route recheck 15:38 | 判断能否启动正式 Development | 六槽位仍全部返回 `AllocationQuota.FreeTierOnly`，保持外部阻塞 |
| preflight closed-loop v2 | 离线核对完整 Development/Test 入口 | 暴露独立审计器使用错误 Suite 和空 Gate fail-open |
| `dev_repair_v2_full` 首次正式尝试 | 最终路由与预冻结通过后启动 5 题 Development；前两题均失败后中止 | comparison 选出单根因被误要求 recommendation，触发 `NO_PROGRESS_LOOP` |
| comparison-gatefix GCD | 验证首版 Gate 修复 | 暴露多候选补证后的单候选 recommendation 也必须允许 |
| review-selection-gatefix GCD | 验证完整 Review 选择规则 | 1/1 solved，`VALIDATION_PASSED`，进入重新预冻结 |

所有 pilot 都只使用 development split。正式 test split 只在框架和评测协议冻结后运行一次；失败的 pilot 不进入正式成功率表。

闭环异常修复又保留了独立运行 ID：`smoke_repair_v2*` 用于暴露 Replan 和 Review 事务问题，`dev_repair_v2_gcd`、`dev_repair_v2_flatten`、`dev_repair_v2_bucketsort`、`dev_repair_v2_parenthesization` 用于五类针对性验证，`dev_repair_v2` 与 `dev_repair_v2_gatefix` 是完整 Development 过程中被新门禁否决的历史证据。它们不会与最终通过冻结门的运行合并统计。

## 49. 总结

这些问题共同说明，RepoPilot-MAS 的难点不在“多写几个角色 Prompt”，而在边界协议：

- LLM 决策需要 Schema 和确定性状态机共同约束；
- 重试、扩图、阶段迁移和引用必须有精确语义；
- Worker 证据不能被自然语言自报替代；
- GPU、API 配额和异步 Runtime 都需要显式生命周期；
- Trace 必须可去重、可追溯，评测门必须区分任务失败与实验无效；
- 开发集、测试集和第三方冻结源必须在代码层隔离。

面试时可以从“错误 Worker/mode → Gate 死角 → 决策历史缺失 → GPU OOM → 评测口径错误”这条链说明项目如何从能跑的 Demo 演化为可审计系统。
