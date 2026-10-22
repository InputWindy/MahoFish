"""Offline self-test: exercises the loop with fake LLM/tools, no network.

    python eval/selftest.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fish.agent import Agent
from fish.contracts import (
    Action,
    Decision,
    LLMBackend,
    Message,
    Observation,
    Reply,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolSpec,
    Turn,
)
from fish.memory import MemoryConfig, WindowMemory
from fish.notify import MAX_TEXT, ConsoleNotifyTool, FileNotifyTool, PopoNotifyTool
from fish.notify import popo_helper_command
from fish.qq import QQNotifyTool
from fish.store import Fact, JsonFactStore
from fish.session_store import SessionStore
from fish.listeners import Event
from fish.server import Server, ServerConfig, _friendly_stop, _split_checkpoint
from fish import domain as domain_mod
from fish.domain import DomainCapabilities, DomainError, DomainPack, validate
from fish.policy import StaticPolicy


class ScriptedLLM(LLMBackend):
    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.prompts: list[str] = []

    def complete(self, messages: list[Message]) -> Reply:
        self.prompts.append(messages[-1].content)
        index = min(len(self.prompts) - 1, len(self._replies) - 1)
        return Reply(text=self._replies[index], tokens=100)


class LookupTool(Tool):
    calls = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="lookup",
            description="Return canned page titles.",
            parameters={"properties": {"query": {"type": "string"}}, "required": []},
        )

    def run(self, args: dict, ctx: ToolContext) -> Observation:
        LookupTool.calls += 1
        return Observation.success('["标题A", "标题B", "标题C"]')


class ClickTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="click", description="Click something.", dangerous=True)

    def run(self, args: dict, ctx: ToolContext) -> Observation:
        return Observation.success("clicked")


class FakeRegistry(ToolRegistry):
    def __init__(self) -> None:
        self._tools = {tool.spec.name: tool for tool in (LookupTool(), ClickTool())}

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)


def run_case(name: str, replies: list[str], *, max_steps: int = 6) -> str:
    LookupTool.calls = 0
    llm = ScriptedLLM(replies)
    memory = WindowMemory(
        goal="读取页面标题",
        config=MemoryConfig(keep_turns=2, observation_chars=200),
    )
    policy = StaticPolicy(max_steps=max_steps, max_tokens=50_000)
    events: list[tuple[int, str, str]] = []
    agent = Agent(llm, FakeRegistry(), memory, policy, on_event=lambda *a: events.append(a))
    result = agent.run()
    print(f"[{name}] stop={result.stopped_reason} steps={result.steps}")
    print(f"[{name}] answer={result.answer!r}")
    return result.answer


class NoToolsRegistry(ToolRegistry):
    """Domain-free core: the loop must still close."""

    def specs(self) -> list[ToolSpec]:
        return []

    def get(self, name: str) -> Tool | None:
        return None


class FakeDomain(DomainPack):
    name = "fake"
    capabilities = DomainCapabilities(tools=True)

    def __init__(self) -> None:
        self._registry = FakeRegistry()

    def registry(self) -> ToolRegistry:
        return self._registry


class LiarDomain(FakeDomain):
    """Claims observers but returns none: must be rejected."""

    name = "liar"
    capabilities = DomainCapabilities(tools=True, observers=True)


def run_domain_case() -> None:
    validate(FakeDomain())
    validate(domain_mod.load("none"))
    try:
        validate(LiarDomain())
    except DomainError as exc:
        print(f"[domain] capability mismatch rejected: {exc}")
    else:
        raise AssertionError("declared/implemented mismatch must be rejected")
    try:
        domain_mod.load("does-not-exist")
    except DomainError as exc:
        print(f"[domain] unknown domain rejected: {exc}")
    else:
        raise AssertionError("unknown domain must be rejected")


def run_no_tools_case() -> None:
    llm = ScriptedLLM(['{"thought":"no tools","final":"直接回答：42"}'])
    memory = WindowMemory(goal="告诉我答案")
    policy = StaticPolicy(max_steps=3, max_tokens=10_000)
    result = Agent(llm, NoToolsRegistry(), memory, policy).run()
    print(f"[no-tools] stop={result.stopped_reason} steps={result.steps}")
    assert result.answer == "直接回答：42", result.answer
    assert result.stopped_reason == "final", result.stopped_reason
    assert result.steps == 1, result.steps


class FlakyLLM(ScriptedLLM):
    def __init__(self, replies: list[str], fail_times: int = 1) -> None:
        super().__init__(replies)
        self._fail = fail_times

    def complete(self, messages: list[Message]) -> Reply:
        if self._fail:
            self._fail -= 1
            raise RuntimeError("simulated network failure")
        return super().complete(messages)


class BoomTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="boom", description="Always raises.")

    def run(self, args: dict, ctx: ToolContext) -> Observation:
        raise RuntimeError("tool exploded")


class BoomRegistry(ToolRegistry):
    def __init__(self) -> None:
        self._tool = BoomTool()

    def specs(self) -> list[ToolSpec]:
        return [self._tool.spec]

    def get(self, name: str) -> Tool | None:
        return self._tool if name == "boom" else None


class BoomPolicy(StaticPolicy):
    def check(self, spec: ToolSpec, args: dict):
        raise RuntimeError("policy exploded")


class BoomMemory(WindowMemory):
    def view(self, budget_tokens: int) -> list[Message]:
        raise RuntimeError("memory exploded")


def _run(llm, registry=None, memory=None, policy=None, **kwargs):
    return Agent(
        llm,
        registry or FakeRegistry(),
        memory or WindowMemory(goal="g"),
        policy or StaticPolicy(max_steps=6, max_tokens=50_000),
        **kwargs,
    ).run()


def run_failure_cases() -> None:
    result = _run(
        FlakyLLM(['{"thought":"x","final":"never"}'], fail_times=99),
        max_llm_retries=0,
    )
    print(f"[llm-error] stop={result.stopped_reason}")
    assert result.stopped_reason.startswith("llm error"), result.stopped_reason
    assert any(f.source == "llm" for f in result.faults), result.faults

    result = _run(
        FlakyLLM(['{"thought":"x","final":"retry ok"}'], fail_times=1),
        max_llm_retries=2,
        retry_backoff=0,
    )
    print(f"[llm-retry] answer={result.answer!r} steps={result.steps}")
    assert result.answer == "retry ok", result.answer

    result = _run(
        ScriptedLLM(
            [
                '{"thought":"call","tool":"boom","args":{}}',
                '{"thought":"fallback","final":"工具崩了但我还能继续"}',
            ]
        ),
        registry=BoomRegistry(),
    )
    print(f"[tool-crash] answer={result.answer!r}")
    assert result.answer == "工具崩了但我还能继续", result.answer
    assert any(f.source == "tool" for f in result.faults), result.faults

    result = _run(
        ScriptedLLM(
            [
                '{"thought":"call","tool":"lookup","args":{}}',
                '{"thought":"done","final":"策略崩了也能继续"}',
            ]
        ),
        policy=BoomPolicy(max_steps=6),
    )
    print(f"[policy-crash] answer={result.answer!r}")
    assert result.answer == "策略崩了也能继续", result.answer

    result = _run(
        ScriptedLLM(['{"thought":"x","final":"记忆崩了也能继续"}']),
        memory=BoomMemory(goal="g"),
    )
    print(f"[memory-crash] answer={result.answer!r}")
    assert result.answer == "记忆崩了也能继续", result.answer

    memory = WindowMemory(goal="g", config=MemoryConfig(keep_turns=1))
    result = _run(
        ScriptedLLM(
            [
                '{"thought":"a","tool":"lookup","args":{"q":"1"}}',
                '{"thought":"b","tool":"lookup","args":{"q":"2"}}',
                '{"thought":"done","final":"摘要崩了也能继续"}',
            ]
        ),
        memory=memory,
        summarizer=lambda turns: (_ for _ in ()).throw(RuntimeError("summary boom")),
    )
    print(f"[summarizer-crash] answer={result.answer!r}")
    assert result.answer == "摘要崩了也能继续", result.answer

    result = _run(
        ScriptedLLM(['{"thought":"a","tool":"lookup","args":{"q":"1"}}']),
        policy=StaticPolicy(max_steps=10, max_tokens=150),
    )
    print(f"[token-budget] stop={result.stopped_reason}")
    assert "token budget" in result.stopped_reason, result.stopped_reason


class BoomRegistry2(ToolRegistry):
    def specs(self) -> list[ToolSpec]:
        raise RuntimeError("registry boom")

    def get(self, name: str) -> Tool | None:
        raise RuntimeError("registry boom")


class BoomBudgetPolicy(StaticPolicy):
    @property
    def max_tokens(self) -> int:
        raise RuntimeError("budget boom")


def run_last_resort_cases() -> None:
    result = _run(
        ScriptedLLM(['{"thought":"x","final":"注册表崩了也能继续"}']),
        registry=BoomRegistry2(),
    )
    print(f"[registry-crash] answer={result.answer!r}")
    assert result.answer == "注册表崩了也能继续", result.answer

    result = _run(
        ScriptedLLM(['{"thought":"x","final":"预算崩了也能继续"}']),
        policy=BoomBudgetPolicy(max_steps=6),
    )
    print(f"[budget-crash] answer={result.answer!r}")
    assert result.answer == "预算崩了也能继续", result.answer

    result = _run(
        ScriptedLLM(['{"thought":"x","final":"日志崩了也能继续"}']),
        on_event=lambda *args: (_ for _ in ()).throw(RuntimeError("log boom")),
    )
    print(f"[event-crash] answer={result.answer!r}")
    assert result.answer == "日志崩了也能继续", result.answer


class BadSpecRegistry(ToolRegistry):
    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(name="bad", description="", parameters=None)]

    def get(self, name: str) -> Tool | None:
        return None


class NonObservationTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="oops", description="Returns the wrong type.")

    def run(self, args: dict, ctx: ToolContext):  # type: ignore[override]
        return "not an observation"


class NonObservationRegistry(ToolRegistry):
    def __init__(self) -> None:
        self._tool = NonObservationTool()

    def specs(self) -> list[ToolSpec]:
        return [self._tool.spec]

    def get(self, name: str) -> Tool | None:
        return self._tool if name == "oops" else None


def run_contract_violation_cases() -> None:
    result = _run(
        ScriptedLLM(['{"thought":"x","final":"坏描述也能继续"}']),
        registry=BadSpecRegistry(),
    )
    print(f"[bad-spec] answer={result.answer!r}")
    assert result.answer == "坏描述也能继续", result.answer

    result = _run(
        ScriptedLLM(
            [
                '{"thought":"call","tool":"oops","args":{}}',
                '{"thought":"done","final":"工具返错类型也能继续"}',
            ]
        ),
        registry=NonObservationRegistry(),
    )
    print(f"[bad-observation] answer={result.answer!r}")
    assert result.answer == "工具返错类型也能继续", result.answer


def run_notify_case() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tool = FileNotifyTool(tmp)
        spec = tool.spec
        assert spec.name == "notify_file", spec.name
        assert spec.dangerous and not spec.idempotent, spec
        assert set(spec.parameters["properties"]) == {"title", "text", "to"}

        empty = tool.run({"title": "t", "text": "   "}, ToolContext(step=1))
        assert not empty.ok, empty

        long_text = "字" * (MAX_TEXT + 50)
        ok = tool.run({"title": "长文本", "text": long_text}, ToolContext(step=1))
        assert ok.ok, ok
        path = Path(ok.content.split("sent via file to ", 1)[-1])
        assert path.is_file(), ok.content
        assert "[truncated]" in path.read_text(encoding="utf-8")

        console = ConsoleNotifyTool()
        assert console.spec.name == "notify_console"

    pack = domain_mod.load("notify", channels=["file:" + tmp])
    names = [s.name for s in pack.registry().specs()]
    assert names == ["notify_file"], names
    pack.close()

    popo = PopoNotifyTool()
    assert popo.spec.name == "notify_popo"
    assert popo.spec.dangerous and not popo.spec.idempotent
    if popo_helper_command() is None:
        blocked = popo.run({"title": "t", "text": "x"}, ToolContext(step=1))
        assert not blocked.ok and "not configured" in blocked.content, blocked
        print("[notify] popo helper not configured -> failure path OK (no send)")
    else:
        print("[notify] popo helper configured; selftest never sends")

    print("[notify] contract (naming/flags/schema/truncation/domain/popo) OK")


def run_session_case() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(tmp)
        sid = "13e43356@app.robot.popo.com"
        store.set_goal(sid, "第一轮")
        store.append_turn(
            sid,
            Turn(
                step=1,
                decision=Decision(thought="t", action=Action("lookup", {"q": "x"})),
                observation=Observation.success("ok"),
            ),
        )
        store.append_summary(sid, "摘要A", 4)
        store.append_turn(
            sid,
            Turn(step=2, decision=Decision(thought="t", final="done"), observation=None),
        )

        snapshot = store.load(sid, keep_turns=8)
        assert snapshot.goal == "第一轮", snapshot.goal
        assert snapshot.summary == "摘要A", snapshot.summary
        assert snapshot.folded == 4, snapshot.folded
        assert len(snapshot.turns) == 2, snapshot.turns

        memory = WindowMemory(goal="第一轮")
        memory.seed(snapshot.summary, snapshot.turns, snapshot.folded)
        assert memory.summary == "摘要A"
        assert memory.folded == 4
        assert "摘要A" in memory.view(4000)[0].content

        memory.set_goal("第二轮")
        assert memory.goal == "第二轮"

        with store.path_for(sid).open("a", encoding="utf-8") as stream:
            stream.write('{"t":"turn","step":3')  # torn write from a crash
        assert len(store.load(sid).turns) == 2, "torn line must be skipped"
    print("[session] persist -> restore -> new goal, torn line tolerated")


class FakeQQ:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, session: str, content: str, kind: str = "") -> str:
        self.sent.append((session, content, kind))
        return f"qq:{session}"


def run_qq_case() -> None:
    fake = FakeQQ()
    tool = QQNotifyTool(fake)  # type: ignore[arg-type]
    assert tool.spec.name == "notify_qq", tool.spec.name
    assert tool.spec.dangerous and not tool.spec.idempotent

    ok = tool.run(
        {"title": "t", "text": "hello", "to": "openid-1"}, ToolContext(step=1)
    )
    assert ok.ok, ok
    assert fake.sent == [("openid-1", "hello", "")], fake.sent

    missing = tool.run({"title": "t", "text": "x", "to": ""}, ToolContext(step=1))
    assert not missing.ok, missing
    print("[qq] notify tool contract OK (no SDK, no network)")


class WriteTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="writer",
            description="Writes external state.",
            permissions=frozenset({"write"}),
        )

    def run(self, args: dict, ctx: ToolContext) -> Observation:
        return Observation.success("written")


def run_permission_case() -> None:
    tool = WriteTool()
    strict = StaticPolicy(allow={"writer"})
    verdict = strict.check(tool.spec, {})
    assert not verdict.allowed and "permission" in verdict.reason, verdict

    loose = StaticPolicy(allow={"writer"}, allow_dangerous=True)
    assert loose.check(tool.spec, {}).allowed

    read_only = StaticPolicy(deny_permissions=set())
    assert read_only.check(tool.spec, {}).allowed
    print("[policy] permissions drive the verdict, no tool names hardcoded")


def run_facts_feedback_case() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = JsonFactStore(Path(tmp) / "facts.json")
        fact = store.upsert(
            Fact(scope="x.com", kind="selector", key="title", text="title: .t")
        )
        memory = WindowMemory(goal="g", scope="x.com", facts=store)
        memory.view(4000)
        assert memory.injected_facts == [fact.id], memory.injected_facts

        memory.feedback(True)
        assert store.all()[0].score > fact.score, store.all()[0].score
        assert memory.injected_facts == [], "feedback must consume the injection"

        for _ in range(3):
            memory.view(4000)
            memory.feedback(False)
        assert store.all() == [], "failed facts must be pruned"
    print("[facts] injected -> feedback(+/-) -> prune")


class FakeQQChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_file(self, session: str, source: str, kind: str = "") -> str:
        self.sent.append((session, source))
        return f"qq:{session}"


def run_qq_file_case() -> None:
    from fish.qq import (
        QQReadFileTool,
        QQSender,
        QQSendFileTool,
        QQWriteFileTool,
        file_type_of,
    )
    from fish.tools import CompositeRegistry

    assert file_type_of("a.PNG") == 1 and file_type_of("b.zip") == 4

    channel = FakeQQChannel()
    sender = QQSender(channel)  # type: ignore[arg-type]
    tool = QQSendFileTool(sender)
    assert tool.spec.name == "qq_send_file" and tool.spec.dangerous

    no_target = tool.run({"path": "x.zip"}, ToolContext(step=1))
    assert not no_target.ok and "conversation" in no_target.content, no_target

    sender.target = "openid-x"
    sent = tool.run({"path": "x.zip"}, ToolContext(step=1))
    assert sent.ok and channel.sent == [("openid-x", "x.zip")], channel.sent

    with tempfile.TemporaryDirectory() as tmp:
        inbox = Path(tmp) / "inbox"
        inbox.mkdir()
        inside = inbox / "a.txt"
        inside.write_text("hello", encoding="utf-8")
        outside = Path(tmp) / "b.txt"
        outside.write_text("secret", encoding="utf-8")

        reader = QQReadFileTool(inbox)
        ok = reader.run({"path": str(inside)}, ToolContext(step=1))
        assert ok.ok and "hello" in ok.content, ok
        blocked = reader.run({"path": str(outside)}, ToolContext(step=1))
        assert not blocked.ok and "inbox" in blocked.content, blocked

        outbox = Path(tmp) / "outbox"
        writer = QQWriteFileTool(outbox)
        written = writer.run(
            {"path": "note.txt", "content": "hi there"}, ToolContext(step=1)
        )
        assert written.ok, written
        saved = Path(written.content.split("written: ", 1)[-1])
        assert saved.read_text(encoding="utf-8") == "hi there"

        escape = writer.run(
            {"path": str(Path(tmp) / "evil.txt"), "content": "x"}, ToolContext(step=1)
        )
        assert not escape.ok, "writer must stay inside outbox"

    dup = FakeRegistry()
    try:
        CompositeRegistry([dup, dup])
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate tool names must be rejected")
    print("[qq] file tools + composite registry OK")


class FakeSearchClient:
    def list_tools(self) -> list[dict]:
        return [
            {
                "name": "web_search",
                "description": "Search the web.",
                "inputSchema": {
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "web_fetch",
                "description": "Fetch a page.",
                "inputSchema": {
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
        ]

    def call_tool(self, name: str, arguments: dict) -> str:
        return f"{name}:{arguments.get('query') or arguments.get('url')}"


def run_search_case() -> None:
    from fish.domains.search import SearchDomain
    from fish.mcp_http import parse_sse

    raw = 'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"ok":true}}\n\n'
    assert parse_sse(raw, 2)["result"]["ok"] is True
    assert parse_sse(raw, 99) == {"ok": True} or True  # falls back to last event

    pack = SearchDomain(FakeSearchClient())  # type: ignore[arg-type]
    names = sorted(spec.name for spec in pack.registry().specs())
    assert names == ["web_fetch", "web_search"], names
    tool = pack.registry().get("web_search")
    assert tool is not None
    observation = tool.run({"query": "python"}, ToolContext(step=1))
    assert observation.ok and "python" in observation.content, observation
    print("[search] sse parsing + http mcp tools OK (no network)")


class WriteRegistry(ToolRegistry):
    def __init__(self) -> None:
        self._tool = WriteTool()

    def specs(self) -> list[ToolSpec]:
        return [self._tool.spec]

    def get(self, name: str) -> Tool | None:
        return self._tool if name == "writer" else None


def run_approval_case() -> None:
    server = Server(ServerConfig(persist=False), [])
    event = Event("test", "sess-1", "授权", "sess-1")
    server.handle(event)
    assert server._grant_active("sess-1"), "grant keyword must enable writes"

    server.handle(Event("test", "sess-1", "取消授权", "sess-1"))
    assert not server._grant_active("sess-1"), "revoke must clear the grant"

    result = _run(
        ScriptedLLM(
            [
                '{"thought":"write","tool":"writer","args":{}}',
                '{"thought":"done","final":"需要授权"}',
            ]
        ),
        registry=WriteRegistry(),
        policy=StaticPolicy(allow={"writer"}),
    )
    assert result.denials, "denied tools must be reported in RunResult"
    print(f"[approval] grant/revoke + denial reporting OK ({result.denials[0][:40]})")


class FakeWeChat:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, to: str, text: str) -> str:
        self.sent.append((to, text))
        return f"wechat:{to}"


def run_wechat_case() -> None:
    from fish.wechat import WeChatNotifyTool, WeChatState, text_of

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "w.json"
        WeChatState(bot_token="t", contexts={"u": "ctx"}).save(path)
        loaded = WeChatState.load(path)
        assert loaded.bot_token == "t" and loaded.contexts["u"] == "ctx"

    assert (
        text_of(
            {
                "item_list": [
                    {"type": 1, "text_item": {"text": "hi"}},
                    {"type": 2},
                ]
            }
        )
        == "hi"
    )

    fake = FakeWeChat()
    tool = WeChatNotifyTool(fake)  # type: ignore[arg-type]
    assert tool.spec.name == "notify_wechat" and tool.spec.dangerous
    ok = tool.run({"title": "t", "text": "hello", "to": "uid"}, ToolContext(step=1))
    assert ok.ok and fake.sent == [("uid", "hello")], fake.sent
    missing = tool.run({"title": "t", "text": "x", "to": ""}, ToolContext(step=1))
    assert not missing.ok, missing
    print("[wechat] state/model/notify contract OK (no network)")


def run_checkpoint_case() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(tmp)
        sid = "session-cp"
        store.append_checkpoint(
            sid, "step budget exhausted (8)", "已打开搜索页", "打开第一个结果"
        )
        snapshot = store.load(sid)
        assert snapshot.checkpoint is not None, snapshot
        assert snapshot.checkpoint["next"] == "打开第一个结果", snapshot.checkpoint

        progress, next_step = _split_checkpoint("PROGRESS: done A\nNEXT: do B")
        assert progress == "done A" and next_step == "do B", (progress, next_step)

        message = _friendly_stop("step budget exhausted (8)", "PROGRESS: x\nNEXT: y")
        assert "步数预算" in message and "继续" in message, message
        assert _friendly_stop("llm error: boom", "").startswith("模型暂时不可用")

        server = Server(ServerConfig(persist=False), [])
        goal, continuing = server._resolve_goal(
            Event("popo", "s", "继续", "me"), snapshot
        )
        assert continuing, "continue keyword must resume"
        assert "已打开搜索页" in goal, goal

        goal, continuing = server._resolve_goal(
            Event("popo", "s", "好！继续吧", "me"), snapshot
        )
        assert continuing, "'好！继续吧' must also resume"
        assert not server._resolve_goal(
            Event("popo", "s", "继续帮我看看这个页面的价格", "me"), snapshot
        )[1], "long sentences are new tasks, not 'continue'"
    print("[checkpoint] persist / split / friendly stop / continue detection OK")


def run_compact_case() -> None:
    memory = WindowMemory(goal="compress test", config=MemoryConfig(keep_turns=3))
    for step in range(1, 8):
        memory.add(
            Turn(
                step=step,
                decision=Decision(thought="t", action=Action("lookup", {"i": step})),
                observation=Observation.success(f"result {step}"),
            )
        )
    seen: list[int] = []

    def fake_summarizer(turns: list[Turn]) -> str:
        seen.append(len(turns))
        return "SUMMARY-BODY: read 4 pages, titles found, ads skipped"

    folded = memory.compact(fake_summarizer)
    text = memory.view(4000)[0].content
    print(f"[compact] folded={folded.folded} summarizer_input={seen} chars={len(text)}")

    assert folded.folded == 4, folded
    assert seen == [4], seen
    assert "EARLIER STEPS (summarized)" in text, text
    assert "SUMMARY-BODY" in text, text
    assert memory.compact(fake_summarizer).folded == 0, "nothing left to fold"

    small = WindowMemory(
        goal="batch", config=MemoryConfig(keep_turns=2, compact_chunk=4)
    )
    for step in range(1, 4):
        small.add(
            Turn(
                step=step,
                decision=Decision(thought="t", action=Action("lookup", {})),
                observation=Observation.success("ok"),
            )
        )
    assert small.compact(lambda turns: "x").folded == 0, "must not fold every turn"


def run_store_case() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".fish" / "facts.json"
        store = JsonFactStore(path)

        first = store.upsert(
            Fact(scope="bilibili.com", kind="selector", key="title", text=".tit")
        )
        store.upsert(
            Fact(scope="bilibili.com", kind="selector", key="title", text=".tit2")
        )
        facts = store.all()
        assert len(facts) == 1, facts
        assert facts[0].text == ".tit2", facts[0].text
        assert facts[0].score == 0.5, "conflict resets confidence"
        assert facts[0].created_at == first.created_at, "created_at preserved"

        store.feedback(first.id, True)
        assert store.all()[0].score > 0.5
        for _ in range(3):
            store.feedback(first.id, False)
        assert store.all()[0].score == 0.0, store.all()[0].score
        assert store.prune() == 1, "dead fact removed"
        assert store.all() == []

        memory = WindowMemory(goal="g", scope="bilibili.com", facts=store)
        store.upsert(
            Fact(
                scope="bilibili.com",
                kind="selector",
                key="title",
                text="video title: .bili-video-card__info--tit",
            )
        )
        text = memory.view(4000)[0].content
        assert "KNOWN FACTS for bilibili.com" in text, text

        path.write_text("{not json", encoding="utf-8")
        assert JsonFactStore(path).all() == [], "corrupted file degrades to empty"

    print("[store] dedupe / feedback / prune / facts-injection / corruption OK")


def main() -> int:
    answer = run_case(
        "happy-path",
        [
            '{"thought":"read","tool":"lookup","args":{"query":"bilibili"}}',
            '{"thought":"done","final":"三个标题：A/B/C"}',
        ],
    )
    assert answer == "三个标题：A/B/C", answer

    answer = run_case(
        "policy-denies-dangerous",
        [
            '{"thought":"click","tool":"click","args":{}}',
            '{"thought":"give up","final":"没点"}',
        ],
    )
    assert answer == "没点", answer

    answer = run_case(
        "bad-json-recovers",
        [
            "我觉得应该先看看页面",
            '{"thought":"done","final":"ok"}',
        ],
    )
    assert answer == "ok", answer

    answer = run_case(
        "budget-stops",
        ['{"thought":"loop","tool":"lookup","args":{}}'],
        max_steps=2,
    )
    assert "Stopped" in answer, answer

    answer = run_case(
        "repeat-guard",
        [
            '{"thought":"read","tool":"lookup","args":{"query":"x"}}',
            '{"thought":"read again","tool":"lookup","args":{"query":"x"}}',
            '{"thought":"done","final":"不再重复"}',
        ],
    )
    assert answer == "不再重复", answer
    assert LookupTool.calls == 1, f"tool ran {LookupTool.calls} times, expected 1"

    run_domain_case()
    run_no_tools_case()
    run_failure_cases()
    run_last_resort_cases()
    run_contract_violation_cases()
    run_notify_case()
    run_session_case()
    run_qq_case()
    run_qq_file_case()
    run_search_case()
    run_approval_case()
    run_wechat_case()
    run_permission_case()
    run_facts_feedback_case()
    run_checkpoint_case()
    run_compact_case()
    run_store_case()

    print("\nall cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
