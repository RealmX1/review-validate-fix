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
import rvf_bootstrap_confirm
import rvf_prep_file
from rvf_logging import log_root, start_run
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
# manual 触发可内联指定 review scope：`/review-validate-fix scope: a.py b.py`。
# 取首个 `scope:`（行首或空白前缀，避免命中 `telescope:` 之类）之后直到行尾的
# 内容作为 primary 文件清单。大小写不敏感；建议把 `scope:` 放在该行末尾。
RVF_MANUAL_SCOPE_RE = re.compile(
    r"(?:^|\s)scope:\s*(.+)",
    re.IGNORECASE | re.MULTILINE,
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


def parse_manual_scope_directive(prompt: str | None) -> list[str]:
    """从 manual 触发串里解析内联 ``scope:`` 指令。

    语法：``/review-validate-fix scope: a.py b.py``——取首个 ``scope:`` 之后直到
    行尾的内容，按空白 / 逗号切分成 primary 文件清单，并去掉包裹引号。路径
    规范化（去 ``./`` 前缀、反斜杠归一、去重排序）交给下游 ``prepare_run`` 的
    ``normalized_scope_list``，本函数只负责切分。无 ``scope:`` 时返回空列表。

    注意：``scope:`` 取到行尾，故该行 ``scope:`` 之后的普通文字也会被当成文件；
    约定把 ``scope:`` 放在行末（或单独成行）。
    """
    if not prompt:
        return []
    match = RVF_MANUAL_SCOPE_RE.search(prompt)
    if match is None:
        return []
    tokens = (
        token.strip().strip("'\"")
        for token in re.split(r"[,\s]+", match.group(1).strip())
    )
    return [token for token in tokens if token]


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
    extra_primary_files: list[str] | None = None,
) -> dict[str, Any]:
    """Import prepare_review_run lazily to avoid pulling diff_tracker on early-exit paths."""
    import prepare_review_run  # noqa: PLC0415 - intentional lazy import

    return prepare_review_run.prepare_run_from_prep_file(
        record,
        timeout_seconds=timeout_seconds,
        user_prompt_excerpt=user_prompt_excerpt,
        extra_primary_files=extra_primary_files,
    )


def _existing_shared_workflow_state(payload: dict[str, Any]) -> dict[str, Any] | None:
    rvf_run = payload.get("rvf_run")
    if not isinstance(rvf_run, dict):
        return None
    state = rvf_run.get("shared_workflow_state")
    if isinstance(state, dict):
        return state
    return None


def _bootstrap_confirm_state_root(state_root: str | Path | None) -> Path:
    if state_root is not None:
        return Path(state_root).expanduser()
    return log_root()


def _handle_bootstrap_confirmation(
    event: dict[str, Any],
    prompt: str | None,
    *,
    state_root: Path,
) -> dict[str, Any] | None:
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    marker = rvf_bootstrap_confirm.read_marker(state_root, session_id.strip())
    if marker is None:
        rvf_bootstrap_confirm.sweep_expired(state_root)
        return None
    if rvf_bootstrap_confirm.marker_is_expired(marker):
        rvf_bootstrap_confirm.delete_marker(state_root, session_id.strip())
        rvf_bootstrap_confirm.sweep_expired(state_root)
        return {
            "continue": True,
            "status": "bootstrap_confirm_expired",
            "workflow_started": False,
            "systemMessage": (
                "review-validate-fix: 上一次 bootstrap 确认 marker 已过期，已自动清理。"
                "若仍需触发 RVF，请重新调用。"
            ),
        }
    if rvf_bootstrap_confirm.is_yes_literal(prompt):
        rvf_bootstrap_confirm.delete_marker(state_root, session_id.strip())
        try:
            import codex_stop_review_validate_fix as stop_hook  # noqa: PLC0415

            task_payload = stop_hook.resume_dispatch_from_confirmation_marker(marker)
            return {
                "continue": True,
                "status": "bootstrap_confirm_resumed",
                "workflow_started": True,
                "systemMessage": (
                    "review-validate-fix: 已收到 yes 确认，bootstrap dispatch 已恢复。"
                ),
                "resume_payload": task_payload,
            }
        except Exception as exc:
            return {
                "continue": True,
                "status": "bootstrap_confirm_resume_failed",
                "workflow_started": False,
                "systemMessage": (
                    "review-validate-fix: bootstrap dispatch 恢复失败："
                    f"{type(exc).__name__}: {exc}"
                ),
            }
    rvf_bootstrap_confirm.delete_marker(state_root, session_id.strip())
    return {
        "continue": True,
        "status": "bootstrap_confirm_cancelled",
        "workflow_started": False,
        "systemMessage": (
            "review-validate-fix: 未严格匹配 yes/Yes/YES，bootstrap dispatch 已取消。"
            "本次用户 prompt 将按正常流程处理。"
        ),
    }


