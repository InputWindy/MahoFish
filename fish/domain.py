"""Domain extension protocol (docs/contracts.md C15).

Built on two proven designs rather than a bespoke framework:

* explicit capability declaration + fail-fast validation, borrowed from the MCP
  lifecycle (a domain states what it provides, mismatches are rejected);
* discovery through package metadata (``importlib.metadata.entry_points``), the
  standard Python plugin mechanism.

Domains are assembled by the composition root (``main.py``). Nothing in
``fish/agent.py`` knows about them.
"""

from __future__ import annotations

import importlib
import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Callable

from .contracts import ToolRegistry, TurnObserver
from .tools import CompositeRegistry

ENTRY_POINT_GROUP = "fish.domains"

_BUILTIN: dict[str, str] = {
    "none": "fish.domains.core:CoreDomain",
    "browser": "fish.domains.browser:BrowserDomain",
    "notify": "fish.domains.notify:NotifyDomain",
    "qq": "fish.domains.qq:QQDomain",
    "search": "fish.domains.search:SearchDomain",
    "messaging": "fish.domains.messaging:MessagingDomain",
}

DomainFactory = Callable[..., "DomainPack"]


class DomainError(RuntimeError):
    pass


@dataclass(frozen=True)
class DomainCapabilities:
    """What a domain claims to provide. Checked against the implementation."""

    tools: bool = False
    hints: bool = False
    observers: bool = False
    scope: bool = False
    closable: bool = True


class DomainPack(ABC):
    name: str = "unknown"
    capabilities: DomainCapabilities = DomainCapabilities()

    @abstractmethod
    def registry(self) -> ToolRegistry:
        ...

    def hints(self) -> str:
        return ""

    def observers(self) -> list[TurnObserver]:
        return []

    def scope_for(self, goal: str) -> str | None:
        return None

    def close(self) -> None:
        """Release resources. Must be idempotent."""


class CompositeDomain(DomainPack):
    """Several domains behind one pack: merged tools, concatenated hints."""

    def __init__(self, packs: list[DomainPack]) -> None:
        if not packs:
            raise DomainError("composite domain needs at least one pack")
        self._packs = packs
        self.name = "+".join(pack.name for pack in packs)
        self.capabilities = DomainCapabilities(
            tools=any(pack.capabilities.tools for pack in packs),
            hints=any(pack.capabilities.hints for pack in packs),
            observers=any(pack.capabilities.observers for pack in packs),
            scope=any(pack.capabilities.scope for pack in packs),
            closable=all(pack.capabilities.closable for pack in packs),
        )
        self._registry = CompositeRegistry([pack.registry() for pack in packs])

    def registry(self) -> ToolRegistry:
        return self._registry

    def hints(self) -> str:
        return "\n".join(h for h in (pack.hints() for pack in self._packs) if h)

    def observers(self) -> list[TurnObserver]:
        return [observer for pack in self._packs for observer in pack.observers()]

    def scope_for(self, goal: str) -> str | None:
        for pack in self._packs:
            scope = pack.scope_for(goal)
            if scope:
                return scope
        return None

    def close(self) -> None:
        for pack in self._packs:
            try:
                pack.close()
            except Exception:
                pass


def available() -> list[str]:
    names = set(_BUILTIN)
    names.update(entry.name for entry in _entry_points())
    return sorted(names)


def load(name: str, **options: Any) -> DomainPack:
    factory = _factory(name)
    try:
        pack = factory(**_accepted_options(factory, options))
    except DomainError:
        raise
    except Exception as exc:  # launch failures, missing binaries, ...
        raise DomainError(f"domain {name!r}: assembly failed: {exc}") from exc
    if not isinstance(pack, DomainPack):
        raise DomainError(f"domain {name!r} did not return a DomainPack")
    try:
        validate(pack)
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError(f"domain {name!r}: validation crashed: {exc}") from exc
    return pack


def validate(pack: DomainPack) -> None:
    """Fail fast when declared capabilities and behaviour disagree."""
    caps = pack.capabilities
    specs = pack.registry().specs()
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise DomainError(f"domain {pack.name!r}: duplicate tool names {names}")
    if caps.tools and not specs:
        raise DomainError(f"domain {pack.name!r}: claims tools but exposes none")
    if not caps.tools and specs:
        raise DomainError(f"domain {pack.name!r}: exposes tools but claims none")
    for spec in specs:
        if not spec.description.strip():
            raise DomainError(f"domain {pack.name!r}: tool {spec.name!r} has no description")

    observers = pack.observers()
    if caps.observers and not observers:
        raise DomainError(f"domain {pack.name!r}: claims observers but returns none")
    if not caps.observers and observers:
        raise DomainError(f"domain {pack.name!r}: returns observers but claims none")
    for observer in observers:
        if not callable(getattr(observer, "on_turn", None)):
            raise DomainError(f"domain {pack.name!r}: observer lacks on_turn")

    if caps.hints and not isinstance(pack.hints(), str):
        raise DomainError(f"domain {pack.name!r}: hints() must return str")


def _accepted_options(factory: Any, options: dict[str, Any]) -> dict[str, Any]:
    """Pass only what the factory can accept, so domains stay decoupled."""
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return {}
    params = signature.parameters.values()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return dict(options)
    names = {
        p.name
        for p in params
        if p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return {key: value for key, value in options.items() if key in names}


def _factory(name: str) -> DomainFactory:
    target = _BUILTIN.get(name)
    if target is not None:
        return _load_target(target)
    for entry in _entry_points():
        if entry.name == name:
            return entry.load()
    raise DomainError(f"unknown domain {name!r}; available: {available()}")


def _load_target(target: str) -> DomainFactory:
    module_name, _, attr = target.partition(":")
    module = importlib.import_module(module_name)
    factory = getattr(module, attr, None)
    if factory is None:
        raise DomainError(f"cannot find {target}")
    return factory


def _entry_points() -> list[Any]:
    try:
        return list(entry_points(group=ENTRY_POINT_GROUP))
    except TypeError:  # Python < 3.10 compatibility
        return list(entry_points().get(ENTRY_POINT_GROUP, []))
