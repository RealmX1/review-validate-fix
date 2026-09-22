#!/usr/bin/env python3
"""Kimi Code CLI Stop hook shim for review-validate-fix。

薄 shim：把 Kimi Stop event 归一后转发给同 plugin 内的 RVF 核心
``skills/review-validate-fix/scripts/stop_review_validate_fix.py``。共享逻辑（事件归一 +
stdin→normalize→subprocess→fail-open 骨架）收在 sibling ``_kimi_hook_entry`` 单一契约里。

⚠️ **退出码恒 0**。Kimi 的 ``resultFromExitCode`` 只认 ``0``（解析 stdout）与 ``2``（block，
reason 取 stderr）；其他任意码都会让 stdout 被完全丢弃并 allow，且无任何报错。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 自举 SCRIPT_DIR 上 sys.path，使 sibling ``_kimi_hook_entry`` 在脚本直跑与单测加载两种
# 上下文都可 import。stdlib-only、不触 core/adapters，保 hook 的 fail-open 鲁棒性。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _kimi_hook_entry import run_kimi_hook  # noqa: E402


def main() -> int:
    return run_kimi_hook(
        event_name="Stop",
        core_script=(
            "skills",
            "review-validate-fix",
            "scripts",
            "stop_review_validate_fix.py",
        ),
        timeout_env="KIMI_RVF_STOP_HOOK_TIMEOUT",
        default_timeout="115",
        silent_success=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
