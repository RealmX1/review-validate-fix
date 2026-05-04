#!/usr/bin/env python3
"""Tests for orphan_detect.classify_run + .interrupted marker IO."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = (
    ROOT
    / "plugins"
    / "review-validate-fix"
    / "skills"
    / "review-validate-fix"
    / "scripts"
)


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _make_run_dir(root: Path, run_id: str = "rvf-orphan-test") -> Path:
    run_dir = root / "runs" / run_id
    (run_dir / "artifacts").mkdir(parents=True)
    return run_dir


def _write_summary(run_dir: Path, payload: dict) -> None:
    (run_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def test_classify_run_finalized_when_lock_exists(tmp_path: Path) -> None:
    od = _load("orphan_detect")
    run_dir = _make_run_dir(tmp_path)
    _write_summary(
        run_dir,
        {"run_id": "r1", "status": "started", "timestamp": "2026-05-04T00:00:00Z"},
    )
    # Lock content irrelevant for classification.
    (run_dir / "artifacts" / ".finalize.lock").write_text("not even json", encoding="utf-8")

    result = od.classify_run(run_dir, now_iso="2026-05-04T01:00:00Z")
    assert result.kind == "finalized"
    assert result.has_finalize_lock is True
    assert result.run_id == "r1"
    assert result.prior_status == "started"


def test_classify_run_half_broken_when_summary_missing(tmp_path: Path) -> None:
    od = _load("orphan_detect")
    run_dir = _make_run_dir(tmp_path)
    # No summary.json at all.
    result = od.classify_run(run_dir, now_iso="2026-05-04T01:00:00Z")
    assert result.kind == "half_broken"
    assert result.run_id is None
    assert result.prior_status is None
    assert result.has_finalize_lock is False


def test_classify_run_half_broken_when_summary_malformed(tmp_path: Path) -> None:
    od = _load("orphan_detect")
    run_dir = _make_run_dir(tmp_path)
    (run_dir / "summary.json").write_text("{this is :: not json", encoding="utf-8")
    result = od.classify_run(run_dir, now_iso="2026-05-04T01:00:00Z")
    assert result.kind == "half_broken"
    assert result.run_id is None


def test_classify_run_cancel_without_lock(tmp_path: Path) -> None:
    od = _load("orphan_detect")
    run_dir = _make_run_dir(tmp_path)
    _write_summary(
        run_dir,
        {
            "run_id": "r-cancel",
            "status": "cline-kanban-rvf-cancelled",
            "timestamp": "2026-05-04T00:00:00Z",
        },
    )
    result = od.classify_run(run_dir, now_iso="2026-05-04T01:00:00Z")
    assert result.kind == "cancel_without_lock"
    assert result.prior_status == "cline-kanban-rvf-cancelled"


def test_classify_run_running_when_started_and_recent(tmp_path: Path) -> None:
    od = _load("orphan_detect")
    run_dir = _make_run_dir(tmp_path)
    _write_summary(
        run_dir,
        {
            "run_id": "r-fresh",
            "status": "started",
            "timestamp": "2026-05-04T00:00:00Z",
        },
    )
    # 1 hour ago, default threshold 6h => running.
    result = od.classify_run(run_dir, now_iso="2026-05-04T01:00:00Z")
    assert result.kind == "running"
    assert result.age_seconds is not None
    assert abs(result.age_seconds - 3600.0) < 1e-6


def test_classify_run_orphan_candidate_when_stale(tmp_path: Path) -> None:
    od = _load("orphan_detect")
    run_dir = _make_run_dir(tmp_path)
    _write_summary(
        run_dir,
        {
            "run_id": "r-stale",
            "status": "started",
            "timestamp": "2026-05-04T00:00:00Z",
        },
    )
    # 12 hours later, default threshold 6h => orphan_candidate.
    result = od.classify_run(run_dir, now_iso="2026-05-04T12:00:00Z")
    assert result.kind == "orphan_candidate"
    assert result.age_seconds is not None
    assert result.age_seconds >= 6 * 3600


def test_classify_run_orphan_candidate_when_timestamp_missing(tmp_path: Path) -> None:
    od = _load("orphan_detect")
    run_dir = _make_run_dir(tmp_path)
    _write_summary(run_dir, {"run_id": "r-no-ts", "status": "started"})
    result = od.classify_run(run_dir, now_iso="2026-05-04T01:00:00Z")
    assert result.kind == "orphan_candidate"
    assert result.age_seconds is None
    assert result.prior_timestamp is None


def test_classify_run_has_interrupted_marker_flag(tmp_path: Path) -> None:
    od = _load("orphan_detect")
    run_dir = _make_run_dir(tmp_path)
    _write_summary(
        run_dir,
        {
            "run_id": "r-marked",
            "status": "started",
            "timestamp": "2026-05-04T00:00:00Z",
        },
    )
    (run_dir / "artifacts" / ".interrupted").write_text("{}", encoding="utf-8")
    result = od.classify_run(run_dir, now_iso="2026-05-04T01:00:00Z")
    assert result.has_interrupted_marker is True
    # Marker presence does NOT change the kind.
    assert result.kind == "running"


def test_marker_round_trip_includes_extra(tmp_path: Path) -> None:
    od = _load("orphan_detect")
    run_dir = _make_run_dir(tmp_path)
    _write_summary(
        run_dir,
        {
            "run_id": "r-rt",
            "status": "started",
            "timestamp": "2026-05-04T00:00:00Z",
        },
    )
    cls = od.classify_run(run_dir, now_iso="2026-05-04T12:00:00Z")
    extra = {"prompted_via": "/rvf-analyze", "user_note": "long lunch"}
    marker_path = od.write_interrupted_marker(
        run_dir,
        classification=cls,
        user_decision="lazy_finalized",
        lazy_finalize_decision_kind="lazy_orphan_finalize",
        extra=extra,
    )
    assert marker_path == run_dir / "artifacts" / ".interrupted"
    assert marker_path.exists()

    payload = od.read_interrupted_marker(run_dir)
    assert payload is not None
    assert payload["schema_version"] == od.INTERRUPTED_MARKER_SCHEMA_VERSION
    assert payload["user_decision"] == "lazy_finalized"
    assert payload["lazy_finalize_decision_kind"] == "lazy_orphan_finalize"
    assert payload["extra"] == extra
    assert payload["classification"]["run_id"] == "r-rt"
    assert payload["classification"]["kind"] == "orphan_candidate"
    assert "written_at" in payload and payload["written_at"].endswith("Z")


def test_marker_overwrite_replaces_state_atomically(tmp_path: Path) -> None:
    od = _load("orphan_detect")
    run_dir = _make_run_dir(tmp_path)
    _write_summary(
        run_dir,
        {
            "run_id": "r-overwrite",
            "status": "started",
            "timestamp": "2026-05-04T00:00:00Z",
        },
    )
    cls = od.classify_run(run_dir, now_iso="2026-05-04T12:00:00Z")
    od.write_interrupted_marker(
        run_dir,
        classification=cls,
        user_decision="auto_classified_only",
        extra={"first": True},
    )
    # Second write with different fields.
    od.write_interrupted_marker(
        run_dir,
        classification=cls,
        user_decision="declined_finalize",
        lazy_finalize_decision_kind=None,
        extra={"second": True},
    )

    payload = od.read_interrupted_marker(run_dir)
    assert payload is not None
    assert payload["user_decision"] == "declined_finalize"
    assert payload["extra"] == {"second": True}
    assert payload["lazy_finalize_decision_kind"] is None
    # No leftover .tmp partial-state file.
    tmp_files = list((run_dir / "artifacts").glob(".interrupted.tmp"))
    assert tmp_files == []


def test_read_interrupted_marker_handles_missing_and_malformed(tmp_path: Path) -> None:
    od = _load("orphan_detect")
    run_dir = _make_run_dir(tmp_path)
    # Missing.
    assert od.read_interrupted_marker(run_dir) is None
    # Malformed JSON.
    (run_dir / "artifacts" / ".interrupted").write_text("not { json", encoding="utf-8")
    assert od.read_interrupted_marker(run_dir) is None
