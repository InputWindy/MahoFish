"""Regression harness: run fixed tasks, judge by rules, diff against baseline.

    python eval/run_tasks.py                    # all tasks
    python eval/run_tasks.py --only arith,json_only
    python eval/run_tasks.py --save-baseline
    python eval/run_tasks.py --backend deepseek
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fish import domain
from fish.agent import Agent
from fish.llm import build_llm
from fish.memory import WindowMemory
from fish.policy import StaticPolicy
from fish.store import JsonFactStore
from fish.summarize import make_summarizer

TASKS_FILE = ROOT / "eval" / "tasks.json"
RESULT_DIR = ROOT / "eval" / "results"
LATEST = RESULT_DIR / "latest.json"
BASELINE = RESULT_DIR / "baseline.json"


def load_tasks(only: set[str] | None) -> list[dict]:
    tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    if only:
        tasks = [task for task in tasks if task["id"] in only]
    if not tasks:
        raise SystemExit("no tasks selected")
    return tasks


def evaluate(answer: str, checks: list[dict]) -> tuple[bool, str]:
    text = answer or ""
    for check in checks:
        kind = check.get("type")
        value = check.get("value")
        if kind == "contains":
            if str(value) not in text:
                return False, f"missing {value!r}"
        elif kind == "regex":
            if not re.search(str(value), text):
                return False, f"regex {value!r} not matched"
        elif kind == "equals":
            if text.strip() != str(value).strip():
                return False, "answer differs from expected"
        elif kind == "min_items":
            pattern = str(check.get("pattern") or value)
            found = len(re.findall(pattern, text))
            if found < int(value):
                return False, f"matched {found} item(s), expected >= {value}"
        elif kind == "min_lines":
            pattern = str(check.get("pattern") or r".")
            found = sum(1 for line in text.splitlines() if re.search(pattern, line))
            if found < int(value):
                return False, f"only {found} matching line(s), expected >= {value}"
        elif kind == "not_contains":
            if str(value) in text:
                return False, f"should not contain {value!r}"
        else:
            return False, f"unknown check type {kind!r}"
    return True, ""


def run_task(task: dict, llm) -> dict:
    started = time.monotonic()
    pack = None
    record = {"id": task["id"], "passed": False, "steps": 0, "tokens": 0,
              "faults": 0, "seconds": 0.0, "note": ""}
    try:
        domains = task.get("domains") or [task.get("domain", "none")]
        packs = [
            domain.load(str(name), **task.get("domain_options", {}))
            for name in domains
        ]
        pack = packs[0] if len(packs) == 1 else domain.CompositeDomain(packs)
        registry = pack.registry()
        specs = registry.specs()
        budget = task.get("budget", {})
        goal = str(task["goal"])
        if task.get("format") == "numbered_list":
            goal += "\n\n输出格式：编号列表，每行一条（1. ...），不要用表格。"
        policy = StaticPolicy(
            allow={spec.name for spec in specs},
            allow_dangerous=bool(budget.get("allow_dangerous", False)),
            max_steps=int(budget.get("max_steps", 8)),
            max_tokens=int(budget.get("max_tokens", 60_000)),
            deadline_seconds=float(budget.get("max_seconds", 180)),
        )
        memory = WindowMemory(
            goal=goal,
            scope=pack.scope_for(task["goal"]),
            facts=JsonFactStore(ROOT / ".fish" / "facts.json"),
        )
        agent = Agent(
            llm,
            registry,
            memory,
            policy,
            summarizer=make_summarizer(llm),
            hints=pack.hints(),
            max_llm_retries=1,
            retry_backoff=0.2,
        )
        result = agent.run()
        passed, note = evaluate(result.answer, task.get("checks", []))
        record.update(
            passed=passed,
            steps=result.steps,
            tokens=result.usage.tokens,
            faults=len(result.faults),
            note=note or result.stopped_reason,
            answer=result.answer,
        )
    except Exception as exc:  # a broken task must not stop the batch
        record["note"] = f"error: {exc}"
    finally:
        if pack is not None:
            try:
                pack.close()
            except Exception:
                pass
        record["seconds"] = round(time.monotonic() - started, 1)
    return record


def previous() -> dict[str, dict]:
    if not BASELINE.is_file():
        return {}
    try:
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {item["id"]: item for item in data.get("tasks", [])}


def report(records: list[dict], baseline: dict[str, dict]) -> None:
    header = f"{'id':<18}{'pass':<6}{'steps':<7}{'tokens':<8}{'faults':<8}{'secs':<7}delta"
    print(header)
    print("-" * len(header))
    for record in records:
        mark = "PASS" if record["passed"] else "FAIL"
        old = baseline.get(record["id"])
        delta = ""
        if old is not None:
            delta = f"was {'PASS' if old['passed'] else 'FAIL'}"
            if old["passed"] and not record["passed"]:
                delta += " REGRESSED"
        print(
            f"{record['id']:<18}{mark:<6}{record['steps']:<7}{record['tokens']:<8}"
            f"{record['faults']:<8}{record['seconds']:<7}{delta}"
        )
        if not record["passed"]:
            print(f"    -> {record['note']}")
    passed = sum(1 for record in records if record["passed"])
    print("-" * len(header))
    print(f"{passed}/{len(records)} passed")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Run the regression task set.")
    parser.add_argument("--only", default="", help="comma separated task ids")
    parser.add_argument(
        "--backend",
        default="deepseek",
        choices=["codemaker", "deepseek"],
        help="codemaker cannot reliably drive external tools, use deepseek",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--save-baseline", action="store_true")
    args = parser.parse_args(argv)

    only = {part.strip() for part in args.only.split(",") if part.strip()} or None
    tasks = load_tasks(only)
    if args.backend == "codemaker" and any(
        task.get("domain", "none") != "none" for task in tasks
    ):
        print(
            "[warn] codemaker backend tends to answer from its own environment "
            "prompt and ignore external tools; expect flaky tool tasks",
            file=sys.stderr,
        )
    llm = build_llm(args.backend, args.model)
    try:
        records = [run_task(task, llm) for task in tasks]
    finally:
        close = getattr(llm, "close", None)
        if callable(close):
            close()

    baseline = previous()
    report(records, baseline)

    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"backend": args.backend, "tasks": records}
    LATEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.save_baseline:
        BASELINE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"baseline saved to {BASELINE.relative_to(ROOT)}")
    return 0 if all(record["passed"] for record in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
