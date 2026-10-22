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
setup.bat        # 一键检查环境：Python / 依赖 / .env / Node / chrome-devtools
```

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

**限制**：codemaker 自带系统提示会描述它自己的环境（UE/MCP 工具），即使我们禁用全部工具并声明"以下清单为准"，模型仍会偶发地回答"没有浏览器工具"。因此：

- `--domain none`（纯推理）→ codemaker 可用
- 带工具的域（如 `browser`）→ 请用 `--backend deepseek`

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
| `--domain` | `none`（默认，纯核心）/ `browser`（Chrome DevTools MCP） |
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
| `main.py` | CLI 入口，组装一次 episode |
| `fish/runtime.py` | 一次 episode 的统一装配（main / scheduler / eval 共用） |
| `fish/scheduler.py` | 定时任务轮询：到点跑一次 episode 并推送 |
| `fish/server.py` | 常驻服务：监听线程 → 队列 → worker → 回复原渠道 |
| `fish/listeners.py` | 监听来源抽象 + POPO 轮询实现 |
| `fish/session_store.py` | 会话追加日志：持久化 / 恢复 / 坏行容错 |
| `fish/notify.py` | 通知渠道：`console` / `file`，可扩展 POPO/邮件 |
| `fish/contracts.py` | 全部协议、数据类、JSON 决策解析 |
| `fish/mcp.py` | 手写 MCP stdio 客户端（initialize / tools/list / tools/call） |
| `fish/domain.py` | 域扩展协议：`DomainPack` / 能力声明 / 装配与校验 |
| `fish/domains/browser.py` | 浏览器域：Chrome MCP 工具 + 提示 + URL scope |
| `fish/domains/core.py` | 无域核心：零工具，只能直接作答 |
| `eval/selftest.py` | 离线自测：21 个机制用例，不用网络 |
| `eval/domain_check.py` | 域合规检查：能力声明、工具描述、close 幂等 |
| `eval/tasks.json` | 回归任务定义（目标 + 判分规则 + 预算） |
| `eval/run_tasks.py` | 回归 runner：批量跑、出表、与 `results/baseline.json` 对比 |

纯标准库，没有 pip 依赖。

```bash
python eval/selftest.py      # 离线跑：21 个机制用例，不用网络
python eval/domain_check.py  # 域合规检查
python eval/run_tasks.py     # 8 条任务回归集，规则判分 + 基线对比
```

## 开发用 skill

`.codemaker/skills/` 下三个 skill，改这个仓库时自动激活，把上面这些约束固化成可执行规则：

| skill | 触发时机 | 内容 |
|---|---|---|
| `agent-contracts` | 改 `fish/*.py`、`main.py`、`eval/` | 五契约边界、禁止的依赖方向、改能力时该动哪个文件 |
| `agent-tool-design` | 新增/修改工具 | 工具粒度、描述写法、`dangerous/idempotent` 标注、错误文案 |
| `agent-eval` | 验证改动 | Fake → 真工具 → 真模型 三层验证顺序与必备用例 |

## Agent Server（常驻 + 多来源）

```
Listener 线程（POPO / 定时 / ...） → Event 队列 → 单 worker → 回复原渠道
                                                      │
                                       会话记忆 .fish/sessions/*.jsonl
```

```bash
python -m fish.server --once --dry-run --since-minutes 60   # 只看检测，不跑不答
python -m fish.server --once                                # 处理一轮
python -m fish.server                                    # 常驻，15s 轮询
python -m fish.server --verbose --max-steps 8
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--channel` | `popo` | 监听来源（目前仅 POPO） |
| `--session` | 西蓝花会话 | 监听哪个会话 |
| `--self` | 自动探测 | 只响应自己发的消息 |
| `--interval` | 15s | 轮询间隔，也是回复延迟上限 |
| `--since-minutes` | 0 | 首次启动回溯窗口 |
| `--no-persist` | 关 | 不落盘会话（敏感场景） |
| `--once` / `--dry-run` / `--verbose` | — | 单轮 / 只看 / 打步骤 |

**会话记忆**：每轮 turn 追加到 `.fish/sessions/<hash>.jsonl`（append-only，坏一行只丢一条）；压缩摘要也落盘，重启直接复用，不重复付摘要费用。恢复时只载入摘要 + 最近 8 轮，控制上下文。

**断点续做**：episode 非正常结束（预算耗尽 / 模型不可用）时，会额外做一次压缩调用，把「已完成的进度 + 下一步」写成 checkpoint 落盘，并把友好提示回给你：```
这次没做完（步数预算用完）。

已保留进度：
PROGRESS: 已打开搜索结果页，拿到 3 个候选链接
NEXT: 打开第一个链接读文档

回复「继续」我接着做。
```

你回「继续 / 接着 / continue」时，服务会把 checkpoint 注入新目标再跑一轮，不会从零重来。

### QQ 渠道（官方机器人）

需要 `pip install qq-botpy`（可选依赖，不装不影响 POPO），并在 `.env` 配置：

```
FISH_QQ_APPID=你的AppID
FISH_QQ_SECRET=你的AppSecret
```

```bash
python -m fish.server --channels popo,qq    # 两个来源同时监听
python -m fish.server --channels qq
```

平台约束（不是我们的设计选择）：

- 回复必须引用收到的 `msg_id`（被动回复），监听器因此缓存每个会话最近一条
- 群里只有 **@机器人** 会触发；私聊直接发即可
- botpy 走 WebSocket 网关，**不需要公网回调地址**

### 搜索能力（web_search / web_fetch）

无需额外 API key：复用你本机 `~/.codemaker/mcps.json` 里已配置的搜索 MCP 服务（streamable HTTP）。

- 服务启动时自动挂载 `web_search` / `web_fetch`
- 关闭：`--no-search`
- 换服务：`FISH_SEARCH_MCP_URL` + `FISH_SEARCH_MCP_HEADERS`（JSON）

### 危险动作授权

写类操作（浏览器 `click`/`fill`、`qq_write_file`、`qq_send_file`）默认被拒。被拒时回复末尾会提示：

```
（有 1 个操作需要写权限，回复「授权」后我会重试）
```

- 回复 **授权**：当前会话获得 10 分钟写权限，再发一次命令即可
- 回复 **取消授权**：立即收回
- 启动时加 `--allow-dangerous` 可对所有会话常开（不推荐）

## 定时任务

`fish/scheduler.py` 只是一个外层轮询器：到点的 job 跑**一次有界 episode**，然后把结果推给你。agent 循环本身没有"永不停止"模式。

```bash
python -m fish.scheduler --once        # 立刻跑所有 job（调试用）
python -m fish.scheduler               # 常驻，默认 60s 轮询
python -m fish.scheduler --verbose     # 打印每一步
```

job 定义（`fish/jobs.example.json`）：

```json
{
  "id": "bili_daily",
  "goal": "打开 https://www.bilibili.com ，读取首页推荐，输出前 5 条并总结",
  "domain": "browser",
  "domain_options": {"headless": true},
  "backend": "deepseek",
  "notifier": "file:.fish/reports",
  "every_seconds": 86400,
  "max_steps": 8,
  "timeout": 300
}
```

| 字段 | 说明 |
|---|---|
| `every_seconds` | 间隔；状态存在 `.fish/scheduler_state.json`，重启不会重跑 |
| `notify` | `["file:.fish/reports"]` / `["console"]`，可多渠道 |
| `backend` | 带工具的域请用 `deepseek` |
| 失败处理 | 单个 job 失败只记日志并推送失败信息，不中断调度 |

不想常驻时，用 Windows 任务计划或 cron 每天调一次即可：

```bat
schtasks /create /tn fish_daily /tr "cmd /c cd /d D:\PM\Fish && python -m fish.scheduler --once" /sc daily /st 09:00
```

### 通知渠道（Tool 形态）

通知不是宿主私有的抽象，而是 `Tool` 的专用子类：

```
Tool → NotifyTool → ConsoleNotifyTool / FileNotifyTool / PopoNotifyTool
```

- 命名固定 `notify_<channel>`，参数固定 `title` / `text` / `to?`
- `dangerous=True`、`idempotent=False`（外部副作用，重复调用会重复发送）
- `run` 永不抛异常，渠道故障返回 `Observation.failure`
- **同一份实现两个调用者**：agent 通过 `notify` 域调用；scheduler 直接 `tool.run(...)`，不经过模型

新增渠道只需写一个 `send`：

```python
class MyNotifyTool(NotifyTool):
    channel = "mychannel"

    def send(self, title: str, text: str, to: str) -> str:
        ...
```

job 里写 `"notify": ["file:.fish/reports", "console", "popo"]` 即可多渠道。

### POPO 渠道

`notify_popo` 复用本机已有的 `broccoli-helper` MCP 服务（`~/.codemaker/mcps.json` 里的 stdio 配置），调用它的 `send_popo_notification`，因此**不需要机器人 identify/secret**：

```json
"notify": ["popo"]                    // 默认发给当前登录用户
"notify": ["popo:someone@corp.netease.com"]
```

| 环境变量 | 作用 |
|---|---|
| `FISH_POPO_HELPER` | 覆盖 helper 脚本路径（默认从 mcps.json 读取） |
| `FISH_POPO_RECEIVER` | 覆盖默认收件人邮箱 |

限制：依赖 CodeMaker 安装（helper 脚本与登录态），换机器需重新配置；发送前最好先 `console` 干看一遍内容。

**QQ 不建议接**：需要独立 QQ 号 + 第三方协议端（NapCat/onebot），违反腾讯条款、封号风险高、还要维护掉线重连。POPO 机器人、邮件、企业微信都更稳。

## 回归集

`eval/tasks.json` 定义固定任务，`eval/run_tasks.py` 批量跑并与 `eval/results/baseline.json` 对比（基线进版本库，`latest.json` 忽略）。

```json
{
  "id": "page_title",
  "domain": "browser",
  "domain_options": {"headless": true},
  "goal": "打开 https://example.com ，输出页面标题。",
  "checks": [{"type": "contains", "value": "Example Domain"}],
  "budget": {"max_steps": 4, "max_seconds": 120}
}
```

判分类型：`contains` / `regex` / `equals` / `min_items`（按 pattern 计数）/ `not_contains`。

```bash
python eval/run_tasks.py                          # 全部，deepseek
python eval/run_tasks.py --only bili_top5          # 单条
python eval/run_tasks.py --backend codemaker       # 仅适合 domain=none 的任务
python eval/run_tasks.py --save-baseline           # 更新基线
```

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
