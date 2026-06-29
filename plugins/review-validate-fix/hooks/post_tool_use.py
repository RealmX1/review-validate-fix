#!/usr/bin/env python3
"""Claude Code PostToolUse hook shim for review-validate-fix.

薄 shim：把 Claude PostToolUse event 转发给同 plugin 内的 RVF 核心
``skills/review-validate-fix/scripts/rvf_post_tool_use.py``。该核心在「主 agent 本回合
首次写型工具落地」时 park 父 Kanban 卡片（race-free 落点，见核心文件 docstring）。

所有共享逻辑（host-ownership 守卫 + stdin→normalize→subprocess→fail-open 骨架）收在
sibling ``_claude_hook_entry`` 单一契约里。取静默成功语义：park 是纯副作用、无 payload
回灌，成功无输出时不报信。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _claude_hook_entry import run_claude_hook  # noqa: E402


def main() -> int:
    return run_claude_hook(
        event_name="PostToolUse",
        core_script=(
            "skills",
            "review-validate-fix",
            "scripts",
            "rvf_post_tool_use.py",
        ),
        timeout_env="CLAUDE_RVF_POST_TOOL_USE_HOOK_TIMEOUT",
        default_timeout="30",
        silent_success=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
