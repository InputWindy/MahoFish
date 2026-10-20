"""Minimal MCP (Model Context Protocol) stdio client.

Speaks newline-delimited JSON-RPC 2.0 to a locally spawned MCP server, which is
all this demo needs: initialize / tools/list / tools/call.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from typing import Any


class MCPError(RuntimeError):
    pass


class StdioMCPClient:
    def __init__(
        self,
        command: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not command:
            raise MCPError("empty command")
        self._timeout = timeout
        resolved = shutil.which(command[0])
        if resolved is None:
            raise MCPError(f"executable not found: {command[0]}")
        argv = [resolved, *command[1:]]

        merged_env = os.environ.copy()
        merged_env.update(env or {})
        merged_env.setdefault("PYTHONUNBUFFERED", "1")

        self._proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=merged_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._next_id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._stderr_tail: list[str] = []
        self._closed = False

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "maho-fish", "version": "0.1.0"},
            },
        )
        self._notify("notifications/initialized", {})

    # -- public API ---------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            result = self._request("tools/list", params)
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = self._request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        return _render_tool_result(result)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._proc.stdin.close()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()

    def __enter__(self) -> "StdioMCPClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- internals ----------------------------------------------------------

    def _read_stdout(self) -> None:
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" not in message:
                continue  # server-initiated notification
            msg_id = message.get("id")
            if not isinstance(msg_id, int):
                continue
            with self._lock:
                slot = self._pending.pop(msg_id, None)
            if slot is None:
                continue
            event, holder = slot
            holder.update(message)
            event.set()

    def _read_stderr(self) -> None:
        assert self._proc.stderr is not None
        for raw in self._proc.stderr:
            line = raw.rstrip()
            if not line:
                continue
            self._stderr_tail.append(line)
            del self._stderr_tail[:-40]

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            slot = (threading.Event(), {})
            self._pending[msg_id] = slot
        self._write(
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        )
        event, holder = slot
        if not event.wait(self._timeout):
            with self._lock:
                self._pending.pop(msg_id, None)
            raise MCPError(
                f"timeout waiting for {method!r} response; "
                f"server stderr tail: {self._stderr_tail[-5:]}"
            )
        if "error" in holder:
            raise MCPError(f"{method} failed: {holder['error']}")
        result = holder.get("result")
        return result if isinstance(result, dict) else {}

    def _write(self, payload: dict[str, Any]) -> None:
        if self._closed or self._proc.poll() is not None:
            raise MCPError(
                "MCP server is not running; stderr tail: " f"{self._stderr_tail[-5:]}"
            )
        line = json.dumps(payload, ensure_ascii=False)
        assert self._proc.stdin is not None
        self._proc.stdin.write(line + "\n")
        self._proc.stdin.flush()


def _render_tool_result(result: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in result.get("content", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            chunks.append(str(item.get("text", "")))
        elif item.get("type") == "image":
            mime = item.get("mimeType", "image/*")
            chunks.append(f"[image omitted: {mime}]")
        else:
            chunks.append(json.dumps(item, ensure_ascii=False))
    if not chunks and result.get("structuredContent") is not None:
        chunks.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(c for c in chunks if c)
    if result.get("isError"):
        return f"ERROR: {text}"
    return text or "(empty result)"
