"""Sliding-window memory: keeps recent turns verbatim, folds older ones into
a one-line ledger. No summarisation call yet - that is a strategy upgrade
behind the same ``Memory`` contract.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Sequence

from .contracts import Memory, Message, Role, Turn, estimate_tokens


class WindowMemory(Memory):
    def __init__(
        self,
        goal: str,
        keep_turns: int = 8,
        observation_chars: int = 1500,
    ) -> None:
        self._goal = goal
        self._keep = max(1, keep_turns)
        self._obs_chars = max(200, observation_chars)
        self._turns: list[Turn] = []

    @property
    def goal(self) -> str:
        return self._goal

    def add(self, turn: Turn) -> None:
        self._turns.append(turn)

    def reset(self) -> None:
        self._turns.clear()

    def view(self, budget_tokens: int) -> list[Message]:
        budget_chars = max(2000, budget_tokens * 3)
        recent = self._turns[-self._keep :]
        older = self._turns[: len(self._turns) - len(recent)]

        blocks: list[str] = []
        used = estimate_tokens(self._goal) * 3
        for turn in reversed(recent):
            block = self._render_turn(turn)
            size = len(block)
            if used + size > budget_chars and blocks:
                break
            blocks.append(block)
            used += size
        blocks.reverse()

        parts = [f"GOAL: {self._goal}"]
        if older:
            parts.append(f"EARLIER STEPS (folded): {self._ledger(older)}")
        if blocks:
            parts.append("ACTIONS SO FAR:\n\n" + "\n\n".join(blocks))
        else:
            parts.append("ACTIONS SO FAR:\n\n(no actions yet)")
        parts.append("Your next reply (one JSON object):")
        return [Message(role=Role.USER, content="\n\n".join(parts))]

    # -- internals ----------------------------------------------------------

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
        if len(body) > self._obs_chars:
            body = body[: self._obs_chars] + "...[truncated]"
        return f"{head}\nresult:\n{body}"

    @staticmethod
    def _ledger(turns: Sequence[Turn]) -> str:
        names: Counter[str] = Counter()
        for turn in turns:
            if turn.decision and turn.decision.action:
                names[turn.decision.action.tool] += 1
        used = ", ".join(f"{name}x{count}" for name, count in names.items()) or "none"
        return f"{len(turns)} steps, tools used: {used}"


def _compact(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 200 else text[:200] + "..."
