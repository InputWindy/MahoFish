"""Offline self-test: exercises the loop with fake LLM/tools, no network.

    python eval/selftest.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fish.agent import Agent
from fish.contracts import (
    LLMBackend,
    Message,
    Observation,
    Reply,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolSpec,
)
from fish.memory import WindowMemory
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
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="lookup",
            description="Return canned page titles.",
            parameters={"properties": {"query": {"type": "string"}}, "required": []},
        )

    def run(self, args: dict, ctx: ToolContext) -> Observation:
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
    llm = ScriptedLLM(replies)
    memory = WindowMemory(goal="读取页面标题", keep_turns=2, observation_chars=200)
    policy = StaticPolicy(max_steps=max_steps, max_tokens=50_000)
    events: list[tuple[int, str, str]] = []
    agent = Agent(llm, FakeRegistry(), memory, policy, on_event=lambda *a: events.append(a))
    result = agent.run()
    print(f"[{name}] stop={result.stopped_reason} steps={result.steps}")
    print(f"[{name}] answer={result.answer!r}")
    return result.answer


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

    print("\nall cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
