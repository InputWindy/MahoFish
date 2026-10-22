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
import base64
import os
import re
import threading
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

from .contracts import Observation, Tool, ToolContext, ToolSpec
from .listeners import Event, Listener
from .llm import load_dotenv
from .notify import NotifyTool

ROOT = Path(__file__).resolve().parents[1]
INBOX = ROOT / ".fish" / "inbox"
OUTBOX = ROOT / ".fish" / "outbox"
MAX_INLINE = 5 * 1024 * 1024  # platform limit for file_data upload
_SAFE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")

FILE_TYPES = {
    ".png": 1,
    ".jpg": 1,
    ".jpeg": 1,
    ".gif": 1,
    ".mp4": 2,
    ".silk": 3,
    ".mp3": 3,
    ".wav": 3,
}


def file_type_of(source: str) -> int:
    return FILE_TYPES.get(Path(source.split("?")[0]).suffix.lower(), 4)


class QQChannel:
    name = "qq"

    def __init__(self, appid: str = "", secret: str = "") -> None:
        load_dotenv()
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

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
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
                    getattr(message, "attachments", None) or [],
                )

            async def on_group_at_message_create(self, message):
                outer._push(
                    "group",
                    str(message.group_openid),
                    str(message.content or ""),
                    str(message.id),
                    getattr(message, "attachments", None) or [],
                )

        intents = botpy.Intents(public_messages=True, direct_message=True)
        self._client = Client(intents=intents)
        try:
            self._client.run(appid=self._appid, secret=self._secret)
        except Exception as exc:  # keep the process alive; report loudly
            print(f"[qq] client stopped: {exc}", flush=True)

    def _push(
        self,
        kind: str,
        session: str,
        content: str,
        msg_id: str,
        attachments: Iterable = (),
    ) -> None:
        self._last_msg[session] = msg_id
        self._kinds[session] = kind
        saved = self._download(msg_id, attachments)
        if saved:
            listing = "\n".join(f"- {path}" for path in saved)
            content = f"{content}\n\n[收到附件，已保存到本地]\n{listing}".strip()
        if not content.strip() or self._emit is None:
            return
        self._emit(
            Event(
                channel=self.name,
                session_id=session,
                text=content.strip(),
                target=session,
                meta={"kind": kind, "msg_id": msg_id, "attachments": saved},
            )
        )

    def _download(self, msg_id: str, attachments: Iterable) -> list[str]:
        saved: list[str] = []
        for attachment in attachments:
            url = str(getattr(attachment, "url", "") or "")
            if not url:
                continue
            name = str(getattr(attachment, "filename", "") or "file")
            safe = _SAFE.sub("_", name)[-80:] or "file"
            path = INBOX / f"{msg_id[:8]}-{safe}"
            try:
                INBOX.mkdir(parents=True, exist_ok=True)
                urllib.request.urlretrieve(url, path)
                saved.append(str(path))
            except Exception as exc:
                print(f"[qq] attachment download failed: {exc}", flush=True)
        return saved

    def send_file(self, session: str, source: str, kind: str = "") -> str:
        """Send an image/video/voice/file to a QQ user or group."""
        if self._client is None or self._loop is None:
            raise RuntimeError("qq client is not ready yet")
        from botpy.api import Route

        kind = kind or self.kind_of(session)
        file_type = file_type_of(source)
        payload: dict[str, object] = {"file_type": file_type, "srv_send_msg": True}
        if source.startswith(("http://", "https://")):
            payload["url"] = source
        else:
            path = Path(source)
            if not path.is_file():
                raise RuntimeError(f"file not found: {source}")
            data = path.read_bytes()
            if len(data) > MAX_INLINE:
                raise RuntimeError(
                    "local files over 5MB need chunked upload, which is not implemented"
                )
            payload["file_data"] = base64.b64encode(data).decode("ascii")
            payload["file_name"] = path.name
        if kind == "group":
            route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=session)
        else:
            route = Route("POST", "/v2/users/{openid}/files", openid=session)
        api = self._client.api

        async def _send():
            return await api._http.request(route, json=payload)

        future = asyncio.run_coroutine_threadsafe(_send(), self._loop)
        future.result(timeout=60)
        return f"qq:{kind}:{session} ({file_type})"

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


