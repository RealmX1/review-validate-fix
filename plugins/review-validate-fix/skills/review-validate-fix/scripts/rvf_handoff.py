#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from rvf_logging import RunLedger, log_root, rvf_state_fields, safe_token


HANDOFF_FILE_MARKER = "RVF_HANDOFF_FILE"
HANDOFF_FILE_RE = re.compile(
    rf"^\s*{re.escape(HANDOFF_FILE_MARKER)}\s*:\s*(.+?)\s*$",
    re.MULTILINE,
)
SESSION_PATH_KEYS = (
    "transcript_path",
    "session_path",
    "conversation_path",
    "log_path",
    "session_file",
)
FALSE_VALUES = {"0", "false", "no", "n", "off", "disabled"}
MARKDOWN_SUFFIXES = {".md", ".markdown"}


def _message_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)
    return None


def handoff_path_from_text(text: str | None) -> Path | None:
    if not isinstance(text, str):
        return None
    matches = HANDOFF_FILE_RE.findall(text)
    if not matches:
        return None
    raw = matches[-1].strip().strip("`\"'")
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1].strip()
    return Path(raw).expanduser() if raw else None


def latest_assistant_message(path: Path) -> str | None:
    latest: str | None = None
    try:
        with path.expanduser().open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "event_msg" and payload.get("type") == "agent_message":
                    message = payload.get("message")
                    if isinstance(message, str):
                        latest = message
                    continue
                if record.get("type") != "response_item":
                    continue
                if payload.get("type") != "message" or payload.get("role") != "assistant":
                    continue
                message = _message_text(payload.get("content"))
                if message:
                    latest = message
    except (OSError, UnicodeDecodeError):
        return None
    return latest


