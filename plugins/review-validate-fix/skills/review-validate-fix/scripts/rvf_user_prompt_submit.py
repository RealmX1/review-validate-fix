#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rvf_prep_file
from session_label import text_from_message_payload


DISPATCH_TOKEN_RE = re.compile(r"\bRVF_DISPATCH=token=([0-9A-Fa-f]{16})\b")


def _latest_user_message_from_transcript(path: Path) -> str | None:
    latest: str | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "event_msg" and payload.get("type") == "user_message":
                    message = payload.get("message")
                    if isinstance(message, str):
                        latest = message
                    continue
                if record.get("type") == "response_item":
                    if payload.get("type") == "message" and payload.get("role") == "user":
                        text = text_from_message_payload(payload)
                        if text:
                            latest = text
    except OSError:
        return None
    return latest


def prompt_text_from_event(event: dict[str, Any]) -> tuple[str | None, str]:
    prompt = event.get("prompt")
    if isinstance(prompt, str):
        return prompt, "prompt"
    direct = event.get("last_user_message")
    if isinstance(direct, str):
        return direct, "last_user_message"
    for key in ("transcript_path", "conversation_path", "session_path"):
        value = event.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        message = _latest_user_message_from_transcript(Path(value).expanduser())
        if message:
            return message, key
    return None, "missing"


def dispatch_token_from_text(text: str | None) -> str | None:
    if not text:
        return None
    match = DISPATCH_TOKEN_RE.search(text)
    if match is None:
        return None
    return match.group(1).lower()


def inspect_user_prompt_submit(
    event: dict[str, Any],
    *,
    prep_root: str | Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    prompt, prompt_source = prompt_text_from_event(event)
    token = dispatch_token_from_text(prompt)
    base_payload: dict[str, Any] = {
        "continue": True,
        "workflow_started": False,
        "prompt_source": prompt_source,
    }
    if token is None:
        return {**base_payload, "status": "no_token"}

    lookup_now = rvf_prep_file.parse_timestamp(now) if now else None
    lookup = rvf_prep_file.read_prep_file(token, root=prep_root, now=lookup_now)
    payload = {
        **base_payload,
        "status": lookup.status,
        "token": token,
        "prep_file_path": str(lookup.path),
    }
    diagnostic: dict[str, Any] = {
        "event": "user_prompt_submit_dispatch_probe",
        "status": lookup.status,
        "workflow_started": False,
        "prep_file_path": str(lookup.path),
        "prompt_source": prompt_source,
    }
    for key in ("cwd", "hook_event_name", "session_id", "agent_id", "agent_type"):
        value = event.get(key)
        if isinstance(value, str) and value:
            diagnostic[key] = value
    if lookup.error:
        diagnostic["error"] = lookup.error
        payload["error"] = lookup.error
    try:
        diagnostic_path = rvf_prep_file.append_diagnostic(
            root=prep_root,
            token=token,
            record=diagnostic,
        )
        payload["diagnostic_path"] = str(diagnostic_path)
    except (OSError, rvf_prep_file.PrepFileError) as exc:
        payload["diagnostic_error"] = str(exc)
    return payload


def read_event_stdin() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect RVF UserPromptSubmit dispatch tokens without starting workflow.")
    parser.add_argument("--prep-root", default=None, help="Override RVF prep file root for tests or local diagnostics.")
    parser.add_argument("--now", default=None, help="Override current UTC timestamp for deterministic tests.")
    parser.add_argument("--json", action="store_true", help="Emit detector result JSON. Actual hook mode stays silent.")
    args = parser.parse_args()

    result = inspect_user_prompt_submit(read_event_stdin(), prep_root=args.prep_root, now=args.now)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
