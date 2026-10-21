"""Notification domain: exposes ``notify_<channel>`` tools to the agent.

The scheduler does not need this pack - it calls the same tools directly, just
without a model in the middle.
"""

from __future__ import annotations

from ..contracts import ToolRegistry
from ..domain import DomainCapabilities, DomainPack
from ..notify import build_notify_tools
from ..tools import LocalRegistry


class NotifyDomain(DomainPack):
    name = "notify"
    capabilities = DomainCapabilities(tools=True)

    def __init__(self, channels: list[str] | None = None) -> None:
        self._registry = LocalRegistry(build_notify_tools(channels or ["console"]))

    def registry(self) -> ToolRegistry:
        return self._registry
