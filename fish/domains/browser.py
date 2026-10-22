"""Chrome DevTools MCP domain: tools, prompt hints and URL scope."""

from __future__ import annotations

import re

from ..contracts import ToolRegistry
from ..domain import DomainCapabilities, DomainPack
from ..tools import MCPToolRegistry, build_chrome_client

_URL = re.compile(r"https?://([^/\s]+)")

# Domain knowledge lives here, not in fish/tools.py: which browser tool writes,
# which one is non-idempotent, what capability it needs.
BROWSER_OVERRIDES: dict[str, dict[str, object]] = {
    "navigate_page": {"permissions": {"network"}, "idempotent": False},
    "list_pages": {"permissions": {"read"}},
    "select_page": {"permissions": {"read"}},
    "take_snapshot": {"permissions": {"read"}},
    "evaluate_script": {"permissions": {"exec"}},
    "click": {"permissions": {"write"}, "dangerous": True, "idempotent": False},
    "fill": {"permissions": {"write"}, "dangerous": True, "idempotent": False},
    "wait_for": {"permissions": {"read"}},
    "take_screenshot": {"permissions": {"read"}},
}

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
        self._registry = MCPToolRegistry(self._client, BROWSER_OVERRIDES)

    def registry(self) -> ToolRegistry:
        return self._registry

    def hints(self) -> str:
        return HINTS

    def scope_for(self, goal: str) -> str | None:
        match = _URL.search(goal)
        return match.group(1).lower() if match else None

    def close(self) -> None:
        self._client.close()
