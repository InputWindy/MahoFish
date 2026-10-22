"""WeChat personal bot channel via the official ClawBot / iLink Bot API.

Protocol (HTTP+JSON, no SDK): scan a QR code to obtain ``bot_token``, then
long-poll ``getupdates`` and answer with ``sendmessage`` carrying the inbound
``context_token``.

    python -m fish.wechat login     # print a QR, scan with WeChat
    python -m fish.wechat poll      # print inbound messages (no agent)
"""

from __future__ import annotations

import base64
import json
import random
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .contracts import Observation, Tool, ToolContext, ToolSpec
from .listeners import Event, Listener
from .notify import NotifyTool

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".fish" / "wechat_state.json"

BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"
LONG_POLL_TIMEOUT = 45.0

MSG_USER = 1
MSG_BOT = 2
ITEM_TEXT = 1
STATE_FINISH = 2


class WeChatError(RuntimeError):
    pass


class SessionExpired(WeChatError):
    pass


def _uin() -> str:
    return base64.b64encode(struct.pack(">I", random.getrandbits(32))).decode()


@dataclass
class WeChatState:
    bot_token: str = ""
    base_url: str = BASE_URL
    bot_id: str = ""
    buf: str = ""
    contexts: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = STATE) -> "WeChatState":
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls(
            bot_token=str(data.get("bot_token", "")),
            base_url=str(data.get("base_url") or BASE_URL),
            bot_id=str(data.get("bot_id", "")),
            buf=str(data.get("buf", "")),
            contexts={str(k): str(v) for k, v in (data.get("contexts") or {}).items()},
        )

    def save(self, path: Path = STATE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "bot_token": self.bot_token,
                    "base_url": self.base_url,
                    "bot_id": self.bot_id,
                    "buf": self.buf,
                    "contexts": self.contexts,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def _request(
    method: str,
    endpoint: str,
    body: dict[str, Any] | None = None,
    token: str = "",
    base_url: str = BASE_URL,
    timeout: float = 40.0,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _uin(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise WeChatError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise WeChatError(f"network: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WeChatError(f"bad json: {raw[:200]!r}") from exc


def login(state: WeChatState, timeout: float = 600.0) -> WeChatState:
    """Show a QR code, refresh it when it expires, persist the bot token."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = _request(
            "POST",
            f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}",
            {"local_token_list": []},
        )
        qrcode = str(response.get("qrcode") or "")
        content = str(response.get("qrcode_img_content") or "")
        if not qrcode or not content:
            raise WeChatError(f"unexpected qr response: {str(response)[:200]}")
        _print_qr(content)
        print("请用微信扫码并在手机上确认。", flush=True)

        expired = False
        while time.monotonic() < deadline:
            status = _request(
                "GET",
                f"ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(qrcode)}",
                timeout=60.0,
            )
            kind = str(status.get("status") or "")
            if kind == "confirmed":
                state.bot_token = str(status.get("bot_token") or "")
                state.base_url = str(status.get("baseurl") or BASE_URL)
                state.bot_id = str(status.get("ilink_bot_id") or "")
                if not state.bot_token:
                    raise WeChatError("confirmed without bot_token")
                state.save()
                print("登录成功，凭据已保存到 .fish/wechat_state.json", flush=True)
                return state
            if kind == "expired":
                print("二维码过期，正在刷新…", flush=True)
                expired = True
                break
        if not expired and time.monotonic() >= deadline:
            break
    raise WeChatError("login timed out")


def _print_qr(content: str) -> None:
    try:
        import qrcode as qrlib
    except ImportError:
        print(f"未安装 qrcode，请手动打开/扫码：{content}", flush=True)
        return
    qr = qrlib.QRCode(border=2, box_size=8)
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    path = ROOT / ".fish" / "wechat_login.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    print(f"二维码已保存：{path}", flush=True)
    print(f"备用链接：{content}", flush=True)


def get_updates(state: WeChatState, timeout: float = LONG_POLL_TIMEOUT) -> list[dict[str, Any]]:
    payload = {"get_updates_buf": state.buf}
    try:
        response = _request(
            "POST",
            "ilink/bot/getupdates",
            payload,
            token=state.bot_token,
            base_url=state.base_url,
            timeout=timeout,
        )
    except WeChatError:
        return []
    if response.get("errcode") == -14 or response.get("ret") == -14:
        raise SessionExpired("wechat session expired, login again")
    buf = response.get("get_updates_buf")
    if isinstance(buf, str) and buf:
        state.buf = buf
        state.save()
    return [msg for msg in (response.get("msgs") or []) if isinstance(msg, dict)]


def text_of(message: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in message.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == ITEM_TEXT:
            text = (item.get("text_item") or {}).get("text")
            if text:
                chunks.append(str(text))
    return "\n".join(chunks).strip()


def send_text(state: WeChatState, to: str, text: str) -> str:
    ctx = state.contexts.get(to, "")
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to,
            "client_id": f"maho-fish-{uuid.uuid4().hex[:12]}",
            "message_type": MSG_BOT,
            "message_state": STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
    }
    if ctx:
        body["msg"]["context_token"] = ctx
    _request(
        "POST",
        "ilink/bot/sendmessage",
        body,
        token=state.bot_token,
        base_url=state.base_url,
    )
    return f"wechat:{to}"


class WeChatChannel:
    name = "wechat"

    def __init__(self, state: WeChatState | None = None) -> None:
        self.state = state or WeChatState.load()
        self._emit: Callable[[Event], None] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def configured(self) -> bool:
        return bool(self.state.bot_token)

    def start(self, emit: Callable[[Event], None]) -> None:
        if not self.configured:
            raise RuntimeError("wechat not logged in: run `python -m fish.wechat login`")
        self._emit = emit
        self._thread = threading.Thread(target=self._run, name="listener-wechat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                messages = get_updates(self.state)
            except SessionExpired as exc:
                print(f"[wechat] {exc}", flush=True)
                self._stop.wait(60)
                continue
            except Exception as exc:
                print(f"[wechat] poll failed: {exc}", flush=True)
                self._stop.wait(5)
                continue
            for message in messages:
                if message.get("message_type") != MSG_USER:
                    continue
                sender = str(message.get("from_user_id") or "")
                text = text_of(message)
                token = message.get("context_token")
                if sender and token:
                    self.state.contexts[sender] = str(token)
                    self.state.save()
                if not sender or not text or self._emit is None:
                    continue
                self._emit(
                    Event(
                        channel=self.name,
                        session_id=sender,
                        text=text,
                        target=sender,
                        meta={"msg_id": str(message.get("message_id") or "")},
                    )
                )

    def send(self, to: str, text: str) -> str:
        return send_text(self.state, to, text)


class WeChatListener(Listener):
    name = "wechat"

    def __init__(self, channel: WeChatChannel | None = None) -> None:
        self.channel = channel or WeChatChannel()

    def start(self, emit: Callable[[Event], None]) -> None:
        self.channel.start(emit)

    def stop(self) -> None:
        self.channel.stop()


class WeChatNotifyTool(NotifyTool):
    channel = "wechat"

    def __init__(self, wechat: WeChatChannel) -> None:
        super().__init__("")
        self._wechat = wechat

    def send(self, title: str, text: str, to: str) -> str:
        if not to:
            raise RuntimeError("wechat target (user id) is required")
        return self._wechat.send(to, text)


def main(argv: list[str] | None = None) -> int:
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    command = (argv or sys.argv[1:] or ["login"])[0]
    state = WeChatState.load()
    if command == "login":
        login(state)
        return 0
    if command == "poll":
        if not state.bot_token:
            print("not logged in", file=sys.stderr)
            return 2
        for _ in range(3):
            for message in get_updates(state):
                print(json.dumps(message, ensure_ascii=False)[:300])
        return 0
    print("usage: python -m fish.wechat [login|poll]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
