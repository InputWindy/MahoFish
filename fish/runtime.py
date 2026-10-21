"""One agent episode: assemble LLM + domain + memory + policy, run, clean up.

Shared by ``main.py``, ``eval/run_tasks.py`` and ``fish/scheduler.py`` so the
wiring exists in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import domain
from .agent import Agent
from .contracts import Memory, RunResult, TurnObserver
from .llm import build_llm
from .memory import WindowMemory
from .policy import StaticPolicy
from .store import JsonFactStore
from .summarize import make_summarizer


@dataclass
class EpisodeConfig:
    goal: str
    domain: str = "none"
    domain_options: dict = field(default_factory=dict)
    backend: str = "codemaker"
    model: str | None = None
    max_steps: int = 12
    max_tokens: int = 200_000
    timeout: float = 1800.0
    max_llm_retries: int = 2
    allow_dangerous: bool = False
    facts_path: Path | None = None
    hints_extra: str = ""
    memory: Memory | None = None
    observers: list[TurnObserver] | None = None
    on_event: Callable[[int, str, str], None] | None = None
    on_tools: Callable[[str, list[str]], None] | None = None
    on_llm: Callable[[str, str], None] | None = None


def run_episode(config: EpisodeConfig) -> RunResult:
    llm = build_llm(config.backend, config.model)
    if config.on_llm is not None:
        config.on_llm(getattr(llm, "name", "unknown"), getattr(llm, "model", "-"))
    pack = None
    try:
        pack = domain.load(config.domain, **config.domain_options)
        registry = pack.registry()
        specs = _safe(registry.specs, default=[])
        if config.on_tools is not None:
            config.on_tools(pack.name, [spec.name for spec in specs])

        policy = StaticPolicy(
            allow={spec.name for spec in specs},
            allow_dangerous=config.allow_dangerous,
            max_steps=config.max_steps,
            max_tokens=config.max_tokens,
            deadline_seconds=config.timeout,
        )
        facts_path = config.facts_path or (Path.cwd() / ".fish" / "facts.json")
        memory = config.memory or WindowMemory(
            goal=config.goal,
            scope=_safe(pack.scope_for, config.goal, default=None),
            facts=JsonFactStore(facts_path),
        )
        agent = Agent(
            llm,
            registry,
            memory,
            policy,
            on_event=config.on_event,
            summarizer=make_summarizer(llm),
            observers=config.observers,
            hints=_safe(pack.hints, default="") + config.hints_extra,
            max_llm_retries=config.max_llm_retries,
        )
        return agent.run()
    finally:
        if pack is not None:
            try:
                pack.close()
            except Exception:
                pass
        close = getattr(llm, "close", None)
        if callable(close):
            close()


def _safe(fn, *args, default=None):
    try:
        return fn(*args)
    except Exception:
        return default
