#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SKILL = ROOT / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"
CONTRACT_SCRIPT = ROOT / "scripts" / "check_skill_contracts.sh"


def main() -> int:
    if not PLUGIN_SKILL.exists():
        print(f"缺少 plugin skill: {PLUGIN_SKILL}", file=sys.stderr)
        return 2
    if not CONTRACT_SCRIPT.exists():
        print(f"缺少契约检查脚本: {CONTRACT_SCRIPT}", file=sys.stderr)
        return 2

    completed = subprocess.run(
        ["bash", str(CONTRACT_SCRIPT)],
        cwd=ROOT,
        text=True,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
