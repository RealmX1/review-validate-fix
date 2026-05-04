#!/usr/bin/env python3
"""RVF run lifecycle classification + .interrupted marker IO.

Backend for the Phase C ``/rvf-analyze`` skill. Pure, deterministic, non-interactive:
classify a run dir's lifecycle state, and persist a marker the skill writes after
prompting the user. No prompts, no LLM calls, no orchestration of finalize_run --
the skill layer drives that.

Stays self-contained on purpose: does not import sibling RVF scripts so that the
skill can call this module without dragging the whole RVF runtime in.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


ClassificationKind = Literal[
    "finalized",
    "running",
    "orphan_candidate",
    "cancel_without_lock",
    "half_broken",
]


UserDecision = Literal[
    "lazy_finalized",
    "declined_finalize",
    "auto_classified_only",
]


INTERRUPTED_MARKER_SCHEMA_VERSION = 1
INTERRUPTED_MARKER_FILENAME = ".interrupted"

# Status strings RunLedger.summary() may emit. Only the two explicitly listed
# below get bespoke treatment; everything else is defensively classified.
_STATUS_STARTED = "started"
_STATUS_PREPARE_COMPLETED = "prepare-completed"
_CANCELLED_STATUSES = frozenset({"cline-kanban-rvf-cancelled", "cancelled"})
_INFLIGHT_STATUSES = frozenset({_STATUS_STARTED, _STATUS_PREPARE_COMPLETED})


@dataclass(frozen=True)
class Classification:
    kind: ClassificationKind
    run_dir: str
    run_id: str | None
    prior_status: str | None
    prior_timestamp: str | None
    age_seconds: float | None
    has_finalize_lock: bool
    has_interrupted_marker: bool
    detected_at: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso_z(ts: str) -> datetime | None:
    """Parse an ISO8601 Z timestamp. Returns None on any parse failure."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_summary(path: Path) -> dict[str, Any] | None:
    """Returns parsed dict, or None if missing/unparseable/non-dict."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic JSON write: <name>.tmp + os.replace.

    Marker file is leading-dot already, so use the visible ``.tmp`` suffix
    (so ``ls -la`` still surfaces orphan tmp files if a write is interrupted).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def classify_run(
    run_dir: Path,
    *,
    orphan_age_seconds: float = 6 * 3600,
    now_iso: str | None = None,
) -> Classification:
    """Pure classification of a run dir's lifecycle state. No side effects."""
    run_dir = Path(run_dir)
    artifacts_dir = run_dir / "artifacts"
    lock_path = artifacts_dir / ".finalize.lock"
    marker_path = artifacts_dir / INTERRUPTED_MARKER_FILENAME

    has_lock = lock_path.exists()
    has_marker = marker_path.exists()
    detected_at = now_iso if now_iso else _utc_now_iso()

    summary = _read_summary(run_dir / "summary.json")
    run_id = summary.get("run_id") if isinstance(summary, dict) else None
    prior_status = summary.get("status") if isinstance(summary, dict) else None
    prior_timestamp = summary.get("timestamp") if isinstance(summary, dict) else None
    if not isinstance(run_id, str):
        run_id = None
    if not isinstance(prior_status, str):
        prior_status = None
    if not isinstance(prior_timestamp, str):
        prior_timestamp = None

    age_seconds: float | None = None
    if prior_timestamp is not None:
        prior_dt = _parse_iso_z(prior_timestamp)
        now_dt = _parse_iso_z(detected_at)
        if prior_dt is not None and now_dt is not None:
            age_seconds = (now_dt - prior_dt).total_seconds()

    if has_lock:
        kind: ClassificationKind = "finalized"
    elif summary is None:
        kind = "half_broken"
    elif prior_status in _CANCELLED_STATUSES:
        kind = "cancel_without_lock"
    elif prior_status in _INFLIGHT_STATUSES:
        # Missing/unparseable timestamp → err on stale.
        if age_seconds is None:
            kind = "orphan_candidate"
        elif age_seconds < orphan_age_seconds:
            kind = "running"
        else:
            kind = "orphan_candidate"
    else:
        kind = "half_broken"

    return Classification(
        kind=kind,
        run_dir=str(run_dir),
        run_id=run_id,
        prior_status=prior_status,
        prior_timestamp=prior_timestamp,
        age_seconds=age_seconds,
        has_finalize_lock=has_lock,
        has_interrupted_marker=has_marker,
        detected_at=detected_at,
    )


def write_interrupted_marker(
    run_dir: Path,
    *,
    classification: Classification,
    user_decision: UserDecision,
    lazy_finalize_decision_kind: str | None = None,
    extra: dict | None = None,
) -> Path:
    """Atomically write ``<run_dir>/artifacts/.interrupted``. Overwrites any prior marker."""
    run_dir = Path(run_dir)
    artifacts_dir = run_dir / "artifacts"
    marker_path = artifacts_dir / INTERRUPTED_MARKER_FILENAME

    payload: dict[str, Any] = {
        "schema_version": INTERRUPTED_MARKER_SCHEMA_VERSION,
        "classification": asdict(classification),
        "user_decision": user_decision,
        "lazy_finalize_decision_kind": lazy_finalize_decision_kind,
        "extra": extra if extra is not None else {},
        "written_at": _utc_now_iso(),
    }

    _atomic_write_json(marker_path, payload)
    return marker_path


def read_interrupted_marker(run_dir: Path) -> dict | None:
    """Return parsed marker payload or None if missing/unparseable. Never raises."""
    marker_path = Path(run_dir) / "artifacts" / INTERRUPTED_MARKER_FILENAME
    try:
        raw = marker_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
