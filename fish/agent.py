"""The loop. Depends only on ``fish.contracts``."""

from __future__ import annotations

import json
import time
from typing import Callable, Sequence

from .contracts import (
    Decision,
    LLMBackend,
    Memory,
    Message,
    Observation,
    Policy,
    PROTOCOL_HINT,
    Role,
    RunResult,
    Summarizer,
    ToolContext,
    ToolRegistry,
    Turn,
    Usage,
    parse_decision,
)

SYSTEM_PROMPT = """\
You are a browser automation agent. You cannot see the screen; the only things
you know about a page are what tools return to you.

{protocol}

Available tools:
{tools}

Rules:
- One tool per reply.
- `evaluate_script` is the cheapest way to read page content: pass a JS arrow
  function, e.g. {{"function": "() => document.title"}}.
- Prefer reading the DOM over screenshots.
- If a tool returns an error, change the call instead of repeating it.
- If the data you already have satisfies the goal, answer with "final" right
  away. Do not keep refining extraction for cosmetic reasons.
"""

EventHook = Callable[[int, str, str], None]


class Agent:
    def __init__(
        self,
        llm: LLMBackend,
        tools: ToolRegistry,
        memory: Memory,
        policy: Policy,
        on_event: EventHook | None = None,
        summarizer: Summarizer | None = None,
        min_context_tokens: int = 2000,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._memory = memory
        self._policy = policy
        self._on_event = on_event
        self._summarizer = summarizer
        self._min_context = min_context_tokens

    def run(self) -> RunResult:
        system = SYSTEM_PROMPT.format(
            protocol=PROTOCOL_HINT,
            tools="\n".join(spec.render() for spec in self._tools.specs()),
        )
        started = time.monotonic()
        usage = Usage()
        step = 0
        last_signature: str | None = None

        while True:
            stopped, reason = self._policy.exhausted()
            if stopped:
                return RunResult(
                    answer=f"Stopped: {reason}",
                    steps=usage.steps,
                    usage=usage,
                    stopped_reason=reason,
                )

            step += 1
            if self._summarizer is not None:
                try:
                    folded = self._memory.compact(self._summarizer)
                except Exception as exc:  # compaction must never break the run
                    folded = 0
                    self._emit(step, "compact", f"failed: {exc}")
                if folded:
                    self._emit(step, "compact", f"folded {folded} turns")
            budget = self._context_budget(usage)
            messages: Sequence[Message] = [
                Message(role=Role.SYSTEM, content=system),
                *self._memory.view(budget),
            ]
            reply = self._llm.complete(messages)
            usage = Usage(
                steps=usage.steps + 1,
                tokens=usage.tokens + reply.tokens,
                seconds=time.monotonic() - started,
            )
            self._policy.spend(usage)

            decision = parse_decision(reply.text)
            if decision is None:
                self._emit(step, "model", f"unparsable reply: {reply.text[:300]!r}")
                self._memory.add(
                    Turn(
                        step=step,
                        decision=None,
                        observation=Observation.failure(
                            "your reply was not a single JSON object; "
                            "reply with {\"tool\": ...} or {\"final\": ...}"
                        ),
                    )
                )
                continue

            if decision.thought:
                self._emit(step, "thought", decision.thought)

            if decision.is_final:
                self._memory.add(Turn(step=step, decision=decision, observation=None))
                return RunResult(
                    answer=decision.final or "",
                    steps=usage.steps,
                    usage=usage,
                    stopped_reason="final",
                )

            signature = _signature(decision)
            if signature == last_signature:
                observation = Observation.failure(
                    "you repeated the exact same call; it will return the same "
                    "result. Change the call or reply with 'final'."
                )
                self._emit(step, "repeat", observation.content)
            else:
                observation = self._execute(step, decision)
                last_signature = signature
            self._memory.add(
                Turn(step=step, decision=decision, observation=observation)
            )
    # -- internals ----------------------------------------------------------

    def _execute(self, step: int, decision: Decision) -> Observation:
        action = decision.action
        assert action is not None
        self._emit(step, "tool", f"{action.tool} {action.args}")

        tool = self._tools.get(action.tool)
        if tool is None:
            available = [spec.name for spec in self._tools.specs()]
            observation = Observation.failure(
                f"unknown tool {action.tool!r}; available: {available}"
            )
        else:
            verdict = self._policy.check(tool.spec, action.args)
            if not verdict.allowed:
                observation = Observation.failure(f"denied by policy: {verdict.reason}")
            else:
                observation = tool.run(action.args, ToolContext(step=step))

        preview = observation.content
        if len(preview) > 1200:
            preview = preview[:1200] + "..."
        self._emit(step, "result", preview)
        return observation

    def _context_budget(self, usage: Usage) -> int:
        remaining = self._policy.max_tokens - usage.tokens
        return max(self._min_context, remaining)

    def _emit(self, step: int, kind: str, text: str) -> None:
        if self._on_event is not None:
            self._on_event(step, kind, text)


def _signature(decision: Decision) -> str:
    action = decision.action
    if action is None:
        return ""
    return json.dumps([action.tool, action.args], sort_keys=True, ensure_ascii=False)
