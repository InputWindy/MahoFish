"""Entry point: let an LLM drive a real Chrome and report what it finds.

Example:
    python main.py
    python main.py --goal "打开 bilibili.com，总结首页推荐的前 5 个视频" --headless
"""

from __future__ import annotations

import argparse
import sys

from fish import domain
from fish.runtime import EpisodeConfig, run_episode

DEFAULT_GOAL = (
    "打开 https://www.bilibili.com ，读取首页推荐视频列表，"
    "输出前 5 个视频的标题、UP 主、播放量，并一句话总结今天首页的推荐倾向。"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LLM-driven browser agent (Chrome DevTools MCP)."
    )
    parser.add_argument("--goal", default=DEFAULT_GOAL, help="what the agent should do")
    parser.add_argument(
        "--backend",
        choices=["codemaker", "deepseek"],
        default="codemaker",
        help="codemaker = local cli for development (default); deepseek = release",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="override FISH_MODEL (default: deepseek-chat)",
    )
    parser.add_argument("--max-steps", type=int, default=12, help="step budget")
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="retries per LLM call before the run stops with 'llm error'",
    )
    parser.add_argument("--max-tokens", type=int, default=200_000, help="token budget")
    parser.add_argument(
        "--timeout", type=float, default=1800.0, help="wall-clock budget in seconds"
    )
    parser.add_argument(
        "--allow-dangerous",
        action="store_true",
        help="allow mutating tools (click/fill)",
    )
    parser.add_argument(
        "--domain",
        choices=domain.available(),
        default="none",
        help="domain pack to assemble (default: none = core only)",
    )
    parser.add_argument(
        "--headless", action="store_true", help="run Chrome without a visible window"
    )
    parser.add_argument("--quiet", action="store_true", help="hide step trace")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)

    def on_llm(name: str, model: str) -> None:
        print(f"[fish] backend={name} model={model} goal={args.goal}", flush=True)

    def on_tools(name: str, tools: list[str]) -> None:
        print(f"[fish] domain={name} tools={tools}", flush=True)

    def on_event(step: int, kind: str, text: str) -> None:
        if kind == "fault":
            print(f"[{step:02d}]   FAULT | {text}", file=sys.stderr, flush=True)
            return
        if not args.quiet:
            print(f"[{step:02d}] {kind:>7} | {text}\n", flush=True)

    config = EpisodeConfig(
        goal=args.goal,
        domain=args.domain,
        domain_options={"headless": args.headless},
        backend=args.backend,
        model=args.model,
        max_steps=args.max_steps,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
        max_llm_retries=args.max_retries,
        allow_dangerous=args.allow_dangerous,
        on_event=on_event,
        on_tools=on_tools,
        on_llm=on_llm,
    )
    try:
        result = run_episode(config)
    except Exception as exc:
        print(f"[fish] {exc}", file=sys.stderr)
        return 2

    print("\n=== RESULT ===")
    print(result.answer)
    if result.faults:
        print(f"\n[faults] {len(result.faults)} handled failure(s):")
        for fault in result.faults:
            print(f"  - step {fault.step} {fault.source}: {fault.message}")
    print(
        f"\n[stats] steps={result.steps} tokens={result.usage.tokens} "
        f"(estimated={result.usage.estimated_tokens}) "
        f"seconds={result.usage.seconds:.1f} stop={result.stopped_reason}"
    )
    return 0 if result.stopped_reason == "final" else 1


if __name__ == "__main__":
    raise SystemExit(main())
