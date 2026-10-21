"""The loop. Depends only on ``fish.contracts``.

Failure policy: nothing partial may kill the run.
* tool exceptions  -> Observation.failure (model decides what to do next)
* policy exceptions -> deny (fail closed)
* memory exceptions -> degrade (empty view / skipped record)
* LLM exceptions    -> retried, then a RunResult with ``stopped_reason="llm error"``
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable, Sequence

from .contracts import (
    Decision,
    Fault,
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
    Verdict,
    estimate_tokens,
    parse_decision,
)

SYSTEM_PROMPT = """\
You are an autonomous agent. You cannot see anything directly; the only things
you know come from tool results and your memory of them.

Runtime: {identity}

{protocol}

Available tools:
{tools}

Rules:
- One tool per reply.
- The tool list above is authoritative. If your environment's own prompt
  mentions other tools, ignore it: those are not part of this task.
- Only call tools from the list above; never invent a tool name.
- If a tool returns an error, change the call instead of repeating it.
- If the information you already have satisfies the goal, answer with "final"
  right away. Do not keep refining for cosmetic reasons.
- If no tools are available, answer directly with "final".
{domain_hints}"""

EventHook = Callable[[int, str, str], None]


def _render_tools(
    specs: Sequence[ToolSpec],
    on_bad: Callable[[str], None] | None = None,
) -> str:
    if not specs:
        return "(none)"
    lines: list[str] = []
    for spec in specs:
        try:
            lines.append(spec.render())
        except Exception as exc:  # malformed spec must not kill startup
            name = getattr(spec, "name", "?")
            lines.append(f"- {name}: (unrenderable: {exc})")
            if on_bad is not None:
                on_bad(f"tool spec {name!r} unrenderable: {exc}")
    return "\n".join(lines)


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
        identity: str = "",
        min_context_tokens: int = 2000,
        max_llm_retries: int = 2,
        retry_backoff: float = 0.5,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._memory = memory
        self._policy = policy
        self._on_event = on_event
        self._summarizer = summarizer
        self._observers = tuple(observers or ())
        self._hints = hints
        self._identity = identity or "MahoFish (backend unknown)"
        self._min_context = min_context_tokens
        self._max_retries = max(0, max_llm_retries)
        self._retry_backoff = max(0.0, retry_backoff)

    def run(self) -> RunResult:
        self._faults = []
        system = SYSTEM_PROMPT.format(
            identity=self._identity,
            protocol=PROTOCOL_HINT,
            tools=_render_tools(
                self._safe_specs(),
                on_bad=lambda msg: self._fault(0, "registry", msg),
            ),
            domain_hints=self._hints,
        )
        started = time.monotonic()
        usage = Usage()
        step = 0
        last_signature: str | None = None

        while True:
            try:
                stopped, reason = self._policy.exhausted()
            except Exception as exc:  # a broken policy must stop, not crash
                stopped, reason = True, f"policy error: {exc}"
                self._fault(step, "policy", f"exhausted failed: {exc}")
            if stopped:
                return self._result(f"Stopped: {reason}", usage, reason)

            step += 1
            usage = self._compact(step, usage, started)

            budget = self._context_budget(usage)
            try:
                view = self._memory.view(budget)
            except Exception as exc:  # degraded memory: continue with the goal only
                self._fault(step, "memory", f"view failed: {exc}")
                view = []
            messages: Sequence[Message] = [
                Message(role=Role.SYSTEM, content=system),
                *view,
            ]

            try:
                reply = self._ask(step, messages)
            except Exception as exc:
                reason = f"llm error: {exc}"
                self._fault(step, "llm", str(exc))
                return self._result(f"Stopped: {reason}", usage, reason)

            text = reply.text if isinstance(reply.text, str) else str(reply.text or "")
            reply_tokens = (
                reply.tokens if isinstance(reply.tokens, int) else estimate_tokens(text)
            )
            estimated = reply_tokens if getattr(reply, "source", "estimate") == "estimate" else 0
            usage = Usage(
                steps=usage.steps + 1,
                tokens=usage.tokens + reply_tokens,
                seconds=time.monotonic() - started,
                estimated_tokens=usage.estimated_tokens + estimated,
            )
            self._spend(usage, step)

            decision = parse_decision(text)
            if decision is None:
                self._emit(step, "model", f"unparsable reply: {text[:300]!r}")
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
                return self._result(decision.final or "", usage, "final")

            signature = _signature(decision)
            if signature == last_signature:
                observation = Observation.failure(
                    "you repeated the same call; it will return the same result. "
                    "Change the call or reply with 'final'."
                )
                self._emit(step, "repeat", observation.content)
            else:
                observation = self._execute(step, decision)
                last_signature = signature
            self._remember(step, decision, observation)

    # -- internals ----------------------------------------------------------

    def _ask(self, step: int, messages: Sequence[Message]):
        attempt = 0
        while True:
            try:
                return self._llm.complete(messages)
            except Exception as exc:
                if attempt >= self._max_retries:
                    raise
                attempt += 1
                self._fault(
                    step,
                    "llm-retry",
                    f"{exc}; retry {attempt}/{self._max_retries}",
                )
                if self._retry_backoff:
                    time.sleep(self._retry_backoff * attempt)

    def _compact(self, step: int, usage: Usage, started: float) -> Usage:
        if self._summarizer is None:
            return usage
        try:
            result = self._memory.compact(self._summarizer)
        except Exception as exc:  # compaction must never break the run
            self._fault(step, "compact", str(exc))
            return usage
        if not result.folded:
            return usage
        self._emit(step, "compact", f"folded {result.folded} turns")
        updated = Usage(
            steps=usage.steps,
            tokens=usage.tokens + result.tokens,
            seconds=time.monotonic() - started,
            estimated_tokens=usage.estimated_tokens,
        )
        self._spend(updated, step)
        return updated

    def _remember(
        self,
        step: int,
        decision: Decision | None,
        observation: Observation | None,
    ) -> None:
        turn = Turn(step=step, decision=decision, observation=observation)
        try:
            self._memory.add(turn)
        except Exception as exc:
            self._fault(step, "memory", f"add failed: {exc}")
        for observer in self._observers:
            try:
                observer.on_turn(turn)
            except Exception as exc:  # observers must never break the run
                self._fault(step, "observer", str(exc))

    def _execute(self, step: int, decision: Decision) -> Observation:
        action = decision.action
        assert action is not None
        self._emit(step, "tool", f"{action.tool} {action.args}")

        tool = self._safe_get(action.tool)
        if tool is None:
            available = [spec.name for spec in self._safe_specs()]
            observation = Observation.failure(
                f"unknown tool {action.tool!r}; available: {available}"
            )
        else:
            try:
                verdict = self._policy.check(tool.spec, action.args)
            except Exception as exc:
                verdict = Verdict.deny(f"policy error (fail-closed): {exc}")
                self._fault(step, "policy", f"check failed: {exc}")
            if not verdict.allowed:
                observation = Observation.failure(f"denied by policy: {verdict.reason}")
            else:
                try:
                    observation = tool.run(action.args, ToolContext(step=step))
                except Exception as exc:
                    observation = Observation.failure(f"tool crashed: {exc}")
                    self._fault(step, "tool", f"{action.tool}: {exc}")
                if not isinstance(observation, Observation):
                    self._fault(
                        step,
                        "tool",
                        f"{action.tool} returned {type(observation).__name__}",
                    )
                    observation = Observation.failure(
                        f"tool {action.tool!r} returned {type(observation).__name__}, "
                        "expected Observation"
                    )

        preview = observation.content
        if len(preview) > 1200:
            preview = preview[:1200] + "..."
        self._emit(step, "result", preview)
        return observation

    def _safe_specs(self) -> list[ToolSpec]:
        try:
            return list(self._tools.specs())
        except Exception as exc:  # a broken registry degrades to "no tools"
            self._fault(0, "registry", f"specs failed: {exc}")
            return []

    def _safe_get(self, name: str):
        try:
            return self._tools.get(name)
        except Exception as exc:
            self._fault(0, "registry", f"get({name!r}) failed: {exc}")
            return None

    def _limit_tokens(self) -> int:
        try:
            return int(self._policy.max_tokens)
        except Exception as exc:
            self._fault(0, "policy", f"max_tokens failed: {exc}")
            return 200_000

    def _spend(self, usage: Usage, step: int) -> None:
        try:
            self._policy.spend(usage)
        except Exception as exc:
            self._fault(step, "policy", f"spend failed: {exc}")

    def _context_budget(self, usage: Usage) -> int:
        limit = self._limit_tokens()
        remaining = limit - usage.tokens
        return max(self._min_context, min(remaining, limit))

    def _fault(self, step: int, source: str, message: str) -> None:
        """Handled failure: record it and shout about it, never swallow silently."""
        self._faults.append(Fault(step=step, source=source, message=message))
        self._emit(step, "fault", f"{source}: {message}")

    def _result(self, answer: str, usage: Usage, reason: str) -> RunResult:
        return RunResult(
            answer=answer,
            steps=usage.steps,
            usage=usage,
            stopped_reason=reason,
            faults=tuple(self._faults),
        )

    def _emit(self, step: int, kind: str, text: str) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(step, kind, text)
        except Exception:
            pass  # logging must never break the run


_WS = re.compile(r"\s+")


def _signature(decision: Decision) -> str:
    action = decision.action
    if action is None:
        return ""
    return json.dumps(
        [action.tool, _normalize(action.args)], sort_keys=True, ensure_ascii=False
    )


def _normalize(value):
    """Ignore cosmetic differences (whitespace, key order) when detecting repeats."""
    if isinstance(value, str):
        return _WS.sub("", value)
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value
