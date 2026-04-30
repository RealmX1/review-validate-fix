#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SKILL = ROOT / "plugins" / "review-validate-fix" / "skills" / "review-validate-fix"
CONTRACT_SCRIPT = ROOT / "scripts" / "check_skill_contracts.sh"


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 review-validate-fix plugin 契约检查。")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示底层验证与测试命令输出。")
    args = parser.parse_args()

    if not PLUGIN_SKILL.exists():
        print(f"缺少 plugin skill: {PLUGIN_SKILL}", file=sys.stderr)
        return 2
    if not CONTRACT_SCRIPT.exists():
        print(f"缺少契约检查脚本: {CONTRACT_SCRIPT}", file=sys.stderr)
        return 2

    command = ["bash", str(CONTRACT_SCRIPT)]
    if args.verbose:
        command.append("--verbose")
        completed = subprocess.run(command, cwd=ROOT, text=True)
    else:
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        if completed.returncode != 0:
            print("plugin 契约检查失败", file=sys.stderr)
            if completed.stdout:
                print(completed.stdout, end="", file=sys.stderr)
            if completed.stderr:
                print(completed.stderr, end="", file=sys.stderr)
        else:
            print("plugin 契约检查通过")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