def event_session_paths(event: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for key in SESSION_PATH_KEYS:
        value = event.get(key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    return paths


def handoff_path_from_event(event: dict[str, Any]) -> Path | None:
    direct = handoff_path_from_text(event.get("last_assistant_message"))
    if direct is not None:
        return direct
    for path in event_session_paths(event):
        candidate = handoff_path_from_text(latest_assistant_message(path))
        if candidate is not None:
            return candidate
    return None


def validate_handoff_path(path: Path) -> tuple[bool, str]:
    if path.suffix.lower() not in MARKDOWN_SUFFIXES:
        return False, "not_markdown"
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False, "unresolvable"
    if not resolved.is_file():
        return False, "missing"
    return True, "ok"


def handoff_open_enabled() -> bool:
    value = os.environ.get("CODEX_RVF_OPEN_HANDOFF")
    if value is None:
        return True
    return value.strip().lower() not in FALSE_VALUES


def handoff_open_command(path: Path) -> list[str]:
    configured = os.environ.get("CODEX_RVF_IDE_OPEN_CMD")
    if configured and configured.strip():
        tokens = shlex.split(configured)
        if any("{path}" in token for token in tokens):
            return [token.replace("{path}", str(path)) for token in tokens]
        return [*tokens, str(path)]
    if sys.platform == "darwin":
        return ["open", str(path)]
    if sys.platform.startswith("win"):
        return ["cmd", "/c", "start", "", str(path)]
    return ["xdg-open", str(path)]


def open_handoff_file(path: Path) -> dict[str, Any]:
    enabled = handoff_open_enabled()
    if not enabled:
        return {"enabled": False, "opened": False, "reason": "disabled"}
    command = handoff_open_command(path)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "enabled": True,
            "opened": False,
            "reason": "timeout",
            "command": command,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
    except OSError as exc:
        return {
            "enabled": True,
            "opened": False,
            "reason": "exec_failed",
            "command": command,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "enabled": True,
        "opened": completed.returncode == 0,
        "reason": "opened" if completed.returncode == 0 else "command_failed",
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _handoff_path_digest(handoff_path: Path) -> str:
    return hashlib.sha256(str(handoff_path).encode("utf-8")).hexdigest()[:12]


def _opened_marker_path(root: Path, handoff_path: Path) -> Path:
    return root / "handoff-opened" / f"{_handoff_path_digest(handoff_path)}.json"


def _write_json_marker(path: Path, payload: dict[str, Any]) -> tuple[bool, dict[str, str] | None]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return False, {
            "kind": "log_unavailable",
            "operation": "handoff_marker",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return True, None


def _manual_open_marker_root(path: Path) -> Path | None:
    if any(os.environ.get(key) for key in ("CODEX_RVF_LOG_ROOT", "CODEX_RVF_STATE_DIR")):
        return log_root()
    run_dir = os.environ.get("CODEX_RVF_RUN_DIR")
    if run_dir and run_dir.strip():
        run_dir_path = Path(run_dir).expanduser()
        if run_dir_path.parent.name == "runs":
            return run_dir_path.parent.parent
        return log_root()
    if (
        path.parent.name == "artifacts"
        and path.parent.parent.parent.name == "runs"
    ):
        return path.parent.parent.parent.parent
    return None


def _record_manual_open(path: Path, open_result: dict[str, Any]) -> dict[str, Any]:
    marker_root = _manual_open_marker_root(path)
    if marker_root is None:
        return {"marker_written": False, "reason": "no_rvf_log_context"}
    if open_result.get("opened") is not True:
        return {"marker_written": False, "reason": "not_opened"}
    marker_path = _opened_marker_path(marker_root, path)
    marker_payload = {
        "source": "manual_open",
        "handoff_path": str(path),
        "open_result": open_result,
    }
    marker_written, marker_error = _write_json_marker(marker_path, marker_payload)
    return {
        "marker_path": str(marker_path),
        "marker_written": marker_written,
        "marker_error": marker_error,
    }


def manual_open_handoff_payload(path: Path) -> dict[str, Any]:
    valid, reason = validate_handoff_path(path)
    if not valid:
        return {
            "valid": False,
            "handoff_path": str(path),
            "opened": False,
            "reason": f"handoff_file_{reason}",
        }
    resolved = path.expanduser().resolve()
    open_result = open_handoff_file(resolved)
    marker_result = _record_manual_open(resolved, open_result)
    return {
        "valid": True,
        "handoff_path": str(resolved),
        "opened": bool(open_result.get("opened")),
        "reason": open_result.get("reason"),
        "open_result": open_result,
        "manual_open_marker": marker_result,
    }


def _advised_marker_path(ledger: RunLedger, session_id: str, handoff_path: Path) -> Path:
    return (
        ledger.root
        / "handoff-advised"
        / f"{safe_token(session_id)}.{_handoff_path_digest(handoff_path)}.json"
    )


def handoff_completion_payload(
    event: dict[str, Any],
    ledger: RunLedger,
) -> dict[str, Any] | None:
    handoff_path = handoff_path_from_event(event)
    if handoff_path is None:
        return None

    valid, reason = validate_handoff_path(handoff_path)
    if not valid:
        ledger.event(
            phase="handoff",
            event="handoff_file_marker_invalid",
            status="skipped",
            reason_code=f"handoff_file_{reason}",
            session_id=str(event.get("session_id") or "unknown-session"),
            paths={"handoff": str(handoff_path)},
        )
        return None

    resolved = handoff_path.expanduser().resolve()
    try:
        previous_summary = json.loads(ledger.summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous_summary = {}
    if not isinstance(previous_summary, dict):
        previous_summary = {}
    previous_state_fields = {
        "backend": previous_summary.get("rvf_backend")
        if isinstance(previous_summary.get("rvf_backend"), str)
        else None,
        "backend_raw": previous_summary.get("rvf_backend_raw")
        if isinstance(previous_summary.get("rvf_backend_raw"), str)
        else None,
        "scope_contract_path": previous_summary.get("rvf_scope_contract_path"),
        "scope_of_work_path": previous_summary.get("rvf_scope_of_work_path"),
        "review_packet_path": previous_summary.get("rvf_review_packet_path"),
        "session_manifest_path": previous_summary.get("rvf_session_manifest_path"),
    }
    session_id = str(event.get("session_id") or "unknown-session")
    marker_path = _advised_marker_path(ledger, session_id, resolved)
    opened_marker_path = _opened_marker_path(ledger.root, resolved)
    marker_written = False
    marker_error: dict[str, str] | None = None
    already_advised = marker_path.exists()
    already_opened = opened_marker_path.exists()
    open_result: dict[str, Any]
    if already_advised or already_opened:
        open_result = {
            "enabled": handoff_open_enabled(),
            "opened": False,
            "reason": "already_advised" if already_advised else "already_opened",
        }
    else:
        open_result = open_handoff_file(resolved)
        marker_payload = {
            "session_id": session_id,
            "handoff_path": str(resolved),
            "open_result": open_result,
        }
        marker_written, marker_error = _write_json_marker(marker_path, marker_payload)
        if marker_error is not None:
            ledger._diagnose("handoff_marker", OSError(marker_error["error"]))
        if open_result.get("opened") is True:
            opened_written, opened_error = _write_json_marker(
                opened_marker_path,
                {
                    "source": "handoff_advisory",
                    **marker_payload,
                },
            )
            if not opened_written and marker_error is None:
                marker_error = opened_error
                ledger._diagnose(
                    "handoff_opened_marker",
                    OSError(opened_error["error"] if opened_error else "unknown error"),
                )

    status = "completed" if open_result.get("opened") or already_advised or already_opened else "warning"
    ledger.event(
        phase="handoff",
        event="handoff_file_ready",
        status=status,
        reason_code="handoff_file_ready",
        level="info" if status == "completed" else "warn",
        session_id=session_id,
        paths={
            "handoff": str(resolved),
            "marker": str(marker_path),
            "opened_marker": str(opened_marker_path),
        },
        handoff_open_enabled=open_result.get("enabled"),
        handoff_open_result=open_result,
        marker_written=marker_written,
        marker_error=marker_error,
        already_advised=already_advised,
        already_opened=already_opened,
        **rvf_state_fields(
            phase="complete",
            **previous_state_fields,
            handoff_path=resolved,
            completion_gate="handoff_file_ready",
        ),
    )
    message = f"review-validate-fix run 已结束。Handoff markdown 文件: {resolved}"
    if open_result.get("enabled") is False:
        message += "。自动打开已禁用。"
    elif open_result.get("opened"):
        message += "。已尝试自动打开。"
    elif already_advised or already_opened:
        message += "。此前已处理过该 handoff。"
    else:
        message += "。自动打开失败，详情见 summary。"
    return ledger.hook_payload(
        status="handoff-advisory",
        reason_code="handoff_file_ready",
        message=message,
        detail=str(resolved),
        handoff_path=str(resolved),
        handoff_open_enabled=open_result.get("enabled"),
        handoff_open_result=open_result,
        marker_path=str(marker_path),
        opened_marker_path=str(opened_marker_path),
        marker_written=marker_written,
        marker_error=marker_error,
        already_advised=already_advised,
        already_opened=already_opened,
        **rvf_state_fields(
            phase="complete",
            **previous_state_fields,
            handoff_path=resolved,
            completion_gate="handoff_file_ready",
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open an RVF handoff markdown file with the configured/default editor."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    open_parser = subparsers.add_parser("open", help="Open a handoff markdown file.")
    open_parser.add_argument("path", help="Path to handoff.md.")
    args = parser.parse_args(argv)

    if args.command == "open":
        payload = manual_open_handoff_payload(Path(args.path).expanduser())
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if payload.get("valid") else 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
