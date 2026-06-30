#!/usr/bin/env python3
"""Live web dashboard for the global reviewed-diff tracker.

Run as a local HTTP server; the page polls a JSON snapshot endpoint and
re-renders client-side. JSON-only consumers can hit /api/snapshot directly.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import sqlite3
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _rvf_pyroot  # noqa: E402,F401 — pyroot 上 sys.path，供 core.* import
from core.session_scope_allocation import reviewable_unit_diff_tracker  # noqa: E402
from session_label import codex_session_label  # noqa: E402


_APP_SERVER_NAME_CACHE: dict[str, str | None] = {}
_APP_SERVER_NAME_CACHE_DISK_LOADED: dict[Path, bool] = {}
_APP_SERVER_NAME_CACHE_FILENAME = "dashboard-thread-names.json"
_APP_SERVER_NAME_CACHE_PERSIST_LOCK = threading.Lock()
_APP_SERVER_MODULE: Any = None
_APP_SERVER_MODULE_LOADED = False


def _disable_app_server_lookup() -> bool:
    import os as _os
    return _os.environ.get("RVF_DASHBOARD_DISABLE_APP_SERVER_LOOKUP", "").strip().lower() in {"1", "true", "yes", "on"}


def _load_app_server_module() -> Any:
    global _APP_SERVER_MODULE, _APP_SERVER_MODULE_LOADED
    if _APP_SERVER_MODULE_LOADED:
        return _APP_SERVER_MODULE
    _APP_SERVER_MODULE_LOADED = True
    if _disable_app_server_lookup():
        return None
    try:
        import codex_stop_review_validate_fix as mod  # heavy; lazy
        _APP_SERVER_MODULE = mod
    except Exception:
        _APP_SERVER_MODULE = None
    return _APP_SERVER_MODULE


def _thread_name_cache_path(cache_dir: Path | None) -> Path | None:
    if cache_dir is None:
        return None
    try:
        return Path(cache_dir) / _APP_SERVER_NAME_CACHE_FILENAME
    except Exception:
        return None


def _load_thread_name_cache_from_disk(cache_dir: Path | None) -> None:
    path = _thread_name_cache_path(cache_dir)
    if path is None or _APP_SERVER_NAME_CACHE_DISK_LOADED.get(path):
        return
    _APP_SERVER_NAME_CACHE_DISK_LOADED[path] = True
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return
    for key, value in entries.items():
        if key in _APP_SERVER_NAME_CACHE:
            continue
        name = value.get("name") if isinstance(value, dict) else None
        if isinstance(name, str) and name.strip():
            _APP_SERVER_NAME_CACHE[key] = name.strip()
        else:
            _APP_SERVER_NAME_CACHE[key] = None


def _persist_thread_name_cache(cache_dir: Path | None) -> None:
    path = _thread_name_cache_path(cache_dir)
    if path is None:
        return
    with _APP_SERVER_NAME_CACHE_PERSIST_LOCK:
        payload = {
            "saved_at": _utc_now_iso(),
            "entries": {
                key: {"name": value, "saved_at": _utc_now_iso()}
                for key, value in _APP_SERVER_NAME_CACHE.items()
                if value is not None
            },
        }
        serialized = json.dumps(payload, indent=2)
        tmp_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp file in the same directory so os.replace is atomic
            # on the same filesystem; this prevents concurrent ThreadingMixIn
            # writers from interleaving and corrupting the on-disk cache.
            fd, tmp_name = tempfile.mkstemp(
                prefix=path.name + ".",
                suffix=".tmp",
                dir=str(path.parent),
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(serialized)
            except Exception:
                # fdopen took ownership of fd; nothing extra to close here.
                raise
            os.replace(str(tmp_path), str(path))
            tmp_path = None
        except OSError:
            pass
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


def _lookup_app_server_thread_name(
    session_id: str | None,
    cwd: str | None,
    *,
    cache_dir: Path | None = None,
) -> str | None:
    if not session_id:
        return None
    cache_key = f"{session_id}|{cwd or ''}"
    _load_thread_name_cache_from_disk(cache_dir)
    if cache_key in _APP_SERVER_NAME_CACHE:
        return _APP_SERVER_NAME_CACHE[cache_key]
    mod = _load_app_server_module()
    if mod is None:
        _APP_SERVER_NAME_CACHE[cache_key] = None
        return None
    name: str | None = None
    try:
        result = mod.parent_thread_name_from_app_server(session_id, cwd)
        if isinstance(result, dict):
            value = result.get("name")
            if isinstance(value, str) and value.strip():
                name = value.strip()
    except Exception:
        name = None
    _APP_SERVER_NAME_CACHE[cache_key] = name
    if name is not None:
        _persist_thread_name_cache(cache_dir)
    return name


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _fetch_all(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, tuple(params))
    return [_row_to_dict(row) for row in cur.fetchall()]


def _read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    rows = _fetch_all(conn, "SELECT key, value FROM meta")
    return {row["key"]: row["value"] for row in rows}


def _read_events_tail(events_path: Path, limit: int) -> list[dict[str, Any]]:
    if not events_path.is_file() or limit <= 0:
        return []
    with events_path.open("r", encoding="utf-8") as handle:
        lines = handle.readlines()
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    out.reverse()
    return out


def _compute_counters(snapshot: dict[str, Any]) -> dict[str, Any]:
    units = snapshot["units"]
    leases = snapshot["leases"]
    review_states: dict[str, int] = {}
    observed_states: dict[str, int] = {}
    tombstone_states: dict[str, int] = {}
    for unit in units:
        review_states[unit["review_state"]] = review_states.get(unit["review_state"], 0) + 1
        observed_states[unit["observed_state"]] = observed_states.get(unit["observed_state"], 0) + 1
        tombstone_key = "tombstoned" if unit.get("is_tombstoned") else "active"
        tombstone_states[tombstone_key] = tombstone_states.get(tombstone_key, 0) + 1
    lease_states: dict[str, int] = {}
    for lease in leases:
        lease_states[lease["state"]] = lease_states.get(lease["state"], 0) + 1
    participants = snapshot.get("lease_participants", []) or []
    participants_active = sum(1 for p in participants if p.get("state") == "active")
    return {
        "units_total": len(units),
        "review_state": review_states,
        "observed_state": observed_states,
        "tombstone_state": tombstone_states,
        "leases_total": len(leases),
        "lease_state": lease_states,
        "sessions_total": len(snapshot["sessions"]),
        "manual_runs_total": len(snapshot["manual_runs"]),
        "branches_total": len(snapshot["branches"]),
        "worktrees_total": len(snapshot["worktrees"]),
        "lease_participants_total": len(participants),
        "lease_participants_active": participants_active,
    }


_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


def _build_codex_transcript_index(root: Path | None = None) -> dict[str, Path]:
    """Map session_id (UUIDv7, 36 chars) → newest matching transcript path.

    Codex transcript filenames are `rollout-<timestamp>-<uuid>.jsonl`. We pull
    the trailing 36 chars before `.jsonl` as the session id and keep the most
    recently-modified file for each id.
    """
    base = root or _CODEX_SESSIONS_DIR
    idx: dict[str, Path] = {}
    if not base.is_dir():
        return idx
    try:
        for path in base.rglob("rollout-*.jsonl"):
            name = path.name
            if not name.endswith(".jsonl") or len(name) < 42:
                continue
            sid = name[-42:-6]
            if sid.count("-") != 4:
                continue
            existing = idx.get(sid)
            if existing is None:
                idx[sid] = path
                continue
            try:
                if path.stat().st_mtime > existing.stat().st_mtime:
                    idx[sid] = path
            except OSError:
                continue
    except OSError:
        pass
    return idx


def _read_codex_session_meta(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    return {}
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                git = payload.get("git")
                if not isinstance(git, dict):
                    git = {}
                return {
                    "cwd": payload.get("cwd") or "",
                    "originator": payload.get("originator") or "",
                    "branch": git.get("branch") or "",
                    "commit": (git.get("commit_hash") or "")[:8],
                }
    except (OSError, UnicodeDecodeError):
        return {}
    return {}


def _short_cwd_label(cwd: str) -> str:
    if not cwd:
        return ""
    parts = list(Path(cwd).parts)
    if parts and parts[0] == "/":
        parts = parts[1:]
    if len(parts) >= 2 and parts[0] == "Users":
        parts = parts[2:]
    while parts and parts[0].startswith("."):
        parts = parts[1:]
    if not parts:
        return cwd
    return "/".join(parts[-3:]) if len(parts) >= 3 else "/".join(parts)


def _enrich_sessions_with_codex_meta(sessions: list[dict[str, Any]], *, cache_dir: Path | None = None) -> None:
    if not sessions:
        return
    needed = {
        s.get("session_id")
        for s in sessions
        if s.get("session_id") and not str(s.get("session_id")).startswith("demo-")
    }
    if not needed:
        return
    idx = _build_codex_transcript_index()
    if not idx:
        return
    for s in sessions:
        sid = s.get("session_id")
        if not sid or sid not in idx:
            continue
        transcript_path = idx[sid]
        meta = _read_codex_session_meta(transcript_path)
        cwd_value = ""
        if meta:
            cwd_value = meta.get("cwd", "")
            s["origin_cwd"] = cwd_value
            s["origin_branch"] = meta.get("branch", "")
            s["origin_commit"] = meta.get("commit", "")
            s["originator"] = meta.get("originator", "")
        thread_name = _lookup_app_server_thread_name(sid, cwd_value or None, cache_dir=cache_dir)
        if thread_name:
            s["display_name"] = thread_name
            s["display_name_source"] = "app_server_thread_name"
            continue
        prompt_label = codex_session_label(transcript_path)
        if prompt_label:
            s["display_name"] = prompt_label
            s["display_name_source"] = "first_user_prompt"
            continue
        cwd_label = _short_cwd_label(cwd_value)
        if cwd_label:
            s["display_name"] = cwd_label
            s["display_name_source"] = "cwd"


def collect_snapshot(repo: Path, *, log_root_override: Path | None = None, events_limit: int = 100,
                    include_tombstones: bool = False) -> dict[str, Any]:
    paths = reviewable_unit_diff_tracker._lease_repo_paths(repo, log_root_override)
    repo_resolved, key, directory, db_path, events_path, common_dir = paths
    snapshot: dict[str, Any] = {
        "generated_at": _utc_now_iso(),
        "repo": {
            "repo_key": key,
            "repo_path": str(repo_resolved),
            "git_common_dir": str(common_dir),
            "tracker_dir": str(directory),
            "db_path": str(db_path),
            "events_path": str(events_path),
            "db_exists": db_path.is_file(),
        },
        "meta": {},
        "branches": [],
        "worktrees": [],
        "sessions": [],
        "session_units": [],
        "units": [],
        "leases": [],
        "lease_units": [],
        "manual_runs": [],
        "lease_participants": [],
        "tombstones": [],
        "events": [],
        "counters": {},
    }
    if not db_path.is_file():
        snapshot["events"] = _read_events_tail(events_path, events_limit)
        snapshot["counters"] = _compute_counters(snapshot)
        return snapshot
    conn = reviewable_unit_diff_tracker._open_conn(db_path)
    try:
        snapshot["meta"] = _read_meta(conn)
        snapshot["branches"] = _fetch_all(conn, "SELECT * FROM branches ORDER BY last_seen_at DESC")
        snapshot["worktrees"] = _fetch_all(conn, "SELECT * FROM worktrees ORDER BY last_seen_at DESC")
        snapshot["sessions"] = _fetch_all(conn, "SELECT * FROM sessions ORDER BY last_seen_at DESC")
        snapshot["session_units"] = _fetch_all(conn, "SELECT * FROM session_units ORDER BY assigned_at DESC")
        snapshot["units"] = _fetch_all(conn, "SELECT * FROM units ORDER BY last_observed_at DESC")
        snapshot["leases"] = _fetch_all(conn, "SELECT * FROM leases ORDER BY created_at DESC")
        snapshot["lease_units"] = _fetch_all(conn, "SELECT lease_id, unit_id FROM lease_units")
        snapshot["manual_runs"] = _fetch_all(conn, "SELECT * FROM manual_rvf_runs ORDER BY completed_at DESC")
        try:
            snapshot["lease_participants"] = _fetch_all(
                conn,
                "SELECT lease_id, reviewer_id, run_id, state, owns_lease, "
                "joined_at, last_activity_at, finished_at, release_reason "
                "FROM lease_participants ORDER BY joined_at DESC",
            )
        except sqlite3.OperationalError as exc:
            # Only swallow the "missing table/column on a v2 DB" case.
            # Anything else (database locked, malformed, disk I/O error,
            # unexpected schema drift) must surface so it isn't silently
            # masked as an empty participants list.
            message = str(exc).lower()
            if message.startswith("no such table") or message.startswith("no such column"):
                snapshot["lease_participants"] = []
            else:
                raise
        if include_tombstones:
            snapshot["tombstones"] = _fetch_all(conn, "SELECT * FROM tombstones ORDER BY retired_at DESC")
    finally:
        conn.close()
    _enrich_sessions_with_codex_meta(snapshot["sessions"], cache_dir=directory)
    snapshot["events"] = _read_events_tail(events_path, events_limit)
    snapshot["counters"] = _compute_counters(snapshot)
    return snapshot


SHELL_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>RVF tracker — __REPO_KEY__</title>
<style>
:root {
  color-scheme: dark light;
  --bg: #0f1115;
  --bg-2: #181b22;
  --panel: #1c1f27;
  --panel-2: #232732;
  --border: #2a2f3a;
  --text: #e8ecf1;
  --muted: #8a93a6;
  --accent: #6fb1fc;
  --ok: #4ade80;
  --warn: #fbbf24;
  --danger: #f87171;
  --neutral: #94a3b8;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  background: var(--bg); color: var(--text);
  padding: 12px 16px 32px;
}
.shell { max-width: 1500px; margin: 0 auto; }
header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; }
header h1 { font-size: 16px; margin: 0; font-weight: 600; }
header .path { font-size: 12px; color: var(--muted); }
header .ts { font-size: 11px; color: var(--muted); display: flex; gap: 16px; align-items: center; }
header .ts #live-text { display: inline-block; min-width: 16ch; text-align: left; font-variant-numeric: tabular-nums; }
header .ts #last-update { display: inline-block; min-width: 18ch; text-align: left; font-variant-numeric: tabular-nums; }
header .ts .live { display: inline-flex; align-items: center; gap: 6px; }
header .ts .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ok); animation: pulse 1.5s infinite; }
@keyframes pulse { 0%, 100% { opacity: 1 } 50% { opacity: 0.4 } }
header .ts.error .dot { background: var(--danger); animation: none; }
header .ts.frozen .dot { background: var(--accent); animation: none; }
header .ts .badge { background: rgba(111, 177, 252, 0.2); color: var(--accent); border: 1px solid rgba(111, 177, 252, 0.4); padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; letter-spacing: 0.5px; }
header button { background: var(--bg-2); color: var(--text); border: 1px solid var(--border); border-radius: 4px; padding: 4px 10px; font: inherit; cursor: pointer; }
header button:hover { background: var(--panel); }
header button.frozen { background: rgba(111, 177, 252, 0.2); border-color: var(--accent); color: var(--accent); }

.kpi-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 6px; margin-bottom: 12px; }
.kpi { background: var(--panel); border: 1px solid var(--border); border-radius: 4px; padding: 8px 10px; }
.kpi .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; }
.kpi .value { font-size: 20px; font-weight: 600; line-height: 1.2; }
.kpi.split .value { font-size: 12px; font-weight: 500; display: flex; gap: 6px; flex-wrap: wrap; line-height: 1.4; }

.tag { display: inline-block; padding: 1px 5px; border-radius: 3px; font-size: 11px; font-weight: 600; background: var(--bg-2); border: 1px solid var(--border); color: var(--text); white-space: nowrap; }
.tag.ok { color: var(--ok); border-color: rgba(74, 222, 128, 0.4); }
.tag.warn { color: var(--warn); border-color: rgba(251, 191, 36, 0.4); }
.tag.danger { color: var(--danger); border-color: rgba(248, 113, 113, 0.4); }
.tag.neutral { color: var(--neutral); border-color: var(--border); }

section { margin-top: 14px; }
section h2 { font-size: 13px; font-weight: 600; color: var(--accent); margin: 0 0 6px; padding-bottom: 4px; border-bottom: 1px solid var(--border); }
section h2 .count { color: var(--muted); font-weight: 400; font-size: 11px; margin-left: 6px; }
.section-title-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 0 0 6px; padding-bottom: 4px; border-bottom: 1px solid var(--border); }
.section-title-row h2 { margin: 0; padding: 0; border: 0; }
.panel > .section-title-row { margin: 0; padding: 6px 10px; background: var(--bg-2); border-bottom: 1px solid var(--border); }
.unit-controls { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 11px; font-weight: 500; }
.segmented { display: inline-flex; border: 1px solid var(--border); border-radius: 4px; overflow: hidden; background: var(--bg-2); }
.segmented button { appearance: none; border: 0; border-left: 1px solid var(--border); background: transparent; color: var(--muted); padding: 2px 7px; font: inherit; cursor: pointer; }
.segmented button:first-child { border-left: 0; }
.segmented button.active { background: rgba(111, 177, 252, 0.18); color: var(--accent); }
.segmented button:hover { background: var(--panel-2); color: var(--text); }

.row-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }
@media (max-width: 1180px) { .row-3 { grid-template-columns: 1fr 1fr; } }
@media (max-width: 800px) { .row-3 { grid-template-columns: 1fr; } }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 4px; overflow: hidden; }
.panel > h2 { margin: 0; padding: 6px 10px; background: var(--bg-2); font-size: 12px; border-bottom: 1px solid var(--border); border-radius: 0; }

table { border-collapse: collapse; width: 100%; table-layout: fixed; font-size: 13px; }
th, td { padding: 4px 8px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--border); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
th { background: var(--bg-2); color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px; padding: 4px 8px; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(111, 177, 252, 0.05); }
td.mono, .mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 12px; }
td.wrap { white-space: normal; word-break: break-all; }
td.num { text-align: right; }
.empty { color: var(--muted); font-style: italic; padding: 8px 12px; }
.sess-name { font-size: 13px; line-height: 1.25; }
.sess-id { font-size: 11px; color: var(--muted); line-height: 1.2; }
.muted { color: var(--muted); font-weight: normal; }
.src-tag { display: inline-block; font-size: 9px; padding: 0 4px; border-radius: 2px; margin-right: 6px; vertical-align: middle; text-transform: uppercase; letter-spacing: 0.3px; }
.src-app { background: rgba(120, 220, 150, 0.18); color: #98e0b0; }
.src-prompt { background: rgba(220, 200, 120, 0.18); color: #d8c088; }
.src-cwd { background: rgba(150, 150, 170, 0.18); color: var(--muted); }
/* Truncate visually but keep full text in DOM so triple-click / drag selection captures it. */
.id-trunc { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.path-trunc { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cell-2row { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.cell-2row > * { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.table-scroll { overflow-x: auto; }
.sessions-scroll {
  --sessions-header-height: 27px;
  --sessions-row-height: 40px;
  max-height: calc(var(--sessions-header-height) + (var(--sessions-row-height) * 10));
  overflow-y: auto;
}
.sessions-scroll table.t-sessions thead th { position: sticky; top: 0; z-index: 1; }
.sessions-scroll table.t-sessions tbody tr { height: var(--sessions-row-height); }
.owners-list { display: flex; flex-direction: column; gap: 6px; }
.ownr { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.ownr > * { min-width: 0; }
.ownr-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ownr-id { font-size: 10px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kind-pill { display: inline-block; font-size: 9px; padding: 0 4px; border-radius: 2px; vertical-align: middle; text-transform: uppercase; letter-spacing: 0.3px; }
.kind-owned { background: rgba(120, 220, 150, 0.16); color: #98e0b0; }
.kind-takeover { background: rgba(220, 180, 120, 0.16); color: #e6c282; }
.kind-transferred { background: rgba(150, 150, 170, 0.16); color: var(--muted); }
.kind-reviewer { background: rgba(120, 180, 220, 0.16); color: #9ec3e6; }
.kind-validate-fix { background: rgba(180, 150, 220, 0.16); color: #c0a8e0; }
.kind-manual { background: rgba(220, 150, 120, 0.18); color: #e0b098; }

/* Per-section column widths. */
table.t-sessions th:nth-child(1), table.t-sessions td:nth-child(1) { width: auto; }
table.t-sessions th:nth-child(2), table.t-sessions td:nth-child(2) { width: 70px; text-align: right; }
table.t-sessions th:nth-child(3), table.t-sessions td:nth-child(3) { width: 110px; }
table.t-sessions th:nth-child(4), table.t-sessions td:nth-child(4) { width: 80px; }

table.t-leases th:nth-child(1), table.t-leases td:nth-child(1) { width: 30%; }
table.t-leases th:nth-child(2), table.t-leases td:nth-child(2) { width: auto; }
table.t-leases th:nth-child(3), table.t-leases td:nth-child(3) { width: 90px; }
table.t-leases th:nth-child(4), table.t-leases td:nth-child(4) { width: 50px; text-align: right; }
table.t-leases th:nth-child(5), table.t-leases td:nth-child(5) { width: 22%; }
table.t-leases th:nth-child(6), table.t-leases td:nth-child(6) { width: 100px; }
table.t-leases td { vertical-align: top; }
.parts-list { display: flex; flex-wrap: wrap; gap: 4px; }

table.t-manual th:nth-child(1), table.t-manual td:nth-child(1) { width: 28%; }
table.t-manual th:nth-child(2), table.t-manual td:nth-child(2) { width: 28%; }
table.t-manual th:nth-child(3), table.t-manual td:nth-child(3) { width: auto; }
table.t-manual th:nth-child(4), table.t-manual td:nth-child(4) { width: 90px; }

table.t-units th:nth-child(1), table.t-units td:nth-child(1) { width: 110px; }
table.t-units th:nth-child(2), table.t-units td:nth-child(2) { width: auto; }
table.t-units th:nth-child(3), table.t-units td:nth-child(3) { width: 180px; }
table.t-units th:nth-child(4), table.t-units td:nth-child(4) { width: 110px; }
table.t-units th:nth-child(5), table.t-units td:nth-child(5) { width: 90px; }
table.t-units th:nth-child(6), table.t-units td:nth-child(6) { width: 90px; }
table.t-units th:nth-child(7), table.t-units td:nth-child(7) { width: 105px; }
table.t-units th:nth-child(8), table.t-units td:nth-child(8) { width: 22%; }
table.t-units th:nth-child(9), table.t-units td:nth-child(9) { width: 120px; }
table.t-units th:nth-child(10), table.t-units td:nth-child(10) { width: 90px; }
table.t-units td { white-space: normal; vertical-align: top; }
table.t-units td:nth-child(1), table.t-units td:nth-child(2) { white-space: nowrap; }

table.t-events th:nth-child(1), table.t-events td:nth-child(1) { width: 180px; }
table.t-events th:nth-child(2), table.t-events td:nth-child(2) { width: 180px; }
table.t-events th:nth-child(3), table.t-events td:nth-child(3) { width: auto; }

.banner { padding: 10px 12px; background: var(--panel); border: 1px solid var(--border); border-radius: 4px; margin-bottom: 12px; font-size: 12px; color: var(--muted); }
.banner code { background: var(--bg-2); padding: 1px 4px; border-radius: 2px; font-size: 11px; }
</style>
</head><body>
<div class="shell">
  <header>
    <div>
      <h1>RVF tracker — <span class="mono" id="repo-key">__REPO_KEY__</span></h1>
      <div class="path mono" id="repo-path">__REPO_PATH__</div>
    </div>
    <div class="ts" id="status">
      <span class="badge" id="mode-badge" hidden>FROZEN</span>
      <span class="live"><span class="dot"></span><span id="live-text">connecting…</span></span>
      <span id="last-update">—</span>
      <button id="freeze-btn" type="button">freeze</button>
      <button id="download-btn" type="button">download json</button>
    </div>
  </header>
  <div id="banner-empty" class="banner" hidden>no <code>tracker.sqlite3</code> for this repo yet — no claims registered.</div>
  <div class="kpi-strip" id="kpi-strip"></div>
  <div class="row-3">
    <section class="panel"><h2>Sessions <span class="count" id="count-sessions"></span></h2><div id="sessions-body"></div></section>
    <section class="panel"><h2>Active leases <span class="count" id="count-leases"></span></h2><div id="leases-body"></div></section>
    <section class="panel"><h2>Manual RVF runs <span class="count" id="count-manual"></span></h2><div id="manual-body"></div></section>
  </div>
  <section class="panel">
    <div class="section-title-row">
      <h2>Units <span class="count" id="count-units"></span></h2>
      <div class="unit-controls">
        <span>superseded</span>
        <span class="segmented" id="superseded-mode">
          <button type="button" data-mode="time">time</button>
          <button type="button" data-mode="path">path</button>
          <button type="button" data-mode="hidden">hidden</button>
          <button type="button" data-mode="expanded">expanded</button>
        </span>
      </div>
    </div>
    <div id="units-body"></div>
  </section>
  <section class="panel"><h2>Recent events <span class="count" id="count-events"></span></h2><div id="events-body"></div></section>
</div>
<script>
const POLL_MS = __POLL_MS__;
const REDRAW_MS = 1000;
const SELECTION_RENDER_GRACE_MS = 60 * 1000;
const $ = (id) => document.getElementById(id);
let frozen = false;
let serverFrozen = false;
let lastSnapshot = null;
let lastFetchedAt = null;
let lastError = null;
let frozenNow = null;
let selectionRenderDeferredSince = null;
function readStoredSupersededMode() {
  try { return localStorage.getItem('rvf-tracker-superseded-mode') || 'time'; }
  catch (_) { return 'time'; }
}
function writeStoredSupersededMode(mode) {
  try { localStorage.setItem('rvf-tracker-superseded-mode', mode); }
  catch (_) {}
}
let supersededMode = readStoredSupersededMode();
if (supersededMode === 'grouped') supersededMode = 'time';
if (!['time', 'path', 'hidden', 'expanded'].includes(supersededMode)) supersededMode = 'time';
const SUPERSEDED_TIME_GAP_MS = 10 * 60 * 1000;

function effectiveNow() {
  if (serverFrozen && lastSnapshot && lastSnapshot.generated_at) {
    const t = new Date(lastSnapshot.generated_at).getTime();
    if (!isNaN(t)) return t;
  }
  if (frozen && frozenNow != null) return frozenNow;
  return Date.now();
}

function esc(s) {
  if (s === null || s === undefined) return '-';
  return String(s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtAge(iso, now) {
  if (!iso) return '-';
  const t = new Date(iso).getTime();
  if (isNaN(t)) return '-';
  const sec = Math.floor((now - t) / 1000);
  if (sec < 0) return 'in ' + fmtDur(-sec);
  return fmtDur(sec) + ' ago';
}
function fmtEta(iso, now) {
  if (!iso) return ['-', 'neutral'];
  const t = new Date(iso).getTime();
  if (isNaN(t)) return ['-', 'neutral'];
  const sec = Math.floor((t - now) / 1000);
  if (sec <= 0) return ['expired ' + fmtDur(-sec) + ' ago', 'danger'];
  if (sec < 60) return ['in ' + sec + 's', 'warn'];
  return ['in ' + fmtDur(sec), 'ok'];
}
function fmtDur(sec) {
  if (sec < 60) return sec + 's';
  if (sec < 3600) return Math.floor(sec/60) + 'm' + String(sec%60).padStart(2, '0') + 's';
  if (sec < 86400) return Math.floor(sec/3600) + 'h' + String(Math.floor((sec%3600)/60)).padStart(2, '0') + 'm';
  return Math.floor(sec/86400) + 'd' + String(Math.floor((sec%86400)/3600)).padStart(2, '0') + 'h';
}
function latestIso(a, b) {
  if (!a) return b || '';
  if (!b) return a || '';
  const at = new Date(a).getTime();
  const bt = new Date(b).getTime();
  if (isNaN(at)) return b;
  if (isNaN(bt)) return a;
  return bt > at ? b : a;
}
function shortHash(s, n) {
  if (!s) return '-';
  const head = String(s).split(':').pop();
  return head.slice(0, n || 12);
}
function tag(text, kind) { return '<span class="tag ' + (kind || 'neutral') + '">' + esc(text) + '</span>'; }
function kindPill(kind) {
  if (!kind) return '';
  return `<span class="kind-pill kind-${esc(kind)}">${esc(kind)}</span>`;
}
function fullIdSpan(id, extraClass) {
  if (!id) return '<span class="muted">-</span>';
  return `<span class="id-trunc mono${extraClass ? ' ' + extraClass : ''}" title="${esc(id)}">${esc(id)}</span>`;
}
function sessionRefCell(sessionsById, sessionId, opts) {
  opts = opts || {};
  if (!sessionId) return '<span class="muted">-</span>';
  const s = sessionsById && sessionsById[sessionId];
  const titleParts = [sessionId];
  if (s && s.origin_cwd) titleParts.push('cwd=' + s.origin_cwd);
  if (s && s.origin_branch) titleParts.push('branch=' + s.origin_branch);
  if (s && s.originator) titleParts.push(s.originator);
  const titleAttr = ' title="' + esc(titleParts.join('\n')) + '"';
  const trailing = opts.trailing || '';
  if (s && s.display_name) {
    const srcMap = { 'app_server_thread_name': ['src-app', 'name'], 'first_user_prompt': ['src-prompt', 'prompt'], 'cwd': ['src-cwd', 'cwd'] };
    const srcInfo = srcMap[s.display_name_source] || ['', ''];
    const srcTag = srcInfo[1] ? `<span class="src-tag ${srcInfo[0]}">${srcInfo[1]}</span>` : '';
    return `<div class="ownr"${titleAttr}><div class="ownr-name">${srcTag}${esc(s.display_name)}${trailing}</div><div class="ownr-id mono"><span class="id-trunc">${esc(sessionId)}</span></div></div>`;
  }
  return `<div class="ownr"${titleAttr}><div class="ownr-name mono"><span class="id-trunc">${esc(sessionId)}</span>${trailing}</div></div>`;
}
function hasUserSelection() {
  const sel = window.getSelection && window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return false;
  return sel.toString().length > 0;
}
const REVIEW_KIND = { available: 'ok', assigned: 'warn', reviewed: 'neutral' };
const OBSERVED_KIND = { dirty: 'warn', committed: 'ok', superseded: 'neutral' };
const TOMBSTONE_KIND = { active: 'ok', tombstoned: 'danger' };
const LEASE_KIND = { active: 'ok', paused: 'warn', completed: 'neutral', 'stale-released': 'danger', 'failed-released': 'danger' };

function renderKpis(c) {
  const cells = [];
  cells.push(`<div class="kpi"><div class="label">units</div><div class="value">${c.units_total||0}</div></div>`);
  cells.push(`<div class="kpi"><div class="label">leases</div><div class="value">${c.leases_total||0}</div></div>`);
  if (c.lease_participants_total) {
    cells.push(`<div class="kpi"><div class="label">participants</div><div class="value">${c.lease_participants_active||0} / ${c.lease_participants_total||0}</div></div>`);
  }
  cells.push(`<div class="kpi"><div class="label">sessions</div><div class="value">${c.sessions_total||0}</div></div>`);
  cells.push(`<div class="kpi"><div class="label">manual runs</div><div class="value">${c.manual_runs_total||0}</div></div>`);
  cells.push(`<div class="kpi"><div class="label">branches</div><div class="value">${c.branches_total||0}</div></div>`);
  cells.push(`<div class="kpi"><div class="label">worktrees</div><div class="value">${c.worktrees_total||0}</div></div>`);
  function pillMap(label, mapping, kindMap) {
    const keys = Object.keys(mapping || {}).sort();
    if (!keys.length) return '';
    const inner = keys.map(k => tag(k + '=' + mapping[k], kindMap[k] || 'neutral')).join(' ');
    return `<div class="kpi split"><div class="label">${esc(label)}</div><div class="value">${inner}</div></div>`;
  }
  cells.push(pillMap('review state', c.review_state, REVIEW_KIND));
  cells.push(pillMap('observed state', c.observed_state, OBSERVED_KIND));
  cells.push(pillMap('unit lifecycle', c.tombstone_state, TOMBSTONE_KIND));
  cells.push(pillMap('lease state', c.lease_state, LEASE_KIND));
  $('kpi-strip').innerHTML = cells.join('');
}

function renderSessions(snap, now) {
  const sessions = snap.sessions || [];
  $('count-sessions').textContent = '(' + sessions.length + ')';
  if (!sessions.length) { $('sessions-body').innerHTML = '<div class="empty">none</div>'; return; }
  const counts = {};
  for (const su of (snap.session_units || [])) {
    const key = su.session_id + '|' + su.assignment_kind;
    counts[key] = (counts[key] || 0) + 1;
  }
  const kindBySession = {};
  for (const l of (snap.leases || [])) {
    if (!l.session_id || !l.holder_kind) continue;
    (kindBySession[l.session_id] = kindBySession[l.session_id] || new Set()).add(l.holder_kind);
  }
  const rows = sessions.map(s => {
    const owned = counts[s.session_id + '|owned'] || 0;
    const takeover = counts[s.session_id + '|takeover'] || 0;
    const transferred = counts[s.session_id + '|transferred'] || 0;
    const pills = [tag('o ' + owned, 'ok')];
    if (takeover) pills.push(tag('t ' + takeover, 'warn'));
    if (transferred) pills.push(tag('x ' + transferred, 'neutral'));
    const kindsHeld = kindBySession[s.session_id];
    let kindCell = '';
    if (kindsHeld) {
      const order = ['reviewer', 'validate-fix', 'manual'];
      const ordered = order.filter(k => kindsHeld.has(k)).concat(Array.from(kindsHeld).filter(k => !order.includes(k)));
      kindCell = ordered.map(k => tag(k, k === 'reviewer' ? 'neutral' : (k === 'manual' ? 'warn' : 'ok'))).join(' ');
    }
    const sid = s.session_id || '';
    const sidShort = sid.slice(0, 8) + (sid.length > 8 ? '…' : '');
    const titleParts = [sid];
    if (s.origin_cwd) titleParts.push('cwd=' + s.origin_cwd);
    if (s.origin_branch) titleParts.push('branch=' + s.origin_branch);
    if (s.originator) titleParts.push(s.originator);
    const title = titleParts.join('\n');
    let primary;
    if (s.display_name) {
      const branchSuffix = s.origin_branch ? ` <span class="muted">[${esc(s.origin_branch)}]</span>` : '';
      const srcMap = { 'app_server_thread_name': ['src-app', 'name'], 'first_user_prompt': ['src-prompt', 'prompt'], 'cwd': ['src-cwd', 'cwd'] };
      const srcInfo = srcMap[s.display_name_source] || ['', ''];
      const srcTag = srcInfo[1] ? `<span class="src-tag ${srcInfo[0]}">${srcInfo[1]}</span>` : '';
      primary = `<div class="cell-2row"><div class="sess-name">${srcTag}${esc(s.display_name)}${branchSuffix}</div><div class="sess-id mono"><span class="id-trunc">${esc(sid)}</span></div></div>`;
    } else {
      primary = `<div class="mono"><span class="id-trunc">${esc(sid)}</span></div>`;
    }
    return `<tr><td title="${esc(title)}">${primary}</td><td>${pills.join(' ')}</td><td>${kindCell}</td><td class="mono" title="${esc(s.last_seen_at)}">${esc(fmtAge(s.last_seen_at, now))}</td></tr>`;
  }).join('');
  $('sessions-body').innerHTML = `<div class="table-scroll sessions-scroll"><table class="t-sessions"><thead><tr><th>session_id</th><th>units</th><th>held lease</th><th>last seen</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderLeases(snap, now) {
  const active = (snap.leases || []).filter(l => l.state === 'active');
  $('count-leases').textContent = '(' + active.length + ' active · ' + ((snap.leases || []).length - active.length) + ' other)';
  if (!active.length) { $('leases-body').innerHTML = '<div class="empty">no active leases</div>'; return; }
  const sessionsById = {};
  for (const s of (snap.sessions || [])) sessionsById[s.session_id] = s;
  const luByLease = {};
  for (const lu of (snap.lease_units || [])) (luByLease[lu.lease_id] = luByLease[lu.lease_id] || []).push(lu);
  const partsByLease = {};
  for (const p of (snap.lease_participants || [])) (partsByLease[p.lease_id] = partsByLease[p.lease_id] || []).push(p);
  function partsCell(l) {
    const list = partsByLease[l.lease_id] || [];
    if (!list.length) return '<span class="muted">-</span>';
    const items = list.map(p => {
      const stateClass = p.state === 'active' ? 'kind-owned' : (p.state === 'completed' ? 'kind-reviewer' : 'kind-transferred');
      const owns = p.owns_lease ? ' ★' : '';
      const titleParts = [`reviewer=${p.reviewer_id}`, `run=${p.run_id}`, `state=${p.state}`, `joined=${p.joined_at}`];
      if (p.finished_at) titleParts.push(`finished=${p.finished_at}`);
      if (p.release_reason) titleParts.push(`reason=${p.release_reason}`);
      return `<span class="kind-pill ${stateClass}" title="${esc(titleParts.join('\n'))}">${esc(p.reviewer_id)}${owns}</span>`;
    }).join(' ');
    return `<div class="parts-list">${items}</div>`;
  }
  const rows = active.map(l => {
    const ucount = (luByLease[l.lease_id] || []).length;
    const [eta, kind] = fmtEta(l.expires_at, now);
    return `<tr>`
      + `<td>${fullIdSpan(l.lease_id)}</td>`
      + `<td>${sessionRefCell(sessionsById, l.session_id)}</td>`
      + `<td>${kindPill(l.holder_kind)}</td>`
      + `<td class="num">${ucount}</td>`
      + `<td>${partsCell(l)}</td>`
      + `<td>${tag(eta, kind)}</td>`
      + `</tr>`;
  }).join('');
  $('leases-body').innerHTML = `<table class="t-leases"><thead><tr><th>lease_id</th><th>session</th><th>kind</th><th>units</th><th>participants</th><th>expires</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderManual(snap, now) {
  const runs = snap.manual_runs || [];
  $('count-manual').textContent = '(' + runs.length + ')';
  if (!runs.length) { $('manual-body').innerHTML = '<div class="empty">none</div>'; return; }
  const sessionsById = {};
  for (const s of (snap.sessions || [])) sessionsById[s.session_id] = s;
  const rows = runs.map(r =>
    `<tr>`
    + `<td>${sessionRefCell(sessionsById, r.session_id)}</td>`
    + `<td>${fullIdSpan(r.run_id)}</td>`
    + `<td>${fullIdSpan(r.scope_hash)}</td>`
    + `<td class="mono" title="${esc(r.completed_at)}">${esc(fmtAge(r.completed_at, now))}</td>`
    + `</tr>`).join('');
  $('manual-body').innerHTML = `<table class="t-manual"><thead><tr><th>session</th><th>run</th><th>scope</th><th>completed</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderUnits(snap, now) {
  const units = snap.units || [];
  const supersededCount = units.filter(u => u.observed_state === 'superseded').length;
  $('count-units').textContent = '(' + units.length + (supersededCount ? ' · ' + supersededCount + ' superseded ' + supersededMode : '') + ')';
  for (const btn of document.querySelectorAll('#superseded-mode button')) {
    btn.classList.toggle('active', btn.dataset.mode === supersededMode);
  }
  if (!units.length) { $('units-body').innerHTML = '<div class="empty">none</div>'; return; }
  const sessionsById = {};
  for (const s of (snap.sessions || [])) sessionsById[s.session_id] = s;
  const worktreesByKey = {};
  for (const w of (snap.worktrees || [])) worktreesByKey[w.worktree_key] = w;
  const branchesByKey = {};
  for (const b of (snap.branches || [])) branchesByKey[b.branch_key] = b;
  const ownersByUnit = {};
  for (const su of (snap.session_units || [])) (ownersByUnit[su.unit_id] = ownersByUnit[su.unit_id] || []).push(su);
  const activeLeasesById = {};
  for (const l of (snap.leases || [])) if (l.state === 'active') activeLeasesById[l.lease_id] = l;
  const holdingLeaseByUnit = {};
  for (const lu of (snap.lease_units || [])) {
    const l = activeLeasesById[lu.lease_id];
    if (l) holdingLeaseByUnit[lu.unit_id] = l;
  }
  const activePartsByLease = {};
  for (const p of (snap.lease_participants || [])) {
    if (p.state !== 'active') continue;
    (activePartsByLease[p.lease_id] = activePartsByLease[p.lease_id] || []).push(p);
  }
  function locCell(u) {
    const wt = worktreesByKey[u.worktree_key];
    const br = branchesByKey[u.branch_key];
    const wtLabel = wt && wt.worktree_path ? wt.worktree_path.split('/').filter(Boolean).slice(-2).join('/') : (u.worktree_key || '-');
    const brLabel = br && br.refname ? br.refname : (u.branch_key ? '(unknown branch_key)' : '<no branch>');
    const wtTitle = wt && wt.worktree_path ? wt.worktree_path : (u.worktree_key || '');
    const brTitle = br && br.refname ? br.refname : '';
    return `<div class="cell-2row"><div title="${esc(wtTitle)}">${esc(wtLabel)}</div><div class="muted" style="font-size:11px" title="${esc(brTitle)}">${esc(brLabel)}</div></div>`;
  }
  function ownersCell(u) {
    const list = ownersByUnit[u.unit_id] || [];
    if (!list.length) return '<span class="muted">-</span>';
    return '<div class="owners-list">' + list.map(su => {
      const trailing = ` ${kindPill(su.assignment_kind)}`;
      return sessionRefCell(sessionsById, su.session_id, { trailing });
    }).join('') + '</div>';
  }
  function leaseCell(u) {
    const l = holdingLeaseByUnit[u.unit_id];
    if (!l) return '<span class="muted">-</span>';
    const [eta, etaKind] = fmtEta(l.expires_at, now);
    const parts = activePartsByLease[l.lease_id] || [];
    const sharedTag = parts.length > 1 ? ` <span class="kind-pill kind-takeover" title="${esc(parts.map(p => p.reviewer_id + (p.owns_lease ? ' (owner)' : '')).join('\n'))}">+${parts.length - 1}</span>` : '';
    const trailing = ` ${kindPill(l.holder_kind)} ${tag(eta, etaKind)}${sharedTag}`;
    return sessionRefCell(sessionsById, l.session_id, { trailing });
  }
  function groupPathsCell(group) {
    const paths = Array.from(new Set(group.units.map(u => u.path).filter(Boolean))).sort();
    const title = paths.join('\n');
    if (!paths.length) return '<span class="muted">-</span>';
    if (group.groupKind === 'path') {
      const rep = group.rep;
      return `<span class="path-trunc" title="${esc(rep.path)}">${esc(rep.path)}</span>${rep.old_path ? `<div class="muted" style="font-size:11px" title="from ${esc(rep.old_path)}">← ${esc(rep.old_path)}</div>` : ''}`;
    }
    const preview = paths.slice(0, 3).map(p => `<div class="path-trunc" title="${esc(p)}">${esc(p)}</div>`).join('');
    const more = paths.length > 3 ? `<div class="muted" style="font-size:11px">+${paths.length - 3} more</div>` : '';
    return `<div title="${esc(title)}"><div>${tag(paths.length + ' paths', 'neutral')}</div>${preview}${more}</div>`;
  }
  function groupLocCell(group) {
    if (group.groupKind === 'path') return locCell(group.rep);
    const worktrees = new Set(group.units.map(u => u.worktree_key || '').filter(Boolean));
    const branches = new Set(group.units.map(u => u.branch_key || '').filter(Boolean));
    if (worktrees.size === 1 && branches.size <= 1) return locCell(group.rep);
    return `<div class="cell-2row"><div>${esc(worktrees.size || 0)} worktrees</div><div class="muted" style="font-size:11px">${esc(branches.size || 0)} branches</div></div>`;
  }
  function unitRow(u) {
    return `<tr>`
      + `<td>${fullIdSpan(u.unit_id)}</td>`
      + `<td><span class="path-trunc" title="${esc(u.path)}">${esc(u.path)}</span>${u.old_path ? `<div class="muted" style="font-size:11px" title="from ${esc(u.old_path)}">← ${esc(u.old_path)}</div>` : ''}</td>`
      + `<td>${locCell(u)}</td>`
      + `<td>${esc(u.kind)}</td>`
      + `<td>${tag(u.observed_state, OBSERVED_KIND[u.observed_state] || 'neutral')}</td>`
      + `<td>${tag(u.review_state, REVIEW_KIND[u.review_state] || 'neutral')}</td>`
      + `<td>${tag(u.is_tombstoned ? 'tombstoned' : 'active', u.is_tombstoned ? 'danger' : 'ok')}${u.tombstone_reason ? `<div class="muted" style="font-size:11px">${esc(u.tombstone_reason)}</div>` : ''}</td>`
      + `<td>${ownersCell(u)}</td>`
      + `<td>${leaseCell(u)}</td>`
      + `<td class="mono" title="${esc(u.last_observed_at)}">${esc(fmtAge(u.last_observed_at, now))}</td>`
      + `</tr>`;
  }
  function supersededGroupRow(group) {
    const rep = group.rep;
    const ids = group.units.map(u => u.unit_id).filter(Boolean);
    const kinds = {};
    const reviews = {};
    let tombstoned = 0;
    for (const u of group.units) {
      kinds[u.kind || 'unknown'] = (kinds[u.kind || 'unknown'] || 0) + 1;
      reviews[u.review_state || 'unknown'] = (reviews[u.review_state || 'unknown'] || 0) + 1;
      if (u.is_tombstoned) tombstoned += 1;
    }
    const kindText = Object.keys(kinds).sort().map(k => `${esc(k)} x${kinds[k]}`).join('<br>');
    const reviewCell = Object.keys(reviews).sort().map(k => tag(k + '=' + reviews[k], REVIEW_KIND[k] || 'neutral')).join(' ');
    const lifecycle = tombstoned
      ? tag('tombstoned=' + tombstoned, 'danger') + (tombstoned < group.units.length ? ' ' + tag('active=' + (group.units.length - tombstoned), 'ok') : '')
      : tag('active=' + group.units.length, 'ok');
    const title = ids.join('\n');
    const label = group.groupKind === 'time'
      ? `${group.units.length} superseded · ${esc(fmtAge(group.last_observed_at, now))} last-observed burst`
      : `${group.units.length} superseded`;
    return `<tr>`
      + `<td><span class="id-trunc mono" title="${esc(title)}">${label}</span></td>`
      + `<td>${groupPathsCell(group)}</td>`
      + `<td>${groupLocCell(group)}</td>`
      + `<td>${kindText}</td>`
      + `<td>${tag('superseded', 'neutral')}</td>`
      + `<td>${reviewCell}</td>`
      + `<td>${lifecycle}</td>`
      + `<td>${ownersCell(rep)}</td>`
      + `<td>${leaseCell(rep)}</td>`
      + `<td class="mono" title="${esc(group.last_observed_at)}">${esc(fmtAge(group.last_observed_at, now))}</td>`
      + `</tr>`;
  }
  function displayItem(html, lastObservedAt, ordinal) {
    return { html, lastObservedAt: lastObservedAt || '', ordinal };
  }
  function displaySort(a, b) {
    const at = new Date(a.lastObservedAt).getTime();
    const bt = new Date(b.lastObservedAt).getTime();
    if (!isNaN(at) && !isNaN(bt) && bt !== at) return bt - at;
    if (isNaN(at) && !isNaN(bt)) return 1;
    if (!isNaN(at) && isNaN(bt)) return -1;
    return a.ordinal - b.ordinal;
  }
  function supersededPathGroups(superseded) {
    const groups = {};
    let groupOrdinal = 0;
    for (const u of superseded) {
      const key = [u.worktree_key || '', u.branch_key || '', u.path || '', u.old_path || ''].join('\u0000');
      const existing = groups[key];
      if (existing) {
        existing.units.push(u);
        existing.last_observed_at = latestIso(existing.last_observed_at, u.last_observed_at);
      } else {
        groups[key] = { groupKind: 'path', rep: u, units: [u], last_observed_at: u.last_observed_at || '', ordinal: groupOrdinal++ };
      }
    }
    return Object.values(groups);
  }
  function supersededTimeGroups(superseded) {
    const sorted = superseded.slice().sort((a, b) => {
      const at = new Date(a.last_observed_at || '').getTime();
      const bt = new Date(b.last_observed_at || '').getTime();
      if (!isNaN(at) && !isNaN(bt) && bt !== at) return bt - at;
      if (isNaN(at) && !isNaN(bt)) return 1;
      if (!isNaN(at) && isNaN(bt)) return -1;
      return String(a.path || '').localeCompare(String(b.path || ''));
    });
    const groups = [];
    for (const u of sorted) {
      const t = new Date(u.last_observed_at || '').getTime();
      const last = groups[groups.length - 1];
      const lastT = last ? new Date(last.oldest_observed_at || '').getTime() : NaN;
      if (last && !isNaN(t) && !isNaN(lastT) && Math.abs(lastT - t) <= SUPERSEDED_TIME_GAP_MS) {
        last.units.push(u);
        last.oldest_observed_at = u.last_observed_at || last.oldest_observed_at;
        last.last_observed_at = latestIso(last.last_observed_at, u.last_observed_at);
      } else {
        groups.push({
          groupKind: 'time',
          rep: u,
          units: [u],
          last_observed_at: u.last_observed_at || '',
          oldest_observed_at: u.last_observed_at || '',
          ordinal: groups.length,
        });
      }
    }
    return groups;
  }
  const displayRows = [];
  let ordinal = 0;
  if (supersededMode === 'expanded') {
    for (const u of units) displayRows.push(displayItem(unitRow(u), u.last_observed_at, ordinal++));
  } else {
    const superseded = [];
    for (const u of units) {
      if (u.observed_state !== 'superseded') {
        displayRows.push(displayItem(unitRow(u), u.last_observed_at, ordinal++));
        continue;
      }
      if (supersededMode === 'hidden') continue;
      superseded.push(u);
    }
    const groups = supersededMode === 'path' ? supersededPathGroups(superseded) : supersededTimeGroups(superseded);
    for (const group of groups) {
      displayRows.push(displayItem(supersededGroupRow(group), group.last_observed_at, ordinal++));
    }
  }
  const rows = displayRows.sort(displaySort).map(item => item.html).join('');
  if (!rows) { $('units-body').innerHTML = '<div class="empty">none in current superseded mode</div>'; return; }
  $('units-body').innerHTML = `<table class="t-units"><thead><tr><th>unit_id</th><th>path</th><th>worktree / branch</th><th>kind</th><th>observed</th><th>review</th><th>lifecycle</th><th>session owners</th><th>active lease</th><th>last obs</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderEvents(snap) {
  const events = snap.events || [];
  $('count-events').textContent = '(' + events.length + ')';
  if (!events.length) { $('events-body').innerHTML = '<div class="empty">none</div>'; return; }
  const rows = events.map(e => {
    const ts = e.ts || e.timestamp || '';
    const evt = e.event || '-';
    const fields = {};
    for (const k of Object.keys(e)) if (k !== 'event' && k !== 'ts' && k !== 'timestamp') fields[k] = e[k];
    const text = JSON.stringify(fields);
    return `<tr><td class="mono">${esc(ts)}</td><td class="mono">${esc(evt)}</td><td class="mono wrap" title="${esc(text)}">${esc(text)}</td></tr>`;
  }).join('');
  $('events-body').innerHTML = `<table class="t-events"><thead><tr><th>ts</th><th>event</th><th>fields</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderAll(snap) {
  const now = effectiveNow();
  document.title = 'RVF tracker — ' + (snap.repo && snap.repo.repo_key || '?');
  $('repo-key').textContent = snap.repo && snap.repo.repo_key || '?';
  $('repo-path').textContent = snap.repo && snap.repo.repo_path || '';
  const exists = snap.repo && snap.repo.db_exists;
  $('banner-empty').hidden = exists;
  renderKpis(snap.counters || {});
  renderSessions(snap, now);
  renderLeases(snap, now);
  renderManual(snap, now);
  renderUnits(snap, now);
  renderEvents(snap);
}

async function tick() {
  if (frozen || serverFrozen) return;
  try {
    const r = await fetch('/api/snapshot', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    lastSnapshot = data;
    lastFetchedAt = Date.now();
    lastError = null;
    serverFrozen = data._dashboard_mode === 'frozen';
    redraw();
  } catch (e) {
    lastError = e;
    $('status').classList.add('error');
    $('live-text').textContent = 'error';
    $('last-update').textContent = String(e);
  }
}
function redraw() {
  if (!lastSnapshot) return;
  if (lastError) return;
  $('status').classList.remove('error');
  if (serverFrozen) {
    $('status').classList.add('frozen');
    $('mode-badge').hidden = false;
    $('live-text').textContent = 'frozen';
    $('last-update').textContent = 'snapshot ' + (lastSnapshot.generated_at || '').replace('T', ' ').replace('Z', '');
    $('freeze-btn').disabled = true;
    $('freeze-btn').classList.add('frozen');
  } else if (frozen) {
    $('status').classList.remove('frozen');
    $('mode-badge').hidden = true;
    $('live-text').textContent = 'frozen (client)';
    if (frozenNow != null && lastFetchedAt != null) {
      const ago = Math.max(0, Math.floor((frozenNow - lastFetchedAt) / 1000));
      $('last-update').textContent = 'frozen at +' + ago + 's';
    }
  } else {
    $('status').classList.remove('frozen');
    $('mode-badge').hidden = true;
    $('live-text').textContent = 'live · poll ' + Math.round(POLL_MS/1000) + 's';
    if (lastFetchedAt) {
      const ago = Math.floor((Date.now() - lastFetchedAt) / 1000);
      $('last-update').textContent = ago <= 0 ? 'fetched just now' : ('fetched ' + ago + 's ago');
    }
  }
  if (hasUserSelection()) {
    if (selectionRenderDeferredSince == null) selectionRenderDeferredSince = Date.now();
    if (Date.now() - selectionRenderDeferredSince < SELECTION_RENDER_GRACE_MS) return;
    selectionRenderDeferredSince = null;
  } else {
    selectionRenderDeferredSince = null;
  }
  renderAll(lastSnapshot);
}
$('freeze-btn').addEventListener('click', () => {
  if (serverFrozen) return;
  frozen = !frozen;
  $('freeze-btn').textContent = frozen ? 'resume' : 'freeze';
  $('freeze-btn').classList.toggle('frozen', frozen);
  if (frozen) {
    frozenNow = Date.now();
  } else {
    frozenNow = null;
  }
  redraw();
  if (!frozen) tick();
});
$('download-btn').addEventListener('click', () => {
  if (!lastSnapshot) return;
  const blob = new Blob([JSON.stringify(lastSnapshot, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const ts = (lastSnapshot.generated_at || new Date().toISOString()).replace(/[:.]/g, '-');
  const repoKey = (lastSnapshot.repo && lastSnapshot.repo.repo_key) || 'snapshot';
  a.href = url; a.download = `tracker-${repoKey}-${ts}.json`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
for (const btn of document.querySelectorAll('#superseded-mode button')) {
  btn.addEventListener('click', () => {
    const mode = btn.dataset.mode || 'time';
    if (!['time', 'path', 'hidden', 'expanded'].includes(mode)) return;
    supersededMode = mode;
    writeStoredSupersededMode(mode);
    redraw();
  });
}
tick();
setInterval(tick, POLL_MS);
setInterval(redraw, REDRAW_MS);
</script>
</body></html>
"""


