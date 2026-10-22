"""Chrome DevTools MCP as an implementation of ``contracts.ToolRegistry``."""

from __future__ import annotations

import re
from typing import Any

from .contracts import (
    Observation,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolSpec,
)
from .mcp import StdioMCPClient

# Tools exposed to the model. Everything else the server offers is hidden so
# the prompt stays small. Order matters: it is the order shown to the model.
ALLOWED_TOOLS = (
    "navigate_page",
    "list_pages",
    "select_page",
    "take_snapshot",
    "evaluate_script",
    "click",
    "fill",
    "wait_for",
    "take_screenshot",
)

MAX_RESULT_CHARS = 6000


class MCPTool(Tool):
    def __init__(
        self,
        client: StdioMCPClient,
        raw: dict[str, Any],
        registry: "MCPToolRegistry",
        override: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._raw = raw
        self._registry = registry
        self._override = dict(override or {})
        schema = dict(raw.get("inputSchema") or {})
        self._requires_page_id = "pageId" in (schema.get("required") or [])

    @property
    def spec(self) -> ToolSpec:
        name = str(self._raw.get("name", ""))
        schema = dict(self._raw.get("inputSchema") or {})
        params = dict(schema.get("properties") or {})
        params.pop("pageId", None)
        required = [r for r in (schema.get("required") or []) if r != "pageId"]
        return ToolSpec(
            name=name,
            description=" ".join(str(self._raw.get("description", "")).split()),
            parameters={"properties": params, "required": required},
            dangerous=bool(self._override.get("dangerous", False)),
            idempotent=bool(self._override.get("idempotent", True)),
            permissions=frozenset(self._override.get("permissions") or ()),
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        payload = dict(args)
        if self._requires_page_id and "pageId" not in payload:
            page_id = self._registry.resolve_page_id()
            if page_id is not None:
                payload["pageId"] = page_id
        try:
            text = self._client.call_tool(self.spec.name, payload)
        except Exception as exc:
            return Observation.failure(str(exc))
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n...[truncated]"
        return Observation.success(text)


class MCPToolRegistry(ToolRegistry):
    def __init__(
        self,
        client: StdioMCPClient,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._client = client
        self._tools: dict[str, MCPTool] = {}
        self._page_id: int | None = None
        mapping = dict(overrides or {})
        for raw in client.list_tools():
            name = str(raw.get("name", ""))
            if name in ALLOWED_TOOLS:
                self._tools[name] = MCPTool(client, raw, self, mapping.get(name))

    def specs(self) -> list[ToolSpec]:
        return [
            self._tools[name].spec for name in ALLOWED_TOOLS if name in self._tools
        ]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    # -- helpers ------------------------------------------------------------

    def resolve_page_id(self) -> int | None:
        if self._page_id is not None:
            return self._page_id
        try:
            text = self._client.call_tool("list_pages", {})
        except Exception:
            return None
        fallback: int | None = None
        for line in text.splitlines():
            match = re.match(r"\s*(\d+):\s", line)
            if not match:
                continue
            page_id = int(match.group(1))
            if "[selected]" in line:
                self._page_id = page_id
                return page_id
            if fallback is None:
                fallback = page_id
        self._page_id = fallback or 1
        return self._page_id


class LocalRegistry(ToolRegistry):
    """Registry over in-process tools (not MCP-backed)."""

    def __init__(self, tools: list[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            name = tool.spec.name
            if name in self._tools:
                raise ValueError(f"duplicate tool name {name!r}")
            self._tools[name] = tool

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)


class EmptyRegistry(ToolRegistry):
    """Domain-free core: no tools, loop only produces final answers."""

    def specs(self) -> list[ToolSpec]:
        return []

    def get(self, name: str) -> Tool | None:
        return None


def build_chrome_client(
    node_command: list[str] | None = None,
    headless: bool = False,
    timeout: float = 120.0,
) -> StdioMCPClient:
    """Spawn a local `chrome-devtools-mcp` server over stdio."""
    if node_command is None:
        node_command = ["npx.cmd", "-y", "chrome-devtools-mcp@1.11.0"]
    args = list(node_command)
    if headless:
        args.append("--headless")
    return StdioMCPClient(args, timeout=timeout)
