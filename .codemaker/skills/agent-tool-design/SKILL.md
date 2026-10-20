---
name: agent-tool-design
description: 给 MahoFish 新增或修改工具（fish/tools.py、ALLOWED_TOOLS、MCPTool、ToolRegistry 实现）时自动激活。约束工具粒度、描述写法、参数命名、dangerous/idempotent 标注、结果截断与错误文案，避免模型乱调工具或对错误无法自愈。
---

# Agent 工具设计

模型犯错的 80% 来自工具设计，不是模型本身。工具是 agent 的产品界面。

## 粒度：一个工具 = 一个原子动作

| 好 | 坏 |
|---|---|
| `navigate_page(url)` | `browse_and_summarize(url)`：把决策塞进工具 |
| `evaluate_script(fn)` | `get_video_list()`：站点耦合，换站就废 |
| `click(uid)` | `click_like_button()`：语义任务，不是动作 |

判断标准：**工具名能不能用"动词 + 宾语"说清楚？** 说不清就是任务，不该是工具。

## 描述三段式

```python
description = (
    "做什么（一句，动词开头）。"
    "何时用（什么场景该选它）。"
    "何时不要用（更便宜/更稳的替代是什么）。"
)
```

真实例子（注入给模型的原文）：`evaluate_script(...): Evaluate JavaScript inside the target page. ... Returns the response as JSON, so returned values have to be JSON-serializable.`

反面例子：`description="获取数据"` —— 模型只能猜。

## 参数

- **越少越好**：能自动补的就别暴露（`pageId` 由 `MCPTool.run` 自动补，`spec` 里已剔除）
- **必填越少越好**：`required=[]` 优先，模型少犯错
- **命名保持源系统原样**：MCP 的 `uid` / `function` 不要改名，否则模型看了描述、参数对不上
- **schema 要真**：`properties` 里的类型直接影响模型输出格式

## 返回值

```python
Observation.success(text)      # 成功，text 是给模型看的内容
Observation.failure("原因")     # 失败，内容自动加 "ERROR: " 前缀
```

- **永不抛异常**，除非底层进程死了
- **错误必须可行动**：`unknown tool 'foo'; available: ['click', 'fill']` 能让模型下一步自愈；`tool error` 不能
- **单条结果截断 6KB**（`MAX_RESULT_CHARS`），超长数据在前 6KB 里就要给结论
- **图像类工具返回占位文本**（`[image omitted: image/png]`），纯文本后端看不懂图

## 元数据

| 字段 | 何时置位 | 后果 |
|---|---|---|
| `dangerous=True` | 会改变外部状态（`click` / `fill` / 发消息 / 写文件） | 默认被 `StaticPolicy` 拒绝，需 `--allow-dangerous` |
| `idempotent=False` | 重放会产生副作用（导航会丢页面状态） | 提示模型不要重复调用 |

漏标的后果：模型重试时重复点击、重复发消息，且策略层无法拦截。

## 新增工具的固定动作

1. `fish/tools.py` 的 `ALLOWED_TOOLS` 加名字（顺序 = 展示顺序，常用靠前）
2. 危险或非幂等的，加进 `DANGEROUS_TOOLS` / `NON_IDEMPOTENT_TOOLS`
3. 校验 `specs()` 输出：参数是否干净、描述是否可读、标注是否正确
4. `eval/selftest.py` 的 `FakeRegistry` 补一条同名 Fake 工具 + 一个用例
5. 真实链路验证：`python -c "..."` 调一次 `registry.get(name).run(...)`，看 `Observation.ok`

## 反例清单

- 工具内部调用 LLM（工具开始思考 → 不可测、成本翻倍）
- 工具内部做重试（重试归属循环，工具只报错）
- 工具返回原始 HTML/整页 dump（吃光上下文，应返回抽取后的结构）
- 一个工具塞多个 action 参数（`action="click"| "type"`，模型会选错）
