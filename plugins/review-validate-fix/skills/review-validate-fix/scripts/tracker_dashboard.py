#!/usr/bin/env python3
"""Live web dashboard for the global reviewed-diff tracker.

Run as a local HTTP server; the page polls a JSON snapshot endpoint and
re-renders client-side. JSON-only consumers can hit /api/snapshot directly.
"""
from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import diff_tracker  # noqa: E402


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
    for unit in units:
        review_states[unit["review_state"]] = review_states.get(unit["review_state"], 0) + 1
        observed_states[unit["observed_state"]] = observed_states.get(unit["observed_state"], 0) + 1
    lease_states: dict[str, int] = {}
    for lease in leases:
        lease_states[lease["state"]] = lease_states.get(lease["state"], 0) + 1
    return {
        "units_total": len(units),
        "review_state": review_states,
        "observed_state": observed_states,
        "leases_total": len(leases),
        "lease_state": lease_states,
        "sessions_total": len(snapshot["sessions"]),
        "manual_runs_total": len(snapshot["manual_runs"]),
        "branches_total": len(snapshot["branches"]),
        "worktrees_total": len(snapshot["worktrees"]),
    }


def collect_snapshot(repo: Path, *, log_root_override: Path | None = None, events_limit: int = 100,
                    include_tombstones: bool = False) -> dict[str, Any]:
    paths = diff_tracker._lease_repo_paths(repo, log_root_override)
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
        "tombstones": [],
        "events": [],
        "counters": {},
    }
    if not db_path.is_file():
        snapshot["events"] = _read_events_tail(events_path, events_limit)
        snapshot["counters"] = _compute_counters(snapshot)
        return snapshot
    conn = diff_tracker._open_conn(db_path)
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
        if include_tombstones:
            snapshot["tombstones"] = _fetch_all(conn, "SELECT * FROM tombstones ORDER BY retired_at DESC")
    finally:
        conn.close()
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

/* Per-section column widths. */
table.t-sessions th:nth-child(1), table.t-sessions td:nth-child(1) { width: auto; }
table.t-sessions th:nth-child(2), table.t-sessions td:nth-child(2) { width: 70px; text-align: right; }
table.t-sessions th:nth-child(3), table.t-sessions td:nth-child(3) { width: 80px; }

table.t-leases th:nth-child(1), table.t-leases td:nth-child(1) { width: auto; }
table.t-leases th:nth-child(2), table.t-leases td:nth-child(2) { width: 70px; }
table.t-leases th:nth-child(3), table.t-leases td:nth-child(3) { width: 50px; text-align: right; }
table.t-leases th:nth-child(4), table.t-leases td:nth-child(4) { width: 100px; }

table.t-manual th:nth-child(1), table.t-manual td:nth-child(1) { width: 28%; }
table.t-manual th:nth-child(2), table.t-manual td:nth-child(2) { width: 28%; }
table.t-manual th:nth-child(3), table.t-manual td:nth-child(3) { width: auto; }
table.t-manual th:nth-child(4), table.t-manual td:nth-child(4) { width: 90px; }

table.t-units th:nth-child(1), table.t-units td:nth-child(1) { width: 110px; }
table.t-units th:nth-child(2), table.t-units td:nth-child(2) { width: auto; }
table.t-units th:nth-child(3), table.t-units td:nth-child(3) { width: 110px; }
table.t-units th:nth-child(4), table.t-units td:nth-child(4) { width: 100px; }
table.t-units th:nth-child(5), table.t-units td:nth-child(5) { width: 100px; }
table.t-units th:nth-child(6), table.t-units td:nth-child(6) { width: 22%; }
table.t-units th:nth-child(7), table.t-units td:nth-child(7) { width: 200px; }
table.t-units th:nth-child(8), table.t-units td:nth-child(8) { width: 90px; }

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
  <section class="panel"><h2>Units <span class="count" id="count-units"></span></h2><div id="units-body"></div></section>
  <section class="panel"><h2>Recent events <span class="count" id="count-events"></span></h2><div id="events-body"></div></section>
</div>
<script>
const POLL_MS = __POLL_MS__;
const REDRAW_MS = 1000;
const $ = (id) => document.getElementById(id);
let frozen = false;
let serverFrozen = false;
let lastSnapshot = null;
let lastFetchedAt = null;
let lastError = null;
let frozenNow = null;

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
function shortHash(s, n) {
  if (!s) return '-';
  const head = String(s).split(':').pop();
  return head.slice(0, n || 12);
}
function tag(text, kind) { return '<span class="tag ' + (kind || 'neutral') + '">' + esc(text) + '</span>'; }
const REVIEW_KIND = { available: 'ok', assigned: 'warn', reviewed: 'neutral', tombstoned: 'danger' };
const OBSERVED_KIND = { dirty: 'warn', committed: 'ok', superseded: 'neutral' };
const LEASE_KIND = { active: 'ok', paused: 'warn', completed: 'neutral', 'stale-released': 'danger', 'failed-released': 'danger' };

