#!/usr/bin/env python3
"""Pure helpers for deriving a human-readable label for a Codex session.

Extracted from codex_stop_review_validate_fix so dashboards and other tools
can reuse the label/excerpt logic without importing the Stop-hook module.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


DEFAULT_PARENT_CONVERSATION_FALLBACK_CHARS = 60


def text_from_message_payload(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def strip_codex_user_message_preamble(text: str) -> str:
    remaining = text.lstrip()
    while remaining:
        changed = False
        if remaining.startswith("# AGENTS.md instructions for "):
            match = re.search(r"</INSTRUCTIONS>\s*", remaining, flags=re.DOTALL)
            if not match:
                return ""
            remaining = remaining[match.end() :].lstrip()
            changed = True

        for tag in ("environment_context",):
            open_tag = f"<{tag}>"
            close_tag = f"</{tag}>"
            if remaining.startswith(open_tag):
                close_index = remaining.find(close_tag)
                if close_index == -1:
                    return ""
                remaining = remaining[close_index + len(close_tag) :].lstrip()
                changed = True

        if not changed:
            break
    return remaining.strip()


def first_user_message(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
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
                    if isinstance(message, str) and message.strip():
                        cleaned = strip_codex_user_message_preamble(message)
                        if cleaned:
                            return cleaned
                    continue

                if record.get("type") == "response_item":
                    if payload.get("type") == "message" and payload.get("role") == "user":
                        text = text_from_message_payload(payload)
                        cleaned = strip_codex_user_message_preamble(text)
                        if cleaned:
                            return cleaned
    except (OSError, UnicodeDecodeError):
        return None
    return None


def single_line_excerpt(text: str, max_chars: int) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed.replace('"', "'")[:max_chars].strip()


def parent_conversation_fallback_chars() -> int:
    raw = os.environ.get("RVF_PARENT_CONVERSATION_FALLBACK_CHARS")
    if raw is None or not raw.strip():
        return DEFAULT_PARENT_CONVERSATION_FALLBACK_CHARS
    try:
        return max(12, int(raw))
    except ValueError:
        return DEFAULT_PARENT_CONVERSATION_FALLBACK_CHARS


def codex_session_label(path: Path | None, *, max_chars: int | None = None) -> str | None:
    """Return a one-line excerpt of the first user message in a Codex transcript.

    Suitable as a human-readable session display name. Returns None when the
    transcript is missing/unreadable or contains no user message past the
    AGENTS.md / environment_context preamble.
    """
    if path is None:
        return None
    message = first_user_message(path)
    if not message:
        return None
    limit = max_chars if max_chars is not None else parent_conversation_fallback_chars()
    excerpt = single_line_excerpt(message, limit)
    return excerpt or None
