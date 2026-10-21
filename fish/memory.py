"""Sliding-window memory with an explicit compaction step.

RECENT turns stay verbatim; older ones are folded by ``compact()`` using an
injected summarizer, so this module never imports an LLM. FACTS are handled by
``fish/store.py`` (see docs/contracts.md C8/C9).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Sequence

from .contracts import (
    CompactionResult,
    Memory,
    Message,
    Role,
    Summarizer,
    Summary,
    Turn,
    estimate_tokens,
)
from .store import Fact, FactStore


@dataclass(frozen=True)
class MemoryConfig:
    keep_turns: int = 8
    observation_chars: int = 1500
    summary_ratio: float = 0.25
    facts_ratio: float = 0.15
    facts_limit: int = 8
    max_turns: int = 200


class WindowMemory(Memory):
    def __init__(
        self,
        goal: str,
        config: MemoryConfig | None = None,
        scope: str | None = None,
        facts: FactStore | None = None,
    ) -> None:
        self._goal = goal
        self._config = config or MemoryConfig()
        self._scope = scope
        self._facts = facts
        self._turns: list[Turn] = []
        self._summary: str = ""
        self._folded = 0
        self._folded_tools: Counter[str] = Counter()

    @property
    def goal(self) -> str:
        return self._goal

    @property
    def scope(self) -> str | None:
        return self._scope

    @property
    def summary(self) -> str:
        return self._summary

    def set_goal(self, goal: str) -> None:
        self._goal = goal

    def seed(self, summary: str = "", turns: Sequence[Turn] = (), folded: int = 0) -> None:
        """Restore a persisted session before continuing it."""
        if summary:
            self._summary = summary
        if folded:
            self._folded = max(self._folded, folded)
        for turn in turns:
            self._turns.append(turn)
        if len(self._turns) > self._config.max_turns:
            del self._turns[: len(self._turns) - self._config.max_turns]

    @property
    def folded(self) -> int:
        return self._folded

    def add(self, turn: Turn) -> None:
        self._turns.append(turn)
        while len(self._turns) > self._config.max_turns:
            self._evict(self._turns.pop(0))

    def compact(self, summarizer: Summarizer) -> CompactionResult:
        keep = max(1, self._config.keep_turns)
        if len(self._turns) <= keep:
            return CompactionResult()
        older = self._turns[: len(self._turns) - keep]
        produced = summarizer(older)
        if isinstance(produced, Summary):
            text, tokens = produced.text.strip(), produced.tokens
        else:
            text, tokens = str(produced).strip(), 0
        if text:
            self._summary = f"{self._summary}\n{text}".strip() if self._summary else text
        for turn in older:
            self._evict(turn)
        del self._turns[: len(older)]
        return CompactionResult(folded=len(older), tokens=tokens)

    def view(self, budget_tokens: int) -> list[Message]:
        budget_chars = max(2000, budget_tokens * 3)
        facts_budget = int(budget_chars * self._config.facts_ratio)
        summary_budget = int(budget_chars * self._config.summary_ratio)

        parts = [f"GOAL: {self._goal}"]
        facts_block = self._render_facts(facts_budget)
        if facts_block:
            parts.append(facts_block)
        head = self._render_summary(summary_budget)
        if head:
            parts.append(head)

        blocks: list[str] = []
        used = sum(len(p) for p in parts)
        for turn in reversed(self._turns):
            block = self._render_turn(turn)
            if used + len(block) > budget_chars and blocks:
                break
            blocks.append(block)
            used += len(block)
        blocks.reverse()
        if blocks:
            parts.append("ACTIONS SO FAR:\n\n" + "\n\n".join(blocks))
        else:
            parts.append("ACTIONS SO FAR:\n\n(no actions yet)")
        parts.append("Your next reply (one JSON object):")
        return [Message(role=Role.USER, content="\n\n".join(parts))]

    def reset(self) -> None:
        self._turns.clear()
        self._summary = ""
        self._folded = 0
        self._folded_tools.clear()

    def context_tokens(self, budget_tokens: int) -> int:
        return estimate_tokens(self.view(budget_tokens)[0].content)

    # -- internals ----------------------------------------------------------

    def _evict(self, turn: Turn) -> None:
        self._folded += 1
        action = turn.decision.action if turn.decision else None
        if action is not None:
            self._folded_tools[action.tool] += 1

    def _render_facts(self, limit: int) -> str:
        if self._facts is None or not self._scope:
            return ""
        try:
            facts: list[Fact] = self._facts.query(
                self._scope, limit=self._config.facts_limit
            )
        except Exception:
            return ""  # facts are an accelerator, never a blocker
        if not facts:
            return ""
        header = f"KNOWN FACTS for {self._scope}:"
        lines = [header]
        used = len(header)
        for fact in facts:
            line = f"- {fact.text}"
            if used + len(line) > limit and len(lines) > 1:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    def _render_summary(self, limit: int) -> str:
        if self._summary:
            text = self._summary
            if len(text) > limit:
                text = text[-limit:]
            return f"EARLIER STEPS (summarized):\n{text}"
        if self._folded:
            tools = ", ".join(f"{n}x{c}" for n, c in self._folded_tools.items()) or "-"
            return f"EARLIER STEPS (folded): {self._folded} steps, tools used: {tools}"
        return ""

    def _render_turn(self, turn: Turn) -> str:
        decision = turn.decision
        if decision is None:
            return f"step {turn.step}: (no decision recorded)"
        if decision.is_final:
            return f"step {turn.step}: final={decision.final}"
        action = decision.action
        tool = action.tool if action else "?"
        args = action.args if action else {}
        head = f"step {turn.step}: tool={tool} args={_compact(args)}"
        obs = turn.observation
        if obs is None:
            return head
        body = obs.content
        limit = self._config.observation_chars
        if len(body) > limit:
            body = body[:limit] + "...[truncated]"
        return f"{head}\nresult:\n{body}"


def _compact(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 200 else text[:200] + "..."
