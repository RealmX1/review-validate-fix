#!/usr/bin/env python3
"""Kimi Code CLI UserPromptSubmit hook shim for review-validate-fix。

薄 shim：转发给 ``skills/review-validate-fix/scripts/rvf_user_prompt_submit.py``。
``silent_success=True``：无 dispatch token / marker / manual trigger 时不输出，让 prompt
原样继续。

事件名一律取 ``_kimi_hook_entry.KIMI_HOOK_EVENT_NAME_USER_PROMPT_SUBMIT``，不在本文件另存
一份字面量——理由见该常量的 docstring（分叉会让出口翻译静默退化成逐字透传）。

⚠️ 退出码恒 0，理由同 ``kimi_stop.py``。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _kimi_hook_entry import (  # noqa: E402
    KIMI_HOOK_EVENT_NAME_USER_PROMPT_SUBMIT,
    run_kimi_hook,
)


def main() -> int:
    return run_kimi_hook(
        # ⚠️ 必须传常量，**绝不能**写裸字面量 ``"UserPromptSubmit"``：``run_kimi_hook``
        # 正是以「``event_name`` 是否等于该常量」决定要不要做出口线材翻译 / 抑制
        # developer-only 诊断。字面量一旦与常量分叉，两条分支同时静默失效、退回逐字
        # 透传 core stdout —— 而 Kimi 在 UserPromptSubmit 上会把整段 stdout 当正文注入
        # 模型上下文。传常量后该判据恒真，字符串取值本身不再参与分支。详见常量 docstring。
        event_name=KIMI_HOOK_EVENT_NAME_USER_PROMPT_SUBMIT,
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
