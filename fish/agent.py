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
    ToolSpec,
    Turn,
    TurnObserver,
    Usage,
    parse_decision,
)

SYSTEM_PROMPT = """\
You are an autonomous agent. You cannot see anything directly; the only things
you know come from tool results and your memory of them.

{protocol}

Available tools:
{tools}

Rules:
- One tool per reply.
- Only call tools from the list above; never invent a tool name.
- If a tool returns an error, change the call instead of repeating it.
- If the information you already have satisfies the goal, answer with "final"
  right away. Do not keep refining for cosmetic reasons.
- If no tools are available, answer directly with "final".
{domain_hints}"""


def _render_tools(specs: Sequence[ToolSpec]) -> str:
    if not specs:
        return "(none)"
    return "\n".join(spec.render() for spec in specs)

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
        observers: Sequence[TurnObserver] | None = None,
        hints: str = "",
        min_context_tokens: int = 2000,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._memory = memory
        self._policy = policy
        self._on_event = on_event
        self._summarizer = summarizer
        self._observers = tuple(observers or ())
        self._hints = hints
        self._min_context = min_context_tokens

    def run(self) -> RunResult:
        system = SYSTEM_PROMPT.format(
            protocol=PROTOCOL_HINT,
            tools=_render_tools(self._tools.specs()),
            domain_hints=self._hints,
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
                self._remember(
                    step,
                    None,
                    Observation.failure(
                        "your reply was not a single JSON object; "
                        "reply with {\"tool\": ...} or {\"final\": ...}"
                    ),
                )
                continue

            if decision.thought:
                self._emit(step, "thought", decision.thought)

            if decision.is_final:
                self._remember(step, decision, None)
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
            self._remember(step, decision, observation)
    # -- internals ----------------------------------------------------------

    def _remember(
        self,
        step: int,
        decision: Decision | None,
        observation: Observation | None,
    ) -> None:
        turn = Turn(step=step, decision=decision, observation=observation)
        self._memory.add(turn)
        for observer in self._observers:
            try:
                observer.on_turn(turn)
            except Exception as exc:  # observers must never break the run
                self._emit(step, "observer", f"failed: {exc}")

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
