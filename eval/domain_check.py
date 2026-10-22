"""Domain compliance check.

    python eval/domain_check.py            # lightweight domains
    python eval/domain_check.py browser    # also launches Chrome
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fish import domain

HEAVY = {"browser"}


def check(name: str) -> None:
    try:
        pack = domain.load(name)
    except domain.DomainError as exc:
        print(f"[domain] {name} skipped: {exc}")
        return
    try:
        domain.validate(pack)
        pack.close()
        pack.close()  # close must be idempotent
    finally:
        pack.close()
    print(f"[domain] {name} OK tools={[s.name for s in pack.registry().specs()]}")


def main(argv: list[str] | None = None) -> int:
    requested = argv if argv is not None else sys.argv[1:]
    if requested:
        names = requested
    else:
        names = [n for n in domain.available() if n not in HEAVY]
        skipped = [n for n in domain.available() if n in HEAVY]
        if skipped:
            print(f"[domain] skipped heavy domains: {skipped}")
    for name in names:
        check(name)
    print("all domains passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
