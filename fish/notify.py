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

import json
import os
import re
import shutil
from abc import abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .contracts import Observation, Tool, ToolContext, ToolSpec
from .mcp import MCPError, StdioMCPClient

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


class PopoNotifyTool(NotifyTool):
    """Sends through the locally configured ``broccoli-helper`` MCP server.

    The helper is a stdio MCP server listed in ``~/.codemaker/mcps.json``; we
    reuse its launch command instead of hardcoding paths or credentials.
    """

    channel = "popo"

    def __init__(
        self,
        receiver: str = "",
        command: list[str] | None = None,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(receiver)
        self._receiver = receiver or os.environ.get("FISH_POPO_RECEIVER", "")
        self._command = command or popo_helper_command()
        self._timeout = timeout

    def send(self, title: str, text: str, to: str) -> str:
        if not self._command:
            raise RuntimeError(
                "popo helper not configured: add 'broccoli-helper' to "
                "~/.codemaker/mcps.json or set FISH_POPO_HELPER"
            )
        payload: dict[str, Any] = {"title": title, "content": text}
        receiver = to or self._receiver
        if receiver:
            payload["receiver"] = receiver
        try:
            with StdioMCPClient(self._command, timeout=self._timeout) as client:
                reply = client.call_tool("send_popo_notification", payload)
        except MCPError as exc:
            raise RuntimeError(f"popo helper failed: {exc}") from exc
        return receiver or "current user"


def popo_helper_command() -> list[str] | None:
    """Launch command for the POPO helper MCP server, from CodeMaker's config."""
    override = os.environ.get("FISH_POPO_HELPER", "").strip()
    if override:
        return [override]
    config = Path.home() / ".codemaker" / "mcps.json"
    if not config.is_file():
        return None
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    servers = data.get("mcpServers", data)
    entry = servers.get("broccoli-helper") if isinstance(servers, dict) else None
    if not isinstance(entry, dict) or entry.get("type") not in (None, "stdio"):
        return None
    command = entry.get("command")
    if not command:
        return None
    argv = [str(command), *[str(arg) for arg in entry.get("args") or []]]
    if shutil.which(argv[0]) is None:
        return None
    return argv


def build_notify_tool(spec: str) -> NotifyTool:
    """``console`` | ``file`` | ``file:<dir>`` | ``popo`` | ``popo:<email>``."""
    kind, _, arg = spec.partition(":")
    kind = kind.strip().lower()
    if kind == "console":
        return ConsoleNotifyTool()
    if kind == "file":
        return FileNotifyTool(arg.strip() or ".fish/reports")
    if kind == "popo":
        return PopoNotifyTool(receiver=arg.strip())
    raise ValueError(f"unknown notify channel {spec!r}; use console, file[:dir] or popo[:email]")


def build_notify_tools(specs: Iterable[str]) -> list[NotifyTool]:
    return [build_notify_tool(spec) for spec in specs]
