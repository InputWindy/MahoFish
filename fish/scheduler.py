"""Scheduled episodes.

    python -m fish.scheduler --jobs fish/jobs.example.json --once   # run now
    python -m fish.scheduler --jobs fish/jobs.example.json          # daemon

Each job is one bounded agent episode; the scheduler never touches the loop's
internals. Cron/Windows Task Scheduler can replace ``--loop`` entirely.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .notify import Notifier, NotifierError, build_notifier
from .runtime import EpisodeConfig, run_episode

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / ".fish" / "scheduler_state.json"


def load_jobs(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    if not isinstance(jobs, list) or not jobs:
        raise SystemExit(f"no jobs in {path}")
    for job in jobs:
        if "id" not in job or "goal" not in job:
            raise SystemExit(f"job missing id/goal: {job}")
        job.setdefault("domain", "none")
        job.setdefault("backend", "deepseek")
        job.setdefault("notifier", "file")
        job.setdefault("every_seconds", 24 * 3600)
    return jobs


def load_state() -> dict[str, float]:
    if not STATE_FILE.is_file():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(key): float(value) for key, value in data.items()}


def save_state(state: dict[str, float]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def is_due(job: dict[str, Any], state: dict[str, float], now: float) -> bool:
    last = state.get(job["id"], 0.0)
    return now - last >= float(job["every_seconds"])


def render(title: str, result, job: dict[str, Any]) -> str:
    lines = [
        f"goal: {job['goal']}",
        f"domain: {job['domain']}  backend: {job['backend']}",
        "",
        result.answer,
        "",
        "---",
        f"steps={result.steps} tokens={result.usage.tokens} "
        f"seconds={result.usage.seconds:.1f} stop={result.stopped_reason}",
    ]
    if result.faults:
        lines.append(f"faults={len(result.faults)}")
        for fault in result.faults:
            lines.append(f"  - step {fault.step} {fault.source}: {fault.message}")
    return "\n".join(lines)


def run_job(job: dict[str, Any], verbose: bool) -> tuple[bool, str]:
    notifier: Notifier = build_notifier(str(job["notifier"]))
    on_event = None
    if verbose:
        def on_event(step: int, kind: str, text: str) -> None:
            print(f"[{job['id']}:{step:02d}] {kind:>7} | {text}", flush=True)

    started = datetime.now()
    try:
        result = run_episode(
            EpisodeConfig(
                goal=str(job["goal"]),
                domain=str(job["domain"]),
                domain_options=dict(job.get("domain_options") or {}),
                backend=str(job["backend"]),
                model=job.get("model"),
                max_steps=int(job.get("max_steps", 12)),
                max_tokens=int(job.get("max_tokens", 200_000)),
                timeout=float(job.get("timeout", 600)),
                allow_dangerous=bool(job.get("allow_dangerous", False)),
                on_event=on_event,
            )
        )
    except Exception as exc:  # a broken job must not stop the scheduler
        text = f"job failed: {type(exc).__name__}: {exc}"
        destination = _safe_send(notifier, f"fish/{job['id']} FAILED", text)
        print(f"[{job['id']}] {text} -> {destination}", flush=True)
        return False, text

    text = render(job["id"], result, job)
    destination = _safe_send(notifier, f"fish/{job['id']}", text)
    ok = result.stopped_reason == "final"
    print(
        f"[{job['id']}] {'ok' if ok else 'stopped'} "
        f"steps={result.steps} tokens={result.usage.tokens} "
        f"{datetime.now() - started} -> {destination}",
        flush=True,
    )
    return ok, text


def _safe_send(notifier: Notifier, title: str, text: str) -> str:
    try:
        return notifier.send(title, text)
    except Exception as exc:
        print(f"[notify] {title} failed: {exc}", file=sys.stderr, flush=True)
        return f"notify failed: {exc}"


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Run scheduled agent jobs.")
    parser.add_argument("--jobs", default=str(ROOT / "fish" / "jobs.example.json"))
    parser.add_argument("--once", action="store_true", help="run every job now")
    parser.add_argument("--interval", type=float, default=60.0, help="poll seconds")
    parser.add_argument("--verbose", action="store_true", help="print step trace")
    args = parser.parse_args(argv)

    try:
        jobs = load_jobs(Path(args.jobs))
    except (OSError, json.JSONDecodeError, NotifierError) as exc:
        print(f"[scheduler] {exc}", file=sys.stderr)
        return 2

    state = {} if args.once else load_state()
    print(f"[scheduler] {len(jobs)} job(s), once={args.once}", flush=True)
    try:
        while True:
            now = time.time()
            for job in jobs:
                if args.once or is_due(job, state, now):
                    ok, _ = run_job(job, args.verbose)
                    state[job["id"]] = time.time()
                    save_state(state)
                    if args.once and not ok:
                        print(f"[scheduler] job {job['id']} did not finish", flush=True)
            if args.once:
                return 0
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        print("\n[scheduler] stopped by user", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
