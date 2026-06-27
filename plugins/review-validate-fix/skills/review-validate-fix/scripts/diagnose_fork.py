#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_stop_review_validate_fix as stop_hook


def read_event() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return event if isinstance(event, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose Codex app-server fork behavior outside the Stop hook path.",
    )
    parser.add_argument(
        "--mode",
        choices=["gui", "app-server", "auto", "manual", "dry-run"],
        help="Temporary CODEX_RVF_FORK_EXPERIMENT_MODE override.",
    )
    parser.add_argument(
        "--message",
        default=f"{stop_hook.FORK_EXPERIMENT_MARKER}: diagnose fork behavior",
        help="Diagnostic user message recorded in the run summary.",
    )
    args = parser.parse_args()

    if args.mode:
        os.environ["CODEX_RVF_FORK_EXPERIMENT_MODE"] = args.mode

    event = read_event()
    cwd_value = event.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else None
    ledger = stop_hook.start_run("stop-hook", repo=cwd, cwd=cwd)
    payload = stop_hook.run_fork_experiment(event, args.message, ledger)
    stop_hook.emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
