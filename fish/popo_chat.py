"""Chat with the agent through a POPO bot conversation (polling).

The platform has no push/callback, so this polls ``popo-cli popo message_search``
for messages in one session, runs one bounded episode per new message from the
configured user, and replies through the POPO notify tool (the bot side).

    python -m fish.popo_chat --once --dry-run    # inspect, never send
    python -m fish.popo_chat                     # reply loop
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .contracts import ToolContext
from .notify import PopoNotifyTool
from .runtime import EpisodeConfig, run_episode

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / ".fish" / "popo_chat_state.json"

DEFAULT_SESSION = "13e43356@app.robot.popo.com"  # 西蓝花
MAX_REPLY = 3000
SEEN_LIMIT = 200


@dataclass(frozen=True)
class ChatMessage:
    msg_id: str
    ts: int
    sender: str
    session_id: str
    content: str


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


def search_since(since_ms: int, page_size: int = 50) -> list[ChatMessage]:
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
    messages: list[ChatMessage] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        messages.append(
            ChatMessage(
                msg_id=str(row.get("msg_id", "")),
                ts=int(row.get("t_msg") or 0),
                sender=str(row.get("nfrom", "")),
                session_id=str(row.get("session_id", "")),
                content=str(row.get("content", "")),
            )
        )
    messages.sort(key=lambda m: m.ts)
    return messages


def load_state() -> dict[str, Any]:
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def current_user() -> str:
    try:
        raw = _cli("popo", "team_current_user_info")
        payload = json.loads(raw)
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


def pick_new(
    messages: Iterable[ChatMessage],
    session_id: str,
    self_uid: str,
    seen: set[str],
) -> list[ChatMessage]:
    return [
        message
        for message in messages
        if message.session_id == session_id
        and message.sender == self_uid
        and message.msg_id not in seen
        and message.content.strip()
    ]


def answer(message: ChatMessage, args: argparse.Namespace) -> str:
    result = run_episode(
        EpisodeConfig(
            goal=message.content.strip(),
            domain=args.domain,
            domain_options={"headless": True},
            backend=args.backend,
            max_steps=args.max_steps,
            max_tokens=60_000,
            timeout=args.timeout,
            on_event=_trace if args.verbose else None,
        )
    )
    text = result.answer.strip() or "(empty answer)"
    if len(text) > MAX_REPLY:
        text = text[:MAX_REPLY] + "\n...[truncated]"
    return text


def _trace(step: int, kind: str, text: str) -> None:
    print(f"  [{step:02d}] {kind:>7} | {text}", flush=True)


def handle(message: ChatMessage, args: argparse.Namespace, notifier: PopoNotifyTool) -> bool:
    preview = message.content.replace("\n", " ")[:60]
    print(f"[chat] {datetime.now():%H:%M:%S} <- {preview}", flush=True)
    if args.dry_run:
        print("[chat] dry-run: no episode, no reply", flush=True)
        return True
    try:
        reply = answer(message, args)
    except Exception as exc:  # a broken episode must not kill the loop
        reply = f"处理失败：{type(exc).__name__}: {exc}"
    observation = notifier.run(
        {"title": "MahoFish 回复", "text": reply, "to": args.self or ""},
        ToolContext(step=0),
    )
    print(f"[chat] -> {observation.content}", flush=True)
    return observation.ok


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="POPO chat loop (polling).")
    parser.add_argument("--session", default=DEFAULT_SESSION)
    parser.add_argument("--self", default="", help="your POPO uid (auto-detected)")
    parser.add_argument("--domain", default="none")
    parser.add_argument("--backend", default="codemaker")
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--since-minutes",
        type=float,
        default=0.0,
        help="on first run, look this far back instead of starting from now",
    )
    args = parser.parse_args(argv)

    self_uid = args.self or current_user()
    if not self_uid:
        print("[chat] cannot resolve your uid; pass --self", file=sys.stderr)
        return 2
    args.self = self_uid

    state = load_state()
    if "last_ts" not in state:  # first run: ignore history, only new messages
        start = int(time.time() * 1000) - int(max(0.0, args.since_minutes) * 60_000)
        state = {"last_ts": start, "seen": []}
        save_state(state)
        print(f"[chat] first run: window starts {args.since_minutes:.0f} min back", flush=True)

    seen: set[str] = set(state.get("seen") or [])
    notifier = PopoNotifyTool(receiver=self_uid)
    print(
        f"[chat] session={args.session} self={self_uid} "
        f"backend={args.backend} interval={args.interval}s dry_run={args.dry_run}",
        flush=True,
    )

    while True:
        try:
            messages = search_since(int(state["last_ts"]))
            fresh = pick_new(messages, args.session, self_uid, seen)
            for message in fresh:
                handle(message, args, notifier)
                seen.add(message.msg_id)
                state["last_ts"] = max(int(state["last_ts"]), message.ts + 1)
            if fresh:
                state["seen"] = list(seen)[-SEEN_LIMIT:]
                save_state(state)
        except Exception as exc:  # network/CLI hiccup: report and keep polling
            print(f"[chat] poll failed: {exc}", file=sys.stderr, flush=True)
        if args.once:
            return 0
        time.sleep(max(2.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
