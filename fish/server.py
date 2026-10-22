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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import domain
from .contracts import ToolContext
from .domains.search import available as search_available
from .listeners import Event, Listener, PopoListener, current_user
from .memory import WindowMemory
from .notify import PopoNotifyTool
from .qq import QQChannel, QQListener, QQNotifyTool
from .wechat import WeChatChannel, WeChatListener, WeChatNotifyTool
from .wecom import WeComChannel, WeComListener, WeComNotifyTool
from .runtime import EpisodeConfig, run_episode
from .session_store import SessionStore, TurnPersister
from .store import JsonFactStore

ROOT = Path(__file__).resolve().parents[1]
_URL = re.compile(r"https?://([^/\s]+)")
CONTINUE_WORDS = (
    "继续",
    "接着",
    "接着做",
    "继续做",
    "继续吧",
    "continue",
    "cont",
    "goon",
    "keepgoing",
)
GRANT_WORDS = ("授权", "允许写", "允许", "allow", "approve")
REVOKE_WORDS = ("取消授权", "收回授权", "revoke")
GRANT_TTL = 600.0


@dataclass
class ServerConfig:
    domain: str = "browser"
    backend: str = "deepseek"
    max_steps: int = 12
    timeout: float = 300.0
    allow_dangerous: bool = False
    persist: bool = True
    dry_run: bool = False
    verbose: bool = False
    search: bool = True
    facts_path: Path | None = None
    sessions_dir: Path | None = None


