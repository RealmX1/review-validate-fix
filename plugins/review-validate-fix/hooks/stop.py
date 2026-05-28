#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parents[1])).resolve()
RVF_CORE = PLUGIN_ROOT / "skills" / "review-validate-fix" / "scripts" / "codex_stop_review_validate_fix.py"


def emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def _is_codex_invocation(event: dict[str, object]) -> bool:
    """判断本次 Stop hook 是否被 Codex 旁路触发（应当 no-op）。

    背景：Codex plugin loader 把 plugin-packaged ``hooks/hooks.json`` 也当
    成 hooks 源加载（见 ``~/.codex/config.toml`` ``[hooks.state.
    review-validate-fix@local-codex-plugins:hooks/hooks.json:stop:0:0]``），
    与 ``~/.codex/hooks.json`` 里 ``install_to_codex.py`` 注册的 RVF Stop
    entry 平行执行，导致同一 Codex Stop 事件触发两次 RVF。修法对齐
    UPS 侧 commit 6c6ff47：本 shim 是 **Claude Code 专用**——一旦能正
    向证据判定调用方是 Codex 就静默退出，让 Codex 端那个直调核心的
    entry 独自处理。

    检测策略（保守，仅在正向证据时返回 True）：
    1. 事件里任一会话路径键（``transcript_path`` / ``conversation_path`` /
       ``session_path`` / ``session_file``）落在 ``/.codex/sessions/`` 下
       → Codex 转写文件路径，确诊 Codex。
    2. 兜底：未匹配则返回 False（按 Claude 跑，杜绝把 Claude 误判成
       Codex 而失声）。
    """
    for key in ("transcript_path", "conversation_path", "session_path", "session_file"):
        value = event.get(key)
        if isinstance(value, str) and "/.codex/sessions/" in value:
            return True
    return False


def main() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        emit(
            {
                "continue": True,
                "systemMessage": "review-validate-fix Claude Stop hook skipped: invalid JSON input.",
            }
        )
        return 0

    if not isinstance(event, dict):
        event = {}

    if _is_codex_invocation(event):
        # Codex 通过 ``~/.codex/hooks.json`` 里 installer 注册的 entry 直
        # 调核心；这个 plugin-packaged shim 是 Claude Code 专用。静默退出
        # 防止同一 Codex Stop 事件触发两次 RVF。
        return 0

    event.setdefault("source", {"provider": "claude-code", "plugin": "review-validate-fix"})
    event.setdefault("hook_event_name", "Stop")
    if not event.get("cwd"):
        event["cwd"] = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    env = os.environ.copy()
    env.setdefault("CODEX_RVF_CLINE_KANBAN_AGENT_ID", "claude")
    env.setdefault("CODEX_RVF_LOG_ROOT", str(Path.home() / ".claude" / "rvf"))
    env.setdefault("CODEX_RVF_DEV_SYNC", "0")

    try:
        completed = subprocess.run(
            [sys.executable, str(RVF_CORE)],
            input=json.dumps(event, ensure_ascii=False),
            capture_output=True,
            text=True,
            env=env,
            timeout=float(env.get("CLAUDE_RVF_STOP_HOOK_TIMEOUT", "115")),
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - hooks must fail open.
        emit(
            {
                "continue": True,
                "systemMessage": (
                    "review-validate-fix Claude Stop hook failed before dispatch: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }
        )
        return 0

    if completed.stdout.strip():
        sys.stdout.write(completed.stdout)
        return 0

    detail = (completed.stderr or "").strip()
    message = "review-validate-fix Claude Stop hook completed without payload."
    if completed.returncode != 0:
        message = f"review-validate-fix Claude Stop hook exited {completed.returncode}."
    if detail:
        message += f" stderr={detail[:500]}"
    emit({"continue": True, "systemMessage": message})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
