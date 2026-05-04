#!/usr/bin/env python3
from __future__ import annotations

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


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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


def _make_run(tmp_path: Path, repo: Path, transcript: Path) -> Path:
    run_dir = tmp_path / "rvf-run"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": "rvf-test-run",
                "status": "started",
                "reason_code": "test",
                "repo": str(repo),
                "events_path": str(run_dir / "events.jsonl"),
                "artifacts_dir": str(artifacts),
                "run_dir": str(run_dir),
            }
        ),
        encoding="utf-8",
    )
    snapshot = _load("workspace_snapshot")
    (artifacts / "before-workspace-snapshot.json").write_text(
        json.dumps(snapshot.capture(repo), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_dir


def _write_transcript(path: Path, marker: str) -> None:
    records = [
        {"timestamp": "t0", "type": "session_meta", "payload": {"id": "S"}},
        {
            "timestamp": "t1",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": f"go {marker}"},
        },
    ]
    with path.open("w", encoding="utf-8") as handle:
        for r in records:
            handle.write(json.dumps(r) + "\n")


def test_finalize_run_writes_trajectory_and_diff_and_is_idempotent(tmp_path: Path) -> None:
    finalize = _load("rvf_run_finalize")
    capture = _load("trajectory_capture")
    repo = _init_repo(tmp_path / "repo")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(transcript, capture.RVF_FORK_MARKER)
    run_dir = _make_run(tmp_path, repo, transcript)
    # mutate workspace so after diff has content
    (repo / "README.md").write_text("hello changed\n", encoding="utf-8")
    event = {"transcript_path": str(transcript), "session_id": "S", "cwd": str(repo)}

    record1 = finalize.finalize_run(run_dir=run_dir, event=event, decision_kind="test")
    assert record1["trajectory"]["pre_rvf_source_kind"] == "same-session-slice"
    assert record1["workspace_diff"]["status"] == "complete"
    lock = run_dir / "artifacts" / ".finalize.lock"
    assert lock.exists()
    summary_after = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert "finalize" in summary_after
    assert summary_after["finalize"]["run_id"] == "rvf-test-run"

    # Idempotency: rerun returns cached
    lock_mtime_before = lock.stat().st_mtime_ns
    record2 = finalize.finalize_run(run_dir=run_dir, event=event, decision_kind="test")
    assert record2.get("already_finalized") is True
    assert lock.stat().st_mtime_ns == lock_mtime_before


def test_finalize_for_handoff_resolves_run_dir_from_handoff_path(tmp_path: Path) -> None:
    finalize = _load("rvf_run_finalize")
    capture = _load("trajectory_capture")
    repo = _init_repo(tmp_path / "repo")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(transcript, capture.RVF_FORK_MARKER)
    run_dir = _make_run(tmp_path, repo, transcript)
    handoff = run_dir / "artifacts" / "handoff.md"
    handoff.write_text("# handoff\n", encoding="utf-8")

    event = {"transcript_path": str(transcript), "session_id": "S", "cwd": str(repo)}
    record = finalize.finalize_for_handoff(
        handoff_path=handoff, event=event, decision_kind="handoff-advisory"
    )
    assert record is not None
    assert record["run_dir"] == str(run_dir)
    assert record["decision_kind"] == "handoff-advisory"


def test_finalize_for_handoff_returns_none_when_run_dir_missing(
    tmp_path: Path, monkeypatch
) -> None:
    # Stale CODEX_RVF_RUN_DIR inherited from a previous RVF run (or from a
    # reviewer subprocess) used to leak into resolve_run_dir; ensure a missing
    # handoff/event truly resolves to None even if the env points at some other
    # run_dir.
    monkeypatch.delenv("CODEX_RVF_RUN_DIR", raising=False)
    finalize = _load("rvf_run_finalize")
    handoff = tmp_path / "stray.md"
    handoff.write_text("hi", encoding="utf-8")
    record = finalize.finalize_for_handoff(handoff_path=handoff, event={})
    assert record is None
