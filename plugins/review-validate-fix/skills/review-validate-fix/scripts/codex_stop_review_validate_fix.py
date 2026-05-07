#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import struct
import hashlib
import base64
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rvf_logging import RunLedger, log_root, normalize_rvf_backend, rvf_state_fields, start_run
from rvf_handoff import handoff_completion_payload, handoff_path_from_event
from rvf_run_finalize import finalize_for_handoff, surface_finalize_record_errors
from rvf_analyze_advisory import (
    RVF_ANALYZE_FOLLOWUP_MARKER,
    surface_rvf_analyze_advisory,
)
from session_manifest import build_manifest
from diff_tracker import (
    LEGACY_REASON_NO_SESSION_OWNED_DIRTY,
    LEGACY_REASON_SESSION_OWNED_DIRTY,
    REASON_NO_UNASSIGNED_REVIEW_SCOPE,
    REASON_MANUAL_SCOPE_ALREADY_COMPLETED,
    REASON_UNASSIGNED_REVIEW_SCOPE_AVAILABLE,
    _disabled as _tracker_disabled,
    _manual_suppression_scope_probe,
    allocate_review_scope,
    find_manual_rvf_run_for_scope_hash,
)
from cline_kanban_client import (
    DEFAULT_START_CMD as DEFAULT_CLINE_KANBAN_START_CMD,
    DEFAULT_START_TIMEOUT_SECONDS as DEFAULT_CLINE_KANBAN_START_TIMEOUT_SECONDS,
    DEFAULT_TASK_CMD as DEFAULT_CLINE_KANBAN_TASK_CMD,
    DEFAULT_TMUX_SESSION as DEFAULT_CLINE_KANBAN_TMUX_SESSION,
)


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_GATE = SKILL_DIR / "scripts" / "review_validate_fix_gate.sh"
DEFAULT_CONFIG = Path.home() / ".codex" / "config.toml"
DEFAULT_STATE_DIR = SKILL_DIR / "state"
DEFAULT_APP_SERVER_CONTROL_SOCKET = (
    Path.home() / ".codex" / "app-server-control" / "app-server-control.sock"
)
DEFAULT_BRIDGE_SOCKET = Path.home() / ".codex" / "app-server-control" / "rvf-app-server.sock"
DEFAULT_BRIDGE_LOG = Path.home() / ".codex" / "app-server-control" / "rvf-app-server.log"
DEFAULT_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DEFAULT_SESSION_HOOK_STATE_DIR = SKILL_DIR / "state" / "session-hook"
DEFAULT_CLINE_KANBAN_CLIENT = SKILL_DIR / "scripts" / "cline_kanban_client.py"
DEFAULT_CLINE_KANBAN_STATE_DIR = Path.home() / ".cline" / "kanban"
DEFAULT_PREPARE_REVIEW_RUN = SKILL_DIR / "scripts" / "prepare_review_run.py"
DEFAULT_HANDOFF_HELPER = SKILL_DIR / "scripts" / "rvf_handoff.py"
KANBAN_TASK_SUPPRESSIONS_DIRNAME = "kanban-task-suppressions"
DEFAULT_FORK_VISIBILITY_TIMEOUT_SECONDS = 8.0
DEFAULT_OPEN_GUI_FORK_ATTEMPTS = 3
DEFAULT_OPEN_GUI_FORK_RETRY_DELAY_SECONDS = 5
DEFAULT_BRIDGE_GUI_UNVERIFIED_POLICY = "auto"
DEFAULT_PARENT_CONVERSATION_FALLBACK_CHARS = 60
FORK_EXPERIMENT_MARKER = "RVF_FORK_EXPERIMENT"
RVF_FORK_MARKER = "RVF_FORKED_REVIEW_VALIDATE_FIX"
CLINE_KANBAN_TASK_MARKER = "RVF_CLINE_KANBAN_TASK"
KANBAN_FOLLOWUP_MARKER = "RVF_KANBAN_FOLLOWUP_TRIGGER"
DEFAULT_KANBAN_FOLLOWUP_LEASE_TTL_SECONDS = 60 * 60
KANBAN_FOLLOWUP_LEASE_TTL_ENV = "CODEX_RVF_KANBAN_FOLLOWUP_LEASE_TTL_SECONDS"
SESSION_HOOK_CONTROL_KEY = "RVF_STOP_HOOK"
SUPPRESS_STOP_HOOK_MARKER = "CODEX_RVF_SUPPRESS_STOP_HOOK=1"
MANUAL_RVF_COMPLETED_AT_KEY = "manual_rvf_completed_at"
MANUAL_RVF_RUN_ID_KEY = "manual_rvf_run_id"
MANUAL_RVF_MARKER_KEYS = (
    MANUAL_RVF_COMPLETED_AT_KEY,
    MANUAL_RVF_RUN_ID_KEY,
    "manual_rvf_updated_at",
    "manual_rvf_expires_at",
    "manual_rvf_repo",
    "manual_rvf_head",
    "manual_rvf_dirty_hash",
)
MANUAL_RVF_MARKER_TTL_SECONDS = 12 * 60 * 60
DEFAULT_RVF_MODE = "fork"
DEFAULT_FORK_LAUNCH_MODE = "auto"
AUTO_FORK_LAUNCH_MODES = {"auto", "detect", "fallback"}
APP_SERVER_CLIENT_INFO = {
    "name": "review-validate-fix-stop-hook",
    "title": "review-validate-fix Stop hook",
    "version": "0.1.0",
}
SUPPRESS_ENV_NAMES = (
    "CODEX_RVF_SUPPRESS",
    "CODEX_RVF_SUPPRESS_STOP_HOOK",
)
SESSION_PATH_KEYS = (
    "transcript_path",
    "session_path",
    "conversation_path",
    "log_path",
    "session_file",
)
SESSION_SCOPE_PATH_KEYS = tuple(key for key in SESSION_PATH_KEYS if key != "log_path")
PLAN_DOC_REVIEW_DIR_PREFIXES = ("docs/", "doc/", ".claude/plans/")
PLAN_DOC_REVIEW_NAME_MARKERS = (
    "plan",
    "blueprint",
    "prd",
    "proposal",
    "decision",
    "scaffold",
    "handoff",
    "roadmap",
    "rfc",
)


@dataclass(frozen=True)
class GateResult:
    status: str
    repo: str | None
    output: str


@dataclass(frozen=True)
class StopDecision:
    action: str
    reason_code: str
    repo: str | None = None
    cwd: str | None = None
    parent_thread_id: str | None = None
    parent_thread_path: Path | None = None
    backend: str = "off"
    message: str = ""
    summary_fields: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    status: str = "skipped"


@dataclass(frozen=True)
class ProviderHealthRequirement:
    provider: str
    reason: str
    command: tuple[str, ...]
    remediation: str


class AppServerError(RuntimeError):
    pass


class AppServerSocketSelectionError(AppServerError):
    def __init__(self, message: str, socket_selection: dict[str, Any]) -> None:
        super().__init__(message)
        self.socket_selection = socket_selection


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def skip_payload(
    reason: str,
    ledger: RunLedger | None = None,
    reason_code: str = "skipped",
    **summary_fields: Any,
) -> dict[str, Any]:
    if ledger is not None:
        ledger.event(
            phase="gate",
            event="skipped",
            status="skipped",
            reason_code=reason_code,
            message=reason,
        )
        return ledger.hook_payload(
            status="skipped",
            reason_code=reason_code,
            message=reason,
            **summary_fields,
        )
    return {
        "continue": True,
        "systemMessage": f"review-validate-fix Stop hook 未创建 fork：{reason}",
    }


def stop_hook_rvf_state_fields(
    *,
    phase: str,
    backend: str | None = None,
    backend_raw: str | None = None,
    prepare_metadata: dict[str, Any] | None = None,
    handoff_path: str | Path | None = None,
    completion_gate: str | None = None,
) -> dict[str, Any]:
    metadata = prepare_metadata or {}
    return rvf_state_fields(
        phase=phase,
        backend=backend,
        backend_raw=backend_raw,
        scope_contract_path=metadata.get("scope_contract"),
        scope_of_work_path=metadata.get("scope_of_work_file"),
        review_packet_path=metadata.get("review_packet"),
        session_manifest_path=metadata.get("session_manifest_file"),
        handoff_path=handoff_path,
        completion_gate=completion_gate,
    )


def state_dir() -> Path:
    return log_root()


def kanban_task_suppression_path(task_id: str) -> Path:
    return state_dir() / KANBAN_TASK_SUPPRESSIONS_DIRNAME / f"{safe_state_key(task_id)}.json"