def render_shell(repo_key: str, repo_path: str, *, poll_seconds: int) -> str:
    poll_ms = max(int(poll_seconds * 1000), 250)
    return (
        SHELL_HTML
        .replace("__REPO_KEY__", repo_key)
        .replace("__REPO_PATH__", repo_path)
        .replace("__POLL_MS__", str(poll_ms))
    )


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "RVFTrackerDashboard/1.0"
    snapshot_loader = staticmethod(lambda: {"error": "no loader bound"})
    repo_label: str = ""
    repo_key_label: str = ""
    is_frozen: bool = False
    poll_seconds: int = 2

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = render_shell(
                self.repo_key_label,
                self.repo_label,
                poll_seconds=self.poll_seconds,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/snapshot":
            try:
                snap = self.snapshot_loader()
                if isinstance(snap, dict):
                    snap.setdefault("_dashboard_mode", "frozen" if self.is_frozen else "live")
                body = json.dumps(snap, ensure_ascii=False, default=str).encode("utf-8")
                code = 200
            except Exception as exc:
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                code = 500
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"not found")

    def log_message(self, fmt: str, *args: Any) -> None:
        return


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _live_loader(repo: Path, *, log_root_override: Path | None, events_limit: int,
                 include_tombstones: bool):
    def load() -> dict[str, Any]:
        return collect_snapshot(
            repo,
            log_root_override=log_root_override,
            events_limit=events_limit,
            include_tombstones=include_tombstones,
        )
    return load