class QQSender:
    """Shared, mutable target so tools know which conversation to answer."""

    def __init__(self, channel: QQChannel) -> None:
        self.channel = channel
        self.target = ""


class QQSendFileTool(Tool):
    def __init__(self, sender: QQSender, default_target: str = "") -> None:
        self._sender = sender
        self._default = default_target

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="qq_send_file",
            description=(
                "Send an image/video/voice/file to the current QQ conversation. "
                "Use a local path under .fish/inbox or a public http URL."
            ),
            parameters={
                "properties": {
                    "path": {"type": "string"},
                    "to": {"type": "string"},
                },
                "required": ["path"],
            },
            dangerous=True,
            idempotent=False,
            permissions=frozenset({"read", "network"}),
        )

    def run(self, args: dict, ctx: ToolContext) -> Observation:
        path = str(args.get("path") or "").strip()
        target = str(args.get("to") or self._default or self._sender.target).strip()
        if not path:
            return Observation.failure("path is required")
        if not target:
            return Observation.failure("no qq conversation to send to")
        try:
            destination = self._sender.channel.send_file(target, path)
        except Exception as exc:
            return Observation.failure(f"qq send_file failed: {exc}")
        return Observation.success(f"file sent via {destination}")


class QQReadFileTool(Tool):
    """Read a downloaded attachment; deliberately limited to .fish/inbox."""

    def __init__(self, inbox: Path = INBOX, max_chars: int = 4000) -> None:
        self._inbox = inbox.resolve()
        self._max = max_chars

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="qq_read_file",
            description="Read a text attachment previously downloaded to .fish/inbox.",
            parameters={
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            permissions=frozenset({"read"}),
        )

    def run(self, args: dict, ctx: ToolContext) -> Observation:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return Observation.failure("path is required")
        path = Path(raw)
        try:
            resolved = path.resolve()
        except OSError as exc:
            return Observation.failure(f"bad path: {exc}")
        if not resolved.is_relative_to(self._inbox):
            return Observation.failure("only files under .fish/inbox can be read")
        if not resolved.is_file():
            return Observation.failure(f"file not found: {resolved}")
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return Observation.failure(f"read failed: {exc}")
        if len(text) > self._max:
            text = text[: self._max] + "\n...[truncated]"
        return Observation.success(text or "(empty file)")


class QQWriteFileTool(Tool):
    """Create a text file under .fish/outbox so it can be sent afterwards."""

    def __init__(self, root: Path = OUTBOX, max_chars: int = 200_000) -> None:
        self._root = root.resolve()
        self._max = max_chars

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="qq_write_file",
            description=(
                "Create a text file under .fish/outbox. "
                "Then call qq_send_file with the absolute path it returns."
            ),
            parameters={
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            idempotent=False,
            permissions=frozenset({"write"}),
        )

    def run(self, args: dict, ctx: ToolContext) -> Observation:
        raw = str(args.get("path") or "").strip()
        content = str(args.get("content") or "")
        if not raw:
            return Observation.failure("path is required")
        if len(content) > self._max:
            return Observation.failure(f"content too large (> {self._max} chars)")
        target = Path(raw)
        if not target.is_absolute():
            target = self._root / target.name
        try:
            resolved = target.resolve()
            resolved.relative_to(self._root)
        except (OSError, ValueError):
            return Observation.failure("only files under .fish/outbox can be written")
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
        except OSError as exc:
            return Observation.failure(f"write failed: {exc}")
        return Observation.success(f"written: {resolved}")


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
