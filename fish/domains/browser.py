"""Chrome DevTools MCP domain: tools, prompt hints and URL scope."""

from __future__ import annotations

import re

from ..contracts import ToolRegistry
from ..domain import DomainCapabilities, DomainPack
from ..tools import MCPToolRegistry, build_chrome_client

_URL = re.compile(r"https?://([^/\s]+)")

HINTS = """\
- `evaluate_script` is the cheapest way to read page content: pass a JS arrow
  function, e.g. {"function": "() => document.title"}.
- Prefer reading the DOM over screenshots.
"""


class BrowserDomain(DomainPack):
    name = "browser"
    capabilities = DomainCapabilities(tools=True, hints=True, scope=True)

    def __init__(self, headless: bool = False) -> None:
        self._client = build_chrome_client(headless=headless)
        self._registry = MCPToolRegistry(self._client)

    def registry(self) -> ToolRegistry:
        return self._registry

    def hints(self) -> str:
        return HINTS

    def scope_for(self, goal: str) -> str | None:
        match = _URL.search(goal)
        return match.group(1).lower() if match else None

    def close(self) -> None:
        self._client.close()