def _frozen_loader(snapshot_path: Path):
    resolved = snapshot_path.expanduser().resolve()
    def load() -> dict[str, Any]:
        return json.loads(resolved.read_text(encoding="utf-8"))
    return load


def serve(*, snapshot_loader, repo_label: str, repo_key_label: str,
          host: str = "127.0.0.1", port: int = 8765, poll_seconds: int = 2,
          is_frozen: bool = False, open_browser: bool = True) -> _ThreadingServer:
    handler = type(
        "_BoundHandler",
        (_Handler,),
        {
            "snapshot_loader": staticmethod(snapshot_loader),
            "repo_label": repo_label,
            "repo_key_label": repo_key_label,
            "is_frozen": is_frozen,
            "poll_seconds": poll_seconds,
        },
    )
    server = _ThreadingServer((host, port), handler)
    actual_host, actual_port = server.server_address[:2]
    url = f"http://{actual_host}:{actual_port}/"
    mode = "frozen" if is_frozen else f"live · poll {poll_seconds}s"
    print(f"RVF tracker dashboard serving at {url} ({mode})")
    print(f"  source: {repo_label}")
    print(f"  ctrl-c to stop")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return server


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracker_dashboard",
        description="Live web dashboard for the global reviewed-diff tracker.",
    )
    parser.add_argument("--repo", default=None,
                        help="Path to the target repo / worktree (live mode).")
    parser.add_argument("--from-snapshot", default=None,
                        help="Serve a frozen view from a snapshot JSON file generated by --snapshot-json.")
    parser.add_argument("--snapshot-json", default=None,
                        help="One-shot: write snapshot JSON to this path and exit (no server). Pair with --repo.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--poll-seconds", type=int, default=2, help="Client poll interval in live mode (default 2).")
    parser.add_argument("--limit-events", type=int, default=100, help="Tail size for events.jsonl (default 100).")
    parser.add_argument("--include-tombstones", action="store_true")
    parser.add_argument("--no-open", action="store_true", help="Do not auto-open browser.")
    parser.add_argument("--log-root", default=None, help="Override CODEX_RVF_LOG_ROOT.")
    return parser


