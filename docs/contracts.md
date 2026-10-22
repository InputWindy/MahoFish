# 汇点与设计约束

本文件冻结 agent 的全部数据汇点。**任何"小需求"都必须先在这里找到落点**；找不到落点或触碰禁止项，才允许改协议，且必须走 §4 的变更流程。

```
                 ┌───────────── Agent 循环 ─────────────┐
   Memory ──┐    │  读 Message → 问 LLM → 解析 Decision  │    ┌── Tools
            ├──▶ │  → Policy 校验 → 调 Tool → Observation │ ───┘
   Facts ───┘    │  → 写 Turn                             │
                 └───────────────────────────────────────┘
     汇点：Message / Reply / Decision / Observation / Turn / Fact / Usage
```

---

## 1. 汇点清单

### C1 · Message（模型输入汇点）

| 项 | 内容 |
|---|---|
| 定义 | `Message(role: Role, content: str)`，位于 `fish/contracts.py` |
| 允许扩展 | 加**有默认值**的可选字段（如 `attachments: tuple[Attachment, ...] = ()`） |
| 禁止 | ① 用第三方 SDK 类型替换；② 把工具结果拼成非 Message 结构直接送模型；③ 由实现模块（llm/tools）定义 Message |
| 演进闸门 | 只有出现**真实多模态调用方**（有视觉模型 + 有截图需求）才允许加 `attachments` 或 `parts` |
| 判定 | 需要"模型看到图片" → 走闸门；需要"模型多一段文本" → 不是汇点变更，改 prompt 即可 |

### C2 · Reply（模型输出汇点）

| 项 | 内容 |
|---|---|
| 定义 | `Reply(text: str, tokens: int)` |
| 允许扩展 | 加可选统计字段（如 `finish_reason`） |
| 禁止 | 在 Reply 里做结构化解析；解析只允许出现在 `parse_decision` |
| 演进闸门 | 换用原生 function calling 时才允许加 `tool_calls` 字段 |

### C3 · Decision / Action（决策协议汇点）

| 项 | 内容 |
|---|---|
| 定义 | 单 JSON 对象：`{"thought", "tool", "args"}` 或 `{"thought", "final"}`；由 `parse_decision` 唯一解析 |
| 允许扩展 | 可选字段（如 `"confidence"`），解析方必须容忍未知字段 |
| 禁止 | ① 多动作并行（一次回多个 tool）；② 在工具/记忆里改协议；③ 解析逻辑出现在 `parse_decision` 之外 |
| 演进闸门 | 并行动作、原生 tool calling 属于协议升级：必须同步 `PROTOCOL_HINT` + `parse_decision` + selftest 用例 |

### C4 · Observation（工具返回汇点）

| 项 | 内容 |
|---|---|
| 定义 | `Observation(ok: bool, content: str, error: str \| None)` |
| 规则 | ① 工具**永不抛异常**（除底层进程死亡）；② 单条内容上限 6000 字符；③ 错误必须可行动（含可选值/建议） |
| 允许扩展 | 加可选 `media` 字段走 C1 闸门；加可选 `meta: dict` 做诊断 |
| 禁止 | 返回原始 HTML/dump；在工具内重试；返回自定义对象 |

### C5 · ToolSpec（工具元数据汇点）

| 项 | 内容 |
|---|---|
| 定义 | `ToolSpec(name, description, parameters, dangerous, idempotent)` |
| 规则 | 策略只能依据 `dangerous` 判定，**不得按工具名硬编码**（当前 `DANGEROUS_TOOLS` 是唯一例外，需逐步迁到 spec） |
| 允许扩展 | 加 `cost_hint`、`required_permissions` 等元数据字段 |
| 禁止 | 在循环里判断具体工具名 |

### C6 · ToolRegistry（工具装配汇点）

| 项 | 内容 |
|---|---|
| 定义 | `specs() -> list[ToolSpec]` / `get(name) -> Tool \| None` |
| 允许扩展 | 多注册中心合并、按任务过滤工具集（返回子集） |
| 禁止 | 循环 `import` 具体 registry；工具注册表依赖 LLM |