def write_kanban_task_suppression(
    *,
    task_id: str,
    cwd: str,
    ledger: RunLedger,
) -> str:
    path = kanban_task_suppression_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": task_id,
        "suppress_stop_hook": True,
        "reason": "rvf-created-cline-kanban-task",
        "repo": cwd,
        "run_id": ledger.run_id,
        "run_dir": str(ledger.run_dir),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def read_kanban_task_suppression(task_id: str) -> dict[str, Any] | None:
    path = kanban_task_suppression_path(task_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def session_hook_state_dir() -> Path:
    explicit = os.environ.get("CODEX_RVF_SESSION_HOOK_STATE_DIR")
    if explicit and explicit.strip():
        return Path(explicit).expanduser()

    state_root = os.environ.get("CODEX_RVF_STATE_DIR")
    if state_root and state_root.strip():
        return Path(state_root).expanduser() / "session-hook"

    return DEFAULT_SESSION_HOOK_STATE_DIR


def read_event() -> dict[str, Any] | None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        event = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    return event


def is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def is_falsey(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"0", "false", "no", "n", "off", "skip", "disabled"}


def provider_health_check_enabled() -> bool:
    return not is_falsey(os.environ.get("CODEX_RVF_PROVIDER_HEALTH_CHECK"))


def provider_health_timeout_seconds() -> float:
    raw = os.environ.get("CODEX_RVF_PROVIDER_HEALTH_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return 12.0
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 12.0


def codex_bin() -> str:
    return os.environ.get("CODEX_RVF_CODEX_BIN", "codex")


def safe_state_key(value: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return key[:180] if key else "unknown-session"


def source_marks_subagent(source: Any) -> bool:
    return isinstance(source, dict) and isinstance(source.get("subagent"), dict)


def session_meta_marks_subagent(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    return False
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if isinstance(payload, dict) and source_marks_subagent(payload.get("source")):
                    return True
    except OSError:
        return False
    return False


def event_session_paths(event: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for key in SESSION_PATH_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    return paths


def event_session_scope_paths(event: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for key in SESSION_SCOPE_PATH_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    return paths


def first_readable_session_path(event: dict[str, Any]) -> Path | None:
    for path in event_session_scope_paths(event):
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        try:
            with resolved.open("rb"):
                pass
        except OSError:
            continue
        return resolved
    return None


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


def latest_user_message(path: Path) -> str | None:
    latest: str | None = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
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


def first_user_message(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
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
    except OSError:
        return None
    return None


def user_messages_containing(path: Path, marker: str) -> list[str]:
    messages: list[str] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue

                text = ""
                if record.get("type") == "event_msg" and payload.get("type") == "user_message":
                    message = payload.get("message")
                    text = message if isinstance(message, str) else ""
                elif record.get("type") == "response_item":
                    if payload.get("type") == "message" and payload.get("role") == "user":
                        text = text_from_message_payload(payload)

                if marker in text:
                    messages.append(text)
    except OSError:
        return []
    return messages


def latest_user_message_from_event(event: dict[str, Any]) -> str | None:
    direct = event.get("last_user_message")
    if isinstance(direct, str) and direct:
        return direct

    for path in event_session_paths(event):
        message = latest_user_message(path)
        if message:
            return message
    return None


def session_id_from_path(path: Path) -> str | None:
    meta = session_meta_from_path(path)
    value = meta.get("id")
    return value if isinstance(value, str) and value else None


def session_meta_from_path(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    return {}
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                return payload if isinstance(payload, dict) else {}
    except OSError:
        return {}
    return {}


def session_id_from_event(event: dict[str, Any]) -> str | None:
    value = event.get("session_id")
    if isinstance(value, str) and value:
        return value

    for path in event_session_paths(event):
        session_id = session_id_from_path(path)
        if session_id:
            return session_id
    return None


def parent_thread_path_from_event(event: dict[str, Any]) -> Path | None:
    for path in event_session_paths(event):
        expanded = path.expanduser()
        if expanded.exists() and session_id_from_path(expanded) is not None:
            return expanded.resolve()
    return None


def parent_thread_id_from_event(event: dict[str, Any]) -> str | None:
    for path in event_session_paths(event):
        session_id = session_id_from_path(path.expanduser())
        if session_id:
            return session_id

    env_value = os.environ.get("CODEX_THREAD_ID")
    if env_value and env_value.strip():
        return env_value.strip()

    for key in (
        "thread_id",
        "threadId",
        "conversation_id",
        "conversationId",
        "session_id",
    ):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return session_id_from_event(event)


def short_identifier(value: str | None, fallback: str = "unknown") -> str:
    if not value:
        return fallback
    stripped = value.strip()
    if not stripped:
        return fallback
    first_segment = stripped.split("-", 1)[0]
    if re.match(r"^[A-Fa-f0-9]{8,}(?:-|$)", stripped):
        return first_segment[:12]
    return stripped[:32]


def short_run_ref(run_id: str) -> str:
    match = re.search(r"-([A-Fa-f0-9]{8,})$", run_id)
    if match:
        return match.group(1)[:12]
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]


def transcript_origin_label(path: Path | None, session_id: str | None) -> str | None:
    if path is None:
        return None
    stem = path.stem
    if stem.startswith("rollout-"):
        stem = stem.removeprefix("rollout-")
    match = re.match(
        r"(?P<started>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(?P<session>[A-Za-z0-9]{8,12})",
        stem,
    )
    if match:
        return f"{match.group('started')} {match.group('session')}"
    if session_id:
        return short_identifier(session_id)
    return path.name


def parent_conversation_fallback_chars() -> int:
    raw = os.environ.get("CODEX_RVF_PARENT_CONVERSATION_FALLBACK_CHARS")
    if raw is None or not raw.strip():
        return DEFAULT_PARENT_CONVERSATION_FALLBACK_CHARS
    try:
        return max(12, int(raw))
    except ValueError:
        return DEFAULT_PARENT_CONVERSATION_FALLBACK_CHARS


def single_line_excerpt(text: str, max_chars: int) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed.replace('"', "'")[:max_chars].strip()


def quoted_prompt_session_name(path: Path | None) -> str | None:
    if path is None:
        return None
    message = first_user_message(path)
    if not message:
        return None
    excerpt = single_line_excerpt(message, parent_conversation_fallback_chars())
    if not excerpt:
        return None
    return f'"{excerpt}"'


def name_lookup_confirms_unnamed_thread(name_lookup: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(name_lookup, dict)
        and name_lookup.get("thread_found") is True
        and not name_lookup.get("name")
    )


def parent_conversation_origin(
    *,
    parent_session_id: str | None,
    parent_thread_path: Path | None,
    run_id: str,
    parent_thread_name: str | None = None,
    name_lookup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session_id = parent_session_id or (
        session_id_from_path(parent_thread_path) if parent_thread_path is not None else None
    )
    transcript_path = str(parent_thread_path) if parent_thread_path is not None else None
    name_source = "app_server_name"
    label = parent_thread_name.strip() if isinstance(parent_thread_name, str) else ""
    if not label and name_lookup_confirms_unnamed_thread(name_lookup):
        label = quoted_prompt_session_name(parent_thread_path) or ""
        name_source = "first_user_prompt_fallback" if label else "session_ref_fallback"
    if not label:
        label = f"Codex {transcript_origin_label(parent_thread_path, session_id) or short_identifier(session_id)}"
        name_source = "session_ref_fallback"
    run_ref = short_run_ref(run_id)
    return {
        "label": label,
        "task_title": f"RVF from {label} run {run_ref}",
        "name_source": name_source,
        "name_lookup": name_lookup,
        "session_id": session_id,
        "session_short_id": short_identifier(session_id),
        "codex_url": f"codex://local/{session_id}" if session_id else None,
        "transcript_path": transcript_path,
        "transcript_file": parent_thread_path.name if parent_thread_path is not None else None,
        "run_id": run_id,
        "run_ref": run_ref,
    }


def source_origin_for_kanban_task(
    *,
    task_id: str,
    attempt_id: str | None,
    task_title: str | None,
    task_title_source: str | None,
    fallback_origin: dict[str, Any],
) -> dict[str, Any]:
    title = task_title.strip() if isinstance(task_title, str) else ""
    origin = dict(fallback_origin)
    if title:
        label = title
        name_source = task_title_source or "cline_kanban_task_title"
    else:
        label = f"Cline Kanban task {task_id}"
        name_source = "cline_kanban_task_id_fallback"
    origin.update(
        {
            "label": label,
            "name_source": name_source,
            "source_kind": "cline-kanban-task",
            "kanban_task_id": task_id,
            "kanban_attempt_id": attempt_id,
            "kanban_task_title": title or None,
            "kanban_task_title_source": task_title_source,
            "codex_session_label": fallback_origin.get("label"),
            "codex_session_name_source": fallback_origin.get("name_source"),
        }
    )
    return origin


def value_or_unavailable(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if value is not None:
        text = str(value).strip()
        if text:
            return text
    return "<unavailable>"


def parent_origin_prompt_block(
    *,
    parent_origin: dict[str, Any],
    origin_path: str | None,
) -> str:
    parent_conversation_ref = value_or_unavailable(
        parent_origin.get("label") or parent_origin.get("session_id")
    )
    parent_conversation_source = value_or_unavailable(parent_origin.get("name_source"))
    parent_codex_url = value_or_unavailable(parent_origin.get("codex_url"))
    parent_transcript_path = value_or_unavailable(parent_origin.get("transcript_path"))
    parent_transcript_file = value_or_unavailable(parent_origin.get("transcript_file"))
    parent_origin_path = value_or_unavailable(origin_path)
    lines = [
        "Original Codex conversation metadata:\n"
        f"RVF_PARENT_CONVERSATION_REF: {parent_conversation_ref}\n"
        f"RVF_PARENT_CONVERSATION_NAME: {parent_conversation_ref}\n"
        f"RVF_PARENT_CONVERSATION_NAME_SOURCE: {parent_conversation_source}\n"
        f"RVF_PARENT_CODEX_URL: {parent_codex_url}\n"
        f"RVF_PARENT_TRANSCRIPT_PATH: {parent_transcript_path}\n"
        f"RVF_PARENT_TRANSCRIPT_FILE: {parent_transcript_file}\n"
        f"RVF_ORIGIN_METADATA: {parent_origin_path}\n"
    ]
    if parent_origin.get("source_kind") == "cline-kanban-task":
        lines.append(
            "RVF_PARENT_SOURCE_KIND: cline-kanban-task\n"
            f"RVF_PARENT_KANBAN_TASK_ID: {value_or_unavailable(parent_origin.get('kanban_task_id'))}\n"
            f"RVF_PARENT_KANBAN_ATTEMPT_ID: {value_or_unavailable(parent_origin.get('kanban_attempt_id'))}\n"
            f"RVF_PARENT_KANBAN_TASK_TITLE: {value_or_unavailable(parent_origin.get('kanban_task_title'))}\n"
            "RVF_PARENT_KANBAN_TASK_TITLE_SOURCE: "
            f"{value_or_unavailable(parent_origin.get('kanban_task_title_source'))}\n"
            "RVF_PARENT_CODEX_SESSION_REF: "
            f"{value_or_unavailable(parent_origin.get('codex_session_label'))}\n"
            "RVF_PARENT_CODEX_SESSION_NAME_SOURCE: "
            f"{value_or_unavailable(parent_origin.get('codex_session_name_source'))}\n"
        )
    lines.append(
        "\n"
        "维护 handoff.md 时，`## Origin` 必须逐字保留上面的 original "
        "Codex conversation name/ref、name source、codex URL、transcript path "
        "和 origin metadata path；如果存在 `RVF_PARENT_KANBAN_TASK_ID`，还必须写入 "
        "`source Kanban task id` 和 `source Kanban attempt id`，以便任务改名后仍可反查"
        "当前 task title；不要把 `RVF_PARENT_SESSION_ID` 当成 conversation name source。"
    )
    return "".join(lines)


def parent_origin_summary_fields(
    *,
    parent_session_id: str | None,
    parent_thread_path: Path | None,
    parent_origin: dict[str, Any],
    parent_name_lookup: dict[str, Any],
    origin_path: str | None,
) -> dict[str, Any]:
    return {
        "parent_thread_id": parent_session_id,
        "parent_thread_path": str(parent_thread_path) if parent_thread_path is not None else None,
        "parent_conversation_ref": parent_origin.get("label"),
        "parent_conversation_name": parent_origin.get("label"),
        "parent_conversation_name_source": parent_origin.get("name_source"),
        "parent_thread_name_lookup": parent_name_lookup,
        "parent_codex_url": parent_origin.get("codex_url"),
        "parent_origin_path": origin_path,
        "parent_transcript_file": parent_origin.get("transcript_file"),
        "parent_source_kind": parent_origin.get("source_kind"),
        "parent_kanban_task_id": parent_origin.get("kanban_task_id"),
        "parent_kanban_attempt_id": parent_origin.get("kanban_attempt_id"),
        "parent_kanban_task_title": parent_origin.get("kanban_task_title"),
        "parent_kanban_task_title_source": parent_origin.get("kanban_task_title_source"),
        "parent_codex_session_ref": parent_origin.get("codex_session_label"),
        "parent_codex_session_name_source": parent_origin.get("codex_session_name_source"),
    }


def add_parent_origin_to_rvf_fork_prompt(
    prompt: str,
    *,
    parent_origin: dict[str, Any],
    origin_path: str | None,
) -> str:
    if RVF_FORK_MARKER not in prompt:
        return prompt
    if "RVF_PARENT_CONVERSATION_NAME_SOURCE:" in prompt:
        return prompt
    return (
        f"{prompt.rstrip()}\n\n"
        f"{parent_origin_prompt_block(parent_origin=parent_origin, origin_path=origin_path)}"
    )


def session_hook_state_path(session_id: str) -> Path:
    return session_hook_state_dir() / f"{safe_state_key(session_id)}.json"


def session_hook_id_from_event(event: dict[str, Any]) -> str | None:
    return session_id_from_event(event) or parent_thread_id_from_event(event)


def parse_session_hook_control(text: str | None) -> str | None:
    if not text:
        return None
    pattern = re.compile(
        rf"^\s*{re.escape(SESSION_HOOK_CONTROL_KEY)}\s*:\s*([A-Za-z_-]+)\s*$",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip().lower().replace("_", "-")
    if value in {"off", "disable", "disabled", "skip", "suppress"}:
        return "off"
    if value in {"on", "enable", "enabled", "resume"}:
        return "on"
    if value in {"status", "state"}:
        return "status"
    return None


def read_session_hook_state(session_id: str) -> dict[str, Any] | None:
    path = session_hook_state_path(session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_session_hook_state(session_id: str, state: dict[str, Any]) -> Path:
    path = session_hook_state_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["session_id"] = session_id
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def write_manual_rvf_session_marker(
    *,
    session_id: str,
    run_id: str,
    repo: str | Path | None = None,
    completed_at: str | None = None,
    ttl_seconds: int = MANUAL_RVF_MARKER_TTL_SECONDS,
) -> Path:
    timestamp = completed_at or datetime.now(timezone.utc).isoformat()
    try:
        completed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        completed = datetime.now(timezone.utc)
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    expires_at = datetime.fromtimestamp(completed.timestamp() + ttl_seconds, timezone.utc).isoformat()
    state = read_session_hook_state(session_id) or {}
    marker_update = {
        MANUAL_RVF_COMPLETED_AT_KEY: timestamp,
        MANUAL_RVF_RUN_ID_KEY: run_id,
        "manual_rvf_updated_at": datetime.now(timezone.utc).isoformat(),
        "manual_rvf_expires_at": expires_at,
    }
    snapshot = manual_rvf_dirty_snapshot(Path(repo).expanduser().resolve()) if repo is not None else None
    if snapshot is not None:
        marker_update.update(snapshot)
    state.update(marker_update)
    return write_session_hook_state(session_id, state)


def parse_iso_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def manual_rvf_dirty_snapshot(repo: Path) -> dict[str, str] | None:
    completed_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed_root.returncode != 0:
        return None
    root = Path(completed_root.stdout.strip()).resolve()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode != 0 or status.returncode != 0 or diff.returncode != 0:
        return None
    digest = hashlib.sha256()
    digest.update(head.stdout.encode("utf-8", "replace"))
    digest.update(b"\0")
    digest.update(status.stdout.encode("utf-8", "replace"))
    digest.update(b"\0")
    digest.update(diff.stdout.encode("utf-8", "replace"))
    for raw_line in status.stdout.splitlines():
        if not raw_line.startswith("?? ") or len(raw_line) < 4:
            continue
        rel = raw_line[3:].strip()
        path = root / rel
        if not path.is_file():
            continue
        digest.update(b"\0untracked\0")
        digest.update(rel.encode("utf-8", "replace"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            return None
    return {
        "manual_rvf_repo": str(root),
        "manual_rvf_head": head.stdout.strip(),
        "manual_rvf_dirty_hash": digest.hexdigest(),
    }


def read_manual_rvf_session_marker(session_id: str, repo: str | Path | None = None) -> dict[str, Any] | None:
    state = read_session_hook_state(session_id)
    if state is None:
        return None

    completed_at = state.get(MANUAL_RVF_COMPLETED_AT_KEY)
    run_id = state.get(MANUAL_RVF_RUN_ID_KEY)
    expires_at = state.get("manual_rvf_expires_at")
    if not isinstance(completed_at, str) or not completed_at.strip():
        return None
    if not isinstance(run_id, str) or not run_id.strip():
        return None
    if isinstance(expires_at, str) and expires_at.strip():
        expires = parse_iso_datetime(expires_at)
        if expires is None or datetime.now(timezone.utc) >= expires:
            return None
    else:
        completed = parse_iso_datetime(completed_at)
        if completed is None:
            return None
        if datetime.now(timezone.utc).timestamp() - completed.timestamp() >= MANUAL_RVF_MARKER_TTL_SECONDS:
            return None

    if repo is not None:
        snapshot = manual_rvf_dirty_snapshot(Path(repo).expanduser().resolve())
        if snapshot is None:
            return None
        for key in ("manual_rvf_repo", "manual_rvf_head", "manual_rvf_dirty_hash"):
            if state.get(key) != snapshot[key]:
                return None

    return {
        "session_id": session_id,
        MANUAL_RVF_COMPLETED_AT_KEY: completed_at,
        MANUAL_RVF_RUN_ID_KEY: run_id,
        "manual_rvf_expires_at": expires_at,
        "manual_rvf_repo": state.get("manual_rvf_repo"),
        "manual_rvf_head": state.get("manual_rvf_head"),
        "manual_rvf_dirty_hash": state.get("manual_rvf_dirty_hash"),
        "state_path": str(session_hook_state_path(session_id)),
    }


def clear_manual_rvf_session_marker(session_id: str) -> Path | None:
    state = read_session_hook_state(session_id)
    path = session_hook_state_path(session_id)
    if state is None:
        return None

    for key in MANUAL_RVF_MARKER_KEYS:
        state.pop(key, None)

    if set(state) <= {"session_id"}:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return path

    return write_session_hook_state(session_id, state)


def session_hook_disabled(session_id: str) -> bool:
    state = read_session_hook_state(session_id)
    return state is not None and state.get("enabled") is False


def set_session_hook_enabled(
    *,
    session_id: str,
    enabled: bool,
    latest_user: str | None,
) -> Path | None:
    path = session_hook_state_path(session_id)
    if enabled:
        state = read_session_hook_state(session_id) or {}
        if any(key in state for key in MANUAL_RVF_MARKER_KEYS):
            state.pop("enabled", None)
            state.pop("control", None)
            state.pop("latest_user_message", None)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            return write_session_hook_state(session_id, state)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return path
        return path

    state = read_session_hook_state(session_id) or {}
    state.update(
        {
            "enabled": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "control": SESSION_HOOK_CONTROL_KEY,
            "latest_user_message": latest_user,
        }
    )
    return write_session_hook_state(session_id, state)


def session_hook_control_payload(
    event: dict[str, Any],
    latest_user: str | None,
) -> dict[str, Any] | None:
    action = parse_session_hook_control(latest_user)
    if action is None:
        return None

    session_id = session_hook_id_from_event(event)
    if not session_id:
        return {
            "continue": True,
            "reason_code": "session_hook_gate_unknown_session",
            "systemMessage": (
                "review-validate-fix 无法记录当前 chat session 的 RVF 自动触发 gate："
                "Stop event 未暴露 session id。Stop hook 本身未因此关闭。"
            ),
        }

    if action == "status":
        status = "disabled" if session_hook_disabled(session_id) else "enabled"
        return {
            "continue": True,
            "reason_code": "session_hook_gate_status",
            "control_action": "status",
            "session_hook_gate_state": status,
            "systemMessage": (
                "当前 chat session 的 RVF 自动触发 gate 状态为 "
                f"{status}。这只表示本 session 后续 Stop 是否允许自动启动 RVF "
                "fork/continuation/review；不表示全局 Stop hook 是否安装或运行。"
                f"session_id={session_id}"
            ),
        }

    enabled = action == "on"
    state_path = set_session_hook_enabled(
        session_id=session_id,
        enabled=enabled,
        latest_user=latest_user,
    )
    status = "enabled" if enabled else "disabled"
    reason_code = "session_hook_gate_enabled" if enabled else "session_hook_gate_disabled"
    action_label = "允许" if enabled else "禁止"
    return {
        "continue": True,
        "reason_code": reason_code,
        "control_action": action,
        "session_hook_gate_state": status,
        "state_path": str(state_path) if state_path is not None else None,
        "systemMessage": (
            f"已记录当前 chat session 的 RVF 自动触发 gate 为 {status}，"
            f"即后续 Stop 将{action_label}自动启动 RVF fork/continuation/review。"
            "这不是关闭全局 Stop hook：dispatcher 仍会运行，dev sync 仍可能执行。"
            f"session_id={session_id}; state={state_path}。"
        ),
    }


def string_event_value(event: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def configured_reasoning_effort() -> str | None:
    env_value = os.environ.get("CODEX_RVF_FORK_REASONING_EFFORT")
    if env_value and env_value.strip():
        return env_value.strip()

    config_path = Path(os.environ.get("CODEX_RVF_CODEX_CONFIG", str(DEFAULT_CONFIG)))
    if not config_path.exists():
        return None

    try:
        import tomllib

        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        value = data.get("model_reasoning_effort")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        pass

    pattern = re.compile(r'^model_reasoning_effort\s*=\s*"([^"]+)"\s*$')
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            match = pattern.match(line.strip())
            if match:
                return match.group(1)
    except OSError:
        return None
    return None


def reasoning_effort_for_fork(event: dict[str, Any]) -> str | None:
    return string_event_value(
        event,
        (
            "model_reasoning_effort",
            "reasoning_effort",
            "reasoningEffort",
        ),
    ) or configured_reasoning_effort()


def fork_experiment_prompt(parent_session_id: str, cwd: str | None) -> str:
    cwd_line = cwd or "<unknown cwd>"
    return (
        "Codex fork experiment sidecar session.\n\n"
        f"Parent session id: {parent_session_id}\n"
        f"Parent cwd: {cwd_line}\n\n"
        "请用中文简短回复：\n"
        "1. 你是否看起来是一个新 fork 出来的会话。\n"
        "2. 你能看到的当前工作目录是什么。\n"
        "3. 你是否看到了父会话的上下文。\n\n"
        "不要运行 $review-validate-fix，不要修改文件。"
    )


def fork_review_validate_fix_prompt(
    parent_session_id: str,
    parent_cwd: str | None,
    repo: str,
) -> str:
    cwd_line = parent_cwd or "<unknown cwd>"
    return (
        "$review-validate-fix\n\n"
        f"{RVF_FORK_MARKER}\n"
        f"RVF_PARENT_SESSION_ID: {parent_session_id}\n"
        f"RVF_PARENT_CWD: {cwd_line}\n"
        f"RVF_TARGET_REPO: {repo}\n\n"
        "这是由已配置的 Codex Stop hook 在上一轮停止后 fork 出来的 "
        "review-validate-fix 会话。请基于完整父会话历史和当前未提交改动运行 "
        "review-validate-fix。\n\n"
        f"目标仓库: {repo}\n\n"
        "如果父会话历史里出现 `RVF_STOP_HOOK: off`、`RVF_STOP_HOOK: on` "
        "或 `RVF_STOP_HOOK: status`、`RVF_STOP_HOOK_CHANNEL: ...` 这样的行，"
        "请只把它们视为 Stop hook "
        "会话控制元数据；不要把它们当成用户分配的代码任务、review issue、"
        "research 对象或 scope-of-work 内容。\n\n"
        "从准备阶段开始创建并持续维护 run artifact `handoff.md`。完成后最终回复"
        "第一行输出 `RVF_HANDOFF_FILE: <handoff.md 绝对路径>`，随后只追加"
        "1-3 句极短中文说明 reviewers 和 validate/fixers 做了什么；不要在正文里重复"
        "handoff 文件内容。最终回复前先运行 "
        f"`python3 {shell_quote(str(DEFAULT_HANDOFF_HELPER))} open <handoff.md 绝对路径>` "
        "尝试用默认编辑器打开该 markdown 文件；Stop hook 仍会把 "
        "`RVF_HANDOFF_FILE` marker 作为兜底完成信号处理。"
    )


def kanban_followup_review_validate_fix_prompt(
    *,
    task_id: str,
    attempt_id: str | None,
    target_repo: str,
    cwd: str | None,
    ledger: RunLedger,
    source_origin: dict[str, Any],
    origin_path: str | None,
) -> str:
    attempt_line = f"RVF_CURRENT_ATTEMPT_ID: {attempt_id}\n" if attempt_id else ""
    cwd_line = cwd or "<unknown cwd>"
    origin_block = parent_origin_prompt_block(
        parent_origin=source_origin,
        origin_path=origin_path,
    )
    return (
        "$review-validate-fix\n\n"
        f"{KANBAN_FOLLOWUP_MARKER}\n"
        f"RVF_RUN_ID: {ledger.run_id}\n"
        f"RVF_TARGET_REPO: {target_repo}\n"
        f"RVF_CURRENT_TASK_ID: {task_id}\n"
        f"{attempt_line}"
        f"RVF_CURRENT_CWD: {cwd_line}\n\n"
        f"{origin_block}\n\n"
        "上面的 RVF_PARENT_CONVERSATION_* 字段指本次 follow-up 的定位来源："
        "如果当前会话位于 Cline Kanban task 内，它们应优先使用 Kanban task "
        "title/name，方便开发者在 Kanban UI 中定位；否则使用源 Codex chat session "
        "name/ref。维护 handoff.md 时，`## Origin` 的 `original Codex conversation`、"
        "`conversation name source`、`original Codex URL`、`original transcript` "
        "和 `origin metadata` 必须保留这些值；若存在 `RVF_PARENT_KANBAN_*` 字段，"
        "还必须写 `source Kanban task id`、`source Kanban attempt id`、"
        "`source Kanban task title at trigger`，并让 `generated Kanban task` 写当前 "
        "task/attempt id。即使 task title 之后被开发者改名，后续 agent 也能用 task id "
        "查回当前名称。\n\n"
        "这是由 Cline Kanban host 在当前 task 的 coding agent chat session 中注入的"
        "真实用户消息，用于在同一 task/session 内触发 review-validate-fix。"
        "不要创建新的 Kanban task，不要 fork 新会话，也不要把这条消息当作 hook system context。\n\n"
        "请在当前 task worktree 中运行完整 review-validate-fix。目标仓库为上面的 "
        "`RVF_TARGET_REPO`；如果当前 task worktree 的 repo root 与该路径不同，以当前 task "
        "worktree 为执行位置，并在 handoff 中记录这一点。\n\n"
        "从准备阶段开始创建并持续维护 run artifact `handoff.md`。完成后最终回复"
        "第一行输出 `RVF_HANDOFF_FILE: <handoff.md 绝对路径>`，随后只追加"
        "1-3 句极短中文说明 reviewers 和 validate/fixers 做了什么；不要在正文里重复"
        "handoff 文件内容。最终回复前先运行 "
        f"`python3 {shell_quote(str(DEFAULT_HANDOFF_HELPER))} open <handoff.md 绝对路径>` "
        "尝试用默认编辑器打开该 markdown 文件；Stop hook 仍会把 "
        "`RVF_HANDOFF_FILE` marker 作为兜底完成信号处理。"
    )


def parse_marker_value(text: str, key: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(text)
    return match.group(1) if match else None


def rvf_fork_context(latest_user: str | None) -> dict[str, str] | None:
    if not latest_user or RVF_FORK_MARKER not in latest_user:
        return None
    parent_session_id = parse_marker_value(latest_user, "RVF_PARENT_SESSION_ID")
    parent_cwd = parse_marker_value(latest_user, "RVF_PARENT_CWD")
    target_repo = parse_marker_value(latest_user, "RVF_TARGET_REPO")
    if not parent_session_id or not parent_cwd or not target_repo:
        return None
    return {
        "parent_session_id": parent_session_id,
        "parent_cwd": parent_cwd,
        "target_repo": target_repo,
    }


def rvf_fork_context_from_event(event: dict[str, Any]) -> dict[str, str] | None:
    for path in event_session_paths(event):
        for message in user_messages_containing(path.expanduser(), RVF_FORK_MARKER):
            context = rvf_fork_context(message)
            if context is not None:
                return context
    return None


def session_user_message_contains(event: dict[str, Any], marker: str) -> bool:
    return any(
        user_messages_containing(path.expanduser(), marker)
        for path in event_session_paths(event)
    )


def cline_kanban_script_path(env_name: str, default: Path) -> Path:
    value = os.environ.get(env_name)
    if value and value.strip():
        return Path(value).expanduser()
    return default


def event_or_env_text(
    event: dict[str, Any],
    env_names: tuple[str, ...],
    event_keys: tuple[str, ...],
) -> str | None:
    for name in env_names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return string_event_value(event, event_keys)


def is_codex_agent_id(agent_id: str | None) -> bool:
    if agent_id is None:
        return False
    normalized = agent_id.strip().lower()
    return (
        normalized in {"codex", "codex-cli", "openai-codex"}
        or normalized.startswith("codex:")
        or "codex" in re.split(r"[^a-z0-9]+", normalized)
    )


def provider_health_requirements(
    decision: StopDecision,
    event: dict[str, Any],
) -> list[ProviderHealthRequirement]:
    if decision.backend == "gui":
        return [
            ProviderHealthRequirement(
                provider="codex",
                reason="Legacy GUI/app-server RVF fallback uses Codex as the child session provider.",
                command=(codex_bin(), "login", "status"),
                remediation="请先运行 `codex login`，或使用 `codex login --with-api-key` 配置可用认证。",
            )
        ]

    if decision.backend == "kanban":
        agent_id = os.environ.get("CODEX_RVF_CLINE_KANBAN_AGENT_ID", "codex").strip() or "codex"
        if is_codex_agent_id(agent_id):
            return [
                ProviderHealthRequirement(
                    provider="codex",
                    reason=f"Cline Kanban RVF task will start agent_id={agent_id!r}.",
                    command=(codex_bin(), "login", "status"),
                    remediation=(
                        "请先运行 `codex login`，确认 `codex login status` 成功后再让 "
                        "Stop hook 创建 Cline Kanban RVF task。"
                    ),
                )
            ]

    if decision.backend == "kanban-followup":
        agent_id = (
            event_or_env_text(
                event,
                ("KANBAN_AGENT_ID", "CLINE_KANBAN_AGENT_ID"),
                ("kanban_agent_id", "kanbanAgentId", "agent_id", "agentId"),
            )
            or os.environ.get("CODEX_RVF_CLINE_KANBAN_AGENT_ID", "codex").strip()
            or "codex"
        )
        if is_codex_agent_id(agent_id):
            return [
                ProviderHealthRequirement(
                    provider="codex",
                    reason=f"Cline Kanban follow-up is targeting agent_id={agent_id!r}.",
                    command=(codex_bin(), "login", "status"),
                    remediation="请先运行 `codex login`，再重试 RVF follow-up 注入。",
                )
            ]

    return []


def command_output_text(stdout: str | None, stderr: str | None) -> str:
    return "\n".join(part for part in (stdout or "", stderr or "") if part).strip()


def subprocess_output_text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def codex_login_output_indicates_failure(output: str) -> bool:
    normalized = output.strip().lower()
    if not normalized:
        return False
    failure_markers = (
        "not logged in",
        "not authenticated",
        "logged out",
        "login expired",
        "session expired",
        "expired session",
        "authentication expired",
        "auth expired",
        "invalid credentials",
        "credential expired",
        "token expired",
    )
    return any(marker in normalized for marker in failure_markers)


def run_provider_health_requirement(
    requirement: ProviderHealthRequirement,
    timeout_seconds: float,
) -> dict[str, Any]:
    command = list(requirement.command)
    record: dict[str, Any] = {
        "provider": requirement.provider,
        "reason": requirement.reason,
        "command": command,
        "remediation": requirement.remediation,
        "status": "failed",
    }
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        record.update(
            {
                "returncode": None,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
                "failure_reason": "command_missing",
            }
        )
        return record
    except subprocess.TimeoutExpired as exc:
        record.update(
            {
                "returncode": None,
                "stdout": subprocess_output_text(exc.stdout),
                "stderr": subprocess_output_text(exc.stderr),
                "failure_reason": "timeout",
                "timeout_seconds": timeout_seconds,
            }
        )
        return record
    except Exception as exc:
        record.update(
            {
                "returncode": None,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
                "failure_reason": "error",
            }
        )
        return record

    output = command_output_text(completed.stdout, completed.stderr)
    failed = completed.returncode != 0
    if requirement.provider == "codex" and codex_login_output_indicates_failure(output):
        failed = True
    record.update(
        {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "status": "failed" if failed else "ok",
            "failure_reason": "nonzero_or_auth_unhealthy" if failed else None,
        }
    )
    return record


def maybe_start_codex_login(ledger: RunLedger) -> dict[str, Any] | None:
    if not is_truthy(os.environ.get("CODEX_RVF_AUTO_CODEX_LOGIN")):
        return None

    log_path = ledger.artifacts_dir / "codex-login.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("ab") as log_file:
            process = subprocess.Popen(
                [codex_bin(), "login"],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
    except Exception as exc:
        return {
            "started": False,
            "error": f"{type(exc).__name__}: {exc}",
            "command": [codex_bin(), "login"],
            "log_path": str(log_path),
        }

    return {
        "started": True,
        "pid": process.pid,
        "command": [codex_bin(), "login"],
        "log_path": str(log_path),
    }


def provider_health_failure_message(
    failed: list[dict[str, Any]],
    login_attempt: dict[str, Any] | None,
) -> str:
    providers = ", ".join(sorted({str(item.get("provider")) for item in failed if item.get("provider")}))
    first = failed[0]
    remediation = str(first.get("remediation") or "请先修复 provider 认证状态后重试。")
    detail = command_output_text(
        str(first.get("stdout") or ""),
        str(first.get("stderr") or ""),
    )
    detail_line = f" health_output={detail[:240]!r}。" if detail else ""
    login_line = ""
    if login_attempt is not None:
        if login_attempt.get("started") is True:
            login_line = (
                " 已按 CODEX_RVF_AUTO_CODEX_LOGIN=1 尝试后台启动 `codex login`，"
                f"log={login_attempt.get('log_path')}。"
            )
        else:
            login_line = (
                " 已尝试后台启动 `codex login`，但启动失败："
                f"{login_attempt.get('error')}。"
            )
    return (
        "provider 登录/认证健康检查未通过，已阻止 RVF 自动启动，避免创建会立即失败的 "
        f"review 任务。providers={providers or '<unknown>'}。{remediation}"
        f"{detail_line}{login_line}"
    )


def provider_health_guard_decision(
    decision: StopDecision,
    event: dict[str, Any],
    ledger: RunLedger,
) -> StopDecision | None:
    if not provider_health_check_enabled():
        return None

    requirements = provider_health_requirements(decision, event)
    if not requirements:
        return None

    timeout_seconds = provider_health_timeout_seconds()
    results = [run_provider_health_requirement(requirement, timeout_seconds) for requirement in requirements]
    health_path = ledger.artifact(
        "provider-health.json",
        {
            "enabled": True,
            "backend": decision.backend,
            "timeout_seconds": timeout_seconds,
            "results": results,
        },
    )
    failed = [result for result in results if result.get("status") != "ok"]
    ledger.event(
        phase="provider-health",
        event="completed" if not failed else "failed",
        status="completed" if not failed else "failed",
        reason_code="provider_health_completed" if not failed else "provider_health_failed",
        repo=decision.repo,
        cwd=decision.cwd,
        backend=decision.backend,
        paths={"provider_health": health_path} if health_path else {},
        providers=[result.get("provider") for result in results],
        **stop_hook_rvf_state_fields(
            phase="prepare",
            backend=decision.backend,
            backend_raw=decision.backend,
        ),
    )
    if not failed:
        return None

    login_attempt = (
        maybe_start_codex_login(ledger)
        if any(result.get("provider") == "codex" for result in failed)
        else None
    )
    message = provider_health_failure_message(failed, login_attempt)
    return skip_decision(
        message,
        ledger,
        "provider_health_failed",
        repo=decision.repo,
        cwd=decision.cwd,
        backend=decision.backend,
        provider_health_path=health_path,
        provider_health=results,
        login_attempt=login_attempt,
        gate_status=(decision.summary_fields or {}).get("gate_status"),
        **stop_hook_rvf_state_fields(
            phase="prepare",
            backend=decision.backend,
            backend_raw=decision.backend,
        ),
    )


# Cline Kanban 在 task session 的 hook 环境中自动设置 KANBAN_TASK_ID 和
# KANBAN_WORKSPACE_ID；这是 kanban-followup 判断“当前 Stop hook 位于 Kanban task
# 内”的原生信号。早期 Kanban task 只设置 KANBAN_HOOK_TASK_ID，重启 runtime 后
# 这些旧 session 仍可能触发 Stop hook，因此保留 legacy hook env alias。ATTEMPT/
# PROJECT_PATH 不是公开文档确认的自动变量，这里只作为 host 定制字段或 Stop event
# 扩展字段兼容读取。
def current_kanban_task_id(event: dict[str, Any]) -> str | None:
    return event_or_env_text(
        event,
        ("KANBAN_TASK_ID", "CLINE_KANBAN_TASK_ID", "KANBAN_HOOK_TASK_ID"),
        ("kanban_task_id", "kanbanTaskId", "task_id", "taskId"),
    )


def current_kanban_attempt_id(event: dict[str, Any]) -> str | None:
    return event_or_env_text(
        event,
        ("KANBAN_ATTEMPT_ID", "CLINE_KANBAN_ATTEMPT_ID"),
        ("kanban_attempt_id", "kanbanAttemptId", "attempt_id", "attemptId"),
    )


def current_kanban_task_title(event: dict[str, Any]) -> str | None:
    return event_or_env_text(
        event,
        (
            "KANBAN_TASK_TITLE",
            "KANBAN_TASK_NAME",
            "CLINE_KANBAN_TASK_TITLE",
            "CLINE_KANBAN_TASK_NAME",
        ),
        (
            "kanban_task_title",
            "kanbanTaskTitle",
            "kanban_task_name",
            "kanbanTaskName",
            "task_title",
            "taskTitle",
            "task_name",
            "taskName",
        ),
    )


def current_kanban_project_path(event: dict[str, Any], fallback: str) -> str:
    value = event_or_env_text(
        event,
        ("KANBAN_PROJECT_PATH", "CLINE_KANBAN_PROJECT_PATH"),
        ("kanban_project_path", "kanbanProjectPath", "project_path", "projectPath"),
    )
    return value or fallback


def startup_scope_text(
    *,
    cwd: str,
    parent_session_id: str,
    parent_thread_path: Path | None,
    prompt_path: str,
    ledger: RunLedger,
) -> str:
    transcript = str(parent_thread_path) if parent_thread_path is not None else "<unknown>"
    return (
        "# Scope of Work: Cline Kanban RVF startup\n\n"
        "本文件由 Stop hook 在创建 Cline Kanban task 前生成，用于冻结 task 启动时的 review 输入。\n\n"
        f"- 目标仓库：`{cwd}`\n"
        f"- parent session id：`{parent_session_id}`\n"
        f"- parent transcript path：`{transcript}`\n"
        f"- run id：`{ledger.run_id}`\n"
        f"- run dir：`{ledger.run_dir}`\n"
        f"- fork prompt：`{prompt_path}`\n\n"
        "Kanban task 的 scope 只能以本 run artifacts 中已经生成的 scope.contract.json 作为最终 scope contract；"
        "review packet、session manifest、workspace snapshot 和 worktree bootstrap 仅作为冻结证据、审计上下文或"
        "重放输入。不要在排队后用实时 worktree 重新定义 scope。"
    )


def freeze_cline_kanban_startup_artifacts(
    *,
    cwd: str,
    parent_session_id: str,
    parent_thread_path: Path | None,
    prompt_path: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    scope_path = ledger.artifact(
        "headless-startup-scope-of-work.md",
        startup_scope_text(
            cwd=cwd,
            parent_session_id=parent_session_id,
            parent_thread_path=parent_thread_path,
            prompt_path=prompt_path,
            ledger=ledger,
        ),
    )
    if not scope_path:
        raise RuntimeError("failed to write Cline Kanban startup scope artifact")
    command = [
        sys.executable,
        str(DEFAULT_PREPARE_REVIEW_RUN),
        "--repo",
        cwd,
        "--session-context",
        scope_path,
        "--rvf-run-id",
        ledger.run_id,
        "--rvf-run-dir",
        str(ledger.run_dir),
        "--rvf-backend",
        "kanban-task",
    ]
    if parent_thread_path is not None:
        command.extend(["--transcript", str(parent_thread_path)])
    tracker_scope_meta = getattr(ledger, "tracker_scope_meta", None)
    tracker_scope_path = None
    if isinstance(tracker_scope_meta, dict):
        raw_tracker_scope_path = tracker_scope_meta.get("tracker_scope_path")
        if isinstance(raw_tracker_scope_path, (str, Path)) and str(raw_tracker_scope_path).strip():
            tracker_scope_path = Path(raw_tracker_scope_path).expanduser()
            if not tracker_scope_path.exists():
                raise RuntimeError(f"allocated tracker scope artifact missing: {tracker_scope_path}")
            command.extend(["--tracker-scope", str(tracker_scope_path)])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **ledger.env()},
        check=False,
    )
    ledger.artifact(
        "cline-kanban-startup-prepare-command.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or "failed to freeze Cline Kanban startup review artifacts"
        )
    try:
        metadata = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid Cline Kanban startup prepare JSON: {completed.stdout!r}") from exc
    metadata_path = ledger.artifact("cline-kanban-startup-prepare.json", metadata)
    ledger.event(
        phase="prepare",
        event="cline_kanban_startup_artifacts_frozen",
        status="completed",
        reason_code="startup_artifacts_frozen",
        repo=cwd,
        cwd=cwd,
        paths={
            "metadata": metadata_path,
            "scope_of_work": metadata.get("scope_of_work_file"),
            "session_manifest": metadata.get("session_manifest_file"),
            "review_packet": metadata.get("review_packet"),
            "snapshot": metadata.get("before_workspace_snapshot"),
            "worktree_bootstrap": metadata.get("worktree_bootstrap"),
            "review_env": metadata.get("review_env_file"),
            "review_agent_context": metadata.get("review_agent_context_file"),
            "tracker_scope": str(tracker_scope_path) if tracker_scope_path is not None else None,
        },
        **stop_hook_rvf_state_fields(
            phase="prepare",
            backend="kanban-task",
            backend_raw="cline-kanban",
            prepare_metadata=metadata,
        ),
    )
    return {"metadata_path": metadata_path, "metadata": metadata}


def git_head(cwd: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "failed to resolve git HEAD")
    return completed.stdout.strip()


def parse_json_command_output(completed: subprocess.CompletedProcess[str], *, label: str) -> dict[str, Any]:
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"{label} failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid {label} JSON: {completed.stdout!r}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid {label} payload: {payload!r}")
    return payload


def shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def cline_kanban_task_prompt(
    *,
    cwd: str,
    prompt_path: str,
    parent_session_id: str,
    parent_thread_path: Path | None,
    parent_origin: dict[str, Any],
    ledger: RunLedger,
    startup_prepare: dict[str, Any],
) -> str:
    del startup_prepare
    transcript = str(parent_thread_path) if parent_thread_path is not None else "<unknown>"
    parent_conversation_ref = str(parent_origin.get("label") or "<unknown Codex conversation>")
    parent_conversation_source = str(parent_origin.get("name_source") or "<unknown>")
    parent_codex_url = str(parent_origin.get("codex_url") or "<unavailable>")
    parent_transcript_file = str(parent_origin.get("transcript_file") or "<unknown>")
    apply_helper = SKILL_DIR / "scripts" / "apply_worktree_bootstrap.py"
    handoff_helper = DEFAULT_HANDOFF_HELPER
    original_prompt = Path(prompt_path).read_text(encoding="utf-8")
    return (
        "$review-validate-fix\n\n"
        f"{RVF_FORK_MARKER}\n"
        f"{CLINE_KANBAN_TASK_MARKER}\n"
        "RVF_TARGET_REPO: .\n"
        f"RVF_PARENT_REPO: {cwd}\n"
        f"RVF_PARENT_CWD: {cwd}\n"
        f"RVF_RUN_ID: {ledger.run_id}\n"
        f"RVF_RUN_DIR: {ledger.run_dir}\n"
        "RVF_ARTIFACTS_DIR: $RVF_RUN_DIR/artifacts\n"
        f"RVF_PARENT_SESSION_ID: {parent_session_id}\n"
        f"RVF_PARENT_CONVERSATION_REF: {parent_conversation_ref}\n"
        f"RVF_PARENT_CONVERSATION_NAME: {parent_conversation_ref}\n"
        f"RVF_PARENT_CONVERSATION_NAME_SOURCE: {parent_conversation_source}\n"
        f"RVF_PARENT_CODEX_URL: {parent_codex_url}\n"
        f"RVF_PARENT_TRANSCRIPT_PATH: {transcript}\n"
        f"RVF_PARENT_TRANSCRIPT_FILE: {parent_transcript_file}\n"
        "RVF_REVIEW_ENV: $RVF_ARTIFACTS_DIR/review-env.sh\n"
        "RVF_REVIEW_AGENT_CONTEXT: $RVF_ARTIFACTS_DIR/review-agent-context.md\n"
        "RVF_ORIGIN_METADATA: $RVF_ARTIFACTS_DIR/origin.json\n"
        "RVF_ORIGINAL_FORK_PROMPT: $RVF_ARTIFACTS_DIR/fork.prompt.txt\n\n"
        "Original Codex conversation trace:\n"
        f"- name/ref: `{parent_conversation_ref}`\n"
        f"- name source: `{parent_conversation_source}`\n"
        f"- open: `{parent_codex_url}`\n"
        f"- transcript: `{transcript}`\n"
        f"- origin metadata: `$RVF_ARTIFACTS_DIR/origin.json`\n\n"
        "你运行在 Cline Kanban 为本 task 创建的独立 git worktree 中。执行 repo 是当前 task worktree；"
        "如果需要绝对路径，使用 `git rev-parse --show-toplevel`。上面的父 repo 仅作 metadata，"
        "不要回到父 worktree 运行 review/validate/fix。开始任何 review/validate/fix 前，必须先把父会话的 "
        "session-owned 未提交改动重放到当前 worktree：\n\n"
        "```sh\n"
        'RVF_TASK_REPO="$(git rev-parse --show-toplevel)"\n'
        f"export RVF_RUN_DIR={shell_quote(str(ledger.run_dir))}\n"
        f"export CODEX_RVF_LOG_ROOT={shell_quote(str(ledger.root))}\n"
        f"export CODEX_RVF_RUN_ID={shell_quote(str(ledger.run_id))}\n"
        'export CODEX_RVF_RUN_DIR="$RVF_RUN_DIR"\n'
        'export RVF_ARTIFACTS_DIR="$RVF_RUN_DIR/artifacts"\n'
        '. "$RVF_ARTIFACTS_DIR/review-env.sh"\n'
        'export RVF_REPO="$RVF_TASK_REPO"\n'
        f"python3 {shell_quote(str(apply_helper))} --metadata \"$RVF_WORKTREE_BOOTSTRAP\" --repo \"$RVF_REPO\"\n"
        "```\n\n"
        "然后读取并复用已经冻结的 RVF artifacts；命令和说明中继续使用这些变量，不要重复展开 run artifacts 目录：\n"
        "- review env: `$RVF_ARTIFACTS_DIR/review-env.sh`\n"
        "- review agent context: `$RVF_ARTIFACTS_DIR/review-agent-context.md`\n"
        "- scope contract: `$RVF_SCOPE_CONTRACT`\n"
        "- review packet: `$RVF_REVIEW_PACKET`\n"
        "- session manifest: `$RVF_SESSION_MANIFEST`\n"
        "- worktree bootstrap: `$RVF_WORKTREE_BOOTSTRAP`\n\n"
        "不得用 Kanban worktree 当前实时 diff 重新定义 scope；review scope 只能以 `$RVF_SCOPE_CONTRACT` "
        "为准，review packet 仅作为冻结 reviewer 输入，session manifest 只作为 ownership evidence 和 tracker 审计来源。"
        "不要在当前 Cline Kanban worktree 里重新运行 `prepare_review_run.py` 创建新的 run；"
        "本 task 已经复用上面的 `RVF_RUN_DIR` / `CODEX_RVF_RUN_DIR`，所有 handoff、reviewer 输出、"
        "summary 和 events 都必须继续写入该 installed plugin state run。"
        "Handoff 默认开启时，必须持续维护 "
        "`$RVF_ARTIFACTS_DIR/handoff.md`，并在文件顶部保留 `## Origin` 区块，"
        "逐字写入上面的 original Codex conversation name/ref、name source、codex URL、transcript path、"
        "RVF run id 和 origin metadata path。最终回复第一行输出 "
        "`RVF_HANDOFF_FILE: <handoff.md 绝对路径>`，随后只追加 1-3 句极短中文说明。"
        "最终回复前必须先运行：\n\n"
        "```sh\n"
        f"python3 {shell_quote(str(handoff_helper))} open \"$RVF_ARTIFACTS_DIR/handoff.md\"\n"
        "```\n\n"
        "原始 fork prompt 如下，仅作兼容元数据：\n\n"
        "```text\n"
        f"{original_prompt.rstrip()}\n"
        "```\n"
    )


def cline_kanban_client_env(ledger: RunLedger) -> dict[str, str]:
    env = {**os.environ, **ledger.env()}
    for name in SUPPRESS_ENV_NAMES:
        env.pop(name, None)
    return env


def start_cline_kanban_task(
    *,
    cwd: str,
    prompt_path: str,
    parent_session_id: str,
    parent_thread_path: Path | None,
    parent_origin: dict[str, Any],
    ledger: RunLedger,
    task_title: str,
    model: str | None,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    del model, reasoning_effort
    client = cline_kanban_script_path("CODEX_RVF_CLINE_KANBAN_CLIENT", DEFAULT_CLINE_KANBAN_CLIENT)
    startup_prepare = freeze_cline_kanban_startup_artifacts(
        cwd=cwd,
        parent_session_id=parent_session_id,
        parent_thread_path=parent_thread_path,
        prompt_path=prompt_path,
        ledger=ledger,
    )
    task_prompt = cline_kanban_task_prompt(
        cwd=cwd,
        prompt_path=prompt_path,
        parent_session_id=parent_session_id,
        parent_thread_path=parent_thread_path,
        parent_origin=parent_origin,
        ledger=ledger,
        startup_prepare=startup_prepare,
    )
    task_prompt_path = ledger.artifact("cline-kanban-task.prompt.md", task_prompt)
    if not task_prompt_path:
        raise RuntimeError("failed to write Cline Kanban task prompt artifact")

    task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD)
    start_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_START_CMD", DEFAULT_CLINE_KANBAN_START_CMD)
    start_timeout = os.environ.get(
        "CODEX_RVF_CLINE_KANBAN_START_TIMEOUT",
        str(DEFAULT_CLINE_KANBAN_START_TIMEOUT_SECONDS),
    )
    tmux_session = os.environ.get("CODEX_RVF_CLINE_KANBAN_TMUX_SESSION", DEFAULT_CLINE_KANBAN_TMUX_SESSION)
    base_ref = os.environ.get("CODEX_RVF_CLINE_KANBAN_BASE_REF", "").strip() or git_head(cwd)
    agent_id = os.environ.get("CODEX_RVF_CLINE_KANBAN_AGENT_ID", "codex").strip() or "codex"
    auto_review_enabled = is_truthy(os.environ.get("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_ENABLED"))
    auto_review_mode = os.environ.get("CODEX_RVF_CLINE_KANBAN_AUTO_REVIEW_MODE", "commit").strip() or "commit"
    start_in_plan_mode = is_truthy(os.environ.get("CODEX_RVF_CLINE_KANBAN_START_IN_PLAN_MODE"))
    env = cline_kanban_client_env(ledger)

    ensure_command = [
        sys.executable,
        str(client),
        "ensure",
        "--repo",
        cwd,
        "--task-cmd",
        task_cmd,
        "--start-cmd",
        start_cmd,
        "--start-timeout",
        start_timeout,
        "--tmux-session",
        tmux_session,
        "--start-if-needed",
    ]
    ensure_completed = subprocess.run(ensure_command, capture_output=True, text=True, env=env, check=False)
    ensure_payload = parse_json_command_output(ensure_completed, label="Cline Kanban ensure")
    ledger.artifact(
        "cline-kanban-ensure.json",
        {
            "command": ensure_command,
            "returncode": ensure_completed.returncode,
            "stdout": ensure_completed.stdout,
            "stderr": ensure_completed.stderr,
            "payload": ensure_payload,
        },
    )

    create_command = [
        sys.executable,
        str(client),
        "create",
        "--repo",
        cwd,
        "--task-cmd",
        task_cmd,
        "--base-ref",
        base_ref,
        "--prompt",
        task_prompt,
        "--title",
        task_title,
        "--agent-id",
        agent_id,
    ]
    if start_in_plan_mode:
        create_command.append("--start-in-plan-mode")
    if auto_review_enabled:
        create_command.extend(["--auto-review-enabled", "--auto-review-mode", auto_review_mode])
    create_completed = subprocess.run(create_command, capture_output=True, text=True, env=env, check=False)
    create_payload = parse_json_command_output(create_completed, label="Cline Kanban task create")
    ledger.artifact(
        "cline-kanban-create-task.json",
        {
            "command": create_command,
            "returncode": create_completed.returncode,
            "stdout": create_completed.stdout,
            "stderr": create_completed.stderr,
            "payload": create_payload,
        },
    )
    task_id = str(create_payload.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError(f"Cline Kanban task create response did not include task_id: {create_payload!r}")
    suppression_path = write_kanban_task_suppression(task_id=task_id, cwd=cwd, ledger=ledger)

    start_command = [
        sys.executable,
        str(client),
        "start",
        "--repo",
        cwd,
        "--task-cmd",
        task_cmd,
        "--task-id",
        task_id,
    ]
    start_completed = subprocess.run(start_command, capture_output=True, text=True, env=env, check=False)
    start_payload = parse_json_command_output(start_completed, label="Cline Kanban task start")
    ledger.artifact(
        "cline-kanban-start-task.json",
        {
            "command": start_command,
            "returncode": start_completed.returncode,
            "stdout": start_completed.stdout,
            "stderr": start_completed.stderr,
            "payload": start_payload,
        },
    )
    metadata = startup_prepare.get("metadata") if isinstance(startup_prepare.get("metadata"), dict) else {}
    return {
        "cline_kanban_task_id": task_id,
        "cline_kanban_base_ref": base_ref,
        "cline_kanban_task_prompt_path": task_prompt_path,
        "cline_kanban_stop_hook_suppression_path": suppression_path,
        "cline_kanban_ensure": ensure_payload,
        "cline_kanban_create": create_payload,
        "cline_kanban_start": start_payload,
        "cline_kanban_task_cmd": task_cmd,
        "cline_kanban_start_cmd": start_cmd,
        "cline_kanban_tmux_session": tmux_session,
        "cline_kanban_agent_id": agent_id,
        "cline_kanban_auto_review_enabled": auto_review_enabled,
        "cline_kanban_auto_review_mode": auto_review_mode if auto_review_enabled else None,
        "workspace_path": cwd,
        "startup_prepare_metadata_path": startup_prepare.get("metadata_path"),
        "worktree_bootstrap_path": metadata.get("worktree_bootstrap"),
        "worktree_bootstrap_patch_path": metadata.get("worktree_bootstrap_patch"),
        "worktree_bootstrap_files_dir": metadata.get("worktree_bootstrap_files_dir"),
        **stop_hook_rvf_state_fields(
            phase="prepare",
            backend="kanban-task",
            backend_raw="cline-kanban",
            prepare_metadata=metadata,
        ),
    }


def start_cline_kanban_followup_message(
    *,
    project_path: str,
    task_id: str,
    attempt_id: str | None,
    prompt: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    client = cline_kanban_script_path("CODEX_RVF_CLINE_KANBAN_CLIENT", DEFAULT_CLINE_KANBAN_CLIENT)
    task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD)
    prompt_path = ledger.artifact("kanban-followup.prompt.md", prompt)
    if not prompt_path:
        raise RuntimeError("failed to write Cline Kanban follow-up prompt artifact")

    command = [
        sys.executable,
        str(client),
        "message",
        "--repo",
        project_path,
        "--task-cmd",
        task_cmd,
        "--task-id",
        task_id,
        "--prompt-file",
        prompt_path,
        "--source",
        "review-validate-fix",
        "--idempotency-key",
        ledger.run_id,
    ]
    if attempt_id:
        command.extend(["--attempt-id", attempt_id])

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **ledger.env()},
        check=False,
    )
    command_path = ledger.artifact(
        "kanban-followup-message.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    payload = parse_json_command_output(completed, label="Cline Kanban task message")
    message_id = str(payload.get("message_id") or payload.get("messageId") or "").strip()
    if not message_id:
        raise RuntimeError(f"Cline Kanban task message response did not include message_id: {payload!r}")
    payload["message_id"] = message_id
    payload.setdefault("task_id", task_id)
    if attempt_id:
        payload.setdefault("attempt_id", attempt_id)

    ledger.artifact(
        "kanban-followup-message-result.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "payload": payload,
        },
    )
    payload["command_artifact_path"] = command_path
    payload["prompt_path"] = prompt_path
    payload["task_cmd"] = task_cmd
    payload["project_path"] = project_path
    return payload


def _iter_nested_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_nested_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_nested_dicts(child)


def _task_id_from_mapping(value: dict[str, Any]) -> str | None:
    for key in ("id", "task_id", "taskId"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _task_title_from_mapping(value: dict[str, Any]) -> str | None:
    for key in ("title", "name", "task_title", "taskTitle", "task_name", "taskName"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def kanban_task_title_from_payload(payload: dict[str, Any], task_id: str) -> str | None:
    expected = task_id.strip()
    if not expected:
        return None
    for candidate in _iter_nested_dicts(payload):
        if _task_id_from_mapping(candidate) != expected:
            continue
        title = _task_title_from_mapping(candidate)
        if title:
            return title
    return None


def cline_kanban_state_dir() -> Path:
    value = os.environ.get("CODEX_RVF_CLINE_KANBAN_STATE_DIR")
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser()
    return DEFAULT_CLINE_KANBAN_STATE_DIR


def _same_existing_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _kanban_session_matches_project(session: dict[str, Any], *, task_id: str, project_path: Path) -> bool:
    session_task_id = session.get("taskId") or session.get("task_id") or session.get("id")
    if session_task_id != task_id:
        return False
    workspace_path = session.get("workspacePath") or session.get("workspace_path")
    if not isinstance(workspace_path, str) or not workspace_path.strip():
        return False
    return _same_existing_path(Path(workspace_path), project_path)


def _candidate_kanban_board_paths(*, project_path: Path, task_id: str) -> list[Path]:
    workspaces_dir = cline_kanban_state_dir() / "workspaces"
    if not workspaces_dir.is_dir():
        return []
    candidates: list[Path] = []

    project_named_board = workspaces_dir / project_path.name / "board.json"
    if project_named_board.exists():
        candidates.append(project_named_board)

    for sessions_path in sorted(workspaces_dir.glob("*/sessions.json")):
        try:
            payload = json.loads(sessions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        sessions = [value for value in payload.values() if isinstance(value, dict)]
        if any(_kanban_session_matches_project(session, task_id=task_id, project_path=project_path) for session in sessions):
            board_path = sessions_path.with_name("board.json")
            if board_path.exists() and board_path not in candidates:
                candidates.insert(0, board_path)

    return candidates


def lookup_cline_kanban_board_task_title(
    *,
    project_path: str,
    task_id: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    project = Path(project_path).expanduser()
    checked: list[str] = []
    errors: list[str] = []
    for board_path in _candidate_kanban_board_paths(project_path=project, task_id=task_id):
        checked.append(str(board_path))
        try:
            payload = json.loads(board_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{board_path}: {type(exc).__name__}: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{board_path}: non-object JSON")
            continue
        title = kanban_task_title_from_payload(payload, task_id)
        if title:
            artifact = ledger.artifact(
                "kanban-followup-board-title-lookup.json",
                {
                    "state_dir": str(cline_kanban_state_dir()),
                    "project_path": project_path,
                    "task_id": task_id,
                    "checked": checked,
                    "matched_board": str(board_path),
                    "title": title,
                    "errors": errors,
                },
            )
            return {
                "title": title,
                "source": "cline_kanban_board_lookup",
                "artifact": artifact,
            }
    artifact = ledger.artifact(
        "kanban-followup-board-title-lookup.json",
        {
            "state_dir": str(cline_kanban_state_dir()),
            "project_path": project_path,
            "task_id": task_id,
            "checked": checked,
            "errors": errors,
        },
    )
    return {
        "title": None,
        "source": "cline_kanban_board_lookup_missing_title",
        "artifact": artifact,
    }


def lookup_cline_kanban_task_title(
    *,
    project_path: str,
    task_id: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    client = cline_kanban_script_path("CODEX_RVF_CLINE_KANBAN_CLIENT", DEFAULT_CLINE_KANBAN_CLIENT)
    task_cmd = os.environ.get("CODEX_RVF_CLINE_KANBAN_TASK_CMD", DEFAULT_CLINE_KANBAN_TASK_CMD)
    command = [
        sys.executable,
        str(client),
        "list",
        "--repo",
        project_path,
        "--task-cmd",
        task_cmd,
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, **ledger.env()},
        check=False,
    )
    artifact = ledger.artifact(
        "kanban-followup-task-lookup.json",
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode != 0:
        board_lookup = lookup_cline_kanban_board_task_title(
            project_path=project_path,
            task_id=task_id,
            ledger=ledger,
        )
        if board_lookup.get("title"):
            board_lookup["task_list_lookup"] = {
                "source": "cline_kanban_task_lookup_failed",
                "artifact": artifact,
                "error": completed.stderr.strip() or completed.stdout.strip(),
            }
            return board_lookup
        return {
            "title": None,
            "source": "cline_kanban_task_lookup_failed",
            "artifact": artifact,
            "error": completed.stderr.strip() or completed.stdout.strip(),
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "title": None,
            "source": "cline_kanban_task_lookup_invalid_json",
            "artifact": artifact,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, dict):
        return {
            "title": None,
            "source": "cline_kanban_task_lookup_invalid_payload",
            "artifact": artifact,
        }
    title = kanban_task_title_from_payload(payload, task_id)
    if not title:
        board_lookup = lookup_cline_kanban_board_task_title(
            project_path=project_path,
            task_id=task_id,
            ledger=ledger,
        )
        if board_lookup.get("title"):
            board_lookup["task_list_lookup"] = {
                "source": "cline_kanban_task_lookup_missing_title",
                "artifact": artifact,
            }
            return board_lookup
    return {
        "title": title,
        "source": "cline_kanban_task_lookup" if title else "cline_kanban_task_lookup_missing_title",
        "artifact": artifact,
    }


def run_codex_fork(
    *,
    parent_session_id: str,
    cwd: str | None,
    prompt: str,
    log_prefix: str,
    mode_env_name: str = "CODEX_RVF_FORK_MODE",
    suppress_child_stop_hook: bool = False,
    model: str | None = None,
    reasoning_effort: str | None = None,
    parent_thread_path: Path | None = None,
    fallback_failure_reason: str | None = None,
    allow_desktop_unavailable_report: bool = True,
    ledger: RunLedger | None = None,
    extra_summary: dict[str, Any] | None = None,
    launch_mode: str | None = None,
) -> dict[str, Any]:
    mode = (
        launch_mode
        if launch_mode is not None
        else os.environ.get(mode_env_name, DEFAULT_FORK_LAUNCH_MODE)
    ).strip().lower()
    ledger = ledger or start_run("stop-hook", repo=cwd, cwd=cwd)

    if not parent_session_id:
        return skip_payload(
            "Stop event did not expose a parent thread id.",
            ledger,
            "missing_parent_thread_id",
            log_prefix=log_prefix,
            cwd=cwd,
        )

    effective_prompt = prompt
    if suppress_child_stop_hook and SUPPRESS_STOP_HOOK_MARKER not in effective_prompt:
        effective_prompt = (
            f"{effective_prompt.rstrip()}\n\n"
            "Stop hook child-session metadata:\n"
            f"{SUPPRESS_STOP_HOOK_MARKER}\n"
            "当前 fork 结束时请跳过 review-validate-fix Stop hook。"
        )

    parent_name_lookup = parent_thread_name_from_app_server(parent_session_id, cwd)
    parent_origin = parent_conversation_origin(
        parent_session_id=parent_session_id,
        parent_thread_path=parent_thread_path,
        run_id=ledger.run_id,
        parent_thread_name=parent_name_lookup.get("name"),
        name_lookup=parent_name_lookup,
    )
    origin_path = ledger.artifact("origin.json", parent_origin)
    effective_prompt = add_parent_origin_to_rvf_fork_prompt(
        effective_prompt,
        parent_origin=parent_origin,
        origin_path=origin_path,
    )
    prompt_path = ledger.artifact("fork.prompt.txt", effective_prompt)
    ledger.event(
        phase="fork",
        event="started",
        status="started",
        reason_code="fork_started",
        parent_thread_id=parent_session_id,
        paths={"prompt": prompt_path} if prompt_path else {},
        mode=mode,
        log_prefix=log_prefix,
    )

    result: dict[str, Any] = {
        "mode": mode,
        "log_prefix": log_prefix,
        "parent_thread_id": parent_session_id,
        "parent_thread_path": str(parent_thread_path) if parent_thread_path is not None else None,
        "parent_conversation_ref": parent_origin.get("label"),
        "parent_conversation_name": parent_origin.get("label"),
        "parent_conversation_name_source": parent_origin.get("name_source"),
        "parent_thread_name_lookup": parent_name_lookup,
        "parent_codex_url": parent_origin.get("codex_url"),
        "parent_origin_path": origin_path,
        "parent_transcript_file": parent_origin.get("transcript_file"),
        "cwd": cwd,
        "prompt_path": prompt_path,
        "suppress_child_stop_hook": suppress_child_stop_hook,
        "model": model,
        "reasoning_effort": reasoning_effort,
    }

    if mode in {"manual", "prepare", "prepared", "log-only"}:
        result["status"] = "manual-prepared"
    elif mode == "dry-run":
        result["status"] = "dry-run"
        app_server_requests = app_server_fork_requests(
            parent_thread_id=parent_session_id,
            parent_thread_path=parent_thread_path,
            cwd=cwd,
            prompt=effective_prompt,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        request_path = ledger.artifact("app-server-requests.json", app_server_requests)
        result["app_server_requests_path"] = request_path
    elif mode in {"cline-kanban", "cline", "kanban", "ck"}:
        result["mode"] = "cline-kanban"
        if parent_thread_path is None:
            result.update(
                {
                    "status": "cline-kanban-unavailable",
                    "error": (
                        "CODEX_RVF_FORK_MODE=cline-kanban requires a readable parent "
                        "transcript/session scope anchor; task was not started."
                    ),
                }
            )
        elif not cwd:
            result.update(
                {
                    "status": "cline-kanban-unconfigured",
                    "error": "CODEX_RVF_FORK_MODE=cline-kanban requires a target repo cwd.",
                }
            )
        elif not prompt_path:
            result.update(
                {
                    "status": "cline-kanban-unavailable",
                    "error": "fork prompt artifact is unavailable; Cline Kanban task was not started.",
                }
            )
        else:
            task_title = str(parent_origin["task_title"])
            try:
                task_payload = start_cline_kanban_task(
                    cwd=cwd,
                    prompt_path=prompt_path,
                    parent_session_id=parent_session_id,
                    parent_thread_path=parent_thread_path,
                    parent_origin=parent_origin,
                    ledger=ledger,
                    task_title=task_title,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
                result.update(
                    {
                        "status": "cline-kanban-started",
                        "task_title": task_title,
                        "cline_kanban_task_title": task_title,
                        **task_payload,
                    }
                )
            except Exception as exc:
                result.update(
                    {
                        "status": "cline-kanban-unavailable",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        if (
            extra_summary
            and extra_summary.get("backend_selection_mode") == "auto"
            and cline_kanban_failure_allows_legacy_gui_fallback(result)
            and legacy_gui_fallback_enabled()
        ):
            cline_failure = {
                "status": result.get("status"),
                "error": result.get("error"),
                "mode": "cline-kanban",
            }
            ledger.event(
                phase="fork",
                event="legacy_gui_fallback_started",
                status="started",
                reason_code="legacy_gui_fallback_started",
                parent_thread_id=parent_session_id,
                paths={"prompt": prompt_path} if prompt_path else {},
                primary_backend="cline-kanban",
                fallback_backend="gui",
                error=cline_failure.get("error"),
            )
            try:
                fallback_payload = run_app_server_fork(
                    parent_thread_id=parent_session_id,
                    parent_thread_path=parent_thread_path,
                    cwd=cwd,
                    prompt=effective_prompt,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    log_path=ledger.summary_path,
                )
                result.update(fallback_payload)
                result["mode"] = "legacy-gui"
                result["effective_backend"] = "legacy-gui"
                result["legacy_gui_fallback"] = {
                    "started": True,
                    "primary_backend": "cline-kanban",
                    "fallback_backend": "gui",
                    "primary_failure": cline_failure,
                }
            except Exception as exc:
                result["legacy_gui_fallback"] = {
                    "started": False,
                    "primary_backend": "cline-kanban",
                    "fallback_backend": "gui",
                    "primary_failure": cline_failure,
                    "error": f"{type(exc).__name__}: {exc}",
                }
    elif mode in {"gui", "app-server", "appserver", "auto"}:
        try:
            result.update(
                run_app_server_fork(
                    parent_thread_id=parent_session_id,
                    parent_thread_path=parent_thread_path,
                    cwd=cwd,
                    prompt=effective_prompt,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    log_path=ledger.summary_path,
                )
            )
        except Exception as exc:
            failure: dict[str, Any] = {
                "status": "app-server-failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            socket_selection = getattr(exc, "socket_selection", None)
            if isinstance(socket_selection, dict):
                failure["socket_selection"] = socket_selection
                bridge_policy = socket_selection.get("bridge_policy")
                if bridge_policy == "report":
                    if allow_desktop_unavailable_report:
                        failure["status"] = "desktop-control-unavailable-report"
                        failure["report_reason"] = (
                            fallback_failure_reason
                            or "Codex Desktop control socket unavailable; GUI fork was not created."
                        )
                    else:
                        failure["status"] = "manual-prepared"
                        failure["desktop_control_unavailable_fallback"] = "manual"
                elif bridge_policy == "manual":
                    failure["status"] = "manual-prepared"
                elif bridge_policy == "fail":
                    failure["status"] = "desktop-control-unavailable-fail"
                    failure["report_reason"] = failure["error"]
            result.update(failure)
    else:
        result.update(
            {
                "status": "unsupported-mode",
                "error": (
                    f"Unsupported {mode_env_name}={mode!r}. Use auto, gui, cline-kanban, dry-run, "
                    "or manual. Terminal/CLI fork launch is intentionally disabled."
                ),
            }
        )

    status = result.get("status", "unknown")
    reason_code = str(status).replace("_", "-")
    if status == "desktop-control-unavailable-report":
        reason_code = "desktop_control_unavailable_continuation_disabled"
    elif status == "desktop-control-unavailable-fail":
        reason_code = "desktop_control_unavailable_fail_policy"
    elif status == "app-server-failed":
        reason_code = "app_server_fork_failed"
    elif status == "manual-prepared":
        reason_code = "manual_prepared"
    elif status == "dry-run":
        reason_code = "dry_run"
    elif status == "app-server-started":
        reason_code = "fork_started"
    elif status == "cline-kanban-started":
        reason_code = "cline_kanban_task_started"
    elif status == "cline-kanban-unconfigured":
        reason_code = "cline_kanban_unconfigured"
    elif status == "cline-kanban-unavailable":
        reason_code = "cline_kanban_unavailable"

    event_paths: dict[str, Any] = {}
    if prompt_path:
        event_paths["prompt"] = prompt_path
    if result.get("parent_origin_path"):
        event_paths["origin"] = result["parent_origin_path"]
    if result.get("app_server_requests_path"):
        event_paths["app_server_requests"] = result["app_server_requests_path"]
    if result.get("cline_kanban_task_prompt_path"):
        event_paths["cline_kanban_task_prompt"] = result["cline_kanban_task_prompt_path"]
    if result.get("worktree_bootstrap_path"):
        event_paths["worktree_bootstrap"] = result["worktree_bootstrap_path"]
    if status == "app-server-started":
        ledger.event(
            phase="fork",
            event="completed",
            status=str(status),
            reason_code=reason_code,
            parent_thread_id=parent_session_id,
            fork_thread_id=result.get("fork_thread_id") if isinstance(result.get("fork_thread_id"), str) else None,
            paths=event_paths,
            socket_source=result.get("socket_source"),
            gui_visibility=result.get("gui_visibility"),
        )
    elif status == "cline-kanban-started":
        ledger.event(
            phase="fork",
            event="completed",
            status=str(status),
            reason_code=reason_code,
            parent_thread_id=parent_session_id,
            paths=event_paths,
            mode="cline-kanban",
            cline_kanban_task_id=result.get("cline_kanban_task_id"),
            cline_kanban_task_title=result.get("task_title"),
            cline_kanban_base_ref=result.get("cline_kanban_base_ref"),
            parent_conversation_ref=result.get("parent_conversation_ref"),
            parent_conversation_name=result.get("parent_conversation_name"),
            parent_conversation_name_source=result.get("parent_conversation_name_source"),
            parent_codex_url=result.get("parent_codex_url"),
        )
    elif status in {"dry-run", "manual-prepared"}:
        ledger.event(
            phase="fork",
            event="prepared",
            status=str(status),
            reason_code=reason_code,
            parent_thread_id=parent_session_id,
            paths=event_paths,
            mode=mode,
        )
    else:
        ledger.event(
            phase="fork",
            event="failed",
            status=str(status),
            reason_code=reason_code,
            parent_thread_id=parent_session_id,
            paths=event_paths,
            error=result.get("error") or result.get("report_reason"),
        )

    if status == "manual-prepared":
        message = (
            "manual fork prompt prepared; no Terminal was launched and no "
            "current-chat continuation was submitted."
        )
    elif status == "app-server-started":
        message = "Codex GUI/app-server fork was started."
    elif status == "cline-kanban-started":
        message = "Cline Kanban RVF task was created and started."
    elif status == "cline-kanban-unconfigured":
        message = str(result.get("error") or "Cline Kanban RVF mode is not configured.")
    elif status == "cline-kanban-unavailable":
        message = str(result.get("error") or "Cline Kanban is unavailable; task was not started.")
    elif status in {"desktop-control-unavailable-report", "desktop-control-unavailable-fail"}:
        report_reason = result.get("report_reason")
        message = report_reason if isinstance(report_reason, str) else "Codex GUI fork unavailable."
    else:
        message = f"{log_prefix} triggered: {status}."

    summary_fields = dict(result)
    summary_fields.pop("status", None)
    result_state_fields = {
        key: value
        for key, value in summary_fields.items()
        if key == "rvf_state" or key.startswith("rvf_")
    }
    if extra_summary:
        summary_fields.update(extra_summary)
    if result_state_fields.get("rvf_state"):
        summary_fields.update(result_state_fields)
    if "rvf_state" not in summary_fields:
        backend_raw = str(summary_fields.get("backend") or result.get("mode") or mode)
        canonical_backend = normalize_rvf_backend(backend_raw)
        if canonical_backend is not None:
            summary_fields.update(
                stop_hook_rvf_state_fields(
                    phase="prepare",
                    backend=canonical_backend,
                    backend_raw=backend_raw,
                )
            )
    return ledger.hook_payload(
        status=str(status),
        reason_code=reason_code,
        message=message,
        **summary_fields,
    )


def app_server_fork_requests(
    *,
    parent_thread_id: str,
    parent_thread_path: Path | None,
    cwd: str | None,
    prompt: str,
    model: str | None,
    reasoning_effort: str | None,
) -> list[dict[str, Any]]:
    fork_params: dict[str, Any] = {
        "threadId": parent_thread_id,
        "cwd": cwd,
        "excludeTurns": True,
        "persistExtendedHistory": True,
    }
    if parent_thread_path is not None:
        fork_params["path"] = str(parent_thread_path)
    if model:
        fork_params["model"] = model

    turn_params: dict[str, Any] = {
        "threadId": "<fork_thread_id>",
        "input": [{"type": "text", "text": prompt, "text_elements": []}],
        "cwd": cwd,
        "summary": "auto",
        "personality": None,
        "outputSchema": None,
    }
    if model:
        turn_params["model"] = model
    if reasoning_effort:
        turn_params["effort"] = reasoning_effort

    return [
        {"method": "thread/fork", "params": fork_params},
        {"method": "turn/start", "params": turn_params},
    ]


class AppServerWebSocket:
    def __init__(self, socket_path: Path, timeout: float = 15) -> None:
        self.socket_path = socket_path
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.socket.connect(str(socket_path))
        self.recv_buffer = b""
        self.perform_handshake()
        self.next_id = 1
        self.notifications: list[dict[str, Any]] = []

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass

    def perform_handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        self.socket.sendall(request)

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.socket.recv(4096)
            if not chunk:
                raise AppServerError("app-server websocket handshake closed")
            response += chunk
            if len(response) > 16384:
                raise AppServerError("app-server websocket handshake response too large")

        header_bytes, self.recv_buffer = response.split(b"\r\n\r\n", 1)
        header_text = header_bytes.decode("iso-8859-1")
        lines = header_text.split("\r\n")
        status_line = lines[0] if lines else ""
        if not status_line.startswith("HTTP/1.1 101") and not status_line.startswith(
            "HTTP/1.0 101"
        ):
            raise AppServerError(f"app-server websocket handshake failed: {status_line}")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        accept = headers.get("sec-websocket-accept")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if accept != expected:
            raise AppServerError("app-server websocket handshake accept mismatch")

    def send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        mask = os.urandom(4)
        if len(data) < 126:
            header = bytes([0x81, 0x80 | len(data)])
        elif len(data) < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", len(data))
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", len(data))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.socket.sendall(header + mask + masked)

    def recv_exact(self, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        if self.recv_buffer:
            chunk = self.recv_buffer[:remaining]
            chunks.append(chunk)
            remaining -= len(chunk)
            self.recv_buffer = self.recv_buffer[len(chunk) :]
        while remaining > 0:
            chunk = self.socket.recv(remaining)
            if not chunk:
                raise AppServerError("app-server websocket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def recv_json(self) -> dict[str, Any]:
        first, second = self.recv_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self.recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.recv_exact(8))[0]

        mask = self.recv_exact(4) if second & 0x80 else None
        payload = self.recv_exact(length)
        if mask is not None:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

        if opcode == 0x8:
            raise AppServerError("app-server websocket closed")
        if opcode == 0x9:
            self.send_pong(payload)
            return self.recv_json()
        if opcode != 0x1:
            raise AppServerError(f"unsupported websocket opcode {opcode}")
        return json.loads(payload.decode("utf-8"))

    def send_pong(self, payload: bytes) -> None:
        if len(payload) >= 126:
            return
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask + masked)

    def request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.send_json(payload)
        while True:
            response = self.recv_json()
            if response.get("id") != request_id:
                self.notifications.append(response)
                continue
            error = response.get("error")
            if error:
                raise AppServerError(json.dumps(error, ensure_ascii=False))
            result = response.get("result")
            return result if isinstance(result, dict) else {}


def can_connect_app_server_socket(socket_path: Path) -> bool:
    return bool(probe_app_server_socket(socket_path).get("protocol_ok"))


def app_server_probe_ready(probe: dict[str, Any]) -> bool:
    if "protocol_ok" in probe:
        return bool(probe.get("protocol_ok"))
    return bool(probe.get("connect_ok"))


def probe_app_server_socket(socket_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(socket_path),
        "exists": socket_path.exists(),
        "parent_exists": socket_path.parent.exists(),
        "is_socket": False,
        "connect_ok": False,
        "protocol_ok": False,
        "reason": None,
    }
    try:
        if socket_path.exists():
            result["is_socket"] = socket_path.is_socket()
    except OSError as exc:
        result.update(
            {
                "reason": "stat-error",
                "error": f"{type(exc).__name__}: {exc}",
                "errno": getattr(exc, "errno", None),
            }
        )
        return result

    if not result["exists"]:
        result["reason"] = "missing"
        return result
    if not result["is_socket"]:
        result["reason"] = "not-a-socket"
        return result

    try:
        probe = AppServerWebSocket(socket_path, timeout=0.5)
        result["connect_ok"] = True
        result["protocol_ok"] = True
        result["reason"] = "websocket-ok"
        return result
    except AppServerError as exc:
        result.update(
            {
                "connect_ok": True,
                "reason": "websocket-failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return result
    except OSError as exc:
        result.update(
            {
                "reason": "connect-failed",
                "error": f"{type(exc).__name__}: {exc}",
                "errno": getattr(exc, "errno", None),
            }
        )
        return result
    finally:
        try:
            probe.close()
        except UnboundLocalError:
            pass


def bridge_socket_path() -> Path:
    env_value = os.environ.get("CODEX_RVF_BRIDGE_SOCKET")
    if env_value and env_value.strip():
        return Path(env_value).expanduser().resolve()
    return DEFAULT_BRIDGE_SOCKET.resolve()


def bridge_log_path() -> Path:
    env_value = os.environ.get("CODEX_RVF_BRIDGE_LOG")
    if env_value and env_value.strip():
        return Path(env_value).expanduser().resolve()
    return DEFAULT_BRIDGE_LOG.resolve()


def select_app_server_socket() -> tuple[Path, str, dict[str, Any]]:
    explicit = os.environ.get("CODEX_RVF_APP_SERVER_SOCKET")
    if explicit and explicit.strip():
        socket_path = Path(explicit).expanduser().resolve()
        return socket_path, "explicit", {"explicit": probe_app_server_socket(socket_path)}

    desktop_probe = probe_app_server_socket(DEFAULT_APP_SERVER_CONTROL_SOCKET)
    if app_server_probe_ready(desktop_probe):
        return DEFAULT_APP_SERVER_CONTROL_SOCKET, "desktop-control", {
            "desktop_control": desktop_probe,
        }

    bridge_policy = bridge_gui_unverified_policy()
    if bridge_policy not in {"auto", "bridge"}:
        socket_selection = {
            "desktop_control": desktop_probe,
            "bridge": probe_app_server_socket(bridge_socket_path()),
            "bridge_policy": bridge_policy,
        }
        raise AppServerSocketSelectionError(
            "desktop-control unavailable; bridge fallback disabled by "
            f"CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY={bridge_policy}",
            socket_selection,
        )

    bridge_probe = probe_app_server_socket(bridge_socket_path())
    if bridge_policy == "auto" and app_server_probe_ready(bridge_probe):
        return bridge_socket_path(), "bridge", {
            "desktop_control": desktop_probe,
            "bridge": bridge_probe,
            "bridge_policy": bridge_policy,
            "bridge_decision": "existing-bridge-connect-ok",
        }

    try:
        socket_path = ensure_bridge_app_server()
    except Exception as exc:
        socket_selection = {
            "desktop_control": desktop_probe,
            "bridge": probe_app_server_socket(bridge_socket_path()),
            "bridge_policy": bridge_policy,
        }
        raise AppServerSocketSelectionError(
            f"desktop-control unavailable and bridge fallback failed: {exc}",
            socket_selection,
        ) from exc
    return socket_path, "bridge", {
        "desktop_control": desktop_probe,
        "bridge": probe_app_server_socket(socket_path),
        "bridge_policy": bridge_policy,
    }


def select_existing_app_server_socket_for_metadata() -> tuple[Path, str, dict[str, Any]]:
    explicit = os.environ.get("CODEX_RVF_APP_SERVER_SOCKET")
    if explicit and explicit.strip():
        socket_path = Path(explicit).expanduser().resolve()
        probe = probe_app_server_socket(socket_path)
        if app_server_probe_ready(probe):
            return socket_path, "explicit", {"explicit": probe}
        raise AppServerSocketSelectionError(
            "explicit app-server socket unavailable for metadata lookup",
            {"explicit": probe},
        )

    desktop_probe = probe_app_server_socket(DEFAULT_APP_SERVER_CONTROL_SOCKET)
    if app_server_probe_ready(desktop_probe):
        return DEFAULT_APP_SERVER_CONTROL_SOCKET, "desktop-control", {
            "desktop_control": desktop_probe,
        }

    bridge_path = bridge_socket_path()
    bridge_probe = probe_app_server_socket(bridge_path)
    if app_server_probe_ready(bridge_probe):
        return bridge_path, "bridge", {
            "desktop_control": desktop_probe,
            "bridge": bridge_probe,
        }

    raise AppServerSocketSelectionError(
        "no existing app-server socket available for metadata lookup",
        {"desktop_control": desktop_probe, "bridge": bridge_probe},
    )


def bridge_gui_unverified_policy() -> str:
    if is_truthy(os.environ.get("CODEX_RVF_ALLOW_BRIDGE_APP_SERVER")):
        return "bridge"
    raw = os.environ.get(
        "CODEX_RVF_BRIDGE_GUI_UNVERIFIED_POLICY",
        DEFAULT_BRIDGE_GUI_UNVERIFIED_POLICY,
    )
    value = raw.strip().lower() if raw else DEFAULT_BRIDGE_GUI_UNVERIFIED_POLICY
    if value in {"auto", "detect", "fallback"}:
        return "auto"
    if value in {"bridge", "allow", "allowed", "fork", "app-server", "appserver"}:
        return "bridge"
    if value in {"manual", "prepare", "prepared", "log-only"}:
        return "manual"
    if value in {"fail", "error"}:
        return "fail"
    return "report"


def ensure_bridge_app_server(restart_existing: bool = False) -> Path:
    socket_path = bridge_socket_path()
    if (
        not restart_existing
        and socket_path.exists()
        and can_connect_app_server_socket(socket_path)
    ):
        return socket_path

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if restart_existing:
        stop_existing_bridge_app_servers(socket_path)
    if socket_path.exists():
        socket_path.unlink()

    log_path = bridge_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        subprocess.Popen(
            [
                codex_bin(),
                "app-server",
                "--listen",
                f"unix://{socket_path}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if socket_path.exists() and can_connect_app_server_socket(socket_path):
            return socket_path
        time.sleep(0.1)
    raise AppServerError(f"app-server bridge socket did not become ready: {socket_path}")


def bridge_app_server_listener_pids(socket_path: Path) -> list[int]:
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-U"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode not in {0, 1}:
        return []

    pids: list[int] = []
    socket_text = str(socket_path)
    for line in result.stdout.splitlines():
        if socket_text not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        if pid in pids:
            continue
        try:
            command = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        command_text = command.stdout.strip()
        if (
            command.returncode == 0
            and "codex app-server" in command_text
            and f"unix://{socket_text}" in command_text
        ):
            pids.append(pid)
    return pids


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_existing_bridge_app_servers(socket_path: Path) -> dict[str, Any]:
    pids = [pid for pid in bridge_app_server_listener_pids(socket_path) if pid != os.getpid()]
    stopped: list[int] = []
    failed: list[dict[str, Any]] = []
    force_killed: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, 15)
            stopped.append(pid)
        except ProcessLookupError:
            stopped.append(pid)
        except OSError as exc:
            failed.append({"pid": pid, "error": str(exc)})

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        alive = [pid for pid in stopped if process_is_running(pid)]
        if not alive:
            break
        time.sleep(0.1)

    still_running = [pid for pid in stopped if process_is_running(pid)]
    if still_running and not is_falsey(
        os.environ.get("CODEX_RVF_BRIDGE_FORCE_KILL_ON_RESTART", "1")
    ):
        for pid in still_running:
            try:
                os.kill(pid, 9)
                force_killed.append(pid)
            except ProcessLookupError:
                force_killed.append(pid)
            except OSError as exc:
                failed.append({"pid": pid, "signal": 9, "error": str(exc)})
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            alive = [pid for pid in force_killed if process_is_running(pid)]
            if not alive:
                break
            time.sleep(0.1)
        still_running = [pid for pid in stopped if process_is_running(pid)]
    return {
        "pids": pids,
        "stopped": stopped,
        "force_killed": force_killed,
        "failed": failed,
        "still_running": still_running,
    }


def bridge_retry_after_app_server_error(error: Exception) -> bool:
    if is_falsey(os.environ.get("CODEX_RVF_BRIDGE_RETRY_ON_APP_SERVER_ERROR")):
        return False
    text = str(error).lower()
    return (
        "failed to load configuration" in text
        or "operation not permitted" in text
        or "os error 1" in text
    )


def maybe_open_fork_in_codex(fork_thread_id: str) -> bool:
    if os.environ.get("CODEX_RVF_OPEN_GUI_FORK", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False
    if sys.platform != "darwin":
        return False
    url = f"codex://local/{fork_thread_id}"
    try:
        subprocess.Popen(
            ["open", url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def open_gui_fork_unavailable_reason() -> str | None:
    if os.environ.get("CODEX_RVF_OPEN_GUI_FORK", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return "disabled"
    if sys.platform != "darwin":
        return "unsupported-platform"
    return None


def open_gui_fork_attempts() -> int:
    raw = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_ATTEMPTS")
    if raw is None or not raw.strip():
        return DEFAULT_OPEN_GUI_FORK_ATTEMPTS
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_OPEN_GUI_FORK_ATTEMPTS


def open_gui_fork_retry_delay_seconds() -> float:
    raw = os.environ.get("CODEX_RVF_OPEN_GUI_FORK_RETRY_DELAY_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_OPEN_GUI_FORK_RETRY_DELAY_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_OPEN_GUI_FORK_RETRY_DELAY_SECONDS


def open_fork_in_codex_with_retries(fork_thread_id: str) -> dict[str, Any]:
    max_attempts = open_gui_fork_attempts()
    retry_delay = open_gui_fork_retry_delay_seconds()
    attempts: list[dict[str, Any]] = []
    started = time.monotonic()
    unavailable_reason = open_gui_fork_unavailable_reason()
    if unavailable_reason is not None:
        opened = maybe_open_fork_in_codex(fork_thread_id)
        attempts.append(
            {
                "attempt": 1,
                "opened": opened,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        return {
            "opened": opened,
            "attempts": attempts,
            "retry_delay_seconds": retry_delay,
            "skipped_retries_reason": unavailable_reason,
        }
    for attempt in range(1, max_attempts + 1):
        opened = maybe_open_fork_in_codex(fork_thread_id)
        attempts.append(
            {
                "attempt": attempt,
                "opened": opened,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        if opened:
            break
        if attempt < max_attempts:
            time.sleep(retry_delay)
    return {
        "opened": any(item["opened"] for item in attempts),
        "attempts": attempts,
        "retry_delay_seconds": retry_delay,
    }


def fork_visibility_timeout_seconds() -> float:
    raw = os.environ.get("CODEX_RVF_FORK_VISIBILITY_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_FORK_VISIBILITY_TIMEOUT_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_FORK_VISIBILITY_TIMEOUT_SECONDS


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def notification_thread_path(
    notifications: list[dict[str, Any]],
    thread_id: str,
) -> str | None:
    for notification in reversed(notifications):
        if notification.get("method") != "thread/started":
            continue
        params = notification.get("params")
        thread = params.get("thread") if isinstance(params, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            continue
        path = thread.get("path")
        if isinstance(path, str) and path:
            return path
    return None


def fork_session_visibility(
    thread_id: str,
    hinted_path: str | None,
) -> dict[str, Any]:
    active_paths: list[str] = []
    hinted = Path(hinted_path).expanduser() if hinted_path else None
    hinted_exists = False
    if hinted is not None:
        try:
            hinted_exists = hinted.exists()
        except OSError:
            hinted_exists = False
        if hinted_exists and path_is_relative_to(hinted, DEFAULT_CODEX_SESSIONS_DIR):
            active_paths.append(str(hinted))

    if not active_paths and DEFAULT_CODEX_SESSIONS_DIR.exists():
        active_paths.extend(
            str(path)
            for path in DEFAULT_CODEX_SESSIONS_DIR.rglob(f"*{thread_id}*.jsonl")
        )

    location = "active" if active_paths else "missing"

    return {
        "thread_id": thread_id,
        "hinted_path": str(hinted) if hinted is not None else None,
        "hinted_exists": hinted_exists,
        "location": location,
        "active_paths": active_paths,
    }


def wait_for_fork_session_visibility(
    thread_id: str,
    hinted_path: str | None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    timeout = fork_visibility_timeout_seconds() if timeout_seconds is None else timeout_seconds
    deadline = time.monotonic() + timeout
    checks = 0
    while True:
        checks += 1
        visibility = fork_session_visibility(thread_id, hinted_path)
        visibility["checks"] = checks
        visibility["timeout_seconds"] = timeout
        if visibility["location"] != "missing" or time.monotonic() >= deadline:
            return visibility
        time.sleep(0.1)


def compact_app_server_thread(thread: dict[str, Any]) -> dict[str, Any]:
    status = thread.get("status")
    return {
        "id": thread.get("id"),
        "name": thread.get("name"),
        "path": thread.get("path"),
        "cwd": thread.get("cwd"),
        "source": thread.get("source"),
        "createdAt": thread.get("createdAt"),
        "updatedAt": thread.get("updatedAt"),
        "status": status if isinstance(status, dict) else None,
    }


def initialize_app_server_client(client: Any) -> None:
    client.request(
        "initialize",
        {
            "clientInfo": APP_SERVER_CLIENT_INFO,
            "capabilities": {
                "experimentalApi": True,
                "optOutNotificationMethods": [],
            },
        },
    )
    send_json = getattr(client, "send_json", None)
    if callable(send_json):
        send_json({"method": "initialized"})


def request_app_server_diagnostic(
    client: AppServerWebSocket,
    method: str,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        return {"ok": True, "result": client.request(method, params)}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def app_server_thread_visibility_diagnostics(
    client: AppServerWebSocket,
    thread_id: str,
    cwd: str | None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {"thread_id": thread_id}

    read_probe = request_app_server_diagnostic(
        client,
        "thread/read",
        {"threadId": thread_id, "includeTurns": False},
    )
    if read_probe.get("ok"):
        result = read_probe.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        read_probe = {
            "ok": True,
            "contains_thread": isinstance(thread, dict) and thread.get("id") == thread_id,
            "thread": compact_app_server_thread(thread) if isinstance(thread, dict) else None,
        }
    diagnostics["thread_read"] = read_probe

    list_params: dict[str, Any] = {
        "limit": 50,
        "sortKey": "updated_at",
        "sortDirection": "desc",
        "archived": False,
        "useStateDbOnly": False,
    }
    if cwd:
        list_params["cwd"] = cwd
    list_probe = request_app_server_diagnostic(client, "thread/list", list_params)
    if list_probe.get("ok"):
        result = list_probe.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        threads = data if isinstance(data, list) else []
        matches = [
            compact_app_server_thread(thread)
            for thread in threads
            if isinstance(thread, dict) and thread.get("id") == thread_id
        ]
        list_probe = {
            "ok": True,
            "params": list_params,
            "contains_thread": bool(matches),
            "matches": matches,
            "returned": len(threads),
            "nextCursor": result.get("nextCursor") if isinstance(result, dict) else None,
        }
    diagnostics["thread_list"] = list_probe

    loaded_probe = request_app_server_diagnostic(
        client,
        "thread/loaded/list",
        {"limit": 200},
    )
    if loaded_probe.get("ok"):
        result = loaded_probe.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        loaded_ids = (
            [item for item in data if isinstance(item, str)]
            if isinstance(data, list)
            else []
        )
        loaded_probe = {
            "ok": True,
            "contains_thread": thread_id in loaded_ids,
            "returned": len(loaded_ids),
            "nextCursor": result.get("nextCursor") if isinstance(result, dict) else None,
        }
    diagnostics["thread_loaded_list"] = loaded_probe

    return diagnostics


def app_server_thread_name_from_result(result: dict[str, Any], thread_id: str) -> str | None:
    thread = result.get("thread")
    if isinstance(thread, dict) and thread.get("id") == thread_id:
        name = thread.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def app_server_thread_metadata_from_result(result: dict[str, Any], thread_id: str) -> dict[str, Any] | None:
    thread = result.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        return None
    name = thread.get("name")
    return {"thread_found": True, "name": name.strip() if isinstance(name, str) and name.strip() else None}


def parent_thread_name_from_app_server(
    thread_id: str | None,
    cwd: str | None,
) -> dict[str, Any]:
    if not thread_id:
        return {"name": None, "thread_found": False, "source": "missing-thread-id"}
    try:
        socket_path, socket_source, socket_selection = select_existing_app_server_socket_for_metadata()
    except Exception as exc:
        return {
            "name": None,
            "thread_found": False,
            "source": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        client = AppServerWebSocket(socket_path)
    except Exception as exc:
        return {
            "name": None,
            "thread_found": False,
            "source": socket_source,
            "socket_path": str(socket_path),
            "socket_selection": socket_selection,
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        initialize_app_server_client(client)
        read_error = None
        try:
            read_result = client.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
            )
            read_metadata = app_server_thread_metadata_from_result(read_result, thread_id)
            name = read_metadata.get("name") if read_metadata else None
        except Exception as exc:
            read_metadata = None
            name = None
            read_error = f"{type(exc).__name__}: {exc}"
        if name:
            return {
                "name": name,
                "thread_found": True,
                "source": socket_source,
                "method": "thread/read",
                "socket_path": str(socket_path),
                "socket_selection": socket_selection,
            }
        if read_metadata is not None:
            return {
                "name": None,
                "thread_found": True,
                "source": socket_source,
                "method": "thread/read",
                "socket_path": str(socket_path),
                "socket_selection": socket_selection,
                "reason": "thread-unnamed",
            }

        list_params: dict[str, Any] = {
            "limit": 50,
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "archived": False,
            "useStateDbOnly": False,
        }
        if cwd:
            list_params["cwd"] = cwd
        list_result = client.request("thread/list", list_params)
        data = list_result.get("data")
        threads = data if isinstance(data, list) else []
        for thread in threads:
            if not isinstance(thread, dict) or thread.get("id") != thread_id:
                continue
            name_value = thread.get("name")
            name = name_value.strip() if isinstance(name_value, str) else ""
            lookup = {
                "name": name or None,
                "thread_found": True,
                "source": socket_source,
                "method": "thread/list",
                "socket_path": str(socket_path),
                "socket_selection": socket_selection,
            }
            if not name:
                lookup["reason"] = "thread-unnamed"
            return lookup
        return {
            "name": None,
            "thread_found": False,
            "source": socket_source,
            "method": "thread/list",
            "socket_path": str(socket_path),
            "socket_selection": socket_selection,
            "reason": "thread-not-found",
            "thread_read_error": read_error,
        }
    except Exception as exc:
        return {
            "name": None,
            "thread_found": False,
            "source": socket_source,
            "socket_path": str(socket_path),
            "socket_selection": socket_selection,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        client.close()


def run_app_server_fork_with_socket(
    *,
    socket_path: Path,
    socket_source: str,
    socket_selection: dict[str, Any],
    parent_thread_id: str,
    parent_thread_path: Path | None,
    cwd: str | None,
    prompt: str,
    model: str | None,
    reasoning_effort: str | None,
    bridge_retry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client = AppServerWebSocket(socket_path)
    try:
        initialize_app_server_client(client)
        requests = app_server_fork_requests(
            parent_thread_id=parent_thread_id,
            parent_thread_path=parent_thread_path,
            cwd=cwd,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        fork_result = client.request("thread/fork", requests[0]["params"])
        fork_thread = fork_result.get("thread")
        if not isinstance(fork_thread, dict) or not isinstance(fork_thread.get("id"), str):
            raise AppServerError("thread/fork did not return a fork thread id")
        fork_thread_id = fork_thread["id"]
        fork_thread_path = (
            fork_thread.get("path") if isinstance(fork_thread.get("path"), str) else None
        )
        turn_params = dict(requests[1]["params"])
        turn_params["threadId"] = fork_thread_id
        turn_result = client.request("turn/start", turn_params)
        turn = turn_result.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        session_hint = fork_thread_path or notification_thread_path(
            client.notifications,
            fork_thread_id,
        )
        session_visibility = wait_for_fork_session_visibility(fork_thread_id, session_hint)
        app_server_visibility = app_server_thread_visibility_diagnostics(
            client,
            fork_thread_id,
            cwd,
        )
        open_result = open_fork_in_codex_with_retries(fork_thread_id)
        session_location = session_visibility.get("location")
        gui_visibility = "unverified-bridge-only"
        if socket_source == "desktop-control":
            gui_visibility = (
                "verified"
                if session_location == "active"
                else f"unverified-session-{session_location or 'unknown'}"
            )
        result = {
            "status": "app-server-started",
            "socket_path": str(socket_path),
            "socket_source": socket_source,
            "socket_selection": socket_selection,
            "fork_thread_id": fork_thread_id,
            "fork_thread_path": fork_thread_path,
            "turn_id": turn_id,
            "session_visibility": session_visibility,
            "app_server_visibility": app_server_visibility,
            "gui_visibility": gui_visibility,
            "opened_gui_deeplink": open_result["opened"],
            "open_gui_deeplink": open_result,
            "notifications": client.notifications[-20:],
        }
        if bridge_retry is not None:
            result["bridge_retry"] = bridge_retry
        return result
    finally:
        client.close()


def run_app_server_fork(
    *,
    parent_thread_id: str,
    parent_thread_path: Path | None,
    cwd: str | None,
    prompt: str,
    model: str | None,
    reasoning_effort: str | None,
    log_path: Path,
) -> dict[str, Any]:
    socket_path, socket_source, socket_selection = select_app_server_socket()
    try:
        return run_app_server_fork_with_socket(
            socket_path=socket_path,
            socket_source=socket_source,
            socket_selection=socket_selection,
            parent_thread_id=parent_thread_id,
            parent_thread_path=parent_thread_path,
            cwd=cwd,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    except AppServerError as first_error:
        if socket_source != "bridge" or not bridge_retry_after_app_server_error(first_error):
            raise

        retry_socket = ensure_bridge_app_server(restart_existing=True)
        retry_selection = {
            "desktop_control": socket_selection.get("desktop_control"),
            "bridge": probe_app_server_socket(retry_socket),
            "bridge_policy": socket_selection.get("bridge_policy", "auto"),
            "bridge_decision": "restarted-after-app-server-error",
        }
        retry = {
            "reason": "app-server-error",
            "first_error": f"{type(first_error).__name__}: {first_error}",
            "first_socket_path": str(socket_path),
            "first_socket_selection": socket_selection,
            "restarted_socket_path": str(retry_socket),
        }
        return run_app_server_fork_with_socket(
            socket_path=retry_socket,
            socket_source="bridge",
            socket_selection=retry_selection,
            parent_thread_id=parent_thread_id,
            parent_thread_path=parent_thread_path,
            cwd=cwd,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            bridge_retry=retry,
        )


def run_fork_experiment(
    event: dict[str, Any],
    latest_user: str,
    ledger: RunLedger | None = None,
) -> dict[str, Any]:
    session_id = parent_thread_id_from_event(event)
    session_path = parent_thread_path_from_event(event)
    cwd_value = event.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else None
    prompt = fork_experiment_prompt(session_id or "", cwd)
    model = string_event_value(event, ("model",))
    reasoning_effort = reasoning_effort_for_fork(event)
    payload = run_codex_fork(
        parent_session_id=session_id or "",
        cwd=cwd,
        prompt=prompt,
        log_prefix="fork-experiment",
        mode_env_name="CODEX_RVF_FORK_EXPERIMENT_MODE",
        suppress_child_stop_hook=True,
        model=model,
        reasoning_effort=reasoning_effort,
        parent_thread_path=session_path,
        allow_desktop_unavailable_report=False,
        ledger=ledger,
        extra_summary={
            "marker": os.environ.get("CODEX_RVF_FORK_EXPERIMENT_MARKER", FORK_EXPERIMENT_MARKER),
            "latest_user_message_path": (
                ledger.artifact("latest-user-message.txt", latest_user)
                if ledger is not None
                else None
            ),
        },
    )
    return payload


def rvf_mode() -> str:
    mode = os.environ.get("CODEX_RVF_MODE", DEFAULT_RVF_MODE).strip().lower()
    if mode in {"continuation", "continue", "block"}:
        return "report"
    if mode in {"off", "skip", "disabled", "disable"}:
        return "off"
    return "fork"


def normalize_backend_from_env(
    event: dict[str, Any] | None = None,
    mode_env_name: str = "CODEX_RVF_FORK_MODE",
) -> str:
    mode = os.environ.get("CODEX_RVF_MODE", DEFAULT_RVF_MODE).strip().lower()
    if mode in {"off", "skip", "disabled", "disable"}:
        return "off"
    if mode in {"continuation", "continue", "block"}:
        return "report-only"

    fork_mode = os.environ.get(mode_env_name, DEFAULT_FORK_LAUNCH_MODE).strip().lower()
    if fork_mode in AUTO_FORK_LAUNCH_MODES:
        if event is not None and current_kanban_task_id(event):
            return "kanban-followup"
        return "kanban"
    if fork_mode in {"gui", "app-server", "appserver"}:
        return "gui"
    if fork_mode in {"cline-kanban", "cline", "kanban", "ck"}:
        return "kanban"
    if fork_mode in {"kanban-followup", "kanban-message", "kanban-inject"}:
        return "kanban-followup"
    if fork_mode in {"manual", "prepare", "prepared", "log-only"}:
        return "manual"
    if fork_mode == "dry-run":
        return "dry-run"
    return fork_mode


def fork_mode_selection_from_env(mode_env_name: str = "CODEX_RVF_FORK_MODE") -> str:
    fork_mode = os.environ.get(mode_env_name, DEFAULT_FORK_LAUNCH_MODE).strip().lower()
    return "auto" if fork_mode in AUTO_FORK_LAUNCH_MODES else "explicit"


def legacy_gui_fallback_enabled() -> bool:
    return not is_falsey(os.environ.get("CODEX_RVF_AUTO_LEGACY_GUI_FALLBACK", "1"))


def cline_kanban_failure_allows_legacy_gui_fallback(result: dict[str, Any]) -> bool:
    if result.get("status") not in {"cline-kanban-unavailable", "cline-kanban-unconfigured"}:
        return False
    error = str(result.get("error") or "")
    blocking_fragments = (
        "no listener pane belongs to tmux session `cline-kanban`",
        "Stop the foreign listener",
    )
    return not any(fragment in error for fragment in blocking_fragments)


def launch_mode_for_backend(backend: str) -> str:
    if backend == "kanban":
        return "cline-kanban"
    if backend == "gui":
        return "gui"
    return backend


def fork_cwd_for_event(event: dict[str, Any], repo: str) -> str:
    cwd_value = event.get("cwd")
    if not isinstance(cwd_value, str) or not cwd_value.strip():
        return repo

    try:
        cwd_path = Path(cwd_value).expanduser().resolve()
        repo_path = Path(repo).expanduser().resolve()
    except OSError:
        return repo

    if cwd_path == repo_path or path_is_relative_to(cwd_path, repo_path):
        return str(cwd_path)
    return repo


def fork_review_validate_fix(
    event: dict[str, Any],
    repo: str,
    ledger: RunLedger | None = None,
) -> dict[str, Any]:
    parent_session_id = parent_thread_id_from_event(event) or ""
    parent_thread_path = parent_thread_path_from_event(event)
    cwd = fork_cwd_for_event(event, repo)
    prompt = fork_review_validate_fix_prompt(parent_session_id, cwd, repo)
    model = string_event_value(event, ("model",))
    reasoning_effort = reasoning_effort_for_fork(event)
    return run_codex_fork(
        parent_session_id=parent_session_id,
        cwd=cwd,
        prompt=prompt,
        log_prefix="review-validate-fix-fork",
        suppress_child_stop_hook=False,
        model=model,
        reasoning_effort=reasoning_effort,
        parent_thread_path=parent_thread_path,
        fallback_failure_reason=fork_failure_report(repo),
        ledger=ledger,
    )


def launch_backend(
    decision: StopDecision,
    event: dict[str, Any],
    ledger: RunLedger,
) -> dict[str, Any]:
    if not decision.repo:
        return skip_payload(
            "Stop decision did not include a target repo.",
            ledger,
            "missing_target_repo",
            backend=decision.backend,
        )
    cwd = decision.cwd or fork_cwd_for_event(event, decision.repo)
    if decision.backend == "kanban-followup":
        task_id = current_kanban_task_id(event)
        if not task_id:
            return skip_payload(
                "Cline Kanban follow-up backend requires KANBAN_TASK_ID or task_id in the Stop event.",
                ledger,
                "kanban_followup_missing_task_id",
                repo=decision.repo,
                cwd=cwd,
                backend=decision.backend,
                **stop_hook_rvf_state_fields(
                    phase="prepare",
                    backend="kanban-followup",
                    backend_raw=decision.backend,
                ),
            )
        attempt_id = current_kanban_attempt_id(event)
        project_path = current_kanban_project_path(event, decision.repo)
        source_session_id = session_id_from_event(event) or parent_thread_id_from_event(event)
        source_thread_path = parent_thread_path_from_event(event)
        source_name_lookup = parent_thread_name_from_app_server(source_session_id, cwd)
        codex_origin = parent_conversation_origin(
            parent_session_id=source_session_id,
            parent_thread_path=source_thread_path,
            run_id=ledger.run_id,
            parent_thread_name=source_name_lookup.get("name"),
            name_lookup=source_name_lookup,
        )
        task_title = current_kanban_task_title(event)
        task_title_source = "cline_kanban_task_env" if task_title else None
        task_lookup: dict[str, Any] | None = None
        if not task_title:
            task_lookup = lookup_cline_kanban_task_title(
                project_path=project_path,
                task_id=task_id,
                ledger=ledger,
            )
            lookup_title = task_lookup.get("title")
            if isinstance(lookup_title, str) and lookup_title.strip():
                task_title = lookup_title.strip()
                task_title_source = str(task_lookup.get("source") or "cline_kanban_task_lookup")
        source_origin = source_origin_for_kanban_task(
            task_id=task_id,
            attempt_id=attempt_id,
            task_title=task_title,
            task_title_source=task_title_source,
            fallback_origin=codex_origin,
        )
        origin_path = ledger.artifact("origin.json", source_origin)
        source_origin_fields = parent_origin_summary_fields(
            parent_session_id=source_session_id,
            parent_thread_path=source_thread_path,
            parent_origin=source_origin,
            parent_name_lookup=source_name_lookup,
            origin_path=origin_path,
        )
        prompt = kanban_followup_review_validate_fix_prompt(
            task_id=task_id,
            attempt_id=attempt_id,
            target_repo=decision.repo,
            cwd=cwd,
            ledger=ledger,
            source_origin=source_origin,
            origin_path=origin_path,
        )
        ledger.event(
            phase="fork",
            event="kanban_followup_started",
            status="started",
            reason_code="kanban_followup_started",
            repo=decision.repo,
            cwd=cwd,
            mode="kanban-followup",
            cline_kanban_task_id=task_id,
            cline_kanban_attempt_id=attempt_id,
            cline_kanban_task_title=source_origin.get("kanban_task_title"),
            cline_kanban_task_title_source=source_origin.get("kanban_task_title_source"),
            cline_kanban_task_lookup=task_lookup,
            **source_origin_fields,
            **stop_hook_rvf_state_fields(
                phase="prepare",
                backend="kanban-followup",
                backend_raw=decision.backend,
            ),
        )
        try:
            message_payload = start_cline_kanban_followup_message(
                project_path=project_path,
                task_id=task_id,
                attempt_id=attempt_id,
                prompt=prompt,
                ledger=ledger,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            ledger.event(
                phase="fork",
                event="kanban_followup_failed",
                status="kanban-followup-unavailable",
                reason_code="kanban_followup_unavailable",
                repo=decision.repo,
                cwd=cwd,
                mode="kanban-followup",
                cline_kanban_task_id=task_id,
                cline_kanban_attempt_id=attempt_id,
                cline_kanban_task_title=source_origin.get("kanban_task_title"),
                cline_kanban_task_title_source=source_origin.get("kanban_task_title_source"),
                cline_kanban_task_lookup=task_lookup,
                **source_origin_fields,
                error=error,
                **stop_hook_rvf_state_fields(
                    phase="prepare",
                    backend="kanban-followup",
                    backend_raw=decision.backend,
                ),
            )
            return ledger.hook_payload(
                status="kanban-followup-unavailable",
                reason_code="kanban_followup_unavailable",
                message=f"Cline Kanban follow-up user message was not injected: {error}",
                repo=decision.repo,
                cwd=cwd,
                backend=decision.backend,
                cline_kanban_task_id=task_id,
                cline_kanban_attempt_id=attempt_id,
                cline_kanban_project_path=project_path,
                cline_kanban_task_title=source_origin.get("kanban_task_title"),
                cline_kanban_task_title_source=source_origin.get("kanban_task_title_source"),
                cline_kanban_task_lookup=task_lookup,
                error=error,
                **source_origin_fields,
                **stop_hook_rvf_state_fields(
                    phase="prepare",
                    backend="kanban-followup",
                    backend_raw=decision.backend,
                ),
            )

        raw_status = str(message_payload.get("status") or "").strip().lower()
        status = (
            "kanban-followup-started"
            if raw_status in {"started", "running", "in_progress", "in-progress"}
            else "kanban-followup-enqueued"
        )
        reason_code = status.replace("-", "_")
        paths = {
            key: value
            for key, value in {
                "prompt": message_payload.get("prompt_path"),
                "message_command": message_payload.get("command_artifact_path"),
            }.items()
            if value
        }
        ledger.event(
            phase="fork",
            event="kanban_followup_completed",
            status=status,
            reason_code=reason_code,
            repo=decision.repo,
            cwd=cwd,
            paths=paths,
            mode="kanban-followup",
            cline_kanban_task_id=message_payload.get("task_id"),
            cline_kanban_attempt_id=message_payload.get("attempt_id"),
            cline_kanban_task_title=source_origin.get("kanban_task_title"),
            cline_kanban_task_title_source=source_origin.get("kanban_task_title_source"),
            cline_kanban_task_lookup=task_lookup,
            cline_kanban_message_id=message_payload.get("message_id"),
            cline_kanban_turn_id=message_payload.get("turn_id") or message_payload.get("turnId"),
            cline_kanban_checkpoint_id=message_payload.get("checkpoint_id") or message_payload.get("checkpointId"),
            **source_origin_fields,
            **stop_hook_rvf_state_fields(
                phase="prepare",
                backend="kanban-followup",
                backend_raw=decision.backend,
            ),
        )
        return ledger.hook_payload(
            status=status,
            reason_code=reason_code,
            message="Cline Kanban follow-up user message was injected.",
            repo=decision.repo,
            cwd=cwd,
            backend=decision.backend,
            mode="kanban-followup",
            prompt_path=message_payload.get("prompt_path"),
            cline_kanban_task_id=message_payload.get("task_id"),
            cline_kanban_attempt_id=message_payload.get("attempt_id"),
            cline_kanban_project_path=project_path,
            cline_kanban_task_title=source_origin.get("kanban_task_title"),
            cline_kanban_task_title_source=source_origin.get("kanban_task_title_source"),
            cline_kanban_task_lookup=task_lookup,
            cline_kanban_message_id=message_payload.get("message_id"),
            cline_kanban_turn_id=message_payload.get("turn_id") or message_payload.get("turnId"),
            cline_kanban_checkpoint_id=message_payload.get("checkpoint_id") or message_payload.get("checkpointId"),
            kanban_followup_payload=message_payload,
            **source_origin_fields,
            **stop_hook_rvf_state_fields(
                phase="prepare",
                backend="kanban-followup",
                backend_raw=decision.backend,
            ),
            **{
                key: value
                for key, value in (decision.summary_fields or {}).items()
                if key != "rvf_state" and not key.startswith("rvf_")
            },
        )
    if not decision.parent_thread_id:
        return skip_payload(
            "Stop event did not expose a parent thread id.",
            ledger,
            "missing_parent_thread_id",
            repo=decision.repo,
            cwd=decision.cwd,
            backend=decision.backend,
            **stop_hook_rvf_state_fields(
                phase="prepare",
                backend=decision.backend,
                backend_raw=decision.backend,
            ),
        )

    prompt = fork_review_validate_fix_prompt(decision.parent_thread_id, cwd, decision.repo)
    model = string_event_value(event, ("model",))
    reasoning_effort = reasoning_effort_for_fork(event)
    return run_codex_fork(
        parent_session_id=decision.parent_thread_id,
        cwd=cwd,
        prompt=prompt,
        log_prefix="review-validate-fix-fork",
        suppress_child_stop_hook=False,
        model=model,
        reasoning_effort=reasoning_effort,
        parent_thread_path=decision.parent_thread_path,
        fallback_failure_reason=fork_failure_report(decision.repo),
        ledger=ledger,
        launch_mode=launch_mode_for_backend(decision.backend),
        extra_summary={
            "backend": decision.backend,
            **(decision.summary_fields or {}),
        },
    )


def review_validate_fix_dispatch(
    event: dict[str, Any],
    repo: str,
    ledger: RunLedger | None = None,
) -> dict[str, Any] | None:
    mode = rvf_mode()
    if mode == "off":
        return skip_payload(
            "CODEX_RVF_MODE=off",
            ledger,
            "mode_off",
            repo=repo,
        )
    if mode == "report":
        report = fork_failure_report(repo)
        if ledger is not None:
            ledger.event(
                phase="fork",
                event="skipped",
                status="skipped",
                reason_code="continuation_disabled",
                repo=repo,
                message=report,
            )
            return ledger.hook_payload(
                status="skipped",
                reason_code="continuation_disabled",
                message=report,
                repo=repo,
            )
        return {"continue": True, "systemMessage": report}
    return fork_review_validate_fix(event, repo, ledger)


class _StopContextError(Exception):
    """Raised by `resolve_stop_context` when the Stop event provided a session
    transcript path but it isn't readable. The orchestrator unwraps the
    `skip_payload` attribute and short-circuits the gate."""

    def __init__(self, skip_payload_value: dict[str, Any]) -> None:
        super().__init__("stop context unresolved")
        self.skip_payload = skip_payload_value


@dataclass
class SessionScopePrecheck:
    checked: bool = False
    context: dict[str, Any] | None = None
    skip_payload: dict[str, Any] | None = None
    route_paths: list[str] | None = None


def _string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def session_change_type_from_manifest(manifest: dict[str, Any] | None) -> str | None:
    if not isinstance(manifest, dict):
        return None
    owned_paths = _string_list(manifest.get("owned_paths"))
    owned_dirty_paths = _string_list(manifest.get("owned_dirty_paths"))
    if not owned_paths:
        return "no_codebase_changes"
    if not owned_dirty_paths:
        return "no_dirty_codebase_changes"
    return "dirty_codebase_changes"


def resolve_stop_context(
    event: dict[str, Any],
    repo: str,
    ledger: RunLedger,
) -> dict[str, Any]:
    """Pull the structured fields the rest of the gate flow needs out of the
    Stop event. Raises `_StopContextError` when a session transcript path was
    provided but unreadable — caller maps that to a `transcript_unavailable`
    skip payload."""
    session_paths = event_session_scope_paths(event)
    transcript: Path | None = None
    if session_paths:
        transcript = first_readable_session_path(event)
        if transcript is None:
            ledger.event(
                phase="gate",
                event="session_scope_unavailable",
                status="skipped",
                reason_code="transcript_unavailable",
                repo=repo,
                cwd=event.get("cwd"),
                paths={"transcripts": [str(path) for path in session_paths]},
            )
            raise _StopContextError(
                skip_payload(
                    "session transcript path was provided but is not readable; skipped RVF fork/review.",
                    ledger,
                    "transcript_unavailable",
                    repo=repo,
                )
            )
    return {
        "event": event,
        "repo": repo,
        "cwd": event.get("cwd"),
        "session_id": session_hook_id_from_event(event),
        "parent_session_id": parent_thread_id_from_event(event),
        "session_paths": session_paths,
        "transcript": transcript,
        "latest_user": latest_user_message_from_event(event),
        "session_hook_control": parse_session_hook_control(latest_user_message_from_event(event)),
    }


def refresh_global_diff_tracker(
    context: dict[str, Any],
    ledger: RunLedger,
) -> dict[str, Any]:
    """Emit the Slice 3 shape-compliance ledger event and seed session-unit
    attribution from the transcript via `build_manifest`. The manifest helper
    transitively calls `diff_tracker.register_claims`, which writes
    `session_units` rows for transcript-attributed owned paths. The allocator
    then reads those rows directly without re-deriving them.

    Stays a light step in spirit: nothing here touches the SQLite store
    directly (D10) — observation is still consolidated inside the allocator's
    BEGIN IMMEDIATE transaction. The `build_manifest` invocation pre-populates
    `session_units` for transcript-aware sessions; manual CLI invocations (no
    transcript) bypass this entirely and rely on the allocator's
    `auto_claim_observed=True` fallback."""
    repo = context.get("repo")
    transcript = context.get("transcript")
    ledger.event(
        phase="gate",
        event="tracker_refresh_started",
        status="in_progress",
        reason_code="tracker_refresh_started",
        repo=repo,
        cwd=context.get("cwd"),
        session_id=context.get("session_id"),
    )
    if not isinstance(repo, str) or not repo or transcript is None:
        # 没 transcript / 没 repo：合法的"无 seeding"分支，不算失败。调用方
        # 会继续走 allocator 的 auto-claim fallback 或返回 None。
        return {"observed": False, "manifest": None}
    try:
        manifest = build_manifest(Path(repo).expanduser().resolve(), transcript)
    except Exception as exc:
        # build_manifest 失败时 emit `tracker_refresh_failed`（保持原 ledger
        # event，不双重 log），并把错误信息回传给调用方，让 orchestrator /
        # dispatcher 显式 emit `session_manifest_failed` skip_payload。
        # 历史上的 legacy_session_scope_gate_payload 在同样条件下也是 fail-loud
        # 跳过 fork；新版 4-function split 必须保留这个语义，否则 manifest
        # 失败会被 allocator 的空 session_units 静默降级为 "no scope" 误判。
        error_message = f"{type(exc).__name__}: {exc}"
        ledger.event(
            phase="gate",
            event="tracker_refresh_failed",
            status="failed",
            reason_code="session_manifest_failed",
            repo=repo,
            cwd=context.get("cwd"),
            error=error_message,
        )
        return {"observed": False, "manifest": None, "error": error_message}
    context["manifest"] = manifest
    context["session_change_type"] = session_change_type_from_manifest(manifest)
    return {"observed": True, "manifest": manifest}


def evaluate_session_gate(
    context: dict[str, Any],
    ledger: RunLedger,
) -> dict[str, Any] | None:
    """Hand back a skip payload when the session-level gate already says
    "don't fork" (manual marker present, RVF_STOP_HOOK=disable). Returns None
    to continue with allocator-driven scope check."""
    if context.get("session_hook_control") == "disable":
        # When the user disabled the stop hook for this session via
        # `RVF_STOP_HOOK: disable`, suppress the auto fork. Note the dispatch
        # higher up in main() also catches this; this is a defense-in-depth
        # check so direct callers of the gate flow get the same answer.
        ledger.event(
            phase="gate",
            event="session_hook_disabled_via_marker",
            status="skipped",
            reason_code="session_hook_disabled",
            repo=context.get("repo"),
            cwd=context.get("cwd"),
            session_id=context.get("session_id"),
        )
        return skip_payload(
            "RVF_STOP_HOOK marker disabled the auto fork for this session.",
            ledger,
            "session_hook_disabled",
            repo=context.get("repo"),
            session_id=context.get("session_id"),
        )
    marker_payload = manual_rvf_session_marker_payload(context["event"], ledger)
    if marker_payload is not None:
        return marker_payload
    # DB scope_hash suppression is wired at allocator entry; candidate unit_ids
    # are not available at this layer.
    return None


def _positive_int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def auto_review_lease_ttl_seconds(context: dict[str, Any]) -> int | None:
    event = context.get("event")
    backend = normalize_backend_from_env(event if isinstance(event, dict) else None)
    if backend == "kanban-followup" and isinstance(event, dict) and current_kanban_task_id(event):
        return _positive_int_from_env(
            KANBAN_FOLLOWUP_LEASE_TTL_ENV,
            DEFAULT_KANBAN_FOLLOWUP_LEASE_TTL_SECONDS,
        )
    return None


def allocate_auto_review_scope(
    context: dict[str, Any],
    ledger: RunLedger,
    *,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    """Producer-side gate. Tracker-disabled fallback delegates to the legacy
    manifest-based gate. Otherwise runs `allocate_review_scope` and converts
    the outcome into:
        * None       — allocator acquired a lease (or dry-run says one would
                       be acquired); the Stop hook continues to fork.
        * skip_payload — empty allocator scope or unrecoverable error.
        * dry_run dict — when `dry_run=True` the dispatcher gets the candidate
                         metadata without any tracker writes.
    """
    repo = context.get("repo")
    if _tracker_disabled():
        if dry_run:
            # The dispatcher dry-run path also predates the tracker — feed it
            # back through the manifest gate so disable-mode dispatchers see
            # exactly the same answer they did before Slice 3.
            return _dispatcher_dry_run_via_legacy(context, ledger)
        return legacy_session_scope_gate_payload(context["event"], repo, ledger)

    session_id = context.get("session_id")
    if not session_id:
        # Without a session id we can't meaningfully bind the allocator.
        # Fall back to the legacy gate so behavior matches Phase 0/1 for
        # transcript-less events.
        if dry_run:
            return _dispatcher_dry_run_via_legacy(context, ledger)
        return legacy_session_scope_gate_payload(context["event"], repo, ledger)

    repo_path = Path(repo).expanduser().resolve() if isinstance(repo, str) and repo else None
    if repo_path is None:
        return None

    event = context.get("event")
    backend = normalize_backend_from_env(event if isinstance(event, dict) else None)
    if backend == "kanban-followup" and (
        not isinstance(event, dict) or not current_kanban_task_id(event)
    ):
        ledger.event(
            phase="gate",
            event="kanban_followup_missing_task_id",
            status="skipped",
            reason_code="kanban_followup_missing_task_id",
            repo=repo,
            cwd=context.get("cwd"),
            session_id=session_id,
            **stop_hook_rvf_state_fields(
                phase="complete",
                backend="kanban-followup",
                backend_raw="kanban-followup",
                completion_gate="kanban_followup_missing_task_id",
            ),
        )
        if dry_run:
            return {
                "would_proceed": False,
                "candidate_unit_count": 0,
                "result": None,
                "reason": "kanban_followup_missing_task_id",
            }
        return skip_payload(
            "kanban-followup backend requires the current Cline Kanban task id.",
            ledger,
            "kanban_followup_missing_task_id",
            repo=repo,
            session_id=session_id,
            backend="kanban-followup",
            **stop_hook_rvf_state_fields(
                phase="complete",
                backend="kanban-followup",
                backend_raw="kanban-followup",
                completion_gate="kanban_followup_missing_task_id",
            ),
        )

    run_id = ledger.run_id if hasattr(ledger, "run_id") else "stop-hook-run"
    reviewer_id = "stop-hook" if not dry_run else None
    parent_session_id = context.get("parent_session_id")
    if parent_session_id == session_id:
        parent_session_id = None
    lease_ttl_seconds = auto_review_lease_ttl_seconds(context)
    try:
        manual_probe = _manual_suppression_scope_probe(
            repo=repo_path,
            session_id=session_id,
            parent_session_id=parent_session_id,
        )
        scope_hash = manual_probe.get("scope_hash") if isinstance(manual_probe, dict) else None
        if isinstance(scope_hash, str) and scope_hash:
            manual_match = find_manual_rvf_run_for_scope_hash(
                repo=repo_path,
                scope_hash=scope_hash,
            )
            if manual_match is not None:
                ledger.event(
                    phase="gate",
                    event="manual_scope_hash_match",
                    status="skipped",
                    reason_code=REASON_MANUAL_SCOPE_ALREADY_COMPLETED,
                    repo=repo,
                    cwd=context.get("cwd"),
                    session_id=session_id,
                    tracker_scope_hash=scope_hash,
                    manual_rvf_session_id=manual_match.get("session_id"),
                    manual_rvf_run_id=manual_match.get("run_id"),
                    manual_rvf_completed_at=manual_match.get("completed_at"),
                )
                if dry_run:
                    return {
                        "would_proceed": False,
                        "candidate_unit_count": manual_probe.get("candidate_unit_count", 0),
                        "result": manual_probe,
                        "reason": REASON_MANUAL_SCOPE_ALREADY_COMPLETED,
                    }
                return skip_payload(
                    "manual RVF already completed for this tracker scope",
                    ledger,
                    REASON_MANUAL_SCOPE_ALREADY_COMPLETED,
                    repo=repo,
                    session_id=session_id,
                    tracker_scope_hash=scope_hash,
                    manual_rvf_session_id=manual_match.get("session_id"),
                    manual_rvf_run_id=manual_match.get("run_id"),
                    manual_rvf_completed_at=manual_match.get("completed_at"),
                )
        if dry_run:
            result = allocate_review_scope(
                repo=repo_path,
                session_id=session_id,
                run_id=run_id,
                reviewer_id=reviewer_id,
                output_scope_path=None,
                parent_session_id=parent_session_id,
                holder_kind="reviewer",
                dry_run=True,
                auto_claim_observed=False,
                lease_ttl_seconds=lease_ttl_seconds,
            )
            status = result.get("status")
            if status == "dry_run":
                return {
                    "would_proceed": bool(result.get("would_acquire")),
                    "candidate_unit_count": result.get("candidate_unit_count", 0),
                    "result": result,
                }
            if status == "empty":
                return {
                    "would_proceed": False,
                    "candidate_unit_count": result.get("candidate_unit_count", 0),
                    "result": result,
                }
        result = allocate_review_scope(
            repo=repo_path,
            session_id=session_id,
            run_id=run_id,
            reviewer_id=reviewer_id,
            output_scope_path=None,  # Stop-hook stamps via ledger.artifact below.
            parent_session_id=parent_session_id,
            holder_kind="reviewer",
            dry_run=dry_run,
            lease_ttl_seconds=lease_ttl_seconds,
            # Auto Stop-hook attribution comes from
            # `refresh_global_diff_tracker` → `build_manifest` →
            # `register_claims`; auto-claim here would broaden scope past
            # transcript intent.
            auto_claim_observed=False,
        )
    except Exception as exc:
        ledger.event(
            phase="gate",
            event="allocate_review_scope_failed",
            status="failed",
            reason_code="allocator_error",
            repo=repo,
            cwd=context.get("cwd"),
            error=f"{type(exc).__name__}: {exc}",
        )
        if dry_run:
            return {"would_proceed": False, "candidate_unit_count": 0, "result": None}
        return skip_payload(
            "allocator raised; skipped RVF fork/review.",
            ledger,
            "allocator_error",
            repo=repo,
            error=f"{type(exc).__name__}: {exc}",
        )

    status = result.get("status")
    if status == "allocated":
        scope_payload = result.get("scope")
        artifact_path = ledger.artifact("tracker-scope.json", scope_payload) if scope_payload else None
        # D12: stash the tracker scope meta on the ledger so subsequent hook
        # payload builders (Slice 6 fork-prompt splice) can pick it up. The
        # field is a convention; nothing in Slice 3 reads it back yet.
        if artifact_path is not None:
            try:
                meta = getattr(ledger, "tracker_scope_meta", None)
                if not isinstance(meta, dict):
                    meta = {}
                    setattr(ledger, "tracker_scope_meta", meta)
                meta["tracker_scope_path"] = artifact_path
                meta["tracker_lease_id"] = result.get("lease_id")
                meta["tracker_scope_hash"] = result.get("scope_hash")
                meta["tracker_dir"] = result.get("tracker_dir")
                meta["tracker_lease_ttl_seconds"] = lease_ttl_seconds
            except (AttributeError, TypeError):
                pass
        ledger.event(
            phase="gate",
            event="tracker_scope_allocated",
            status="allocated",
            reason_code=REASON_UNASSIGNED_REVIEW_SCOPE_AVAILABLE,
            reason_code_legacy_alias=LEGACY_REASON_SESSION_OWNED_DIRTY,
            repo=repo,
            cwd=context.get("cwd"),
            session_id=session_id,
            tracker_scope_path=artifact_path,
            tracker_lease_id=result.get("lease_id"),
            tracker_scope_hash=result.get("scope_hash"),
            tracker_unit_count=len(result.get("scope", {}).get("unit_ids", []) if scope_payload else []),
            tracker_lease_ttl_seconds=lease_ttl_seconds,
        )
        if dry_run:
            return {
                "would_proceed": True,
                "candidate_unit_count": result.get("candidate_unit_count", 0),
                "result": result,
            }
        return None
    if status == "dry_run":
        return {
            "would_proceed": bool(result.get("would_acquire")),
            "candidate_unit_count": result.get("candidate_unit_count", 0),
            "result": result,
        }
    if status == "empty":
        manifest = context.get("manifest")
        session_change_type = context.get("session_change_type")
        session_owned_paths = _string_list(manifest.get("owned_paths")) if isinstance(manifest, dict) else []
        session_owned_dirty_paths = (
            _string_list(manifest.get("owned_dirty_paths")) if isinstance(manifest, dict) else []
        )
        ledger.event(
            phase="gate",
            event="session_scope_clean",
            status="skipped",
            reason_code=REASON_NO_UNASSIGNED_REVIEW_SCOPE,
            reason_code_legacy_alias=LEGACY_REASON_NO_SESSION_OWNED_DIRTY,
            repo=repo,
            cwd=context.get("cwd"),
            session_id=session_id,
            candidate_unit_count=result.get("candidate_unit_count", 0),
            leased_excluded_count=result.get("leased_excluded_count", 0),
            session_change_type=session_change_type,
            session_owned_paths=session_owned_paths,
            session_owned_dirty_paths=session_owned_dirty_paths,
        )
        if dry_run:
            return {
                "would_proceed": False,
                "candidate_unit_count": result.get("candidate_unit_count", 0),
                "result": result,
                "session_change_type": session_change_type,
            }
        return skip_payload(
            "no unassigned review scope",
            ledger,
            REASON_NO_UNASSIGNED_REVIEW_SCOPE,
            # D4: keep legacy `reason=no_session_owned_dirty` substring in the
            # hook systemMessage for one release so dispatcher / installed-hook
            # downstream assertions don't all churn at once. The structured
            # `reason_code` field has already flipped to the new name.
            detail=f"reason={LEGACY_REASON_NO_SESSION_OWNED_DIRTY}",
            repo=repo,
            session_id=session_id,
            reason_code_legacy_alias=LEGACY_REASON_NO_SESSION_OWNED_DIRTY,
            candidate_unit_count=result.get("candidate_unit_count", 0),
            leased_excluded_count=result.get("leased_excluded_count", 0),
            session_change_type=session_change_type,
            session_owned_paths=session_owned_paths,
            session_owned_dirty_paths=session_owned_dirty_paths,
        )
    # Other statuses (lock_timeout / error / disabled / unsupported_repo) all
    # degrade gracefully: the allocator already emitted its own events.jsonl
    # marker, and we let the Stop hook continue (returning None) so callers
    # see Phase-0 behavior rather than a hard skip on transient lock issues.
    if dry_run:
        return {
            "would_proceed": False,
            "candidate_unit_count": result.get("candidate_unit_count", 0),
            "result": result,
        }
    return None


def _dispatcher_dry_run_via_legacy(
    context: dict[str, Any],
    ledger: RunLedger,
) -> dict[str, Any]:
    """Dispatcher dry-run helper for tracker-disabled / session-id-less events.
    Builds the manifest exactly like the legacy gate and reports whether the
    legacy `owned_dirty_paths` set is non-empty."""
    repo = context.get("repo")
    transcript = context.get("transcript")
    if not isinstance(repo, str) or not repo or transcript is None:
        return {"would_proceed": False, "candidate_unit_count": 0, "result": None}
    try:
        manifest = build_manifest(Path(repo).expanduser().resolve(), transcript)
    except Exception:
        return {"would_proceed": False, "candidate_unit_count": 0, "result": None}
    owned_dirty = manifest.get("owned_dirty_paths")
    has_dirty = isinstance(owned_dirty, list) and bool(owned_dirty)
    return {
        "would_proceed": has_dirty,
        "candidate_unit_count": len(owned_dirty) if isinstance(owned_dirty, list) else 0,
        "result": {"status": "legacy", "manifest": manifest},
    }


def legacy_session_scope_gate_payload(
    event: dict[str, Any],
    repo: str,
    ledger: RunLedger,
) -> dict[str, Any] | None:
    """Phase-0/1 gate body, kept verbatim so `CODEX_RVF_TRACKER_DISABLE=1`
    users see no behavior change. Reason-code literals stay
    `no_session_owned_dirty` / `session_owned_dirty` here on purpose."""
    session_paths = event_session_scope_paths(event)
    if not session_paths:
        return None

    transcript = first_readable_session_path(event)
    if transcript is None:
        ledger.event(
            phase="gate",
            event="session_scope_unavailable",
            status="skipped",
            reason_code="transcript_unavailable",
            repo=repo,
            cwd=event.get("cwd"),
            paths={"transcripts": [str(path) for path in session_paths]},
        )
        return skip_payload(
            "session transcript path was provided but is not readable; skipped RVF fork/review.",
            ledger,
            "transcript_unavailable",
            repo=repo,
        )

    try:
        manifest = build_manifest(Path(repo).expanduser().resolve(), transcript)
    except Exception as exc:
        ledger.event(
            phase="gate",
            event="session_scope_failed",
            status="failed",
            reason_code="session_manifest_failed",
            repo=repo,
            cwd=event.get("cwd"),
            paths={"transcript": str(transcript)},
            error=f"{type(exc).__name__}: {exc}",
        )
        return skip_payload(
            "session manifest failed; skipped RVF fork/review.",
            ledger,
            "session_manifest_failed",
            repo=repo,
            error=f"{type(exc).__name__}: {exc}",
        )

    manifest_path = ledger.artifact("session-manifest.json", manifest)
    owned_dirty = manifest.get("owned_dirty_paths")
    if isinstance(owned_dirty, list) and owned_dirty:
        ledger.event(
            phase="gate",
            event="session_scope_detected",
            status="dirty",
            reason_code="session_owned_dirty",
            repo=repo,
            cwd=event.get("cwd"),
            paths={"manifest": manifest_path} if manifest_path else {},
            owned_dirty_paths=owned_dirty,
        )
        return None

    ledger.event(
        phase="gate",
        event="session_scope_clean",
        status="skipped",
        reason_code="no_session_owned_dirty",
        repo=repo,
        cwd=event.get("cwd"),
        paths={"manifest": manifest_path} if manifest_path else {},
        unattributed_dirty_paths=manifest.get("unattributed_dirty_paths"),
    )
    return skip_payload(
        "no session-owned dirty paths",
        ledger,
        "no_session_owned_dirty",
        repo=repo,
        unattributed_dirty_paths=manifest.get("unattributed_dirty_paths"),
    )


def session_scope_gate_payload(
    event: dict[str, Any],
    repo: str,
    ledger: RunLedger,
) -> dict[str, Any] | None:
    """Thin orchestrator over the 4 split functions. Preserves the historical
    contract: returns None to continue, or a hook payload dict to skip."""
    if _tracker_disabled():
        return legacy_session_scope_gate_payload(event, repo, ledger)
    try:
        context = resolve_stop_context(event, repo, ledger)
    except _StopContextError as exc:
        return exc.skip_payload
    if not context.get("session_paths"):
        # Match legacy behavior: no transcript-derived paths means no gate.
        return None
    refresh_result = refresh_global_diff_tracker(context, ledger)
    refresh_error = refresh_result.get("error") if isinstance(refresh_result, dict) else None
    if refresh_error:
        # 与 legacy_session_scope_gate_payload 的 build_manifest 异常分支一致：
        # manifest 构建失败时返回 `session_manifest_failed` skip payload，让
        # Stop hook 显式跳过 fork（fail-loud），避免下游 allocator 看到空的
        # session_units 后被静默判为 `no_unassigned_review_scope` 干净跳过。
        return skip_payload(
            "session manifest failed; skipped RVF fork/review.",
            ledger,
            "session_manifest_failed",
            repo=context.get("repo"),
            session_id=context.get("session_id"),
            error=refresh_error,
        )
    gated = evaluate_session_gate(context, ledger)
    if gated is not None:
        return gated
    return allocate_auto_review_scope(context, ledger, dry_run=False)


def precheck_session_scope_for_dirty_route(
    event: dict[str, Any],
    repo: str,
    ledger: RunLedger,
) -> SessionScopePrecheck:
    """Run the transcript-aware part of the session gate before whole-repo
    dirty-route shortcuts. This keeps background dirty files from deciding
    route type for a read-only chat session."""
    if not event_session_scope_paths(event):
        return SessionScopePrecheck()

    if _tracker_disabled():
        payload = legacy_session_scope_gate_payload(event, repo, ledger)
        return SessionScopePrecheck(checked=True, skip_payload=payload)

    try:
        context = resolve_stop_context(event, repo, ledger)
    except _StopContextError as exc:
        return SessionScopePrecheck(checked=True, skip_payload=exc.skip_payload)

    refresh_result = refresh_global_diff_tracker(context, ledger)
    refresh_error = refresh_result.get("error") if isinstance(refresh_result, dict) else None
    if refresh_error:
        return SessionScopePrecheck(
            checked=True,
            skip_payload=skip_payload(
                "session manifest failed; skipped RVF fork/review.",
                ledger,
                "session_manifest_failed",
                repo=context.get("repo"),
                session_id=context.get("session_id"),
                error=refresh_error,
            ),
        )

    gated = evaluate_session_gate(context, ledger)
    if gated is not None:
        return SessionScopePrecheck(checked=True, context=context, skip_payload=gated)

    manifest = context.get("manifest")
    route_paths = (
        _string_list(manifest.get("owned_dirty_paths"))
        if isinstance(manifest, dict)
        else None
    )
    return SessionScopePrecheck(checked=True, context=context, route_paths=route_paths)


def manual_rvf_session_marker_payload(
    event: dict[str, Any],
    ledger: RunLedger,
) -> dict[str, Any] | None:
    session_id = session_hook_id_from_event(event)
    if not session_id:
        return None

    cwd = event.get("cwd")
    marker = read_manual_rvf_session_marker(session_id, cwd if isinstance(cwd, str) and cwd else None)
    if marker is None:
        return None

    run_id = marker[MANUAL_RVF_RUN_ID_KEY]
    completed_at = marker[MANUAL_RVF_COMPLETED_AT_KEY]
    return skip_payload(
        "当前 chat session 已完成手动 $review-validate-fix；"
        "installed Stop hook 跳过自动 RVF fork/review，"
        "但这不是 CODEX_RVF_SUPPRESS_STOP_HOOK 全 hook suppress。"
        f"session_id={session_id}; manual_rvf_run_id={run_id}; "
        f"manual_rvf_completed_at={completed_at}",
        ledger,
        "manual_rvf_already_ran",
        session_id=session_id,
        manual_rvf_run_id=run_id,
        manual_rvf_completed_at=completed_at,
        manual_rvf_expires_at=marker.get("manual_rvf_expires_at"),
        manual_rvf_repo=marker.get("manual_rvf_repo"),
        manual_rvf_dirty_hash=marker.get("manual_rvf_dirty_hash"),
        manual_rvf_marker_path=marker.get("state_path"),
        **stop_hook_rvf_state_fields(
            phase="complete",
            backend="manual",
            backend_raw="manual",
            completion_gate="manual_rvf_already_ran",
        ),
    )


def should_suppress(event: dict[str, Any], latest_user: str | None = None) -> bool:
    if explicit_suppress_requested(event, latest_user):
        return True

    if source_marks_subagent(event.get("source")):
        return True

    return any(session_meta_marks_subagent(path) for path in event_session_paths(event))


def explicit_suppress_requested(event: dict[str, Any], latest_user: str | None = None) -> bool:
    if any(is_truthy(os.environ.get(name)) for name in SUPPRESS_ENV_NAMES):
        return True

    if latest_user and SUPPRESS_STOP_HOOK_MARKER in latest_user:
        return True

    if session_user_message_contains(event, SUPPRESS_STOP_HOOK_MARKER):
        return True

    task_id = current_kanban_task_id(event)
    if task_id:
        marker = read_kanban_task_suppression(task_id)
        if marker and marker.get("suppress_stop_hook") is True:
            return True

    if event.get("suppress_review_validate_fix") is True:
        return True
    if event.get("review_validate_fix_suppressed") is True:
        return True

    return False


def event_marks_subagent(event: dict[str, Any]) -> bool:
    if source_marks_subagent(event.get("source")):
        return True
    return any(session_meta_marks_subagent(path) for path in event_session_paths(event))


def run_gate(repo: str) -> GateResult:
    gate = Path(os.environ.get("CODEX_RVF_GATE", str(DEFAULT_GATE)))
    try:
        completed = subprocess.run(
            ["bash", str(gate), repo],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return GateResult("ERROR", None, str(exc))

    output = completed.stdout.strip()
    first_line = output.splitlines()[0] if output else ""
    parts = first_line.split(maxsplit=1)
    status = parts[0] if parts else "ERROR"
    resolved_repo = parts[1] if len(parts) > 1 else None
    return GateResult(status, resolved_repo, output)


def changed_paths_from_gate_output(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines()[1:]:
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if not path:
            continue
        if " -> " in path:
            old_path, new_path = path.rsplit(" -> ", 1)
            paths.extend([old_path.strip(), new_path.strip()])
            continue
        paths.append(path)
    return paths


def plan_doc_review_classification(paths: list[str]) -> dict[str, Any]:
    normalized = [path.replace("\\", "/") for path in paths if path]
    doc_paths = [
        path
        for path in normalized
        if path.startswith(PLAN_DOC_REVIEW_DIR_PREFIXES)
        and path.lower().endswith((".md", ".mdx", ".rst", ".txt"))
    ]
    plan_like_paths = [
        path
        for path in doc_paths
        if any(marker in Path(path).name.lower() for marker in PLAN_DOC_REVIEW_NAME_MARKERS)
    ]
    return {
        "changed_paths": normalized,
        "doc_paths": doc_paths,
        "plan_like_paths": plan_like_paths,
        "should_route": bool(normalized)
        and len(doc_paths) == len(normalized)
        and bool(plan_like_paths),
    }


def fork_failure_report(repo: str) -> str:
    return (
        "review-validate-fix Stop hook 未运行：无法创建 Codex GUI fork，"
        "且 Stop continuation prompt 已禁用，因为它不会创建真正的新用户 prompt，"
        "只会作为 hook system context 出现在当前轨迹中。"
        f" target_repo={repo}。请检查 Codex Desktop control socket / app-server fork 能力；"
        "修复前需要用户手动触发 $review-validate-fix。"
    )


def payload_decision(
    payload: dict[str, Any],
    *,
    reason_code: str,
    repo: str | None = None,
    cwd: str | None = None,
    backend: str = "off",
    status: str = "skipped",
) -> StopDecision:
    return StopDecision(
        action="emit",
        reason_code=reason_code,
        repo=repo,
        cwd=cwd,
        backend=backend,
        payload=payload,
        status=status,
    )


def skip_decision(
    message: str,
    ledger: RunLedger,
    reason_code: str,
    *,
    repo: str | None = None,
    cwd: str | None = None,
    backend: str = "off",
    **summary_fields: Any,
) -> StopDecision:
    payload_fields = dict(summary_fields)
    if repo is not None:
        payload_fields.setdefault("repo", repo)
    if cwd is not None:
        payload_fields.setdefault("cwd", cwd)
    if backend != "off":
        payload_fields.setdefault("backend", backend)
    payload = skip_payload(
        message,
        ledger,
        reason_code,
        **payload_fields,
    )
    return StopDecision(
        action="emit",
        reason_code=reason_code,
        repo=repo,
        cwd=cwd,
        backend=backend,
        message=message,
        summary_fields=payload_fields,
        payload=payload,
        status="skipped",
    )


def session_hook_control_decision(
    event: dict[str, Any],
    latest_user: str | None,
    ledger: RunLedger,
) -> StopDecision | None:
    session_control = session_hook_control_payload(event, latest_user)
    if session_control is None:
        return None

    session_control_reason = (
        session_control.get("reason_code")
        if isinstance(session_control.get("reason_code"), str)
        else "session_hook_control"
    )
    session_control_message = (
        session_control.get("systemMessage")
        if isinstance(session_control.get("systemMessage"), str)
        else None
    )
    ledger.event(
        phase="gate",
        event="session_hook_control",
        status="completed",
        reason_code=session_control_reason,
        session_id=session_hook_id_from_event(event),
        control_action=session_control.get("control_action"),
        session_hook_gate_state=session_control.get("session_hook_gate_state"),
        state_path=session_control.get("state_path"),
    )
    if (
        session_control.get("control_action") == "on"
        and session_control_reason == "session_hook_gate_enabled"
    ):
        ledger.event(
            phase="gate",
            event="session_hook_control_continue",
            status="completed",
            reason_code=session_control_reason,
            session_id=session_hook_id_from_event(event),
            control_action=session_control.get("control_action"),
            session_hook_gate_state=session_control.get("session_hook_gate_state"),
            state_path=session_control.get("state_path"),
            message=(
                "RVF_STOP_HOOK:on re-enabled this session; continuing the same "
                "Stop event through the normal RVF gate."
            ),
        )
        return None
    ledger.summary(
        status="session-hook-control",
        reason_code=session_control_reason,
        message=session_control_message,
        session_id=session_hook_id_from_event(event),
        control_action=session_control.get("control_action"),
        session_hook_gate_state=session_control.get("session_hook_gate_state"),
        state_path=session_control.get("state_path"),
    )
    payload = ledger.hook_payload(
        status="session-hook-control",
        reason_code=session_control_reason,
        message=session_control_message,
        session_id=session_hook_id_from_event(event),
        control_action=session_control.get("control_action"),
        session_hook_gate_state=session_control.get("session_hook_gate_state"),
        state_path=session_control.get("state_path"),
    )
    return payload_decision(
        payload,
        reason_code=session_control_reason,
        status="session-hook-control",
    )


def report_only_decision(repo: str, ledger: RunLedger) -> StopDecision:
    report = fork_failure_report(repo)
    ledger.event(
        phase="fork",
        event="skipped",
        status="skipped",
        reason_code="continuation_disabled",
        repo=repo,
        message=report,
    )
    payload = ledger.hook_payload(
        status="skipped",
        reason_code="continuation_disabled",
        message=report,
        repo=repo,
        backend="report-only",
    )
    return payload_decision(
        payload,
        reason_code="continuation_disabled",
        repo=repo,
        backend="report-only",
    )


def evaluate_stop_event(event: dict[str, Any], ledger: RunLedger) -> StopDecision:
    latest_user = latest_user_message_from_event(event)
    cwd_value = event.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else None

    if event.get("stop_hook_active") is True:
        return skip_decision(
            "检测到 stop_hook_active=true，为避免递归已跳过。",
            ledger,
            "stop_hook_active",
            cwd=cwd,
            detail="Codex 已在执行 Stop hook，RVF 跳过以避免递归",
        )

    handoff_path_value = handoff_path_from_event(event)
    if handoff_path_value is not None:
        payload = handoff_completion_payload(event, ledger)
        if payload is not None:
            try:
                finalize_record = finalize_for_handoff(
                    handoff_path=handoff_path_value,
                    event=event,
                    decision_kind="handoff-advisory",
                )
                surface_finalize_record_errors(ledger, finalize_record, payload=payload)
                surface_rvf_analyze_advisory(
                    event=event,
                    ledger=ledger,
                    payload=payload,
                    finalize_record=finalize_record,
                )
            except Exception as exc:
                ledger.event(
                    phase="handoff",
                    event="finalize_failed",
                    status="warning",
                    reason_code="finalize_error",
                    level="warn",
                    error={"kind": type(exc).__name__, "message": str(exc)},
                )
            return payload_decision(payload, reason_code="handoff_file_ready", cwd=cwd)

    if latest_user and RVF_ANALYZE_FOLLOWUP_MARKER in latest_user:
        return skip_decision(
            "当前最新用户消息是 Cline Kanban 注入的 RVF analyze follow-up trigger；"
            "本次 Stop 跳过自动 RVF，避免复盘消息结束后递归触发主 review loop。",
            ledger,
            "rvf_analyze_followup_trigger_turn",
            cwd=cwd,
        )

    if latest_user and KANBAN_FOLLOWUP_MARKER in latest_user:
        return skip_decision(
            "当前最新用户消息是 Cline Kanban 注入的 RVF follow-up trigger；"
            "本次 Stop 跳过自动 RVF，避免同一 synthetic user turn 结束后递归触发。",
            ledger,
            "kanban_followup_trigger_turn",
            cwd=cwd,
            backend="kanban-followup",
            **stop_hook_rvf_state_fields(
                phase="complete",
                backend="kanban-followup",
                backend_raw="kanban-followup",
                completion_gate="kanban_followup_trigger_turn",
            ),
        )

    fork_context = rvf_fork_context(latest_user) or rvf_fork_context_from_event(event)
    if fork_context is not None:
        return skip_decision(
            "当前会话已是 review-validate-fix fork，会等待最终 RVF_HANDOFF_FILE，不会再次 fork。",
            ledger,
            "already_rvf_fork",
            cwd=cwd,
        )

    if event_marks_subagent(event):
        return skip_decision(
            "Stop event 来自 Codex subagent，post-work review 只允许主会话触发。",
            ledger,
            "subagent_stop_event",
            cwd=cwd,
        )

    session_control = session_hook_control_decision(event, latest_user, ledger)
    if session_control is not None:
        return session_control

    manual_marker_payload = manual_rvf_session_marker_payload(event, ledger)
    if manual_marker_payload is not None:
        return payload_decision(
            manual_marker_payload,
            reason_code="manual_rvf_already_ran",
            cwd=cwd,
        )

    session_id = session_hook_id_from_event(event)
    if session_id and session_hook_disabled(session_id):
        return skip_decision(
            "当前 chat session 已禁用 RVF_STOP_HOOK；"
            "只跳过 RVF fork/continuation/review gate，"
            f"不控制 dispatcher 的 dev sync。session_id={session_id}",
            ledger,
            "session_hook_disabled",
            cwd=cwd,
            session_id=session_id,
        )

    if should_suppress(event, latest_user):
        return skip_decision("检测到 suppress 标记或环境变量。", ledger, "suppressed", cwd=cwd)

    cwd_result: GateResult | None = None
    if cwd:
        cwd_result = run_gate(cwd)
        ledger.event(
            phase="gate",
            event="dirty_gate_completed",
            status=cwd_result.status.lower(),
            reason_code=f"gate_{cwd_result.status.lower()}",
            repo=cwd_result.repo,
            cwd=cwd,
            gate_output_path=ledger.artifact("gate-output.txt", cwd_result.output) if cwd_result.output else None,
        )
        if cwd_result.status == "DIRTY" and cwd_result.repo:
            all_changed_paths = changed_paths_from_gate_output(cwd_result.output)
            session_precheck = precheck_session_scope_for_dirty_route(
                event,
                cwd_result.repo,
                ledger,
            )
            if session_precheck.skip_payload is not None:
                return payload_decision(
                    session_precheck.skip_payload,
                    reason_code="session_scope_skipped",
                    repo=cwd_result.repo,
                    cwd=cwd,
                )

            route_candidate_paths = (
                session_precheck.route_paths
                if session_precheck.route_paths is not None
                else all_changed_paths
            )
            doc_review = plan_doc_review_classification(route_candidate_paths)
            if doc_review["should_route"]:
                ledger.event(
                    phase="gate",
                    event="plan_doc_review_routed",
                    status="skipped",
                    reason_code="plan_document_only",
                    repo=cwd_result.repo,
                    cwd=cwd,
                    changed_paths=doc_review["changed_paths"],
                    plan_like_paths=doc_review["plan_like_paths"],
                    route="plan-doc-maintainer-review",
                )
                return skip_decision(
                    "plan/document-only dirty scope should route to Plan/Doc Maintainer Review, "
                    "not full review-validate-fix.",
                    ledger,
                    "plan_document_only",
                    repo=cwd_result.repo,
                    cwd=cwd,
                    route="plan-doc-maintainer-review",
                    changed_paths=doc_review["changed_paths"],
                    doc_paths=doc_review["doc_paths"],
                    plan_like_paths=doc_review["plan_like_paths"],
                    **stop_hook_rvf_state_fields(
                        phase="complete",
                        backend="plan-doc-review",
                        backend_raw="plan-doc-review",
                        completion_gate="plan_document_only",
                    ),
                )
            if session_precheck.context is not None:
                session_scope_payload = allocate_auto_review_scope(
                    session_precheck.context,
                    ledger,
                    dry_run=False,
                )
            elif session_precheck.checked:
                session_scope_payload = None
            else:
                session_scope_payload = session_scope_gate_payload(event, cwd_result.repo, ledger)
            if session_scope_payload is not None:
                return payload_decision(
                    session_scope_payload,
                    reason_code="session_scope_skipped",
                    repo=cwd_result.repo,
                    cwd=cwd,
                )

            backend = normalize_backend_from_env(event)
            backend_selection_mode = fork_mode_selection_from_env()
            if backend == "off":
                return skip_decision(
                    "CODEX_RVF_MODE=off",
                    ledger,
                    "mode_off",
                    repo=cwd_result.repo,
                    cwd=cwd,
                    backend=backend,
                )
            if backend == "report-only":
                return report_only_decision(cwd_result.repo, ledger)

            parent_thread_id = parent_thread_id_from_event(event)
            parent_thread_path = parent_thread_path_from_event(event)
            if backend != "kanban-followup" and not parent_thread_id:
                return skip_decision(
                    "Stop event did not expose a parent thread id.",
                    ledger,
                    "missing_parent_thread_id",
                    repo=cwd_result.repo,
                    cwd=cwd,
                    backend=backend,
                    log_prefix="review-validate-fix-fork",
                )
            if backend == "kanban":
                parent_thread_path = first_readable_session_path(event)
                if parent_thread_path is None and backend_selection_mode != "auto":
                    return skip_decision(
                        "Cline Kanban backend requires a readable parent transcript/session "
                        "scope anchor; skipped to avoid starting with an empty session-owned "
                        "worktree bootstrap.",
                        ledger,
                        "cline_kanban_missing_scope_anchor",
                        repo=cwd_result.repo,
                        cwd=cwd,
                        backend=backend,
                    )

            return StopDecision(
                action="launch",
                reason_code="backend_selected",
                repo=cwd_result.repo,
                cwd=fork_cwd_for_event(event, cwd_result.repo),
                parent_thread_id=parent_thread_id,
                parent_thread_path=parent_thread_path,
                backend=backend,
                message="RVF backend selected.",
                summary_fields={
                    "gate_status": cwd_result.status,
                    "backend_selection_mode": backend_selection_mode,
                    "legacy_gui_fallback_role": "backup-of-backup"
                    if backend_selection_mode == "auto"
                    else None,
                    **stop_hook_rvf_state_fields(
                        phase="prepare",
                        backend=backend,
                        backend_raw=backend,
                    ),
                },
                status="started",
            )
        if cwd_result.status == "CLEAN":
            return skip_decision(
                f"当前 cwd 仓库是 clean。repo={cwd_result.repo or cwd}",
                ledger,
                "clean_repo",
                repo=cwd_result.repo or cwd,
                cwd=cwd,
            )

    if cwd_result is not None:
        return skip_decision(
            "当前 cwd 不在 git repo/worktree 内，未自动选择目标仓库。"
            f"cwd gate={cwd_result.status}; cwd={cwd}。"
            "请主会话询问用户提供要运行 review-validate-fix 的目标 repo 路径。",
            ledger,
            "cwd_not_git_repo",
            cwd=cwd,
            gate_status=cwd_result.status,
        )

    return skip_decision(
        "Stop event 未提供可检查的 cwd，未自动选择目标仓库。"
        "请主会话询问用户提供要运行 review-validate-fix 的目标 repo 路径。",
        ledger,
        "missing_cwd",
    )


def start_stop_hook_ledger(event: dict[str, Any]) -> RunLedger:
    cwd_value = event.get("cwd")
    ledger = start_run(
        "stop-hook",
        repo=str(cwd_value) if isinstance(cwd_value, str) else None,
        cwd=str(cwd_value) if isinstance(cwd_value, str) else None,
    )
    stop_event_path = ledger.artifact("stop-event.json", event)
    ledger.event(
        phase="gate",
        event="stop_event_received",
        status="started",
        reason_code="stop_event_received",
        session_id=session_id_from_event(event),
        paths={"stop_event": stop_event_path} if stop_event_path else {},
    )
    return ledger


def suppressed_decision(event: dict[str, Any], ledger: RunLedger) -> StopDecision:
    cwd_value = event.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else None
    message = "检测到 suppress 标记或环境变量。"
    ledger.event(
        phase="gate",
        event="suppressed",
        status="skipped",
        reason_code="suppressed",
        cwd=cwd,
        session_id=session_id_from_event(event),
        message=message,
    )
    return skip_decision(
        message,
        ledger,
        "suppressed",
        cwd=cwd,
        detail="检测到 suppress 标记或环境变量，已跳过 RVF Stop hook。",
    )


def main() -> int:
    event = read_event()
    if event is None:
        return 0

    latest_user = latest_user_message_from_event(event)
    ledger = start_stop_hook_ledger(event)
    # 整体 suppress 必须早于 handoff、dirty gate 和 backend launch，避免子会话停止时继续生成 review/fork artifact。
    if explicit_suppress_requested(event, latest_user) and parse_session_hook_control(latest_user) is None:
        decision = suppressed_decision(event, ledger)
        if decision.payload is not None:
            emit(decision.payload)
        return 0

    decision = evaluate_stop_event(event, ledger)
    if decision.action == "launch":
        provider_health_decision = provider_health_guard_decision(decision, event, ledger)
        if provider_health_decision is not None and provider_health_decision.payload is not None:
            emit(provider_health_decision.payload)
            return 0
        emit(launch_backend(decision, event, ledger))
        return 0
    if decision.payload is not None:
        emit(decision.payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
