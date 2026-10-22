"""WeCom (企业微信) smart-robot channel over the official WebSocket long link.

Protocol: wss://openws.work.weixin.qq.com
* connect, then ``aibot_subscribe`` with bot_id + secret
* inbound ``aibot_msg_callback`` carries ``headers.req_id`` (needed to reply)
  and ``body.chattype`` (single/group), ``chatid`` for groups
* reply with ``aibot_respond_msg`` (24h window) or push with ``aibot_send_msg``
* ``ping`` every 30s

Only one connection per bot is allowed: a new subscribe kicks the old one.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .listeners import Event, Listener
from .llm import load_dotenv
from .notify import NotifyTool

WS_URL = "wss://openws.work.weixin.qq.com"
PING_INTERVAL = 30.0


def new_req_id() -> str:
    return f"maho-{uuid.uuid4().hex[:16]}"


def parse_callback(message: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise an aibot_msg_callback; None for events/other commands."""
    if message.get("cmd") != "aibot_msg_callback":
        return None
    body = message.get("body") or {}
    if not isinstance(body, dict):
        return None
    text = str((body.get("text") or {}).get("content") or "").strip()
    if not text:
        return None
    chat_type = str(body.get("chattype") or "single")
    sender = str((body.get("from") or {}).get("userid") or "")
    session = str(body.get("chatid") or sender)
    req_id = str((message.get("headers") or {}).get("req_id") or "")
    if not session or not req_id:
        return None
    return {
        "session": session,
        "text": text,
        "chat_type": chat_type,
        "sender": sender,
        "req_id": req_id,
        "msg_id": str(body.get("msgid") or ""),
    }


@dataclass
class Reply:
    session: str
    chat_type: str
    req_id: str


class WeComChannel:
    name = "wecom"

    def __init__(self, bot_id: str = "", secret: str = "") -> None:
        load_dotenv()
        self._bot_id = bot_id or os.environ.get("FISH_WECOM_BOT_ID", "")
        self._secret = secret or os.environ.get("FISH_WECOM_SECRET", "")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None
        self._emit: Callable[[Event], None] | None = None
        self._thread: threading.Thread | None = None
        self._replies: dict[str, Reply] = {}
        self._subscribed = threading.Event()
        self._error = ""

    @property
    def configured(self) -> bool:
        return bool(self._bot_id and self._secret)

    def start(self, emit: Callable[[Event], None]) -> None:
        if not self.configured:
            raise RuntimeError(
                "WeCom credentials missing: set FISH_WECOM_BOT_ID and FISH_WECOM_SECRET"
            )
        self._emit = emit
        self._thread = threading.Thread(target=self._run, name="listener-wecom", daemon=True)
        self._thread.start()
        if not self._subscribed.wait(timeout=20):
            raise RuntimeError(f"wecom subscribe failed: {self._error or 'timeout'}")

    def stop(self) -> None:
        loop, ws = self._loop, self._ws
        if loop is not None and ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception:
                pass

    # -- asyncio side -------------------------------------------------------

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:
            self._error = str(exc)
            print(f"[wecom] connection ended: {exc}", flush=True)
        finally:
            self._subscribed.set()

    async def _main(self) -> None:
        import websockets

        async with websockets.connect(WS_URL, ping_interval=None, max_size=None) as ws:
            self._ws = ws
            await self._send_ws(
                {
                    "cmd": "aibot_subscribe",
                    "headers": {"req_id": new_req_id()},
                    "body": {"bot_id": self._bot_id, "secret": self._secret},
                }
            )
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            reply = json.loads(raw)
            if reply.get("errcode") not in (0, None):
                self._error = f"errcode={reply.get('errcode')} {reply.get('errmsg')}"
                return
            print("[wecom] subscribed", flush=True)
            self._subscribed.set()
            pinger = asyncio.create_task(self._ping_loop())
            try:
                async for raw in ws:
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    self._handle(message)
            finally:
                pinger.cancel()

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await self._send_ws({"cmd": "ping", "headers": {"req_id": new_req_id()}})
            except Exception:
                return

    async def _send_ws(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("wecom websocket is not connected")
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    def _handle(self, message: dict[str, Any]) -> None:
        parsed = parse_callback(message)
        if parsed is None:
            return
        self._replies[parsed["session"]] = Reply(
            session=parsed["session"],
            chat_type=parsed["chat_type"],
            req_id=parsed["req_id"],
        )
        if self._emit is not None:
            self._emit(
                Event(
                    channel=self.name,
                    session_id=parsed["session"],
                    text=parsed["text"],
                    target=parsed["session"],
                    meta={
                        "chat_type": parsed["chat_type"],
                        "req_id": parsed["req_id"],
                        "msg_id": parsed["msg_id"],
                    },
                )
            )

    # -- server side --------------------------------------------------------

    def send(self, session: str, text: str) -> str:
        if self._loop is None:
            raise RuntimeError("wecom channel is not running")
        reply = self._replies.get(session)
        if reply is not None:
            payload = {
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": reply.req_id},
                "body": {"msgtype": "markdown", "markdown": {"content": text}},
            }
        else:
            payload = {
                "cmd": "aibot_send_msg",
                "headers": {"req_id": new_req_id()},
                "body": {
                    "chatid": session,
                    "chat_type": 2 if session.startswith("group") else 1,
                    "msgtype": "markdown",
                    "markdown": {"content": text},
                },
            }
        future = asyncio.run_coroutine_threadsafe(self._send_ws(payload), self._loop)
        future.result(timeout=30)
        return f"wecom:{session}"


class WeComListener(Listener):
    name = "wecom"

    def __init__(self, channel: WeComChannel | None = None) -> None:
        self.channel = channel or WeComChannel()

    def start(self, emit: Callable[[Event], None]) -> None:
        self.channel.start(emit)

    def stop(self) -> None:
        self.channel.stop()


class WeComNotifyTool(NotifyTool):
    channel = "wecom"

    def __init__(self, wecom: WeComChannel) -> None:
        super().__init__("")
        self._wecom = wecom

    def send(self, title: str, text: str, to: str) -> str:
        if not to:
            raise RuntimeError("wecom target (chatid/userid) is required")
        return self._wecom.send(to, text)
