#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
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


from _rvf_test_support.loader import load_script_module as _load


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "rvf@example.com")
    _git(path, "config", "user.name", "RVF")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _write_transcript(path: Path, marker: str) -> None:
    records = [
        {"timestamp": "2026-05-04T00:00:00Z", "type": "session_meta", "payload": {"id": "S"}},
        {
            "timestamp": "2026-05-04T01:00:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": f"go {marker}"},
        },
    ]
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _bootstrap_run(tmp_path: Path, repo: Path, *, run_id: str) -> tuple[Path, Path]:
    """Build a synthetic state/runs/<run_id> with summary.json + before-snapshot.

    Returns (run_dir, log_root)."""
    log_root = tmp_path / "rvf-log-root"
    runs_dir = log_root / "runs" / run_id
    artifacts = runs_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (runs_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "started",
                "reason_code": "test",
                "repo": str(repo),
                "cwd": str(repo),
                "events_path": str(runs_dir / "events.jsonl"),
                "artifacts_dir": str(artifacts),
                "run_dir": str(runs_dir),
                "timestamp": "2026-05-04T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    snapshot = _load("workspace_snapshot")
    (artifacts / "before-workspace-snapshot.json").write_text(
        json.dumps(snapshot.capture(repo), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return runs_dir, log_root


def _make_args(*, run_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        run_id=None,
        run_dir=str(run_dir),
        summary=None,
        task_cmd="kanban",
        force_after=0.1,
        dry_run=False,
    )


def test_cancel_run_invokes_finalize_and_writes_lock(
    tmp_path: Path, monkeypatch
) -> None:
    cancel = _load("cancel_rvf_run")
    capture = _load("trajectory_capture")
    repo = _init_repo(tmp_path / "repo")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(transcript, capture.RVF_SKILL_TRIGGER)
    run_dir, log_root = _bootstrap_run(tmp_path, repo, run_id="rvf-cancel-1")
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    monkeypatch.delenv("CODEX_RVF_RUN_DIR", raising=False)

    payload = cancel.cancel_run(_make_args(run_dir=run_dir))
    assert payload["status"] == "cancelled"

    lock = run_dir / "artifacts" / ".finalize.lock"
    assert lock.exists(), "finalize_run did not run after cancel"
    record = json.loads(lock.read_text(encoding="utf-8"))
    assert record["decision_kind"] == "cancelled"
    assert record["run_id"] == "rvf-cancel-1"


def test_cancel_after_normal_finalize_keeps_first_lock(
    tmp_path: Path, monkeypatch
) -> None:
    cancel = _load("cancel_rvf_run")
    finalize = _load("rvf_run_finalize")
    capture = _load("trajectory_capture")
    repo = _init_repo(tmp_path / "repo")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(transcript, capture.RVF_SKILL_TRIGGER)
    run_dir, log_root = _bootstrap_run(tmp_path, repo, run_id="rvf-cancel-2")
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))
    monkeypatch.delenv("CODEX_RVF_RUN_DIR", raising=False)

    # Pretend a normal handoff already finalized this run.
    event = {"transcript_path": str(transcript), "session_id": "S", "cwd": str(repo)}
    first = finalize.finalize_run(
        run_dir=run_dir, event=event, decision_kind="handoff-advisory"
    )
    assert first["decision_kind"] == "handoff-advisory"

    lock = run_dir / "artifacts" / ".finalize.lock"
    lock_mtime_before = lock.stat().st_mtime_ns

    payload = cancel.cancel_run(_make_args(run_dir=run_dir))
    assert payload["status"] == "cancelled"

    # idempotent: lock still records the original decision_kind.
    record = json.loads(lock.read_text(encoding="utf-8"))
    assert record["decision_kind"] == "handoff-advisory"
    assert lock.stat().st_mtime_ns == lock_mtime_before
