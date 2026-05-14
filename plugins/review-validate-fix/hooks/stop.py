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
