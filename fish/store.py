"""Cross-task facts (docs/contracts.md C9).

v0: JSON file, atomic write, id-based dedupe, score feedback, no LLM calls.
"""

from __future__ import annotations

import json
import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCORE_INIT = 0.5
SCORE_SUCCESS = 0.1
SCORE_FAIL = -0.3
PRUNE_AT = 0.0
MAX_TEXT = 400


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Fact:
    scope: str
    kind: str
    key: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = SCORE_INIT
    hits: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def id(self) -> str:
        return f"{self.scope}:{self.kind}:{self.key}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["id"] = self.id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Fact":
        return cls(
            scope=str(data["scope"]),
            kind=str(data["kind"]),
            key=str(data["key"]),
            text=str(data["text"])[:MAX_TEXT],
            metadata=dict(data.get("metadata") or {}),
            score=float(data.get("score", SCORE_INIT)),
            hits=int(data.get("hits", 0)),
            created_at=str(data.get("created_at") or _now()),
            updated_at=str(data.get("updated_at") or _now()),
        )


class FactStore(ABC):
    @abstractmethod
    def upsert(self, fact: Fact) -> Fact:
        ...

    @abstractmethod
    def query(
        self, scope: str | None, limit: int = 8, kinds: Iterable[str] | None = None
    ) -> list[Fact]:
        ...

    @abstractmethod
    def feedback(self, fact_id: str, success: bool) -> Fact | None:
        ...

    @abstractmethod
    def prune(self) -> int:
        ...

    @abstractmethod
    def all(self) -> list[Fact]:
        ...


class JsonFactStore(FactStore):
    def __init__(self, path: str | Path = ".fish/facts.json") -> None:
        self._path = Path(path)
        self._facts: dict[str, Fact] = {}
        self._load()

    # -- FactStore ----------------------------------------------------------

    def upsert(self, fact: Fact) -> Fact:
        incoming = replace(fact, text=fact.text[:MAX_TEXT])
        existing = self._facts.get(incoming.id)
        if existing is None:
            self._facts[incoming.id] = incoming
            self._flush()
            return incoming
        if existing.text != incoming.text:
            # conflict policy: overwrite and reset confidence
            updated = replace(
                incoming,
                score=SCORE_INIT,
                hits=existing.hits,
                created_at=existing.created_at,
                updated_at=_now(),
            )
        else:
            updated = replace(existing, updated_at=_now())
        self._facts[updated.id] = updated
        self._flush()
        return updated

    def query(
        self, scope: str | None, limit: int = 8, kinds: Iterable[str] | None = None
    ) -> list[Fact]:
        kind_set = set(kinds) if kinds else None
        items = [
            fact
            for fact in self._facts.values()
            if (scope is None or fact.scope == scope)
            and (kind_set is None or fact.kind in kind_set)
        ]
        items.sort(key=lambda f: (f.score, f.updated_at), reverse=True)
        return items[: max(0, limit)]

    def feedback(self, fact_id: str, success: bool) -> Fact | None:
        fact = self._facts.get(fact_id)
        if fact is None:
            return None
        delta = SCORE_SUCCESS if success else SCORE_FAIL
        updated = replace(
            fact,
            score=max(0.0, min(1.0, fact.score + delta)),
            hits=fact.hits + (1 if success else 0),
            updated_at=_now(),
        )
        self._facts[fact_id] = updated
        self._flush()
        return updated

    def prune(self) -> int:
        dead = [fid for fid, fact in self._facts.items() if fact.score <= PRUNE_AT]
        for fid in dead:
            del self._facts[fid]
        if dead:
            self._flush()
        return len(dead)

    def all(self) -> list[Fact]:
        return list(self._facts.values())

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # corrupted file degrades to empty, never blocks the run
        for item in raw.get("facts", []) if isinstance(raw, dict) else []:
            try:
                fact = Fact.from_dict(item)
            except (KeyError, TypeError, ValueError):
                continue
            self._facts[fact.id] = fact

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "facts": [f.to_dict() for f in self._facts.values()]}
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(self._path.parent),
            suffix=".tmp",
        )
        try:
            with handle as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            os.replace(handle.name, self._path)
        except OSError:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
