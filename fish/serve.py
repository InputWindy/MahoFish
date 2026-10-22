"""Supervisor: run ``fish.server`` as a child, rotate its log, restart on exit.

    python -m fish.serve --log .fish/server.log --channels popo,qq,wechat,wecom

Unknown arguments are forwarded to ``fish.server`` (see ``serve.bat``).
Rotation is size based (default 5 MB x 5 files) because the child's stdout is a
pipe we own, which is the only reliable way on Windows.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BACKOFF = 60.0
MIN_BACKOFF = 5.0


class RotatingLog:
    def __init__(self, path: Path, max_bytes: int, backups: int) -> None:
        self.path = path
        self.max_bytes = max(1024, max_bytes)
        self.backups = max(1, backups)
        self._size = 0
        self._handle = None
        self._open()

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            self._size = self.path.stat().st_size
        else:
            self._size = 0
        self._handle = self.path.open("a", encoding="utf-8", errors="replace")

    def _rotate(self) -> None:
        if self._handle is not None:
            self._handle.close()
        for index in range(self.backups - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{index}")
            dst = self.path.with_name(f"{self.path.name}.{index + 1}")
            if src.is_file():
                dst.unlink(missing_ok=True)
                src.rename(dst)
        first = self.path.with_name(f"{self.path.name}.1")
        first.unlink(missing_ok=True)
        if self.path.is_file():
            self.path.rename(first)
        self._open()

    def write(self, line: str) -> None:
        data = line if line.endswith("\n") else line + "\n"
        if self._handle is None:
            self._open()
        self._handle.write(data)
        self._handle.flush()
        self._size += len(data.encode("utf-8"))
        if self._size >= self.max_bytes:
            self._rotate()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def pump(stream, log: RotatingLog) -> None:
    for line in stream:
        log.write(line.rstrip("\n"))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Supervise the MahoFish server.")
    parser.add_argument("--log", default=str(ROOT / ".fish" / "server.log"))
    parser.add_argument("--max-mb", type=float, default=5.0)
    parser.add_argument("--backups", type=int, default=5)
    args, passthrough = parser.parse_known_args(argv)

    log = RotatingLog(Path(args.log), int(args.max_mb * 1024 * 1024), args.backups)
    child_args = [sys.executable, "-m", "fish.server", *passthrough]
    attempt = 0
    current: subprocess.Popen | None = None

    log.write(f"=== supervisor start {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    log.write("cmd: " + " ".join(child_args))
    try:
        while True:
            child = subprocess.Popen(
                child_args,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            current = child
            log.write(
                f"--- child pid={child.pid} attempt={attempt + 1} "
                f"at {time.strftime('%H:%M:%S')} ---"
            )
            try:
                assert child.stdout is not None
                pump(child.stdout, log)
            except Exception as exc:
                log.write(f"pump error: {exc}")
            code = child.wait()
            log.write(f"--- child exited code={code} ---")
            attempt += 1
            delay = min(MAX_BACKOFF, MIN_BACKOFF * attempt)
            log.write(f"restarting in {delay:.0f}s")
            time.sleep(delay)
    except KeyboardInterrupt:
        log.write("--- supervisor stopped by user ---")
        if current is not None and current.poll() is None:
            current.terminate()
            try:
                current.wait(timeout=10)
            except subprocess.TimeoutExpired:
                current.kill()
        return 0
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