def _claude_projects_root() -> Path:
    """Root holding Claude Code 的 per-project transcript 目录。

    尊重 ``CLAUDE_CONFIG_DIR``（Claude Code 把整棵 ``~/.claude`` 树迁到那里），
    否则回落 ``~/.claude``。transcript 落在
    ``<root>/projects/<cwd-slug>/<session-id>.jsonl``。
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = (
        Path(base.strip()).expanduser()
        if isinstance(base, str) and base.strip()
        else Path("~/.claude").expanduser()
    )
    return root / "projects"


def _claude_project_slug(cwd: str) -> str:
    """Claude Code 的 project 目录 slug：cwd 里每个 ``/`` 与 ``.`` → ``-``。"""
    return re.sub(r"[/.]", "-", cwd)


def _resolve_child_transcript_path(
    event: dict[str, Any], *, child_session_id: str
) -> tuple[Path | None, dict[str, Any]]:
    """确定性解析被 dispatch 的 child agent transcript 路径。

    返回 ``(path, info)``。即使文件尚未落盘也给出 child transcript 位置——child
    的*首条* UserPromptSubmit 时 host 已为 transcript 命名但可能还没写出。
    ``capture_run`` 会在 capture 时（child 自身 Stop，那时文件必存在）重新
    ``.is_file()`` 校验，因此记录一个"尚未存在但即将存在"的路径是安全的，且
    严格优于 ``None``（旧行为让持久 ``origin.json`` 对 child 拓扑失明）。

    解析顺序（fail-safe，绝不臆造路径）：
      1. *declared* —— host 在 hook payload 里上报的 ``transcript_path`` /
         ``conversation_path`` / ``session_path``（Claude 与 Codex 均会带），
         即便尚未落盘也采用。
      2. *derived* —— 仅 Claude，且无 declared 路径时：重建
         ``<claude-projects>/<cwd-slug>/<session-id>.jsonl``，仅当该 project
         目录已存在（与 flush 无关的 Claude 信号）才采用，否则返回 ``None``。
    """
    info: dict[str, Any] = {}
    raw = (
        event.get("transcript_path")
        or event.get("conversation_path")
        or event.get("session_path")
    )
    if isinstance(raw, str) and raw.strip():
        candidate = Path(raw.strip()).expanduser().resolve()
        info["transcript_source"] = "declared"
        info["child_transcript_exists"] = candidate.is_file()
        return candidate, info

    cwd = event.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        project_dir = _claude_projects_root() / _claude_project_slug(cwd.strip())
        if project_dir.is_dir():
            derived = (project_dir / f"{child_session_id}.jsonl").resolve()
            info["transcript_source"] = "derived"
            info["child_transcript_exists"] = derived.is_file()
            return derived, info
        info["transcript_source"] = "derive_skipped_no_project_dir"
        info["derive_candidate_dir"] = str(project_dir)
        return None, info

    info["transcript_source"] = "unavailable"
    return None, info


def _backfill_child_session(
    record: rvf_prep_file.PrepFileRecord,
    event: dict[str, Any],
    *,
    prep_root: str | Path | None,
) -> tuple[rvf_prep_file.PrepFileRecord, dict[str, Any]]:
    """Self-backfill the dispatched task agent's session into the prep + origin.

    When the UserPromptSubmit hook fires *inside* a dispatched task agent whose
    session differs from the recorded origin — Cline Kanban flow-2-branch /
    flow-2-inplace *and* flow-1-self-rising / kanban-followup (each runs in its
    own Claude session) — the parent Stop hook only knows its own transcript, so
    ``trajectory_capture.capture_run`` would slice the wrong conversation. Here
    we record the task agent's ``child_session_id`` / ``child_transcript_path``
    into:

    1. the prep payload — ledger trail + idempotency;
    2. the persistent ``origin.json`` — the channel ``capture_run`` reads
       long after the short-TTL prep file has been swept.

    Conservative + idempotent: acts only when the current session id is present
    and differs from ``origin_session_id`` (so same-session manual / followup
    dispatch is untouched). Returns the possibly-updated prep record + a debug
    dict (also emitted as a prep diagnostic).

    Ordering assumption: the parent Stop hook writes ``origin.json`` and spawns
    the task agent *before* the task agent can submit a token-bearing prompt, so
    by the time this runs the parent's ``origin.json`` is fully written; the
    merge here only adds child keys and preserves parent keys. A corrupt/partial
    read still fails closed (caught → ``origin_write_error`` → capture falls back
    to parent transcript, no regression).
    """
    debug: dict[str, Any] = {"backfilled": False}
    child_session_id = event.get("session_id")
    if not isinstance(child_session_id, str) or not child_session_id.strip():
        debug["skip_reason"] = "no_child_session_id"
        return record, debug
    child_session_id = child_session_id.strip()
    origin_session_id = record.payload.get("origin_session_id")
    if not isinstance(origin_session_id, str) or not origin_session_id.strip():
        debug["skip_reason"] = "no_origin_session_id"
        return record, debug
    if child_session_id == origin_session_id.strip():
        debug["skip_reason"] = "same_session"
        return record, debug

    child_transcript, transcript_info = _resolve_child_transcript_path(
        event, child_session_id=child_session_id
    )
    child_transcript_str = str(child_transcript) if child_transcript is not None else None
    debug.update(
        {
            "child_session_id": child_session_id,
            "child_transcript_path": child_transcript_str,
            **transcript_info,
        }
    )

    if (
        record.payload.get("child_session_id") != child_session_id
        or record.payload.get("child_transcript_path") != child_transcript_str
    ):
        try:
            record = rvf_prep_file.update_prep_file(
                record,
                {
                    "child_session_id": child_session_id,
                    "child_transcript_path": child_transcript_str,
                },
            )
        except (OSError, rvf_prep_file.PrepFileError) as exc:
            debug["prep_update_error"] = f"{type(exc).__name__}: {exc}"

    origin_path_raw = record.payload.get("origin_metadata_path")
    origin_path: Path | None = None
    if isinstance(origin_path_raw, str) and origin_path_raw.strip():
        origin_path = Path(origin_path_raw).expanduser()
    else:
        rvf_run = record.payload.get("rvf_run")
        if isinstance(rvf_run, dict):
            run_dir = rvf_run.get("run_dir")
            if isinstance(run_dir, str) and run_dir.strip():
                origin_path = Path(run_dir).expanduser() / "artifacts" / "origin.json"

    if origin_path is not None and origin_path.is_file():
        try:
            origin_payload = json.loads(origin_path.read_text(encoding="utf-8"))
            if not isinstance(origin_payload, dict):
                origin_payload = {}
            if (
                origin_payload.get("child_session_id") != child_session_id
                or origin_payload.get("child_transcript_path") != child_transcript_str
            ):
                origin_payload["child_session_id"] = child_session_id
                origin_payload["child_transcript_path"] = child_transcript_str
                # Reuse rvf_prep_file's atomic writer (O_EXCL tmp + random
                # suffix + replace + failure cleanup) rather than a weaker
                # ad-hoc one — single source of truth for atomic JSON IO.
                rvf_prep_file._atomic_write_json(origin_path, origin_payload)
            debug["backfilled"] = True
            debug["origin_path"] = str(origin_path)
        except (OSError, json.JSONDecodeError) as exc:
            debug["origin_write_error"] = f"{type(exc).__name__}: {exc}"
    else:
        debug["skip_reason"] = "origin_metadata_unavailable"
        if origin_path is not None:
            debug["origin_missing"] = str(origin_path)

    try:
        rvf_prep_file.append_diagnostic(
            root=prep_root,
            token=record.token,
            record={
                "event": "user_prompt_submit_child_session_backfill",
                "status": "ok" if debug.get("backfilled") else "skipped",
                **{key: value for key, value in debug.items() if key != "backfilled"},
            },
        )
    except (OSError, rvf_prep_file.PrepFileError):
        pass
    return record, debug


def inspect_user_prompt_submit(
    event: dict[str, Any],
    *,
    prep_root: str | Path | None = None,
    now: str | None = None,
    shared_workflow_timeout_seconds: float = 60.0,
    bootstrap_confirm_state_root: str | Path | None = None,
) -> dict[str, Any]:
    prompt, prompt_source = prompt_text_from_event(event)
    base_payload: dict[str, Any] = {
        "continue": True,
        "workflow_started": False,
        "prompt_source": prompt_source,
    }
    confirm_root = _bootstrap_confirm_state_root(bootstrap_confirm_state_root)
    confirm_result = _handle_bootstrap_confirmation(event, prompt, state_root=confirm_root)
    if confirm_result is not None:
        confirm_result.setdefault("prompt_source", prompt_source)
        return confirm_result
    if prompt is None:
        return {**base_payload, "status": "no_prompt"}

    token = dispatch_token_from_text(prompt)
    origin_marker = detect_origin_marker(prompt) if token is None else None
    is_manual = token is None and origin_marker is None and detect_manual_trigger(prompt)
    diagnostic_session_keys = ("cwd", "hook_event_name", "session_id", "agent_id", "agent_type")

    record: rvf_prep_file.PrepFileRecord | None = None
    dispatch_origin: str | None = None
    payload: dict[str, Any] = {**base_payload}
    # manual 路径解析出的内联 scope（primary 文件），喂给 shared workflow；
    # 其它 dispatch 路径保持空（scope 由 Stop hook / prep payload 决定）。
    manual_extra_primary_files: list[str] = []

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
        record, child_debug = _backfill_child_session(record, event, prep_root=prep_root)
        if child_debug.get("backfilled"):
            payload["child_session_id"] = child_debug.get("child_session_id")
            payload["child_transcript_path"] = child_debug.get("child_transcript_path")
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
            manual_extra_primary_files = parse_manual_scope_directive(prompt)
            payload.update(
                {
                    "status": "manual_prep_created",
                    "token": record.token,
                    "prep_file_path": str(record.path),
                    "dispatch_origin": dispatch_origin,
                    "manual_dispatch_debug": debug,
                }
            )
            if manual_extra_primary_files:
                payload["manual_scope_files"] = manual_extra_primary_files
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
            extra_primary_files=manual_extra_primary_files or None,
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
            scope_files=manual_extra_primary_files or None,
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
    scope_files: list[str] | None = None,
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
    if scope_files:
        # 触发串里的 `scope:` 已把这些文件作为 primary scope 注入 scope.contract；
        # 提示 agent 无需再手动指定，仍可按需覆盖 scope-of-work。
        lines.append(f"- inline scope (primary): {', '.join(scope_files)}")
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
    parser.add_argument(
        "--bootstrap-confirm-state-root",
        default=None,
        help="Override RVF state root for bootstrap-confirmation marker lookup (tests only).",
    )
    args = parser.parse_args()

    result = inspect_user_prompt_submit(
        read_event_stdin(),
        prep_root=args.prep_root,
        now=args.now,
        shared_workflow_timeout_seconds=args.shared_workflow_timeout_seconds,
        bootstrap_confirm_state_root=args.bootstrap_confirm_state_root,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    elif isinstance(result.get("systemMessage"), str) and result.get("systemMessage"):
        print(
            json.dumps(
                {"continue": bool(result.get("continue", True)), "systemMessage": result["systemMessage"]},
                ensure_ascii=False,
            )
        )
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
