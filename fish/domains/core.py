"""Domain-free pack: no tools, so the loop can only produce final answers."""

from __future__ import annotations

from ..domain import DomainCapabilities, DomainPack
from ..contracts import ToolRegistry
from ..tools import EmptyRegistry


class CoreDomain(DomainPack):
    name = "none"
    capabilities = DomainCapabilities(tools=False)

    def __init__(self) -> None:
        self._registry = EmptyRegistry()

    def registry(self) -> ToolRegistry:
        return self._registry
