#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_LOG_ROOT = SKILL_DIR / "state"
DEFAULT_INLINE_BYTES = 2048
COMPONENTS = {
    "command-lock",
    "dispatcher",
    "stop-hook",
    "prepare-run",
    "reviewer",
    "cline-kanban",
    "contract-check",
    "installer",
}
PRESERVED_SUMMARY_KEYS = {
    "log_prefix",
    "mode",
    "issue_title",
    "parent_thread_id",
    "parent_thread_path",
    "parent_transcript_path",
    "prompt_path",
    "runner_command",
    "runner_pid",
    "runner_stderr_path",
    "runner_stdout_path",
    "startup_prepare_metadata_path",
    "suppress_child_stop_hook",
    "cline_kanban_task_id",
    "cline_kanban_base_ref",
    "cline_kanban_task_prompt_path",
    "workspace_path",
    "worktree_bootstrap_path",
    "worktree_bootstrap_patch_path",
    "worktree_bootstrap_files_dir",
}
PHASES = {
    "dev-sync",
    "gate",
    "fork",
    "prepare",
    "review",
    "validate",
    "handoff",
    "cleanup",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return token.strip("-")[:80] or "rvf"


def new_run_id(component: str) -> str:
    return f"rvf-{compact_timestamp()}-{safe_token(component)}-{secrets.token_hex(4)}"


def new_event_id() -> str:
    return f"evt-{secrets.token_hex(8)}"


def log_root() -> Path:
    for key in ("CODEX_RVF_LOG_ROOT", "CODEX_RVF_STATE_DIR"):
        value = os.environ.get(key)
        if value and value.strip():
            return Path(value).expanduser()
    return DEFAULT_LOG_ROOT


def max_inline_bytes(default: int = DEFAULT_INLINE_BYTES) -> int:
    value = os.environ.get("CODEX_RVF_LOG_MAX_INLINE_BYTES")
    if not value or not value.strip():
        return default
    try:
        return max(0, int(value))
    except ValueError:
        return default


def log_level() -> str:
    value = os.environ.get("CODEX_RVF_LOG_LEVEL", "info").strip().lower()
    return value if value in {"debug", "info", "warn", "error"} else "info"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


class RunLedger:
    def __init__(
        self,
        *,
        component: str,
        repo: str | Path | None = None,
        cwd: str | Path | None = None,
        run_id: str | None = None,
        correlation_id: str | None = None,
        run_dir: str | Path | None = None,
    ) -> None:
        self.component = component
        self.repo = str(repo) if repo is not None else None
        self.cwd = str(cwd) if cwd is not None else None
        self.run_id = (
            run_id
            or os.environ.get("CODEX_RVF_RUN_ID")
            or new_run_id(component)
        )
        self.correlation_id = (
            correlation_id
            or os.environ.get("CODEX_RVF_CORRELATION_ID")
            or self.run_id
        )
        self.root = log_root()
        env_run_dir = os.environ.get("CODEX_RVF_RUN_DIR")
        self.run_dir = (
            Path(run_dir).expanduser()
            if run_dir
            else Path(env_run_dir).expanduser()
            if env_run_dir
            else self.root / "runs" / self.run_id
        )
        self.events_path = self.run_dir / "events.jsonl"
        self.summary_path = self.run_dir / "summary.json"
        self.artifacts_dir = self.run_dir / "artifacts"
        self.available = True
        self.diagnostics: list[dict[str, Any]] = []
        self.last_summary: dict[str, Any] | None = None
        try:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.available = False
            self._diagnose("mkdir", exc)

    def _diagnose(self, operation: str, exc: BaseException) -> None:
        self.available = False
        self.diagnostics.append(
            {
                "kind": "log_unavailable",
                "operation": operation,
                "error": f"{type(exc).__name__}: {exc}",
                "run_dir": str(self.run_dir),
            }
        )

    def env(self) -> dict[str, str]:
        return {
            "CODEX_RVF_RUN_ID": self.run_id,
            "CODEX_RVF_CORRELATION_ID": self.correlation_id,
            "CODEX_RVF_LOG_ROOT": str(self.root),
            "CODEX_RVF_RUN_DIR": str(self.run_dir),
        }

    def artifact_path(self, name: str) -> Path:
        return self.artifacts_dir / safe_token(name)

    def unique_artifact_path(self, name: str) -> Path:
        path = self.artifact_path(name)
        if not path.exists():
            return path
        suffix = path.suffix
        stem = path.name[: -len(suffix)] if suffix else path.name
        for index in range(2, 10000):
            candidate = path.with_name(f"{stem}.{index}{suffix}")
            if not candidate.exists():
                return candidate
        return path.with_name(f"{stem}.{secrets.token_hex(4)}{suffix}")

    def artifact(
        self,
        name: str,
        content_or_bytes: bytes | str | dict[str, Any] | list[Any],
        max_inline_bytes: int | None = None,
        unique: bool = False,
    ) -> str | None:
        del max_inline_bytes
        path = self.unique_artifact_path(name) if unique else self.artifact_path(name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content_or_bytes, bytes):
                path.write_bytes(content_or_bytes)
            elif isinstance(content_or_bytes, str):
                path.write_text(content_or_bytes, encoding="utf-8")
            else:
                path.write_text(
                    json.dumps(content_or_bytes, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            return str(path)
        except OSError as exc:
            self._diagnose(f"artifact:{name}", exc)
            return None

    def event(
        self,
        *,
        component: str | None = None,
        phase: str,
        event: str,
        status: str,
        reason_code: str | None = None,
        level: str | None = None,
        duration_ms: int | None = None,
        repo: str | None = None,
        cwd: str | None = None,
        session_id: str | None = None,
        parent_thread_id: str | None = None,
        fork_thread_id: str | None = None,
        paths: dict[str, Any] | None = None,
        error: Any | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "timestamp": utc_now(),
            "level": level or log_level(),
            "component": component or self.component,
            "phase": phase,
            "event": event,
            "status": status,
            "reason_code": reason_code,
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "event_id": new_event_id(),
            "duration_ms": duration_ms,
            "repo": repo if repo is not None else self.repo,
            "cwd": cwd if cwd is not None else self.cwd,
            "session_id": session_id,
            "parent_thread_id": parent_thread_id,
            "fork_thread_id": fork_thread_id,
            "paths": paths or {},
            "error": error,
        }
        if fields:
            record.update(fields)
        if record["component"] not in COMPONENTS:
            record["error"] = record.get("error") or {
                "kind": "invalid_component",
                "value": record["component"],
            }
        if phase not in PHASES:
            record["error"] = record.get("error") or {
                "kind": "invalid_phase",
                "value": phase,
            }
        try:
            _append_jsonl(self.events_path, record)
        except OSError as exc:
            self._diagnose(f"event:{event}", exc)
        return record

    def summary(
        self,
        *,
        status: str,
        reason_code: str,
        message: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp": utc_now(),
            "updated_at": utc_now(),
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "status": status,
            "reason_code": reason_code,
            "message": message,
            "repo": fields.pop("repo", self.repo),
            "cwd": fields.pop("cwd", self.cwd),
            "run_dir": str(self.run_dir),
            "events_path": str(self.events_path),
            "artifacts_dir": str(self.artifacts_dir),
        }
        payload.update(fields)
        previous = _read_json_object(self.summary_path)
        for key in PRESERVED_SUMMARY_KEYS:
            if key not in payload or payload.get(key) is None:
                value = previous.get(key)
                if value is not None:
                    payload[key] = value
        if self.diagnostics:
            payload["diagnostics"] = self.diagnostics
            payload["log_unavailable"] = True
        self.last_summary = payload
        try:
            _atomic_write_text(
                self.summary_path,
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            )
            self.latest_pointer(status=status, reason_code=reason_code)
        except OSError as exc:
            self._diagnose("summary", exc)
            payload["diagnostics"] = self.diagnostics
            payload["log_unavailable"] = True
        return payload

    def latest_pointer(self, *, status: str, reason_code: str) -> dict[str, Any] | None:
        pointer = {
            "run_id": self.run_id,
            "summary_path": str(self.summary_path),
            "events_path": str(self.events_path),
            "status": status,
            "reason_code": reason_code,
            "updated_at": utc_now(),
        }
        try:
            _atomic_write_text(
                self.root / "latest.json",
                json.dumps(pointer, ensure_ascii=False, indent=2) + "\n",
            )
            return pointer
        except OSError as exc:
            self._diagnose("latest_pointer", exc)
            return None

    def hook_payload(
        self,
        *,
        status: str,
        reason_code: str,
        continue_: bool = True,
        message: str | None = None,
        detail: str | None = None,
        **summary_fields: Any,
    ) -> dict[str, Any]:
        summary = self.summary(
            status=status,
            reason_code=reason_code,
            message=message,
            **summary_fields,
        )
        if summary.get("log_unavailable"):
            system_message = (
                f"review-validate-fix: {status}; reason={reason_code}; "
                "log_unavailable=true"
            )
        else:
            detail_note = f"; detail={detail}" if detail else ""
            system_message = (
                f"review-validate-fix: {status}; reason={reason_code}{detail_note}; "
                f"summary={self.summary_path}"
            )
        return {"continue": continue_, "systemMessage": system_message}


def start_run(
    component: str,
    repo: str | Path | None = None,
    cwd: str | Path | None = None,
    run_id: str | None = None,
    correlation_id: str | None = None,
    run_dir: str | Path | None = None,
) -> RunLedger:
    return RunLedger(
        component=component,
        repo=repo,
        cwd=cwd,
        run_id=run_id,
        correlation_id=correlation_id,
        run_dir=run_dir,
    )
