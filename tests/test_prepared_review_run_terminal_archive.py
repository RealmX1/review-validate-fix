"""准备轮次终止时的精确归档、等待及跨重启恢复。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _rvf_test_support.loader import load_script_module


def prepared_run(tmp_path, monkeypatch):
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "user-local"))
    (tmp_path / "repo").mkdir(exist_ok=True)
    run_directory = tmp_path / "rvf-review-start-0123456789abcdef"
    artifacts = run_directory / "artifacts"
    artifacts.mkdir(parents=True)
    (run_directory / "summary.json").write_text(json.dumps({
        "run_id": run_directory.name, "repo": str(tmp_path / "repo"),
        "run_dir": str(run_directory), "session_id": "parent-session",
    }))
    return run_directory


def prepare_bound_dispatcher_execution_before_terminal_intent(run_directory):
    evidence = load_script_module("dispatcher_writer_execution_evidence")
    dispatcher = load_script_module("dispatch_reviewers")
    summary = json.loads((run_directory / "summary.json").read_text())
    status_path = run_directory / "artifacts/reviewers/.dispatch-thread.status.json"
    entrypoint = Path(dispatcher.__file__)
    payload = {"kind": "dispatch", "run_id": run_directory.name, "run_dir": str(run_directory),
        "repo": summary["repo"], "session_id": summary["session_id"], "tmux_session": "bound-dispatch-session",
        "command": [sys.executable, str(entrypoint)], "returncode": None, "launch_status": "launched"}
    payload = evidence.prepare_dispatcher_writer_execution_evidence_binding(status_path, payload)
    payload = evidence.record_dispatcher_writer_execution_tmux_attempt_boundary(status_path, payload["dispatcher_writer_execution_id"])
    status_path.write_text(json.dumps(payload))
    return payload


def complete_previously_admitted_dispatcher_execution(run_directory, monkeypatch):
    evidence = load_script_module("dispatcher_writer_execution_evidence")
    status_path = run_directory / "artifacts/reviewers/.dispatch-thread.status.json"
    payload = json.loads(status_path.read_text())
    entrypoint = Path(payload["command"][1])
    with monkeypatch.context() as environment:
        environment.setenv(evidence.DISPATCHER_WRITER_EXECUTION_ID_ENVIRONMENT_VARIABLE, payload["dispatcher_writer_execution_id"])
        environment.setenv(evidence.DISPATCHER_WRITER_EXECUTION_EVIDENCE_PATH_ENVIRONMENT_VARIABLE, payload["dispatcher_writer_execution_evidence_path"])
        environment.setattr(sys, "argv", [str(entrypoint)])
        assert evidence.run_actual_dispatcher_with_writer_execution_evidence(lambda: 0, dispatcher_script_path=entrypoint) == 0


def test_aborted_preparation_is_archived_once_without_successful_review(tmp_path, monkeypatch):
    archive = load_script_module("review_run_terminal_archive")
    run_directory = prepared_run(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: calls.append(kwargs) or {
        "run_id": run_directory.name, "run_dir": str(run_directory), "decision_kind": kwargs["decision_kind"],
    })
    monkeypatch.setattr(archive, "surface_rvf_analyze_advisory", lambda **kwargs: None)
    result = archive.request_review_run_terminal_archive(
        run_dir=run_directory, decision_kind="aborted_before_review", reason="候选在派审前改变",
        event={"session_id": "parent-session"},
    )
    assert result["status"] == "archived"
    assert calls[0]["decision_kind"] == "aborted_before_review"
    assert calls[0]["event"]["review_run_terminal_observed_at"]
    archive.resume_pending_review_run_terminal_archives(repo=tmp_path / "repo", session_id="parent-session")
    assert len(calls) == 1


def test_live_dispatch_waits_for_evidence_and_restarts_without_duplicate_archive(tmp_path, monkeypatch):
    archive = load_script_module("review_run_terminal_archive")
    run_directory = prepared_run(tmp_path, monkeypatch)
    status_path = run_directory / "artifacts/reviewers/.dispatch-thread.status.json"
    status_path.parent.mkdir()
    status_path.write_text(json.dumps({"pid": os.getpid(), "returncode": None, "launch_status": "launched"}))
    prepare_bound_dispatcher_execution_before_terminal_intent(run_directory)
    calls = []
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: calls.append(kwargs) or {})
    monkeypatch.setattr(archive, "surface_rvf_analyze_advisory", lambda **kwargs: None)
    waiting = archive.request_review_run_terminal_archive(
        run_dir=run_directory, decision_kind="cancelled", reason="终止本轮",
        event={"session_id": "parent-session"},
    )
    assert waiting["status"] == "waiting_for_evidence"
    assert not calls
    complete_previously_admitted_dispatcher_execution(run_directory, monkeypatch)
    archive.resume_pending_review_run_terminal_archives(repo=tmp_path / "repo", session_id="foreign-session")
    assert not calls
    archive.resume_pending_review_run_terminal_archives(repo=tmp_path / "repo", session_id="parent-session")
    assert len(calls) == 1


def test_archive_retry_budget_is_durable_and_resets_only_after_evidence_changes(tmp_path, monkeypatch):
    archive = load_script_module("review_run_terminal_archive")
    run_directory = prepared_run(tmp_path, monkeypatch)
    transcript = tmp_path / "missing-transcript.jsonl"
    attempts = []

    def failing_archive(**kwargs):
        attempts.append(kwargs)
        raise OSError("归档写入暂时不可用")

    monkeypatch.setattr(archive, "finalize_run", failing_archive)
    archive.request_review_run_terminal_archive(run_dir=run_directory, decision_kind="aborted_before_review",
        reason="准备终止", event={"session_id": "parent-session", "transcript_path": str(transcript)})
    for _ in range(6):
        archive.resume_pending_review_run_terminal_archives(repo=tmp_path / "repo", session_id="parent-session")
    assert len(attempts) == 3
    transcript.write_text("{}\n")
    archive.resume_pending_review_run_terminal_archives(repo=tmp_path / "repo", session_id="parent-session")
    assert len(attempts) == 4


def test_preparation_root_is_durable_even_with_runtime_directory(tmp_path, monkeypatch):
    prep = load_script_module("rvf_prep_file")
    monkeypatch.delenv("CODEX_RVF_PREP_ROOT", raising=False)
    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "persistent-user-local"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "reboot-cleared-runtime"))
    assert prep.prep_root() == tmp_path / "persistent-user-local/runtime-state/prepared-review-run-inputs"


def test_expired_inputs_remain_available_for_cleanup_of_prepared_run(tmp_path, monkeypatch):
    prep = load_script_module("rvf_prep_file")
    run_directory = prepared_run(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    record = prep.write_prep_file({"target_worktree": str(tmp_path / "repo"), "rvf_run": {
        "run_id": run_directory.name, "run_dir": str(run_directory),
    }}, root=tmp_path / "prepared-inputs", now=now, ttl_seconds=1)
    expired_at = now + timedelta(seconds=2)
    assert prep.sweep_stale(root=record.path.parent, now=expired_at) == []
    assert prep.read_prep_file(record.token, root=record.path.parent, now=expired_at).status == "expired"
    assert record.path.is_file()


def test_cancelled_process_without_dispatch_status_must_exit_before_archiving(tmp_path, monkeypatch):
    archive = load_script_module("review_run_terminal_archive")
    run_directory = prepared_run(tmp_path, monkeypatch)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", run_directory.name])
    summary_path = run_directory / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["still_running_pids"] = [process.pid]
    summary_path.write_text(json.dumps(summary))
    calls = []
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: calls.append(kwargs) or {})
    monkeypatch.setattr(archive, "surface_rvf_analyze_advisory", lambda **kwargs: None)
    try:
        waiting = archive.request_review_run_terminal_archive(run_dir=run_directory,
            decision_kind="cancelled", reason="仍有取消未结束的写入进程")
        assert waiting["status"] == "waiting_for_evidence"
        assert not calls
    finally:
        process.terminate()
        process.wait(timeout=5)
    archive.resume_pending_review_run_terminal_archives(repo=tmp_path / "repo", session_id="parent-session")
    assert len(calls) == 1
    assert calls[0]["event"]["review_run_skip_live_workspace_snapshot"] is True


def test_stale_dispatch_status_recovers_from_bound_runtime_delivery(tmp_path, monkeypatch):
    archive = load_script_module("review_run_terminal_archive")
    detached = load_script_module("rvf_detached_thread")
    run_directory = prepared_run(tmp_path, monkeypatch)
    status = run_directory / "artifacts/reviewers/.dispatch-thread.status.json"
    status.parent.mkdir()
    status.write_text(json.dumps({"returncode": None, "launch_status": "launched",
                                 "tmux_session": "bound-dispatch-session", "pid": os.getpid()}))
    prepare_bound_dispatcher_execution_before_terminal_intent(run_directory)
    monkeypatch.setattr(detached, "_tmux_session_alive", lambda session: False)
    calls = []
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: calls.append(kwargs) or {})
    monkeypatch.setattr(archive, "surface_rvf_analyze_advisory", lambda **kwargs: None)
    result = archive.request_review_run_terminal_archive(run_dir=run_directory,
        decision_kind="interrupted", reason="原生退出回写前进程已退出")
    assert result["status"] == "waiting_for_evidence"
    assert not calls
    complete_previously_admitted_dispatcher_execution(run_directory, monkeypatch)
    result = archive.resume_pending_review_run_terminal_archives(repo=tmp_path / "repo", session_id="parent-session")[0]
    assert result["status"] == "archived"
    assert len(calls) == 1


def test_partial_finalization_retries_only_failed_work_and_preserves_original_failure(tmp_path, monkeypatch):
    archive = load_script_module("review_run_terminal_archive")
    finalize = load_script_module("rvf_run_finalize")
    run_directory = prepared_run(tmp_path, monkeypatch)
    captures, releases = [], []
    def capture(**kwargs):
        captures.append(kwargs)
        if len(captures) == 1:
            raise OSError("转录暂时不可读")
        return {"post_rvf_source_kind": "same-session-slice"}
    monkeypatch.setattr(finalize, "capture_run", capture)
    monkeypatch.setattr(finalize, "_release_tracker_lease", lambda *args, **kwargs: releases.append(kwargs) or {})
    monkeypatch.setattr(finalize, "_write_usage_summary", lambda *args: {})
    monkeypatch.setattr(finalize, "_scaffold_analysis", lambda *args: {})
    monkeypatch.setattr(archive, "surface_rvf_analyze_advisory", lambda **kwargs: None)
    first = archive.request_review_run_terminal_archive(run_dir=run_directory,
        decision_kind="aborted_before_review", reason="准备轮次终止")
    assert first["status"] == "waiting_for_evidence"
    recovered = archive.resume_pending_review_run_terminal_archives(repo=tmp_path / "repo", session_id="parent-session")
    assert recovered[0]["status"] == "archived"
    assert len(captures) == 2
    assert len(releases) == 1
    histories = list((run_directory / "artifacts/terminal-archive-finalization-attempts").glob("*.json"))
    assert any(json.loads(path.read_text()).get("errors") for path in histories)


def test_analysis_recovers_after_bound_transcript_returns_without_repeating_archive(tmp_path, monkeypatch):
    archive = load_script_module("review_run_terminal_archive")
    run_directory = prepared_run(tmp_path, monkeypatch)
    transcript = tmp_path / "bound-transcript.jsonl"
    finalizations, analyses = [], []
    for variable in ("RVF_SUPPRESS_STOP_HOOK", "RVF_ANALYZE_THREAD"):
        monkeypatch.delenv(variable, raising=False)
    def finalize(**kwargs):
        finalizations.append(kwargs)
        result = {"run_dir": str(run_directory), "analysis": {"summary_md_path": "summary.md", "causality_json_path": "causality.json"}}
        path = run_directory / "summary.json"
        summary = json.loads(path.read_text())
        summary["finalize"] = result
        path.write_text(json.dumps(summary))
        return result
    monkeypatch.setattr(archive, "finalize_run", finalize)
    monkeypatch.setattr(archive, "surface_rvf_analyze_advisory", lambda **kwargs: analyses.append(kwargs))
    result = archive.request_review_run_terminal_archive(run_dir=run_directory, decision_kind="declined",
        reason="保留终态，等待原生转录", event={"transcript_path": str(transcript)})
    assert result["status"] == "archived"
    assert result["analysis_recovery_condition"]
    assert not analyses
    transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": "parent-session"}}) + "\n")
    monkeypatch.setenv("RVF_SUPPRESS_STOP_HOOK", "1")
    archive.resume_pending_review_run_terminal_archives(repo=tmp_path / "repo", session_id="parent-session")
    assert not analyses
    monkeypatch.delenv("RVF_SUPPRESS_STOP_HOOK")
    archive.resume_pending_review_run_terminal_archives(repo=tmp_path / "repo", session_id="parent-session")
    assert len(analyses) == 1
    assert len(finalizations) == 1


def test_concurrent_terminal_finalizers_execute_side_effects_once(tmp_path, monkeypatch):
    finalize = load_script_module("rvf_run_finalize")
    run_directory = prepared_run(tmp_path, monkeypatch)
    entered, release = Event(), Event()
    captures = []
    def capture(**kwargs):
        captures.append(kwargs)
        entered.set()
        assert release.wait(timeout=5)
        return {}
    monkeypatch.setattr(finalize, "capture_run", capture)
    monkeypatch.setattr(finalize, "_release_tracker_lease", lambda *args, **kwargs: {})
    monkeypatch.setattr(finalize, "_write_usage_summary", lambda *args: {})
    monkeypatch.setattr(finalize, "_scaffold_analysis", lambda *args: {})
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(finalize.finalize_run, run_dir=run_directory, decision_kind="cancelled")
        assert entered.wait(timeout=5)
        second = executor.submit(finalize.finalize_run, run_dir=run_directory, decision_kind="cancelled")
        release.set()
        results = [first.result(timeout=5), second.result(timeout=5)]
    assert len(captures) == 1
    assert sum(bool(result.get("already_finalized")) for result in results) == 1


def test_manual_review_quiet_marker_does_not_hide_independent_archive_recovery(tmp_path, monkeypatch):
    engine = load_script_module("stop_review_validate_fix")
    archive = load_script_module("review_run_terminal_archive")
    from core.run_ledger.run_ledger import RunLedger

    monkeypatch.setenv("RVF_USER_LOCAL_DATA_ROOT", str(tmp_path / "isolated-user-local"))
    for variable in ("RVF_ANALYZE_THREAD", "RVF_SUPPRESS_STOP_HOOK"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(engine, "awaiting_dispatched_agent_decision", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "kanban_followup_in_progress_decision", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "session_hook_control_decision", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "manual_rvf_session_marker_payload", lambda *args: {"continue": True, "systemMessage": "本轮已手动审查"})
    monkeypatch.setattr(engine, "should_suppress", lambda *args: False)
    restored = []
    monkeypatch.setattr(archive, "resume_pending_review_run_terminal_archives",
                        lambda **kwargs: restored.append(kwargs) or [{"status": "waiting_for_evidence"}])
    ledger = RunLedger(component="stop-hook", run_id="stop-test", run_dir=tmp_path / "stop-test")
    decision = engine.evaluate_stop_event({"cwd": str(tmp_path), "session_id": "parent-session",
                                          "last_user_message": "工作已完成"}, ledger)
    assert len(restored) == 1
    assert decision.reason_code == "manual_rvf_already_ran"
    assert not decision.payload.get("decision")


def test_missing_writer_evidence_leaves_durable_request_and_recovers(tmp_path, monkeypatch):
    archive = load_script_module("review_run_terminal_archive")
    run_directory = prepared_run(tmp_path, monkeypatch)
    def unavailable_writer_state(*args):
        raise OSError("tracker 暂时不可读")
    monkeypatch.setattr(archive, "_writers_still_active", unavailable_writer_state)
    first = archive.request_review_run_terminal_archive(run_dir=run_directory,
        decision_kind="interrupted", reason="等待写入者证据恢复")
    assert first["status"] == "waiting_for_evidence"
    assert (run_directory / "artifacts/review-run-terminal-archive-request.json").is_file()
    calls = []
    monkeypatch.setattr(archive, "_writers_still_active", lambda *args: False)
    monkeypatch.setattr(archive, "finalize_run", lambda **kwargs: calls.append(kwargs) or {})
    archive.resume_pending_review_run_terminal_archives(repo=tmp_path / "repo", session_id="parent-session")
    assert len(calls) == 1
