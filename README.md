# MahoFish

一个最小可跑的 **LLM 驱动的浏览器 agent**：大模型自己决定开哪个页面、执行什么 JS、看什么内容，最后把结果讲给你听。

默认任务是"打开 B 站首页，总结推荐内容"，但 `--goal` 可以换成任何网页任务。

```
LLM  ──决定调用哪个工具──▶  agent 循环  ──JSON-RPC/stdio──▶  chrome-devtools-mcp  ──CDP──▶  Chrome
      ◀──工具结果回灌───────
```

## 为什么这么设计

- **模型本身没有手脚**。它只会输出文本；真正开浏览器的是 MCP server（`chrome-devtools-mcp`，Google 官方，走 Chrome DevTools Protocol）。
- **不用 function calling 也能跑**。agent 用"单 JSON 协议"约束模型输出，任何 OpenAI 兼容模型都能当大脑，换模型不改代码。
- **读 DOM 而不是截图**。截图对纯文本模型无意义；`evaluate_script` 直接取页面数据，快且省 token。

## 快速开始

前置：Python 3.10+，Node 20+，本机装了 Chrome。默认开发后端用本机 `codemaker` 登录态，不需要 API key；只有 `--backend deepseek`（发布）才需要 key。

```bash
# 1. 拉 MCP server（第一次会下载）
npx -y chrome-devtools-mcp@1.11.0 --version

# 2. 跑默认任务（有界面）
python main.py

# 3. 无界面 + 自定义目标
python main.py --headless --goal "打开 https://www.bilibili.com ，总结首页推荐的前 5 个视频"

# 4. 发布后端（需要 key）
copy .env.example .env      # 填 FISH_API_KEY=sk-...
python main.py --backend deepseek
```

## LLM 配置

两个后端，按部署阶段分工：

| `--backend` | 用途 | 依赖 |
|---|---|---|
| `codemaker`（默认） | 本地开发/自测 | 本机 `codemaker` CLI 登录态，零 key |
| `deepseek` | 发布版 | API key（`FISH_API_KEY`） |

### codemaker（默认，开发版）

```bash
python main.py                      # 默认就是 codemaker
python main.py --backend codemaker  # 等价写法
```

- 自动在空闲端口起 `codemaker serve`，工作目录是临时目录，跑完即关闭
- 每次补全新建 session、用完删除，保证无状态
- 显式关闭它自带的 `bash/read/write/edit` 等工具，只当纯模型用
- 已有服务时设 `FISH_CODEMAKER_URL=http://127.0.0.1:4123`，不再自启
- `--model provider/modelID` 指定模型，不填用服务端默认

实测开销（单次运行）：服务冷启动 ~2.3s（只一次，不按对话算）；建/删 session ~0.05s；单次补全 1–2.5s（短 prompt）。端到端 4 步约 19s，大头是模型推理和页面上下文，不是 CLI 初始化。

### deepseek（发布版）

任何 OpenAI 兼容的 `/chat/completions`，默认指向 DeepSeek。

```bash
python main.py --backend deepseek
```

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FISH_API_KEY` | 无（必填） | 也认 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` |
| `FISH_BASE_URL` | `https://api.deepseek.com/v1` | 换网关/本地模型改这里 |
| `FISH_MODEL` | `deepseek-chat` | 命令行 `--model` 可覆盖 |

读取顺序：真实环境变量 > 仓库根 `.env`。缺 key 时启动即报错，不会静默失败。

换成任意兼容服务（公司网关、Ollama、vLLM）：

```bash
FISH_BASE_URL=http://127.0.0.1:11434/v1
FISH_MODEL=qwen2.5:14b
FISH_API_KEY=ollama          # 本地服务随便填
```

## 参数

| 参数 | 说明 |
|---|---|
| `--goal` | 给 agent 的任务 |
| `--backend` | `codemaker`（默认，开发）/ `deepseek`（发布） |
| `--model` | 覆盖 `FISH_MODEL` |
| `--max-steps` | 步数预算，默认 12 |
| `--max-tokens` | token 预算，默认 200000 |
| `--timeout` | 墙钟预算（秒），默认 1800 |
| `--allow-dangerous` | 允许 `click` / `fill` 这类改页面的工具 |
| `--headless` | 不开窗口 |
| `--quiet` | 不打步骤日志 |

## 代码结构

五个契约全在 `fish/contracts.py`，其余都是它们的实现。循环只依赖这五个，任何一块可整体替换。

```
Agent = ⟨LLM, Tools, Memory⟩ × Loop(+Policy)
```

| 契约 | 实现 | 职责 |
|---|---|---|
| `LLMBackend.complete(messages) -> Reply` | `fish/llm.py`：`DeepSeekClient`（任何 OpenAI 兼容接口） | 外接大脑 |
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

## 汇点与设计约束

[`docs/contracts.md`](docs/contracts.md) 冻结了全部 14 个数据汇点（Message / Reply / Decision / Observation / ToolSpec / ToolRegistry / Turn / Memory.view / Fact / Policy / Usage / 循环 / 配置 / 落盘），每个汇点写明：允许的扩展方式、禁止事项、以及**演进闸门**（什么条件下才允许改协议）。

改代码前先在那里找落点：找不到落点或触碰禁止项，才走 §4 的契约变更流程。

## 已知限制

- 模型看不到图：需要视觉时得换多模态后端，`take_screenshot` 目前只返回占位文本
- 走单 JSON 协议而非原生 function calling：每步都要重发全量上下文，长任务成本偏高
- 选择器靠模型自己写 JS，站点改版会退化；生产化应固化每站的取数脚本
- 单页、单步、无登录态；登录/Cookie 走 `--user-data-dir` 另配

## 合规提醒

只读、低频、遵守目标站点的 robots.txt 与服务条款。不要拿它做批量注册、刷量、自动发帖这类事——平台风控识别得出自动化行为，账号风险自负。

## License

MIT，见 [LICENSE](LICENSE)。
