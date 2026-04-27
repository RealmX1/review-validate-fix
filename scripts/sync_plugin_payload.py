#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SKILL = ROOT / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"


def run(cmd: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(cmd, cwd=cwd, text=True)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="兼容旧入口：plugin 内 skill 已是 canonical，只做可选契约检查。"
    )
    parser.add_argument(
        "--check-contracts",
        action="store_true",
        help="运行 plugin skill 自带 scripts/check_contracts.sh。",
    )
    args = parser.parse_args()

    if not PLUGIN_SKILL.exists():
        print(f"缺少 plugin skill: {PLUGIN_SKILL}", file=sys.stderr)
        return 2

    if args.check_contracts:
        run(["bash", str(PLUGIN_SKILL / "scripts" / "check_contracts.sh")], cwd=PLUGIN_SKILL)

    print(f"plugin skill 已是 canonical: {PLUGIN_SKILL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
