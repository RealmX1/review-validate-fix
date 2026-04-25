#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_SRC = ROOT / "skill" / "review-validate-fix"
PLUGIN_SKILL_DST = ROOT / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"


IGNORE_NAMES = {
    ".DS_Store",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "state",
}


def ignore(_: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORE_NAMES or name.endswith(".pyc")}


def run(cmd: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(cmd, cwd=cwd, text=True)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 canonical skill 同步到 plugin 包装层，避免两份 skill 内容漂移。"
    )
    parser.add_argument(
        "--check-contracts",
        action="store_true",
        help="同步后运行 skill 自带 scripts/check_contracts.sh。",
    )
    args = parser.parse_args()

    if not SKILL_SRC.exists():
        print(f"缺少 canonical skill: {SKILL_SRC}", file=sys.stderr)
        return 2

    if PLUGIN_SKILL_DST.exists():
        shutil.rmtree(PLUGIN_SKILL_DST)
    PLUGIN_SKILL_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SKILL_SRC, PLUGIN_SKILL_DST, ignore=ignore)

    if args.check_contracts:
        run(["bash", str(SKILL_SRC / "scripts" / "check_contracts.sh")], cwd=SKILL_SRC)
        run(["bash", str(PLUGIN_SKILL_DST / "scripts" / "check_contracts.sh")], cwd=PLUGIN_SKILL_DST)

    print(f"已同步: {SKILL_SRC} -> {PLUGIN_SKILL_DST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
