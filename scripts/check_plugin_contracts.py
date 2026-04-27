#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SKILL = ROOT / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"


def main() -> int:
    if not PLUGIN_SKILL.exists():
        print(f"缺少 plugin skill: {PLUGIN_SKILL}", file=sys.stderr)
        return 2

    completed = subprocess.run(
        ["bash", str(PLUGIN_SKILL / "scripts" / "check_contracts.sh")],
        cwd=PLUGIN_SKILL,
        text=True,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
