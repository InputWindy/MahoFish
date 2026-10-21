"""Agent server: listeners -> queue -> one worker -> reply to the source.

Threading model:
* one thread per listener (I/O bound polling)
* one worker thread (the main loop) processing events sequentially, which keeps
  per-session state single-writer and avoids concurrent fact-store writes
"""

from __future__ import annotations

import argparse
import queue
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .contracts import ToolContext
from .listeners import Event, Listener, PopoListener, current_user
from .memory import WindowMemory
from .notify import PopoNotifyTool
from .runtime import EpisodeConfig, run_episode
from .session_store import SessionStore, TurnPersister
from .store import JsonFactStore

ROOT = Path(__file__).resolve().parents[1]
_URL = re.compile(r"https?://([^/\s]+)")


@dataclass
class ServerConfig:
    domain: str = "browser"
    backend: str = "deepseek"
    max_steps: int = 8
    timeout: float = 300.0
    allow_dangerous: bool = False
    persist: bool = True
    dry_run: bool = False
    verbose: bool = False
    facts_path: Path | None = None
    sessions_dir: Path | None = None


class Server:
    def __init__(self, config: ServerConfig, listeners: Iterable[Listener]) -> None:
        self._config = config
        self._listeners = list(listeners)
        self._queue: queue.Queue[Event] = queue.Queue()
        self._store = SessionStore(
            config.sessions_dir or (ROOT / ".fish" / "sessions"),
            persist=config.persist,
        )
        self._facts = config.facts_path or (ROOT / ".fish" / "facts.json")

    # -- lifecycle ----------------------------------------------------------

    def serve_forever(self) -> int:
        for listener in self._listeners:
            listener.start(self._queue.put)
            print(f"[server] listener started: {listener.name}", flush=True)
        try:
            while True:
                event = self._queue.get()
                try:
                    self.handle(event)
                except Exception as exc:  # one bad event must not kill the server
                    print(f"[server] event failed: {exc}", file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            print("\n[server] stopping", flush=True)
            return 0
        finally:
            for listener in self._listeners:
                listener.stop()

    def drain_once(self) -> int:
        """Poll every listener once, then handle what arrived."""
        for listener in self._listeners:
            poll = getattr(listener, "poll_once", None)
            if callable(poll):
                poll(self._queue.put)
        handled = 0
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                return handled
            self.handle(event)
            handled += 1

    # -- event handling -----------------------------------------------------

    def handle(self, event: Event) -> str:
        preview = event.text.replace("\n", " ")[:60]
        print(f"[server] {event.channel} <- {preview}", flush=True)
        if self._config.dry_run:
            print("[server] dry-run: no episode, no reply", flush=True)
            return ""

        memory = self._restore_memory(event)
        observers = (
            [TurnPersister(self._store, event.session_id)]
            if self._config.persist
            else None
        )
        try:
            result = run_episode(
                EpisodeConfig(
                    goal=event.text,
                    domain=self._config.domain,
                    domain_options={"headless": True},
                    backend=self._config.backend,
                    max_steps=self._config.max_steps,
                    max_tokens=120_000,
                    timeout=self._config.timeout,
                    allow_dangerous=self._config.allow_dangerous,
                    memory=memory,
                    observers=observers,
                    on_event=self._trace,
                )
            )
            answer = result.answer
        except Exception as exc:  # reply with the failure instead of dying
            answer = f"处理失败：{type(exc).__name__}: {exc}"
            print(f"[server] episode failed: {exc}", file=sys.stderr, flush=True)

        if self._config.persist and memory.summary:
            self._store.append_summary(
                event.session_id, memory.summary, memory.folded
            )
        self._reply(event, answer)
        return answer

    def _restore_memory(self, event: Event) -> WindowMemory:
        snapshot = self._store.load(event.session_id, keep_turns=8)
        memory = WindowMemory(
            goal=event.text,
            scope=_scope_of(event.text),
            facts=JsonFactStore(self._facts),
        )
        memory.seed(snapshot.summary, snapshot.turns, snapshot.folded)
        memory.set_goal(event.text)
        if self._config.persist:
            self._store.set_goal(event.session_id, event.text)
        return memory

    def _reply(self, event: Event, text: str) -> None:
        text = text.strip() or "(empty answer)"
        if event.channel == "popo":
            tool = PopoNotifyTool(receiver=event.target)
            observation = tool.run(
                {"title": "MahoFish 回复", "text": text[:3000], "to": event.target},
                ToolContext(step=0),
            )
            print(f"[server] -> {observation.content}", flush=True)
            return
        print(f"[server] -> {event.channel}:{event.target}\n{text}", flush=True)

    def _trace(self, step: int, kind: str, text: str) -> None:
        if self._config.verbose:
            print(f"  [{step:02d}] {kind:>7} | {text}", flush=True)


def _scope_of(text: str) -> str | None:
    match = _URL.search(text)
    return match.group(1).lower() if match else None


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="MahoFish agent server.")
    parser.add_argument("--channel", default="popo", choices=["popo"])
    parser.add_argument("--session", default="13e43356@app.robot.popo.com")
    parser.add_argument("--self", default="", help="your POPO uid (auto-detected)")
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--since-minutes", type=float, default=0.0)
    parser.add_argument("--domain", default="browser")
    parser.add_argument("--backend", default="deepseek")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    self_uid = args.self or current_user()
    if not self_uid:
        print("[server] cannot resolve your uid; pass --self", file=sys.stderr)
        return 2

    listener = PopoListener(
        session_id=args.session,
        self_uid=self_uid,
        interval=args.interval,
        since_minutes=args.since_minutes,
    )
    server = Server(
        ServerConfig(
            domain=args.domain,
            backend=args.backend,
            max_steps=args.max_steps,
            timeout=args.timeout,
            persist=not args.no_persist,
            dry_run=args.dry_run,
            verbose=args.verbose,
        ),
        [listener],
    )
    print(
        f"[server] self={self_uid} session={args.session} "
        f"domain={args.domain} backend={args.backend} "
        f"persist={not args.no_persist} dry_run={args.dry_run}",
        flush=True,
    )
    if args.once:
        handled = server.drain_once()
        print(f"[server] handled {handled} event(s)", flush=True)
        return 0
    return server.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
