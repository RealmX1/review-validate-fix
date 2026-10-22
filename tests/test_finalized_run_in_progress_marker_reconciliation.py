"""完成证据、按 run 清理和清理故障的隔离回归。"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/review-validate-fix/skills/review-validate-fix/scripts"))


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("RVF_", "CODEX_RVF_", "KANBAN_", "CLINE_KANBAN_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("RVF_HOME", str(tmp_path / "rvf-home"))
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "user-local-data"))
    monkeypatch.setenv("RVF_KANBAN_FOLLOWUP_LOCK_ROOT", str(tmp_path / "followup-markers"))
    monkeypatch.setenv("RVF_PROVIDER_HEALTH_CHECK", "0")
    monkeypatch.setenv("CODEX_RVF_FORK_MODE", "off")
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    engine = importlib.import_module("stop_review_validate_fix")
    monkeypatch.setattr(engine, "_kanban_followup_pending_decision", lambda *args, **kwargs: None)
    return repository, engine, importlib.import_module("kanban_followup_lock")


def write_run_and_marker(repository, locks, *, run_id="completed-run", session_id="test-session", task_id=None):
    run_dir = repository.parent / run_id
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "handoff.md").write_text("# 交接\n", encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps({"run_id": run_id, "repo": str(repository)}))
    marker_path = locks.write_marker(
        task_id=task_id, session_id=session_id, repo=str(repository), cwd=str(repository),
        run_id=run_id, run_dir=str(run_dir), state=locks.STATE_IN_PROGRESS,
    )
    record = {"run_id": run_id, "run_dir": str(run_dir), "errors": [],
              "completed_at": "2026-10-22T00:00:00Z", "decision_kind": "handoff"}
    return run_dir, marker_path, record


def stop_decision(repository, engine):
    from core.run_ledger.run_ledger import start_run
    ledger = start_run("test-finalized-marker")
    decision = engine.kanban_followup_in_progress_decision(
        {"cwd": str(repository), "session_id": "test-session"}, ledger, cwd=str(repository),
    )
    events = [json.loads(line) for line in ledger.events_path.read_text().splitlines()]
    return decision, events


@pytest.mark.parametrize("invalid_record", [
    "{invalid", "[]", "{}", {"run_id": "different-run"},
    {"run_dir": "/different-run"}, {"completed_at": "invalid"},
    {"run_id": 123}, {"run_dir": 123},
])
def test_unusable_finalize_evidence_does_not_suppress_unfinished_run(isolated_runtime, invalid_record):
    repository, engine, locks = isolated_runtime
    run_dir, marker_path, record = write_run_and_marker(repository, locks)
    content = json.dumps({**record, **invalid_record}) if isinstance(invalid_record, dict) else invalid_record
    (run_dir / "artifacts/.finalize.lock").write_text(content)
    decision, events = stop_decision(repository, engine)
    assert decision is not None and decision.reason_code == "kanban_followup_in_progress_reengage"
    assert marker_path.exists()
    assert not any(e["event"] == "kanban_followup_lock_reconciled_for_already_finalized_run" for e in events)


def test_finalized_run_with_auxiliary_errors_is_not_reengaged(isolated_runtime):
    repository, engine, locks = isolated_runtime
    run_dir, marker_path, record = write_run_and_marker(repository, locks)
    record["errors"] = [{"stage": "analysis_scaffold", "error": "测试附属故障"}]
    (run_dir / "artifacts/.finalize.lock").write_text(json.dumps(record))
    decision, events = stop_decision(repository, engine)
    assert decision is None and not marker_path.exists()
    reconciled = next(e for e in events if e["event"] == "kanban_followup_lock_reconciled_for_already_finalized_run")
    assert reconciled["finalize_completion_evidence"]["finalize_errors"] == record["errors"]


def test_completed_marker_unlink_failure_is_diagnostic_without_reengage(isolated_runtime, monkeypatch):
    repository, engine, locks = isolated_runtime
    run_dir, marker_path, record = write_run_and_marker(repository, locks)
    (run_dir / "artifacts/.finalize.lock").write_text(json.dumps(record))
    original_unlink = Path.unlink
    def deny_marker_unlink(path, *args, **kwargs):
        if path == marker_path:
            raise PermissionError("测试清理失败")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", deny_marker_unlink)
    decision, events = stop_decision(repository, engine)
    assert decision is None and marker_path.exists()
    reconciled = next(e for e in events if e["event"] == "kanban_followup_lock_reconciled_for_already_finalized_run")
    assert reconciled["status"] == "warning"
    assert reconciled["kept_kanban_followup_in_progress_markers"][0]["cleanup_reason"] == "marker_unlink_failed"


@pytest.mark.parametrize("different_identity", [
    {}, {"run_id": "different-run"}, {"run_id": "completed-run", "run_dir": "/different-run"},
])
def test_handoff_preserves_unknown_or_conflicting_run_identity(isolated_runtime, different_identity):
    repository, _engine, locks = isolated_runtime
    run_dir, marker_path, record = write_run_and_marker(repository, locks)
    marker_path.write_text(json.dumps(different_identity))
    removed, kept = locks.clear_markers_belonging_to_handed_off_run(
        task_id=None, session_id="test-session", repo=repository,
        run_id=record["run_id"], run_dir=run_dir,
    )
    assert removed == [] and len(kept) == 1 and marker_path.exists()


def test_cleanup_removes_only_its_run_and_bootstrap_replay_claim(isolated_runtime):
    repository, _engine, locks = isolated_runtime
    run_dir, marker_path, _record = write_run_and_marker(repository, locks)
    _other_dir, other_path, _other_record = write_run_and_marker(repository, locks, run_id="other-run", session_id="other-session")
    marker = locks.read_marker(task_id=None, session_id="test-session", repo=repository)
    assert locks.claim_bootstrap_replay_in_flight(marker, window_seconds=900)[0]
    claim_path = locks._bootstrap_replay_claim_path_for(marker_path)
    assert claim_path.exists()
    removed, kept = locks.clear_markers_belonging_to_handed_off_run(
        task_id=None, session_id="test-session", repo=repository, run_id="completed-run", run_dir=run_dir,
    )
    assert removed == [str(marker_path)] and kept == []
    assert not marker_path.exists() and not claim_path.exists() and other_path.exists()


def test_both_marker_keys_of_one_completed_run_are_reconciled_once(isolated_runtime, monkeypatch):
    repository, engine, locks = isolated_runtime
    run_dir, session_path, record = write_run_and_marker(repository, locks)
    _run_dir, task_path, _record = write_run_and_marker(repository, locks, task_id="same-task", session_id=None)
    monkeypatch.setenv("KANBAN_TASK_ID", "same-task")
    (run_dir / "artifacts/.finalize.lock").write_text(json.dumps(record))
    decision, events = stop_decision(repository, engine)
    assert decision is None and not task_path.exists() and not session_path.exists()
    assert len([e for e in events if e["event"] == "kanban_followup_lock_reconciled_for_already_finalized_run"]) == 1


def test_handoff_cleanup_survives_completion_notification_publish_failure(isolated_runtime, monkeypatch):
    repository, engine, locks = isolated_runtime
    run_dir, marker_path, _record = write_run_and_marker(repository, locks)
    finalizer = importlib.import_module("rvf_run_finalize")
    def fail_completion_publication(*args, **kwargs):
        raise OSError("测试通知发布失败")
    monkeypatch.setattr(finalizer, "publish_completion", fail_completion_publication)
    from core.run_ledger.run_ledger import start_run
    payload = {"continue": True}
    engine.complete_rvf_handoff_and_clear_in_flight_marker_for_stop_event(
        {"cwd": str(repository), "session_id": "test-session"}, start_run("test-publish-failure"),
        handoff_path=run_dir / "artifacts/handoff.md", payload=payload,
        cwd=str(repository), decision_kind="handoff",
    )
    assert (run_dir / "artifacts/.finalize.lock").is_file()
    assert not marker_path.exists() and payload == {"continue": True}


def test_claude_handoff_keeps_other_run_in_the_same_session(isolated_runtime, monkeypatch):
    repository, engine, locks = isolated_runtime
    run_dir, marker_path, record = write_run_and_marker(repository, locks)
    monkeypatch.setattr(engine, "finalize_for_handoff", lambda **kwargs: record)
    monkeypatch.setattr(engine, "surface_rvf_analyze_advisory", lambda **kwargs: None)
    persisted = json.loads(marker_path.read_text())
    marker_path.write_text(json.dumps({**persisted, "run_id": "another-run"}))
    from core.run_ledger.run_ledger import start_run
    engine.complete_rvf_handoff_and_clear_in_flight_marker_for_stop_event(
        {"cwd": str(repository), "session_id": "test-session"}, start_run("test-claude-handoff"),
        handoff_path=run_dir / "artifacts/handoff.md", payload={"continue": True},
        cwd=str(repository), decision_kind="handoff-advisory",
    )
    assert marker_path.exists()
