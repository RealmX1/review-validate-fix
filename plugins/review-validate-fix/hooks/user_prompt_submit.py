#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook shim for review-validate-fix.

薄 shim：把 Claude UserPromptSubmit event 转发给同 plugin 内的共享核心
``skills/review-validate-fix/scripts/rvf_user_prompt_submit.py``（Codex 经
``~/.codex/hooks.json`` 用同一核心）。转发核心 stdout 让 manual auto-prep
路径把 ``hookSpecificOutput.additionalContext`` 回灌 Claude 会话，并让 Cline
Kanban dispatch 路径自回填 ``child_session_id`` / ``child_transcript_path``。

所有共享逻辑（host-ownership 守卫 + stdin→normalize→subprocess→fail-open
骨架）收在 sibling ``_claude_hook_entry`` 单一契约里（S3 / handoff G）。
UPS 取静默成功语义：成功无输出时不报信、让 prompt 原样继续。
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
        event_name="UserPromptSubmit",
        core_script=(
            "skills",
            "review-validate-fix",
            "scripts",
            "rvf_user_prompt_submit.py",
        ),
        timeout_env="CLAUDE_RVF_USER_PROMPT_HOOK_TIMEOUT",
        default_timeout="85",
        silent_success=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
