"""LLM backends (one per deployment mode).

* ``DeepSeekClient``       - release. Any OpenAI-compatible endpoint, API key
                             from env or ``.env``.
* ``CodemakerServerLLM``   - development. Talks to a local ``codemaker serve``
                             HTTP API, reusing the CLI login state so no API
                             key is needed.

Both are stateless from the agent's point of view: the loop resends the full
conversation every step.

    FISH_API_KEY=sk-...
    FISH_BASE_URL=https://api.deepseek.com/v1
    FISH_MODEL=deepseek-chat
    FISH_CODEMAKER_URL=http://127.0.0.1:4123   # optional, else auto-started
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from .contracts import LLMBackend, Message, Reply, estimate_tokens

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

# codemaker's own agent tools, disabled so it behaves like a plain completion
# endpoint instead of editing files on its own.
CODEMAKER_TOOLS_OFF = (
    "bash",
    "read",
    "write",
    "edit",
    "patch",
    "glob",
    "grep",
    "list",
    "webfetch",
    "task",
    "todowrite",
    "todoread",
)


class LLMError(RuntimeError):
    pass


def load_dotenv(path: Path = ENV_FILE) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else None


class DeepSeekClient(LLMBackend):
    name = "deepseek"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        load_dotenv()
        self._base_url = (
            base_url
            or _first_env("FISH_BASE_URL", "OPENAI_BASE_URL", "DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com/v1"
        ).rstrip("/")
        self._api_key = api_key or _first_env(
            "FISH_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"
        )
        if not self._api_key:
            raise LLMError(
                "missing API key: put FISH_API_KEY=sk-... in .env "
                "(copy .env.example) or set the env var"
            )
        self._model = (
            model
            or _first_env("FISH_MODEL", "OPENAI_MODEL", "DEEPSEEK_MODEL")
            or "deepseek-chat"
        )
        self._timeout = timeout

    @property
    def model(self) -> str:
        return self._model

    def complete(self, messages: Sequence[Message]) -> Reply:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in messages
            ],
            "temperature": 0.2,
            "stream": False,
        }
        try:
            body = _request_json(
                "POST",
                f"{self._base_url}/chat/completions",
                payload,
                self._timeout,
                {"Authorization": f"Bearer {self._api_key}"},
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise LLMError(f"HTTP {exc.code} from {self._base_url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"cannot reach {self._base_url}: {exc.reason}") from exc

        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"unexpected response: {str(body)[:800]}")
        content = str(choices[0].get("message", {}).get("content") or "").strip()
        usage = body.get("usage") or {}
        tokens = int(usage.get("total_tokens") or 0) or estimate_tokens(content)
        return Reply(text=content, tokens=tokens)


class CodemakerServerLLM(LLMBackend):
    """Development backend: one HTTP round-trip per step against `codemaker serve`."""

    name = "codemaker"

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        timeout: float = 240.0,
        ready_timeout: float = 90.0,
    ) -> None:
        load_dotenv()
        self._url = (url or _first_env("FISH_CODEMAKER_URL") or "").rstrip("/")
        self._model = model or _first_env("FISH_MODEL", "CODEMAKER_MODEL")
        self._timeout = timeout
        self._ready_timeout = ready_timeout
        self._proc: subprocess.Popen[bytes] | None = None
        self._workdir: str | None = None
        if self._url:
            self._wait_ready()
        else:
            self._start_server()

    @property
    def model(self) -> str:
        return self._model or "(server default)"

    # -- lifecycle ----------------------------------------------------------

    def _start_server(self) -> None:
        port = _free_port()
        self._workdir = tempfile.mkdtemp(prefix="fish-codemaker-")
        binary = shutil.which("codemaker")
        if binary is None:
            raise LLMError("`codemaker` CLI not found in PATH")
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            [binary, "serve", "--port", str(port)],
            cwd=self._workdir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        self._url = f"http://127.0.0.1:{port}"
        self._wait_ready()

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise LLMError(
                    f"codemaker serve exited early (code {self._proc.returncode})"
                )
            try:
                _request_json("GET", f"{self._url}/session", None, 5.0)
                return
            except Exception:
                time.sleep(1.0)
        raise LLMError(f"codemaker serve not ready at {self._url}")

    def close(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None

    def __enter__(self) -> "CodemakerServerLLM":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- completion ---------------------------------------------------------

    def complete(self, messages: Sequence[Message]) -> Reply:
        prompt = "\n\n".join(message.render() for message in messages)
        session_id = self._create_session()
        try:
            text = self._send(session_id, prompt)
        finally:
            self._delete_session(session_id)
        return Reply(text=text, tokens=estimate_tokens(prompt + text))

    def _create_session(self) -> str:
        try:
            session = _request_json(
                "POST", f"{self._url}/session", {"title": "maho-fish"}, 30.0
            )
        except Exception as exc:
            raise LLMError(f"cannot create codemaker session: {exc}") from exc
        session_id = str(session.get("id", ""))
        if not session_id:
            raise LLMError(f"unexpected session response: {str(session)[:300]}")
        return session_id

    def _delete_session(self, session_id: str) -> None:
        try:
            _request_json("DELETE", f"{self._url}/session/{session_id}", None, 10.0)
        except Exception:
            pass  # leaking one session is not worth failing the run

    def _send(self, session_id: str, prompt: str) -> str:
        payload: dict[str, Any] = {
            "parts": [{"type": "text", "text": prompt}],
            "tools": {name: False for name in CODEMAKER_TOOLS_OFF},
        }
        if self._model:
            provider, _, model_id = self._model.partition("/")
            if provider and model_id:
                payload["model"] = {"providerID": provider, "modelID": model_id}
        try:
            reply = _request_json(
                "POST",
                f"{self._url}/session/{session_id}/message",
                payload,
                self._timeout,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise LLMError(f"HTTP {exc.code} from codemaker: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"cannot reach codemaker at {self._url}: {exc.reason}") from exc

        return _extract_text(reply)


def _extract_text(reply: Any) -> str:
    if not isinstance(reply, dict):
        return str(reply)
    texts = [
        str(part.get("text", ""))
        for part in reply.get("parts") or []
        if isinstance(part, dict)
        and part.get("type") == "text"
        and not part.get("synthetic")
    ]
    text = "\n".join(t for t in texts if t).strip()
    if text:
        return text
    tools = [
        str(part.get("tool", ""))
        for part in reply.get("parts") or []
        if isinstance(part, dict) and part.get("type") == "tool"
    ]
    if tools:
        return f"(codemaker tried its own tools: {tools}; no text answer)"
    return ""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_llm(backend: str = "deepseek", model: str | None = None) -> LLMBackend:
    if backend == "codemaker":
        return CodemakerServerLLM(model=model)
    if backend == "deepseek":
        return DeepSeekClient(model=model)
    raise LLMError(f"unknown backend: {backend}")
