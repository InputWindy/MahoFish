"""HTTP API: let other systems (DSH, scripts, cron) submit one episode.

    POST /v1/run    {"goal": "...", "domain": "browser", "backend": "deepseek"}
    GET  /v1/health

Auth: if FISH_API_TOKEN is set, callers must send ``Authorization: Bearer``.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .llm import load_dotenv
from .runtime import EpisodeConfig, run_episode

Runner = Callable[[EpisodeConfig], Any]


def handle_payload(
    payload: dict[str, Any],
    runner: Runner = run_episode,
) -> tuple[int, dict[str, Any]]:
    goal = str(payload.get("goal") or "").strip()
    if not goal:
        return 400, {"error": "goal is required"}
    config = EpisodeConfig(
        goal=goal,
        domain=str(payload.get("domain") or "browser"),
        domain_options=dict(payload.get("domain_options") or {"headless": True}),
        backend=str(payload.get("backend") or "deepseek"),
        model=payload.get("model"),
        max_steps=min(int(payload.get("max_steps") or 12), 40),
        max_tokens=min(int(payload.get("max_tokens") or 120_000), 1_000_000),
        timeout=min(float(payload.get("timeout") or 300), 1800),
        allow_dangerous=bool(payload.get("allow_dangerous", False)),
    )
    try:
        result = runner(config)
    except Exception as exc:
        return 500, {"error": f"{type(exc).__name__}: {exc}"}
    return 200, {
        "answer": result.answer,
        "steps": result.steps,
        "tokens": result.usage.tokens,
        "seconds": round(result.usage.seconds, 1),
        "stopped_reason": result.stopped_reason,
        "faults": [
            {"step": f.step, "source": f.source, "message": f.message}
            for f in result.faults
        ],
        "denials": list(result.denials),
    }


class Handler(BaseHTTPRequestHandler):
    runner: Runner = staticmethod(run_episode)
    token: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default
        print(f"[api] {self.address_string()} {fmt % args}", flush=True)

    def _authorized(self) -> bool:
        if not self.token:
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {self.token}"

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/v1/health":
            self._reply(404, {"error": "not found"})
            return
        self._reply(200, {"ok": True, "auth": bool(self.token)})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/v1/run":
            self._reply(404, {"error": "not found"})
            return
        if not self._authorized():
            self._reply(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, json.JSONDecodeError):
            self._reply(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._reply(400, {"error": "payload must be an object"})
            return
        status, body = handle_payload(payload, type(self).runner)
        self._reply(status, body)


def make_server(host: str, port: int, runner: Runner, token: str) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"runner": staticmethod(runner), "token": token})
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    import argparse

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    parser = argparse.ArgumentParser(description="MahoFish HTTP API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--token", default=os.environ.get("FISH_API_TOKEN", ""))
    args = parser.parse_args(argv)

    server = make_server(args.host, args.port, run_episode, args.token)
    print(f"[api] listening on http://{args.host}:{args.port} auth={bool(args.token)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[api] stopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
