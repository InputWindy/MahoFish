---
name: agent-eval
description: 验证 MahoFish 改动（循环、记忆、策略、工具、后端）是否可交付时自动激活。规定"先 Fake 后真机"的验证顺序、四类必测用例、真实 MCP 链路 smoke 与端到端预算参数，避免用一次真机运行冒充验证。
---

# Agent 验收

agent 有两层不确定性（模型随机 + 网页变化），所以验证必须分层：**离线可复现，在线只验连通**。

## 顺序：Fake → 真工具 → 真模型

```
eval/selftest.py      FakeLLM + FakeTool     秒级、无网络、必跑
真实 MCP registry     FakeLLM + 真 Chrome     验工具契约是否被遵守
python main.py        真模型 + 真 Chrome      验端到端，最贵，最少跑
```

跳过前两层直接跑第三层，等于用一次运气冒充回归测试。

## 第一层：离线用例（`eval/selftest.py`）

四类必须覆盖，新增能力继续往这里加：

| 用例 | 断言什么 |
|---|---|
| `happy-path` | 动作序列正确 + 收敛到 `final` |
| `policy-denies-dangerous` | 危险工具被拒，且模型能看到拒绝原因后改路 |
| `bad-json-recovers` | 模型回非 JSON → 注入失败观察 → 最终仍能收敛 |
| `budget-stops` | 步数耗尽返回 `Stopped: ...`，`stopped_reason` 正确 |

写法约定：

- `ScriptedLLM(replies)` 按顺序吐预设回复，`prompts` 记录每轮输入，可用于断言上下文内容
- 断言只看**契约层结果**（`RunResult.answer/stopped_reason`、`Observation.ok`），不断言提示词全文
- 每个用例独立构造 `WindowMemory` / `StaticPolicy`，不共享状态

```bash
python eval/selftest.py     # 全绿才算过
```

## 第二层：真实工具链路

只验"工具契约没被实现破坏"，不接模型：

```python
from fish.contracts import ToolContext
from fish.tools import MCPToolRegistry, build_chrome_client

with build_chrome_client(headless=True) as client:
    reg = MCPToolRegistry(client)
    print([s.render() for s in reg.specs()])          # 参数是否干净、标注是否正确
    print(reg.get("navigate_page").run({"url": "https://www.bilibili.com"}, ToolContext(step=1)))
    print(reg.get("evaluate_script").run({"function": "() => document.title"}, ToolContext(step=2)))
```

判定：`Observation.ok is True`，且 `content` 不是 `ERROR:` 开头。

## 第三层：端到端

```bash
python main.py --headless --max-steps 4 --timeout 600 --quiet
```

带预算参数跑，避免一次失败挂几十分钟。判定三件事：

1. `stop=final`（不是 `step budget exhausted`）
2. `steps` 在预期内（B 站首页总结正常 2–4 步）
3. `tokens` 没有爆炸（单步 > 3 万说明记忆没压缩好）

多轮任务的成本曲线：**同一目标连跑 3 次**，看 steps/tokens 波动。波动大说明提示词或记忆不稳定。

## 后端可用性单独验

后端挂了要能一眼区分是"模型问题"还是"agent 问题"：

```bash
# 只验后端，不碰浏览器
python -c "from fish.llm import build_llm; from fish.contracts import Message, Role; \
print(build_llm().complete([Message(Role.USER, '回复 OK')]))"
```

已知坑：

- 缺 key 时 `build_llm()` 直接抛 `LLMError`，不会静默走到浏览器
- `.env` 里的值不会覆盖已存在的真实环境变量，排查"改了没生效"先查 `set FISH_API_KEY`
- 代理/防火墙会表现为 `cannot reach ...: timed out`，与 HTTP 401（key 错）要分清

## 交付前清单

- [ ] `python -m py_compile main.py fish\*.py eval\selftest.py`
- [ ] `python eval/selftest.py` 全绿
- [ ] 真实工具链路 smoke 通过
- [ ] 端到端 `stop=final`，steps/tokens 在预期内
- [ ] 新增行为都有对应离线用例（没有就先补用例再改代码）