### C7 · Turn（记忆写入汇点）

| 项 | 内容 |
|---|---|
| 定义 | `Turn(step: int, decision: Decision \| None, observation: Observation \| None)`，不可变 |
| 规则 | 写入不做压缩、不做摘要；原文进、原文留 |
| 允许扩展 | 加 `usage`、`timestamp` 等元数据 |
| 禁止 | 在 Turn 里存渲染后的 prompt 文本；写入时裁剪内容（裁剪属于读出汇点 C8） |

### C8 · Memory.view（记忆读出汇点）

| 项 | 内容 |
|---|---|
| 定义 | `view(budget_tokens: int) -> list[Message]`；`compact(summarizer: Summarizer) -> CompactionResult(folded, tokens)` |
| 组装顺序 | `FACTS`（≤15% 预算，永不裁剪） → `SUMMARY`（≤25%） → `RECENT TURNS`（剩余） |
| 规则 | ① 只返回 `Message`；② **view 必须是纯函数**（不发网络请求、不调模型）；③ 压缩由循环显式调用 `compact()` 触发，`Summarizer` 以回调注入，`Memory` 不 import LLM；④ 预算不足时先从 recent 尾部砍 |
| scope | facts 检索所需的域在**构造时注入**（每个任务一个 Memory 实例），不通过 `view` 参数传入 |
| 允许扩展 | 换压缩策略（滑窗/滚动摘要/检索）不改接口；新增 fact 检索维度不改接口；`set_goal()` 为可选方法（默认 no-op），用于同一 Memory 跨轮复用 |
| 禁止 | 返回非 Message；在 view 里发网络请求；把权限/预算判断塞进 memory；在 add 里压缩 |
| 实现状态 | `WindowMemory` 已实现 RECENT + 折叠台账 + `compact()`；FACTS 待实现（协议先冻结） |

### C9 · Fact（持久化汇点）

| 项 | 内容 |
|---|---|
| 定义 | `Fact(id, scope, kind, text, metadata, score, hits, created_at, updated_at)` |
| 规则 | ① `id = f"{scope}:{kind}:{key}"`，天然去重；② 只存**可复用可验证**的事实（选择器、字段映射、站点特性），不存一次性数据；③ 淘汰靠 `score` 反馈，**不用 LLM 决策 ADD/UPDATE/DELETE** |
| 允许扩展 | 加 `ttl`、`source_task`、向量检索字段（不改现有字段语义） |
| 禁止 | 存视频标题/正文等一次性内容；在 store 里调模型；把 facts 写进 Turn |
| 实现状态 | v0 已实现：`fish/store.py` 的 `Fact` + `JsonFactStore`；自动抽取与 `remember_fact` 工具待做（§6 第 3 步） |

### C10 · Policy（边界汇点）

| 项 | 内容 |
|---|---|
| 定义 | `max_tokens` / `check(spec, args) -> Verdict` / `spend(usage)` / `exhausted() -> (bool, str)` |
| 规则 | 预算与权限**只在这里**；循环不得自行计数 |
| 允许扩展 | 交互式审批（ask）、沙箱、QPS 限速——都是新 Policy 实现 |
| 禁止 | 在工具内做权限判断；在循环里 `if tool == "click"` |

### C11 · Usage / RunResult（可观测汇点）

| 项 | 内容 |
|---|---|
| 定义 | `Usage(steps, tokens, seconds)`；`RunResult(answer, steps, usage, stopped_reason)` |
| 规则 | `stopped_reason` 必须是稳定枚举：`final`、`step budget exhausted`、`token budget exhausted`、`time budget exhausted`、`llm error: <detail>`、`policy error: <detail>`。日志/评测按字符串前缀匹配 |
| 失败来源 | `Usage.estimated_tokens` 记录由估算（非 provider 上报）计入的 token，用于判断预算数字的可信度 |
| 故障可见 | `RunResult.faults: tuple[Fault(step, source, message), ...]`：**每个被兜底的故障都必须记录**，开发期同时立即打印 `FAULT`，禁止静默吞掉 |
| 允许扩展 | 加 `cost`、`trace_id`、`facts_used` |
| 禁止 | 自由文本 reason；在 RunResult 里塞对话历史 |

