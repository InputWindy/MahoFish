"""Cross-channel messaging: let the agent send through another channel.

The agent normally replies on the channel that spoke to it (server routing).
This domain adds an explicit ``send_message`` tool so it can, for example, take
an instruction in POPO and deliver it to the owner on QQ.

Targets default to the last conversation seen on that channel, so the agent
usually only needs ``channel`` and ``text``.
"""

from __future__ import annotations

from typing import Callable

from ..contracts import Observation, Tool, ToolContext, ToolSpec
from ..domain import DomainCapabilities, DomainPack
from ..tools import LocalRegistry

SendFn = Callable[[str, str], str]
TargetFn = Callable[[str], str]


class SendMessageTool(Tool):
    def __init__(
        self,
        senders: dict[str, SendFn],
        targets: TargetFn,
    ) -> None:
        self._senders = dict(senders)
        self._targets = targets

    @property
    def spec(self) -> ToolSpec:
        channels = sorted(self._senders)
        return ToolSpec(
            name="send_message",
            description=(
                "Send a text message to the owner through another channel "
                f"({', '.join(channels)}). Use it to relay something the user "
                "asked you to pass on."
            ),
            parameters={
                "properties": {
                    "channel": {"type": "string", "enum": channels},
                    "text": {"type": "string"},
                    "target": {"type": "string"},
                },
                "required": ["channel", "text"],
            },
            dangerous=True,
            idempotent=False,
            permissions=frozenset({"network"}),
        )

    def run(self, args: dict, ctx: ToolContext) -> Observation:
        channel = str(args.get("channel") or "").strip().lower()
        text = str(args.get("text") or "").strip()
        if not channel or not text:
            return Observation.failure("channel and text are required")
        sender = self._senders.get(channel)
        if sender is None:
            return Observation.failure(
                f"channel {channel!r} is not connected; available: {sorted(self._senders)}"
            )
        target = str(args.get("target") or "").strip() or self._targets(channel)
        if not target:
            return Observation.failure(
                f"no known {channel} conversation yet; pass target explicitly"
            )
        try:
            destination = sender(target, text[:3000])
        except Exception as exc:
            return Observation.failure(f"{channel} send failed: {exc}")
        return Observation.success(f"sent via {destination}")


class MessagingDomain(DomainPack):
    name = "messaging"
    capabilities = DomainCapabilities(tools=True)

    def __init__(
        self,
        senders: dict[str, SendFn] | None = None,
        targets: TargetFn | None = None,
    ) -> None:
        self._senders = dict(senders or {})
        self._targets = targets or (lambda channel: "")
        self._registry = LocalRegistry([SendMessageTool(self._senders, self._targets)])

    def registry(self) -> LocalRegistry:
        return self._registry