def _main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)
    log_root_override = Path(args.log_root).expanduser().resolve() if args.log_root else None

    if args.snapshot_json:
        if not args.repo:
            print("--snapshot-json requires --repo", file=sys.stderr)
            return 2
        repo = Path(args.repo).expanduser().resolve()
        snap = collect_snapshot(
            repo,
            log_root_override=log_root_override,
            events_limit=args.limit_events,
            include_tombstones=args.include_tombstones,
        )
        Path(args.snapshot_json).expanduser().write_text(
            json.dumps(snap, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return 0

    if args.from_snapshot:
        snapshot_path = Path(args.from_snapshot).expanduser().resolve()
        if not snapshot_path.is_file():
            print(f"snapshot file not found: {snapshot_path}", file=sys.stderr)
            return 2
        loader = _frozen_loader(snapshot_path)
        try:
            initial = loader()
        except Exception as exc:
            print(f"failed to read snapshot: {exc}", file=sys.stderr)
            return 2
        repo_info = initial.get("repo", {}) if isinstance(initial, dict) else {}
        repo_label = str(snapshot_path)
        repo_key = repo_info.get("repo_key") or snapshot_path.stem
        server = serve(
            snapshot_loader=loader,
            repo_label=repo_label,
            repo_key_label=str(repo_key),
            host=args.host,
            port=args.port,
            poll_seconds=args.poll_seconds,
            is_frozen=True,
            open_browser=not args.no_open,
        )
    else:
        if not args.repo:
            print("--repo or --from-snapshot is required", file=sys.stderr)
            return 2
        repo = Path(args.repo).expanduser().resolve()
        try:
            paths = reviewable_unit_diff_tracker._lease_repo_paths(repo, log_root_override)
            repo_key = paths[1]
        except Exception as exc:
            repo_key = f"<error: {exc}>"
        loader = _live_loader(
            repo,
            log_root_override=log_root_override,
            events_limit=args.limit_events,
            include_tombstones=args.include_tombstones,
        )
        server = serve(
            snapshot_loader=loader,
            repo_label=str(repo),
            repo_key_label=str(repo_key),
            host=args.host,
            port=args.port,
            poll_seconds=args.poll_seconds,
            is_frozen=False,
            open_browser=not args.no_open,
        )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
