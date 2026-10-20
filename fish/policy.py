"""Static policy: name allow/deny lists plus hard budgets.

Deliberately dumb and dependency-free. An interactive policy (ask the human
before a dangerous tool) implements the same ``Policy`` contract.
"""

from __future__ import annotations

from typing import Any

from .contracts import Policy, ToolSpec, Usage, Verdict


class StaticPolicy(Policy):
    def __init__(
        self,
        allow: set[str] | None = None,
        deny: set[str] | None = None,
        allow_dangerous: bool = False,
        max_steps: int = 12,
        max_tokens: int = 200_000,
        deadline_seconds: float = 1800.0,
    ) -> None:
        self._allow = allow
        self._deny = deny or set()
        self._allow_dangerous = allow_dangerous
        self._max_steps = max_steps
        self._max_tokens = max_tokens
        self._deadline = deadline_seconds
        self._usage = Usage()

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    def check(self, spec: ToolSpec, args: dict[str, Any]) -> Verdict:
        if spec.name in self._deny:            return Verdict.deny(f"tool {spec.name!r} is on the deny list")
        if self._allow is not None and spec.name not in self._allow:
            return Verdict.deny(f"tool {spec.name!r} is not on the allow list")
        if spec.dangerous and not self._allow_dangerous:
            return Verdict.deny(
                f"tool {spec.name!r} mutates the page; pass --allow-dangerous"
            )
        return Verdict.allow()

    def spend(self, usage: Usage) -> None:
        self._usage = usage

    def exhausted(self) -> tuple[bool, str]:
        if self._usage.steps >= self._max_steps:
            return True, f"step budget exhausted ({self._max_steps})"
        if self._usage.tokens >= self._max_tokens:
            return True, f"token budget exhausted ({self._max_tokens})"
        if self._usage.seconds >= self._deadline:
            return True, f"time budget exhausted ({self._deadline:.0f}s)"
        return False, ""

    @property
    def usage(self) -> Usage:
        return self._usage
