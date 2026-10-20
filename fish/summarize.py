"""Turn folding: an LLM-backed summarizer injected into Memory.compact()."""

from __future__ import annotations

from typing import Sequence

from .contracts import LLMBackend, Message, Role, Summarizer, Summary, Turn

PROMPT = """\
Compress the agent's past steps into at most 3 short lines for its own future
reference. Keep only what changes the next decision:
1) done so far  2) facts learned about the page/site  3) what failed and why
No preamble, no markdown, no bullet symbols. Under 200 characters.

STEPS:
{steps}
"""

MAX_STEP_CHARS = 500
MAX_TOTAL_CHARS = 4000


def render_turns(turns: Sequence[Turn]) -> str:
    lines: list[str] = []
    for turn in turns:
        action = turn.decision.action if turn.decision else None
        tool = f"{action.tool} {action.args}" if action else "-"
        body = turn.observation.content if turn.observation else ""
        if turn.observation and not turn.observation.ok:
            body = f"FAILED: {body}"
        if len(body) > MAX_STEP_CHARS:
            body = body[:MAX_STEP_CHARS] + "..."
        lines.append(f"step {turn.step}: {tool}\n{body}")
    text = "\n\n".join(lines)
    return text[-MAX_TOTAL_CHARS:]


def make_summarizer(llm: LLMBackend) -> Summarizer:
    def summarize(turns: Sequence[Turn]) -> str | Summary:
        steps = render_turns(turns)
        if not steps:
            return ""
        try:
            reply = llm.complete([Message(Role.USER, PROMPT.format(steps=steps))])
        except Exception:
            return ""  # degrade to the folded-steps ledger, never break the run
        return Summary(text=reply.text.strip(), tokens=reply.tokens)

    return summarize
