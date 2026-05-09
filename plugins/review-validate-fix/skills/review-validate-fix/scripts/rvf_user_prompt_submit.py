#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rvf_prep_file
from rvf_logging import start_run
from session_label import text_from_message_payload


DISPATCH_TOKEN_RE = re.compile(r"\bRVF_DISPATCH=token=([0-9A-Fa-f]{16})\b")
RVF_FORK_MARKER = "RVF_FORKED_REVIEW_VALIDATE_FIX"
CLINE_KANBAN_TASK_MARKER = "RVF_CLINE_KANBAN_TASK"
KANBAN_FOLLOWUP_MARKER = "RVF_KANBAN_FOLLOWUP_TRIGGER"
DISPATCH_ORIGIN_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fork", re.compile(rf"\b{re.escape(RVF_FORK_MARKER)}\b")),
    ("kanban-task", re.compile(rf"\b{re.escape(CLINE_KANBAN_TASK_MARKER)}\b")),
    ("kanban-followup", re.compile(rf"\b{re.escape(KANBAN_FOLLOWUP_MARKER)}\b")),
)
RVF_MANUAL_TRIGGERS = ("$review-validate-fix", "/review-validate-fix", ":review-validate-fix")
# Match a manual trigger only at line start or after whitespace, and only
# when the trailing token boundary is clean (\b). This avoids accidentally
# triggering on quoted/embedded literals that appear inside review packets,
# transcript excerpts, error stacks, or normal prose like
# "please document the /review-validate-fix tool".
RVF_MANUAL_TRIGGER_RE = re.compile(
    r"(?:^|\s)[\$/:]review-validate-fix\b",
    re.MULTILINE,
)


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


def detect_origin_marker(text: str) -> str | None:
    for name, pattern in DISPATCH_ORIGIN_MARKERS:
        if pattern.search(text):
            return name
    return None


def detect_manual_trigger(text: str) -> bool:
    return bool(RVF_MANUAL_TRIGGER_RE.search(text))


def _resolve_cwd(event: dict[str, Any]) -> tuple[str, bool]:
    raw = event.get("cwd")
    if isinstance(raw, str) and raw.strip():
        return raw.strip(), False
    return str(Path.cwd()), True


def _git_resolved_repo(cwd: str) -> str | None:
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            top = result.stdout.strip()
            if top:
                return top
    except (FileNotFoundError, OSError):
        return None
    return None


def _create_manual_prep_file(
    *,
    event: dict[str, Any],
    prompt: str,
) -> tuple[rvf_prep_file.PrepFileRecord, dict[str, Any]]:
    """Create a prep file for a same-session manual /review-validate-fix invocation.

    The hook owns this prep file (Stop hook didn't write one). Returns the prep record
    plus a debug dict describing where cwd / transcript came from.
    """
    cwd, cwd_inferred = _resolve_cwd(event)
    origin_repo = _git_resolved_repo(cwd) or cwd
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        session_id = None
    transcript_raw = event.get("transcript_path") or event.get("conversation_path") or event.get("session_path")
    transcript_path: Path | None = None
    if isinstance(transcript_raw, str) and transcript_raw.strip():
        candidate = Path(transcript_raw).expanduser()
        if candidate.exists():
            transcript_path = candidate.resolve()

    ledger = start_run("user-prompt-submit-manual", repo=origin_repo, cwd=cwd)
    artifacts_dir = ledger.artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    target_flow = "flow-manual"
    payload: dict[str, Any] = {
        "origin_session_id": session_id,
        "origin_repo": origin_repo,
        "origin_cwd": cwd,
        "origin_transcript_path": str(transcript_path) if transcript_path else None,
        "target_flow": target_flow,
        "target_worktree": cwd,
        "target_kanban_task_id": None,
        "target_session_id": session_id,
        "dispatch_origin": "post_user_prompt_manual",
        "dispatch_cwd_inferred": cwd_inferred,
        "rvf_run": {
            "run_id": ledger.run_id,
            "run_dir": str(ledger.run_dir),
            "artifacts_dir": str(artifacts_dir),
            "scope_contract_path": str(artifacts_dir / "inputs" / "scope.contract.json"),
            "tracker_scope_path": None,
            "tracker_lease_id": None,
            "tracker_scope_hash": None,
        },
        "handoff_expectations": {
            "handoff_path": str(artifacts_dir / "handoff.md"),
            "expected_artifacts": ["review-result.json", "merge-table.md", "handoff.md"],
        },
        "workflow_constraints": {
            "pause_origin_edits": False,
            "in_place_mode": True,
        },
    }
    rvf_prep_file.sweep_stale()
    record = rvf_prep_file.write_prep_file(payload)
    ledger.event(
        phase="prepare",
        event="manual_dispatch_prep_file_written",
        status="completed",
        reason_code="manual_dispatch_prep_file_written",
        repo=origin_repo,
        cwd=cwd,
        paths={"prep_file": str(record.path)},
        target_flow=target_flow,
        dispatch_origin="post_user_prompt_manual",
    )
    debug = {
        "cwd": cwd,
        "cwd_inferred": cwd_inferred,
        "origin_repo": origin_repo,
        "session_id": session_id,
        "transcript_path": str(transcript_path) if transcript_path else None,
    }
    return record, debug


