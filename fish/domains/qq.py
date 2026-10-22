"""QQ domain: file send/read/write tools on top of the live QQ channel."""

from __future__ import annotations

from ..contracts import ToolRegistry
from ..domain import DomainCapabilities, DomainPack
from ..qq import (
    QQChannel,
    QQReadFileTool,
    QQSender,
    QQSendFileTool,
    QQWriteFileTool,
)
from ..tools import LocalRegistry

HINTS = """\
- Received attachments are already saved under .fish/inbox; read them with
  qq_read_file before answering.
- To send generated text as a file: call qq_write_file(path=".fish/outbox/<name>.txt",
  content="...") and then qq_send_file(path="<the absolute path it returned>").
"""


class QQDomain(DomainPack):
    name = "qq"
    capabilities = DomainCapabilities(tools=True, hints=True)

    def __init__(self, channel: QQChannel) -> None:
        self._sender = QQSender(channel)
        self._registry = LocalRegistry(
            [QQSendFileTool(self._sender), QQReadFileTool(), QQWriteFileTool()]
        )

    def registry(self) -> ToolRegistry:
        return self._registry

    def hints(self) -> str:
        return HINTS

    def set_target(self, target: str) -> None:
        """Point the send tool at the conversation that just spoke."""
        self._sender.target = target
