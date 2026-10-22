"""Minimal MCP client over streamable HTTP (JSON-RPC + SSE responses).

Used to reuse an already configured search MCP server instead of asking for a
new API key. Only initialize / tools/list / tools/call are implemented.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ACCEPT = "application/json, text/event-stream"


class HttpMCPError(RuntimeError):
    pass


def parse_sse(raw: str, msg_id: int | None) -> dict[str, Any]:
    """Return the JSON-RPC object matching msg_id from an SSE or plain body."""
    text = raw.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise HttpMCPError(f"bad json: {raw[:200]!r}") from exc
    fallback: dict[str, Any] | None = None
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if not payload:
            continue
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        if msg_id is None or message.get("id") == msg_id:
            return message
        fallback = fallback or message
    return fallback or {}


class HttpMCPClient:
    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 40.0,
    ) -> None:
        self._url = url
        self._headers = dict(headers or {})
        self._timeout = timeout
        self._next_id = 0
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        self._post(
            {
                "jsonrpc": "2.0",
                "id": self._id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "maho-fish", "version": "0.1.0"},
                },
            }
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        result = self._request("tools/list", {})
        return list(result.get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.initialize()
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        return _render(result)

    # -- internals ----------------------------------------------------------

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        msg_id = self._id()
        message = self._post(
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params},
            expect=msg_id,
        )
        if "error" in message:
            raise HttpMCPError(f"{method} failed: {message['error']}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def _post(
        self, payload: dict[str, Any], expect: int | None = None
    ) -> dict[str, Any]:
        headers = {
            **self._headers,
            "Content-Type": "application/json",
            "Accept": ACCEPT,
        }
        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise HttpMCPError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise HttpMCPError(f"cannot reach {self._url}: {exc.reason}") from exc
        if not raw.strip():
            return {}
        return parse_sse(raw, expect if expect is not None else payload.get("id"))


def _render(result: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            chunks.append(str(item.get("text", "")))
    if not chunks and result.get("structuredContent") is not None:
        chunks.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(c for c in chunks if c)
    if result.get("isError"):
        return f"ERROR: {text}"
    return text or "(empty result)"


def from_codemaker_config(server_name: str) -> tuple[str, dict[str, str]] | None:
    """Read url + headers for an MCP server listed in ~/.codemaker/mcps.json."""
    config = Path.home() / ".codemaker" / "mcps.json"
    if not config.is_file():
        return None
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    servers = data.get("mcpServers", data)
    entry = servers.get(server_name) if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        return None
    url = entry.get("url")
    if not url:
        return None
    headers = {
        str(key): str(value) for key, value in (entry.get("headers") or {}).items()
    }
    return str(url), headers
