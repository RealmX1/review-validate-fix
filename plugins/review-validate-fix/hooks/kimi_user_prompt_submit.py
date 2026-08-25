#!/usr/bin/env python3
"""Kimi Code CLI UserPromptSubmit hook shim for review-validate-fix。

薄 shim：转发给 ``skills/review-validate-fix/scripts/rvf_user_prompt_submit.py``。
``silent_success=True``：无 dispatch token / marker / manual trigger 时不输出，让 prompt
原样继续。

⚠️ 退出码恒 0，理由同 ``kimi_stop.py``。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _kimi_hook_entry import run_kimi_hook  # noqa: E402


def main() -> int:
    return run_kimi_hook(
        event_name="UserPromptSubmit",
        core_script=(
            "skills",
            "review-validate-fix",
            "scripts",
            "rvf_user_prompt_submit.py",
        ),
        timeout_env="KIMI_RVF_USER_PROMPT_HOOK_TIMEOUT",
        default_timeout="85",
        silent_success=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
