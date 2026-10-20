"""The agent contract.

Everything in this project is an implementation of the five abstractions
declared here. The loop in ``fish/agent.py`` depends on nothing else, so any of
them can be replaced (different LLM, different tools, different memory
strategy, different policy) without touching the others.

    Agent = <LLM, Tools, Memory> x Loop(+Policy)
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Sequence


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class Message:
    role: Role
    content: str

    def render(self) -> str:
        return f"[{self.role.value}]\n{self.content}"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    dangerous: bool = False
    idempotent: bool = True

    def render(self) -> str:
        params = ", ".join(
            f"{key}{'' if key in (self.parameters.get('required') or []) else '?'}"
            for key in (self.parameters.get("properties") or {})
        )
        flags = []
        if self.dangerous:
            flags.append("dangerous")
        if not self.idempotent:
            flags.append("non-idempotent")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        return f"- {self.name}({params}){suffix}: {self.description}"


@dataclass(frozen=True)
class Action:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    """One parsed assistant turn: either an action or a final answer."""

    thought: str = ""
    action: Action | None = None
    final: str | None = None

    @property
    def is_final(self) -> bool:
        return self.final is not None


@dataclass(frozen=True)
class Observation:
    ok: bool
    content: str
    error: str | None = None

    @classmethod
    def success(cls, content: str) -> "Observation":
        return cls(True, content)

    @classmethod
    def failure(cls, error: str) -> "Observation":
        return cls(False, f"ERROR: {error}", error)


@dataclass(frozen=True)
class Reply:
    text: str
    tokens: int = 0


@dataclass(frozen=True)
class Usage:
    steps: int = 0
    tokens: int = 0
    seconds: float = 0.0

    def plus(self, other: "Usage") -> "Usage":
        return Usage(
            self.steps + other.steps,
            self.tokens + other.tokens,
            self.seconds + other.seconds,
        )


@dataclass(frozen=True)
class Turn:
    """One completed loop iteration, as remembered."""

    step: int
    decision: Decision | None
    observation: Observation | None


# Injected by the loop so Memory never depends on an LLM implementation.
Summarizer = Callable[[Sequence[Turn]], str]


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""

    @classmethod
    def allow(cls) -> "Verdict":
        return cls(True)

    @classmethod
    def deny(cls, reason: str) -> "Verdict":
        return cls(False, reason)


@dataclass(frozen=True)
class RunResult:
    answer: str
    steps: int
    usage: Usage
    stopped_reason: str


class ToolContext:
    """Per-call execution context handed to tools."""

    def __init__(self, step: int = 0, timeout: float | None = None) -> None:
        self.step = step
        self.timeout = timeout


# -- the five contracts -----------------------------------------------------


class LLMBackend(ABC):
    name: str = "unknown"

    @abstractmethod
    def complete(self, messages: Sequence[Message]) -> Reply:
        """Return the next assistant turn for the given conversation."""


class Tool(ABC):
    @property
    @abstractmethod
    def spec(self) -> ToolSpec:
        ...

    @abstractmethod
    def run(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        ...


class ToolRegistry(ABC):
    @abstractmethod
    def specs(self) -> list[ToolSpec]:
        ...

    @abstractmethod
    def get(self, name: str) -> Tool | None:
        ...


class Memory(ABC):
    @abstractmethod
    def add(self, turn: Turn) -> None:
        ...

    @abstractmethod
    def view(self, budget_tokens: int) -> list[Message]:
        """Compressed conversation for the next LLM call. Must be pure."""

    @abstractmethod
    def compact(self, summarizer: Summarizer) -> int:
        """Fold old turns into the rolling summary; return how many folded."""

    @abstractmethod
    def reset(self) -> None:
        ...


class Policy(ABC):
    @property
    @abstractmethod
    def max_tokens(self) -> int:
        """Total token budget for one run; also caps single-call context."""

    @abstractmethod
    def check(self, spec: ToolSpec, args: dict[str, Any]) -> Verdict:
        ...

    @abstractmethod
    def spend(self, usage: Usage) -> None:
        ...

    @abstractmethod
    def exhausted(self) -> tuple[bool, str]:
        """(stopped, reason) - budget already blown?"""


# -- the wire protocol between LLM and loop ---------------------------------

PROTOCOL_HINT = """\
Reply with EXACTLY ONE JSON object and nothing else (no prose, no fences):
  {"thought": "<one short sentence>", "tool": "<tool_name>", "args": {...}}
When the goal is reached:
  {"thought": "<one short sentence>", "final": "<answer for the user>"}
"""


def parse_decision(text: str) -> Decision | None:
    """Extract a Decision from model output; None if unusable."""
    payload = _first_json_object(text)
    if payload is None:
        return None
    thought = str(payload.get("thought", "")).strip()
    if "final" in payload:
        return Decision(thought=thought, final=str(payload["final"]).strip())
    tool = str(payload.get("tool", "")).strip()
    if not tool:
        return None
    raw_args = payload.get("args")
    args = raw_args if isinstance(raw_args, dict) else {}
    return Decision(thought=thought, action=Action(tool=tool, args=args))


def estimate_tokens(text: str) -> int:
    """Cheap heuristic: ~3 chars per token for mixed CJK/latin text."""
    return max(1, len(text) // 3)


def _first_json_object(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
