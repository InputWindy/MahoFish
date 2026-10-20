"""LLM backend: any OpenAI-compatible ``/chat/completions`` endpoint.

Defaults to DeepSeek. Configuration comes from the environment, or from a
``.env`` file at the repo root (real env vars win):

    FISH_API_KEY=sk-...
    FISH_BASE_URL=https://api.deepseek.com/v1
    FISH_MODEL=deepseek-chat
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from .contracts import LLMBackend, Message, Reply, estimate_tokens

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


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


def build_llm(model: str | None = None) -> LLMBackend:
    return DeepSeekClient(model=model)
