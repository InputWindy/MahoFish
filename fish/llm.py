"""LLM backends. Both implement ``contracts.LLMBackend``.

* ``codemaker``  - shells out to a locally installed, already logged-in
                   ``codemaker`` CLI. No API key required.
* ``openai``     - any OpenAI-compatible ``/chat/completions`` endpoint
                   (DeepSeek, CodeMaker gateway, vLLM, Ollama, ...).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Any, Sequence

from .contracts import LLMBackend, Message, Reply, estimate_tokens

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class LLMError(RuntimeError):
    pass


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _render_messages(messages: Sequence[Message]) -> str:
    return "\n\n".join(message.render() for message in messages)


class CodemakerCLI(LLMBackend):
    """Uses `codemaker run` as a one-shot completion backend."""

    name = "codemaker"

    def __init__(
        self,
        model: str | None = None,
        timeout: float = 600.0,
        workdir: str | None = None,
    ) -> None:
        binary = shutil.which("codemaker")
        if binary is None:
            raise LLMError(
                "`codemaker` CLI not found in PATH. Install it, or use "
                "--backend openai with an API key instead."
            )
        self._binary = binary
        self._model = model
        self._timeout = timeout
        self._workdir = workdir

    def complete(self, messages: Sequence[Message]) -> Reply:
        prompt = _render_messages(messages)
        argv = [self._binary, "run", "--format", "default"]
        if self._workdir:
            argv += ["--dir", self._workdir]
        if self._model:
            argv += ["-m", self._model]
        argv.append(prompt)

        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"codemaker run timed out after {self._timeout}s") from exc

        out = _strip_ansi(proc.stdout or "").strip()
        if proc.returncode != 0 and not out:
            raise LLMError(
                f"codemaker run failed (exit {proc.returncode}): "
                f"{_strip_ansi(proc.stderr or '').strip()[:800]}"
            )
        return Reply(text=out, tokens=estimate_tokens(prompt + out))


class OpenAICompat(LLMBackend):
    """Any OpenAI-compatible chat completions endpoint."""

    name = "openai"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        self._base_url = (
            base_url
            or os.environ.get("FISH_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.deepseek.com/v1"
        ).rstrip("/")
        self._api_key = (
            api_key
            or os.environ.get("FISH_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        if not self._api_key:
            raise LLMError(
                "no API key: set FISH_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY"
            )
        self._model = (
            model
            or os.environ.get("FISH_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or "deepseek-chat"
        )
        self._timeout = timeout

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
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
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


def build_llm(backend: str, model: str | None, workdir: str | None) -> LLMBackend:
    if backend == "openai":
        return OpenAICompat(model=model)
    if backend == "codemaker":
        return CodemakerCLI(model=model, workdir=workdir)
    raise LLMError(f"unknown backend: {backend}")
