#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sqlite3
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


def _unit_review_states(tracker_dir: str, unit_ids: list[str]) -> dict[str, str]:
    placeholders = ",".join("?" for _ in unit_ids)
    conn = sqlite3.connect(str(Path(tracker_dir) / "tracker.sqlite3"))
    try:
        return {
            unit_id: state
            for unit_id, state in conn.execute(
                f"SELECT unit_id, review_state FROM units WHERE unit_id IN ({placeholders})",
                tuple(unit_ids),
            )
        }
    finally:
        conn.close()


def _write_transcript(path: Path, marker: str, *, originator: str | None = "Codex Desktop") -> None:
    session_meta_payload: dict = {"id": "S"}
    if originator is not None:
        session_meta_payload["originator"] = originator
    records = [
        {"timestamp": "t0", "type": "session_meta", "payload": session_meta_payload},
        {
            "timestamp": "2026-05-05T00:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 60,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 1,
                        "total_tokens": 110,
                    }
                },
            },
        },
        {
            "timestamp": "2026-05-05T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": f"go {marker}"},
        },
        {
            "timestamp": "2026-05-05T00:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 250,
                        "cached_input_tokens": 160,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 3,
                        "total_tokens": 280,
                    }
                },
            },
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
    assert record1["trajectory"]["host"] == "codex"
    assert record1["trajectory"]["host_originator"] == "Codex Desktop"
    assert record1["workspace_diff"]["status"] == "complete"
    assert record1["usage"]["input_tokens"] == 150
    assert record1["usage"]["cached_input_tokens"] == 100
    assert record1["usage"]["output_tokens"] == 20
    assert record1["usage"]["noncached_input_tokens"] == 50
    assert (run_dir / "artifacts" / "usage" / "usage-summary.json").is_file()
    assert record1["analysis"]["summary_md_path"].endswith(
        "/artifacts/analysis/summary.md"
    )
    assert record1["analysis"]["causality_json_path"].endswith(
        "/artifacts/analysis/causality.json"
    )
    assert (run_dir / "artifacts" / "analysis" / "summary.md").is_file()
    assert (run_dir / "artifacts" / "analysis" / "causality.json").is_file()
    lock = run_dir / "artifacts" / ".finalize.lock"
    assert lock.exists()
    summary_after = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert "finalize" in summary_after
    assert summary_after["finalize"]["run_id"] == "rvf-test-run"
    assert summary_after["finalize"]["analysis"]["stats"]["run_id"] == "rvf-test-run"

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


def test_finalize_run_releases_tracker_lease_from_scope_contract(tmp_path: Path, monkeypatch) -> None:
    finalize = _load("rvf_run_finalize")
    diff_tracker = _load("diff_tracker")
    capture = _load("trajectory_capture")
    repo = _init_repo(tmp_path / "repo")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(transcript, capture.RVF_FORK_MARKER)
    run_dir = _make_run(tmp_path, repo, transcript)
    log_root = tmp_path / "state"
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))

    (repo / "README.md").write_text("hello changed for tracker\n", encoding="utf-8")
    allocated = diff_tracker.allocate_review_scope(
        repo=repo,
        session_id="S",
        run_id="rvf-test-run",
        reviewer_id="allocator",
        log_root_override=log_root,
    )
    assert allocated["status"] == "allocated"

    inputs = run_dir / "artifacts" / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / "scope.contract.json").write_text(
        json.dumps(
            {
                "version": 2,
                "run_id": "rvf-test-run",
                "repo": str(repo),
                "primary_units": allocated["scope"]["unit_ids"],
                "tracker_lease_id": allocated["lease_id"],
                "tracker_scope_hash": allocated["scope_hash"],
            }
        ),
        encoding="utf-8",
    )

    record = finalize.finalize_run(
        run_dir=run_dir,
        event={"transcript_path": str(transcript), "session_id": "S", "cwd": str(repo)},
        decision_kind="test",
    )

    assert record["tracker_lease_release"]["released"] is True
    assert record["tracker_lease_release"]["lease_id"] == allocated["lease_id"]
    assert record["tracker_lease_release"]["release_state"] == "completed"
    states = _unit_review_states(allocated["tracker_dir"], allocated["scope"]["unit_ids"])
    assert states
    assert set(states.values()) == {"reviewed"}
    next_alloc = diff_tracker.allocate_review_scope(
        repo=repo,
        session_id="S",
        run_id="rvf-next-run",
        reviewer_id="allocator",
        log_root_override=log_root,
    )
    assert next_alloc["status"] == "empty"
    second = diff_tracker.lease_release(
        repo=repo,
        lease_id=allocated["lease_id"],
        log_root_override=log_root,
    )
    assert second["released"] is True
    assert second["reason"] == "lease_already_completed"


def test_finalize_run_marks_stale_tracker_scope_reviewed_from_contract_units(
    tmp_path: Path,
    monkeypatch,
) -> None:
    finalize = _load("rvf_run_finalize")
    diff_tracker = _load("diff_tracker")
    capture = _load("trajectory_capture")
    repo = _init_repo(tmp_path / "repo")
    transcript = tmp_path / "rollout.jsonl"
    _write_transcript(transcript, capture.RVF_FORK_MARKER)
    run_dir = _make_run(tmp_path, repo, transcript)
    log_root = tmp_path / "state"
    monkeypatch.setenv("CODEX_RVF_LOG_ROOT", str(log_root))

    (repo / "README.md").write_text("hello changed for stale tracker\n", encoding="utf-8")
    allocated = diff_tracker.allocate_review_scope(
        repo=repo,
        session_id="S",
        run_id="rvf-test-run",
        reviewer_id="allocator",
        lease_ttl_seconds=1,
        log_root_override=log_root,
        now="2026-05-06T00:00:00Z",
    )
    assert allocated["status"] == "allocated"
    swept = diff_tracker.sweep_stale(
        repo=repo,
        log_root_override=log_root,
        now="2026-05-06T00:00:02Z",
    )
    assert [item["lease_id"] for item in swept] == [allocated["lease_id"]]
    assert set(
        _unit_review_states(allocated["tracker_dir"], allocated["scope"]["unit_ids"]).values()
    ) == {"available"}

    inputs = run_dir / "artifacts" / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / "scope.contract.json").write_text(
        json.dumps(
            {
                "version": 2,
                "run_id": "rvf-test-run",
                "repo": str(repo),
                "primary_units": allocated["scope"]["unit_ids"],
                "tracker_lease_id": allocated["lease_id"],
                "tracker_scope_hash": allocated["scope_hash"],
            }
        ),
        encoding="utf-8",
    )

    record = finalize.finalize_run(
        run_dir=run_dir,
        event={"transcript_path": str(transcript), "session_id": "S", "cwd": str(repo)},
        decision_kind="test",
    )

    release = record["tracker_lease_release"]
    assert release["released"] is True
    assert release["status"] == "released"
    assert release["reason"] == "lease_completed_after_stale"
    assert release["released_unit_count"] == len(allocated["scope"]["unit_ids"])
    assert set(
        _unit_review_states(allocated["tracker_dir"], allocated["scope"]["unit_ids"]).values()
    ) == {"reviewed"}
    next_alloc = diff_tracker.allocate_review_scope(
        repo=repo,
        session_id="S",
        run_id="rvf-next-run",
        reviewer_id="allocator",
        log_root_override=log_root,
    )
    assert next_alloc["status"] == "empty"


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