### C12 · 循环（行为汇点）

| 项 | 内容 |
|---|---|
| 定义 | `Agent.run() -> RunResult`，循环只依赖 C1–C11 |
| 规则 | 终止条件只来自 `Policy.exhausted()` 与 `final`（首轮检查 `exhausted()` 抛异常时按 `policy error` 停止）；重复调用拦截在循环（`tool+args` 归一化后相同则不执行）；每步允许调用 `memory.compact(summarizer)` 触发折叠，摘要逻辑本身不得写在循环里 |
| 失败策略（不得 fatal） | LLM 异常 → 重试 `max_llm_retries` 次，仍失败返回 `stopped_reason="llm error: ..."`；工具异常 → `Observation.failure("tool crashed: ...")`；`Policy.check` 异常 → deny（fail-closed）；`Policy.exhausted/spend` 异常 → 按 `policy error` 停止或忽略；`Memory.view/add/compact` 异常 → 降级并继续 |
| 允许扩展 | 换范式（Plan-Execute / Reflexion）= 新循环实现，接口不变；可注入 `observer: TurnObserver`，每轮写入后回调 |
| observer 约束 | core 只调用 `observer.on_turn(turn)`，不得读取 turn 内容做判断；observer 异常必须吞掉，不得影响循环结果 |
| 禁止 | 循环内 import 具体工具/registry/后端；循环内实现摘要逻辑；循环内写盘；**静默吞掉可恢复故障（必须走 `Fault` 记录）** |

### C13 · 配置（外部输入汇点）

| 项 | 内容 |
|---|---|
| 定义 | 环境变量统一 `FISH_*` 前缀（`FISH_API_KEY` / `FISH_BASE_URL` / `FISH_MODEL` / `FISH_CODEMAKER_URL`） |
| 规则 | 真实环境变量优先于 `.env`；密钥只从 env 读 |
| 禁止 | 新增无前缀变量；把 key 写入代码或日志 |

### C14 · 落盘（持久化路径汇点）

| 项 | 内容 |
|---|---|
| 定义 | 运行时数据统一在 `.fish/`（`facts.json`、`sessions/*.jsonl`、`reports/`、`*_state.json`），已 gitignore |
| 规则 | 原子写（临时文件 + `os.replace`）；读失败降级为空，不阻塞主流程 |
| 禁止 | 往仓库其他目录写运行时数据；把 `.fish/` 提交进版本库 |

### C15 · 域插件（Domain Pack）

