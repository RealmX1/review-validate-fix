#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest


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


def _make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        target=None,
        run_id=None,
        run_dir=None,
        latest=False,
        orphan_age_hours=6.0,
        auto_finalize_orphan=False,
        decline_finalize=False,
        force=False,
        json=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _bootstrap_run(
    log_root: Path,
    *,
    run_id: str,
    status: str = "started",
    timestamp: str = "2026-05-04T00:00:00Z",
    finalize_lock: bool = False,
) -> Path:
    run_dir = log_root / "runs" / run_id
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": status,
                "timestamp": timestamp,
                "events_path": str(run_dir / "events.jsonl"),
            }
        ),
        encoding="utf-8",
    )
    if finalize_lock:
        (artifacts / ".finalize.lock").write_text(
            json.dumps({"decision_kind": "handoff", "run_id": run_id}),
            encoding="utf-8",
        )
    return run_dir


def test_resolve_run_dir_from_run_id(tmp_path, monkeypatch):
    rvf_analyze = _load("rvf_analyze")
    log_root = tmp_path / "rvf-log"
    run_dir = _bootstrap_run(log_root, run_id="rvf-1")
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    args = _make_args(run_id="rvf-1")
    resolved = rvf_analyze.resolve_run_dir(args)
    assert resolved == run_dir


