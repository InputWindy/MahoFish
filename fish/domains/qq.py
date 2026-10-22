"""QQ domain: file send/read tools on top of the live QQ channel."""

from __future__ import annotations

from ..contracts import ToolRegistry
from ..domain import DomainCapabilities, DomainPack
from ..qq import QQChannel, QQReadFileTool, QQSender, QQSendFileTool
from ..tools import LocalRegistry


class QQDomain(DomainPack):
    name = "qq"
    capabilities = DomainCapabilities(tools=True)

    def __init__(self, channel: QQChannel) -> None:
        self._sender = QQSender(channel)
        self._registry = LocalRegistry(
            [QQSendFileTool(self._sender), QQReadFileTool()]
        )

    def registry(self) -> ToolRegistry:
        return self._registry

    def set_target(self, target: str) -> None:
        """Point the send tool at the conversation that just spoke."""
        self._sender.target = target