def _run_shared_workflow(
    *,
    record: rvf_prep_file.PrepFileRecord,
    user_prompt_excerpt: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Import prepare_review_run lazily to avoid pulling diff_tracker on early-exit paths."""
    import prepare_review_run  # noqa: PLC0415 - intentional lazy import

    return prepare_review_run.prepare_run_from_prep_file(
        record,
        timeout_seconds=timeout_seconds,
        user_prompt_excerpt=user_prompt_excerpt,
    )


def _existing_shared_workflow_state(payload: dict[str, Any]) -> dict[str, Any] | None:
    rvf_run = payload.get("rvf_run")
    if not isinstance(rvf_run, dict):
        return None
    state = rvf_run.get("shared_workflow_state")
    if isinstance(state, dict):
        return state
    return None


def inspect_user_prompt_submit(
    event: dict[str, Any],
    *,
    prep_root: str | Path | None = None,
    now: str | None = None,
    shared_workflow_timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    prompt, prompt_source = prompt_text_from_event(event)
    base_payload: dict[str, Any] = {
        "continue": True,
        "workflow_started": False,
        "prompt_source": prompt_source,
    }
    if prompt is None:
        return {**base_payload, "status": "no_prompt"}

    token = dispatch_token_from_text(prompt)
    origin_marker = detect_origin_marker(prompt) if token is None else None
    is_manual = token is None and origin_marker is None and detect_manual_trigger(prompt)
    diagnostic_session_keys = ("cwd", "hook_event_name", "session_id", "agent_id", "agent_type")

    record: rvf_prep_file.PrepFileRecord | None = None
    dispatch_origin: str | None = None
    payload: dict[str, Any] = {**base_payload}

    if token is not None:
        lookup_now = rvf_prep_file.parse_timestamp(now) if now else None
        lookup = rvf_prep_file.read_prep_file(token, root=prep_root, now=lookup_now)
        payload.update(
            {
                "status": lookup.status,
                "token": token,
                "prep_file_path": str(lookup.path),
            }
        )
        diagnostic: dict[str, Any] = {
            "event": "user_prompt_submit_dispatch_probe",
            "status": lookup.status,
            "workflow_started": False,
            "prep_file_path": str(lookup.path),
            "prompt_source": prompt_source,
        }
        if lookup.error:
            diagnostic["error"] = lookup.error
            payload["error"] = lookup.error
        for key in diagnostic_session_keys:
            value = event.get(key)
            if isinstance(value, str) and value:
                diagnostic[key] = value
        try:
            diag_path = rvf_prep_file.append_diagnostic(root=prep_root, token=token, record=diagnostic)
            payload["diagnostic_path"] = str(diag_path)
        except (OSError, rvf_prep_file.PrepFileError) as exc:
            payload["diagnostic_error"] = str(exc)
        if lookup.status != "valid" or lookup.payload is None:
            return payload
        record = rvf_prep_file.PrepFileRecord(
            token=lookup.token, path=lookup.path, payload=dict(lookup.payload)
        )
        dispatch_origin = str(lookup.payload.get("dispatch_origin") or "stop_hook")
    elif origin_marker is not None:
        # Marker without token is an inconsistent state: dispatch should always set token.
        try:
            diag_path = rvf_prep_file.append_diagnostic(
                root=prep_root,
                token=rvf_prep_file.generate_token(),
                record={
                    "event": "user_prompt_submit_dispatch_probe",
                    "status": "dispatch_marker_without_token",
                    "origin_marker": origin_marker,
                    "prompt_source": prompt_source,
                    **{
                        key: event.get(key)
                        for key in diagnostic_session_keys
                        if isinstance(event.get(key), str) and event.get(key)
                    },
                },
            )
            payload["diagnostic_path"] = str(diag_path)
        except (OSError, rvf_prep_file.PrepFileError) as exc:
            payload["diagnostic_error"] = str(exc)
        return {
            **payload,
            "status": "dispatch_marker_without_token",
            "origin_marker": origin_marker,
        }
    elif is_manual:
        try:
            record, debug = _create_manual_prep_file(event=event, prompt=prompt)
            dispatch_origin = "post_user_prompt_manual"
            payload.update(
                {
                    "status": "manual_prep_created",
                    "token": record.token,
                    "prep_file_path": str(record.path),
                    "dispatch_origin": dispatch_origin,
                    "manual_dispatch_debug": debug,
                }
            )
        except Exception as exc:
            return {
                **payload,
                "status": "manual_prep_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
    else:
        return {**base_payload, "status": "no_token"}

    assert record is not None  # narrow type for mypy/readers

    existing_state = _existing_shared_workflow_state(record.payload)
    if existing_state is not None and existing_state.get("status") == "completed":
        payload["workflow_started"] = False
        payload["shared_workflow_state"] = existing_state
        try:
            diag_path = rvf_prep_file.append_diagnostic(
                root=prep_root,
                token=record.token,
                record={
                    "event": "user_prompt_submit_shared_workflow_skipped",
                    "status": "already_completed",
                    "prep_file_path": str(record.path),
                    "dispatch_origin": dispatch_origin,
                    "prompt_source": prompt_source,
                },
            )
            payload.setdefault("diagnostic_path", str(diag_path))
        except (OSError, rvf_prep_file.PrepFileError) as exc:
            payload["diagnostic_error"] = str(exc)
        return payload

    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        rvf_prep_file.append_diagnostic(
            root=prep_root,
            token=record.token,
            record={
                "event": "user_prompt_submit_shared_workflow_started",
                "status": "started",
                "started_at": started_at,
                "prep_file_path": str(record.path),
                "dispatch_origin": dispatch_origin,
                "prompt_source": prompt_source,
            },
        )
    except (OSError, rvf_prep_file.PrepFileError):
        pass

    try:
        result_state = _run_shared_workflow(
            record=record,
            user_prompt_excerpt=prompt[:2000] if prompt else None,
            timeout_seconds=shared_workflow_timeout_seconds,
        )
    except Exception as exc:
        result_state = {
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        try:
            new_rvf_run = dict(record.payload.get("rvf_run") or {})
            new_rvf_run["shared_workflow_state"] = result_state
            rvf_prep_file.update_prep_file(record, {"rvf_run": new_rvf_run})
        except (OSError, rvf_prep_file.PrepFileError):
            pass

    payload["workflow_started"] = result_state.get("status") == "completed"
    payload["shared_workflow_state"] = result_state
    try:
        rvf_prep_file.append_diagnostic(
            root=prep_root,
            token=record.token,
            record={
                "event": "user_prompt_submit_shared_workflow_finished",
                "status": result_state.get("status"),
                "prep_file_path": str(record.path),
                "dispatch_origin": dispatch_origin,
                "error": result_state.get("error"),
            },
        )
    except (OSError, rvf_prep_file.PrepFileError):
        pass
    # Manual same-session path: the hook does not modify the user's prompt and
    # does not export RVF env vars into the agent process, so the agent has no
    # other way to discover the prep file path. Emit a `hookSpecificOutput`
    # block with `additionalContext` so the harness can inject the path /
    # next-step pointer into the main agent's context.
    if dispatch_origin == "post_user_prompt_manual":
        additional_context = _manual_additional_context_text(
            prep_file_path=str(record.path),
            shared_workflow_state=result_state,
        )
        payload["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    return payload


def _manual_additional_context_text(
    *,
    prep_file_path: str,
    shared_workflow_state: dict[str, Any],
) -> str:
    status = shared_workflow_state.get("status")
    artifacts = shared_workflow_state.get("artifacts") if isinstance(shared_workflow_state, dict) else None
    review_env = (
        artifacts.get("review_env") if isinstance(artifacts, dict) else None
    )
    lines = [
        "RVF dispatch prep (post-user-prompt manual auto-prep):",
        f"- prep_file: {prep_file_path}",
        f"- shared_workflow_state.status: {status}",
    ]
    if isinstance(review_env, str) and review_env:
        lines.append(f"- review_env: {review_env}")
    lines.append(
        "- next: source the review env, then `cat $RVF_PREP_FILE` for full payload."
    )
    return "\n".join(lines)


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
    parser = argparse.ArgumentParser(
        description=(
            "RVF UserPromptSubmit hook: detect dispatch tokens / origin markers / manual triggers, "
            "and run the shared prepare entry when applicable."
        )
    )
    parser.add_argument("--prep-root", default=None, help="Override RVF prep file root for tests or local diagnostics.")
    parser.add_argument("--now", default=None, help="Override current UTC timestamp for deterministic tests.")
    parser.add_argument(
        "--shared-workflow-timeout-seconds",
        type=float,
        default=60.0,
        help="Hard timeout for in-process shared prepare execution (seconds).",
    )
    parser.add_argument("--json", action="store_true", help="Emit detector result JSON. Actual hook mode stays silent.")
    args = parser.parse_args()

    result = inspect_user_prompt_submit(
        read_event_stdin(),
        prep_root=args.prep_root,
        now=args.now,
        shared_workflow_timeout_seconds=args.shared_workflow_timeout_seconds,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    elif "hookSpecificOutput" in result:
        # Manual same-session path needs to surface the prep file path back to
        # the main agent. The harness reads `hookSpecificOutput.additionalContext`
        # from a hook's stdout JSON and injects it as additional context. We
        # only emit this payload when something explicitly populated
        # `hookSpecificOutput`; non-manual paths still stay silent in hook mode.
        print(
            json.dumps(
                {"hookSpecificOutput": result["hookSpecificOutput"]},
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
