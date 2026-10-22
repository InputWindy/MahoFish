"""QQ official bot channel (optional dependency: ``pip install qq-botpy``).

botpy connects out over a WebSocket gateway, so no public callback URL is
needed. One QQChannel instance owns the client and its asyncio loop; the server
thread talks to it through ``send()``.

Two constraints from the platform:
* replies are passive - they must reference the incoming ``msg_id``;
* group messages only wake the bot when it is @-mentioned.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Callable

from .listeners import Event, Listener
from .notify import NotifyTool


class QQChannel:
    name = "qq"

    def __init__(self, appid: str = "", secret: str = "") -> None:
        self._appid = appid or os.environ.get("FISH_QQ_APPID", "")
        self._secret = secret or os.environ.get("FISH_QQ_SECRET", "")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._emit: Callable[[Event], None] | None = None
        self._last_msg: dict[str, str] = {}
        self._kinds: dict[str, str] = {}
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def configured(self) -> bool:
        return bool(self._appid and self._secret)

    def start(self, emit: Callable[[Event], None]) -> None:
        if not self.configured:
            raise RuntimeError(
                "QQ bot credentials missing: set FISH_QQ_APPID and FISH_QQ_SECRET"
            )
        try:
            import botpy  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("qq-botpy is not installed: pip install qq-botpy") from exc
        self._emit = emit
        self._thread = threading.Thread(target=self._run, name="listener-qq", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._client is None or self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)
        except Exception:
            pass

    # -- botpy side ---------------------------------------------------------

    def _run(self) -> None:
        import botpy

        outer = self

        class Client(botpy.Client):
            async def on_ready(self):
                outer._loop = asyncio.get_running_loop()
                outer._ready.set()
                name = getattr(getattr(self, "robot", None), "name", "qq-bot")
                print(f"[qq] ready as {name}", flush=True)

            async def on_c2c_message_create(self, message):
                outer._push(
                    "c2c",
                    str(message.author.user_openid),
                    str(message.content or ""),
                    str(message.id),
                )

            async def on_group_at_message_create(self, message):
                outer._push(
                    "group",
                    str(message.group_openid),
                    str(message.content or ""),
                    str(message.id),
                )

        intents = botpy.Intents(public_messages=True, direct_message=True)
        self._client = Client(intents=intents)
        try:
            self._client.run(appid=self._appid, secret=self._secret)
        except Exception as exc:  # keep the process alive; report loudly
            print(f"[qq] client stopped: {exc}", flush=True)

    def _push(self, kind: str, session: str, content: str, msg_id: str) -> None:
        self._last_msg[session] = msg_id
        self._kinds[session] = kind
        if not content.strip() or self._emit is None:
            return
        self._emit(
            Event(
                channel=self.name,
                session_id=session,
                text=content.strip(),
                target=session,
                meta={"kind": kind, "msg_id": msg_id},
            )
        )

    # -- server side --------------------------------------------------------

    def kind_of(self, session: str) -> str:
        return self._kinds.get(session, "c2c")

    def send(self, session: str, content: str, kind: str = "") -> str:
        if self._client is None or self._loop is None:
            raise RuntimeError("qq client is not ready yet")
        msg_id = self._last_msg.get(session)
        kind = kind or self.kind_of(session)
        api = self._client.api

        async def _send():
            if kind == "group":
                return await api.post_group_message(
                    group_openid=session, msg_type=0, msg_id=msg_id, content=content
                )
            return await api.post_c2c_message(
                openid=session, msg_type=0, msg_id=msg_id, content=content
            )

        future = asyncio.run_coroutine_threadsafe(_send(), self._loop)
        future.result(timeout=30)
        return f"qq:{kind}:{session}"


class QQListener(Listener):
    name = "qq"

    def __init__(self, channel: QQChannel | None = None) -> None:
        self.channel = channel or QQChannel()

    def start(self, emit: Callable[[Event], None]) -> None:
        self.channel.start(emit)

    def stop(self) -> None:
        self.channel.stop()


class QQNotifyTool(NotifyTool):
    channel = "qq"

    def __init__(self, qq: QQChannel, kind: str = "") -> None:
        super().__init__("")
        self._qq = qq
        self._kind = kind

    def send(self, title: str, text: str, to: str) -> str:
        if not to:
            raise RuntimeError("qq target (openid) is required")
        return self._qq.send(to, text, self._kind)


def make_qq_notify_tool(qq: QQChannel) -> NotifyTool:
    return QQNotifyTool(qq)
