# MahoFish

一个最小可跑的 **LLM 驱动的浏览器 agent**：大模型自己决定开哪个页面、执行什么 JS、看什么内容，最后把结果讲给你听。

默认任务是"打开 B 站首页，总结推荐内容"，但 `--goal` 可以换成任何网页任务。

```
LLM  ──决定调用哪个工具──▶  agent 循环  ──JSON-RPC/stdio──▶  chrome-devtools-mcp  ──CDP──▶  Chrome
      ◀──工具结果回灌───────
```

## 为什么这么设计

- **模型本身没有手脚**。它只会输出文本；真正开浏览器的是 MCP server（`chrome-devtools-mcp`，Google 官方，走 Chrome DevTools Protocol）。
- **不用 function calling 也能跑**。agent 用"单 JSON 协议"约束模型输出，任何后端（包括只会聊天的 CLI 模型）都能当大脑。
- **读 DOM 而不是截图**。截图对纯文本模型无意义；`evaluate_script` 直接取页面数据，快且省 token。

## 快速开始

前置：Windows / macOS / Linux，Python 3.10+，Node 20+，本机装了 Chrome。

```bash
# 1. 拉 MCP server（第一次会下载）
npx -y chrome-devtools-mcp@1.11.0 --version

# 2. 跑默认任务（有界面）
python main.py

# 3. 无界面 + 自定义目标
python main.py --headless --goal "打开 https://www.bilibili.com ，总结首页推荐的前 5 个视频"
```

## LLM 后端

### `--backend codemaker`（默认，零 API key）

调用本机已登录的 `codemaker` CLI 当一次性补全后端：

```bash
python main.py --backend codemaker -m netease-codemaker/glm-5.3-flash
```

凭证由 CLI 自己管理；换机器只要重新 `codemaker` 登录。

### `--backend openai`（任何 OpenAI 兼容接口）

```bash
set FISH_API_KEY=sk-xxx
set FISH_BASE_URL=https://api.deepseek.com/v1    # 可选，默认 DeepSeek
set FISH_MODEL=deepseek-chat                      # 可选
python main.py --backend openai
```

也认 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`。**key 只从环境变量读，不要写进代码。**

## 参数

| 参数 | 说明 |
|---|---|
| `--goal` | 给 agent 的任务 |
| `--backend` | `codemaker` / `openai` |
| `--model` | 模型名 |
| `--max-steps` | 步数预算，默认 12 |
| `--headless` | 不开窗口 |
| `--quiet` | 不打步骤日志 |

## 代码结构

五个契约全在 `fish/contracts.py`，其余都是它们的实现。循环只依赖这五个，任何一块可整体替换。

```
Agent = ⟨LLM, Tools, Memory⟩ × Loop(+Policy)
```

| 契约 | 实现 | 职责 |
|---|---|---|
| `LLMBackend.complete(messages) -> Reply` | `fish/llm.py`：`CodemakerCLI` / `OpenAICompat` | 外接大脑 |
| `Tool.spec` / `Tool.run(args, ctx) -> Observation` | `fish/tools.py`：`MCPTool`（Chrome MCP 工具） | 行为能力 |
| `ToolRegistry.specs()/get(name)` | `fish/tools.py`：`MCPToolRegistry` | 工具目录 |
| `Memory.add(turn)` / `view(budget_tokens)` | `fish/memory.py`：`WindowMemory` | 记忆：滑窗 + 折叠 |
| `Policy.check(spec,args)` / `spend(usage)` / `exhausted()` | `fish/policy.py`：`StaticPolicy` | 边界：白名单 + 预算 |
| 循环 | `fish/agent.py`：`Agent.run() -> RunResult` | 把上面四个串起来 |

| 文件 | 职责 |
|---|---|
| `main.py` | CLI 入口，组装五块 |
| `fish/contracts.py` | 全部协议、数据类、JSON 决策解析 |
| `fish/mcp.py` | 手写 MCP stdio 客户端（initialize / tools/list / tools/call） |
| `eval/selftest.py` | 离线自测：FakeLLM/FakeTool 驱动循环，不用网络 |

纯标准库，没有 pip 依赖。

```bash
python eval/selftest.py   # 离线跑：正常路径、策略拦截、坏 JSON、预算耗尽
```

## 开发用 skill

`.codemaker/skills/` 下三个 skill，改这个仓库时自动激活，把上面这些约束固化成可执行规则：

| skill | 触发时机 | 内容 |
|---|---|---|
| `agent-contracts` | 改 `fish/*.py`、`main.py`、`eval/` | 五契约边界、禁止的依赖方向、改能力时该动哪个文件 |
| `agent-tool-design` | 新增/修改工具 | 工具粒度、描述写法、`dangerous/idempotent` 标注、错误文案 |
| `agent-eval` | 验证改动 | Fake → 真工具 → 真模型 三层验证顺序与必备用例 |

## 已知限制

- 模型看不到图：需要视觉时得换多模态后端，`take_screenshot` 目前只返回占位文本
- `codemaker` 后端是**一次性补全**，每步要重发全量上下文，比原生 function calling 慢
- 选择器靠模型自己写 JS，站点改版会退化；生产化应固化每站的取数脚本
- 单页、单步、无登录态；登录/Cookie 走 `--user-data-dir` 另配

## 合规提醒

只读、低频、遵守目标站点的 robots.txt 与服务条款。不要拿它做批量注册、刷量、自动发帖这类事——平台风控识别得出自动化行为，账号风险自负。

## License

MIT，见 [LICENSE](LICENSE)。
