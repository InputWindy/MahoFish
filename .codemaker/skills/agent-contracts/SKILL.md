---
name: agent-contracts
description: 修改 MahoFish（fish/*.py、main.py、eval/）的 agent 循环、契约、记忆或策略时自动激活。固化 agent = ⟨LLM, Tools, Memory⟩ × Loop(+Policy) 的五契约边界，禁止循环耦合具体实现，禁止工具/记忆互相依赖，防止协议改动漏改三处。
---

# Agent 五契约

> 改任何汇点前，先看 `docs/contracts.md`：14 个汇点的允许扩展、禁止事项、演进闸门都在那里。没有闸门里写的现实触发条件，不许动协议。

## 唯一真源

`fish/contracts.py` 是全部协议、数据类、决策解析的唯一来源。任何模块**只允许**依赖它，不允许依赖别的实现模块。

```
LLMBackend.complete(messages) -> Reply(text, tokens)
Tool.spec -> ToolSpec / Tool.run(args, ctx) -> Observation(ok, content, error)
ToolRegistry.specs() -> list[ToolSpec] / get(name) -> Tool | None
Memory.add(turn) / view(budget_tokens) -> list[Message] / reset()
Policy.max_tokens / check(spec, args) -> Verdict / spend(usage) / exhausted()
Agent.run() -> RunResult(answer, steps, usage, stopped_reason)
```

## 允许的依赖方向

```
main.py ──▶ agent.py ──▶ contracts ◀── llm.py / tools.py / memory.py / policy.py
```

**禁止**（出现即视为设计错误，必须重构）：

| 反模式 | 为什么错 |
|---|---|
| `agent.py` 里 `import` 具体工具或 `chrome-devtools-mcp` | 循环就被绑死在浏览器上了 |
| `memory.py` 依赖 `ToolSpec` / 工具名 | 记忆退化成"日志格式化器" |
| `tools.py` 依赖 `LLMBackend` | 工具开始自己思考 |
| `contracts.py` 依赖任何实现模块 | 循环成了双向依赖 |
| 循环内部自己数步数/算 token | 预算散落两处，必然不一致 |

## 五条硬规则

1. **Observation 不抛异常**。工具失败一律 `Observation.failure(...)`，让模型看见错误并自愈；只有底层 infra 崩溃（MCP 进程死了）才允许抛。
2. **预算只在 Policy**。循环每步 `policy.spend(usage)`，退出条件只问 `policy.exhausted()`。
3. **记忆只认 `Turn` / `Message`**。`Memory.view()` 返回给 LLM 的消息，不含工具清单、不含 prompt 模板。
4. **元数据驱动策略**。`dangerous` / `idempotent` 是 `ToolSpec` 的字段，拦截逻辑写在 `Policy.check()`，不写在循环里。
5. **循环负责兜底收敛**。重复调用（`tool + args` 与上一步相同）不执行，回 `Observation.failure` 逼模型换路；连续同工具多步无进展时注入收敛提示。这是循环职责，不要下沉到工具或策略。
6. **协议改动必须同步三处**：`contracts.PROTOCOL_HINT`、`contracts.parse_decision()`、`eval/selftest.py` 的用例。

## 想加能力时，改哪里

| 需求 | 落点 | 循环改动 |
|---|---|---|
| 换模型 / 加网关 | `llm.py` 新增 `LLMBackend` 实现 | 0 行 |
| 换工具源（MCP / 本地函数 / HTTP） | 新增 `ToolRegistry` 实现 | 0 行 |
| 换记忆策略（摘要、向量检索） | 新增 `Memory` 实现 | 0 行 |
| 换范式（Plan-Execute、Reflexion） | 只改 `agent.py` | 只改循环 |
| 加权限/审批/沙箱 | 新增 `Policy` 实现 | 0 行 |

如果发现"必须改循环才能加这个能力"，说明契约缺失——先补契约，再实现。

## 改完自检

```bash
python -m py_compile main.py fish\*.py eval\selftest.py
python eval\selftest.py     # 四个用例必须全绿
```

再人工确认一遍：

- 新增的 `import` 是否跨越了上面那张"禁止"表
- 新增的失败路径是否返回 `Observation.failure`
- 新增的计数/限额是否落在 `Policy`