function renderKpis(c) {
  const cells = [];
  cells.push(`<div class="kpi"><div class="label">units</div><div class="value">${c.units_total||0}</div></div>`);
  cells.push(`<div class="kpi"><div class="label">leases</div><div class="value">${c.leases_total||0}</div></div>`);
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
  const rows = sessions.map(s => {
    const owned = counts[s.session_id + '|owned'] || 0;
    const takeover = counts[s.session_id + '|takeover'] || 0;
    const transferred = counts[s.session_id + '|transferred'] || 0;
    const pills = [tag('o ' + owned, 'ok')];
    if (takeover) pills.push(tag('t ' + takeover, 'warn'));
    if (transferred) pills.push(tag('x ' + transferred, 'neutral'));
    return `<tr><td class="mono" title="${esc(s.session_id)}">${esc(s.session_id)}</td><td>${pills.join(' ')}</td><td class="mono" title="${esc(s.last_seen_at)}">${esc(fmtAge(s.last_seen_at, now))}</td></tr>`;
  }).join('');
  $('sessions-body').innerHTML = `<table class="t-sessions"><thead><tr><th>session_id</th><th>units</th><th>last seen</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderLeases(snap, now) {
  const active = (snap.leases || []).filter(l => l.state === 'active');
  $('count-leases').textContent = '(' + active.length + ' active · ' + ((snap.leases || []).length - active.length) + ' other)';
  if (!active.length) { $('leases-body').innerHTML = '<div class="empty">no active leases</div>'; return; }
  const luByLease = {};
  for (const lu of (snap.lease_units || [])) (luByLease[lu.lease_id] = luByLease[lu.lease_id] || []).push(lu);
  const rows = active.map(l => {
    const ucount = (luByLease[l.lease_id] || []).length;
    const [eta, kind] = fmtEta(l.expires_at, now);
    const holderKind = l.holder_kind === 'reviewer' ? 'ok' : (l.holder_kind === 'manual' ? 'warn' : 'neutral');
    return `<tr><td class="mono" title="${esc(l.lease_id)} session=${esc(l.session_id)}">${esc(shortHash(l.lease_id, 24))}</td><td>${tag(l.holder_kind, holderKind)}</td><td class="num">${ucount}</td><td>${tag(eta, kind)}</td></tr>`;
  }).join('');
  $('leases-body').innerHTML = `<table class="t-leases"><thead><tr><th>lease_id</th><th>kind</th><th>units</th><th>expires</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderManual(snap, now) {
  const runs = snap.manual_runs || [];
  $('count-manual').textContent = '(' + runs.length + ')';
  if (!runs.length) { $('manual-body').innerHTML = '<div class="empty">none</div>'; return; }
  const rows = runs.map(r => `<tr><td class="mono" title="${esc(r.session_id)}">${esc(r.session_id)}</td><td class="mono" title="${esc(r.run_id)}">${esc(r.run_id)}</td><td class="mono" title="${esc(r.scope_hash)}">${esc(shortHash(r.scope_hash, 16))}</td><td class="mono" title="${esc(r.completed_at)}">${esc(fmtAge(r.completed_at, now))}</td></tr>`).join('');
  $('manual-body').innerHTML = `<table class="t-manual"><thead><tr><th>session</th><th>run</th><th>scope</th><th>completed</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderUnits(snap, now) {
  const units = snap.units || [];
  $('count-units').textContent = '(' + units.length + ')';
  if (!units.length) { $('units-body').innerHTML = '<div class="empty">none</div>'; return; }
  const ownersByUnit = {};
  for (const su of (snap.session_units || [])) (ownersByUnit[su.unit_id] = ownersByUnit[su.unit_id] || []).push(su);
  const activeLeasesById = {};
  for (const l of (snap.leases || [])) if (l.state === 'active') activeLeasesById[l.lease_id] = l;
  const holderByUnit = {};
  for (const lu of (snap.lease_units || [])) {
    const l = activeLeasesById[lu.lease_id];
    if (l) holderByUnit[lu.unit_id] = l.session_id + ' (' + l.holder_kind + ')';
  }
  const rows = units.map(u => {
    const owners = (ownersByUnit[u.unit_id] || []).map(su => su.session_id + ':' + su.assignment_kind).join(', ') || '-';
    const holder = holderByUnit[u.unit_id] || '-';
    return `<tr><td class="mono" title="${esc(u.unit_id)}">${esc(u.unit_id.slice(0, 12))}</td><td class="mono" title="${esc(u.path)}">${esc(u.path)}</td><td>${esc(u.kind)}</td><td>${tag(u.observed_state, OBSERVED_KIND[u.observed_state] || 'neutral')}</td><td>${tag(u.review_state, REVIEW_KIND[u.review_state] || 'neutral')}</td><td class="mono" title="${esc(owners)}">${esc(owners)}</td><td class="mono" title="${esc(holder)}">${esc(holder)}</td><td class="mono" title="${esc(u.last_observed_at)}">${esc(fmtAge(u.last_observed_at, now))}</td></tr>`;
  }).join('');
  $('units-body').innerHTML = `<table class="t-units"><thead><tr><th>unit_id</th><th>path</th><th>kind</th><th>observed</th><th>review</th><th>session owners</th><th>active lease</th><th>last obs</th></tr></thead><tbody>${rows}</tbody></table>`;
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
            paths = diff_tracker._lease_repo_paths(repo, log_root_override)
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
