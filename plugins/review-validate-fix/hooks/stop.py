#!/usr/bin/env python3
"""Claude Code Stop hook shim for review-validate-fix.

薄 shim：把 Claude Stop event 转发给同 plugin 内的 RVF 核心
``skills/review-validate-fix/scripts/codex_stop_review_validate_fix.py``。所有
共享逻辑（host-ownership 守卫 + stdin→normalize→subprocess→fail-open 骨架）
收在 sibling ``_claude_hook_entry`` 单一契约里（S3 / handoff G）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 自举 SCRIPT_DIR 上 sys.path，使 sibling ``_claude_hook_entry`` 在脚本直跑、
# 经 ``spec_from_file_location`` 单测加载 两种上下文都可 import。stdlib-only、
# 不触 core/adapters，保 hook 的 fail-open 鲁棒性。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _claude_hook_entry import run_claude_hook  # noqa: E402


def main() -> int:
    return run_claude_hook(
        event_name="Stop",
        core_script=(
            "skills",
            "review-validate-fix",
            "scripts",
            "codex_stop_review_validate_fix.py",
        ),
        timeout_env="CLAUDE_RVF_STOP_HOOK_TIMEOUT",
        default_timeout="115",
        silent_success=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
