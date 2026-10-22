"""One-shot environment setup: python tools/setup.py

Checks the toolchain, installs Python dependencies, creates .env and warms up
the chrome-devtools-mcp download. Safe to re-run.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIN_PYTHON = (3, 10)
ENV_KEYS_QQ = ("FISH_QQ_APPID", "FISH_QQ_SECRET")


def ok(text: str) -> None:
    print(f"  [ok] {text}")


def warn(text: str) -> None:
    print(f"  [warn] {text}")


def fail(text: str) -> None:
    print(f"  [fail] {text}")


def check_python() -> bool:
    print("1) Python")
    if sys.version_info < MIN_PYTHON:
        fail(f"需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+，当前 {sys.version.split()[0]}")
        return False
    ok(f"Python {sys.version.split()[0]}")
    return True


def install_deps() -> bool:
    print("2) Python 依赖")
    req = ROOT / "requirements.txt"
    if not req.is_file():
        warn("requirements.txt 不存在，跳过")
        return True
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(req)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        fail("pip install 失败：")
        print((proc.stderr or proc.stdout)[-800:])
        return False
    ok("依赖已安装")
    try:
        import botpy  # noqa: F401

        ok("qq-botpy 可用")
    except ImportError:
        warn("qq-botpy 未安装成功，QQ 通道将不可用")
    return True


def setup_env() -> bool:
    print("3) 配置文件")
    env = ROOT / ".env"
    example = ROOT / ".env.example"
    if env.is_file():
        ok(".env 已存在（不覆盖）")
    elif example.is_file():
        env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        warn("已从 .env.example 创建 .env，请填写 FISH_API_KEY / QQ 凭据")
    else:
        warn("没有 .env.example，跳过")
    if env.is_file():
        text = env.read_text(encoding="utf-8")
        missing = [key for key in ("FISH_API_KEY",) + ENV_KEYS_QQ if key not in text]
        if missing:
            warn("以下键尚未配置：" + ", ".join(missing))
    return True


def check_node() -> bool:
    print("4) Node / chrome-devtools-mcp")
    npx = shutil.which("npx")
    if npx is None:
        fail("未找到 npx（Node 20+），浏览器域不可用")
        return False
    proc = subprocess.run(
        "npx -y chrome-devtools-mcp@1.11.0 --version",
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if proc.returncode != 0:
        fail("chrome-devtools-mcp 启动失败，检查网络或 npm")
        return False
    ok(f"chrome-devtools-mcp {proc.stdout.strip()}")
    return True


def check_codemaker() -> None:
    print("5) codemaker CLI（可选，免费开发后端）")
    if shutil.which("codemaker"):
        ok("codemaker 可用")
    else:
        warn("未安装 codemaker，只能使用 --backend deepseek")


def check_popo() -> None:
    print("6) POPO 通道（可选）")
    config = Path.home() / ".codemaker" / "mcps.json"
    if config.is_file():
        ok("发现 ~/.codemaker/mcps.json，POPO 通知可用")
    else:
        warn("没有 ~/.codemaker/mcps.json，POPO 通道不可用")
    if shutil.which("popo-cli"):
        ok("popo-cli 可用（POPO 对话轮询）")
    else:
        warn("未安装 popo-cli，POPO 只能单向推送")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 56)
    print("MahoFish 环境检查")
    print("=" * 56)
    results = [check_python(), install_deps(), setup_env(), check_node()]
    check_codemaker()
    check_popo()
    print("-" * 56)
    if all(results):
        print("环境就绪。常用命令：")
        print("  python main.py --goal \"...\"                       # 单次任务")
        print("  python -m fish.server --channels popo,qq           # 常驻服务")
        print("  python -m fish.scheduler --once                    # 定时任务")
        print("  python eval/selftest.py                            # 离线自测")
        return 0
    print("有检查未通过，请按上面提示处理后重跑 tools/setup.py")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
