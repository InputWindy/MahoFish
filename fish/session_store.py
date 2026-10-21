"""Append-only session transcripts.

One JSONL file per session; a torn last line costs one turn, never the file.
Compaction results are stored too, so a restart does not pay for summarisation
twice.

    {"t":"meta","goal":"...","updated":"2026-10-21T22:50:00Z"}
    {"t":"turn","step":1,"decision":{...},"observation":{...}}
    {"t":"compact","folded":8,"summary":"..."}
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import Action, Decision, Observation, Turn


@dataclass
class Snapshot:
    goal: str = ""
    summary: str = ""
    folded: int = 0
    turns: list[Turn] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def turn_to_dict(turn: Turn) -> dict[str, Any]:
    decision = turn.decision
    payload: dict[str, Any] = {"t": "turn", "step": turn.step}
    if decision is None:
        payload["decision"] = None
    else:
        payload["decision"] = {
            "thought": decision.thought,
            "final": decision.final,
            "action": None
            if decision.action is None
            else {"tool": decision.action.tool, "args": decision.action.args},
        }
    observation = turn.observation
    payload["observation"] = (
        None
        if observation is None
        else {
            "ok": observation.ok,
            "content": observation.content,
            "error": observation.error,
        }
    )
    return payload


def turn_from_dict(data: dict[str, Any]) -> Turn | None:
    raw_decision = data.get("decision")
    decision: Decision | None = None
    if isinstance(raw_decision, dict):
        raw_action = raw_decision.get("action")
        action = None
        if isinstance(raw_action, dict):
            action = Action(
                tool=str(raw_action.get("tool", "")),
                args=dict(raw_action.get("args") or {}),
            )
        decision = Decision(
            thought=str(raw_decision.get("thought", "")),
            action=action,
            final=raw_decision.get("final"),
        )
    raw_observation = data.get("observation")
    observation = None
    if isinstance(raw_observation, dict):
        observation = Observation(
            ok=bool(raw_observation.get("ok", False)),
            content=str(raw_observation.get("content", "")),
            error=raw_observation.get("error"),
        )
    try:
        step = int(data.get("step", 0))
    except (TypeError, ValueError):
        return None
    return Turn(step=step, decision=decision, observation=observation)


class SessionStore:
    def __init__(self, root: str | Path = ".fish/sessions", persist: bool = True) -> None:
        self._root = Path(root)
        self._persist = persist

    def path_for(self, session_id: str) -> Path:
        digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:16]
        return self._root / f"{digest}.jsonl"

    def append_turn(self, session_id: str, turn: Turn) -> None:
        self._append(session_id, turn_to_dict(turn))

    def append_summary(self, session_id: str, summary: str, folded: int) -> None:
        if not summary:
            return
        self._append(
            session_id, {"t": "compact", "folded": int(folded), "summary": summary}
        )

    def set_goal(self, session_id: str, goal: str) -> None:
        self._append(session_id, {"t": "meta", "goal": goal, "updated": _now()})

    def load(self, session_id: str, keep_turns: int = 8) -> Snapshot:
        snapshot = Snapshot()
        path = self.path_for(session_id)
        if not path.is_file():
            return snapshot
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return snapshot
        turns: list[Turn] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn line from a crash: skip it
            if not isinstance(row, dict):
                continue
            kind = row.get("t")
            if kind == "meta":
                snapshot.goal = str(row.get("goal", ""))
            elif kind == "compact":
                snapshot.summary = str(row.get("summary", ""))
                try:
                    snapshot.folded = int(row.get("folded", 0))
                except (TypeError, ValueError):
                    snapshot.folded = 0
            elif kind == "turn":
                turn = turn_from_dict(row)
                if turn is not None:
                    turns.append(turn)
        snapshot.turns = turns[-max(1, keep_turns) :]
        return snapshot

    # -- internals ----------------------------------------------------------

    def _append(self, session_id: str, row: dict[str, Any]) -> None:
        if not self._persist:
            return
        path = self.path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


class TurnPersister:
    """TurnObserver that appends every stored turn to the session log."""

    def __init__(self, store: SessionStore, session_id: str) -> None:
        self._store = store
        self._session_id = session_id

    def on_turn(self, turn: Turn) -> None:
        self._store.append_turn(self._session_id, turn)