| 项 | 内容 |
|---|---|
| 定义 | 按领域封装的扩展模块，放 `fish/domains/<domain>.py`，实现 `DomainPack`（`fish/domain.py`） |
| 接口 | `capabilities: DomainCapabilities` + `registry()`；可选 `hints()` / `observers()` / `scope_for(goal)` / `close()` |
| 注册 | 内置映射（`none` / `browser`）；第三方域走 Python entry points（group `fish.domains`），不扫目录 |
| 生命周期 | 组合根装配 → 校验能力声明 → 运行 → `close()`（必须幂等） |
| 规则 | ① core **不得 import** 任何 `fish.domains.*`；② 由组合根（`main.py`）装配并注入；③ 插件失败必须降级，不得中断任务；④ **能力声明必须与实现一致**，装配期由 `validate()` 拒绝不一致（照 MCP 能力协商，早失败）；⑤ 运行期工具异常仍走 `Observation.failure`，域内部异常记录后降级 |
| 基线 | 能力声明与失败隔离借鉴 [MCP 生命周期规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)；注册方式用 [Python entry points](https://packaging.python.org/guides/creating-and-discovering-plugins/) |
| 允许扩展 | 新增域（browser / desktop / im）、同域多插件（不同抽取策略） |
| 禁止 | 在插件里改循环行为、改协议、绕过 Policy；把域知识（站点名、选择器规则）写进 core |
| 演进闸门 | 只有"同一份逻辑需要在 2 个以上域复用"时，才允许把插件内的逻辑提升为通用模块 |

### C16 · 通知工具（Tool 的专用子类）

| 项 | 内容 |
|---|---|
| 分层 | `Tool`（契约） → `NotifyTool`（通知语义样板，`fish/notify.py`） → `ConsoleNotifyTool` / `FileNotifyTool` / `PopoNotifyTool`（只实现 `send`） |
| 命名 | `spec.name = f"notify_{channel}"`，`channel ∈ {console, file, popo, ...}` |
| 参数 | 固定 `title: str`（必填）、`text: str`（必填）、`to: str`（可选） |
| 语义 | `dangerous=True`（外部副作用）、`idempotent=False`（重复调用会重复发送）；`text` 为空 → `Observation.failure`；超过 4000 字符截断并标记 |
| 失败 | `run` 永不抛异常，渠道故障 → `Observation.failure("... send failed: ...")` |
| 调用者 | ① agent（经域 `notify` 暴露）② 宿主（`scheduler` 直接 `tool.run(...)`，不经过模型）——同一份实现 |
| 禁止 | ① 在 Tool 体系外另造 Notifier 抽象；② 把 token/webhook 放进参数；③ 给 `NotifyTool` 增加契约外的方法（删掉它、直接继承 `Tool` 后 Agent/Policy/域协议应零改动） |
| 实现状态 | `console` / `file` / `popo` / `qq` 已实现（popo 走本机 `broccoli-helper`；qq 走官方 `qq-botpy` 的 WebSocket 网关，被动回复带 `msg_id`） |

---

## 2. 判定：小需求该改哪儿

| 需求示例 | 正确落点 | 是否动架构 |
|---|---|---|
| 加一个网页工具 | `fish/tools.py` 的 `ALLOWED_TOOLS` + 危险集合 | 否 |
| 换模型 / 加厂商 | `fish/llm.py` 新增/改配置 | 否 |
| 记忆换压缩算法 | `fish/memory.py` 新实现 | 否 |
| 加"每站限 5 页" | 新 `Policy` 实现 | 否 |
| 让模型看图 | C1 闸门：`attachments` | **是**（协议升级） |
| 一次回多个工具调用 | C3 闸门：协议 v2 | **是** |
| 存跨任务知识 | C9 扩字段 | 否 |
| 任务并行 | 新增调度层，不改循环 | 否（新模块） |

## 3. 违反禁止项的典型症状

- 循环里出现 `if "bilibili" in url` → 站点知识漏进了行为层，应进 Fact
- 工具里出现 `time.sleep` 重试 → 重试属于循环
- `memory.py` 出现 `import fish.llm` → 摘要方式错了，该用 `Summarizer` 回调
- `RunResult.stopped_reason` 出现自由文本 → 评测会失效
- `Message.content` 里出现 `<image>` 之类伪标签 → 多模态在偷偷走后门

## 4. 契约变更流程（唯一允许改汇点的路径）

1. 在本文档对应汇点的"演进闸门"里写明触发条件
2. 修改 `fish/contracts.py`
3. 同步三处：`PROTOCOL_HINT`、`parse_decision`、`eval/selftest.py` 用例
4. 同步 `README.md` 与 `.codemaker/skills/agent-*`
5. selftest 全绿 + 一次端到端 `stop=final` 才可提交

**没有第 1 步的现实触发条件，不允许动汇点。** 这是防止"为一个小需求改架构"的唯一闸门。

## 5. 基线对照

| 汇点 | 参照开源实现 | 取什么 | 不取什么 |
|---|---|---|---|
| C8 读出 | OpenHands SDK `Condenser.condense()` | 两段式（摘要 + 近期）、压缩不自带 LLM | 事件流复杂度、Condensation 事件机制 |
| C9 持久化 | Mem0 memory item（text + metadata + score） | 条目形状、去重、score 反馈 | 每次 add 一次 LLM 决策；v3 的 ADD-only |

## 6. 落地顺序（协议已冻结，按此推进）

| 步骤 | 内容 | 改动范围 | 验收 |
|---|---|---|---|
| 1 | `fish/store.py`：`Fact` + `JsonFactStore`（原子写 `.fish/facts.json`） | 新模块 | selftest：upsert 去重、score 淘汰、损坏文件降级 |
| 2 | `fish/memory.py`：`CondensingMemory`（FACTS + SUMMARY + RECENT，`Summarizer` 回调） | C8 实现 | selftest：预算裁剪、摘要折叠、facts 注入 |
| 3 | 事实写入：选择器自动抽取 + `remember_fact` 工具 | 工具 + 记忆 | 端到端：第二次同站任务步数下降 |
| 4 | 评测：固定 20 条任务回归集，记录成功率/步数/tokens | 新 `eval/` | 基线表 |

任一实现阶段发现需要改汇点 → 停手，回到 §4 流程。

## 7. 可调参数（不是汇点，可随时改）

这些是**旋钮**，不进 `contracts.py`，调整不构成协议变更。但必须集中定义（实现时放在 `fish/memory.py` 的 `MemoryConfig`、`fish/policy.py` 的构造参数），不允许散落在实现里。

| # | 参数 | 默认值 | 可调范围 | 调参依据（怎么验证） |
|---|---|---|---|---|
| 1 | `keep_turns`（原文保留轮数） | 8 | 4–20 | 20 条任务回归集：步数、tokens、成功率 |
| 1b | `compact_chunk`（批量折叠阈值） | 4 | 2–8 | 超过 `keep_turns + chunk` 才折叠；过小会每步都调摘要，过大则上下文变长 |
| 2 | `summary_trigger`（折叠触发） | 未折叠轮数 > `keep_turns`，或本步输入 > 60% 预算 | 50%–75% | 观察 `view()` 输出 token 与 `steps` 变化 |
| 3 | `observation_chars`（单条观察上限） | 1500 | 500–3000 | 看截断后是否丢关键字段（抽查 5 条） |
| 4 | `summary_format` | 3 行：`已完成 / 已知 / 失败` | 3–6 行 | 固定追问探测信息保留率 |
| 5 | `facts_inject_limit` | 8 | 3–15 | 注入条数 vs 成功率（超过 10 条后通常收益递减） |
| 6 | `facts_budget_ratio` | 0.15 | 0.10–0.25 | 与 `summary_budget_ratio` 联动，总占比不超 40% |
| 7 | `summary_budget_ratio` | 0.25 | 0.15–0.35 | 同上 |
| 8 | `score_init` / `score_success` / `score_fail` | 0.5 / +0.1 / −0.3 | 步长 0.05–0.4 | 两轮回归：正确 facts 的留存 vs 失效 facts 的淘汰速度 |
| 9 | `score_prune_at` | ≤ 0 | 固定 | 归零即淘汰，不给"将死"条目留位 |
| 10 | `domain_match` | 精确 host | 精确 host / 二级域 / URL 正则 | 站点分布实测（当前 B 站精确 host 够用） |
| 11 | `selector_extract` | 正则抓 `querySelector('...')` | 正则 / 模型标注 | 正则命中率抽查；低于 60% 再上模型 |
| 12 | `summary_persist` | 关闭（进程内） | 开/关 | 跨任务是否复用摘要；当前靠 facts 即可 |
| 13 | `conflict_policy` | 同 `id` 覆盖 + `score` 重置 0.5 | 覆盖 / 双版本 | 站点改版场景：改版后旧值必须先失效 |
| 14 | `max_turns`（硬上限） | 200 | 100–500 | 防上下文泄漏导致的 OOM |
| 15 | `max_llm_retries` | 2 | 0–5 | 统计一次运行里 `retry` 事件次数与最终失败率 |
| 16 | `retry_backoff` | 0.5s | 0–5s，线性递增 | 观察重试成功率；离线测试固定传 0 |

## 8. 参数改动规则

1. 改参数 → 只改集中定义的默认值，**不动 `contracts.py`**
2. 改完必须在 §7 表格更新"默认值"，并在提交信息里写一句**为什么**
3. 没有测量数据的调参视为猜测：要么补一次对照实验，要么在提交信息里标注"未验证"
4. 参数影响汇点语义时（例如 `domain_match` 改成正则导致 `scope` 不再是域名）→ 升级为协议变更，走 §4

