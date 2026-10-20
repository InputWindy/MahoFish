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
from fish.store import Fact, JsonFactStore
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


def run_no_tools_case() -> None:
    llm = ScriptedLLM(['{"thought":"no tools","final":"直接回答：42"}'])
    memory = WindowMemory(goal="告诉我答案")
    policy = StaticPolicy(max_steps=3, max_tokens=10_000)
    result = Agent(llm, NoToolsRegistry(), memory, policy).run()
    print(f"[no-tools] stop={result.stopped_reason} steps={result.steps}")
    assert result.answer == "直接回答：42", result.answer
    assert result.stopped_reason == "final", result.stopped_reason
    assert result.steps == 1, result.steps


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
    print(f"[compact] folded={folded} summarizer_input={seen} chars={len(text)}")

    assert folded == 4, folded
    assert seen == [4], seen
    assert "EARLIER STEPS (summarized)" in text, text
    assert "SUMMARY-BODY" in text, text
    assert memory.compact(fake_summarizer) == 0, "nothing left to fold"


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

    run_no_tools_case()
    run_compact_case()
    run_store_case()

    print("\nall cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