class Server:
    def __init__(
        self,
        config: ServerConfig,
        listeners: Iterable[Listener],
        qq_channel: QQChannel | None = None,
        wechat_channel: WeChatChannel | None = None,
        wecom_channel: WeComChannel | None = None,
    ) -> None:
        self._config = config
        self._listeners = list(listeners)
        self._qq = qq_channel
        self._wechat = wechat_channel
        self._wecom = wecom_channel
        self._queue: queue.Queue[Event] = queue.Queue()
        self._store = SessionStore(
            config.sessions_dir or (ROOT / ".fish" / "sessions"),
            persist=config.persist,
        )
        self._facts = config.facts_path or (ROOT / ".fish" / "facts.json")
        self._pack = None
        self._qq_domain = None
        self._grants: dict[str, float] = {}

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
            if self._pack is not None:
                try:
                    self._pack.close()
                except Exception:
                    pass

    def _ensure_pack(self):
        """Load domains once and reuse them: spawning Chrome per message is slow."""
        if self._pack is not None:
            return self._pack
        try:
            packs = [domain.load(self._config.domain, headless=True)]
            if self._qq is not None:
                self._qq_domain = domain.load("qq", channel=self._qq)
                packs.append(self._qq_domain)
            if self._config.search and search_available():
                packs.append(domain.load("search"))
            self._pack = packs[0] if len(packs) == 1 else domain.CompositeDomain(packs)
        except Exception as exc:
            print(f"[server] domain load failed: {exc}", file=sys.stderr, flush=True)
            return None
        return self._pack

    def _grant_active(self, session: str) -> bool:
        expiry = self._grants.get(session, 0.0)
        if expiry and expiry > time.time():
            return True
        if expiry:
            self._grants.pop(session, None)
        return False

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

        command = _norm(event.text)
        if command in GRANT_WORDS:
            self._grants[event.session_id] = time.time() + GRANT_TTL
            self._reply(
                event,
                "已授权写操作 10 分钟。请把刚才的命令再发一次。",
            )
            return ""
        if command in REVOKE_WORDS:
            self._grants.pop(event.session_id, None)
            self._reply(event, "已收回写操作授权。")
            return ""
        allow_dangerous = self._config.allow_dangerous or self._grant_active(
            event.session_id
        )

        snapshot = self._store.load(event.session_id, keep_turns=8)
        goal, continuing = self._resolve_goal(event, snapshot)
        if continuing:
            print("[server] continue: resuming from checkpoint", flush=True)

        memory = self._restore_memory(event, snapshot, goal)
        pack = self._ensure_pack()
        if pack is None:
            answer = "域装配失败，暂时无法处理。"
            self._reply(event, answer)
            return answer
        if self._qq_domain is not None:
            self._qq_domain.set_target(event.target)

        observers = (
            [TurnPersister(self._store, event.session_id)]
            if self._config.persist
            else None
        )
        captured: list[str] = []

        def on_checkpoint(stop: str, text: str) -> None:
            captured.append(text)
            if self._config.persist:
                progress, next_step = _split_checkpoint(text)
                self._store.append_checkpoint(
                    event.session_id, stop, progress, next_step
                )

        try:
            result = run_episode(
                EpisodeConfig(
                    goal=goal,
                    domain=self._config.domain,
                    domain_options={"headless": True},
                    backend=self._config.backend,
                    max_steps=self._config.max_steps,
                    max_tokens=120_000,
                    timeout=self._config.timeout,
                    allow_dangerous=allow_dangerous,
                    memory=memory,
                    observers=observers,
                    pack=pack,
                    checkpoint=True,
                    on_checkpoint=on_checkpoint,
                    on_event=self._trace,
                )
            )
            if result.stopped_reason == "final":
                answer = result.answer
                if result.denials and not allow_dangerous:
                    answer += (
                        f"\n\n（有 {len(result.denials)} 个操作需要写权限，"
                        "回复「授权」后我会重试）"
                    )
            else:
                answer = _friendly_stop(
                    result.stopped_reason, captured[-1] if captured else ""
                )
        except Exception as exc:  # reply with the failure instead of dying
            answer = f"处理失败：{type(exc).__name__}: {exc}"
            print(f"[server] episode failed: {exc}", file=sys.stderr, flush=True)

        if self._config.persist and memory.summary:
            self._store.append_summary(
                event.session_id, memory.summary, memory.folded
            )
        self._reply(event, answer)
        return answer

    def _resolve_goal(self, event: Event, snapshot) -> tuple[str, bool]:
        text = event.text.strip()
        if not _is_continue(text):
            return text, False
        checkpoint = snapshot.checkpoint or {}
        progress = checkpoint.get("progress", "")
        next_step = checkpoint.get("next", "")
        if progress or next_step:
            return (
                "继续上次未完成的任务。\n"
                f"上次进度：{progress}\n下一步：{next_step}",
                True,
            )
        return f"继续上次未完成的任务：{snapshot.goal or '（无记录）'}", True

    def _restore_memory(self, event: Event, snapshot, goal: str) -> WindowMemory:
        memory = WindowMemory(
            goal=goal,
            scope=_scope_of(goal),
            facts=JsonFactStore(self._facts),
        )
        memory.seed(snapshot.summary, snapshot.turns, snapshot.folded)
        memory.set_goal(goal)
        if self._config.persist:
            self._store.set_goal(event.session_id, goal)
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
        if event.channel == "qq":
            if self._qq is None:
                print("[server] qq channel is not configured", file=sys.stderr)
                return
            tool = QQNotifyTool(self._qq)
            observation = tool.run(
                {"title": "MahoFish", "text": text[:3000], "to": event.target},
                ToolContext(step=0),
            )
            print(f"[server] -> {observation.content}", flush=True)
            return
        if event.channel == "wechat":
            if self._wechat is None:
                print("[server] wechat channel is not configured", file=sys.stderr)
                return
            tool = WeChatNotifyTool(self._wechat)
            observation = tool.run(
                {"title": "MahoFish", "text": text[:3000], "to": event.target},
                ToolContext(step=0),
            )
            print(f"[server] -> {observation.content}", flush=True)
            return
        if event.channel == "wecom":
            if self._wecom is None:
                print("[server] wecom channel is not configured", file=sys.stderr)
                return
            tool = WeComNotifyTool(self._wecom)
            observation = tool.run(
                {"title": "MahoFish", "text": text[:20000], "to": event.target},
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


def _norm(text: str) -> str:
    return re.sub(r"[\s，。！!？?、,.~…:：\"'「」]+", "", text.lower())


def _is_continue(text: str) -> bool:
    """'继续' should also match '好！继续吧' - short replies only."""
    compact = _norm(text)
    if not compact or len(compact) > 10:
        return False
    return any(word in compact for word in CONTINUE_WORDS)


def _split_checkpoint(text: str) -> tuple[str, str]:
    progress, next_step = "", ""
    for line in text.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("PROGRESS:"):
            progress = stripped.split(":", 1)[1].strip()
        elif upper.startswith("NEXT:"):
            next_step = stripped.split(":", 1)[1].strip()
    if not progress and not next_step:
        progress = text.strip()
    return progress[:500], next_step[:300]


def _friendly_stop(reason: str, checkpoint: str) -> str:
    if reason.startswith("step budget"):
        head = "这次没做完（步数预算用完）。"
    elif reason.startswith("token budget"):
        head = "这次没做完（token 预算用完）。"
    elif reason.startswith("time budget"):
        head = "这次超时中断了。"
    elif reason.startswith("llm error"):
        head = "模型暂时不可用，任务中断。"
    elif reason.startswith("policy error"):
        head = "安全策略拦截，任务中断。"
    else:
        head = f"任务中断（{reason}）。"
    parts = [head]
    if checkpoint:
        parts.append("已保留进度：\n" + checkpoint.strip()[:800])
    parts.append("回复「继续」我接着做。")
    return "\n\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="MahoFish agent server.")
    parser.add_argument(
        "--channels",
        default="popo",
        help="comma separated listeners: popo,qq,wechat,wecom",
    )
    parser.add_argument("--session", default="13e43356@app.robot.popo.com")
    parser.add_argument("--self", default="", help="your POPO uid (auto-detected)")
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--since-minutes", type=float, default=0.0)
    parser.add_argument("--domain", default="browser")
    parser.add_argument("--backend", default="deepseek")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument(
        "--allow-dangerous",
        action="store_true",
        help="allow write-side tools (browser click/fill, qq_send_file)",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-search",
        action="store_true",
        help="do not attach the web_search/web_fetch tools",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    names = [part.strip() for part in args.channels.split(",") if part.strip()]
    listeners: list[Listener] = []
    qq_channel: QQChannel | None = None
    wechat_channel: WeChatChannel | None = None
    wecom_channel: WeComChannel | None = None
    self_uid = args.self or current_user()

    if "popo" in names:
        if not self_uid:
            print("[server] cannot resolve your uid; pass --self", file=sys.stderr)
            return 2
        listeners.append(
            PopoListener(
                session_id=args.session,
                self_uid=self_uid,
                interval=args.interval,
                since_minutes=args.since_minutes,
            )
        )
    if "qq" in names:
        qq_channel = QQChannel()
        if not qq_channel.configured:
            print(
                "[server] set FISH_QQ_APPID and FISH_QQ_SECRET to enable qq",
                file=sys.stderr,
            )
            return 2
        listeners.append(QQListener(qq_channel))
    if "wechat" in names:
        wechat_channel = WeChatChannel()
        if not wechat_channel.configured:
            print(
                "[server] wechat not logged in: python -m fish.wechat login",
                file=sys.stderr,
            )
            return 2
        listeners.append(WeChatListener(wechat_channel))
    if "wecom" in names:
        wecom_channel = WeComChannel()
        if not wecom_channel.configured:
            print(
                "[server] set FISH_WECOM_BOT_ID and FISH_WECOM_SECRET to enable wecom",
                file=sys.stderr,
            )
            return 2
        listeners.append(WeComListener(wecom_channel))
    if not listeners:
        print(f"[server] no listener enabled for {args.channels!r}", file=sys.stderr)
        return 2

    server = Server(
        ServerConfig(
            domain=args.domain,
            backend=args.backend,
            max_steps=args.max_steps,
            timeout=args.timeout,
            persist=not args.no_persist,
            dry_run=args.dry_run,
            verbose=args.verbose,
            allow_dangerous=args.allow_dangerous,
            search=not args.no_search,
        ),
        listeners,
        qq_channel=qq_channel,
        wechat_channel=wechat_channel,
        wecom_channel=wecom_channel,
    )
    print(
        f"[server] channels={names} self={self_uid or '-'} session={args.session} "
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
