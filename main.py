"""Entry point: let an LLM drive a real Chrome and report what it finds.

Example:
    python main.py
    python main.py --goal "打开 bilibili.com，总结首页推荐的前 5 个视频" --headless
"""

from __future__ import annotations

import argparse
import sys

from fish.agent import Agent
from fish.llm import LLMError, build_llm
from fish.memory import WindowMemory
from fish.policy import StaticPolicy
from fish.tools import MCPToolRegistry, build_chrome_client

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
        choices=["deepseek", "codemaker"],
        default="deepseek",
        help="deepseek = release; codemaker = local cli for development",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="override FISH_MODEL (default: deepseek-chat)",
    )
    parser.add_argument("--max-steps", type=int, default=12, help="step budget")
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
        "--headless", action="store_true", help="run Chrome without a visible window"
    )
    parser.add_argument("--quiet", action="store_true", help="hide step trace")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)

    try:
        llm = build_llm(args.backend, args.model)
    except LLMError as exc:
        print(f"[llm] {exc}", file=sys.stderr)
        return 2

    print(f"[fish] backend={llm.name} model={llm.model} goal={args.goal}", flush=True)
    try:
        with build_chrome_client(headless=args.headless) as client:
            registry = MCPToolRegistry(client)
            specs = registry.specs()
            print(f"[fish] tools={[s.name for s in specs]}", flush=True)
            if not specs:
                print(
                    "[fish] chrome-devtools-mcp exposed no usable tools",
                    file=sys.stderr,
                )
                return 3

            policy = StaticPolicy(
                allow={s.name for s in specs},
                allow_dangerous=args.allow_dangerous,
                max_steps=args.max_steps,
                max_tokens=args.max_tokens,
                deadline_seconds=args.timeout,
            )
            memory = WindowMemory(goal=args.goal)
            on_event = None
            if not args.quiet:
                def on_event(step: int, kind: str, text: str) -> None:
                    print(f"[{step:02d}] {kind:>7} | {text}\n", flush=True)

            agent = Agent(llm, registry, memory, policy, on_event=on_event)
            result = agent.run()
    finally:
        close = getattr(llm, "close", None)
        if callable(close):
            close()

    print("\n=== RESULT ===")
    print(result.answer)
    print(
        f"\n[stats] steps={result.steps} tokens={result.usage.tokens} "
        f"seconds={result.usage.seconds:.1f} stop={result.stopped_reason}"
    )
    return 0 if result.stopped_reason == "final" else 1


if __name__ == "__main__":
    raise SystemExit(main())
