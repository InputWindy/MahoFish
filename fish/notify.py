"""Notification tools.

Hierarchy (three layers, one contract):

    Tool                      abstract: spec + run            (contracts.py)
      └── NotifyTool          fixes notification semantics    (this module)
            ├── ConsoleNotifyTool   just implements send()
            ├── FileNotifyTool
            └── PopoNotifyTool      (future)

``NotifyTool`` adds no contract: it only fills in the boilerplate every channel
would otherwise duplicate (naming, dangerous/idempotent flags, argument schema,
text clipping, error wrapping). Deleting it and inheriting ``Tool`` directly
would not require touching Agent, Policy or the domain protocol.
"""

from __future__ import annotations

import re
from abc import abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .contracts import Observation, Tool, ToolContext, ToolSpec

MAX_TEXT = 4000
_SAFE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_-]+")

PARAMETERS: dict[str, Any] = {
    "properties": {
        "title": {"type": "string"},
        "text": {"type": "string"},
        "to": {"type": "string"},
    },
    "required": ["title", "text"],
}


class NotifyTool(Tool):
    """One delivery channel. Subclasses only implement ``send``."""

    channel: str = "unknown"

    def __init__(self, destination: str = "") -> None:
        self._destination = destination

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=f"notify_{self.channel}",
            description=(
                f"Send a message via {self.channel}. "
                "Use it only when the result must leave the agent."
            ),
            parameters=PARAMETERS,
            dangerous=True,      # external side effect
            idempotent=False,    # repeated calls send twice
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        title = str(args.get("title") or "").strip()
        text = str(args.get("text") or "")
        to = str(args.get("to") or self._destination).strip()
        if not text.strip():
            return Observation.failure("text must not be empty")
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + "\n...[truncated]"
        try:
            target = self.send(title or "(no title)", text, to)
        except Exception as exc:  # a dead channel is a failure, never a crash
            return Observation.failure(f"{self.channel} send failed: {exc}")
        return Observation.success(f"sent via {self.channel} to {target}")

    @abstractmethod
    def send(self, title: str, text: str, to: str) -> str:
        """Deliver the message; return a human readable destination."""


class ConsoleNotifyTool(NotifyTool):
    channel = "console"

    def send(self, title: str, text: str, to: str) -> str:
        print(f"\n===== {title} =====\n{text}\n")
        return "console"


class FileNotifyTool(NotifyTool):
    channel = "file"

    def __init__(self, directory: str | Path = ".fish/reports") -> None:
        super().__init__(str(directory))
        self._dir = Path(directory)

    def send(self, title: str, text: str, to: str) -> str:
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = _SAFE.sub("-", title).strip("-")[:40] or "report"
        path = self._dir / f"{stamp}-{slug}.md"
        path.write_text(f"# {title}\n\n{text}\n", encoding="utf-8")
        return str(path)


def build_notify_tool(spec: str) -> NotifyTool:
    """``console`` | ``file`` | ``file:<dir>``."""
    kind, _, arg = spec.partition(":")
    kind = kind.strip().lower()
    if kind == "console":
        return ConsoleNotifyTool()
    if kind == "file":
        return FileNotifyTool(arg.strip() or ".fish/reports")
    raise ValueError(f"unknown notify channel {spec!r}; use console or file[:dir]")


def build_notify_tools(specs: Iterable[str]) -> list[NotifyTool]:
    return [build_notify_tool(spec) for spec in specs]
