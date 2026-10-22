"""Search domain: web_search / web_fetch from a streamable-HTTP MCP server.

Reuses the server already configured in ~/.codemaker/mcps.json (no extra API
key). Override with FISH_SEARCH_MCP_URL / FISH_SEARCH_MCP_HEADERS (JSON).
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..contracts import Observation, Tool, ToolContext, ToolSpec
from ..domain import DomainCapabilities, DomainError, DomainPack
from ..mcp_http import HttpMCPClient, from_codemaker_config
from ..tools import LocalRegistry

ALLOWED = ("web_search", "web_fetch")
SERVER_NAME = "Websearch MCP Server"


def default_client() -> HttpMCPClient:
    url = os.environ.get("FISH_SEARCH_MCP_URL", "").strip()
    headers: dict[str, str] = {}
    raw_headers = os.environ.get("FISH_SEARCH_MCP_HEADERS", "").strip()
    if raw_headers:
        try:
            headers = {str(k): str(v) for k, v in json.loads(raw_headers).items()}
        except json.JSONDecodeError as exc:
            raise DomainError(f"FISH_SEARCH_MCP_HEADERS is not JSON: {exc}") from exc
    if not url:
        found = from_codemaker_config(SERVER_NAME)
        if found is None:
            raise DomainError(
                f"no search MCP server: set FISH_SEARCH_MCP_URL or configure "
                f"{SERVER_NAME!r} in ~/.codemaker/mcps.json"
            )
        url, config_headers = found
        headers = headers or config_headers
    return HttpMCPClient(url, headers)


class HttpMCPTool(Tool):
    def __init__(self, client: HttpMCPClient, raw: dict[str, Any]) -> None:
        self._client = client
        self._raw = raw

    @property
    def spec(self) -> ToolSpec:
        schema = dict(self._raw.get("inputSchema") or {})
        params = dict(schema.get("properties") or {})
        return ToolSpec(
            name=str(self._raw.get("name", "")),
            description=" ".join(str(self._raw.get("description", "")).split()),
            parameters={
                "properties": params,
                "required": list(schema.get("required") or []),
            },
            permissions=frozenset({"network"}),
        )

    def run(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        try:
            return Observation.success(self._client.call_tool(self.spec.name, args))
        except Exception as exc:
            return Observation.failure(f"{self.spec.name} failed: {exc}")


class SearchDomain(DomainPack):
    name = "search"
    capabilities = DomainCapabilities(tools=True)

    def __init__(self, client: HttpMCPClient | None = None) -> None:
        self._client = client or default_client()
        tools = [
            HttpMCPTool(self._client, raw)
            for raw in self._client.list_tools()
            if str(raw.get("name")) in ALLOWED
        ]
        if not tools:
            raise DomainError("search MCP server exposes none of web_search/web_fetch")
        self._registry = LocalRegistry(tools)

    def registry(self) -> LocalRegistry:
        return self._registry


def available() -> bool:
    try:
        default_client()
    except DomainError:
        return False
    return True
