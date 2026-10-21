"""Listeners: sources that turn external messages into Events.

A listener only polls/subscribes and emits; it never runs an agent. Adding a
new source (scheduled task, HTTP, file watcher) means adding one class.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
POPO_STATE = ROOT / ".fish" / "popo_listener_state.json"


@dataclass(frozen=True)
class Event:
    channel: str
    session_id: str
    text: str
    target: str
    meta: dict[str, Any] = field(default_factory=dict)


class Listener(ABC):
    name: str = "unknown"

    @abstractmethod
    def start(self, emit: Callable[[Event], None]) -> None:
        """Begin producing events; must return immediately."""

    def stop(self) -> None:
        return None


def _cli(*args: str, timeout: float = 60.0) -> str:
    binary = shutil.which("popo-cli")
    if binary is None:
        raise RuntimeError("popo-cli not found in PATH")
    argv = [binary, *args] if binary.lower().endswith(".exe") else [
        "cmd",
        "/c",
        "popo-cli",
        *args,
    ]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"popo-cli failed ({proc.returncode}): {proc.stderr[:300]}")
    return proc.stdout


def _find_result_list(node: Any) -> list[dict[str, Any]] | None:
    if isinstance(node, dict):
        found = node.get("result_list")
        if isinstance(found, list):
            return found
        for value in node.values():
            nested = _find_result_list(value)
            if nested is not None:
                return nested
    elif isinstance(node, list):
        for item in node:
            nested = _find_result_list(item)
            if nested is not None:
                return nested
    return None


def search_since(since_ms: int, page_size: int = 50) -> list[dict[str, Any]]:
    raw = _cli(
        "popo",
        "message_search",
        "type=1",
        "page=0",
        f"pageSize={page_size}",
        "msgType=0",
        f"timeStart={since_ms}",
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"unexpected popo-cli output: {raw[:200]!r}") from exc
    rows = _find_result_list(payload) or []
    return [row for row in rows if isinstance(row, dict)]


def current_user() -> str:
    try:
        payload = json.loads(_cli("popo", "team_current_user_info"))
    except Exception:
        return ""

    def walk(node: Any) -> str:
        if isinstance(node, dict):
            uid = node.get("uid")
            if isinstance(uid, str) and "@" in uid:
                return uid
            for value in node.values():
                found = walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found:
                    return found
        return ""

    return walk(payload)


class PopoListener(Listener):
    """Polls one bot conversation and emits the owner's new messages."""

    name = "popo"

    def __init__(
        self,
        session_id: str = "13e43356@app.robot.popo.com",
        self_uid: str = "",
        interval: float = 15.0,
        state_path: Path = POPO_STATE,
        since_minutes: float = 0.0,
    ) -> None:
        self.session_id = session_id
        self.self_uid = self_uid or current_user()
        self.interval = max(2.0, interval)
        self.state_path = state_path
        self.since_minutes = since_minutes
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: set[str] = set()
        self._last_ts = 0

    def start(self, emit: Callable[[Event], None]) -> None:
        self._load_state()
        self._thread = threading.Thread(
            target=self._run, args=(emit,), name=f"listener-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def poll_once(self, emit: Callable[[Event], None]) -> int:
        self._load_state()
        return self._poll(emit)

    # -- internals ----------------------------------------------------------

    def _run(self, emit: Callable[[Event], None]) -> None:
        while not self._stop.is_set():
            try:
                self._poll(emit)
            except Exception as exc:  # a broken poll must not kill the thread
                print(f"[listener:{self.name}] poll failed: {exc}", flush=True)
            self._stop.wait(self.interval)

    def _poll(self, emit: Callable[[Event], None]) -> int:
        rows = search_since(self._last_ts)
        fresh: list[dict[str, Any]] = []
        for row in rows:
            msg_id = str(row.get("msg_id", ""))
            if (
                str(row.get("session_id", "")) != self.session_id
                or str(row.get("nfrom", "")) != self.self_uid
                or not msg_id
                or msg_id in self._seen
                or not str(row.get("content", "")).strip()
            ):
                continue
            fresh.append(row)
        fresh.sort(key=lambda row: int(row.get("t_msg") or 0))
        for row in fresh:
            emit(
                Event(
                    channel=self.name,
                    session_id=self.session_id,
                    text=str(row.get("content", "")).strip(),
                    target=self.self_uid,
                    meta={"msg_id": str(row.get("msg_id", ""))},
                )
            )
            self._seen.add(str(row.get("msg_id", "")))
            self._last_ts = max(self._last_ts, int(row.get("t_msg") or 0) + 1)
        if fresh:
            self._save_state()
        return len(fresh)

    def _load_state(self) -> None:
        if self._last_ts:
            return
        if self.state_path.is_file():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            self._last_ts = int(data.get("last_ts") or 0)
            self._seen = set(data.get("seen") or [])
        if not self._last_ts:
            self._last_ts = int(time.time() * 1000) - int(
                max(0.0, self.since_minutes) * 60_000
            )

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"last_ts": self._last_ts, "seen": list(self._seen)[-200:]}
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
