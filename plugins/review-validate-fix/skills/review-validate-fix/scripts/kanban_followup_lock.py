#!/usr/bin/env python3
"""Task/session scoped in-progress marker for Cline Kanban RVF follow-up runs."""

from __future__ import annotations

import json
import os
import secrets
import fcntl
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rvf_logging import safe_token


SUBDIR_NAME = "kanban-followup-in-progress"
MARKER_VERSION = 1
DEFAULT_TTL_SECONDS = 6 * 60 * 60
TTL_ENV = "CODEX_RVF_KANBAN_FOLLOWUP_IN_PROGRESS_TTL_SECONDS"
LOCK_ROOT_ENV = "CODEX_RVF_KANBAN_FOLLOWUP_LOCK_ROOT"
STATUS_ACTIVE = "active"
STATUS_STALE = "stale"
STATUS_INVALID = "invalid"


@dataclass(frozen=True)
class AcquireResult:
    acquired: bool
    path: Path | None
    marker: dict[str, Any] | None
    status: str


def _root(root: Path | None = None) -> Path:
    if root is not None:
        return root.expanduser() / SUBDIR_NAME
    raw = os.environ.get(LOCK_ROOT_ENV)
    if raw and raw.strip():
        return Path(raw).expanduser()
    return Path.home() / ".rvf" / SUBDIR_NAME


def _task_path(task_id: str, root: Path | None = None) -> Path:
    return _root(root) / f"task-{safe_token(task_id)}.json"


def _session_path(session_id: str, root: Path | None = None) -> Path:
    return _root(root) / f"sess-{safe_token(session_id)}.json"


def marker_paths(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> list[Path]:
    paths: list[Path] = []
    if task_id:
        paths.append(_task_path(task_id, root))
    if session_id:
        paths.append(_session_path(session_id, root))
    return paths


def ttl_seconds() -> float:
    raw = os.environ.get(TTL_ENV)
    if raw is None or not raw.strip():
        return float(DEFAULT_TTL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_TTL_SECONDS)
    return max(0.0, value)


def _parse_iso_ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _open_marker_exclusive(path: Path, encoded: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _takeover_lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.takeover.lock")


@contextmanager
def _with_takeover_lock(path: Path):
    lock_path = _takeover_lock_path(path)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(fd, "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def marker_payload(
    *,
    task_id: str | None,
    session_id: str | None,
    run_id: str,
    run_dir: str,
    repo: str | None,
    cwd: str | None,
    attempt_id: str | None = None,
    message_id: str | None = None,
    turn_id: str | None = None,
    prompt_path: str | None = None,
) -> dict[str, Any]:
    ttl = ttl_seconds()
    return {
        "marker_version": MARKER_VERSION,
        "state": "in_progress",
        "armed_at": _iso_now(),
        "expires_at": _iso_after(ttl),
        "ttl_seconds": ttl,
        "kanban_task_id": task_id,
        "kanban_attempt_id": attempt_id,
        "session_id": session_id,
        "run_id": run_id,
        "run_dir": run_dir,
        "repo": repo,
        "cwd": cwd,
        "message_id": message_id,
        "turn_id": turn_id,
        "prompt_path": prompt_path,
    }


def write_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    run_id: str,
    run_dir: str,
    repo: str | None,
    cwd: str | None,
    attempt_id: str | None = None,
    message_id: str | None = None,
    turn_id: str | None = None,
    prompt_path: str | None = None,
    root: Path | None = None,
) -> Path | None:
    paths = marker_paths(task_id=task_id, session_id=session_id, root=root)
    if not paths:
        return None
    payload = marker_payload(
        task_id=task_id,
        session_id=session_id,
        run_id=run_id,
        run_dir=run_dir,
        repo=repo,
        cwd=cwd,
        attempt_id=attempt_id,
        message_id=message_id,
        turn_id=turn_id,
        prompt_path=prompt_path,
    )
    target = paths[0]
    _atomic_write(target, payload)
    return target


def acquire_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    run_id: str,
    run_dir: str,
    repo: str | None,
    cwd: str | None,
    attempt_id: str | None = None,
    message_id: str | None = None,
    turn_id: str | None = None,
    prompt_path: str | None = None,
    root: Path | None = None,
) -> AcquireResult:
    paths = marker_paths(task_id=task_id, session_id=session_id, root=root)
    if not paths:
        return AcquireResult(acquired=True, path=None, marker=None, status=STATUS_INVALID)
    target = paths[0]
    payload = marker_payload(
        task_id=task_id,
        session_id=session_id,
        run_id=run_id,
        run_dir=run_dir,
        repo=repo,
        cwd=cwd,
        attempt_id=attempt_id,
        message_id=message_id,
        turn_id=turn_id,
        prompt_path=prompt_path,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    for _ in range(2):
        try:
            _open_marker_exclusive(target, encoded)
        except FileExistsError:
            marker = read_marker(task_id=task_id, session_id=session_id, root=root)
            status = marker_status(marker)
            if status == STATUS_ACTIVE:
                return AcquireResult(acquired=False, path=target, marker=marker, status=status)
            with _with_takeover_lock(target):
                marker = read_marker(task_id=task_id, session_id=session_id, root=root)
                status = marker_status(marker)
                if status == STATUS_ACTIVE:
                    return AcquireResult(acquired=False, path=target, marker=marker, status=status)
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    return AcquireResult(acquired=False, path=target, marker=marker, status=status)
                try:
                    _open_marker_exclusive(target, encoded)
                except FileExistsError:
                    continue
                return AcquireResult(acquired=True, path=target, marker=payload, status=STATUS_ACTIVE)
            continue
        return AcquireResult(acquired=True, path=target, marker=payload, status=STATUS_ACTIVE)
    marker = read_marker(task_id=task_id, session_id=session_id, root=root)
    return AcquireResult(
        acquired=False,
        path=target,
        marker=marker,
        status=marker_status(marker),
    )


def read_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> dict[str, Any] | None:
    for path in marker_paths(task_id=task_id, session_id=session_id, root=root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("_marker_path", str(path))
            return payload
    return None


def clear_marker(
    *,
    task_id: str | None,
    session_id: str | None,
    root: Path | None = None,
) -> list[str]:
    removed: list[str] = []
    for path in marker_paths(task_id=task_id, session_id=session_id, root=root):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        removed.append(str(path))
    return removed


def marker_status(
    marker: dict[str, Any] | None,
    *,
    now_ts: float | None = None,
) -> str:
    if not isinstance(marker, dict):
        return STATUS_INVALID
    current_ts = datetime.now(timezone.utc).timestamp() if now_ts is None else now_ts
    expires_ts = _parse_iso_ts(marker.get("expires_at"))
    if expires_ts is not None:
        return STATUS_ACTIVE if current_ts <= expires_ts else STATUS_STALE
    armed_ts = _parse_iso_ts(marker.get("armed_at"))
    if armed_ts is None:
        return STATUS_INVALID
    return STATUS_ACTIVE if current_ts - armed_ts <= ttl_seconds() else STATUS_STALE