def test_resolve_run_dir_from_latest_pointer(tmp_path, monkeypatch):
    rvf_analyze = _load("rvf_analyze")
    log_root = tmp_path / "rvf-log"
    run_dir = _bootstrap_run(log_root, run_id="rvf-latest")
    (log_root / "latest.json").write_text(
        json.dumps({"summary_path": str(run_dir / "summary.json")}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    args = _make_args(latest=True)
    resolved = rvf_analyze.resolve_run_dir(args)
    assert resolved == run_dir


def test_resolve_run_dir_positional_latest(tmp_path, monkeypatch):
    rvf_analyze = _load("rvf_analyze")
    log_root = tmp_path / "rvf-log"
    run_dir = _bootstrap_run(log_root, run_id="rvf-pos")
    (log_root / "latest.json").write_text(
        json.dumps({"summary_path": str(run_dir / "summary.json")}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    args = _make_args(target="latest")
    resolved = rvf_analyze.resolve_run_dir(args)
    assert resolved == run_dir


def test_resolve_failed_returns_none(tmp_path, monkeypatch):
    rvf_analyze = _load("rvf_analyze")
    log_root = tmp_path / "rvf-log"
    log_root.mkdir()
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    args = _make_args(run_id="does-not-exist")
    assert rvf_analyze.resolve_run_dir(args) is None


def test_analyze_finalized_run_scaffolds(tmp_path, monkeypatch):
    rvf_analyze = _load("rvf_analyze")
    log_root = tmp_path / "rvf-log"
    run_dir = _bootstrap_run(log_root, run_id="rvf-finalized", finalize_lock=True)
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    code, payload = rvf_analyze.analyze(_make_args(run_id="rvf-finalized"))
    assert code == rvf_analyze.EXIT_OK
    assert payload["status"] == "ok"
    assert payload["classification"]["kind"] == "finalized"
    assert payload["user_decision"] is None  # no marker write for already-finalized
    assert Path(payload["summary_md_path"]).is_file()
    assert Path(payload["causality_json_path"]).is_file()
    causality = json.loads(Path(payload["causality_json_path"]).read_text(encoding="utf-8"))
    assert causality["schema_version"] == 1


def test_analyze_orphan_without_decision_returns_needs_decision(tmp_path, monkeypatch):
    rvf_analyze = _load("rvf_analyze")
    log_root = tmp_path / "rvf-log"
    _bootstrap_run(
        log_root,
        run_id="rvf-orph",
        status="started",
        timestamp="2026-01-01T00:00:00Z",  # very stale
    )
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    code, payload = rvf_analyze.analyze(_make_args(run_id="rvf-orph"))
    assert code == rvf_analyze.EXIT_NEEDS_DECISION
    assert payload["status"] == "needs_decision"
    assert payload["classification"]["kind"] == "orphan_candidate"


def test_analyze_orphan_decline_finalize_writes_marker_and_scaffolds(
    tmp_path, monkeypatch
):
    rvf_analyze = _load("rvf_analyze")
    log_root = tmp_path / "rvf-log"
    run_dir = _bootstrap_run(
        log_root,
        run_id="rvf-decline",
        status="started",
        timestamp="2026-01-01T00:00:00Z",
    )
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    code, payload = rvf_analyze.analyze(
        _make_args(run_id="rvf-decline", decline_finalize=True)
    )
    assert code == rvf_analyze.EXIT_OK
    assert payload["user_decision"] == "declined_finalize"
    marker = run_dir / "artifacts" / ".interrupted"
    assert marker.is_file()
    record = json.loads(marker.read_text(encoding="utf-8"))
    assert record["user_decision"] == "declined_finalize"
    assert record["classification"]["kind"] == "orphan_candidate"


def test_analyze_orphan_auto_finalize_invokes_finalize_run(tmp_path, monkeypatch):
    rvf_analyze = _load("rvf_analyze")
    log_root = tmp_path / "rvf-log"
    run_dir = _bootstrap_run(
        log_root,
        run_id="rvf-auto",
        status="started",
        timestamp="2026-01-01T00:00:00Z",
    )
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))

    code, payload = rvf_analyze.analyze(
        _make_args(run_id="rvf-auto", auto_finalize_orphan=True)
    )
    assert code == rvf_analyze.EXIT_OK
    assert payload["user_decision"] == "lazy_finalized"
    # finalize_run should have written the lock
    assert (run_dir / "artifacts" / ".finalize.lock").is_file()
    # marker must reflect the lazy finalize choice
    marker = json.loads(
        (run_dir / "artifacts" / ".interrupted").read_text(encoding="utf-8")
    )
    assert marker["user_decision"] == "lazy_finalized"
    assert marker["lazy_finalize_decision_kind"] == "lazy_orphan_finalize"
    # post-finalize re-classification should now read as finalized
    assert payload["classification"]["kind"] == "finalized"
    assert "lazy_finalize" in payload
    assert payload["lazy_finalize"]["decision_kind"] == "lazy_orphan_finalize"


def test_analyze_running_refuses_without_force(tmp_path, monkeypatch):
    rvf_analyze = _load("rvf_analyze")
    orphan_detect = _load("orphan_detect")
    log_root = tmp_path / "rvf-log"
    # Use _utc_now-ish recent timestamp so it's running, not orphan_candidate.
    recent = orphan_detect._utc_now_iso()
    _bootstrap_run(log_root, run_id="rvf-live", status="started", timestamp=recent)
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    code, payload = rvf_analyze.analyze(_make_args(run_id="rvf-live"))
    assert code == rvf_analyze.EXIT_RUNNING
    assert payload["status"] == "running"


def test_analyze_force_through_running_scaffolds_with_marker(tmp_path, monkeypatch):
    rvf_analyze = _load("rvf_analyze")
    orphan_detect = _load("orphan_detect")
    log_root = tmp_path / "rvf-log"
    recent = orphan_detect._utc_now_iso()
    run_dir = _bootstrap_run(log_root, run_id="rvf-forced", status="started", timestamp=recent)
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    code, payload = rvf_analyze.analyze(_make_args(run_id="rvf-forced", force=True))
    assert code == rvf_analyze.EXIT_OK
    marker = json.loads(
        (run_dir / "artifacts" / ".interrupted").read_text(encoding="utf-8")
    )
    assert marker["user_decision"] == "auto_classified_only"
    assert marker["extra"].get("forced_through_running") is True


def test_analyze_half_broken_writes_marker_and_scaffolds(tmp_path, monkeypatch):
    rvf_analyze = _load("rvf_analyze")
    log_root = tmp_path / "rvf-log"
    # half-broken: run dir exists but summary.json is malformed JSON
    run_dir = log_root / "runs" / "rvf-broken"
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "summary.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    code, payload = rvf_analyze.analyze(_make_args(run_id="rvf-broken"))
    assert code == rvf_analyze.EXIT_OK
    assert payload["classification"]["kind"] == "half_broken"
    assert payload["user_decision"] == "auto_classified_only"
    marker = run_dir / "artifacts" / ".interrupted"
    assert marker.is_file()


def test_analyze_resolve_failed_exit_code(tmp_path, monkeypatch):
    rvf_analyze = _load("rvf_analyze")
    log_root = tmp_path / "rvf-log"
    log_root.mkdir()
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    code, payload = rvf_analyze.analyze(_make_args(run_id="never-existed"))
    assert code == rvf_analyze.EXIT_RESOLVE_FAILED
    assert payload["status"] == "resolve_failed"


def test_main_emits_json_and_exit_code(tmp_path, monkeypatch, capsys):
    rvf_analyze = _load("rvf_analyze")
    log_root = tmp_path / "rvf-log"
    _bootstrap_run(log_root, run_id="rvf-main", finalize_lock=True)
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    code = rvf_analyze.main(["--run-id", "rvf-main"])
    assert code == rvf_analyze.EXIT_OK
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["classification"]["kind"] == "finalized"
