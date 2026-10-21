"""Pluggable delivery for scheduled reports.

Only ``console`` and ``file`` ship today. POPO / email / QQ are additional
Notifier implementations; nothing else in the scheduler changes.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

_SAFE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_-]+")


class NotifierError(RuntimeError):
    pass


class Notifier(ABC):
    channel: str = "unknown"

    @abstractmethod
    def send(self, title: str, text: str) -> str:
        """Deliver a message; return a human readable destination."""


class ConsoleNotifier(Notifier):
    channel = "console"

    def send(self, title: str, text: str) -> str:
        print(f"\n===== {title} =====\n{text}\n")
        return "console"


class FileNotifier(Notifier):
    channel = "file"

    def __init__(self, directory: str | Path = ".fish/reports") -> None:
        self._dir = Path(directory)

    def send(self, title: str, text: str) -> str:
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = _SAFE.sub("-", title).strip("-")[:40] or "report"
        path = self._dir / f"{stamp}-{slug}.md"
        path.write_text(f"# {title}\n\n{text}\n", encoding="utf-8")
        return str(path)


class MultiNotifier(Notifier):
    channel = "multi"

    def __init__(self, notifiers: list[Notifier]) -> None:
        self._notifiers = notifiers

    def send(self, title: str, text: str) -> str:
        results: list[str] = []
        for notifier in self._notifiers:
            try:
                results.append(notifier.send(title, text))
            except Exception as exc:  # one dead channel must not drop the rest
                results.append(f"{notifier.channel} failed: {exc}")
        return "; ".join(results)


def build_notifier(spec: str) -> Notifier:
    """Build from a compact spec: ``console``, ``file``, ``file:<dir>``."""
    kind, _, arg = spec.partition(":")
    kind = kind.strip().lower()
    if kind == "console":
        return ConsoleNotifier()
    if kind == "file":
        return FileNotifier(arg.strip() or ".fish/reports")
    raise NotifierError(f"unknown notifier {spec!r}; use console or file[:dir]")
